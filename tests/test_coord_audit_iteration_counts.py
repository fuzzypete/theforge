"""Tests for audit iteration count accuracy (issue-601).

Verifies:
- dev_attempts_total counts all dev invocations
- dev_iterations_productive counts productive dev calls
- review_cycles_total is a separate distinct field
- Each dev telemetry entry carries a (cycle, iteration) tuple
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    DevIterationTelemetry,
    Phase,
)
from theforge.runners import AgentResult


def _make_config(tmp_path: Path):
    from theforge.config import (
        DEFAULT_DEV_PROFILE,
        DEFAULT_PREFLIGHT_PROFILE,
        DEFAULT_REVIEW_PROFILE,
        ForgeConfig,
        RetryPolicy,
        ValidationConfig,
        WorkspaceConfig,
    )

    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(
            gate_command="make gate",
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


def _make_task(tmp_path: Path):
    from theforge.task import TaskStory

    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# Test spec", encoding="utf-8")
    return TaskStory(name="Test Task", slug="test-task", story_path=spec_path)


def _agent_result(cost: float = 0.10, profile: str = "dev") -> AgentResult:
    return AgentResult(
        success=True,
        output="done",
        session_id=None,
        cost_usd=cost,
        exit_code=0,
        raw={},
        profile_name=profile,
    )


def _make_result(state: CoordinatorState) -> CoordinatorResult:
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")


class TestIterationCountFields:
    """AC1: audit records dev_attempts_total, dev_iterations_productive, review_cycles_total."""

    def test_no_calls_all_zero(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["iterations"]["dev_attempts_total"] == 0
        assert log["iterations"]["dev_iterations_productive"] == 0
        assert log["iterations"]["review_cycles_total"] == 0
        assert log["totals"]["dev_attempts_total"] == 0
        assert log["totals"]["dev_iterations_productive"] == 0
        assert log["totals"]["review_cycles_total"] == 0

    def test_productive_dev_only_no_handoff_fix(self, tmp_path: Path) -> None:
        """When no handoff-fix retries occur, dev_attempts_total == dev_iterations_productive."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result())
        state.dev_results.append(_agent_result())
        state.dev_durations.extend([10.0, 12.0])
        state.dev_iteration = 2  # 2 productive iterations
        state.review_cycle = 1

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert log["iterations"]["dev_attempts_total"] == 2
        assert log["iterations"]["dev_iterations_productive"] == 2
        assert log["iterations"]["review_cycles_total"] == 1
        assert log["totals"]["dev_attempts_total"] == 2
        assert log["totals"]["dev_iterations_productive"] == 2
        assert log["totals"]["review_cycles_total"] == 1

    def test_productive_count_survives_cycle_reset(self, tmp_path: Path) -> None:
        """dev_iterations_productive counts across ALL review cycles, not just the last.

        Regression test: state.dev_iteration resets to 0 on REQUEST_CHANGES; using it
        as the productive count would report 0 (or N from the final cycle) instead of
        the true cumulative total.  len(state.dev_results) is the correct source.
        """
        state = CoordinatorState()
        # Simulate 2 productive dev calls in cycle 1 + 1 productive call in cycle 2
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_durations.extend([10.0, 12.0, 9.0])
        # dev_iteration is reset to 0 by review_phase on REQUEST_CHANGES, then
        # incremented once for cycle 2's single dev call — final value is 1, not 3.
        state.dev_iteration = 1
        state.review_cycle = 2

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        # Must reflect all 3 productive calls, not just the last cycle's counter
        assert log["iterations"]["dev_iterations_productive"] == 3
        assert log["totals"]["dev_iterations_productive"] == 3
        # Backward-compat alias must also be correct
        assert log["iterations"]["dev_iterations"] == 3
        assert log["totals"]["dev_iterations"] == 3

    def test_dev_attempts_total_equals_productive_count(self, tmp_path: Path) -> None:
        """dev_attempts_total equals dev_iterations_productive when no handoff-fix retries."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_durations.extend([10.0, 12.0])
        state.dev_iteration = 2
        state.review_cycle = 1

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert log["iterations"]["dev_attempts_total"] == 2
        assert log["iterations"]["dev_iterations_productive"] == 2
        assert log["totals"]["dev_attempts_total"] == 2
        assert log["totals"]["dev_iterations_productive"] == 2

    def test_total_dev_cost(self, tmp_path: Path) -> None:
        """total_dev_cost property sums all dev result costs."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result(cost=1.00))
        state.dev_results.append(_agent_result(cost=0.25))

        assert state.total_dev_cost == pytest.approx(1.25)


class TestCycleIterationTuple:
    """AC2: each dev telemetry entry has (cycle, iteration) tuple."""

    def test_cycle_field_present_in_dev_loop(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.dev_iteration_telemetry.append(
            DevIterationTelemetry(
                iteration=1,
                max_iterations=3,
                cost_usd=0.50,
                duration_s=10.0,
                cycle=0,
                gate_result="FAIL",
            )
        )
        state.dev_iteration_telemetry.append(
            DevIterationTelemetry(
                iteration=1,
                max_iterations=3,
                cost_usd=0.60,
                duration_s=12.0,
                cycle=1,
                gate_result="PASS",
            )
        )

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        dev_loop = log["iterations"]["dev_loop"]
        assert len(dev_loop) == 2
        assert dev_loop[0]["cycle"] == 0
        assert dev_loop[0]["iteration"] == 1
        assert dev_loop[1]["cycle"] == 1
        assert dev_loop[1]["iteration"] == 1

    def test_cycle_defaults_to_zero(self, tmp_path: Path) -> None:
        """DevIterationTelemetry.cycle defaults to 0 for backward compat."""
        t = DevIterationTelemetry(iteration=1, max_iterations=3, cost_usd=None, duration_s=5.0)
        assert t.cycle == 0


class TestCostAgentRoles:
    """Dev agent entries are tagged correctly in cost.agents."""

    def test_dev_role_in_agents(self, tmp_path: Path) -> None:
        """cost.agents entries for dev calls use role='dev'."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result(cost=1.00))
        state.dev_durations.append(10.0)

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        agents = log["cost"]["agents"]
        roles = [a["role"] for a in agents]
        assert "dev" in roles
        assert all(a["role"] != "dev/handoff-fix" for a in agents)
