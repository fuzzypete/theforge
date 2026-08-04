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
- **reasoning_effort** — 3 buckets, resolved *per phase* (#1108). Plan leads dev
  by one band because planning runs once and its output constrains every
  downstream phase; review tracks dev. The resolved level is applied at
  assignment time to the selected :class:`~theforge.config.types.ModelProfile`
  for each phase — as an effort string on transports that express effort that
  way, as a token budget on transports that express it as a thinking budget, and
  not at all on transports with no passthrough (recorded
  ``provider_unsupported``). This axis was previously documented as
  intentionally excluded from score control; that exclusion is retracted.

Raising a bucket ceiling is a single-line edit to the relevant ``*_BUCKETS``
tuple here — assignment, coordinator preflight, and static role derivation all
route through this module and stay aligned.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

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

# ── Reasoning effort (#1108) ──────────────────────────────────────────
#
# The only valid effort levels. A configured value outside this set is a hard
# config error, never a silent fallback (config loading is an integrity
# boundary).
VALID_REASONING_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

# The phases the reasoning-effort axis is resolved for. ``review`` covers both
# plan-review and code-review; preflight is deliberately excluded (it is a fixed
# cheap classification pass whose cost the score is derived *from*).
REASONING_EFFORT_PHASES: tuple[str, ...] = ("plan", "dev", "review")

# Ordered upper-bound score buckets per phase. Deliberately asymmetric: plan
# starts a band above dev because the plan runs once and constrains every
# downstream phase, so under-thinking it is the expensive mistake.
PLAN_EFFORT_BUCKETS: tuple[tuple[int, str], ...] = (
    (3, "medium"),
    (6, "high"),
    (10, "high"),
)
DEV_EFFORT_BUCKETS: tuple[tuple[int, str], ...] = (
    (3, "low"),
    (6, "medium"),
    (10, "high"),
)
REVIEW_EFFORT_BUCKETS: tuple[tuple[int, str], ...] = (
    (3, "low"),
    (6, "medium"),
    (10, "high"),
)

REASONING_EFFORT_PHASE_BUCKETS: dict[str, tuple[tuple[int, str], ...]] = {
    "plan": PLAN_EFFORT_BUCKETS,
    "dev": DEV_EFFORT_BUCKETS,
    "review": REVIEW_EFFORT_BUCKETS,
}

# Token budgets for transports that express thinking as a token count rather
# than an effort level. Operator-overridable per provider in forge.yaml.
DEFAULT_EFFORT_TOKEN_BUDGETS: dict[str, int] = {
    "low": 2048,
    "medium": 8192,
    "high": 24576,
}

# Canonical provider-support statuses recorded in ``routing_decision``.
PROVIDER_UNSUPPORTED = "provider_unsupported"
SUPPORTED_METERED = "supported_metered"
SUPPORTED_UNMETERED = "supported_unmetered"

# How reasoning effort is expressed on a transport.
KNOB_EFFORT = "effort"  # a "low"/"medium"/"high" string
KNOB_BUDGET = "budget"  # an integer thinking-token budget
KNOB_NONE = "none"  # no passthrough exists on this transport


@dataclass(frozen=True)
class EffortKnob:
    """How one transport accepts reasoning effort, and whether it meters it.

    ``kind`` is the shape of the knob the transport accepts (:data:`KNOB_EFFORT`,
    :data:`KNOB_BUDGET`, :data:`KNOB_NONE`). ``captures_thinking_spend`` records
    whether that transport's *cost accounting path* folds thinking tokens into
    the run's measured cost — a separate fact from whether the knob exists, and
    the reason ``supported_unmetered`` is a distinct status: score-driven spend
    on a surface the audit reports as $0.00 must be visible, not silent.
    """

    kind: str
    captures_thinking_spend: bool


# Keyed by ``TransportSpec.runner``, which is unique across both kinds
# (claude/codex/gemini/ghaw are CLI runners; anthropic/openai/google/deepseek are
# API adapters). Capability metadata only — no routing policy lives in runners.
REASONING_EFFORT_TRANSPORT_SUPPORT: dict[str, EffortKnob] = {
    # Codex CLI passes ``-c model_reasoning_effort=<level>`` (runner_codex.py) and
    # prices reasoning output from ``turn.completed`` usage events.
    "codex": EffortKnob(KNOB_EFFORT, True),
    # Google API adapter builds a ThinkingConfig from ``thinking_budget`` and
    # bills ``thoughts_token_count`` through _estimate_cost (adapters/google.py).
    "google": EffortKnob(KNOB_BUDGET, True),
    # No passthrough today.
    "claude": EffortKnob(KNOB_NONE, False),
    "gemini": EffortKnob(KNOB_NONE, False),  # CLI has no mechanism; ignores it
    "ghaw": EffortKnob(KNOB_NONE, False),
    "anthropic": EffortKnob(KNOB_NONE, False),
    "openai": EffortKnob(KNOB_NONE, False),
    "deepseek": EffortKnob(KNOB_NONE, False),
}

_UNKNOWN_TRANSPORT_KNOB = EffortKnob(KNOB_NONE, False)


def effort_knob_for(runner: str | None) -> EffortKnob:
    """Return the reasoning-effort capability for a transport runner.

    An unknown/absent runner is treated as having no passthrough — a new
    transport must opt in explicitly rather than inherit a knob it never wired.
    """
    if not runner:
        return _UNKNOWN_TRANSPORT_KNOB
    return REASONING_EFFORT_TRANSPORT_SUPPORT.get(runner, _UNKNOWN_TRANSPORT_KNOB)


def provider_support_status(knob: EffortKnob, *, cost_captured: bool) -> str:
    """Classify a transport's reasoning-effort support for the audit.

    ``cost_captured`` is the caller's answer to "does this run's measured cost
    actually include the thinking spend?" — the transport must both fold
    thinking tokens into its accounting *and* have a pricing entry for the
    selected model. Both facts are checked because either one being false leaves
    the score-driven spend outside the audited total.
    """
    if knob.kind == KNOB_NONE:
        return PROVIDER_UNSUPPORTED
    return SUPPORTED_METERED if cost_captured else SUPPORTED_UNMETERED


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


# NOTE: there is deliberately no ``score_to_reasoning_effort`` sibling of the
# helpers above. That axis is phase-aware and override-aware, and assignment
# needs the bucket/range/thresholds alongside the output to record the decision,
# so :func:`axis_decision` is its single entry point. A convenience wrapper would
# be a second resolution path nothing calls.


# ── Canonical policy metadata (drives instrumentation + docs) ──────────


@dataclass(frozen=True)
class RoutingAxis:
    """One score-derived routing axis and its canonical bucket policy.

    ``key`` is the stable axis id recorded in ``routing_decision``. ``buckets``
    is the ordered upper-bound table (empty when the axis is not score-driven).
    ``phase_buckets`` is set only on phase-aware axes (:data:`reasoning_effort`),
    where the same score resolves to a different output per phase; ``buckets``
    is then the fallback for a phase with no entry. ``rationale`` is the
    one-line operator-facing justification.
    """

    key: str
    description: str
    score_controlled: bool
    buckets: tuple[tuple[int, str], ...]
    rationale: str
    phase_buckets: Mapping[str, tuple[tuple[int, str], ...]] = field(default_factory=dict)

    def buckets_for(
        self,
        phase: str | None = None,
        overrides: Mapping[str, tuple[tuple[int, str], ...]] | None = None,
    ) -> tuple[tuple[int, str], ...]:
        """Return the bucket table in force for ``phase``.

        ``overrides`` is the operator's configured per-phase table (config layer);
        it wins over the built-in ``phase_buckets`` default for that phase.
        """
        if phase is not None:
            configured = (overrides or {}).get(phase)
            if configured:
                return configured
            default = self.phase_buckets.get(phase)
            if default:
                return default
        return self.buckets

    def bucket_bounds(
        self,
        score: int,
        phase: str | None = None,
        overrides: Mapping[str, tuple[tuple[int, str], ...]] | None = None,
    ) -> tuple[int, int]:
        """Return the inclusive [lower, upper] score range covering ``score``."""
        buckets = self.buckets_for(phase, overrides)
        lower = 1
        for upper, _ in buckets:
            if score <= upper:
                return (lower, upper)
            lower = upper + 1
        # Score above the top bucket's ceiling — clamp to the last bucket.
        return (lower, buckets[-1][0])

    def output_for(
        self,
        score: int,
        phase: str | None = None,
        overrides: Mapping[str, tuple[tuple[int, str], ...]] | None = None,
    ) -> str:
        """Return the selected bucket output for ``score``."""
        return _bucket_for(self.buckets_for(phase, overrides), score)

    def bucket_name(
        self,
        score: int,
        phase: str | None = None,
        overrides: Mapping[str, tuple[tuple[int, str], ...]] | None = None,
    ) -> str:
        """Return the bucket label for ``score`` (same as the output token)."""
        return self.output_for(score, phase, overrides)


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
        score_controlled=True,
        buckets=DEV_EFFORT_BUCKETS,
        phase_buckets=REASONING_EFFORT_PHASE_BUCKETS,
        rationale=(
            "3 buckets — plan leads dev by one band; effort scales with score within the tier"
        ),
    ),
}


def axis_decision(
    axis_key: str,
    score: int | None,
    *,
    phase: str | None = None,
    overrides: Mapping[str, tuple[tuple[int, str], ...]] | None = None,
) -> dict[str, object]:
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
    buckets = axis.buckets_for(phase, overrides)
    block: dict[str, object] = {
        "axis": axis.key,
        "description": axis.description,
        "score": score,
        "score_controlled": axis.score_controlled,
        "thresholds": [upper for upper, _ in buckets],
        "rationale": axis.rationale,
    }
    if phase is not None:
        block["phase"] = phase
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
    lower, upper = axis.bucket_bounds(score, phase, overrides)
    block["applied"] = True
    block["bucket"] = axis.bucket_name(score, phase, overrides)
    block["range"] = [lower, upper]
    block["output"] = axis.output_for(score, phase, overrides)
    return block
