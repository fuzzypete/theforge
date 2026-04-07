"""DEV phase handler: prompt routing, agent invocation, budget enforcement, zero-change guard."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterable
from dataclasses import replace as _dc_replace
from pathlib import Path

import yaml

from theforge.config import ForgeConfig
from theforge.coordinator.context_scope import plan_file_list
from theforge.sessions import save_sessions
from theforge.task import ContextAssembler, TaskStory, build_dev_prompt, build_fix_prompt
from theforge.traces import write_trace

from .gate import _is_gate_skip
from .logging import StructuredLogger
from .notify import _escalate_notify
from .state import CoordinatorResult, CoordinatorState, DevIterationTelemetry, Phase
from .util import _fmt_duration, _log, _log_phase, _log_verbose, resolve_timeout

# ── Lazy runner slot ──────────────────────────────────────────────────
# None until first call; tests may replace before calling run_task.
# Patch targets:
#   theforge.coordinator.dev_phase.run_agent        — dev agent call
#   theforge.coordinator.dev_phase.log_agent_result — dev result logging
run_agent = None
log_agent_result = None


def _extract_failed_tests(gate_output_tail: str) -> list[str]:
    """Best-effort extraction of failing test identifiers from gate output."""
    failed: list[str] = []
    for raw_line in gate_output_tail.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("FAILED ", "ERROR ")):
            candidate = line.split()[1].rstrip(":")
            if candidate not in failed:
                failed.append(candidate)
        elif "::" in line and any(token in line.lower() for token in ("failed", "error")):
            candidate = line.split()[0].rstrip(":")
            if candidate not in failed:
                failed.append(candidate)
    return failed


def _git_lines(workspace_path: Path, args: Iterable[str]) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return []
    if proc.returncode not in (0, 1):
        return []
    return [
        line.strip() for line in proc.stdout.decode(errors="replace").splitlines() if line.strip()
    ]


def record_dev_iteration_telemetry(
    state: CoordinatorState,
    workspace_path: Path,
    *,
    max_iterations: int,
    gate_result: str | None,
    gate_output_tail: str = "",
) -> None:
    """Capture per-iteration dev telemetry after validation completes."""
    if not state.dev_results or not state.dev_durations:
        return
    iteration = state.dev_iteration
    dev_result = state.dev_results[-1]
    duration_s = state.dev_durations[-1]
    baseline = state.last_dev_start_commit or "HEAD"
    files_changed = _git_lines(workspace_path, ["diff", "--name-only", baseline, "HEAD"])
    dirty_files = [
        line.split(maxsplit=1)[-1]
        for line in _git_lines(workspace_path, ["status", "--porcelain"])
    ]
    for dirty in dirty_files:
        if dirty not in files_changed:
            files_changed.append(dirty)

    failed_tests = _extract_failed_tests(gate_output_tail)
    prev_failed = (
        state.dev_iteration_telemetry[-1].failed_tests if state.dev_iteration_telemetry else []
    )
    tests_fixed_count = len(set(prev_failed) - set(failed_tests)) if prev_failed else 0
    meaningful_progress = bool(files_changed or tests_fixed_count > 0)
    state.dev_iteration_telemetry.append(
        DevIterationTelemetry(
            iteration=iteration,
            max_iterations=max_iterations,
            cost_usd=dev_result.cost_usd,
            duration_s=duration_s,
            gate_result=gate_result,
            failed_tests=failed_tests,
            files_changed=files_changed,
            files_changed_count=len(files_changed),
            tests_fixed_count=tests_fixed_count,
            meaningful_progress=meaningful_progress,
        )
    )


def _still_open_p1s_for_dev_prompt(state: CoordinatorState) -> list:
    """Return still-open P1 findings for carry-forward prompt context."""
    open_dispositions = {
        "unresolved",
        "net_new",
        "corroborated_new",
        "regression",
        "ac_blocking",
    }
    return [
        record
        for record in state.finding_registry
        if record.severity == "P1" and record.disposition in open_dispositions
    ]


def _prior_open_p1s_for_dev_prompt(state: CoordinatorState) -> list:
    """Return still-open P1s that predate the most recent review cycle."""
    if state.review_cycle <= 1:
        return []
    return [
        record
        for record in _still_open_p1s_for_dev_prompt(state)
        if record.cycle_first_seen < state.review_cycle
    ]


def _current_cycle_p1s_for_dev_prompt(state: CoordinatorState) -> list:
    """Return classified P1s from the most recent review cycle."""
    if state.review_cycle <= 0:
        return []
    return [
        record
        for record in state.finding_registry
        if record.severity == "P1" and record.cycle_last_seen == state.review_cycle
    ]


def _ensure_runners() -> None:
    global run_agent, log_agent_result
    if run_agent is not None and log_agent_result is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if log_agent_result is None:
        log_agent_result = _r.log_agent_result


def _run_dev_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
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
        state.dev_prompt_injected_finding_ids.append([])
        state.retry_reason = None
        state.human_feedback = None
    elif state.retry_reason in ("review_changes", "extend") and state.last_review_findings:
        carry_forward_p1s = _prior_open_p1s_for_dev_prompt(state)
        current_cycle_p1s = _current_cycle_p1s_for_dev_prompt(state)
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
            prior_open_p1s=carry_forward_p1s or None,
            classified_p1s=current_cycle_p1s or None,
            surviving_families=state.surviving_families or None,
            conventions=config.conventions_soft,
        )
        state.dev_prompt_injected_finding_ids.append([r.finding_id for r in carry_forward_p1s])
        state.escalation_note = None  # consumed
    else:
        dev_context = ContextAssembler.from_config(config).assemble(
            phase="dev",
            story_text=story_content,
            file_list=plan_file_list(state.plan_structured) or None,
        )
        state.context_manifests.append({"phase": "dev", "manifest": dev_context})
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
            preflight_sufficiency=state.preflight_sufficiency,
            conventions=config.conventions_soft,
            assembled_context=dev_context,
        )
        state.dev_prompt_injected_finding_ids.append([])
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
    # agent produced no changes relative to the previous iteration baseline,
    # escalate immediately. This rejects self-reported handoffs when the
    # worktree is unchanged instead of burning another review cycle.
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
                "Dev retry produced no changes in the worktree relative to the previous "
                "iteration baseline — escalating to avoid re-reviewing identical code"
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
