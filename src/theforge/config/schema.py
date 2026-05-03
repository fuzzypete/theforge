"""Config-domain types for the v0.8 simplified configuration schema.

These types define the config-side representation — what you write in forge.yaml
when using the new simplified model list. They are separate from the runtime types
(ModelProfile, ForgeConfig) in types.py, which remain the coordinator's currency.

New phases requiring model-backed agents compose ModelRef instead of duplicating
transport fields (cli, provider, model, budget_usd, timeout_seconds, etc.).
Use bridge.py (role_assignment_to_profiles) to convert to ModelProfile instances.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .types import ApiFallbackConfig

if TYPE_CHECKING:
    from .models import TransportSpec


@dataclass(frozen=True)
class ModelRef:
    """Transport/model atom — reusable core of every phase config.

    Dispatch reads the embedded TransportSpec. ``cli`` and ``provider`` remain
    as identity fields (provider keys auth and pricing; cli identifies the
    binary for CLI dispatch). The XOR constraint between them is no longer
    enforced — TransportSpec.kind is the single source of truth for how a
    ref is executed, and is auto-populated from cli/provider when omitted.

    Adding a new model-backed phase requires only a new wrapper dataclass that
    embeds a ModelRef — no transport fields are copy-pasted.
    """

    model: str
    budget_usd: float
    timeout_seconds: int
    # Transport — exactly one of cli/provider should be set
    cli: str | None = None
    provider: str | None = None
    # Optional: preference fallbacks
    fallback_models: tuple[str, ...] = ()
    # Optional: complexity-dependent timeout overrides
    timeout_medium_seconds: int | None = None
    timeout_large_seconds: int | None = None
    # Optional: provider-specific runtime controls
    reasoning_effort: str | None = None  # "low" | "medium" | "high"; Codex only
    thinking_budget: int | None = None  # Gemini ThinkingConfig token budget
    base_url: str | None = None  # override provider endpoint (Ollama, etc.)
    max_iterations: int | None = None  # override default agent loop iterations
    max_tool_output_bytes: int = 51200  # cap for tool output (50 KB default)
    api_fallback: ApiFallbackConfig | None = None  # CLI-only same-provider API fallback
    registry_id: str | None = None  # canonical model registry key, when sourced from a registry
    registry_source: str = "builtin"  # "builtin" | "forge.yaml"
    # Explicit TransportSpec — carried through derive_roles → bridge → ModelProfile
    # so runtime dispatch reads TransportSpec.kind rather than inferring from cli/provider.
    transport: TransportSpec | None = None

    def __post_init__(self) -> None:
        if self.transport is None and (self.cli or self.provider):
            from .models import infer_transport

            inferred = infer_transport(self.cli, self.provider)
            if inferred is not None:
                object.__setattr__(self, "transport", inferred)


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
