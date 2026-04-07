"""Tests for routing hard convention violations after gate PASS.

Covers: TestConventionViolationRouting.
"""

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _shell_with_gate,
)

from theforge.config.types import HardConventionsConfig
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.task import TaskStory


def _make_task(tmp_path: Path) -> TaskStory:
    """Create a test task with a real spec file."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
    )


class TestConventionViolationRouting:
    """Hard convention violations after gate PASS stay in review, not DEV retry."""

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_convention_violation_after_passing_gate_goes_to_review_not_dev_retry(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        base_config = _make_config(tmp_path)
        config = dataclasses.replace(
            base_config,
            conventions_hard=HardConventionsConfig(max_module_lines=500),
            retry=base_config.retry.__class__(
                max_dev_iterations=base_config.retry.max_dev_iterations,
                max_review_cycles=1,
                max_handoff_retries=base_config.retry.max_handoff_retries,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace, decisions=["PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.", cost_usd=0.0)
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with patch("theforge.coordinator.engine._run_validate_phase") as mock_validate:
            from theforge.coordinator.validate_phase import _ValidateOutcome

            mock_validate.return_value = (_ValidateOutcome.REVIEW_CONVENTION_BLOCK, None)
            result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert mock_dev_prompt.call_count == config.retry.max_dev_iterations
        mock_fix_prompt.assert_not_called()
        assert len(result.state.review_results) == config.retry.max_review_cycles
        convention_review = result.state.review_results[0]
        assert convention_review.verdict == "REQUEST_CHANGES"
        assert any(f.severity == "P1" for f in convention_review.findings)
        assert "Hard convention violation" in convention_review.findings[0].description
        assert result.message == (
            f"Review requested changes after {config.retry.max_review_cycles} cycles. "
            f"Max cycles ({config.retry.max_review_cycles}) exhausted."
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_convention_violation_still_escalates_when_dev_budget_exhausted(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        tmp_path,
    ):
        base_config = _make_config(tmp_path)
        config = dataclasses.replace(
            base_config,
            conventions_hard=HardConventionsConfig(max_module_lines=500),
            retry=base_config.retry.__class__(
                max_dev_iterations=1,
                max_review_cycles=2,
                max_handoff_retries=base_config.retry.max_handoff_retries,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, decisions=["PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.", cost_usd=0.0)
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        with patch("theforge.coordinator.engine._run_validate_phase") as mock_validate:
            from theforge.coordinator.validate_phase import _ValidateOutcome

            mock_validate.return_value = (
                _ValidateOutcome.ESCALATE,
                CoordinatorResult(
                    success=False,
                    phase=Phase.ESCALATE,
                    state=CoordinatorState(phase=Phase.ESCALATE),
                    message="Hard convention violations after 1 attempts",
                ),
            )
            result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.message == "Hard convention violations after 1 attempts"
        assert len(result.state.review_results) == 0
