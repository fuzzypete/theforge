"""CLI subprocess wrapper for invoking LLM agents.

Dispatches to the appropriate CLI based on ModelProfile.cli.
Supports Claude Code, Codex (OpenAI), and Gemini (Google) CLIs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable

from .config import ModelProfile

# ── Log level ─────────────────────────────────────────────────────────


class LogLevel(IntEnum):
    PROGRESS = 0  # default: phase transitions, agent start/done, verdicts
    VERBOSE = 1  # adds tool activity, heartbeats, raw output


_LOG_LEVEL: LogLevel = LogLevel.PROGRESS


def set_log_level(level: LogLevel) -> None:
    global _LOG_LEVEL
    _LOG_LEVEL = level


def _log(msg: str) -> None:
    print(f"[forge] {msg}", file=sys.stderr, flush=True)


def _log_verbose(msg: str) -> None:
    if _LOG_LEVEL >= LogLevel.VERBOSE:
        print(f"[forge] {msg}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class ModelUsage:
    """Per-model token and cost breakdown from a single agent invocation."""

    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class AgentResult:
    """Structured result from an agent invocation."""

    success: bool  # subprocess returned 0
    output: str  # agent's text response
    session_id: str | None  # for --resume on follow-up
    cost_usd: float  # total invocation cost
    exit_code: int  # raw exit code
    raw: dict[str, Any]  # full parsed JSON (if available)
    profile_name: str = ""  # identifies which profile produced this result
    model_usage: tuple[ModelUsage, ...] = ()  # per-model breakdown (Claude only)


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
) -> tuple[_SubprocessOutcome, float]:
    """Run a subprocess in a background thread with 30s heartbeat.

    Returns (outcome, elapsed_seconds). The caller handles interpreting
    the outcome into an AgentResult.
    """
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


def _parse_model_usage(result_json: dict[str, Any]) -> tuple[ModelUsage, ...]:
    """Extract per-model usage breakdown from Claude CLI JSON output."""
    raw_usage = result_json.get("modelUsage", {})
    if not isinstance(raw_usage, dict):
        return ()
    usages = []
    for model_name, data in raw_usage.items():
        if not isinstance(data, dict):
            continue
        usages.append(
            ModelUsage(
                model=model_name,
                input_tokens=int(data.get("inputTokens", 0)),
                output_tokens=int(data.get("outputTokens", 0)),
                cache_read_tokens=int(data.get("cacheReadInputTokens", 0)),
                cache_creation_tokens=int(data.get("cacheCreationInputTokens", 0)),
                cost_usd=float(data.get("costUSD", 0.0)),
            )
        )
    return tuple(usages)


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
            cost_usd=0.0,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
        )
    if isinstance(exc, FileNotFoundError):
        return AgentResult(
            success=False,
            output=f"ERROR: '{cli_name}' CLI not found. Is it installed?",
            session_id=None,
            cost_usd=0.0,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
        )
    return None


# ── Runner dispatch ───────────────────────────────────────────────────


def run_agent(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
) -> AgentResult:
    """Run an agent using the CLI specified in profile.cli.

    Dispatches to the appropriate runner implementation.
    Prompt is passed via stdin to avoid shell escaping issues.
    """
    runners = {
        "claude": _run_claude,
        "codex": _run_codex,
        "gemini": _run_gemini,
    }

    runner_fn = runners.get(profile.cli)
    if runner_fn is None:
        return AgentResult(
            success=False,
            output=f"Unknown CLI: {profile.cli!r}. Supported: {list(runners.keys())}",
            session_id=None,
            cost_usd=0.0,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
        )

    return runner_fn(
        prompt=prompt,
        profile=profile,
        working_dir=working_dir,
        session_id=session_id,
    )


def run_agent_pool(
    *,
    prompt: str,
    profiles: list[ModelProfile],
    working_dir: Path,
) -> list[AgentResult]:
    """Run multiple agents concurrently with the same prompt.

    Returns results in the same order as the input profiles list.
    Uses ThreadPoolExecutor for parallel execution; single-agent pools
    run directly without thread overhead. Each agent runs independently
    with no shared context.
    """
    if len(profiles) == 1:
        return [run_agent(prompt=prompt, profile=profiles[0], working_dir=working_dir)]

    names = ", ".join(p.name or f"{p.cli}/{p.model}" for p in profiles)
    _log(f"  Starting review pool: {names} (parallel)")

    pool_start = time.monotonic()
    results: list[AgentResult | None] = [None] * len(profiles)
    agent_durations: list[float] = [0.0] * len(profiles)

    def _timed_agent(idx: int, profile: ModelProfile) -> AgentResult:
        t0 = time.monotonic()
        try:
            return run_agent(prompt=prompt, profile=profile, working_dir=working_dir)
        finally:
            agent_durations[idx] = time.monotonic() - t0

    with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
        futures = {pool.submit(_timed_agent, i, p): i for i, p in enumerate(profiles)}
        for future in as_completed(futures):
            idx = futures[future]
            profile = profiles[idx]
            label = profile.name or f"{profile.cli}/{profile.model}"
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
                    cost_usd=0.0,
                    exit_code=-1,
                    raw={},
                    profile_name=profile.name,
                )

    wall_clock = time.monotonic() - pool_start
    sequential_est = sum(agent_durations)
    _log(
        f"  Review pool complete: {wall_clock:.0f}s wall clock ({sequential_est:.0f}s sequential)"
    )
    return results  # type: ignore[return-value]


# ── Claude Code CLI ──────────────────────────────────────────────────


def _format_tool_input_preview(inp: dict[str, Any]) -> str:
    """Return a short preview string for a tool's input dict."""
    if not inp:
        return ""
    for v in inp.values():
        if isinstance(v, str):
            return v[:120]
    return str(inp)[:120]


def _process_stream_event(line: str, label: str) -> None:
    """Process a single JSONL stream event and print tool activity to stderr."""
    if not line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return

    event_type = event.get("type")

    label_tag = f" [{label}]" if label else ""
    if event_type == "tool_use_summary":
        summary = event.get("summary", "")
        if summary:
            _log_verbose(f"  ↳ {summary}{label_tag}")
    elif event_type == "assistant":
        message = event.get("message", {})
        content = message.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tool_name = item.get("name", "?")
                inp = item.get("input", {})
                preview = _format_tool_input_preview(inp)
                _log_verbose(f"  ↳ {tool_name}: {preview}{label_tag}")


def _run_claude(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
) -> AgentResult:
    """Invoke `claude -p --output-format stream-json --verbose` as a subprocess."""
    cmd: list[str] = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        profile.model,
    ]

    if profile.allowed_tools:
        cmd.extend(["--allowedTools", " ".join(profile.allowed_tools)])

    if session_id:
        cmd.extend(["--resume", session_id])

    # Unset CLAUDECODE so the subprocess isn't blocked by the nested-session check
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    label = profile.name or f"{profile.cli}/{profile.model}"
    _log(f"  Starting {label} (model={profile.model}, timeout={profile.timeout_seconds}s)...")

    start = time.monotonic()
    deadline = start + profile.timeout_seconds
    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(working_dir),
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(prompt)
        proc.stdin.close()

        lines: list[str] = []
        assert proc.stdout is not None

        # Enforce wall-clock timeout on the streaming loop via a watchdog thread.
        # proc.wait(timeout=...) only fires after stdout is drained, which never
        # happens if the agent streams indefinitely.
        def _watchdog() -> None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            if proc.poll() is None:
                proc.kill()

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()

        for line in proc.stdout:
            lines.append(line)
            _process_stream_event(line.strip(), label)
            if time.monotonic() > deadline:
                proc.kill()
                timed_out = True
                break

        proc.wait()
    except FileNotFoundError:
        return AgentResult(
            success=False,
            output="ERROR: 'claude' CLI not found. Is it installed?",
            session_id=None,
            cost_usd=0.0,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
        )

    if timed_out or (time.monotonic() - start) >= profile.timeout_seconds * 1.05:
        timed_out = True

    if timed_out:
        return AgentResult(
            success=False,
            output=f"TIMEOUT: Agent exceeded {profile.timeout_seconds}s limit",
            session_id=None,
            cost_usd=0.0,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
        )

    elapsed = time.monotonic() - start
    _log_verbose(f"  ... {label} done ({elapsed:.0f}s)")

    # Find the result line (type=result) in the JSONL stream
    result_json: dict[str, Any] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
            if event.get("type") == "result":
                result_json = event
                break
        except (json.JSONDecodeError, ValueError):
            continue

    if not result_json:
        raw_output = "".join(lines).strip()
        stderr_text = ""
        if proc.stderr:
            try:
                stderr_text = proc.stderr.read()
            except Exception:
                pass
        return AgentResult(
            success=proc.returncode == 0,
            output=raw_output or stderr_text or "(no output)",
            session_id=None,
            cost_usd=0.0,
            exit_code=proc.returncode,
            raw={},
            profile_name=profile.name,
        )

    try:
        cost = float(result_json.get("total_cost_usd", 0.0))
    except (TypeError, ValueError):
        cost = 0.0

    return AgentResult(
        success=proc.returncode == 0,
        output=result_json.get("result", "".join(lines)),
        session_id=result_json.get("session_id"),
        cost_usd=cost,
        exit_code=proc.returncode,
        raw=result_json,
        profile_name=profile.name,
        model_usage=_parse_model_usage(result_json),
    )


# ── Codex CLI ────────────────────────────────────────────────────────


def _run_codex(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
) -> AgentResult:
    """Invoke `npx @openai/codex exec --full-auto` as a subprocess.

    Output is captured via a temp file using `-o <file>`;
    falls back to stdout if the file is empty.
    """
    fd, output_path_str = tempfile.mkstemp(suffix=".txt", prefix="forge_codex_")
    os.close(fd)
    output_file = Path(output_path_str)

    cmd: list[str] = [
        "npx",
        "@openai/codex",
        "exec",
        "--full-auto",
        "-m",
        profile.model,
    ]
    if profile.reasoning_effort:
        cmd += ["-c", f'model_reasoning_effort="{profile.reasoning_effort}"']
    cmd += [
        "-C",
        str(working_dir),
        "-o",
        str(output_file),
        prompt,
    ]

    label = profile.name or f"{profile.cli}/{profile.model}"
    outcome, elapsed = _run_with_heartbeat(
        run_fn=lambda: subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=profile.timeout_seconds,
        ),
        label=label,
        profile=profile,
        cli_name="npx @openai/codex",
    )

    try:
        if outcome.exception:
            result = _handle_exception(
                outcome.exception, profile=profile, cli_name="npx @openai/codex"
            )
            if result:
                return result
            raise outcome.exception

        proc = outcome.proc
        assert proc is not None
        _log_verbose(f"  ... {label} done ({elapsed:.0f}s)")

        # Read output file; fall back to stdout then stderr
        output_text = ""
        try:
            content = output_file.read_text(encoding="utf-8").strip()
            if content:
                output_text = content
        except OSError:
            pass

        if not output_text:
            output_text = proc.stdout or proc.stderr or "(no output)"

        # Try JSON parse for structured response
        result_json: dict[str, Any] = {}
        try:
            result_json = json.loads(output_text)
        except (json.JSONDecodeError, ValueError):
            pass

        if result_json:
            return AgentResult(
                success=proc.returncode == 0,
                output=result_json.get("result", output_text),
                session_id=None,
                cost_usd=0.0,
                exit_code=proc.returncode,
                raw=result_json,
                profile_name=profile.name,
            )

        return AgentResult(
            success=proc.returncode == 0,
            output=output_text,
            session_id=None,
            cost_usd=0.0,
            exit_code=proc.returncode,
            raw={},
            profile_name=profile.name,
        )
    finally:
        try:
            output_file.unlink(missing_ok=True)
        except OSError:
            pass


# ── Gemini CLI ───────────────────────────────────────────────────────


def _run_gemini(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
) -> AgentResult:
    """Invoke `npx @google/gemini-cli -p <prompt> --yolo -m <model> -o json` as a subprocess."""
    cmd: list[str] = [
        "npx",
        "@google/gemini-cli",
        "-p",
        prompt,
        "--yolo",
        "-m",
        profile.model,
        "-o",
        "json",
    ]

    label = profile.name or f"{profile.cli}/{profile.model}"
    outcome, elapsed = _run_with_heartbeat(
        run_fn=lambda: subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(working_dir),
            timeout=profile.timeout_seconds,
        ),
        label=label,
        profile=profile,
        cli_name="gemini",
    )

    if outcome.exception:
        result = _handle_exception(outcome.exception, profile=profile, cli_name="gemini")
        if result:
            return result
        raise outcome.exception

    proc = outcome.proc
    assert proc is not None
    _log_verbose(f"  ... {label} done ({elapsed:.0f}s)")

    # Parse JSON output (-o json requests structured response)
    result_json: dict[str, Any] = {}
    try:
        result_json = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return AgentResult(
            success=proc.returncode == 0,
            output=proc.stdout or proc.stderr or "(no output)",
            session_id=None,
            cost_usd=0.0,
            exit_code=proc.returncode,
            raw={},
            profile_name=profile.name,
        )

    return AgentResult(
        success=proc.returncode == 0,
        output=result_json.get("response", result_json.get("result", proc.stdout)),
        session_id=None,
        cost_usd=0.0,
        exit_code=proc.returncode,
        raw=result_json,
        profile_name=profile.name,
    )


def log_agent_result(result: AgentResult, role: str) -> None:
    """Print a summary of an agent result to stderr (verbose-only)."""
    status = "OK" if result.success else "FAIL"
    _log_verbose(
        f"  [{role}] {status} | exit={result.exit_code} | "
        f"cost=${result.cost_usd:.3f} | "
        f"output={len(result.output)} chars"
    )
