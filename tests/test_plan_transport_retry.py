"""Seam-level tests for transient-transport retry of the PLAN agent call (#1672).

Sprint 065a91f3caf1 escalated two stories at PLAN because a single
connection-closed transport error produced an empty plan and the coordinator
escalated immediately with no retry — unlike DEV and PLAN_REVIEW, which both
retry transient failures. These tests exercise the coordinator PREFLIGHT→PLAN
seam and assert the draft call now retries a transient failure before
escalating, records the retry in the audit trail, and still escalates a genuine
(non-transient) planning failure.
"""

import dataclasses
from unittest.mock import patch

from coord_test_helpers import (
    PREFLIGHT_PROCEED_MEDIUM,
    _make_agent_result,
    _make_plan_config,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase

_CONNECTION_CLOSED = (
    "API Error: Connection closed mid-response. The response above may be incomplete."
)


def _prep(mock_validate_story, mock_shell, mock_preflight, tmp_path):
    from theforge.story_validator import StoryValidationResult

    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()
    mock_validate_story.return_value = StoryValidationResult(verdict="PASS")
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
    )
    return task


@patch("theforge.coordinator.plan_flow.time.sleep", lambda *_a, **_k: None)
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.util._run_shell")
@patch("theforge.story_validator.validate_story")
def test_plan_draft_retries_transient_connection_closed(
    mock_validate_story, mock_shell, mock_preflight, mock_plan_agent, tmp_path
):
    """A connection-closed draft failure is retried, not escalated."""
    config = _make_plan_config(tmp_path)
    task = _prep(mock_validate_story, mock_shell, mock_preflight, tmp_path)

    mock_plan_agent.side_effect = [
        _make_agent_result(success=False, output=_CONNECTION_CLOSED, cost_usd=0.0),
        _make_agent_result(success=True, output="# Plan\n\n- step 1", cost_usd=0.10),
    ]

    result = run_task(config, task, stop_phase=Phase.PLAN)

    assert result.success is True
    assert mock_plan_agent.call_count == 2  # retried once instead of escalating
    retries = result.state.plan_transport_retries
    assert len(retries) == 1
    assert retries[0]["phase"] == "PLAN"
    assert retries[0]["retry"] == 1


@patch("theforge.coordinator.plan_flow.time.sleep", lambda *_a, **_k: None)
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.util._run_shell")
@patch("theforge.story_validator.validate_story")
def test_plan_draft_escalates_after_retries_exhausted(
    mock_validate_story, mock_shell, mock_preflight, mock_plan_agent, tmp_path
):
    """When every attempt hits the transient error, escalate after the bound."""
    base = _make_plan_config(tmp_path)
    config = dataclasses.replace(
        base, retry=dataclasses.replace(base.retry, max_plan_transport_retries=1)
    )
    task = _prep(mock_validate_story, mock_shell, mock_preflight, tmp_path)

    mock_plan_agent.side_effect = [
        _make_agent_result(success=False, output=_CONNECTION_CLOSED, cost_usd=0.0),
        _make_agent_result(success=False, output=_CONNECTION_CLOSED, cost_usd=0.0),
    ]

    result = run_task(config, task, stop_phase=Phase.PLAN)

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    assert mock_plan_agent.call_count == 2  # 1 initial + 1 retry, then escalate
    assert len(result.state.plan_transport_retries) == 1


@patch("theforge.coordinator.plan_flow.time.sleep", lambda *_a, **_k: None)
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.util._run_shell")
@patch("theforge.story_validator.validate_story")
def test_plan_draft_genuine_failure_does_not_retry(
    mock_validate_story, mock_shell, mock_preflight, mock_plan_agent, tmp_path
):
    """A non-transient planning failure escalates immediately with no retry."""
    config = _make_plan_config(tmp_path)
    task = _prep(mock_validate_story, mock_shell, mock_preflight, tmp_path)

    mock_plan_agent.side_effect = [
        _make_agent_result(
            success=False, output="I could not decompose this story.", cost_usd=0.02
        ),
        _make_agent_result(success=True, output="# Plan\n\n- step 1", cost_usd=0.10),
    ]

    result = run_task(config, task, stop_phase=Phase.PLAN)

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    assert mock_plan_agent.call_count == 1  # no retry on a genuine failure
    assert result.state.plan_transport_retries == []
