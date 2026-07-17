"""Adaptive model assignment — pure deterministic routing with escalation learning.

All public functions in this module are pure (no I/O, no LLM calls) except for
the two I/O helpers load_escalation_history() and append_escalation_record(),
which are called only by the coordinator.
"""

from __future__ import annotations

import logging
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
from .routing import score_to_dev_tier

log = logging.getLogger(__name__)


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
    """Return agents matching tier, sorted by budget_usd ascending."""
    matches = [a for a in agents if a.tier == tier]
    return sorted(matches, key=lambda a: a.budget_usd)


def _rerank_by_profiles(
    candidates: list[AgentDef],
    model_profiles: dict | None,
    role: str,
    complexity: str | None,
    min_runs: int = 3,
) -> list[AgentDef]:
    """Stable-sort candidates: high-success-rate first when enough data exists.

    Only role="dev" is profile-aware today; other roles pass through unchanged.
    Candidates without ``min_runs`` observations retain their original relative
    order (sort is stable) so the cheapest-budget tie-break still wins when
    nobody has a track record yet.
    """
    if not model_profiles or role != "dev":
        return candidates
    from theforge.model_profiles import get_dev_success_rate  # noqa: PLC0415

    def _key(agent: AgentDef) -> tuple[int, float]:
        rate = get_dev_success_rate(
            model_profiles,
            agent.name,
            complexity,
            min_runs,
            actual_model=agent.model,
            provider=agent.provider,
            cli=agent.cli,
        )
        if rate is None:
            return (1, 0.0)
        # Negative rate so higher success sorts first among observed agents.
        return (0, -rate)

    return sorted(candidates, key=_key)


def _pick_agent(
    agents: list[AgentDef],
    tier: str,
    secrets: dict[str, str] | None = None,
    model_profiles: dict | None = None,
    role: str = "",
    complexity: str | None = None,
) -> AgentDef | None:
    """Pick cheapest agent of the given tier that has usable auth.

    Skips API agents whose provider key is missing from the environment.
    When ``model_profiles`` is provided and ``role == "dev"``, agents with a
    higher observed success rate at the given complexity are preferred over
    the budget-ordered default.
    """
    candidates = [a for a in _agents_by_tier(agents, tier) if _has_auth(a, secrets)]
    candidates = _rerank_by_profiles(candidates, model_profiles, role, complexity)
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


def _select_reviewers(
    agents: list[AgentDef],
    tier: str,
    n: int,
    prefer_cross_provider: bool,
    exclude_model: str | None = None,
    secrets: dict[str, str] | None = None,
    unhealthy_models: set[str] | None = None,
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
        rationale["per_story_routing_cost_target"] += f" (protected roles: {sorted(protected_roles)})"

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
    adaptive_enabled = assignment_config.adaptive_enabled
    # In static mode, ignore the numeric score, capability profiles, and
    # escalation/promotion learning — fall through to PHASE_TIER + min_reviewers.
    score = _normalize_complexity_score(complexity_score) if adaptive_enabled else None
    effective_history = history if adaptive_enabled else []
    effective_promotions = sprint_promotions if adaptive_enabled else None
    effective_profiles = model_profiles if adaptive_enabled else None
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
        if promoted is not None:
            effective_dev_tier = _promote_tier(dev_base_tier)
            # Use filtered matching records (same slice as _check_promotion uses)
            _matching = [
                r
                for r in effective_history
                if r.complexity == norm_complexity
                and (
                    r.dev_model == dev_model_name
                    or (dev_canonical and r.dev_model == dev_canonical)
                )
            ][-10:]
            escalation_cnt = sum(1 for r in _matching if r.outcome == "ESCALATE")
            rationale["dev"] = (
                f"{norm_complexity} dev promoted {dev_model_name} "
                f"(tier {dev_base_tier} → {effective_dev_tier}) — "
                f"{escalation_cnt}/10 recent {norm_complexity} stories escalated"
            )
        else:
            if score is not None:
                rationale["dev"] = (
                    f"complexity score {score} ({norm_complexity}) → tier {effective_dev_tier}"
                )
            else:
                rationale["dev"] = f"{norm_complexity} complexity → tier {effective_dev_tier}"

        dev_agent = _pick_agent(
            agents,
            effective_dev_tier,
            secrets,
            model_profiles=effective_profiles,
            role="dev",
            complexity=norm_complexity,
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
            )
            if _rate is not None:
                rationale["dev"] += f" (profile success_rate={_rate:.2f} @ {norm_complexity})"
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

    # ── Preflight ──────────────────────────────────────────────────────
    if "preflight" in explicit_profiles:
        preflight_profile = explicit_profiles["preflight"]
        rationale["preflight"] = f"explicit override: {preflight_profile.model}"
    else:
        tier = PHASE_TIER["preflight"][norm_complexity]
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
        return decision

    planner_floor_tier = None
    if (
        planner_target_tier == "strong"
        and dev_selected_tier == "strong"
        and "planner" not in explicit_profiles
    ):
        planner_floor_tier = "strong"
    decision = _enforce_budget(
        decision,
        agents,
        cap,
        dev_floor_tier=dev_base_tier,
        planner_floor_tier=planner_floor_tier,
        locked_roles=locked_roles,
    )

    return decision


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
