"""CLI subprocess wrapper for invoking LLM agents.

Dispatches to the appropriate CLI based on ModelProfile.cli.
MVP supports Claude Code CLI. Extensible to Codex, Gemini, etc.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ModelProfile


@dataclass(frozen=True)
class AgentResult:
    """Structured result from an agent invocation."""

    success: bool  # subprocess returned 0
    output: str  # agent's text response
    session_id: str | None  # for --resume on follow-up
    cost_usd: float  # invocation cost
    exit_code: int  # raw exit code
    raw: dict[str, Any]  # full parsed JSON (if available)


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
        # Future: "codex": _run_codex, "gemini": _run_gemini
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
        )

    return runner_fn(
        prompt=prompt,
        profile=profile,
        working_dir=working_dir,
        session_id=session_id,
    )


# ── Claude Code CLI ──────────────────────────────────────────────────


def _run_claude(
    *,
    prompt: str,
    profile: ModelProfile,
    working_dir: Path,
    session_id: str | None = None,
) -> AgentResult:
    """Invoke `claude -p --output-format json` as a subprocess.

    The prompt is passed via stdin to avoid shell escaping issues
    with large spec content.
    """
    cmd: list[str] = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        profile.model,
    ]

    if profile.allowed_tools:
        cmd.extend(["--allowedTools", " ".join(profile.allowed_tools)])

    if session_id:
        cmd.extend(["--resume", session_id])

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(working_dir),
            timeout=profile.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return AgentResult(
            success=False,
            output=f"TIMEOUT: Agent exceeded {profile.timeout_seconds}s limit",
            session_id=None,
            cost_usd=0.0,
            exit_code=-1,
            raw={},
        )
    except FileNotFoundError:
        return AgentResult(
            success=False,
            output="ERROR: 'claude' CLI not found. Is Claude Code installed?",
            session_id=None,
            cost_usd=0.0,
            exit_code=-1,
            raw={},
        )

    # Parse JSON output
    result_json: dict[str, Any] = {}
    try:
        result_json = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        # If JSON parsing fails, fall back to raw text
        return AgentResult(
            success=proc.returncode == 0,
            output=proc.stdout or proc.stderr or "(no output)",
            session_id=None,
            cost_usd=0.0,
            exit_code=proc.returncode,
            raw={},
        )

    return AgentResult(
        success=proc.returncode == 0,
        output=result_json.get("result", proc.stdout),
        session_id=result_json.get("session_id"),
        cost_usd=result_json.get("cost_usd", 0.0),
        exit_code=proc.returncode,
        raw=result_json,
    )


def log_agent_result(result: AgentResult, role: str) -> None:
    """Print a summary of an agent result to stderr."""
    status = "OK" if result.success else "FAIL"
    print(
        f"  [{role}] {status} | exit={result.exit_code} | "
        f"cost=${result.cost_usd:.3f} | "
        f"output={len(result.output)} chars",
        file=sys.stderr,
    )
