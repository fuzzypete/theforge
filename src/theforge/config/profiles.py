"""Profile parsing and smart config assignment."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from .auth import check_agent_auth
from .defaults import (
    API_PROVIDER_DEFAULT_TOOLS,
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    PROVIDER_SDK_MAP,
    SUPPORTED_CLIS,
)
from .models import AgentDef, _resolve_model_info
from .types import SUPPORTED_PROVIDERS, ApiFallbackConfig, ModelProfile

_COST_RANK_TO_TIER = {1: "cheap", 2: "mid", 3: "strong"}


def _agents_from_models(models: list[str], budget_usd: float) -> list[AgentDef]:
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
        info = _resolve_model_info(key)
        tier = _COST_RANK_TO_TIER.get(info.cost_rank, "mid")
        agents.append(
            AgentDef(
                name=key.replace("/", "-"),
                provider=None,
                model=info.model,
                budget_usd=per_agent_budget,
                timeout_seconds=DEFAULT_DEV_PROFILE.timeout_seconds,
                tier=tier,
                cli=info.cli,
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


def _apply_profile_overrides(base: ModelProfile, data: dict[str, Any]) -> ModelProfile:
    """Apply partial forge.yaml profile overrides on top of an auto-assigned profile."""
    tools = data.get("allowed_tools")
    effective_provider = data.get("provider") or base.provider
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
        cli=data.get("cli", base.cli),
        provider=data.get("provider", base.provider),
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
    infos = [(m, _resolve_model_info(m)) for m in models]
    sorted_models = sorted(infos, key=lambda x: (x[1].cost_rank, -x[1].capability))

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
        provider=None,
        model=dev_info.model,
        budget_usd=dev_budget,
        timeout_seconds=DEFAULT_DEV_PROFILE.timeout_seconds,
        allowed_tools=DEFAULT_DEV_PROFILE.allowed_tools,
        phase="dev",
    )
    preflight_profile = ModelProfile(
        name="preflight",
        cli=preflight_info.cli,
        provider=None,
        model=preflight_info.model,
        budget_usd=preflight_budget,
        timeout_seconds=DEFAULT_PREFLIGHT_PROFILE.timeout_seconds,
        allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
        phase="preflight",
    )
    review_pool = [
        ModelProfile(
            name=k.replace("/", "-"),
            cli=i.cli,
            provider=None,
            model=i.model,
            budget_usd=reviewer_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
        )
        for k, i in review_pairs
    ]

    synthesis_profile: ModelProfile | None = None
    if has_synthesis:
        synth_key, synth_info = max(review_pairs, key=lambda x: x[1].capability)
        synthesis_profile = ModelProfile(
            name="synthesis",
            cli=synth_info.cli,
            provider=None,
            model=synth_info.model,
            budget_usd=synthesis_budget,
            timeout_seconds=DEFAULT_REVIEW_PROFILE.timeout_seconds,
            allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
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

    if cli and provider:
        raise ValueError(f"Profile {name!r} cannot have both 'cli' and 'provider' set. Use one.")
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


def _parse_provider_fallbacks(
    data: dict[str, Any],
    *,
    secrets: dict[str, str] | None = None,
) -> dict[str, ApiFallbackConfig]:
    """Parse top-level provider_fallbacks config."""
    if not data:
        return {}
    if not isinstance(data, dict):
        raise ValueError("provider_fallbacks must be a mapping of provider -> config")

    fallbacks: dict[str, ApiFallbackConfig] = {}
    for provider, raw in data.items():
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unsupported provider {provider!r} in provider_fallbacks. "
                f"Supported: {sorted(SUPPORTED_PROVIDERS)}"
            )
        if provider == "deepseek":
            raise ValueError(
                "provider_fallbacks does not support 'deepseek' because it has no CLI"
            )
        if not isinstance(raw, dict):
            raise ValueError(f"provider_fallbacks.{provider} must be a mapping")

        fallback_profile = _parse_profile(
            f"{provider}-api-fallback",
            {"provider": provider, **raw},
            role="review",
            secrets=secrets,
        )
        fallbacks[provider] = ApiFallbackConfig(
            provider=provider,
            model=fallback_profile.model,
            timeout_seconds=fallback_profile.timeout_seconds,
            reasoning_effort=fallback_profile.reasoning_effort,
            thinking_budget=fallback_profile.thinking_budget,
            base_url=fallback_profile.base_url,
            max_iterations=fallback_profile.max_iterations,
        )
    return fallbacks


def _apply_provider_fallback(
    profile: ModelProfile,
    fallbacks: dict[str, ApiFallbackConfig],
) -> ModelProfile:
    """Attach a same-provider API fallback to CLI profiles when configured."""
    if profile.provider or not profile.cli:
        return profile
    provider = CLI_PROVIDER_MAP.get(profile.cli)
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
        api_fallback=fallback,
        github_handle=profile.github_handle,
        phase=profile.phase,
        sandbox_mode=profile.sandbox_mode,
    )
