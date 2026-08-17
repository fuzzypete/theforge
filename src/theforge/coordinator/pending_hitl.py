"""Pending-file-based HITL gate implementations."""

from __future__ import annotations

import time
from dataclasses import replace as _dc_replace
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
    from theforge.notify_backends import format_pending_decision_notification, send_notifications

    p1 = sum(1 for f in parsed_review.findings if f.severity == "P1")
    p2 = sum(1 for f in parsed_review.findings if f.severity == "P2")
    elapsed = time.monotonic() - task_start
    # Bound the wait by the story budget containing it *before* writing the file,
    # so the timeout_at an operator reads is the window the poller will actually
    # honour rather than the unbounded configured one (#2333).
    timeout_seconds = int(
        _pending.bounded_gate_wait(
            config.notifications.human_review_timeout_seconds, "HUMAN_REVIEW"
        )
    )

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

    pending_path = _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase="HUMAN_REVIEW",
        reason=reason,
        options=["approve", "reject", "escalate", "extend"],
        timeout_seconds=timeout_seconds,
        project_root=project_root,
    )

    pending_record = _pending.read_pending(_eff_run_id, project_root=project_root) or {}
    send_notifications(
        config,
        title=f"TheForge: review needed — {task.slug} (HUMAN_REVIEW)",
        body=format_pending_decision_notification(pending_record, pending_path=pending_path),
    )

    _poll_start = time.monotonic()
    decision, _decided_at = _pending.poll_pending(
        _eff_run_id,
        timeout_seconds,
        project_root=project_root,
        phase_label="HUMAN_REVIEW",
        # Bounded above, before write_pending: the window in the file and
        # the window the poller honours are the same number.
        already_bounded=True,
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

    When ``advisory`` is a valid report, the pending file presents the
    executable subset of the action taxonomy as the options and embeds the
    report + evidence packet as structured payload; the operator must select one
    of those actions.

    The presented options are the taxonomy filtered to what the current state can
    actually perform (see :mod:`.escalate_actions`) — ``accept`` is withheld when
    no approvable reviewer result is retained. Withheld actions and their reasons
    are named in the reason text and recorded under ``extra["omitted_actions"]``.

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
    from theforge.notify_backends import format_pending_decision_notification, send_notifications

    from .escalate_actions import available_escalate_actions, omitted_actions_note

    timeout_seconds = int(
        _pending.bounded_gate_wait(config.notifications.human_review_timeout_seconds, "ESCALATE")
    )
    approve_count = sum(1 for v in reviewer_verdicts.values() if v == "APPROVE")
    total_count = len(reviewer_verdicts)
    verdict_line = (
        f"{approve_count}/{total_count} reviewers APPROVE" if reviewer_verdicts else "no verdicts"
    )
    gate_line = gate_result or ""

    _eff_run_id = run_id or task.slug
    project_root = getattr(config, "project_root", None)

    advisory_ok = advisory is not None and advisory.ok
    advisory_has_recommendation = advisory_ok and bool((advisory.recommendation or "").strip())
    escalate_reason_text = escalate_reason or state.escalate_reason or ""

    # Offer only what this state can carry out. An action presented here is a
    # promise the gate keeps: presenting one it cannot perform converts the
    # operator's wait into a wasted one and forces a substitution nobody chose
    # (#2300). Withheld actions are named with their reason rather than silently
    # dropped, so the absence is legible.
    options, omitted_actions = available_escalate_actions(state, ACTION_TAXONOMY)
    omitted_note = omitted_actions_note(omitted_actions)

    if advisory_ok and advisory_has_recommendation:
        assert advisory is not None
        display_advisory = advisory
        recommendation_withheld = advisory.recommendation in omitted_actions
        if omitted_actions:
            display_advisory = _dc_replace(
                advisory,
                options=[o for o in advisory.options if o.action not in omitted_actions],
                # A recommendation the operator cannot select is not a
                # recommendation; drop it from the rendered/persisted payload and
                # explain the omission in the reason text below.
                recommendation="" if recommendation_withheld else advisory.recommendation,
            )
        extra: dict = {
            "advisory": display_advisory.to_dict(),
            "evidence_packet": state.advisory_packet,
            "decision_required": True,
        }
        reason = render_advisory_for_pending(
            display_advisory, _build_packet_stub(state, escalate_reason)
        )
        if omitted_actions:
            extra["omitted_actions"] = dict(omitted_actions)
            withheld_rec_note = (
                f"NOTE: the advisor recommended {advisory.recommendation!r}, but this run "
                "cannot perform it, so it is not offered."
                if recommendation_withheld
                else ""
            )
            reason = "\n".join(filter(None, [reason, "", omitted_note, withheld_rec_note]))
    elif advisory_ok:
        assert advisory is not None
        extra = {
            "decision_required": True,
            "advisory_no_recommendation": True,
            "advisory": advisory.to_dict(),
            "evidence_packet": state.advisory_packet,
        }
        if omitted_actions:
            extra["omitted_actions"] = dict(omitted_actions)
        reason = "\n".join(
            filter(
                None,
                [
                    "ESCALATION — advisor completed but recommended no action; select an action.",
                    advisory.rationale,
                    verdict_line,
                    gate_line,
                    escalate_reason_text[:200],
                    omitted_note,
                ],
            )
        )
    else:
        # No usable advisory (agent failed / malformed). Still require an explicit
        # operator decision — surface the performable taxonomy plus the raw
        # escalation context.
        extra = {"decision_required": True, "advisory_unavailable": True}
        if omitted_actions:
            extra["omitted_actions"] = dict(omitted_actions)
        launch_failed = bool(getattr(state, "advisory_launch_failure", False))
        unavailable_reason = getattr(state, "advisory_unavailable_reason", None)
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
        elif unavailable_reason:
            extra["advisory_unavailable_reason"] = unavailable_reason
            headline = [
                "ESCALATION — advisory input missing; select an action.",
                "The advisor role could not produce a usable structured report for this gate.",
                f"reason: {unavailable_reason}",
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
                    escalate_reason_text[:200],
                    omitted_note,
                ],
            )
        )

    _cu._log("─── Pending Escalate Gate ───")
    _cu._log(f"  Run ID:  {_eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")
    _cu._log(f"  Options: {', '.join(options)}")
    if omitted_actions:
        for _action, _why in sorted(omitted_actions.items()):
            _cu._log(f"  Not offered: {_action} — {_why}")

    pending_path = _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase="ESCALATE",
        reason=reason,
        options=options,
        timeout_seconds=timeout_seconds,
        project_root=project_root,
        extra=extra,
    )

    pending_record = _pending.read_pending(_eff_run_id, project_root=project_root) or {}
    send_notifications(
        config,
        title=f"TheForge: decision needed — {task.slug} (ESCALATE)",
        body=format_pending_decision_notification(pending_record, pending_path=pending_path),
    )

    _poll_start = time.monotonic()
    decision, _decided_at = _pending.poll_pending(
        _eff_run_id,
        timeout_seconds,
        project_root=project_root,
        phase_label="ESCALATE",
        # Bounded above, before write_pending: the window in the file and
        # the window the poller honours are the same number.
        already_bounded=True,
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


def cleanup_escalate_pending(
    task: "TaskStory",
    config: "ForgeConfig",
    run_id: str = "",
) -> None:
    """Remove the escalate gate's pending checkpoint after it has been resolved.

    :func:`_pending_escalate_gate` preserves (and refreshes) the checkpoint on
    timeout so an operator can still act on it. When something else resolves that
    expired gate — today, the opt-in ``retry.escalate_timeout_policy:
    apply_advice`` applying the advisory recommendation (#2279) — the checkpoint
    must go, exactly as it does for an explicit selection: a resolved decision
    that leaves a pending file behind reads as still-awaiting-an-operator.

    Lives here, next to the writer, so the effective run id / project root are
    resolved by the same rule that wrote the file rather than by a caller's
    reconstruction of it.
    """
    from theforge import pending as _pending

    _pending.cleanup_pending(run_id or task.slug, getattr(config, "project_root", None))


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
    from theforge.notify_backends import format_pending_decision_notification, send_notifications

    timeout_seconds = int(
        _pending.bounded_gate_wait(config.plan_review.timeout_seconds, "PLAN_REVIEW")
    )
    first_3_lines = "\n".join(plan_text.splitlines()[:3])
    plan_summary = first_3_lines[:200]
    reason = f"{plan_summary}\nWorktree: .forge/worktrees/{task.slug}"

    _eff_run_id = run_id or task.slug
    project_root = getattr(config, "project_root", None)

    _cu._log("─── Pending Plan Review ───")
    _cu._log(f"  Run ID:  {_eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")

    pending_path = _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase="PLAN_REVIEW",
        reason=reason,
        options=["approve", "regenerate", "abandon"],
        timeout_seconds=timeout_seconds,
        project_root=project_root,
    )

    pending_record = _pending.read_pending(_eff_run_id, project_root=project_root) or {}
    send_notifications(
        config,
        title=f"TheForge: plan ready — {task.slug} (PLAN_REVIEW)",
        body=format_pending_decision_notification(pending_record, pending_path=pending_path),
    )

    _pr_start = time.monotonic()
    state.plan_review_mode = "pending"
    mode = config.plan_review.mode

    decision, _decided_at = _pending.poll_pending(
        _eff_run_id,
        timeout_seconds,
        project_root=project_root,
        phase_label="PLAN_REVIEW",
        # Bounded above, before write_pending: the window in the file and
        # the window the poller honours are the same number.
        already_bounded=True,
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
