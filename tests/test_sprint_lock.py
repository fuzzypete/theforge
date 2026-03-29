"""Tests for sprint concurrency guard (flock-based per-story locking)."""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.sprint.lock import SprintConflictError, acquire_story_locks, release_story_locks

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

        mock_result = MagicMock()
        mock_result.specs_failed = 0

        with patch("theforge.cli.sprint.load_config", return_value=mock_config):
            with patch("theforge.cli.sprint.run_sprint", return_value=mock_result) as mock_run:
                with patch("theforge.detach.remove_pid"):
                    rc = cli.cmd_sprint(args)

        assert rc == 0
        mock_run.assert_called_once()
