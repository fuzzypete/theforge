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
    _read_lock_metadata,
    acquire_story_locks,
    check_active_worktrees,
    cleanup_story_locks,
    integration_lock,
    release_story_locks,
    sweep_story_locks,
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
            pid_text, timestamp, fingerprint = (
                lock_path.read_text(encoding="utf-8").strip().split("|", 2)
            )
            assert pid_text.isdigit()
            assert timestamp
            assert fingerprint
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
        lock_path.write_text("424242|old-start", encoding="utf-8")

        flock_calls = []

        def fake_flock(_fd, _flags):
            flock_calls.append(1)
            if len(flock_calls) == 1:
                raise BlockingIOError
            return None

        with (
            patch("theforge.sprint.lock.fcntl.flock", side_effect=fake_flock),
            patch("theforge.sprint.lock._pid_matches_fingerprint", return_value=False),
            patch("theforge.sprint.lock._current_process_fingerprint", return_value="new-start"),
        ):
            fds, conflicted = acquire_story_locks(["story-z"], tmp_path)

        try:
            assert conflicted == []
            assert len(fds) == 1
            assert len(flock_calls) == 2
            pid, fingerprint = _read_lock_metadata(fds[0])
            assert pid is not None
            assert fingerprint == "new-start"
        finally:
            release_story_locks(fds)

    def test_recycled_pid_lock_is_removed_and_retried(self, tmp_path: Path) -> None:
        """A lock whose PID is alive but fingerprint changed is treated as stale."""
        lock_path = tmp_path / ".forge" / "locks" / "story-recycled.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("424242|old-start", encoding="utf-8")

        flock_calls = []

        def fake_flock(_fd, _flags):
            flock_calls.append(1)
            if len(flock_calls) == 1:
                raise BlockingIOError
            return None

        with (
            patch("theforge.sprint.lock.fcntl.flock", side_effect=fake_flock),
            patch("theforge.sprint.lock._pid_matches_fingerprint", return_value=False),
            patch("theforge.sprint.lock._current_process_fingerprint", return_value="new-start"),
        ):
            fds, conflicted = acquire_story_locks(["story-recycled"], tmp_path)

        try:
            assert conflicted == []
            assert len(fds) == 1
            assert len(flock_calls) == 2
            assert lock_path.read_text(encoding="utf-8").strip().endswith("|new-start")
        finally:
            release_story_locks(fds)

    def test_live_matching_pid_lock_remains_conflict(self, tmp_path: Path) -> None:
        """A lock owned by the same live process instance must remain a conflict."""
        lock_path = tmp_path / ".forge" / "locks" / "story-live.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("424242|same-start", encoding="utf-8")

        with (
            patch("theforge.sprint.lock.fcntl.flock", side_effect=BlockingIOError),
            patch("theforge.sprint.lock._pid_matches_fingerprint", return_value=True),
        ):
            fds, conflicted = acquire_story_locks(["story-live"], tmp_path)

        try:
            assert fds == []
            assert conflicted == ["story-live"]
            assert lock_path.read_text(encoding="utf-8") == "424242|same-start"
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


class TestCleanupStoryLocks:
    def test_removes_matching_pid_lock(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".forge" / "locks" / "story-a.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("424242|same-start\n", encoding="utf-8")

        with patch("theforge.sprint.lock._pid_matches_fingerprint", return_value=True):
            cleaned = cleanup_story_locks(["story-a"], tmp_path, pid=424242)

        assert cleaned == ["story-a"]
        assert not lock_path.exists()

    def test_removes_dead_matching_pid_lock_without_fingerprint_match(
        self, tmp_path: Path
    ) -> None:
        lock_path = tmp_path / ".forge" / "locks" / "story-a.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("424242|old-start\n", encoding="utf-8")

        with (
            patch("theforge.detach._is_pid_alive", return_value=False),
            patch("theforge.sprint.lock._pid_matches_fingerprint", return_value=False),
        ):
            cleaned = cleanup_story_locks(["story-a"], tmp_path, pid=424242)

        assert cleaned == ["story-a"]
        assert not lock_path.exists()

    def test_keeps_live_matching_pid_with_different_fingerprint_lock(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".forge" / "locks" / "story-a.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("424242|other-start\n", encoding="utf-8")

        with (
            patch("theforge.detach._is_pid_alive", return_value=True),
            patch("theforge.sprint.lock._pid_matches_fingerprint", return_value=False),
        ):
            cleaned = cleanup_story_locks(["story-a"], tmp_path, pid=424242)

        assert cleaned == []
        assert lock_path.exists()


class TestSweepStoryLocks:
    def test_removes_unlocked_stale_lock_files(self, tmp_path: Path) -> None:
        lock_dir = tmp_path / ".forge" / "locks"
        lock_dir.mkdir(parents=True)
        stale_lock = lock_dir / "issue-1110.lock"
        stale_lock.write_text("40858|Sat May  2 15:49:34 2026|old-start\n", encoding="utf-8")

        removed = sweep_story_locks(tmp_path)

        assert removed == [stale_lock]
        assert not stale_lock.exists()

    def test_keeps_live_locked_files(self, tmp_path: Path) -> None:
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
            args=(str(tmp_path), "issue-1111", ready_event, release_event),
        )
        proc.start()
        try:
            assert ready_event.wait(timeout=5)
            removed = sweep_story_locks(tmp_path)
            assert removed == []
            assert (tmp_path / ".forge" / "locks" / "issue-1111.lock").exists()
        finally:
            release_event.set()
            proc.join(timeout=5)

    def test_unlink_runs_while_flock_still_held(self, tmp_path: Path) -> None:
        """Regression for issue-1264: sweep must hold the flock across unlink.

        If the flock is dropped before the file is unlinked, a concurrent
        sprint can acquire the about-to-be-deleted lock — the exact race the
        sweep is supposed to close.
        """
        import fcntl as _fcntl

        lock_dir = tmp_path / ".forge" / "locks"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "issue-1264.lock"
        lock_path.write_text("99999|x|fp\n", encoding="utf-8")

        def attempt_flock(path: str, conn: object) -> None:
            try:
                fd = open(path, "a+")
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    conn.send("acquired")
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                except BlockingIOError:
                    conn.send("blocked")
                finally:
                    fd.close()
            except FileNotFoundError:
                conn.send("missing")
            finally:
                conn.close()

        observed: list[str] = []
        real_unlink = Path.unlink

        def observing_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if self == lock_path:
                parent_conn, child_conn = _mp.Pipe()
                proc = _mp.Process(target=attempt_flock, args=(str(self), child_conn))
                proc.start()
                child_conn.close()
                try:
                    observed.append(parent_conn.recv())
                except EOFError:
                    observed.append("eof")
                proc.join(timeout=5)
                parent_conn.close()
            return real_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", observing_unlink):
            removed = sweep_story_locks(tmp_path)

        assert removed == [lock_path]
        assert observed == ["blocked"]


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
        from theforge.artifacts import ESCALATED_MARKER_PATH

        worktree = tmp_path / ".forge" / "worktrees" / "story-a"
        (worktree / ESCALATED_MARKER_PATH).parent.mkdir(parents=True)
        (worktree / ESCALATED_MARKER_PATH).write_text("final_phase: ESCALATE\n", encoding="utf-8")
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
            with patch("theforge.sprint.lock._read_lock_metadata", return_value=(424242, None)):
                with pytest.raises(TimeoutError, match="held by pid 424242"):
                    with integration_lock(
                        tmp_path,
                        timeout_seconds=0.1,
                        poll_interval_seconds=0.01,
                    ):
                        pass


# ── Integration: cmd_sprint conflict check ───────────────────────────────


class TestCmdSprintConflictGuard:
    """cmd_sprint respects the launch lock conflict contract."""

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
            force=False,
        )

    def test_conflict_drops_locked_story_and_runs_remaining(self, tmp_path: Path, capsys) -> None:
        """Initial launch drops only the conflicted story and runs the rest."""
        from theforge import cli

        story_a = self._make_story(tmp_path, "my-feature")
        story_b = self._make_story(tmp_path, "other-feature")
        manifest = self._make_manifest(tmp_path, story_a, story_b)
        args = self._make_args(tmp_path, manifest)

        mock_config = MagicMock()
        mock_config.project_root = tmp_path
        mock_config.workspace.path_pattern = ".forge/worktrees/{slug}"
        mock_config.workspace.base_branch = "main"
        mock_config.workspace.branch_pattern = "forge/{slug}"

        mock_result = MagicMock()
        mock_result.specs_failed = 0

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
                with patch("theforge.cli.sprint.run_sprint", return_value=mock_result) as mock_run:
                    with patch("theforge.detach.remove_pid"):
                        rc = cli.cmd_sprint(args)

            assert rc == 0
            mock_run.assert_called_once()
            assert mock_run.call_args.kwargs["dropped_slugs"] == {
                "my-feature": "story-lock-held-by-other-process"
            }
            captured = capsys.readouterr()
            assert "DROPPED" in captured.err
            assert "my-feature" in captured.err
            assert "alive=yes" in captured.err
        finally:
            release_event.set()
            proc.join(timeout=5)

    def test_force_overrides_live_lock_and_runs_every_story(self, tmp_path: Path, capsys) -> None:
        """--force warns and proceeds even when a live process holds a story lock."""
        from theforge import cli

        story = self._make_story(tmp_path, "my-feature")
        manifest = self._make_manifest(tmp_path, story)
        args = self._make_args(tmp_path, manifest)
        args.force = True

        mock_config = MagicMock()
        mock_config.project_root = tmp_path
        mock_config.workspace.path_pattern = ".forge/worktrees/{slug}"
        mock_config.workspace.base_branch = "main"
        mock_config.workspace.branch_pattern = "forge/{slug}"

        mock_result = MagicMock()
        mock_result.specs_failed = 0

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
                with patch("theforge.cli.sprint.run_sprint", return_value=mock_result) as mock_run:
                    with patch("theforge.detach.remove_pid"):
                        rc = cli.cmd_sprint(args)

            assert rc == 0
            mock_run.assert_called_once()
            assert mock_run.call_args.kwargs["dropped_slugs"] == {}
            captured = capsys.readouterr()
            assert "--force overrides apparent story lock conflicts" in captured.err
            assert "Proceeding without launch locks" in captured.err
        finally:
            release_event.set()
            proc.join(timeout=5)

    def test_stale_lock_is_reaped_and_story_runs(self, tmp_path: Path, capsys) -> None:
        """A dead-PID launch lock is removed during the pre-sprint sweep."""
        from theforge import cli

        story = self._make_story(tmp_path, "issue-1110")
        manifest = self._make_manifest(tmp_path, story)
        args = self._make_args(tmp_path, manifest)

        lock_dir = tmp_path / ".forge" / "locks"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "issue-1110.lock"
        lock_path.write_text(
            "40858|Sat May  2 15:49:34 2026|Sat May  2 15:49:34 2026\n",
            encoding="utf-8",
        )

        mock_config = MagicMock()
        mock_config.project_root = tmp_path
        mock_config.workspace.path_pattern = ".forge/worktrees/{slug}"
        mock_config.workspace.base_branch = "main"
        mock_config.workspace.branch_pattern = "forge/{slug}"

        mock_result = MagicMock()
        mock_result.specs_failed = 0

        with patch("theforge.cli.sprint.load_config", return_value=mock_config):
            with patch("theforge.cli.sprint.run_sprint", return_value=mock_result) as mock_run:
                with patch("theforge.detach.remove_pid"):
                    rc = cli.cmd_sprint(args)

        assert rc == 0
        mock_run.assert_called_once()
        lock_parts = lock_path.read_text(encoding="utf-8").strip().split("|", 2)
        assert len(lock_parts) == 3
        assert lock_parts[0].isdigit()
        assert lock_parts[1] != "Sat May  2 15:49:34 2026"
        captured = capsys.readouterr()
        assert "Reaped 1 stale story lock file" in captured.err

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
            with patch("theforge.sprint.launch_guard.acquire_story_locks_detailed") as mock_locks:
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
        mock_config.workspace.branch_pattern = "forge/{slug}"

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
        # Resume skips the active-worktree guard, but other git subprocesses may still
        # run elsewhere in cmd_sprint. Filter specifically for the worktree collision check.
        git_calls_for_worktree = [
            call
            for call in mock_git.call_args_list
            if call.args and call.args[0][:3] == ["git", "-C", str(worktree)]
        ]
        assert git_calls_for_worktree == []
