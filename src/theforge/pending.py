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

#: Env var carrying the run id of the process that owns this run. Exported by
#: ``detach.export_run_context`` on both the detached and foreground paths.
_OWNER_RUN_ID_ENV = "FORGE_DETACHED_RUN_ID"


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
    # Which run is holding this gate. A checkpoint is keyed by the *story* run
    # id, which two concurrently live sprints can share for the same slug — so
    # the owning sprint run is recorded here, and a status view can tell whose
    # gate it is looking at rather than inferring it from the slug (#2313).
    owner_run_id = os.environ.get(_OWNER_RUN_ID_ENV)
    if owner_run_id:
        data["owner_run_id"] = owner_run_id
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


#: Working seconds a gate must leave its story after a decision arrives, so the
#: answer can actually be acted on inside the story's own deadline.
GATE_DECISION_TAIL_RESERVE_SECONDS = 300

#: However tight the enclosing budget, never offer an operator less than this.
MIN_GATE_WAIT_SECONDS = 60


def bounded_gate_wait(timeout_seconds: float, phase_label: str = "") -> float:
    """Bound a gate's wait by the story budget that contains it (#2333).

    Gate waits are configured globally (``notifications.human_review_timeout_seconds``,
    ``plan_review.timeout_seconds``) and were previously passed straight through,
    so a gate could be told to wait for exactly as long as the enclosing sprint
    worker was allowed to live — a decision nobody could have supplied in time.
    The wait itself is excluded from the story's deadline (see
    :func:`theforge.worker_budget.operator_wait`), so what this bounds is the
    promise: the gate asks for no more than the story can still honour, leaving a
    tail in which the answer can be acted on.

    A no-op outside a sprint worker, where there is no enclosing budget.
    """
    from . import worker_budget as _wb

    budget = _wb.current_worker_budget()
    if budget is None:
        return timeout_seconds
    remaining = budget.remaining()
    allowed = float(
        max(MIN_GATE_WAIT_SECONDS, int(remaining - GATE_DECISION_TAIL_RESERVE_SECONDS))
    )
    if allowed >= timeout_seconds:
        return timeout_seconds
    _cu._log(
        f"  Gate wait bounded {int(timeout_seconds)}s → {int(allowed)}s by the enclosing "
        f"story budget ({_cu._fmt_duration(max(0.0, remaining))} of working time left"
        f" on {budget.slug}{f'; phase {phase_label}' if phase_label else ''})"
    )
    return allowed


def poll_pending(
    run_id: str,
    timeout_seconds: float,
    poll_interval: float = 2.0,
    project_root: Path | None = None,
    phase_label: str = "",
) -> tuple[str, str | None]:
    """Poll the pending file until a decision field appears or timeout expires.

    The whole poll is marked as operator-wait time on the enclosing story budget:
    time the system spends waiting for a decision it asked a human to make is not
    time the worker is unresponsive, and the sprint scheduler credits it back to
    the story deadline rather than charging the story for a wait the system chose
    to begin (#2333).

    Returns (decision, decided_at) or ("timeout", None) on expiry.
    """
    from . import worker_budget as _wb

    effective_timeout = bounded_gate_wait(timeout_seconds, phase_label)
    deadline = time.monotonic() + effective_timeout
    last_log = time.monotonic()

    with _wb.operator_wait(phase_label or "pending decision"):
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
        f"  Pending decision timed out after {_cu._fmt_duration(effective_timeout)}"
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
