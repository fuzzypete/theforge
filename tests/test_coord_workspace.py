"""Tests for workspace-related coordinator behaviour.

Extracted from test_coordinator.py: workspace failure, auto-merge, auto-push,
stale worktree detection, and conflict resolution.
"""

import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED,
    REQUEST_CHANGES_REVIEW,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_task,
    _preflight_then,
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
from theforge.coord_workspace import _create_workspace
from theforge.coordinator import (
    Phase,
    _is_stale_worktree,
    _remove_worktree,
    run_task,
)

# ── Workspace failure ─────────────────────────────────────────────


class TestCoordinatorWorkspaceFailure:
    """Test that workspace creation failure escalates immediately."""

    @patch("theforge.coord_util._run_shell")
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
                _write_handoff(Path(cwd), "PASS")
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

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_merge_success_on_approve(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=True: merge occurs after APPROVE, result.merge.merged is True."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace, merge_succeeds=True)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
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

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_merge_false_no_merge(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=False (default): no merge, result.merge is None."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=False)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.merge is not None
        assert result.merge["action"] == "none"

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_merge_no_merge_on_escalate(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=True: no merge when result is ESCALATE (not APPROVE)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented."),
            _make_agent_result(success=True, output="Fixed."),
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.merge is None  # no merge attempted on ESCALATE

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_merge_ff_fails_falls_back(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=True: non-ff fallback used when ff-only fails."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace, merge_succeeds=False)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.merge is not None
        assert result.merge["merged"] is True  # fell back to --no-edit and succeeded

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_merge_safety_no_base_branch(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=True: skips merge if base branch doesn't exist."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git branch --list" in cmd:
                return (True, "")  # base branch not found
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True  # run still succeeds
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None
        assert "not found" in result.merge["error"]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_merge_safety_dirty_project_root(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """auto_merge=True: skips merge if project root has uncommitted changes."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        dirty_seen = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
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
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True  # run still succeeds
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None
        assert "Uncommitted" in result.merge["error"]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_merge_safety_no_commits_ahead(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """auto_merge=True: skips merge if branch has no commits ahead of base."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
                return (True, "OK")
            if "git status --porcelain" in cmd:
                return (True, "")
            if "git branch --list" in cmd:
                return (True, "main")
            if "git log" in cmd and ".." in cmd:
                return (True, "")  # no commits ahead
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert "no commits" in result.merge["error"]

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_merge_merge_info_in_audit(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Merge info appears in audit log under 'merge' key."""
        from theforge.coordinator import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace, merge_succeeds=True)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
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

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_merge_false_no_merge_key_in_audit(
        self, mock_shell, mock_agent, mock_pool, tmp_path
    ):
        """Without auto_merge, audit 'merge' key is None."""
        from theforge.coordinator import generate_audit_log

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
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
                _write_handoff(Path(cwd), "PASS")
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

    @patch("theforge.coord_workspace.subprocess.run")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_push_after_merge(
        self, mock_shell, mock_agent, mock_pool, mock_subprocess, tmp_path
    ):
        """auto_push=True + merge success -> git push called with base_branch."""
        config = self._make_auto_push_config(tmp_path, auto_push=True)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
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

    @patch("theforge.coord_workspace.subprocess.run")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_push_disabled_by_default(
        self, mock_shell, mock_agent, mock_pool, mock_subprocess, tmp_path
    ):
        """auto_push=False (default) -> git push not called."""
        config = self._make_auto_push_config(tmp_path, auto_push=False)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
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

    @patch("theforge.coord_workspace.subprocess.run")
    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_auto_push_failure_non_fatal(
        self, mock_shell, mock_agent, mock_pool, mock_subprocess, tmp_path
    ):
        """auto_push=True + push fails -> warning logged, run still DONE."""
        import subprocess as _subprocess

        config = self._make_auto_push_config(tmp_path, auto_push=True)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_gate_and_merge(workspace)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
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


# ── Stale worktree tests ──────────────────────────────────────────


def _make_stale_config(tmp_path: Path, stale_worktree_days: int = 1) -> ForgeConfig:
    """Create a test ForgeConfig with a WorkspaceConfig that has stale_worktree_days."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
            base_branch="main",
            stale_worktree_days=stale_worktree_days,
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


class TestStaleWorktree:
    """Tests for stale worktree detection (R1-R6 from spec)."""

    # -- _is_stale_worktree unit tests --

    @patch("theforge.coord_util._run_shell")
    def test_stale_zero_commits_ahead(self, mock_shell, tmp_path):
        """Branch has 0 commits ahead of base -> stale."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "git log main..feat/my-spec --oneline" in cmd:
                return (True, "")  # empty -> 0 commits ahead
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True
        assert "0 commits ahead" in info

    @patch("theforge.coord_util._run_shell")
    def test_old_commit_not_stale(self, mock_shell, tmp_path):
        """Branch has commits ahead even if old -> NOT stale (age alone is no reason to delete)."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "--oneline" in cmd:
                return (True, "abc123 some commit")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is False
        assert "1 commit ahead" in info

    @patch("theforge.coord_util._run_shell")
    def test_fresh_worktree_reused(self, mock_shell, tmp_path):
        """Branch has commits ahead -> not stale (safe to reuse, age is irrelevant)."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "--oneline" in cmd:
                return (True, "abc123 commit one\ndef456 commit two\nghi789 commit three")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is False
        assert "3 commits ahead" in info
        assert "stale" not in info

    @patch("theforge.coord_util._run_shell")
    def test_stale_worktree_days_zero_always_removes(self, mock_shell, tmp_path):
        """stale_worktree_days=0 -> always stale regardless of commit state (CI/automated mode)."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=0)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True
        assert "stale_worktree_days=0" in info

    @patch("theforge.coord_util._run_shell")
    def test_stale_branch_not_found(self, mock_shell, tmp_path):
        """Worktree dir exists but branch is gone (corrupted state) -> stale."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        mock_shell.return_value = (False, "fatal: not a git repository")

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True

    # -- _remove_worktree unit tests --

    @patch("theforge.coord_util._run_shell")
    def test_remove_worktree_logs_warning(self, mock_shell, tmp_path, capsys):
        """Warning is logged before removal."""
        mock_shell.return_value = (True, "")

        _remove_worktree(tmp_path / "my-spec", "feat/my-spec", tmp_path)

        captured = capsys.readouterr()
        assert "stale worktree detected" in captured.err
        assert "feat/my-spec" in captured.err

    @patch("theforge.coord_util._run_shell")
    def test_remove_failure_does_not_raise(self, mock_shell, tmp_path, capsys):
        """git worktree remove failure is logged but does not raise."""
        mock_shell.return_value = (False, "error: not a git worktree")

        # Must not raise
        _remove_worktree(tmp_path / "my-spec", "feat/my-spec", tmp_path)

        captured = capsys.readouterr()
        assert "Warning" in captured.err

    # -- Integration: _create_workspace stale detection --

    @patch("theforge.coord_util._run_shell")
    def test_no_existing_worktree(self, mock_shell, tmp_path):
        """Path doesn't exist -> no staleness check, normal workspace creation."""
        config = _make_stale_config(tmp_path, stale_worktree_days=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug

        # Workspace does NOT exist initially
        assert not workspace.exists()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "mkdir" in cmd:
                workspace.mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        from theforge.coordinator import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace
        # rev-parse should NOT have been called (no stale check needed)
        for call in mock_shell.call_args_list:
            cmd_arg = call[0][0]
            assert "rev-parse" not in cmd_arg

    @patch("theforge.coord_util._run_shell")
    def test_stale_worktree_removed_on_create(self, mock_shell, tmp_path):
        """Stale worktree (0 commits ahead) is removed and workspace recreated."""
        config = _make_stale_config(tmp_path, stale_worktree_days=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()  # Pre-existing stale worktree

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/test-task")
            if "--oneline" in cmd:
                return (True, "")  # 0 commits ahead
            if "worktree remove" in cmd:
                # Simulate removal
                import shutil

                if workspace.exists():
                    shutil.rmtree(workspace)
                return (True, "")
            if "branch -D" in cmd:
                return (True, "")
            if "mkdir" in cmd:
                workspace.mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        from theforge.coordinator import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace

        # Verify git worktree remove was called
        calls = [c[0][0] for c in mock_shell.call_args_list]
        assert any("worktree remove" in c for c in calls)

    @patch("theforge.coord_util._run_shell")
    def test_fresh_worktree_not_removed(self, mock_shell, tmp_path):
        """Fresh worktree (recent commits ahead) is reused without removal."""
        config = _make_stale_config(tmp_path, stale_worktree_days=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()  # Pre-existing fresh worktree

        recent_ts = int(
            (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
            ).timestamp()
        )

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/test-task")
            if "--oneline" in cmd:
                return (True, "abc123 a commit")
            if "--format=%ct" in cmd:
                return (True, str(recent_ts))
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        from theforge.coordinator import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace

        # Verify git worktree remove was NOT called
        calls = [c[0][0] for c in mock_shell.call_args_list]
        assert not any("worktree remove" in c for c in calls)
        assert not any("branch -D" in c for c in calls)

    def test_stale_worktree_days_parsed_from_forge_yaml(self, tmp_path):
        """stale_worktree_days in forge.yaml is parsed into WorkspaceConfig."""
        from theforge.config import load_config

        config_file = tmp_path / "forge.yaml"
        config_file.write_text(
            """\
project: myproject
workspace:
  create_command: "git worktree add {slug}"
  path_pattern: ".forge/worktrees/{slug}"
  branch_pattern: "feat/{slug}"
  stale_worktree_days: 3
""",
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert cfg.workspace.stale_worktree_days == 3

    def test_stale_worktree_days_defaults_to_1(self, tmp_path):
        """stale_worktree_days defaults to 1 when not set in forge.yaml."""
        from theforge.config import load_config

        config_file = tmp_path / "forge.yaml"
        config_file.write_text("project: myproject\n", encoding="utf-8")
        cfg = load_config(config_file)
        assert cfg.workspace.stale_worktree_days == 1


# ── Conflict resolution tests ────────────────────────────────────


class TestConflictResolution:
    """Tests for auto-resolution of merge conflicts (R1-R7 from spec)."""

    def _shell_with_conflict(
        self,
        workspace: "Path",
        conflicted_files: list[str],
        gate_pass: bool = True,
        git_add_ok: bool = True,
        git_commit_ok: bool = True,
    ):
        """Shell side_effect: simulates a merge conflict scenario."""

        def side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                if gate_pass:
                    _write_handoff(Path(cwd), "PASS")
                return (gate_pass, "OK" if gate_pass else "FAIL")
            if "git status --porcelain" in cmd:
                return (True, "")  # clean
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
                return (False, "fatal: Not possible to fast-forward")
            if "git merge --no-edit" in cmd:
                return (False, "CONFLICT (content): Merge conflict")
            if "git diff --name-only --diff-filter=U" in cmd:
                return (True, "\n".join(conflicted_files))
            if "git add" in cmd:
                return (git_add_ok, "")
            if "git commit --no-edit" in cmd:
                return (git_commit_ok, "")
            if "git merge --abort" in cmd:
                return (True, "")
            if "git worktree remove" in cmd:
                return (True, "")
            return (True, "OK")

        return side_effect

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coord_workspace.run_agent")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_conflict_resolution_succeeds(
        self, mock_shell, mock_agent, mock_ws_agent, mock_pool, tmp_path
    ):
        """Merge conflict -> agent resolves -> gate passes -> merged=True."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_conflict(
            workspace, conflicted_files=["src/foo.py"], gate_pass=True
        )
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented."),  # dev
        )
        # coord_workspace.run_agent is called for conflict resolution
        mock_ws_agent.return_value = _make_agent_result(success=True, output="Resolved conflicts.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.merge is not None
        assert result.merge["merged"] is True
        assert result.merge["error"] is None

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coord_workspace.run_agent")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_conflict_resolution_gate_fails(
        self, mock_shell, mock_agent, mock_ws_agent, mock_pool, tmp_path
    ):
        """Merge conflict -> agent resolves -> gate fails -> merge aborted, merged=False."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # First gate call (VALIDATE phase): PASS; second gate call (conflict resolution): FAIL
        gate_calls = {"n": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                gate_calls["n"] += 1
                if gate_calls["n"] == 1:
                    # VALIDATE phase gate -> PASS so we get to REVIEW
                    _write_handoff(Path(cwd), "PASS")
                    return (True, "OK")
                else:
                    # Conflict resolution gate -> FAIL
                    return (False, "Tests failed")
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
                return (True, "Switched")
            if "git merge --ff-only" in cmd:
                return (False, "fatal: Not possible to fast-forward")
            if "git merge --no-edit" in cmd:
                return (False, "CONFLICT (content): Merge conflict")
            if "git diff --name-only --diff-filter=U" in cmd:
                return (True, "src/foo.py")
            if "git merge --abort" in cmd:
                return (True, "")
            if "git worktree remove" in cmd:
                return (True, "")
            return (True, "OK")

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented."),
        )
        # coord_workspace.run_agent is called for conflict resolution
        mock_ws_agent.return_value = _make_agent_result(success=True, output="Resolved conflicts.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True  # overall run succeeds even if merge failed
        assert result.phase == Phase.DONE
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_conflict_too_many_files_skipped(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """More than 5 conflicted files -> auto-resolution skipped, merge fails."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # 6 conflicted files -- over the limit of 5
        many_files = [f"src/file{i}.py" for i in range(6)]
        mock_shell.side_effect = self._shell_with_conflict(workspace, conflicted_files=many_files)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented.")
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        # Run succeeds overall but merge was not completed
        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_no_conflict_no_resolution(self, mock_shell, mock_agent, mock_pool, tmp_path):
        """Clean merge (no conflict) -> conflict resolution never invoked."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Fast-forward merge succeeds immediately -- no conflict
        def shell_side_effect(cmd, cwd, **kwargs):
            if "gate" in cmd:
                _write_handoff(Path(cwd), "PASS")
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
                return (True, "Switched")
            if "git merge --ff-only" in cmd:
                return (True, "Fast-forward")  # success -- no conflict
            if "git worktree remove" in cmd:
                return (True, "")
            return (True, "OK")

        agent_calls = {"n": 0}

        def agent_side_effect(**kwargs):
            agent_calls["n"] += 1
            if agent_calls["n"] == 1:
                return _make_agent_result(output=PREFLIGHT_PROCEED)  # preflight
            return _make_agent_result(output="Implemented.")  # dev

        mock_shell.side_effect = shell_side_effect
        mock_agent.side_effect = agent_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.merge is not None
        assert result.merge["merged"] is True
        # Only preflight + dev agent calls -- no conflict resolver call
        assert agent_calls["n"] == 2

    @patch("theforge.coordinator.run_agent_pool")
    @patch("theforge.coord_workspace.run_agent")
    @patch("theforge.coordinator.run_agent")
    @patch("theforge.coord_util._run_shell")
    def test_conflict_resolution_timeout(
        self, mock_shell, mock_agent, mock_ws_agent, mock_pool, tmp_path
    ):
        """Conflict resolution agent fails (simulating timeout) -> merge aborted."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_conflict(
            workspace, conflicted_files=["src/foo.py"]
        )
        # Preflight, then dev, then conflict resolution agent (fails with timeout)
        mock_agent.side_effect = _preflight_then(
            _make_agent_result(success=True, output="Implemented."),  # dev
            _make_agent_result(
                success=False, output="TIMEOUT after 120s"
            ),  # conflict resolver times out
        )
        # coord_workspace.run_agent is called for conflict resolution
        mock_ws_agent.return_value = _make_agent_result(success=False, output="TIMEOUT after 120s")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True  # overall run succeeds
        assert result.phase == Phase.DONE
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None


# ── Branch collision recovery ──────────────────────────────────────


class TestWorkspaceBranchCollision:
    """Test _create_workspace branch-collision recovery paths."""

    def _porcelain_for(self, wt_path: Path, branch: str) -> str:
        return f"worktree {wt_path}\nHEAD abc123\nbranch refs/heads/{branch}\n\n"

    @patch("theforge.coord_util._run_shell")
    def test_existing_worktree_directory_reused(self, mock_shell, tmp_path, capsys):
        """create_command fails, worktree is registered and directory exists → reuse."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        # workspace_path (path_pattern="{slug}") does NOT exist on disk — only the branch
        # is registered in git at a different (real) directory.
        registered_wt = tmp_path / "registered_wt"
        registered_wt.mkdir()  # the registered worktree directory exists

        branch = config.workspace.branch_pattern.format(slug=task.slug)

        def side_effect(cmd, cwd, **kwargs):
            if "mkdir" in cmd:
                return (False, "fatal: branch already exists")
            if "git branch --list" in cmd:
                return (True, f"  {branch}")
            if "git worktree list" in cmd:
                return (True, self._porcelain_for(registered_wt, branch))
            return (True, "")

        mock_shell.side_effect = side_effect

        path, returned_branch, err = _create_workspace(config, task)

        assert err is None
        assert path == registered_wt
        assert returned_branch == branch
        captured = capsys.readouterr()
        assert "↻ WORKSPACE" in captured.err
        assert "reusing existing worktree (registered)" in captured.err

    @patch("theforge.coord_util._run_shell")
    def test_missing_directory_pruned_and_recreated(self, mock_shell, tmp_path, capsys):
        """create_command fails, worktree registered but dir missing → prune then recreate."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace_path = tmp_path / task.slug
        branch = config.workspace.branch_pattern.format(slug=task.slug)
        call_count = {"mkdir": 0}

        def side_effect(cmd, cwd, **kwargs):
            if "mkdir" in cmd:
                if call_count["mkdir"] == 0:
                    call_count["mkdir"] += 1
                    return (False, "fatal: branch already exists")
                workspace_path.mkdir(parents=True, exist_ok=True)
                return (True, "")
            if "git branch --list" in cmd:
                return (True, f"  {branch}")
            if "git worktree list" in cmd:
                # worktree registered but directory does not exist (not created yet)
                return (True, self._porcelain_for(workspace_path, branch))
            if "git worktree prune" in cmd:
                return (True, "")
            if "git log" in cmd:
                return (True, "")  # 0 commits ahead
            if "git branch -D" in cmd:
                return (True, "")
            return (True, "")

        mock_shell.side_effect = side_effect

        path, returned_branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace_path
        assert returned_branch == branch
        captured = capsys.readouterr()
        assert "⚠ WORKSPACE" in captured.err
        assert "pruning" in captured.err

    @patch("theforge.coord_util._run_shell")
    def test_branch_with_commits_reattached(self, mock_shell, tmp_path, capsys):
        """create_command fails, no worktree registered, branch has commits → reattach."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace_path = tmp_path / task.slug
        branch = config.workspace.branch_pattern.format(slug=task.slug)

        def side_effect(cmd, cwd, **kwargs):
            if "mkdir" in cmd:
                return (False, "fatal: branch already exists")
            if "git branch --list" in cmd:
                return (True, f"  {branch}")
            if "git worktree list" in cmd:
                return (True, "worktree /main/path\nHEAD abc\nbranch refs/heads/main\n\n")
            if "git log" in cmd:
                return (True, "abc123 feat: some work\n")  # has commits
            if "git worktree add" in cmd and "-b" not in cmd:
                workspace_path.mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = side_effect

        path, returned_branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace_path
        assert returned_branch == branch
        captured = capsys.readouterr()
        assert "↻ WORKSPACE" in captured.err
        assert "reattaching worktree" in captured.err

    @patch("theforge.coord_util._run_shell")
    def test_stale_branch_deleted_and_recreated(self, mock_shell, tmp_path, capsys):
        """create_command fails, no worktree registered, 0 commits ahead → delete + recreate."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace_path = tmp_path / task.slug
        branch = config.workspace.branch_pattern.format(slug=task.slug)
        call_count = {"mkdir": 0}

        def side_effect(cmd, cwd, **kwargs):
            if "mkdir" in cmd:
                if call_count["mkdir"] == 0:
                    call_count["mkdir"] += 1
                    return (False, "fatal: branch already exists")
                workspace_path.mkdir(parents=True, exist_ok=True)
                return (True, "")
            if "git branch --list" in cmd:
                return (True, f"  {branch}")
            if "git worktree list" in cmd:
                return (True, "worktree /main/path\nHEAD abc\nbranch refs/heads/main\n\n")
            if "git log" in cmd:
                return (True, "")  # 0 commits ahead
            if "git branch -D" in cmd:
                return (True, "")
            return (True, "")

        mock_shell.side_effect = side_effect

        path, returned_branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace_path
        assert returned_branch == branch
        captured = capsys.readouterr()
        assert "⚠ WORKSPACE" in captured.err
        assert "stale branch" in captured.err
