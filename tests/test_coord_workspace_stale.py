"""Tests for stale worktree detection, conflict resolution, and branch collision recovery.

Extracted from test_coord_workspace.py.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_task,
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
from theforge.coordinator.workspace import _create_workspace, _is_stale_worktree, _remove_worktree

# ── Shared helper ─────────────────────────────────────────────────


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


# ── Stale worktree tests ──────────────────────────────────────────


class TestStaleWorktree:
    """Tests for stale worktree detection (R1-R6 from spec)."""

    # -- _is_stale_worktree unit tests --

    @patch("theforge.coordinator.util._run_shell")
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

    @patch("theforge.coordinator.util._run_shell")
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

    @patch("theforge.coordinator.util._run_shell")
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

    @patch("theforge.coordinator.util._run_shell")
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

    @patch("theforge.coordinator.util._run_shell")
    def test_stale_branch_not_found(self, mock_shell, tmp_path):
        """Worktree dir exists but branch is gone (corrupted state) -> stale."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        mock_shell.return_value = (False, "fatal: not a git repository")

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True

    # -- _remove_worktree unit tests --

    @patch("theforge.coordinator.util._run_shell")
    def test_remove_worktree_logs_warning(self, mock_shell, tmp_path, capsys):
        """Warning is logged before removal."""
        mock_shell.return_value = (True, "")

        _remove_worktree(tmp_path / "my-spec", "feat/my-spec", tmp_path)

        captured = capsys.readouterr()
        assert "stale worktree detected" in captured.err
        assert "feat/my-spec" in captured.err

    @patch("theforge.coordinator.util._run_shell")
    def test_remove_failure_does_not_raise(self, mock_shell, tmp_path, capsys):
        """git worktree remove failure is logged but does not raise."""
        mock_shell.return_value = (False, "error: not a git worktree")

        # Must not raise
        _remove_worktree(tmp_path / "my-spec", "feat/my-spec", tmp_path)

        captured = capsys.readouterr()
        assert "Warning" in captured.err

    # -- Integration: _create_workspace stale detection --

    @patch("theforge.coordinator.util._run_shell")
    def test_no_existing_worktree(self, mock_shell, tmp_path):
        """Path doesn't exist -> no staleness check, normal workspace creation."""
        config = _make_stale_config(tmp_path, stale_worktree_days=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug

        # Workspace does NOT exist initially
        assert not workspace.exists()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "main")  # project root is on base branch
            if "mkdir" in cmd:
                workspace.mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        from theforge.coordinator.workspace import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace
        # The stale-check git log --oneline should NOT have been called (no existing worktree)
        for call in mock_shell.call_args_list:
            cmd_arg = call[0][0]
            assert "--oneline" not in cmd_arg

    @patch("theforge.coordinator.util._run_shell")
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

        from theforge.coordinator.workspace import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace

        # Verify git worktree remove was called
        calls = [c[0][0] for c in mock_shell.call_args_list]
        assert any("worktree remove" in c for c in calls)

    @patch("theforge.coordinator.util._run_shell")
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

        from theforge.coordinator.workspace import _create_workspace

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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.workspace.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_conflict_resolution_succeeds(
        self, mock_shell, mock_agent, mock_preflight, mock_ws_agent, mock_pool, tmp_path
    ):
        """Merge conflict -> agent resolves -> gate passes -> merged=True."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_conflict(
            workspace, conflicted_files=["src/foo.py"], gate_pass=True
        )
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented."),
            # dev,
        ]
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.workspace.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_conflict_resolution_gate_fails(
        self, mock_shell, mock_agent, mock_preflight, mock_ws_agent, mock_pool, tmp_path
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
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_conflict_too_many_files_skipped(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """More than 5 conflicted files -> auto-resolution skipped, merge fails."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # 6 conflicted files -- over the limit of 5
        many_files = [f"src/file{i}.py" for i in range(6)]
        mock_shell.side_effect = self._shell_with_conflict(workspace, conflicted_files=many_files)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
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

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_no_conflict_no_resolution(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
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

        mock_shell.side_effect = shell_side_effect
        mock_preflight.return_value = _make_agent_result(output=PREFLIGHT_PROCEED)
        mock_agent.return_value = _make_agent_result(output="Implemented.")  # dev
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is True
        assert result.merge is not None
        assert result.merge["merged"] is True
        # Only dev agent call -- no conflict resolver call (preflight mocked separately)
        assert mock_agent.call_count == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.workspace.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_conflict_resolution_timeout(
        self, mock_shell, mock_agent, mock_preflight, mock_ws_agent, mock_pool, tmp_path
    ):
        """Conflict resolution agent fails (simulating timeout) -> merge aborted."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = self._shell_with_conflict(
            workspace, conflicted_files=["src/foo.py"]
        )
        # dev, then conflict resolution agent (fails with timeout); preflight mocked separately
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented."),  # dev
        ]
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

    @patch("theforge.coordinator.util._run_shell")
    def test_existing_worktree_directory_reused(self, mock_shell, tmp_path, capsys):
        """create_command fails, worktree is registered and directory exists -> reuse."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        # workspace_path (path_pattern="{slug}") does NOT exist on disk -- only the branch
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

    @patch("theforge.coordinator.util._run_shell")
    def test_missing_directory_pruned_and_recreated(self, mock_shell, tmp_path, capsys):
        """create_command fails, worktree registered but dir missing -> prune then recreate."""
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

    @patch("theforge.coordinator.util._run_shell")
    def test_branch_with_commits_reattached(self, mock_shell, tmp_path, capsys):
        """create_command fails, no worktree registered, branch has commits -> reattach."""
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

    @patch("theforge.coordinator.util._run_shell")
    def test_stale_branch_deleted_and_recreated(self, mock_shell, tmp_path, capsys):
        """create_command fails, no worktree registered, 0 commits ahead -> delete + recreate."""
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
