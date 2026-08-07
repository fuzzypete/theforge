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
from theforge.task import (
    TaskStory,
    load_story,
    parse_plan_output,
)

from . import live_state as _live_state
from . import story_budget as _story_budget
from .agent_failure import is_infrastructure_abort
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
from .resume_persistence import save_resume_record
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
    _round_cost,
    live_complexity_fields,
)
from .workspace import (
    _base_branch_lands_locally,
    _create_workspace,
    landing_precondition_error,
    pull_base_branch,
)
from .workspace_scrub import _scrub_forge_history
from .worktree_drift import is_drift_classification

# ── Lazy runner symbols ───────────────────────────────────────────────
# Populated by _ensure_runners() at entry points.
# Preflight: patch theforge.coordinator.preflight_flow.run_agent
# DEV:       patch theforge.coordinator.dev_phase.run_agent
run_agent = None
run_agent_pool = None
log_agent_result = None
LogLevel = None

# ── Lazy runner import ────────────────────────────────────────────────


def _fresh_run_state() -> CoordinatorState:
    """Return a fresh per-run state container.

    In-run routing escalations are scoped to this object, so constructing a new
    state for the next story is the return path that clears them.
    """
    return CoordinatorState()


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


# ── Log-tee / SIGTERM context manager ────────────────────────────────


@contextmanager
def _run_log_context(
    config: ForgeConfig,
    logger: StructuredLogger,
    task: TaskStory,
    state: CoordinatorState,
    task_start: float,
) -> Generator[None, None, None]:
    """Set up per-run log tee, live-state registration, and SIGTERM handler.

    The live-state registration is what lets the sprint scheduler write a real
    audit for a story whose worker never returns (worker timeout): without it
    the accumulated dev/gate history is unreachable from outside this thread
    (#2013). It is bound to the same scope as the log tee because they have the
    same lifetime — everything the run records is in flight between them.
    """
    _live_state.register_live_state(task.slug, state)
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
        _live_state.release_live_state(task.slug, state)
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
from .run_setup import (  # noqa: E402,I001
    REENTRY_MODE_PIPELINE_RESUME,
    _rebase_onto_main,
    _setup_resume_entry,
)
from .validate_phase import (  # noqa: E402
    _run_validate_phase,
    _ValidateOutcome,
    record_validate_block,
)


def _run_end_outcome(result: CoordinatorResult) -> str:
    """Outcome label for the ``run_end`` structured-log event.

    A run that ended because no agent judgment could be obtained is not an
    escalation: nothing was learned about the story (#1951). Emitting the same
    label for both would make the two indistinguishable in the event stream —
    the exact conflation this distinction exists to prevent.
    """
    if result.success:
        return "done"
    if result.infrastructure_failure or is_infrastructure_abort(result.state):
        return "infrastructure_abort"
    return "escalate"


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


def _maybe_recover_failed_challenger(
    state: CoordinatorState,
    config: ForgeConfig,
    log_fn: "Callable[[str], None]",
    logger: StructuredLogger | None,
) -> ForgeConfig | None:
    """Recover a failed exploration challenger by retrying through the winner.

    Returns a config whose dev profile is swapped back to the current winner
    when a challenger attempt just failed, else ``None`` (no recovery — the
    caller returns the escalation as-is). Fires at most once per story; the
    failure is recorded as an *exploration* failure in the routing_decision
    block so it never counts as the story's final routing outcome (ADR-0006
    clause 8 "recoverable").
    """
    import dataclasses  # noqa: PLC0415

    from theforge import exploration as _exp  # noqa: PLC0415

    challenger = state.exploration_challenger
    winner_profile = state.exploration_winner_dev_profile
    if not challenger or state.exploration_recovered or winner_profile is None:
        return None

    outcome = _exp.ExplorationOutcome(
        mode=_exp.MODE_CHALLENGER,
        routing_key=challenger.get("routing_key", ""),
        pool=list(challenger.get("pool") or []),
        selected=challenger.get("challenger"),
        winner=challenger.get("winner"),
        reason="challenger_attempt_failed",
    )
    recovery = _exp.recover_from_failed_challenger(outcome)
    if recovery is None:  # pragma: no cover - guarded above
        return None

    # Record the failure in the audit substrate view so the exploration outcome
    # stays reconstructable (the challenger failed; the story ran on the winner).
    if isinstance(state.routing_decision, dict):
        _dev_block = state.routing_decision.get("dev")
        if isinstance(_dev_block, dict) and isinstance(_dev_block.get("exploration"), dict):
            _dev_block["exploration"]["challenger_failed"] = True
            _dev_block["exploration"]["recovery"] = recovery.failure_record

    state.exploration_recovered = True
    # Retry through the winner: fresh dev attempt, clear the challenger's failed
    # transport/escalation state so the winner starts clean.
    state.retry_reason = None
    state.dev_escalated = False
    state.pending_dev_transport_retry_count = 0
    state.pending_dev_transport_retry_events = []
    log_fn(
        f"  Exploration recovery: challenger {challenger.get('challenger')} failed → "
        f"retrying through winner {getattr(winner_profile, 'model', challenger.get('winner'))}"
    )
    if logger:
        logger._safe_emit(
            "exploration_recovery",
            challenger=challenger.get("challenger"),
            winner=challenger.get("winner"),
            routing_key=challenger.get("routing_key"),
        )
    return dataclasses.replace(config, dev_profile=winner_profile)


def _refuse_dev_without_nonreview_funds(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    log_fn: "Callable[[str], None]",
    logger: StructuredLogger | None,
) -> CoordinatorResult | None:
    """Refuse a dev attempt the non-reserved allocation can no longer fund (#2258).

    Money the seating reconciliation committed to verification is not spendable
    by dev. Once everything the allocation left for non-review work is gone,
    another dev attempt would draw down the reserved review cycles and leave the
    work unreviewed — precisely the failure the reservation exists to prevent.
    Returns ``None`` when the attempt is funded, when no reservation was seated,
    or when spend is unmeasured; otherwise records the refusal on ``state`` and
    returns the escalation result the caller must return.
    """
    exhausted = _story_budget.nonreview_funding_exhausted(
        state.review_funding_reservation,
        state.story_allocation,
        observed_usd=state.total_cost_measured,
        review_observed_usd=state.total_review_cost_measured,
        participants=[config.dev_profile.name],
    )
    if exhausted is None:
        return None
    state.allocation_exhausted = exhausted
    state.error = _story_budget.format_shortfall(exhausted, story=task.slug)
    state.error_type = "allocation_exhausted"
    state.phase = Phase.ESCALATE
    log_fn(f"  ⚠ {state.error}")
    if logger:
        logger._safe_emit(
            "allocation_exhausted",
            phase="DEV",
            **{
                key: exhausted.get(key)
                for key in (
                    "allocation_usd",
                    "nonreview_allocation_usd",
                    "reserved_review_usd",
                    "reserved_review_cycles",
                    "observed_usd",
                )
            },
        )
    return CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message=state.error,
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
        or state.adaptive_dev_cost_estimate_usd == 0.0
    ):
        _static_dev_timeout, _timeout_override_active = resolve_timeout_with_active(
            config.dev_profile.timeout_seconds,
            config.dev_profile.timeout_medium_seconds,
            config.dev_profile.timeout_large_seconds,
            state.preflight_complexity,
            state.preflight_complexity_score,
        )
        _static_dev_max = config.dev_profile.max_iterations or config.retry.max_dev_iterations
        _static_dev_cost_estimate = config.dev_profile.budget_usd
        _explicit_dev_override = "dev" in getattr(state, "_explicit_roles", set())
        # Adaptive iterations now reads the SQLite audit substrate; we pass
        # project_root and let the helper resolve substrate access internally.
        _history_path = config.project_root
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
            base_cost_estimate_usd=_static_dev_cost_estimate,
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
                dev_cost_estimate_usd=_static_dev_cost_estimate,
                audit=_audit,
            )
        # ── Seat permissions the allocation can actually fund (#2238) ─────────
        # The allocation and the permitted review cycles are derived
        # independently from the same complexity signal, so nothing stops a
        # story being granted more review cycles than its allocation can pay
        # for. Reconcile them here — the last point before dev spends, where
        # reducing scope is still free — rather than discovering the shortfall
        # at review dispatch, where the only options left are escalate or
        # accept unverified work.
        #
        # Price one review cycle from observed prior review-cycle spend plus
        # explicit headroom, not from the reviewers' execution ceilings. The
        # exact seated figure is persisted on state so dispatch cannot silently
        # re-price the same granted cycle later in the run.
        _reviewer_names = [profile.name for profile in config.review_pool]
        _review_cycle_planning = _story_budget.derive_review_cycle_planning_price(
            config.project_root,
            configured_ceiling_usd=sum(float(p.budget_usd) for p in config.review_pool),
            composition=_reviewer_names,
        ).as_dict()
        _reconciliation = _story_budget.reconcile_review_cycles(
            state.story_allocation,
            dev_cost_estimate_usd=_limits.dev_cost_estimate_usd,
            dev_cost_estimate_basis=_limits.audit.get("dev_cost_estimate_basis"),
            review_cycle_cost_usd=float(_review_cycle_planning["planned_cost_usd"]),
            review_cycle_planning=_review_cycle_planning,
            requested_review_max=_limits.review_max,
            spent_so_far_usd=state.total_cost_measured,
        )
        # ── Hold the seated reservation across the phases (#2258) ────────────
        # The reconciliation above is a projection over an estimate. Nothing
        # held its conclusion, so a dev phase that overran its estimate spent
        # the money review had been seated with and review was refused anyway —
        # in exactly the case verification was most wanted. Record the reserved
        # portion of the allocation on state: REVIEW funds from it, and DEV is
        # refused further attempts once the rest of the allocation is gone, so
        # the decision binds when it is exceeded and not only when it is made.
        _reservation = {
            "allocation_usd": _reconciliation.get("allocation_usd"),
            "reserved_review_usd": _reconciliation.get("reserved_review_usd") or 0.0,
            "reserved_review_cycles": _reconciliation.get("reserved_review_cycles") or 0,
            "review_cycle_cost_usd": _reconciliation.get("review_cycle_cost_usd"),
            "action": _reconciliation.get("action"),
        }
        if _reservation["allocation_usd"] is not None and _reservation["reserved_review_usd"]:
            _reservation["nonreview_allocation_usd"] = round(
                float(_reservation["allocation_usd"]) - float(_reservation["reserved_review_usd"]),
                4,
            )
        _audit = dict(_limits.audit)
        _audit["review_cycle_planning"] = _review_cycle_planning
        _audit["review_cycle_reconciliation"] = _reconciliation
        _audit["review_funding_reservation"] = _reservation
        if _reconciliation["action"] in (
            _story_budget.RECONCILE_REDUCED,
            _story_budget.RECONCILE_UNFUNDABLE,
            _story_budget.RECONCILE_NONCOMPARABLE_DEV_ESTIMATE,
        ):
            _audit["rationale"] = (
                f"{_audit.get('rationale', '')} "
                f"{_story_budget.format_reconciliation(_reconciliation)}"
            ).strip()
        _limits = type(_limits)(
            dev_max=_limits.dev_max,
            review_max=_reconciliation["reconciled_review_max"],
            dev_timeout_seconds=_limits.dev_timeout_seconds,
            dev_cost_estimate_usd=_limits.dev_cost_estimate_usd,
            audit=_audit,
        )

        state.adaptive_dev_max = _limits.dev_max
        state.adaptive_review_max = _limits.review_max
        state.adaptive_dev_timeout_seconds = _limits.dev_timeout_seconds
        state.adaptive_dev_cost_estimate_usd = _limits.dev_cost_estimate_usd
        state.adaptive_review_cycle_planning = _review_cycle_planning
        state.review_funding_reservation = _reservation
        state.adaptive_limits_audit = _limits.audit

        if _reconciliation["action"] == _story_budget.RECONCILE_UNFUNDABLE:
            # Not one review cycle fits. Spending on work that cannot then be
            # checked is the failure this reconciliation exists to prevent, so
            # the run stops here — before DEV — and says why.
            _shortfall = _story_budget.seating_shortfall(
                state.story_allocation,
                _reconciliation,
                participants=_reviewer_names,
            )
            if _shortfall is not None:
                state.allocation_exhausted = _shortfall
                state.error = _story_budget.format_shortfall(_shortfall, story=task.slug)
                state.error_type = "allocation_exhausted"
                state.phase = Phase.ESCALATE
                _log(f"  ⚠ {state.error}")
                if logger:
                    logger._safe_emit(
                        "allocation_exhausted",
                        phase="SEATING",
                        **{
                            k: _reconciliation.get(k)
                            for k in (
                                "allocation_usd",
                                "dev_cost_estimate_usd",
                                "review_cycle_cost_usd",
                                "requested_review_max",
                                "shortfall_usd",
                            )
                        },
                    )
                return CoordinatorResult(
                    success=False,
                    phase=Phase.ESCALATE,
                    state=state,
                    message=state.error,
                )

        _log_verbose(
            "  adaptive limits: "
            f"dev_max={_limits.dev_max} review_max={_limits.review_max} "
            f"dev_timeout={_limits.dev_timeout_seconds}s "
            f"dev_cost_estimate=${_limits.dev_cost_estimate_usd:.4f} "
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
                        "cost_usd": state.total_cost_measured,
                        **live_complexity_fields(
                            state.preflight_complexity, state.preflight_complexity_score
                        ),
                        "current_model": config.dev_profile.model,
                        "detail": {
                            "review_cycle": state.review_cycle,
                            "review_max_cycles": (
                                state.adaptive_review_max or config.retry.max_review_cycles
                            ),
                            "dev_iteration": state.dev_iteration,
                            "dev_max_iterations": (
                                state.adaptive_dev_max or config.retry.max_dev_iterations
                            ),
                        },
                    }
                )
            # ── Non-review funds exhausted (#2258) ────────────────────────
            # Binds on RETRY attempts only: the first attempt is the one seating
            # already ruled on, and refusing it here would refuse a story before
            # it did any work at all.
            if state.dev_trace_count > 0:
                _dev_refusal = _refuse_dev_without_nonreview_funds(
                    state, config, task, _log, logger
                )
                if _dev_refusal is not None:
                    return _dev_refusal
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
                # ── Failed-challenger recovery (#325, ADR-0006 clause 8) ──────
                # If this dev slot ran an exploration challenger and it failed,
                # the failure must NOT be the story's final routing outcome:
                # record it as an exploration failure, swap to the current
                # winner, and retry — once.
                _recovered_config = _maybe_recover_failed_challenger(state, config, _log, logger)
                if _recovered_config is not None:
                    config = _recovered_config
                    continue
                return escalation

            if state.dev_escalated and state_update_fn is not None:
                state_update_fn(
                    {
                        "phase": "DEV",
                        "iteration": state.dev_iteration,
                        "cost_usd": state.total_cost_measured,
                        **live_complexity_fields(
                            state.preflight_complexity, state.preflight_complexity_score
                        ),
                        "current_model": config.dev_profile.model,
                        "detail": {
                            "review_cycle": state.review_cycle,
                            "review_max_cycles": (
                                state.adaptive_review_max or config.retry.max_review_cycles
                            ),
                            "dev_iteration": state.dev_iteration,
                            "dev_max_iterations": (
                                state.adaptive_dev_max or config.retry.max_dev_iterations
                            ),
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

            # A dev-phase timeout retry (iterations remaining) re-enters DEV
            # directly rather than routing through VALIDATE — the timed-out
            # iteration may have produced no commits, and validating an
            # unchanged worktree is pointless. Mirror the MAX_ITERATIONS retry.
            if state.retry_reason in (
                RetryReason.MAX_ITERATIONS_NO_SUBMIT,
                RetryReason.TIMEOUT_RESUME,
            ):
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
            if _val_outcome == _ValidateOutcome.RETRY_DEV_NEW_CYCLE:
                # The dev iteration pool is spent but the finding is still
                # fixable, so it is charged to a review cycle and handed back to
                # the dev. The finding is recorded in state.validate_blocks —
                # the reviewer record stays reviewer-only (#1981).
                _block_label = (
                    "Convention violations"
                    if state.retry_reason == RetryReason.CONVENTION_VIOLATIONS
                    else "Gate failures"
                )
                record_validate_block(
                    state, outcome="opened_review_cycle", reason="review_cycle_bought"
                )
                state.review_cycle += 1
                state.validate_opened_review_cycles += 1
                _review_cap = state.adaptive_review_max or config.retry.max_review_cycles
                if state.review_cycle >= _review_cap:
                    # VALIDATE routes to ESCALATE itself when no cycle remains;
                    # this stays as the loop bound so no phase outcome can spin
                    # the state machine forever.
                    state.phase = Phase.ESCALATE
                    state.error = (
                        f"{_block_label} persisted after {state.review_cycle} cycles. "
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
                # Opening a review cycle refills the dev iteration pool, exactly
                # as review_phase does when it sends findings back to the dev.
                state.budget.reset_cycle()
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
                                # Durable copy: a timeout escalation is exactly
                                # the kind of event whose run is least likely to
                                # reach a normal finalization, so the record must
                                # not depend on this process surviving (#2155).
                                save_resume_record(
                                    config.project_root,
                                    state,
                                    slug=task.slug,
                                    story_content=story_content,
                                    run_id=state.run_id,
                                )
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
    base_lands_locally: bool | None = None,
    lands_in_project_root: bool | None = None,
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
        base_lands_locally: Override for the base-branch publication guard —
            whether *any* story in the surrounding run merges into the local
            base checkout. Defaults to deriving it from this task's own
            auto_merge and config, which is right for a standalone run but not
            inside a sprint, where an earlier story may have merged locally
            while this one did not.
        lands_in_project_root: Override for the landing precondition — whether
            *this* story's approval merges into the project-root checkout.
            Distinct from base_lands_locally, which is a run-wide question about
            the base branch. The sprint scheduler supplies it because in
            parallel mode it forces a local merge on dependency parents after
            the story returns, which auto_merge and on_approve do not reveal.
    """
    _ensure_runners()
    state = _fresh_run_state()
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
        p2_policy=config.dev.p2_policy,
    )

    # ── Per-story log directory ───────────────────────────────────
    # Create early (before WORKSPACE) so the tee can write run-<id>.log from start.
    state.log_dir = _make_story_log_dir(config, task.slug, sprint_name=_sprint_name)

    # ── Per-run log tee + SIGTERM handler ────────────────────────────
    with _run_log_context(config, logger, task, state, _task_start):
        _log(f"  Dev P2 policy: {config.dev.p2_policy}")
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

        # Refuse before the workspace exists, not after review has been paid
        # for: a dirty project root blocks this story's landing, and in a
        # sprint this is the first point at which a root dirtied by the
        # operator mid-run can be observed while the spend is still avoidable.
        _landing_block = landing_precondition_error(
            config, auto_merge=auto_merge, lands_in_project_root=lands_in_project_root
        )
        if _landing_block is not None:
            state.phase = Phase.ESCALATE
            state.error = _landing_block
            _log(f"✗ ESCALATE   {_landing_block}")
            logger._safe_emit("phase_end", phase="WORKSPACE", outcome="escalate")
            logger._safe_emit("escalate", reason=_landing_block, phase="WORKSPACE")
            logger._safe_emit(
                "run_end", outcome="escalate", total_cost_usd=0.0, total_duration_s=0.0
            )
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=_landing_block,
            )

        workspace_path, branch_name, err = _create_workspace(
            config,
            task,
            no_pull=no_pull,
            lands_locally=(
                base_lands_locally
                if base_lands_locally is not None
                else _base_branch_lands_locally(config, auto_merge=auto_merge)
            ),
        )
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
                # A classified condition already says what happened and what to
                # do about it; wrapping it in "Workspace creation failed" would
                # re-frame it as a mechanism failure (#1993).
                message=(
                    err if is_drift_classification(err) else f"Workspace creation failed: {err}"
                ),
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
            # The one place a SKIPPED verdict is a real claim: the operator
            # deliberately bypassed the phase. Persist it so a later resume
            # reports the bypass rather than degrading it to "no record"
            # (#2155) — SKIPPED and absent must stay distinguishable.
            save_resume_record(
                config.project_root,
                state,
                slug=task.slug,
                story_content=story_content,
                run_id=_run_id,
            )
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
                outcome=_run_end_outcome(result),
                total_cost_usd=_round_cost(state.total_cost_measured),
                total_duration_s=round(_total_elapsed, 2),
            )
            return result

        # ── PREFLIGHT ──────────────────────────────────────────────────
        if cached_preflight_state is not None:
            from .preflight import (  # noqa: PLC0415
                _apply_preflight_config,
                persist_routing_decision,
            )

            cache_valid, cache_validation = validate_preflight_cache(
                cached_preflight_state,
                config=config,
                workspace_path=workspace_path,
                story_content=story_content,
            )
            state.preflight_cache_validation = cache_validation
            if cache_valid:
                apply_cached_preflight_state(state, cached_preflight_state)
                config = _apply_preflight_config(config, state, task_slug=task.slug)
                # Routing resolved from a cached verdict is still this run's
                # decision — persist it so a later resume can recover it (#2154).
                persist_routing_decision(
                    config,
                    state,
                    task_slug=task.slug,
                    story_content=story_content,
                    run_id=_run_id,
                )
                save_resume_record(
                    config.project_root,
                    state,
                    slug=task.slug,
                    story_content=story_content,
                    run_id=_run_id,
                )
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
                outcome=_run_end_outcome(result),
                total_cost_usd=_round_cost(state.total_cost_measured),
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

        # ── Post-plan dev-tier checkpoint apply (#1387) ───────────────
        # plan_flow re-evaluated ONLY the dev tier after a clean plan-review and
        # stored the (possibly demoted) decision on state. When the checkpoint
        # actually changed dev, swap config.dev_profile before DEV runs; every
        # other role (preflight, planner, plan_review, code_review) is untouched.
        _checkpoint_decision = getattr(state, "_adaptive_decision", None)
        _cp_block = (
            (state.routing_decision or {}).get("dev", {}).get("post_plan_checkpoint", {})
            if state.routing_decision
            else {}
        )
        if _checkpoint_decision is not None and _cp_block.get("fired"):
            import dataclasses  # noqa: PLC0415

            config = dataclasses.replace(config, dev_profile=_checkpoint_decision.dev)
            _log_verbose(
                f"  [adaptive] post-plan checkpoint applied: dev → "
                f"{_checkpoint_decision.dev.model} "
                f"({_cp_block.get('baseline_tier')} → {_cp_block.get('final_tier')})"
            )

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
                mark_merge_failed(
                    state,
                    result,
                    _merge_info.get("error"),
                    branch_name,
                    arming_failed=bool(_merge_info.get("arming_failed")),
                    inherited_dev_residue=bool(_merge_info.get("inherited_dev_residue")),
                )

        _total_elapsed = time.monotonic() - _task_start
        _fire_post_run_hook(config, state, task, result, _run_id, _total_elapsed, logger)
        logger._safe_emit(
            "run_end",
            outcome=_run_end_outcome(result),
            total_cost_usd=_round_cost(state.total_cost_measured),
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

    Not every terminal path reaches here. A run that ends inside PREFLIGHT or
    the plan flow returns its phase result directly from run_task (the
    ``_pf_result`` / ``_plan_result`` early returns above), so those outcomes —
    including their infrastructure aborts — persist nothing by *omission*,
    which predates #1951 and is unchanged by it. The infrastructure-abort guard
    below is therefore load-bearing for the paths that DO reach this function:
    the DEV→VALIDATE→REVIEW loop (where the #1951 defect was observed — a dead
    credential persisted as escalation evidence) and both resume entry points.
    Do not read the guard as the only thing keeping preflight/plan aborts out
    of memory, and do not delete it on the assumption that it is unreachable.
    """
    # ── No judgment obtained ⇒ nothing to learn (#1951) ────────────────
    # Durable memory must be sourced only from invocations that actually
    # produced model output. A run aborted because the substrate never answered
    # made no statement about the story, and persisting its outcome would write
    # a revoked credential into escalation history as evidence that this story
    # escalates — indistinguishable from a real escalation once written, and
    # biasing every later routing decision that reads it.
    if is_infrastructure_abort(state):
        _cause = state.infrastructure_failure or {}
        _log_verbose(
            "[adaptive] skipping memory persistence: no story judgment was obtained "
            f"for story={task.slug} (infrastructure abort: "
            f"phase={_cause.get('phase')} category={_cause.get('category')} "
            f"exit={_cause.get('exit_code')})"
        )
        return
    if not state.preflight_complexity:
        _log_verbose(
            f"[model_profiles] skipping update: preflight_complexity unset for story={task.slug}"
        )
        return

    # ── Adaptive escalation memory ─────────────────────────────────────
    # Escalation history is now derived from substrate audit records on read
    # (see coordinator.escalation_history). The audit record this run produces
    # carries every field the router needs (slug, preflight.complexity,
    # outcome.success, dev identity in cost.agents). Assignment history is a
    # derived view; inspect it via ``forge audits export-assignment-history``.
    # No separate write to the legacy .forge/assignment_history.yaml snapshot.
    if config.assignment.escalation_memory and config.agents:
        _log_verbose(
            f"[adaptive] escalation memory persisted via audit substrate: "
            f"story={task.slug} outcome={'DONE' if result.success else 'ESCALATE'}"
        )

    # ── Model capability profiles (independent of escalation_memory) ───
    try:
        from .model_profiles_bridge import update_profiles_from_run  # noqa: PLC0415

        _profiles_path = config.project_root / ".forge" / "model_profiles.yaml"
        update_profiles_from_run(
            profiles_path=_profiles_path,
            history_path=None,
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
    base_lands_locally: bool | None = None,
    lands_in_project_root: bool | None = None,
    reentry_mode: str = REENTRY_MODE_PIPELINE_RESUME,
) -> CoordinatorResult:
    """Shared body for run_from_review and run_from_dev.

    Both entry points reuse an existing worktree, differ only in which phase
    they start at and whether the first coordinator loop iteration skips DEV.
    """
    _ensure_runners()
    if not no_pull:
        # Blocking, unlike the informational behind-origin check this replaced:
        # the reused worktree is about to be rebased onto the base branch, so an
        # unpublished commit there would be absorbed into this story's diff.
        pull_base_branch(
            config,
            lands_locally=(
                base_lands_locally
                if base_lands_locally is not None
                else _base_branch_lands_locally(config, auto_merge=auto_merge)
            ),
        )
    setup = _setup_resume_entry(
        config,
        task,
        workspace_path,
        initial_phase=initial_phase,
        notify=notify,
        run_id=run_id,
        reentry_mode=reentry_mode,
    )
    if isinstance(setup, CoordinatorResult):
        return setup
    state, logger, branch_name, story_content, _task_start = setup

    # Same landing precondition run_task enforces, at the resume entry point:
    # a resumed story re-runs dev and/or review, so a dirty project root has to
    # refuse here rather than after that spend (#2048).
    _landing_block = landing_precondition_error(
        config, auto_merge=auto_merge, lands_in_project_root=lands_in_project_root
    )
    if _landing_block is not None:
        state.phase = Phase.ESCALATE
        state.error = _landing_block
        _log(f"✗ ESCALATE   {_landing_block}")
        logger._safe_emit("escalate", reason=_landing_block, phase="INIT")
        logger._safe_emit("run_end", outcome="escalate", total_cost_usd=0.0, total_duration_s=0.0)
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=_landing_block,
        )

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
        from .preflight import (  # noqa: PLC0415
            _apply_preflight_config,
            persist_routing_decision,
        )

        cache_valid, cache_validation = validate_preflight_cache(
            cached_preflight_state,
            config=config,
            workspace_path=workspace_path,
            story_content=story_content,
        )
        state.preflight_cache_validation = cache_validation
        if cache_valid:
            apply_cached_preflight_state(state, cached_preflight_state)
            config = _apply_preflight_config(config, state, task_slug=task.slug)
            persist_routing_decision(
                config,
                state,
                task_slug=task.slug,
                story_content=story_content,
                run_id=logger._run_id,
            )
            save_resume_record(
                config.project_root,
                state,
                slug=task.slug,
                story_content=story_content,
                run_id=logger._run_id,
            )
            # Dispatch the cached verdict just like run_task's cache-valid
            # branch. Without this, a terminal ALREADY_DONE verdict served from
            # cache is applied to state but never acted on, so the resume path
            # falls straight through into DEV/GATE and escalates the empty
            # branch as missing-work.
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
            if _pf_result is not None:
                _total_elapsed = time.monotonic() - _task_start
                _fire_post_run_hook(
                    config, state, task, _pf_result, logger._run_id, _total_elapsed, logger
                )
                return _pf_result
            if _pf_already_done_loop:
                skip_dev_first_iter = True
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
    else:
        # No cached preflight state: the scheduler's in-memory map lost this
        # story (a mid-sprint re-exec drops it for stories already in flight).
        # Without this branch the resumed phases run against config exactly as
        # loaded from forge.yaml — the static roster — silently discarding the
        # panel size routing already decided for this story (#2154). Recover the
        # persisted decision; when none is usable, say so rather than passing off
        # the roster as a routed panel.
        from .preflight import restore_routing_decision  # noqa: PLC0415

        config, _routing_recovery = restore_routing_decision(
            config,
            state,
            task_slug=task.slug,
            story_content=story_content,
            log=_log,
        )
        logger._safe_emit("routing_recovery", phase="RESUME", **_routing_recovery)

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
                mark_merge_failed(
                    state,
                    result,
                    _merge_info.get("error"),
                    branch_name,
                    arming_failed=bool(_merge_info.get("arming_failed")),
                    inherited_dev_residue=bool(_merge_info.get("inherited_dev_residue")),
                )

        _total_elapsed = time.monotonic() - _task_start
        _fire_post_run_hook(config, state, task, result, logger._run_id, _total_elapsed, logger)
        logger._safe_emit(
            "run_end",
            outcome=_run_end_outcome(result),
            total_cost_usd=_round_cost(state.total_cost_measured),
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
    base_lands_locally: bool | None = None,
    lands_in_project_root: bool | None = None,
    reentry_mode: str = REENTRY_MODE_PIPELINE_RESUME,
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
        lands_in_project_root: Caller's answer to whether this story's approval
            merges into the project-root checkout, used by the landing
            precondition. The sprint scheduler supplies it; None derives it from
            auto_merge and config.
        reentry_mode: Which operator-facing command re-entered. ``forge review``
            passes REENTRY_MODE_REVIEW so the resume disclosure states that this
            path *runs* an outstanding review cycle; a ``--resume`` triage that
            lands here is a pipeline resume and keeps the default.
    """
    return _run_resume_coordinator(
        config,
        task,
        workspace_path,
        initial_phase=Phase.REVIEW,
        skip_dev_first_iter=True,
        reentry_mode=reentry_mode,
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
        base_lands_locally=base_lands_locally,
        lands_in_project_root=lands_in_project_root,
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
    base_lands_locally: bool | None = None,
    lands_in_project_root: bool | None = None,
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
        lands_in_project_root: Caller's answer to whether this story's approval
            merges into the project-root checkout, used by the landing
            precondition. The sprint scheduler supplies it; None derives it from
            auto_merge and config.
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
        base_lands_locally=base_lands_locally,
        lands_in_project_root=lands_in_project_root,
    )


# ── Review-only mode ─────────────────────────────────────────────────


def run_review_only(
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    *,
    notify: bool = False,
    run_id: str | None = None,
    sprint_name: str | None = None,
    branch_name: str | None = None,
) -> CoordinatorResult:
    """Run only the REVIEW phase on an existing worktree.

    Skips WORKSPACE, PREFLIGHT, DEV, VALIDATE.
    Returns a CoordinatorResult with phase=DONE (APPROVE) or ESCALATE
    (REQUEST_CHANGES — no DEV retry in review-only mode).

    ``sprint_name`` nests this story's logs and audit under the sprint, exactly
    as ``run_task`` does; without it a review run inside a sprint would write
    its record outside the sprint's log tree. ``branch_name`` overrides the
    branch derived from ``task.slug`` — needed when the worktree under review
    belongs to another story's branch, as it does for a batch-group member
    reviewed against the group leader's shared branch (#727).
    """
    _ensure_runners()
    state = _fresh_run_state()
    state.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _ro_task_start = time.monotonic()

    _run_id = run_id or _generate_run_id()
    state.run_id = _run_id
    state.sprint_name = sprint_name
    state.log_dir = _make_story_log_dir(config, task.slug, sprint_name=sprint_name)
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
        p2_policy=config.dev.p2_policy,
    )
    _log(f"  Dev P2 policy: {config.dev.p2_policy}")

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
    _branch_name = branch_name or config.workspace.branch_pattern.format(slug=task.slug)
    state.branch_name = _branch_name

    story_content = task.story_text if task.story_text is not None else load_story(task.story_path)
    state.story_content = story_content

    return _run_review_only_phase(
        state,
        config,
        task,
        story_content,
        workspace_path,
        _branch_name,
        notify=notify,
        logger=logger,
        task_start=_ro_task_start,
    )


# ── Audit ────────────────────────────────────────────────────────────
