"""REVIEW phase handler: pool execution, verdict routing, escalation, review-only mode."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import replace as _dc_replace
from enum import Enum, auto
from pathlib import Path

from theforge.config import MODEL_REGISTRY, ForgeConfig
from theforge.coordinator.context_scope import plan_file_list
from theforge.review import (
    ReviewFinding,
    ReviewResult,
    _best_individual_result,
    review_to_dev_handoff,
)
from theforge.task import ContextAssembler, TaskStory, build_review_prompt

from .completion import _append_cycle_history, _finalize_approve
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
    _get_commit_diffs,
    _get_commit_log,
    _get_dev_notes,
    _get_diff_content,
    _get_diff_stat,
    _get_handoff_commit_warning,
    _get_handoff_content,
    _latest_forge_handoff_path,
)
from .review_pool import _run_review_pool
from .run_setup import save_trajectory_state
from .state import (
    CoordinatorResult,
    CoordinatorState,
    FindingRecord,
    Phase,
    RetryReason,
    ReviewCycleMetadata,
    ReviewIterationTelemetry,
)
from .util import _fmt_duration, _log, _log_phase, _log_verbose, _run_worktree_eval


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
    task: TaskStory,
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


def _record_review_iteration_telemetry(
    state: CoordinatorState,
    parsed_review: ReviewResult,
    *,
    review_cost: float,
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
            iteration=state.review_cycle,
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


# ── Review-phase helpers ──────────────────────────────────────────────


def _apply_review_parse_fallback(
    candidate: ReviewResult,
    individual_results: list[ReviewResult],
) -> ReviewResult:
    """Fall back from a merged candidate with parse errors to best individual or synthetic P1."""
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
    _log(
        "  ⚠ all reviewers failed to produce usable output — "
        "injecting synthetic P1, returning REQUEST_CHANGES"
    )
    return ReviewResult(
        verdict="REQUEST_CHANGES",
        summary="Review pool failed to produce a usable verdict",
        findings=[
            ReviewFinding(
                severity="P1",
                file="",
                line=None,
                description=(
                    "All reviewers failed to produce parseable output. Manual review required."
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


def _log_review_findings(
    parsed_review: ReviewResult,
    p1_count: int,
    p2_count: int,
    review_cost: float,
    logger: StructuredLogger | None,
) -> None:
    """Log review summary and findings grouped by severity; emit structured review_result event."""
    _log(f"  Summary: {parsed_review.summary}")
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
            p1_count=p1_count,
            p2_count=p2_count,
            cost_usd=round(review_cost, 6),
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
    review_cost: float,
    review_elapsed: float,
    run_id: str,
    exhausted_cycles: bool,
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
        _append_cycle_history(state, parsed_review)
        if exhausted_cycles:
            _approve_msg = (
                f"Task '{task.name}' completed. "
                f"Human approved after {state.review_cycle} cycle(s). "
            )
        else:
            _approve_msg = (
                f"Task '{task.name}' completed. "
                f"Human approved after {state.review_cycle} cycle(s), "
                f"{state.dev_iteration} dev iteration(s). "
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
) -> tuple[_ReviewOutcome, CoordinatorResult | None, ForgeConfig]:
    """Run the full REVIEW phase: pool+synthesis, parse retries, verdict handling.

    Returns (outcome, result_or_none, possibly_updated_config).
    DONE/ESCALATE → result is a CoordinatorResult.
    RETRY_DEV → result is None, caller loops back to DEV.
    config is returned because persistent-P1 model escalation may replace it.
    """
    state.phase = Phase.REVIEW
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "REVIEW",
                "iteration": state.review_cycle + 1,
                "cost_usd": state.total_cost,
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
        logger=logger,
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

    # ── Graceful empty-merge fallback ─────────────────────────────────
    parsed_review = _apply_review_parse_fallback(_candidate, _individual_results)

    # Valid verdict — increment review cycle counter
    state.review_cycle += 1
    state.review_results.append(parsed_review)

    _review_elapsed = time.monotonic() - _review_pool_start
    _p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
    _p2_count = sum(1 for f in parsed_review.findings if f.severity == "P2")
    _review_cost = (
        sum(r.cost_usd or 0.0 for r in state.review_agent_results) - _review_cost_before_cycle
    )

    # ── Finding classification via worktree subprocess ────────────────────
    # Run update_finding_registry in the worktree's Python environment so that
    # self-hosting sprints evaluate the worktree's classifier, not the
    # coordinator's own copy. sys.path is never mutated; isolation is via
    # PYTHONPATH in the subprocess.
    _fr_payload: dict = {
        "finding_registry": [
            {
                "finding_id": r.finding_id,
                "cycle_first_seen": r.cycle_first_seen,
                "cycle_last_seen": r.cycle_last_seen,
                "file": r.file,
                "line": r.line,
                "severity": r.severity,
                "description": r.description,
                "reporter": r.reporter,
                "disposition": r.disposition,
            }
            for r in state.finding_registry
        ],
        "cycle_results": [
            (
                reviewer_name,
                {
                    "verdict": rr.verdict,
                    "summary": rr.summary,
                    "findings": [
                        {
                            "severity": f.severity,
                            "file": f.file,
                            "line": f.line,
                            "description": f.description,
                            "suggestion": f.suggestion,
                        }
                        for f in rr.findings
                    ],
                    "story_matches": rr.story_matches,
                    "story_mismatches": rr.story_mismatches,
                    "test_adequate": rr.test_adequate,
                    "test_gaps": rr.test_gaps,
                    "parse_errors": rr.parse_errors,
                    "raw_yaml": rr.raw_yaml,
                },
            )
            for reviewer_name, rr in state.last_cycle_reviewer_results
        ],
        "workspace_path": str(workspace_path),
        "cycle_num": state.review_cycle,
        "prev_commit": state.last_dev_start_commit
        if isinstance(state.last_dev_start_commit, str)
        else None,
    }
    _fr_result = _run_worktree_eval(workspace_path, "update_finding_registry", _fr_payload)
    # Reconstruct state.finding_registry as FindingRecord objects with shared
    # references so that AC-blocking disposition mutations below propagate
    # correctly to the audit trail.
    state.finding_registry = [FindingRecord(**r) for r in _fr_result["finding_registry"]]
    _classified = [state.finding_registry[i] for i in _fr_result["classified_indices"]]

    _allow_net_new_bypass = config.finding_classifier.allow_net_new_bypass
    _log(f"  [finding_classifier] allow_net_new_bypass={_allow_net_new_bypass}")

    # ── Gate-contradiction downgrade ──────────────────────────────────────────
    # If the most recent gate decision was PASS, any P1 finding whose description
    # matches a gate-verifiable pattern (test failures, build errors, lint failures)
    # is mechanically contradicted. Such findings are downgraded in-place to
    # disposition gate_contradicted so they do not block approval.
    # Pattern matching is keyword-based (conservative). False negatives are
    # acceptable — the P1 remains blocking if no pattern matches.
    # No downgrade occurs when the gate decision is FAIL, BLOCKED, or absent.
    _GATE_VERIFIABLE_PATTERNS = (
        "test fail",
        "tests fail",
        "test failure",
        "failing test",
        "build fail",
        "build error",
        "lint fail",
        "lint error",
        "compilation fail",
        "compile fail",
        "import error",
        "syntax error",
        " failures",
        "0 passed",
        "test suite",
        "broken build",
        "does not compile",
    )
    _last_gate = state.gate_decisions[-1] if state.gate_decisions else None
    if _last_gate == "PASS":
        for _rec in _classified:
            if _rec.severity != "P1":
                continue
            _desc_lower = _rec.description.lower()
            _matched = next((p for p in _GATE_VERIFIABLE_PATTERNS if p in _desc_lower), None)
            if _matched is not None:
                _log(
                    f"  ↷ gate_contradicted: P1 downgraded"
                    f" (gate={_last_gate}, pattern={_matched!r}):"
                    f" {_rec.description[:80]}"
                )
                _rec.disposition = "gate_contradicted"  # type: ignore[assignment]

    if state.review_cycle >= 2:
        # has_blocking_p1 / net_new_p1s inlined to avoid importing theforge.finding_classifier.
        # Logic is identical to the functions in finding_classifier.py.
        # gate_contradicted is intentionally excluded: these findings are mechanically
        # disproven by a PASS gate and must not block approval.
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
        # AC-violation override: a net-new P1 from a reviewer who also flagged
        # matches_spec=false is not speculative — it asserts the story was not completed
        # correctly and must block regardless of its disposition classification.
        # Conservative rule: if *any* reviewer in the pool returned matches_spec=false,
        # all net-new P1s from that reviewer are treated as AC-blocking.
        _ac_failing_reporters = {
            name for name, rr in state.last_cycle_reviewer_results if not rr.story_matches
        }
        if _ac_failing_reporters:
            _ac_blocking_p1s = [r for r in _nonblocking_p1s if r.reporter in _ac_failing_reporters]
            _nonblocking_p1s = [
                r for r in _nonblocking_p1s if r.reporter not in _ac_failing_reporters
            ]
            if _ac_blocking_p1s:
                _blocking_p1 = True
                # Persist the AC-blocking classification so the audit trail records
                # these findings as ac_blocking rather than net_new.
                for _rec in _ac_blocking_p1s:
                    _rec.disposition = "ac_blocking"  # type: ignore[assignment]
                _ac_descs = "; ".join(r.description[:80] for r in _ac_blocking_p1s)
                _log(
                    f"  ✗ {len(_ac_blocking_p1s)} net-new P1(s) blocked"
                    f" (AC-blocking: reviewer indicated matches_spec=false): {_ac_descs}"
                )
        # When allow_net_new_bypass is disabled, net-new P1s are treated as blocking.
        # Persist the disposition change so the audit trail records these as blocking,
        # not as net_new (which audit.py would serialize under non_blocking_p1s).
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
        # gate_contradicted P1s are accounted for and must not trigger this fallback.
        if (
            not _blocking_p1
            and not _nonblocking_p1s
            and not _gate_contradicted_p1s
            and _p1_count > 0
        ):
            _blocking_p1 = True
    else:
        # Cycle 1: any P1 is blocking (no prior baseline to classify against).
        # Gate-contradicted P1s are non-blocking even on cycle 1.
        if _classified:
            _blocking_p1 = any(
                r.severity == "P1" and r.disposition != "gate_contradicted" for r in _classified
            )
        else:
            _blocking_p1 = _p1_count > 0
        _nonblocking_p1s = []

    # ── Trajectory classification via worktree subprocess ─────────────────
    # Runs for EVERY successfully merged parsed_review (APPROVE, exhausted, retry).
    # Uses a dedicated monotonic counter (trajectory_cycle) that is never reset
    # or decremented by extend/reject/exhausted-gate paths — unlike review_cycle.
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
        _cf_payload: dict = {
            "current_findings": [
                {
                    "severity": f.severity,
                    "file": f.file,
                    "line": f.line,
                    "description": f.description,
                    "suggestion": f.suggestion,
                }
                for f in parsed_review.findings
            ],
            "current_cycle": state.trajectory_cycle,
            "trajectory_store": state.finding_trajectory,
            # Pass stored dicts directly; subprocess eval handles missing suggestion
            "prior_cycle_findings": [
                (cycle_num, findings) for cycle_num, findings in state.review_cycle_findings[:-1]
            ],
        }
        _cf_result = _run_worktree_eval(workspace_path, "classify_families", _cf_payload)
        state.finding_trajectory = _cf_result["trajectory_store"]
        state.surviving_families = _cf_result["surviving_families"]
    else:
        state.surviving_families = []

    save_trajectory_state(workspace_path, state)
    _record_review_iteration_telemetry(
        state,
        parsed_review,
        review_cost=_review_cost,
        review_elapsed=_review_elapsed,
        max_iterations=config.retry.max_review_cycles,
    )

    _log_review_findings(parsed_review, _p1_count, _p2_count, _review_cost, logger)

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
            )
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
    if config.models is not None and len(state.review_results) >= 2:
        _prev_result = state.review_results[-2]
        _is_persistent_p1 = _has_persistent_p1(parsed_review.findings, _prev_result.findings)

    _persistent_tag = " (persistent)" if _is_persistent_p1 else ""
    _log(
        f"  ✗ REVIEW   REQUEST_CHANGES  {_p1_count} P1  {_p2_count} P2{_persistent_tag}"
        f"  ${_review_cost:.2f}  {_fmt_duration(_review_elapsed)}"
    )

    # Escalate dev model on persistent P1 (only when explicitly enabled via forge.yaml)
    if (
        config.retry.auto_model_escalation
        and _is_persistent_p1
        and not state.dev_escalated
        and (state.total_dev_cost < config.dev_profile.budget_usd)
    ):
        _curr_key = _find_registry_key_for_profile(config.dev_profile)
        if _curr_key is not None:
            _next_key = _escalate_dev_model(_curr_key, config.models)
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
) -> CoordinatorResult:
    """Run the REVIEW phase for the review-only entry point.

    No DEV retry: REQUEST_CHANGES → ESCALATE immediately.
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
        assembled_context=review_context,
        sandboxed=state.sandboxed,
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
    _ro_cost = sum(r.cost_usd or 0.0 for r in state.review_agent_results)
    _ro_elapsed = _pool_elapsed

    _log_review_findings(parsed_review, _ro_p1, _ro_p2, _ro_cost, logger)

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
