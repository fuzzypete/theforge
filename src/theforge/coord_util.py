"""Coordinator utilities: logging, run-id generation, and shell execution.

Extracted from coord_state.py so that coord_state.py can remain stdlib-only
(dataclasses/enums only, no project imports at runtime).
"""

from __future__ import annotations

import secrets
import subprocess
import sys
from pathlib import Path

from .runner import LogLevel

# ── Log level ─────────────────────────────────────────────────────────

_LOG_LEVEL: LogLevel = LogLevel.PROGRESS


def set_log_level(level: LogLevel) -> None:
    global _LOG_LEVEL
    _LOG_LEVEL = level


# ── Logging ──────────────────────────────────────────────────────────


def _fmt_duration(seconds: float) -> str:
    """Format duration as '2h 14m 3s', '14m 3s', or '47s'."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _log(msg: str) -> None:
    """Print coordinator status to stderr (always shown)."""
    print(f"[forge] {msg}", file=sys.stderr, flush=True)


def _log_verbose(msg: str) -> None:
    """Print coordinator detail to stderr (verbose mode only)."""
    if _LOG_LEVEL >= LogLevel.VERBOSE:
        print(f"[forge] {msg}", file=sys.stderr, flush=True)


def _log_phase(phase: object, detail: str = "") -> None:
    suffix = f"   {detail}" if detail else ""
    _log(f"▸ {phase.name}{suffix}")  # type: ignore[attr-defined]


# ── Run ID ───────────────────────────────────────────────────────────


def _generate_run_id() -> str:
    """Return a short random hex run ID (12 chars)."""
    return secrets.token_hex(6)


# ── Shell helper ─────────────────────────────────────────────────────


def _run_shell(cmd: str, cwd: Path, timeout: int = 120) -> tuple[bool, str]:
    """Run a shell command. Returns (success, combined output)."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout,
        )
        output = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s: {cmd}"
    except Exception as e:
        return False, f"ERROR: {e}"
