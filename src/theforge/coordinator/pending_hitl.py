"""Pending-file-based HITL gate implementations."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import util as _cu

if TYPE_CHECKING:
    from theforge.config import ForgeConfig
    from theforge.escalation_advisor import AdvisoryReport, EvidencePacket
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
        f"{parsed_review.verdict} ({p1} P1, {p2} P2) — "
        f"{_cu._fmt_cost_total(state.total_cost_measured, state.total_cost)} "
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
    advisory: "AdvisoryReport | None" = None,
) -> str:
    """Pending-file-based escalate gate with a fresh-context advisory report.

    When ``advisory`` is a valid report, the pending file presents the fixed
    action taxonomy (Accept / Land-core-defer-edges / Redirect / Decompose /
    Elevate / Defer-or-Abandon) as the options and embeds the report + evidence
    packet as structured payload; the operator must select one of those actions.

    The max-cycles path no longer auto-rejects on timeout: a timeout returns
    ``"timeout"`` so the caller preserves the escalation for an operator decision
    rather than discarding the work. Returns the selected taxonomy action (or a
    legacy ``approve``/``reject``/``continue`` value), or ``"timeout"``.
    """
    from theforge import pending as _pending
    from theforge.escalation_advisor import (
        ACTION_TAXONOMY,
        render_advisory_for_pending,
    )
    from theforge.notify_backends import send_notifications

    timeout_seconds = config.notifications.human_review_timeout_seconds
    approve_count = sum(1 for v in reviewer_verdicts.values() if v == "APPROVE")
    total_count = len(reviewer_verdicts)
    verdict_line = (
        f"{approve_count}/{total_count} reviewers APPROVE" if reviewer_verdicts else "no verdicts"
    )
    gate_line = gate_result or ""

    _eff_run_id = run_id or task.slug
    project_root = getattr(config, "project_root", None)

    advisory_ok = advisory is not None and advisory.ok
    if advisory_ok:
        options = list(ACTION_TAXONOMY)
        extra: dict = {
            "advisory": advisory.to_dict(),
            "evidence_packet": state.advisory_packet,
            "decision_required": True,
        }
        reason = render_advisory_for_pending(advisory, _build_packet_stub(state, escalate_reason))
    else:
        # No usable advisory (agent failed / malformed). Still require an explicit
        # operator decision — surface the taxonomy plus the raw escalation context.
        options = list(ACTION_TAXONOMY)
        extra = {"decision_required": True, "advisory_unavailable": True}
        launch_failed = bool(getattr(state, "advisory_launch_failure", False))
        if launch_failed:
            # An advisor that never started is a defect in forge's own
            # configuration, not an investigation that reached no conclusion. Say
            # which one happened, why, and that it cost nothing — the operator is
            # choosing unaided either way, but only one of the two is worth
            # repairing and retrying (#2164).
            launch_reason = (
                getattr(state, "advisory_launch_reason", None)
                or "the advisor process exited before contacting the model"
            )
            extra["advisory_launch_failure"] = True
            extra["advisory_launch_reason"] = launch_reason
            extra["advisory_cost_usd"] = 0.0
            headline = [
                "ESCALATION — the advisory agent FAILED TO LAUNCH; select an action.",
                (
                    "This is a forge configuration / tool-invocation defect, NOT an "
                    "advisor that ran and reached no conclusion: the model was never "
                    "contacted and $0.00 was spent. Repairing the launch defect and "
                    "re-running the escalation can still produce advice."
                ),
                f"launch failure: {launch_reason}",
            ]
        else:
            headline = ["ESCALATION — advisory report unavailable; select an action."]
        reason = "\n".join(
            filter(
                None,
                [
                    *headline,
                    verdict_line,
                    gate_line,
                    escalate_reason[:200],
                ],
            )
        )

    _cu._log("─── Pending Escalate Gate ───")
    _cu._log(f"  Run ID:  {_eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")
    _cu._log(f"  Options: {', '.join(options)}")

    _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase="ESCALATE",
        reason=reason,
        options=options,
        timeout_seconds=timeout_seconds,
        project_root=project_root,
        extra=extra,
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

    waited_str = _cu._fmt_duration(waited)
    if decision == "timeout":
        # Contract change (#1664): no auto-reject. PRESERVE the pending checkpoint
        # so the operator can still select an action — do NOT delete the file here
        # (deleting it would leave the operator with nothing to resolve). Rewrite
        # it with a refreshed window and an awaiting-decision marker so the stale
        # sweeper does not immediately remove the still-actionable record and the
        # advisory report + taxonomy options remain available for selection.
        extra["timed_out_awaiting_decision"] = True
        preserve_reason = (
            "ESCALATION TIMED OUT — awaiting an operator action selection "
            "(no auto-reject).\n\n" + reason
        )
        _pending.write_pending(
            run_id=_eff_run_id,
            story=task.slug,
            phase="ESCALATE",
            reason=preserve_reason,
            options=options,
            timeout_seconds=timeout_seconds,
            project_root=project_root,
            extra=extra,
        )
        _cu._log(
            f"  Escalate gate timed out after {waited_str} — pending checkpoint preserved"
            f" for operator decision (no auto-reject): {_eff_run_id}"
        )
        return "timeout"

    # A decision was made — clean up the resolved pending file and return it.
    _pending.cleanup_pending(_eff_run_id, project_root)
    _cu._log(f"  Escalate gate decision: {decision!r} (waited {waited_str})")
    return decision


def _build_packet_stub(state: "_cs.CoordinatorState", escalate_reason: str) -> "EvidencePacket":
    """Reconstruct a lightweight EvidencePacket from the serialized state payload.

    ``render_advisory_for_pending`` only reads a few packet fields (story name,
    issue ref, escalation reason, cycle count); rebuild those from the audit dict
    stored on state so rendering does not require threading the live packet.
    """
    from theforge.escalation_advisor import CycleEvidence, EvidencePacket

    payload = state.advisory_packet or {}
    cycles = [
        CycleEvidence(
            cycle=int(c.get("cycle") or 0),
            verdict=str(c.get("verdict") or ""),
            summary=str(c.get("summary") or ""),
            findings=list(c.get("findings") or []),
        )
        for c in (payload.get("cycles") or [])
    ]
    return EvidencePacket(
        story_name=str(payload.get("story_name") or ""),
        issue_ref=str(payload.get("issue_ref") or ""),
        issue_body=str(payload.get("issue_body") or ""),
        acceptance_criteria=list(payload.get("acceptance_criteria") or []),
        cycles=cycles,
        reviewer_verdicts=dict(payload.get("reviewer_verdicts") or {}),
        final_verdict=payload.get("final_verdict"),
        dev_diff=str(payload.get("dev_diff") or ""),
        test_failures=str(payload.get("test_failures") or ""),
        escalation_reason=str(payload.get("escalation_reason") or escalate_reason),
    )


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
