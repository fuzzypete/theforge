"""Pending-file-based HITL gate implementations."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import util as _cu

if TYPE_CHECKING:
    from theforge.config import ForgeConfig
    from theforge.review import ReviewResult
    from theforge.task import TaskStory

    from . import state as _cs


def _pending_human_review(
    state: "_cs.CoordinatorState",
    parsed_review: "ReviewResult",
    workspace_path: "Path",
    branch_name: str,
    task: "TaskStory",
    config: "ForgeConfig",
    task_start: float,
    run_id: str = "",
) -> tuple[str, str | None]:
    """Pending-file-based human review decision.

    Writes a pending file, sends notifications, polls for decision.
    Returns (decision, feedback) where decision is one of:
    approve | reject | escalate | extend | timeout
    """
    from theforge import pending as _pending
    from theforge.notify_backends import send_notifications

    p1 = sum(1 for f in parsed_review.findings if f.severity == "P1")
    p2 = sum(1 for f in parsed_review.findings if f.severity == "P2")
    elapsed = time.monotonic() - task_start
    timeout_seconds = config.notifications.human_review_timeout_seconds

    reason = (
        f"{parsed_review.verdict} ({p1} P1, {p2} P2) — ${state.total_cost:.2f} "
        f"{_cu._fmt_duration(elapsed)}\n{parsed_review.summary[:120]}\nBranch: {branch_name}"
    )

    _eff_run_id = run_id or task.slug
    project_root = getattr(config, "project_root", None)

    _cu._log("─── Pending Human Review ───")
    _cu._log(f"  Run ID:  {_eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")

    _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase="HUMAN_REVIEW",
        reason=reason,
        options=["approve", "reject", "escalate", "extend"],
        timeout_seconds=timeout_seconds,
        project_root=project_root,
    )

    send_notifications(
        config,
        title=f"TheForge: review needed — {task.slug}",
        body=reason[:300],
    )

    _poll_start = time.monotonic()
    decision, _decided_at = _pending.poll_pending(
        _eff_run_id, timeout_seconds, project_root=project_root
    )
    state.human_review_waited_seconds = time.monotonic() - _poll_start
    state.human_review_mode = "pending"

    _pending.cleanup_pending(_eff_run_id, project_root)

    if decision == "timeout":
        _cu._log(
            f"Pending review timed out after"
            f" {_cu._fmt_duration(state.human_review_waited_seconds)}"
        )
        return "timeout", None

    waited_str = _cu._fmt_duration(state.human_review_waited_seconds or 0)
    _cu._log(f"Pending review decision: {decision!r} (waited {waited_str})")
    return decision, None


def _pending_escalate_gate(
    state: "_cs.CoordinatorState",
    task: "TaskStory",
    config: "ForgeConfig",
    escalate_reason: str,
    reviewer_verdicts: dict[str, str],
    gate_result: "str | None",
    run_id: str = "",
) -> str:
    """Pending-file-based escalate gate. Returns 'approve' | 'reject' | 'continue'."""
    from theforge import pending as _pending
    from theforge.notify_backends import send_notifications

    timeout_seconds = config.notifications.human_review_timeout_seconds
    approve_count = sum(1 for v in reviewer_verdicts.values() if v == "APPROVE")
    total_count = len(reviewer_verdicts)
    verdict_line = (
        f"{approve_count}/{total_count} reviewers APPROVE" if reviewer_verdicts else "no verdicts"
    )
    gate_line = gate_result or ""
    reason = "\n".join(filter(None, [verdict_line, gate_line, escalate_reason[:120]]))

    _eff_run_id = run_id or task.slug
    project_root = getattr(config, "project_root", None)

    _cu._log("─── Pending Escalate Gate ───")
    _cu._log(f"  Run ID:  {_eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")

    _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase="ESCALATE",
        reason=reason,
        options=["approve", "reject", "continue"],
        timeout_seconds=timeout_seconds,
        project_root=project_root,
    )

    send_notifications(
        config,
        title=f"TheForge: ESCALATE — {task.slug}",
        body=reason[:300],
    )

    _poll_start = time.monotonic()
    decision, _decided_at = _pending.poll_pending(
        _eff_run_id, timeout_seconds, project_root=project_root
    )
    waited = time.monotonic() - _poll_start
    state.human_review_waited_seconds = (state.human_review_waited_seconds or 0.0) + waited

    _pending.cleanup_pending(_eff_run_id, project_root)

    waited_str = _cu._fmt_duration(waited)
    if decision == "timeout":
        _cu._log(f"  Escalate gate timed out after {waited_str} — auto-rejecting")
        return "reject"

    _cu._log(f"  Escalate gate decision: {decision!r} (waited {waited_str})")
    if decision in ("approve", "reject", "continue"):
        return decision
    return "reject"


def _pending_plan_review(
    state: "_cs.CoordinatorState",
    plan_text: str,
    workspace_path: "Path",
    task: "TaskStory",
    config: "ForgeConfig",
    run_id: str = "",
) -> str:
    """Pending-file-based plan review. Returns 'approve' | 'regenerate' | 'abandon'."""
    from theforge import pending as _pending
    from theforge.notify_backends import send_notifications

    timeout_seconds = config.plan_review.timeout_seconds
    first_3_lines = "\n".join(plan_text.splitlines()[:3])
    plan_summary = first_3_lines[:200]
    reason = f"{plan_summary}\nWorktree: .forge/worktrees/{task.slug}"

    _eff_run_id = run_id or task.slug
    project_root = getattr(config, "project_root", None)

    _cu._log("─── Pending Plan Review ───")
    _cu._log(f"  Run ID:  {_eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")

    _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase="PLAN_REVIEW",
        reason=reason,
        options=["approve", "regenerate", "abandon"],
        timeout_seconds=timeout_seconds,
        project_root=project_root,
    )

    send_notifications(
        config,
        title=f"TheForge: plan ready — {task.slug}",
        body=reason[:300],
    )

    _pr_start = time.monotonic()
    state.plan_review_mode = "pending"
    mode = config.plan_review.mode

    decision, _decided_at = _pending.poll_pending(
        _eff_run_id, timeout_seconds, project_root=project_root
    )
    state.plan_review_waited_seconds = time.monotonic() - _pr_start

    _pending.cleanup_pending(_eff_run_id, project_root)

    if decision == "timeout":
        waited_str = _cu._fmt_duration(state.plan_review_waited_seconds or 0)
        if mode == "advisory":
            _cu._log(f"  ⚠ PLAN_REVIEW   advisory timeout — auto-approving after {waited_str}")
            state.plan_review_mode = "advisory-timeout"
            return "approve"
        else:
            _cu._log(f"  ✗ PLAN_REVIEW   blocking timeout after {waited_str} — abandoning")
            return "abandon"

    waited_str = _cu._fmt_duration(state.plan_review_waited_seconds or 0)
    _cu._log(f"  Pending plan review decision: {decision!r} (waited {waited_str})")
    if decision in ("approve", "regenerate", "abandon"):
        return decision
    return "abandon"
