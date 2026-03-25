"""Pending decision file interface for HITL coordination.

The coordinator writes .forge/pending/<run-id>.yaml when a human decision
is needed. Anything can write the decision field — the CLI, a webhook, a
human editor. The coordinator polls the file until a decision appears or
the timeout expires.
"""

from __future__ import annotations

import datetime
import os
import time
from pathlib import Path
from typing import Any

import yaml

from .coordinator import util as _cu


def _pending_dir(project_root: Path | None = None) -> Path:
    """Return .forge/pending/ relative to project root or cwd."""
    base = project_root or Path.cwd()
    return base / ".forge" / "pending"


def write_pending(
    run_id: str,
    story: str,
    phase: str,
    reason: str,
    options: list[str],
    timeout_seconds: int,
    project_root: Path | None = None,
) -> Path:
    """Write a pending decision file for the given run.

    Returns the path to the created file.
    """
    pending_dir = _pending_dir(project_root)
    pending_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    timeout_at = now + datetime.timedelta(seconds=timeout_seconds)

    data: dict[str, Any] = {
        "run_id": run_id,
        "story": story,
        "phase": phase,
        "reason": reason,
        "options": options,
        "created_at": now.isoformat(),
        "timeout_at": timeout_at.isoformat(),
        "pid": os.getpid(),
    }

    path = pending_dir / f"{run_id}.yaml"
    path.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")
    _cu._log(f"  Pending decision written: {path}")
    return path


def read_pending(run_id: str, project_root: Path | None = None) -> dict[str, Any] | None:
    """Load and return the pending YAML dict, or None if not found."""
    path = _pending_dir(project_root) / f"{run_id}.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def poll_pending(
    run_id: str,
    timeout_seconds: int,
    poll_interval: float = 2.0,
    project_root: Path | None = None,
) -> tuple[str, str | None]:
    """Poll the pending file until a decision field appears or timeout expires.

    Returns (decision, decided_at) or ("timeout", None) on expiry.
    """
    path = _pending_dir(project_root) / f"{run_id}.yaml"
    deadline = time.monotonic() + timeout_seconds
    last_log = time.monotonic()

    while time.monotonic() < deadline:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("decision"):
                decision = str(data["decision"]).strip()
                decided_at = data.get("decided_at")
                _cu._log(f"  Pending decision received: {decision!r}")
                return decision, decided_at
        except Exception:
            pass

        now = time.monotonic()
        if now - last_log >= 60:
            remaining = max(0, deadline - now)
            _cu._log(
                f"  Waiting for pending decision on {run_id}"
                f" ({_cu._fmt_duration(remaining)} remaining)"
            )
            last_log = now

        sleep_secs = min(poll_interval, max(0.0, deadline - time.monotonic()))
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    _cu._log(
        f"  Pending decision timed out after {_cu._fmt_duration(timeout_seconds)}"
        " — auto-escalating"
    )
    return "timeout", None


def resolve_pending(
    run_id: str,
    decision: str,
    project_root: Path | None = None,
) -> bool:
    """Write decision + decided_at into the pending file.

    Returns True if the file existed and was updated, False otherwise.
    """
    path = _pending_dir(project_root) / f"{run_id}.yaml"
    if not path.exists():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        data["decision"] = decision
        data["decided_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        path.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")
        return True
    except Exception:
        return False


def cleanup_pending(run_id: str, project_root: Path | None = None) -> None:
    """Remove the pending file for the given run_id."""
    path = _pending_dir(project_root) / f"{run_id}.yaml"
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def list_pending(project_root: Path | None = None) -> list[dict[str, Any]]:
    """Return list of all pending YAML files with parsed contents."""
    pending_dir = _pending_dir(project_root)
    if not pending_dir.exists():
        return []
    results = []
    for p in sorted(pending_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                results.append(data)
        except Exception:
            pass
    return results


def cleanup_stale(project_root: Path | None = None) -> int:
    """Remove pending files whose timeout has passed and no decision was written.

    Also removes files for PIDs that are no longer running.
    Returns the number of files removed.
    """
    removed = 0
    for entry in list_pending(project_root):
        run_id = entry.get("run_id", "")
        if not run_id:
            continue

        # If already decided, leave it — coordinator will clean up
        if entry.get("decision"):
            continue

        # Remove if past timeout_at
        timeout_at_str = entry.get("timeout_at")
        if timeout_at_str:
            try:
                timeout_at = datetime.datetime.fromisoformat(timeout_at_str)
                now = datetime.datetime.now(datetime.timezone.utc)
                if now > timeout_at:
                    cleanup_pending(run_id, project_root)
                    removed += 1
                    continue
            except Exception:
                pass

        # Remove if PID is no longer running
        pid = entry.get("pid")
        if pid is not None:
            try:
                os.kill(int(pid), 0)
            except (ProcessLookupError, PermissionError):
                # ProcessLookupError: no such process
                # PermissionError: process exists but we can't signal it
                # Only remove on ProcessLookupError
                pass
            except OSError as exc:
                import errno

                if exc.errno == errno.ESRCH:
                    cleanup_pending(run_id, project_root)
                    removed += 1

    return removed
