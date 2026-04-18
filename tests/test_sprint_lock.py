"""Tests for sprint concurrency guard (flock-based per-story locking)."""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.sprint.lock import (
    SprintConflictError,
    acquire_story_locks,
    check_active_worktrees,
    integration_lock,
    release_story_locks,
)

# Use 'fork' so local functions can be passed to child processes without pickling.
_mp = multiprocessing.get_context("fork")

# ── Unit tests for acquire_story_locks / release_story_locks ────────────


class TestAcquireStoryLocks:
    def test_empty_slugs_returns_empty(self, tmp_path: Path) -> None:
        """acquire_story_locks([]) returns ([], []) without creating any files."""
        fds, conflicted = acquire_story_locks([], tmp_path)
        assert fds == []
        assert conflicted == []

    def test_single_slug_acquires_lock(self, tmp_path: Path) -> None:
        """Acquiring a lock returns one fd and no conflicts."""
        fds, conflicted = acquire_story_locks(["my-story"], tmp_path)
        try:
            assert conflicted == []
            assert len(fds) == 1
            lock_path = tmp_path / ".forge" / "locks" / "my-story.lock"
            assert lock_path.read_text(encoding="utf-8").strip().isdigit()
        finally:
            release_story_locks(fds)

    def test_lock_dir_created_automatically(self, tmp_path: Path) -> None:
        """The .forge/locks/ directory is created if it doesn't exist."""
        lock_dir = tmp_path / ".forge" / "locks"
        assert not lock_dir.exists()
        fds, _ = acquire_story_locks(["story-a"], tmp_path)
        try:
            assert lock_dir.exists()
        finally:
            release_story_locks(fds)

    def test_multiple_slugs_acquired(self, tmp_path: Path) -> None:
        """All slugs in the list get locked when none conflict."""
        slugs = ["story-a", "story-b", "story-c"]
        fds, conflicted = acquire_story_locks(slugs, tmp_path)
        try:
            assert conflicted == []
            assert len(fds) == len(slugs)
        finally:
            release_story_locks(fds)

    def test_conflict_detected_from_child_process(self, tmp_path: Path) -> None:
        """acquire_story_locks detects a lock held by another process."""

        # Hold a lock in a child process and signal the parent to check
        ready_event = _mp.Event()
        release_event = _mp.Event()

        def hold_lock(path: str, slug: str, ready: object, release: object) -> None:
            fds, conflicted = acquire_story_locks([slug], Path(path))
            if conflicted:
                sys.exit(1)
            ready.set()
            release.wait(timeout=10)
            release_story_locks(fds)

        proc = _mp.Process(
            target=hold_lock,
            args=(str(tmp_path), "story-x", ready_event, release_event),
        )
        proc.start()
        try:
            assert ready_event.wait(timeout=5), "child did not acquire lock in time"
            fds, conflicted = acquire_story_locks(["story-x"], tmp_path)
            try:
                assert conflicted == ["story-x"]
                assert fds == []
            finally:
                release_story_locks(fds)
        finally:
            release_event.set()
            proc.join(timeout=5)

    def test_conflict_releases_all_acquired_locks(self, tmp_path: Path) -> None:
        """When one slug conflicts, already-acquired locks are released."""
        ready_event = _mp.Event()
        release_event = _mp.Event()

        def hold_lock(path: str, slug: str, ready: object, release: object) -> None:
            fds, _ = acquire_story_locks([slug], Path(path))
            ready.set()
            release.wait(timeout=10)
            release_story_locks(fds)

        proc = _mp.Process(
            target=hold_lock,
            args=(str(tmp_path), "story-b", ready_event, release_event),
        )
        proc.start()
        try:
            assert ready_event.wait(timeout=5)
            # story-a would succeed, story-b conflicts — expect full rollback
            fds, conflicted = acquire_story_locks(["story-a", "story-b"], tmp_path)
            try:
                assert conflicted == ["story-b"]
                assert fds == []
            finally:
                release_story_locks(fds)

            # story-a lock must have been released — re-acquire should succeed
            fds2, conflicted2 = acquire_story_locks(["story-a"], tmp_path)
            try:
                assert conflicted2 == []
                assert len(fds2) == 1
            finally:
                release_story_locks(fds2)
        finally:
            release_event.set()
            proc.join(timeout=5)

    def test_release_allows_reacquire(self, tmp_path: Path) -> None:
        """release_story_locks frees the lock so a subsequent acquire succeeds."""
        fds, _ = acquire_story_locks(["story-y"], tmp_path)
        release_story_locks(fds)

        fds2, conflicted = acquire_story_locks(["story-y"], tmp_path)
        try:
            assert conflicted == []
            assert len(fds2) == 1
        finally:
            release_story_locks(fds2)

    def test_stale_pid_lock_is_removed_and_retried(self, tmp_path: Path) -> None:
        """A conflicting lock with a dead PID is removed and retried."""
        lock_path = tmp_path / ".forge" / "locks" / "story-z.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("424242", encoding="utf-8")

        flock_calls = []

        def fake_flock(_fd, _flags):
            flock_calls.append(1)
            if len(flock_calls) == 1:
                raise BlockingIOError
            return None

        with (
            patch("theforge.sprint.lock.fcntl.flock", side_effect=fake_flock),
            patch("theforge.sprint.lock.os.kill", side_effect=ProcessLookupError),
        ):
            fds, conflicted = acquire_story_locks(["story-z"], tmp_path)

        try:
            assert conflicted == []
            assert len(fds) == 1
            assert len(flock_calls) == 2
            assert lock_path.read_text(encoding="utf-8").strip().isdigit()
        finally:
            release_story_locks(fds)

    def test_empty_conflicting_lock_is_not_treated_as_stale(self, tmp_path: Path) -> None:
        """An empty lock file under active contention is reported as a conflict."""
        lock_path = tmp_path / ".forge" / "locks" / "story-empty.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("", encoding="utf-8")

        with patch("theforge.sprint.lock.fcntl.flock", side_effect=BlockingIOError):
            fds, conflicted = acquire_story_locks(["story-empty"], tmp_path)

        try:
            assert fds == []
            assert conflicted == ["story-empty"]
            assert lock_path.exists()
            assert lock_path.read_text(encoding="utf-8") == ""
        finally:
            release_story_locks(fds)


class TestCheckActiveWorktrees:
    def test_missing_worktree_is_not_active(self, tmp_path: Path) -> None:
        active = check_active_worktrees(["story-a"], ".forge/worktrees/{slug}", "main", tmp_path)
        assert active == []

    def test_existing_worktree_with_commits_is_active(self, tmp_path: Path) -> None:
        worktree = tmp_path / ".forge" / "worktrees" / "story-a"
        worktree.mkdir(parents=True)

        completed = MagicMock(returncode=0, stdout="3\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed) as mock_run:
            active = check_active_worktrees(
                ["story-a"], ".forge/worktrees/{slug}", "main", tmp_path
            )

        assert active == ["story-a"]
        mock_run.assert_called_once()

    def test_existing_worktree_without_commits_is_not_active(self, tmp_path: Path) -> None:
        worktree = tmp_path / ".forge" / "worktrees" / "story-a"
        worktree.mkdir(parents=True)

        completed = MagicMock(returncode=0, stdout="0\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            active = check_active_worktrees(
                ["story-a"], ".forge/worktrees/{slug}", "main", tmp_path
            )

        assert active == []

    def test_escalated_worktree_is_not_active(self, tmp_path: Path) -> None:
        """Worktrees with final_phase == ESCALATE are preserved, not collisions."""
        import yaml as _yaml

        worktree = tmp_path / ".forge" / "worktrees" / "story-a"
        (worktree / ".forge").mkdir(parents=True)
        (worktree / ".forge" / "audit.yaml").write_text(
            _yaml.dump({"outcome": {"final_phase": "ESCALATE"}}),
            encoding="utf-8",
        )
        # Worktree has commits ahead — git check would report "active" if reached.
        completed = MagicMock(returncode=0, stdout="5\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed) as mock_run:
            active = check_active_worktrees(
                ["story-a"], ".forge/worktrees/{slug}", "main", tmp_path
            )

        assert active == []
        # Short-circuit: git rev-list must not have been invoked for escalated wt.
        mock_run.assert_not_called()

    def test_non_escalated_audit_is_still_active(self, tmp_path: Path) -> None:
        """Worktrees whose audit shows DONE (or any non-ESCALATE phase) still count."""
        import yaml as _yaml

        worktree = tmp_path / ".forge" / "worktrees" / "story-a"
        (worktree / ".forge").mkdir(parents=True)
        (worktree / ".forge" / "audit.yaml").write_text(
            _yaml.dump({"outcome": {"final_phase": "DEV"}}),
            encoding="utf-8",
        )
        completed = MagicMock(returncode=0, stdout="3\n")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            active = check_active_worktrees(
                ["story-a"], ".forge/worktrees/{slug}", "main", tmp_path
            )

        assert active == ["story-a"]

    def test_git_failure_is_not_active(self, tmp_path: Path) -> None:
        worktree = tmp_path / ".forge" / "worktrees" / "story-a"
        worktree.mkdir(parents=True)

        completed = MagicMock(returncode=1, stdout="", stderr="fatal")
        with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
            active = check_active_worktrees(
                ["story-a"], ".forge/worktrees/{slug}", "main", tmp_path
            )

        assert active == []


# ── SprintConflictError ──────────────────────────────────────────────────


class TestSprintConflictError:
    def test_carries_conflicting_slugs(self) -> None:
        slugs = ["alpha", "beta"]
        exc = SprintConflictError(slugs)
        assert exc.conflicting_slugs == slugs

    def test_message_contains_slugs(self) -> None:
        exc = SprintConflictError(["foo", "bar"])
        msg = str(exc)
        assert "foo" in msg
        assert "bar" in msg


class TestIntegrationLock:
    def test_timeout_reports_owner_pid(self, tmp_path: Path) -> None:
        """integration_lock times out instead of blocking forever."""
        lock_path = tmp_path / ".forge" / "merge.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("424242", encoding="utf-8")

        monotonic_values = iter([0.0, 0.0, 0.11])

        with (
            patch("theforge.sprint.lock.fcntl.flock", side_effect=BlockingIOError),
            patch(
                "theforge.sprint.lock.time.monotonic", side_effect=lambda: next(monotonic_values)
            ),
            patch("theforge.sprint.lock.time.sleep"),
        ):
            with patch("theforge.sprint.lock._read_lock_pid", return_value=424242):
                with pytest.raises(TimeoutError, match="held by pid 424242"):
                    with integration_lock(
                        tmp_path,
                        timeout_seconds=0.1,
                        poll_interval_seconds=0.01,
                    ):
                        pass


# ── Integration: cmd_sprint conflict check ───────────────────────────────


class TestCmdSprintConflictGuard:
    """cmd_sprint returns exit code 1 and prints conflicting slugs when locked."""

    def _make_story(self, tmp_path: Path, slug: str) -> Path:
        story = tmp_path / f"{slug}.md"
        story.write_text(f"---\nslug: {slug}\nname: {slug}\n---\n# {slug}\n", encoding="utf-8")
        return story

    def _make_manifest(self, tmp_path: Path, *story_paths: Path) -> Path:
        manifest = tmp_path / "sprint.yaml"
        rel_paths = [str(p.relative_to(tmp_path)) for p in story_paths]
        lines = ["name: test-sprint", "budget_usd: 5.0", "stories:"]
        lines += [f"  - {p}" for p in rel_paths]
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return manifest

    def _make_args(self, tmp_path: Path, manifest: Path) -> argparse.Namespace:
        forge_yaml = tmp_path / "forge.yaml"
        if not forge_yaml.exists():
            forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
        return argparse.Namespace(
            manifest=str(manifest),
            config=str(forge_yaml),
            fg=True,
            detach=False,
            resume=False,
            auto_merge=False,
            interactive=False,
            verbose=False,
            no_notify=True,
        )

    def test_conflict_returns_exit_1(self, tmp_path: Path, capsys) -> None:
        """cmd_sprint returns 1 when a story lock is already held."""
        from theforge import cli

        story = self._make_story(tmp_path, "my-feature")
        manifest = self._make_manifest(tmp_path, story)
        args = self._make_args(tmp_path, manifest)

        mock_config = MagicMock()
        mock_config.project_root = tmp_path
        mock_config.workspace.path_pattern = ".forge/worktrees/{slug}"
        mock_config.workspace.base_branch = "main"

        ready_event = _mp.Event()
        release_event = _mp.Event()

        def hold_lock(path: str, slug: str, ready: object, release: object) -> None:
            fds, _ = acquire_story_locks([slug], Path(path))
            ready.set()
            release.wait(timeout=10)
            release_story_locks(fds)

        proc = _mp.Process(
            target=hold_lock,
            args=(str(tmp_path), "my-feature", ready_event, release_event),
        )
        proc.start()
        try:
            assert ready_event.wait(timeout=5)

            with patch("theforge.cli.sprint.load_config", return_value=mock_config):
                with patch("theforge.cli.sprint.run_sprint") as mock_run:
                    rc = cli.cmd_sprint(args)

            assert rc == 1
            mock_run.assert_not_called()
            captured = capsys.readouterr()
            assert "my-feature" in captured.err
        finally:
            release_event.set()
            proc.join(timeout=5)

    def test_no_conflict_calls_run_sprint(self, tmp_path: Path) -> None:
        """cmd_sprint proceeds to run_sprint when no conflicts exist."""
        from theforge import cli

        story = self._make_story(tmp_path, "clean-story")
        manifest = self._make_manifest(tmp_path, story)
        args = self._make_args(tmp_path, manifest)

        mock_config = MagicMock()
        mock_config.project_root = tmp_path
        mock_config.workspace.path_pattern = ".forge/worktrees/{slug}"
        mock_config.workspace.base_branch = "main"

        mock_result = MagicMock()
        mock_result.specs_failed = 0

        with patch("theforge.cli.sprint.load_config", return_value=mock_config):
            with patch("theforge.cli.sprint.run_sprint", return_value=mock_result) as mock_run:
                with patch("theforge.detach.remove_pid"):
                    rc = cli.cmd_sprint(args)

        assert rc == 0
        mock_run.assert_called_once()

    def test_active_worktree_guard_runs_before_lock_acquisition(self, tmp_path: Path) -> None:
        """Active worktrees abort launch before per-story locks are attempted."""
        from theforge import cli

        story = self._make_story(tmp_path, "busy-story")
        manifest = self._make_manifest(tmp_path, story)
        args = self._make_args(tmp_path, manifest)

        mock_config = MagicMock()
        mock_config.project_root = tmp_path
        mock_config.workspace.path_pattern = ".forge/worktrees/{slug}"
        mock_config.workspace.base_branch = "main"

        worktree = tmp_path / ".forge" / "worktrees" / "busy-story"
        worktree.mkdir(parents=True)

        with patch("theforge.cli.sprint.load_config", return_value=mock_config):
            with patch("theforge.sprint.launch_guard.acquire_story_locks") as mock_locks:
                with patch(
                    "theforge.sprint.lock.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="1\n"),
                ):
                    rc = cli.cmd_sprint(args)

        assert rc == 1
        mock_locks.assert_not_called()

    def test_active_worktree_returns_exit_1(self, tmp_path: Path, capsys) -> None:
        """cmd_sprint returns 1 when a story already has an active worktree."""
        from theforge import cli

        story = self._make_story(tmp_path, "my-feature")
        manifest = self._make_manifest(tmp_path, story)
        args = self._make_args(tmp_path, manifest)

        mock_config = MagicMock()
        mock_config.project_root = tmp_path
        mock_config.workspace.path_pattern = ".forge/worktrees/{slug}"
        mock_config.workspace.base_branch = "main"

        worktree = tmp_path / ".forge" / "worktrees" / "my-feature"
        worktree.mkdir(parents=True)

        with patch("theforge.cli.sprint.load_config", return_value=mock_config):
            with patch("theforge.cli.sprint.run_sprint") as mock_run:
                with patch(
                    "theforge.sprint.lock.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="2\n"),
                ):
                    rc = cli.cmd_sprint(args)

        assert rc == 1
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "Stories already have active worktrees" in captured.err
        assert "my-feature" in captured.err

    def test_resume_skips_active_worktree_guard(self, tmp_path: Path) -> None:
        """cmd_sprint allows resume runs even when the worktree is active."""
        from theforge import cli

        story = self._make_story(tmp_path, "resume-story")
        manifest = self._make_manifest(tmp_path, story)
        args = self._make_args(tmp_path, manifest)
        args.resume = True

        mock_config = MagicMock()
        mock_config.project_root = tmp_path
        mock_config.workspace.path_pattern = ".forge/worktrees/{slug}"
        mock_config.workspace.base_branch = "main"

        worktree = tmp_path / ".forge" / "worktrees" / "resume-story"
        worktree.mkdir(parents=True)

        mock_result = MagicMock()
        mock_result.specs_failed = 0

        with patch("theforge.cli.sprint.load_config", return_value=mock_config):
            with patch("theforge.cli.sprint.run_sprint", return_value=mock_result) as mock_run:
                with patch("theforge.sprint.lock.subprocess.run") as mock_git:
                    with patch("theforge.detach.remove_pid"):
                        rc = cli.cmd_sprint(args)

        assert rc == 0
        mock_run.assert_called_once()
        mock_git.assert_not_called()
