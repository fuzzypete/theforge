"""Role derivation logic: map a simple model list to a RoleAssignment.

derive_roles() is the insertion point for future adaptive routing (v0.9).
A router can replace this function and return a RoleAssignment without
restructuring the type hierarchy. The function signature is stable: it
accepts a list of model keys and optional per-role overrides and returns
a RoleAssignment that the bridge module converts to ModelProfile instances.
"""

from __future__ import annotations

from typing import Any

from .defaults import DEFAULT_DEV_PROFILE, DEFAULT_PREFLIGHT_PROFILE, DEFAULT_REVIEW_PROFILE
from .models import ModelInfo, _resolve_model_info
from .schema import (
    DevRoleConfig,
    ModelRef,
    PlanRoleConfig,
    PreflightRoleConfig,
    ReviewRoleConfig,
    RoleAssignment,
)

# Default plan budget and timeout, matching PlanConfig defaults in types.py
_DEFAULT_PLAN_BUDGET_USD: float = 0.50
_DEFAULT_PLAN_TIMEOUT_SECONDS: int = 600

# Tier × complexity routing table.  The canonical reference for v0.8 role selection.
# cost_rank integers: 1=cheap, 2=mid, 3=strong  (matching AgentDef tier vocabulary).
# preflight is intentionally absent — it uses a static assignment regardless of complexity.
_COMPLEXITY_TIER: dict[str, dict[str, str]] = {
    "plan": {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"},
    "dev": {"LOW": "cheap", "MEDIUM": "mid", "HIGH": "strong"},
    "review": {"LOW": "mid", "MEDIUM": "mid", "HIGH": "strong"},
}

# Bidirectional mapping between tier name and cost_rank integer.
_COST_RANK_TO_TIER: dict[int, str] = {1: "cheap", 2: "mid", 3: "strong"}
_TIER_TO_COST_RANK: dict[str, int] = {"cheap": 1, "mid": 2, "strong": 3}

# Normalise loose complexity strings to canonical levels.
_COMPLEXITY_NORM: dict[str, str] = {
    "small": "LOW",
    "low": "LOW",
    "medium": "MEDIUM",
    "large": "HIGH",
    "high": "HIGH",
}


def _score_to_dev_tier(score: int) -> str:
    """Map a 1-10 complexity score to a dev-phase model tier."""
    if score <= 3:
        return "cheap"
    if score <= 6:
        return "mid"
    return "strong"


def _pick_by_tier(
    candidates: list[tuple[str, ModelInfo]],
    target_tier: str,
) -> tuple[str, ModelInfo]:
    """Pick the cheapest model matching target_tier; fall back to nearest available tier.

    Candidates must be pre-sorted (cheapest cost_rank first, capability desc within rank).
    Falls back upward (higher rank) before falling back downward (lower rank).
    """
    target_rank = _TIER_TO_COST_RANK.get(target_tier, 2)

    exact = [(k, i) for k, i in candidates if i.cost_rank == target_rank]
    if exact:
        return exact[0]

    for rank in range(target_rank + 1, 4):
        higher = [(k, i) for k, i in candidates if i.cost_rank == rank]
        if higher:
            return higher[0]

    for rank in range(target_rank - 1, 0, -1):
        lower = [(k, i) for k, i in candidates if i.cost_rank == rank]
        if lower:
            return lower[0]

    return candidates[0]


def _make_model_ref(
    *,
    info: ModelInfo,
    budget_usd: float,
    timeout_seconds: int,
) -> ModelRef:
    """Construct a ModelRef from resolved model info, carrying transport through."""
    return ModelRef(
        model=info.model,
        cli=info.cli,
        provider=info.provider,
        budget_usd=budget_usd,
        timeout_seconds=timeout_seconds,
        transport=info.transport,
    )


def _phase_candidates(
    sorted_models: list[tuple[str, ModelInfo]],
    phase: str,
) -> list[tuple[str, ModelInfo]]:
    """Filter candidates by AgentSpec.phase_eligibility for the given phase.

    Role derivation reads phase_eligibility (carried through from AgentSpec) so
    a model explicitly excluded from, e.g., preflight cannot be selected for it.
    Falls back to the full list if filtering leaves nothing selectable.
    """
    eligible = [(k, i) for k, i in sorted_models if phase in i.phase_eligibility]
    return eligible if eligible else sorted_models


def _apply_ref_overrides(ref: ModelRef, overrides: dict[str, Any]) -> ModelRef:
    """Return a new ModelRef with fields replaced by values in overrides.

    Transport switching: when the override supplies ``provider`` without ``cli``,
    the derived ``cli`` is cleared so the resulting ref unambiguously dispatches
    via the API adapter; the inverse applies when ``cli`` is supplied without
    ``provider``. TransportSpec is not copied from ``ref`` — the newly constructed
    ModelRef's ``__post_init__`` re-infers it from the effective cli/provider
    pair so transport always matches dispatch identity.
    """
    if "provider" in overrides and "cli" not in overrides:
        # Switching to API transport — clear the derived cli value
        new_cli: str | None = None
        new_provider: str | None = overrides["provider"]
    elif "cli" in overrides and "provider" not in overrides:
        # Switching to CLI transport — clear the derived provider value
        new_cli = overrides["cli"]
        new_provider = None
    else:
        # Both or neither supplied — preserve caller-provided values. When both
        # are set, transport inference prefers cli (see models.infer_transport).
        new_cli = overrides.get("cli", ref.cli)
        new_provider = overrides.get("provider", ref.provider)

    return ModelRef(
        model=overrides.get("model", ref.model),
        cli=new_cli,
        provider=new_provider,
        budget_usd=overrides.get("budget_usd", ref.budget_usd),
        timeout_seconds=overrides.get("timeout_seconds", ref.timeout_seconds),
        fallback_models=overrides.get("fallback_models", ref.fallback_models),
        timeout_medium_seconds=overrides.get("timeout_medium_seconds", ref.timeout_medium_seconds),
        timeout_large_seconds=overrides.get("timeout_large_seconds", ref.timeout_large_seconds),
        reasoning_effort=overrides.get("reasoning_effort", ref.reasoning_effort),
        thinking_budget=overrides.get("thinking_budget", ref.thinking_budget),
        base_url=overrides.get("base_url", ref.base_url),
        max_iterations=overrides.get("max_iterations", ref.max_iterations),
        max_tool_output_bytes=overrides.get("max_tool_output_bytes", ref.max_tool_output_bytes),
        api_fallback=overrides.get("api_fallback", ref.api_fallback),
    )


def derive_roles(
    models: list[str],
    overrides: dict[str, Any] | None = None,
    *,
    budget_usd: float = 10.0,
    complexity: str | None = None,
    complexity_score: int | None = None,
) -> RoleAssignment:
    """Map a simple model list to a RoleAssignment.

    Assignment algorithm (mirrors _auto_assign_models in profiles.py):
    1. Sort by cost_rank asc, capability desc
    2. dev = cheapest dev-capable model (fallback: first in sorted list)
    3. preflight = cheapest "fast" tier, else same as dev
    4. review_pool = all models except dev (if only 1, pool = [dev])
    5. synthesis = highest-capability model from review_pool (skip if pool <= 1)

    When ``complexity`` is provided ("small"/"medium"/"large" or "LOW"/"MEDIUM"/"HIGH"),
    tier × complexity routing applies instead of the static cheapest-first rules:
    - dev: LOW→cheap, MEDIUM→mid, HIGH→strong
    - plan: LOW→mid, MEDIUM→strong, HIGH→strong
    - review: LOW→single mid/strong reviewer (no synthesis), MEDIUM→default pool,
      HIGH→all mid/strong reviewers + synthesis

    Budget distribution:
    - dev: 60% of total budget_usd
    - preflight: max(2%, $1)
    - synthesis: max(2%, $1) when pool > 1
    - each reviewer: remaining / pool_size

    Args:
        models: Non-empty list of model keys (e.g. "claude/sonnet", "openai/gpt-5.4").
            Keys are looked up in MODEL_REGISTRY; unknown keys get sensible defaults.
        overrides: Optional dict keyed by role name ("dev", "preflight", "plan",
            "synthesis"). Each value is a dict of field names → replacement values
            for the ModelRef of that role. Phase-specific fields (allowed_tools,
            sandbox_mode, etc.) can also be included. For "review_pool", the value
            is a list of per-reviewer override dicts (indexed by pool position).
        budget_usd: Total budget to distribute across all roles. Defaults to $10.
        complexity: Optional complexity level; if supplied activates tier × complexity
            routing. Accepts "small"/"medium"/"large" or "LOW"/"MEDIUM"/"HIGH".
        complexity_score: Optional 1-10 preflight complexity score. When present,
            converted routing sites may use it to differentiate stories that share
            the same legacy complexity band.

    Returns:
        A RoleAssignment holding the four derived role configs plus optional synthesis.
        This is the stable insertion point for future adaptive routing: a router
        replaces this function and returns a RoleAssignment without restructuring
        the type hierarchy.
    """
    if not models:
        raise ValueError("models list must be non-empty")

    effective_overrides: dict[str, Any] = overrides or {}

    # Normalise complexity to canonical level string, or None for static assignment.
    norm_complexity: str | None = (
        _COMPLEXITY_NORM.get(complexity.lower()) if complexity is not None else None
    )

    # Build (model_key, ModelInfo) pairs and sort: cheapest first, then by capability desc
    infos = [(m, _resolve_model_info(m)) for m in models]
    sorted_models = sorted(infos, key=lambda x: (x[1].cost_rank, -x[1].capability))

    # Phase-eligibility-aware candidate pools — a model's AgentSpec may exclude it
    # from specific phases (e.g. a pro model excluded from preflight to avoid overspend).
    dev_pool = _phase_candidates(sorted_models, "dev")
    preflight_pool = _phase_candidates(sorted_models, "preflight")
    plan_pool = _phase_candidates(sorted_models, "plan")
    review_pool_sorted = _phase_candidates(sorted_models, "review")

    # dev: cheapest dev-capable model; fall back to first if none are dev-capable
    dev_candidates = [(k, i) for k, i in dev_pool if i.dev_capable]
    if not dev_candidates:
        dev_candidates = dev_pool

    # Tier × complexity routing applies at all defined complexity levels.
    # When norm_complexity is None (not supplied), static cheapest-first rules apply.
    _apply_tier_routing = norm_complexity is not None

    if _apply_tier_routing:
        target_dev_tier = (
            _score_to_dev_tier(complexity_score)
            if complexity_score is not None
            else _COMPLEXITY_TIER["dev"][norm_complexity]
        )
        dev_key, dev_info = _pick_by_tier(dev_candidates, target_dev_tier)
    else:
        dev_key, dev_info = dev_candidates[0]

    # preflight: cheapest "fast" tier, else same as dev (static — not complexity-adjusted)
    fast_models = [(k, i) for k, i in preflight_pool if i.tier == "fast"]
    preflight_key, preflight_info = fast_models[0] if fast_models else (dev_key, dev_info)

    # plan: tier-based selection when complexity is supplied; else same as dev
    if _apply_tier_routing:
        target_plan_tier = _COMPLEXITY_TIER["plan"][norm_complexity]
        plan_candidates = [(k, i) for k, i in plan_pool if i.dev_capable] or plan_pool
        _plan_key, plan_info = _pick_by_tier(plan_candidates, target_plan_tier)
    else:
        plan_info = dev_info

    # review_pool: all models except dev; if only one model total, pool = [dev]
    review_pairs = [(k, i) for k, i in review_pool_sorted if k != dev_key]
    if not review_pairs:
        review_pairs = [(dev_key, dev_info)]

    # Complexity-aware review pool sizing
    #   LOW → single mid/strong reviewer, no synthesis
    #   MEDIUM/HIGH → all mid/strong reviewers + synthesis
    #   None → static (all except dev)
    if norm_complexity == "LOW":
        mid_strong = [(k, i) for k, i in review_pairs if i.cost_rank >= 2]
        review_pairs = (
            [min(mid_strong, key=lambda x: (x[1].cost_rank, -x[1].capability))]
            if mid_strong
            else [review_pairs[0]]
        )
        has_synthesis = False
    elif norm_complexity in ("MEDIUM", "HIGH"):
        mid_strong = [(k, i) for k, i in review_pairs if i.cost_rank >= 2]
        if mid_strong:
            review_pairs = mid_strong
        # Always create synthesis for MEDIUM/HIGH (even with single reviewer)
        has_synthesis = True
    else:
        has_synthesis = len(review_pairs) > 1

    # Budget distribution
    preflight_budget = max(budget_usd * 0.02, 1.0)
    dev_budget = budget_usd * 0.60
    synthesis_budget = max(budget_usd * 0.02, 1.0) if has_synthesis else 0.0
    remaining = max(budget_usd - dev_budget - preflight_budget - synthesis_budget, 0.0)
    reviewer_budget = remaining / len(review_pairs)

    # --- Build dev role ---
    dev_ref = _make_model_ref(
        info=dev_info,
        budget_usd=dev_budget,
        timeout_seconds=DEFAULT_DEV_PROFILE.timeout_seconds,
    )
    dev_role_overrides = effective_overrides.get("dev", {})
    if dev_role_overrides:
        dev_ref = _apply_ref_overrides(dev_ref, dev_role_overrides)
    dev_role = DevRoleConfig(
        ref=dev_ref,
        allowed_tools=dev_role_overrides.get("allowed_tools", DEFAULT_DEV_PROFILE.allowed_tools),
        sandbox_mode=dev_role_overrides.get("sandbox_mode", "workspace-write"),
    )

    # --- Build preflight role ---
    preflight_ref = _make_model_ref(
        info=preflight_info,
        budget_usd=preflight_budget,
        timeout_seconds=DEFAULT_PREFLIGHT_PROFILE.timeout_seconds,
    )
    preflight_role_overrides = effective_overrides.get("preflight", {})
    if preflight_role_overrides:
        preflight_ref = _apply_ref_overrides(preflight_ref, preflight_role_overrides)
    preflight_role = PreflightRoleConfig(
        ref=preflight_ref,
        allowed_tools=preflight_role_overrides.get(
            "allowed_tools", DEFAULT_PREFLIGHT_PROFILE.allowed_tools
        ),
    )

    # --- Build plan role (defaults to same model as dev; complexity-aware: tier-based) ---
    plan_ref = _make_model_ref(
        info=plan_info,
        budget_usd=_DEFAULT_PLAN_BUDGET_USD,
        timeout_seconds=_DEFAULT_PLAN_TIMEOUT_SECONDS,
    )
    plan_role_overrides = effective_overrides.get("plan", {})
    if plan_role_overrides:
        plan_ref = _apply_ref_overrides(plan_ref, plan_role_overrides)
    plan_role = PlanRoleConfig(
        ref=plan_ref,
        allowed_tools=plan_role_overrides.get(
            "allowed_tools", DEFAULT_PREFLIGHT_PROFILE.allowed_tools
        ),
        validate_spec=plan_role_overrides.get("validate_spec", True),
    )

    # --- Build review pool ---
    pool_overrides_list: list[dict[str, Any]] = effective_overrides.get("review_pool", [])
    review_pool: list[ReviewRoleConfig] = []
    for idx, (k, info) in enumerate(review_pairs):
        review_ref = _make_model_ref(
            info=info,
            budget_usd=reviewer_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
        )
        if isinstance(pool_overrides_list, list) and idx < len(pool_overrides_list):
            review_ref = _apply_ref_overrides(review_ref, pool_overrides_list[idx])
        review_pool.append(
            ReviewRoleConfig(
                ref=review_ref,
                allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
                # Preserve registry key format ("claude-opus") so name-based profile
                # overrides in the loader continue to match auto-assigned pool entries.
                name=k.replace("/", "-"),
            )
        )

    # --- Build synthesis role ---
    synthesis_role: ReviewRoleConfig | None = None
    if has_synthesis:
        synth_key, synth_info = max(review_pairs, key=lambda x: x[1].capability)
        synth_ref = _make_model_ref(
            info=synth_info,
            budget_usd=synthesis_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
        )
        synth_overrides = effective_overrides.get("synthesis", {})
        if synth_overrides:
            synth_ref = _apply_ref_overrides(synth_ref, synth_overrides)
        synthesis_role = ReviewRoleConfig(
            ref=synth_ref,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
        )

    # --- Build plan_agent_review role (None unless overrides enable it) ---
    plan_agent_review_role: ReviewRoleConfig | None = None
    par_overrides = effective_overrides.get("plan_agent_review", {})
    if par_overrides:
        # Default base: dev model at plan-phase budget; overrides can change any field
        par_ref = _make_model_ref(
            info=dev_info,
            budget_usd=_DEFAULT_PLAN_BUDGET_USD,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
        )
        par_ref = _apply_ref_overrides(par_ref, par_overrides)
        plan_agent_review_role = ReviewRoleConfig(
            ref=par_ref,
            allowed_tools=par_overrides.get("allowed_tools", DEFAULT_REVIEW_PROFILE.allowed_tools),
        )

    return RoleAssignment(
        dev=dev_role,
        preflight=preflight_role,
        plan=plan_role,
        review_pool=tuple(review_pool),
        synthesis=synthesis_role,
        plan_agent_review=plan_agent_review_role,
    )
