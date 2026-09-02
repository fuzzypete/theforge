"""CLI subprocess wrapper for invoking LLM agents.

Dispatches to the appropriate runner based on ModelProfile.transport.
Provider-specific runners live in dedicated modules:
  - runner_claude.py  — Claude Code CLI
  - runner_codex.py   — OpenAI Codex CLI
  - runner_gemini.py  — Google Gemini CLI
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from theforge.agent_types import (
    COST_UNKNOWN,
    FAILURE_KILLED_BEFORE_OUTPUT,
    AgentResult,
    ModelUsage,
)
from theforge.log_level import LogLevel
from theforge.log_util import _log_line

from ..config import ModelProfile
from ..config.models import model_fallback_transport
from .sandbox import capability_transport_refusal

# ── Logging helpers ───────────────────────────────────────────────────


def _log(msg: str) -> None:
    _log_line("[forge]", msg)


def _log_verbose(msg: str) -> None:
    from theforge.log_level import _LOG_LEVEL as _LL

    if _LL >= LogLevel.VERBOSE:
        _log_line("[forge]", msg)


# ── Heartbeat helper ─────────────────────────────────────────────────


@dataclass
class _SubprocessOutcome:
    """Mutable container for background subprocess result."""

    proc: subprocess.CompletedProcess[str] | None = None
    exception: BaseException | None = None


def _run_with_heartbeat(
    *,
    run_fn: Callable[[], subprocess.CompletedProcess[str]],
    label: str,
    profile: ModelProfile,
    cli_name: str,
    quiet: bool = False,
) -> tuple[_SubprocessOutcome, float]:
    """Run a subprocess in a background thread with 30s heartbeat.

    Returns (outcome, elapsed_seconds). The caller handles interpreting
    the outcome into an AgentResult.
    """
    if not quiet:
        _log(f"  Starting {label} (model={profile.model}, timeout={profile.timeout_seconds}s)...")

    outcome = _SubprocessOutcome()

    def _run() -> None:
        try:
            outcome.proc = run_fn()
        except BaseException as e:
            outcome.exception = e

    start = time.monotonic()
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    while thread.is_alive():
        thread.join(timeout=30)
        if thread.is_alive():
            elapsed = int(time.monotonic() - start)
            _log_verbose(f"  ... {label} still running ({elapsed}s elapsed)")

    elapsed = time.monotonic() - start
    return outcome, elapsed


def _decode_stream(raw: object) -> str:
    """Coerce a subprocess stream (str, bytes, or None) to text."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _handle_exception(
    exc: BaseException,
    *,
    profile: ModelProfile,
    cli_name: str,
    partial_cost_fn: Callable[[str], tuple[float | None, tuple[ModelUsage, ...]]] | None = None,
) -> AgentResult | None:
    """Handle common subprocess exceptions. Returns AgentResult or None to re-raise.

    ``partial_cost_fn`` is an opt-in seam for transports that can price the output
    a killed run had already emitted. It receives the partial stdout salvaged from
    the ``TimeoutExpired`` and returns ``(cost_usd, model_usage)``; returning
    ``(None, ())`` keeps the run cost-unknown. Runners that don't pass it (Claude,
    which reconstructs partial spend from its own stream loop, and Gemini) behave
    exactly as before.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        cost_usd: float | None = None
        model_usage: tuple[ModelUsage, ...] = ()
        if partial_cost_fn is not None:
            cost_usd, model_usage = partial_cost_fn(_decode_stream(exc.stdout))
        return AgentResult(
            success=False,
            output=f"TIMEOUT: Agent exceeded {profile.timeout_seconds}s limit",
            session_id=None,
            cost_usd=cost_usd,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
            model_usage=model_usage,
            # A timeout is a distinct, retryable failure kind — classify it so
            # the coordinator can hand back to dev instead of escalating.
            failure_code="timeout",
        )
    if isinstance(exc, FileNotFoundError):
        return AgentResult(
            success=False,
            output=f"ERROR: '{cli_name}' CLI not found. Is it installed?",
            session_id=None,
            cost_usd=None,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
            startup_failure=True,
        )
    return None


_CLI_RETRY_EXIT_CODES = frozenset({69, 75})
_CLI_QUOTA_FALLBACK_PATTERNS = (
    "429",
    "usage limit",
    "current quota",
    "quota limit",
    "free tier limits have been reached",
    "rate limit",
    "quota",
    "resource exhausted",
    "resource_exhausted",
    "spend limit",
    "credit balance",
    "insufficient credit",
    "balance is too low",
    "service unavailable",
    "temporarily unavailable",
    "unavailable",
    "overloaded",
    "try again later",
)
_CLI_MODEL_FALLBACK_PATTERNS = (
    "model not found",
    "model_not_found",
    "no such model",
    "invalid model",
    "model does not exist",
    "model is deprecated",
    "model has been deprecated",
)

# Known CLI binary names — used to distinguish CLI vs API model identifiers in
# fallback_models lists for CLI profiles.
_KNOWN_CLI_NAMES = frozenset({"claude", "codex", "gemini", "ghaw"})

# Quota refusals that name a reset moment. A limit with a stated reset time is a
# different fact from a limit without one: repeating the first is certain to
# reproduce it until that time, so the coordinator is entitled to stop rather
# than spend the remaining budget re-asking (#2298). Deliberately does NOT match
# the vague "try again later" — that carries no such certainty.
_CLI_QUOTA_RESET_PATTERNS = (
    re.compile(r"try again (?:at|after|on)\s+([^.\n]{3,80})", re.IGNORECASE),
    re.compile(r"(?:limit |quota )?resets?\s+(?:at|on|in)\s+([^.\n]{3,80})", re.IGNORECASE),
    re.compile(r"available again\s+(?:at|on|in)\s+([^.\n]{3,80})", re.IGNORECASE),
)


def _parse_quota_reset(output: str) -> str | None:
    """Return the reset moment a quota refusal stated, or None if it stated none."""
    for pattern in _CLI_QUOTA_RESET_PATTERNS:
        match = pattern.search(output)
        if match:
            return match.group(1).strip().rstrip(",;")
    return None


def _capability_refusal_result(
    profile: ModelProfile, transport_label: str, refusal: str
) -> AgentResult:
    """Fail-closed startup failure for a transport that cannot express a declaration.

    Uses the same ``SANDBOX_CAPABILITY_PROFILE_UNSUPPORTED`` marker the CLI
    runners emit, so the coordinator's existing failure classification
    (``agent_failure``, ``sprint.rca``) sees one capability-gap signal
    regardless of which transport refused.
    """
    _log(f"✗ {transport_label}: {refusal}")
    return AgentResult(
        success=False,
        output=f"SANDBOX_CAPABILITY_PROFILE_UNSUPPORTED: {refusal}",
        session_id=None,
        cost_usd=None,
        exit_code=-1,
        raw={},
        profile_name=profile.name,
        startup_failure=True,
    )


def _fallback_unavailable_reason(profile: ModelProfile) -> str:
    """Explain why no transport fallback could be attempted for ``profile``."""
    provider = profile.provider_family or profile.provider or "unknown"
    if profile.api_fallback is None and not profile.fallback_models:
        return f"no transport fallback configured for provider {provider!r}"
    return f"no configured fallback for provider {provider!r} resolved to an API transport"


@dataclass(frozen=True)
class _CliFallbackDecision:
    reason: str
    cli_quota_error_observed: bool


def _classify_cli_fallback_decision(result: AgentResult) -> _CliFallbackDecision | None:
    """Return structured fallback metadata when a CLI failure should retry via API."""
    if result.success:
        return None
    if result.startup_failure:
        return _CliFallbackDecision(reason="CLI unavailable", cli_quota_error_observed=False)
    if result.exit_code in _CLI_RETRY_EXIT_CODES:
        return _CliFallbackDecision(
            reason=f"CLI exited {result.exit_code}",
            cli_quota_error_observed=True,
        )
    output = result.output.lower()
    for pattern in _CLI_QUOTA_FALLBACK_PATTERNS:
        if pattern in output:
            return _CliFallbackDecision(
                reason=f"matched {pattern!r}",
                cli_quota_error_observed=True,
            )
    for pattern in _CLI_MODEL_FALLBACK_PATTERNS:
        if pattern in output:
            return _CliFallbackDecision(
                reason=f"matched {pattern!r}",
                cli_quota_error_observed=False,
            )
    return None


def _classify_cli_fallback(result: AgentResult) -> str | None:
    """Return a fallback reason when a CLI failure should retry via API."""
    decision = _classify_cli_fallback_decision(result)
    return decision.reason if decision is not None else None


def _build_api_fallback_profile(profile: ModelProfile) -> ModelProfile | None:
    """Build the API-transport fallback profile for a CLI profile, when configured.

    The transport is replaced explicitly: ``replace()`` would otherwise carry the
    CLI TransportSpec across and the "fallback" would dispatch straight back to
    the CLI that just failed.
    """
    fallback = profile.api_fallback
    if fallback is None:
        return None
    return replace(
        profile,
        cli=None,
        provider=fallback.provider,
        transport=fallback.transport(),
        model=fallback.model,
        timeout_seconds=fallback.timeout_seconds or profile.timeout_seconds,
        reasoning_effort=(
            fallback.reasoning_effort
            if fallback.reasoning_effort is not None
            else profile.reasoning_effort
        ),
        thinking_budget=(
            fallback.thinking_budget
            if fallback.thinking_budget is not None
            else profile.thinking_budget
        ),
        base_url=fallback.base_url if fallback.base_url is not None else profile.base_url,
        max_iterations=(
            fallback.max_iterations
            if fallback.max_iterations is not None
            else profile.max_iterations
        ),
        api_fallback=None,
    )


def _build_cli_fallback_api_profile(
    profile: ModelProfile,
    fallback_model: str,
) -> ModelProfile | None:
    """Build an API-transport profile for a ``fallback_models`` entry.

    The provider is the profile's own provider family — a transport fallback
    never changes provider. Returns None when the profile has no provider
    identity or the provider has no API adapter.

    The transport comes from :func:`model_fallback_transport`, which is the same
    function the load-time pricing enumeration reads, so what gets dispatched
    here and what gets priced and reported at load cannot disagree (#2335).
    """
    provider = profile.provider_family
    transport = model_fallback_transport(provider)
    if transport is None:
        return None
    return replace(
        profile,
        cli=None,
        provider=provider,
        transport=transport,
        model=fallback_model,
        fallback_models=(),
        api_fallback=None,
    )


def _maybe_run_api_fallback(
    *,
    result: AgentResult,
    prompt: str,
    profile: ModelProfile,
    api_fallback_profile: ModelProfile | None,
    working_dir: Path,
    session_id: str | None,
    quiet: bool,
    secrets: dict[str, str] | None,
    plain_text: bool,
) -> AgentResult:
    """Retry a retryable CLI failure via API when fallback is safe.

    Tries, in order:
    1. The legacy api_fallback profile (TransportFallbackConfig), if configured.
    2. Each entry in profile.fallback_models that resolves to an API model.

    Only quota-exhaustion/capacity, model-not-found, and fail-closed API
    capability mismatches trigger fallback.
    When a resumed CLI session hits one of those failures, the retry crosses
    transports and starts a fresh API request with the same prompt context.
    That preserves forward progress within the same dev iteration, but session
    continuity is intentionally lost because CLI sessions cannot resume via API.

    model_config is attached to the returned result whenever profile.fallback_models
    is non-empty, regardless of which path fires. model_used is set to the model that
    actually ran (the CLI model for non-fallback paths, the API model for fallback paths).
    """
    # Compute model_config before any early return so it is attached on all paths.
    # Non-empty only when fallback_models is configured (used to annotate the result
    # so the audit trail knows a preference list was in play).
    model_config = profile.models if profile.fallback_models else ()
    cli_label = profile.name or profile.model

    decision = _classify_cli_fallback_decision(result)
    if decision is None:
        # CLI succeeded or non-retryable failure — no fallback needed.
        return replace(
            result,
            model_config=model_config,
            model_used=result.model_used or profile.model,
            transport_used=result.transport_used or "cli",
        )

    reason = decision.reason

    def _annotate_cli_result(
        cli_result: AgentResult, not_applied: str | None = None
    ) -> AgentResult:
        """Annotate a failure for which no fallback was ever attempted.

        The failure was classified as fallback-eligible, so recording only the
        reason would describe a decision without its outcome. Say why nothing
        was attempted, and — for a quota refusal that named its own reset time —
        carry that time forward so the coordinator can tell a failure certain to
        repeat from one merely likely to (#2298).
        """
        not_applied = not_applied or _fallback_unavailable_reason(profile)
        reset_at = (
            _parse_quota_reset(cli_result.output or "")
            if decision.cli_quota_error_observed
            else None
        )
        _log(
            f"  ⚠ {cli_label} CLI failed ({reason}); no transport fallback applied "
            f"— {not_applied}" + (f"; provider stated reset at {reset_at}" if reset_at else "")
        )
        return replace(
            cli_result,
            model_config=model_config,
            model_used=cli_result.model_used or profile.model,
            cli_quota_error_observed=decision.cli_quota_error_observed,
            transport_fallback_fired=False,
            transport_fallback_reason=reason,
            transport_fallback_not_applied_reason=not_applied,
            provider_quota_reset_at=reset_at,
            transport_used="cli",
        )

    def _annotate_api_result(api_result: AgentResult, model_used: str) -> AgentResult:
        return replace(
            api_result,
            model_config=model_config,
            model_used=api_result.model_used or model_used,
            cli_quota_error_observed=decision.cli_quota_error_observed,
            transport_fallback_fired=True,
            transport_fallback_reason=reason,
            transport_used="api",
        )

    # A CLI that declared sandbox capabilities must not fall back to a transport
    # that cannot express them — that would turn a fail-closed capability
    # refusal into a quiet run without the capability (#2038).
    capability_refusal = capability_transport_refusal(profile, "api")
    if capability_refusal is not None:
        return _annotate_cli_result(result, not_applied=capability_refusal)

    if session_id is not None:
        _log(
            f"  ⚠ {cli_label} CLI failed ({reason}) while resuming {session_id}; "
            "retrying via a fresh API request and dropping CLI session continuity"
        )

    from theforge.runners import api as runner_api  # noqa: PLC0415
    from theforge.runners.api import _classify_api_model_fallback  # noqa: PLC0415

    # Track the last API attempt result so we can surface it (not the original CLI error)
    # when all fallback paths fail.
    last_fb_result: AgentResult | None = None
    last_fb_model: str | None = None

    # --- Legacy api_fallback (TransportFallbackConfig) ---
    if api_fallback_profile is not None:
        _log(
            f"  ⚠ {cli_label} CLI failed ({reason}); "
            f"retrying via {api_fallback_profile.provider}/{api_fallback_profile.model}"
        )
        fallback_result = runner_api.run_api_agent(
            prompt=prompt,
            profile=api_fallback_profile,
            working_dir=working_dir,
            quiet=quiet,
            secrets=secrets or {},
            plain_text=plain_text,
        )
        if fallback_result.success:
            return _annotate_api_result(fallback_result, api_fallback_profile.model)
        # api_fallback failed — record it as the best result so far, then fall through
        # to fallback_models. If there are no fallback_models, last_fb_result ensures we
        # return the API failure rather than the original CLI failure.
        last_fb_result = _annotate_api_result(fallback_result, api_fallback_profile.model)
        last_fb_model = api_fallback_profile.model

    # --- New fallback_models list ---

    for fallback_model in profile.fallback_models:
        # Bare CLI names in fallback_models are treated as API (ambiguous → API per spec)
        api_profile = _build_cli_fallback_api_profile(profile, fallback_model)
        if api_profile is None:
            _log(
                f"  ⚠ {cli_label} could not build API fallback "
                f"for model {fallback_model!r} — skipping"
            )
            continue
        _log(
            f"  ⚠ {cli_label} CLI failed ({reason}); retrying via API model {fallback_model!r}..."
        )
        fb_result = runner_api.run_api_agent(
            prompt=prompt,
            profile=api_profile,
            working_dir=working_dir,
            quiet=quiet,
            secrets=secrets or {},
            plain_text=plain_text,
        )
        if fb_result.success:
            _log(f"  ✓ {cli_label} fallback to {fallback_model!r} succeeded")
            return _annotate_api_result(fb_result, fallback_model)

        if not _classify_api_model_fallback(fb_result):
            # Non-fallback error; stop iterating and surface this result
            return _annotate_api_result(fb_result, fallback_model)

        last_fb_result = _annotate_api_result(fb_result, fallback_model)
        last_fb_model = fallback_model
        _log(f"  ⚠ {cli_label} API fallback {fallback_model!r} also failed")

    # Return the last API attempt result (legacy fallback or exhausted fallback_models),
    # so operators see the final API failure rather than the original CLI error.
    if last_fb_result is not None:
        return replace(last_fb_result, model_config=model_config, model_used=last_fb_model)

    # No API fallback was available or attempted (no api_fallback_profile, no fallback_models).
    # Return the original CLI result — there is nothing else to surface.
    return _annotate_cli_result(result)


# ── Runner dispatch ───────────────────────────────────────────────────


def _profile_transport_kind(profile: ModelProfile) -> str:
    """Return the TransportSpec.kind for a ModelProfile: 'cli' or 'api'.

    TransportSpec is the sole source of truth for runner dispatch. Profile
    construction normalizes the raw cli/provider spelling into a transport once,
    so this read is always authoritative and nothing here re-derives dispatch
    from those fields.
    """
    return profile.mode


def _profile_cli_runner(profile: ModelProfile) -> str | None:
    """Return the CLI runner key for dispatch (e.g. 'claude', 'codex', 'gemini')."""
    if profile.transport is not None and profile.transport.kind == "cli":
        return profile.transport.runner
    return None


def _fill_invocation_identity(result: AgentResult, profile: ModelProfile) -> AgentResult:
    """Back-fill the identity fields a runner did not report for itself.

    ``model_used`` alone is not an identity: a bare model name the catalog
    offers over both transports (``gpt-5.4``) cannot be canonicalized without
    knowing which one ran, and indexes under its own spelling instead (#2225).
    So the transport is filled from the profile alongside the model whenever a
    runner left it empty.

    Both fields are only ever filled, never overwritten — a runner that
    switched transport mid-invocation (the CLI→API fallback) has already
    recorded the transport that actually served.

    The *configured* half of the ledger is stamped alongside (#2205): the two
    are one contract, and filling the resolved identity without also naming what
    was configured is how the collapse this replaced happened.
    """
    if result.model_used is None or not result.transport_used:
        result = replace(
            result,
            model_used=result.model_used if result.model_used is not None else profile.model,
            transport_used=result.transport_used or _profile_transport_kind(profile),
        )
    return _stamp_configured_identity(result, profile)


def _stamp_configured_identity(result: AgentResult, profile: ModelProfile) -> AgentResult:
    """Record what the invocation was *configured* as, beside what resolved (#2205).

    ``model_used``/``transport_used`` are the resolved primary identity — the
    concrete model that served. They are not the identity the operator selected:
    an alias resolves, a preference list falls back, a CLI failure crosses to the
    API. Recording only the survivor forces a choice among three different facts
    and discards the rest, which is exactly what leaves cost and outcome attached
    to something nobody configured.

    So the configured spelling is stamped verbatim from the profile this dispatch
    started from, before any of those rewrites. Only ever filled, never
    overwritten: a runner that already knows its own configured identity (a
    fallback attempt that carries the *original* profile's spelling) has the more
    specific reading.

    ``cost_provenance`` is normalized here too: an unmeasured cost cannot have
    been reported *or* estimated, so ``cost_usd is None`` always pairs with
    :data:`COST_UNKNOWN` no matter what a runner stamped.
    """
    if not isinstance(result, AgentResult):
        return result
    changes: dict[str, object] = {}
    if result.configured_model is None:
        changes["configured_model"] = profile.model
    if result.configured_transport is None:
        changes["configured_transport"] = _profile_transport_kind(profile)
    if result.reasoning_effort is None and profile.reasoning_effort is not None:
        changes["reasoning_effort"] = profile.reasoning_effort
    if result.cost_usd is None and result.cost_provenance != COST_UNKNOWN:
        changes["cost_provenance"] = COST_UNKNOWN
    if not changes:
        return result
    return replace(result, **changes)  # type: ignore[arg-type]


def run_agent(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
    fallback_to_file: bool = True,
    quiet: bool = False,
    is_pool: bool = False,
    secrets: dict[str, str] | None = None,
    plain_text: bool = False,
    stop_event: "threading.Event | None" = None,
    progress_cb: "Callable[[dict], None] | None" = None,
) -> AgentResult:
    """Run an agent using the transport specified in its profile.

    Every dispatch path returns through :func:`_stamp_configured_identity`, so
    the configured-vs-resolved pair of the invocation ledger (#2205) is recorded
    once at this seam rather than at each of the six transport returns below.

    Dispatches on TransportSpec.kind ('cli' vs 'api'), not on provider string.
    Provider-specific behavior lives inside adapters.
    Prompt is passed via stdin to CLI runners to avoid shell escaping issues.
    When quiet=True the per-agent 'Starting...' log is suppressed
    (used by run_agent_pool which emits a pool-level banner instead).
    When is_pool=True the runner will not attempt session-ID extraction
    strategies that are unsafe for concurrent invocations (e.g. scanning
    a global index file). Claude is unaffected — it extracts the ID from
    its own stdout stream. Codex and Gemini are affected.
    """
    if _profile_transport_kind(profile) == "api":
        # The API tool runtime confines writes by path check, not by host
        # sandbox, so it has no axis for a declared write root or mach service.
        # Refuse before dispatch rather than run with the capability absent.
        refusal = capability_transport_refusal(profile, "api")
        if refusal is not None:
            return _stamp_configured_identity(
                _capability_refusal_result(profile, "api", refusal), profile
            )
        from theforge.runners import api as runner_api  # noqa: PLC0415

        api_result = runner_api.run_api_agent(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            quiet=quiet,
            secrets=secrets or {},
            plain_text=plain_text,
            progress_cb=progress_cb,
        )
        return _fill_invocation_identity(api_result, profile)

    cli = _profile_cli_runner(profile)
    api_fallback_profile = _build_api_fallback_profile(profile)

    if cli == "claude":
        from .runner_claude import _run_claude  # noqa: PLC0415

        result = _run_claude(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            session_id=session_id,
            fallback_to_file=fallback_to_file,
            quiet=quiet,
            secrets=secrets,
            stop_event=stop_event,
        )
        result = _maybe_run_api_fallback(
            result=result,
            prompt=prompt,
            profile=profile,
            api_fallback_profile=api_fallback_profile,
            working_dir=working_dir,
            session_id=session_id,
            quiet=quiet,
            secrets=secrets,
            plain_text=plain_text,
        )
        return _fill_invocation_identity(result, profile)

    if cli == "codex":
        # Codex brings its own `--sandbox` containment and never goes through
        # workspace_effect_sandbox_command, so a declared grant would be
        # silently dropped. Refuse instead (#2038).
        refusal = capability_transport_refusal(profile, "codex")
        if refusal is not None:
            return _stamp_configured_identity(
                _capability_refusal_result(profile, "codex", refusal), profile
            )
        from .runner_codex import _run_codex  # noqa: PLC0415

        result = _run_codex(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            session_id=session_id,
            quiet=quiet,
            is_pool=is_pool,
            secrets=secrets,
        )
        result = _maybe_run_api_fallback(
            result=result,
            prompt=prompt,
            profile=profile,
            api_fallback_profile=api_fallback_profile,
            working_dir=working_dir,
            session_id=session_id,
            quiet=quiet,
            secrets=secrets,
            plain_text=plain_text,
        )
        return _fill_invocation_identity(result, profile)

    if cli == "gemini":
        from .runner_gemini import _run_gemini  # noqa: PLC0415

        result = _run_gemini(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            session_id=session_id,
            quiet=quiet,
            is_pool=is_pool,
            secrets=secrets,
        )
        result = _maybe_run_api_fallback(
            result=result,
            prompt=prompt,
            profile=profile,
            api_fallback_profile=api_fallback_profile,
            working_dir=working_dir,
            session_id=session_id,
            quiet=quiet,
            secrets=secrets,
            plain_text=plain_text,
        )
        return _fill_invocation_identity(result, profile)

    if cli == "ghaw":
        # gh-aw runs the work on a GitHub Actions runner, not under this host's
        # sandbox, so a host write root or mach service cannot be granted there.
        refusal = capability_transport_refusal(profile, "ghaw")
        if refusal is not None:
            return _stamp_configured_identity(
                _capability_refusal_result(profile, "ghaw", refusal), profile
            )
        from .runner_ghaw import _run_ghaw  # noqa: PLC0415

        # No API fallback on this transport: gh-aw failures (dispatch, Actions
        # queueing, engine auth on the runner) are not provider-quota signals,
        # and re-running the dev phase locally would silently swap substrates.
        result = _run_ghaw(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            session_id=session_id,
            quiet=quiet,
            is_pool=is_pool,
            secrets=secrets,
        )
        return _fill_invocation_identity(result, profile)

    # No CLI runner resolved. When the profile has no transport at all its raw
    # ``cli`` never normalized to one — name that unresolved value so the
    # operator sees what they wrote, not a bare None.
    unresolved = cli if cli is not None else profile.cli
    # No resolved identity to record — nothing was invoked — but the configured
    # one is exactly what the operator needs to see for an unresolvable profile.
    return _stamp_configured_identity(
        AgentResult(
            success=False,
            output=(
                f"Unknown CLI: {unresolved!r}. Supported: ['claude', 'codex', 'gemini', 'ghaw']"
            ),
            session_id=None,
            cost_usd=None,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
            startup_failure=True,
        ),
        profile,
    )


def run_agent_pool(
    *,
    prompt: str | list[str],
    profiles: list[ModelProfile],
    working_dir: Path,
    session_ids: list[str | None] | None = None,
    secrets: dict[str, str] | None = None,
    plain_text: bool = False,
    stop_event: "threading.Event | None" = None,
    progress_cb: "Callable[[dict], None] | None" = None,
    durations_out: "list[float] | None" = None,
) -> list[AgentResult]:
    """Run multiple agents concurrently, each with its own prompt or a shared prompt.

    When prompt is a list, each agent gets its corresponding prompt (length must
    equal profiles length). When prompt is a string, all agents share it.

    Returns results in the same order as the input profiles list.
    Uses ThreadPoolExecutor for parallel execution; single-agent pools
    run directly without thread overhead. Each agent runs independently
    with no shared context.

    When ``durations_out`` is provided, it is populated (via slice assignment) with
    the per-agent wall-clock seconds, index-aligned to ``profiles`` — the pool
    already measures these internally, so this exposes them without changing the
    return type. Callers that don't pass it are unaffected (#1443, per-plan-reviewer
    latency capture).
    """
    prompts: list[str] = [prompt] * len(profiles) if isinstance(prompt, str) else prompt
    if session_ids is not None:
        assert len(session_ids) == len(profiles), "session_ids must match profiles length"

    def _emit_progress(event: dict) -> None:
        """Fire the passive progress callback; a raising cb never breaks the pool."""
        if progress_cb is None:
            return
        try:
            progress_cb(event)
        except Exception:
            pass

    if len(profiles) == 1:
        sid = session_ids[0] if session_ids else None
        only = profiles[0]
        _single_start = time.monotonic()
        result = run_agent(
            prompt=prompts[0],
            profile=only,
            working_dir=working_dir,
            session_id=sid,
            fallback_to_file=False,
            secrets=secrets,
            plain_text=plain_text,
            stop_event=stop_event,
            progress_cb=progress_cb,
        )
        if durations_out is not None:
            durations_out[:] = [time.monotonic() - _single_start]
        only_label = only.name or only.identity_label
        # Only a successful result means the reviewer is done. An unsuccessful
        # (e.g. transient transport) result is a finished attempt the coordinator
        # may still retry — marking it done would count/label it wrongly while
        # the retry loop is running.
        if getattr(result, "success", False):
            _emit_progress({"label": only_label, "done": True})
        return [result]

    names = ", ".join(p.name or p.identity_label for p in profiles)
    _log(f"  Starting review pool: {names} (parallel)")

    pool_start = time.monotonic()
    results: list[AgentResult | None] = [None] * len(profiles)
    agent_durations: list[float] = [0.0] * len(profiles)

    def _timed_agent(idx: int, profile: ModelProfile) -> AgentResult:
        t0 = time.monotonic()
        try:
            sid = session_ids[idx] if session_ids else None
            return run_agent(
                prompt=prompts[idx],
                profile=profile,
                working_dir=working_dir,
                session_id=sid,
                fallback_to_file=False,
                quiet=True,
                is_pool=True,
                secrets=secrets,
                plain_text=plain_text,
                stop_event=stop_event,
                progress_cb=progress_cb,
            )
        finally:
            agent_durations[idx] = time.monotonic() - t0

    with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
        futures = {pool.submit(_timed_agent, i, p): i for i, p in enumerate(profiles)}
        for future in as_completed(futures):
            idx = futures[future]
            profile = profiles[idx]
            label = profile.name or profile.identity_label
            duration = agent_durations[idx]
            try:
                results[idx] = future.result()
                _log(f"  ... {label} done ({duration:.0f}s)")
                # Only mark the reviewer done when the attempt actually
                # succeeded. A returned-but-unsuccessful result (transient
                # transport failure) is a finished attempt the coordinator may
                # still retry; counting it as done would falsely report it in the
                # pool-done progress while the retry loop is still running.
                if getattr(results[idx], "success", False):
                    _emit_progress({"label": label, "done": True})
            except Exception as exc:
                _log(f"  ... {label} failed ({duration:.0f}s): {exc}")
                results[idx] = AgentResult(
                    success=False,
                    output=f"ERROR: {exc}",
                    session_id=None,
                    cost_usd=None,
                    exit_code=-1,
                    raw={},
                    profile_name=profile.name,
                )

    wall_clock = time.monotonic() - pool_start
    sequential_est = sum(agent_durations)
    _log(
        f"  Review pool complete: {wall_clock:.0f}s wall clock ({sequential_est:.0f}s sequential)"
    )
    if durations_out is not None:
        durations_out[:] = list(agent_durations)
    assert all(r is not None for r in results), "BUG: pool finished with unfilled result slots"
    return cast(list[AgentResult], results)


def _killed_before_output(result: AgentResult) -> bool:
    """True when the runner classified this result as an invocation that never ran."""
    return getattr(result, "failure_code", None) == FAILURE_KILLED_BEFORE_OUTPUT


def log_agent_result(result: AgentResult, role: str) -> None:
    """Print a summary of an agent result to stderr (verbose-only)."""
    status = "OK" if result.success else "FAIL"
    # An invocation that never ran says so on the line an operator actually
    # reads (#2832). Without it, `exit=-9 | cost=$0.000 | output=0 chars` is
    # what a killed-before-anything invocation and an agent that worked and
    # went quiet both print, and the three occurrences in that issue were read
    # as agent failures for exactly that reason.
    _never_ran = " | never ran (killed before any output)" if _killed_before_output(result) else ""
    _log_verbose(
        f"  [{role}] {status} | exit={result.exit_code} | "
        f"cost={'${:.3f}'.format(result.cost_usd) if result.cost_usd is not None else 'unknown'} |"
        f" "
        f"output={len(result.output)} chars{_never_ran}"
    )
    if not result.success and result.output:
        preview = result.output[:300].replace("\n", " ").strip()
        _log_verbose(f"  [{role}] error output: {preview}")
