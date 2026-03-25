"""VALIDATE phase handler.

Owns _run_validate_phase: gate execution, dirty worktree auto-commit,
and retry/escalation logic on FAIL/BLOCKED gate decisions.
"""

from __future__ import annotations

import subprocess
import time
from enum import Enum, auto
from pathlib import Path
from types import ModuleType

from theforge.config import ForgeConfig
from theforge.task import TaskStory as TaskSpec  # noqa: F401

from . import util as _cu
from .gate import _is_gate_skip, _run_gate_full
from .logging import StructuredLogger
from .notify import _escalate_notify
from .state import CoordinatorResult, CoordinatorState, Phase
from .util import _log, _log_phase, _log_verbose
from .workspace_reader import _get_handoff_content, _get_raw_dev_notes


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
