"""Tests for audit iteration count accuracy (issue-601).

Verifies:
- dev_attempts_total counts all invocations including handoff-fix retries
- dev_iterations_productive counts only productive dev calls (no handoff-fix)
- review_cycles_total is a separate distinct field
- Each dev telemetry entry carries a (cycle, iteration) tuple
- Handoff-fix retries are tagged with role="dev/handoff-fix" in cost.agents
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
            handoff_file="handoff.yaml",
            gate_decision_key="gate_result",
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

    def test_handoff_fix_inflates_attempts_total(self, tmp_path: Path) -> None:
        """dev_attempts_total = productive + handoff-fix; productive count is unchanged."""
        state = CoordinatorState()
        # 2 productive dev calls
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_results.append(_agent_result(cost=0.50))
        state.dev_durations.extend([10.0, 12.0])
        state.dev_iteration = 2
        # 3 handoff-fix retries
        state.dev_handoff_fix_results.append(_agent_result(cost=0.05))
        state.dev_handoff_fix_results.append(_agent_result(cost=0.05))
        state.dev_handoff_fix_results.append(_agent_result(cost=0.05))
        state.review_cycle = 1

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert log["iterations"]["dev_attempts_total"] == 5  # 2 + 3
        assert log["iterations"]["dev_iterations_productive"] == 2
        assert log["iterations"]["review_cycles_total"] == 1
        assert log["totals"]["dev_attempts_total"] == 5
        assert log["totals"]["dev_iterations_productive"] == 2

    def test_cost_counts_include_handoff_fix(self, tmp_path: Path) -> None:
        """cost.dev_invocations counts all invocations; dev_productive_invocations excludes fix."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result(cost=1.00))
        state.dev_durations.append(10.0)
        state.dev_handoff_fix_results.append(_agent_result(cost=0.10))
        state.dev_handoff_fix_results.append(_agent_result(cost=0.10))
        state.dev_iteration = 1

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert log["cost"]["dev_invocations"] == 3  # 1 productive + 2 handoff-fix
        assert log["cost"]["dev_productive_invocations"] == 1
        assert log["cost"]["dev_handoff_fix_invocations"] == 2

    def test_total_dev_cost_includes_handoff_fix(self, tmp_path: Path) -> None:
        """total_dev_cost property sums both productive and handoff-fix costs."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result(cost=1.00))
        state.dev_handoff_fix_results.append(_agent_result(cost=0.25))

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


class TestHandoffFixTagging:
    """AC3: handoff-fix retries are tagged distinctly in cost.agents."""

    def test_handoff_fix_role_in_agents(self, tmp_path: Path) -> None:
        """cost.agents entries for handoff-fix retries use role='dev/handoff-fix'."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result(cost=1.00))
        state.dev_durations.append(10.0)
        state.dev_handoff_fix_results.append(_agent_result(cost=0.05, profile="dev"))
        state.dev_handoff_fix_results.append(_agent_result(cost=0.05, profile="dev"))

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        agents = log["cost"]["agents"]
        roles = [a["role"] for a in agents]
        assert "dev" in roles
        assert "dev/handoff-fix" in roles
        fix_entries = [a for a in agents if a["role"] == "dev/handoff-fix"]
        assert len(fix_entries) == 2

    def test_productive_dev_entries_precede_handoff_fix(self, tmp_path: Path) -> None:
        """In cost.agents, productive dev entries come before handoff-fix entries."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result(cost=1.00))
        state.dev_durations.append(10.0)
        state.dev_handoff_fix_results.append(_agent_result(cost=0.05))

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        agents = log["cost"]["agents"]
        assert agents[0]["role"] == "dev"
        assert agents[1]["role"] == "dev/handoff-fix"

    def test_no_handoff_fix_means_no_fix_entries(self, tmp_path: Path) -> None:
        """When no handoff-fix retries occur, no dev/handoff-fix entries in agents."""
        state = CoordinatorState()
        state.dev_results.append(_agent_result())
        state.dev_durations.append(10.0)

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        agents = log["cost"]["agents"]
        assert all(a["role"] != "dev/handoff-fix" for a in agents)
