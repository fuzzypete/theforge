"""Claude Code CLI runner.

Invokes `claude -p --output-format stream-json --verbose` as a subprocess,
streams JSONL events, and returns an AgentResult.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from theforge.agent_types import AgentResult, ModelUsage
from theforge.log_util import _log_line
from theforge.runners.stuck_detection import StuckTracker, build_observation
from theforge.task.handoff_parser import ParseError, extract_dev_handoff
from theforge.workspace_env import build_workspace_env

from ..config import ModelProfile

# ── Logging helpers ───────────────────────────────────────────────────


def _log(msg: str) -> None:
    _log_line("[forge]", msg)


def _log_verbose(msg: str) -> None:
    from theforge.log_level import _LOG_LEVEL, LogLevel  # noqa: PLC0415

    if _LOG_LEVEL >= LogLevel.VERBOSE:
        _log_line("[forge]", msg)


# ── Claude-specific helpers ───────────────────────────────────────────


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


class _StreamCall:
    """Minimal duck-type for stuck_detection.build_observation()."""

    __slots__ = ("name", "arguments")

    def __init__(self, name: str, arguments: dict | None) -> None:
        self.name = name
        self.arguments = arguments if isinstance(arguments, dict) else {}


class _ClaudeStreamMonitor:
    """Group Claude stream events into iterations and feed a StuckTracker.

    Claude emits ``assistant`` events with one or more ``tool_use`` items per
    LLM turn, then ``user`` events with the matching ``tool_result`` items.
    We treat one assistant turn (with its tool_results) as one agent
    iteration. The monitor calls ``tracker.observe`` once per iteration.

    On a stuck-pattern termination the monitor records a reason and the
    runner kills the subprocess; the runner translates the reason into a
    failure ``AgentResult`` (exit_code -2) so the coordinator can attribute
    the early termination correctly.
    """

    def __init__(self, profile: ModelProfile) -> None:
        self._tracker = StuckTracker(profile)
        self._enabled = self._tracker.enabled
        self._tool_name_by_id: dict[str, str] = {}
        self._pending_calls: list[_StreamCall] = []
        self._pending_results: list[dict] = []
        self.terminate_reason: str | None = None
        self.terminate_pattern: str | None = None
        self.nudge_pattern: str | None = None  # last nudge pattern (for logging)
        self._pending_nudge: str | None = None  # nudge text awaiting delivery
        self.iteration_count = 0

    def consume_pending_nudge(self) -> str | None:
        """Return and clear the nudge message awaiting delivery, if any."""
        msg = self._pending_nudge
        self._pending_nudge = None
        return msg

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def should_terminate(self) -> bool:
        return self.terminate_reason is not None

    def ingest(self, line: str) -> None:
        if not self._enabled or not line:
            return
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return
        et = event.get("type")
        if et == "assistant":
            self._on_assistant(event)
        elif et == "user":
            self._on_user(event)
        elif et == "result":
            self._flush()

    def finalize(self) -> None:
        """Flush any unprocessed pending iteration at end of stream."""
        if self._enabled:
            self._flush()

    def _on_assistant(self, event: dict) -> None:
        msg = event.get("message", {}) or {}
        content = msg.get("content", []) or []
        new_calls: list[_StreamCall] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                tid = item.get("id", "")
                name = item.get("name", "") or ""
                args = item.get("input", {})
                if tid:
                    self._tool_name_by_id[tid] = name
                new_calls.append(_StreamCall(name, args))
        if not new_calls:
            return
        # If a previous iteration's results never arrived, flush what we have.
        if self._pending_calls:
            self._flush()
        self._pending_calls = new_calls

    def _on_user(self, event: dict) -> None:
        msg = event.get("message", {}) or {}
        content = msg.get("content", []) or []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            tid = item.get("tool_use_id", "")
            tool_name = self._tool_name_by_id.get(tid, "")
            raw_content = item.get("content", "")
            if isinstance(raw_content, list):
                parts = []
                for piece in raw_content:
                    if isinstance(piece, dict) and "text" in piece:
                        parts.append(str(piece.get("text", "")))
                    else:
                        parts.append(str(piece))
                text = " ".join(parts)
            else:
                text = str(raw_content)
            is_error = bool(item.get("is_error"))
            content_str = f"Error: {text}" if is_error and not text.startswith("Error") else text
            self._pending_results.append({"name": tool_name, "content": content_str})
        if self._pending_calls and len(self._pending_results) >= len(self._pending_calls):
            self._flush()

    def _flush(self) -> None:
        if not self._pending_calls and not self._pending_results:
            return
        obs = build_observation(self._pending_calls, self._pending_results)
        nudge_msg, terminate_reason, pattern = self._tracker.observe(obs)
        self.iteration_count += 1
        if nudge_msg is not None:
            self.nudge_pattern = pattern
            self._pending_nudge = nudge_msg
        if terminate_reason is not None and self.terminate_reason is None:
            self.terminate_reason = terminate_reason
            self.terminate_pattern = pattern
        self._pending_calls = []
        self._pending_results = []


def _write_user_message(stdin: Any, text: str) -> None:
    """Write a single user message to claude's stream-json input pipe.

    Claude's ``--input-format stream-json`` reads one JSON object per line; each
    line that is a ``user`` message is processed as another conversation turn.
    Used both for the initial prompt and for stuck-detection nudges.
    """
    if stdin is None:
        return
    payload = {"type": "user", "message": {"role": "user", "content": text}}
    try:
        stdin.write(json.dumps(payload) + "\n")
        stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        # Subprocess closed stdin (e.g. after kill); silently ignore.
        pass


def _try_parse_handoff(output: str) -> dict | None:
    """Best-effort extraction of <forge_handoff> from agent output. Logs on parse error."""
    try:
        return extract_dev_handoff(output)
    except ParseError as exc:
        _log_verbose(f"  handoff parse error (non-fatal): {exc}")
        return None


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
        "--input-format",
        "stream-json",
        "--verbose",
        "--model",
        profile.model,
    ]

    if profile.allowed_tools:
        cmd.extend(["--allowedTools", " ".join(profile.allowed_tools)])

    if session_id:
        cmd.extend(["--resume", session_id])

    # Engage Claude's native permission mode when sandboxing is requested.
    # "default" prompts before writes outside cwd; in automated runs where there is
    # no human to approve, out-of-worktree writes are effectively blocked.
    # NOTE: Claude CLI has no mechanically-enforced read-only mode. When
    # sandbox_mode == "read-only" we apply the same --permission-mode default and
    # log a warning so operators know the read-only constraint is not syscall-enforced.
    if profile.sandbox_mode == "read-only":
        _log(
            "  WARNING: sandbox_mode=read-only is not mechanically enforced by Claude CLI; "
            "applying --permission-mode default (writes require permission approval). "
            "Use a provider/API profile for true read-only enforcement."
        )
    if profile.sandbox_mode != "none":
        cmd.extend(["--permission-mode", "default"])

    # Unset CLAUDECODE so the subprocess isn't blocked by the nested-session check
    env = build_workspace_env(working_dir, extra=secrets)
    env.pop("CLAUDECODE", None)

    label = profile.name or f"{profile.cli or profile.provider}/{profile.model}"
    if not quiet:
        _log(f"  Starting {label} (model={profile.model}, timeout={profile.timeout_seconds}s)...")

    start_wall = time.time()
    start = time.monotonic()
    deadline = start + profile.timeout_seconds
    timed_out = False
    stuck_monitor = _ClaudeStreamMonitor(profile)
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
        # Send the initial prompt as a stream-json user message. stdin is kept
        # open so stuck-detection nudges can be injected as additional user
        # messages mid-run; it is closed only after the stream completes
        # (or the run is killed).
        _write_user_message(proc.stdin, prompt)

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
            stripped = line.strip()
            _process_stream_event(stripped, label_prefix=lp)
            stuck_monitor.ingest(stripped)
            pending_nudge = stuck_monitor.consume_pending_nudge()
            if pending_nudge is not None:
                _write_user_message(proc.stdin, pending_nudge)
                _log(f"  ⚠ {label} stuck-detection nudge sent: {stuck_monitor.nudge_pattern}")
            if stuck_monitor.should_terminate:
                _log(
                    f"  ⚠ {label} stuck-detection terminate after "
                    f"{stuck_monitor.iteration_count} iterations: "
                    f"{stuck_monitor.terminate_pattern}"
                )
                proc.kill()
                break
            if time.monotonic() > deadline:
                proc.kill()
                timed_out = True
                break
            # Break as soon as the result event arrives — the stream is complete.
            # Closing stdin immediately lets the subprocess exit cleanly rather
            # than blocking until the watchdog fires (#1054).
            try:
                if stripped and json.loads(stripped).get("type") == "result":
                    break
            except (json.JSONDecodeError, ValueError):
                pass

        stuck_monitor.finalize()
        # Close stdin now that the stream is done (or the proc was killed) so
        # any reader cleanup completes; ignore errors if stdin is already
        # closed by the kill path.
        try:
            proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass
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
            startup_failure=True,
        )

    if stuck_monitor.should_terminate:
        partial_output = "".join(lines)
        return AgentResult(
            success=False,
            output=(
                f"Agent loop terminated: {stuck_monitor.terminate_reason} "
                f"(at iteration {stuck_monitor.iteration_count})"
            ),
            session_id=_get_claude_session_id(
                partial_output,
                working_dir,
                fallback_to_file=fallback_to_file,
                min_mtime=start_wall,
            ),
            cost_usd=None,
            exit_code=-2,
            raw={},
            profile_name=profile.name,
            failure_code="stuck_pattern",
            dev_handoff=_try_parse_handoff(partial_output),
        )

    if timed_out or (time.monotonic() - start) >= profile.timeout_seconds * 1.05:
        timed_out = True

    if timed_out:
        partial_output = "".join(lines)
        _timeout_output = f"TIMEOUT: Agent exceeded {profile.timeout_seconds}s limit"
        return AgentResult(
            success=False,
            output=_timeout_output,
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
            dev_handoff=_try_parse_handoff(partial_output),
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
        _noresult_output = raw_output or stderr_text or "(no output)"
        return AgentResult(
            success=proc.returncode == 0,
            output=_noresult_output,
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
            dev_handoff=_try_parse_handoff(_noresult_output),
        )

    try:
        raw_cost = result_json.get("total_cost_usd")
        cost = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        cost = None

    _success_output = result_json.get("result", "".join(lines))
    return AgentResult(
        success=proc.returncode == 0,
        output=_success_output,
        session_id=result_json.get("session_id"),
        cost_usd=cost,
        exit_code=proc.returncode,
        raw=result_json,
        profile_name=profile.name,
        model_usage=_parse_model_usage(result_json),
        dev_handoff=_try_parse_handoff(_success_output),
    )
