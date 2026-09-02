"""REVIEW phase handler: pool execution, verdict routing, escalation, review-only mode."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace as _dc_replace
from enum import Enum, auto
from pathlib import Path

from theforge.assignment import (
    MECHANISM_PERSISTENT_P1_DEV_ESCALATION,
    MECHANISM_RUN_SCOPED_RESET,
)
from theforge.config import (
    ESCALATE_TIMEOUT_APPLY_ADVICE,
    ESCALATE_TIMEOUT_PRESERVE,
    ForgeConfig,
    apply_model_info,
)
from theforge.coordinator.context_scope import plan_file_list
from theforge.escalation_advisor import (
    ACTION_FORGE_OPERATIONS,
    ACTION_LABELS,
    ACTION_TAXONOMY,
    AdvisoryReport,
    action_disposition,
)
from theforge.review import (
    ReviewFinding,
    ReviewResult,
    _best_individual_result,
    review_to_dev_handoff,
)
from theforge.review_topology import detect_topology_walk
from theforge.symptom_test_classifier import escalate_symptom_test_findings
from theforge.task import ContextAssembler, TaskStory, build_review_prompt

from . import story_budget as _story_budget
from .batch_diff import BatchReviewContext, batch_member_story_diff, latest_dev_handoff
from .commit_guard import _has_commits_ahead_of_base
from .completion import _append_cycle_history, _finalize_approve
from .diff_grounding import (
    GroundingResult,
    StoryDiff,
    ground_p1_records,
    is_diff_grounded,
)
from .escalate_actions import (
    ACCEPT_UNAVAILABLE_REASON,
    NAMED_ACTION_UNAVAILABLE_REASON,
    approvable_review_result,
    available_escalate_actions,
)
from .escalation_advisor_flow import run_escalation_advisor
from .gate_contradiction import asserts_gate_verifiable_failure
from .gate_green_salvage import record_gate_green_checkpoint as _record_gate_green_checkpoint
from .logging import StructuredLogger
from .notify import (
    _escalate_gate_interactive,
    _escalate_notify,
    _human_review,
    _is_pending_file_mode,
    _is_remote_mode,
    _ntfy_done_notify,
)
from .pending_hitl import (
    _pending_escalate_gate,
    _pending_human_review,
    cleanup_escalate_pending,
)
from .preflight import (
    _escalate_dev_model,
    _find_registry_key_for_profile,
    _has_persistent_p1,
    _persistent_p1_descriptions,
)
from .remote_gates import (
    _escalate_gate_remote,
    _remote_human_review,
)
from .resume_persistence import save_resume_record
from .review_context import (
    _get_commit_diffs,
    _get_commit_log,
    _get_dev_notes,
    _get_diff_content,
    _get_diff_stat,
    _get_handoff_commit_warning,
    _get_handoff_content,
    _latest_forge_handoff_path,
    gate_profile_prompt_kwargs,
    hard_convention_review_kwargs,
)
from .review_pool import _run_review_pool
from .run_setup import save_trajectory_state
from .state import (
    ADVICE_APPLIED,
    ADVICE_ELEVATE,
    ADVICE_LAUNCH_FAILURE,
    ADVICE_NO_RECOMMENDATION,
    ADVICE_NOT_PERFORMABLE,
    ADVICE_POLICY_PRESERVE,
    ADVICE_UNAVAILABLE,
    ADVICE_UNPARSEABLE,
    ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT,
    ESCALATE_SOURCE_NO_INTERACTION,
    ESCALATE_SOURCE_OPERATOR,
    ESCALATE_SOURCE_OPERATOR_DECLINED,
    ESCALATE_SOURCE_POLICY_AUTO_APPROVE,
    ESCALATE_SOURCE_POLICY_REJECT,
    ESCALATE_SOURCE_TIMEOUT_PENDING,
    CoordinatorResult,
    CoordinatorState,
    Phase,
    RetryReason,
    ReviewCycleMetadata,
    ReviewedCommitVerification,
    ReviewIterationTelemetry,
)
from .util import (
    _fmt_cost,
    _fmt_cost_total,
    _fmt_duration,
    _log,
    _log_phase,
    _log_verbose,
    _round_cost,
    _run_shell,
    live_complexity_fields,
    sum_costs,
)


def _perform_dev_model_escalation(
    config: ForgeConfig,
) -> tuple[str, str, ForgeConfig] | None:
    """Bump the dev model to the next higher-capability model in the escalation chain.

    Returns (old_model_name, new_model_name, new_config) or None when no larger
    model is available. Callers are responsible for updating state flags and emitting
    audit records appropriate to their escalation reason.
    """
    # Pass the registry through as-is: an explicitly empty {} must stay empty
    # rather than collapsing to the built-in default via `... or None`.
    registry = config.model_registry
    curr_key = _find_registry_key_for_profile(config.dev_profile, registry=registry)
    if curr_key is None:
        return None
    next_key = _escalate_dev_model(curr_key, config.models, registry=registry)
    if next_key is None:
        return None
    from theforge.config.models import _resolve_model_info  # noqa: PLC0415

    next_info = _resolve_model_info(next_key, registry=registry)
    old_model = config.dev_profile.model
    new_dev = apply_model_info(config.dev_profile, next_info)
    return old_model, next_info.model, _dc_replace(config, dev_profile=new_dev)


def _record_persistent_p1_dev_escalation(
    state: CoordinatorState,
    *,
    previous_model: str,
    escalated_model: str,
    persistent_descriptions: list[str],
    p1_file: str,
) -> None:
    """Attach the in-run persistent-P1 escalation to the canonical audit block."""
    if not isinstance(state.routing_decision, dict):
        state.routing_decision = {"origin": "preflight"}

    dev_block = state.routing_decision.get("dev")
    if not isinstance(dev_block, dict):
        dev_block = {}
        state.routing_decision["dev"] = dev_block

    dev_block["persistent_p1_dev_escalation"] = {
        "mechanism": MECHANISM_PERSISTENT_P1_DEV_ESCALATION,
        "fired": True,
        "scope": "run",
        "return_path": MECHANISM_RUN_SCOPED_RESET,
        "signal": {
            "kind": "persistent_p1",
            "review_cycle": state.review_cycle,
            "file": p1_file,
            "descriptions": list(persistent_descriptions),
        },
        "model_swap": {
            "from_model": previous_model,
            "to_model": escalated_model,
        },
    }


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


def _record_reviewed_commit_provenance(
    state: CoordinatorState, meta: ReviewCycleMetadata, workspace_path: Path
) -> None:
    """Stamp the commit this cycle judges, and its verification state (#2052).

    Called at cycle open, before reviewers read the tree, so the recorded SHA is
    the one they are given. The verification state is derived mechanically from
    the gate provenance already on state — never from verdict or summary text,
    which would let a reviewer's prose stand in for a gate result.
    """
    ok, out = _run_shell("git rev-parse HEAD", workspace_path)
    sha = out.strip() if ok else ""
    meta.reviewed_commit = sha or None
    story_validation_verdict = (
        state.story_validation_result.verdict
        if state.story_validation_result is not None
        else None
    )
    meta.verification = ReviewedCommitVerification.derive(
        reviewed_commit=meta.reviewed_commit,
        gate_commit=state.last_gate_commit,
        gate_decision=state.last_gate_decision,
        gate_runs=state.gate_runs,
        validate_blocks=len(state.validate_blocks),
        story_validation_verdict=story_validation_verdict,
    )


#: One sentence per reason an opted-in expiry did NOT apply the advice, appended
#: to the preserve message so the run itself states which situation occurred
#: rather than leaving it to be reconstructed (#2279). ``ADVICE_POLICY_PRESERVE``
#: has no entry: a project that did not opt in is seeing its normal behaviour and
#: has nothing to explain.
_TIMEOUT_ADVICE_NOTES: dict[str, str] = {
    ADVICE_ELEVATE: (
        "The advisor recommended 'elevate' — the taxonomy's answer for a case no "
        "automated choice fits — so the expiry deliberately made no selection and "
        "routed this to a human design decision."
    ),
    ADVICE_NOT_PERFORMABLE: (
        "The advisor's recommendation could not be applied because this run cannot "
        "perform it, and no substitute action was chosen in its place."
    ),
    ADVICE_NO_RECOMMENDATION: (
        "The advisory report recommended no action, so there was nothing to apply — "
        "an absence of advice, not consent to a fallback."
    ),
    ADVICE_UNPARSEABLE: (
        "The advisor ran but produced no parseable report, so there was no "
        "recommendation to apply."
    ),
    ADVICE_LAUNCH_FAILURE: (
        "The advisor never launched, so no recommendation was ever produced to apply."
    ),
    ADVICE_UNAVAILABLE: (
        "This gate surface produced no advisory report, so there was no recommendation to apply."
    ),
}


def _decided_by_advice(state: CoordinatorState) -> bool:
    """True when this gate outcome came from advice applied at an expiry.

    The outcome of an applied recommendation is deliberately identical to the
    operator's — but the *account* of it must not be, or the run would tell the
    operator they approved something while the record says otherwise (#2279).
    Read from the same field the audit and resume records publish, so the
    message and the provenance cannot disagree.
    """
    return state.escalate_decision_source == ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT


def _timeout_applies_advice(config: ForgeConfig) -> bool:
    """True when this project opted in to applying advice at an expired gate.

    Read through ``getattr`` so a ForgeConfig built by an older caller (or a test
    fixture predating the field) means "preserve" rather than raising — the
    default is the behaviour every project already has.
    """
    return getattr(config.retry, "escalate_timeout_policy", ESCALATE_TIMEOUT_PRESERVE) == (
        ESCALATE_TIMEOUT_APPLY_ADVICE
    )


def _advice_for_expired_gate(
    state: CoordinatorState,
    advisory: "AdvisoryReport | None",
) -> tuple[str | None, str]:
    """Decide whether an expired gate can act on the advisory it produced.

    Returns ``(action, status)``: ``action`` is the taxonomy action to apply, or
    None when the gate must keep waiting; ``status`` is the
    ``ESCALATE_TIMEOUT_ADVICE_STATUSES`` value naming why.

    Every non-applied outcome is a *preserve*, never a fallback action. An absent
    recommendation is the absence of advice, not consent to any particular
    outcome, so the five ways advice can be missing are told apart rather than
    collapsed into one silent default:

    * the advisor never reached the model (a repairable forge defect),
    * this gate surface produces no advisory at all (remote / interactive),
    * the report failed schema validation,
    * the report is valid but recommends nothing,
    * the recommendation is real but this run cannot perform it (e.g. ``accept``
      with no approvable reviewer result), which is the one case where applying
      it would mean substituting an action nobody chose.

    ``elevate`` is separate from all of those: the taxonomy already treats it as
    the answer for a case no automated choice fits, so an advisor that returns it
    has deliberately said "a human decides this" — and the expiry honours that by
    leaving the story exactly where a non-opted-in project would leave it.
    """
    if advisory is None:
        if state.advisory_launch_failure:
            return None, ADVICE_LAUNCH_FAILURE
        return None, ADVICE_UNAVAILABLE
    if not advisory.ok:
        return None, ADVICE_UNPARSEABLE
    recommendation = (advisory.recommendation or "").strip().lower()
    if not recommendation or recommendation not in ACTION_TAXONOMY:
        return None, ADVICE_NO_RECOMMENDATION
    if recommendation == "elevate":
        return None, ADVICE_ELEVATE
    performable, _omitted = available_escalate_actions(state, ACTION_TAXONOMY)
    if recommendation not in performable:
        return None, ADVICE_NOT_PERFORMABLE
    return recommendation, ADVICE_APPLIED


def _run_escalate_gate(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    auto_merge: bool,
    notify: bool,
    logger: "StructuredLogger | None",
    run_id: str = "",
    history_already_appended: bool = False,
) -> "CoordinatorResult | None":
    """HITL decision gate at review-related ESCALATE exit points.

    Every exit persists the gate's decision and any advisory it generated to the
    story's durable phase record. The escalation advisory is charged to the run
    and rendered to the operator, and the runs that reach this gate are the ones
    least likely to end through a normal finalization — so the record must not
    depend on this process surviving to write it (#2155).

    ``history_already_appended`` is set by callers that appended the triggering
    cycle to ``state.cycle_history`` themselves — the topology-walk route does,
    so the advisor's evidence packet contains the cycle that caused the
    escalation. The history window holds three entries, so appending it twice
    would evict an earlier cycle and hand the advisor a shorter churn pattern
    than the run actually produced.

    Returns:
        CoordinatorResult — approve or reject decision (caller should return it).
        None — continue decision (caller should reset phase to REVIEW, decrement
               review_cycle by 1 if it was already incremented, and loop).
    """
    try:
        return _run_escalate_gate_inner(
            state,
            config,
            task,
            workspace_path,
            branch_name,
            task_start,
            auto_merge=auto_merge,
            notify=notify,
            logger=logger,
            run_id=run_id,
            history_already_appended=history_already_appended,
        )
    finally:
        save_resume_record(
            config.project_root,
            state,
            slug=task.slug,
            story_content=state.story_content,
            run_id=run_id or state.run_id,
        )


def _run_escalate_gate_inner(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    auto_merge: bool,
    notify: bool,
    logger: "StructuredLogger | None",
    run_id: str = "",
    history_already_appended: bool = False,
) -> "CoordinatorResult | None":
    """Body of :func:`_run_escalate_gate`; see there for the contract."""
    escalate_policy = config.retry.escalate_policy
    reviewer_verdicts = _build_reviewer_verdicts(state)
    gate_result: str | None = None
    if state.gate_decisions:
        gate_result = state.gate_decisions[-1]

    escalate_reason = state.error or "ESCALATE"

    def _make_escalate_result(
        decision: str | None = "reject", message: str | None = None
    ) -> CoordinatorResult:
        # decision=None means "no decision was reached" — leave escalate_decision
        # unset so a later operator selection is still recordable (#2300).
        if decision is not None:
            state.escalate_decision = decision
        state.escalate_reason = escalate_reason
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=message or state.error or escalate_reason,
        )

    # Policy: reject — preserve current behavior without prompting
    if escalate_policy == "reject":
        state.escalate_decision = "reject"
        state.escalate_reason = escalate_reason
        state.escalate_decision_source = ESCALATE_SOURCE_POLICY_REJECT
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
                state.escalate_decision_source = ESCALATE_SOURCE_POLICY_AUTO_APPROVE
                if not history_already_appended:
                    _append_cycle_history(state, state.review_results[-1])
                _release_review_reservation(
                    state, retained_cycles=0, reason="approve_escalate_gate"
                )
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
                    review_cost=state.total_review_cost_measured,
                    review_elapsed=0.0,
                    message=(
                        f"Task '{task.name}' completed. "
                        f"Human auto-approved via escalate gate "
                        f"after {state.review_cycle} cycle(s). "
                    ),
                    run_id=run_id,
                )

    # Determine interaction method. `advisory` stays None on every surface that
    # does not produce a report (remote / interactive / no-interaction), so the
    # expiry branch below can ask for one without assuming which path ran.
    advisory: "AdvisoryReport | None" = None
    if _is_pending_file_mode(notify, config):
        # Generate a fresh-context advisory report so the operator selects from the
        # fixed action taxonomy instead of the system auto-rejecting on timeout.
        advisory = run_escalation_advisor(state, config, task, workspace_path)
        decision = _pending_escalate_gate(
            state,
            task,
            config,
            escalate_reason,
            reviewer_verdicts,
            gate_result,
            run_id=run_id,
            advisory=advisory,
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
        state.escalate_decision_source = ESCALATE_SOURCE_NO_INTERACTION

    state.escalate_reason = escalate_reason

    # An expired gate, opted in: apply the advice rather than discarding it.
    #
    # Guarded on decision == "timeout" alone, which is exactly "no selection
    # arrived". A selection that reached the gate before expiry — from the
    # pending file, ntfy, or the terminal — never enters here, so an operator who
    # is present always governs, whatever the advisor recommended.
    if decision == "timeout" and _timeout_applies_advice(config):
        applied, advice_status = _advice_for_expired_gate(state, advisory)
        state.escalate_timeout_advice = advice_status
        if applied is not None:
            _log(
                f"  Escalate gate expired with no operator selection — applying the advisory "
                f"recommendation {applied!r} (retry.escalate_timeout_policy=apply_advice)"
            )
            # Resolved, not awaiting: the checkpoint _pending_escalate_gate
            # refreshed on expiry must go, exactly as it does for an explicit
            # selection. Leaving it would show a decided story as still pending.
            cleanup_escalate_pending(task, config, run_id=run_id)
            decision = applied
            state.escalate_decision_source = ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT
        else:
            _log(
                f"  Escalate gate expired — advisory recommendation NOT applied "
                f"({advice_status}); preserving for an operator decision"
            )
    elif decision == "timeout":
        state.escalate_timeout_advice = ADVICE_POLICY_PRESERVE
    else:
        # Anything that is not an expiry came back from a gate surface an
        # operator was answering, so the outcome is theirs — including a value
        # the normalisation below rejects. The `or` preserves the
        # no-interaction attribution set above, which is the one non-expiry
        # decision no operator made.
        state.escalate_decision_source = state.escalate_decision_source or ESCALATE_SOURCE_OPERATOR

    # Normalise legacy (approve/reject/continue) and taxonomy actions into a
    # coordinator disposition. Taxonomy actions come from the advisory-backed
    # pending gate; approve/continue/reject may still come from interactive/remote.
    norm = (decision or "").strip().lower()
    if norm in ACTION_TAXONOMY:
        state.escalate_selected_action = norm
        disposition = action_disposition(norm)  # "approve" | "reject" | "named"
    elif norm == "approve":
        disposition = "approve"
    elif norm == "continue":
        disposition = "continue"
    elif norm == "timeout":
        disposition = "preserve"
    else:
        disposition = "reject"

    if disposition == "approve":
        # Resolve the ReviewResult this approval would finalize BEFORE acting.
        # The gate filters `accept` out of its options when this is None, so
        # reaching here means the selection is stale (a pending file written
        # before the state degraded, or a legacy approve from another surface).
        # A stale selection is DECLINED, not silently converted into its
        # opposite: substituting reject would record an outcome nobody chose and
        # would pick the one direction that cannot be revisited (#2300).
        approvable = approvable_review_result(state)
        if approvable is None:
            selected = norm or "approve"
            state.escalate_selected_action = selected
            state.escalate_declined_action = selected
            state.escalate_declined_reason = ACCEPT_UNAVAILABLE_REASON
            state.escalate_decision_source = ESCALATE_SOURCE_OPERATOR_DECLINED
            _log(
                f"  ⚠ Escalate gate: {selected!r} selected but cannot be performed "
                f"({ACCEPT_UNAVAILABLE_REASON}) — DECLINED; story left as it was "
                f"(no substitute decision recorded)"
            )
            return _make_escalate_result(
                None,
                message=(
                    f"Escalation preserved: the selected action {selected!r} could not be "
                    f"performed ({ACCEPT_UNAVAILABLE_REASON}). The selection was declined "
                    "rather than substituted; the story is unchanged and an operator "
                    "action selection is still required."
                ),
            )
        state.escalate_decision = norm if norm in ACTION_TAXONOMY else "approve"
        if not history_already_appended:
            _append_cycle_history(state, approvable)
        _release_review_reservation(state, retained_cycles=0, reason="approve_escalate_gate")
        return _finalize_approve(
            state,
            config,
            task,
            approvable,
            workspace_path,
            branch_name,
            task_start,
            auto_merge=auto_merge,
            notify=notify,
            logger=None,
            review_cost=state.total_review_cost_measured,
            review_elapsed=0.0,
            message=(
                (
                    f"Task '{task.name}' completed. "
                    f"Escalate gate expired with no operator selection and the advisory "
                    f"recommendation {state.escalate_decision!r} was applied after "
                    f"{state.review_cycle} cycle(s). "
                )
                if _decided_by_advice(state)
                else (
                    f"Task '{task.name}' completed. "
                    f"Human approved via escalate gate after {state.review_cycle} cycle(s). "
                )
            ),
            run_id=run_id,
        )

    if disposition == "continue":
        state.escalate_decision = "continue"
        _log("  Escalate gate: continue — granting one more review cycle")
        state.phase = Phase.REVIEW
        return None

    if disposition == "named":
        op = ACTION_FORGE_OPERATIONS.get(norm, norm)
        label = ACTION_LABELS.get(norm, norm)
        state.escalate_declined_action = norm
        state.escalate_declined_reason = NAMED_ACTION_UNAVAILABLE_REASON
        state.escalate_decision_source = ESCALATE_SOURCE_OPERATOR_DECLINED
        _log(
            f"  ⚠ Escalate gate: {label} selected but was not carried out "
            f"({state.escalate_declined_reason}) — DECLINED; named operation remains {op}"
        )
        return _make_escalate_result(
            None,
            message=(
                f"Escalation preserved: the selected action {label} was not carried out "
                f"({state.escalate_declined_reason}). "
                + f"The named operation remains {op}, and an operator action selection "
                "is still required."
            ),
        )

    if disposition == "preserve":
        # Timeout with no explicit selection: the contract change (#1664) is that
        # this preserves the escalation for an operator decision rather than
        # auto-rejecting and discarding the work. Only the pending-file path
        # generates an advisory report; remote/interactive timeouts preserve too
        # but without one, so vary the message on what was actually produced.
        _log("  Escalate gate: no selection (timeout) — preserving for operator decision")
        if state.advisory_generated:
            preserve_message = (
                "Escalation preserved: an advisory report was generated and an "
                "operator action selection is still required (no auto-reject)."
            )
        elif state.advisory_launch_failure:
            # Distinct from "no advisory": the advisor never started, so the
            # missing advice is a repairable forge configuration defect that cost
            # nothing — not evidence that the escalation resists analysis (#2164).
            preserve_message = (
                "Escalation preserved: the escalation advisor FAILED TO LAUNCH "
                f"({state.advisory_launch_reason or 'no reason captured'}) — a forge "
                "configuration/tool-invocation defect that spent $0.00 and never "
                "reached the model. An operator action selection is still required "
                "(no auto-reject)."
            )
        else:
            preserve_message = (
                "Escalation preserved: an operator action selection is still "
                "required (no auto-reject)."
            )
        # Under the opt-in policy the operator asked for advice to be applied, so
        # NOT applying it is the surprising outcome and has to say which of the
        # possible absences occurred — an unusable report and a deliberate
        # `elevate` are the same "still waiting" from the outside, and only one of
        # them is a defect worth repairing.
        advice_note = _TIMEOUT_ADVICE_NOTES.get(state.escalate_timeout_advice or "")
        if advice_note:
            preserve_message = f"{preserve_message} {advice_note}"
        state.escalate_decision_source = ESCALATE_SOURCE_TIMEOUT_PENDING
        return _make_escalate_result("advisory_pending", message=preserve_message)

    # reject / defer_or_abandon / any unrecognised decision
    return _make_escalate_result("reject")


def _record_review_iteration_telemetry(
    state: CoordinatorState,
    parsed_review: ReviewResult,
    *,
    review_cost: float | None,
    review_elapsed: float,
    max_iterations: int,
) -> None:
    prior_ids = {
        record.finding_id
        for record in state.finding_registry
        if record.cycle_first_seen < state.review_cycle
    }
    new_by_severity = {"P1": 0, "P2": 0}
    repeated_by_severity = {"P1": 0, "P2": 0}
    for record in state.finding_registry:
        if record.cycle_last_seen != state.review_cycle:
            continue
        bucket = new_by_severity if record.finding_id not in prior_ids else repeated_by_severity
        bucket[record.severity] = bucket.get(record.severity, 0) + 1
    findings_by_severity = {
        "P1": sum(1 for f in parsed_review.findings if f.severity == "P1"),
        "P2": sum(1 for f in parsed_review.findings if f.severity == "P2"),
    }
    state.review_iteration_telemetry.append(
        ReviewIterationTelemetry(
            # Sequential position of this recorded reviewer cycle, which is what
            # build_reviews() numbers reviews[].cycle by. state.review_cycle is
            # not usable here: VALIDATE opening a cycle advances it without
            # appending an entry, so the two numberings drifted apart (#1986).
            iteration=len(state.review_iteration_telemetry) + 1,
            max_iterations=max_iterations,
            cost_usd=review_cost,
            duration_s=review_elapsed,
            verdict=parsed_review.verdict,
            findings_by_severity=findings_by_severity,
            new_findings_by_severity=new_by_severity,
            repeated_findings_by_severity=repeated_by_severity,
            novel_findings=sum(new_by_severity.values()),
            restated_findings=sum(repeated_by_severity.values()),
        )
    )


class _ReviewOutcome(Enum):
    DONE = auto()
    ESCALATE = auto()
    RETRY_DEV = auto()


def _open_p2_findings(parsed_review: ReviewResult) -> list[ReviewFinding]:
    """Return the P2 findings raised by the current review pass."""
    return [f for f in parsed_review.findings if f.severity == "P2"]


def _p2_finding_key(f: ReviewFinding) -> tuple[str, int | None, str]:
    """Return a stable fingerprint for a P2 finding."""
    return (f.file or "", f.line, (f.description or "").strip())


def _build_carry_findings(
    parsed_review: ReviewResult,
    carry_keys: list[tuple[str, int | None, str]],
) -> list[ReviewFinding]:
    """Filter the current P2 findings to those still in the carried set."""
    if not carry_keys:
        return []
    carry_set = {tuple(key) for key in carry_keys}
    return [
        f for f in parsed_review.findings if f.severity == "P2" and _p2_finding_key(f) in carry_set
    ]


def _carry_handoff(findings: list[ReviewFinding]) -> str:
    """Render carried P2 findings as a dev-agent handoff body."""
    if not findings:
        return "No specific findings provided."
    lines: list[str] = ["## Carried P2 Findings"]
    for f in findings:
        loc = f" (line {f.line})" if f.line is not None else ""
        lines.append(f"\n### [P2] `{f.file}`{loc}")
        lines.append(f"**Issue:** {f.description}")
        if f.suggestion:
            lines.append(f"**Fix:** {f.suggestion}")
    return "\n".join(lines)


def _release_review_reservation(
    state: CoordinatorState,
    *,
    retained_cycles: int,
    reason: str,
) -> None:
    """Release the unspent review reserve once review has terminally approved (#2340).

    The reservation is priced at seating against the *maximum* review cycles the
    story was granted. When review reaches an approve-equivalent terminal path
    those cycles cannot all occur, so continuing to withhold their money turns a
    safety margin into a phantom debit — a story gets refused a funded P2-cleanup
    dev attempt against dollars that provably will not be spent.

    ``retained_cycles`` is how many review cycles remain reachable: one for a P2
    cleanup pass (its dev iteration loops back through REVIEW), zero once the
    approval is being finalized. Callers must sit where the branch PROVES that
    count — an approve alone does not, since an interactive session can still
    reject or extend back into REVIEW. Every route that finalizes an approval
    settles here (normal, interactive, escalate-gate, hygiene replay, early
    termination), each naming itself in ``reason``, so no DONE story lands with
    an audit record still claiming the gross seated reserve is withheld. The
    release is mirrored into
    ``adaptive_limits_audit`` so the run audit keeps showing the real dispatch
    inputs without a new audit schema path.

    A reservation released by an earlier cleanup pass is released again when a
    later cycle approves: the retained cycle it held may by then have run, and
    the recomputation starts from what is still protected, so a re-release only
    ever shrinks the retained balance.
    """
    reservation = state.review_funding_reservation
    if not isinstance(reservation, dict):
        return
    released = _story_budget.release_review_reservation(
        reservation,
        review_observed_usd=state.total_review_cost_measured,
        retained_cycles=retained_cycles,
        review_cycle=state.review_cycle,
        reason=reason,
    )
    if released is reservation:
        # Nothing to release (no reserve seated, or review spend unmeasured).
        return
    state.review_funding_reservation = released
    if isinstance(state.adaptive_limits_audit, dict):
        state.adaptive_limits_audit["review_funding_reservation"] = released


def _clear_p2_cleanup_state(state: CoordinatorState) -> None:
    """Reset cleanup tracking when the loop exits (clean, regression, cap, etc.)."""
    state.p2_cleanup_active = False
    state.p2_cleanup_findings = []
    state.p2_cleanup_carry_keys = []


def _maybe_enter_p2_cleanup(
    state: CoordinatorState,
    config: ForgeConfig,
    parsed_review: ReviewResult,
) -> bool:
    """Decide whether to re-enter DEV for advisory P2 cleanup after APPROVE.

    Mutates state when entering cleanup: sets retry_reason=P2_CLEANUP,
    p2_cleanup_active=True, captures p2_cleanup_findings filtered to the
    originally carried P2 set, and appends an audit entry. Returns True to
    signal the caller should return RETRY_DEV instead of DONE.

    On first entry the current P2 findings are fingerprinted and stored as
    state.p2_cleanup_carry_keys; on subsequent passes only those carried
    findings still raised by the reviewer keep the loop running. New P2
    findings raised after carry capture are recorded but do not extend the
    loop — preventing an unbounded "follow every new advisory" pattern.

    Returns False when:
      - the feature is disabled,
      - no carried P2 findings remain (or none existed),
      - the dev iteration budget for this cycle is exhausted,
      - entering cleanup would consume the final iteration reserved for repair, or
      - the configured cleanup-iteration cap has been reached.
    """
    current_p2s = _open_p2_findings(parsed_review)
    if not state.p2_cleanup_active:
        # First evaluation post-APPROVE — capture the carry set from the
        # P2 findings raised by THIS review.
        carry_keys = [list(_p2_finding_key(f)) for f in current_p2s]
    else:
        carry_keys = list(state.p2_cleanup_carry_keys)
    remaining_carried = _build_carry_findings(parsed_review, carry_keys)
    audit_base = {
        "review_cycle": state.review_cycle,
        "dev_iteration": state.dev_iteration,
        "p2_count": len(current_p2s),
        "carried_p2_count": len(carry_keys),
        "remaining_carried_p2_count": len(remaining_carried),
        "budget_remaining": state.budget.remaining(),
        "cleanup_iterations": state.p2_cleanup_iterations,
    }
    if not config.retry.p2_cleanup_enabled:
        state.p2_cleanup_audit.append({"action": "skip_disabled", **audit_base})
        _clear_p2_cleanup_state(state)
        return False
    if not remaining_carried:
        # Either the original review had no P2s, or every carried P2 was
        # resolved by a prior cleanup pass. Either way we exit the loop.
        action = "exit_clean" if state.p2_cleanup_active else "skip_no_p2"
        state.p2_cleanup_audit.append({"action": action, **audit_base})
        _clear_p2_cleanup_state(state)
        return False
    if state.budget.remaining() <= 0:
        state.p2_cleanup_audit.append({"action": "skip_budget", **audit_base})
        _clear_p2_cleanup_state(state)
        return False
    if state.budget.remaining() < 2:
        state.p2_cleanup_audit.append({"action": "skip_budget_reserve", **audit_base})
        _clear_p2_cleanup_state(state)
        return False
    cap = config.retry.p2_cleanup_max_iterations
    if cap > 0 and state.p2_cleanup_iterations >= cap:
        state.p2_cleanup_audit.append({"action": "skip_cap", **audit_base})
        _clear_p2_cleanup_state(state)
        return False
    state.p2_cleanup_carry_keys = carry_keys
    state.p2_cleanup_findings = [
        {
            "severity": f.severity,
            "file": f.file,
            "line": f.line,
            "description": f.description,
            "suggestion": f.suggestion,
        }
        for f in remaining_carried
    ]
    action = "continue" if state.p2_cleanup_iterations > 0 else "enter"
    state.p2_cleanup_active = True
    state.p2_cleanup_iterations += 1
    state.retry_reason = RetryReason.P2_CLEANUP
    state.human_feedback = None
    state.last_review_findings = _carry_handoff(remaining_carried)
    state.p2_cleanup_audit.append({"action": action, **audit_base})
    return True


# ── Review-phase helpers ──────────────────────────────────────────────


def _story_diff_for_review(
    state: CoordinatorState,
    task: TaskStory,
    workspace_path: Path,
    *,
    batch_context: BatchReviewContext | None = None,
) -> StoryDiff | None:
    """Return this story's own file set, or None to use the branch diff.

    Only one situation makes the branch diff the wrong answer: a cost-aware
    batch group, where several independent stories share one branch. There the
    set is narrowed to the commits the shared handoff attributes to this story,
    so a sibling member's findings cannot ground against this member (#2525).

    Membership is decided by the *context object*, never by whether a handoff
    came with it. A member whose dev pass produced no structured handoff is
    still on a shared branch, so it gets an unavailable batch file set — which
    grounds nothing — rather than the branch diff, which would ground its
    siblings' findings against it.

    The batch leader is detected from ``task.batch_members`` and reads the
    handoff its own DEV phase captured; a non-leader member is reviewed through
    the review-only path with a fresh state, so its caller supplies the context.
    """
    if batch_context is not None:
        return batch_member_story_diff(workspace_path, batch_context.dev_handoff, task.slug)
    if task.batch_members:
        return batch_member_story_diff(workspace_path, latest_dev_handoff(state), task.slug)
    return None


def _record_grounding(state: CoordinatorState, grounding: GroundingResult) -> None:
    """Record what this cycle's findings were grounded against, for the audit.

    Suppressing findings against a file set the record does not name would make
    the suppression unreviewable — the operator could see *that* a P1 was set
    aside but not *what* it was checked against (conventions #6).
    """
    state.review_diff_grounding = {
        "review_cycle": state.review_cycle,
        **grounding.story_diff.as_audit_record(),
        "ungrounded_p1_ids": [record.finding_id for record in grounding.ungrounded],
        # Findings that stopped being suppressed this cycle because the change
        # grew to include the file they cite. A suppression that silently lifts
        # is as much a decision as one that starts, and this is the only place
        # the reversal is visible.
        "restored_p1_ids": [record.finding_id for record in grounding.restored],
    }


REVIEW_INFRASTRUCTURE_PARSE_FAILURE_REASON = (
    "Review infrastructure failure: all reviewers failed to produce parseable output. "
    "This is a review-layer defect, not an implementation defect — DEV cannot resolve it. "
    "Manual review required."
)


def _apply_review_parse_fallback(
    candidate: ReviewResult,
    individual_results: list[ReviewResult],
) -> ReviewResult | None:
    """Fall back from a merged candidate with parse errors to best individual result.

    Returns the candidate unchanged when it has no parse errors. When the merged
    candidate has parse errors, returns the best parseable individual result if any
    reviewer produced one. Returns None when no reviewer produced parseable output —
    callers must treat this as a review-infrastructure failure and escalate directly
    rather than routing the run back through DEV with a synthetic finding.
    """
    if not candidate.parse_errors:
        return candidate
    _log(
        f"  ⚠ review merge produced parse errors — falling back to best individual result "
        f"({len(individual_results)} reviewer(s) with valid output)"
    )
    _fallback = _best_individual_result(individual_results)
    if _fallback is not None:
        _log(f"  ↩ using best individual result: {_fallback.verdict}")
        return _fallback
    _log("  ✗ all reviewers failed to produce parseable output — escalating directly")
    return None


def _story_label(task: TaskStory) -> str:
    """Human-readable story tag for finding log lines, e.g. ``Add retry backoff (#42)``."""
    if task.github_issue is not None:
        return f"{task.name} (#{task.github_issue})"
    return task.name


def _log_review_findings(
    parsed_review: ReviewResult,
    p1_count: int,
    p2_count: int,
    review_cost: float | None,
    logger: StructuredLogger | None,
    task: TaskStory,
) -> None:
    """Log review summary and findings grouped by severity; emit structured review_result event.

    Each finding line surfaces the reviewer profile that produced it and the story
    under review so operators can see which model flagged what on which story. The
    reporter tag is always present — an unattributed finding renders ``[reviewer: ?]``
    rather than being hidden, so the single-reviewer (no-pool) case still shows a tag.
    """
    _log(f"  Summary: {parsed_review.summary}")
    _story_tag = f" [story: {_story_label(task)}]"
    _findings_by_sev: dict[str, list] = {}
    for _f in parsed_review.findings:
        _findings_by_sev.setdefault(_f.severity, []).append(_f)
    for _sev in sorted(_findings_by_sev):
        for _f in _findings_by_sev[_sev]:
            _loc = f" [{_f.file}:{_f.line}]" if _f.file else ""
            _reporter_tag = f" [reviewer: {_f.reporter or '?'}]"
            _log(f"  [{_sev}]{_reporter_tag}{_story_tag}{_loc} {_f.description}")
    if logger:
        logger._safe_emit(
            "review_result",
            verdict=parsed_review.verdict,
            p1_count=p1_count,
            p2_count=p2_count,
            cost_usd=_round_cost(review_cost),
        )


def _handle_interactive_review_decision(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    parsed_review: ReviewResult,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    auto_merge: bool,
    notify: bool,
    review_cost: float | None,
    review_elapsed: float,
    run_id: str,
    exhausted_cycles: bool,
    history_already_appended: bool = False,
) -> tuple[_ReviewOutcome, CoordinatorResult | None, ForgeConfig]:
    """Handle the HUMAN_REVIEW decision flow for an interactive session.

    Called from the APPROVE path (exhausted_cycles=False) and the exhausted-cycles
    REQUEST_CHANGES path (exhausted_cycles=True).  Dispatches to the right backend
    (pending-file / remote / terminal) and maps the human decision to a coordinator
    outcome.
    """
    state.phase = Phase.HUMAN_REVIEW
    _log_phase(state.phase, "cycles exhausted" if exhausted_cycles else "")
    if _is_pending_file_mode(notify, config):
        decision, feedback = _pending_human_review(
            state,
            parsed_review,
            workspace_path,
            branch_name,
            task,
            config,
            task_start,
            run_id=run_id,
        )
    elif _is_remote_mode(notify, config):
        decision, feedback = _remote_human_review(
            state, parsed_review, workspace_path, branch_name, task, config, task_start
        )
    else:
        decision, feedback = _human_review(state, parsed_review, workspace_path, branch_name)
    state.human_review_decision = decision
    state.human_review_feedback = feedback

    if decision == "approve":
        if not history_already_appended:
            _append_cycle_history(state, parsed_review)
        # The operator ended the run, so the seated review cycles cannot run.
        # This is the only interactive outcome that proves that: extend and
        # reject both loop back through REVIEW, and a release before the
        # decision would strip their cycles of the reservation (#2258/#2340).
        _release_review_reservation(state, retained_cycles=0, reason="approve_final")
        if exhausted_cycles:
            _approve_msg = (
                f"Task '{task.name}' completed. "
                f"Human approved after {state.review_cycle} cycle(s). "
            )
        else:
            _approve_msg = (
                f"Task '{task.name}' completed. "
                f"Human approved after {state.review_cycle} cycle(s), "
                # budget.total_count, not state.dev_iteration — see the
                # automatic-approve message below for why (#1983).
                f"{state.budget.total_count} dev iteration(s). "
            )
        return (
            _ReviewOutcome.DONE,
            _finalize_approve(
                state,
                config,
                task,
                parsed_review,
                workspace_path,
                branch_name,
                task_start,
                auto_merge=auto_merge,
                notify=notify,
                logger=None,
                review_cost=review_cost,
                review_elapsed=review_elapsed,
                message=_approve_msg,
                run_id=run_id,
            ),
            config,
        )

    if decision in ("escalate", "timeout"):
        state.phase = Phase.ESCALATE
        state.escalate_kind = "content"
        if decision == "timeout":
            state.error = "Remote review timed out — auto-escalated."
        elif exhausted_cycles:
            state.error = "Human chose to escalate after exhausted cycles."
        else:
            state.error = "Human chose to escalate after APPROVE."
        _log(f"✗ ESCALATE   {state.error}")
        _escalate_notify(task, state, notify, config)
        return (
            _ReviewOutcome.ESCALATE,
            CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            ),
            config,
        )

    if decision == "extend":
        if not history_already_appended:
            _append_cycle_history(state, parsed_review)
        state.budget.reset_cycle()
        state.review_cycle = 0
        state.human_review_extra_cycles += 1
        # exhausted-cycles: always populate so the next DEV iteration uses fix-prompt
        # even when findings list is empty (REQUEST_CHANGES with no explicit findings).
        # approve-path: only populate when there are findings to hand off.
        if exhausted_cycles:
            state.last_review_findings = review_to_dev_handoff(parsed_review)
        else:
            state.last_review_findings = (
                review_to_dev_handoff(parsed_review) if parsed_review.findings else None
            )
        state.human_feedback = None
        state.retry_reason = RetryReason.EXTEND
        _log(
            f"Human extended — granting fresh budget "
            f"(extra_cycles={state.human_review_extra_cycles})"
        )
        return _ReviewOutcome.RETRY_DEV, None, config

    # decision == "reject"
    if not history_already_appended:
        _append_cycle_history(state, parsed_review)
    state.budget.reset_cycle()
    state.last_review_findings = None
    state.retry_reason = RetryReason.REJECT
    if exhausted_cycles:
        # Treat as extend + reject: grant fresh budget
        state.review_cycle = 0
        state.human_review_extra_cycles += 1
        state.human_feedback = feedback
        _log(
            "Human rejected (cycles exhausted) — granting fresh budget "
            f"(extra_cycles={state.human_review_extra_cycles})"
        )
    else:
        state.human_feedback = feedback
        _log("Human rejected — looping back to dev with feedback")
    return _ReviewOutcome.RETRY_DEV, None, config


def _maybe_replay_hygiene_consensus(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    auto_merge: bool,
    notify: bool,
    logger: StructuredLogger | None,
    run_id: str,
) -> tuple[_ReviewOutcome, CoordinatorResult | None, ForgeConfig] | None:
    """Replay a prior APPROVE consensus captured at hygiene-escalation time.

    Returns a DONE outcome when the dev commit is unchanged from the hygiene
    trip and a prior APPROVE candidate was captured.  Returns None to fall
    through to a fresh review otherwise; in that case escalation-state fields
    are cleared so a future hygiene escalation does not silently reuse them.
    """
    prior_review = state.hygiene_escalation_prior_review
    prior_sha = state.hygiene_escalation_dev_commit_sha or ""
    _ok_sha, _sha_out = _run_shell("git rev-parse HEAD", workspace_path)
    head_sha = _sha_out.strip() if _ok_sha else ""

    if not head_sha or not prior_sha or head_sha != prior_sha:
        audit = {
            "resume_action": "rerun_dev_commit_changed"
            if (head_sha and prior_sha)
            else "rerun_no_prior_consensus",
            "escalate_kind": state.escalate_kind,
            "prior_approve_count": state.hygiene_escalation_prior_approve_count,
            "total_count": state.hygiene_escalation_total_count,
            "dev_commit_sha_at_hygiene_trip": prior_sha or None,
            "dev_commit_sha_at_resume": head_sha or None,
        }
        state.hygiene_resume_audit = audit
        if logger:
            logger._safe_emit("hygiene_resume", **audit)
        _log(
            "  ↺ RESUME   dev commit changed since hygiene escalation"
            f" (was {prior_sha[:8] or '?'}, now {head_sha[:8] or '?'})"
            " — running fresh review"
        )
        # Clear so a future hygiene escalation in this run does not replay.
        state.escalate_kind = None
        state.hygiene_escalation_prior_review = None
        return None

    # Refuse to replay if the worktree mutation that caused the original
    # hygiene escalation (or any new mutation) is still present. Replaying
    # under a dirty tree would let unreviewed reviewer-created changes ride
    # under the prior dev-commit approval — and in merge mode land_story
    # could auto-commit those changes before merging. Fail closed: ESCALATE
    # with the offending paths so the operator either removes/quarantines
    # them or marks the run rejected — do not fall through to a fresh
    # review, whose own hygiene snapshot would treat the persistent
    # mutation as part of the baseline and silently approve it.
    from .workspace_hygiene import snapshot_porcelain  # noqa: PLC0415

    _porcelain = snapshot_porcelain(workspace_path)
    if _porcelain:
        offending = sorted(entry[3:] if len(entry) > 3 else entry for entry in _porcelain)
        audit = {
            "resume_action": "rerun_dirty_worktree",
            "escalate_kind": state.escalate_kind,
            "prior_approve_count": state.hygiene_escalation_prior_approve_count,
            "total_count": state.hygiene_escalation_total_count,
            "dev_commit_sha_at_hygiene_trip": prior_sha,
            "dev_commit_sha_at_resume": head_sha,
            "offending_paths": offending,
        }
        state.hygiene_resume_audit = audit
        if logger:
            logger._safe_emit("hygiene_resume", **audit)
        _log(
            "  ↺ RESUME   refusing to replay APPROVE consensus — worktree still"
            f" dirty ({len(offending)} unresolved path(s)): {', '.join(offending[:5])}"
        )
        # Keep escalate_kind='hygiene' so the operator-facing state still
        # reflects the unresolved hygiene escalation; clear the prior review
        # so a subsequent extend/retry cycle starts fresh once the workspace
        # is clean.
        state.hygiene_escalation_prior_review = None
        state.phase = Phase.ESCALATE
        state.error = (
            "Workspace hygiene escalation has unresolved mutations at resume; "
            f"remove or quarantine offending paths before retrying: {', '.join(offending)}"
        )
        if logger:
            logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
            logger._safe_emit(
                "escalate", reason=state.error, phase="REVIEW", escalate_kind="hygiene"
            )
        _escalate_notify(task, state, notify, config)
        return (
            _ReviewOutcome.ESCALATE,
            CoordinatorResult(success=False, phase=state.phase, state=state, message=state.error),
            config,
        )

    # Apply the same empty-diff guard as the normal APPROVE path: refuse to
    # mark DONE on a branch with no commits ahead of base. Without this,
    # replaying a captured APPROVE on a branch whose commits have been
    # stripped (or never landed) would silently approve an empty diff.
    if not _has_commits_ahead_of_base(workspace_path, config.workspace.base_branch):
        audit = {
            "resume_action": "rerun_no_commits_ahead",
            "escalate_kind": state.escalate_kind,
            "prior_approve_count": state.hygiene_escalation_prior_approve_count,
            "total_count": state.hygiene_escalation_total_count,
            "dev_commit_sha_at_hygiene_trip": prior_sha,
            "dev_commit_sha_at_resume": head_sha,
            "base_branch": config.workspace.base_branch,
        }
        state.hygiene_resume_audit = audit
        if logger:
            logger._safe_emit("hygiene_resume", **audit)
        _log(
            "  ↺ RESUME   refusing to replay APPROVE consensus — branch has no"
            f" commits ahead of {config.workspace.base_branch}; running fresh review"
        )
        state.escalate_kind = None
        state.hygiene_escalation_prior_review = None
        return None

    state.review_cycle += 1
    state.review_results.append(prior_review)
    audit = {
        "resume_action": "replayed_consensus",
        "escalate_kind": state.escalate_kind,
        "prior_approve_count": state.hygiene_escalation_prior_approve_count,
        "total_count": state.hygiene_escalation_total_count,
        "dev_commit_sha_at_hygiene_trip": prior_sha,
        "dev_commit_sha_at_resume": head_sha,
    }
    state.hygiene_resume_audit = audit
    if logger:
        logger._safe_emit("hygiene_resume", **audit)
    _log(
        "  ↺ RESUME   replaying prior APPROVE consensus from hygiene escalation,"
        f" dev commit {prior_sha[:8]}"
    )
    # Clear so a subsequent escalation does not replay the same record.
    state.escalate_kind = None
    state.hygiene_escalation_prior_review = None
    _append_cycle_history(state, prior_review)
    _release_review_reservation(state, retained_cycles=0, reason="approve_hygiene_replay")
    return (
        _ReviewOutcome.DONE,
        _finalize_approve(
            state,
            config,
            task,
            prior_review,
            workspace_path,
            branch_name,
            task_start,
            auto_merge=auto_merge,
            notify=notify,
            logger=logger,
            review_cost=0.0,
            review_elapsed=0.0,
            message=(
                f"Task '{task.name}' completed. "
                f"Replayed prior APPROVE consensus from hygiene escalation. "
            ),
            run_id=run_id,
        ),
        config,
    )


def _run_review_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    story_content: str,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    interactive: bool,
    auto_merge: bool,
    notify: bool,
    logger: StructuredLogger | None,
    run_id: str = "",
    state_update_fn: Callable[[dict], None] | None = None,
    stop_event: "threading.Event | None" = None,
) -> tuple[_ReviewOutcome, CoordinatorResult | None, ForgeConfig]:
    """Run the full REVIEW phase: pool+synthesis, parse retries, verdict handling.

    Returns (outcome, result_or_none, possibly_updated_config).
    DONE/ESCALATE → result is a CoordinatorResult.
    RETRY_DEV → result is None, caller loops back to DEV.
    config is returned because persistent-P1 model escalation may replace it.
    """
    state.phase = Phase.REVIEW

    # Resume short-circuit: if the prior run escalated due to a workspace-hygiene
    # mutation but the reviewer pool had already produced an APPROVE consensus
    # for the dev commit, replay that consensus instead of re-running the pool
    # against an unchanged dev commit (issue #1499).
    if state.escalate_kind == "hygiene" and state.hygiene_escalation_prior_review is not None:
        _resume_result = _maybe_replay_hygiene_consensus(
            state,
            config,
            task,
            workspace_path,
            branch_name,
            task_start,
            auto_merge=auto_merge,
            notify=notify,
            logger=logger,
            run_id=run_id,
        )
        if _resume_result is not None:
            return _resume_result
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "REVIEW",
                "iteration": state.review_cycle + 1,
                "cost_usd": state.total_cost_measured,
                "coordinator_state": state,
                **live_complexity_fields(
                    state.preflight_complexity, state.preflight_complexity_score
                ),
                "current_model": f"panel({len(config.review_pool)})",
                # Populate detail on REVIEW entry so STAGE renders cycle=N/M and
                # the stale prior-phase detail (e.g. GATE/VALIDATE gate_status) is
                # overwritten immediately. story_state.transition replaces detail
                # wholesale, so supplying it here clears the leftover field.
                "detail": {
                    "review_cycle": state.review_cycle + 1,
                    "review_max_cycles": (
                        state.adaptive_review_max or config.retry.max_review_cycles
                    ),
                },
            }
        )
    if logger:
        logger._safe_emit("phase_start", phase="REVIEW", iteration=state.review_cycle + 1)
    # Reset per-cycle parse failure counts so transient failures from one cycle
    # do not accumulate into the next. Permanent demotions are tracked separately
    # in state.reviewer_demoted, which is never reset.
    state.reviewer_parse_failure_counts = {}
    max_parse_retries = config.retry.max_review_parse_retries
    _review_pool_start = time.monotonic()
    _pool_model_names = "+".join(p.model for p in config.review_pool)
    _log_phase(state.phase, f"{_pool_model_names}  cycle={state.review_cycle + 1}")

    meta = ReviewCycleMetadata(
        pool_models=[p.name for p in config.review_pool],
        successful=[],
        failed=[],
        synthesized=False,
        parse_retries=0,
    )
    _record_reviewed_commit_provenance(state, meta, workspace_path)
    state.review_cycle_metadata.append(meta)
    # Snapshot the result COUNT, not a coerced dollar subtotal: this cycle's
    # cost is the sum over the results this cycle appends, and that sum stays
    # None-aware (an unmeasured attempt makes the cycle cost unknown).
    _review_results_before_cycle = len(state.review_agent_results)

    from . import scope_guard  # noqa: PLC0415
    from .workspace_hygiene import (  # noqa: PLC0415
        enforce_pre_review_hygiene,
        reconcile_post_review_mutations,
        snapshot_porcelain,
    )

    # ── Workspace hygiene gate (every REVIEW cycle entry) ─────────────
    # Stray untracked paths inherited from a prior interrupted iteration,
    # resume, or non-DEV phase mutation must be quarantined BEFORE reviewers
    # observe the tree — otherwise reviewers flag stale pollution as a
    # finding even though the implementation itself is valid (see #1501).
    # Symmetric to enforce_pre_dev_hygiene; modified-tracked files are
    # audited but left in place (validate-phase auto-commit owns cleanup).
    _hygiene_run_id = run_id or state.run_id or "unknown"
    _pre_review_ok, _pre_review_diag, _pre_review_audit = enforce_pre_review_hygiene(
        workspace_path,
        _hygiene_run_id,
        cycle=state.review_cycle,
    )
    state.workspace_hygiene_audit.append({"phase": "PRE_REVIEW", **_pre_review_audit})
    if _pre_review_audit.get("quarantined"):
        _q_paths = ", ".join(_pre_review_audit["quarantined"])
        _q_dir = _pre_review_audit.get("quarantine_dir")
        _log(f"  ⚠ REVIEW   quarantined stray paths to {_q_dir}: {_q_paths}")
    if not _pre_review_ok:
        state.phase = Phase.ESCALATE
        state.error = _pre_review_diag or "Workspace hygiene gate refused REVIEW entry"
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
            logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
        _escalate_notify(task, state, notify, config)
        return (
            _ReviewOutcome.ESCALATE,
            CoordinatorResult(success=False, phase=state.phase, state=state, message=state.error),
            config,
        )

    # ── Diff-scope guard (DEV → REVIEW boundary) ──────────────────────
    # The DEV phase owns tree mutation, so any file the dev agent commits into
    # its feature branch — including repo-root environment/tooling config it
    # added to unblock its own broken worktree (poetry.toml, .npmrc, editor
    # dirs, *.local) or forge's own runtime artifacts — flows through unchecked
    # unless something inspects the committed diff. Fail closed by escalating
    # with the offending paths rather than silently rewriting committed history
    # over a heuristic denylist (a poetry-packaging story may legitimately edit
    # poetry.toml). Same class as the earlier handoff.yaml leak, generalized
    # (theforge #1615).
    _scope_ok, _scope_diag, _scope_audit = scope_guard.check_committed_scope(
        workspace_path,
        config.workspace.base_branch,
    )
    state.workspace_hygiene_audit.append({"phase": "SCOPE_GUARD", **_scope_audit})
    if not _scope_ok:
        state.phase = Phase.ESCALATE
        state.error = _scope_diag or "Diff-scope guard refused REVIEW entry"
        state.escalate_kind = "hygiene"
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
            logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
        _escalate_notify(task, state, notify, config)
        return (
            _ReviewOutcome.ESCALATE,
            CoordinatorResult(success=False, phase=state.phase, state=state, message=state.error),
            config,
        )

    _review_hygiene_before = snapshot_porcelain(workspace_path)

    successful, failed_results, _candidate, _individual_results, _named_parsed = _run_review_pool(
        state,
        config,
        task,
        story_content,
        workspace_path,
        branch_name,
        meta,
        notify=notify,
        pool_attempt=0,
        max_review_parse_retries=max_parse_retries,
        logger=logger,
        stop_event=stop_event,
        state_update_fn=state_update_fn,
    )
    state.last_cycle_reviewer_results = _named_parsed

    _review_ok, _review_diag, _review_offending, _post_review_audit = (
        reconcile_post_review_mutations(
            workspace_path,
            _review_hygiene_before,
            _hygiene_run_id,
            state.review_cycle,
        )
    )
    state.workspace_hygiene_audit.append(
        {
            "phase": "REVIEW",
            "ok": _review_ok,
            "quarantined": _post_review_audit.get("quarantined", []),
            "tracked_changes": _post_review_audit.get("tracked_changes", []),
            "reverted": _post_review_audit.get("reverted", []),
            "offending_paths": _review_offending,
        }
    )
    if _post_review_audit.get("quarantined"):
        _moved = ", ".join(_post_review_audit["quarantined"])
        _q_dir = _post_review_audit.get("quarantine_dir")
        _log(f"  ⚠ REVIEW   quarantined reviewer scratch to {_q_dir}: {_moved}")
    if _post_review_audit.get("reverted"):
        _rev = ", ".join(_post_review_audit["reverted"])
        _log(f"  ⚠ REVIEW   reverted reviewer-side tracked mutations: {_rev}")
    if not _review_ok:
        state.phase = Phase.ESCALATE
        state.error = _review_diag or "REVIEW phase mutated the worktree"
        state.escalate_kind = "hygiene"
        # Capture prior reviewer consensus so `forge sprint --resume` can replay
        # the APPROVE outcome for an unchanged dev commit instead of re-exposing
        # the run to reviewer flakiness on a hygiene-only escalation.
        _approve_count = sum(1 for _, rr in _named_parsed if rr.verdict == "APPROVE")
        _total_count = len(_named_parsed)
        state.hygiene_escalation_prior_approve_count = _approve_count
        state.hygiene_escalation_total_count = _total_count
        if (
            _candidate is not None
            and not _candidate.parse_errors
            and _candidate.verdict == "APPROVE"
        ):
            _ok_sha, _sha_out = _run_shell("git rev-parse HEAD", workspace_path)
            if _ok_sha and _sha_out.strip():
                state.hygiene_escalation_dev_commit_sha = _sha_out.strip()
                state.hygiene_escalation_prior_review = _candidate
                _log(
                    f"  ↺ Captured prior APPROVE consensus "
                    f"({_approve_count}/{_total_count}) at dev commit "
                    f"{state.hygiene_escalation_dev_commit_sha[:8]} for resume replay"
                )
        save_trajectory_state(workspace_path, state)
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
            logger._safe_emit(
                "escalate",
                reason=state.error,
                phase="REVIEW",
                escalate_kind="hygiene",
                prior_approve_count=_approve_count,
                total_count=_total_count,
                dev_commit_sha=state.hygiene_escalation_dev_commit_sha,
            )
        _escalate_notify(task, state, notify, config)
        return (
            _ReviewOutcome.ESCALATE,
            CoordinatorResult(success=False, phase=state.phase, state=state, message=state.error),
            config,
        )

    if _candidate is None:
        # All reviewers failed or budget exceeded —
        # state.error already set by _run_review_pool
        _gate_result = _run_escalate_gate(
            state,
            config,
            task,
            workspace_path,
            branch_name,
            task_start,
            auto_merge=auto_merge,
            notify=notify,
            logger=logger,
            run_id=run_id,
        )
        if _gate_result is not None:
            return _ReviewOutcome.ESCALATE, _gate_result, config
        # Gate said "continue" — re-enter REVIEW (review_cycle not incremented here)
        return _ReviewOutcome.RETRY_DEV, None, config

    # ── Graceful empty-merge fallback ─────────────────────────────────
    parsed_review = _apply_review_parse_fallback(_candidate, _individual_results)
    if parsed_review is None:
        # All reviewers failed schema parsing. This is a review-infrastructure
        # failure, not an actionable code finding — escalate directly rather than
        # injecting a synthetic P1 and looping back through DEV (which cannot fix
        # reviewer output) only to later surface as a misleading no-changes
        # escalation.
        state.phase = Phase.ESCALATE
        state.error = REVIEW_INFRASTRUCTURE_PARSE_FAILURE_REASON
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
            logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
        _escalate_notify(task, state, notify, config)
        return (
            _ReviewOutcome.ESCALATE,
            CoordinatorResult(success=False, phase=state.phase, state=state, message=state.error),
            config,
        )

    # Valid verdict — increment review cycle counter
    state.review_cycle += 1

    # ── Symptom-verification test escalation (bug-fix PRs only) ───────────────
    # A reviewer finding that the seam-level integration test for the closing
    # bug's symptom path is absent is load-bearing for shipping the fix: without
    # it, a regression on that path reaches operators undetected (the #1402 /
    # #1407 failure mode). For bug-class stories, escalate such a P2 to P1 so the
    # merge blocks until the symptom-verification test lands. The escalation is
    # applied to BOTH the merged review (drives p1/p2 counts + the per-cycle audit
    # record) and each per-reviewer result (drives the finding classifier's
    # disposition, which the blocking decision keys on) so every downstream signal
    # sees a consistent P1. Generic "coverage could be higher" findings are
    # untouched — the detector requires an explicit seam/symptom-path signal.
    _is_bug_fix = (task.type or "").strip().lower() == "bug"
    if _is_bug_fix:
        _merged_findings, _symptom_escalations = escalate_symptom_test_findings(
            parsed_review.findings, is_bug_fix=True
        )
        if _symptom_escalations:
            parsed_review = _dc_replace(parsed_review, findings=_merged_findings)
            _reescalated: list[tuple[str, ReviewResult]] = []
            for _name, _rr in state.last_cycle_reviewer_results:
                _rr_findings, _rr_escs = escalate_symptom_test_findings(
                    _rr.findings, is_bug_fix=True
                )
                if _rr_escs:
                    _rr = _dc_replace(_rr, findings=_rr_findings)
                _reescalated.append((_name, _rr))
            state.last_cycle_reviewer_results = _reescalated
            for _esc in _symptom_escalations:
                state.symptom_test_escalations.append({"review_cycle": state.review_cycle, **_esc})
            _esc_descs = "; ".join(e["description"][:80] for e in _symptom_escalations)
            _log(
                f"  ↑ {len(_symptom_escalations)} P2→P1 escalation(s) "
                f"(missing seam-level symptom test on bug-fix PR): {_esc_descs}"
            )

    state.review_results.append(parsed_review)

    _review_elapsed = time.monotonic() - _review_pool_start
    _p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
    _p2_count = sum(1 for f in parsed_review.findings if f.severity == "P2")
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "REVIEW",
                "iteration": state.review_cycle,
                "cost_usd": state.total_cost_measured,
                "coordinator_state": state,
                **live_complexity_fields(
                    state.preflight_complexity, state.preflight_complexity_score
                ),
                "detail": {
                    # Re-include cycle context so the wholesale detail replace at
                    # story_state.transition does not wipe STAGE after a cycle
                    # completes while DETAIL shows the P1/P2 counts.
                    "review_cycle": state.review_cycle,
                    "review_max_cycles": (
                        state.adaptive_review_max or config.retry.max_review_cycles
                    ),
                    "review_p1": _p1_count,
                    "review_p2": _p2_count,
                },
            }
        )
    _review_cost = sum_costs(
        r.cost_usd for r in state.review_agent_results[_review_results_before_cycle:]
    )

    # ── Finding classification (in-process) ───────────────────────────────
    # update_finding_registry operates on coordinator-internal dataclasses
    # (FindingRecord, ReviewFinding) and exists to drive coordinator decisions.
    # It must run against the launched (installed) version's schema, not the
    # worktree's — running it in a subprocess with PYTHONPATH=worktree/src
    # silently swaps the dataclass shape across the parent/child boundary and
    # crashes on any internal refactor (see #1386). Run in-process: there is
    # no per-worktree state mutation to isolate, and no project-owned code is
    # imported.
    from theforge.finding_classifier import update_finding_registry as _update_finding_registry

    _classified = _update_finding_registry(
        state=state,
        cycle_results=list(state.last_cycle_reviewer_results),
        workspace_path=workspace_path,
        cycle_num=state.review_cycle,
        prev_commit=state.last_dev_start_commit
        if isinstance(state.last_dev_start_commit, str)
        else None,
    )

    _allow_net_new_bypass = config.finding_classifier.allow_net_new_bypass
    _log(f"  [finding_classifier] allow_net_new_bypass={_allow_net_new_bypass}")

    # ── Diff grounding (eligibility precondition for every blocking path) ─────
    # A P1 may only decide this story's outcome when it is about this story's
    # change. Grounding is checked against the story's whole merge-base-to-HEAD
    # diff — NOT state.last_dev_start_commit, which describes only the latest dev
    # iteration and would strand findings about files touched in an earlier one.
    # A finding that cannot be tied to the diff (file not touched, no resolvable
    # file cited, diff uncomputable) is recorded as diff_ungrounded: visible in
    # the registry and in the audit's non_blocking_p1s, blocking nothing, and not
    # handed back to dev as work to fix. This runs BEFORE the gate-contradiction
    # downgrade, the AC-violation override, the allow_net_new_bypass promotion,
    # and the cycle blocking checks, so no promotion path can reach a blocking
    # disposition without having passed it (#2525).
    _grounding = ground_p1_records(
        _classified,
        workspace_path,
        config.workspace.base_branch,
        story_diff=_story_diff_for_review(state, task, workspace_path),
        log=_log,
    )
    _record_grounding(state, _grounding)
    _story_changed_files = _grounding.changed_files
    _ungrounded_p1s = list(_grounding.ungrounded)
    # True when this cycle produced P1 evidence and none of it survived grounding.
    # No disposition assigned below moves a record into or out of diff_ungrounded,
    # so this is stable for the rest of the phase. The story-changed-nothing case
    # is excluded: with no change to judge, "no finding is about this change" is
    # not a reason to stop blocking.
    _only_ungrounded_p1s = _grounding.only_ungrounded and not _grounding.story_changed_nothing
    # An ungrounded finding is not work for the dev agent. When a retry is driven
    # by some OTHER, grounded blocker, the handoff must not also carry back a
    # sibling story's acceptance criteria as something to fix — the dev agent has
    # no way to satisfy it and would churn trying. The findings stay in the
    # registry and in the audit; only the actionable handoff is narrowed. P2s are
    # left alone: they are advisory, never decide the outcome, and the P2 policy
    # already asks the dev to judge proximity for itself (#2525).
    _dev_handoff_review = parsed_review
    if _ungrounded_p1s:
        _dev_handoff_review = _dc_replace(
            parsed_review,
            findings=[
                f
                for f in parsed_review.findings
                if f.severity != "P1"
                or is_diff_grounded(f.file, _story_changed_files, workspace_path)
            ],
        )

    # ── Gate-contradiction downgrade (assertion-based) ────────────────────────
    # A PASS gate mechanically contradicts exactly one class of P1: a claim that
    # tests/build/lint are currently FAILING.  It has no bearing on a claim that
    # coverage is inadequate, that acceptance evidence was never produced, or that
    # a criterion remains undemonstrated — all of which are entirely consistent
    # with a green gate.  Suppression is therefore derived from what a finding
    # *asserts* (asserts_gate_verifiable_failure), not from its subject matter,
    # and it fails closed: a finding whose assertion is not established as a
    # gate-verifiable failure keeps blocking.
    # No downgrade occurs when the gate decision is FAIL, BLOCKED, or absent.
    _last_gate = state.gate_decisions[-1] if state.gate_decisions else None
    if _last_gate == "PASS":
        for _rec in _classified:
            if _rec.severity != "P1":
                continue
            if _rec.disposition == "diff_ungrounded":
                # Already non-blocking for a stronger reason; keep the more
                # specific disposition rather than restating it as a gate
                # contradiction.
                continue
            if asserts_gate_verifiable_failure(_rec.description):
                _log(
                    f"  ↷ gate_contradicted: P1 downgraded"
                    f" (gate={_last_gate}, asserts gate-verifiable failure):"
                    f" {_rec.description[:80]}"
                )
                _rec.disposition = "gate_contradicted"  # type: ignore[assignment]

    # ── AC-violation override (runs after every disposition assignment) ───────
    # A reviewer that returned matches_spec=false asserts the story was not
    # completed correctly.  Any P1 from that reviewer must block — even one a
    # mechanical signal downgraded to gate_contradicted, since a green gate cannot
    # contradict a claim that acceptance evidence is missing.  This override runs
    # AFTER the gate-contradiction downgrade and inspects P1s regardless of their
    # current disposition, so no suppression can run ahead of the guard written to
    # catch exactly this error, whatever order dispositions were assigned in.
    # It does NOT re-block a diff_ungrounded P1: a reviewer's self-reported
    # matches_spec flag is not evidence that a finding about a file this story
    # never touched describes this story's change (#2525).
    _ac_failing_reporters = {
        name for name, rr in state.last_cycle_reviewer_results if not rr.story_matches
    }
    _ac_reblocked = [
        _rec
        for _rec in _classified
        if _rec.severity == "P1"
        and _rec.reporter in _ac_failing_reporters
        and _rec.disposition in ("net_new", "gate_contradicted")
    ]
    if _ac_reblocked:
        # Persist the AC-blocking classification so the audit trail records these
        # findings as ac_blocking rather than net_new/gate_contradicted.
        for _rec in _ac_reblocked:
            _rec.disposition = "ac_blocking"  # type: ignore[assignment]
        _ac_descs = "; ".join(r.description[:80] for r in _ac_reblocked)
        _log(
            f"  ✗ {len(_ac_reblocked)} P1(s) blocked"
            f" (AC-blocking: reviewer indicated matches_spec=false): {_ac_descs}"
        )

    # Baseline-vs-no-baseline classification must key on how many real reviewer
    # verdicts have actually been merged, not on state.review_cycle: engine.py's
    # VALIDATE phase also advances review_cycle (for gate/convention findings,
    # before any reviewer has run), so a story whose VALIDATE phase opened a
    # cycle first would otherwise see its first-ever reviewer verdict wrongly
    # classified against a nonexistent baseline. trajectory_cycle is incremented
    # exactly once per merged verdict (below, after this branch), so its value
    # here is the count of PRIOR verdicts already processed for this story.
    if state.trajectory_cycle >= 1:
        # has_blocking_p1 / net_new_p1s inlined to avoid importing theforge.finding_classifier.
        # Logic is identical to the functions in finding_classifier.py.
        # gate_contradicted is intentionally excluded: these findings are mechanically
        # disproven by a PASS gate and must not block approval.  The AC-violation
        # override above has already re-blocked any gate_contradicted P1 from a
        # matches_spec=false reviewer, so only genuinely test-failure-asserting
        # findings remain gate_contradicted here.
        # diff_ungrounded is excluded for the same structural reason: a finding
        # that could not be checked against this story's change is not evidence
        # about it, so it is neither blocking nor eligible for any promotion
        # below (#2525).
        _BLOCKING_DISPOSITIONS = {"unresolved", "regression", "corroborated_new", "ac_blocking"}
        _blocking_p1 = any(
            r.severity == "P1" and r.disposition in _BLOCKING_DISPOSITIONS for r in _classified
        )
        _nonblocking_p1s = [
            r for r in _classified if r.severity == "P1" and r.disposition == "net_new"
        ]
        _gate_contradicted_p1s = [
            r for r in _classified if r.severity == "P1" and r.disposition == "gate_contradicted"
        ]
        # When allow_net_new_bypass is disabled, net-new P1s are treated as blocking.
        # Persist the disposition change so the audit trail records these as blocking,
        # not as net_new (which audit.py would serialize under non_blocking_p1s).
        # Only grounded net-new P1s reach here: ungrounded ones were already
        # re-dispositioned above and are absent from _nonblocking_p1s.
        if not _allow_net_new_bypass and _nonblocking_p1s:
            _blocking_p1 = True
            for _rec in _nonblocking_p1s:
                _rec.disposition = "ac_blocking"  # type: ignore[assignment]
            _flag_descs = "; ".join(r.description[:80] for r in _nonblocking_p1s)
            _log(
                f"  ✗ {len(_nonblocking_p1s)} net-new P1(s) blocked"
                f" (flag: allow_net_new_bypass=false): {_flag_descs}"
            )
            _nonblocking_p1s = []
        # Fallback: if the merged review has P1s but none were classified (e.g., synthetic
        # P1 injection when all reviewers failed to produce parseable output), block
        # traditionally to avoid silently passing an unknown failure.
        # gate_contradicted and diff_ungrounded P1s are accounted for and must not
        # trigger this fallback.
        if (
            not _blocking_p1
            and not _nonblocking_p1s
            and not _gate_contradicted_p1s
            and not _ungrounded_p1s
            and _p1_count > 0
        ):
            _blocking_p1 = True
    else:
        # Cycle 1: any P1 is blocking (no prior baseline to classify against).
        # Gate-contradicted and diff-ungrounded P1s are non-blocking even on
        # cycle 1 — "first cycle" is not a reason to let an uncheckable finding
        # decide the outcome.
        if _classified:
            _blocking_p1 = any(
                r.severity == "P1"
                and r.disposition not in ("gate_contradicted", "diff_ungrounded")
                for r in _classified
            )
        else:
            _blocking_p1 = _p1_count > 0
        _nonblocking_p1s = []

    # ── Unsatisfied-criterion → blocking (single source of truth) ──────────────
    # A merged story_matches=false is blocking on its own, independent of P1 count
    # or disposition. Fold it into _blocking_p1 HERE — before trajectory /
    # early-termination / effective-approve — so every DONE-exit path keys on one
    # blocking signal instead of each re-deriving the criterion check. A review can
    # be schema-legal with verdict APPROVE (or REQUEST_CHANGES with every P1
    # suppressed) and still report matches_spec=false with zero blocking P1s;
    # deriving the check only at the effective-approve step let the zero-findings
    # early-termination branch finalize DONE ahead of it. Merged story_matches is
    # all()-over-valid-reviewers, so parse-failed reviewers (which default
    # story_matches=False) do not spuriously block a clean fallback approval.
    # One exception: when this cycle's ONLY P1 evidence is diff_ungrounded, the
    # criterion the reviewer says is unsatisfied is the criterion those findings
    # describe — and they were just established to be about something other than
    # this change. Re-blocking here would reinstate through the merged flag
    # exactly what was made non-blocking finding-by-finding (#2525). A
    # matches_spec=false backed by a grounded P1, or by no P1 evidence at all
    # (nothing to ground, so nothing was suppressed), still blocks as before.
    if not parsed_review.story_matches and not _blocking_p1:
        if _only_ungrounded_p1s:
            _log(
                f"  ↷ REVIEW   matches_spec=false not blocking — all "
                f"{len(_ungrounded_p1s)} P1(s) backing it are diff_ungrounded "
                f"(no finding checkable against this story's diff)"
            )
        else:
            _blocking_p1 = True
            _log(
                "  ✗ REVIEW   matches_spec=false — story does not match spec; blocking "
                "approval on the unsatisfied criterion (independent of P1 count)"
            )

    # ── Trajectory classification (in-process) ─────────────────────────────
    # Runs for EVERY successfully merged parsed_review (APPROVE, exhausted, retry).
    # Uses a dedicated monotonic counter (trajectory_cycle) that is never reset
    # or decremented by extend/reject/exhausted-gate paths — unlike review_cycle.
    # classify_families is orchestrator-internal: see the in-process rationale
    # at the update_finding_registry call site above (#1386).
    state.trajectory_cycle += 1

    # Snapshot this cycle's findings as plain dicts for cross-cycle matching
    _finding_snapshot: list[dict] = [
        {
            "file": f.file,
            "line": f.line,
            "description": f.description,
            "severity": f.severity,
        }
        for f in parsed_review.findings
    ]
    state.review_cycle_findings.append((state.trajectory_cycle, _finding_snapshot))

    # Classify families on cycle 2+ (need at least one prior cycle to match against)
    if state.trajectory_cycle >= 2:
        from theforge.review_finding_classifier import classify_families as _classify_families

        def _rf_from_snapshot(d: dict) -> ReviewFinding:
            return ReviewFinding(
                severity=d.get("severity", "P1"),
                file=d.get("file", ""),
                line=d.get("line"),
                observed=d.get("observed", d.get("description", "")),
                suggestion=d.get("suggestion"),
                expected=d.get("expected", ""),
                evidence=d.get("evidence", ""),
            )

        _updated_store, _surviving = _classify_families(
            current_findings=list(parsed_review.findings),
            current_cycle=state.trajectory_cycle,
            trajectory_store=state.finding_trajectory,
            prior_cycle_findings=[
                (cycle_num, [_rf_from_snapshot(f) for f in findings])
                for cycle_num, findings in state.review_cycle_findings[:-1]
            ],
        )
        state.finding_trajectory = _updated_store
        state.surviving_families = _surviving
    else:
        state.surviving_families = []

    # ── Topology-walk detection (#2372) ────────────────────────────────────
    # Counting findings cannot separate a change that is converging from one
    # that resolves each finding correctly and then discovers the same concern
    # somewhere new. The detector reads the family trajectory just classified
    # plus the registry dispositions and returns evidence only when the
    # sequence unambiguously shows the second shape; every ambiguity returns
    # None, so the failure mode is one more dev cycle rather than halting work
    # that was about to finish. Routing on it happens below, at the
    # REQUEST_CHANGES branch — this is only the computation, done here so the
    # signal reaches the trajectory sidecar and the audit record even on the
    # cycles it does not route.
    state.review_topology_signal = detect_topology_walk(
        trajectory_cycle=state.trajectory_cycle,
        review_cycle_findings=state.review_cycle_findings,
        finding_trajectory=state.finding_trajectory,
        finding_registry=state.finding_registry,
        review_cycle=state.review_cycle,
    )

    save_trajectory_state(workspace_path, state)
    _adaptive_review_max = state.adaptive_review_max or config.retry.max_review_cycles
    _record_review_iteration_telemetry(
        state,
        parsed_review,
        review_cost=_review_cost,
        review_elapsed=_review_elapsed,
        max_iterations=_adaptive_review_max,
    )

    _log_review_findings(parsed_review, _p1_count, _p2_count, _review_cost, logger, task)

    # ── Early termination: consecutive zero-new-findings cycles ────────
    # Stop the review loop regardless of verdict when the reviewer converges
    # (produces zero new findings for N consecutive iterations).  This saves
    # budget on stories where the reviewer is stuck restating the same issues.
    _zero_stop = config.retry.review_zero_findings_stop
    if _zero_stop > 0 and len(state.review_iteration_telemetry) >= _zero_stop:
        _tail = state.review_iteration_telemetry[-_zero_stop:]
        if all(sum(t.new_findings_by_severity.values()) == 0 for t in _tail):
            _log(
                f"  ⏹ REVIEW  early termination after {_zero_stop} consecutive cycles "
                f"with zero new findings"
            )
            state.review_early_terminated = True
            if not _blocking_p1:
                # No blocking P1 — treat as APPROVE path.
                _append_cycle_history(state, parsed_review)
                _release_review_reservation(
                    state, retained_cycles=0, reason="approve_early_termination"
                )
                return (
                    _ReviewOutcome.DONE,
                    _finalize_approve(
                        state,
                        config,
                        task,
                        parsed_review,
                        workspace_path,
                        branch_name,
                        task_start,
                        auto_merge=auto_merge,
                        notify=notify,
                        logger=logger,
                        review_cost=_review_cost,
                        review_elapsed=_review_elapsed,
                        message=(
                            f"Task '{task.name}' completed. "
                            f"Review converged (early termination: zero new findings for "
                            f"{_zero_stop} consecutive cycles)."
                        ),
                        run_id=run_id,
                    ),
                    config,
                )
            # Blocking P1 persists but reviewer has converged — escalate so
            # the persistent issue gets human attention rather than looping.
            state.phase = Phase.ESCALATE
            state.escalate_kind = "content"
            state.error = (
                f"Review converged with unresolved blocking P1(s) after "
                f"{_zero_stop} consecutive zero-new-findings cycles."
            )
            if logger:
                logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
            return (
                _ReviewOutcome.ESCALATE,
                CoordinatorResult(
                    success=False,
                    phase=Phase.ESCALATE,
                    state=state,
                    message=state.error,
                ),
                config,
            )

    # ── APPROVE (or disposition-gated pass) ─────────────────────────
    # _blocking_p1 is the single source of truth for whether this cycle may pass:
    # it already folds in blocking P1 dispositions, the net-new/AC-blocking
    # overrides, and (above) the unsatisfied-criterion signal. A cycle is
    # approve-equivalent exactly when nothing blocks — this covers both a genuine
    # APPROVE verdict and a REQUEST_CHANGES verdict whose P1s are all non-blocking
    # net_new (net_new_pass). Cross-validation forbids APPROVE with P1 findings, so
    # the only way an APPROVE verdict carries _blocking_p1 is the matches_spec=false
    # fold — which must correctly deny approval.
    _effective_approve = not _blocking_p1

    # ── Empty-diff guard ────────────────────────────────────────────
    # APPROVE on a branch with zero commits ahead of base is a workflow failure,
    # not a real approval — refuse to advance to DONE.
    if _effective_approve and not _has_commits_ahead_of_base(
        workspace_path, config.workspace.base_branch
    ):
        state.phase = Phase.ESCALATE
        state.escalate_kind = "content"
        state.error = (
            "Review verdict APPROVE on a branch with no commits ahead of base — "
            "refusing to mark DONE on an empty diff"
        )
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
            logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
        _escalate_notify(task, state, notify, config)
        return (
            _ReviewOutcome.ESCALATE,
            CoordinatorResult(
                success=False,
                phase=Phase.ESCALATE,
                state=state,
                message=state.error,
            ),
            config,
        )

    if _effective_approve and _nonblocking_p1s:
        _nb_descs = "; ".join(r.description[:80] for r in _nonblocking_p1s)
        _log(f"  ↷ {len(_nonblocking_p1s)} net-new P1(s) recorded but not blocking: {_nb_descs}")

    if _effective_approve:
        _verdict_label = (
            "APPROVE" if parsed_review.verdict == "APPROVE" else "REQUEST_CHANGES→net_new_pass"
        )
        _log(
            f"  ✓ REVIEW   {_verdict_label}  {_p1_count} P1  {_p2_count} P2"
            f"  {_fmt_cost(_review_cost)}  {_fmt_duration(_review_elapsed)}"
        )
        _append_cycle_history(state, parsed_review)
        # ── P2 cleanup (post-APPROVE advisory iterations) ──────────────
        # Default behaviour in both interactive and non-interactive modes:
        # when the review left open P2s and dev iteration budget remains,
        # re-enter DEV with the carried P2 list. Budget exhaustion, no
        # remaining carried P2s, an iteration cap, or p2_cleanup_enabled=
        # false all fall through to the existing approve handler.
        if _maybe_enter_p2_cleanup(state, config, parsed_review):
            # Review has approved: of the cycles the reserve was priced against,
            # only the re-review of this cleanup pass is still reachable. Release
            # the rest BEFORE the dev dispatch this returns to is checked for
            # funding, so cleanup is funded from money that provably cannot be
            # spent on review (#2340).
            _release_review_reservation(state, retained_cycles=1, reason="approve_p2_cleanup")
            _entry = state.p2_cleanup_audit[-1]
            # Remember the commit this approval was taken on, if the gate had
            # passed on that exact commit. Cleanup spends the dev pool and never
            # buys a review cycle, so an iteration that breaks the gate is
            # terminal — this is the floor under that risk (#2028).
            _checkpoint = _record_gate_green_checkpoint(
                state,
                parsed_review,
                carried_p2_count=_entry["remaining_carried_p2_count"],
            )
            if _checkpoint is not None:
                _log(
                    f"  ⎈ CHECKPOINT  {_checkpoint.commit[:8]} is gate-green and "
                    "review-approved; it is the floor if cleanup breaks the gate"
                )
            _log(
                f"  ↻ P2 CLEANUP  entering dev iteration "
                f"({_entry['remaining_carried_p2_count']} carried P2(s), "
                f"budget_remaining={_entry['budget_remaining']})"
            )
            if logger:
                logger._safe_emit(
                    "phase_end",
                    phase="REVIEW",
                    outcome="approve_p2_cleanup",
                    cost_usd=_round_cost(_review_cost),
                    duration_s=round(_review_elapsed, 2),
                )
                logger._safe_emit(
                    "p2_cleanup",
                    action=_entry["action"],
                    p2_count=_entry["p2_count"],
                    carried_p2_count=_entry["carried_p2_count"],
                    remaining_carried_p2_count=_entry["remaining_carried_p2_count"],
                    review_cycle=_entry["review_cycle"],
                    dev_iteration=_entry["dev_iteration"],
                    budget_remaining=_entry["budget_remaining"],
                )
            return _ReviewOutcome.RETRY_DEV, None, config
        # Cleanup was either skipped or terminated.
        # The reservation is released where the branch PROVES the cycles it was
        # priced against cannot run — not merely where review approved. An
        # interactive session may still send this back through REVIEW on a
        # reject or an extend, so the release for that path happens inside the
        # decision handler, on its approve branch only.
        if interactive:
            return _handle_interactive_review_decision(
                state,
                config,
                task,
                parsed_review,
                workspace_path,
                branch_name,
                task_start,
                auto_merge=auto_merge,
                notify=notify,
                review_cost=_review_cost,
                review_elapsed=_review_elapsed,
                run_id=run_id,
                exhausted_cycles=False,
                history_already_appended=True,
            )
        else:
            # The run finalizes here: no review cycle is reachable any more, so
            # nothing of the reserve is still review's.
            _release_review_reservation(state, retained_cycles=0, reason="approve_final")
            return (
                _ReviewOutcome.DONE,
                _finalize_approve(
                    state,
                    config,
                    task,
                    parsed_review,
                    workspace_path,
                    branch_name,
                    task_start,
                    auto_merge=auto_merge,
                    notify=notify,
                    logger=logger,
                    review_cost=_review_cost,
                    review_elapsed=_review_elapsed,
                    message=(
                        f"Task '{task.name}' completed. "
                        f"Review approved after {state.review_cycle} cycle(s), "
                        # budget.total_count, not state.dev_iteration: the latter
                        # aliases the per-cycle counter, which reset_cycle() zeroes
                        # whenever a new review cycle opens, so a multi-cycle story
                        # would report only its final cycle's dev calls (#1983).
                        f"{state.budget.total_count} dev iteration(s). "
                    ),
                    run_id=run_id,
                ),
                config,
            )

    # ── REQUEST_CHANGES (blocking P1s present) ───────────────────
    # If we were in P2 cleanup and this review brought back blocking P1s,
    # the cleanup pass regressed. Exit cleanup so the engine resets the
    # per-cycle budget normally for the upcoming blocking-fix cycle.
    if state.p2_cleanup_active:
        state.p2_cleanup_audit.append(
            {
                "action": "exit_regression",
                "review_cycle": state.review_cycle,
                "dev_iteration": state.dev_iteration,
                "p2_count": _p2_count,
                "p1_count": _p1_count,
                "budget_remaining": state.budget.remaining(),
                "cleanup_iterations": state.p2_cleanup_iterations,
            }
        )
        _clear_p2_cleanup_state(state)
    _is_persistent_p1 = False
    if config.models is not None and len(state.review_results) >= 2:
        _prev_result = state.review_results[-2]
        _is_persistent_p1 = _has_persistent_p1(parsed_review.findings, _prev_result.findings)

    _persistent_tag = " (persistent-P1)" if _is_persistent_p1 else ""
    _log(
        f"  ✗ REVIEW   REQUEST_CHANGES  {_p1_count} P1{_persistent_tag}  {_p2_count} P2"
        f"  {_fmt_cost(_review_cost)}  {_fmt_duration(_review_elapsed)}"
    )

    # Escalate dev model on persistent P1 (only when explicitly enabled via forge.yaml)
    if (
        config.assignment.adaptive_enabled
        and config.retry.auto_model_escalation
        and _is_persistent_p1
        and not state.dev_escalated
        and (
            state.total_dev_cost
            < (state.adaptive_dev_cost_estimate_usd or config.dev_profile.budget_usd)
        )
    ):
        _esc = _perform_dev_model_escalation(config)
        if _esc is not None:
            _old_model, _new_model_name, config = _esc
            _p1_file = next(
                (f.file for f in parsed_review.findings if f.severity == "P1"),
                "unknown",
            )
            _log(
                f"  Dev escalation: {_old_model} → {_new_model_name} (persistent P1 in {_p1_file})"
            )
            state.dev_escalated = True
            _prev_result = state.review_results[-2]
            _persistent_descs = _persistent_p1_descriptions(
                parsed_review.findings, _prev_result.findings
            )
            _record_persistent_p1_dev_escalation(
                state,
                previous_model=_old_model,
                escalated_model=_new_model_name,
                persistent_descriptions=_persistent_descs,
                p1_file=_p1_file,
            )
            state.escalation_note = (
                f"MODEL ESCALATION: A P1 finding persisted across review cycles. "
                f"The previous model ({_old_model}) was unable to resolve it. "
                f"You are now running on an upgraded model ({_new_model_name}). "
                f"Persistent finding(s): {'; '.join(_persistent_descs)}"
            )

    # ── Topology walk detected before the ceiling (#2372) ──────────────────
    # The loop is not converging: each cycle has resolved its predecessor and
    # raised the same concern somewhere new. Another dev pass would price
    # discovery as implementation and arrive at the same operator decision
    # several cycles later, so route to the escalate gate NOW and let the
    # advisor reason about the framing rather than about the latest finding.
    #
    # Guarded on review_cycle (the budget counter) staying below the ceiling:
    # at or above it the exhausted-cycles branch below is already the right
    # exit, and firing here too would escalate the same cycle twice. The family
    # span is measured in trajectory_cycle, which is a different counter — it
    # is never decremented by extend/reject/gate-continue — so the two are
    # never compared to each other.
    #
    # Routed once per run: a gate "continue" is the operator choosing to keep
    # going with the pattern in hand, and re-raising it on the very next cycle
    # would spend the decision they just made.
    if (
        state.review_topology_signal
        and not state.review_topology_escalated
        and state.review_cycle < _adaptive_review_max
    ):
        _signal = state.review_topology_signal
        _cycle_seq = ", ".join(str(c) for c in _signal.get("cycles", []))
        state.review_topology_escalated = True
        # Names THIS escalation as the detector's, so the advisor is told the
        # ceiling was not reached. Cleared on the continue path below, so a
        # later ceiling-triggered escalation is not misdescribed as early.
        state.review_topology_triggered = True
        state.phase = Phase.ESCALATE
        state.escalate_kind = "content"
        state.error = (
            f"Topology walk detected at review cycle {state.review_cycle} of "
            f"{_adaptive_review_max}: cycles {_cycle_seq} each resolved the previous "
            f"cycle's findings and raised a new instance of the same concern "
            f"({_signal.get('seed_anchor')!r}) at a location not previously flagged. "
            f"The loop is inventorying a surface, not converging — escalating for a "
            f"decision about the story's framing instead of spending another "
            f"development cycle."
        )
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
            logger._safe_emit(
                "escalate",
                reason=state.error,
                phase="REVIEW",
                topology_signal=_signal,
            )
        # Append BEFORE the gate so the advisor's evidence packet contains the
        # cycle that triggered the escalation; the gate is told not to append it
        # again on its approve paths.
        _append_cycle_history(state, parsed_review)
        # Persist the routed flag with the signal, so a --resume does not
        # re-escalate a pattern the operator has already decided on.
        save_trajectory_state(workspace_path, state)
        _gate_result = _run_escalate_gate(
            state,
            config,
            task,
            workspace_path,
            branch_name,
            task_start,
            auto_merge=auto_merge,
            notify=notify,
            logger=logger,
            run_id=run_id,
            history_already_appended=True,
        )
        if _gate_result is not None:
            return _ReviewOutcome.ESCALATE, _gate_result, config
        # Gate said "continue". Unlike the exhausted-cycles branch below there is
        # no review_cycle increment to undo — this escalation happened with
        # budget still available, so the cycles already run stay run and the loop
        # resumes from where it is rather than replaying them.
        state.error = None
        state.escalate_kind = None
        state.review_topology_triggered = False
        state.last_review_findings = review_to_dev_handoff(_dev_handoff_review)
        state.budget.reset_cycle()
        state.human_feedback = None
        state.retry_reason = RetryReason.REVIEW_CHANGES
        save_trajectory_state(workspace_path, state)
        return _ReviewOutcome.RETRY_DEV, None, config

    if state.review_cycle >= _adaptive_review_max:
        if interactive:
            return _handle_interactive_review_decision(
                state,
                config,
                task,
                parsed_review,
                workspace_path,
                branch_name,
                task_start,
                auto_merge=auto_merge,
                notify=notify,
                review_cost=_review_cost,
                review_elapsed=_review_elapsed,
                run_id=run_id,
                exhausted_cycles=True,
            )
        else:
            state.phase = Phase.ESCALATE
            state.escalate_kind = "content"
            state.error = (
                f"Review requested changes after {state.review_cycle} cycles. "
                f"Max cycles ({_adaptive_review_max}) exhausted."
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
            _gate_result = _run_escalate_gate(
                state,
                config,
                task,
                workspace_path,
                branch_name,
                task_start,
                auto_merge=auto_merge,
                notify=notify,
                logger=logger,
                run_id=run_id,
            )
            if _gate_result is not None:
                return _ReviewOutcome.ESCALATE, _gate_result, config
            # Gate said "continue" — undo review_cycle increment so next cycle is valid
            state.review_cycle -= 1
            return _ReviewOutcome.RETRY_DEV, None, config

    # Within budget — feed findings back to dev
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="REVIEW",
            outcome="request_changes",
            cost_usd=_round_cost(_review_cost),
            duration_s=round(_review_elapsed, 2),
        )
    _append_cycle_history(state, parsed_review)
    state.last_review_findings = review_to_dev_handoff(_dev_handoff_review)
    state.budget.reset_cycle()
    state.human_feedback = None
    state.retry_reason = RetryReason.REVIEW_CHANGES
    _log_verbose(f"Sending {len(parsed_review.findings)} findings back to dev agent")
    return _ReviewOutcome.RETRY_DEV, None, config


# ── Review-only phase ─────────────────────────────────────────────────


def _run_review_only_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    story_content: str,
    workspace_path: Path,
    branch_name: str,
    *,
    notify: bool,
    logger: StructuredLogger | None,
    task_start: float,
    batch_context: BatchReviewContext | None = None,
) -> CoordinatorResult:
    """Run the REVIEW phase for the review-only entry point.

    No DEV retry: REQUEST_CHANGES → ESCALATE immediately.

    ``batch_context`` is supplied when this story is a non-leader member of a
    batch group being reviewed on the leader's worktree. Its presence — not the
    handoff it carries — is what tells grounding the branch holds more than this
    story, so the member is judged against its own commits rather than its
    siblings' changes too (#2525).
    """
    state.phase = Phase.REVIEW
    if logger:
        logger._safe_emit("phase_start", phase="REVIEW", iteration=1)
    state.review_cycle = 1
    state.budget.reset_cycle()
    _pool_model_names_ro = "+".join(p.model for p in config.review_pool)
    _log_phase(state.phase, f"{_pool_model_names_ro}  cycle=1  (review-only)")

    commit_log = _get_commit_log(workspace_path, config.workspace.base_branch)
    diff_stat = _get_diff_stat(workspace_path, config.workspace.base_branch)
    diff_content = _get_diff_content(workspace_path, config.workspace.base_branch)
    commit_diffs = _get_commit_diffs(workspace_path, config.workspace.base_branch)
    _forge_path = _latest_forge_handoff_path(state)
    handoff_content = _get_handoff_content(forge_handoff_path=_forge_path)
    dev_notes = _get_dev_notes(workspace_path, forge_handoff_path=_forge_path)
    handoff_commit_warning = _get_handoff_commit_warning(
        workspace_path, config.workspace.base_branch, forge_handoff_path=_forge_path
    )
    if logger:
        logger._safe_emit(
            "review_git_context",
            base_branch=config.workspace.base_branch,
            commit_log=commit_log,
            diff_stat=diff_stat,
            handoff_commit_warning=handoff_commit_warning,
        )
    review_context = ContextAssembler.from_config(config).assemble(
        phase="review",
        story_text=story_content,
        file_list=plan_file_list(state.plan_structured) or None,
        agent_role="review",
        phase_iteration=state.review_cycle,
    )
    state.context_manifests.append({"phase": "review", "manifest": review_context})
    review_prompt = build_review_prompt(
        task,
        story_content=story_content,
        commit_log=commit_log,
        diff_stat=diff_stat,
        diff_content=diff_content,
        commit_diffs=commit_diffs,
        workspace_path=str(workspace_path),
        branch=branch_name,
        handoff_content=handoff_content,
        handoff_commit_warning=handoff_commit_warning,
        mode=config.review_pool[0].mode,
        dev_notes=dev_notes,
        cycle_history=None,
        conventions=config.conventions_soft,
        **hard_convention_review_kwargs(config),
        assembled_context=review_context,
        sandboxed=state.sandboxed,
        containment=state.dev_containment,
        authoritative_gate_decision=state.last_gate_decision,
        authoritative_gate_commit=state.last_gate_commit,
        **gate_profile_prompt_kwargs(state),
        p2_policy=config.dev.p2_policy,
    )

    meta = ReviewCycleMetadata(
        pool_models=[p.name for p in config.review_pool],
        successful=[],
        failed=[],
        synthesized=False,
    )
    _record_reviewed_commit_provenance(state, meta, workspace_path)
    state.review_cycle_metadata.append(meta)

    _pool_start = time.monotonic()
    successful, failed_results, parsed_review, _individual, _named_parsed = _run_review_pool(
        state,
        config,
        task,
        story_content,
        workspace_path,
        branch_name,
        meta,
        notify=notify,
        review_prompts=review_prompt,
        enforce_budgets=False,
        logger=logger,
    )
    state.last_cycle_reviewer_results = _named_parsed
    _pool_elapsed = time.monotonic() - _pool_start

    if parsed_review is None:
        _log(f"✗ ESCALATE   {state.error}")
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    state.review_results.append(parsed_review)

    if parsed_review.parse_errors:
        _log_verbose(f"Review parse errors: {parsed_review.parse_errors}")
        canonical_summary = f"PARSE ERROR: {parsed_review.summary}"
        parsed_review = ReviewResult(
            verdict="REQUEST_CHANGES",
            summary=canonical_summary,
            findings=parsed_review.findings,
            story_matches=parsed_review.story_matches,
            story_mismatches=parsed_review.story_mismatches,
            test_adequate=parsed_review.test_adequate,
            test_gaps=parsed_review.test_gaps,
            parse_errors=parsed_review.parse_errors,
            raw_yaml=parsed_review.raw_yaml,
        )
        state.review_results[-1] = parsed_review

    _ro_p1 = sum(1 for f in parsed_review.findings if f.severity == "P1")
    _ro_p2 = sum(1 for f in parsed_review.findings if f.severity == "P2")
    _ro_cost = sum_costs(r.cost_usd for r in state.review_agent_results)
    _ro_elapsed = _pool_elapsed

    _log_review_findings(parsed_review, _ro_p1, _ro_p2, _ro_cost, logger, task)

    # ── Diff grounding (same eligibility precondition as the retry loop) ───────
    # Review-only has no DEV retry, so its REQUEST_CHANGES → ESCALATE step is the
    # whole outcome decision — and it is the path sprint batch members are
    # reviewed through, i.e. exactly the batching scenario #2525 was reported
    # from. It must therefore run the same grounding check, not a weaker one: a
    # P1 naming a file this story never touched cannot fail it here either.
    # Classifying the cycle first is what puts the dispositions in
    # state.finding_registry, so the audit records why a finding was set aside.
    #
    # For a batch member the worktree's branch diff is the whole group's change,
    # so grounding against it would let a sibling member's findings ground here.
    # batch_context narrows the set to this member's own commits, and when its
    # attribution is unusable leaves the set unknown rather than falling back to
    # the branch — a fallback would reinstate exactly the cross-member grounding
    # it exists to prevent.
    from theforge.finding_classifier import update_finding_registry as _update_finding_registry

    _classified_ro = _update_finding_registry(
        state=state,
        cycle_results=list(state.last_cycle_reviewer_results),
        workspace_path=workspace_path,
        cycle_num=state.review_cycle,
        prev_commit=None,
    )
    _grounding_ro = ground_p1_records(
        _classified_ro,
        workspace_path,
        config.workspace.base_branch,
        story_diff=_story_diff_for_review(
            state, task, workspace_path, batch_context=batch_context
        ),
        log=_log,
    )
    _record_grounding(state, _grounding_ro)

    # A REQUEST_CHANGES whose every P1 is ungrounded rests entirely on evidence
    # that was just established not to describe this change — including its
    # matches_spec flag, which the same reviewer derived from those same
    # findings. Treat it as an approval rather than a rejection nobody can act
    # on. Fails closed everywhere else: a parse failure, a verdict with no P1
    # evidence to ground, a single grounded P1, or a story whose own file set is
    # known to be empty — nothing was implemented, so there is nothing to approve
    # and the review must stay free to say so.
    _ro_ungrounded_pass = (
        parsed_review.verdict != "APPROVE"
        and not parsed_review.parse_errors
        and _grounding_ro.only_ungrounded
        and not _grounding_ro.story_changed_nothing
    )
    if _ro_ungrounded_pass:
        _log(
            f"  ↷ REVIEW   REQUEST_CHANGES not blocking — all "
            f"{len(_grounding_ro.ungrounded)} P1(s) are diff_ungrounded "
            f"(no finding checkable against this story's diff)"
        )

    if parsed_review.verdict == "APPROVE" or _ro_ungrounded_pass:
        state.phase = Phase.DONE
        _dur = _fmt_duration(_ro_elapsed)
        # Name the basis rather than flattening both to APPROVE: a reviewer that
        # approved and one whose objections were all unverifiable against this
        # diff are different outcomes, and the operator reading the run needs to
        # tell them apart.
        _ro_label = "REQUEST_CHANGES→diff_ungrounded_pass" if _ro_ungrounded_pass else "APPROVE"
        _log(f"  ✓ REVIEW   {_ro_label}  {_ro_p1} P1  {_ro_p2} P2  {_fmt_cost(_ro_cost)}  {_dur}")
        _log(
            f"✓ DONE   total={_fmt_cost_total(state.total_cost_measured, state.total_cost)}"
            f"  {_fmt_duration(_ro_elapsed)}"
        )
        if logger:
            logger._safe_emit(
                "phase_end",
                phase="REVIEW",
                outcome="approve_diff_ungrounded" if _ro_ungrounded_pass else "approve",
                cost_usd=_round_cost(_ro_cost),
                duration_s=round(_ro_elapsed, 2),
            )
            logger._safe_emit(
                "run_end",
                outcome="done",
                total_cost_usd=_round_cost(state.total_cost_measured),
                total_duration_s=round(time.monotonic() - task_start, 2),
            )
        _ntfy_done_notify(
            task, state, config, notify, parsed_review.summary, _ro_elapsed, branch_name
        )
        return CoordinatorResult(
            success=True,
            phase=state.phase,
            state=state,
            message=(f"Task '{task.name}' review-only: {_ro_label}. Branch: {branch_name}"),
        )

    # REQUEST_CHANGES — no DEV retry in review-only mode
    state.phase = Phase.ESCALATE
    p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
    # Report what actually blocked. Counting every P1 would name findings that
    # were set aside as diff_ungrounded moments ago, sending the operator after
    # work the run itself decided was not about this story.
    _ro_ungrounded_note = (
        f" ({len(_grounding_ro.ungrounded)} further P1(s) recorded as diff_ungrounded)"
        if _grounding_ro.ungrounded
        else ""
    )
    # Counted from the classified records, which are what grounding ran over.
    # The merged-review P1 count can differ (cross-reviewer bucketing), so
    # subtracting one from the other would be arithmetic across two populations;
    # it is only the fallback for a cycle that produced no records at all.
    _ro_blocking_p1s = (
        len(_grounding_ro.p1_records) - len(_grounding_ro.ungrounded)
        if _grounding_ro.p1_records
        else p1_count
    )
    state.error = (
        f"Review requested changes ({_ro_blocking_p1s} P1 finding(s))"
        f"{_ro_ungrounded_note}. No retry in review-only mode."
    )
    _log(
        f"  ✗ REVIEW   REQUEST_CHANGES  {_ro_p1} P1  {_ro_p2} P2"
        f"  {_fmt_cost(_ro_cost)}  {_fmt_duration(_ro_elapsed)}"
    )
    _log(f"✗ ESCALATE   {state.error}")
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="REVIEW",
            outcome="escalate",
            cost_usd=_round_cost(_ro_cost),
            duration_s=round(_ro_elapsed, 2),
        )
        logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
        logger._safe_emit(
            "run_end",
            outcome="escalate",
            total_cost_usd=_round_cost(state.total_cost_measured),
            total_duration_s=round(time.monotonic() - task_start, 2),
        )
    _escalate_notify(task, state, notify, config)
    return CoordinatorResult(
        success=False,
        phase=state.phase,
        state=state,
        message=state.error,
    )
