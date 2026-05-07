"""Adaptive resource limits: derive per-story dev budgets and review-cycle caps.

Pure-Python (stdlib only). Given preflight complexity and model capability
profiles, compute per-story dev ``max_iterations``, ``timeout_seconds``, and
``budget_usd``. Review-cycle caps continue to use the existing complexity plus
recent audit-history signal.

Design:

- ``retry.max_dev_iterations`` / ``retry.max_review_cycles`` act as the floor
  (never grant fewer iterations than the operator configured).
- ``retry.max_dev_iterations_cap`` / ``retry.max_review_cycles_cap`` are the
  hard ceiling (safety rail) — adaptive growth never exceeds them.
- Dev limits are learned from per-complexity model profile averages with a
  deterministic headroom factor.
- Review-cycle caps keep the existing complexity + recent-history uplift path.
- Deterministic: same inputs always yield the same limits.
- Fallback: insufficient profile history → static configured dev limits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from theforge.config.types import RetryPolicy

# Cap on how many recent records we scan; prevents unbounded I/O on
# long-lived projects while still giving ~a sprint's worth of signal.
_HISTORY_TAIL = 50

_BAND_TO_SCORE = {"small": 2, "medium": 5, "large": 9}
_HEADROOM_FACTOR = 1.5
# Budget headroom is scaled by complexity band because strong-tier models on
# large stories have higher cost variance than cheap-tier models on small ones.
# Using a flat 1.5x average produced budgets ~2x too tight for LARGE stories.
_BUDGET_HEADROOM_BY_BAND: dict[str, float] = {"small": 1.5, "medium": 2.0, "large": 2.5}
_MIN_PROFILE_RUNS = 3


@dataclass(frozen=True)
class AdaptiveLimits:
    """Derived per-story iteration budget plus audit breadcrumbs."""

    dev_max: int
    review_max: int
    dev_timeout_seconds: int
    dev_budget_usd: float
    audit: dict = field(default_factory=dict)


def _score_from_inputs(score: int | None, band: str | None) -> int | None:
    if score is not None and 1 <= int(score) <= 10:
        return int(score)
    if band:
        return _BAND_TO_SCORE.get(band.lower())
    return None


def _scale_to_band(score: int, floor: int, cap: int) -> int:
    """Linear interpolation: score=1 → floor, score=10 → cap (rounded up)."""
    if cap <= floor:
        return floor
    # Fraction of the (cap - floor) range allocated at this score.
    frac = (score - 1) / 9  # score 1..10 → 0.0..1.0
    frac = max(0.0, min(1.0, frac))
    return floor + math.ceil(frac * (cap - floor))


def _round_money(value: float) -> float:
    return round(max(value, 0.01), 4)


def _ceil_int(value: float) -> int:
    return max(1, int(math.ceil(value)))


def _read_history_tail(project_root: Path) -> list[dict]:
    """Return up to ``_HISTORY_TAIL`` recent story-level audit records.

    Reads the SQLite audit substrate ordered by ``started_at`` DESC. Only
    story-level records (those carrying an ``iterations`` block) are
    relevant for adaptive iteration learning, so sprint-level records are
    filtered out. A missing or corrupt substrate yields an empty list —
    adaptive iteration falls back to complexity-only scaling.
    """
    from theforge.coordinator import audit_substrate

    try:
        conn = audit_substrate.require_substrate(project_root)
    except (audit_substrate.SubstrateMissingError, audit_substrate.SubstrateCorruptError):
        return []
    try:
        # Pull extra so sprint-level rows (no iterations block) can be filtered.
        candidates = audit_substrate.tail_records(conn, _HISTORY_TAIL * 3)
    finally:
        conn.close()
    records: list[dict] = []
    for rec in candidates:
        if not isinstance(rec, dict):
            continue
        if "iterations" not in rec:
            continue
        records.append(rec)
    # tail_records is DESC; preserve previous semantics (callers expect
    # the last N items in chronological order).
    records.reverse()
    return records[-_HISTORY_TAIL:]


def _extract_record_score(rec: dict) -> int | None:
    pf = rec.get("preflight") or {}
    if not isinstance(pf, dict):
        return None
    score = pf.get("complexity_score")
    if isinstance(score, int) and 1 <= score <= 10:
        return score
    band = pf.get("complexity")
    if isinstance(band, str):
        return _BAND_TO_SCORE.get(band.lower())
    return None


def _extract_dev_used(rec: dict) -> int | None:
    it = rec.get("iterations") or {}
    if not isinstance(it, dict):
        return None
    # Prefer the most specific field; fall back progressively.
    for key in ("dev_iterations_productive", "dev_iterations", "dev_attempts_total"):
        val = it.get(key)
        if isinstance(val, int) and val > 0:
            return val
    return None


def _extract_review_used(rec: dict) -> int | None:
    it = rec.get("iterations") or {}
    if not isinstance(it, dict):
        return None
    for key in ("review_cycles_total", "review_cycles"):
        val = it.get(key)
        if isinstance(val, int) and val > 0:
            return val
    return None


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # Nearest-rank method — deterministic, no float surprises.
    idx = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def derive_limits(
    complexity_score: int | None,
    complexity_band: str | None,
    retry_policy: "RetryPolicy",
    *,
    model_name: str,
    model_actual: str | None = None,
    model_provider: str | None = None,
    model_cli: str | None = None,
    base_timeout_seconds: int,
    base_budget_usd: float,
    static_dev_max: int,
    review_history_path: Path | None,  # repurposed: project_root (None disables history)
    model_profiles: dict | None,
) -> AdaptiveLimits:
    """Compute per-story adaptive limits.

    Returns an :class:`AdaptiveLimits` with the chosen maximums and an audit
    dict describing the inputs used, profile sample size, the learned averages,
    any review-history uplift, and the final chosen limits. When adaptive
    derivation is disabled on the policy, returns static config values verbatim.
    """
    floor_dev = max(1, retry_policy.max_dev_iterations)
    floor_review = max(1, retry_policy.max_review_cycles)
    cap_dev = max(floor_dev, retry_policy.max_dev_iterations_cap)
    cap_review = max(floor_review, retry_policy.max_review_cycles_cap)

    audit: dict = {
        "enabled": retry_policy.adaptive_iterations,
        "complexity_score_input": complexity_score,
        "complexity_band_input": complexity_band,
        "floor_dev": floor_dev,
        "floor_review": floor_review,
        "cap_dev": cap_dev,
        "cap_review": cap_review,
        "model_name": model_name,
        "model_actual": model_actual,
        "model_provider": model_provider,
        "model_cli": model_cli,
        "headroom_factor": _HEADROOM_FACTOR,
        "min_profile_runs": _MIN_PROFILE_RUNS,
        "base_timeout_seconds": base_timeout_seconds,
        "base_budget_usd": base_budget_usd,
        "static_dev_max": static_dev_max,
    }

    if not retry_policy.adaptive_iterations:
        audit["rationale"] = "adaptive_iterations disabled; using static configured limits"
        return AdaptiveLimits(
            dev_max=static_dev_max,
            review_max=floor_review,
            dev_timeout_seconds=base_timeout_seconds,
            dev_budget_usd=base_budget_usd,
            audit=audit,
        )

    score = _score_from_inputs(complexity_score, complexity_band)
    audit["complexity_score_used"] = score

    if score is None:
        audit["rationale"] = "no complexity score available; using static configured limits"
        return AdaptiveLimits(
            dev_max=static_dev_max,
            review_max=floor_review,
            dev_timeout_seconds=base_timeout_seconds,
            dev_budget_usd=base_budget_usd,
            audit=audit,
        )

    # Review-cycle adaptation stays on the existing complexity/history path.
    base_review = _scale_to_band(score, floor_review, cap_review)
    audit["base_review"] = base_review

    profile_stats = None
    if model_profiles:
        from theforge.model_profiles import get_dev_complexity_stats  # noqa: PLC0415

        profile_stats = get_dev_complexity_stats(
            model_profiles,
            model_name,
            complexity_band,
            min_runs=_MIN_PROFILE_RUNS,
            actual_model=model_actual,
            provider=model_provider,
            cli=model_cli,
        )
    profile_runs = int(profile_stats["runs"]) if profile_stats is not None else 0
    audit["profile_history_runs"] = profile_runs
    audit["profile_avg_iterations"] = (
        round(float(profile_stats["avg_iterations"]), 4) if profile_stats is not None else None
    )
    audit["profile_avg_cost_usd"] = (
        round(float(profile_stats["avg_cost_usd"]), 6) if profile_stats is not None else None
    )

    if profile_stats is None:
        chosen_dev = static_dev_max
        chosen_timeout = base_timeout_seconds
        chosen_budget = base_budget_usd
        dev_rationale = (
            "insufficient profile history for complexity band; using static configured dev limits"
        )
    else:
        raw_dev = profile_stats["avg_iterations"] * _HEADROOM_FACTOR
        chosen_dev = max(floor_dev, min(cap_dev, _ceil_int(raw_dev)))
        timeout_per_iteration = base_timeout_seconds / max(static_dev_max, 1)
        chosen_timeout = _ceil_int(chosen_dev * timeout_per_iteration)
        _budget_headroom = _BUDGET_HEADROOM_BY_BAND.get(
            (complexity_band or "").lower(), _HEADROOM_FACTOR
        )
        chosen_budget = _round_money(profile_stats["avg_cost_usd"] * _budget_headroom)
        audit["profile_raw_dev_max"] = round(raw_dev, 4)
        audit["budget_headroom_factor"] = _budget_headroom
        dev_rationale = (
            f"derived dev limits from {profile_runs} "
            f"{complexity_band or 'unknown'}-band profile runs with "
            f"{_HEADROOM_FACTOR}x iteration headroom and {_budget_headroom}x "
            "budget headroom; timeout scaled from static per-iteration baseline."
        )
    audit["chosen_dev_max"] = chosen_dev
    audit["chosen_dev_timeout_seconds"] = chosen_timeout
    audit["chosen_dev_budget_usd"] = round(chosen_budget, 4)

    # Review history: p75 of matching-complexity runs; records within ±1 of the score.
    history_sample = 0
    p75_review = 0
    if review_history_path is not None:
        recs = _read_history_tail(review_history_path)
        matching_review: list[int] = []
        for rec in recs:
            rec_score = _extract_record_score(rec)
            if rec_score is None or abs(rec_score - score) > 1:
                continue
            r = _extract_review_used(rec)
            if r is not None:
                matching_review.append(r)
        history_sample = len(matching_review)
        p75_review = _percentile(matching_review, 75)
    audit["review_history_sample_size"] = history_sample
    audit["p75_review"] = p75_review

    # Review limit remains bounded by the existing complexity-derived base.
    raw_review = max(base_review, p75_review + 1 if p75_review > 0 else 0)
    chosen_review = max(floor_review, min(cap_review, raw_review))
    audit["chosen_review_max"] = chosen_review
    if history_sample > 0 and raw_review > base_review:
        review_note = f" review history raised review_max to {chosen_review}."
    elif history_sample > 0:
        review_note = " review history stayed within the complexity-derived review base."
    else:
        review_note = " no matching review history; using complexity-derived review limit."
    audit["rationale"] = f"{dev_rationale}{review_note}"

    return AdaptiveLimits(
        dev_max=chosen_dev,
        review_max=chosen_review,
        dev_timeout_seconds=chosen_timeout,
        dev_budget_usd=chosen_budget,
        audit=audit,
    )
