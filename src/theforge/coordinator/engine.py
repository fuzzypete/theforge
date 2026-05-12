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
    VALIDATE → REVIEW:      Gate exited 0 (PASS)
    VALIDATE → DEV:         Gate failed, retries remaining
    VALIDATE → ESCALATE:    Gate failed, no retries left
    REVIEW → DONE:          Review verdict is APPROVE
    REVIEW → DEV:           Review verdict is REQUEST_CHANGES, retries remaining
    REVIEW → ESCALATE:      Review verdict is REQUEST_CHANGES, no retries left
"""

from __future__ import annotations

import datetime
import signal
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from theforge.artifacts import (
    PLAN_PATH,
    ensure_parent_dir,
)
from theforge.config import ForgeConfig
from theforge.review import ReviewFinding, ReviewResult
from theforge.task import (
    TaskStory,
    load_story,
    parse_plan_output,
)

from .cancellation import StoryCancelled
from .log_tee import (  # noqa: E402
    _begin_run_log_tee,
    _end_run_log_tee,
    _make_story_log_dir,
    _safe_signal,
)

# ── Structured logging ────────────────────────────────────────────────
from .logging import StructuredLogger
from .notify import _escalate_notify
from .preflight_cache import (
    apply_cached_preflight_state,
    validate_preflight_cache,
)
from .signals import (  # noqa: E402
    _fire_post_run_hook,
    _make_sigterm_handler,
    _set_timeout_resume,
)
from .state import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    RetryReason,
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


def _build_convention_blocking_review(state: CoordinatorState) -> ReviewResult:
    """Create a synthetic blocking review result for hard convention violations."""
    findings = [
        ReviewFinding(
            severity="P1",
            file=str(violation.get("file", "")),
            line=None,
            description=(
                f"Hard convention violation [{violation.get('rule', 'unknown')}] in "
                f"{violation.get('file', 'unknown')}: {violation.get('detail', '')}"
            ),
            suggestion="Resolve the hard convention violation before approval.",
        )
        for violation in state.convention_violations
    ]
    if not findings:
        findings = [
            ReviewFinding(
                severity="P1",
                file="",
                line=None,
                description=(
                    "Hard convention violations detected after gate PASS, but no violation "
                    "details were recorded. Manual investigation required."
                ),
                suggestion=(
                    "Inspect validate-phase convention logs and fix the blocking violation."
                ),
            )
        ]
    return ReviewResult(
        verdict="REQUEST_CHANGES",
        summary=(
            "Hard convention violations detected after gate PASS; approval is blocked "
            "until they are fixed."
        ),
        findings=findings,
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={
            "source": "validate_convention_block",
            "convention_violations": state.convention_violations,
        },
    )


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
from .review_phase import (  # noqa: E402
    _perform_dev_model_escalation,
    _ReviewOutcome,
    _run_review_only_phase,
    _run_review_phase,
)
from .run_setup import _rebase_onto_main, _setup_resume_entry  # noqa: E402,I001
from .validate_phase import _run_validate_phase, _ValidateOutcome  # noqa: E402


def _cancelled_result(task: TaskStory, state: CoordinatorState) -> CoordinatorResult:
    """Build a failed CoordinatorResult for a story aborted via stop_event.

    Bypasses _record_run_memory() — the sprint scheduler has already written
    the authoritative timeout audit record; writing a separate ESCALATE
    record here would produce two contradictory failure narratives.
    """
    _log(f"INFO {task.slug}: cancelled by sprint stop_event")
    state.phase = Phase.ESCALATE
    state.error = "Story cancelled by sprint timeout"
    state.error_type = "StoryCancelled"
    return CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message="Story cancelled by sprint timeout",
    )


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
    stop_event: "threading.Event | None" = None,
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
    # Derive per-story adaptive iteration limits from preflight complexity and
    # historical usage.  RetryPolicy.adaptive_iterations=False returns the
    # policy floor values verbatim; floor and cap always bound the outcome.
    from theforge.model_profiles import load_profiles  # noqa: PLC0415

    from .adaptive_iterations import derive_limits  # noqa: PLC0415
    from .util import resolve_timeout_with_active  # noqa: PLC0415

    if (
        state.adaptive_dev_max == 0
        or state.adaptive_review_max == 0
        or state.adaptive_dev_timeout_seconds == 0
        or state.adaptive_dev_budget_usd == 0.0
    ):
        _static_dev_timeout, _timeout_override_active = resolve_timeout_with_active(
            config.dev_profile.timeout_seconds,
            config.dev_profile.timeout_medium_seconds,
            config.dev_profile.timeout_large_seconds,
            state.preflight_complexity,
            state.preflight_complexity_score,
        )
        _static_dev_max = config.dev_profile.max_iterations or config.retry.max_dev_iterations
        _static_dev_budget = config.dev_profile.budget_usd
        _explicit_dev_override = "dev" in getattr(state, "_explicit_roles", set())
        _history_path = config.project_root / ".forge" / "audits" / "history.jsonl"
        _profiles_path = config.project_root / ".forge" / "model_profiles.yaml"
        _adaptive_resource_enabled = (
            config.assignment.enabled
            and config.assignment.adaptive_enabled
            and not _explicit_dev_override
        )
        _limits = derive_limits(
            state.preflight_complexity_score,
            state.preflight_complexity,
            config.retry,
            model_name=config.dev_profile.name,
            model_actual=config.dev_profile.model,
            model_provider=config.dev_profile.provider,
            model_cli=config.dev_profile.cli,
            base_timeout_seconds=_static_dev_timeout,
            base_budget_usd=_static_dev_budget,
            static_dev_max=_static_dev_max,
            review_history_path=_history_path,
            model_profiles=load_profiles(_profiles_path) if _adaptive_resource_enabled else None,
        )
        if not _adaptive_resource_enabled:
            _audit = dict(_limits.audit)
            _audit["enabled"] = False
            _audit["explicit_dev_override"] = _explicit_dev_override
            if _explicit_dev_override:
                _audit["rationale"] = (
                    "explicit dev forge.yaml override preserved over adaptive limits"
                )
            else:
                _audit["rationale"] = (
                    "adaptive assignment disabled; using static configured dev limits"
                )
            if _limits.audit.get("review_history_sample_size", 0) > 0:
                if _limits.audit.get("chosen_review_max", 0) > _limits.audit.get("base_review", 0):
                    _audit["rationale"] += (
                        f" review history raised review_max to {_limits.review_max}."
                    )
                else:
                    _audit["rationale"] += (
                        " review history stayed within the complexity-derived review base."
                    )
            else:
                _audit["rationale"] += (
                    " no matching review history; using complexity-derived review limit."
                )
            _limits = type(_limits)(
                dev_max=_static_dev_max,
                review_max=_limits.review_max,
                dev_timeout_seconds=_static_dev_timeout,
                dev_budget_usd=_static_dev_budget,
                audit=_audit,
            )
        state.adaptive_dev_max = _limits.dev_max
        state.adaptive_review_max = _limits.review_max
        state.adaptive_dev_timeout_seconds = _limits.dev_timeout_seconds
        state.adaptive_dev_budget_usd = _limits.dev_budget_usd
        state.adaptive_limits_audit = _limits.audit
        _log_verbose(
            "  adaptive limits: "
            f"dev_max={_limits.dev_max} review_max={_limits.review_max} "
            f"dev_timeout={_limits.dev_timeout_seconds}s "
            f"dev_budget=${_limits.dev_budget_usd:.4f} "
            f"({_limits.audit.get('rationale', '')})"
        )

    # Initialise the budget's per-cycle limit from the adaptive value.  The
    # budget tracks both the per-cycle count (cycle_count, replaces
    # _dev_calls_this_cycle) and the cumulative count across all review cycles
    # (total_count).  Mutations go exclusively through budget.consume() and
    # budget.reset_cycle() so that every consumption is recorded in
    # budget.consumption_log.
    state.budget.max_iterations = state.adaptive_dev_max

    while True:
        if stop_event is not None and stop_event.is_set():
            raise StoryCancelled()
        if not _skip_dev:
            # ── DEV ───────────────────────────────────────────────
            state.phase = Phase.DEV
            if state_update_fn is not None:
                state_update_fn(
                    {
                        "phase": "DEV",
                        "iteration": state.dev_iteration,
                        "cost_usd": state.total_cost,
                        "complexity": state.preflight_complexity,
                        "current_model": config.dev_profile.model,
                        "detail": {
                            "review_cycle": state.review_cycle,
                            "dev_iteration": state.dev_iteration,
                        },
                    }
                )
            state.budget.consume(review_cycle=state.review_cycle)
            state.dev_trace_count += 1
            escalation = _run_dev_phase(
                state,
                config,
                task,
                story_content,
                workspace_path,
                branch_name,
                notify=notify,
                logger=logger,
                stop_event=stop_event,
            )
            if escalation is not None:
                return escalation

            if state.dev_escalated and state_update_fn is not None:
                state_update_fn(
                    {
                        "phase": "DEV",
                        "iteration": state.dev_iteration,
                        "cost_usd": state.total_cost,
                        "complexity": state.preflight_complexity,
                        "current_model": config.dev_profile.model,
                        "detail": {
                            "review_cycle": state.review_cycle,
                            "dev_iteration": state.dev_iteration,
                        },
                    }
                )

            # ── Startup failure guard ──────────────────────────────
            if state.dev_results and state.dev_results[-1].startup_failure:
                _last = state.dev_results[-1]
                _snippet = _last.output[:200] if _last.output else "(no output)"
                state.phase = Phase.ESCALATE
                state.error = f"DEV aborted: agent launcher startup failed: {_snippet}"
                _log(f"✗ ESCALATE   {state.error}")
                if logger:
                    logger._safe_emit("escalate", reason=state.error, phase="DEV")
                return CoordinatorResult(
                    success=False,
                    phase=Phase.ESCALATE,
                    state=state,
                    message=state.error,
                )

            if state.retry_reason == RetryReason.MAX_ITERATIONS_NO_SUBMIT:
                continue

            # ── Scrub forge-artifact commits from branch history ──
            _scrub_forge_history(workspace_path, branch_name, config.workspace.base_branch)

            if stop_event is not None and stop_event.is_set():
                raise StoryCancelled()

            # ── VALIDATE ──────────────────────────────────────────
            _val_outcome, _val_result = _run_validate_phase(
                state,
                config,
                task,
                workspace_path,
                notify=notify,
                logger=logger,
                state_update_fn=state_update_fn,
            )
            if _val_outcome == _ValidateOutcome.ESCALATE:
                return _val_result  # type: ignore[return-value]
            if _val_outcome == _ValidateOutcome.ALREADY_COMPLETE:
                # Dev cycle determined no work was needed and the handoff
                # documents this with verifiable cited commits. Short-circuit
                # to DONE (skip REVIEW and merge) — the cited commits are
                # already on the base branch.
                return _val_result  # type: ignore[return-value]
            if _val_outcome == _ValidateOutcome.REVIEW_CONVENTION_BLOCK:
                state.review_cycle += 1
                state.review_results.append(_build_convention_blocking_review(state))
                _review_cap = state.adaptive_review_max or config.retry.max_review_cycles
                if state.review_cycle >= _review_cap:
                    state.phase = Phase.ESCALATE
                    state.error = (
                        f"Convention violations persisted after {state.review_cycle} cycles. "
                        f"Max cycles ({_review_cap}) exhausted."
                    )
                    _log(f"✗ ESCALATE   {state.error}")
                    if logger:
                        logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                        logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
                    return CoordinatorResult(
                        success=False,
                        phase=Phase.ESCALATE,
                        state=state,
                        message=state.error,
                    )
                continue
            elif _val_outcome == _ValidateOutcome.RETRY_DEV:
                if (
                    state.dev_results
                    and state.dev_results[-1].exit_code == -9
                    and state.dev_session_id
                    and state.retry_reason == RetryReason.GATE_FAIL
                ):
                    gate_result = "FAIL"
                    if state.gate_decisions:
                        gate_result = state.gate_decisions[-1]
                    elif state.human_feedback:
                        prefix = "Gate validation failed: "
                        if state.human_feedback.startswith(prefix):
                            gate_result = f"FAIL - {state.human_feedback.removeprefix(prefix)}"
                    if config.retry.auto_model_escalation and not state.timeout_escalation_used:
                        _esc = _perform_dev_model_escalation(config)
                        if _esc is not None:
                            # Atomically claim the sprint-level escalation slot
                            # BEFORE mutating config/state so parallel sprint
                            # workers cannot both escalate. O_CREAT|O_EXCL is the
                            # claim primitive: FileExistsError ⇒ peer won race.
                            _claimed = True
                            if state.sprint_name:
                                import os  # noqa: PLC0415

                                _esc_flag = (
                                    config.project_root
                                    / ".forge"
                                    / "sprints"
                                    / state.sprint_name
                                    / "timeout_escalation_used"
                                )
                                _esc_flag.parent.mkdir(parents=True, exist_ok=True)
                                try:
                                    _fd = os.open(
                                        str(_esc_flag),
                                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                                        0o644,
                                    )
                                    os.close(_fd)
                                except FileExistsError:
                                    _claimed = False
                            if not _claimed:
                                # Peer worker already escalated — record the gate
                                # and continue with timeout resume on current model.
                                state.timeout_escalation_used = True
                            else:
                                from .util import resolve_timeout_with_active  # noqa: PLC0415

                                _old_timeout_model, _new_timeout_model, config = _esc
                                _orig_timeout = state.adaptive_dev_timeout_seconds
                                _new_timeout, _ = resolve_timeout_with_active(
                                    config.dev_profile.timeout_seconds,
                                    config.dev_profile.timeout_medium_seconds,
                                    config.dev_profile.timeout_large_seconds,
                                    state.preflight_complexity,
                                    state.preflight_complexity_score,
                                )
                                state.adaptive_dev_timeout_seconds = _new_timeout
                                state.timeout_escalation_used = True
                                state.timeout_escalation_audit = {
                                    "original_model": _old_timeout_model,
                                    "new_model": _new_timeout_model,
                                    "original_timeout_seconds": _orig_timeout,
                                    "new_timeout_seconds": _new_timeout,
                                    "reason": "timeout",
                                }
                                _log(
                                    f"  Timeout escalation:"
                                    f" {_old_timeout_model} → {_new_timeout_model}"
                                    f" (timeout {_orig_timeout}s → {_new_timeout}s)"
                                )
                    _set_timeout_resume(state, gate_result)
                continue

        _skip_dev = False  # all subsequent iterations start at DEV

        # ── stop_phase gate ───────────────────────────────────
        if stop_phase is not None and stop_phase.value <= Phase.VALIDATE.value:
            state.phase = Phase.VALIDATE
            if logger:
                logger._safe_emit("phase_end", phase="VALIDATE", outcome="pass")
            return CoordinatorResult(
                success=True,
                phase=Phase.VALIDATE,
                state=state,
                message=f"Stopped at --until {stop_phase.name.lower()}",
            )

        if logger:
            logger._safe_emit("phase_end", phase="VALIDATE", outcome="pass")

        if stop_event is not None and stop_event.is_set():
            raise StoryCancelled()

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
            state_update_fn=state_update_fn,
            stop_event=stop_event,
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
        # RETRY_DEV — reset per-cycle budget counter and loop back.
        # Exception: P2 cleanup iterations after APPROVE keep accumulating
        # against the existing cycle budget so cleanup cannot exceed the
        # configured dev iteration pool.
        if not state.p2_cleanup_active:
            state.budget.reset_cycle()


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
    defer_landing: bool = False,
    stop_event: "threading.Event | None" = None,
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
    state.story_content = story_content
    _sprint_name = sprint_name  # passed to _make_story_log_dir for sprint nesting
    state.sprint_name = sprint_name

    # Pre-populate timeout_escalation_used from sprint-level flag so only one escalation
    # fires across all stories in the same sprint, not just within a single story run.
    if sprint_name:
        _sprint_esc_flag = (
            config.project_root / ".forge" / "sprints" / sprint_name / "timeout_escalation_used"
        )
        if _sprint_esc_flag.exists():
            state.timeout_escalation_used = True

    # ── Structured logger ──────────────────────────────────────────
    _run_id = run_id or _generate_run_id()
    state.run_id = _run_id
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
        if config.models is not None:
            models_str = ", ".join(config.models)
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
            try:
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
                    stop_event=stop_event,
                )
            except StoryCancelled:
                return _cancelled_result(task, state)
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

            cache_valid, cache_validation = validate_preflight_cache(
                cached_preflight_state,
                config=config,
                workspace_path=workspace_path,
            )
            state.preflight_cache_validation = cache_validation
            if cache_valid:
                apply_cached_preflight_state(state, cached_preflight_state)
                config = _apply_preflight_config(config, state, task_slug=task.slug)
                from .preflight_flow import _handle_preflight_verdict  # noqa: PLC0415

                config, _pf_result, _pf_already_done_loop = _handle_preflight_verdict(
                    verdict=state.preflight_verdict,
                    reason=state.preflight_reason,
                    state=state,
                    config=config,
                    task=task,
                    branch_name=branch_name,
                    notify=notify,
                    logger=logger,
                    task_start=_task_start,
                )
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
            _total_elapsed = time.monotonic() - _task_start
            _fire_post_run_hook(config, state, task, _pf_result, _run_id, _total_elapsed, logger)
            return _pf_result
        if _pf_already_done_loop:
            # ALREADY_DONE override: commits on branch without prior APPROVE → resume REVIEW
            try:
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
                    stop_event=stop_event,
                )
            except StoryCancelled:
                return _cancelled_result(task, state)
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
        try:
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
                stop_event=stop_event,
            )
        except StoryCancelled:
            return _cancelled_result(task, state)

        # ── Landing (single-story path) ───────────────────────────────
        # _finalize_approve defers all git operations and sets landing_status
        # = "pending_integration".  We perform the actual merge here, without
        # a lock, because single-story runs have exactly one worker.
        # When defer_landing=True (sprint worker path), skip: the scheduler
        # thread will call _attempt_integration under integration_lock.
        if result.success and result.landing_status == "pending_integration" and not defer_landing:
            from .completion import land_story, mark_merge_failed  # noqa: PLC0415

            _effective_on_approve = "merge" if auto_merge else config.workspace.on_approve
            _parsed_review = state.review_results[-1] if state.review_results else None
            _merge_info, _landing_status = land_story(
                config,
                task,
                branch_name,
                workspace_path,
                _parsed_review,
                state,
                _effective_on_approve,
                logger=logger,
                run_id=_run_id,
            )
            result.merge = _merge_info
            result.landing_status = _landing_status
            if _merge_info.get("merged"):
                result.message += " Merged."
            elif _merge_info.get("merge_queued"):
                result.message += f" PR queued: {_merge_info.get('pr_url', '')}"
            elif _landing_status == "failed":
                mark_merge_failed(state, result, _merge_info.get("error"), branch_name)

        _total_elapsed = time.monotonic() - _task_start
        _fire_post_run_hook(config, state, task, result, _run_id, _total_elapsed, logger)
        logger._safe_emit(
            "run_end",
            outcome="done" if result.success else "escalate",
            total_cost_usd=round(state.total_cost, 6),
            total_duration_s=round(_total_elapsed, 2),
        )

        _record_run_memory(config, task, state, result)
        return result


# ── Post-run persistence (shared by run_task and resume paths) ────────


def _record_run_memory(
    config: "ForgeConfig",
    task: "TaskStory",
    state: "CoordinatorState",
    result: "CoordinatorResult",
) -> None:
    """Persist end-of-run memory: escalation history + model profiles.

    Called from both run_task() and _run_resume_coordinator() so a run started
    via 'forge resume' or a sprint worker contributes the same telemetry as a
    fresh run_task invocation. Changes to what we persist at run completion
    belong in this one function — not duplicated at each entry point.
    """
    if not state.preflight_complexity:
        _log_verbose(
            f"[model_profiles] skipping update: preflight_complexity unset for story={task.slug}"
        )
        return

    # ── Adaptive escalation memory ─────────────────────────────────────
    if config.assignment.escalation_memory and config.agents:
        from theforge.assignment import (  # noqa: I001, PLC0415
            EscalationRecord as _EscRec,
            append_escalation_record as _append_esc,
        )

        from theforge.model_profiles import canonical_id_from_identity  # noqa: PLC0415

        _esc_path = config.project_root / ".forge" / "assignment_history.yaml"
        _esc_outcome = "DONE" if result.success else "ESCALATE"
        _esc_canonical = canonical_id_from_identity(
            actual_model=getattr(config.dev_profile, "model", None),
            provider=getattr(config.dev_profile, "provider", None),
            cli=getattr(config.dev_profile, "cli", None),
        )
        _esc_record = _EscRec(
            story=task.slug,
            complexity=state.preflight_complexity.upper()
            if state.preflight_complexity in ("small", "medium", "large")
            else state.preflight_complexity,
            dev_model=_esc_canonical or config.dev_profile.name,
            outcome=_esc_outcome,
            reason=state.escalation_note or "",
            timestamp=datetime.datetime.utcnow().isoformat() + "Z",
        )
        _append_esc(_esc_path, _esc_record)
        _log_verbose(
            f"[adaptive] Wrote escalation record: story={task.slug} "
            f"complexity={_esc_record.complexity} outcome={_esc_outcome}"
        )

    # ── Model capability profiles (independent of escalation_memory) ───
    try:
        from .model_profiles_bridge import update_profiles_from_run  # noqa: PLC0415

        _profiles_path = config.project_root / ".forge" / "model_profiles.yaml"
        _history_path = config.project_root / ".forge" / "assignment_history.yaml"
        update_profiles_from_run(
            profiles_path=_profiles_path,
            history_path=_history_path if _history_path.exists() else None,
            config=config,
            state=state,
            success=result.success,
        )
    except Exception as exc:  # noqa: BLE001
        _log_verbose(f"[model_profiles] update failed: {exc}")


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
    cached_preflight_state: CoordinatorState | None = None,
    defer_landing: bool = False,
    stop_event: "threading.Event | None" = None,
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
    state.sprint_name = sprint_name

    # Mirror the sprint-sticky timeout-escalation guard from run_task so resumed
    # stories see the flag written by an earlier story in the same sprint.
    if sprint_name:
        _sprint_esc_flag = (
            config.project_root / ".forge" / "sprints" / sprint_name / "timeout_escalation_used"
        )
        if _sprint_esc_flag.exists():
            state.timeout_escalation_used = True

    if cached_preflight_state is not None:
        from .preflight import _apply_preflight_config  # noqa: PLC0415

        cache_valid, cache_validation = validate_preflight_cache(
            cached_preflight_state,
            config=config,
            workspace_path=workspace_path,
        )
        state.preflight_cache_validation = cache_validation
        if cache_valid:
            apply_cached_preflight_state(state, cached_preflight_state)
            config = _apply_preflight_config(config, state, task_slug=task.slug)
        else:
            # Cache invalidated mid-sprint (e.g., base branch advanced after a
            # prior batch merged): re-run preflight against the current base so
            # downstream phases get fresh complexity/work_type/likely_files
            # rather than the default SKIPPED state. Without this, dev runs
            # blind and typically loops without producing changes.
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
                stop_phase=None,
            )
            if _pf_result is not None:
                return _pf_result
            if _pf_already_done_loop:
                skip_dev_first_iter = True

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
        try:
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
                stop_event=stop_event,
            )
        except StoryCancelled:
            return _cancelled_result(task, state)

        # ── Landing (single-story resume path) ───────────────────────
        # Skip when defer_landing=True (sprint worker): scheduler handles it.
        if result.success and result.landing_status == "pending_integration" and not defer_landing:
            from .completion import land_story, mark_merge_failed  # noqa: PLC0415

            _effective_on_approve = "merge" if auto_merge else config.workspace.on_approve
            _parsed_review = state.review_results[-1] if state.review_results else None
            _rws = state.workspace_path or workspace_path
            _merge_info, _landing_status = land_story(
                config,
                task,
                branch_name,
                _rws,
                _parsed_review,
                state,
                _effective_on_approve,
                logger=logger,
                run_id=logger._run_id if logger else "",
            )
            result.merge = _merge_info
            result.landing_status = _landing_status
            if _merge_info.get("merged"):
                result.message += " Merged."
            elif _merge_info.get("merge_queued"):
                result.message += f" PR queued: {_merge_info.get('pr_url', '')}"
            elif _landing_status == "failed":
                mark_merge_failed(state, result, _merge_info.get("error"), branch_name)

        _total_elapsed = time.monotonic() - _task_start
        _fire_post_run_hook(config, state, task, result, logger._run_id, _total_elapsed, logger)
        logger._safe_emit(
            "run_end",
            outcome="done" if result.success else "escalate",
            total_cost_usd=round(state.total_cost, 6),
            total_duration_s=round(_total_elapsed, 2),
        )
        _record_run_memory(config, task, state, result)
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
    cached_preflight_state: CoordinatorState | None = None,
    defer_landing: bool = False,
    stop_event: "threading.Event | None" = None,
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
        defer_landing: When True, skip the landing step and leave
            landing_status="pending_integration" for the caller (sprint scheduler).
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
        cached_preflight_state=cached_preflight_state,
        defer_landing=defer_landing,
        stop_event=stop_event,
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
    cached_preflight_state: CoordinatorState | None = None,
    defer_landing: bool = False,
    stop_event: "threading.Event | None" = None,
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
        defer_landing: When True, skip the landing step and leave
            landing_status="pending_integration" for the caller (sprint scheduler).
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
        cached_preflight_state=cached_preflight_state,
        defer_landing=defer_landing,
        stop_event=stop_event,
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
    state.run_id = _run_id
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
    state.story_content = story_content

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
