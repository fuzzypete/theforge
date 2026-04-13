"""Tests for gate CONTRADICTION detection and validate-phase escalation.

Covers:
- gate.py detects exit-nonzero + success-text → "CONTRADICTION" decision
- gate.py plain fail (no success text) → "FAIL" unchanged
- validate_phase.py escalates immediately on CONTRADICTION (no dev retry)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _make_config,
    _make_task,
    _write_handoff,
)

from theforge.coordinator.gate import _run_gate_full
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.coordinator.validate_phase import _run_validate_phase, _ValidateOutcome

# ── gate.py unit tests ────────────────────────────────────────────────────────


class TestGateContradictionDetection:
    """_run_gate_full returns CONTRADICTION when exit!=0 but output says success."""

    def test_gate_contradiction_detected_all_checks_passed(self, tmp_path: Path):
        """Exit non-zero + 'All checks passed!' → CONTRADICTION."""
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with patch("theforge.coordinator.gate._cu._run_shell") as mock_shell:
            mock_shell.return_value = (
                False,
                "Some warnings\nAll checks passed!\nExit 1 due to coverage threshold",
            )
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd = _run_gate_full(config, workspace)

        assert decision == "CONTRADICTION"
        assert error is None

    def test_gate_contradiction_detected_0_failed(self, tmp_path: Path):
        """Exit non-zero + '0 failed' in output → CONTRADICTION."""
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with patch("theforge.coordinator.gate._cu._run_shell") as mock_shell:
            mock_shell.return_value = (False, "0 failed, 5 passed")
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd = _run_gate_full(config, workspace)

        assert decision == "CONTRADICTION"
        assert error is None

    def test_gate_plain_fail_unchanged(self, tmp_path: Path):
        """Exit non-zero with genuine failure output → FAIL (not CONTRADICTION)."""
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with patch("theforge.coordinator.gate._cu._run_shell") as mock_shell:
            mock_shell.return_value = (False, "2 failed, 1 passed\nERROR: test_foo")
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd = _run_gate_full(config, workspace)

        assert decision == "FAIL"
        assert error is None

    def test_gate_pass_unchanged(self, tmp_path: Path):
        """Exit zero → PASS regardless of output."""
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with patch("theforge.coordinator.gate._cu._run_shell") as mock_shell:
            mock_shell.return_value = (True, "All checks passed!")
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd = _run_gate_full(config, workspace)

        assert decision == "PASS"
        assert error is None

    def test_gate_contradiction_case_insensitive(self, tmp_path: Path):
        """Pattern matching is case-insensitive."""
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with patch("theforge.coordinator.gate._cu._run_shell") as mock_shell:
            mock_shell.return_value = (False, "ALL CHECKS PASSED!")
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd = _run_gate_full(config, workspace)

        assert decision == "CONTRADICTION"


# ── validate_phase.py integration tests ───────────────────────────────────────


def _make_state(config) -> CoordinatorState:
    """Minimal coordinator state for validate-phase tests."""
    state = CoordinatorState()
    state.phase = Phase.VALIDATE
    state.budget.max_iterations = 2
    return state


class TestValidatePhaseContradictionEscalates:
    """Validate phase escalates immediately on CONTRADICTION instead of retrying dev."""

    @patch("theforge.coordinator.validate_phase._run_gate_full")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase._escalate_notify")
    def test_contradiction_escalates(
        self, mock_notify, mock_conventions, mock_gate, tmp_path: Path
    ):
        """CONTRADICTION gate decision → ESCALATE outcome, no dev retry."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        _write_handoff(workspace, "CONTRADICTION")

        mock_gate.return_value = ("CONTRADICTION", None, "All checks passed!", "make gate")
        mock_conventions.return_value = None  # no conventions configured

        state = _make_state(config)

        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            workspace,
            notify=False,
            logger=None,
        )

        assert outcome == _ValidateOutcome.ESCALATE
        assert result is not None
        assert result.success is False
        assert state.phase == Phase.ESCALATE
        assert "non-zero" in state.error.lower() or "indicated success" in state.error.lower()
        mock_notify.assert_called_once()

    @patch("theforge.coordinator.validate_phase._run_gate_full")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase._escalate_notify")
    def test_contradiction_error_message_content(
        self, mock_notify, mock_conventions, mock_gate, tmp_path: Path
    ):
        """CONTRADICTION escalation message mentions test-runner quirk for human context."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        _write_handoff(workspace, "CONTRADICTION")

        mock_gate.return_value = ("CONTRADICTION", None, "All checks passed!", "make gate")
        mock_conventions.return_value = None

        state = _make_state(config)

        _run_validate_phase(state, config, task, workspace, notify=False, logger=None)

        assert "human review" in state.error.lower() or "test-runner" in state.error.lower()

    @patch("theforge.coordinator.validate_phase._run_gate_full")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase._escalate_notify")
    def test_contradiction_appended_to_gate_decisions(
        self, mock_notify, mock_conventions, mock_gate, tmp_path: Path
    ):
        """CONTRADICTION is recorded in state.gate_decisions for audit trail."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        _write_handoff(workspace, "CONTRADICTION")

        mock_gate.return_value = ("CONTRADICTION", None, "All checks passed!", "make gate")
        mock_conventions.return_value = None

        state = _make_state(config)

        _run_validate_phase(state, config, task, workspace, notify=False, logger=None)

        assert "CONTRADICTION" in state.gate_decisions

    @patch("theforge.coordinator.validate_phase._run_gate_full")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase._escalate_notify")
    def test_plain_fail_still_retries(
        self, mock_notify, mock_conventions, mock_gate, tmp_path: Path
    ):
        """Plain FAIL still causes RETRY_DEV (not broken by CONTRADICTION branch)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        _write_handoff(workspace, "FAIL")

        mock_gate.return_value = ("FAIL", None, "2 failed, 1 passed", "make gate")
        mock_conventions.return_value = None

        state = _make_state(config)

        outcome, result = _run_validate_phase(
            state, config, task, workspace, notify=False, logger=None
        )

        assert outcome == _ValidateOutcome.RETRY_DEV
        assert result is None
        mock_notify.assert_not_called()
