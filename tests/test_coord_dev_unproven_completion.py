"""Seam-level tests for the dev-phase unproven-completion guard (#927).

A dev that exits successfully but hands off a completion claim (an acceptance
criterion marked MET) without gate PASS evidence must be escalated at the dev
seam — the coordinator catches what the dev should have declared as a blocking
failure. A well-formed completion (MET + gate_result PASS) must pass the guard.
"""

from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase


def _completion_handoff(*, gate_result: str | None) -> dict:
    """A dev handoff that marks its acceptance criterion MET (a completion claim)."""
    data: dict = {
        "summary": "Implemented the thing.",
        "commits": [{"sha": "abc1234", "message": "feat(x): implement"}],
        "acceptance_criteria": [{"criterion": "It works", "status": "MET", "notes": "tested"}],
        "story_deviations": "none",
        "deferred_items": "none",
    }
    if gate_result is not None:
        data["gate_result"] = gate_result
    return data


class TestUnprovenCompletionGuard:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_completion_without_gate_evidence_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """A successful dev claiming MET with no gate_result → ESCALATE at dev seam."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True,
            output="Done.",
            profile_name="dev",
            dev_handoff=_completion_handoff(gate_result=None),
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "without gate PASS evidence" in result.message
        # Escalated at the dev seam before any review cycle ran.
        assert mock_pool.call_count == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_completion_with_gate_blocked_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """MET + gate_result BLOCKED is still an unproven completion → ESCALATE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True,
            output="Done.",
            profile_name="dev",
            dev_handoff=_completion_handoff(gate_result="BLOCKED"),
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "without gate PASS evidence" in result.message

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_well_formed_completion_is_not_escalated(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """MET + gate_result PASS passes the guard and proceeds to review → DONE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True,
            output="Done.",
            profile_name="dev",
            dev_handoff=_completion_handoff(gate_result="PASS"),
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase != Phase.ESCALATE
        # The unproven-completion guard did not block the review cycle.
        assert mock_pool.call_count >= 1
