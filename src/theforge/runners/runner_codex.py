"""Codex (OpenAI) CLI runner.

Invokes `npx @openai/codex exec --full-auto` as a subprocess,
captures output via a temp file, and returns an AgentResult.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from theforge.agent_types import AgentResult

from ..config import ModelProfile
from .cli import _handle_exception, _run_with_heartbeat

# ── Logging helpers ───────────────────────────────────────────────────


def _log(msg: str) -> None:
    print(f"[forge] {msg}", file=sys.stderr, flush=True)


def _log_verbose(msg: str) -> None:
    from theforge.log_level import _LOG_LEVEL, LogLevel  # noqa: PLC0415

    if _LOG_LEVEL >= LogLevel.VERBOSE:
        print(f"[forge] {msg}", file=sys.stderr, flush=True)


# ── Codex-specific helpers ────────────────────────────────────────────


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
