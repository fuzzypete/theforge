"""Model registry: AgentSpec / TransportSpec / ModelInfo and model-related helpers.

The registry is the single source of truth for what agents the system knows about.
Each registry entry is expressed as an `AgentSpec` (provider + model + capability
metadata) paired with an explicit `TransportSpec` (how to invoke it — cli or api).
`ModelInfo` is retained as a legacy flat view derived from these specs so existing
callers continue to work while the codebase migrates.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, TypeVar, overload

from .types import ApiFallbackConfig, AssignmentConfig, ModelProfile, PlanConfig

_ProfileT = TypeVar("_ProfileT", ModelProfile, PlanConfig)


@dataclass(frozen=True)
class TransportSpec:
    """How to execute an agent.

    kind: "cli" — invoke via a locally installed binary (Claude/Codex/Gemini CLI)
          "api" — invoke via a provider SDK (Anthropic/OpenAI/Google/DeepSeek)
    runner: the logical runner module key (e.g. "claude", "codex", "gemini",
            "anthropic", "openai", "google", "deepseek"). For CLI transports this
            identifies both the runner and the binary; for API transports this
            identifies the adapter to dispatch to.
    executable: only meaningful for kind="cli" — the binary name on PATH.
    """

    kind: str  # "cli" | "api"
    runner: str
    executable: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("cli", "api"):
            raise ValueError(f"TransportSpec.kind must be 'cli' or 'api', got {self.kind!r}")
        if self.kind == "cli" and not self.executable:
            raise ValueError("TransportSpec(kind='cli') requires an executable name")
        if self.kind == "api" and self.executable is not None:
            raise ValueError("TransportSpec(kind='api') must not set an executable")


@dataclass(frozen=True)
class AgentSpec:
    """First-class description of an agent: provider + model + capability + transport.

    AgentSpec separates *what* the agent is (provider, model, tier, capability,
    cost, phase eligibility) from *how* to invoke it (TransportSpec). Role
    derivation reads AgentSpec fields; runner dispatch reads TransportSpec.kind.
    """

    provider: str  # "anthropic" | "openai" | "google" | "deepseek"
    model: str  # model identifier (e.g. "sonnet", "gpt-5.4", "deepseek-reasoner")
    transport: TransportSpec
    tier: str  # "fast" | "strong" (semantic speed/latency band)
    capability: int  # 1-10 relative capability score
    cost_rank: int  # 1=cheap, 2=moderate, 3=expensive
    dev_capable: bool = True  # whether this agent is allowed to own the dev role
    phase_eligibility: frozenset[str] = frozenset({"preflight", "dev", "plan", "review"})
    tool_mode: str = "auto"  # "auto" = follow transport default; reserved for future use
    registry_source: str = "builtin"  # "builtin" | "forge.yaml"
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None


# Canonical transport objects — referenced by AGENT_REGISTRY entries.
_TRANSPORT_CLAUDE_CLI = TransportSpec(kind="cli", runner="claude", executable="claude")
_TRANSPORT_CODEX_CLI = TransportSpec(kind="cli", runner="codex", executable="codex")
_TRANSPORT_GEMINI_CLI = TransportSpec(kind="cli", runner="gemini", executable="gemini")
# gh-aw (GitHub Agentic Workflows): dispatched through the `gh` binary; the
# agent itself executes remotely on GitHub Actions (ADR-0004 spike backend).
_TRANSPORT_GHAW_CLI = TransportSpec(kind="cli", runner="ghaw", executable="gh")
_TRANSPORT_ANTHROPIC_API = TransportSpec(kind="api", runner="anthropic")
_TRANSPORT_OPENAI_API = TransportSpec(kind="api", runner="openai")
_TRANSPORT_GOOGLE_API = TransportSpec(kind="api", runner="google")
_TRANSPORT_DEEPSEEK_API = TransportSpec(kind="api", runner="deepseek")


_CLI_TRANSPORT_MAP: dict[str, TransportSpec] = {
    "claude": _TRANSPORT_CLAUDE_CLI,
    "codex": _TRANSPORT_CODEX_CLI,
    "gemini": _TRANSPORT_GEMINI_CLI,
    "ghaw": _TRANSPORT_GHAW_CLI,
}
_PROVIDER_API_TRANSPORT_MAP: dict[str, TransportSpec] = {
    "anthropic": _TRANSPORT_ANTHROPIC_API,
    "openai": _TRANSPORT_OPENAI_API,
    "google": _TRANSPORT_GOOGLE_API,
    "deepseek": _TRANSPORT_DEEPSEEK_API,
}

_MODEL_OVERLAY_PROVIDER_MAP: dict[str, tuple[str, TransportSpec]] = {
    "anthropic": ("anthropic", _TRANSPORT_CLAUDE_CLI),
    "claude": ("anthropic", _TRANSPORT_CLAUDE_CLI),
    "openai": ("openai", _TRANSPORT_CODEX_CLI),
    "openai-api": ("openai", _TRANSPORT_OPENAI_API),
    "deepseek": ("deepseek", _TRANSPORT_DEEPSEEK_API),
    "google": ("google", _TRANSPORT_GOOGLE_API),
    "gemini": ("google", _TRANSPORT_GOOGLE_API),
    "gemini-cli": ("google", _TRANSPORT_GEMINI_CLI),
}


def infer_transport(
    cli: str | None,
    provider: str | None,
) -> TransportSpec | None:
    """Infer the canonical TransportSpec from legacy cli/provider fields.

    Dispatch should ultimately read TransportSpec.kind directly. This helper
    bridges the gap: when a caller constructs a profile without supplying an
    explicit TransportSpec, this function produces one so dispatch has a
    single source of truth.

    ``cli`` wins when both are supplied — a profile that names a CLI binary
    is dispatched via that binary. The caller can still set ``transport``
    explicitly to override this inference.
    """
    if cli and cli in _CLI_TRANSPORT_MAP:
        return _CLI_TRANSPORT_MAP[cli]
    if provider and provider in _PROVIDER_API_TRANSPORT_MAP:
        return _PROVIDER_API_TRANSPORT_MAP[provider]
    return None


_DEFAULT_PHASE_ELIGIBILITY: frozenset[str] = frozenset({"preflight", "dev", "plan", "review"})


@dataclass(frozen=True)
class ModelInfo:
    """Legacy flat view derived from an AgentSpec.

    Retained for backward compatibility: existing callers read `.cli`, `.provider`,
    `.model`, `.tier`, `.capability`, `.cost_rank`, `.dev_capable`. New code should
    prefer `AgentSpec` + `TransportSpec` directly.

    `phase_eligibility` and `transport` are carried through the projection so role
    derivation can filter candidates per-role and so runtime dispatch can read the
    explicit TransportSpec rather than inferring transport from cli/provider.
    """

    cli: str | None  # "claude", "codex", "gemini"; None for API-backed providers
    model: str  # model identifier for the CLI
    tier: str  # "fast" or "strong"
    capability: int  # relative capability score (1-10)
    cost_rank: int  # 1=cheap, 2=moderate, 3=expensive
    dev_capable: bool = True  # False for models whose CLI doesn't support dev tools
    provider: str | None = None  # API transport, mutually exclusive with cli
    phase_eligibility: frozenset[str] = _DEFAULT_PHASE_ELIGIBILITY
    registry_id: str | None = None  # canonical model registry key
    registry_source: str = "builtin"  # "builtin" | "forge.yaml"
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    transport: TransportSpec | None = None  # the canonical TransportSpec this view was built from


_CLI_TO_PROVIDER: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
}


@dataclass(frozen=True)
class AgentDef:
    """An agent available in the pool for adaptive model assignment."""

    name: str
    provider: str | None  # "anthropic", "openai", etc. — None for CLI agents
    model: str
    budget_usd: float
    timeout_seconds: int
    tier: str  # "cheap" | "mid" | "strong"
    cli: str | None = None  # "claude", "codex", etc. — set for CLI agents
    api_fallback: ApiFallbackConfig | None = None
    strengths: tuple[str, ...] = ()
    registry_id: str | None = None
    registry_source: str = "builtin"

    @property
    def effective_provider(self) -> str | None:
        """Return the agent's provider for routing/display purposes.

        For API agents this is the explicit ``provider`` field. For CLI-only
        agents (where ``provider`` is None), this derives the provider from the
        ``cli`` binary: ``claude`` → ``anthropic``, ``codex`` → ``openai``,
        ``gemini`` → ``google``. The raw ``provider`` field stays None for CLI
        agents so dispatch (which reads ``ModelProfile.provider`` to choose
        between CLI and API runners) is unaffected.
        """
        if self.provider is not None:
            return self.provider
        if self.cli is not None:
            return _CLI_TO_PROVIDER.get(self.cli)
        return None

    def to_model_profile(self, *, allowed_tools: tuple[str, ...] = ()) -> ModelProfile:
        """Convert to a ModelProfile for use in coordinator config."""
        return ModelProfile(
            name=self.name,
            cli=self.cli,
            provider=self.provider,
            model=self.model,
            budget_usd=self.budget_usd,
            timeout_seconds=self.timeout_seconds,
            allowed_tools=allowed_tools,
            api_fallback=self.api_fallback,
            registry_id=self.registry_id,
            registry_source=self.registry_source,
        )


# ── Agent registry ────────────────────────────────────────────────────
#
# Each entry resolves a `<provider>/<model>` key to an explicit AgentSpec with
# an explicit TransportSpec. Adding a new API-backed model is a single entry
# here — no role-derivation, profile, or runner-dispatch edits required.
AGENT_REGISTRY: dict[str, AgentSpec] = {
    # ── Anthropic (CLI) ───────────────────────────────────────────────
    "claude/sonnet": AgentSpec(
        provider="anthropic",
        model="sonnet",
        transport=_TRANSPORT_CLAUDE_CLI,
        tier="fast",
        capability=7,
        cost_rank=1,
    ),
    "claude/opus": AgentSpec(
        provider="anthropic",
        model="opus",
        transport=_TRANSPORT_CLAUDE_CLI,
        tier="strong",
        capability=10,
        cost_rank=3,
    ),
    # ── OpenAI (CLI via Codex) ────────────────────────────────────────
    "openai/gpt-5.4": AgentSpec(
        provider="openai",
        model="gpt-5.4",
        transport=_TRANSPORT_CODEX_CLI,
        tier="strong",
        capability=9,
        cost_rank=2,
    ),
    "openai/gpt-5.4-mini": AgentSpec(
        provider="openai",
        model="gpt-5.4-mini",
        transport=_TRANSPORT_CODEX_CLI,
        tier="cheap",
        capability=7,
        cost_rank=1,
    ),
    "openai/gpt-5.4-pro": AgentSpec(
        provider="openai",
        model="gpt-5.4-pro",
        transport=_TRANSPORT_CODEX_CLI,
        tier="strong",
        capability=10,
        cost_rank=3,
    ),
    # ── DeepSeek (API) ────────────────────────────────────────────────
    "deepseek/deepseek-reasoner": AgentSpec(
        provider="deepseek",
        model="deepseek-reasoner",
        transport=_TRANSPORT_DEEPSEEK_API,
        tier="strong",
        capability=9,
        cost_rank=2,
    ),
    "deepseek/deepseek-chat": AgentSpec(
        provider="deepseek",
        model="deepseek-chat",
        transport=_TRANSPORT_DEEPSEEK_API,
        tier="fast",
        capability=7,
        cost_rank=1,
    ),
    # ── Google (API) ─────────────────────────────────────────────────
    "google/gemini-3-flash-preview": AgentSpec(
        provider="google",
        model="gemini-3-flash-preview",
        transport=_TRANSPORT_GOOGLE_API,
        tier="cheap",
        capability=7,
        cost_rank=1,
    ),
    "google/gemini-3.1-pro-preview": AgentSpec(
        provider="google",
        model="gemini-3.1-pro-preview",
        transport=_TRANSPORT_GOOGLE_API,
        tier="strong",
        capability=9,
        cost_rank=2,
    ),
    "google/gemini-2.5-pro": AgentSpec(
        provider="google",
        model="gemini-2.5-pro",
        transport=_TRANSPORT_GOOGLE_API,
        tier="strong",
        capability=8,
        cost_rank=2,
    ),
    # ── Local OpenAI-compatible models (route via Codex CLI with base_url) ──
    "openai/codestral": AgentSpec(
        provider="openai",
        model="codestral",
        transport=_TRANSPORT_CODEX_CLI,
        tier="fast",
        capability=7,
        cost_rank=1,
    ),
    "openai/deepseek-coder": AgentSpec(
        provider="openai",
        model="deepseek-coder",
        transport=_TRANSPORT_CODEX_CLI,
        tier="fast",
        capability=7,
        cost_rank=1,
    ),
    "openai/llama3.1": AgentSpec(
        provider="openai",
        model="llama3.1",
        transport=_TRANSPORT_CODEX_CLI,
        tier="fast",
        capability=6,
        cost_rank=1,
    ),
    "openai/qwen2.5-coder": AgentSpec(
        provider="openai",
        model="qwen2.5-coder",
        transport=_TRANSPORT_CODEX_CLI,
        tier="fast",
        capability=7,
        cost_rank=1,
    ),
    # ── OpenAI (API — disambiguated with 'openai-api/' prefix so operators can
    # select the OpenAI API path separately from the Codex CLI path) ──────
    "openai-api/gpt-5.4": AgentSpec(
        provider="openai",
        model="gpt-5.4",
        transport=_TRANSPORT_OPENAI_API,
        tier="strong",
        capability=9,
        cost_rank=2,
    ),
    "openai-api/gpt-5.4-mini": AgentSpec(
        provider="openai",
        model="gpt-5.4-mini",
        transport=_TRANSPORT_OPENAI_API,
        tier="cheap",
        capability=7,
        cost_rank=1,
    ),
    "openai-api/gpt-5.4-pro": AgentSpec(
        provider="openai",
        model="gpt-5.4-pro",
        transport=_TRANSPORT_OPENAI_API,
        tier="strong",
        capability=10,
        cost_rank=3,
        # Reasoning-heavy — intentionally excluded from the preflight role.
        phase_eligibility=frozenset({"dev", "plan", "review"}),
    ),
    # ── Gemini CLI (explicit opt-in) ─────────────────────────────────
    "gemini-cli/gemini-2.5-pro": AgentSpec(
        provider="google",
        model="gemini-2.5-pro",
        transport=_TRANSPORT_GEMINI_CLI,
        tier="strong",
        capability=8,
        cost_rank=2,
        dev_capable=False,
    ),
    "gemini-cli/gemini-3-flash-preview": AgentSpec(
        provider="google",
        model="gemini-3-flash-preview",
        transport=_TRANSPORT_GEMINI_CLI,
        tier="cheap",
        capability=7,
        cost_rank=1,
        dev_capable=False,
    ),
    "gemini-cli/gemini-3.1-pro-preview": AgentSpec(
        provider="google",
        model="gemini-3.1-pro-preview",
        transport=_TRANSPORT_GEMINI_CLI,
        tier="strong",
        capability=9,
        cost_rank=2,
        dev_capable=False,
    ),
}


# ── Canonical model identity ─────────────────────────────────────────
#
# The canonical model ID is the single key under which all profile data,
# assignment history and audit records accumulate. Two registrations resolve
# to the same canonical ID iff they refer to the same actual provider, model
# and transport.kind. Format: ``<provider>/<model>/<transport.kind>``
# (e.g. ``anthropic/sonnet/cli``, ``openai/gpt-5.4/api``).


def canonical_model_id(provider: str, model: str, transport_kind: str) -> str:
    """Build the canonical ID from its three constituent parts."""
    return f"{provider}/{model}/{transport_kind}"


def canonical_id_for_spec(spec: AgentSpec) -> str:
    """Return the canonical ID for an :class:`AgentSpec`."""
    return canonical_model_id(spec.provider, spec.model, spec.transport.kind)


def is_canonical_model_id(value: str | None) -> bool:
    """Return True if ``value`` already follows the canonical format."""
    if not value or not isinstance(value, str):
        return False
    parts = value.split("/")
    return len(parts) == 3 and bool(parts[0]) and bool(parts[1]) and parts[2] in ("cli", "api")


def known_model_overlay_providers() -> tuple[str, ...]:
    """Return accepted provider/adaptor tokens for forge.yaml model overlays."""
    return tuple(sorted(_MODEL_OVERLAY_PROVIDER_MAP))


def overlay_transport(provider: str) -> tuple[str, TransportSpec]:
    """Resolve a forge.yaml provider token to its provider family and transport."""
    if provider not in _MODEL_OVERLAY_PROVIDER_MAP:
        known = ", ".join(sorted(_MODEL_OVERLAY_PROVIDER_MAP))
        raise ValueError(
            f"Unknown provider {provider!r} in models.custom. Known providers/adapters: {known}"
        )
    return _MODEL_OVERLAY_PROVIDER_MAP[provider]


def custom_model_capability(tier: str) -> int:
    """Return the default capability score for a custom model tier."""
    by_tier = {"cheap": 6, "fast": 7, "strong": 9}
    if tier not in by_tier:
        raise ValueError(f"models.custom tier must be one of {sorted(by_tier)}, got {tier!r}")
    return by_tier[tier]


def custom_model_cost_rank(input_cost_per_mtok: float, output_cost_per_mtok: float) -> int:
    """Map per-MTok pricing to the routing cost bands used by the registry."""
    price_signal = max(float(input_cost_per_mtok), float(output_cost_per_mtok))
    if price_signal <= 5.0:
        return 1
    if price_signal <= 25.0:
        return 2
    return 3


def custom_model_dev_capable(transport: TransportSpec) -> bool:
    """Return whether a custom model transport can own the dev role."""
    return not (transport.kind == "cli" and transport.runner == "gemini")


def _spec_to_model_info(
    model_key: str | AgentSpec,
    spec: AgentSpec | None = None,
) -> ModelInfo:
    """Project an AgentSpec down to the legacy ModelInfo view.

    Carries `phase_eligibility` and the underlying `TransportSpec` through the
    projection so role derivation can filter by phase and runtime dispatch can
    read the explicit transport rather than re-inferring it from cli/provider.
    """
    if spec is None:
        spec = model_key
        assert isinstance(spec, AgentSpec)
        model_key = spec.model

    assert isinstance(model_key, str)
    if spec.transport.kind == "cli":
        return ModelInfo(
            cli=spec.transport.runner,
            model=spec.model,
            tier=spec.tier,
            capability=spec.capability,
            cost_rank=spec.cost_rank,
            dev_capable=spec.dev_capable,
            provider=None,
            phase_eligibility=spec.phase_eligibility,
            registry_id=model_key,
            registry_source=spec.registry_source,
            input_cost_per_mtok=spec.input_cost_per_mtok,
            output_cost_per_mtok=spec.output_cost_per_mtok,
            transport=spec.transport,
        )
    return ModelInfo(
        cli=None,
        model=spec.model,
        tier=spec.tier,
        capability=spec.capability,
        cost_rank=spec.cost_rank,
        dev_capable=spec.dev_capable,
        provider=spec.provider,
        phase_eligibility=spec.phase_eligibility,
        registry_id=model_key,
        registry_source=spec.registry_source,
        input_cost_per_mtok=spec.input_cost_per_mtok,
        output_cost_per_mtok=spec.output_cost_per_mtok,
        transport=spec.transport,
    )


# Derived legacy view — exactly mirrors AGENT_REGISTRY.
MODEL_REGISTRY: dict[str, ModelInfo] = {
    k: _spec_to_model_info(k, v) for k, v in AGENT_REGISTRY.items()
}


def model_info_view(
    registry: dict[str, AgentSpec] | None = None,
) -> dict[str, ModelInfo]:
    """Return a {model_key: ModelInfo} view of an AgentSpec registry.

    With ``registry=None`` returns the built-in MODEL_REGISTRY. Pass
    ``ForgeConfig.model_registry`` (the merged built-in + forge.yaml overlay)
    so consumers see user-declared custom models as first-class entries.
    """
    if registry is None:
        return MODEL_REGISTRY
    return {k: _spec_to_model_info(k, v) for k, v in registry.items()}


@overload
def apply_model_info(profile: ModelProfile, info: ModelInfo) -> ModelProfile: ...


@overload
def apply_model_info(profile: PlanConfig, info: ModelInfo) -> PlanConfig: ...


def apply_model_info(profile: _ProfileT, info: ModelInfo) -> _ProfileT:
    """Return profile with model identity and dispatch transport from ``info``.

    Mid-run model swaps must update all fields that define dispatch together.
    ``PlanConfig`` predates explicit ``TransportSpec`` storage, so this helper
    writes ``transport`` only for profiles that carry that field.
    """
    profile_fields = {field.name for field in fields(profile)}
    updates: dict[str, object] = {
        "cli": info.cli,
        "model": info.model,
        "provider": info.provider,
    }
    if "registry_id" in profile_fields:
        updates["registry_id"] = info.registry_id
    if "registry_source" in profile_fields:
        updates["registry_source"] = info.registry_source
    if "transport" in profile_fields:
        updates["transport"] = info.transport
    return replace(profile, **updates)


def resolve_agent_spec(
    model_key: str,
    registry: dict[str, AgentSpec] | None = None,
) -> AgentSpec:
    """Resolve a `provider/model` key to its AgentSpec.

    Raises ValueError for any key not present in AGENT_REGISTRY — there is no
    provider-prefix-to-CLI guessing. To support a new model, add an explicit
    registry entry.
    """
    effective_registry = registry or AGENT_REGISTRY
    if model_key not in effective_registry:
        known = sorted(effective_registry)
        raise ValueError(
            f"Unknown model {model_key!r}: not in AGENT_REGISTRY. "
            f"Add an explicit registry entry. Known models: {known}"
        )
    return effective_registry[model_key]


def _resolve_model_info(
    model_key: str,
    registry: dict[str, AgentSpec] | None = None,
) -> ModelInfo:
    """Resolve a model key to its legacy ModelInfo view.

    Delegates to resolve_agent_spec(): unknown keys raise ValueError rather than
    falling back to a prefix-derived CLI.
    """
    spec = resolve_agent_spec(model_key, registry=registry)
    return _spec_to_model_info(model_key, spec)


def _planner_candidate_models(agents: list[AgentDef]) -> set[str]:
    """Return model names the adaptive planner can select at runtime.

    Mirrors assign_models planner selection logic (PHASE_TIER["plan"]):
      LOW → mid tier (fallback: highest-budget agent if no mid agents)
      MEDIUM/HIGH → strong tier (fallback: highest-budget agent if no strong agents)

    Auth filtering is intentionally omitted: auth state can change at runtime,
    so the static check only considers structural availability.
    """
    if not agents:
        return set()

    # PHASE_TIER["plan"] = {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"}
    planner_tiers = {"LOW": "mid", "MEDIUM": "strong", "HIGH": "strong"}
    highest_budget = sorted(agents, key=lambda a: -a.budget_usd)[0]

    candidate_models: set[str] = set()
    for tier in planner_tiers.values():
        tier_agents = sorted([a for a in agents if a.tier == tier], key=lambda a: a.budget_usd)
        if tier_agents:
            candidate_models.add(tier_agents[0].model)
        else:
            # Mirror assign_models fallback: pick highest-budget agent
            candidate_models.add(highest_budget.model)

    return candidate_models


def _parse_assignment(assignment_raw: dict[str, Any]) -> AssignmentConfig:
    """Parse assignment config from raw YAML dict."""
    return AssignmentConfig(
        enabled=bool(assignment_raw.get("enabled", False)),
        min_reviewers=int(assignment_raw.get("min_reviewers", 1)),
        max_reviewers=int(assignment_raw.get("max_reviewers", 3)),
        prefer_cross_provider=bool(assignment_raw.get("prefer_cross_provider", True)),
        max_cost_per_story_usd=(
            float(assignment_raw["max_cost_per_story_usd"])
            if assignment_raw.get("max_cost_per_story_usd") is not None
            else None
        ),
        escalation_memory=bool(assignment_raw.get("escalation_memory", True)),
        adaptive_enabled=bool(assignment_raw.get("adaptive_enabled", True)),
    )
