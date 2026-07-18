"""Regression tests confirming the sibling-worktree write detector has been removed.

The detector (_iter_sibling_worktrees, _git_status_porcelain_ignored,
_is_forge_artifact_status_line) was retired in favour of native CLI sandboxing.
These tests verify:
  - The helper functions no longer exist in dev_phase
  - _run_dev_phase does NOT escalate on sibling-worktree writes
  - Parallel sprints (max_parallel > 1) no longer trip on legitimate sibling activity
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

import theforge.coordinator.dev_phase as dev_phase_mod
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase
from theforge.runners import AgentResult


def _approve_pool_result() -> list[AgentResult]:
    return [
        AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id=None,
            cost_usd=0.10,
            exit_code=0,
            raw={},
            profile_name="review",
        )
    ]


class TestSiblingDetectorRemoved:
    """The three detector helpers must not exist in dev_phase."""

    def test_iter_sibling_worktrees_not_exported(self) -> None:
        """_iter_sibling_worktrees must not exist in dev_phase."""
        assert not hasattr(dev_phase_mod, "_iter_sibling_worktrees"), (
            "_iter_sibling_worktrees was not removed from dev_phase"
        )

    def test_is_forge_artifact_status_line_not_exported(self) -> None:
        """_is_forge_artifact_status_line must not exist in dev_phase."""
        assert not hasattr(dev_phase_mod, "_is_forge_artifact_status_line"), (
            "_is_forge_artifact_status_line was not removed from dev_phase"
        )

    def test_git_status_porcelain_ignored_not_exported(self) -> None:
        """_git_status_porcelain_ignored must not exist in dev_phase."""
        assert not hasattr(dev_phase_mod, "_git_status_porcelain_ignored"), (
            "_git_status_porcelain_ignored was not removed from dev_phase"
        )


class TestNoSiblingEscalation:
    """_run_dev_phase must NOT escalate when a sibling worktree has new writes."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.log_agent_result")
    @patch("theforge.coordinator.dev_phase.run_agent")
    def test_sibling_write_does_not_escalate(
        self,
        mock_dev_agent,
        mock_log_result,
        mock_preflight_agent,
        mock_shell,
        mock_pool,
        tmp_path: Path,
    ) -> None:
        """A write to a sibling worktree no longer causes ESCALATE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Set up a sibling worktree that will appear to have new files
        sibling = tmp_path / ".forge" / "worktrees" / "issue-999"
        sibling.mkdir(parents=True)
        (sibling / "new_file.py").write_text("# written by sibling agent\n")

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight_agent.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result()
        mock_pool.return_value = _approve_pool_result()

        result = run_task(config, task, notify=False)

        # Must NOT escalate — sibling writes are now contained by native sandbox
        assert result.state.phase != Phase.ESCALATE, f"Unexpected ESCALATE: {result.message}"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch_gate_shell()
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.log_agent_result")
    @patch("theforge.coordinator.dev_phase.run_agent")
    def test_git_status_not_called_for_siblings(
        self,
        mock_dev_agent,
        mock_log_result,
        mock_preflight_agent,
        mock_shell,
        mock_pool,
        tmp_path: Path,
    ) -> None:
        """subprocess.run is NOT called with --porcelain --ignored for siblings."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        sibling = tmp_path / ".forge" / "worktrees" / "issue-888"
        sibling.mkdir(parents=True)

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight_agent.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result()
        mock_pool.return_value = _approve_pool_result()

        with patch("theforge.coordinator.dev_phase.subprocess.run") as mock_sub:
            # Generic success response for any subprocess.run call in dev_phase
            mock_sub.return_value = MagicMock(returncode=0, stdout=b"abc123\n", stderr=b"")
            run_task(config, task, notify=False)

        # None of the subprocess.run calls should use --porcelain --ignored
        for call in mock_sub.call_args_list:
            args = call[0][0] if call[0] else call[1].get("args", [])
            assert "--porcelain" not in args or "--ignored" not in args, (
                "subprocess.run was called with --porcelain --ignored — "
                "the sibling detector was not fully removed"
            )
