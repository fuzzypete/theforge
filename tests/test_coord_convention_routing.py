"""Tests for routing hard convention violations after gate PASS.

Covers: TestConventionViolationRouting.
"""

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
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
    def test_convention_violation_after_passing_gate_creates_synthetic_blocking_review(
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
                max_review_cycles=2,
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

        with patch("theforge.coordinator.engine._run_validate_phase") as mock_validate:
            from theforge.coordinator.validate_phase import _ValidateOutcome

            mock_validate.side_effect = [
                (_ValidateOutcome.REVIEW_CONVENTION_BLOCK, None),
                (
                    _ValidateOutcome.ESCALATE,
                    CoordinatorResult(
                        success=False,
                        phase=Phase.ESCALATE,
                        state=CoordinatorState(phase=Phase.ESCALATE),
                        message="forced stop after synthetic convention review",
                    ),
                ),
            ]
            result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert mock_validate.call_count == 2
        assert mock_dev_prompt.call_count == 2
        mock_fix_prompt.assert_not_called()
        mock_pool.assert_not_called()
        second_validate_state = mock_validate.call_args_list[1].args[0]
        assert len(second_validate_state.review_results) == 1
        convention_review = second_validate_state.review_results[0]
        assert convention_review.verdict == "REQUEST_CHANGES"
        assert any(f.severity == "P1" for f in convention_review.findings)
        assert "Hard convention violation" in convention_review.findings[0].description
        assert convention_review.raw_yaml == {
            "source": "validate_convention_block",
            "convention_violations": [],
        }
        assert result.message == "forced stop after synthetic convention review"

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_convention_block_escalates_when_review_cycles_exhausted(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """Convention block with max_review_cycles=1 escalates immediately."""
        base_config = _make_config(tmp_path)
        config = dataclasses.replace(
            base_config,
            conventions_hard=HardConventionsConfig(max_module_lines=500),
            retry=base_config.retry.__class__(
                max_dev_iterations=base_config.retry.max_dev_iterations,
                max_review_cycles=1,
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

        with patch("theforge.coordinator.engine._run_validate_phase") as mock_validate:
            from theforge.coordinator.validate_phase import _ValidateOutcome

            mock_validate.return_value = (_ValidateOutcome.REVIEW_CONVENTION_BLOCK, None)
            result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "Convention violations persisted" in result.message
        assert "Max cycles (1) exhausted" in result.message
        # Only one validate call — escalated on the spot
        assert mock_validate.call_count == 1
        # Review pool never invoked
        mock_pool.assert_not_called()

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
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, decisions=["PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.", cost_usd=0.0)

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


def test_build_convention_blocking_review_injects_fallback_p1_when_details_missing() -> None:
    state = CoordinatorState(convention_violations=[])

    from theforge.coordinator.engine import _build_convention_blocking_review

    review = _build_convention_blocking_review(state)

    assert review.verdict == "REQUEST_CHANGES"
    assert len(review.findings) == 1
    assert review.findings[0].severity == "P1"
    assert "no violation details were recorded" in review.findings[0].description.lower()
    assert review.raw_yaml == {
        "source": "validate_convention_block",
        "convention_violations": [],
    }
