"""Config-domain types for the v0.8 simplified configuration schema.

These types define the config-side representation — what you write in forge.yaml
when using the new simplified model list. They are separate from the runtime types
(ModelProfile, ForgeConfig) in types.py, which remain the coordinator's currency.

New phases requiring model-backed agents compose ModelRef instead of duplicating
transport fields (cli, provider, model, budget_usd, timeout_seconds, etc.).
Use bridge.py (role_assignment_to_profiles) to convert to ModelProfile instances.

ModelRef itself is defined in types.py — the coordinator runtime types there
(PlanConfig, PlanAgentReviewConfig) compose it too, and types.py is the
lower-dependency module — and re-exported here for the config-domain spelling.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import ModelRef

__all__ = [
    "DevRoleConfig",
    "ModelRef",
    "PlanRoleConfig",
    "PreflightRoleConfig",
    "ReviewRoleConfig",
    "RoleAssignment",
]


@dataclass(frozen=True)
class DevRoleConfig:
    """Configuration for the DEV phase agent.

    Composes ModelRef for transport/limits; adds dev-specific controls.
    No transport fields are duplicated here.
    """

    ref: ModelRef
    allowed_tools: tuple[str, ...] = ("Read", "Edit", "Write", "Bash", "Glob", "Grep", "WebFetch")
    sandbox_mode: str = "workspace-write"


@dataclass(frozen=True)
class PreflightRoleConfig:
    """Configuration for the PREFLIGHT phase agent.

    Composes ModelRef for transport/limits; adds preflight-specific controls.
    No transport fields are duplicated here.
    """

    ref: ModelRef
    allowed_tools: tuple[str, ...] = ("Read", "Bash", "Glob", "Grep")


@dataclass(frozen=True)
class PlanRoleConfig:
    """Configuration for the PLAN phase agent.

    Composes ModelRef for transport/limits; adds plan-specific controls.
    No transport fields are duplicated here.
    """

    ref: ModelRef
    allowed_tools: tuple[str, ...] = ("Read", "Bash", "Glob", "Grep")
    validate_spec: bool = True


@dataclass(frozen=True)
class ReviewRoleConfig:
    """Configuration for a single REVIEW phase agent.

    Composes ModelRef for transport/limits; adds review-specific controls.
    No transport fields are duplicated here.
    """

    ref: ModelRef
    review_role: str | None = None  # "correctness" | "patterns" | "edge-cases"
    allowed_tools: tuple[str, ...] = ("Read", "Bash", "Glob", "Grep")
    name: str | None = None  # optional profile identifier; used by bridge for ModelProfile.name


@dataclass(frozen=True)
class RoleAssignment:
    """Container for all role configs derived from a simple model list.

    Produced by derive_roles() in role_derivation.py. This dataclass is the
    clean insertion point for future adaptive routing (v0.9): a router replaces
    derive_roles() and returns a RoleAssignment without restructuring the type
    hierarchy. The bridge module (bridge.py) converts this to ModelProfile
    instances for the coordinator.

    plan_agent_review replaces PlanAgentReviewConfig's transport fields: it
    composes ReviewRoleConfig (which embeds ModelRef) instead of duplicating
    cli/provider/model/budget_usd/timeout fields. None = plan agent review
    disabled, matching PlanAgentReviewConfig's enabled=False default.
    """

    dev: DevRoleConfig
    preflight: PreflightRoleConfig
    plan: PlanRoleConfig
    review_pool: tuple[ReviewRoleConfig, ...]
    synthesis: ReviewRoleConfig | None = None
    plan_agent_review: ReviewRoleConfig | None = None  # None = disabled
