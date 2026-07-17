"""Per-run log tee and story log directory helpers."""

from __future__ import annotations

import sys as _sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

# The worker-slug thread-local now lives in the stdlib-only leaf module
# ``theforge.log_util`` so the shared log-line emitter can prefix every line
# (coordinator *and* runner) with it. Re-exported here for backward
# compatibility with existing importers.
from theforge.log_util import get_worker_slug as get_worker_slug
from theforge.log_util import set_worker_slug as set_worker_slug

if TYPE_CHECKING:
    from theforge.config import ForgeConfig

    from .logging import StructuredLogger


def _safe_signal(signum, handler):
    """Register a signal handler only from the main thread.

    Worker threads (e.g. parallel sprint) cannot register signal handlers.
    Returns the previous handler if registered, or None if skipped.
    """
    import signal

    if threading.current_thread() is threading.main_thread():
        return signal.signal(signum, handler)
    return None


def _make_story_log_dir(
    config: "ForgeConfig",
    task_slug: str,
    sprint_name: "str | None" = None,
) -> "Path | None":
    """Create and return the per-story log directory under <project_root>/.forge/logs/.

    For sprint runs: <project_root>/.forge/logs/<sprint-name>/<slug>/
    For standalone runs: <project_root>/.forge/logs/<slug>/

    Returns the created Path on success, or None on failure (best-effort).
    """
    try:
        if sprint_name:
            log_dir = config.project_root / ".forge" / "logs" / sprint_name / task_slug
        else:
            log_dir = config.project_root / ".forge" / "logs" / task_slug
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
    except Exception:
        return None


def _write_log_artifact(log_dir: "Path | None", relative_path: str, content: str) -> None:
    """Write content to <log_dir>/<relative_path>. Best-effort; never raises."""
    if log_dir is None:
        return
    try:
        dest = log_dir / relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    except Exception:
        pass


class _TeeStderr:
    """Write-through wrapper that copies every stderr write to a log file."""

    def __init__(self, original: object, log_fh: object) -> None:
        self._orig = original
        self._fh = log_fh

    def write(self, s: str) -> int:
        self._orig.write(s)
        try:
            self._fh.write(s)
            self._fh.flush()
        except Exception:
            pass  # best-effort; never crash the coordinator
        return len(s)

    def flush(self) -> None:
        self._orig.flush()
        try:
            self._fh.flush()
        except Exception:
            pass

    def __getattr__(self, name: str) -> object:
        return getattr(self._orig, name)


def _begin_run_log_tee(
    config: "ForgeConfig",
    logger: "StructuredLogger",
    task_slug: str,
    log_dir: "Path | None" = None,
) -> "tuple[object, object] | None":
    """Open per-run log file and install tee on sys.stderr.

    Returns (fh, orig_stderr) on success, or None if logging is disabled or
    the file cannot be opened (best-effort; never raises).
    """
    if not config.log.enabled:
        return None
    if threading.current_thread() is not threading.main_thread():
        return None  # skip in worker threads; parallel sprints avoid cross-story tee stacking
    try:
        if log_dir is not None:
            per_run_path = log_dir / f"run-{logger._run_id}.log"
        else:
            per_run_path = logger._log_path.parent / f"{task_slug}-{logger._run_id}.log"
        per_run_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(per_run_path, "a", encoding="utf-8")  # noqa: SIM115
        orig = _sys.stderr
        _sys.stderr = _TeeStderr(orig, fh)
        return (fh, orig)
    except Exception:
        return None


def _end_run_log_tee(tee_state: "tuple[object, object] | None") -> None:
    """Restore sys.stderr and close the per-run log file."""
    if tee_state is None:
        return
    fh, orig = tee_state
    try:
        _sys.stderr = orig
        fh.close()
    except Exception:
        pass
