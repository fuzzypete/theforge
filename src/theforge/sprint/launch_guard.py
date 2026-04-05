"""Sprint launch concurrency guards for active worktrees and story locks."""

from __future__ import annotations

from typing import Any

from theforge.sprint.lock import acquire_story_locks
from theforge.sprint.preflight import (
    abort_for_running_stories,
    check_active_worktrees_or_continue,
)


def acquire_launch_story_locks(
    *,
    slugs: list[str],
    config: Any,
    resume: bool,
) -> tuple[list, int | None]:
    """Acquire launch-time story locks after checking for active worktrees."""
    active_worktree_error = check_active_worktrees_or_continue(
        slugs=slugs,
        config=config,
        resume=resume,
    )
    if active_worktree_error is not None:
        return [], active_worktree_error
    locked_fds, conflicted = acquire_story_locks(slugs, config.project_root)
    if conflicted:
        return [], abort_for_running_stories(conflicted)
    return locked_fds, None
