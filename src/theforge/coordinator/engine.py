"""Coordinator: deterministic state machine for dev→review loops.

The coordinator is the heart of TheForge. It is NOT an LLM — it is a Python
program that mechanically orchestrates agent invocations. Every decision is
deterministic. Every boundary is a validation checkpoint.

State machine:
    INIT → WORKSPACE → PREFLIGHT → PLAN → PLAN_REVIEW → DEV → VALIDATE → REVIEW
        → (loop or DONE/ESCALATE)

Transitions:
    INIT → WORKSPACE:       Always (create workspace)
    WORKSPACE → PREFLIGHT:  Workspace created successfully
    PREFLIGHT → PLAN/DEV:   Verdict is PROCEED (or agent failed — fail-open)
    PREFLIGHT → DONE:       Verdict is ALREADY_DONE (spec satisfied on main)
    PREFLIGHT → ESCALATE:   Verdict is BLOCKED (spec is stale/invalid)
    PLAN → PLAN_REVIEW:     Plan agent succeeded and plan review is enabled
    PLAN → DEV:             Plan succeeded and review is skipped/disabled
    PLAN_REVIEW → PLAN:     Human requests one regeneration
    PLAN_REVIEW → DEV:      Human approves the plan
    PLAN_REVIEW → stop:     Human abandons the run
    DEV → VALIDATE:         Dev agent finished (success or failure)
    VALIDATE → REVIEW:      Gate produced handoff.yaml with PASS
    VALIDATE → DEV:         Gate failed, retries remaining
    VALIDATE → ESCALATE:    Gate failed, no retries left
    REVIEW → DONE:          Review verdict is APPROVE
    REVIEW → DEV:           Review verdict is REQUEST_CHANGES, retries remaining
    REVIEW → ESCALATE:      Review verdict is REQUEST_CHANGES, no retries left
"""

from __future__ import annotations

import datetime
import signal
import subprocess
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from theforge.artifacts import (
    PLAN_PATH,
    ensure_parent_dir,
    resolve_handoff_path,
)
from theforge.config import ForgeConfig
from theforge.sessions import save_sessions
from theforge.task import (
    TaskStory,
    build_handoff_fix_prompt,
    load_story,
    parse_plan_output,
)

from .log_tee import (  # noqa: E402
    _begin_run_log_tee,
    _end_run_log_tee,
    _make_story_log_dir,
    _safe_signal,
)

# ── Structured logging ────────────────────────────────────────────────
from .logging import StructuredLogger
from .notify import _escalate_notify
from .signals import (  # noqa: E402
    _fire_post_run_hook,
    _make_sigterm_handler,
    _set_timeout_resume,
)
from .state import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
)
from .util import (
    _generate_run_id,
    _log,
    _log_phase,
    _log_verbose,
)
from .workspace import _check_behind_origin, _create_workspace
from .workspace_scrub import _scrub_forge_history

# ── Lazy runner symbols ───────────────────────────────────────────────
# Populated by _ensure_runners() at entry points.
# engine.run_agent owns the handoff-fix retry path only.
# Preflight: patch theforge.coordinator.preflight_flow.run_agent
# DEV:       patch theforge.coordinator.dev_phase.run_agent
run_agent = None
run_agent_pool = None
log_agent_result = None
LogLevel = None

# ── Lazy runner import ────────────────────────────────────────────────


def _ensure_runners() -> None:
    """Import theforge.runners and bind its symbols into this module's namespace.

    Called at each public entry point so the runners package is not imported
    at module load time.  Only fills None slots — preserves any mock patches
    applied by tests before the entry point is called.
    """
    global run_agent, run_agent_pool, log_agent_result, LogLevel
    if (
        run_agent is not None
        and run_agent_pool is not None
        and log_agent_result is not None
        and LogLevel is not None
    ):
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if run_agent_pool is None:
        run_agent_pool = _r.run_agent_pool
    if log_agent_result is None:
        log_agent_result = _r.log_agent_result
    if LogLevel is None:
        LogLevel = _r.LogLevel


def _run_shell(cmd: str, cwd: Path, timeout: int = 120) -> tuple[bool, str]:
    """Run a shell command. Returns (success, combined output).

    Defined here (not re-exported from coord_util) so that
    ``patch('theforge.coordinator._run_shell')`` intercepts calls made
    directly within this module.  Sub-modules (coord_workspace, coord_gate)
    call coord_util._run_shell; patch that symbol when testing those paths.

    On timeout, kills the entire process group so child processes don't
    outlive the shell and consume unbounded memory.
    """
    import os  # noqa: PLC0415

    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        output = (stdout + stderr).strip()
        return proc.returncode == 0, output
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return False, f"TIMEOUT after {timeout}s: {cmd}"
    except Exception as e:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        return False, f"ERROR: {e}"


# ── Log-tee / SIGTERM context manager ────────────────────────────────


@contextmanager
def _run_log_context(
    config: ForgeConfig,
    logger: StructuredLogger,
    task: TaskStory,
    state: CoordinatorState,
    task_start: float,
) -> Generator[None, None, None]:
    """Set up per-run log tee and SIGTERM handler; tear down on exit."""
    _tee = _begin_run_log_tee(config, logger, task.slug, log_dir=state.log_dir)
    _prev_sigterm = None
    if _tee is not None:
        _prev_sigterm = _safe_signal(
            signal.SIGTERM,
            _make_sigterm_handler(
                logger,
                _tee,
                signal.getsignal(signal.SIGTERM),
                state=state,
                task_start=task_start,
                task=task,
                config=config,
            ),
        )
    try:
        yield
    finally:
        _end_run_log_tee(_tee)
        if _prev_sigterm is not None:
            try:
                _safe_signal(signal.SIGTERM, _prev_sigterm)
            except Exception:
                pass


# ── Phase handlers ────────────────────────────────────────────────────
from .dev_phase import _run_dev_phase  # noqa: E402
from .path_setup import prepend_worktree_src  # noqa: E402
from .review_context import _parse_dev_handoff  # noqa: E402
from .review_phase import _ReviewOutcome, _run_review_only_phase, _run_review_phase  # noqa: E402
from .run_setup import _rebase_onto_main, _setup_resume_entry  # noqa: E402
from .validate_phase import _run_validate_phase, _ValidateOutcome  # noqa: E402


def _coordinator_loop(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    story_content: str,
    task_start: float,
    *,
    interactive: bool = False,
    auto_merge: bool = False,
    skip_dev_first_iter: bool = False,
    notify: bool = False,
    logger: StructuredLogger | None = None,
    state_update_fn: "Callable[[dict], None] | None" = None,
    stop_phase: Phase | None = None,
) -> CoordinatorResult:
    """Shared DEV→VALIDATE→REVIEW loop used by run_task() and run_from_review().

    Callers must set state.workspace_path and state.branch_name before calling.

    Args:
        skip_dev_first_iter: When True, the first loop iteration starts directly at
            REVIEW, skipping DEV+VALIDATE. All subsequent iterations run the full
            DEV→VALIDATE→REVIEW sequence. Used by run_from_review() to review the
            existing worktree before invoking the dev agent for the first time.
    """
    assert state.workspace_path is not None
    assert state.branch_name is not None
    workspace_path = state.workspace_path
    branch_name = state.branch_name
    _skip_dev = skip_dev_first_iter
    # Per-cycle retry counter for escalation: reset to 0 at the start of each
    # new review cycle (and on human extend/reject).  state.dev_iteration is a
    # CUMULATIVE counter across all review cycles.  Prompt routing uses
    # state.retry_reason, not dev_iteration, to select build_fix_prompt vs
    # build_dev_prompt.
    _dev_calls_this_cycle: int = 0

    while True:
        if not _skip_dev:
            # ── DEV ───────────────────────────────────────────────
            state.phase = Phase.DEV
            if state_update_fn is not None:
                state_update_fn(
                    {
                        "phase": "DEV",
                        "iteration": state.dev_iteration,
                        "cost_usd": state.total_cost,
                    }
                )
            state.dev_iteration += 1
            state.dev_trace_count += 1
            _dev_calls_this_cycle += 1
            escalation = _run_dev_phase(
                state,
                config,
                task,
                story_content,
                workspace_path,
                branch_name,
                notify=notify,
                logger=logger,
            )
            if escalation is not None:
                return escalation

            # ── Startup failure guard ──────────────────────────────
            if state.dev_results and state.dev_results[-1].startup_failure:
                _last = state.dev_results[-1]
                _snippet = _last.output[:200] if _last.output else "(no output)"
                state.phase = Phase.ESCALATE
                state.error = f"DEV aborted: no agent available ({_snippet})"
                _log(f"✗ ESCALATE   {state.error}")
                if logger:
                    logger._safe_emit("escalate", reason=state.error, phase="DEV")
                return CoordinatorResult(
                    success=False,
                    phase=Phase.ESCALATE,
                    state=state,
                    message=state.error,
                )

            # ── Scrub forge-artifact commits from branch history ──
            _scrub_forge_history(workspace_path, branch_name, config.workspace.base_branch)

            # ── VALIDATE ──────────────────────────────────────────
            _val_outcome, _val_result = _run_validate_phase(
                state,
                config,
                task,
                workspace_path,
                _dev_calls_this_cycle,
                notify=notify,
                logger=logger,
            )
            if _val_outcome == _ValidateOutcome.ESCALATE:
                return _val_result  # type: ignore[return-value]
            if _val_outcome == _ValidateOutcome.RETRY_DEV:
                if (
                    state.dev_results
                    and state.dev_results[-1].exit_code == -9
                    and state.dev_session_id
                    and state.retry_reason == "gate_fail"
                ):
                    gate_result = "FAIL"
                    if state.gate_decisions:
                        gate_result = state.gate_decisions[-1]
                    elif state.human_feedback:
                        prefix = "Gate validation failed: "
                        if state.human_feedback.startswith(prefix):
                            gate_result = f"FAIL - {state.human_feedback.removeprefix(prefix)}"
                    _set_timeout_resume(state, gate_result)
                continue

        if logger:
            logger._safe_emit("phase_end", phase="VALIDATE", outcome="pass")
        _skip_dev = False  # all subsequent iterations start at DEV

        # ── stop_phase gate ───────────────────────────────────
        if stop_phase is not None and stop_phase.value <= Phase.VALIDATE.value:
            state.phase = Phase.VALIDATE
            return CoordinatorResult(
                success=True,
                phase=Phase.VALIDATE,
                state=state,
                message=f"Stopped at --until {stop_phase.name.lower()}",
            )

        # ── DEV HANDOFF VALIDATION ────────────────────────────
        # Validate structured dev handoff after gate passes.
        # Retry up to max_handoff_retries; if still invalid, proceed anyway.
        _handoff = _parse_dev_handoff(config, workspace_path)
        if _handoff is not None and _handoff.parse_errors:
            _max_hf_retries = config.retry.max_handoff_retries
            for _hf_attempt in range(_max_hf_retries):
                _log_verbose(
                    f"Dev handoff validation failed "
                    f"(attempt {_hf_attempt + 1}/{_max_hf_retries}): "
                    f"{_handoff.parse_errors}"
                )
                _log(f"  ⚠ HANDOFF   invalid → retry {_hf_attempt + 1}/{_max_hf_retries}")
                _hf_prompt = build_handoff_fix_prompt(
                    task,
                    workspace_path=workspace_path,
                    branch_name=branch_name,
                    validation_errors=_handoff.parse_errors,
                    handoff_file=config.validation.handoff_file,
                )
                _hf_result = run_agent(
                    prompt=_hf_prompt,
                    profile=config.dev_profile,
                    working_dir=workspace_path,
                    session_id=state.dev_session_id,
                    secrets=config.secrets,
                )
                state.dev_results.append(_hf_result)
                state.dev_session_id = _hf_result.session_id or state.dev_session_id
                save_sessions(
                    workspace_path,
                    state.dev_session_id,
                    state.reviewer_session_ids,
                    state.plan_review_session_ids,
                )
                log_agent_result(_hf_result, "DEV/handoff-fix")
                _handoff = _parse_dev_handoff(config, workspace_path)
                if _handoff is None or not _handoff.parse_errors:
                    _log("  ✓ HANDOFF   valid")
                    break
            else:
                _log("  ⚠ HANDOFF   still invalid after retries — proceeding anyway")

        # ── Persist handoff to logs ────────────────────────────
        if config.validation.handoff_file and state.log_dir is not None:
            try:
                _hf_src = resolve_handoff_path(workspace_path, config.validation.handoff_file)
                if _hf_src is not None and _hf_src.exists():
                    _hf_dest = state.log_dir / f"handoff-iter-{state.dev_iteration}.yaml"
                    _hf_dest.write_bytes(_hf_src.read_bytes())
            except Exception:
                pass  # best-effort, never block pipeline

        # ── REVIEW ────────────────────────────────────────────
        _rev_outcome, _rev_result, config = _run_review_phase(
            state,
            config,
            task,
            story_content,
            workspace_path,
            branch_name,
            task_start,
            interactive=interactive,
            auto_merge=auto_merge,
            notify=notify,
            logger=logger,
            run_id=logger._run_id if logger else "",
        )
        if _rev_outcome in (_ReviewOutcome.DONE, _ReviewOutcome.ESCALATE):
            return _rev_result  # type: ignore[return-value]
        # ── stop_phase gate (REVIEW) ──────────────────────────
        if stop_phase is not None and stop_phase.value <= Phase.REVIEW.value:
            state.phase = Phase.REVIEW
            return CoordinatorResult(
                success=True,
                phase=Phase.REVIEW,
                state=state,
                message=f"Stopped at --until {stop_phase.name.lower()}",
            )
        # RETRY_DEV — reset cycle counter and loop back
        _dev_calls_this_cycle = 0


def run_task(
    config: ForgeConfig,
    task: TaskStory,
    *,
    interactive: bool = False,
    auto_merge: bool = False,
    notify: bool = False,
    run_id: str | None = None,
    plan_path: Path | None = None,
    sprint_name: str | None = None,
    state_update_fn: "Callable[[dict], None] | None" = None,
    start_phase: Phase | None = None,
    stop_phase: Phase | None = None,
    no_pull: bool = False,
    cached_preflight_state: CoordinatorState | None = None,
) -> CoordinatorResult:
    """Execute the full coordinator state machine for a single task.

    This is the main entry point. It creates a workspace, runs the dev agent,
    validates output, runs the review pool (+synthesis if >1 reviewer), and
    loops until done or exhausted.

    Every transition is deterministic. No LLM makes process decisions.

    Args:
        config: The forge configuration.
        task: The task specification.
        interactive: When True, pause at HUMAN_REVIEW for operator input before
            finalizing DONE or ESCALATE. When False (default), behave as before.
        auto_merge: When True, merge the feature branch into base_branch after
            a successful APPROVE. Does NOT merge on ESCALATE or ALREADY_DONE.
    """
    _ensure_runners()
    state = CoordinatorState()
    state.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _task_start = time.monotonic()
    story_content = task.story_text if task.story_text is not None else load_story(task.story_path)
    _sprint_name = sprint_name  # passed to _make_story_log_dir for sprint nesting

    # ── Structured logger ──────────────────────────────────────────
    _run_id = run_id or _generate_run_id()
    logger = StructuredLogger(
        run_id=_run_id,
        project=config.project,
        task=task.slug,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
        project_root=config.project_root,
    )
    logger._safe_emit(
        "run_start",
        specs=[str(task.story_path)],
        budget_usd=config.dev_profile.budget_usd,
        resume=False,
    )

    # ── Per-story log directory ───────────────────────────────────
    # Create early (before WORKSPACE) so the tee can write run-<id>.log from start.
    state.log_dir = _make_story_log_dir(config, task.slug, sprint_name=_sprint_name)

    # ── Per-run log tee + SIGTERM handler ────────────────────────────
    with _run_log_context(config, logger, task, state, _task_start):
        # ── Smart config display ───────────────────────────────────────
        if config.smart_config_models is not None:
            models_str = ", ".join(config.smart_config_models)
            dev_model = config.dev_profile.model
            review_models = ", ".join(p.model for p in config.review_pool)
            synth_model = config.synthesis_profile.model if config.synthesis_profile else "none"
            _log(f"  Models: {models_str}")
            _log(
                f"  Auto-config: dev={dev_model}, review=[{review_models}],"
                f" synthesis={synth_model}"
            )

        # ── Validate --plan path (before touching anything) ─────────
        if plan_path is not None:
            if not plan_path.is_file():
                msg = f"--plan path does not exist or is not a file: {plan_path}"
                _log(f"✗ {msg}")
                return CoordinatorResult(
                    success=False,
                    phase=Phase.INIT,
                    state=state,
                    message=msg,
                )
            try:
                plan_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                msg = f"--plan path is not readable: {plan_path}: {exc}"
                _log(f"✗ {msg}")
                return CoordinatorResult(
                    success=False,
                    phase=Phase.INIT,
                    state=state,
                    message=msg,
                )

        # ── PRE_RUN hook ──────────────────────────────────────────────
        if config.hooks and config.hooks.pre_run:
            from .hooks import build_pre_run_payload
            from .hooks import run_hook as _run_hook

            _pre_payload = build_pre_run_payload(task, _run_id, config)
            _pre_result = _run_hook(
                config.hooks.pre_run,
                _pre_payload,
                config.hooks.timeout_seconds,
                "pre_run",
                logger,
                secrets=config.secrets,
            )
            if _pre_result.exit_code != 0:
                state.phase = Phase.ESCALATE
                state.error = f"pre_run hook aborted run (exit {_pre_result.exit_code})"
                _log(f"✗ ESCALATE   {state.error}")
                logger._safe_emit(
                    "run_end", outcome="escalate", total_cost_usd=0.0, total_duration_s=0.0
                )
                return CoordinatorResult(
                    success=False,
                    phase=state.phase,
                    state=state,
                    message=state.error,
                )

        # ── WORKSPACE ─────────────────────────────────────────────────
        state.phase = Phase.WORKSPACE
        if state_update_fn is not None:
            state_update_fn({"phase": "WORKSPACE", "iteration": 0, "cost_usd": 0.0})
        _log_phase(state.phase, task.slug)
        logger._safe_emit("phase_start", phase="WORKSPACE", iteration=0)

        workspace_path, branch_name, err = _create_workspace(config, task, no_pull=no_pull)
        if err:
            state.phase = Phase.ESCALATE
            state.error = err
            logger._safe_emit("phase_end", phase="WORKSPACE", outcome="escalate")
            logger._safe_emit("escalate", reason=state.error, phase="WORKSPACE")
            logger._safe_emit(
                "run_end", outcome="escalate", total_cost_usd=0.0, total_duration_s=0.0
            )
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=f"Workspace creation failed: {err}",
            )

        assert workspace_path is not None
        assert branch_name is not None
        state.workspace_path = workspace_path
        state.branch_name = branch_name
        prepend_worktree_src(workspace_path)
        logger._safe_emit("phase_end", phase="WORKSPACE", outcome="success")

        # ── Plan injection (--plan) ─────────────────────────────────
        if plan_path is not None:
            plan_text = plan_path.read_text(encoding="utf-8")
            worktree_plan_path = workspace_path / PLAN_PATH
            ensure_parent_dir(worktree_plan_path)
            worktree_plan_path.write_text(plan_text, encoding="utf-8")
            state.plan_output = plan_text
            state.plan_structured = parse_plan_output(plan_text)
            _log(f"  ✓ PLAN   (injected from {plan_path.name})")
            if config.plan_review.enabled:
                _log("  ℹ PLAN_REVIEW   skipped (plan injected)")

        # ── start_phase skip: jump directly to DEV loop ───────────────
        if start_phase is not None and start_phase.value > Phase.PREFLIGHT.value:
            state.preflight_verdict = "SKIPPED"
            _log("  ⚡ PREFLIGHT   skipped (--from phase)")
            # When starting at REVIEW (or later), skip DEV on the first iteration
            # so the existing worktree is reviewed before the dev agent is invoked.
            _skip_dev_first = start_phase.value >= Phase.REVIEW.value
            result = _coordinator_loop(
                state,
                config,
                task,
                story_content,
                _task_start,
                interactive=interactive,
                auto_merge=auto_merge,
                skip_dev_first_iter=_skip_dev_first,
                notify=notify,
                logger=logger,
                state_update_fn=state_update_fn,
                stop_phase=stop_phase,
            )
            _total_elapsed = time.monotonic() - _task_start
            _fire_post_run_hook(config, state, task, result, _run_id, _total_elapsed, logger)
            logger._safe_emit(
                "run_end",
                outcome="done" if result.success else "escalate",
                total_cost_usd=round(state.total_cost, 6),
                total_duration_s=round(_total_elapsed, 2),
            )
            return result

        # ── PREFLIGHT ──────────────────────────────────────────────────
        if cached_preflight_state is not None:
            from .preflight import _apply_preflight_config  # noqa: PLC0415

            state.preflight_verdict = cached_preflight_state.preflight_verdict
            state.preflight_reason = cached_preflight_state.preflight_reason
            state.preflight_complexity = cached_preflight_state.preflight_complexity
            state.preflight_sufficiency = cached_preflight_state.preflight_sufficiency
            state.preflight_work_type = cached_preflight_state.preflight_work_type
            state.preflight_warnings = list(cached_preflight_state.preflight_warnings)
            state.preflight_likely_files = list(cached_preflight_state.preflight_likely_files)
            state.preflight_duration_s = cached_preflight_state.preflight_duration_s
            config = _apply_preflight_config(config, state)
            _pf_result = None
            _pf_already_done_loop = False
        else:
            from .preflight_flow import _run_preflight_phase  # noqa: PLC0415

            config, _pf_result, _pf_already_done_loop = _run_preflight_phase(
                state,
                config,
                task,
                story_content,
                workspace_path,
                branch_name,
                notify=notify,
                logger=logger,
                task_start=_task_start,
                state_update_fn=state_update_fn,
                stop_phase=stop_phase,
            )
        if _pf_result is not None:
            return _pf_result
        if _pf_already_done_loop:
            # ALREADY_DONE override: commits on branch without prior APPROVE → resume REVIEW
            result = _coordinator_loop(
                state,
                config,
                task,
                story_content,
                _task_start,
                interactive=interactive,
                auto_merge=auto_merge,
                skip_dev_first_iter=True,
                notify=notify,
                logger=logger,
                state_update_fn=state_update_fn,
            )
            logger._safe_emit(
                "run_end",
                outcome="done" if result.success else "escalate",
                total_cost_usd=round(state.total_cost, 6),
                total_duration_s=round(time.monotonic() - _task_start, 2),
            )
            return result

        # verdict == "PROCEED" — continue to DEV (possibly via PLAN)

        # ── PLAN FLOW (spec validation, plan, plan review) ──────────────
        from .plan_flow import _run_plan_phase  # noqa: PLC0415

        _plan_result = _run_plan_phase(
            state,
            config,
            task,
            story_content,
            workspace_path,
            plan_path,
            state.preflight_result,
            notify=notify,
            logger=logger,
            run_id=_run_id,
            state_update_fn=state_update_fn,
        )
        if _plan_result is not None:
            return _plan_result

        # ── stop_phase gate: stop before entering DEV ─────────────────
        if stop_phase is not None and stop_phase.value <= Phase.PLAN_REVIEW.value:
            return CoordinatorResult(
                success=True,
                phase=state.phase,
                state=state,
                message=f"Stopped at --until {stop_phase.name.lower()}",
            )

        # ── DEV→VALIDATE→REVIEW loop ─────────────────────────────────
        result = _coordinator_loop(
            state,
            config,
            task,
            story_content,
            _task_start,
            interactive=interactive,
            auto_merge=auto_merge,
            notify=notify,
            logger=logger,
            state_update_fn=state_update_fn,
            stop_phase=stop_phase,
        )
        _total_elapsed = time.monotonic() - _task_start
        _fire_post_run_hook(config, state, task, result, _run_id, _total_elapsed, logger)
        logger._safe_emit(
            "run_end",
            outcome="done" if result.success else "escalate",
            total_cost_usd=round(state.total_cost, 6),
            total_duration_s=round(_total_elapsed, 2),
        )

        # ── Adaptive escalation memory ─────────────────────────────────
        if config.assignment.escalation_memory and config.agents and state.preflight_complexity:
            from .assignment import (
                EscalationRecord as _EscRec,
            )
            from .assignment import (
                append_escalation_record as _append_esc,
            )

            _esc_path = config.project_root / ".forge" / "assignment_history.yaml"
            _esc_outcome = "DONE" if result.success else "ESCALATE"
            _esc_record = _EscRec(
                story=task.slug,
                complexity=state.preflight_complexity.upper()
                if state.preflight_complexity in ("small", "medium", "large")
                else state.preflight_complexity,
                dev_model=config.dev_profile.name,
                outcome=_esc_outcome,
                reason=state.escalation_note or "",
                timestamp=datetime.datetime.utcnow().isoformat() + "Z",
            )
            _append_esc(_esc_path, _esc_record)
            _log_verbose(
                f"[adaptive] Wrote escalation record: story={task.slug} "
                f"complexity={_esc_record.complexity} outcome={_esc_outcome}"
            )

        return result


# ── Shared resume coordinator (run_from_review / run_from_dev) ────────


def _run_resume_coordinator(
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    *,
    initial_phase: Phase,
    skip_dev_first_iter: bool,
    interactive: bool,
    auto_merge: bool,
    notify: bool,
    run_id: str | None,
    sprint_name: str | None,
    state_update_fn: "Callable[[dict], None] | None",
    no_pull: bool = False,
) -> CoordinatorResult:
    """Shared body for run_from_review and run_from_dev.

    Both entry points reuse an existing worktree, differ only in which phase
    they start at and whether the first coordinator loop iteration skips DEV.
    """
    _ensure_runners()
    if not no_pull:
        _check_behind_origin(config)
    setup = _setup_resume_entry(
        config,
        task,
        workspace_path,
        initial_phase=initial_phase,
        notify=notify,
        run_id=run_id,
    )
    if isinstance(setup, CoordinatorResult):
        return setup
    state, logger, branch_name, story_content, _task_start = setup
    state.log_dir = _make_story_log_dir(config, task.slug, sprint_name=sprint_name)
    prepend_worktree_src(workspace_path)

    with _run_log_context(config, logger, task, state, _task_start):
        base_branch = config.workspace.base_branch
        rebase_ok, rebase_err = _rebase_onto_main(str(state.workspace_path), base_branch, logger)
        if not rebase_ok:
            state.phase = Phase.ESCALATE
            state.escalate_reason = (
                f"pre-dev rebase onto {base_branch} failed — conflicts must be resolved manually: "
                f"{rebase_err}"
            )
            state.error = state.escalate_reason
            logger._safe_emit("escalate", reason=state.escalate_reason, phase="RESUME_REBASE")
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=Phase.ESCALATE,
                state=state,
                message=state.escalate_reason,
            )
        logger._safe_emit("rebase", phase="RESUME_REBASE", base_branch=base_branch, outcome="ok")
        result = _coordinator_loop(
            state,
            config,
            task,
            story_content,
            _task_start,
            interactive=interactive,
            auto_merge=auto_merge,
            skip_dev_first_iter=skip_dev_first_iter,
            notify=notify,
            logger=logger,
            state_update_fn=state_update_fn,
        )
        _total_elapsed = time.monotonic() - _task_start
        _fire_post_run_hook(config, state, task, result, logger._run_id, _total_elapsed, logger)
        logger._safe_emit(
            "run_end",
            outcome="done" if result.success else "escalate",
            total_cost_usd=round(state.total_cost, 6),
            total_duration_s=round(_total_elapsed, 2),
        )
        return result


# ── Resume entry points ───────────────────────────────────────────────


def run_from_review(
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    *,
    interactive: bool = False,
    auto_merge: bool = False,
    notify: bool = False,
    run_id: str | None = None,
    sprint_name: str | None = None,
    state_update_fn: "Callable[[dict], None] | None" = None,
    no_pull: bool = False,
) -> CoordinatorResult:
    """Start at REVIEW on an existing worktree, then iterate DEV→VALIDATE→REVIEW as needed.

    This is a first-class entry point that behaves identically to run_task but:
    - Skips WORKSPACE creation and PREFLIGHT (workspace already exists)
    - Begins with an immediate REVIEW of the current worktree state
    - If APPROVE: done (auto-merge if requested)
    - If REQUEST_CHANGES: iterates through DEV→VALIDATE→REVIEW exactly as run_task does

    Args:
        config: The forge configuration.
        task: The task specification.
        workspace_path: Path to the existing worktree.
        interactive: When True, pause at HUMAN_REVIEW for operator input.
        auto_merge: When True, merge the feature branch after APPROVE.
    """
    return _run_resume_coordinator(
        config,
        task,
        workspace_path,
        initial_phase=Phase.REVIEW,
        skip_dev_first_iter=True,
        interactive=interactive,
        auto_merge=auto_merge,
        notify=notify,
        run_id=run_id,
        sprint_name=sprint_name,
        state_update_fn=state_update_fn,
        no_pull=no_pull,
    )


def run_from_dev(
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    *,
    interactive: bool = False,
    auto_merge: bool = False,
    notify: bool = False,
    run_id: str | None = None,
    sprint_name: str | None = None,
    state_update_fn: "Callable[[dict], None] | None" = None,
    no_pull: bool = False,
) -> CoordinatorResult:
    """Start at DEV on an existing worktree, skipping WORKSPACE and PREFLIGHT.

    Used by sprint resume when a worktree has commits ahead of the base branch
    but the gate failed. Reuses the existing workspace without recreating it.

    Args:
        config: The forge configuration.
        task: The task specification.
        workspace_path: Path to the existing worktree.
        interactive: When True, pause at HUMAN_REVIEW for operator input.
        auto_merge: When True, merge the feature branch after APPROVE.
    """
    return _run_resume_coordinator(
        config,
        task,
        workspace_path,
        initial_phase=Phase.DEV,
        skip_dev_first_iter=False,
        interactive=interactive,
        auto_merge=auto_merge,
        notify=notify,
        run_id=run_id,
        sprint_name=sprint_name,
        state_update_fn=state_update_fn,
        no_pull=no_pull,
    )


# ── Review-only mode ─────────────────────────────────────────────────


def run_review_only(
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    *,
    notify: bool = False,
) -> CoordinatorResult:
    """Run only the REVIEW phase on an existing worktree.

    Skips WORKSPACE, PREFLIGHT, DEV, VALIDATE.
    Returns a CoordinatorResult with phase=DONE (APPROVE) or ESCALATE
    (REQUEST_CHANGES — no DEV retry in review-only mode).
    """
    _ensure_runners()
    state = CoordinatorState()
    state.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _ro_task_start = time.monotonic()

    _run_id = _generate_run_id()
    logger = StructuredLogger(
        run_id=_run_id,
        project=config.project,
        task=task.slug,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
        project_root=config.project_root,
    )
    logger._safe_emit(
        "run_start",
        specs=[str(task.story_path)],
        budget_usd=config.dev_profile.budget_usd,
        resume=True,
    )

    # ── Verify workspace exists ───────────────────────────────────────
    if not workspace_path.exists():
        state.phase = Phase.ESCALATE
        state.error = f"Worktree not found at {workspace_path}. Run `forge run` first."
        logger._safe_emit("escalate", reason=state.error, phase="INIT")
        logger._safe_emit("run_end", outcome="escalate", total_cost_usd=0.0, total_duration_s=0.0)
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    state.workspace_path = workspace_path
    branch_name = config.workspace.branch_pattern.format(slug=task.slug)
    state.branch_name = branch_name

    story_content = task.story_text if task.story_text is not None else load_story(task.story_path)

    return _run_review_only_phase(
        state,
        config,
        task,
        story_content,
        workspace_path,
        branch_name,
        notify=notify,
        logger=logger,
        task_start=_ro_task_start,
    )


# ── Audit ────────────────────────────────────────────────────────────
