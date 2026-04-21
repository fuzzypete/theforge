"""Credential and auth-state scrub definitions for gate/test isolation."""

from __future__ import annotations

from pathlib import Path

from .auth import PROVIDER_API_KEY_MAP
from .models import AGENT_REGISTRY

_PROVIDER_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_AUTH_TOKEN",),
    "google": ("GEMINI_API_KEY",),
    "openai": ("OPENAI_BASE_URL",),
}

_DOTENV_ENV_VARS: tuple[str, ...] = (
    "DOTENV_PATH",
    "DOTENV_FILE",
    "PYTHON_DOTENV_DISABLED",
)

_PROVIDER_ENV_VARS = {env_var for env_var in PROVIDER_API_KEY_MAP.values()}
for spec in AGENT_REGISTRY.values():
    _PROVIDER_ENV_VARS.update(_PROVIDER_ENV_ALIASES.get(spec.provider, ()))

SCRUBBED_ENV_VARS: tuple[str, ...] = tuple(
    sorted(
        {
            *_PROVIDER_ENV_VARS,
            *_DOTENV_ENV_VARS,
        }
    )
)

SCRUBBED_HOME_PATHS: tuple[Path, ...] = (
    Path(".codex") / "auth.json",
    Path(".claude"),
    Path(".config") / "gcloud",
    Path(".gemini"),
)

__all__ = ["SCRUBBED_ENV_VARS", "SCRUBBED_HOME_PATHS"]
