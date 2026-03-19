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
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, cast

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
    cost_usd: float | None


@dataclass(frozen=True)
class AgentResult:
    """Structured result from an agent invocation."""

    success: bool  # subprocess returned 0
    output: str  # agent's text response
    session_id: str | None  # for --resume on follow-up
    cost_usd: float | None  # total invocation cost
    exit_code: int  # raw exit code
    raw: dict[str, Any]  # full parsed JSON (if available)
    profile_name: str = ""  # identifies which profile produced this result
    model_usage: tuple[ModelUsage, ...] = ()  # per-model breakdown (Claude only)
    structured_data: dict | None = None  # NEW: parsed JSON for API reviewers


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
        from . import runner_api

        return runner_api.run_api_agent(
            prompt=prompt,
            profile=profile,
            working_dir=working_dir,
            quiet=quiet,
            secrets=secrets or {},
        )

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
            cost_usd=None,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
        )

    runner_kwargs: dict[str, Any] = {
        "prompt": prompt,
        "profile": profile,
        "working_dir": working_dir,
        "session_id": session_id,
        "quiet": quiet,
        "secrets": secrets or {},
    }
    if profile.cli == "claude":
        runner_kwargs["fallback_to_file"] = fallback_to_file
    if profile.cli in ("codex", "gemini"):
        runner_kwargs["is_pool"] = is_pool
    return runner_fn(**runner_kwargs)


def run_agent_pool(
    *,
    prompt: str | list[str],
    profiles: list[ModelProfile],
    working_dir: Path,
    session_ids: list[str | None] | None = None,
    secrets: dict[str, str] | None = None,
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


# ── Claude Code CLI ──────────────────────────────────────────────────


def _format_tool_input_preview(inp: dict[str, Any]) -> str:
    """Return a short preview string for a tool's input dict."""
    if not inp:
        return ""
    for v in inp.values():
        if isinstance(v, str):
            return v[:120]
    return str(inp)[:120]


def _process_stream_event(line: str, label: str = "", *, label_prefix: str = "") -> None:
    """Process a single JSONL stream event and print tool activity to stderr.

    label: accepted for API compatibility but not used for formatting.
    label_prefix: if non-empty, prepended to tool activity lines
        (e.g. "[reviewer-a] "). Callers set this only in parallel pool mode.
    """
    if not line:
        return
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return

    event_type = event.get("type")

    if event_type == "tool_use_summary":
        summary = event.get("summary", "")
        if summary:
            _log_verbose(f"  ↳ {label_prefix}{summary}")
    elif event_type == "assistant":
        message = event.get("message", {})
        content = message.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tool_name = item.get("name", "?")
                inp = item.get("input", {})
                preview = _format_tool_input_preview(inp)
                _log_verbose(f"  ↳ {label_prefix}{tool_name}: {preview}")


def _get_claude_session_id(
    output: str,
    cwd: Path,
    *,
    fallback_to_file: bool = True,
    min_mtime: float | None = None,
) -> str | None:
    """Extract a Claude session id from stream output or transcript files."""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            return sid

    if not fallback_to_file:
        return None

    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.is_dir():
        return None

    try:
        project_slug = str(cwd.resolve()).replace("/", "-")
        project_dir = claude_projects / project_slug
        if not project_dir.is_dir():
            return None

        candidates = []
        for path in project_dir.glob("*.jsonl"):
            mtime = path.stat().st_mtime
            if min_mtime is not None and mtime <= min_mtime:
                continue
            candidates.append((mtime, path))
    except OSError:
        return None

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1].stem


def _run_claude(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
    fallback_to_file: bool = True,
    quiet: bool = False,
    secrets: dict[str, str] | None = None,
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
    env = {**os.environ, **(secrets or {})}
    env.pop("CLAUDECODE", None)

    label = profile.name or f"{profile.cli or profile.provider}/{profile.model}"
    if not quiet:
        _log(f"  Starting {label} (model={profile.model}, timeout={profile.timeout_seconds}s)...")

    start_wall = time.time()
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

        lp = f"[{label}] " if quiet else ""
        for line in proc.stdout:
            lines.append(line)
            _process_stream_event(line.strip(), label_prefix=lp)
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
            cost_usd=None,
            exit_code=-1,
            raw={},
            profile_name=profile.name,
        )

    if timed_out or (time.monotonic() - start) >= profile.timeout_seconds * 1.05:
        timed_out = True

    if timed_out:
        partial_output = "".join(lines)
        return AgentResult(
            success=False,
            output=f"TIMEOUT: Agent exceeded {profile.timeout_seconds}s limit",
            session_id=_get_claude_session_id(
                partial_output,
                working_dir,
                fallback_to_file=fallback_to_file,
                min_mtime=start_wall,
            ),
            cost_usd=None,
            exit_code=-9,
            raw={},
            profile_name=profile.name,
        )

    elapsed = time.monotonic() - start
    if not quiet:
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
            session_id=_get_claude_session_id(
                raw_output or stderr_text,
                working_dir,
                fallback_to_file=fallback_to_file,
                min_mtime=start_wall,
            ),
            cost_usd=None,
            exit_code=proc.returncode,
            raw={},
            profile_name=profile.name,
        )

    try:
        raw_cost = result_json.get("total_cost_usd")
        cost = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        cost = None

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


def _get_codex_session_id(*, min_mtime: float) -> str | None:
    """Return the newest codex session ID created after min_mtime.

    Scans ~/.codex/session_index.jsonl for entries whose updated_at timestamp
    is strictly after min_mtime (epoch seconds). Same pattern as the Claude
    transcript-file fallback in _get_claude_session_id().
    """
    index_file = Path.home() / ".codex" / "session_index.jsonl"
    try:
        lines = index_file.read_text().splitlines()
    except OSError:
        return None

    best_id: str | None = None
    best_ts: float | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = entry.get("id")
        updated = entry.get("updated_at")
        if not sid or not updated:
            continue
        try:
            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            ts = dt.timestamp()
        except ValueError:
            continue
        if ts > min_mtime and (best_ts is None or ts > best_ts):
            best_ts = ts
            best_id = sid
    return best_id


def _run_codex(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
    quiet: bool = False,
    is_pool: bool = False,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Invoke `npx @openai/codex exec --full-auto` as a subprocess.

    Output is captured via a temp file using `-o <file>`;
    falls back to stdout if the file is empty.

    Session ID extraction scans ~/.codex/session_index.jsonl for the newest
    entry after the run start. This is safe for sequential (single-reviewer)
    runs but not for parallel pools — when is_pool=True we return None to
    avoid misattributing a concurrent invocation's session to this one.
    """
    fd, output_path_str = tempfile.mkstemp(suffix=".txt", prefix="forge_codex_")
    os.close(fd)
    output_file = Path(output_path_str)

    # Resume: `codex exec resume <id> [flags] -` (prompt via stdin).
    # Fresh start: `codex exec [flags] <prompt>` (prompt as positional arg).
    if session_id:
        cmd: list[str] = [
            "npx",
            "@openai/codex",
            "exec",
            "resume",
            session_id,
            "--full-auto",
            "-m",
            profile.model,
        ]
        if profile.reasoning_effort:
            cmd += ["-c", f"model_reasoning_effort={profile.reasoning_effort}"]
        cmd += ["-C", str(working_dir), "-o", str(output_file), "-"]
        stdin_prompt: str | None = prompt
    else:
        cmd = [
            "npx",
            "@openai/codex",
            "exec",
            "--full-auto",
            "-m",
            profile.model,
        ]
        if profile.reasoning_effort:
            cmd += ["-c", f"model_reasoning_effort={profile.reasoning_effort}"]
        cmd += ["-C", str(working_dir), "-o", str(output_file), prompt]
        stdin_prompt = None

    start_wall = time.time()
    label = profile.name or f"{profile.cli or profile.provider}/{profile.model}"
    _codex_env = {**os.environ, **(secrets or {})}
    outcome, elapsed = _run_with_heartbeat(
        run_fn=lambda: subprocess.run(
            cmd,
            input=stdin_prompt,
            capture_output=True,
            text=True,
            timeout=profile.timeout_seconds,
            env=_codex_env,
        ),
        label=label,
        profile=profile,
        cli_name="npx @openai/codex",
        quiet=quiet,
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
        if not quiet:
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

        # Only extract session_id for sequential runs; parallel pools risk
        # picking up a sibling invocation's entry from the global index.
        extracted_sid = None if is_pool else _get_codex_session_id(min_mtime=start_wall)

        if result_json:
            return AgentResult(
                success=proc.returncode == 0,
                output=result_json.get("result", output_text),
                session_id=extracted_sid,
                cost_usd=None,
                exit_code=proc.returncode,
                raw=result_json,
                profile_name=profile.name,
            )

        return AgentResult(
            success=proc.returncode == 0,
            output=output_text,
            session_id=extracted_sid,
            cost_usd=None,
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
    quiet: bool = False,
    is_pool: bool = False,
    secrets: dict[str, str] | None = None,
) -> AgentResult:
    """Invoke `npx @google/gemini-cli -p <prompt> --yolo -m <model> -o json` as a subprocess.

    Session resume: gemini --resume accepts "latest", an index number, or a UUID.
    Sessions are scoped to the current working directory. We return "latest" so the
    next sequential call resumes the same project session. This is only safe for
    single-reviewer runs — parallel pools get session_id=None because "--resume latest"
    is not invocation-scoped and two concurrent gemini reviewers would trample each
    other's context.
    """
    cmd: list[str] = ["npx", "@google/gemini-cli"]
    if session_id:
        cmd += ["--resume", session_id]
    cmd += [
        "-p",
        prompt,
        "--yolo",
        "-m",
        profile.model,
        "-o",
        "json",
    ]

    # NOTE: Gemini CLI has no --config flag for thinking config.
    # reasoning_effort is silently ignored for gemini until a CLI mechanism exists.
    # The model uses its default thinking level.

    label = profile.name or f"{profile.cli or profile.provider}/{profile.model}"
    _gemini_env = {**os.environ, **(secrets or {})}
    outcome, elapsed = _run_with_heartbeat(
        run_fn=lambda: subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(working_dir),
            timeout=profile.timeout_seconds,
            env=_gemini_env,
        ),
        label=label,
        profile=profile,
        cli_name="gemini",
        quiet=quiet,
    )

    if outcome.exception:
        result = _handle_exception(outcome.exception, profile=profile, cli_name="gemini")
        if result:
            return result
        raise outcome.exception

    proc = outcome.proc
    assert proc is not None
    if not quiet:
        _log_verbose(f"  ... {label} done ({elapsed:.0f}s)")

    # Parse JSON output (-o json requests structured response).
    # The gemini CLI emits preamble lines (e.g. "YOLO mode is enabled.") to
    # stdout before the JSON object, so find the first '{' and parse from there.
    result_json: dict[str, Any] = {}
    json_start = proc.stdout.find("{")
    json_candidate = proc.stdout[json_start:] if json_start != -1 else proc.stdout
    try:
        result_json = json.loads(json_candidate)
    except (json.JSONDecodeError, ValueError):
        # Don't return "latest" on parse failure: the CLI may have exited before
        # creating a resumable session, so resuming would attach to stale context.
        return AgentResult(
            success=proc.returncode == 0,
            output=proc.stdout or proc.stderr or "(no output)",
            session_id=None,
            cost_usd=None,
            exit_code=proc.returncode,
            raw={},
            profile_name=profile.name,
        )

    # "latest" is only safe for sequential single-reviewer runs; parallel pools
    # would trample each other since --resume latest is not invocation-scoped.
    resume_sid = None if is_pool else "latest"
    return AgentResult(
        success=proc.returncode == 0,
        output=result_json.get("response", result_json.get("result", proc.stdout)),
        session_id=resume_sid,
        cost_usd=None,
        exit_code=proc.returncode,
        raw=result_json,
        profile_name=profile.name,
    )


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
