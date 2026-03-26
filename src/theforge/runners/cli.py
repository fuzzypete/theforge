"""CLI subprocess wrapper for invoking LLM agents.

Dispatches to the appropriate CLI based on ModelProfile.cli.
Provider-specific runners live in dedicated modules:
  - runner_claude.py  — Claude Code CLI
  - runner_codex.py   — OpenAI Codex CLI
  - runner_gemini.py  — Google Gemini CLI
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from theforge.agent_types import AgentResult, ModelUsage  # noqa: F401
from theforge.log_level import _LOG_LEVEL, LogLevel, set_log_level  # noqa: F401

from ..config import ModelProfile

# ── Logging helpers ───────────────────────────────────────────────────


def _log(msg: str) -> None:
    print(f"[forge] {msg}", file=sys.stderr, flush=True)


def _log_verbose(msg: str) -> None:
    from theforge.log_level import _LOG_LEVEL as _LL

    if _LL >= LogLevel.VERBOSE:
        print(f"[forge] {msg}", file=sys.stderr, flush=True)


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


# ── Runner dispatch ───────────────────────────────────────────────────


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
) -> AgentResult:
    """Run an agent using the transport specified in its profile.

    Dispatches to API or CLI runner based on profile.mode.
    Prompt is passed via stdin to CLI runners to avoid shell escaping issues.
    When quiet=True the per-agent 'Starting...' log is suppressed
    (used by run_agent_pool which emits a pool-level banner instead).
    When is_pool=True the runner will not attempt session-ID extraction
    strategies that are unsafe for concurrent invocations (e.g. scanning
    a global index file). Claude is unaffected — it extracts the ID from
    its own stdout stream. Codex and Gemini are affected.
    """
    if profile.mode == "api":
        from theforge.runners import api as runner_api  # noqa: PLC0415

        return runner_api.run_api_agent(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            quiet=quiet,
            secrets=secrets or {},
            plain_text=plain_text,
        )

    cli = profile.cli

    if cli == "claude":
        from .runner_claude import _run_claude  # noqa: PLC0415

        return _run_claude(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            session_id=session_id,
            fallback_to_file=fallback_to_file,
            quiet=quiet,
            secrets=secrets,
        )

    if cli == "codex":
        from .runner_codex import _run_codex  # noqa: PLC0415

        return _run_codex(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            session_id=session_id,
            quiet=quiet,
            is_pool=is_pool,
            secrets=secrets,
        )

    if cli == "gemini":
        from .runner_gemini import _run_gemini  # noqa: PLC0415

        return _run_gemini(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            session_id=session_id,
            quiet=quiet,
            is_pool=is_pool,
            secrets=secrets,
        )

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
