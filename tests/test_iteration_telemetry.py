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
from theforge.sprint.audit import _write_sprint_summary
from theforge.sprint.manifest import SprintManifest, SprintResult


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
