"""Tests for stale plan file cleanup in coordinator/plan_flow.py."""

from unittest.mock import patch

from coord_test_helpers import (
    PREFLIGHT_PROCEED_MEDIUM,
    _make_agent_result,
    _make_plan_config,
    _make_task,
    _shell_with_gate,
)

from theforge.artifacts import LEGACY_PLAN_PATH, PLAN_PATH
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase


@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.util._run_shell")
@patch("theforge.story_validator.validate_story")
def test_stale_plan_files_removed_before_plan_agent(
    mock_validate_story,
    mock_shell,
    mock_preflight,
    mock_plan_agent,
    tmp_path,
):
    """Both .forge/plan.md and forge_plan.md are deleted before a new plan is written."""
    from theforge.story_validator import StoryValidationResult

    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    # Plant stale plan files at both locations
    preferred_plan = workspace / PLAN_PATH
    legacy_plan = workspace / LEGACY_PLAN_PATH
    preferred_plan.parent.mkdir(parents=True, exist_ok=True)
    preferred_plan.write_text("# Old plan\n", encoding="utf-8")
    legacy_plan.write_text("# Also old\n", encoding="utf-8")

    mock_validate_story.return_value = StoryValidationResult(verdict="PASS")
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
    )

    new_plan_text = "# Fresh plan\n\n- step 1"
    mock_plan_agent.return_value = _make_agent_result(
        success=True, output=new_plan_text, cost_usd=0.10
    )

    # Stop at PLAN so we don't need to mock the full DEV/REVIEW loop
    result = run_task(config, task, stop_phase=Phase.PLAN)

    assert result.success is True

    # The preferred plan path must contain the new content, not the old
    written = preferred_plan.read_text(encoding="utf-8")
    assert written == new_plan_text

    # The legacy plan file must no longer exist (removed before new plan was written)
    assert not legacy_plan.exists()


@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.util._run_shell")
@patch("theforge.story_validator.validate_story")
def test_no_stale_files_is_fine(
    mock_validate_story,
    mock_shell,
    mock_preflight,
    mock_plan_agent,
    tmp_path,
):
    """Plan phase runs without error when no stale plan files exist."""
    from theforge.story_validator import StoryValidationResult

    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

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
