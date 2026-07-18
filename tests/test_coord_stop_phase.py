"""Stop-phase coordinator tests split from test_coordinator.py."""

from unittest.mock import patch

from coord_test_helpers import PREFLIGHT_PROCEED_MEDIUM, _make_plan_config, _shell_with_gate

from tests.test_coordinator import _make_agent_result, _make_task
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase


class TestStopPhasePlan:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    @patch("theforge.story_validator.validate_story")
    def test_until_plan_stops_before_dev(
        self,
        mock_validate_story,
        mock_shell,
        mock_dev_agent,
        mock_plan_agent,
        mock_preflight,
        mock_pool,
        tmp_path,
    ):
        """--until plan: run PREFLIGHT+PLAN, then stop before DEV."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        from theforge.story_validator import StoryValidationResult

        mock_validate_story.return_value = StoryValidationResult(verdict="PASS")
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_plan_agent.return_value = _make_agent_result(
            success=True, output="# Plan\n\n- step 1", cost_usd=0.10
        )

        result = run_task(config, task, stop_phase=Phase.PLAN)

        assert result.success is True
        assert result.phase == Phase.PLAN
        assert "Stopped at --until plan" in result.message
        assert result.state.dev_iteration == 0
        mock_dev_agent.assert_not_called()
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    @patch("theforge.story_validator.validate_story")
    def test_until_plan_review_stops_before_dev(
        self,
        mock_validate_story,
        mock_shell,
        mock_dev_agent,
        mock_plan_agent,
        mock_preflight,
        mock_pool,
        tmp_path,
    ):
        """--until plan-review: stop at the same point as --until plan."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        from theforge.story_validator import StoryValidationResult

        mock_validate_story.return_value = StoryValidationResult(verdict="PASS")
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_plan_agent.return_value = _make_agent_result(
            success=True, output="# Plan\n\n- step 1", cost_usd=0.10
        )

        result = run_task(config, task, stop_phase=Phase.PLAN_REVIEW)

        assert result.success is True
        assert result.phase == Phase.PLAN
        assert "Stopped at --until plan_review" in result.message
        assert result.state.dev_iteration == 0
        mock_dev_agent.assert_not_called()
        mock_pool.assert_not_called()
