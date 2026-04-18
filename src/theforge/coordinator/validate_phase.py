"""VALIDATE phase handler: gate execution, dirty-worktree auto-commit, retry/escalate routing."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import subprocess
import time
import types
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.task import TaskStory

from . import util as _cu
from .dev_phase import _extract_failed_tests, record_dev_iteration_telemetry
from .gate import _is_gate_skip, _parse_dirty_files, _run_gate_debug_command, _run_gate_full
from .logging import StructuredLogger
from .notify import _escalate_notify
from .review_context import _get_handoff_content, _get_raw_dev_notes, _latest_forge_handoff_path
from .state import CoordinatorResult, CoordinatorState, DevIterationTelemetry, Phase, RetryReason
from .util import _log, _log_phase, _log_verbose
from .workspace import _deindex_forge_artifacts


class _ValidateOutcome(Enum):
    PASS = auto()
    RETRY_DEV = auto()
    REVIEW_CONVENTION_BLOCK = auto()
    ESCALATE = auto()


def _is_identical_failure(telemetry: list[DevIterationTelemetry]) -> bool:
    """Return True if the last two recorded iterations share an identical failure signature.

    Two failure signatures are identical when:
    - Both iterations timed out, OR
    - Both have the same non-empty set of failing tests.
    """
    if len(telemetry) < 2:
        return False
    prev = telemetry[-2]
    curr = telemetry[-1]
    if curr.is_timeout and prev.is_timeout:
        return True
    if curr.failed_tests and set(curr.failed_tests) == set(prev.failed_tests):
        return True
    return False


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


def _check_conventions_parallel(
    config: ForgeConfig,
    workspace_path: Path,
) -> tuple[list, list] | None:
    """Return (all_violations, net_new_violations) or None if conventions are not configured.

    Designed to run in a thread concurrently with gate execution.
    Gate commands are test runners; they don't write source files.
    Convention scanner reads source, not test output. Parallel execution is safe.
    """
    if config.conventions_hard is None:
        return None
    _config_dict = dataclasses.asdict(config.conventions_hard)
    baseline_ref = _get_convention_baseline_ref(workspace_path, config.workspace.base_branch)
    if baseline_ref is not None:
        result = _cu._run_worktree_eval(
            workspace_path,
            "check_conventions",
            {
                "config": _config_dict,
                "project_root": str(workspace_path),
                "baseline_ref": baseline_ref,
            },
        )
        all_v = [types.SimpleNamespace(**d) for d in result["all_violations"]]
        net_v = [types.SimpleNamespace(**d) for d in result["violations"]]
    else:
        result = _cu._run_worktree_eval(
            workspace_path,
            "check_conventions",
            {"config": _config_dict, "project_root": str(workspace_path)},
        )
        all_v = [types.SimpleNamespace(**d) for d in result["violations"]]
        net_v = all_v
    return all_v, net_v


def _run_validate_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    *,
    notify: bool,
    logger: StructuredLogger | None,
    state_update_fn: Callable[[dict], None] | None = None,
) -> tuple[_ValidateOutcome, CoordinatorResult | None]:
    """Run one VALIDATE iteration. Returns (outcome, result).

    ESCALATE returns (ESCALATE, CoordinatorResult). RETRY_DEV and PASS return (outcome, None).
    Does NOT emit 'phase_end VALIDATE pass' — caller emits that (it fires on skip too).
    """
    state.phase = Phase.VALIDATE
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "VALIDATE",
                "iteration": state.dev_iteration,
                "cost_usd": state.total_cost,
            }
        )
    if logger:
        logger._safe_emit("phase_start", phase="VALIDATE", iteration=state.dev_iteration)

    # Submit convention check to run in parallel with gate execution.
    # Gate commands are test runners; they don't write source files. Convention
    # scanner reads source, not test output. Parallel execution is safe.
    _cv_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _cv_future: concurrent.futures.Future = _cv_executor.submit(
        _check_conventions_parallel, config, workspace_path
    )

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

    # Retrieve convention check result (ran in parallel; should be done by now).
    try:
        _cv_result_raw = _cv_future.result(timeout=120)
    except Exception as exc:
        _log(f"  ⚠ Convention check failed or timed out: {exc}")
        _cv_result_raw = None
    finally:
        _cv_executor.shutdown(wait=False)

    _cv_all: list = _cv_result_raw[0] if _cv_result_raw is not None else []
    _cv_violations: list = _cv_result_raw[1] if _cv_result_raw is not None else []

    if gate_err:
        is_timeout = "timed out" in (gate_err or "").lower()
        if is_timeout:
            debug_telemetry = _run_gate_debug_command(
                config,
                workspace_path,
                iter_num=state.dev_iteration,
            )
            if debug_telemetry is not None:
                state.gate_debug_telemetry.append(debug_telemetry)
                gate_err = (
                    f"{gate_err}. Gate debug command ran; see audit "
                    f"iterations.gate_debug[-1] and trace "
                    f".forge/traces/{state.dev_iteration}-gate-debug.txt."
                    f"\nGate debug output tail:\n{debug_telemetry.output_tail}"
                )
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=config.retry.max_dev_iterations,
            gate_result=gate_result_for_telemetry,
            gate_output_tail=gate_output_tail or gate_err,
            is_timeout=is_timeout,
        )
        # Check consecutive identical failures (including timeouts) before escalating.
        if _is_identical_failure(state.dev_iteration_telemetry):
            state.phase = Phase.ESCALATE
            state.error = (
                f"Identical gate failure on consecutive iterations"
                f" (iteration {state.dev_iteration}): gate error: {gate_err}."
                f" Remaining retry budget: {state.budget.remaining()}."
            )
        else:
            state.phase = Phase.ESCALATE
            state.error = gate_err
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
            logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
        _escalate_notify(task, state, notify, config)
        return _ValidateOutcome.ESCALATE, CoordinatorResult(
            success=False, phase=state.phase, state=state, message=state.error
        )

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
            dirty_files = _parse_dirty_files(dirty_out)
            if dirty_files:
                raw_names = ", ".join(dirty_files)
                _log(f"Dirty worktree detected: {raw_names}")

                # Auto-commit: synthesize message from handoff, don't
                # re-invoke the agent (full-prompt retry burns tokens and
                # times out — the agent already wrote the code).
                dev_notes = _get_raw_dev_notes(
                    forge_handoff_path=_latest_forge_handoff_path(state)
                )
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
        if state.budget.is_exhausted():
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
        handoff_text = _get_handoff_content(forge_handoff_path=_latest_forge_handoff_path(state))
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
        state.retry_reason = RetryReason.GATE_FAIL
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
        if _is_identical_failure(state.dev_iteration_telemetry):
            state.phase = Phase.ESCALATE
            state.error = (
                f"Identical gate failure on consecutive iterations"
                f" (iteration {state.dev_iteration}): gate returned {gate_decision}."
                f" Remaining retry budget: {state.budget.remaining()}."
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
            _escalate_notify(task, state, notify, config)
            return _ValidateOutcome.ESCALATE, CoordinatorResult(
                success=False, phase=state.phase, state=state, message=state.error
            )
        if _cv_violations:
            _blocking_cv2 = [v for v in _cv_violations if v.blocking]
            _followup_cv2 = [v for v in _cv_violations if not v.blocking]
            state.convention_violations = [
                {"rule": v.rule, "file": v.file, "detail": v.detail, "blocking": v.blocking}
                for v in _cv_violations
            ]
            if _blocking_cv2:
                lines = [f"  - [{v.rule}] {v.file}: {v.detail}" for v in _blocking_cv2]
                state.human_feedback += (
                    "\n\nAdditionally, hard convention violations were detected:\n"
                    + "\n".join(lines)
                )
            for v in _followup_cv2:
                _log(f"  Convention follow-up [hygiene]: {v.rule} in {v.file} — {v.detail}")
            _log(
                f"  ✗ VALIDATE   convention violations also found"
                f" ({len(_blocking_cv2)} blocking, {len(_followup_cv2)} follow-up)"
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

    # Convention check ran in parallel with gate; use pre-fetched result.
    # Runs in the worktree's subprocess so self-hosting sprints evaluate the
    # worktree's version of conventions.py, not the coordinator's own copy.
    if config.conventions_hard is not None:
        if _cv_violations:
            blocking_violations = [v for v in _cv_violations if v.blocking]
            followup_violations = [v for v in _cv_violations if not v.blocking]

            # Record ALL violations on state — blocking flag preserved
            state.convention_violations = [
                {
                    "rule": v.rule,
                    "file": v.file,
                    "detail": v.detail,
                    "blocking": v.blocking,
                }
                for v in _cv_violations
            ]

            # Log and emit follow-up (hygiene) violations — not blocking
            for v in followup_violations:
                _log(f"  Convention follow-up [hygiene]: {v.rule} in {v.file} — {v.detail}")
                if logger:
                    logger._safe_emit(
                        "convention_followup",
                        severity="hygiene",
                        rule=v.rule,
                        file=v.file,
                        detail=v.detail,
                        suggested_story_title=f"Split {v.file} below LOC limit",
                    )

            if blocking_violations:
                lines = [f"  - [{v.rule}] {v.file}: {v.detail}" for v in blocking_violations]
                human_feedback = "Hard convention violations detected:\n" + "\n".join(lines)
                state.human_feedback = human_feedback
                state.retry_reason = RetryReason.CONVENTION_VIOLATIONS
                _log(
                    f"  ✗ VALIDATE   convention violations"
                    f" ({len(blocking_violations)} blocking found)"
                )
                for v in blocking_violations:
                    _log(f"    [{v.rule}] {v.file}: {v.detail}")
                if state.budget.is_exhausted():
                    state.phase = Phase.ESCALATE
                    state.error = (
                        f"Hard convention violations after {state.dev_iteration} attempts"
                    )
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
            # Only follow-up violations — proceed to PASS
        else:
            state.convention_violations = [
                {
                    "rule": v.rule,
                    "file": v.file,
                    "detail": v.detail,
                    "blocking": False,
                }
                for v in _cv_all
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
