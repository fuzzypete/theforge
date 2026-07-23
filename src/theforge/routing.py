"""Canonical score-to-routing policy — the single SSOT for every axis.

This module is the one place that decides how the 1-10 preflight complexity
score maps to a routing *output* on each axis (which model tier, how many
reviewers). :mod:`theforge.assignment` consumes these tables; nothing else may
hardcode a score threshold.

The score is granular *as input* (1-10); the output on each axis is
deliberately coarse — models and reviewers come in discrete units, not a
continuous gradient, and aggressive bucketing limits cost variance (see
issue #1019). The intended design per axis is stated below and encoded in
:data:`ROUTING_POLICY`; the operator-facing narrative lives in
``docs/guides/routing-policy.md`` (cross-linked from ADR-0006 clause 7).

Intended design, per axis (all axes: option (a) — the coarse buckets are
intentional and the score's extra resolution is a signal reserved for future
finer-grained routing, not a defect):

- **dev tier** — 3 buckets. Splits the legacy MEDIUM band so localized work
  (1-3) stays cheap while broader cross-module work (7-10) escalates sooner.
- **plan tier** — 2 buckets. Planning quality is bimodal: either the story is
  mechanical enough for a mid planner (1-5) or it needs a strong one (6-10).
- **reviewer count** (plan_review + code_review) — 3 buckets. Reviewer eyes are
  the coarsest axis: min for low-risk (1-4), the configured midpoint for medium
  (5-7), max for high-risk (8-10). Bounded by ``min_reviewers``/``max_reviewers``.
- **reasoning_effort** — intentionally NOT score-controlled. It is a per-model
  ModelProfile/ModelRef field set by config/overrides
  (``config/role_derivation.py``), never derived from ``complexity_score``.
  Recorded in the axis table so its omission from score routing is explicit
  rather than silent.

Raising a bucket ceiling is a single-line edit to the relevant ``*_BUCKETS``
tuple here — assignment, coordinator preflight, and static role derivation all
route through this module and stay aligned.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Per-axis bucket tables (ordered by inclusive upper bound) ──────────

# Ordered upper-bound buckets for the 1-10 dev complexity score.
DEV_SCORE_TIER_BUCKETS: tuple[tuple[int, str], ...] = (
    (3, "cheap"),
    (6, "mid"),
    (10, "strong"),
)

# Ordered upper-bound buckets for the planner / reviewer model tier.
PLAN_SCORE_TIER_BUCKETS: tuple[tuple[int, str], ...] = (
    (5, "mid"),
    (10, "strong"),
)

# Ordered upper-bound buckets for the reviewer *count* axis. Outputs are the
# symbolic count targets ("min"/"mid"/"max") the assignment layer resolves
# against the configured ``min_reviewers``/``max_reviewers`` (and midpoint).
REVIEWER_SCORE_COUNT_BUCKETS: tuple[tuple[int, str], ...] = (
    (4, "min"),
    (7, "mid"),
    (10, "max"),
)

# Legacy enum fallback used when a numeric score is unavailable.
DEV_COMPLEXITY_TIER: dict[str, str] = {
    "LOW": "cheap",
    "MEDIUM": "mid",
    "HIGH": "strong",
}


def _bucket_for(buckets: tuple[tuple[int, str], ...], score: int) -> str:
    """Return the output for the first bucket whose upper bound covers ``score``."""
    for max_score, output in buckets:
        if score <= max_score:
            return output
    return buckets[-1][1]


def score_to_dev_tier(score: int) -> str:
    """Return the dev-phase tier for a 1-10 complexity score."""
    return _bucket_for(DEV_SCORE_TIER_BUCKETS, score)


def score_to_plan_tier(score: int) -> str:
    """Return the planner/reviewer model tier for a 1-10 complexity score."""
    return _bucket_for(PLAN_SCORE_TIER_BUCKETS, score)


def score_to_reviewer_target(score: int) -> str:
    """Return the reviewer-count target token ("min"/"mid"/"max") for a score."""
    return _bucket_for(REVIEWER_SCORE_COUNT_BUCKETS, score)


# ── Canonical policy metadata (drives instrumentation + docs) ──────────


@dataclass(frozen=True)
class RoutingAxis:
    """One score-derived routing axis and its canonical bucket policy.

    ``key`` is the stable axis id recorded in ``routing_decision``. ``buckets``
    is the ordered upper-bound table (empty when the axis is not score-driven).
    ``score_controlled`` is False only for :data:`reasoning_effort`, whose value
    comes from config/overrides — recorded so its exclusion from score routing
    is explicit. ``rationale`` is the one-line operator-facing justification.
    """

    key: str
    description: str
    score_controlled: bool
    buckets: tuple[tuple[int, str], ...]
    rationale: str

    def bucket_bounds(self, score: int) -> tuple[int, int]:
        """Return the inclusive [lower, upper] score range covering ``score``."""
        lower = 1
        for upper, _ in self.buckets:
            if score <= upper:
                return (lower, upper)
            lower = upper + 1
        # Score above the top bucket's ceiling — clamp to the last bucket.
        return (lower, self.buckets[-1][0])

    def output_for(self, score: int) -> str:
        """Return the selected bucket output for ``score``."""
        return _bucket_for(self.buckets, score)

    def bucket_name(self, score: int) -> str:
        """Return the bucket label for ``score`` (same as the output token)."""
        return self.output_for(score)


ROUTING_POLICY: dict[str, RoutingAxis] = {
    "dev_tier": RoutingAxis(
        key="dev_tier",
        description="Dev-phase model tier",
        score_controlled=True,
        buckets=DEV_SCORE_TIER_BUCKETS,
        rationale=(
            "3 buckets — splits the legacy MEDIUM band so localized work (1-3) "
            "stays cheap while broad cross-module work (7-10) escalates sooner"
        ),
    ),
    "plan_tier": RoutingAxis(
        key="plan_tier",
        description="Planner and reviewer model tier",
        score_controlled=True,
        buckets=PLAN_SCORE_TIER_BUCKETS,
        rationale=(
            "2 buckets — planning quality is bimodal: mid planner for mechanical "
            "stories (1-5), strong planner for design-heavy ones (6-10)"
        ),
    ),
    "reviewer_count": RoutingAxis(
        key="reviewer_count",
        description="Number of plan-review / code-review reviewers",
        score_controlled=True,
        buckets=REVIEWER_SCORE_COUNT_BUCKETS,
        rationale=(
            "3 buckets — min reviewers for low-risk (1-4), configured midpoint "
            "for medium (5-7), max for high-risk (8-10), bounded by "
            "min_reviewers/max_reviewers"
        ),
    ),
    "reasoning_effort": RoutingAxis(
        key="reasoning_effort",
        description="Model reasoning-effort / thinking budget",
        score_controlled=False,
        buckets=(),
        rationale=(
            "intentionally NOT score-controlled — a per-model config/override "
            "field (config/role_derivation.py), never derived from complexity_score"
        ),
    ),
}


def axis_decision(axis_key: str, score: int | None) -> dict[str, object]:
    """Return the canonical routing-policy decision for an axis at a score.

    Pure data assembly for the ``routing_decision`` audit block (#1391,
    ADR-0006 clause 7). Records the score, bucket name, threshold values and the
    covering range, the selected output, and the one-line rationale so every
    axis explains itself from the audit alone. ``score`` is ``None`` under static
    band routing (adaptive disabled) or when preflight produced no numeric score;
    the block then records ``applied: False`` with the reason rather than a
    fabricated bucket.
    """
    axis = ROUTING_POLICY[axis_key]
    block: dict[str, object] = {
        "axis": axis.key,
        "description": axis.description,
        "score": score,
        "score_controlled": axis.score_controlled,
        "thresholds": [upper for upper, _ in axis.buckets],
        "rationale": axis.rationale,
    }
    if not axis.score_controlled:
        block["applied"] = False
        block["bucket"] = None
        block["range"] = None
        block["output"] = None
        block["reason"] = "not_score_controlled"
        return block
    if score is None:
        block["applied"] = False
        block["bucket"] = None
        block["range"] = None
        block["output"] = None
        block["reason"] = "no_numeric_score_static_band_routing"
        return block
    lower, upper = axis.bucket_bounds(score)
    block["applied"] = True
    block["bucket"] = axis.bucket_name(score)
    block["range"] = [lower, upper]
    block["output"] = axis.output_for(score)
    return block
