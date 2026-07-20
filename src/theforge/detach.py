"""Process detachment primitives for forge run/sprint.

Provides spawn-based daemonization (re-exec via subprocess.Popen so the
detached process is a fresh interpreter — required for macOS Objective-C
fork-safety), App Nap suppression (macOS), PID file management, and run
status queries.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Env sentinel used to mark a re-exec'd detached child. Read by
# ``is_detached_child()`` so cli/sprint.py and cli/run.py can skip the
# daemonize step in the child while still running the workload.
_DETACHED_ENV = "FORGE_DETACHED"
_DETACHED_RUN_ID_ENV = "FORGE_DETACHED_RUN_ID"
_DETACHED_SLUG_ENV = "FORGE_DETACHED_SLUG"
# Orchestrator project root, published so deep-in-stack runners can locate the
# .forge/runs/agents sidecar dir for process-group registration.
_PROJECT_ROOT_ENV = "FORGE_PROJECT_ROOT"

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
    """Remove .forge/runs/<run-id>.pid (and run-type markers), ignoring missing files."""
    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass
    remove_sprint_marker(run_id, project_root)
    remove_diagnose_marker(run_id, project_root)


def write_sprint_marker(run_id: str, project_root: Path) -> Path:
    """Mark <run_id> as a sprint run before any sprint state is written.

    Without this marker, ``forge status --watch`` cannot recognise a freshly
    detached sprint as watchable during preflight/DAG-build (only a PID file
    exists at that point), and silently falls back to a one-shot snapshot.
    """
    runs_dir = project_root / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    marker = runs_dir / f"{run_id}.sprint"
    marker.write_text("sprint\n", encoding="utf-8")
    return marker


def remove_sprint_marker(run_id: str, project_root: Path) -> None:
    """Remove .forge/runs/<run_id>.sprint, ignoring missing files."""
    marker = project_root / ".forge" / "runs" / f"{run_id}.sprint"
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def has_sprint_marker(run_id: str, project_root: Path) -> bool:
    """Return True when <run_id>.sprint marker exists."""
    return (project_root / ".forge" / "runs" / f"{run_id}.sprint").exists()


def write_diagnose_marker(run_id: str, project_root: Path) -> Path:
    """Mark <run_id> as a diagnose run before any diagnose state is written.

    Diagnose runs execute in the foreground (and, under ``--parallel``, as
    threads sharing one PID), so there is no detached child that writes a
    per-run type marker. This marker lets ``forge status`` classify a live
    PID-backed run as a diagnose before any diagnose-specific state exists on
    disk — mirroring the sprint marker for the detached sprint path.
    """
    runs_dir = project_root / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    marker = runs_dir / f"{run_id}.diagnose"
    marker.write_text("diagnose\n", encoding="utf-8")
    return marker


def remove_diagnose_marker(run_id: str, project_root: Path) -> None:
    """Remove .forge/runs/<run_id>.diagnose, ignoring missing files."""
    marker = project_root / ".forge" / "runs" / f"{run_id}.diagnose"
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def has_diagnose_marker(run_id: str, project_root: Path) -> bool:
    """Return True when <run_id>.diagnose marker exists."""
    return (project_root / ".forge" / "runs" / f"{run_id}.diagnose").exists()


def write_run_ended(
    run_id: str, project_root: Path, outcome: str = "stopped", *, force: bool = False
) -> None:
    """Write .forge/runs/<run_id>.ended with the terminal outcome.

    outcome is a short string: "stopped", "completed", or "orphaned".
    When force=False (default) the file is not overwritten if it already
    exists — the first writer (usually the SIGTERM handler) wins.
    """
    runs_dir = project_root / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    ended_file = runs_dir / f"{run_id}.ended"
    if not force and ended_file.exists():
        return
    try:
        ended_file.write_text(outcome, encoding="utf-8")
    except OSError:
        pass


def read_run_ended(run_id: str, project_root: Path) -> str | None:
    """Return the terminal outcome recorded in .forge/runs/<run_id>.ended, or None."""
    ended_file = project_root / ".forge" / "runs" / f"{run_id}.ended"
    try:
        return ended_file.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


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
    """Return best-effort status dict for a run (active, terminal, or orphaned).

    Checks in order:
    1. .ended sentinel → returns terminal outcome as phase (STOPPED, COMPLETED, …).
    2. Active PID file → reads phase from log (existing behaviour).
    3. No PID and no .ended → run exited without writing a terminal marker → ORPHANED.

    Returns dict with keys: phase, cost_usd, elapsed_seconds, log_path.
    """
    log_path = _find_log_path(slug, run_id, project_root)
    elapsed_seconds: float | None = None
    cost_usd: float | None = None

    # Compute elapsed and cost once; reused across all return paths.
    if log_path is not None and log_path.exists():
        try:
            stat = log_path.stat()
            created = getattr(stat, "st_birthtime", stat.st_ctime)
            elapsed_seconds = time.time() - created
        except OSError:
            pass

        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            for line in reversed(content.splitlines()):
                if "Total cost:" in line or "total_cost" in line:
                    m = re.search(r"\$([0-9]+\.[0-9]+)", line)
                    if m:
                        cost_usd = float(m.group(1))
                    break
        except OSError:
            pass

    base = {"cost_usd": cost_usd, "elapsed_seconds": elapsed_seconds, "log_path": log_path}

    # 1. Terminal marker — written by forge stop or normal exit.
    outcome = read_run_ended(run_id, project_root)
    if outcome is not None:
        return {**base, "phase": outcome.upper()}

    # 2. Active run — PID file exists; read phase from log.
    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    if pid_file.exists():
        phase = "RUNNING"
        if log_path is not None and log_path.exists():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in reversed(lines):
                    if "[forge] ▸ " in line:
                        parts = line.split("[forge] ▸ ", 1)
                        if len(parts) == 2:
                            phase = parts[1].split()[0] if parts[1].split() else phase
                        break
            except OSError:
                pass
        return {**base, "phase": phase}

    # 3. No PID, no .ended → process exited without writing a terminal marker.
    return {**base, "phase": "ORPHANED"}


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


def find_run_id_for_pid(project_root: Path, pid: int) -> str | None:
    """Return the run_id whose PID file contains ``pid``, or None."""
    runs_dir = project_root / ".forge" / "runs"
    try:
        for pf in runs_dir.glob("*.pid"):
            try:
                lines = pf.read_text(encoding="utf-8").splitlines()
                if lines and int(lines[0].strip()) == pid:
                    return pf.stem
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None


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


def is_detached_child() -> bool:
    """Return True when this process is a re-exec'd detached child.

    Set by ``daemonize_run`` in the parent before spawning. cli/sprint.py
    and cli/run.py read this to skip the daemonize step in the child while
    still running the workload.
    """
    return os.environ.get(_DETACHED_ENV) == "1"


def export_run_context(run_id: str, project_root: Path) -> None:
    """Publish (run_id, project_root) into the environment for deep-in-stack code.

    The CLI runners spawn agent subprocesses far below where ``project_root`` is
    known; they need it to register spawned process groups into the orchestrator's
    ``.forge/runs/agents`` sidecar dir (see ``theforge.process_group``). Export it
    here rather than threading it through the runner API. ``FORGE_DETACHED_RUN_ID``
    is already set on the detached path; set it too so the foreground/``--fg`` path
    records the run id as well.
    """
    os.environ[_PROJECT_ROOT_ENV] = str(project_root)
    os.environ.setdefault(_DETACHED_RUN_ID_ENV, run_id)


def setup_detached_child(
    run_id: str, slug: str, project_root: Path, *, is_sprint: bool = False
) -> None:
    """Initialize a re-exec'd detached child: write PID file and redirect file.

    The parent already redirected stdin/stdout/stderr via ``subprocess.Popen``
    kwargs, so no fd manipulation is needed here. We just write the PID file
    so ``forge status`` can find this run, and write the re-exec redirect
    sidecar if this child was launched by a coordinator post-pull execv.
    """
    log_file = project_root / ".forge" / "logs" / slug / f"run-{run_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    export_run_context(run_id, project_root)
    write_pid(run_id, slug, project_root)
    if is_sprint:
        write_sprint_marker(run_id, project_root)

    # Coordinator post-pull execv inherits FORGE_DETACHED + FORGE_PREV_RUN_ID
    # in env. Write the redirect sidecar so the log follower can switch.
    prev_run_id = os.environ.pop("FORGE_PREV_RUN_ID", None)
    if prev_run_id:
        write_reexec_redirect(prev_run_id, run_id, log_file, project_root)


def daemonize_run(run_id: str, slug: str, project_root: Path, *, is_sprint: bool = False) -> None:
    """Spawn a fresh interpreter as a detached child and exit the parent.

    Replaces the previous ``os.fork()``-based double-fork daemonization to
    avoid macOS Objective-C fork-safety crashes when the parent has already
    initialized Foundation/CoreFoundation (e.g., via subprocess work).

    The child re-executes the forge CLI with ``FORGE_DETACHED=1`` set in the
    environment; the entry point detects the sentinel via
    ``is_detached_child()`` and skips this function, calling
    ``setup_detached_child`` instead.

    This function never returns in the parent — it always calls ``sys.exit(0)``.
    """
    log_file = project_root / ".forge" / "logs" / slug / f"run-{run_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Re-exec env: orthogonal to FORGE_PREV_RUN_ID (coordinator-reexec sentinel).
    env = dict(os.environ)
    env[_DETACHED_ENV] = "1"
    env[_DETACHED_RUN_ID_ENV] = run_id
    env[_DETACHED_SLUG_ENV] = slug

    log_fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "theforge.cli", *sys.argv[1:]],
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
            env=env,
            cwd=os.getcwd(),
        )
    finally:
        os.close(log_fd)

    # Write PID file pre-emptively so a `forge status` invoked immediately
    # after launch has something to read. The child also writes it on entry
    # via setup_detached_child (idempotent — same pid, same slug).
    runs_dir = project_root / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    pid_file = runs_dir / f"{run_id}.pid"
    pid_file.write_text(f"{proc.pid}\n{slug}\n", encoding="utf-8")
    if is_sprint:
        write_sprint_marker(run_id, project_root)

    print("[forge] Run started in background")
    print(f"[forge] Run ID:  {run_id}")
    print(f"[forge] Log:     {log_file}")
    print("[forge] Status:  forge status")
    print(f"[forge] Logs:    forge logs {run_id}")
    sys.stdout.flush()
    sys.exit(0)


# ── Signal / cleanup helpers ─────────────────────────────────────────


def install_cleanup_handler(run_id: str, project_root: Path) -> None:
    """Install SIGTERM handler that writes a terminal marker and removes the PID file."""
    original_handler = signal.getsignal(signal.SIGTERM)

    def _handler(signum: int, frame: object) -> None:
        write_run_ended(run_id, project_root, "stopped")
        remove_pid(run_id, project_root)
        if callable(original_handler):
            original_handler(signum, frame)  # type: ignore[call-arg]
        else:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _handler)
