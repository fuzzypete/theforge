from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.sprint.lock import _read_lock_metadata
from theforge.sprint.preflight import (
    abort_for_active_worktrees,
    abort_for_running_stories,
    check_active_worktrees_or_continue,
    reacquire_story_locks_in_daemon,
)


def test_abort_for_active_worktrees_lists_slugs(capsys) -> None:
    rc = abort_for_active_worktrees(["story-a", "story-b"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "Stories already have active worktrees" in captured.err
    assert "story-a, story-b" in captured.err


def test_abort_for_running_stories_lists_slugs(capsys) -> None:
    rc = abort_for_running_stories(["story-a", "story-b"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "Stories already running" in captured.err
    assert "story-a, story-b" in captured.err


def test_check_active_worktrees_returns_abort_when_active(tmp_path: Path) -> None:
    config = MagicMock()
    config.workspace.path_pattern = ".forge/worktrees/{slug}"
    config.workspace.base_branch = "main"
    config.project_root = tmp_path

    with patch("theforge.sprint.preflight.check_active_worktrees", return_value=["story-a"]):
        rc = check_active_worktrees_or_continue(
            slugs=["story-a", "story-b"],
            config=config,
            resume=False,
        )

    assert rc == 1


def test_reacquire_story_locks_in_daemon_updates_pid_and_fingerprint_in_place() -> None:
    fd_a = io.StringIO()
    fd_a.write("11111|parent-start")
    fd_b = io.StringIO()
    fd_b.write("11111|parent-start")

    with patch("theforge.sprint.lock._current_process_fingerprint", return_value="daemon-start"):
        returned = reacquire_story_locks_in_daemon(
            ["story-a", "story-b"],
            Path("/tmp/project"),
            [fd_a, fd_b],
        )

    # Same FD objects returned — no close, no re-acquire
    assert returned == [fd_a, fd_b]

    pid_a, fingerprint_a = _read_lock_metadata(fd_a)
    pid_b, fingerprint_b = _read_lock_metadata(fd_b)
    assert pid_a is not None
    assert pid_b is not None
    assert fingerprint_a == "daemon-start"
    assert fingerprint_b == "daemon-start"


def test_reacquire_story_locks_in_daemon_does_not_close_fds() -> None:
    # Regression: before fix, the inherited FDs were closed, which dropped the
    # flock and caused a self-conflict race with the still-alive parent process.
    inherited_fd = MagicMock()
    inherited_fd.read.return_value = ""

    reacquire_story_locks_in_daemon(
        ["story-a"],
        Path("/tmp/project"),
        [inherited_fd],
    )

    inherited_fd.close.assert_not_called()
