"""Unified auth resolution for TheForge model profiles.

All runtime callers should use ``check_agent_auth`` instead of hand-rolling
their own API-key or CLI-binary checks.
"""

from __future__ import annotations

import os
import shutil

from .defaults import PROVIDER_API_KEY_MAP, SUPPORTED_CLIS
from .types import SUPPORTED_PROVIDERS, ModelProfile

# CLI names that use npx rather than a direct binary
_NPX_CLIS = frozenset({"codex", "gemini"})

# Local endpoint prefixes — API key not required for these
_LOCAL_PREFIXES = (
    "http://localhost",
    "http://127.0.0.1",
    "http://0.0.0.0",
    "http://[::1]",
)


def check_agent_auth(
    profile: ModelProfile,
    secrets: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Return ``(ready, reason)`` for *profile*.

    ``ready`` is True when the profile has the credentials/binaries it needs.
    ``reason`` is an empty string when ready, or a human-readable explanation
    of what is missing when not ready.

    Resolution order for API keys: ``secrets`` dict first, then ``os.environ``.

    Rules:
    - CLI profiles — checks whether the appropriate binary exists on PATH.
      ``claude`` -> ``shutil.which("claude")``;
      ``codex`` / ``gemini`` -> ``shutil.which("npx")``.
    - API profiles with a local base_url (localhost / 127.0.0.1 / 0.0.0.0 / ::1):
      key check is skipped for ``openai`` and ``deepseek`` providers.
      ``google`` does NOT support local endpoints, so the key is always required.
    - ``google`` provider: checks ``GOOGLE_API_KEY`` first, then ``GEMINI_API_KEY``.
    - All other API providers: checks the key from ``PROVIDER_API_KEY_MAP``.

    Raises:
        ValueError: when ``profile.cli`` or ``profile.provider`` contains an
            unsupported value that we cannot classify.
    """
    merged: dict[str, str] = {**os.environ, **(secrets or {})}

    # ── CLI profiles ──────────────────────────────────────────────────
    if profile.cli is not None:
        if profile.cli not in SUPPORTED_CLIS:
            raise ValueError(
                f"check_agent_auth: unsupported CLI {profile.cli!r} in profile "
                f"{profile.name!r}. Supported: {sorted(SUPPORTED_CLIS)}"
            )
        if profile.cli in _NPX_CLIS:
            ok = shutil.which("npx") is not None
            return (True, "") if ok else (False, "npx not found in PATH")
        ok = shutil.which(profile.cli) is not None
        return (True, "") if ok else (False, f"{profile.cli!r} not found in PATH")

    # ── API profiles ──────────────────────────────────────────────────
    if profile.provider is not None:
        if profile.provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"check_agent_auth: unsupported provider {profile.provider!r} in profile "
                f"{profile.name!r}. Supported: {sorted(SUPPORTED_PROVIDERS)}"
            )

        # Local endpoints skip the key check (openai/deepseek only — not google)
        if profile.provider in {"openai", "deepseek"} and profile.base_url:
            if any(profile.base_url.startswith(p) for p in _LOCAL_PREFIXES):
                return (True, "")

        # Google: check GOOGLE_API_KEY then GEMINI_API_KEY as fallback
        if profile.provider == "google":
            ok = bool(merged.get("GOOGLE_API_KEY") or merged.get("GEMINI_API_KEY"))
            return (True, "") if ok else (False, "GOOGLE_API_KEY or GEMINI_API_KEY not set")

        # All other providers
        key_var = PROVIDER_API_KEY_MAP.get(profile.provider)
        if not key_var:
            return (True, "")
        ok = bool(merged.get(key_var))
        return (True, "") if ok else (False, f"{key_var} not set")

    # Neither cli nor provider set
    raise ValueError(
        f"check_agent_auth: profile {profile.name!r} has neither 'cli' nor 'provider' set"
    )
