"""Coordinator utilities: logging, run-id generation, and shell execution.

Extracted from coord_state.py so that coord_state.py can remain stdlib-only
(dataclasses/enums only, no project imports at runtime).
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

from theforge.coordinator.log_tee import get_worker_slug
from theforge.log_level import LogLevel

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
    slug = get_worker_slug()
    prefix = f"[{slug}] " if slug else ""
    print(f"[forge] {prefix}{msg}", file=sys.stderr, flush=True)


def _log_verbose(msg: str) -> None:
    """Print coordinator detail to stderr (verbose mode only)."""
    if _LOG_LEVEL >= LogLevel.VERBOSE:
        slug = get_worker_slug()
        prefix = f"[{slug}] " if slug else ""
        print(f"[forge] {prefix}{msg}", file=sys.stderr, flush=True)


def _fmt_cost(cost: float | None) -> str:
    """Format a cost value as '$1.23', or 'unknown' when cost is None."""
    return f"${cost:.2f}" if cost is not None else "unknown"


def _log_phase(phase: object, detail: str = "") -> None:
    suffix = f"   {detail}" if detail else ""
    _log(f"▸ {phase.name}{suffix}")  # type: ignore[attr-defined]


# ── Run ID ───────────────────────────────────────────────────────────


def _generate_run_id() -> str:
    """Return a short random hex run ID (12 chars)."""
    return secrets.token_hex(6)


# ── Shell helper ─────────────────────────────────────────────────────


def resolve_timeout(
    base: int,
    medium: int | None,
    large: int | None,
    complexity: str | None,
) -> int:
    """Return the appropriate timeout for the given preflight complexity.

    Selects large/medium override when complexity matches and override is set;
    falls back to base otherwise.
    """
    if complexity == "large" and large is not None:
        return large
    if complexity == "medium" and medium is not None:
        return medium
    return base


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Best-effort kill for a spawned shell process group."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_shell(
    cmd: str, cwd: Path, timeout: int = 120, env: dict[str, str] | None = None
) -> tuple[bool, str]:
    """Run a shell command. Returns (success, combined output).

    On abnormal exit, kills the entire process group so child processes (e.g.
    pytest-xdist workers) don't outlive the shell and consume unbounded memory.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd),
            env=env if env is not None else os.environ.copy(),
            start_new_session=True,
        )
    except Exception as e:
        return False, f"ERROR: {e}"
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        output = (stdout + stderr).strip()
        return proc.returncode == 0, output
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.wait()
        return False, f"TIMEOUT after {timeout}s: {cmd}"
    except BaseException:
        _kill_process_group(proc)
        proc.wait()
        raise
    finally:
        for stream_name in ("stdout", "stderr"):
            stream = getattr(proc, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
