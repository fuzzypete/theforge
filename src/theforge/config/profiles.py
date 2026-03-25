"""Profile parsing and smart config assignment."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from .defaults import (
    API_PROVIDER_DEFAULT_TOOLS,
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    PROVIDER_API_KEY_MAP,
    PROVIDER_SDK_MAP,
    SUPPORTED_CLIS,
)
from .models import _resolve_model_info
from .secrets import _resolve_secret
from .types import SUPPORTED_PROVIDERS, ModelProfile

log = logging.getLogger("theforge.config")


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
    timeout_medium_raw = data.get("timeout_medium_seconds", base.timeout_medium_seconds)
    timeout_large_raw = data.get("timeout_large_seconds", base.timeout_large_seconds)
    return ModelProfile(
        name=base.name,
        cli=data.get("cli", base.cli),
        provider=data.get("provider", base.provider),
        model=data.get("model", base.model),
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
        base_url=data.get("base_url", base.base_url),
        max_iterations=int(max_iter_raw)
        if (max_iter_raw := data.get("max_iterations", base.max_iterations)) is not None
        else None,
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
    )
    preflight_profile = ModelProfile(
        name="preflight",
        cli=preflight_info.cli,
        provider=None,
        model=preflight_info.model,
        budget_usd=preflight_budget,
        timeout_seconds=DEFAULT_PREFLIGHT_PROFILE.timeout_seconds,
        allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
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
        base_url_early = data.get("base_url")
        _is_local = base_url_early and any(
            base_url_early.startswith(p) for p in ("http://localhost", "http://127.0.0.1")
        )
        api_key_var = PROVIDER_API_KEY_MAP.get(provider)
        if api_key_var and not _resolve_secret(api_key_var, secrets or {}) and not _is_local:
            log.warning(
                "Profile %r uses provider %r but $%s is not set — "
                "this agent will be skipped at runtime.",
                name,
                provider,
                api_key_var,
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

    return ModelProfile(
        name=name,
        cli=cli,
        provider=provider,
        model=data.get("model", default.model),
        budget_usd=float(data.get("budget_usd", default.budget_usd)),
        timeout_seconds=int(data.get("timeout_seconds", default.timeout_seconds)),
        timeout_medium_seconds=int(timeout_medium_raw) if timeout_medium_raw is not None else None,
        timeout_large_seconds=int(timeout_large_raw) if timeout_large_raw is not None else None,
        allowed_tools=allowed_tools_tuple,
        reasoning_effort=reasoning_effort,
        review_role=data.get("review_role"),
        base_url=data.get("base_url"),
        max_iterations=int(max_iter_raw)
        if (max_iter_raw := data.get("max_iterations")) is not None
        else None,
    )
