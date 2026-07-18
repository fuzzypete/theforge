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
    mock_gate,
    patch_gate_shell,
)

from theforge.coordinator.gate import run_gate_full
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.coordinator.validate_phase import _run_validate_phase, _ValidateOutcome

# ── gate.py unit tests ────────────────────────────────────────────────────────


class TestGateDecision:
    """run_gate_full trusts exit code as sole gate signal."""

    def test_gate_plain_fail_unchanged(self, tmp_path: Path):
        """Exit non-zero with genuine failure output → FAIL, with the observed exit code."""
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        # Fidelity mode: the gate observes the real 4-tuple (exit_code, timed_out)
        # from the shell primitive rather than a derived 0-if-ok-else-1.
        with patch_gate_shell() as mock_shell:
            mock_shell.return_value = (False, "2 failed, 1 passed\nERROR: test_foo", 1, False)
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd, exit_code = run_gate_full(config, workspace)

        assert decision == "FAIL"
        assert error is None
        assert exit_code == 1

    def test_gate_pass_unchanged(self, tmp_path: Path):
        """Exit zero → PASS regardless of output, reporting exit code 0."""
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with patch_gate_shell() as mock_shell:
            mock_shell.return_value = (True, "All checks passed!", 0, False)
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd, exit_code = run_gate_full(config, workspace)

        assert decision == "PASS"
        assert error is None
        assert exit_code == 0

    def test_gate_nonzero_with_ruff_success_message_is_fail(self, tmp_path: Path):
        """Exit non-zero + ruff 'All checks passed!' in stdout → FAIL (not CONTRADICTION).

        Regression: ruff emits 'All checks passed!' when linting succeeds. A
        multi-stage gate (ruff && pytest) exits non-zero when pytest fails even
        though ruff passed. This must be classified as FAIL, not escalated. The
        gate reports the observed non-zero exit code (e.g. 1 from pytest).
        """
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with patch_gate_shell() as mock_shell:
            mock_shell.return_value = (
                False,
                "All checks passed!\n2 failed, 3 passed\nFAILED tests/test_foo.py::test_bar",
                1,
                False,
            )
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd, exit_code = run_gate_full(config, workspace)

        assert decision == "FAIL"
        assert error is None
        assert exit_code == 1

    def test_gate_timed_out_without_banner_is_timeout(self, tmp_path: Path):
        """Observed timed_out=True → timeout error, even with ordinary output.

        Regression (#1737): the gate must classify a timeout from the observed
        ``timed_out`` field returned by ``_run_shell_detailed``, not by sniffing
        the output for a ``TIMEOUT`` prefix. A process killed on timeout can be
        reaped with a signal exit code (e.g. 137) and partial ordinary output
        that never carries the banner; trusting the output prefix alone would
        report PASS/FAIL with exit 137 instead of a timeout.
        """
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with patch_gate_shell() as mock_shell:
            # timed_out=True with ordinary (non-"TIMEOUT") output and a signal
            # exit code — the exact shape the old output-prefix check missed.
            mock_shell.return_value = (False, "collecting ... tests/test_slow.py", 137, True)
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd, exit_code = run_gate_full(config, workspace)

        assert decision is None
        assert error is not None
        assert "timed out" in error
        assert exit_code == 137

    def test_gate_timed_out_via_sanctioned_seam(self, tmp_path: Path):
        """The sanctioned fidelity seam's ``timed_out=True`` is reported as a timeout.

        Exercises the same regression (#1737) through the one sanctioned seam:
        ``mock_gate`` fidelity mode with ``decision="PASS"`` but an observed
        ``timed_out=True``/``exit_code=137`` must surface as a timeout error, not
        a PASS. This also pins the seam's success flag to the observed values
        (success only when exit 0 and not timed out).
        """
        config = _make_config(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _write_handoff(workspace)

        with mock_gate(workspace, "PASS", exit_code=137, timed_out=True):
            with patch("theforge.coordinator.gate._cu._log"):
                decision, error, _tail, _cmd, exit_code = run_gate_full(config, workspace)

        assert decision is None
        assert error is not None
        assert "timed out" in error
        assert exit_code == 137


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
