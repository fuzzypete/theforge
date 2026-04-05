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


def test_reacquire_story_locks_in_daemon_aborts_on_conflict(capsys) -> None:
    inherited_fd = MagicMock()

    with patch(
        "theforge.sprint.preflight.acquire_story_locks",
        return_value=([], ["story-a", "story-b"]),
    ):
        try:
            reacquire_story_locks_in_daemon(
                ["story-a", "story-b"],
                Path("/tmp/project"),
                [inherited_fd],
            )
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("expected SystemExit")

    inherited_fd.close.assert_called_once()
    captured = capsys.readouterr()
    assert "Stories already running" in captured.err
    assert "story-a, story-b" in captured.err
