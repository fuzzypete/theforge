"""Adaptive model assignment — pure deterministic routing with escalation learning.

All public functions in this module are pure (no I/O, no LLM calls) except for
the two I/O helpers load_escalation_history() and append_escalation_record(),
which are called only by the coordinator.
"""

from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    PROVIDER_API_KEY_MAP,
    AgentDef,
    AssignmentConfig,
    ModelProfile,
)

log = logging.getLogger(__name__)


def _has_auth(agent: AgentDef) -> bool:
    """Return True if the agent's provider has usable auth.

    CLI agents (provider is None) always have auth (the CLI handles its own).
    API agents need their provider's API key in the environment.
    """
    if not agent.provider:
        return True  # CLI agent — auth handled by the CLI binary
    key_var = PROVIDER_API_KEY_MAP.get(agent.provider)
    if not key_var:
        return True  # Unknown provider — assume OK
    return bool(os.getenv(key_var))


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


def _agents_by_tier(agents: list[AgentDef], tier: str) -> list[AgentDef]:
    """Return agents matching tier, sorted by budget_usd ascending."""
    matches = [a for a in agents if a.tier == tier]
    return sorted(matches, key=lambda a: a.budget_usd)


def _pick_agent(agents: list[AgentDef], tier: str) -> AgentDef | None:
    """Pick cheapest agent of the given tier that has usable auth.

    Skips API agents whose provider key is missing from the environment.
    """
    candidates = [a for a in _agents_by_tier(agents, tier) if _has_auth(a)]
    if not candidates:
        log.debug("No authed agents for tier %s — trying any tier with auth", tier)
    return candidates[0] if candidates else None


def _promote_tier(tier: str) -> str:
    """Promote tier one step up; returns same tier if already at max."""
    idx = _TIER_ORDER.index(tier) if tier in _TIER_ORDER else len(_TIER_ORDER) - 1
    return _TIER_ORDER[min(idx + 1, len(_TIER_ORDER) - 1)]


def _select_reviewers(
    agents: list[AgentDef],
    tier: str,
    n: int,
    prefer_cross_provider: bool,
    exclude_model: str | None = None,
) -> list[AgentDef]:
    """Select n reviewer agents from the pool.

    Prefer strong-tier agents; fall back to tier if needed.
    Break ties by lowest budget_usd.
    If prefer_cross_provider, greedily pick from different providers.
    If exclude_model is set, agents with that model are excluded; they are
    only included as a last resort when no other candidates are available.
    """
    # Build candidate list: prefer strong, fall back to requested tier
    # Filter by auth availability — skip agents whose API key is missing
    strong = [a for a in _agents_by_tier(agents, "strong") if _has_auth(a)]
    tier_agents = (
        [a for a in _agents_by_tier(agents, tier) if _has_auth(a)] if tier != "strong" else []
    )
    # Merge: strong first, then same-tier, deduplicated
    seen_names: set[str] = set()
    candidates: list[AgentDef] = []
    for a in strong + tier_agents:
        if a.name not in seen_names:
            candidates.append(a)
            seen_names.add(a.name)

    # If no authed candidates exist, fall back to unauthed agents so the pool
    # is never empty (mirrors the fallback logic in assign_models for dev/planner).
    if not candidates:
        strong_any = _agents_by_tier(agents, "strong")
        tier_any = _agents_by_tier(agents, tier) if tier != "strong" else []
        seen_names = set()
        for a in strong_any + tier_any:
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
            preferred = [a for a in agents if _has_auth(a) and a.model != exclude_model]
        if not preferred:
            # Widen further: any agent regardless of auth with a different model
            preferred = [a for a in agents if a.model != exclude_model]
        if preferred:
            candidates = preferred

    if not prefer_cross_provider:
        return candidates[:n]

    # Greedy cross-provider selection
    selected: list[AgentDef] = []
    used_providers: set[str] = set()

    # First pass: pick from different providers
    for a in candidates:
        if len(selected) >= n:
            break
        if a.provider not in used_providers:
            selected.append(a)
            used_providers.add(a.provider)

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
) -> str | None:
    """Return promoted tier string if promotion is warranted, else None.

    Checks sprint_promotions cache first (sticky within sprint).
    Looks at last 10 records matching complexity+dev_model.
    Promotes if 2+ have outcome=ESCALATE.
    """
    if sprint_promotions and complexity in sprint_promotions:
        return sprint_promotions[complexity]

    # Filter to last 10 matching records
    matching = [
        r for r in history if r.complexity == complexity and r.dev_model == dev_agent_name
    ][-10:]

    if not matching:
        return None

    escalation_count = sum(1 for r in matching if r.outcome == "ESCALATE")
    if escalation_count >= 2:
        return "promoted"  # signal to caller to promote
    return None


def _enforce_budget(
    decision: AssignmentDecision,
    agents: list[AgentDef],
    budget_per_story_usd: float,
) -> AssignmentDecision:
    """Downgrade highest-cost non-preflight model if over budget cap."""
    from dataclasses import replace as _dc_replace

    def _total(d: AssignmentDecision) -> float:
        return (
            d.preflight.budget_usd
            + d.planner.budget_usd
            + sum(p.budget_usd for p in d.plan_reviewers)
            + d.dev.budget_usd
            + sum(p.budget_usd for p in d.code_reviewers)
        )

    if _total(decision) <= budget_per_story_usd:
        return decision

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

    # Iteratively downgrade until within budget (max 10 passes)
    for _ in range(10):
        if _total(decision) <= budget_per_story_usd:
            break

        # Find highest-cost non-preflight, non-planner profile that can be downgraded
        # Candidates: dev, code_reviewers (exclude preflight to preserve quality).
        # All profiles are candidates — _next_cheaper_profile handles pool lookup.
        candidates = []
        candidates.append(("dev", decision.dev))
        for i, p in enumerate(decision.code_reviewers):
            candidates.append((f"code_review_{i}", p))

        if not candidates:
            break

        # Sort by budget descending to downgrade most expensive first
        candidates.sort(key=lambda x: x[1].budget_usd, reverse=True)
        downgraded = False
        for role, profile in candidates:
            cheaper = _next_cheaper_profile(profile)
            if cheaper is not None:
                if role == "dev":
                    decision = _dc_replace(decision, dev=cheaper)
                else:
                    i = int(role.split("_")[-1])
                    new_reviewers = list(decision.code_reviewers)
                    new_reviewers[i] = cheaper
                    decision = _dc_replace(decision, code_reviewers=new_reviewers)
                downgraded = True
                break

        if not downgraded:
            # Can't downgrade any model; try removing a code reviewer (keep min 1)
            if len(decision.code_reviewers) > 1:
                decision = _dc_replace(decision, code_reviewers=decision.code_reviewers[:-1])
            else:
                break

    if _total(decision) > budget_per_story_usd:
        warnings.warn(
            f"[adaptive] Budget cap ${budget_per_story_usd:.2f} cannot be met; "
            f"actual total ${_total(decision):.2f}",
            stacklevel=2,
        )

    return decision


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
    )


# ── Main public function ───────────────────────────────────────────────


def assign_models(
    agents: list[AgentDef],
    assignment_config: AssignmentConfig,
    complexity: str,
    escalation_history: list[EscalationRecord] | None = None,
    explicit_profiles: dict[str, ModelProfile] | None = None,
    sprint_promotions: dict[str, str] | None = None,
) -> AssignmentDecision:
    """Pure deterministic function — no LLM, no I/O.

    Returns an AssignmentDecision with model profiles for each phase.
    explicit_profiles keys: "preflight", "planner", "dev", "code_review", "plan_review"
    """
    if not agents:
        raise ValueError("assign_models requires a non-empty agents pool")

    explicit_profiles = explicit_profiles or {}
    history = escalation_history or []

    norm_complexity = _normalize_complexity(complexity)
    rationale: dict[str, str] = {}

    # ── Dev tier with promotion ────────────────────────────────────────
    dev_base_tier = PHASE_TIER["dev"][norm_complexity]

    # Check if dev profile is explicitly overridden
    if "dev" in explicit_profiles:
        dev_profile = explicit_profiles["dev"]
        rationale["dev"] = f"explicit override: {dev_profile.model}"
    else:
        # Check promotion
        dev_agent_for_check: AgentDef | None = _pick_agent(agents, dev_base_tier)
        dev_model_name = dev_agent_for_check.name if dev_agent_for_check else ""
        promoted = _check_promotion(norm_complexity, dev_model_name, history, sprint_promotions)
        effective_dev_tier = dev_base_tier
        if promoted is not None:
            effective_dev_tier = _promote_tier(dev_base_tier)
            # Use filtered matching records (same slice as _check_promotion uses)
            _matching = [
                r
                for r in history
                if r.complexity == norm_complexity and r.dev_model == dev_model_name
            ][-10:]
            escalation_cnt = sum(1 for r in _matching if r.outcome == "ESCALATE")
            rationale["dev"] = (
                f"{norm_complexity} dev promoted {dev_model_name} "
                f"(tier {dev_base_tier} → {effective_dev_tier}) — "
                f"{escalation_cnt}/10 recent {norm_complexity} stories escalated"
            )
        else:
            rationale["dev"] = f"{norm_complexity} complexity → tier {effective_dev_tier}"

        dev_agent = _pick_agent(agents, effective_dev_tier)
        if dev_agent is None:
            # Fall back to any authed agent
            authed = [a for a in agents if _has_auth(a)]
            if authed:
                dev_agent = sorted(authed, key=lambda a: a.budget_usd)[0]
                rationale["dev"] += " (fallback: cheapest authed)"
            else:
                dev_agent = sorted(agents, key=lambda a: a.budget_usd)[0]
                rationale["dev"] += " (fallback: cheapest, no auth checked)"
        dev_profile = _agent_to_profile(dev_agent, role="dev")

    # ── Preflight ──────────────────────────────────────────────────────
    if "preflight" in explicit_profiles:
        preflight_profile = explicit_profiles["preflight"]
        rationale["preflight"] = f"explicit override: {preflight_profile.model}"
    else:
        tier = PHASE_TIER["preflight"][norm_complexity]
        agent = _pick_agent(agents, tier)
        if agent is None:
            authed = [a for a in agents if _has_auth(a)]
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
        rationale["planner"] = f"explicit override: {planner_profile.model}"
    else:
        tier = PHASE_TIER["plan"][norm_complexity]
        agent = _pick_agent(agents, tier)
        if agent is None:
            authed = [a for a in agents if _has_auth(a)]
            agent = sorted(authed or agents, key=lambda a: -a.budget_usd)[0]
        planner_profile = _agent_to_profile(agent, role="review")
        rationale["planner"] = f"tier {tier} (${agent.budget_usd:.2f})"

    # ── Plan reviewers ─────────────────────────────────────────────────
    if "plan_review" in explicit_profiles:
        plan_reviewers = [explicit_profiles["plan_review"]]
        rationale["plan_review"] = f"explicit override: {explicit_profiles['plan_review'].model}"
    else:
        tier = PHASE_TIER["plan_review"][norm_complexity]
        n = _reviewer_count(
            norm_complexity,
            assignment_config.min_reviewers,
            assignment_config.max_reviewers,
        )
        planner_model = planner_profile.model
        selected = _select_reviewers(
            agents, tier, n, assignment_config.prefer_cross_provider, exclude_model=planner_model
        )
        plan_reviewers = [_agent_to_profile(a, role="review") for a in selected]
        providers = [a.provider for a in selected]
        rationale["plan_review"] = (
            f"{len(plan_reviewers)} reviewer(s), tier {tier}, providers {providers}"
        )

    # ── Code reviewers ─────────────────────────────────────────────────
    if "code_review" in explicit_profiles:
        code_reviewers = [explicit_profiles["code_review"]]
        rationale["code_review"] = f"explicit override: {explicit_profiles['code_review'].model}"
    else:
        tier = PHASE_TIER["code_review"][norm_complexity]
        n = _reviewer_count(
            norm_complexity,
            assignment_config.min_reviewers,
            assignment_config.max_reviewers,
        )
        dev_model = dev_profile.model
        selected = _select_reviewers(
            agents, tier, n, assignment_config.prefer_cross_provider, exclude_model=dev_model
        )
        code_reviewers = [_agent_to_profile(a, role="review") for a in selected]
        providers = [a.provider for a in selected]
        rationale["code_review"] = (
            f"{len(code_reviewers)} reviewer(s), tier {tier}, providers {providers}"
        )

    decision = AssignmentDecision(
        preflight=preflight_profile,
        planner=planner_profile,
        plan_reviewers=plan_reviewers,
        dev=dev_profile,
        code_reviewers=code_reviewers,
        rationale=rationale,
    )

    # Enforce budget cap
    decision = _enforce_budget(decision, agents, assignment_config.budget_per_story_usd)

    return decision


# ── I/O helpers (used only by coordinator) ────────────────────────────


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
            result.append(
                EscalationRecord(
                    story=str(r.get("story", "")),
                    complexity=str(r.get("complexity", "")),
                    dev_model=str(r.get("dev_model", "")),
                    outcome=str(r.get("outcome", "")),
                    reason=str(r.get("reason", "")),
                    timestamp=str(r.get("timestamp", "")),
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

    existing.append(new_entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({"escalations": existing}, f, default_flow_style=False, allow_unicode=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[adaptive] Failed to write assignment_history.yaml: %s", exc)
