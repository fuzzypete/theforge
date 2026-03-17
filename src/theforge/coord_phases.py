"""Phase handler functions extracted from coordinator.py.

These implement the DEV, VALIDATE, and REVIEW phases of the coordinator
state machine.  Functions that call symbols patched in tests accept a
``mod`` parameter (the coordinator module) so lookups resolve in the
coordinator's namespace where patches land.
"""

from __future__ import annotations

import time
from dataclasses import replace as _dc_replace
from enum import Enum, auto
from pathlib import Path
from types import ModuleType

from . import coord_util as _cu
from .config import MODEL_REGISTRY, ForgeConfig
from .coord_gate import (
    _auto_commit_side_effects,
    _is_gate_skip,
    _parse_dirty_files,
    _run_gate_full,
)
from .coord_logging import StructuredLogger
from .coord_notify import (
    _escalate_notify,
    _is_remote_mode,
    _ntfy_done_notify,
    _remote_human_review,
)
from .coord_preflight import (
    _escalate_dev_model,
    _find_registry_key_for_profile,
    _has_persistent_p1,
    _persistent_p1_descriptions,
)
from .coord_state import (
    CoordinatorResult,
    CoordinatorState,
    CycleHistory,
    Phase,
    ReviewCycleMetadata,
)
from .coord_util import _fmt_duration, _log, _log_phase, _log_verbose
from .coord_workspace import _merge_branch
from .review import ReviewResult, parse_review_output, review_to_dev_handoff
from .runner import log_agent_result
from .task import TaskSpec

# ── Helpers ──────────────────────────────────────────────────────────


def _append_cycle_history(state: CoordinatorState, parsed_review: ReviewResult) -> None:
    """Append a CycleHistory entry for this completed review cycle (capped at 3)."""
    entry = CycleHistory(
        cycle=len(state.cycle_history) + 1,
        verdict=parsed_review.verdict,
        summary=parsed_review.summary,
        p1_findings=[f.description[:200] for f in parsed_review.findings if f.severity == "P1"],
    )
    state.cycle_history.append(entry)
    if len(state.cycle_history) > 3:
        state.cycle_history = state.cycle_history[-3:]


# ── State machine ────────────────────────────────────────────────────


def _finalize_approve(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    parsed_review: ReviewResult,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    auto_merge: bool,
    notify: bool,
    logger: StructuredLogger | None,
    review_cost: float,
    review_elapsed: float,
    message: str,
) -> CoordinatorResult:
    """Set DONE, optionally merge, log, notify, return CoordinatorResult.

    Pass logger=None to suppress merge_result/phase_end logger events (interactive paths).
    Pass logger=logger to emit them (non-interactive path).
    """
    state.phase = Phase.DONE
    merge_info: dict | None = None
    merge_suffix = ""
    if auto_merge:
        merge_info = _merge_branch(
            config.project_root,
            config.workspace.base_branch,
            branch_name,
            task.slug,
            workspace_path,
            auto_push=config.workspace.auto_push,
            config=config,
            task_name=task.name,
        )
        merge_suffix = (
            " Merged." if merge_info["merged"] else f" Merge failed: {merge_info['error']}"
        )
        if logger:
            logger._safe_emit(
                "merge_result",
                success=merge_info["merged"],
                branch=branch_name,
                error=merge_info.get("error"),
            )
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="REVIEW",
            outcome="approve",
            cost_usd=round(review_cost, 6),
            duration_s=round(review_elapsed, 2),
        )
    _task_elapsed = time.monotonic() - task_start
    _log(f"✓ DONE   total=${state.total_cost:.2f}  {_fmt_duration(_task_elapsed)}")
    _ntfy_done_notify(
        task, state, config, notify, parsed_review.summary, _task_elapsed, branch_name
    )
    return CoordinatorResult(
        success=True,
        phase=state.phase,
        state=state,
        message=f"{message}Branch: {branch_name}{merge_suffix}",
        merge=merge_info,
    )


class _ReviewOutcome(Enum):
    DONE = auto()
    ESCALATE = auto()
    RETRY_DEV = auto()


def _run_review_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    spec_content: str,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    interactive: bool,
    auto_merge: bool,
    notify: bool,
    logger: StructuredLogger | None,
    mod: ModuleType,
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
    _review_cost_before_cycle = sum(r.cost_usd for r in state.review_agent_results)

    parsed_review = None
    last_parse_error: str | None = None

    for _parse_attempt in range(max_parse_retries + 1):
        if _parse_attempt > 0:
            _log_verbose(
                f"Parse retry {_parse_attempt}/{max_parse_retries} "
                f"for review cycle {state.review_cycle + 1}"
            )

        successful, failed_results, synthesis_output = mod._run_review_pool(
            state,
            config,
            task,
            spec_content,
            workspace_path,
            branch_name,
            meta,
            notify=notify,
        )

        if synthesis_output is None:
            # All reviewers failed, budget exceeded, or synthesis failed —
            # state.error already set by _run_review_pool
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

        _candidate = parse_review_output(synthesis_output)

        if _candidate.parse_errors:
            last_parse_error = str(_candidate.parse_errors)
            _log_verbose(
                f"Review parse errors (attempt {_parse_attempt + 1}): {_candidate.parse_errors}"
            )
            if _parse_attempt < max_parse_retries:
                meta.parse_retries += 1
                _log_verbose(
                    f"Retrying reviewer ({meta.parse_retries}/{max_parse_retries} retries "
                    f"used) — parse error does NOT increment review cycle"
                )
                continue
            break

        parsed_review = _candidate
        break

    if parsed_review is None:
        state.phase = Phase.ESCALATE
        state.error = (
            f"Review pool unreliable: all reviewers failed to produce valid output "
            f"after {meta.parse_retries} retries. Last error: {last_parse_error}"
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
                phase=state.phase,
                state=state,
                message=state.error,
            ),
            config,
        )

    # Valid verdict — increment review cycle counter
    state.review_cycle += 1
    state.review_results.append(parsed_review)

    _review_elapsed = time.monotonic() - _review_pool_start
    _p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
    _p2_count = sum(1 for f in parsed_review.findings if f.severity == "P2")
    _review_cost = sum(r.cost_usd for r in state.review_agent_results) - _review_cost_before_cycle

    _log_verbose(f"Review verdict: {parsed_review.verdict}")
    _log_verbose(f"  Summary: {parsed_review.summary}")
    _log_verbose(f"  Findings: {len(parsed_review.findings)} ({_p1_count} P1)")
    if logger:
        logger._safe_emit(
            "review_result",
            verdict=parsed_review.verdict,
            p1_count=_p1_count,
            p2_count=_p2_count,
            cost_usd=round(_review_cost, 6),
        )

    # ── APPROVE ──────────────────────────────────────────────────
    if parsed_review.verdict == "APPROVE":
        _log(
            f"  ✓ REVIEW   APPROVE  {_p1_count} P1  {_p2_count} P2"
            f"  ${_review_cost:.2f}  {_fmt_duration(_review_elapsed)}"
        )
        if interactive:
            state.phase = Phase.HUMAN_REVIEW
            _log_phase(state.phase)
            if _is_remote_mode(notify, config):
                decision, feedback = _remote_human_review(
                    state, parsed_review, workspace_path, branch_name, task, config, task_start
                )
            else:
                decision, feedback = mod._human_review(
                    state, parsed_review, workspace_path, branch_name
                )
            state.human_review_decision = decision
            state.human_review_feedback = feedback
            if decision == "approve":
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
                ),
                config,
            )

    # ── REQUEST_CHANGES ──────────────────────────────────────────
    _is_persistent_p1 = False
    if config.smart_config_models is not None and len(state.review_results) >= 2:
        _prev_result = state.review_results[-2]
        _is_persistent_p1 = _has_persistent_p1(parsed_review.findings, _prev_result.findings)

    _persistent_tag = " (persistent)" if _is_persistent_p1 else ""
    _log(
        f"  ✗ REVIEW   REQUEST_CHANGES  {_p1_count} P1{_persistent_tag}"
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
            if _is_remote_mode(notify, config):
                decision, feedback = _remote_human_review(
                    state, parsed_review, workspace_path, branch_name, task, config, task_start
                )
            else:
                decision, feedback = mod._human_review(
                    state, parsed_review, workspace_path, branch_name
                )
            state.human_review_decision = decision
            state.human_review_feedback = feedback
            if decision == "approve":
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
    mod: ModuleType,
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
        _log("  Gate: none (spec override)")
        gate_decision: str | None = "PASS"
        gate_err: str | None = None
    else:
        if gate_override is not None:
            _log_phase(state.phase, "running gate... (override)")
            _log(f"  Gate: {gate_override} (spec override)")
        else:
            _log_phase(state.phase, "running gate...")
        gate_decision, gate_err, gate_output_tail = _run_gate_full(
            config, workspace_path, task=task
        )
    _gate_elapsed = time.monotonic() - _gate_start
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
                parsed_files = _parse_dirty_files("\n".join(dirty_lines))

                def _in_scope(f: str) -> bool:
                    for s in task.file_scope:
                        if f == s:
                            return True
                        prefix = s if s.endswith("/") else f"{s}/"
                        if f.startswith(prefix):
                            return True
                    return False

                def _dirty_dev_retry(
                    dirty_files: str,
                ) -> tuple[_ValidateOutcome, CoordinatorResult | None]:
                    _log(f"Dirty worktree detected: {dirty_files}")
                    if dev_calls_this_cycle >= config.retry.max_dev_iterations:
                        state.phase = Phase.ESCALATE
                        state.error = f"Dev agent left uncommitted changes: {dirty_files}"
                        _log(f"✗ ESCALATE   {state.error}")
                        _escalate_notify(task, state, notify, config)
                        return _ValidateOutcome.ESCALATE, CoordinatorResult(
                            success=False, phase=state.phase, state=state, message=state.error
                        )
                    state.human_feedback = (
                        "PROCESS VIOLATION: You left uncommitted changes in"
                        f" the worktree: {dirty_files}. You MUST commit ALL"
                        " modified files before running the gate. Stage and"
                        " commit them now."
                    )
                    state.retry_reason = "dirty_worktree"
                    return _ValidateOutcome.RETRY_DEV, None

                all_tracked = len(parsed_files) == len(dirty_lines)
                if task.file_scope and parsed_files and all_tracked:
                    in_scope = [f for f in parsed_files if _in_scope(f)]
                    out_of_scope = [f for f in parsed_files if not _in_scope(f)]
                    if not in_scope:
                        if _auto_commit_side_effects(workspace_path, out_of_scope):
                            pass  # fall through to PASS
                        else:
                            return _dirty_dev_retry(", ".join(out_of_scope))
                    else:
                        return _dirty_dev_retry(", ".join(parsed_files))
                else:
                    raw_names = ", ".join(
                        line.strip().split(maxsplit=1)[-1] for line in dirty_lines
                    )
                    return _dirty_dev_retry(raw_names)

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
        handoff_text = mod._get_handoff_content(config, workspace_path)
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
    spec_content: str,
    workspace_path: Path,
    branch_name: str,
    *,
    notify: bool,
    logger: StructuredLogger | None,
    mod: ModuleType,
) -> CoordinatorResult | None:
    """Run one DEV iteration. Returns CoordinatorResult on budget escalation, else None.

    Caller must increment state.dev_iteration and _dev_calls_this_cycle before calling.
    Mutates state in-place (appends dev_results, updates dev_session_id, etc.).
    """
    _log_phase(
        state.phase,
        f"{config.dev_profile.model}  iter={state.dev_iteration}",
    )
    if logger:
        logger._safe_emit("phase_start", phase="DEV", iteration=state.dev_iteration)

    _gate_cmd = (
        task.gate_override
        if task.gate_override is not None and not _is_gate_skip(task.gate_override)
        else config.validation.gate_command
    )
    if state.retry_reason == "timeout_resume":
        prompt = (
            state.human_feedback
            or "You were cut off by a timeout. Continue from where you left off."
        )
        state.retry_reason = None
        state.human_feedback = None
    elif state.retry_reason in ("review_changes", "extend") and state.last_review_findings:
        prompt = mod.build_fix_prompt(
            task,
            workspace_path=workspace_path,
            branch_name=branch_name,
            review_findings=state.last_review_findings,
            gate_command=_gate_cmd,
            gate_skipped=_is_gate_skip(task.gate_override),
            iteration=state.dev_iteration,
            cycle_history=state.cycle_history or None,
            escalation_note=state.escalation_note,
        )
        state.escalation_note = None  # consumed
    else:
        prompt = mod.build_dev_prompt(
            task,
            workspace_path=workspace_path,
            branch_name=branch_name,
            spec_content=spec_content,
            gate_command=_gate_cmd,
            gate_skipped=_is_gate_skip(task.gate_override),
            review_findings=state.last_review_findings,
            human_feedback=state.human_feedback,
            preflight_output=(state.preflight_result.output if state.preflight_result else None),
            plan_output=state.plan_output,
            iteration=state.dev_iteration,
            cycle_history=state.cycle_history or None,
            escalation_note=state.escalation_note,
        )
        state.escalation_note = None  # consumed
    state.retry_reason = None  # consumed

    _dev_start = time.monotonic()
    dev_result = mod.run_agent(
        prompt=prompt,
        profile=config.dev_profile,
        working_dir=workspace_path,
        session_id=state.dev_session_id,
    )
    _dev_elapsed = time.monotonic() - _dev_start
    state.dev_results.append(dev_result)
    state.dev_durations.append(_dev_elapsed)
    if dev_result.exit_code == -9:
        state.dev_session_id = dev_result.session_id or state.dev_session_id
    else:
        state.dev_session_id = dev_result.session_id
    log_agent_result(dev_result, "DEV")
    _log(f"  ✓ DEV   ${dev_result.cost_usd:.2f}  {_fmt_duration(_dev_elapsed)}")
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

    return None
