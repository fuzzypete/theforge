"""Process detachment primitives for forge run/sprint.

Provides double-fork daemonization, App Nap suppression (macOS),
PID file management, and run status queries.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Module-level list to prevent GC of App Nap activity tokens
_APP_NAP_ACTIVITIES: list[Any] = []

# ── PID file helpers ─────────────────────────────────────────────────


def write_pid(run_id: str, slug: str, project_root: Path) -> Path:
    """Write PID and slug to .forge/runs/<run-id>.pid.

    File format:
        line 1: PID (integer)
        line 2: slug (string)

    Returns the path to the written PID file.
    """
    runs_dir = project_root / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    pid_file = runs_dir / f"{run_id}.pid"
    pid_file.write_text(f"{os.getpid()}\n{slug}\n", encoding="utf-8")
    return pid_file


def remove_pid(run_id: str, project_root: Path) -> None:
    """Remove .forge/runs/<run-id>.pid, ignoring missing files."""
    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass


def _read_pid_file(pid_file: Path) -> tuple[int, str] | None:
    """Parse a PID file. Returns (pid, slug) or None on error."""
    try:
        lines = pid_file.read_text(encoding="utf-8").splitlines()
        if not lines:
            return None
        pid = int(lines[0].strip())
        slug = lines[1].strip() if len(lines) > 1 else pid_file.stem
        return pid, slug
    except (OSError, ValueError):
        return None


# ── Active run management ────────────────────────────────────────────


def list_active_runs(project_root: Path) -> list[dict]:
    """Scan .forge/runs/*.pid and return list of active run dicts.

    Each dict has: run_id, pid, slug, alive.
    Stale PID files (dead processes) are removed.
    """
    runs_dir = project_root / ".forge" / "runs"
    if not runs_dir.exists():
        return []

    results = []
    for pid_file in sorted(runs_dir.glob("*.pid")):
        run_id = pid_file.stem
        parsed = _read_pid_file(pid_file)
        if parsed is None:
            # Corrupt file — remove it
            try:
                pid_file.unlink()
            except OSError:
                pass
            continue

        pid, slug = parsed
        alive = _is_pid_alive(pid)
        if not alive:
            # Clean up stale PID file
            try:
                pid_file.unlink()
            except OSError:
                pass
        else:
            results.append({"run_id": run_id, "pid": pid, "slug": slug, "alive": True})

    return results


def _is_pid_alive(pid: int) -> bool:
    """Return True if the process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


# ── Run status ──────────────────────────────────────────────────────


def read_run_status(run_id: str, slug: str, project_root: Path) -> dict:
    """Return best-effort status dict for a running process.

    Searches the run log for the most recent phase marker and
    computes elapsed from the log file's creation time.

    Returns dict with keys: phase, cost_usd, elapsed_seconds, log_path.
    """
    log_path = _find_log_path(slug, run_id, project_root)

    phase = "RUNNING"
    cost_usd: float | None = None
    elapsed_seconds: float | None = None

    if log_path is not None and log_path.exists():
        # Extract elapsed from file mtime vs creation time
        try:
            stat = log_path.stat()
            # Use st_birthtime (macOS) or st_ctime as creation proxy
            created = getattr(stat, "st_birthtime", stat.st_ctime)
            elapsed_seconds = time.time() - created
        except OSError:
            pass

        # Scan log for most recent phase marker and cost
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            for line in reversed(lines):
                if "[forge] ▸ " in line:
                    # e.g. "[forge] ▸ DEV   $1.23 elapsed"
                    parts = line.split("[forge] ▸ ", 1)
                    if len(parts) == 2:
                        phase_part = parts[1].split()[0] if parts[1].split() else phase
                        phase = phase_part
                    break
            # Find most recent cost line
            for line in reversed(lines):
                if "Total cost:" in line or "total_cost" in line:
                    # e.g. "  Total cost: $1.234"
                    import re

                    m = re.search(r"\$([0-9]+\.[0-9]+)", line)
                    if m:
                        cost_usd = float(m.group(1))
                    break
        except OSError:
            pass

    return {
        "phase": phase,
        "cost_usd": cost_usd,
        "elapsed_seconds": elapsed_seconds,
        "log_path": log_path,
    }


def _find_log_path(slug: str, run_id: str, project_root: Path) -> Path | None:
    """Find the log file for a run.

    Checks .forge/logs/<slug>/run-<run_id>.log first, then falls back to
    searching for the same filename in any log subdirectory. Finally, falls
    back to the legacy shared run.log path for pre-migration runs.
    """
    new_style = project_root / ".forge" / "logs" / slug / f"run-{run_id}.log"
    if new_style.exists():
        return new_style

    logs_dir = project_root / ".forge" / "logs"
    if logs_dir.exists():
        for match in logs_dir.rglob(f"run-{run_id}.log"):
            return match

    legacy = project_root / ".forge" / "logs" / slug / "run.log"
    if legacy.exists():
        return legacy

    return new_style


# ── App Nap suppression ─────────────────────────────────────────────


def suppress_app_nap() -> None:
    """Prevent macOS App Nap from throttling the process.

    Uses NSProcessInfo via PyObjC if available. Falls back to ctypes.
    No-op on non-macOS platforms or when frameworks are unavailable.
    """
    if sys.platform != "darwin":
        return

    # Try PyObjC first
    try:
        from Foundation import NSProcessInfo  # type: ignore[import]

        NSActivityUserInitiatedAllowingIdleSystemSleep = 0x00FFFFFF
        reason = "forge background run"
        activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiatedAllowingIdleSystemSleep, reason
        )
        # Store on module-level list to prevent GC
        _APP_NAP_ACTIVITIES.append(activity)
        return
    except (ImportError, AttributeError, OSError):
        pass

    # Fallback: IOPMAssertionCreateWithName via ctypes
    try:
        import ctypes
        import ctypes.util

        iokit_path = ctypes.util.find_library("IOKit")
        if iokit_path is None:
            return
        iokit = ctypes.CDLL(iokit_path)
        # kIOPMAssertionTypePreventUserIdleSystemSleep
        assertion_type = ctypes.create_string_buffer(b"PreventUserIdleSystemSleep")
        assertion_level = ctypes.c_uint32(255)  # kIOPMAssertionLevelOn
        assertion_id = ctypes.c_uint32(0)
        iokit.IOPMAssertionCreateWithName(
            assertion_type,
            assertion_level,
            ctypes.create_string_buffer(b"forge background run"),
            ctypes.byref(assertion_id),
        )
    except (ImportError, AttributeError, OSError):
        pass


# ── Re-exec redirect sidecar ─────────────────────────────────────────


def write_reexec_redirect(
    prev_run_id: str, new_run_id: str, new_log: Path, project_root: Path
) -> None:
    """Write a sidecar redirect file so the log follower can switch to the new log.

    Written by the grandchild daemon after a re-exec (source changed after git pull)
    so that ``_follow_log_with_redirect`` in the CLI can detect the new run without
    parsing log lines (which LLM output could spoof).
    """
    redirect_file = project_root / ".forge" / "runs" / f"{prev_run_id}.redirect"
    try:
        redirect_file.write_text(
            json.dumps({"new_run_id": new_run_id, "new_log": str(new_log)}),
            encoding="utf-8",
        )
    except OSError:
        pass  # best-effort; log follower will time out gracefully if this fails


# ── Daemonization ────────────────────────────────────────────────────


def daemonize_run(run_id: str, slug: str, project_root: Path) -> None:
    """Double-fork daemonize, redirect stdio, write PID file.

    On return from this function in the parent, we have already exited 0
    after printing run_id and log path. The grandchild (daemon) continues.

    Raises RuntimeError if os.fork() is unavailable (Windows).
    """
    log_file = project_root / ".forge" / "logs" / slug / f"run-{run_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Save original stdout for the pre-fork print
    orig_stdout = sys.stdout

    # First fork
    try:
        pid = os.fork()
    except AttributeError:
        raise RuntimeError("os.fork() not available on this platform")

    if pid > 0:
        # Parent: wait for intermediate child then exit
        os.waitpid(pid, 0)
        # Print run info to original terminal before exiting
        print("[forge] Run started in background", file=orig_stdout)
        print(f"[forge] Run ID:  {run_id}", file=orig_stdout)
        print(f"[forge] Log:     {log_file}", file=orig_stdout)
        print("[forge] Status:  forge status", file=orig_stdout)
        print(f"[forge] Logs:    forge logs {run_id}", file=orig_stdout)
        orig_stdout.flush()
        sys.exit(0)

    # Intermediate child: become session leader
    os.setsid()

    # Second fork — prevents re-acquiring a controlling terminal
    try:
        pid = os.fork()
    except AttributeError:
        pass
    else:
        if pid > 0:
            sys.exit(0)

    # Grandchild: write PID file, redirect stdio
    write_pid(run_id, slug, project_root)

    # If this is a re-exec (source changed after git pull), write a sidecar
    # redirect file so the log follower can switch to this new run without
    # relying on log-line parsing (which LLM output could spoof).
    prev_run_id = os.environ.pop("FORGE_PREV_RUN_ID", None)
    if prev_run_id:
        write_reexec_redirect(prev_run_id, run_id, log_file, project_root)

    # Redirect stdin to /dev/null — use raw fd ops to avoid issues with
    # Python file objects in forked processes
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)  # fd 0 = stdin
    os.close(null_fd)

    # Redirect stdout/stderr to log file
    log_fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)  # fd 1 = stdout
    os.dup2(log_fd, 2)  # fd 2 = stderr
    os.close(log_fd)

    # Re-open Python-level file objects pointing at the new fds
    sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: WPS515
    sys.stdout = open(log_file, "a", encoding="utf-8", buffering=1)  # noqa: WPS515
    sys.stderr = open(log_file, "a", encoding="utf-8", buffering=1)  # noqa: WPS515


# ── Signal / cleanup helpers ─────────────────────────────────────────


def install_cleanup_handler(run_id: str, project_root: Path) -> None:
    """Install SIGTERM handler that removes the PID file on clean shutdown."""
    original_handler = signal.getsignal(signal.SIGTERM)

    def _handler(signum: int, frame: object) -> None:
        remove_pid(run_id, project_root)
        if callable(original_handler):
            original_handler(signum, frame)  # type: ignore[call-arg]
        else:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _handler)
