"""Tests for convention check running in parallel with gate execution.

Covers: TestConventionParallelCheck — combined gate-fail + convention violation feedback,
PASS path with pre-fetched violations, and no-conventions-configured path.
"""

import dataclasses
import types
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.advisory_conventions import AdvisoryArtifactError
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
    @patch("theforge.coordinator.validate_phase.run_gate_full")
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
        state.budget.max_iterations = config.retry.max_dev_iterations
        mock_gate.return_value = ("FAIL", None, "test output: 1 failed", "make gate", 1)
        mock_cv.return_value = ([viol], [viol])
        mock_handoff.return_value = "handoff content"

        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            workspace,
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
        assert state.last_review_findings is None

    @patch("theforge.coordinator.validate_phase.record_dev_iteration_telemetry")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase.run_gate_full")
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
        """Gate PASSes with blocking=True violations → dev retry inside the current cycle."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            conventions_hard=HardConventionsConfig(max_module_lines=500),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = _make_state()

        viol = _make_violation(blocking=True)
        state.budget.max_iterations = config.retry.max_dev_iterations
        mock_gate.return_value = ("PASS", None, "", "make gate", 0)
        mock_cv.return_value = ([viol], [viol])
        mock_shell.return_value = (True, "")  # clean worktree

        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            workspace,
            notify=False,
            logger=None,
        )

        assert outcome is _ValidateOutcome.RETRY_DEV
        assert result is None
        assert state.retry_reason == RetryReason.CONVENTION_VIOLATIONS
        assert len(state.convention_violations) == 1
        assert "Hard convention violations" in state.human_feedback
        assert state.last_review_findings is None

    @patch("theforge.coordinator.validate_phase.record_dev_iteration_telemetry")
    @patch("theforge.coordinator.validate_phase.update_advisory_violations")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase.run_gate_full")
    @patch("theforge.coordinator.validate_phase._cu._run_shell")
    @patch("theforge.coordinator.validate_phase._deindex_forge_artifacts")
    def test_gate_pass_followup_only_violations_proceed_to_pass(
        self,
        mock_deindex,
        mock_shell,
        mock_gate,
        mock_cv,
        mock_update_advisory,
        mock_telemetry,
        tmp_path,
    ):
        """Gate PASSes with only follow-up (blocking=False) violations → outcome is PASS."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            conventions_hard=HardConventionsConfig(max_module_lines=500),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = _make_state()

        # LOC violations are non-blocking follow-ups
        viol = _make_violation(rule="max_module_lines", blocking=False)
        state.budget.max_iterations = config.retry.max_dev_iterations
        mock_gate.return_value = ("PASS", None, "", "make gate", 0)
        mock_cv.return_value = ([viol], [viol])
        mock_shell.return_value = (True, "")  # clean worktree
        mock_update_advisory.return_value = {
            "path": str(tmp_path / ".forge" / "conventions" / "advisory.yaml"),
            "entry_count": 1,
            "newly_filed_issues": [],
            "entries": {},
        }

        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            workspace,
            notify=False,
            logger=None,
        )

        # Follow-up violations do not block — proceed to PASS
        assert outcome is _ValidateOutcome.PASS
        assert result is None
        # Violations are recorded on state for audit visibility
        assert len(state.convention_violations) == 1
        assert state.convention_violations[0]["rule"] == "max_module_lines"
        assert state.convention_violations[0]["blocking"] is False
        # Follow-up violations are NOT injected into human_feedback
        assert not state.human_feedback or "convention" not in state.human_feedback.lower()
        # retry_reason is not set to CONVENTION_VIOLATIONS
        assert state.retry_reason != RetryReason.CONVENTION_VIOLATIONS
        mock_update_advisory.assert_called_once()
        advisory_payload = mock_update_advisory.call_args.args[1]
        assert advisory_payload == [
            {
                "rule": "max_module_lines",
                "file": "src/foo.py",
                "detail": "Module exceeds 500 lines (600 found)",
                "blocking": False,
            }
        ]

    @patch("theforge.coordinator.validate_phase.record_dev_iteration_telemetry")
    @patch("theforge.coordinator.validate_phase.update_advisory_violations")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase.run_gate_full")
    @patch("theforge.coordinator.validate_phase._cu._run_shell")
    @patch("theforge.coordinator.validate_phase._deindex_forge_artifacts")
    def test_advisory_persistence_failure_does_not_cost_the_story_its_result(
        self,
        mock_deindex,
        mock_shell,
        mock_gate,
        mock_cv,
        mock_update_advisory,
        mock_telemetry,
        tmp_path,
    ):
        """A failure to persist the shared advisory artifact is infrastructure's,
        not the story's: VALIDATE still PASSes and the failure is recorded on the
        run's shared-infrastructure ledger for the audit (#2107)."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            conventions_hard=HardConventionsConfig(max_module_lines=500),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = _make_state()

        viol = _make_violation(rule="max_module_lines", blocking=False)
        state.budget.max_iterations = config.retry.max_dev_iterations
        mock_gate.return_value = ("PASS", None, "", "make gate", 0)
        mock_cv.return_value = ([viol], [viol])
        mock_shell.return_value = (True, "")  # clean worktree
        artifact = tmp_path / ".forge" / "conventions" / "advisory.yaml"
        mock_update_advisory.side_effect = AdvisoryArtifactError(
            artifact, FileNotFoundError(2, "No such file or directory")
        )

        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            workspace,
            notify=False,
            logger=None,
        )

        assert outcome is _ValidateOutcome.PASS
        assert result is None
        assert state.error is None
        failures = state.shared_infrastructure_failures
        assert len(failures) == 1
        assert failures[0]["component"] == "advisory_conventions_artifact"
        assert failures[0]["path"] == str(artifact)
        assert failures[0]["error_type"] == "FileNotFoundError"
        assert "No such file or directory" in failures[0]["error"]

    @patch("theforge.coordinator.validate_phase.record_dev_iteration_telemetry")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase.run_gate_full")
    @patch("theforge.coordinator.validate_phase._cu._run_shell")
    @patch("theforge.coordinator.validate_phase._deindex_forge_artifacts")
    def test_gate_pass_mixed_violations_blocks_on_blocking_only(
        self,
        mock_deindex,
        mock_shell,
        mock_gate,
        mock_cv,
        mock_telemetry,
        tmp_path,
    ):
        """Mixed blocking + follow-up violations: dev retry fires; both recorded."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            conventions_hard=HardConventionsConfig(max_module_lines=500),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        state = _make_state()

        blocking_viol = _make_violation(rule="no_circular_imports", file="src/a.py", blocking=True)
        followup_viol = _make_violation(rule="max_module_lines", file="src/big.py", blocking=False)
        state.budget.max_iterations = config.retry.max_dev_iterations
        mock_gate.return_value = ("PASS", None, "", "make gate", 0)
        mock_cv.return_value = ([blocking_viol, followup_viol], [blocking_viol, followup_viol])
        mock_shell.return_value = (True, "")  # clean worktree

        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            workspace,
            notify=False,
            logger=None,
        )

        # Blocking violations send the finding back to dev
        assert outcome is _ValidateOutcome.RETRY_DEV
        assert result is None
        assert state.retry_reason == RetryReason.CONVENTION_VIOLATIONS
        # Both violations recorded on state
        assert len(state.convention_violations) == 2
        rules = {v["rule"] for v in state.convention_violations}
        assert "no_circular_imports" in rules
        assert "max_module_lines" in rules
        # Only blocking violation text in human_feedback
        assert "no_circular_imports" in state.human_feedback
        assert "max_module_lines" not in state.human_feedback

    @patch("theforge.coordinator.validate_phase.record_dev_iteration_telemetry")
    @patch("theforge.coordinator.validate_phase._get_handoff_content")
    @patch("theforge.coordinator.validate_phase._check_conventions_parallel")
    @patch("theforge.coordinator.validate_phase.run_gate_full")
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

        state.budget.max_iterations = config.retry.max_dev_iterations
        mock_gate.return_value = ("FAIL", None, "1 failed", "make gate", 1)
        mock_cv.return_value = None  # None because conventions_hard is None
        mock_handoff.return_value = "handoff"

        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            workspace,
            notify=False,
            logger=None,
        )

        assert outcome is _ValidateOutcome.RETRY_DEV
        assert result is None
        assert state.retry_reason == RetryReason.GATE_FAIL
        assert "convention" not in state.human_feedback.lower()
        assert state.convention_violations is None or state.convention_violations == []
