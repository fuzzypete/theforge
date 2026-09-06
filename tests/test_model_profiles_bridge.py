"""Seam test: CoordinatorState → model_profiles.yaml via the bridge."""

from __future__ import annotations

from dataclasses import dataclass

from theforge.coordinator.model_profiles_bridge import (
    build_run_outcome,
    update_profiles_from_run,
)
from theforge.coordinator.state import (
    CoordinatorState,
    ReviewCycleMetadata,
    ReviewIterationTelemetry,
)


@dataclass
class _Profile:
    name: str


@dataclass
class _Cfg:
    project_root: str = "."
    dev_profile: _Profile = None  # type: ignore[assignment]
    preflight_profile: _Profile = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.dev_profile is None:
            self.dev_profile = _Profile(name="sonnet")
        if self.preflight_profile is None:
            self.preflight_profile = _Profile(name="deepseek-r1")


def _make_state(*, review_cycles: int = 1) -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.dev_trace_count = 2
    # Populate cost via synthetic AgentResult-like objects? total_dev_cost sums
    # over dev_results; easier to set dev_results to []; we set cost via synthetic
    # entries.

    # We don't have AgentResult import here — set a minimal stand-in that matches
    # the .cost_usd attribute summed by total_dev_cost. A simple dataclass works.
    @dataclass
    class _AR:
        cost_usd: float | None = 0.0

    state.dev_results = [_AR(cost_usd=0.30), _AR(cost_usd=0.20)]
    # preflight_result left as None (total_preflight_cost has a complex raw shape
    # that isn't relevant to what we're asserting here).
    # Set review cycle metadata + telemetry
    for i in range(review_cycles):
        state.review_cycle_metadata.append(
            ReviewCycleMetadata(
                pool_models=["gemini", "opus"],
                successful=["gemini", "opus"],
                failed=[],
                synthesized=False,
            )
        )
        state.review_iteration_telemetry.append(
            ReviewIterationTelemetry(
                iteration=i + 1,
                max_iterations=3,
                cost_usd=0.40,
                duration_s=5.0,
                verdict="APPROVE",
                findings_by_severity={"P1": 0, "P2": 2},
                new_findings_by_severity={},
                repeated_findings_by_severity={},
                novel_findings=0,
                restated_findings=0,
            )
        )
    return state


def test_build_run_outcome_captures_dev_preflight_and_reviewers():
    state = _make_state(review_cycles=2)
    cfg = _Cfg()

    outcome = build_run_outcome(cfg, state, success=True)
    assert outcome.complexity == "medium"
    assert outcome.dev_model == "sonnet"
    assert outcome.dev_success is True
    assert outcome.dev_iterations == 2
    assert outcome.dev_cost_usd == 0.5
    assert outcome.preflight_model == "deepseek-r1"
    assert outcome.preflight_cost_usd == 0.0
    # Two reviewers, two cycles each.
    assert set(outcome.reviewers.keys()) == {"gemini", "opus"}
    cycles, findings, cost = outcome.reviewers["gemini"]
    assert cycles == 2
    assert findings == 4  # 2 P2 per cycle * 2 cycles
    assert round(cost, 2) == 0.40  # $0.40/cycle split 2 ways * 2 cycles = 0.40


def test_update_profiles_from_run_writes_file(tmp_path):
    state = _make_state(review_cycles=1)
    cfg = _Cfg(project_root=str(tmp_path))
    profiles_path = tmp_path / "model_profiles.yaml"

    data = update_profiles_from_run(
        profiles_path=profiles_path,
        history_path=None,
        config=cfg,
        state=state,
        success=True,
    )

    assert profiles_path.exists()
    assert data["models"]["sonnet"]["dev"]["runs"] == 1
    assert data["models"]["deepseek-r1"]["preflight"]["runs"] == 1
    assert data["models"]["gemini"]["review"]["runs"] == 1
    assert data["models"]["opus"]["review"]["runs"] == 1


@dataclass
class _AR:
    cost_usd: float | None = 0.0


def _state_with_dev_costs(costs: list[float | None]) -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_complexity = "medium"
    state.dev_trace_count = 1
    state.dev_results = [_AR(cost_usd=c) for c in costs]
    return state


def test_unmeasured_dev_cost_threads_none_through_bridge():
    """A CoordinatorState whose dev_results are all cost-unmeasured yields a
    RunOutcome with dev_cost_usd is None — the bridge must not coerce to $0.00."""
    state = _state_with_dev_costs([None])
    outcome = build_run_outcome(_Cfg(), state, success=True)
    assert outcome.dev_cost_usd is None


def test_unmeasured_dev_cost_records_cost_unknown_not_zero(tmp_path):
    """update_from_run records the run as cost-unmeasured (not folded into
    _cost_sum) so an unmeasured run stays distinct from a genuinely free one."""
    state = _state_with_dev_costs([None])
    cfg = _Cfg(project_root=str(tmp_path))
    profiles_path = tmp_path / "model_profiles.yaml"

    data = update_profiles_from_run(
        profiles_path=profiles_path,
        history_path=None,
        config=cfg,
        state=state,
        success=True,
    )

    dev = data["models"]["sonnet"]["dev"]
    assert dev["runs"] == 1
    assert dev["_cost_unknown_runs"] == 1
    assert dev["_cost_sum"] == 0.0
    # avg is over measured runs only (0 here) — not a polluted $0.00 average.
    assert dev["avg_cost_usd"] == 0.0
    bucket = dev["by_complexity"]["medium"]
    assert bucket["_cost_unknown_runs"] == 1
    assert bucket["_cost_sum"] == 0.0


def test_measured_zero_dev_cost_is_not_counted_unmeasured(tmp_path):
    """A genuinely free ($0.00 measured) run increments neither _cost_unknown_runs."""
    state = _state_with_dev_costs([0.0])
    cfg = _Cfg(project_root=str(tmp_path))
    profiles_path = tmp_path / "model_profiles.yaml"

    data = update_profiles_from_run(
        profiles_path=profiles_path,
        history_path=None,
        config=cfg,
        state=state,
        success=True,
    )

    dev = data["models"]["sonnet"]["dev"]
    assert dev["runs"] == 1
    assert dev["_cost_unknown_runs"] == 0
    assert dev["_cost_sum"] == 0.0
    assert dev["avg_cost_usd"] == 0.0


def test_mixed_measured_and_unmeasured_dev_costs_are_unknown_aggregate():
    """If any dev attempt is unmeasured, the whole aggregate is cost-unknown —
    collapsing to a measured subtotal would hide the unmeasured attempt."""
    state = _state_with_dev_costs([0.30, None])
    outcome = build_run_outcome(_Cfg(), state, success=True)
    assert outcome.dev_cost_usd is None


def _dev_telemetry(*, is_timeout: bool):
    from theforge.coordinator.state import DevIterationTelemetry

    return DevIterationTelemetry(
        iteration=1,
        max_iterations=3,
        cost_usd=0.10,
        duration_s=42.0,
        is_timeout=is_timeout,
    )


def test_build_run_outcome_populates_duration_from_dev_durations():
    state = _state_with_dev_costs([0.30])
    state.dev_durations = [120.5, 200.25]
    outcome = build_run_outcome(_Cfg(), state, success=True)
    assert outcome.dev_duration_s == 320.75
    # No timeout kill flag → not a censored kill.
    assert outcome.dev_timeout_killed is False


def test_build_run_outcome_no_durations_yields_none():
    state = _state_with_dev_costs([0.30])
    assert state.dev_durations == []
    outcome = build_run_outcome(_Cfg(), state, success=True)
    assert outcome.dev_duration_s is None


def test_build_run_outcome_marks_timeout_killed_and_limit():
    state = _state_with_dev_costs([0.30])
    state.dev_durations = [1349.9]
    state.adaptive_dev_timeout_seconds = 1350
    state.dev_process_timeout_killed = True
    outcome = build_run_outcome(_Cfg(), state, success=False)
    assert outcome.dev_timeout_killed is True
    assert outcome.dev_timeout_limit_s == 1350


def test_build_run_outcome_reads_sticky_kill_flag_not_last_telemetry():
    """The censored-kill signal is the sticky state flag set at kill time, NOT
    the is_timeout of whichever telemetry entry happens to be last. A later
    VALIDATE-phase telemetry write (no is_timeout) must not erase the kill —
    the checkpoint-commit terminal-kill scenario from the spec (#1754)."""
    state = _state_with_dev_costs([0.30])
    state.adaptive_dev_timeout_seconds = 1350
    # Dev was killed at its timeout (flag set in dev_phase)...
    state.dev_process_timeout_killed = True
    # ...but execution fell through to VALIDATE, whose telemetry entry (no
    # is_timeout) is now the tail of dev_iteration_telemetry.
    state.dev_iteration_telemetry = [
        _dev_telemetry(is_timeout=True),
        _dev_telemetry(is_timeout=False),
    ]
    outcome = build_run_outcome(_Cfg(), state, success=True)
    assert outcome.dev_timeout_killed is True
    assert outcome.dev_timeout_limit_s == 1350


def test_build_run_outcome_no_kill_flag_is_not_killed_despite_stale_timeout_tail():
    """Absent the sticky flag, a stale is_timeout on the last telemetry entry
    (e.g. a VALIDATE gate/test-command timeout) does not mark a censored kill."""
    state = _state_with_dev_costs([0.30])
    state.dev_process_timeout_killed = False
    state.dev_iteration_telemetry = [_dev_telemetry(is_timeout=True)]
    outcome = build_run_outcome(_Cfg(), state, success=True)
    assert outcome.dev_timeout_killed is False


def test_build_run_outcome_termination_cause_timeout():
    """A timeout kill sets dev_termination_cause='timeout' so the aggregator can
    segregate the run out of capability stats (#1763)."""
    state = _state_with_dev_costs([0.30])
    state.adaptive_dev_timeout_seconds = 1350
    state.dev_process_timeout_killed = True
    outcome = build_run_outcome(_Cfg(), state, success=False)
    assert outcome.dev_termination_cause == "timeout"


def test_build_run_outcome_termination_cause_stuck():
    """A stuck-pattern terminate sets dev_termination_cause='stuck_pattern'."""
    state = _state_with_dev_costs([0.30])
    state.dev_process_stuck_terminated = True
    outcome = build_run_outcome(_Cfg(), state, success=False)
    assert outcome.dev_termination_cause == "stuck_pattern"
    assert outcome.dev_timeout_killed is False


def test_build_run_outcome_termination_cause_max_iterations_no_submit():
    """A coordinator iteration limit before submit is not model capability evidence."""
    state = _state_with_dev_costs([0.30])
    state.dev_max_iterations_no_submit = True
    outcome = build_run_outcome(_Cfg(), state, success=False)
    assert outcome.dev_termination_cause == "max_iterations_no_submit"
    assert outcome.dev_timeout_killed is False


def test_build_run_outcome_no_submit_survives_error_type_overwrite():
    """The cut-off is read from the sticky flag, not from ``error_type``.

    ``state.error_type`` is last-writer-wins: a later dev iteration assigns its
    own ``failure_code`` over the ``"max_iterations_no_submit"`` classification.
    If the bridge read ``error_type``, the coordinator-caused termination would
    silently revert to being recorded as a model failure — the exact defect.
    """
    state = _state_with_dev_costs([0.30])
    state.dev_max_iterations_no_submit = True
    state.error_type = "gate_fail"
    outcome = build_run_outcome(_Cfg(), state, success=False)
    assert outcome.dev_termination_cause == "max_iterations_no_submit"


def test_build_run_outcome_genuine_dev_failure_still_counts():
    """A model-produced failure is untouched: no cut-off flag, no segregation."""
    state = _state_with_dev_costs([0.30])
    state.error_type = "gate_fail"
    outcome = build_run_outcome(_Cfg(), state, success=False)
    assert outcome.dev_termination_cause is None
    assert outcome.dev_success is False


def test_build_run_outcome_termination_cause_timeout_precedence():
    """When both flags are set, timeout takes precedence over stuck."""
    state = _state_with_dev_costs([0.30])
    state.dev_process_timeout_killed = True
    state.dev_process_stuck_terminated = True
    outcome = build_run_outcome(_Cfg(), state, success=False)
    assert outcome.dev_termination_cause == "timeout"


def test_build_run_outcome_no_termination_cause_on_clean_run():
    """A run the model finished (no harness kill) carries no termination cause."""
    state = _state_with_dev_costs([0.30])
    outcome = build_run_outcome(_Cfg(), state, success=True)
    assert outcome.dev_termination_cause is None
