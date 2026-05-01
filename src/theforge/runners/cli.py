"""CLI subprocess wrapper for invoking LLM agents.

Dispatches to the appropriate CLI based on ModelProfile.cli.
Provider-specific runners live in dedicated modules:
  - runner_claude.py  — Claude Code CLI
  - runner_codex.py   — OpenAI Codex CLI
  - runner_gemini.py  — Google Gemini CLI
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from theforge.agent_types import AgentResult
from theforge.log_level import LogLevel
from theforge.log_util import _log_line

from ..config import ModelProfile

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


def _handle_exception(
    exc: BaseException,
    *,
    profile: ModelProfile,
    cli_name: str,
) -> AgentResult | None:
    """Handle common subprocess exceptions. Returns AgentResult or None to re-raise."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return AgentResult(
            success=False,
            output=f"TIMEOUT: Agent exceeded {profile.timeout_seconds}s limit",
            session_id=None,
            cost_usd=None,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
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
_CLI_FALLBACK_PATTERNS = (
    "429",
    "quota",
    "rate limit",
    "resource exhausted",
    "resource_exhausted",
    "service unavailable",
    "temporarily unavailable",
    "unavailable",
    "overloaded",
    "try again later",
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
_KNOWN_CLI_NAMES = frozenset({"claude", "codex", "gemini"})


def _classify_cli_fallback(result: AgentResult) -> str | None:
    """Return a fallback reason when a CLI failure should retry via API."""
    if result.success:
        return None
    if result.startup_failure:
        return "CLI unavailable"
    if result.exit_code in _CLI_RETRY_EXIT_CODES:
        return f"CLI exited {result.exit_code}"
    output = result.output.lower()
    for pattern in _CLI_FALLBACK_PATTERNS:
        if pattern in output:
            return f"matched {pattern!r}"
    return None


def _build_api_fallback_profile(profile: ModelProfile) -> ModelProfile | None:
    """Build an API fallback profile for a CLI profile when configured."""
    fallback = profile.api_fallback
    if fallback is None:
        return None
    return replace(
        profile,
        cli=None,
        provider=fallback.provider,
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


_CLI_TO_PROVIDER: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
}


def _build_cli_fallback_api_profile(
    profile: ModelProfile,
    fallback_model: str,
) -> ModelProfile | None:
    """Build an API profile for a fallback_models entry on a CLI profile.

    The provider is inferred from the CLI binary name. Returns None if the
    provider cannot be determined.
    """
    provider = _CLI_TO_PROVIDER.get(profile.cli or "")
    if not provider:
        return None
    return replace(
        profile,
        cli=None,
        provider=provider,
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
    1. The legacy api_fallback profile (ApiFallbackConfig), if configured.
    2. Each entry in profile.fallback_models that resolves to an API model.

    Only quota-exhaustion and model-not-found errors trigger fallback.
    Session resumption skips API fallback entirely (can't resume across transports).

    model_config is attached to the returned result whenever profile.fallback_models
    is non-empty, regardless of which path fires. model_used is set to the model that
    actually ran (the CLI model for non-fallback paths, the API model for fallback paths).
    """
    # Compute model_config before any early return so it is attached on all paths.
    # Non-empty only when fallback_models is configured (used to annotate the result
    # so the audit trail knows a preference list was in play).
    model_config = profile.models if profile.fallback_models else ()
    cli_label = profile.name or profile.model

    reason = _classify_cli_fallback(result)
    if reason is None:
        # CLI succeeded or non-retryable failure — no fallback needed.
        if model_config:
            return replace(result, model_config=model_config, model_used=profile.model)
        return result

    if session_id is not None:
        _log(
            f"  ⚠ {cli_label} CLI failed ({reason}), "
            "but API fallback was skipped for a resumed session"
        )
        if model_config:
            return replace(result, model_config=model_config, model_used=profile.model)
        return result

    from theforge.runners import api as runner_api  # noqa: PLC0415
    from theforge.runners.api import _classify_api_model_fallback  # noqa: PLC0415

    # Track the last API attempt result so we can surface it (not the original CLI error)
    # when all fallback paths fail.
    last_fb_result: AgentResult | None = None
    last_fb_model: str | None = None

    # --- Legacy api_fallback (ApiFallbackConfig) ---
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
            # Preserve model_used from the API result if set; otherwise use api_fallback model.
            if fallback_result.model_used is None:
                return replace(fallback_result, model_used=api_fallback_profile.model)
            return fallback_result
        # api_fallback failed — record it as the best result so far, then fall through
        # to fallback_models. If there are no fallback_models, last_fb_result ensures we
        # return the API failure rather than the original CLI failure.
        last_fb_result = fallback_result
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
            return replace(fb_result, model_config=model_config, model_used=fallback_model)

        if not _classify_api_model_fallback(fb_result):
            # Non-fallback error; stop iterating and surface this result
            return replace(fb_result, model_config=model_config, model_used=fallback_model)

        last_fb_result = fb_result
        last_fb_model = fallback_model
        _log(f"  ⚠ {cli_label} API fallback {fallback_model!r} also failed")

    # Return the last API attempt result (legacy fallback or exhausted fallback_models),
    # so operators see the final API failure rather than the original CLI error.
    if last_fb_result is not None:
        return replace(last_fb_result, model_config=model_config, model_used=last_fb_model)

    # No API fallback was available or attempted (no api_fallback_profile, no fallback_models).
    # Return the original CLI result — there is nothing else to surface.
    if model_config:
        return replace(result, model_config=model_config, model_used=profile.model)
    return result


# ── Runner dispatch ───────────────────────────────────────────────────


def _profile_transport_kind(profile: ModelProfile) -> str:
    """Return the TransportSpec.kind for a ModelProfile: 'cli' or 'api'.

    TransportSpec is the sole source of truth for runner dispatch. Profile
    construction auto-populates transport from cli/provider when not set
    explicitly, so this read is always authoritative.
    """
    if profile.transport is not None:
        return profile.transport.kind
    return "api" if profile.provider else "cli"


def _profile_cli_runner(profile: ModelProfile) -> str | None:
    """Return the CLI runner key for dispatch (e.g. 'claude', 'codex', 'gemini')."""
    if profile.transport is not None and profile.transport.kind == "cli":
        return profile.transport.runner
    return profile.cli


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
) -> AgentResult:
    """Run an agent using the transport specified in its profile.

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
        from theforge.runners import api as runner_api  # noqa: PLC0415

        return runner_api.run_api_agent(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            quiet=quiet,
            secrets=secrets or {},
            plain_text=plain_text,
        )

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
        if result.model_used is None:
            result = replace(result, model_used=profile.model)
        return result

    if cli == "codex":
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
        if result.model_used is None:
            result = replace(result, model_used=profile.model)
        return result

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
        if result.model_used is None:
            result = replace(result, model_used=profile.model)
        return result

    return AgentResult(
        success=False,
        output=f"Unknown CLI: {cli!r}. Supported: ['claude', 'codex', 'gemini']",
        session_id=None,
        cost_usd=None,
        exit_code=-1,
        raw={},
        profile_name=profile.name,
        startup_failure=True,
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
) -> list[AgentResult]:
    """Run multiple agents concurrently, each with its own prompt or a shared prompt.

    When prompt is a list, each agent gets its corresponding prompt (length must
    equal profiles length). When prompt is a string, all agents share it.

    Returns results in the same order as the input profiles list.
    Uses ThreadPoolExecutor for parallel execution; single-agent pools
    run directly without thread overhead. Each agent runs independently
    with no shared context.
    """
    prompts: list[str] = [prompt] * len(profiles) if isinstance(prompt, str) else prompt
    if session_ids is not None:
        assert len(session_ids) == len(profiles), "session_ids must match profiles length"

    if len(profiles) == 1:
        sid = session_ids[0] if session_ids else None
        return [
            run_agent(
                prompt=prompts[0],
                profile=profiles[0],
                working_dir=working_dir,
                session_id=sid,
                fallback_to_file=False,
                secrets=secrets,
                plain_text=plain_text,
                stop_event=stop_event,
            )
        ]

    names = ", ".join(p.name or f"{p.cli or p.provider}/{p.model}" for p in profiles)
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
            )
        finally:
            agent_durations[idx] = time.monotonic() - t0

    with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
        futures = {pool.submit(_timed_agent, i, p): i for i, p in enumerate(profiles)}
        for future in as_completed(futures):
            idx = futures[future]
            profile = profiles[idx]
            label = profile.name or f"{profile.cli or profile.provider}/{profile.model}"
            duration = agent_durations[idx]
            try:
                results[idx] = future.result()
                _log(f"  ... {label} done ({duration:.0f}s)")
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
    assert all(r is not None for r in results), "BUG: pool finished with unfilled result slots"
    return cast(list[AgentResult], results)


def log_agent_result(result: AgentResult, role: str) -> None:
    """Print a summary of an agent result to stderr (verbose-only)."""
    status = "OK" if result.success else "FAIL"
    _log_verbose(
        f"  [{role}] {status} | exit={result.exit_code} | "
        f"cost={'${:.3f}'.format(result.cost_usd) if result.cost_usd is not None else 'unknown'} |"
        f" "
        f"output={len(result.output)} chars"
    )
    if not result.success and result.output:
        preview = result.output[:300].replace("\n", " ").strip()
        _log_verbose(f"  [{role}] error output: {preview}")
