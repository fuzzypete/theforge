"""Regression tests for issue #835: re-exec must not silently drop stories.

Covers two behaviors:

1. Escalated worktrees are recognized by their state metadata and are not
   treated as active-worktree collisions during re-exec.
2. A genuine active-worktree collision after re-exec does not abort the whole
   sprint — the conflicted story is marked failed, visibly, and the remaining
   stories continue.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from theforge.sprint.launch_guard import acquire_launch_story_locks


def _mock_config(tmp_path: Path) -> MagicMock:
    config = MagicMock()
    config.project_root = tmp_path
    config.workspace.path_pattern = ".forge/worktrees/{slug}"
    config.workspace.base_branch = "main"
    return config


def _make_worktree_with_audit(tmp_path: Path, slug: str, final_phase: str | None = None) -> Path:
    worktree = tmp_path / ".forge" / "worktrees" / slug
    worktree.mkdir(parents=True)
    if final_phase is not None:
        (worktree / ".forge").mkdir(parents=True, exist_ok=True)
        (worktree / ".forge" / "audit.yaml").write_text(
            yaml.dump({"outcome": {"final_phase": final_phase}}),
            encoding="utf-8",
        )
    return worktree


class TestEscalatedWorktreeNotTreatedAsCollision:
    def test_escalated_worktree_passes_launch_guard(self, tmp_path: Path) -> None:
        """An escalated worktree with committed work does NOT block launch."""
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase="ESCALATE")
        config = _mock_config(tmp_path)

        # Even if git rev-list would report commits ahead, escalated marker wins.
        completed = MagicMock(returncode=0, stdout="7\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829"],
                config=config,
                resume=False,
                allow_drop=False,
            )

        assert launch_error is None
        assert dropped == {}
        # Locks were acquired because the worktree was treated as preserved.
        assert len(locked_fds) == 1
        from theforge.sprint.lock import release_story_locks

        release_story_locks(locked_fds)

    def test_reexec_with_escalated_and_pending_story(self, tmp_path: Path) -> None:
        """Scenario from the bug: three stories, middle escalated, third must run.

        Simulates the post-re-exec launch guard path: ``slugs`` is the full
        sprint slug list; one of them has an escalated worktree; no drop should
        occur and all locks should be acquired.
        """
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase="ESCALATE")
        config = _mock_config(tmp_path)

        # issue-267 and issue-268 have no worktree, issue-829 has escalated one.
        completed = MagicMock(returncode=0, stdout="0\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829", "issue-267", "issue-268"],
                config=config,
                resume=False,
                allow_drop=True,
            )

        try:
            assert launch_error is None, "re-exec must not abort when only collision is escalated"
            assert dropped == {}
            assert len(locked_fds) == 3
        finally:
            from theforge.sprint.lock import release_story_locks

            release_story_locks(locked_fds)


class TestGenuineCollisionDuringReexec:
    def test_reexec_active_worktree_drops_only_conflicted_story(
        self, tmp_path: Path, capsys
    ) -> None:
        """A truly active (non-escalated) worktree is dropped, not an abort."""
        # issue-829 has no audit.yaml -> treated as active (ahead commits).
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase=None)
        config = _mock_config(tmp_path)

        completed = MagicMock(returncode=0, stdout="3\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829", "issue-267", "issue-268"],
                config=config,
                resume=False,
                allow_drop=True,
            )

        try:
            assert launch_error is None, "re-exec must not abort entire sprint"
            assert "issue-829" in dropped
            assert "issue-267" not in dropped
            assert "issue-268" not in dropped
            # Remaining stories acquired locks.
            assert len(locked_fds) == 2

            # Visibility: the drop is loud, not silent.
            captured = capsys.readouterr()
            assert "DROPPED" in captured.err
            assert "issue-829" in captured.err
        finally:
            from theforge.sprint.lock import release_story_locks

            release_story_locks(locked_fds)

    def test_initial_launch_still_aborts_on_active_worktree(self, tmp_path: Path) -> None:
        """Without re-exec (allow_drop=False) the legacy abort behavior is preserved."""
        _make_worktree_with_audit(tmp_path, "issue-829", final_phase=None)
        config = _mock_config(tmp_path)

        completed = MagicMock(returncode=0, stdout="3\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            locked_fds, launch_error, dropped = acquire_launch_story_locks(
                slugs=["issue-829", "issue-267"],
                config=config,
                resume=False,
                allow_drop=False,
            )

        assert launch_error == 1
        assert locked_fds == []
        assert dropped == {}


class TestCliReexecDetection:
    def test_reexec_detected_when_prev_run_id_env_set(self, monkeypatch) -> None:
        from theforge.cli.sprint import _is_reexec

        monkeypatch.setenv("FORGE_PREV_RUN_ID", "run-prev-123")
        assert _is_reexec() is True

    def test_reexec_not_detected_without_env(self, monkeypatch) -> None:
        from theforge.cli.sprint import _is_reexec

        monkeypatch.delenv("FORGE_PREV_RUN_ID", raising=False)
        assert _is_reexec() is False
