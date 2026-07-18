"""Tests for gate exit-code decision and validate-phase retry behavior.

Covers:
- gate.py exit non-zero → FAIL regardless of stdout content
- gate.py exit zero → PASS regardless of stdout content
- validate_phase.py FAIL → RETRY_DEV
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _make_config,
    _make_task,
    _write_handoff,
)

from theforge.coordinator.gate import run_gate_full
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.coordinator.validate_phase import _run_validate_phase, _ValidateOutcome

# ── gate.py unit tests ────────────────────────────────────────────────────────


class TestGateDecision:
    """run_gate_full trusts exit code as sole gate signal."""

    def test_gate_plain_fail_unchanged(self, tmp_path: Path):
        """Exit non-zero with genuine failure output → FAIL."""
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with patch("theforge.coordinator.gate._cu._run_shell") as mock_shell:
            mock_shell.return_value = (False, "2 failed, 1 passed\nERROR: test_foo")
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd, _exit = run_gate_full(config, workspace)

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
                decision, error, _tail, _cmd, _exit = run_gate_full(config, workspace)

        assert decision == "PASS"
        assert error is None

    def test_gate_nonzero_with_ruff_success_message_is_fail(self, tmp_path: Path):
        """Exit non-zero + ruff 'All checks passed!' in stdout → FAIL (not CONTRADICTION).

        Regression: ruff emits 'All checks passed!' when linting succeeds. A
        multi-stage gate (ruff && pytest) exits non-zero when pytest fails even
        though ruff passed. This must be classified as FAIL, not escalated.
        """
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with patch("theforge.coordinator.gate._cu._run_shell") as mock_shell:
            mock_shell.return_value = (
                False,
                "All checks passed!\n2 failed, 3 passed\nFAILED tests/test_foo.py::test_bar",
            )
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd, _exit = run_gate_full(config, workspace)

        assert decision == "FAIL"
        assert error is None


# ── validate_phase.py integration tests ───────────────────────────────────────


def _make_state(config) -> CoordinatorState:
    """Minimal coordinator state for validate-phase tests."""
    state = CoordinatorState()
    state.phase = Phase.VALIDATE
    state.budget.max_iterations = 2
    return state


class TestValidatePhaseFailRetries:
    """Validate phase retries dev on FAIL."""

    @patch("theforge.coordinator.validate_phase.run_gate_full")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase._escalate_notify")
    def test_plain_fail_still_retries(
        self, mock_notify, mock_conventions, mock_gate, tmp_path: Path
    ):
        """Plain FAIL → RETRY_DEV."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        _write_handoff(workspace, "FAIL")

        mock_gate.return_value = ("FAIL", None, "2 failed, 1 passed", "make gate", 1)
        mock_conventions.return_value = None

        state = _make_state(config)

        outcome, result = _run_validate_phase(
            state, config, task, workspace, notify=False, logger=None
        )

        assert outcome == _ValidateOutcome.RETRY_DEV
        assert result is None
        mock_notify.assert_not_called()
