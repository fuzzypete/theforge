"""Shared helpers for status run classification and watch startup."""

from __future__ import annotations

import json
import time
from pathlib import Path

from theforge.sprint.status_reader import find_sprint_summary

WATCH_SPRINT_STARTUP_GRACE_SECONDS = 5.0
WATCH_SPRINT_STARTUP_POLL_SECONDS = 0.1


def is_sprint_run(run_id: str, project_root: Path) -> bool:
    """Return True when run_id belongs to a sprint run.

    Also checks redirect files: a sprint re-exec writes its predecessor's
    ``.redirect`` before the new worker creates ``.state``, so during that
    startup window we still classify the live successor as a sprint when the
    predecessor had sprint state.
    """
    state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
    if state_path.exists():
        return True
    if find_sprint_summary(run_id, project_root) is not None:
        return True

    runs_dir = project_root / ".forge" / "runs"
    # Sprint marker is written at detach time, before any sprint state exists.
    # Without this check, watch mode falls back to a one-shot snapshot during
    # the preflight/DAG-build window when only the PID file is present.
    if (runs_dir / f"{run_id}.sprint").exists():
        return True
    for redirect_file in runs_dir.glob("*.redirect"):
        try:
            data = json.loads(redirect_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if data.get("new_run_id") != run_id:
            continue
        predecessor_state = runs_dir / f"{redirect_file.stem}.state"
        if predecessor_state.exists():
            return True
    return False


def await_watchable_sprint_run(
    run_id: str,
    project_root: Path,
    *,
    grace_seconds: float = WATCH_SPRINT_STARTUP_GRACE_SECONDS,
    poll_seconds: float = WATCH_SPRINT_STARTUP_POLL_SECONDS,
) -> bool:
    """Wait briefly for a live run to become recognizable as a sprint."""
    if is_sprint_run(run_id, project_root):
        return True

    pid_file = project_root / ".forge" / "runs" / f"{run_id}.pid"
    if not pid_file.exists():
        return False

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        time.sleep(max(0.0, poll_seconds))
        if is_sprint_run(run_id, project_root):
            return True
        if not pid_file.exists():
            return False

    return is_sprint_run(run_id, project_root)


def live_sprint_run_ids(project_root: Path) -> list[str]:
    """Return run_ids for all currently-live sprint runs."""
    from theforge import detach

    run_ids: list[str] = []
    for run in detach.list_active_runs(project_root):
        run_id = str(run["run_id"])
        if is_sprint_run(run_id, project_root):
            run_ids.append(run_id)
    return run_ids
