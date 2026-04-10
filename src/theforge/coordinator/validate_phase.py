"""VALIDATE phase handler: gate execution, dirty-worktree auto-commit, retry/escalate routing."""

from __future__ import annotations

import dataclasses
import subprocess
import time
import types
from enum import Enum, auto
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.task import TaskStory

from . import util as _cu
from .dev_phase import _extract_failed_tests, record_dev_iteration_telemetry
from .gate import _is_gate_skip, _run_gate_full
from .logging import StructuredLogger
from .notify import _escalate_notify
from .review_context import _get_handoff_content, _get_raw_dev_notes
from .state import CoordinatorResult, CoordinatorState, Phase
from .util import _log, _log_phase, _log_verbose
from .workspace import _deindex_forge_artifacts


class _ValidateOutcome(Enum):
    PASS = auto()
    RETRY_DEV = auto()
    REVIEW_CONVENTION_BLOCK = auto()
    ESCALATE = auto()


def _test_file_exists_in_head(workspace_path: Path, test_file: str) -> bool:
    """Return whether the failing test file exists in the current checkout."""
    return workspace_path.joinpath(test_file).is_file()


def _format_failed_test_feedback(
    gate_output_tail: str, workspace_path: Path, contract_change: bool = False
) -> tuple[str, bool]:
    """Return retry-feedback text for extracted failing tests and whether they are existing."""
    failed_tests = _extract_failed_tests(gate_output_tail)
    if not failed_tests:
        return "", False

    existing_failures = [
        test_name
        for test_name in failed_tests
        if _test_file_exists_in_head(workspace_path, test_name.split("::", 1)[0])
    ]
    lines = ["\n\nExtracted failing tests (best effort):"]
    lines.extend(f"- {test_name}" for test_name in failed_tests)
    if existing_failures:
        if contract_change:
            lines.append(
                "Some of these tests may assert the old behavioral contract — "
                "update them if they encode the behavior this story is changing."
            )
        else:
            lines.append(
                "These are existing tests your changes broke — "
                "fix your implementation, do not edit these test files."
            )
    return "\n".join(lines), bool(existing_failures)


def _run_validate_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
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
    gate_result_for_telemetry: str | None = None
    if _is_gate_skip(gate_override):
        _log_phase(state.phase, "skipped (gate: none)")
        _log("  Gate: none (story override)")
        gate_decision: str | None = "PASS"
        gate_err: str | None = None
        gate_result_for_telemetry = gate_decision
    else:
        if gate_override is not None:
            _log_phase(state.phase, "running gate... (override)")
            _log(f"  Gate: {gate_override} (story override)")
        else:
            _log_phase(state.phase, "running gate...")
        gate_decision, gate_err, gate_output_tail, resolved_gate_cmd = _run_gate_full(
            config, workspace_path, task=task, iter_num=state.dev_iteration
        )
        gate_result_for_telemetry = gate_decision or "ERROR"
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
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=config.retry.max_dev_iterations,
            gate_result=gate_result_for_telemetry,
            gate_output_tail=gate_output_tail or gate_err,
        )
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
        gate_cmd = resolved_gate_cmd
        partial = ""
        failed_test_feedback, existing_test_failures = _format_failed_test_feedback(
            gate_output_tail, workspace_path, contract_change=state.preflight_contract_change
        )
        if gate_output_tail and gate_output_tail != gate_err:
            tail_chars = config.validation.gate_output_tail_chars
            partial = f"\n\nPartial gate output (last {tail_chars} chars):\n{gate_output_tail}"
            _log(f"  Gate partial output captured ({len(gate_output_tail)} chars)")
        is_timeout = "timed out" in (gate_err or "").lower()
        if is_timeout:
            debug_cmd = config.validation.gate_debug_command
            if debug_cmd:
                diag = f" To diagnose, run `{debug_cmd}` to isolate the hanging or failing test."
            else:
                diag = (
                    f" Run `{gate_cmd}` yourself to reproduce, find"
                    " what is hanging or failing, and isolate the test."
                )
            state.human_feedback = (
                f"The full test suite (`{gate_cmd}`) {gate_err}."
                " Your changes caused a test to hang or the suite to"
                f" take too long.{diag}"
                " Fix the root cause — do not increase timeouts."
                f" Then run the full suite (`{gate_cmd}`) to verify."
                f"{failed_test_feedback}"
                f"{partial}"
            )
        else:
            state.human_feedback = (
                f"The full test suite (`{gate_cmd}`) {gate_err}."
                " Your changes broke something. Run the full test suite in"
                " the worktree, diagnose the root cause, and fix it."
                f"{failed_test_feedback}"
                f"{partial}"
            )
        state.retry_reason = "gate_fail"
        if state.dev_iteration_telemetry:
            state.dev_iteration_telemetry[-1] = dataclasses.replace(
                state.dev_iteration_telemetry[-1],
                existing_test_failures=existing_test_failures,
            )
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
        # Defensive scrub: if an agent force-added .forge artifacts, remove them
        # from the index before dirty-worktree detection and any auto-commit.
        _deindex_forge_artifacts(workspace_path)
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
                _deindex_forge_artifacts(workspace_path)
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
            record_dev_iteration_telemetry(
                state,
                workspace_path,
                max_iterations=config.retry.max_dev_iterations,
                gate_result=gate_decision,
                gate_output_tail=gate_output_tail,
            )
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
            _escalate_notify(task, state, notify, config)
            return _ValidateOutcome.ESCALATE, CoordinatorResult(
                success=False, phase=state.phase, state=state, message=state.error
            )
        handoff_text = _get_handoff_content(config, workspace_path)
        gate_cmd = resolved_gate_cmd
        tail_chars = config.validation.gate_output_tail_chars
        failed_test_feedback, existing_test_failures = _format_failed_test_feedback(
            gate_output_tail, workspace_path, contract_change=state.preflight_contract_change
        )
        state.human_feedback = (
            f"The full test suite (`{gate_cmd}`) failed."
            " Your changes broke something — not just your new tests,"
            " but potentially existing tests too. Run the full suite in"
            " the worktree, find every failure, diagnose the root cause,"
            f" and fix it.{failed_test_feedback}\n\n"
            f"Gate output (last {tail_chars} chars):\n{gate_output_tail}\n\n"
            f"Current handoff:\n{handoff_text}"
        )
        state.retry_reason = "gate_fail"
        _log(f"  ✗ VALIDATE   {gate_decision}  (iter={state.dev_iteration} → retrying)")
        _log(f"Retrying dev (gate={gate_decision}, iter={state.dev_iteration})")
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=config.retry.max_dev_iterations,
            gate_result=gate_decision,
            gate_output_tail=gate_output_tail,
        )
        if state.dev_iteration_telemetry:
            state.dev_iteration_telemetry[-1] = dataclasses.replace(
                state.dev_iteration_telemetry[-1],
                existing_test_failures=existing_test_failures,
            )
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

    record_dev_iteration_telemetry(
        state,
        workspace_path,
        max_iterations=config.retry.max_dev_iterations,
        gate_result=gate_decision,
        gate_output_tail=gate_output_tail,
    )

    # Hard convention checks (post-gate, only on PASS path)
    # Run in the worktree's subprocess so self-hosting sprints evaluate the
    # worktree's version of conventions.py, not the coordinator's own copy.
    if config.conventions_hard is not None:
        _config_dict = dataclasses.asdict(config.conventions_hard)
        baseline_ref = _get_convention_baseline_ref(workspace_path, config.workspace.base_branch)
        if baseline_ref is not None:
            _cv_result = _cu._run_worktree_eval(
                workspace_path,
                "check_conventions",
                {
                    "config": _config_dict,
                    "project_root": str(workspace_path),
                    "baseline_ref": baseline_ref,
                },
            )
            all_cv_violations = [types.SimpleNamespace(**d) for d in _cv_result["all_violations"]]
            cv_violations = [types.SimpleNamespace(**d) for d in _cv_result["violations"]]
        else:
            _cv_result = _cu._run_worktree_eval(
                workspace_path,
                "check_conventions",
                {"config": _config_dict, "project_root": str(workspace_path)},
            )
            all_cv_violations = [types.SimpleNamespace(**d) for d in _cv_result["violations"]]
            cv_violations = all_cv_violations
        if cv_violations:
            state.convention_violations = [
                {
                    "rule": v.rule,
                    "file": v.file,
                    "detail": v.detail,
                    "blocking": v.blocking,
                }
                for v in cv_violations
            ]
            lines = [f"  - [{v.rule}] {v.file}: {v.detail}" for v in cv_violations]
            human_feedback = "Hard convention violations detected:\n" + "\n".join(lines)
            state.human_feedback = human_feedback
            state.retry_reason = "convention_violations"
            _log(f"  ✗ VALIDATE   convention violations ({len(cv_violations)} found)")
            for v in cv_violations:
                _log(f"    [{v.rule}] {v.file}: {v.detail}")
            if dev_calls_this_cycle >= config.retry.max_dev_iterations:
                state.phase = Phase.ESCALATE
                state.error = f"Hard convention violations after {state.dev_iteration} attempts"
                _log(f"✗ ESCALATE   {state.error}")
                if logger:
                    logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                    logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
                _escalate_notify(task, state, notify, config)
                return _ValidateOutcome.ESCALATE, CoordinatorResult(
                    success=False, phase=state.phase, state=state, message=state.error
                )
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="convention_fail")
            return _ValidateOutcome.REVIEW_CONVENTION_BLOCK, None
        else:
            state.convention_violations = [
                {
                    "rule": v.rule,
                    "file": v.file,
                    "detail": v.detail,
                    "blocking": False,
                }
                for v in all_cv_violations
            ]
    else:
        state.convention_violations = []

    return _ValidateOutcome.PASS, None


def _get_convention_baseline_ref(workspace_path: Path, base_branch: str) -> str | None:
    """Resolve a git ref representing pre-existing convention debt."""
    try:
        proc = subprocess.run(
            ["git", "merge-base", "HEAD", base_branch],
            cwd=workspace_path,
            capture_output=True,
            timeout=10,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    ref = proc.stdout.decode().strip()
    return ref or None
