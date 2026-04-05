from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_reacquire_story_locks_in_daemon_updates_pid_in_place() -> None:
    # Simulate two inherited lock FDs (StringIO-like objects with seek/truncate/write/flush)
    import io

    fd_a = io.StringIO()
    fd_a.write("11111")  # parent PID written before fork
    fd_b = io.StringIO()
    fd_b.write("11111")

    import os

    returned = reacquire_story_locks_in_daemon(
        ["story-a", "story-b"],
        Path("/tmp/project"),
        [fd_a, fd_b],
    )

    # Same FD objects returned — no close, no re-acquire
    assert returned == [fd_a, fd_b]

    # Each FD now contains the daemon's PID
    daemon_pid = str(os.getpid())
    fd_a.seek(0)
    assert fd_a.read() == daemon_pid
    fd_b.seek(0)
    assert fd_b.read() == daemon_pid


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
