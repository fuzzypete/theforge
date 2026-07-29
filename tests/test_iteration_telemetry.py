from __future__ import annotations

import datetime as _dt

import yaml
from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    DevIterationTelemetry,
    Phase,
    ReviewIterationTelemetry,
)
from theforge.sprint.audit import _write_sprint_audit, _write_sprint_summary
from theforge.sprint.manifest import ResolvedSprint, SprintManifest, SprintResult


def test_story_audit_includes_iteration_loop_metrics(tmp_path):
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=2, review_cycle=1)
    state.dev_iteration_telemetry = [
        DevIterationTelemetry(
            iteration=1,
            max_iterations=3,
            cost_usd=1.25,
            duration_s=12.3,
            gate_result="FAIL",
            failed_tests=["tests/test_alpha.py::test_one"],
            files_changed=["src/a.py"],
            files_changed_count=1,
            tests_fixed_count=0,
            meaningful_progress=True,
            cli_quota_error_observed=True,
            transport_fallback_fired=True,
            transport_fallback_reason="matched 'usage limit'",
            transport_used="api",
            model_used="gpt-5.4",
        ),
        DevIterationTelemetry(
            iteration=2,
            max_iterations=3,
            cost_usd=0.75,
            duration_s=8.0,
            gate_result="PASS",
            failed_tests=[],
            files_changed=["src/a.py", "tests/test_alpha.py"],
            files_changed_count=2,
            tests_fixed_count=1,
            meaningful_progress=True,
        ),
    ]
    state.review_iteration_telemetry = [
        ReviewIterationTelemetry(
            iteration=1,
            max_iterations=2,
            cost_usd=0.5,
            duration_s=6.2,
            verdict="APPROVE",
            findings_by_severity={"P1": 0, "P2": 1},
            new_findings_by_severity={"P1": 0, "P2": 1},
            repeated_findings_by_severity={"P1": 0, "P2": 0},
            novel_findings=1,
            restated_findings=0,
        )
    ]

    audit = generate_audit_log(
        config, task, CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")
    )

    assert audit["iterations"]["dev_loop"][0]["gate_result"] == "FAIL"
    assert audit["iterations"]["dev_loop"][0]["cli_quota_error_observed"] is True
    assert audit["iterations"]["dev_loop"][0]["transport_fallback_fired"] is True
    assert audit["iterations"]["dev_loop"][0]["transport_used"] == "api"
    assert audit["iterations"]["dev_loop"][0]["model_used"] == "gpt-5.4"
    assert audit["iterations"]["dev_loop"][1]["tests_fixed_count"] == 1
    assert audit["iterations"]["review_loop"][0]["finding_counts"] == {"P1": 0, "P2": 1}
    assert audit["iterations"]["review_loop"][0]["novel_findings"] == 1
    assert audit["iterations"]["usage_summary"]["dev"] == {
        "used": 2,
        "max": 3,
        "hit_limit": False,
        "early_finish": True,
    }


def test_story_audit_includes_runner_failure_fields(tmp_path):
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.dev_iteration_telemetry = [
        DevIterationTelemetry(
            iteration=1,
            max_iterations=3,
            cost_usd=None,
            duration_s=1.2,
            gate_result="RUNNER_CRASH",
            files_changed=["scratch.py"],
            files_changed_count=1,
            meaningful_progress=True,
            agent_exit_code=2,
            runner_failure_code="runner_argument_error",
            runner_failure_summary="error: unexpected argument '-C' found",
            cli_quota_error_observed=False,
            transport_fallback_fired=False,
            transport_used="cli",
            model_used="gpt-5.4",
        )
    ]

    audit = generate_audit_log(
        config,
        task,
        CoordinatorResult(
            success=False,
            phase=Phase.ESCALATE,
            state=state,
            message="runner crash",
        ),
    )

    assert audit["iterations"]["dev_loop"][0]["agent_exit_code"] == 2
    assert audit["iterations"]["dev_loop"][0]["runner_failure_code"] == "runner_argument_error"
    assert audit["iterations"]["dev_loop"][0]["runner_failure_summary"] == (
        "error: unexpected argument '-C' found"
    )
    assert audit["iterations"]["dev_loop"][0]["transport_used"] == "cli"
    assert audit["iterations"]["dev_loop"][0]["model_used"] == "gpt-5.4"


def test_sprint_summary_includes_iteration_usage_distribution(tmp_path):
    manifest = SprintManifest(name="demo-sprint", budget_usd=5.0, stories=["issue:123"])
    state = CoordinatorState(phase=Phase.DONE)
    state.dev_iteration_telemetry = [
        DevIterationTelemetry(iteration=1, max_iterations=3, cost_usd=0.4, duration_s=4.0),
        DevIterationTelemetry(
            iteration=2,
            max_iterations=3,
            cost_usd=0.3,
            duration_s=3.0,
            gate_result="PASS",
        ),
    ]
    state.review_iteration_telemetry = [
        ReviewIterationTelemetry(
            iteration=1,
            max_iterations=2,
            cost_usd=0.2,
            duration_s=2.0,
            verdict="APPROVE",
            findings_by_severity={"P1": 0, "P2": 0},
            new_findings_by_severity={"P1": 0, "P2": 0},
            repeated_findings_by_severity={"P1": 0, "P2": 0},
            novel_findings=0,
            restated_findings=0,
        )
    ]
    result = SprintResult(
        name="demo-sprint",
        budget_usd=5.0,
        results=[
            (
                "issue:123",
                CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok"),
            )
        ],
        total_cost_usd=0.9,
        specs_total=1,
        specs_succeeded=1,
        specs_failed=0,
        specs_skipped=0,
    )
    ts = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
    sprint_log_dir = tmp_path / ".forge" / "logs" / "demo-sprint"

    _write_sprint_summary(
        manifest,
        result,
        ["issue:123"],
        ts,
        ts,
        0.0,
        sprint_log_dir,
        slug_map={"issue:123": "story-123"},
    )

    summary = yaml.safe_load((sprint_log_dir / "sprint-summary.yaml").read_text())
    assert summary["stories"][0]["iteration_usage"]["dev"]["used"] == 2
    assert summary["stories"][0]["iteration_usage"]["review"]["max"] == 2
    assert summary["iteration_usage_distribution"] == [
        {
            "spec": "issue:123",
            "slug": "story-123",
            "dev": {"used": 2, "max": 3},
            "review": {"used": 1, "max": 2},
        }
    ]


def _multi_cycle_dev_state(
    *, per_cycle_max: int, calls_per_cycle: list[int], phase: Phase = Phase.DONE
) -> CoordinatorState:
    """State for a story whose dev iterations span several review cycles.

    ``dev_iteration`` (and therefore ``budget.cycle_count``) is the last cycle's
    count, exactly as the coordinator leaves it: the per-cycle counter is reset
    at every cycle boundary while ``dev_iteration_telemetry`` accumulates for the
    whole story.
    """
    state = CoordinatorState(
        dev_iteration=calls_per_cycle[-1],
        review_cycle=len(calls_per_cycle),
        phase=phase,
    )
    state.adaptive_dev_max = per_cycle_max
    state.dev_iteration_telemetry = [
        DevIterationTelemetry(
            iteration=iteration,
            max_iterations=per_cycle_max,
            cost_usd=0.1,
            duration_s=1.0,
            cycle=cycle,
        )
        for cycle, calls in enumerate(calls_per_cycle, start=1)
        for iteration in range(1, calls + 1)
    ]
    return state


def test_story_audit_dev_usage_is_per_cycle_not_cumulative(tmp_path):
    """A multi-cycle story that never hit its per-cycle cap reports used <= max.

    Four dev iterations across two cycles used to be reported against the
    per-cycle max of 3 as ``{"used": 4, "max": 3}`` — an overrun that never
    happened (#1985).
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = _multi_cycle_dev_state(per_cycle_max=3, calls_per_cycle=[2, 2])
    assert len(state.dev_iteration_telemetry) == 4

    audit = generate_audit_log(
        config, task, CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")
    )

    assert audit["iterations"]["usage_summary"]["dev"] == {
        "used": 2,
        "max": 3,
        "hit_limit": False,
        "early_finish": True,
    }


def test_story_audit_dev_usage_reports_the_cycle_that_hit_the_cap(tmp_path):
    """Exhaustion in any single cycle is still reported, not averaged away."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = _multi_cycle_dev_state(per_cycle_max=2, calls_per_cycle=[2, 1], phase=Phase.ESCALATE)

    audit = generate_audit_log(
        config,
        task,
        CoordinatorResult(success=False, phase=Phase.ESCALATE, state=state, message="stop"),
    )

    assert audit["iterations"]["usage_summary"]["dev"] == {
        "used": 2,
        "max": 2,
        "hit_limit": True,
        "early_finish": False,
    }


def test_story_audit_dev_usage_counts_an_iteration_without_telemetry(tmp_path):
    """A consumed dev call that recorded no telemetry still counts as used.

    ``record_dev_iteration_telemetry`` returns early when the dev call produced
    no result, so the live per-cycle counter is the floor for the in-flight
    cycle.
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=2, review_cycle=1, phase=Phase.ESCALATE)
    state.adaptive_dev_max = 2

    audit = generate_audit_log(
        config,
        task,
        CoordinatorResult(success=False, phase=Phase.ESCALATE, state=state, message="stop"),
    )

    assert audit["iterations"]["usage_summary"]["dev"] == {
        "used": 2,
        "max": 2,
        "hit_limit": True,
        "early_finish": False,
    }


def _multi_cycle_sprint_result(state: CoordinatorState) -> SprintResult:
    return SprintResult(
        name="demo-sprint",
        budget_usd=5.0,
        results=[
            (
                "issue:123",
                CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok"),
            )
        ],
        total_cost_usd=0.9,
        specs_total=1,
        specs_succeeded=1,
        specs_failed=0,
        specs_skipped=0,
    )


def test_sprint_summary_dev_usage_is_per_cycle_not_cumulative(tmp_path):
    manifest = SprintManifest(name="demo-sprint", budget_usd=5.0, stories=["issue:123"])
    state = _multi_cycle_dev_state(per_cycle_max=3, calls_per_cycle=[2, 2])
    ts = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
    sprint_log_dir = tmp_path / ".forge" / "logs" / "demo-sprint"

    _write_sprint_summary(
        manifest,
        _multi_cycle_sprint_result(state),
        ["issue:123"],
        ts,
        ts,
        0.0,
        sprint_log_dir,
        slug_map={"issue:123": "story-123"},
    )

    summary = yaml.safe_load((sprint_log_dir / "sprint-summary.yaml").read_text())
    assert summary["stories"][0]["iteration_usage"]["dev"] == {
        "used": 2,
        "max": 3,
        "hit_limit": False,
        "early_finish": True,
    }
    assert summary["iteration_usage_distribution"][0]["dev"] == {"used": 2, "max": 3}


def test_sprint_audit_dev_usage_is_per_cycle_not_cumulative(tmp_path):
    state = _multi_cycle_dev_state(per_cycle_max=3, calls_per_cycle=[2, 2])
    ts = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)

    _write_sprint_audit(
        manifest=ResolvedSprint(
            name="demo-sprint", budget_usd=5.0, stories=["issue:123"], max_parallel=1
        ),
        result=_multi_cycle_sprint_result(state),
        canonical_refs=["issue:123"],
        started_at=ts,
        finished_at=ts,
        duration=0.0,
        project_root=tmp_path,
        slug_map={"issue:123": "story-123"},
    )

    audit = yaml.safe_load((tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text())
    assert audit["specs"][0]["iteration_usage"]["dev"] == {
        "used": 2,
        "max": 3,
        "hit_limit": False,
        "early_finish": True,
    }
    assert audit["iteration_usage_distribution"][0]["dev"] == {"used": 2, "max": 3}
