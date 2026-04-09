"""Gemini (Google) CLI runner.

Invokes `npx @google/gemini-cli -p <prompt> --yolo -m <model> -o json`
as a subprocess and returns an AgentResult.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


# ── Gemini runner ─────────────────────────────────────────────────────


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
