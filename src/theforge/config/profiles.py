"""Profile parsing and smart config assignment."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from .auth import check_agent_auth
from .defaults import (
    API_PROVIDER_DEFAULT_TOOLS,
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    PROVIDER_SDK_MAP,
    SUPPORTED_CLIS,
)
from .models import AgentDef, AgentSpec, _resolve_model_info, price_tiebreak_signal
from .types import SUPPORTED_PROVIDERS, ModelProfile, TransportFallbackConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import TransportSpec
    from .types import ForgeConfig

_COST_RANK_TO_TIER = {1: "cheap", 2: "mid", 3: "strong"}


def iter_config_profiles(config: "ForgeConfig") -> Iterator[tuple[str, ModelProfile]]:
    """Yield ``(role_label, profile)`` for every ModelProfile a run/sprint uses.

    Covers the dev profile, preflight (+ optional fallback), every reviewer in
    the pool, the optional synthesis profile, and every agent-pool entry (each
    projected to a ModelProfile). Plan/plan-review configs are validated
    separately at load time and are not ModelProfiles; use
    :func:`iter_plan_phase_profiles` for those.
    """
    yield ("dev", config.dev_profile)
    yield ("preflight", config.preflight_profile)
    if config.preflight_fallback_profile is not None:
        yield ("preflight-fallback", config.preflight_fallback_profile)
    for profile in config.review_pool:
        yield ("review", profile)
    if config.synthesis_profile is not None:
        yield ("synthesis", config.synthesis_profile)
    for agent in config.agents:
        yield ("agent-pool", agent.to_model_profile())


def iter_plan_phase_profiles(config: "ForgeConfig") -> Iterator[tuple[str, ModelProfile]]:
    """Yield ``(role_label, profile)`` for the PLAN / PLAN_REVIEW agents.

    These phases dispatch real agents and spend real budget, but their config
    is not a ModelProfile — ``plan`` is scalar fields the coordinator projects
    at dispatch time, and ``plan_agent_review`` carries either a pool or legacy
    scalars. A caller asking "which credentials will this run present?" needs
    them regardless, so this projects both into the same shape
    :func:`iter_config_profiles` yields.

    Only *enabled* phases are yielded: a disabled planner never dispatches, so
    its credential is not a fact about this run. The projection mirrors
    ``plan_flow``'s dispatch-time construction closely enough for auth
    classification (``cli`` / ``provider`` / ``model``); timeouts and tool
    surfaces are not modelled because no credential check reads them.
    """
    plan = getattr(config, "plan", None)
    if plan is not None and getattr(plan, "enabled", False):
        yield (
            "plan",
            ModelProfile(
                name="plan",
                cli=plan.cli,
                provider=plan.provider,
                model=plan.model,
                budget_usd=plan.budget_usd,
                timeout_seconds=plan.timeout,
                allowed_tools=config.preflight_profile.allowed_tools,
            ),
        )
    plan_agent_review = getattr(config, "plan_agent_review", None)
    if plan_agent_review is not None and getattr(plan_agent_review, "enabled", False):
        for profile in plan_agent_review.profiles:
            yield ("plan-review", profile)


def _agents_from_models(
    models: list[str],
    budget_usd: float,
    *,
    registry: dict[str, AgentSpec] | None = None,
) -> list[AgentDef]:
    """Derive AgentDef pool from the v0.8 models: list.

    Each entry becomes one agent keyed by the slash-replaced model key so names
    line up with the auto-assigned review_pool (see _auto_assign_models).
    Tier is derived from ModelInfo.cost_rank: 1→cheap, 2→mid, 3→strong.
    Budget is split evenly; per-role sizing is handled separately by the
    auto-assigner — this pool feeds assign_models() for adaptive selection.
    """
    if not models:
        return []
    per_agent_budget = budget_usd / len(models)
    agents: list[AgentDef] = []
    for key in models:
        info = _resolve_model_info(key, registry=registry)
        tier = _COST_RANK_TO_TIER.get(info.cost_rank, "mid")
        agents.append(
            AgentDef(
                name=key.replace("/", "-"),
                provider=info.provider,
                model=info.model,
                budget_usd=per_agent_budget,
                timeout_seconds=DEFAULT_DEV_PROFILE.timeout_seconds,
                tier=tier,
                cli=info.cli,
                transport=info.transport,
                base_url=info.base_url,
                registry_id=info.registry_id,
                registry_source=info.registry_source,
                input_cost_per_mtok=info.input_cost_per_mtok,
                output_cost_per_mtok=info.output_cost_per_mtok,
            )
        )
    return agents


CLI_PROVIDER_MAP: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
}

log = logging.getLogger("theforge.config")


def _parse_model_spec(
    data: dict[str, Any],
    default_model: str,
    default_fallbacks: tuple[str, ...],
    profile_name: str,
) -> tuple[str, tuple[str, ...]]:
    """Parse the model preference list from profile data.

    Supports three YAML forms (in precedence order):
      - models: [model-a, model-b, ...]       → list of models
      - model: [model-a, model-b, ...]         → list form of model key
      - model: "model-a"                       → scalar (existing behavior)

    Additional fallback_models: [...] entries are appended to the list.
    Returns (primary_model, fallback_models_tuple).
    """
    raw_models = data.get("models")
    raw_model = data.get("model")

    # models: key takes precedence over model:
    spec = raw_models if raw_models is not None else raw_model

    if isinstance(spec, list):
        if not spec:
            raise ValueError(
                f"'model'/'models' list in profile {profile_name!r} must be non-empty"
            )
        primary = str(spec[0])
        fallbacks = tuple(str(m) for m in spec[1:])
    elif isinstance(spec, str):
        primary = spec
        fallbacks = ()
    elif spec is None:
        primary = default_model
        fallbacks = default_fallbacks
    else:
        raise ValueError(
            f"'model'/'models' in profile {profile_name!r} must be a string or list, "
            f"got {type(spec).__name__}"
        )

    # fallback_models: key appends to any list already parsed
    extra = data.get("fallback_models")
    if extra is not None:
        if not isinstance(extra, list):
            raise ValueError(f"'fallback_models' in profile {profile_name!r} must be a list")
        fallbacks = fallbacks + tuple(str(m) for m in extra)

    return primary, fallbacks


_VALID_SANDBOX_MODES = {"workspace-write", "read-only", "none"}


# Override keys that select or constrain which model runs. When a role override
# contains any of these, the operator has expressed a preference about model
# identity/transport, so complexity-aware routing must not overwrite it.
# Resource-only keys (timeout*, budget) express a preference about limits, not
# model choice, and must leave routing active. See #1764.
_MODEL_SELECTION_KEYS: frozenset[str] = frozenset(
    {"model", "models", "fallback_models", "provider", "cli", "transport"}
)


def override_constrains_model(data: dict[str, Any] | None) -> bool:
    """Return True if a role-override block pins/constrains model identity.

    An override that touches only resource limits (``timeout_seconds``,
    ``timeout_medium_seconds``, ``timeout_large_seconds``, ``budget_usd``) or
    other non-model fields expresses a preference about budgets, not model
    selection, so it must not suppress complexity-aware model routing. Only an
    override that names a model, model list, fallbacks, provider, or cli pins
    the role. See #1764.
    """
    if not isinstance(data, dict):
        return False
    return any(key in _MODEL_SELECTION_KEYS for key in data)


def parse_override_transport_kind(data: dict[str, Any], where: str) -> str | None:
    """Read the canonical ``transport: {kind: cli|api}`` object off an override block.

    Returns None when the block does not declare a transport (the raw
    ``cli``/``provider`` spelling is then used to derive one).
    """
    if "transport" not in data:
        return None
    raw = data["transport"]
    if not isinstance(raw, dict):
        raise ValueError(f"'{where}.transport' must be a mapping with a 'kind' key")
    unknown = set(raw) - {"kind"}
    if unknown:
        raise ValueError(f"'{where}.transport' only supports 'kind'; got {sorted(unknown)}")
    kind = raw.get("kind")
    if kind not in ("cli", "api"):
        raise ValueError(f"'{where}.transport.kind' must be 'cli' or 'api', got {kind!r}")
    return str(kind)


def _resolve_override_transport(
    base: ModelProfile,
    data: dict[str, Any],
    new_cli: str | None,
    new_provider: str | None,
) -> "TransportSpec | None":
    """Compute the TransportSpec an override block resolves to.

    An explicit ``transport.kind`` wins and is applied to the effective provider
    family. Otherwise the raw cli/provider spelling is normalized. When the
    override touches neither, the base profile's transport carries over.
    """
    from .models import provider_for_transport, transport_for, transport_from_raw_fields

    kind = parse_override_transport_kind(data, f"overrides.{base.name}")
    if kind is not None:
        provider = new_provider or (
            provider_for_transport(base.transport) if base.transport is not None else None
        )
        if provider is None:
            raise ValueError(
                f"overrides.{base.name}.transport needs a provider: none is set on the "
                "override or on the profile being overridden"
            )
        return transport_for(provider, kind)
    if "provider" in data or "cli" in data:
        return transport_from_raw_fields(new_cli, new_provider)
    return base.transport


def _apply_profile_overrides(base: ModelProfile, data: dict[str, Any]) -> ModelProfile:
    """Apply partial forge.yaml profile overrides on top of an auto-assigned profile.

    Transport switching: an override may name ``transport: {kind: cli|api}``
    directly (canonical), or supply the raw ``provider``/``cli`` spelling. Either
    way the resulting TransportSpec is computed *here*, at the parse boundary,
    and passed explicitly — a transport switch must never leave the base
    profile's old TransportSpec in place.
    """
    tools = data.get("allowed_tools")
    # Transport switching — mirror _apply_ref_overrides semantics
    if "provider" in data and "cli" not in data:
        new_cli: str | None = None
        new_provider: str | None = data["provider"]
    elif "cli" in data and "provider" not in data:
        new_cli = data["cli"]
        new_provider = None
    else:
        new_cli = data.get("cli", base.cli)
        new_provider = data.get("provider", base.provider)
    effective_provider = new_provider
    new_transport = _resolve_override_transport(base, data, new_cli, new_provider)
    reasoning_effort = data.get("reasoning_effort", base.reasoning_effort)
    _VALID_REASONING_EFFORTS = {"low", "medium", "high"}
    if reasoning_effort is not None and reasoning_effort not in _VALID_REASONING_EFFORTS:
        raise ValueError(
            f"reasoning_effort must be one of {sorted(_VALID_REASONING_EFFORTS)}, "
            f"got {reasoning_effort!r} in profile {base.name!r}"
        )
    sandbox_mode = data.get("sandbox_mode", base.sandbox_mode)
    if sandbox_mode not in _VALID_SANDBOX_MODES:
        raise ValueError(
            f"sandbox_mode must be one of {sorted(_VALID_SANDBOX_MODES)}, "
            f"got {sandbox_mode!r} in profile {base.name!r}"
        )
    timeout_medium_raw = data.get("timeout_medium_seconds", base.timeout_medium_seconds)
    timeout_large_raw = data.get("timeout_large_seconds", base.timeout_large_seconds)
    model_str, fallback_models = _parse_model_spec(
        data, base.model, base.fallback_models, base.name
    )
    return ModelProfile(
        name=base.name,
        cli=new_cli,
        provider=new_provider,
        transport=new_transport,
        model=model_str,
        fallback_models=fallback_models,
        budget_usd=float(data.get("budget_usd", base.budget_usd)),
        timeout_seconds=int(data.get("timeout_seconds", base.timeout_seconds)),
        timeout_medium_seconds=int(timeout_medium_raw) if timeout_medium_raw is not None else None,
        timeout_large_seconds=int(timeout_large_raw) if timeout_large_raw is not None else None,
        allowed_tools=(
            tuple(tools)
            if tools is not None
            else (API_PROVIDER_DEFAULT_TOOLS if effective_provider else base.allowed_tools)
        ),
        reasoning_effort=reasoning_effort,
        thinking_budget=int(thinking_budget_raw)
        if (thinking_budget_raw := data.get("thinking_budget", base.thinking_budget)) is not None
        else None,
        base_url=data.get("base_url", base.base_url),
        max_iterations=int(max_iter_raw)
        if (max_iter_raw := data.get("max_iterations", base.max_iterations)) is not None
        else None,
        api_fallback=base.api_fallback,
        github_handle=data.get("github_handle", base.github_handle),
        phase=base.phase,
        sandbox_mode=sandbox_mode,
    )


def _auto_assign_models(
    models: list[str],
    budget_usd: float,
    *,
    registry: dict[str, AgentSpec] | None = None,
) -> tuple[ModelProfile, ModelProfile, list[ModelProfile], ModelProfile | None]:
    """Auto-assign models to stages from a declarative pool.

    Assignment algorithm:
    1. Sort by cost_rank asc, capability desc
    2. dev = cheapest capable model
    3. preflight = cheapest "fast" tier, else same as dev
    4. review_pool = all models except dev (if only 1, pool = [dev])
    5. synthesis = highest-capability model from review_pool (skip if pool <= 1)

    Budget distribution:
    - dev: 60% of total
    - preflight: max(2%, $1)
    - synthesis: max(2%, $1) when pool > 1
    - each reviewer: remaining / pool_size
    """
    infos = [(m, _resolve_model_info(m, registry=registry)) for m in models]
    sorted_models = sorted(
        infos,
        key=lambda x: (
            x[1].cost_rank,
            -x[1].capability,
            price_tiebreak_signal(x[1].input_cost_per_mtok, x[1].output_cost_per_mtok),
        ),
    )

    dev_key, dev_info = sorted_models[0]

    fast = [(k, i) for k, i in sorted_models if i.tier == "fast"]
    preflight_key, preflight_info = fast[0] if fast else sorted_models[0]

    review_pairs = [(k, i) for k, i in sorted_models if k != dev_key]
    if not review_pairs:
        review_pairs = [(dev_key, dev_info)]

    has_synthesis = len(review_pairs) > 1

    preflight_budget = max(budget_usd * 0.02, 1.0)
    dev_budget = budget_usd * 0.60
    synthesis_budget = max(budget_usd * 0.02, 1.0) if has_synthesis else 0.0
    remaining = max(budget_usd - dev_budget - preflight_budget - synthesis_budget, 0.0)
    reviewer_budget = remaining / len(review_pairs)

    dev_profile = ModelProfile(
        name="dev",
        cli=dev_info.cli,
        provider=dev_info.provider,
        transport=dev_info.transport,
        base_url=dev_info.base_url,
        model=dev_info.model,
        budget_usd=dev_budget,
        timeout_seconds=DEFAULT_DEV_PROFILE.timeout_seconds,
        allowed_tools=DEFAULT_DEV_PROFILE.allowed_tools,
        phase="dev",
        registry_id=dev_info.registry_id,
        registry_source=dev_info.registry_source,
    )
    preflight_profile = ModelProfile(
        name="preflight",
        cli=preflight_info.cli,
        provider=preflight_info.provider,
        transport=preflight_info.transport,
        base_url=preflight_info.base_url,
        model=preflight_info.model,
        budget_usd=preflight_budget,
        timeout_seconds=DEFAULT_PREFLIGHT_PROFILE.timeout_seconds,
        allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
        phase="preflight",
        registry_id=preflight_info.registry_id,
        registry_source=preflight_info.registry_source,
    )
    review_pool = [
        ModelProfile(
            name=k.replace("/", "-"),
            cli=i.cli,
            provider=i.provider,
            transport=i.transport,
            base_url=i.base_url,
            model=i.model,
            budget_usd=reviewer_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
            registry_id=i.registry_id,
            registry_source=i.registry_source,
        )
        for k, i in review_pairs
    ]

    synthesis_profile: ModelProfile | None = None
    if has_synthesis:
        synth_key, synth_info = max(review_pairs, key=lambda x: x[1].capability)
        synthesis_profile = ModelProfile(
            name="synthesis",
            cli=synth_info.cli,
            provider=synth_info.provider,
            transport=synth_info.transport,
            base_url=synth_info.base_url,
            model=synth_info.model,
            budget_usd=synthesis_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
            registry_id=synth_info.registry_id,
            registry_source=synth_info.registry_source,
        )

    return dev_profile, preflight_profile, review_pool, synthesis_profile


def _parse_profile(
    name: str,
    data: dict[str, Any],
    *,
    role: str = "review",
    secrets: dict[str, str] | None = None,
) -> ModelProfile:
    """Parse a model profile from forge.yaml data.

    role controls which defaults to apply: "dev" uses DEFAULT_DEV_PROFILE,
    anything else uses DEFAULT_REVIEW_PROFILE. This prevents pool entries
    named "dev" from accidentally inheriting dev-level tools/timeouts.
    """
    if "]" in name:
        raise ValueError(
            f"Profile name {name!r} contains ']', which is not allowed. "
            "Profile names are used as '[name] description' attribution prefixes; "
            "a ']' in the name would break reviewer attribution parsing."
        )
    default = DEFAULT_DEV_PROFILE if role == "dev" else DEFAULT_REVIEW_PROFILE
    cli = data.get("cli")
    provider = data.get("provider")

    if not cli and not provider:
        # Fallback to default if neither is specified
        cli = default.cli
        provider = default.provider

    if cli and cli not in SUPPORTED_CLIS:
        raise ValueError(
            f"Unsupported CLI {cli!r} in profile {name!r}. Supported: {sorted(SUPPORTED_CLIS)}"
        )
    if provider:
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unsupported provider {provider!r} in profile {name!r}. "
                f"Supported: {sorted(SUPPORTED_PROVIDERS)}"
            )
        # Eagerly validate provider readiness
        sdk = PROVIDER_SDK_MAP.get(provider)
        if sdk:
            try:
                importlib.import_module(sdk)
            except ImportError:
                raise ValueError(
                    f"Profile {name!r} uses provider '{provider}' but the required "
                    f"SDK '{sdk}' is not installed. Please install it."
                )
        # Build a stub profile to reuse check_agent_auth for key validation
        _stub = ModelProfile(
            name=name,
            cli=None,
            provider=provider,
            model=data.get("model", ""),
            budget_usd=0.0,
            timeout_seconds=0,
            allowed_tools=(),
            base_url=data.get("base_url"),
        )
        _ready, _reason = check_agent_auth(_stub, secrets or {})
        if not _ready:
            log.warning(
                "Profile %r uses provider %r: %s — this agent will be skipped at runtime.",
                name,
                provider,
                _reason,
            )

    tools = data.get("allowed_tools")
    reasoning_effort = data.get("reasoning_effort")
    _VALID_REASONING_EFFORTS = {"low", "medium", "high"}
    if reasoning_effort is not None and reasoning_effort not in _VALID_REASONING_EFFORTS:
        raise ValueError(
            f"reasoning_effort must be one of {sorted(_VALID_REASONING_EFFORTS)}, "
            f"got {reasoning_effort!r} in profile {name!r}"
        )
    timeout_medium_raw = data.get("timeout_medium_seconds")
    timeout_large_raw = data.get("timeout_large_seconds")

    # Build allowed_tools tuple. For API profiles, normalize capitalized names to canonical names.
    if tools is not None:
        if provider:
            from theforge.runners.tool_runtime import TOOL_NAME_MAP

            allowed_tools_tuple = tuple(TOOL_NAME_MAP.get(t, t) for t in tools)
        else:
            allowed_tools_tuple = tuple(tools)
    elif provider:
        allowed_tools_tuple = API_PROVIDER_DEFAULT_TOOLS
    else:
        allowed_tools_tuple = default.allowed_tools

    sandbox_mode = data.get("sandbox_mode", "workspace-write")
    if sandbox_mode not in _VALID_SANDBOX_MODES:
        raise ValueError(
            f"sandbox_mode must be one of {sorted(_VALID_SANDBOX_MODES)}, "
            f"got {sandbox_mode!r} in profile {name!r}"
        )
    model_str, fallback_models = _parse_model_spec(data, default.model, (), name)
    return ModelProfile(
        name=name,
        cli=cli,
        provider=provider,
        model=model_str,
        fallback_models=fallback_models,
        budget_usd=float(data.get("budget_usd", default.budget_usd)),
        timeout_seconds=int(data.get("timeout_seconds", default.timeout_seconds)),
        timeout_medium_seconds=int(timeout_medium_raw) if timeout_medium_raw is not None else None,
        timeout_large_seconds=int(timeout_large_raw) if timeout_large_raw is not None else None,
        allowed_tools=allowed_tools_tuple,
        reasoning_effort=reasoning_effort,
        thinking_budget=int(thinking_budget_raw)
        if (thinking_budget_raw := data.get("thinking_budget")) is not None
        else None,
        review_role=data.get("review_role"),
        phase=role,
        base_url=data.get("base_url"),
        max_iterations=int(max_iter_raw)
        if (max_iter_raw := data.get("max_iterations")) is not None
        else None,
        github_handle=data.get("github_handle"),
        sandbox_mode=sandbox_mode,
    )


def _parse_transport_fallbacks(
    data: dict[str, Any],
    *,
    secrets: dict[str, str] | None = None,
) -> dict[str, TransportFallbackConfig]:
    """Parse the top-level ``transport_fallback:`` block.

    Keyed by provider family: the entry under ``anthropic`` names the model the
    *Anthropic API* transport runs when the *Anthropic CLI* transport fails. The
    provider never changes across a fallback — only the transport does.
    """
    if not data:
        return {}
    if not isinstance(data, dict):
        raise ValueError("transport_fallbacks must be a mapping of provider -> config")

    fallbacks: dict[str, TransportFallbackConfig] = {}
    for provider, raw in data.items():
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unsupported provider {provider!r} in transport_fallbacks. "
                f"Supported: {sorted(SUPPORTED_PROVIDERS)}"
            )
        if provider == "deepseek":
            raise ValueError(
                "transport_fallbacks does not support 'deepseek' because it has no CLI"
            )
        if not isinstance(raw, dict):
            raise ValueError(f"transport_fallbacks.{provider} must be a mapping")

        fallback_profile = _parse_profile(
            f"{provider}-api-fallback",
            {"provider": provider, **raw},
            role="review",
            secrets=secrets,
        )
        fallbacks[provider] = TransportFallbackConfig(
            provider=provider,
            model=fallback_profile.model,
            timeout_seconds=fallback_profile.timeout_seconds,
            reasoning_effort=fallback_profile.reasoning_effort,
            thinking_budget=fallback_profile.thinking_budget,
            base_url=fallback_profile.base_url,
            max_iterations=fallback_profile.max_iterations,
        )
    return fallbacks


def _apply_transport_fallback(
    profile: ModelProfile,
    fallbacks: dict[str, TransportFallbackConfig],
) -> ModelProfile:
    """Attach the CLI→API transport fallback to CLI-transport profiles.

    Reads ``transport.kind`` — not the legacy cli/provider pair — to decide what
    a profile currently dispatches through.
    """
    if profile.mode != "cli":
        return profile
    provider = profile.provider_family
    if provider is None:
        return profile
    fallback = fallbacks.get(provider)
    if fallback is None:
        return profile
    return ModelProfile(
        name=profile.name,
        cli=profile.cli,
        provider=profile.provider,
        model=profile.model,
        budget_usd=profile.budget_usd,
        timeout_seconds=profile.timeout_seconds,
        allowed_tools=profile.allowed_tools,
        timeout_medium_seconds=profile.timeout_medium_seconds,
        timeout_large_seconds=profile.timeout_large_seconds,
        reasoning_effort=profile.reasoning_effort,
        thinking_budget=profile.thinking_budget,
        review_role=profile.review_role,
        base_url=profile.base_url,
        max_tool_output_bytes=profile.max_tool_output_bytes,
        max_iterations=profile.max_iterations,
        fallback_models=profile.fallback_models,
        api_fallback=fallback,
        github_handle=profile.github_handle,
        phase=profile.phase,
        sandbox_mode=profile.sandbox_mode,
        transport=profile.transport,
    )
