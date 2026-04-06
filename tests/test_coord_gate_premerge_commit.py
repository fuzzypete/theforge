from __future__ import annotations

from pathlib import Path
from unittest.mock import call, patch

from theforge.coordinator.gate import _commit_dirty_worktree


class TestCommitDirtyWorktree:
    def test_dirty_tracked_commits_files(self, tmp_path: Path) -> None:
        with patch("theforge.coordinator.gate._cu._run_shell") as mock_shell:
            mock_shell.side_effect = [
                (True, " M src/app.py\nA  tests/test_app.py\nD  old.txt"),
                (True, ""),
                (True, "[feat] cleanup"),
            ]

            _commit_dirty_worktree(tmp_path)

        assert mock_shell.call_args_list == [
            call("git status --porcelain", tmp_path),
            call("git add -- src/app.py tests/test_app.py old.txt", tmp_path),
            call(
                'git commit -m "chore: commit remaining worktree changes before merge"',
                tmp_path,
            ),
        ]

    def test_clean_worktree_noop(self, tmp_path: Path) -> None:
        with patch(
            "theforge.coordinator.gate._cu._run_shell", return_value=(True, "")
        ) as mock_shell:
            _commit_dirty_worktree(tmp_path)

        mock_shell.assert_called_once_with("git status --porcelain", tmp_path)

    def test_untracked_only_warns_without_commit(self, tmp_path: Path) -> None:
        with (
            patch(
                "theforge.coordinator.gate._cu._run_shell",
                return_value=(True, "?? scratch.txt\n?? notes.md"),
            ) as mock_shell,
            patch("theforge.coordinator.gate._cu._log") as mock_log,
        ):
            _commit_dirty_worktree(tmp_path)

        mock_shell.assert_called_once_with("git status --porcelain", tmp_path)
        mock_log.assert_called_once()
        assert "untracked files remain in worktree" in mock_log.call_args.args[0]
        assert "scratch.txt" in mock_log.call_args.args[0]
