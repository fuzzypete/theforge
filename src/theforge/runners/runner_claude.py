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
from theforge.task.handoff_parser import ParseError, extract_dev_handoff
from theforge.workspace_env import build_workspace_env

from ..config import ModelProfile
from .sandbox import read_only_sandbox_command, workspace_effect_sandbox_command

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
        "--verbose",
        "--model",
        profile.model,
    ]

    if profile.allowed_tools:
        cmd.extend(["--allowedTools", " ".join(profile.allowed_tools)])

    if session_id:
        cmd.extend(["--resume", session_id])

    # Keep Claude's native permission mode enabled when sandboxing is requested.
    # The OS sandbox is the enforcement mechanism; Claude's own permission mode
    # remains as a cooperative layer.
    if profile.sandbox_mode != "none":
        cmd.extend(["--permission-mode", "default"])
        claude_state_dir = Path.home() / ".claude"
        if not claude_state_dir.exists():
            try:
                claude_state_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return AgentResult(
                    success=False,
                    output=(
                        "SANDBOX_UNAVAILABLE: unable to prepare Claude state directory "
                        f"{claude_state_dir}: {exc}"
                    ),
                    session_id=None,
                    cost_usd=None,
                    exit_code=-1,
                    raw={},
                    profile_name=profile.name,
                    startup_failure=True,
                )
        sandbox_builder = (
            read_only_sandbox_command
            if profile.sandbox_mode == "read-only"
            else workspace_effect_sandbox_command
        )
        sandboxed_cmd = sandbox_builder(
            cmd,
            working_dir,
            extra_write_roots=[claude_state_dir],
        )
        if sandboxed_cmd == cmd:
            _log(
                f"✗ claude: sandbox_mode={profile.sandbox_mode!r} requested but platform "
                "sandbox (sandbox-exec/bwrap) is unavailable — refusing to run unsandboxed. "
                "Set sandbox_mode: none to explicitly opt out of write containment."
            )
            return AgentResult(
                success=False,
                output=(
                    f"SANDBOX_UNAVAILABLE: sandbox_mode={profile.sandbox_mode!r} is set but "
                    "the platform sandbox (sandbox-exec on macOS, bwrap on Linux) is not "
                    "available on this host. Claude CLI relies on OS sandboxing for "
                    "mechanical write containment. Set sandbox_mode: none to run without "
                    "write containment."
                ),
                session_id=None,
                cost_usd=None,
                exit_code=-1,
                raw={},
                profile_name=profile.name,
                startup_failure=True,
            )
        cmd = sandboxed_cmd

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
            startup_failure=True,
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
