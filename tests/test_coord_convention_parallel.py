"""Tests for convention check running in parallel with gate execution.

Covers: TestConventionParallelCheck — combined gate-fail + convention violation feedback,
PASS path with pre-fetched violations, and no-conventions-configured path.
"""

import dataclasses
import types
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.config.types import HardConventionsConfig
from theforge.coordinator.state import CoordinatorState, Phase, RetryReason
from theforge.coordinator.validate_phase import _run_validate_phase, _ValidateOutcome


def _make_violation(
    rule: str = "max_module_lines",
    file: str = "src/foo.py",
    detail: str = "Module exceeds 500 lines (600 found)",
    blocking: bool = True,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(rule=rule, file=file, detail=detail, blocking=blocking)


def _make_state() -> CoordinatorState:
    return CoordinatorState(phase=Phase.VALIDATE)


class TestConventionParallelCheck:
    """Convention check runs in parallel with gate; violations appear in combined feedback."""

    @patch("theforge.coordinator.validate_phase.record_dev_iteration_telemetry")
    @patch("theforge.coordinator.validate_phase._get_handoff_content")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase._run_gate_full")
    def test_gate_fail_includes_convention_violations(
        self,
        mock_gate,
        mock_cv,
        mock_handoff,
        mock_telemetry,
        tmp_path,
    ):
        """When gate FAILs, convention violations are appended to human_feedback."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            conventions_hard=HardConventionsConfig(max_module_lines=500),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = _make_state()

        viol = _make_violation()
        mock_gate.return_value = ("FAIL", None, "test output: 1 failed", "make gate")
        mock_cv.return_value = ([viol], [viol])
        mock_handoff.return_value = "handoff content"

        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            workspace,
            dev_calls_this_cycle=0,
            notify=False,
            logger=None,
        )

        assert outcome is _ValidateOutcome.RETRY_DEV
        assert result is None
        assert state.retry_reason == RetryReason.GATE_FAIL
        # Gate failure text is present
        assert "test suite" in state.human_feedback
        # Convention violation text is also present in the same feedback
        assert "convention violations" in state.human_feedback.lower()
        assert "max_module_lines" in state.human_feedback
        assert "src/foo.py" in state.human_feedback
        # Convention violations are recorded on state
        assert len(state.convention_violations) == 1
        assert state.convention_violations[0]["rule"] == "max_module_lines"

    @patch("theforge.coordinator.validate_phase.record_dev_iteration_telemetry")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase._run_gate_full")
    @patch("theforge.coordinator.validate_phase._cu._run_shell")
    @patch("theforge.coordinator.validate_phase._deindex_forge_artifacts")
    def test_gate_pass_convention_violation_still_blocks(
        self,
        mock_deindex,
        mock_shell,
        mock_gate,
        mock_cv,
        mock_telemetry,
        tmp_path,
    ):
        """When gate PASSes but conventions have violations, outcome is REVIEW_CONVENTION_BLOCK."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            conventions_hard=HardConventionsConfig(max_module_lines=500),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = _make_state()

        viol = _make_violation()
        mock_gate.return_value = ("PASS", None, "", "make gate")
        mock_cv.return_value = ([viol], [viol])
        mock_shell.return_value = (True, "")  # clean worktree

        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            workspace,
            dev_calls_this_cycle=0,
            notify=False,
            logger=None,
        )

        assert outcome is _ValidateOutcome.REVIEW_CONVENTION_BLOCK
        assert result is None
        assert state.retry_reason == RetryReason.CONVENTION_VIOLATIONS
        assert len(state.convention_violations) == 1
        assert "Hard convention violations" in state.human_feedback

    @patch("theforge.coordinator.validate_phase.record_dev_iteration_telemetry")
    @patch("theforge.coordinator.validate_phase._get_handoff_content")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase._run_gate_full")
    def test_no_conventions_configured_unchanged(
        self,
        mock_gate,
        mock_cv,
        mock_handoff,
        mock_telemetry,
        tmp_path,
    ):
        """When conventions_hard is None, gate FAIL returns RETRY_DEV with no convention text."""
        config = _make_config(tmp_path)  # conventions_hard is None by default
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = _make_state()

        mock_gate.return_value = ("FAIL", None, "1 failed", "make gate")
        mock_cv.return_value = None  # None because conventions_hard is None
        mock_handoff.return_value = "handoff"

        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            workspace,
            dev_calls_this_cycle=0,
            notify=False,
            logger=None,
        )

        assert outcome is _ValidateOutcome.RETRY_DEV
        assert result is None
        assert state.retry_reason == RetryReason.GATE_FAIL
        assert "convention" not in state.human_feedback.lower()
        assert state.convention_violations is None or state.convention_violations == []
