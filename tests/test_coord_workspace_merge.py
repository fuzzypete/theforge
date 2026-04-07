"""Tests for workspace failure, auto-merge, and auto-push coordinator behaviour.

Extracted from test_coord_workspace.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    _write_handoff,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase

# ── Workspace failure ─────────────────────────────────────────────


class TestCoordinatorWorkspaceFailure:
    """Test that workspace creation failure escalates immediately."""

    @patch("theforge.coordinator.util._run_shell")
    def test_workspace_creation_fails(self, mock_shell, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        # Workspace creation command fails
        mock_shell.return_value = (False, "fatal: branch already exists")

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "workspace" in result.message.lower() or "Workspace" in result.message


# ── Auto-merge tests ──────────────────────────────────────────────


class TestCoordinatorAutoMerge:
    """Tests for the auto_merge=True path."""

    def _shell_with_gate_and_merge(self, workspace: "Path", merge_succeeds: bool = True):
        """Shell side_effect: handles gate, git status, and merge-related commands."""

        def side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")  # clean
            # Stale-worktree detection commands (must come before generic git log checks)
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git branch --list" in cmd:
                return (True, "main")  # base branch exists
            if "git log" in cmd and ".." in cmd:
                return (True, "abc123 feat: implement thing")  # has commits ahead
            if "git checkout" in cmd:
                return (True, "Switched to branch 'main'")
            if "git merge --ff-only" in cmd:
                if merge_succeeds:
                    return (True, "Fast-forward")
                return (False, "fatal: Not possible to fast-forward")
            if "git merge --no-edit" in cmd:
                return (True, "Merge made by 'ort'")
            if "git worktree remove" in cmd:
                return (True, "OK")
            return (True, "OK")

        return side_effect

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_merge_success_on_approve(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """auto_merge=True: merge occurs after APPROVE, result.merge.merged is True."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace, merge_succeeds=True)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.merge is not None
        assert result.merge["attempted"] is True
        assert result.merge["merged"] is True
        assert result.merge["base_branch"] == "main"
        assert result.merge["error"] is None
        assert "Merged." in result.message

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_merge_false_no_merge(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """auto_merge=False (default): no merge, result.merge is None."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=False)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.merge is not None
        assert result.merge["action"] == "none"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_merge_no_merge_on_escalate(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """auto_merge=True: no merge when result is ESCALATE (not APPROVE)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented."),
            _make_agent_result(success=True, output="Fixed."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.merge is None  # no merge attempted on ESCALATE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_merge_ff_fails_falls_back(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """auto_merge=True: non-ff fallback used when ff-only fails."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace, merge_succeeds=False)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.merge is not None
        assert result.merge["merged"] is True  # fell back to --no-edit and succeeded

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_merge_safety_no_base_branch(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """auto_merge=True: skips merge if base branch doesn't exist."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git branch --list" in cmd:
                return (True, "")  # base branch not found
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True  # run still succeeds
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None
        assert "not found" in result.merge["error"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_merge_safety_dirty_project_root(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """auto_merge=True: skips merge if project root has uncommitted changes."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        dirty_seen = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                dirty_seen["n"] += 1
                # First call is from validate (worktree clean check); second is safety check
                if dirty_seen["n"] == 1:
                    return (True, "")  # worktree clean -> proceed to review
                return (True, " M some_file.py")  # project root dirty -> skip merge
            if "git branch --list" in cmd:
                return (True, "main")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True  # run still succeeds
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None
        assert "Uncommitted" in result.merge["error"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_merge_safety_no_commits_ahead(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """auto_merge=True: skips merge if branch has no commits ahead of base."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git branch --list" in cmd:
                return (True, "main")
            if "git log" in cmd and ".." in cmd:
                return (True, "")  # no commits ahead
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert "no commits" in result.merge["error"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_merge_merge_info_in_audit(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Merge info appears in audit log under 'merge' key."""
        from theforge.coordinator.audit import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace, merge_succeeds=True)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)
        audit = generate_audit_log(config, task, result)

        assert "merge" in audit
        merge = audit["merge"]
        assert merge["attempted"] is True
        assert merge["merged"] is True
        assert merge["base_branch"] == "main"
        assert merge["error"] is None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_merge_false_no_merge_key_in_audit(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Without auto_merge, audit 'merge' key is None."""
        from theforge.coordinator.audit import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=False)
        audit = generate_audit_log(config, task, result)

        assert audit["merge"] is not None
        assert audit["merge"]["action"] == "none"


# ── Auto-push tests ───────────────────────────────────────────────


class TestCoordinatorAutoPush:
    """Tests for auto_push=True path inside _merge_branch."""

    def _make_auto_push_config(self, tmp_path: "Path", auto_push: bool = True) -> ForgeConfig:
        return ForgeConfig(
            project="test",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                auto_push=auto_push,
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=DEFAULT_DEV_PROFILE,
            preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        )

    def _shell_with_gate_and_merge(self, workspace: "Path"):
        def side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS", dev_notes="")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            stale_resp = _handle_stale_check_cmd(cmd)
            if stale_resp is not None:
                return stale_resp
            if "git branch --list" in cmd:
                return (True, "main")
            if "git log" in cmd and ".." in cmd:
                return (True, "abc123 feat: implement thing")
            if "git checkout" in cmd:
                return (True, "Switched to branch 'main'")
            if "git merge --ff-only" in cmd:
                return (True, "Fast-forward")
            if "git worktree remove" in cmd:
                return (True, "OK")
            return (True, "OK")

        return side_effect

    @patch("theforge.coordinator.workspace.subprocess.run")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_push_after_merge(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_subprocess, tmp_path
    ):
        """auto_push=True + merge success -> git push called with base_branch."""
        config = self._make_auto_push_config(tmp_path, auto_push=True)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_subprocess.return_value = MagicMock(returncode=0)

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.merge is not None
        assert result.merge["merged"] is True
        # Verify git push was called
        push_calls = [
            c
            for c in mock_subprocess.call_args_list
            if c.args and c.args[0][:3] == ["git", "push", "origin"]
        ]
        assert len(push_calls) == 1
        assert push_calls[0].args[0] == ["git", "push", "origin", "main"]

    @patch("theforge.coordinator.workspace.subprocess.run")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_push_disabled_by_default(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_subprocess, tmp_path
    ):
        """auto_push=False (default) -> git push not called."""
        config = self._make_auto_push_config(tmp_path, auto_push=False)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_subprocess.return_value = MagicMock(returncode=0)

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.merge is not None
        assert result.merge["merged"] is True
        push_calls = [
            c
            for c in mock_subprocess.call_args_list
            if c.args and c.args[0][:3] == ["git", "push", "origin"]
        ]
        assert len(push_calls) == 0

    @patch("theforge.coordinator.workspace.subprocess.run")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_auto_push_failure_non_fatal(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_subprocess, tmp_path
    ):
        """auto_push=True + push fails -> warning logged, run still DONE."""
        import subprocess as _subprocess

        config = self._make_auto_push_config(tmp_path, auto_push=True)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_subprocess.side_effect = _subprocess.CalledProcessError(
            1, ["git", "push", "origin", "main"], stderr=b"auth error"
        )

        result = run_task(config, task, auto_merge=True)

        # Run still succeeds even though push failed
        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.merge is not None
        assert result.merge["merged"] is True
