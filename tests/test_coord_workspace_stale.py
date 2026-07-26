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
    _as_detailed,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_task,
    _write_handoff,
    patch_gate_shell,
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

    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True
        assert "0 commits ahead" in info

    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is False
        assert "1 commit ahead" in info

    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is False
        assert "3 commits ahead" in info
        assert "stale" not in info

    @patch_gate_shell()
    def test_stale_worktree_days_zero_always_removes(self, mock_shell, tmp_path):
        """stale_worktree_days=0 -> always stale for clean, non-escalated worktrees."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=0)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "git status --porcelain" in cmd:
                return (True, "")
            return (True, "")

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True
        assert "stale_worktree_days=0" in info

    @patch_gate_shell()
    def test_escalated_worktree_not_stale(self, mock_shell, tmp_path):
        """Escalated worktrees are preserved even with 0 commits ahead."""
        workspace = tmp_path / "my-spec"
        marker = workspace / ".forge" / "escalated"
        marker.parent.mkdir(parents=True)
        marker.write_text("", encoding="utf-8")
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        mock_shell.return_value = (True, "feat/my-spec", 0, False)

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is False
        assert "escalate marker present" in info
        calls = [c[0][0] for c in mock_shell.call_args_list]
        assert not any("git status --porcelain" in c for c in calls)
        assert not any("git log" in c for c in calls)

    @patch_gate_shell()
    def test_dirty_worktree_not_stale(self, mock_shell, tmp_path):
        """Dirty worktrees are preserved even with 0 commits ahead."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "git status --porcelain" in cmd:
                return (True, " M src/app.py")
            raise AssertionError(f"unexpected command: {cmd}")

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is False
        assert "uncommitted changes present" in info

    @patch_gate_shell()
    def test_git_status_failure_not_stale(self, mock_shell, tmp_path):
        """git status failure -> not stale (cannot determine state, do not delete)."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "git status --porcelain" in cmd:
                return (False, "fatal: not a git repository")
            raise AssertionError(f"unexpected command: {cmd}")

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is False
        assert "Cannot determine worktree status" in info
        assert "git status failed" in info

    @patch_gate_shell()
    def test_stale_worktree_days_zero_preserves_escalated_worktree(self, mock_shell, tmp_path):
        """Escalated worktrees are preserved even when stale_worktree_days=0."""
        workspace = tmp_path / "my-spec"
        marker = workspace / ".forge" / "escalated"
        marker.parent.mkdir(parents=True)
        marker.write_text("", encoding="utf-8")
        config = _make_stale_config(tmp_path, stale_worktree_days=0)

        mock_shell.return_value = (True, "feat/my-spec", 0, False)

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is False
        assert "escalate marker present" in info

    @patch_gate_shell()
    def test_stale_branch_not_found(self, mock_shell, tmp_path):
        """Worktree dir exists but branch is gone (corrupted state) -> stale."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        mock_shell.return_value = (False, "fatal: not a git repository", 1, False)

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is True

    @patch_gate_shell()
    def test_git_log_failure_not_stale(self, mock_shell, tmp_path):
        """git log failure -> not stale (cannot determine state, do not delete)."""
        workspace = tmp_path / "my-spec"
        workspace.mkdir()
        config = _make_stale_config(tmp_path, stale_worktree_days=1)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/my-spec")
            if "--oneline" in cmd:
                return (False, "fatal: ambiguous argument 'main'")
            return (True, "")

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        is_stale, info = _is_stale_worktree(workspace, "main", config)
        assert is_stale is False
        assert "Cannot determine branch state" in info
        assert "git log failed" in info

    # -- _remove_worktree unit tests --

    @patch_gate_shell()
    def test_remove_worktree_logs_warning(self, mock_shell, tmp_path, capsys):
        """Warning is logged before removal."""
        mock_shell.return_value = (True, "", 0, False)

        _remove_worktree(tmp_path / "my-spec", "feat/my-spec", tmp_path)

        captured = capsys.readouterr()
        assert "stale worktree detected" in captured.err
        assert "feat/my-spec" in captured.err

    @patch_gate_shell()
    def test_remove_failure_does_not_raise(self, mock_shell, tmp_path, capsys):
        """git worktree remove failure is logged but does not raise."""
        mock_shell.return_value = (False, "error: not a git worktree", 1, False)

        # Must not raise
        _remove_worktree(tmp_path / "my-spec", "feat/my-spec", tmp_path)

        captured = capsys.readouterr()
        assert "Warning" in captured.err

    @patch_gate_shell()
    def test_remove_worktree_deletes_leftover_forge_shell(self, mock_shell, tmp_path):
        """Successful removal deletes a leftover worktree shell with only Forge metadata."""
        workspace = tmp_path / "my-spec"
        (workspace / ".forge" / "traces").mkdir(parents=True)
        (workspace / ".forge" / "traces" / "1-dev.txt").write_text("trace\n", encoding="utf-8")

        def shell_side_effect(cmd, cwd, **kwargs):
            if "git worktree remove" in cmd:
                return (True, "")
            if "git branch -D" in cmd:
                return (True, "")
            return (True, "")

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        _remove_worktree(workspace, "feat/my-spec", tmp_path)

        assert not workspace.exists()

    # -- Integration: _create_workspace stale detection --

    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        from theforge.coordinator.workspace import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace
        # The stale-check git log --oneline should NOT have been called (no existing worktree)
        for call in mock_shell.call_args_list:
            cmd_arg = call[0][0]
            assert "--oneline" not in cmd_arg

    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        from theforge.coordinator.workspace import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace

        # Verify git worktree remove was called
        calls = [c[0][0] for c in mock_shell.call_args_list]
        assert any("worktree remove" in c for c in calls)

    @patch_gate_shell()
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
            if "git status --porcelain" in cmd:
                return (True, "")
            if "--oneline" in cmd:
                return (True, "abc123 a commit")
            if "--format=%ct" in cmd:
                return (True, str(recent_ts))
            return (True, "")

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        from theforge.coordinator.workspace import _create_workspace

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace

        # Verify git worktree remove was NOT called
        calls = [c[0][0] for c in mock_shell.call_args_list]
        assert not any("worktree remove" in c for c in calls)
        assert not any("branch -D" in c for c in calls)

    @patch_gate_shell()
    def test_escalated_worktree_reused_on_create(self, mock_shell, tmp_path):
        """Existing escalated worktree is reused instead of being swept as stale."""
        config = _make_stale_config(tmp_path, stale_worktree_days=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        marker = workspace / ".forge" / "escalated"
        marker.parent.mkdir(parents=True)
        marker.write_text("", encoding="utf-8")

        mock_shell.return_value = (True, "feat/test-task", 0, False)

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace
        calls = [c[0][0] for c in mock_shell.call_args_list]
        assert not any("worktree remove" in c for c in calls)
        assert not any("branch -D" in c for c in calls)
        assert not any("git status --porcelain" in c for c in calls)
        assert not any("git log" in c for c in calls)

    @patch_gate_shell()
    def test_dirty_worktree_reused_on_create(self, mock_shell, tmp_path):
        """Existing dirty worktree is reused instead of being swept as stale."""
        config = _make_stale_config(tmp_path, stale_worktree_days=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/test-task")
            if "git status --porcelain" in cmd:
                return (True, "?? scratch.txt")
            # Reuse now runs the blocking base-branch sync, not a bare
            # behind-origin probe, so the fetch + ahead/behind delta appear here.
            if "rev-parse HEAD:src/theforge" in cmd:
                return (True, "treesha")
            if "git fetch origin main:main" in cmd:
                return (True, "")
            if "git rev-list --count origin/main..main" in cmd:
                return (True, "0")
            if "git rev-list --count main..origin/main" in cmd:
                return (True, "0")
            if "git rm -f --cached --ignore-unmatch" in cmd:
                return (True, "")
            raise AssertionError(f"unexpected command: {cmd}")

        mock_shell.side_effect = _as_detailed(shell_side_effect)

        path, branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace
        calls = [c[0][0] for c in mock_shell.call_args_list]
        assert not any("worktree remove" in c for c in calls)
        assert not any("branch -D" in c for c in calls)
        assert not any("git log" in c for c in calls)

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
    @patch_gate_shell()
    def test_conflict_resolution_succeeds(
        self, mock_shell, mock_agent, mock_preflight, mock_ws_agent, mock_pool, tmp_path
    ):
        """Merge conflict -> agent resolves -> gate passes -> merged=True."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(
            self._shell_with_conflict(workspace, conflicted_files=["src/foo.py"], gate_pass=True)
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
    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        # coord_workspace.run_agent is called for conflict resolution
        mock_ws_agent.return_value = _make_agent_result(success=True, output="Resolved conflicts.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        assert result.success is False  # landing failure → success=False
        assert result.phase == Phase.MERGE_FAILED
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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
        mock_shell.side_effect = _as_detailed(
            self._shell_with_conflict(workspace, conflicted_files=many_files)
        )
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, auto_merge=True)

        # Landing failed — success=False
        assert result.success is False
        assert result.phase == Phase.MERGE_FAILED
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(shell_side_effect)
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
    @patch_gate_shell()
    def test_conflict_resolution_timeout(
        self, mock_shell, mock_agent, mock_preflight, mock_ws_agent, mock_pool, tmp_path
    ):
        """Conflict resolution agent fails (simulating timeout) -> merge aborted."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _as_detailed(
            self._shell_with_conflict(workspace, conflicted_files=["src/foo.py"])
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

        assert result.success is False  # landing failure → success=False
        assert result.phase == Phase.MERGE_FAILED
        assert result.merge is not None
        assert result.merge["merged"] is False
        assert result.merge["error"] is not None


# ── Branch collision recovery ──────────────────────────────────────


class TestWorkspaceBranchCollision:
    """Test _create_workspace branch-collision recovery paths."""

    def _porcelain_for(self, wt_path: Path, branch: str) -> str:
        return f"worktree {wt_path}\nHEAD abc123\nbranch refs/heads/{branch}\n\n"

    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(side_effect)

        path, returned_branch, err = _create_workspace(config, task)

        assert err is None
        assert path == registered_wt
        assert returned_branch == branch
        captured = capsys.readouterr()
        assert "↻ WORKSPACE" in captured.err
        assert "reusing existing worktree (registered)" in captured.err

    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(side_effect)

        path, returned_branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace_path
        assert returned_branch == branch
        captured = capsys.readouterr()
        assert "⚠ WORKSPACE" in captured.err
        assert "pruning" in captured.err

    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(side_effect)

        path, returned_branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace_path
        assert returned_branch == branch
        captured = capsys.readouterr()
        assert "↻ WORKSPACE" in captured.err
        assert "reattaching worktree" in captured.err

    @patch_gate_shell()
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

        mock_shell.side_effect = _as_detailed(side_effect)

        path, returned_branch, err = _create_workspace(config, task)

        assert err is None
        assert path == workspace_path
        assert returned_branch == branch
        captured = capsys.readouterr()
        assert "⚠ WORKSPACE" in captured.err
        assert "stale branch" in captured.err


class TestRunSetupSplit:
    """Unit tests for _run_setup_split in workspace.py."""

    def test_verbatim_fallback_when_pattern_not_matched(self, tmp_path):
        """When command doesn't match the venv guard pattern, runs it verbatim."""
        from theforge.coordinator.workspace import _run_setup_split

        calls = []

        def fake_shell(cmd, cwd, **kw):
            calls.append(cmd)
            return (True, "ok")

        with patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell):
            ok, out = _run_setup_split("pip install -e .", tmp_path)

        assert ok is True
        assert calls == ["pip install -e ."]

    def test_venv_guard_pattern_splits_correctly(self, tmp_path):
        """When command matches venv guard, venv creation and install run separately."""
        from theforge.coordinator.workspace import _run_setup_split

        cmd = "test -d .venv || (python -m venv .venv && pip install -e .)"
        calls = []

        def fake_shell(cmd_arg, cwd, **kw):
            calls.append(cmd_arg)
            return (True, "ok")

        with patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell):
            ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        assert len(calls) == 2
        assert "test -d .venv" in calls[0]
        assert "python -m venv" in calls[0]
        # Second call is just the install command, not the venv creation
        assert "pip install -e ." in calls[1]
        assert "python -m venv" not in calls[1]

    def test_venv_creation_failure_stops_early(self, tmp_path):
        """If venv creation fails, the install command is not run."""
        from theforge.coordinator.workspace import _run_setup_split

        cmd = "test -d .venv || (python -m venv .venv && pip install -e .)"
        calls = []

        def fake_shell(cmd_arg, cwd, **kw):
            calls.append(cmd_arg)
            return (False, "venv error")

        with patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell):
            ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is False
        assert out == "venv error"
        assert len(calls) == 1  # install was not called

    def test_forge_python_placeholder_replaced_with_sys_executable(self, tmp_path):
        """{forge_python} in setup_command is replaced with sys.executable before running."""
        import sys

        from theforge.coordinator.workspace import _run_setup_split

        cmd = "test -d .venv || ({forge_python} -m venv .venv && pip install -e .)"
        calls = []

        def fake_shell(cmd_arg, cwd, **kw):
            calls.append(cmd_arg)
            return (True, "ok")

        with patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell):
            ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        assert len(calls) == 2
        # venv creation uses sys.executable, not the literal placeholder
        assert sys.executable in calls[0]
        assert "{forge_python}" not in calls[0]

    def test_resolve_setup_command_replaces_placeholder(self):
        """_resolve_setup_command swaps {forge_python} for the absolute interpreter path."""
        import sys

        from theforge.coordinator.workspace import _resolve_setup_command

        result = _resolve_setup_command("test -d .venv || ({forge_python} -m venv .venv)")
        assert sys.executable in result
        assert "{forge_python}" not in result

    def test_resolve_setup_command_noop_without_placeholder(self):
        """_resolve_setup_command is a no-op when {forge_python} is absent."""
        from theforge.coordinator.workspace import _resolve_setup_command

        cmd = "test -d .venv || (python -m venv .venv && pip install -e .)"
        assert _resolve_setup_command(cmd) == cmd

    def test_forge_python_with_spaces_in_path_is_shell_quoted(self, tmp_path):
        """sys.executable with spaces is shell-quoted; command is still split (not verbatim)."""
        from theforge.coordinator.workspace import _run_setup_split

        spaced_exe = "/home/my user/.pyenv/versions/3.12.12/bin/python3.12"
        cmd = "test -d .venv || ({forge_python} -m venv .venv && pip install -e .)"
        calls = []

        def fake_shell(cmd_arg, cwd, **kw):
            calls.append(cmd_arg)
            return (True, "ok")

        with (
            patch("theforge.coordinator.workspace.sys") as mock_sys,
            patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell),
        ):
            mock_sys.executable = spaced_exe
            ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        # Template matched before substitution — command was split, not run verbatim
        assert len(calls) == 2
        # The interpreter path must appear single-quoted (shlex.quote output)
        assert "'/home/my user/.pyenv/versions/3.12.12/bin/python3.12'" in calls[0]
        assert " -m venv" in calls[0]

    def test_forge_python_with_single_quote_in_path_still_splits(self, tmp_path):
        """Interpreter path containing a single quote is handled; command still splits."""
        import shlex

        from theforge.coordinator.workspace import _run_setup_split

        # A path with a single quote — shlex.quote produces a complex fragment that
        # cannot be matched by a simple regex, but we match before substitution so
        # the split must still occur.
        tricky_exe = "/tmp/has'quote/python3"
        cmd = "test -d .venv || ({forge_python} -m venv .venv && pip install -e .)"
        calls = []

        def fake_shell(cmd_arg, cwd, **kw):
            calls.append(cmd_arg)
            return (True, "ok")

        with (
            patch("theforge.coordinator.workspace.sys") as mock_sys,
            patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell),
        ):
            mock_sys.executable = tricky_exe
            ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        # Must have split into two calls, not fallen back to verbatim (one call)
        assert len(calls) == 2
        # The quoted token must appear in the venv creation command
        assert shlex.quote(tricky_exe) in calls[0]

    def test_resolve_setup_command_quotes_spaced_path(self):
        """_resolve_setup_command wraps a space-containing path in single quotes."""
        from theforge.coordinator.workspace import _resolve_setup_command

        spaced_exe = "/home/my user/.pyenv/bin/python3"
        with patch("theforge.coordinator.workspace.sys") as mock_sys:
            mock_sys.executable = spaced_exe
            result = _resolve_setup_command("{forge_python} -m venv .venv")

        assert "'/home/my user/.pyenv/bin/python3'" in result
        assert "{forge_python}" not in result
