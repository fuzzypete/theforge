"""REVIEW phase — escalate gate handling."""

from __future__ import annotations

from pathlib import Path

from theforge.config import ForgeConfig
from theforge.task import TaskStory as TaskSpec  # noqa: F401

from .logging import StructuredLogger
from .notify import (
    _escalate_gate_interactive,
    _escalate_gate_remote,
    _escalate_notify,
    _is_pending_file_mode,
    _is_remote_mode,
    _pending_escalate_gate,
)
from .state import CoordinatorResult, CoordinatorState, Phase
from .util import _log


def _build_reviewer_verdicts(state: CoordinatorState) -> dict[str, str]:
    """Build a profile_name → verdict dict from the last cycle's reviewer results."""
    verdicts: dict[str, str] = {}
    for name, rr in state.last_cycle_reviewer_results:
        verdicts[name] = rr.verdict
    # Fill in FAIL for reviewers that appear in the last cycle metadata but not in named_parsed
    if state.review_cycle_metadata:
        last_meta = state.review_cycle_metadata[-1]
        for failed_name in last_meta.failed:
            if failed_name not in verdicts:
                verdicts[failed_name] = "FAIL"
    return verdicts


def _run_escalate_gate(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    auto_merge: bool,
    notify: bool,
    logger: "StructuredLogger | None",
    run_id: str = "",
) -> "CoordinatorResult | None":
    """HITL decision gate at review-related ESCALATE exit points.

    Returns:
        CoordinatorResult — approve or reject decision (caller should return it).
        None — continue decision (caller should reset phase to REVIEW, decrement
               review_cycle by 1 if it was already incremented, and loop).
    """
    import sys

    from .phase_review_finalize import _append_cycle_history, _finalize_approve

    escalate_policy = config.retry.escalate_policy
    reviewer_verdicts = _build_reviewer_verdicts(state)
    gate_result: str | None = None
    if state.gate_decisions:
        gate_result = state.gate_decisions[-1]

    escalate_reason = state.error or "ESCALATE"

    def _make_escalate_result() -> CoordinatorResult:
        state.escalate_decision = "reject"
        state.escalate_reason = escalate_reason
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error or escalate_reason,
        )

    # Policy: reject — preserve current behavior without prompting
    if escalate_policy == "reject":
        state.escalate_decision = "reject"
        state.escalate_reason = escalate_reason
        return _make_escalate_result()

    # Policy: auto_approve — short-circuit when gate passed and majority approved
    if escalate_policy == "auto_approve":
        if state.review_results:
            approve_count = sum(1 for v in reviewer_verdicts.values() if v == "APPROVE")
            total_count = max(len(reviewer_verdicts), 1)
            majority_approved = approve_count > total_count / 2
            gate_passed = gate_result is not None and "PASS" in gate_result.upper()
            if majority_approved and gate_passed:
                _log(
                    f"  auto_approve: {approve_count}/{total_count} reviewers APPROVE"
                    f" + gate PASS — approving"
                )
                state.escalate_decision = "approve"
                state.escalate_reason = escalate_reason
                _append_cycle_history(state, state.review_results[-1])
                return _finalize_approve(
                    state,
                    config,
                    task,
                    state.review_results[-1],
                    workspace_path,
                    branch_name,
                    task_start,
                    auto_merge=auto_merge,
                    notify=notify,
                    logger=logger,
                    review_cost=state.total_review_cost,
                    review_elapsed=0.0,
                    message=(
                        f"Task '{task.name}' completed. "
                        f"Human auto-approved via escalate gate "
                        f"after {state.review_cycle} cycle(s). "
                    ),
                    run_id=run_id,
                )

    # Determine interaction method
    if _is_pending_file_mode(notify, config):
        decision = _pending_escalate_gate(
            state, task, config, escalate_reason, reviewer_verdicts, gate_result, run_id=run_id
        )
    elif _is_remote_mode(notify, config):
        decision = _escalate_gate_remote(
            state, task, config, escalate_reason, reviewer_verdicts, gate_result
        )
    elif sys.stdin.isatty():
        decision = _escalate_gate_interactive(
            state, escalate_reason, reviewer_verdicts, gate_result
        )
    else:
        # No interaction method available — fall through to reject
        _log("  No interaction method available for escalate gate — rejecting (policy=prompt)")
        decision = "reject"

    state.escalate_reason = escalate_reason

    if decision == "approve":
        if not state.review_results:
            _log("  ⚠ Approve requested but no review results available — rejecting instead")
            state.escalate_decision = "reject"
            return _make_escalate_result()
        state.escalate_decision = "approve"
        _append_cycle_history(state, state.review_results[-1])
        return _finalize_approve(
            state,
            config,
            task,
            state.review_results[-1],
            workspace_path,
            branch_name,
            task_start,
            auto_merge=auto_merge,
            notify=notify,
            logger=None,
            review_cost=state.total_review_cost,
            review_elapsed=0.0,
            message=(
                f"Task '{task.name}' completed. "
                f"Human approved via escalate gate after {state.review_cycle} cycle(s). "
            ),
            run_id=run_id,
        )

    if decision == "continue":
        state.escalate_decision = "continue"
        _log("  Escalate gate: continue — granting one more review cycle")
        state.phase = Phase.REVIEW
        return None

    # reject or any unrecognised decision
    state.escalate_decision = "reject"
    return _make_escalate_result()
