"""Tests for propagating Claude project memory into worktree subprocesses.

Claude Code stores memory under ~/.claude/projects/<encoded-cwd>/memory/.  A
worktree's cwd encodes to a different slug than the main project, so dev
subprocesses spawned in worktrees see no project memory unless we link the
two directories.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.workspace import (
    _claude_project_slug,
    _create_workspace,
    _propagate_claude_memory,
)


def _seed_main_memory(home: Path, project_root: Path, files: dict[str, str]) -> Path:
    main_dir = home / ".claude" / "projects" / _claude_project_slug(project_root) / "memory"
    main_dir.mkdir(parents=True)
    for name, content in files.items():
        (main_dir / name).write_text(content, encoding="utf-8")
    return main_dir


class TestPropagateClaudeMemoryHelper:
    def test_creates_symlink_to_main_memory(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        workspace = project_root / ".forge" / "worktrees" / "issue-99"
        workspace.mkdir(parents=True)

        main_memory = _seed_main_memory(home, project_root, {"MEMORY.md": "- index\n"})

        with patch("theforge.coordinator.workspace.Path.home", return_value=home):
            _propagate_claude_memory(project_root, workspace)

        link = home / ".claude" / "projects" / _claude_project_slug(workspace) / "memory"
        assert link.is_symlink()
        assert link.resolve() == main_memory.resolve()
        assert (link / "MEMORY.md").read_text(encoding="utf-8") == "- index\n"

    def test_idempotent_on_existing_correct_link(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        workspace = project_root / ".forge" / "worktrees" / "issue-99"
        workspace.mkdir(parents=True)
        main_memory = _seed_main_memory(home, project_root, {"a.md": "x"})

        with patch("theforge.coordinator.workspace.Path.home", return_value=home):
            _propagate_claude_memory(project_root, workspace)
            _propagate_claude_memory(project_root, workspace)

        link = home / ".claude" / "projects" / _claude_project_slug(workspace) / "memory"
        assert link.is_symlink()
        assert link.resolve() == main_memory.resolve()

    def test_replaces_stale_symlink(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        workspace = project_root / ".forge" / "worktrees" / "issue-99"
        workspace.mkdir(parents=True)
        main_memory = _seed_main_memory(home, project_root, {})

        wrong_target = tmp_path / "elsewhere"
        wrong_target.mkdir()
        worktree_proj = home / ".claude" / "projects" / _claude_project_slug(workspace)
        worktree_proj.mkdir(parents=True)
        stale_link = worktree_proj / "memory"
        stale_link.symlink_to(wrong_target, target_is_directory=True)

        with patch("theforge.coordinator.workspace.Path.home", return_value=home):
            _propagate_claude_memory(project_root, workspace)

        assert stale_link.is_symlink()
        assert stale_link.resolve() == main_memory.resolve()

    def test_does_not_clobber_real_directory(self, tmp_path):
        """If a real (non-symlink) memory dir already exists in the worktree slot,
        leave it alone — assume the operator has data we shouldn't trash."""
        home = tmp_path / "home"
        home.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        workspace = project_root / ".forge" / "worktrees" / "issue-99"
        workspace.mkdir(parents=True)
        _seed_main_memory(home, project_root, {})

        worktree_proj = home / ".claude" / "projects" / _claude_project_slug(workspace)
        worktree_proj.mkdir(parents=True)
        existing = worktree_proj / "memory"
        existing.mkdir()
        (existing / "operator.md").write_text("local data", encoding="utf-8")

        with patch("theforge.coordinator.workspace.Path.home", return_value=home):
            _propagate_claude_memory(project_root, workspace)

        assert not existing.is_symlink()
        assert (existing / "operator.md").read_text(encoding="utf-8") == "local data"

    def test_no_op_when_main_memory_missing(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude" / "projects").mkdir(parents=True)
        project_root = tmp_path / "project"
        project_root.mkdir()
        workspace = project_root / ".forge" / "worktrees" / "issue-99"
        workspace.mkdir(parents=True)

        with patch("theforge.coordinator.workspace.Path.home", return_value=home):
            _propagate_claude_memory(project_root, workspace)

        worktree_proj = home / ".claude" / "projects" / _claude_project_slug(workspace)
        # Should NOT have created a dangling memory link.
        assert not (worktree_proj / "memory").exists()

    def test_no_op_when_workspace_equals_project_root(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        _seed_main_memory(home, project_root, {"a.md": "x"})

        with patch("theforge.coordinator.workspace.Path.home", return_value=home):
            _propagate_claude_memory(project_root, project_root)

        # Main memory dir is untouched, no extra slugs created.
        slugs = list((home / ".claude" / "projects").iterdir())
        assert len(slugs) == 1


class TestCreateWorkspaceWiresPropagation:
    """Seam-level: _create_workspace must invoke memory propagation on every
    successful return path."""

    @patch("theforge.coordinator.workspace._propagate_claude_memory")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_fresh_path_propagates(self, _log, mock_shell, mock_propagate, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, config.workspace.base_branch)
            if "mkdir" in cmd:
                (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, _branch, err = _create_workspace(config, task, no_pull=True)

        assert err is None
        mock_propagate.assert_called_once_with(config.project_root, workspace_path)

    @patch("theforge.coordinator.workspace._is_stale_worktree", return_value=(False, "fresh"))
    @patch("theforge.coordinator.workspace._propagate_claude_memory")
    @patch("theforge.coordinator.workspace._cu._run_shell", return_value=(True, ""))
    @patch("theforge.coordinator.workspace._cu._log")
    def test_existing_worktree_path_propagates(
        self, _log, _shell, mock_propagate, _is_stale, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        # Pre-create the worktree path so we hit the "existing worktree" branch.
        existing = tmp_path / task.slug
        existing.mkdir()

        workspace_path, _branch, err = _create_workspace(config, task, no_pull=True)

        assert err is None
        assert workspace_path == existing
        mock_propagate.assert_called_once_with(config.project_root, existing)
