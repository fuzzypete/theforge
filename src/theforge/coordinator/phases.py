"""Phase handler functions extracted from coordinator.py.

These implement the DEV, VALIDATE, and REVIEW phases of the coordinator
state machine.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import replace as _dc_replace
from enum import Enum, auto
from pathlib import Path

import yaml

from theforge import finding_classifier as _fc
from theforge.config import MODEL_REGISTRY, ForgeConfig
from theforge.review import (
    ReviewFinding,
    ReviewResult,
    _best_individual_result,
    review_to_dev_handoff,
)
from theforge.sessions import save_sessions
from theforge.task import TaskStory as TaskSpec  # noqa: F401
from theforge.task import build_dev_prompt, build_fix_prompt, build_review_prompt
from theforge.traces import write_trace

from . import util as _cu
from .completion import (  # noqa: F401
    _append_cycle_history,
    _archive_story_to_done,
    _create_pr,
    _finalize_approve,
)
from .gate import (
    _is_gate_skip,
    _run_gate_full,
)
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
from .review_context import (
    _get_commit_log,
    _get_dev_notes,
    _get_handoff_content,
    _get_raw_dev_notes,
)
from .review_pool import _run_review_pool
from .state import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    ReviewCycleMetadata,
)
from .util import _fmt_duration, _log, _log_phase, _log_verbose, resolve_timeout

_pr_log = logging.getLogger(__name__)

# ── Lazy runner slot ──────────────────────────────────────────────────
# None until first call; tests may replace before calling run_task.
# Patch target: theforge.coordinator.phases.run_agent — dev agent call
run_agent = None


def _ensure_runners() -> None:
    global run_agent
    if run_agent is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent


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


class _ReviewOutcome(Enum):
    DONE = auto()
    ESCALATE = auto()
    RETRY_DEV = auto()


def _run_review_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
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
) -> tuple[_ReviewOutcome, CoordinatorResult | None, ForgeConfig]:
    """Run the full REVIEW phase: pool+synthesis, parse retries, verdict handling.

    Returns (outcome, result_or_none, possibly_updated_config).
    DONE/ESCALATE → result is a CoordinatorResult.
    RETRY_DEV → result is None, caller loops back to DEV.
    config is returned because persistent-P1 model escalation may replace it.
    """
    state.phase = Phase.REVIEW
    if logger:
        logger._safe_emit("phase_start", phase="REVIEW", iteration=state.review_cycle + 1)
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
    state.review_cycle_metadata.append(meta)
    _review_cost_before_cycle = sum(r.cost_usd or 0.0 for r in state.review_agent_results)

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
    )
    state.last_cycle_reviewer_results = _named_parsed

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

    parsed_review = _candidate

    # ── Graceful empty-merge fallback ─────────────────────────────────
    if parsed_review.parse_errors:
        _log(
            f"  ⚠ review merge produced parse errors — falling back to best individual result "
            f"({len(_individual_results)} reviewer(s) with valid output)"
        )
        _fallback = _best_individual_result(_individual_results)
        if _fallback is not None:
            _log(f"  ↩ using best individual result: {_fallback.verdict}")
            parsed_review = _fallback
        else:
            _log(
                "  ⚠ all reviewers failed to produce usable output — "
                "injecting synthetic P1, returning REQUEST_CHANGES"
            )
            parsed_review = ReviewResult(
                verdict="REQUEST_CHANGES",
                summary="Review pool failed to produce a usable verdict",
                findings=[
                    ReviewFinding(
                        severity="P1",
                        file="",
                        line=None,
                        description=(
                            "All reviewers failed to produce parseable output. "
                            "Manual review required."
                        ),
                        suggestion="Check reviewer logs for details.",
                    )
                ],
                story_matches=False,
                story_mismatches=[],
                test_adequate=False,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )

    # Valid verdict — increment review cycle counter
    state.review_cycle += 1
    state.review_results.append(parsed_review)

    _review_elapsed = time.monotonic() - _review_pool_start
    _p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
    _p2_count = sum(1 for f in parsed_review.findings if f.severity == "P2")
    _review_cost = (
        sum(r.cost_usd or 0.0 for r in state.review_agent_results) - _review_cost_before_cycle
    )

    # ── Finding classification ─────────────────────────────────────────
    # Call classifier for every cycle (cycle 1 finds are all net_new as baseline).
    # Cycle 1 uses the traditional exit rule (any P1 blocks) because there is no
    # prior registry to determine which findings are latent vs. real regressions.
    # Cycle 2+: disposition-gated logic — only unresolved/regression/corroborated_new block.
    _classified = _fc.update_finding_registry(
        state=state,
        cycle_results=state.last_cycle_reviewer_results,
        workspace_path=workspace_path,
        cycle_num=state.review_cycle,
        prev_commit=state.last_dev_start_commit,
    )
    if state.review_cycle >= 2:
        _blocking_p1 = _fc.has_blocking_p1(_classified)
        _nonblocking_p1s = _fc.net_new_p1s(_classified)
        # Fallback: if the merged review has P1s but none were classified (e.g., synthetic
        # P1 injection when all reviewers failed to produce parseable output), block
        # traditionally to avoid silently passing an unknown failure.
        if not _blocking_p1 and not _nonblocking_p1s and _p1_count > 0:
            _blocking_p1 = True
    else:
        # Cycle 1: any P1 is blocking (no prior baseline to classify against)
        _blocking_p1 = _p1_count > 0
        _nonblocking_p1s = []

    _log(f"  Summary: {parsed_review.summary}")
    # Log findings grouped by severity
    _findings_by_sev: dict[str, list] = {}
    for _f in parsed_review.findings:
        _findings_by_sev.setdefault(_f.severity, []).append(_f)
    for _sev in sorted(_findings_by_sev):
        for _f in _findings_by_sev[_sev]:
            _loc = f" [{_f.file}:{_f.line}]" if _f.file else ""
            _log(f"  [{_sev}]{_loc} {_f.description}")
    if logger:
        logger._safe_emit(
            "review_result",
            verdict=parsed_review.verdict,
            p1_count=_p1_count,
            p2_count=_p2_count,
            cost_usd=round(_review_cost, 6),
        )

    # ── APPROVE (or disposition-gated pass) ─────────────────────────
    # The coordinator makes the blocking decision independently of the synthesized verdict.
    # If the synthesized verdict is REQUEST_CHANGES but all P1s are net_new (single-reviewer,
    # not in changed files, not previously raised), we treat the cycle as passing.
    # Net-new P1s are recorded in the audit trail but do not block.
    _effective_approve = parsed_review.verdict == "APPROVE" or (
        parsed_review.verdict == "REQUEST_CHANGES" and not _blocking_p1
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
            f"  ${_review_cost:.2f}  {_fmt_duration(_review_elapsed)}"
        )
        if interactive:
            state.phase = Phase.HUMAN_REVIEW
            _log_phase(state.phase)
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
                decision, feedback = _human_review(
                    state, parsed_review, workspace_path, branch_name
                )
            state.human_review_decision = decision
            state.human_review_feedback = feedback
            if decision == "approve":
                _append_cycle_history(state, parsed_review)
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
                        review_cost=_review_cost,
                        review_elapsed=_review_elapsed,
                        message=(
                            f"Task '{task.name}' completed. "
                            f"Human approved after {state.review_cycle} cycle(s), "
                            f"{state.dev_iteration} dev iteration(s). "
                        ),
                        run_id=run_id,
                    ),
                    config,
                )
            if decision in ("escalate", "timeout"):
                state.phase = Phase.ESCALATE
                state.error = (
                    "Remote review timed out — auto-escalated."
                    if decision == "timeout"
                    else "Human chose to escalate after APPROVE."
                )
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
                _append_cycle_history(state, parsed_review)
                state.dev_iteration = 0
                state.review_cycle = 0
                state.human_review_extra_cycles += 1
                state.last_review_findings = (
                    review_to_dev_handoff(parsed_review) if parsed_review.findings else None
                )
                state.human_feedback = None
                state.retry_reason = "extend"
                _log(
                    f"Human extended — granting fresh budget "
                    f"(extra_cycles={state.human_review_extra_cycles})"
                )
                return _ReviewOutcome.RETRY_DEV, None, config
            # decision == "reject"
            _append_cycle_history(state, parsed_review)
            state.human_feedback = feedback
            state.last_review_findings = None
            state.retry_reason = "reject"
            state.dev_iteration = 0
            _log("Human rejected — looping back to dev with feedback")
            return _ReviewOutcome.RETRY_DEV, None, config
        else:
            _append_cycle_history(state, parsed_review)
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
                        f"{state.dev_iteration} dev iteration(s). "
                    ),
                    run_id=run_id,
                ),
                config,
            )

    # ── REQUEST_CHANGES (blocking P1s present) ───────────────────
    _is_persistent_p1 = False
    if config.smart_config_models is not None and len(state.review_results) >= 2:
        _prev_result = state.review_results[-2]
        _is_persistent_p1 = _has_persistent_p1(parsed_review.findings, _prev_result.findings)

    _persistent_tag = " (persistent)" if _is_persistent_p1 else ""
    _log(
        f"  ✗ REVIEW   REQUEST_CHANGES  {_p1_count} P1  {_p2_count} P2{_persistent_tag}"
        f"  ${_review_cost:.2f}  {_fmt_duration(_review_elapsed)}"
    )

    # Escalate dev model on persistent P1
    if (
        _is_persistent_p1
        and not state.dev_escalated
        and (state.total_dev_cost < config.dev_profile.budget_usd)
    ):
        _curr_key = _find_registry_key_for_profile(config.dev_profile)
        if _curr_key is not None:
            _next_key = _escalate_dev_model(_curr_key, config.smart_config_models)
            if _next_key is not None:
                _next_info = MODEL_REGISTRY[_next_key]
                _p1_file = next(
                    (f.file for f in parsed_review.findings if f.severity == "P1"),
                    "unknown",
                )
                _log(
                    f"  Dev escalation: {config.dev_profile.model} → {_next_info.model}"
                    f" (persistent P1 in {_p1_file})"
                )
                _old_model = config.dev_profile.model
                _new_dev = _dc_replace(
                    config.dev_profile, cli=_next_info.cli, model=_next_info.model
                )
                config = _dc_replace(config, dev_profile=_new_dev)
                state.dev_escalated = True
                _prev_result = state.review_results[-2]
                _persistent_descs = _persistent_p1_descriptions(
                    parsed_review.findings, _prev_result.findings
                )
                state.escalation_note = (
                    f"MODEL ESCALATION: A P1 finding persisted across review cycles. "
                    f"The previous model ({_old_model}) was unable to resolve it. "
                    f"You are now running on an upgraded model ({_next_info.model}). "
                    f"Persistent finding(s): {'; '.join(_persistent_descs)}"
                )

    if state.review_cycle >= config.retry.max_review_cycles:
        if interactive:
            state.phase = Phase.HUMAN_REVIEW
            _log_phase(state.phase, "cycles exhausted")
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
                decision, feedback = _human_review(
                    state, parsed_review, workspace_path, branch_name
                )
            state.human_review_decision = decision
            state.human_review_feedback = feedback
            if decision == "approve":
                _append_cycle_history(state, parsed_review)
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
                        review_cost=_review_cost,
                        review_elapsed=_review_elapsed,
                        message=(
                            f"Task '{task.name}' completed. "
                            f"Human approved after {state.review_cycle} cycle(s). "
                        ),
                        run_id=run_id,
                    ),
                    config,
                )
            if decision in ("escalate", "timeout"):
                state.phase = Phase.ESCALATE
                state.error = (
                    "Remote review timed out — auto-escalated."
                    if decision == "timeout"
                    else "Human chose to escalate after exhausted cycles."
                )
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
                _append_cycle_history(state, parsed_review)
                state.dev_iteration = 0
                state.review_cycle = 0
                state.human_review_extra_cycles += 1
                state.last_review_findings = review_to_dev_handoff(parsed_review)
                state.human_feedback = None
                state.retry_reason = "extend"
                _log(
                    f"Human extended — granting fresh budget "
                    f"(extra_cycles={state.human_review_extra_cycles})"
                )
                return _ReviewOutcome.RETRY_DEV, None, config
            # decision == "reject" — cycles exhausted: treat as extend + reject
            _append_cycle_history(state, parsed_review)
            state.dev_iteration = 0
            state.review_cycle = 0
            state.human_review_extra_cycles += 1
            state.human_feedback = feedback
            state.last_review_findings = None
            state.retry_reason = "reject"
            _log(
                "Human rejected (cycles exhausted) — granting fresh budget "
                f"(extra_cycles={state.human_review_extra_cycles})"
            )
            return _ReviewOutcome.RETRY_DEV, None, config
        else:
            state.phase = Phase.ESCALATE
            state.error = (
                f"Review requested changes after {state.review_cycle} cycles. "
                f"Max cycles ({config.retry.max_review_cycles}) exhausted."
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
            cost_usd=round(_review_cost, 6),
            duration_s=round(_review_elapsed, 2),
        )
    _append_cycle_history(state, parsed_review)
    state.last_review_findings = review_to_dev_handoff(parsed_review)
    state.dev_iteration = 0
    state.human_feedback = None
    state.retry_reason = "review_changes"
    _log_verbose(f"Sending {len(parsed_review.findings)} findings back to dev agent")
    return _ReviewOutcome.RETRY_DEV, None, config


class _ValidateOutcome(Enum):
    PASS = auto()
    RETRY_DEV = auto()
    ESCALATE = auto()


def _run_validate_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
    dev_calls_this_cycle: int,
    *,
    notify: bool,
    logger: StructuredLogger | None,
) -> tuple[_ValidateOutcome, CoordinatorResult | None]:
    """Run one VALIDATE iteration. Returns (outcome, result).

    ESCALATE returns (ESCALATE, CoordinatorResult). RETRY_DEV and PASS return (outcome, None).
    Does NOT emit 'phase_end VALIDATE pass' — caller emits that (it fires on skip too).
    """
    state.phase = Phase.VALIDATE
    if logger:
        logger._safe_emit("phase_start", phase="VALIDATE", iteration=state.dev_iteration)

    _gate_start = time.monotonic()
    gate_override = task.gate_override
    gate_output_tail: str = ""
    if _is_gate_skip(gate_override):
        _log_phase(state.phase, "skipped (gate: none)")
        _log("  Gate: none (story override)")
        gate_decision: str | None = "PASS"
        gate_err: str | None = None
    else:
        if gate_override is not None:
            _log_phase(state.phase, "running gate... (override)")
            _log(f"  Gate: {gate_override} (story override)")
        else:
            _log_phase(state.phase, "running gate...")
        gate_decision, gate_err, gate_output_tail = _run_gate_full(
            config, workspace_path, task=task, iter_num=state.dev_iteration
        )
    _gate_elapsed = time.monotonic() - _gate_start
    state.validate_durations.append(_gate_elapsed)
    if logger:
        logger._safe_emit(
            "gate_result",
            decision=gate_decision or gate_err,
            duration_s=round(_gate_elapsed, 2),
            output_tail=gate_output_tail[-500:] if gate_output_tail else "",
        )

    if gate_err:
        use_exit_code = not config.validation.handoff_file
        if use_exit_code:
            _log(f"✗ ESCALATE   {gate_err}")
            state.phase = Phase.ESCALATE
            state.error = gate_err
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
            _escalate_notify(task, state, notify, config)
            return _ValidateOutcome.ESCALATE, CoordinatorResult(
                success=False, phase=state.phase, state=state, message=state.error
            )
        _log_verbose(f"Gate error: {gate_err}")
        if dev_calls_this_cycle >= config.retry.max_dev_iterations:
            state.phase = Phase.ESCALATE
            state.error = f"Gate failed after {state.dev_iteration} attempts: {gate_err}"
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
            _escalate_notify(task, state, notify, config)
            return _ValidateOutcome.ESCALATE, CoordinatorResult(
                success=False, phase=state.phase, state=state, message=state.error
            )
        state.human_feedback = f"Gate validation failed: {gate_err}"
        state.retry_reason = "gate_fail"
        _log(f"  ✗ VALIDATE   FAIL  (iter={state.dev_iteration} → retrying)")
        if logger:
            logger._safe_emit("phase_end", phase="VALIDATE", outcome="fail")
        return _ValidateOutcome.RETRY_DEV, None

    assert gate_decision is not None
    state.gate_decisions.append(gate_decision)
    _log_verbose(f"Gate decision: {gate_decision}")

    if gate_decision == "PASS":
        _log("  ✓ VALIDATE   PASS")
        pre_validate_cmd = config.validation.pre_validate_command
        if pre_validate_cmd:
            _log(f"  Running pre-validate command: {pre_validate_cmd}")
            pv_ok, pv_out = _cu._run_shell(pre_validate_cmd, workspace_path)
            if not pv_ok:
                _log(f"  ⚠ Pre-validate command failed (non-fatal): {pv_out[:200]}")
            else:
                _log_verbose(f"Pre-validate output: {pv_out[:200]}")
        dirty_ok, dirty_out = _cu._run_shell("git status --porcelain", workspace_path)
        if dirty_ok and dirty_out.strip():
            handoff_file = config.validation.handoff_file
            if handoff_file:
                dirty_lines = [
                    line
                    for line in dirty_out.splitlines()
                    if line.strip() and not line.endswith(handoff_file)
                ]
            else:
                dirty_lines = [line for line in dirty_out.splitlines() if line.strip()]
            if dirty_lines:
                raw_names = ", ".join(line.strip().split(maxsplit=1)[-1] for line in dirty_lines)
                _log(f"Dirty worktree detected: {raw_names}")

                # Auto-commit: synthesize message from handoff, don't
                # re-invoke the agent (full-prompt retry burns tokens and
                # times out — the agent already wrote the code).
                dev_notes = _get_raw_dev_notes(config, workspace_path)
                if dev_notes:
                    first_line = dev_notes.strip().splitlines()[0][:72]
                    commit_msg = first_line
                else:
                    commit_msg = (
                        f"wip: uncommitted changes from dev iteration {state.dev_iteration}"
                    )
                _cu._run_shell("git add -A", workspace_path)
                # Use subprocess.run directly to avoid shell injection
                # from model-authored dev_notes (quotes, backticks, $()).
                try:
                    subprocess.run(
                        ["git", "commit", "-m", commit_msg],
                        cwd=workspace_path,
                        capture_output=True,
                        timeout=30,
                        check=True,
                    )
                    _log(f"  Auto-committed dirty worktree: {commit_msg}")
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    err_detail = getattr(exc, "stderr", b"").decode(errors="replace")[:200]
                    _log(f"  ⚠ Auto-commit failed: {err_detail}")
                    # Commit failed — worktree is still dirty.
                    # Escalate: don't let uncommitted changes leak into
                    # REVIEW/DONE/PR.
                    state.phase = Phase.ESCALATE
                    state.error = f"Auto-commit failed for dirty worktree: {raw_names}"
                    _log(f"✗ ESCALATE   {state.error}")
                    _escalate_notify(task, state, notify, config)
                    return _ValidateOutcome.ESCALATE, CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    )

    elif gate_decision in ("FAIL", "BLOCKED"):
        if dev_calls_this_cycle >= config.retry.max_dev_iterations:
            state.phase = Phase.ESCALATE
            state.error = f"Gate returned {gate_decision} after {state.dev_iteration} attempts"
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
            _escalate_notify(task, state, notify, config)
            return _ValidateOutcome.ESCALATE, CoordinatorResult(
                success=False, phase=state.phase, state=state, message=state.error
            )
        handoff_text = _get_handoff_content(config, workspace_path)
        state.human_feedback = (
            f"Gate output (last {config.validation.gate_output_tail_chars} chars):\n"
            f"{gate_output_tail}\n\n"
            f"Gate returned {gate_decision}. "
            f"Fix the issues and re-run the gate.\n\n"
            f"Current handoff:\n{handoff_text}"
        )
        state.retry_reason = "gate_fail"
        _log(f"  ✗ VALIDATE   {gate_decision}  (iter={state.dev_iteration} → retrying)")
        _log(f"Retrying dev (gate={gate_decision}, iter={state.dev_iteration})")
        if logger:
            logger._safe_emit("phase_end", phase="VALIDATE", outcome="fail")
        return _ValidateOutcome.RETRY_DEV, None
    else:
        _log(f"Unknown gate decision: {gate_decision!r}, treating as FAIL")
        state.phase = Phase.ESCALATE
        state.error = f"Unknown gate decision: {gate_decision!r}"
        _log(f"✗ ESCALATE   {state.error}")
        _escalate_notify(task, state, notify, config)
        return _ValidateOutcome.ESCALATE, CoordinatorResult(
            success=False, phase=state.phase, state=state, message=state.error
        )

    return _ValidateOutcome.PASS, None


def _run_dev_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    story_content: str,
    workspace_path: Path,
    branch_name: str,
    *,
    notify: bool,
    logger: StructuredLogger | None,
) -> CoordinatorResult | None:
    """Run one DEV iteration. Returns CoordinatorResult on budget escalation, else None.

    Caller must increment state.dev_iteration and _dev_calls_this_cycle before calling.
    Mutates state in-place (appends dev_results, updates dev_session_id, etc.).
    """
    _ensure_runners()
    _log_phase(
        state.phase,
        f"{config.dev_profile.model}  iter={state.dev_iteration}",
    )
    if logger:
        logger._safe_emit("phase_start", phase="DEV", iteration=state.dev_iteration)

    # Capture HEAD before the dev agent runs — used by finding_classifier for git diff.
    # Best-effort: any failure is silently ignored (non-critical for correctness).
    try:
        _head_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=10,
        )
        if _head_proc.returncode == 0:
            state.last_dev_start_commit = _head_proc.stdout.decode().strip()
    except Exception:  # noqa: BLE001  # best-effort, any error is harmless
        pass

    _gate_cmd = (
        task.gate_override
        if task.gate_override is not None and not _is_gate_skip(task.gate_override)
        else config.validation.gate_command
    )
    _dev_entry_reason = state.retry_reason  # snapshot before consumed by prompt routing
    if state.retry_reason == "timeout_resume":
        prompt = (
            state.human_feedback
            or "You were cut off by a timeout. Continue from where you left off."
        )
        state.retry_reason = None
        state.human_feedback = None
    elif state.retry_reason in ("review_changes", "extend") and state.last_review_findings:
        prompt = build_fix_prompt(
            task,
            workspace_path=workspace_path,
            branch_name=branch_name,
            review_findings=state.last_review_findings,
            gate_command=_gate_cmd,
            gate_skipped=_is_gate_skip(task.gate_override),
            iteration=state.dev_iteration,
            cycle_history=state.cycle_history or None,
            escalation_note=state.escalation_note,
            handoff_file=config.validation.handoff_file,
            plan_output=state.plan_structured
            if state.plan_structured is not None
            else state.plan_output,
            classified_p1s=[r for r in state.finding_registry if r.severity == "P1"] or None,
        )
        state.escalation_note = None  # consumed
    else:
        prompt = build_dev_prompt(
            task,
            workspace_path=workspace_path,
            branch_name=branch_name,
            story_content=story_content,
            gate_command=_gate_cmd,
            gate_skipped=_is_gate_skip(task.gate_override),
            review_findings=state.last_review_findings,
            human_feedback=state.human_feedback,
            preflight_output=(state.preflight_result.output if state.preflight_result else None),
            plan_output=state.plan_structured
            if state.plan_structured is not None
            else state.plan_output,
            plan_review_advisory=state.plan_agent_review_findings,
            iteration=state.dev_iteration,
            escalation_note=state.escalation_note,
            cycle_history=state.cycle_history or None,
            handoff_file=config.validation.handoff_file,
        )
        state.escalation_note = None  # consumed
    state.retry_reason = None  # consumed

    write_trace(
        workspace_path / ".forge/traces" / f"{state.dev_trace_count}-dev-prompt.txt",
        prompt,
    )

    _dev_timeout = resolve_timeout(
        config.dev_profile.timeout_seconds,
        config.dev_profile.timeout_medium_seconds,
        config.dev_profile.timeout_large_seconds,
        state.preflight_complexity,
    )
    _dev_override_active = (
        state.preflight_complexity == "large"
        and config.dev_profile.timeout_large_seconds is not None
    ) or (
        state.preflight_complexity == "medium"
        and config.dev_profile.timeout_medium_seconds is not None
    )
    if _dev_override_active:
        _log(f"  Dev timeout: {_dev_timeout}s ({state.preflight_complexity} complexity)")
    else:
        _log(f"  Dev timeout: {_dev_timeout}s")
    _dev_profile = _dc_replace(config.dev_profile, timeout_seconds=_dev_timeout)

    _dev_start = time.monotonic()
    dev_result = run_agent(
        prompt=prompt,
        profile=_dev_profile,
        working_dir=workspace_path,
        session_id=state.dev_session_id,
        secrets=config.secrets,
    )
    _dev_elapsed = time.monotonic() - _dev_start
    write_trace(
        workspace_path / ".forge/traces" / f"{state.dev_trace_count}-dev-output.txt",
        dev_result.output,
    )
    # Write dev iteration log to durable story log dir
    if state.log_dir is not None:
        write_trace(
            state.log_dir / f"dev-iter-{state.dev_iteration}-{config.dev_profile.name}.log",
            dev_result.output or "",
        )
    state.dev_results.append(dev_result)
    state.dev_durations.append(_dev_elapsed)
    # Capture handoff snapshot for audit trail
    _handoff_snap: dict | None = None
    if config.validation.handoff_file:
        try:
            _handoff_path = workspace_path / config.validation.handoff_file
            _handoff_snap = yaml.safe_load(_handoff_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    state.dev_handoff_snapshots.append(_handoff_snap)
    state.dev_session_id = dev_result.session_id or state.dev_session_id
    save_sessions(workspace_path, state.dev_session_id, state.reviewer_session_ids)
    from theforge.runners import log_agent_result  # noqa: PLC0415

    log_agent_result(dev_result, "DEV")
    _dev_cost_str = (
        "${:.2f}".format(dev_result.cost_usd) if dev_result.cost_usd is not None else "unknown"
    )
    _log(f"  ✓ DEV   {_dev_cost_str}  {_fmt_duration(_dev_elapsed)}")
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="DEV",
            outcome="success" if dev_result.success else "failure",
            cost_usd=dev_result.cost_usd,
            duration_s=round(_dev_elapsed, 2),
        )

    if state.total_dev_cost > config.dev_profile.budget_usd:
        state.phase = Phase.ESCALATE
        state.error = (
            f"Dev budget exceeded: spent ${state.total_dev_cost:.4f} "
            f"(limit ${config.dev_profile.budget_usd:.4f})"
        )
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("escalate", reason=state.error, phase="DEV")
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    if not dev_result.success:
        _log_verbose(f"Dev agent failed (exit={dev_result.exit_code})")
        # Don't immediately escalate — try validation anyway,
        # the agent may have committed partial work + run the gate

    # ── Zero-change guard (review-driven retry only) ─────────────────
    # If the coordinator retried DEV after review REQUEST_CHANGES and the dev
    # agent produced no changes (no commits, no dirty files), escalate immediately.
    # Without this guard the coordinator sends the identical code back to review,
    # the finding classifier sees no diff, classifies new P1s as net_new
    # (non-blocking), and the story passes with unfixed code.
    # Only applies when THIS dev pass was entered for review_changes or extend —
    # gate retries and timeout resumes may legitimately produce no code changes.
    _is_review_driven = _dev_entry_reason in ("review_changes", "extend")
    if _is_review_driven and state.last_dev_start_commit:
        _has_commits = False
        _has_dirty = False
        try:
            _diff_proc = subprocess.run(
                ["git", "diff", "--quiet", state.last_dev_start_commit, "HEAD"],
                cwd=str(workspace_path),
                capture_output=True,
                timeout=10,
            )
            _has_commits = _diff_proc.returncode != 0  # exit 1 = diff exists
        except Exception:  # noqa: BLE001
            pass
        try:
            _status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(workspace_path),
                capture_output=True,
                timeout=10,
            )
            _has_dirty = bool(_status_proc.stdout.strip())
        except Exception:  # noqa: BLE001
            pass
        if not _has_commits and not _has_dirty:
            state.phase = Phase.ESCALATE
            state.error = (
                "Dev retry produced no changes — escalating to avoid re-reviewing identical code"
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("escalate", reason=state.error, phase="DEV")
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )

    return None


# ── Review-only phase ─────────────────────────────────────────────────


def _run_review_only_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    story_content: str,
    workspace_path: Path,
    branch_name: str,
    *,
    notify: bool,
    logger: StructuredLogger | None,
    task_start: float,
) -> CoordinatorResult:
    """Run the REVIEW phase for the review-only entry point.

    No DEV retry: REQUEST_CHANGES → ESCALATE immediately.
    """
    state.phase = Phase.REVIEW
    if logger:
        logger._safe_emit("phase_start", phase="REVIEW", iteration=1)
    state.review_cycle = 1
    state.dev_iteration = 0
    _pool_model_names_ro = "+".join(p.model for p in config.review_pool)
    _log_phase(state.phase, f"{_pool_model_names_ro}  cycle=1  (review-only)")

    commit_log = _get_commit_log(workspace_path, config.workspace.base_branch)
    handoff_content = _get_handoff_content(config, workspace_path)
    dev_notes = _get_dev_notes(config, workspace_path)
    review_prompt = build_review_prompt(
        task,
        story_content=story_content,
        commit_log=commit_log,
        workspace_path=str(workspace_path),
        branch=branch_name,
        handoff_content=handoff_content,
        mode=config.review_pool[0].mode,
        dev_notes=dev_notes,
        cycle_history=None,
    )

    meta = ReviewCycleMetadata(
        pool_models=[p.name for p in config.review_pool],
        successful=[],
        failed=[],
        synthesized=False,
    )
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

    _log(f"  Summary: {parsed_review.summary}")
    _ro_findings_by_sev: dict[str, list] = {}
    for _f in parsed_review.findings:
        _ro_findings_by_sev.setdefault(_f.severity, []).append(_f)
    for _sev in sorted(_ro_findings_by_sev):
        for _f in _ro_findings_by_sev[_sev]:
            _loc = f" [{_f.file}:{_f.line}]" if _f.file else ""
            _log(f"  [{_sev}]{_loc} {_f.description}")
    _ro_cost = sum(r.cost_usd or 0.0 for r in state.review_agent_results)
    _ro_elapsed = _pool_elapsed

    if logger:
        logger._safe_emit(
            "review_result",
            verdict=parsed_review.verdict,
            p1_count=_ro_p1,
            p2_count=_ro_p2,
            cost_usd=round(_ro_cost, 6),
        )

    if parsed_review.verdict == "APPROVE":
        state.phase = Phase.DONE
        _dur = _fmt_duration(_ro_elapsed)
        _log(f"  ✓ REVIEW   APPROVE  {_ro_p1} P1  {_ro_p2} P2  ${_ro_cost:.2f}  {_dur}")
        _log(f"✓ DONE   total=${state.total_cost:.2f}  {_fmt_duration(_ro_elapsed)}")
        if logger:
            logger._safe_emit(
                "phase_end",
                phase="REVIEW",
                outcome="approve",
                cost_usd=round(_ro_cost, 6),
                duration_s=round(_ro_elapsed, 2),
            )
            logger._safe_emit(
                "run_end",
                outcome="done",
                total_cost_usd=round(state.total_cost, 6),
                total_duration_s=round(time.monotonic() - task_start, 2),
            )
        _ntfy_done_notify(
            task, state, config, notify, parsed_review.summary, _ro_elapsed, branch_name
        )
        return CoordinatorResult(
            success=True,
            phase=state.phase,
            state=state,
            message=(f"Task '{task.name}' review-only: APPROVE. Branch: {branch_name}"),
        )

    # REQUEST_CHANGES — no DEV retry in review-only mode
    state.phase = Phase.ESCALATE
    p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
    state.error = (
        f"Review requested changes ({p1_count} P1 finding(s)). No retry in review-only mode."
    )
    _log(
        f"  ✗ REVIEW   REQUEST_CHANGES  {_ro_p1} P1  {_ro_p2} P2"
        f"  ${_ro_cost:.2f}  {_fmt_duration(_ro_elapsed)}"
    )
    _log(f"✗ ESCALATE   {state.error}")
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="REVIEW",
            outcome="escalate",
            cost_usd=round(_ro_cost, 6),
            duration_s=round(_ro_elapsed, 2),
        )
        logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
        logger._safe_emit(
            "run_end",
            outcome="escalate",
            total_cost_usd=round(state.total_cost, 6),
            total_duration_s=round(time.monotonic() - task_start, 2),
        )
    _escalate_notify(task, state, notify, config)
    return CoordinatorResult(
        success=False,
        phase=state.phase,
        state=state,
        message=state.error,
    )
