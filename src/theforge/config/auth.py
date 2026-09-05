from __future__ import annotations

import json
import os
import platform
import shutil
import time
from collections.abc import Mapping
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


def _grants_bash(profile: ModelProfile) -> bool:
    """True when this profile's tool surface grants bash under any spelling.

    ``allowed_tools`` carries two vocabularies: forge.yaml's capitalized CLI
    names (``"Bash"``) and the canonical internal names an API profile's tool
    schema is built from (``"bash"``). The API runner canonicalizes before
    granting, so a containment check comparing raw strings against ``"bash"``
    answers a different question than the dispatch does — it reported "no bash
    surface" for a profile the runner then handed bash to (#2346). Ask the
    runner's own map instead of guessing at the spelling.
    """
    from theforge.runners.tool_runtime import grants_bash  # noqa: PLC0415

    return grants_bash(profile.allowed_tools)


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
    if not _grants_bash(profile):
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
    if not _grants_bash(profile):
        return "none"
    return "mechanical" if _host_sandbox_available() else "unavailable"


def _launcher_sandbox_readiness(profile: ModelProfile) -> tuple[bool | None, str]:
    """CLI launchers are always auth-ready; binary presence checked separately."""
    return (None, "unverified")


# ── Claude CLI credential store ──────────────────────────────────────────
# The `claude` CLI authenticates from an OAuth credential file in the user's
# home directory. That credential belongs to the same token family as an
# interactive Claude Code session: a fresh interactive sign-in revokes the
# previous family server-side, and the copy this substrate depends on starts
# returning `401 OAuth access token has been revoked` for every call (#1952).
_CLAUDE_CREDENTIALS_RELPATH = (".claude", ".credentials.json")

# The credential file's OAuth block.
_CLAUDE_OAUTH_KEY = "claudeAiOauth"

# Environment variables that let the CLI authenticate WITHOUT reading the OAuth
# credential store at all. When one is set the store is irrelevant, so probing
# it could only produce a false abort.
#
# Public because the gate/test scrub must strip exactly this set: an ambient
# token that silently satisfies the probe would make credential tests pass or
# fail depending on the developer's shell (CONVENTIONS: tests assert on
# controlled state, not on the host).
CLAUDE_CLI_ENV_TOKENS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def claude_credentials_path() -> Path:
    """Path to the `claude` CLI OAuth credential store for the current user."""
    return Path.home().joinpath(*_CLAUDE_CREDENTIALS_RELPATH)


def _as_epoch_ms(raw: object) -> int:
    """Coerce a credential timestamp to epoch milliseconds; 0 when unusable."""
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def check_claude_credentials(
    merged: Mapping[str, str] | None = None,
    *,
    credentials_path: Path | None = None,
    now_ms: float | None = None,
) -> tuple[bool, str]:
    """Return ``(ready, reason)`` for the `claude` CLI OAuth credential store.

    This inspects the credential the CLI will actually present, so a sprint can
    establish that its agents are reachable before it commits wall clock and
    budget to them rather than rediscovering a dead credential one invocation
    at a time.

    ``ready`` is False only when the store carries positive evidence that no
    usable credential exists. Absence of evidence is not evidence: a missing
    file is treated as ready, because the CLI also authenticates from the macOS
    Keychain and from the environment, and aborting on a file this process
    cannot see would be a false negative on a perfectly healthy host.

    Two failure shapes matter in practice:

    - **Revoked.** After repeated 401s the CLI blanks ``accessToken`` and
      ``refreshToken`` to empty strings *while leaving* ``refreshTokenExpiresAt``
      populated with a future date. The credential superficially reads as valid
      until token *length* is checked rather than expiry, which is exactly why
      an expiry-only check misses it.
    - **Unrefreshable.** The access token has expired and no unexpired refresh
      token remains to mint a new one.

    ``reason`` names the credential path and the condition. It never contains
    token material — not a value, not a prefix, not a length beyond the
    empty/non-empty distinction the classification itself rests on.
    """
    env = merged if merged is not None else os.environ
    for var in CLAUDE_CLI_ENV_TOKENS:
        if (env.get(var) or "").strip():
            return (True, "")

    path = credentials_path if credentials_path is not None else claude_credentials_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # No store on disk: the CLI may authenticate from the Keychain or from
        # a token this process cannot observe. No claim either way.
        return (True, "")
    except OSError as exc:
        return (False, f"claude credential store at {path} is unreadable: {exc.strerror or exc}")

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return (
            False,
            f"claude credential store at {path} is not valid JSON; "
            "re-authenticate the CLI (`claude` → /login)",
        )
    if not isinstance(payload, dict):
        return (
            False,
            f"claude credential store at {path} is not a JSON object; "
            "re-authenticate the CLI (`claude` → /login)",
        )

    oauth = payload.get(_CLAUDE_OAUTH_KEY)
    if not isinstance(oauth, dict):
        # A store written by a different auth backend (or a future CLI layout).
        # Nothing this probe can classify, so it makes no claim.
        return (True, "")

    access = str(oauth.get("accessToken") or "").strip()
    refresh = str(oauth.get("refreshToken") or "").strip()
    expires_at = _as_epoch_ms(oauth.get("expiresAt"))
    refresh_expires_at = _as_epoch_ms(oauth.get("refreshTokenExpiresAt"))
    now = now_ms if now_ms is not None else time.time() * 1000.0

    if not access and not refresh:
        return (
            False,
            f"claude credential store at {path} holds no access or refresh token "
            "(the CLI blanks both after repeated auth rejections, so this is the "
            "signature of an OAuth family that was revoked server-side — most "
            "often by a newer interactive Claude Code sign-in). Re-authenticate "
            "the CLI (`claude` → /login) before starting a sprint.",
        )

    if access and expires_at > now:
        return (True, "")

    # The access token is absent or past its expiry — only an unexpired refresh
    # token can still produce a working call.
    if not refresh:
        return (
            False,
            f"claude credential store at {path} has an expired access token and "
            "no refresh token to renew it; re-authenticate the CLI "
            "(`claude` → /login)",
        )
    if refresh_expires_at and refresh_expires_at <= now:
        return (
            False,
            f"claude credential store at {path} has an expired access token and "
            "an expired refresh token; re-authenticate the CLI (`claude` → /login)",
        )
    return (True, "")


def check_agent_auth(
    profile: ModelProfile,
    secrets: dict[str, str] | None = None,
    *,
    include_sandbox_readiness: bool = True,
    include_credential_probe: bool = False,
) -> tuple[bool | None, str]:
    """Return ``(ready, reason)`` for *profile*.

    ``ready`` is True when the profile has the credentials/binaries it needs.
    ``reason`` is an empty string when ready, or a human-readable explanation
    of what is missing when not ready.

    Resolution order for API keys: ``secrets`` dict first, then ``os.environ``.

    Rules:
    - CLI profiles — checks whether the appropriate binary exists on PATH.
      ``claude`` -> ``shutil.which("claude")``;
      ``codex`` / ``gemini`` -> ``shutil.which("npx")``.
    - ``include_credential_probe`` additionally inspects the ``claude`` CLI's
      OAuth credential store (see :func:`check_claude_credentials`). It is
      opt-in because a *stale* credential must not fail config loading — that
      would refuse ``forge check-config``, the very command an operator runs to
      diagnose it. Callers that are about to spend money on agents (the sprint
      launch gate, ``forge check-config``'s reporting) pass True.
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
            return (None, "unverified")
        # ghaw dispatches through the `gh` binary; the agent executes remotely
        # on GitHub Actions, so local sandbox readiness does not apply.
        if profile.cli == "ghaw":
            if shutil.which("gh") is None:
                return (False, "'gh' not found in PATH (required for cli: ghaw)")
            return (None, "unverified")
        ok = shutil.which(profile.cli) is not None
        if not ok:
            return (False, f"{profile.cli!r} not found in PATH")
        if profile.cli == "claude" and include_credential_probe:
            cred_ready, cred_reason = check_claude_credentials(merged)
            if not cred_ready:
                return (False, cred_reason)
        if include_sandbox_readiness:
            return _launcher_sandbox_readiness(profile)
        return (None, "unverified")

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


def inference_only_secrets(secrets: "Mapping[str, str] | None") -> dict[str, str]:
    """Return only the credentials an advisory agent legitimately needs.

    An ALLOW-list, and the distinction from a deny-list is the point: denying
    ``GH_TOKEN`` answers "is today's known tracker credential absent?", while
    allowing only provider API keys answers "is every credential this stage
    holds one that cannot mutate a tracker?" Only the second survives an
    operator putting a new forge-of-record credential in ``.forge/.env``.

    A key qualifies if it is a provider API key by name, or one of the Claude
    CLI's inference-token env vars. Everything else — tracker tokens, webhook
    URLs, deploy credentials — is dropped, so an advisory stage cannot
    authenticate as the operator against anything.

    Lives here, next to the credential vocabulary it is derived from, because
    more than one advisory stage now needs it (backlog-triage proposals and the
    preflight decomposition assessment) and "which credentials may an advisory
    stage hold" must have exactly one answer.
    """
    if not secrets:
        return {}
    known = {value.upper() for value in PROVIDER_API_KEY_MAP.values()}
    known.update(token.upper() for token in CLAUDE_CLI_ENV_TOKENS)
    return {
        key: value
        for key, value in secrets.items()
        if key.upper() in known or key.upper().endswith("_API_KEY")
    }
