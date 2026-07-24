from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

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


# CLI runners with no native sandbox flag — their write containment comes
# solely from the host wrapper (sandbox-exec/bwrap). Codex, by contrast, passes
# a provider-native ``--sandbox`` flag, so it is mechanically contained without
# the host wrapper.
_HOST_WRAPPED_CLIS = frozenset({"claude", "gemini"})


def _host_sandbox_available() -> bool:
    """Return True when the host sandbox wrapper (sandbox-exec/bwrap) is usable."""
    from theforge.runners.sandbox import workspace_effect_sandbox_command

    probe = workspace_effect_sandbox_command(["true"], Path.cwd())
    return bool(probe) and probe[0] != "true"


def _host_unavailable_reason(effect_label: str) -> tuple[bool, str]:
    """Build the (False, reason) tuple for an unavailable host sandbox."""
    system = platform.system()
    if system == "Darwin":
        return (
            False,
            f"workspace sandbox unavailable: sandbox-exec not usable; {effect_label}",
        )
    if system == "Linux":
        return (
            False,
            f"workspace sandbox unavailable: bwrap not usable; {effect_label}",
        )
    return (True, "")


def _sandbox_readiness(profile: ModelProfile) -> tuple[bool, str]:
    """Return whether *mechanical* workspace containment is active for this profile.

    ``True`` means agent writes are confined by a real OS mechanism (host
    sandbox-exec/bwrap wrapper, or a provider-native sandbox flag). It is NOT
    set merely because ``sandbox_mode != none`` — a native/prompt-only
    permission mode is cooperative, not mechanical, so profiles that rely on the
    host wrapper report ``False`` when that wrapper is unavailable (#1907).
    """
    if profile.mode != "api":
        # CLI profiles: sandbox explicitly disabled → not contained.
        if profile.sandbox_mode == "none":
            return (False, "sandbox disabled by sandbox_mode: none")
        # Claude/Gemini have no native sandbox; containment is the host wrapper.
        if profile.cli in _HOST_WRAPPED_CLIS:
            if _host_sandbox_available():
                return (True, "")
            return _host_unavailable_reason(
                "CLI write containment cannot be enforced; refusing to run unsandboxed"
            )
        # Codex and other CLIs assert a provider-native --sandbox flag.
        return (True, "")
    if "bash" not in profile.allowed_tools:
        return (True, "")

    if _host_sandbox_available():
        return (True, "")
    return _host_unavailable_reason("bash/tool effects will run unsandboxed")


def sandbox_available_for_profile(profile: ModelProfile) -> bool:
    """Return True if *mechanical* workspace containment is available for *profile*.

    Thin public wrapper around the private probe; safe to call repeatedly
    because the underlying sandbox availability check is lru_cache-backed.
    """
    available, _ = _sandbox_readiness(profile)
    return available


def sandbox_containment_mode(profile: ModelProfile) -> str:
    """Classify how a run's writes are contained, for audit/status output.

    Distinguishes mechanically-contained runs from native-flag and
    uncontained/prompt-only ones (#1907):

    - ``"mechanical"`` — host sandbox wrapper (sandbox-exec/bwrap) confines writes.
    - ``"native"``     — provider-native sandbox flag (e.g. Codex ``--sandbox``).
    - ``"unavailable"``— containment requested but the host wrapper is missing;
      the run fails closed rather than proceeding with prompt-only discipline.
    - ``"none"``       — no containment (``sandbox_mode: none`` or no tool surface).
    """
    if profile.mode != "api":
        if profile.sandbox_mode == "none":
            return "none"
        if profile.cli in _HOST_WRAPPED_CLIS:
            return "mechanical" if _host_sandbox_available() else "unavailable"
        return "native"
    if "bash" not in profile.allowed_tools:
        return "none"
    return "mechanical" if _host_sandbox_available() else "unavailable"


def _launcher_sandbox_readiness(profile: ModelProfile) -> tuple[bool, str]:
    """CLI launchers are always auth-ready; binary presence checked separately."""
    return (True, "")


def check_agent_auth(
    profile: ModelProfile,
    secrets: dict[str, str] | None = None,
    *,
    include_sandbox_readiness: bool = True,
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
            if not ok:
                return (False, "npx not found in PATH")
            if include_sandbox_readiness:
                return _launcher_sandbox_readiness(profile)
            return (True, "")
        # ghaw dispatches through the `gh` binary; the agent executes remotely
        # on GitHub Actions, so local sandbox readiness does not apply.
        if profile.cli == "ghaw":
            if shutil.which("gh") is None:
                return (False, "'gh' not found in PATH (required for cli: ghaw)")
            return (True, "")
        ok = shutil.which(profile.cli) is not None
        if not ok:
            return (False, f"{profile.cli!r} not found in PATH")
        if include_sandbox_readiness:
            return _launcher_sandbox_readiness(profile)
        return (True, "")

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
            if not ok:
                return (False, "GOOGLE_API_KEY or GEMINI_API_KEY not set")
            if include_sandbox_readiness:
                return _sandbox_readiness(profile)
            return (True, "")

        # All other providers
        key_var = PROVIDER_API_KEY_MAP.get(profile.provider)
        if not key_var:
            if include_sandbox_readiness:
                return _sandbox_readiness(profile)
            return (True, "")
        ok = bool(merged.get(key_var))
        if not ok:
            return (False, f"{key_var} not set")
        if include_sandbox_readiness:
            return _sandbox_readiness(profile)
        return (True, "")

    # Neither cli nor provider set
    raise ValueError(
        f"check_agent_auth: profile {profile.name!r} has neither 'cli' nor 'provider' set"
    )
