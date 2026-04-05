"""Pre-launch sprint guards for active worktrees and durable daemon locks."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from theforge.sprint.lock import acquire_story_locks, check_active_worktrees

_ACTIVE_WORKTREE_ABORT_PREFIX = "[forge] Stories already have active worktrees"
_RUNNING_STORIES_ABORT_PREFIX = "[forge] Stories already running"


def _format_slug_list(slugs: list[str]) -> str:
    return ", ".join(slugs)


def _abort_with_message(prefix: str, slugs: list[str]) -> int:
    print(f"{prefix}: {_format_slug_list(slugs)}. Aborting.", file=sys.stderr)
    return 1


def abort_for_active_worktrees(slugs: list[str]) -> int:
    """Print the active-worktree refusal message and return the CLI exit code."""
    return _abort_with_message(_ACTIVE_WORKTREE_ABORT_PREFIX, slugs)


def abort_for_running_stories(slugs: list[str]) -> int:
    """Print the running-stories refusal message and return the CLI exit code."""
    return _abort_with_message(_RUNNING_STORIES_ABORT_PREFIX, slugs)


def check_active_worktrees_or_continue(
    *,
    slugs: list[str],
    config: Any,
    resume: bool,
) -> int | None:
    """Return an exit code when active worktrees should block launch."""
    if resume:
        return None
    active_worktrees = check_active_worktrees(
        slugs,
        config.workspace.path_pattern,
        config.workspace.base_branch,
        config.project_root,
    )
    if active_worktrees:
        return abort_for_active_worktrees(active_worktrees)
    return None


def reacquire_story_locks_in_daemon(
    slugs: list[str],
    project_root: Path,
    locked_fds: list,
) -> list:
    """Close inherited lock FDs and re-acquire locks in the daemon process."""
    for fd in locked_fds:
        fd.close()
    refreshed_fds, conflicted = acquire_story_locks(slugs, project_root)
    if conflicted:
        abort_for_running_stories(conflicted)
        sys.exit(1)
    return refreshed_fds
