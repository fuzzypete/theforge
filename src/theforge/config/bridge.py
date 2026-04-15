"""Boundary conversion: RoleAssignment → ModelProfile-based structures.

role_assignment_to_profiles() is the sole crossing point between the new
config-domain types (RoleAssignment / ModelRef) and the runtime types
(ModelProfile) consumed by the coordinator and ForgeConfig.

The coordinator and loader are not modified by this story; this bridge is
additive. Downstream loader stories (#265b, #266) will wire in this bridge
to replace the direct ForgeConfig construction in load.py.
"""

from __future__ import annotations

from .schema import (
    DevRoleConfig,
    PlanRoleConfig,
    PreflightRoleConfig,
    ReviewRoleConfig,
    RoleAssignment,
)
from .types import ModelProfile


def _role_config_to_profile(
    name: str,
    role_config: DevRoleConfig | PreflightRoleConfig | PlanRoleConfig | ReviewRoleConfig,
    phase: str | None = None,
) -> ModelProfile:
    """Convert a single role config to a ModelProfile.

    All shared fields are copied from the embedded ModelRef. Phase-specific
    fields (allowed_tools, sandbox_mode, review_role) are lifted from the
    wrapper. This is the only place the naming gap between ModelRef fields
    and ModelProfile fields is bridged (e.g. timeout_seconds → timeout_seconds
    is consistent; the gap in PlanConfig.timeout vs ModelProfile.timeout_seconds
    does not appear here because ModelRef always uses timeout_seconds).
    """
    ref = role_config.ref
    allowed_tools: tuple[str, ...] = getattr(role_config, "allowed_tools", ())
    review_role: str | None = getattr(role_config, "review_role", None)
    sandbox_mode: str = getattr(role_config, "sandbox_mode", "workspace-write")

    return ModelProfile(
        name=name,
        cli=ref.cli,
        provider=ref.provider,
        model=ref.model,
        fallback_models=ref.fallback_models,
        budget_usd=ref.budget_usd,
        timeout_seconds=ref.timeout_seconds,
        timeout_medium_seconds=ref.timeout_medium_seconds,
        timeout_large_seconds=ref.timeout_large_seconds,
        allowed_tools=allowed_tools,
        reasoning_effort=ref.reasoning_effort,
        thinking_budget=ref.thinking_budget,
        base_url=ref.base_url,
        max_iterations=ref.max_iterations,
        max_tool_output_bytes=ref.max_tool_output_bytes,
        api_fallback=ref.api_fallback,
        review_role=review_role,
        phase=phase,
        sandbox_mode=sandbox_mode,
    )


def role_assignment_to_profiles(ra: RoleAssignment) -> dict[str, object]:
    """Convert a RoleAssignment to ModelProfile-keyed structures for ForgeConfig.

    This is the sole boundary crossing point between config-domain types and
    the runtime types consumed by the coordinator. Coordinator code is not
    modified; callers construct ForgeConfig from the returned dict.

    Returns:
        A dict with keys:
          "dev_profile":              ModelProfile
          "preflight_profile":        ModelProfile
          "plan_profile":             ModelProfile
          "plan_validate_spec":       bool  (preserves PlanRoleConfig.validate_spec)
          "review_pool":              list[ModelProfile]
          "synthesis_profile":        ModelProfile | None
          "plan_agent_review_profile": ModelProfile | None
    """
    dev_profile = _role_config_to_profile("dev", ra.dev, phase="dev")
    preflight_profile = _role_config_to_profile("preflight", ra.preflight, phase="preflight")
    plan_profile = _role_config_to_profile("plan", ra.plan, phase="plan")

    review_pool: list[ModelProfile] = [
        _role_config_to_profile(
            rc.ref.model.replace("/", "-"),
            rc,
            phase="review",
        )
        for rc in ra.review_pool
    ]

    synthesis_profile: ModelProfile | None = None
    if ra.synthesis is not None:
        synthesis_profile = _role_config_to_profile("synthesis", ra.synthesis, phase="review")

    plan_agent_review_profile: ModelProfile | None = None
    if ra.plan_agent_review is not None:
        plan_agent_review_profile = _role_config_to_profile(
            "plan-agent-review", ra.plan_agent_review, phase="plan_review"
        )

    return {
        "dev_profile": dev_profile,
        "preflight_profile": preflight_profile,
        "plan_profile": plan_profile,
        "plan_validate_spec": ra.plan.validate_spec,
        "review_pool": review_pool,
        "synthesis_profile": synthesis_profile,
        "plan_agent_review_profile": plan_agent_review_profile,
    }
