"""Adaptive iteration limits: derive per-story dev/review budgets.

Pure-Python (stdlib only). Given a preflight complexity score (1-10) and the
tail of ``.forge/audits/history.jsonl`` produced by prior runs, compute
per-story ``max_dev_iterations`` / ``max_review_cycles``.

Design:

- ``retry.max_dev_iterations`` / ``retry.max_review_cycles`` act as the floor
  (never grant fewer iterations than the operator configured).
- ``retry.max_dev_iterations_cap`` / ``retry.max_review_cycles_cap`` are the
  hard ceiling (safety rail) — adaptive growth never exceeds them.
- Base grant scales with the 1-10 complexity score: higher score → more budget,
  interpolated linearly between floor and cap.
- Historical p75 usage for matching complexity (within ±1 of the score) bumps
  the base when history shows recent runs at similar complexity ran long.
- Deterministic: same inputs always yield the same limits.
- Fallback: no score + no history → floor values from RetryPolicy verbatim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from theforge.config.types import RetryPolicy

# Cap on how many recent history records we scan; prevents unbounded I/O on
# long-lived projects while still giving ~a sprint's worth of signal.
_HISTORY_TAIL = 50

# Skip adaptive history read entirely for unusually large history files;
# fall back to complexity-only scaling with a warning annotation.
_HISTORY_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB

_BAND_TO_SCORE = {"small": 2, "medium": 5, "large": 9}


@dataclass(frozen=True)
class AdaptiveLimits:
    """Derived per-story iteration budget plus audit breadcrumbs."""

    dev_max: int
    review_max: int
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
    import math

    return floor + math.ceil(frac * (cap - floor))


def _read_history_tail(history_path: Path) -> list[dict]:
    """Return the last _HISTORY_TAIL parseable JSON records.

    Only story-level audit records are useful. Records missing both a
    complexity score and iteration totals are skipped. Malformed lines are
    tolerated (JSONL gracefully degrades).
    """
    if not history_path.exists():
        return []
    try:
        size = history_path.stat().st_size
    except OSError:
        return []
    if size > _HISTORY_MAX_BYTES:
        return []
    try:
        with open(history_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    records: list[dict] = []
    for raw in lines[-_HISTORY_TAIL * 3 :]:  # grab extra so sprint-level lines can be filtered
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        # Only keep story-level audit records (they have an 'iterations' block).
        if "iterations" not in rec:
            continue
        records.append(rec)
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
    history_path: Path | None,
) -> AdaptiveLimits:
    """Compute per-story iteration limits.

    Returns an :class:`AdaptiveLimits` with the chosen maximums and an audit
    dict describing the inputs used, historical sample size, the p75 values
    observed, and the final chosen limits. When ``adaptive_iterations`` is
    disabled on the policy, returns the policy floors verbatim.
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
    }

    if not retry_policy.adaptive_iterations:
        audit["rationale"] = "adaptive_iterations disabled; using policy floors"
        return AdaptiveLimits(dev_max=floor_dev, review_max=floor_review, audit=audit)

    score = _score_from_inputs(complexity_score, complexity_band)
    audit["complexity_score_used"] = score

    if score is None:
        audit["rationale"] = "no complexity score available; using policy floors"
        return AdaptiveLimits(dev_max=floor_dev, review_max=floor_review, audit=audit)

    # Base: scale floor→cap by complexity score.
    base_dev = _scale_to_band(score, floor_dev, cap_dev)
    base_review = _scale_to_band(score, floor_review, cap_review)
    audit["base_dev"] = base_dev
    audit["base_review"] = base_review

    # History: p75 of matching-complexity runs; records within ±1 of the score.
    history_sample = 0
    p75_dev = 0
    p75_review = 0
    if history_path is not None:
        recs = _read_history_tail(history_path)
        matching_dev: list[int] = []
        matching_review: list[int] = []
        for rec in recs:
            rec_score = _extract_record_score(rec)
            if rec_score is None or abs(rec_score - score) > 1:
                continue
            d = _extract_dev_used(rec)
            r = _extract_review_used(rec)
            if d is not None:
                matching_dev.append(d)
            if r is not None:
                matching_review.append(r)
        history_sample = max(len(matching_dev), len(matching_review))
        p75_dev = _percentile(matching_dev, 75)
        p75_review = _percentile(matching_review, 75)
    audit["history_sample_size"] = history_sample
    audit["p75_dev"] = p75_dev
    audit["p75_review"] = p75_review

    # Combine base and p75 — use max(base, p75+1) so history can push the limit
    # up but not below the complexity-derived base.  Clamp to [floor, cap].
    raw_dev = max(base_dev, p75_dev + 1 if p75_dev > 0 else 0)
    raw_review = max(base_review, p75_review + 1 if p75_review > 0 else 0)
    chosen_dev = max(floor_dev, min(cap_dev, raw_dev))
    chosen_review = max(floor_review, min(cap_review, raw_review))
    audit["chosen_dev_max"] = chosen_dev
    audit["chosen_review_max"] = chosen_review
    if history_sample > 0 and (raw_dev > base_dev or raw_review > base_review):
        audit["rationale"] = (
            f"history p75 (dev={p75_dev}, review={p75_review}) raised limits above complexity base"
        )
    elif history_sample > 0:
        audit["rationale"] = "history within complexity base; using complexity-derived limits"
    else:
        audit["rationale"] = "no matching history; using complexity-derived limits"

    return AdaptiveLimits(dev_max=chosen_dev, review_max=chosen_review, audit=audit)
