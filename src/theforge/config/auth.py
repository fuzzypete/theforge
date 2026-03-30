"""Unified agent authentication readiness check.

Single source of truth for "will this agent have usable auth at runtime?"
Used by config loading, adaptive assignment, and CLI tooling.
"""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import ModelProfile

# Supported CLIs that use npx as their invocation path
_NPX_CLIS: frozenset[str] = frozenset({"codex", "gemini"})

# Google accepts GEMINI_API_KEY as a fallback for GOOGLE_API_KEY
_GEMINI_KEY_FALLBACK = "GEMINI_API_KEY"


def agent_is_ready(
    profile: "ModelProfile",
    secrets: dict[str, str],
) -> tuple[bool, str]:
    """Return (True, "") if the agent has usable auth, (False, reason) otherwise.

    Auth resolution for API agents merges os.environ with secrets (secrets
    take precedence), matching runtime adapter behavior.

    CLI agents are checked by verifying the invocation binary exists:

    - ``"claude"``          → requires ``claude`` binary in PATH
    - ``"codex"``, ``"gemini"`` → requires ``npx`` in PATH (both use npx)

    Local API endpoints (``http://localhost`` or ``http://127.0.0.1``) skip
    API key requirements.

    Google provider accepts ``GEMINI_API_KEY`` as a fallback for
    ``GOOGLE_API_KEY``.

    Unsupported CLI or provider values return ``(False, descriptive_message)``
    instead of raising a ``KeyError``.
    """
    from .defaults import PROVIDER_API_KEY_MAP, SUPPORTED_CLIS
    from .types import SUPPORTED_PROVIDERS

    if profile.cli is not None:
        if profile.cli not in SUPPORTED_CLIS:
            return (
                False,
                f"unsupported CLI {profile.cli!r}; supported: {sorted(SUPPORTED_CLIS)}",
            )
        if profile.cli == "claude":
            if shutil.which("claude") is None:
                return False, "claude binary not found in PATH"
            return True, ""
        # codex and gemini both invoke via npx
        if shutil.which("npx") is None:
            return False, f"npx not found in PATH (required for {profile.cli} CLI)"
        return True, ""

    if profile.provider is not None:
        if profile.provider not in SUPPORTED_PROVIDERS:
            supported = sorted(SUPPORTED_PROVIDERS)
            return False, f"unsupported provider {profile.provider!r}; supported: {supported}"

        # Local endpoints skip API key requirements
        base_url: str | None = getattr(profile, "base_url", None)
        if base_url and (
            base_url.startswith("http://localhost") or base_url.startswith("http://127.0.0.1")
        ):
            return True, ""

        # Runtime adapters merge: {**os.environ, **(secrets or {})}
        # Secrets take precedence over os.environ.
        merged = {**os.environ, **secrets}

        if profile.provider == "google":
            # Primary key: GOOGLE_API_KEY; fallback: GEMINI_API_KEY
            if merged.get("GOOGLE_API_KEY") or merged.get(_GEMINI_KEY_FALLBACK):
                return True, ""
            return False, "GOOGLE_API_KEY (or GEMINI_API_KEY) is not set"

        key_var = PROVIDER_API_KEY_MAP.get(profile.provider)
        if key_var is None:
            # No key mapping for this provider — cannot check, assume OK
            return True, ""

        if merged.get(key_var):
            return True, ""
        return False, f"${key_var} is not set"

    # No cli, no provider — nothing to check
    return True, ""
