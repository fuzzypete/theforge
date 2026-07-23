"""Adaptive model assignment — pure deterministic routing with escalation learning.

All public functions in this module are pure (no I/O, no LLM calls) except for
the two I/O helpers load_escalation_history() and append_escalation_record(),
which are called only by the coordinator.
"""

from __future__ import annotations

import logging
import random
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    AgentDef,
    AssignmentConfig,
    ModelProfile,
)
from .config.auth import check_agent_auth
from .config.models import price_tiebreak_signal
from .routing import score_to_dev_tier

log = logging.getLogger(__name__)


# ── Routing explainability: canonical exclusion-reason vocabulary ──────
#
# The routing_decision audit block (#1391) records why each candidate was in
# or out of a role's pool. Reasons are drawn ONLY from this closed set so the
# audit is machine-queryable — no free-form strings for the canonical set.
REASON_AUTH_MISSING = "auth_missing"
REASON_TRANSPORT_UNAVAILABLE = "transport_unavailable"
REASON_TIER_MISMATCH = "tier_mismatch"
REASON_ANTI_SELF_REVIEW = "anti_self_review"
REASON_PHASE_ELIGIBILITY = "phase_eligibility"
REASON_EXPLICIT_OVERRIDE_LOCKED = "explicit_override_locked"
REASON_NONE = "none"  # selected / included candidate

EXCLUSION_REASONS: frozenset[str] = frozenset(
    {
        REASON_AUTH_MISSING,
        REASON_TRANSPORT_UNAVAILABLE,
        REASON_TIER_MISMATCH,
        REASON_ANTI_SELF_REVIEW,
        REASON_PHASE_ELIGIBILITY,
        REASON_EXPLICIT_OVERRIDE_LOCKED,
        REASON_NONE,
    }
)


# ── Routing-symmetry invariant (#1389) ─────────────────────────────────
#
# Every adaptive, history-driven routing mechanism that ratchets a story's dev
# tier (or a reviewer's priority) in ONE direction — promote, escalate,
# deprioritize, exclude — must define a corresponding mechanism that ratchets
# the OPPOSITE way under stated conditions, with audit attribution and tests for
# both directions. Static score-band routing (PHASE_TIER) and hard tier floors
# are exempt: they re-derive from the current story, not from accumulated
# history, so they are not one-way ratchets.
#
# This registry is the machine-readable form of that invariant. The enforcement
# test (tests/test_routing_symmetry_invariant.py) walks it and fails when a
# promotion path has no landed+tested inverse AND no catalogued follow-up. The
# routing_rationale audit field (see _dev_routing_rationale) reports which of
# these paths actually moved the tier on a given story.
#
# The three states the dev routing_rationale can report, kept as a closed set so
# the audit stays machine-queryable (mirrors EXCLUSION_REASONS).
ROUTING_RATIONALE_STAYED = "stayed_at_preflight_tier"
ROUTING_RATIONALE_PROMOTED = "promoted_by"
ROUTING_RATIONALE_DEMOTED = "demoted_by"
ROUTING_RATIONALE_STATES: frozenset[str] = frozenset(
    {ROUTING_RATIONALE_STAYED, ROUTING_RATIONALE_PROMOTED, ROUTING_RATIONALE_DEMOTED}
)

# Stable mechanism names shared by the audit rationale and the symmetry registry
# so the two never drift. Referenced by _dev_routing_rationale and
# apply_post_plan_checkpoint.
MECHANISM_DEV_PROMOTION = "_check_promotion"
MECHANISM_POST_PLAN_DEMOTION = "post_plan_checkpoint"
MECHANISM_REVIEWER_COMPLETION_DEPRIORITIZE = "reviewer_completion_rate"
MECHANISM_PERSISTENT_P1_DEV_ESCALATION = "persistent_p1_dev_escalation"
MECHANISM_RUN_SCOPED_RESET = "fresh_run_state_reset"


@dataclass(frozen=True)
class RoutingMechanism:
    """A single named adaptive routing mechanism.

    ``name`` is the stable identifier used in the audit rationale; ``symbol`` is
    the fully-qualified code symbol implementing it (so the enforcement test can
    import-resolve it and prove it is reachable); ``audit_label`` is the
    routing_rationale / demotion_check label the mechanism emits.
    """

    name: str
    symbol: str
    audit_label: str


@dataclass(frozen=True)
class RoutingSymmetryPair:
    """A promotion/ratchet mechanism paired with its inverse.

    ``demotion`` is the landed inverse mechanism when one exists; otherwise it is
    ``None`` and ``open_followup`` names the tracked catalogue entry for the
    not-yet-landed inverse (kept explicit so an asymmetry is *catalogued*, never
    silently missing). ``promotion_tests`` / ``demotion_tests`` name the test
    modules that exercise each direction so the invariant can require both.

    ``*_test_token`` is the stable string the enforcement test greps for in those
    modules to confirm the direction is exercised. It defaults to the symbol's
    short name, but a mechanism exercised *through* a higher-level entry point
    (e.g. reviewer reranking driven via ``assign_models``, not by calling the
    audit-block builder directly) overrides it with a token that reliably appears
    — keeping the check registry-driven rather than fragile source-scanning.
    """

    promotion: RoutingMechanism
    demotion: RoutingMechanism | None
    promotion_tests: tuple[str, ...]
    demotion_tests: tuple[str, ...] = ()
    open_followup: str | None = None
    promotion_test_token: str | None = None
    demotion_test_token: str | None = None


# The current catalogue of adaptive promotion/deprioritization paths and their
# inverses. Adding a new promotion mechanism WITHOUT either a landed+tested
# demotion or a catalogued open_followup fails the enforcement test — that is the
# architectural backstop this story lands (#1389).
ROUTING_SYMMETRY_REGISTRY: tuple[RoutingSymmetryPair, ...] = (
    # Dev-tier promotion (2+ recent ESCALATE outcomes bump the tier up) is paired
    # with the post-plan checkpoint demotion (clean plan-review on a medium story
    # steps the tier back down) — the first concrete inverse (#1387).
    RoutingSymmetryPair(
        promotion=RoutingMechanism(
            name=MECHANISM_DEV_PROMOTION,
            symbol="theforge.assignment._check_promotion",
            audit_label=ROUTING_RATIONALE_PROMOTED,
        ),
        demotion=RoutingMechanism(
            name=MECHANISM_POST_PLAN_DEMOTION,
            symbol="theforge.assignment.apply_post_plan_checkpoint",
            audit_label=ROUTING_RATIONALE_DEMOTED,
        ),
        promotion_tests=("tests/test_assignment.py",),
        demotion_tests=("tests/test_post_plan_checkpoint.py",),
    ),
    # Reviewer completion-rate deprioritization (#1388): a reviewer with a poor
    # attempt-completion history is reranked down. The inverse — re-inclusion once
    # subsequent attempts complete cleanly — is NOT yet landed; catalogued as an
    # open follow-up (see docs/routing-symmetry-followups.md).
    RoutingSymmetryPair(
        promotion=RoutingMechanism(
            name=MECHANISM_REVIEWER_COMPLETION_DEPRIORITIZE,
            symbol="theforge.assignment._reviewer_completion_check",
            audit_label="completion_check",
        ),
        demotion=None,
        promotion_tests=("tests/test_assignment_reviewer_completion.py",),
        # Exercised via assign_models/_select_reviewers, not by calling the
        # audit-block builder directly, so grep for the mechanism token.
        promotion_test_token="completion",
        open_followup="reviewer-reinclusion",
    ),
    # In-run persistent-P1 dev escalation (#296): a repeated P1 across
    # consecutive review cycles upgrades the dev model for the current run only.
    # The inverse is the next story's fresh CoordinatorState construction, which
    # resets the run-scoped flag and leaves cross-run tier/routing weights
    # untouched.
    RoutingSymmetryPair(
        promotion=RoutingMechanism(
            name=MECHANISM_PERSISTENT_P1_DEV_ESCALATION,
            symbol="theforge.coordinator.review_phase._record_persistent_p1_dev_escalation",
            audit_label="in_run_escalation",
        ),
        demotion=RoutingMechanism(
            name=MECHANISM_RUN_SCOPED_RESET,
            symbol="theforge.coordinator.engine._fresh_run_state",
            audit_label="run_scoped_reset",
        ),
        promotion_tests=("tests/test_coord_preflight_escalation.py",),
        demotion_tests=(
            "tests/test_coord_preflight_escalation.py",
            "tests/test_routing_symmetry_invariant.py",
        ),
        promotion_test_token="persistent_p1_dev_escalation",
        demotion_test_token="_fresh_run_state",
    ),
)


def _dev_routing_rationale(
    promotion_block: dict[str, object],
    base_tier: str,
    effective_tier: str,
) -> dict[str, object]:
    """Unified dev routing-rationale field (#1389, AC clause 4).

    Collapses the separate promotion_check / demotion_check / post_plan_checkpoint
    blocks into ONE operator-facing label naming which symmetric path moved the
    dev tier: it stayed at the preflight tier, was promoted by a named mechanism,
    or was demoted by a named mechanism. Derived purely from the deterministic
    checks already recorded — no new routing logic and no behavior change. The
    post-plan checkpoint overwrites this to ``demoted_by`` when its demotion fires
    (see :func:`apply_post_plan_checkpoint`).
    """
    if promotion_block.get("fired"):
        return {
            "state": ROUTING_RATIONALE_PROMOTED,
            "mechanism": MECHANISM_DEV_PROMOTION,
            "from_tier": base_tier,
            "to_tier": effective_tier,
        }
    return {
        "state": ROUTING_RATIONALE_STAYED,
        "mechanism": None,
        "from_tier": effective_tier,
        "to_tier": effective_tier,
    }


def _agent_canonical_id(agent: AgentDef) -> str | None:
    """Derive the canonical model ID (provider/model/transport) for an agent."""
    from theforge.model_profiles import canonical_id_from_identity  # noqa: PLC0415

    return canonical_id_from_identity(
        actual_model=agent.model,
        provider=agent.provider,
        cli=agent.cli,
    )


def _has_auth(agent: AgentDef, secrets: dict[str, str] | None = None) -> bool:
    """Return True if the agent's provider has usable auth.

    Delegates to ``check_agent_auth`` which merges os.environ with secrets and
    handles CLI binary checks, local endpoints, and Google fallback keys.
    Unsupported provider/CLI values are treated as having auth (best-effort).
    """
    profile = agent.to_model_profile()
    try:
        ready, _ = check_agent_auth(profile, secrets, include_sandbox_readiness=False)
        return ready
    except ValueError:
        return True  # unknown provider/CLI — assume OK to avoid hard failure


def _auth_reason(
    agent: AgentDef, secrets: dict[str, str] | None = None
) -> tuple[bool, str | None]:
    """Return ``(ready, canonical_reason)`` for routing explainability.

    Mirrors :func:`_has_auth`'s readiness verdict but canonicalizes *why* an
    agent is unavailable into the closed reason vocabulary without parsing
    prose beyond a coarse transport-vs-key distinction:

    - a missing CLI binary / ``npx`` / ``gh`` launcher → ``transport_unavailable``
    - a missing API key → ``auth_missing``

    ``reason`` is ``None`` when the agent is ready.
    """
    profile = agent.to_model_profile()
    try:
        ready, reason = check_agent_auth(profile, secrets, include_sandbox_readiness=False)
    except ValueError:
        return True, None  # unknown provider/CLI — assume OK (mirrors _has_auth)
    if ready:
        return True, None
    low = reason.lower()
    if "not found in path" in low or "npx" in low:
        return False, REASON_TRANSPORT_UNAVAILABLE
    return False, REASON_AUTH_MISSING


# ── Data classes ───────────────────────────────────────────────────────


@dataclass
class EscalationRecord:
    """A single entry in the escalation history file."""

    story: str
    complexity: str
    dev_model: str
    outcome: str  # "DONE" | "ESCALATE"
    reason: str = ""
    timestamp: str = ""
    complexity_score: int | None = None


@dataclass
class AssignmentDecision:
    """Model selections for every phase of a story run.

    Note: preflight runs BEFORE assignment is computed, so the preflight
    decision only takes effect if the coordinator stores it for future use
    (e.g. sprint-level config). It does not affect the current run's preflight.
    """

    preflight: ModelProfile
    planner: ModelProfile
    plan_reviewers: list[ModelProfile]
    dev: ModelProfile
    code_reviewers: list[ModelProfile]
    rationale: dict[str, str] = field(default_factory=dict)
    budget_audit: dict[str, object] = field(default_factory=dict)
    # Per-role routing explainability block (#1391). Additive and observational:
    # candidate pool, exclusions with canonical reason, profile signals, adaptive
    # check outcomes, exploration mode, and origin-labeled final rationale. Empty
    # by default so existing constructors/consumers stay intact.
    routing_decision: dict[str, object] = field(default_factory=dict)


# ── Phase → tier mapping ───────────────────────────────────────────────

PHASE_TIER: dict[str, dict[str, str]] = {
    "preflight": {"LOW": "cheap", "MEDIUM": "cheap", "HIGH": "cheap"},
    "plan": {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"},
    "plan_review": {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"},
    "dev": {"LOW": "cheap", "MEDIUM": "mid", "HIGH": "strong"},
    "code_review": {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"},
}

_TIER_ORDER = ["cheap", "mid", "strong"]


# ── Pure helpers ───────────────────────────────────────────────────────


def _normalize_complexity(c: str) -> str:
    """Map small/medium/large → LOW/MEDIUM/HIGH; pass through LOW/MEDIUM/HIGH."""
    mapping = {
        "small": "LOW",
        "medium": "MEDIUM",
        "large": "HIGH",
        "low": "LOW",
        "high": "HIGH",
    }
    return mapping.get(c.lower(), "MEDIUM")


def _reviewer_count(complexity: str, min_r: int, max_r: int) -> int:
    """Return number of reviewers for this complexity."""
    if complexity == "LOW":
        return min_r
    if complexity == "HIGH":
        return max_r
    # MEDIUM: midpoint (round up)
    return min_r + (max_r - min_r + 1) // 2


def _normalize_complexity_score(complexity_score: int | None) -> int | None:
    """Clamp numeric complexity scores to the supported 1-10 range."""
    if complexity_score is None:
        return None
    return max(1, min(10, int(complexity_score)))


def _plan_tier_for_score(complexity: str, complexity_score: int | None) -> str:
    """Return the planner tier, preferring score-driven routing when present."""
    score = _normalize_complexity_score(complexity_score)
    if score is None:
        return PHASE_TIER["plan"][complexity]
    return "mid" if score <= 5 else "strong"


def _dev_tier_for_score(complexity: str, complexity_score: int | None) -> str:
    """Return the dev tier, preferring score-driven routing when present."""
    score = _normalize_complexity_score(complexity_score)
    if score is None:
        return PHASE_TIER["dev"][complexity]
    return score_to_dev_tier(score)


def _reviewer_target_for_score(
    complexity: str,
    complexity_score: int | None,
    min_r: int,
    max_r: int,
) -> int:
    """Return reviewer count, allowing same-band stories to diverge by score."""
    score = _normalize_complexity_score(complexity_score)
    if score is None:
        return _reviewer_count(complexity, min_r, max_r)
    if score <= 4:
        return min_r
    if score >= 8:
        return max_r
    return min_r + (max_r - min_r + 1) // 2


def _decision_total(decision: AssignmentDecision) -> float:
    """Return the total estimated assignment spend for the story."""
    return (
        decision.preflight.budget_usd
        + decision.planner.budget_usd
        + sum(p.budget_usd for p in decision.plan_reviewers)
        + decision.dev.budget_usd
        + sum(p.budget_usd for p in decision.code_reviewers)
    )


def _agents_by_tier(agents: list[AgentDef], tier: str) -> list[AgentDef]:
    """Return agents matching tier, sorted by budget_usd then real per-MTok price.

    Within a tier every agent carries an identical even-split ``budget_usd``, so
    that key alone leaves same-tier candidates tied and the stable sort falls back
    to ``models.enabled`` list order — permanently starving whichever cheap-tier
    model is listed second (issue #1617). Breaking the tie on the real per-MTok
    price already carried on the registry lets the genuinely cheaper model win
    deterministically instead of by pool order.
    """
    matches = [a for a in agents if a.tier == tier]
    return sorted(
        matches,
        key=lambda a: (
            a.budget_usd,
            price_tiebreak_signal(a.input_cost_per_mtok, a.output_cost_per_mtok),
        ),
    )


def _rerank_by_profiles(
    candidates: list[AgentDef],
    model_profiles: dict | None,
    role: str,
    complexity: str | None,
    min_runs: int = 3,
    signals_out: dict[str, dict] | None = None,
    domains: list[str] | None = None,
    domain_signals_out: dict[str, dict] | None = None,
    rerank_audit: dict[str, object] | None = None,
    recency: object | None = None,
) -> list[AgentDef]:
    """Stable-sort candidates: high-success-rate first when enough data exists.

    Only role="dev" is profile-aware today; other roles pass through unchanged.
    Candidates without ``min_runs`` observations are ordered behind every
    observed model but among themselves fall back to the real per-MTok price
    (via :func:`price_tiebreak_signal`), so the cheapest unobserved model is the
    first explored rather than whichever the pool happened to list first (#1617).

    Domain match (issue #155) is the *horizontal* preference axis. When
    ``domains`` is present, an admissible per-domain success rate acts strictly as
    a **tiebreaker** among candidates already tied on their complexity success
    rate — it never overrides the complexity ranking (the vertical axis), never
    promotes an unobserved model over an observed one, and never penalizes a
    cold-start model (a candidate with no admissible domain data contributes a
    neutral ``0.0`` tiebreak weight and keeps its complexity-driven / price
    position). This keeps domain a preference within the eligible pool, per
    ADR-0006 clause 1. ``rerank_audit`` (when supplied) records whether the domain
    tiebreak actually changed the head of the ranking so explainability can mark
    the decision influential or not.
    """
    if not model_profiles or role != "dev":
        return candidates
    from theforge.model_profiles import get_dev_domain_signal, get_dev_signal  # noqa: PLC0415

    requested_domains = [d for d in (domains or []) if isinstance(d, str) and d]

    # One profile read per candidate feeds ranking AND the explainability signals
    # (#1391 AC: no profile re-reads). Collect once, then sort in-memory.
    rows: list[tuple[AgentDef, dict, dict | None]] = []
    for agent in candidates:
        signal = get_dev_signal(
            model_profiles,
            agent.name,
            complexity,
            min_runs,
            actual_model=agent.model,
            provider=agent.provider,
            cli=agent.cli,
            recency=recency,
        )
        if signals_out is not None:
            signals_out[agent.name] = signal
        dsignal: dict | None = None
        if requested_domains:
            dsignal = get_dev_domain_signal(
                model_profiles,
                agent.name,
                requested_domains,
                min_runs,
                actual_model=agent.model,
                provider=agent.provider,
                cli=agent.cli,
            )
            if domain_signals_out is not None:
                domain_signals_out[agent.name] = dsignal
        rows.append((agent, signal, dsignal))

    def _price(agent: AgentDef) -> float:
        return price_tiebreak_signal(agent.input_cost_per_mtok, agent.output_cost_per_mtok)

    def _complexity_key(row: tuple[AgentDef, dict, dict | None]) -> tuple:
        agent, signal, _ = row
        rate = signal["rate"]
        if rate is None:
            return (1, 0.0, _price(agent))
        return (0, -rate, _price(agent))

    def _domain_aware_key(row: tuple[AgentDef, dict, dict | None]) -> tuple:
        agent, signal, dsignal = row
        rate = signal["rate"]
        # Admissible domain rate becomes the tiebreak between the complexity rate
        # and price; no admissible domain data → 0.0 (neutral, not a penalty).
        drate = dsignal["rate"] if dsignal and dsignal.get("rate") is not None else 0.0
        if rate is None:
            # Cold start on complexity: keep static price ordering; domain is not
            # a tiebreaker here (nothing to break — no complexity standing yet).
            return (1, 0.0, 0.0, _price(agent))
        return (0, -rate, -drate, _price(agent))

    domain_sorted = [r[0] for r in sorted(rows, key=_domain_aware_key)]

    if rerank_audit is not None and requested_domains:
        complexity_sorted = [r[0] for r in sorted(rows, key=_complexity_key)]
        cx_head = complexity_sorted[0].name if complexity_sorted else None
        dm_head = domain_sorted[0].name if domain_sorted else None
        rerank_audit["domain_applied"] = True
        rerank_audit["domain_influenced"] = bool(cx_head and dm_head and cx_head != dm_head)
        rerank_audit["complexity_only_head"] = cx_head
        rerank_audit["domain_head"] = dm_head

    return domain_sorted


def _pick_agent(
    agents: list[AgentDef],
    tier: str,
    secrets: dict[str, str] | None = None,
    model_profiles: dict | None = None,
    role: str = "",
    complexity: str | None = None,
    signals_out: dict[str, dict] | None = None,
    domains: list[str] | None = None,
    domain_signals_out: dict[str, dict] | None = None,
    rerank_audit: dict[str, object] | None = None,
    recency: object | None = None,
) -> AgentDef | None:
    """Pick cheapest agent of the given tier that has usable auth.

    Skips API agents whose provider key is missing from the environment.
    When ``model_profiles`` is provided and ``role == "dev"``, agents with a
    higher observed success rate at the given complexity are preferred over
    the budget-ordered default. When ``domains`` is present, an admissible
    per-domain success rate breaks ties within that complexity ordering (#155).

    When ``signals_out`` / ``domain_signals_out`` are provided, per-candidate
    profile signals consulted during reranking are recorded into them (keyed by
    agent name) for the routing explainability block — no extra profile reads
    beyond this pass.
    """
    candidates = [a for a in _agents_by_tier(agents, tier) if _has_auth(a, secrets)]
    candidates = _rerank_by_profiles(
        candidates,
        model_profiles,
        role,
        complexity,
        signals_out=signals_out,
        domains=domains,
        domain_signals_out=domain_signals_out,
        rerank_audit=rerank_audit,
        recency=recency,
    )
    if not candidates:
        log.debug("No authed agents for tier %s — trying any tier with auth", tier)
    return candidates[0] if candidates else None


def _promote_tier(tier: str) -> str:
    """Promote tier one step up; returns same tier if already at max."""
    idx = _TIER_ORDER.index(tier) if tier in _TIER_ORDER else len(_TIER_ORDER) - 1
    return _TIER_ORDER[min(idx + 1, len(_TIER_ORDER) - 1)]


def _reviewer_health_rationale(
    agents: list[AgentDef],
    selected: list[AgentDef],
    tier: str,
    *,
    exclude_model: str | None,
    unhealthy_models: set[str] | None,
) -> str:
    """Return a rationale suffix describing how provider-health shaped selection.

    Returns the empty string when no health signal applied to this selection.

    Two cases must always surface:
    - **deprioritized**: an unhealthy candidate was passed over in favor of a
      healthy alternative.  Computed from agents present in the candidate
      space (tier ladder, not self-excluded by model) but absent from
      ``selected``.
    - **fallback**: a reviewer in ``selected`` is itself in
      ``unhealthy_models``.  This case includes the no-alternative
      self-review fallback, where ``_select_reviewers`` re-admits the
      self-excluded model as the last resort — that selection must still
      be flagged so the operator can tell forced fallback from a clean
      pick.  The fallback check therefore consults the actual selected
      set rather than re-applying the self-exclusion filter.
    """
    if not unhealthy_models:
        return ""

    selected_names = {a.name for a in selected}

    # Any selected reviewer that is itself unhealthy was a forced fallback —
    # surface it regardless of whether self-exclusion would have removed it
    # from the candidate pool.  This is the no-alternative case the operator
    # needs to see (issue #1574, review iter 1).
    fell_back = sorted(n for n in unhealthy_models if n in selected_names)

    # Deprioritized: unhealthy candidates that were in the routing-visible
    # pool (tier ladder, self-exclusion applied) but not selected.  These
    # are the picks the router actively avoided in favor of a healthy
    # alternative.
    descending_tiers = ["strong", "mid", "cheap"]
    deprioritized_set: set[str] = set()
    seen: set[str] = set()
    for t in descending_tiers:
        for a in agents:
            if a.tier != t or a.name in seen:
                continue
            seen.add(a.name)
            if exclude_model is not None and a.model == exclude_model:
                continue
            if a.name in unhealthy_models and a.name not in selected_names:
                deprioritized_set.add(a.name)
    deprioritized = sorted(deprioritized_set)

    parts: list[str] = []
    if deprioritized:
        parts.append(f"health-deprioritized: {', '.join(deprioritized)}")
    if fell_back:
        parts.append(
            f"health-fallback: {', '.join(fell_back)} "
            f"(no healthy alternative at tier {tier} or below)"
        )
    if not parts:
        return ""
    return f" [{'; '.join(parts)}]"


def _rerank_reviewers_by_completion(
    candidates: list[AgentDef],
    model_profiles: dict | None,
    *,
    threshold: float,
    min_runs: int,
    recency: object | None = None,
    signals_out: dict[str, dict] | None = None,
    audit: dict[str, object] | None = None,
) -> list[AgentDef]:
    """Stable sort-after reviewers below the completion-rate floor (#1388).

    The reviewer analog of :func:`_rerank_by_profiles`. A reviewer whose
    recency-weighted completion rate is below ``threshold`` — *and only once it
    has accumulated ``min_runs`` attempts* — is sorted **after** every
    higher-completion candidate. This is a sort-after, not a filter-out: a
    low-completion reviewer stays in the pool and is still selected when no
    better candidate is available (mirroring the dev-side fallback), so it is
    never permanently locked out.

    Below ``min_runs`` a reviewer's signal floor is ``"fail"`` (cold start), so it
    is not deprioritized and ordering falls through to the incoming tier/budget/
    cross-provider order. The sort is stable on the original index, so ties (and
    every non-deprioritized reviewer) keep the existing ordering exactly.
    """
    if not model_profiles or not candidates:
        return candidates
    from theforge.model_profiles import get_review_signal  # noqa: PLC0415

    rows: list[tuple[int, int, AgentDef]] = []
    deprioritized: list[str] = []
    for idx, agent in enumerate(candidates):
        signal = get_review_signal(
            model_profiles,
            agent.name,
            min_runs,
            actual_model=agent.model,
            provider=agent.provider,
            cli=agent.cli,
            recency=recency,
        )
        if signals_out is not None:
            signals_out[agent.name] = signal
        rate = signal["rate"]
        is_low = signal["floor"] == "pass" and rate is not None and rate < threshold
        if is_low:
            deprioritized.append(agent.name)
        rows.append((1 if is_low else 0, idx, agent))

    reranked = [row[2] for row in sorted(rows, key=lambda r: (r[0], r[1]))]
    if audit is not None:
        original_order = [a.name for a in candidates]
        final_order = [a.name for a in reranked]
        audit["mechanism"] = "reviewer_completion_rate"
        audit["threshold"] = threshold
        audit["min_runs"] = min_runs
        audit["applied"] = original_order != final_order
        audit["deprioritized"] = deprioritized
        audit["original_order"] = original_order
        audit["final_order"] = final_order
    return reranked


def _select_reviewers(
    agents: list[AgentDef],
    tier: str,
    n: int,
    prefer_cross_provider: bool,
    exclude_model: str | None = None,
    secrets: dict[str, str] | None = None,
    unhealthy_models: set[str] | None = None,
    *,
    model_profiles: dict | None = None,
    completion_threshold: float = 0.5,
    completion_min_runs: int = 5,
    recency: object | None = None,
    completion_signals_out: dict[str, dict] | None = None,
    completion_audit: dict[str, object] | None = None,
) -> list[AgentDef]:
    """Select n reviewer agents from the pool.

    Prefer strong-tier agents; fall back to tier if needed.
    Break ties by lowest budget_usd.
    If prefer_cross_provider, greedily pick from different providers.
    If exclude_model is set, agents with that model are excluded; they are
    only included as a last resort when no other candidates are available.
    If unhealthy_models is set, agents whose name appears in the set are
    deprioritized: they are excluded when at least one healthy alternative
    exists in the candidate pool, and only included as a last resort.  This
    keeps the router from re-picking a model that has just returned a
    provider-shape failure (capacity, rate-limit, 5xx) within the recent
    health window.
    When ``model_profiles`` is provided, reviewers whose recency-weighted
    completion rate is below ``completion_threshold`` (after ``completion_min_runs``
    attempts) are sorted *after* higher-completion candidates within this pool —
    a sort-after that runs on top of the existing tier/exclude/health selection,
    never as a replacement for it (#1388).
    """
    # Build candidate list spanning the full tier ladder (strong → mid → cheap).
    # Stronger reviewers are always preferred (listed first), but the pool descends
    # past the requested tier so a panel of N can still be filled when the
    # requested tier's authed-provider diversity is too narrow after
    # self-exclusion / cross-provider filtering.  Without the descent, a
    # strong-tier request would degenerate to strong-only, producing fewer
    # eyes for higher-risk stories than a mid-tier request sees — see issue
    # #1542.
    descending_tiers = ["strong", "mid", "cheap"]
    seen_names: set[str] = set()
    candidates: list[AgentDef] = []
    for t in descending_tiers:
        for a in _agents_by_tier(agents, t):
            if _has_auth(a, secrets) and a.name not in seen_names:
                candidates.append(a)
                seen_names.add(a.name)

    # If no authed candidates exist, fall back to unauthed agents so the pool
    # is never empty (mirrors the fallback logic in assign_models for dev/planner).
    if not candidates:
        seen_names = set()
        for t in descending_tiers:
            for a in _agents_by_tier(agents, t):
                if a.name not in seen_names:
                    candidates.append(a)
                    seen_names.add(a.name)

    if not candidates:
        return []

    # Exclude the model that produced the plan/code being reviewed so agents
    # don't self-review.  If the tier/strong pool is exhausted, expand to all
    # authed agents before falling back to self-review (last resort).
    if exclude_model is not None:
        preferred = [a for a in candidates if a.model != exclude_model]
        if not preferred:
            # Widen search: any authed agent with a different model
            preferred = [a for a in agents if _has_auth(a, secrets) and a.model != exclude_model]
        if not preferred:
            # Widen further: any agent regardless of auth with a different model
            preferred = [a for a in agents if a.model != exclude_model]
        if preferred:
            candidates = preferred

    # Deprioritize models that recently returned provider-shape failures.
    # Falling back to an unhealthy model is allowed only when no healthy
    # alternative remains in the candidate pool.
    if unhealthy_models:
        healthy = [a for a in candidates if a.name not in unhealthy_models]
        if healthy:
            candidates = healthy

    # Completion-rate rerank (#1388): sort low-completion reviewers after
    # higher-completion candidates within the pool built above (tier / self-
    # exclusion / health all already applied). Sort-after, not filter-out — the
    # pool membership is unchanged, only its order.
    candidates = _rerank_reviewers_by_completion(
        candidates,
        model_profiles,
        threshold=completion_threshold,
        min_runs=completion_min_runs,
        recency=recency,
        signals_out=completion_signals_out,
        audit=completion_audit,
    )

    if not prefer_cross_provider:
        return candidates[:n]

    # Greedy cross-provider selection
    selected: list[AgentDef] = []
    used_providers: set[str] = set()

    # First pass: pick from different providers (using effective provider so
    # CLI-only agents are distinguished by their underlying CLI binary —
    # claude/codex/gemini map to anthropic/openai/google rather than collapsing
    # to a shared `None`).
    for a in candidates:
        if len(selected) >= n:
            break
        eff = a.effective_provider
        if eff not in used_providers:
            selected.append(a)
            used_providers.add(eff)

    # Second pass: fill remaining from any provider
    for a in candidates:
        if len(selected) >= n:
            break
        if a not in selected:
            selected.append(a)

    return selected[:n]


def _check_promotion(
    complexity: str,
    dev_agent_name: str,
    history: list[EscalationRecord],
    sprint_promotions: dict[str, str] | None,
    *,
    dev_canonical_id: str | None = None,
) -> str | None:
    """Return promoted tier string if promotion is warranted, else None.

    Checks sprint_promotions cache first (sticky within sprint).
    Looks at last 10 records matching complexity and dev model identity.
    Records are matched against ``dev_canonical_id`` when provided (so
    canonicalized history records still match the agent), and against the
    legacy ``dev_agent_name`` as a fallback for unmigrated history.
    Promotes if 2+ have outcome=ESCALATE.
    """
    if sprint_promotions and complexity in sprint_promotions:
        return sprint_promotions[complexity]

    def _matches(r: EscalationRecord) -> bool:
        if r.complexity != complexity:
            return False
        if dev_canonical_id and r.dev_model == dev_canonical_id:
            return True
        return r.dev_model == dev_agent_name

    # Filter to last 10 matching records
    matching = [r for r in history if _matches(r)][-10:]

    if not matching:
        return None

    escalation_count = sum(1 for r in matching if r.outcome == "ESCALATE")
    if escalation_count >= 2:
        return "promoted"  # signal to caller to promote
    return None


def _enforce_budget(
    decision: AssignmentDecision,
    agents: list[AgentDef],
    max_cost_per_story_usd: float,
    dev_floor_tier: str = "cheap",
    planner_floor_tier: str | None = None,
    locked_roles: set[str] | None = None,
) -> AssignmentDecision:
    """Downgrade highest-cost non-preflight model if over the per-story routing cost target.

    Candidates for downgrade: planner, plan_reviewers, dev, code_reviewers.
    Preflight is excluded to preserve classification quality.
    Plan-phase models are included because for MEDIUM+ complexity their costs
    alone can exceed the cap, making it impossible to reach via dev/code_review
    downgrades alone.
    The dev model is never downgraded below dev_floor_tier (the complexity-driven
    tier floor) to preserve the assignment guardrail.
    The planner can also carry a tier floor when adaptive routing chose a strong
    planner for a story that already assigned a strong dev model.
    """
    from dataclasses import replace as _dc_replace

    locked_roles = locked_roles or set()
    initial_total = _decision_total(decision)
    preferred_snapshot = _preferred_snapshot(decision, initial_total)
    budget_steps: list[dict[str, object]] = []
    protected_roles: set[str] = set()

    if initial_total <= max_cost_per_story_usd:
        rationale = dict(decision.rationale)
        rationale["per_story_routing_cost_target"] = (
            f"within per-story routing cost target ${max_cost_per_story_usd:.2f} "
            f"(estimated total ${initial_total:.2f})"
        )
        return _dc_replace(
            decision,
            rationale=rationale,
            budget_audit={
                "target_usd": max_cost_per_story_usd,
                "initial_total_usd": round(initial_total, 2),
                "final_total_usd": round(initial_total, 2),
                "within_target": True,
                "downgraded": False,
                "steps": [],
                "preferred": preferred_snapshot,
            },
        )

    # Build a lookup from profile name → AgentDef for downgrade
    agent_by_name = {a.name: a for a in agents}

    def _next_cheaper_profile(profile: ModelProfile) -> ModelProfile | None:
        agent = agent_by_name.get(profile.name)
        if agent is not None:
            current_tier = agent.tier
            idx = _TIER_ORDER.index(current_tier) if current_tier in _TIER_ORDER else 0
            if idx == 0:
                return None  # already cheapest
            # Try each tier below current, from next-cheaper down to cheapest
            for cheaper_tier in _TIER_ORDER[idx - 1 :: -1]:
                cheaper_agents = _agents_by_tier(agents, cheaper_tier)
                if cheaper_agents:
                    cheaper = cheaper_agents[0]
                    return cheaper.to_model_profile(allowed_tools=profile.allowed_tools)
            return None
        else:
            # Profile not in agent pool (e.g. explicit override) — find any cheaper agent
            cheaper_options = [a for a in agents if a.budget_usd < profile.budget_usd]
            if not cheaper_options:
                return None
            cheapest = min(cheaper_options, key=lambda a: a.budget_usd)
            return cheapest.to_model_profile(allowed_tools=profile.allowed_tools)

    # Iteratively downgrade until within cap (max 10 passes)
    for _ in range(10):
        if _decision_total(decision) <= max_cost_per_story_usd:
            break

        # Find highest-cost non-preflight profile that can be downgraded.
        # Include plan-phase (planner, plan_reviewers) alongside dev and code_reviewers
        # so that MEDIUM+ stories where plan-phase costs alone exceed the cap can still
        # converge. Preflight is excluded to preserve classification quality.
        candidates: list[tuple[str, ModelProfile]] = []
        candidates.append(("planner", decision.planner))
        for i, p in enumerate(decision.plan_reviewers):
            candidates.append((f"plan_review_{i}", p))
        candidates.append(("dev", decision.dev))
        for i, p in enumerate(decision.code_reviewers):
            candidates.append((f"code_review_{i}", p))

        if not candidates:
            break

        # Sort by budget descending to downgrade most expensive first
        candidates.sort(key=lambda x: x[1].budget_usd, reverse=True)
        dev_floor_idx = _TIER_ORDER.index(dev_floor_tier) if dev_floor_tier in _TIER_ORDER else 0
        planner_floor_idx = (
            _TIER_ORDER.index(planner_floor_tier) if planner_floor_tier in _TIER_ORDER else None
        )
        downgraded = False
        for role, profile in candidates:
            role_class = (
                "plan_review"
                if role.startswith("plan_review_")
                else "code_review"
                if role.startswith("code_review_")
                else role
            )
            if role_class in locked_roles:
                protected_roles.add(role_class)
                continue
            cheaper = _next_cheaper_profile(profile)
            if cheaper is not None:
                cheaper_def = agent_by_name.get(cheaper.name)
                # Guardrail: never downgrade dev below its complexity tier floor.
                if role == "dev":
                    if cheaper_def is not None and cheaper_def.tier in _TIER_ORDER:
                        if _TIER_ORDER.index(cheaper_def.tier) < dev_floor_idx:
                            protected_roles.add("dev")
                            continue  # skip — would violate floor
                if role == "planner" and planner_floor_idx is not None:
                    if cheaper_def is not None and cheaper_def.tier in _TIER_ORDER:
                        if _TIER_ORDER.index(cheaper_def.tier) < planner_floor_idx:
                            protected_roles.add("planner")
                            continue  # skip — would violate floor
                if role == "dev":
                    decision = _dc_replace(decision, dev=cheaper)
                elif role == "planner":
                    decision = _dc_replace(decision, planner=cheaper)
                elif role.startswith("plan_review_"):
                    i = int(role.split("_")[-1])
                    new_reviewers = list(decision.plan_reviewers)
                    new_reviewers[i] = cheaper
                    decision = _dc_replace(decision, plan_reviewers=new_reviewers)
                else:
                    i = int(role.split("_")[-1])
                    new_reviewers = list(decision.code_reviewers)
                    new_reviewers[i] = cheaper
                    decision = _dc_replace(decision, code_reviewers=new_reviewers)
                budget_steps.append(
                    {
                        "action": "downgrade",
                        "role": role_class,
                        "from_model": profile.model,
                        "to_model": cheaper.model,
                        "from_budget_usd": profile.budget_usd,
                        "to_budget_usd": cheaper.budget_usd,
                    }
                )
                downgraded = True
                break

        if not downgraded:
            # Can't downgrade any model; try removing a reviewer (keep min 1 each)
            if "code_review" not in locked_roles and len(decision.code_reviewers) > 1:
                removed = decision.code_reviewers[-1]
                decision = _dc_replace(decision, code_reviewers=decision.code_reviewers[:-1])
                budget_steps.append(
                    {
                        "action": "drop_reviewer",
                        "role": "code_review",
                        "model": removed.model,
                        "budget_usd": removed.budget_usd,
                    }
                )
            elif "plan_review" not in locked_roles and len(decision.plan_reviewers) > 1:
                removed = decision.plan_reviewers[-1]
                decision = _dc_replace(decision, plan_reviewers=decision.plan_reviewers[:-1])
                budget_steps.append(
                    {
                        "action": "drop_reviewer",
                        "role": "plan_review",
                        "model": removed.model,
                        "budget_usd": removed.budget_usd,
                    }
                )
            else:
                break

    final_total = _decision_total(decision)
    within_cap = final_total <= max_cost_per_story_usd
    rationale = dict(decision.rationale)
    impacted_roles = {
        str(step.get("role"))
        for step in budget_steps
        if isinstance(step, dict) and step.get("role")
    }
    cap_phrase = f"per-story routing cost target ${max_cost_per_story_usd:.2f}"
    if "planner" in impacted_roles and "planner" in rationale:
        _p = decision.planner
        rationale["planner"] += f"; {cap_phrase} downgraded to {_p.model} @ ${_p.budget_usd:.2f}"
    if "dev" in impacted_roles and "dev" in rationale:
        _d = decision.dev
        rationale["dev"] += f"; {cap_phrase} downgraded to {_d.model} @ ${_d.budget_usd:.2f}"
    if "plan_review" in impacted_roles and "plan_review" in rationale:
        _pr_total = sum(p.budget_usd for p in decision.plan_reviewers)
        rationale["plan_review"] += (
            f"; {cap_phrase} downgraded to "
            f"{[p.model for p in decision.plan_reviewers]} @ ${_pr_total:.2f}"
        )
    if "code_review" in impacted_roles and "code_review" in rationale:
        _cr_total = sum(p.budget_usd for p in decision.code_reviewers)
        rationale["code_review"] += (
            f"; {cap_phrase} downgraded to "
            f"{[p.model for p in decision.code_reviewers]} @ ${_cr_total:.2f}"
        )
    if budget_steps:
        rationale["per_story_routing_cost_target"] = (
            f"{cap_phrase}: downgraded to ${final_total:.2f} via {len(budget_steps)} adjustment(s)"
        )
    elif within_cap:
        rationale["per_story_routing_cost_target"] = (
            f"within {cap_phrase} (estimated total ${final_total:.2f})"
        )
    else:
        rationale["per_story_routing_cost_target"] = (
            f"{cap_phrase} could not be met; estimated total ${final_total:.2f}"
        )
    if protected_roles:
        rationale["per_story_routing_cost_target"] += (
            f" (protected roles: {sorted(protected_roles)})"
        )

    if not within_cap:
        warnings.warn(
            f"[adaptive] per-story routing cost target ${max_cost_per_story_usd:.2f} "
            f"cannot be met; actual total ${final_total:.2f}",
            stacklevel=2,
        )

    audit: dict[str, object] = {
        "target_usd": max_cost_per_story_usd,
        "initial_total_usd": round(initial_total, 2),
        "final_total_usd": round(final_total, 2),
        "within_target": within_cap,
        "downgraded": bool(budget_steps),
        "steps": budget_steps,
        "preferred": preferred_snapshot,
    }
    if protected_roles:
        audit["protected_roles"] = sorted(protected_roles)
    if not within_cap and locked_roles:
        audit["override_forced_overrun"] = True
        audit["locked_roles"] = sorted(locked_roles)
    return _dc_replace(decision, rationale=rationale, budget_audit=audit)


def _preferred_snapshot(decision: AssignmentDecision, total_usd: float) -> dict[str, object]:
    """Capture adaptive's pre-cap selection for warning emission and audit."""
    return {
        "dev": {"model": decision.dev.model, "budget_usd": round(decision.dev.budget_usd, 2)},
        "planner": {
            "model": decision.planner.model,
            "budget_usd": round(decision.planner.budget_usd, 2),
        },
        "plan_reviewers": [
            {"model": p.model, "budget_usd": round(p.budget_usd, 2)}
            for p in decision.plan_reviewers
        ],
        "code_reviewers": [
            {"model": p.model, "budget_usd": round(p.budget_usd, 2)}
            for p in decision.code_reviewers
        ],
        "total_usd": round(total_usd, 2),
    }


def _agent_to_profile(
    agent: AgentDef,
    *,
    role: str,
    allowed_tools: tuple[str, ...] = (),
) -> ModelProfile:
    """Convert AgentDef to ModelProfile with appropriate defaults."""
    if not allowed_tools:
        if role == "dev":
            allowed_tools = DEFAULT_DEV_PROFILE.allowed_tools
        else:
            allowed_tools = DEFAULT_REVIEW_PROFILE.allowed_tools
    return ModelProfile(
        name=agent.name,
        cli=agent.cli,
        provider=agent.provider,
        model=agent.model,
        budget_usd=agent.budget_usd,
        timeout_seconds=agent.timeout_seconds,
        allowed_tools=allowed_tools,
        api_fallback=agent.api_fallback,
        phase=role,
        registry_id=agent.registry_id,
        registry_source=agent.registry_source,
    )


# ── Routing explainability (#1391) ─────────────────────────────────────


def _selected_tier(agents: list[AgentDef], name: str, fallback: str | None) -> str | None:
    """Return the tier of the agent selected for a role, or ``fallback``."""
    for a in agents:
        if a.name == name:
            return a.tier
    return fallback


def _single_model_pool(
    agents: list[AgentDef],
    target_tier: str | None,
    selected_name: str,
    locked: bool,
    secrets: dict[str, str] | None,
) -> list[dict[str, object]]:
    """Build the candidate pool for a single-model role (preflight/planner/dev).

    Every agent is listed with ``included`` and, when excluded, a canonical
    ``reason``. Priority of exclusion reasons is deterministic: the selected
    model is always included; an explicit override locks out the rest; then
    tier mismatch; then auth/transport unavailability.
    """
    pool: list[dict[str, object]] = []
    for a in agents:
        entry: dict[str, object] = {"name": a.name, "tier": a.tier}
        if a.name == selected_name:
            entry["included"] = True
            entry["reason"] = REASON_NONE
        elif locked:
            entry["included"] = False
            entry["reason"] = REASON_EXPLICIT_OVERRIDE_LOCKED
        elif target_tier is not None and a.tier != target_tier:
            entry["included"] = False
            entry["reason"] = REASON_TIER_MISMATCH
        else:
            ready, reason = _auth_reason(a, secrets)
            if not ready:
                entry["included"] = False
                entry["reason"] = reason
            else:
                entry["included"] = True
                entry["reason"] = REASON_NONE
        pool.append(entry)
    return pool


def _reviewer_health_context(
    agents: list[AgentDef],
    selected_names: set[str],
    exclude_model: str | None,
    locked: bool,
    unhealthy_models: set[str] | None,
    secrets: dict[str, str] | None,
) -> tuple[bool, set[str], set[str]]:
    """Reconstruct the provider-health demotion that shaped reviewer selection.

    Mirrors :func:`_select_reviewers`: when at least one *healthy* authed,
    non-self-excluded candidate exists, every *unhealthy* candidate (one that
    recently returned a provider-shape failure — capacity/rate-limit/5xx/quota
    still within the health window) is dropped from the pool. This is a live
    demotion/recovery mechanism (ADR-0006 clause 5), so the routing_decision
    block must record its outcome rather than silently marking a
    health-deprioritized candidate ``included: true``.

    Returns ``(fired, deprioritized, fell_back)``:
    - ``fired``: health demotion actually removed a candidate.
    - ``deprioritized``: unhealthy candidates dropped because a healthy
      alternative existed (excluded from the pool).
    - ``fell_back``: unhealthy candidates that ran anyway — the no-healthy-
      alternative last resort — which stay ``included: true`` (they are in
      ``final.models``, so the block is self-consistent).
    """
    if not unhealthy_models or locked:
        return (False, set(), set())
    eligible = [
        a
        for a in agents
        if (exclude_model is None or a.model != exclude_model) and _has_auth(a, secrets)
    ]
    unhealthy_eligible = {a.name for a in eligible if a.name in unhealthy_models}
    healthy_eligible = {a.name for a in eligible if a.name not in unhealthy_models}
    fired = bool(unhealthy_eligible) and bool(healthy_eligible)
    fell_back = {n for n in unhealthy_eligible if n in selected_names}
    deprioritized = {n for n in unhealthy_eligible if n not in selected_names} if fired else set()
    return (fired, deprioritized, fell_back)


def _reviewer_candidate_pool(
    agents: list[AgentDef],
    selected_names: set[str],
    exclude_model: str | None,
    locked: bool,
    secrets: dict[str, str] | None,
    health_deprioritized: set[str] | None = None,
) -> list[dict[str, object]]:
    """Build the candidate pool for a reviewer role (plan_review/code_review).

    Reviewers span the full tier ladder, so tier is not an exclusion axis here.
    ``included: true`` marks a genuine candidate (authed, not self-excluded, not
    health-deprioritized); the role's ``final.models`` names who actually ran.
    The anti-self-review filter (``exclude_model``) surfaces as
    ``anti_self_review``; a candidate dropped by provider-health demotion
    surfaces as ``transport_unavailable`` with a ``health_deprioritized`` detail
    (a recent provider-shape failure is a transient transport unavailability).
    """
    health_deprioritized = health_deprioritized or set()
    pool: list[dict[str, object]] = []
    for a in agents:
        entry: dict[str, object] = {"name": a.name, "tier": a.tier}
        if a.name in selected_names:
            entry["included"] = True
            entry["reason"] = REASON_NONE
        elif locked:
            entry["included"] = False
            entry["reason"] = REASON_EXPLICIT_OVERRIDE_LOCKED
        elif exclude_model is not None and a.model == exclude_model:
            entry["included"] = False
            entry["reason"] = REASON_ANTI_SELF_REVIEW
        else:
            ready, reason = _auth_reason(a, secrets)
            if not ready:
                entry["included"] = False
                entry["reason"] = reason
            elif a.name in health_deprioritized:
                entry["included"] = False
                entry["reason"] = REASON_TRANSPORT_UNAVAILABLE
                entry["detail"] = "health_deprioritized"
            else:
                entry["included"] = True
                entry["reason"] = REASON_NONE
        pool.append(entry)
    return pool


def _reviewer_demotion_check(
    fired: bool,
    deprioritized: set[str],
    fell_back: set[str],
    unhealthy_models: set[str] | None,
) -> dict[str, object]:
    """Build the per-reviewer-role demotion_check block from health context.

    Records the provider-health demotion path whether or not it fired, so the
    audit shows a checked-but-didn't-fire mechanism with its reason (AC clause 5).
    """
    if not unhealthy_models:
        reason = "no_unhealthy_candidates"
    elif fired:
        reason = f"health_deprioritized: {', '.join(sorted(deprioritized))}"
    elif fell_back:
        reason = "no_healthy_alternative_fell_back"
    else:
        reason = "no_unhealthy_candidates_in_pool"
    return {
        "mechanism": "provider_health",
        "fired": fired,
        "deprioritized": sorted(deprioritized),
        "fell_back": sorted(fell_back),
        "reason": reason,
    }


def _reviewer_completion_check(
    signals: dict[str, dict] | None,
    audit: dict[str, object] | None,
    selected_names: set[str],
) -> dict[str, object]:
    """Build the per-reviewer-role completion_check block (#1388, ADR-0006 c7).

    Records the reviewer completion-rate rerank so the routing_decision explains
    when (and how) attempt-completion history shaped the reviewer ordering. Only
    surfaces the mechanism when it *fired* (changed the order) per AC — but keeps
    the consulted per-candidate signal (attempted/completed counts, rate,
    sample-floor status, threshold result) for the reviewers it weighed so the
    decision stays reconstructable. Returns an empty dict when no reviewer profile
    was consulted (e.g. static routing / no profiles), which the caller omits.
    """
    if not signals and not audit:
        return {}
    fired = bool(audit and audit.get("applied"))
    per_candidate: dict[str, object] = {}
    for name, sig in (signals or {}).items():
        per_candidate[name] = {
            "attempted": sig.get("attempted"),
            "completed": sig.get("completed"),
            "raw": sig.get("raw"),
            "weighted": sig.get("weighted"),
            "rate": sig.get("rate"),
            "floor": sig.get("floor"),
            "selected": name in selected_names,
        }
    block: dict[str, object] = {
        "mechanism": "reviewer_completion_rate",
        "fired": fired,
        "threshold": audit.get("threshold") if audit else None,
        "min_runs": audit.get("min_runs") if audit else None,
        "deprioritized": (audit.get("deprioritized") if audit else None) or [],
        "signals": per_candidate,
    }
    if fired and audit:
        block["original_order"] = audit.get("original_order")
        block["final_order"] = audit.get("final_order")
    return block


def _build_routing_decision(
    decision: AssignmentDecision,
    agents: list[AgentDef],
    *,
    origin: str,
    score: int | None,
    dev_base_tier: str,
    dev_effective_tier: str,
    preflight_tier: str | None,
    planner_tier: str | None,
    dev_signals: dict[str, dict],
    promotion_block: dict[str, object],
    planner_model: str,
    dev_model: str,
    explicit_roles: set[str],
    secrets: dict[str, str] | None,
    unhealthy_models: set[str] | None = None,
    domains: list[str] | None = None,
    dev_domain_signals: dict[str, dict] | None = None,
    dev_domain_match: dict[str, object] | None = None,
    excluded_for_taint: int = 0,
    dev_exploration: dict[str, object] | None = None,
    pr_completion_signals: dict[str, dict] | None = None,
    pr_completion_audit: dict[str, object] | None = None,
    cr_completion_signals: dict[str, dict] | None = None,
    cr_completion_audit: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assemble the per-role routing_decision explainability block (#1391).

    Built at the end of :func:`assign_models` from the FINAL decision (after any
    budget-driven downgrades) so the recorded models/tiers match what runs. Pure
    assembly: no LLM calls, no profile re-reads — profile signals come from the
    ``dev_signals`` already collected during routing.
    """
    rationale = decision.rationale

    def _rat(role: str) -> str:
        # Origin-labeled so future post-assignment checkpoints (#1387) can write
        # into the same block and stay distinguishable from the preflight pass.
        text = (rationale.get(role, "") or "").strip()
        return f"[{origin}] {text}".rstrip()

    # Dev pool at the effective (post-promotion) tier, annotated with the
    # profile signals the router actually weighed for each included candidate.
    dev_pool = _single_model_pool(
        agents, dev_effective_tier, decision.dev.name, "dev" in explicit_roles, secrets
    )
    dev_domain_signals = dev_domain_signals or {}
    requested_domains = [d for d in (domains or []) if isinstance(d, str) and d]
    for entry in dev_pool:
        if entry.get("included") and entry["name"] in dev_signals:
            signals: dict[str, object] = {"success_rate": dev_signals[entry["name"]]}
            # Attach the per-domain slice the router weighed for this candidate so
            # the matching profile slice, sample count, floor status, and
            # raw/weighted values are all reconstructable from the audit (#155).
            if requested_domains and entry["name"] in dev_domain_signals:
                signals["domain"] = dev_domain_signals[entry["name"]]
            entry["signals"] = signals

    # Domain-match block (#155 / ADR-0006 clause 7). Present but explicitly
    # non-influential when domains exist yet did not move the selection; omitted
    # entirely when the story carried no domains (nothing to explain).
    dev_domain_match = dev_domain_match or {}
    domain_block: dict[str, object] | None = None
    if requested_domains:
        influenced = bool(dev_domain_match.get("domain_influenced"))
        selected_slice = dev_domain_signals.get(decision.dev.name)
        domain_block = {
            "domains": requested_domains,
            "influenced": influenced,
            "complexity_only_head": dev_domain_match.get("complexity_only_head"),
            "domain_head": dev_domain_match.get("domain_head"),
            "selected_model_slice": selected_slice,
            "reason": (
                "domain_tiebreak_changed_selection"
                if influenced
                else "domain_signal_did_not_change_selection"
            ),
        }

    exploration = {"mode": "winner"}  # v1: on-policy only (#170/#325 not landed)

    # Provider-health demotion (ADR-0006 clause 5) is a live reviewer-only
    # mechanism. Reconstruct its outcome for each reviewer role so a
    # health-deprioritized candidate is recorded excluded (not falsely included)
    # and the checked-but-didn't-fire path is visible.
    pr_selected = {p.name for p in decision.plan_reviewers}
    pr_exclude = None if "plan_review" in explicit_roles else planner_model
    pr_fired, pr_depri, pr_fellback = _reviewer_health_context(
        agents, pr_selected, pr_exclude, "plan_review" in explicit_roles, unhealthy_models, secrets
    )
    cr_selected = {p.name for p in decision.code_reviewers}
    cr_exclude = None if "code_review" in explicit_roles else dev_model
    cr_fired, cr_depri, cr_fellback = _reviewer_health_context(
        agents, cr_selected, cr_exclude, "code_review" in explicit_roles, unhealthy_models, secrets
    )

    # Reviewer completion-rate rerank (#1388) explanation per reviewer role.
    pr_completion_block = _reviewer_completion_check(
        pr_completion_signals, pr_completion_audit, pr_selected
    )
    cr_completion_block = _reviewer_completion_check(
        cr_completion_signals, cr_completion_audit, cr_selected
    )

    return {
        "origin": origin,
        # Excluded-for-taint count (ADR-0006 clause 4 + clause 7, #1852). How many
        # historical runs the centralized taint gate set aside from the router-
        # consumed escalation history because they failed their own trust checks.
        # Surfaced at the top level so operators see how much history was
        # discounted for taint before any per-role explanation. The runs remain in
        # the substrate (ADR-0002 refusal-to-forget); this is a read-time count.
        "excluded_for_taint": int(excluded_for_taint),
        "preflight": {
            "candidate_pool": _single_model_pool(
                agents,
                preflight_tier,
                decision.preflight.name,
                "preflight" in explicit_roles,
                secrets,
            ),
            "exploration": dict(exploration),
            "final": {
                "model": decision.preflight.model,
                "tier": _selected_tier(agents, decision.preflight.name, preflight_tier),
                "rationale": _rat("preflight"),
            },
        },
        "planner": {
            "candidate_pool": _single_model_pool(
                agents,
                planner_tier,
                decision.planner.name,
                "planner" in explicit_roles,
                secrets,
            ),
            "exploration": dict(exploration),
            "final": {
                "model": decision.planner.model,
                "tier": _selected_tier(agents, decision.planner.name, planner_tier),
                "rationale": _rat("planner"),
            },
        },
        "dev": {
            "score": score,
            "base_tier_from_score": dev_base_tier,
            "candidate_pool": dev_pool,
            # Domain preference (#155): the story's tags, the matching profile
            # slice per candidate (on each pool entry's ``signals.domain``), and
            # whether the domain tiebreak changed the selection. Absent when the
            # story carried no domain tags.
            **({"domain_match": domain_block} if domain_block is not None else {}),
            "promotion_check": promotion_block,
            # Unified routing rationale (#1389, AC clause 4): the single field an
            # operator reads to see which symmetric path moved the dev tier —
            # stayed_at_preflight_tier, promoted_by <mechanism>, or (after the
            # post-plan checkpoint runs) demoted_by <mechanism>. Derived from the
            # deterministic checks below; apply_post_plan_checkpoint overwrites it
            # to demoted_by when its demotion fires.
            "routing_rationale": _dev_routing_rationale(
                promotion_block, dev_base_tier, dev_effective_tier
            ),
            # Dev-tier demotion/recovery (ADR-0006 clause 5 tier-demotion) is a
            # future enforcement (#1389) — no dev-tier demotion runs in v1, so this
            # records "no such mechanism ran" (a complete explanation, not a gap).
            # Provider-health demotion is reviewer-only; see each reviewer role's
            # own demotion_check for that live mechanism's outcome.
            "demotion_check": {
                "mechanism": "dev_tier_demotion",
                "applicable": False,
                "fired": False,
                "checked": None,
                "reason": "no_dev_tier_demotion_mechanism_v1",
            },
            # Post-plan dev-tier checkpoint (#1387). Preflight records the
            # not-yet-run sentinel; apply_post_plan_checkpoint() overwrites this
            # block with the real decision after plan-review completes.
            "post_plan_checkpoint": {
                "fired": False,
                "decision": "pending",
                "reason": "checkpoint_runs_after_plan_review",
            },
            # Challenger-sampling exploration (#325): the labeled, reconstructable
            # decision for the dev slot (mode + routing_key + pool + selection).
            # Falls back to the on-policy winner marker when the router did not
            # produce an exploration decision (static/explicit dev).
            "exploration": dev_exploration if dev_exploration is not None else dict(exploration),
            "final": {
                "model": decision.dev.model,
                "tier": _selected_tier(agents, decision.dev.name, dev_effective_tier),
                "rationale": _rat("dev"),
            },
        },
        "plan_review": {
            "candidate_pool": _reviewer_candidate_pool(
                agents,
                pr_selected,
                pr_exclude,
                "plan_review" in explicit_roles,
                secrets,
                health_deprioritized=pr_depri,
            ),
            "demotion_check": _reviewer_demotion_check(
                pr_fired, pr_depri, pr_fellback, unhealthy_models
            ),
            **({"completion_check": pr_completion_block} if pr_completion_block else {}),
            "exploration": dict(exploration),
            "final": {
                "models": [p.model for p in decision.plan_reviewers],
                "rationale": _rat("plan_review"),
            },
        },
        "code_review": {
            "candidate_pool": _reviewer_candidate_pool(
                agents,
                cr_selected,
                cr_exclude,
                "code_review" in explicit_roles,
                secrets,
                health_deprioritized=cr_depri,
            ),
            "demotion_check": _reviewer_demotion_check(
                cr_fired, cr_depri, cr_fellback, unhealthy_models
            ),
            **({"completion_check": cr_completion_block} if cr_completion_block else {}),
            "exploration": dict(exploration),
            "final": {
                "models": [p.model for p in decision.code_reviewers],
                "rationale": _rat("code_review"),
            },
        },
    }


def reconcile_explicit_reviewer_pools(
    routing_decision: dict[str, object],
    agents: list[AgentDef],
    *,
    plan_reviewers: list[ModelProfile] | None = None,
    code_reviewers: list[ModelProfile] | None = None,
    secrets: dict[str, str] | None = None,
) -> dict[str, object]:
    """Reconcile reviewer role blocks with explicit pools spliced post-assign.

    :func:`assign_models` only sees the FIRST override profile per reviewer role
    (``explicit_profiles`` carries one entry each), so when the coordinator
    splices a fuller explicit ``review_pool`` / ``plan_agent_review`` pool into
    the decision after ``assign_models`` returns, the block's ``final.models``
    and ``candidate_pool`` under-report the reviewers that actually run. This
    rebuilds only the affected reviewer role blocks from the real post-splice
    profiles so the persisted block stays reconstructable and consistent with
    runtime (#1391 iter1). Other roles and the block ``origin`` are preserved.

    Mutates and returns ``routing_decision`` for convenience.
    """
    if not routing_decision:
        return routing_decision

    def _rebuild(role: str, reviewers: list[ModelProfile]) -> None:
        role_block = routing_decision.get(role)
        if not isinstance(role_block, dict):
            return
        selected = {p.name for p in reviewers}
        # Explicit pools are operator-locked: agents outside the pool are locked
        # out; every profile that will run is an included candidate — even ones
        # not present in the adaptive ``agents`` registry.
        pool = _reviewer_candidate_pool(agents, selected, None, True, secrets)
        present = {e["name"] for e in pool}
        for p in reviewers:
            if p.name not in present:
                pool.append(
                    {"name": p.name, "tier": None, "included": True, "reason": REASON_NONE}
                )
        role_block["candidate_pool"] = pool
        final = role_block.get("final")
        if isinstance(final, dict):
            final["models"] = [p.model for p in reviewers]

    if plan_reviewers:
        _rebuild("plan_review", plan_reviewers)
    if code_reviewers:
        _rebuild("code_review", code_reviewers)
    return routing_decision


# ── Post-plan dev-tier checkpoint (#1387, absorbs #1109) ───────────────

# Enumerable rationale tokens for the post-plan checkpoint. Exhaustive so both
# over- and under-correction are observable from routing_decision history
# (ADR-0006 clause 7). ``plan_review_clean_medium`` is the only token that fires
# a downgrade; every other token records why the original tier was preserved
# (or the checkpoint skipped entirely).
POST_PLAN_CHECKPOINT_RATIONALES: frozenset[str] = frozenset(
    {
        "plan_review_clean_medium",
        "plan_tier_reduction_disabled",
        "explicit_dev_override",
        "complexity_not_medium",
        "plan_review_not_approve",
        "plan_review_cycles_exceeded",
        "plan_review_p1_present",
        "plan_review_p2_exceeded",
        "no_reduced_tier_candidate",
    }
)


def _reduced_tier(tier: str) -> str | None:
    """Return the tier exactly one step below ``tier`` (never two); None at floor."""
    idx = _TIER_ORDER.index(tier) if tier in _TIER_ORDER else None
    if idx is None or idx == 0:
        return None
    return _TIER_ORDER[idx - 1]


def apply_post_plan_checkpoint(
    decision: AssignmentDecision,
    agents: list[AgentDef],
    assignment_config: AssignmentConfig,
    complexity: str,
    *,
    plan_review_decision: str,
    plan_review_cycles: int,
    p1_count: int,
    p2_count: int,
    explicit_roles: set[str] | None = None,
    secrets: dict[str, str] | None = None,
    model_profiles: dict | None = None,
    domains: list[str] | None = None,
    recency: object | None = None,
) -> AssignmentDecision:
    """Re-evaluate ONLY the dev tier after plan-review completes (#1387).

    Pure deterministic function — no LLM, no I/O. This is the single post-plan
    dev-tier demotion mechanism (ADR-0006 clause 5 recovery side): a clean
    plan-review on a medium-band story permits stepping the preflight-assigned
    (effective) dev tier down by exactly one level (``strong→mid``, ``mid→cheap``,
    never two). Every gate condition below must hold, otherwise the original dev
    tier is preserved unchanged:

    * ``assignment.plan_tier_reduction`` is enabled
    * dev is not an explicit override / locked role
    * complexity is MEDIUM
    * plan-review verdict is APPROVE
    * plan-review cycle count is exactly 1
    * plan-review P1 == 0
    * plan-review P2 <= 1

    The demotion baseline is the preflight-assigned effective dev tier already
    recorded in ``decision.routing_decision['dev']['final']['tier']`` — the one
    baseline, so the reduction can never double-count the promotion ratchet.
    Every other role (preflight, planner, plan_review, code_review) is untouched.

    ``model_profiles``/``domains``/``recency`` are threaded into the demoted-tier
    agent pick so the cheaper tier is reranked by the same recency-weighted
    success rate and domain tiebreak as the original dev assignment (they are
    optional; omitting them falls back to the deterministic budget/price order).

    Records the outcome into ``routing_decision['dev']['post_plan_checkpoint']``
    with fired/decision/baseline_tier/final_tier/plan_present/rationale, and
    updates ``routing_decision['dev']['final']`` when the tier actually changed so
    the recorded final reflects the model that will run. Returns the (possibly
    dev-updated) AssignmentDecision; the routing_decision dict is mutated in place
    so callers sharing the reference observe the recorded decision.
    """
    from dataclasses import replace as _dc_replace  # noqa: PLC0415

    explicit_roles = explicit_roles or set()
    dev_block = (decision.routing_decision or {}).get("dev")
    dev_block = dev_block if isinstance(dev_block, dict) else None

    # Baseline = preflight-assigned effective dev tier. Prefer the recorded
    # final.tier; fall back to the selected dev agent's registry tier.
    baseline_tier: str | None = None
    if dev_block is not None:
        baseline_tier = (dev_block.get("final") or {}).get("tier")
    baseline_tier = _selected_tier(agents, decision.dev.name, baseline_tier)

    def _record(*, fired: bool, dec: str, rationale: str, final_tier: str | None) -> None:
        block = {
            "fired": fired,
            "decision": dec,
            "baseline_tier": baseline_tier,
            "final_tier": final_tier if final_tier is not None else baseline_tier,
            "plan_present": True,
            "rationale": rationale,
        }
        if dev_block is not None:
            dev_block["post_plan_checkpoint"] = block

    # ── Bypass paths (skipped) — operator intent / conservative config ──
    if not assignment_config.plan_tier_reduction:
        _record(
            fired=False,
            dec="skipped",
            rationale="plan_tier_reduction_disabled",
            final_tier=baseline_tier,
        )
        return decision
    if "dev" in explicit_roles:
        _record(
            fired=False,
            dec="skipped",
            rationale="explicit_dev_override",
            final_tier=baseline_tier,
        )
        return decision

    # ── Evidence gates (preserve on any failure) ───────────────────────
    norm_complexity = _normalize_complexity(complexity)
    if norm_complexity != "MEDIUM":
        _record(
            fired=False,
            dec="preserve",
            rationale="complexity_not_medium",
            final_tier=baseline_tier,
        )
        return decision
    if str(plan_review_decision).upper() != "APPROVE":
        _record(
            fired=False,
            dec="preserve",
            rationale="plan_review_not_approve",
            final_tier=baseline_tier,
        )
        return decision
    if plan_review_cycles != 1:
        _record(
            fired=False,
            dec="preserve",
            rationale="plan_review_cycles_exceeded",
            final_tier=baseline_tier,
        )
        return decision
    if p1_count != 0:
        _record(
            fired=False,
            dec="preserve",
            rationale="plan_review_p1_present",
            final_tier=baseline_tier,
        )
        return decision
    if p2_count > 1:
        _record(
            fired=False,
            dec="preserve",
            rationale="plan_review_p2_exceeded",
            final_tier=baseline_tier,
        )
        return decision

    # ── All gates passed — attempt the one-step demotion ───────────────
    # Select the demoted-tier agent with the SAME recency-weighted success-rate
    # reranking and domain tiebreak the original dev assignment used, so the
    # cheaper tier still routes to its best-performing model rather than the raw
    # budget-ordered default (#1387 review P2).
    target_tier = _reduced_tier(baseline_tier) if baseline_tier else None
    target_agent = (
        _pick_agent(
            agents,
            target_tier,
            secrets,
            model_profiles=model_profiles,
            role="dev",
            complexity=norm_complexity,
            domains=domains,
            recency=recency,
        )
        if target_tier is not None
        else None
    )
    if target_tier is None or target_agent is None:
        _record(
            fired=False,
            dec="preserve",
            rationale="no_reduced_tier_candidate",
            final_tier=baseline_tier,
        )
        return decision

    _record(
        fired=True,
        dec="downgrade",
        rationale="plan_review_clean_medium",
        final_tier=target_tier,
    )
    new_dev = _agent_to_profile(target_agent, role="dev")
    if dev_block is not None:
        # Overwrite the unified routing rationale (#1389): the post-plan
        # checkpoint is the concrete demotion that fired on this story, so the
        # operator sees demoted_by rather than the preflight-time stayed/promoted
        # verdict. baseline_tier is the preflight-assigned (possibly promoted)
        # tier, so from_tier→to_tier reads as the net path.
        dev_block["routing_rationale"] = {
            "state": ROUTING_RATIONALE_DEMOTED,
            "mechanism": MECHANISM_POST_PLAN_DEMOTION,
            "from_tier": baseline_tier,
            "to_tier": target_tier,
        }
        final = dev_block.get("final")
        if isinstance(final, dict):
            final["model"] = new_dev.model
            final["tier"] = target_tier
            _base_rat = final.get("rationale", "")
            final["rationale"] = (
                f"{_base_rat}; post-plan checkpoint demotion {baseline_tier} → "
                f"{target_tier} (clean plan-review on medium)"
            ).lstrip("; ")
    return _dc_replace(decision, dev=new_dev)


# ── Challenger-sampling exploration integration (#325) ─────────────────


@dataclass
class _DevExplorationResult:
    """Outcome of the dev-role exploration decision within :func:`assign_models`.

    ``block`` is the recorded exploration block (labeled, reconstructable) that
    is threaded into the routing_decision. ``route_agent`` is the agent the dev
    slot should actually run when it differs from the deterministic incumbent —
    either a fired challenger OR the audit-derived empirical winner when
    promotion/dethroning moved it off the static-tier pick (winner-mode routing
    must consume the empirical winner, not the stale incumbent). ``None`` means
    keep the deterministic pick (cold start, or the empirical winner already IS
    the incumbent). ``winner_name`` is the recorded winner id for the rationale.
    """

    block: dict[str, object]
    route_agent: AgentDef | None
    winner_name: str | None
    routing_key: str


def _apply_dev_exploration(
    *,
    agents: list[AgentDef],
    incumbent: AgentDef,
    dev_base_tier: str,
    norm_complexity: str,
    domains: list[str] | None,
    model_profiles: dict | None,
    recency: object | None,
    exploration_cfg: object,
    sprint_exploration_budget: int | None,
    secrets: dict[str, str] | None,
    rng: random.Random | None,
) -> _DevExplorationResult:
    """Compute the dev-role challenger-sampling decision (ADR-0006 clause 8).

    Pure except for the injected RNG's stochastic challenger draw. Delegates the
    policy (sample floor, cadence, cold start, sprint cap, recording) to
    :mod:`theforge.exploration`; this function builds the candidate pool from the
    agent registry, selects the audit-derived empirical winner (floor-compliant,
    so winner routing stays within the complexity-tier guardrail), and resolves
    the routed agent — challenger or promoted/dethroned winner — back to its
    :class:`AgentDef`. The exploration block is ALWAYS produced so the dev
    routing decision stays labeled even in on-policy winner mode.
    """
    from theforge import exploration as _exp  # noqa: PLC0415

    key = _exp.RoutingKey.build(phase="dev", complexity=norm_complexity, domains=domains)
    # Eligible challenger pool: every authed agent (dev role has no tier lock for
    # exploration — downward exploration to a cheaper tier is in scope, #170).
    eligible = [a for a in agents if _has_auth(a, secrets)]
    if incumbent not in eligible:
        eligible = [incumbent, *eligible]
    candidates = [
        _exp.Candidate(id=a.name, model=a.model, provider=a.provider, cli=a.cli, tier=a.tier)
        for a in eligible
    ]
    pool_ids = [c.id for c in candidates]

    cap = int(getattr(exploration_cfg, "per_sprint_cap", 0) or 0)
    # Exploration is only ACTIVE when the coordinator wired a sprint budget and
    # the cap is positive. Absent that (single-run/tests/no cap), the block is
    # recorded in on-policy winner mode — no challenger fires, so deterministic
    # routing is byte-for-byte unchanged.
    if sprint_exploration_budget is None or cap <= 0:
        block = _exp.ExplorationOutcome(
            mode=_exp.MODE_WINNER,
            routing_key=key.as_str(),
            pool=pool_ids,
            selected=incumbent.name,
            winner=incumbent.name,
            reason=_exp.REASON_ON_POLICY,
            domains=key.domains,
        ).to_block()
        return _DevExplorationResult(block, None, incumbent.name, key.as_str())

    min_sample = int(getattr(exploration_cfg, "min_sample_size", 3))
    aggregates = _exp.derive_key_aggregates(
        model_profiles,
        candidates,
        key,
        min_sample_size=min_sample,
        recency=recency,
    )
    # Winner selection stays consistent with routing policy: only floor-compliant
    # candidates (tier >= the complexity-required base tier) can be crowned, so a
    # promoted/dethroned winner never drops the dev slot below its guardrail. The
    # challenger pool remains unrestricted (downward exploration is challenger-only).
    floor_idx = _TIER_ORDER.index(dev_base_tier) if dev_base_tier in _TIER_ORDER else 0
    floor_ids = {
        a.name
        for a in eligible
        if a.tier in _TIER_ORDER and _TIER_ORDER.index(a.tier) >= floor_idx
    }
    winner_aggs = {mid: agg for mid, agg in aggregates.items() if mid in floor_ids}
    empirical_winner = _exp.select_winner(winner_aggs, min_sample)
    outcome = _exp.decide_exploration(
        key=key,
        candidates=candidates,
        aggregates=aggregates,
        # The recorded/routed winner is the AUDIT-DERIVED empirical winner, not
        # the deterministic static-tier incumbent — otherwise promotion and
        # dethroning never reach winner-mode routing.
        winner=empirical_winner,
        explore_every_n=int(getattr(exploration_cfg, "explore_every_n", 5)),
        min_sample_size=min_sample,
        sprint_budget_remaining=sprint_exploration_budget,
        rng=rng or random.Random(),
    )
    # Route to the selected model (challenger or promoted winner) when it differs
    # from the deterministic incumbent; cold start (selected is None) keeps the
    # static-tier pick.
    route_agent: AgentDef | None = None
    if outcome.selected is not None:
        cand_agent = next((a for a in eligible if a.name == outcome.selected), None)
        if cand_agent is not None and cand_agent.name != incumbent.name:
            route_agent = cand_agent
    return _DevExplorationResult(
        outcome.to_block(), route_agent, empirical_winner or incumbent.name, key.as_str()
    )


# ── Main public function ───────────────────────────────────────────────


def assign_models(
    agents: list[AgentDef],
    assignment_config: AssignmentConfig,
    complexity: str,
    complexity_score: int | None = None,
    escalation_history: list[EscalationRecord] | None = None,
    explicit_profiles: dict[str, ModelProfile] | None = None,
    sprint_promotions: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
    model_profiles: dict | None = None,
    unhealthy_models: set[str] | None = None,
    routing_origin: str = "preflight",
    domains: list[str] | None = None,
    excluded_for_taint: int = 0,
    sprint_exploration_budget: int | None = None,
    explore_rng: random.Random | None = None,
) -> AssignmentDecision:
    """Pure deterministic function — no LLM, no I/O.

    Returns an AssignmentDecision with model profiles for each phase.
    explicit_profiles keys: "preflight", "planner", "dev", "code_review", "plan_review"

    ``unhealthy_models`` is the set of agent names whose latest provider-shape
    failure (capacity, rate-limit, 5xx, quota) is still within the health
    window.  Reviewer selection deprioritizes them in favor of any same-tier
    or adjacent-tier healthy alternative; with no healthy alternative, the
    selection proceeds and surfaces the health context in the rationale.
    """
    if not agents:
        raise ValueError("assign_models requires a non-empty agents pool")

    explicit_profiles = explicit_profiles or {}
    history = escalation_history or []

    norm_complexity = _normalize_complexity(complexity)
    rationale: dict[str, str] = {}
    # ── Routing explainability accumulators (#1391) ────────────────────
    # Populated during the routing pass and consumed by _build_routing_decision
    # at the end so the block reflects the final (post-budget) decision.
    _dev_signals: dict[str, dict] = {}
    # Per-domain dev signals (#155), keyed by agent name, collected in the same
    # rerank pass as _dev_signals. _dev_domain_match records whether the domain
    # tiebreak actually moved the selection so the routing_decision block can mark
    # the decision influential or explicitly non-influential.
    _dev_domain_signals: dict[str, dict] = {}
    _dev_domain_match: dict[str, object] = {}
    # Reviewer completion-rate rerank accumulators (#1388), per reviewer role.
    # Populated by _select_reviewers and consumed by _build_routing_decision so
    # the routing_decision block records the consulted signal and ranking effect
    # only when reviewer completion actually shaped selection.
    _pr_completion_signals: dict[str, dict] = {}
    _pr_completion_audit: dict[str, object] = {}
    _cr_completion_signals: dict[str, dict] = {}
    _cr_completion_audit: dict[str, object] = {}
    _preflight_tier: str | None = None
    _dev_effective_tier: str = "cheap"
    # Challenger-sampling exploration block for the dev role (#325). None until
    # the dev pick is finalized; then set to the recorded exploration decision
    # (labeled winner/challenger with routing_key + pool). ``_dev_budget_floor``
    # holds the tier the budget enforcer must respect — the challenger's tier in
    # challenger mode so the run spends from the challenger's envelope (clause 8).
    _dev_exploration: dict[str, object] | None = None
    _dev_budget_floor: str | None = None
    _promotion_block: dict[str, object] = {
        "fired": False,
        "matching_records": 0,
        "escalations": 0,
        "outcome": "not_checked",
    }
    adaptive_enabled = assignment_config.adaptive_enabled
    # In static mode, ignore the numeric score, capability profiles, and
    # escalation/promotion learning — fall through to PHASE_TIER + min_reviewers.
    score = _normalize_complexity_score(complexity_score) if adaptive_enabled else None
    effective_history = history if adaptive_enabled else []
    effective_promotions = sprint_promotions if adaptive_enabled else None
    effective_profiles = model_profiles if adaptive_enabled else None
    # Recency-weighting params (#1392): the dev signal ranks on the decayed view
    # of admissible history. Only consulted under adaptive routing (static mode
    # ignores profile learning entirely).
    effective_recency = assignment_config.recency if adaptive_enabled else None
    # Domain preference only applies under adaptive routing; in static mode the
    # horizontal axis is ignored like the numeric score and profile learning.
    effective_domains = domains if adaptive_enabled else None
    locked_roles = set(explicit_profiles)
    if not adaptive_enabled:
        rationale["adaptive_enabled"] = "false (static band-only routing)"

    # ── Dev tier with promotion ────────────────────────────────────────
    dev_base_tier = (
        _dev_tier_for_score(norm_complexity, score)
        if adaptive_enabled
        else PHASE_TIER["dev"][norm_complexity]
    )

    # Check if dev profile is explicitly overridden
    if "dev" in explicit_profiles:
        dev_profile = explicit_profiles["dev"]
        dev_selected_tier: str | None = None
        rationale["dev"] = f"explicit override: {dev_profile.model}"
    else:
        # Check promotion
        dev_agent_for_check: AgentDef | None = _pick_agent(
            agents,
            dev_base_tier,
            secrets,
            model_profiles=effective_profiles,
            role="dev",
            complexity=norm_complexity,
            recency=effective_recency,
        )
        dev_model_name = dev_agent_for_check.name if dev_agent_for_check else ""
        dev_canonical = _agent_canonical_id(dev_agent_for_check) if dev_agent_for_check else None
        promoted = _check_promotion(
            norm_complexity,
            dev_model_name,
            effective_history,
            effective_promotions,
            dev_canonical_id=dev_canonical,
        )
        effective_dev_tier = dev_base_tier
        # Capture the promotion-check outcome for the routing_decision block
        # regardless of whether it fired — a checked-but-didn't-fire path is
        # part of the explanation. Uses the same matching slice _check_promotion
        # consulted, so no additional history scan drives selection.
        _promo_matching = [
            r
            for r in effective_history
            if r.complexity == norm_complexity
            and (r.dev_model == dev_model_name or (dev_canonical and r.dev_model == dev_canonical))
        ][-10:]
        _promo_escalations = sum(1 for r in _promo_matching if r.outcome == "ESCALATE")
        if promoted is not None:
            effective_dev_tier = _promote_tier(dev_base_tier)
            _promotion_block = {
                "fired": True,
                "matching_records": len(_promo_matching),
                "escalations": _promo_escalations,
                "outcome": f"promoted_to_{effective_dev_tier}",
            }
            # Use filtered matching records (same slice as _check_promotion uses)
            _matching = _promo_matching
            escalation_cnt = _promo_escalations
            rationale["dev"] = (
                f"{norm_complexity} dev promoted {dev_model_name} "
                f"(tier {dev_base_tier} → {effective_dev_tier}) — "
                f"{escalation_cnt}/10 recent {norm_complexity} stories escalated"
            )
        else:
            _promotion_block = {
                "fired": False,
                "matching_records": len(_promo_matching),
                "escalations": _promo_escalations,
                "outcome": "no_promotion",
            }
            if score is not None:
                rationale["dev"] = (
                    f"complexity score {score} ({norm_complexity}) → tier {effective_dev_tier}"
                )
            else:
                rationale["dev"] = f"{norm_complexity} complexity → tier {effective_dev_tier}"

        _dev_effective_tier = effective_dev_tier
        dev_agent = _pick_agent(
            agents,
            effective_dev_tier,
            secrets,
            model_profiles=effective_profiles,
            role="dev",
            complexity=norm_complexity,
            signals_out=_dev_signals,
            domains=effective_domains,
            domain_signals_out=_dev_domain_signals,
            rerank_audit=_dev_domain_match,
            recency=effective_recency,
        )
        if dev_agent is not None and effective_profiles:
            from theforge.model_profiles import get_dev_success_rate  # noqa: PLC0415

            _rate = get_dev_success_rate(
                model_profiles,
                dev_agent.name,
                norm_complexity,
                actual_model=dev_agent.model,
                provider=dev_agent.provider,
                cli=dev_agent.cli,
                recency=effective_recency,
            )
            if _rate is not None:
                rationale["dev"] += f" (profile success_rate={_rate:.2f} @ {norm_complexity})"
            # Domain match note (#155): only surfaced when the horizontal tiebreak
            # actually moved the selection — the selected model's admissible
            # per-domain rate over the story's domains.
            if _dev_domain_match.get("domain_influenced") and dev_agent is not None:
                _dsig = _dev_domain_signals.get(dev_agent.name)
                if _dsig and _dsig.get("rate") is not None:
                    rationale["dev"] += (
                        f" (domain match {effective_domains}: "
                        f"rate={_dsig['rate']:.2f} over {_dsig['runs']} runs)"
                    )
        if dev_agent is None:
            # Guardrail: dev tier floor prevents cheap models on MEDIUM/HIGH and
            # mid models on HIGH.  dev_base_tier is the floor (cheap/mid/strong
            # for LOW/MEDIUM/HIGH respectively).
            floor_idx = _TIER_ORDER.index(dev_base_tier) if dev_base_tier in _TIER_ORDER else 0
            authed = [a for a in agents if _has_auth(a, secrets)]
            floor_authed = [
                a
                for a in authed
                if a.tier in _TIER_ORDER and _TIER_ORDER.index(a.tier) >= floor_idx
            ]
            if floor_authed:
                dev_agent = sorted(floor_authed, key=lambda a: a.budget_usd)[0]
                rationale["dev"] += " (fallback: cheapest authed at floor tier)"
            else:
                # No authed agent meets the floor — try unauthed floor-compliant
                # agents before ever going below the floor.
                floor_any = [
                    a
                    for a in agents
                    if a.tier in _TIER_ORDER and _TIER_ORDER.index(a.tier) >= floor_idx
                ]
                if floor_any:
                    dev_agent = sorted(floor_any, key=lambda a: a.budget_usd)[0]
                    rationale["dev"] += " (fallback: cheapest at floor tier, no auth)"
                elif authed:
                    # No floor-compliant agent exists in the pool at all — pick
                    # highest available tier to minimise the violation.
                    best_tier_idx = max(
                        (_TIER_ORDER.index(a.tier) for a in authed if a.tier in _TIER_ORDER),
                        default=0,
                    )
                    best_authed = [a for a in authed if a.tier == _TIER_ORDER[best_tier_idx]]
                    dev_agent = sorted(best_authed or authed, key=lambda a: a.budget_usd)[0]
                    rationale["dev"] += (
                        f" (fallback: best available tier {_TIER_ORDER[best_tier_idx]};"
                        f" WARNING: below {dev_base_tier} floor for {norm_complexity})"
                    )
                else:
                    dev_agent = sorted(agents, key=lambda a: a.budget_usd)[0]
                    rationale["dev"] += " (fallback: cheapest, no auth checked)"
        dev_selected_tier = dev_agent.tier
        dev_profile = _agent_to_profile(dev_agent, role="dev")

        # ── Challenger-sampling exploration (#325, ADR-0006 clause 8) ──────
        # The single sanctioned deviation from deterministic routing. Only
        # under adaptive routing; the block is always recorded (labeled) so the
        # decision is reconstructable even in on-policy winner mode.
        if adaptive_enabled:
            _exp = _apply_dev_exploration(
                agents=agents,
                incumbent=dev_agent,
                dev_base_tier=dev_base_tier,
                norm_complexity=norm_complexity,
                domains=effective_domains,
                model_profiles=effective_profiles,
                recency=effective_recency,
                exploration_cfg=assignment_config.exploration,
                sprint_exploration_budget=sprint_exploration_budget,
                secrets=secrets,
                rng=explore_rng,
            )
            _dev_exploration = _exp.block
            if _exp.route_agent is not None:
                dev_agent = _exp.route_agent
                dev_selected_tier = dev_agent.tier
                dev_profile = _agent_to_profile(dev_agent, role="dev")
                _dev_effective_tier = dev_agent.tier
                if _dev_exploration.get("mode") == "challenger":
                    # Challenger-tier budget envelope (clause 8): the run spends
                    # from the challenger's tier, so the enforcer must not
                    # downgrade below it.
                    _dev_budget_floor = dev_agent.tier
                    rationale["dev"] += (
                        f"; EXPLORATION challenger {dev_agent.model} "
                        f"(tier {dev_agent.tier}) replaces winner {_exp.winner_name} "
                        f"for key {_exp.routing_key}"
                    )
                else:
                    # Winner-mode empirical promotion is NORMAL routing, not an
                    # exploration spend: leave the budget floor at dev_base_tier so
                    # the per-story cost target can still downgrade it to a
                    # floor-compliant cheaper dev (no protected envelope).
                    rationale["dev"] += (
                        f"; EXPLORATION empirical winner {dev_agent.model} "
                        f"(tier {dev_agent.tier}) routed for key {_exp.routing_key} "
                        f"(promotion/dethrone off the static-tier pick)"
                    )

    # ── Preflight ──────────────────────────────────────────────────────
    if "preflight" in explicit_profiles:
        preflight_profile = explicit_profiles["preflight"]
        rationale["preflight"] = f"explicit override: {preflight_profile.model}"
    else:
        tier = PHASE_TIER["preflight"][norm_complexity]
        _preflight_tier = tier
        agent = _pick_agent(agents, tier, secrets)
        if agent is None:
            authed = [a for a in agents if _has_auth(a, secrets)]
            agent = sorted(authed or agents, key=lambda a: a.budget_usd)[0]
        preflight_profile = _agent_to_profile(
            agent,
            role="preflight",
            allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
        )
        rationale["preflight"] = f"tier {tier} (${agent.budget_usd:.2f})"

    # ── Planner ────────────────────────────────────────────────────────
    if "planner" in explicit_profiles:
        planner_profile = explicit_profiles["planner"]
        planner_target_tier: str | None = None
        rationale["planner"] = f"explicit override: {planner_profile.model}"
    else:
        tier = (
            _plan_tier_for_score(norm_complexity, score)
            if adaptive_enabled
            else PHASE_TIER["plan"][norm_complexity]
        )
        planner_target_tier = tier
        agent = _pick_agent(agents, tier, secrets)
        if agent is None:
            authed = [a for a in agents if _has_auth(a, secrets)]
            agent = sorted(authed or agents, key=lambda a: -a.budget_usd)[0]
        planner_profile = _agent_to_profile(agent, role="review")
        if score is not None:
            rationale["planner"] = (
                f"complexity score {score} → tier {tier} (${agent.budget_usd:.2f})"
            )
        else:
            rationale["planner"] = f"tier {tier} (${agent.budget_usd:.2f})"

    # ── Plan reviewers ─────────────────────────────────────────────────
    if "plan_review" in explicit_profiles:
        plan_reviewers = [explicit_profiles["plan_review"]]
        rationale["plan_review"] = f"explicit override: {explicit_profiles['plan_review'].model}"
    else:
        tier = (
            _plan_tier_for_score(norm_complexity, score)
            if adaptive_enabled
            else PHASE_TIER["plan_review"][norm_complexity]
        )
        n = (
            _reviewer_target_for_score(
                norm_complexity,
                score,
                assignment_config.min_reviewers,
                assignment_config.max_reviewers,
            )
            if adaptive_enabled
            else _reviewer_count(
                norm_complexity,
                assignment_config.min_reviewers,
                assignment_config.max_reviewers,
            )
        )
        planner_model = planner_profile.model
        selected = _select_reviewers(
            agents,
            tier,
            n,
            assignment_config.prefer_cross_provider,
            exclude_model=planner_model,
            secrets=secrets,
            unhealthy_models=unhealthy_models,
            model_profiles=effective_profiles,
            completion_threshold=assignment_config.reviewer_completion_threshold,
            completion_min_runs=assignment_config.reviewer_completion_min_runs,
            recency=effective_recency,
            completion_signals_out=_pr_completion_signals,
            completion_audit=_pr_completion_audit,
        )
        plan_reviewers = [_agent_to_profile(a, role="review") for a in selected]
        providers = [a.effective_provider for a in selected]
        score_note = f", complexity score {score}" if score is not None else ""
        shortfall_note = (
            f" [WARNING: requested {n}, only {len(plan_reviewers)} available "
            f"after candidate-pool exhaustion (self-exclusion + cross-provider filter)]"
            if len(plan_reviewers) < n
            else ""
        )
        health_note = _reviewer_health_rationale(
            agents, selected, tier, exclude_model=planner_model, unhealthy_models=unhealthy_models
        )
        rationale["plan_review"] = (
            f"{len(plan_reviewers)} reviewer(s), tier {tier}, "
            f"providers {providers}{score_note}{shortfall_note}{health_note}"
        )

    # ── Code reviewers ─────────────────────────────────────────────────
    if "code_review" in explicit_profiles:
        code_reviewers = [explicit_profiles["code_review"]]
        rationale["code_review"] = f"explicit override: {explicit_profiles['code_review'].model}"
    else:
        tier = (
            _plan_tier_for_score(norm_complexity, score)
            if adaptive_enabled
            else PHASE_TIER["code_review"][norm_complexity]
        )
        n = (
            _reviewer_target_for_score(
                norm_complexity,
                score,
                assignment_config.min_reviewers,
                assignment_config.max_reviewers,
            )
            if adaptive_enabled
            else _reviewer_count(
                norm_complexity,
                assignment_config.min_reviewers,
                assignment_config.max_reviewers,
            )
        )
        dev_model = dev_profile.model
        selected = _select_reviewers(
            agents,
            tier,
            n,
            assignment_config.prefer_cross_provider,
            exclude_model=dev_model,
            secrets=secrets,
            unhealthy_models=unhealthy_models,
            model_profiles=effective_profiles,
            completion_threshold=assignment_config.reviewer_completion_threshold,
            completion_min_runs=assignment_config.reviewer_completion_min_runs,
            recency=effective_recency,
            completion_signals_out=_cr_completion_signals,
            completion_audit=_cr_completion_audit,
        )
        code_reviewers = [_agent_to_profile(a, role="review") for a in selected]
        providers = [a.effective_provider for a in selected]
        score_note = f", complexity score {score}" if score is not None else ""
        shortfall_note = (
            f" [WARNING: requested {n}, only {len(code_reviewers)} available "
            f"after candidate-pool exhaustion (self-exclusion + cross-provider filter)]"
            if len(code_reviewers) < n
            else ""
        )
        health_note = _reviewer_health_rationale(
            agents, selected, tier, exclude_model=dev_model, unhealthy_models=unhealthy_models
        )
        rationale["code_review"] = (
            f"{len(code_reviewers)} reviewer(s), tier {tier}, "
            f"providers {providers}{score_note}{shortfall_note}{health_note}"
        )

    decision = AssignmentDecision(
        preflight=preflight_profile,
        planner=planner_profile,
        plan_reviewers=plan_reviewers,
        dev=dev_profile,
        code_reviewers=code_reviewers,
        rationale=rationale,
    )

    def _attach_routing_decision(dec: AssignmentDecision) -> AssignmentDecision:
        """Build the explainability block from the FINAL decision and attach it.

        Called at every return so budget-driven downgrades are reflected in the
        recorded final models/tiers (plan-review note P1-impl).
        """
        from dataclasses import replace as _dc_replace  # noqa: PLC0415

        block = _build_routing_decision(
            dec,
            agents,
            origin=routing_origin,
            score=score,
            dev_base_tier=dev_base_tier,
            dev_effective_tier=_dev_effective_tier,
            preflight_tier=_preflight_tier,
            planner_tier=planner_target_tier,
            dev_signals=_dev_signals,
            promotion_block=_promotion_block,
            planner_model=dec.planner.model,
            dev_model=dec.dev.model,
            explicit_roles=set(explicit_profiles),
            secrets=secrets,
            unhealthy_models=unhealthy_models,
            domains=effective_domains,
            dev_domain_signals=_dev_domain_signals,
            dev_domain_match=_dev_domain_match,
            excluded_for_taint=excluded_for_taint,
            dev_exploration=_dev_exploration,
            pr_completion_signals=_pr_completion_signals,
            pr_completion_audit=_pr_completion_audit,
            cr_completion_signals=_cr_completion_signals,
            cr_completion_audit=_cr_completion_audit,
        )
        return _dc_replace(dec, routing_decision=block)

    # Enforce per-story routing cost target — pass dev floor so the enforcer never
    # downgrades dev below the complexity-required tier.  When the cap is unset
    # (None), adaptive's selection is preserved as-is and only the sprint-wide
    # budget_usd guard remains.
    cap = assignment_config.max_cost_per_story_usd
    if cap is None:
        for _role in ("planner", "dev", "plan_review", "code_review"):
            if _role in rationale:
                rationale[_role] += "; per-story routing cost target: unset"
        rationale["per_story_routing_cost_target"] = (
            "unset — adaptive routes by complexity; no per-story routing cost target enforcement"
        )
        from dataclasses import replace as _dc_replace

        _initial_total = _decision_total(decision)
        decision = _dc_replace(
            decision,
            rationale=rationale,
            budget_audit={
                "target_usd": None,
                "initial_total_usd": round(_initial_total, 2),
                "final_total_usd": round(_initial_total, 2),
                "within_target": True,
                "downgraded": False,
                "steps": [],
                "preferred": _preferred_snapshot(decision, _initial_total),
            },
        )
        return _attach_routing_decision(decision)

    planner_floor_tier = None
    if (
        planner_target_tier == "strong"
        and dev_selected_tier == "strong"
        and "planner" not in explicit_profiles
    ):
        planner_floor_tier = "strong"
    # Challenger-tier budget envelope (#325 clause 8): when a challenger fires,
    # budget eligibility is evaluated against the challenger's tier — the run
    # spends from the challenger's envelope, not the incumbent winner's — so the
    # enforcer must not downgrade below the challenger's tier.
    _dev_floor = _dev_budget_floor or dev_base_tier
    decision = _enforce_budget(
        decision,
        agents,
        cap,
        dev_floor_tier=_dev_floor,
        planner_floor_tier=planner_floor_tier,
        locked_roles=locked_roles,
    )

    return _attach_routing_decision(decision)


# ── I/O helpers (used only by coordinator) ────────────────────────────


def escalation_records_from_dicts(dicts: list[dict]) -> list[EscalationRecord]:
    """Coerce derived assignment-history dicts into :class:`EscalationRecord`s.

    The dicts follow the same shape produced by
    :func:`theforge.coordinator.audit_substrate.derive_assignment_history`:
    a chronologically-ordered list with story, complexity, dev_model,
    outcome, reason, timestamp, and complexity_score fields.
    """
    result: list[EscalationRecord] = []
    for r in dicts:
        if not isinstance(r, dict):
            continue
        raw_score = r.get("complexity_score")
        if isinstance(raw_score, bool):
            score: int | None = None
        elif isinstance(raw_score, int):
            score = raw_score
        elif isinstance(raw_score, float):
            score = int(raw_score)
        else:
            score = None
        result.append(
            EscalationRecord(
                story=str(r.get("story", "")),
                complexity=str(r.get("complexity", "")),
                dev_model=str(r.get("dev_model", "")),
                outcome=str(r.get("outcome", "")),
                reason=str(r.get("reason", "")),
                timestamp=str(r.get("timestamp", "")),
                complexity_score=score,
            )
        )
    return result


def load_escalation_history(path: Path) -> list[EscalationRecord]:
    """Read .forge/assignment_history.yaml; return [] if missing or malformed."""
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return []
        records = data.get("escalations", [])
        if not isinstance(records, list):
            return []
        result = []
        for r in records:
            if not isinstance(r, dict):
                continue
            raw_score = r.get("complexity_score")
            if isinstance(raw_score, bool):
                score: int | None = None
            elif isinstance(raw_score, int):
                score = raw_score
            elif isinstance(raw_score, float):
                score = int(raw_score)
            else:
                score = None
            result.append(
                EscalationRecord(
                    story=str(r.get("story", "")),
                    complexity=str(r.get("complexity", "")),
                    dev_model=str(r.get("dev_model", "")),
                    outcome=str(r.get("outcome", "")),
                    reason=str(r.get("reason", "")),
                    timestamp=str(r.get("timestamp", "")),
                    complexity_score=score,
                )
            )
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("[adaptive] Failed to load escalation history: %s", exc)
        return []


def append_escalation_record(path: Path, record: EscalationRecord) -> None:
    """Append one record to .forge/assignment_history.yaml, creating if absent."""
    existing: list[dict] = []
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict) and isinstance(data.get("escalations"), list):
                existing = data["escalations"]
        except Exception as exc:  # noqa: BLE001
            log.warning("[adaptive] Could not read assignment_history.yaml: %s", exc)

    new_entry = {
        "story": record.story,
        "complexity": record.complexity,
        "dev_model": record.dev_model,
        "outcome": record.outcome,
    }
    if record.reason:
        new_entry["reason"] = record.reason
    if record.timestamp:
        new_entry["timestamp"] = record.timestamp
    if record.complexity_score is not None:
        new_entry["complexity_score"] = int(record.complexity_score)

    existing.append(new_entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({"escalations": existing}, f, default_flow_style=False, allow_unicode=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[adaptive] Failed to write assignment_history.yaml: %s", exc)
