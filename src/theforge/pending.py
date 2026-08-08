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
from .pid import _is_pid_alive


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
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a pending decision file for the given run.

    ``extra`` holds structured payload beyond the human-readable ``reason`` (for
    example the escalation advisory report + evidence packet) so an operator or a
    tool can inspect the machine-readable options rather than parsing prose. Keys
    in ``extra`` never override the core fields below.

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
    if extra:
        for key, value in extra.items():
            data.setdefault(key, value)

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
        if not isinstance(data, dict):
            return None

        pid = data.get("pid")
        if pid is None:
            cleanup_pending(run_id, project_root)
            return None

        try:
            owner_pid = int(pid)
        except (TypeError, ValueError):
            cleanup_pending(run_id, project_root)
            return None

        if not _is_pid_alive(owner_pid):
            cleanup_pending(run_id, project_root)
            return None

        return data
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
    deadline = time.monotonic() + timeout_seconds
    last_log = time.monotonic()

    while time.monotonic() < deadline:
        data = read_pending(run_id, project_root)
        if isinstance(data, dict) and data.get("decision"):
            decision = str(data["decision"]).strip()
            decided_at = data.get("decided_at")
            _cu._log(f"  Pending decision received: {decision!r}")
            return decision, decided_at

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

    # Wording is deliberately neutral about what happens next: this poller is
    # shared by the human-review, plan-review, and escalate gates, and none of
    # them auto-escalates on expiry any more. What an expiry MEANS is the
    # caller's decision (preserve for an operator, or apply advice under
    # retry.escalate_timeout_policy), so the poller reports only the fact (#2279).
    _cu._log(
        f"  Pending decision timed out after {_cu._fmt_duration(timeout_seconds)}"
        " — no decision received"
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
        try:
            owner_pid = int(pid)
        except (TypeError, ValueError):
            cleanup_pending(run_id, project_root)
            removed += 1
            continue

        if not _is_pid_alive(owner_pid):
            cleanup_pending(run_id, project_root)
            removed += 1

    return removed
