"""Adaptive resource limits: derive per-story dev cost estimates and review caps.

Pure-Python (stdlib only). Given preflight complexity and model capability
profiles, compute per-story dev ``max_iterations``, ``timeout_seconds``, and a
per-story dollar ``cost_estimate_usd``. Review-cycle caps continue to use the
existing complexity plus recent audit-history signal.

The per-story dollar value is an *estimate* derived from historical cost data —
it informs routing, timeout scaling, and audit telemetry. It is NOT an enforced
budget: real post-hoc dollar governance lives at the sprint level
(``forge.yaml: budget_usd``). Nothing here caps or blocks spend after the fact.
The sprint cap does bind mid-story — it is charged the spend of stories still
running and cancels them at the next phase boundary once it is met (#2547) —
but that decision is the sprint's, made against measured spend, never against
an estimate derived here.

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

from theforge.coordinator.story_budget import (
    BAND_HEADROOM as _ALLOCATION_HEADROOM,
)
from theforge.coordinator.story_budget import (
    DEV_ESTIMATE_HEADROOM_BASIS_ALLOCATION as ESTIMATE_HEADROOM_BASIS_ALLOCATION,
)
from theforge.coordinator.story_budget import (
    DEV_ESTIMATE_SCOPE_DEV_PHASE as ESTIMATE_SCOPE_DEV_PHASE,
)
from theforge.coordinator.story_budget import (
    DEV_ESTIMATE_SOURCE_BAND as ESTIMATE_SOURCE_BAND,
)
from theforge.coordinator.story_budget import (
    DEV_ESTIMATE_SOURCE_CONFIGURED as ESTIMATE_SOURCE_CONFIGURED,
)
from theforge.coordinator.story_budget import (
    DEV_ESTIMATE_SOURCE_SCORE as ESTIMATE_SOURCE_SCORE,
)

if TYPE_CHECKING:
    from theforge.config.types import RetryPolicy

# Cap on how many recent records we scan; prevents unbounded I/O on
# long-lived projects while still giving ~a sprint's worth of signal.
_HISTORY_TAIL = 50

_BAND_TO_SCORE = {"small": 2, "medium": 5, "large": 9}
_HEADROOM_FACTOR = 1.5
# Estimate headroom is scaled by complexity band because strong-tier models on
# large stories have higher cost variance than cheap-tier models on small ones.
# Using a flat 1.5x average produced estimates ~2x too tight for LARGE stories.
_ESTIMATE_HEADROOM_BY_BAND: dict[str, float] = {"small": 1.5, "medium": 2.0, "large": 2.5}
_MIN_PROFILE_RUNS = 3

# Where a dollar estimate came from. Only ``DEV_ESTIMATE_SOURCE_SCORE``
# describes the same population as the per-story allocation (see
# ``story_budget.derive_story_allocation``): one complexity score, all models.
# The band-and-model estimate summarises a wider, differently-priced set of
# runs, and the configured fallback summarises nothing observed at all — both
# are recorded for audit but neither may be subtracted from the allocation.
# ``ESTIMATE_SCOPE_DEV_PHASE`` records that this figure prices the dev phase
# while the allocation prices the whole story: same population by score,
# different phase scope, so the audit never implies one measurement.


@dataclass(frozen=True)
class AdaptiveLimits:
    """Derived per-story iteration budget plus audit breadcrumbs."""

    dev_max: int
    review_max: int
    dev_timeout_seconds: int
    # Per-story dollar cost ESTIMATE (historical-cost derived), not an enforced
    # budget. Used for routing/timeout scaling/telemetry; never caps spend.
    dev_cost_estimate_usd: float
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
    relevant for adaptive iteration learning, so sprint-level records
    are filtered out.

    A truly fresh repo (no substrate *and* no other audit inputs) yields
    an empty list — adaptive iteration falls back to complexity-only
    scaling. Otherwise ``require_substrate`` is the source of truth:
    a missing substrate with audit inputs on disk surfaces
    ``SubstrateMissingError`` (operator-facing), a corrupt index surfaces
    ``SubstrateCorruptError``, and a substrate still held by a concurrent
    sibling worker after the bounded lock wait surfaces
    ``SubstrateLockTimeoutError`` (#2906). None of the three is caught here —
    silent no-history routing is exactly what the spec forbids. The lock case
    is transient where the other two are not, but the difference is drawn at
    the sprint worker boundary, which attributes it to shared infrastructure;
    swallowing it here would route the story without history instead.
    """
    from theforge.coordinator import audit_substrate

    sub_path = audit_substrate.substrate_path(project_root)
    if not sub_path.exists() and not audit_substrate.has_audit_inputs(project_root):
        return []
    conn = audit_substrate.require_substrate(project_root)
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


def _configured_estimate_basis(
    *,
    reason: str,
    complexity_band: str | None,
    complexity_score: int | None = None,
) -> dict:
    """Basis for an estimate that is the configured budget, not an observation."""
    return {
        "source": ESTIMATE_SOURCE_CONFIGURED,
        "band": complexity_band,
        "complexity_score": complexity_score,
        "statistic": "configured_budget_usd",
        "scope": ESTIMATE_SCOPE_DEV_PHASE,
        "sample_count": 0,
        "headroom_multiplier": None,
        "headroom_basis": None,
        "allocation_comparable": False,
        "reason": reason,
    }


def _band_estimate_basis(
    *,
    complexity_band: str | None,
    complexity_score: int | None,
    headroom: float,
    sample_count: int,
    reason: str,
) -> dict:
    """Basis for the band-and-model estimate: audit-visible, not comparable.

    A band spans several scores and the model filter narrows it to one
    performer, so this average is drawn from a different and generally more
    expensive population than the score-scoped allocation. It still sizes
    nothing but itself here; ``allocation_comparable`` false is what stops it
    being subtracted from the allocation at seating.
    """
    return {
        "source": ESTIMATE_SOURCE_BAND,
        "band": complexity_band,
        "complexity_score": complexity_score,
        "statistic": "avg_cost_usd",
        "scope": ESTIMATE_SCOPE_DEV_PHASE,
        "sample_count": int(sample_count),
        "headroom_multiplier": headroom,
        "headroom_basis": "band_headroom",
        "allocation_comparable": False,
        "reason": reason,
    }


def _score_estimate_basis(
    *,
    complexity_band: str | None,
    complexity_score: int,
    sample_count: int,
) -> dict:
    """Basis for the score-scoped estimate — the one seating may subtract."""
    return {
        "source": ESTIMATE_SOURCE_SCORE,
        "band": complexity_band,
        "complexity_score": int(complexity_score),
        "statistic": "avg_cost_usd",
        "scope": ESTIMATE_SCOPE_DEV_PHASE,
        "sample_count": int(sample_count),
        "headroom_multiplier": _ALLOCATION_HEADROOM,
        "headroom_basis": ESTIMATE_HEADROOM_BASIS_ALLOCATION,
        "allocation_comparable": True,
        "reason": "profile_score_history",
    }


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
    base_cost_estimate_usd: float,
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
        "base_dev_cost_estimate_usd": base_cost_estimate_usd,
        "static_dev_max": static_dev_max,
        "estimate_headroom_factor": None,
    }

    if not retry_policy.adaptive_iterations:
        audit["rationale"] = "adaptive_iterations disabled; using static configured limits"
        audit["dev_cost_estimate_basis"] = _configured_estimate_basis(
            reason="adaptive_iterations_disabled",
            complexity_band=complexity_band,
        )
        return AdaptiveLimits(
            dev_max=static_dev_max,
            review_max=floor_review,
            dev_timeout_seconds=base_timeout_seconds,
            dev_cost_estimate_usd=base_cost_estimate_usd,
            audit=audit,
        )

    score = _score_from_inputs(complexity_score, complexity_band)
    audit["complexity_score_used"] = score

    if score is None:
        audit["rationale"] = "no complexity score available; using static configured limits"
        audit["dev_cost_estimate_basis"] = _configured_estimate_basis(
            reason="no_complexity_score",
            complexity_band=complexity_band,
        )
        return AdaptiveLimits(
            dev_max=static_dev_max,
            review_max=floor_review,
            dev_timeout_seconds=base_timeout_seconds,
            dev_cost_estimate_usd=base_cost_estimate_usd,
            audit=audit,
        )

    # Review-cycle adaptation stays on the existing complexity/history path.
    base_review = _scale_to_band(score, floor_review, cap_review)
    audit["base_review"] = base_review

    profile_stats = None
    if model_profiles:
        from theforge.model_profiles_read_model import get_dev_complexity_stats  # noqa: PLC0415

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

    # Cost history at this story's own complexity score, across all models —
    # the same population the per-story allocation is drawn from. Read
    # independently of the band/model stats above, which keep sizing iterations
    # and wall-clock.
    score_cost_stats = None
    if model_profiles:
        from theforge.model_profiles_read_model import get_dev_score_cost_stats  # noqa: PLC0415

        score_cost_stats = get_dev_score_cost_stats(
            model_profiles, score, min_runs=_MIN_PROFILE_RUNS
        )
    score_runs = int(score_cost_stats["runs"]) if score_cost_stats is not None else 0
    audit["score_cost_history_runs"] = score_runs
    audit["score_cost_avg_usd"] = (
        round(float(score_cost_stats["avg_cost_usd"]), 6) if score_cost_stats is not None else None
    )

    if profile_stats is None:
        chosen_dev = static_dev_max
        chosen_timeout = base_timeout_seconds
        chosen_estimate = base_cost_estimate_usd
        dev_rationale = (
            "insufficient profile history for complexity band; using static configured dev limits"
        )
        audit["dev_cost_estimate_basis"] = _configured_estimate_basis(
            reason="insufficient_profile_history",
            complexity_band=complexity_band,
            complexity_score=score,
        )
    else:
        raw_dev = profile_stats["avg_iterations"] * _HEADROOM_FACTOR
        chosen_dev = max(floor_dev, min(cap_dev, _ceil_int(raw_dev)))
        timeout_per_iteration = base_timeout_seconds / max(static_dev_max, 1)
        iteration_timeout = _ceil_int(chosen_dev * timeout_per_iteration)
        # An adaptively-learned wall-clock limit must follow from observations of
        # the wall-clock it bounds, not from an iteration count scaled by a static
        # per-iteration baseline. Floor the iteration-derived value by:
        #   - observed completed-run duration + headroom (success must never make
        #     the granted time shrink below what success was observed to need), and
        #   - the max limit that killed comparable runs (a censored observation
        #     can never drive the limit below the value at which it was killed), and
        #   - the operator-configured base timeout (now a hard floor).
        observed_floor = _ceil_int(profile_stats.get("max_duration_s", 0.0) * _HEADROOM_FACTOR)
        kill_floor = _ceil_int(profile_stats.get("max_killed_timeout_s", 0.0))
        # ``_ceil_int`` floors at 1; a genuine no-data signal is 0, so drop the
        # floor to 0 when the underlying observation is absent.
        if not profile_stats.get("max_duration_s", 0.0):
            observed_floor = 0
        if not profile_stats.get("max_killed_timeout_s", 0.0):
            kill_floor = 0
        chosen_timeout = max(iteration_timeout, observed_floor, kill_floor, base_timeout_seconds)
        _estimate_headroom = _ESTIMATE_HEADROOM_BY_BAND.get(
            (complexity_band or "").lower(), _HEADROOM_FACTOR
        )
        chosen_estimate = _round_money(profile_stats["avg_cost_usd"] * _estimate_headroom)
        audit["profile_raw_dev_max"] = round(raw_dev, 4)
        audit["estimate_headroom_factor"] = _estimate_headroom
        audit["dev_cost_estimate_basis"] = _band_estimate_basis(
            complexity_band=complexity_band,
            complexity_score=score,
            headroom=_estimate_headroom,
            sample_count=profile_runs,
            reason="profile_history",
        )
        audit["iteration_derived_timeout_seconds"] = iteration_timeout
        audit["profile_max_duration_s"] = round(float(profile_stats.get("max_duration_s", 0.0)), 4)
        audit["profile_max_killed_timeout_s"] = round(
            float(profile_stats.get("max_killed_timeout_s", 0.0)), 4
        )
        audit["duration_floor_seconds"] = observed_floor
        audit["kill_floor_seconds"] = kill_floor
        floored_on_duration = observed_floor > iteration_timeout and observed_floor >= kill_floor
        floored_on_kill = kill_floor > iteration_timeout and kill_floor > observed_floor
        audit["timeout_floored_on_observation"] = chosen_timeout > iteration_timeout
        dev_rationale = (
            f"derived dev limits from {profile_runs} "
            f"{complexity_band or 'unknown'}-band profile runs with "
            f"{_HEADROOM_FACTOR}x iteration headroom and {_estimate_headroom}x budget headroom"
        )
        if floored_on_kill:
            dev_rationale += (
                f"; timeout floored on the {kill_floor}s limit that killed comparable runs."
            )
        elif floored_on_duration:
            dev_rationale += (
                f"; timeout floored on observed run duration+headroom ({observed_floor}s)."
            )
        elif chosen_timeout > iteration_timeout:
            dev_rationale += (
                f"; timeout floored on the operator-configured base ({base_timeout_seconds}s)."
            )
        else:
            dev_rationale += "; timeout scaled from static per-iteration baseline."

    # The dollar estimate — and only the dollar estimate — prefers the
    # score-scoped average. Iterations and timeout above stay on the band and
    # model stats: those size the work the chosen model does. The dollar figure
    # is reconciled against a score-scoped allocation at seating, so sourcing it
    # from a wider band (or from one expensive model) makes the subtraction
    # meaningless and lets the dev phase consume the budget of every phase
    # after it (#2284).
    if score_cost_stats is not None:
        chosen_estimate = _round_money(score_cost_stats["avg_cost_usd"] * _ALLOCATION_HEADROOM)
        audit["estimate_headroom_factor"] = _ALLOCATION_HEADROOM
        audit["dev_cost_estimate_basis"] = _score_estimate_basis(
            complexity_band=complexity_band,
            complexity_score=score,
            sample_count=score_runs,
        )
        dev_rationale += (
            f" Dev cost estimate from {score_runs} score-{score} run(s) across all models "
            f"(avg ${float(score_cost_stats['avg_cost_usd']):.2f} x {_ALLOCATION_HEADROOM}x "
            "allocation headroom) — the population the story allocation is drawn from."
        )
    else:
        dev_rationale += (
            f" Dev cost estimate is not comparable with the story allocation "
            f"(no score-{score} cost history); seating will not subtract it."
        )

    audit["chosen_dev_max"] = chosen_dev
    audit["chosen_dev_timeout_seconds"] = chosen_timeout
    audit["chosen_dev_cost_estimate_usd"] = round(chosen_estimate, 4)

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
        dev_cost_estimate_usd=chosen_estimate,
        audit=audit,
    )
