"""Sprint launch concurrency guards for active worktrees and story locks."""

from __future__ import annotations

import sys
from typing import Any

from theforge.sprint.lock import acquire_story_locks, check_active_worktrees
from theforge.sprint.preflight import (
    abort_for_active_worktrees,
    abort_for_running_stories,
    check_active_worktrees_or_continue,
)


def acquire_launch_story_locks(
    *,
    slugs: list[str],
    config: Any,
    resume: bool,
    allow_drop: bool = False,
) -> tuple[list, int | None, dict[str, str]]:
    """Acquire launch-time story locks after checking for active worktrees.

    Returns ``(locked_fds, exit_code, dropped_slugs)``.

    When ``allow_drop`` is False (default, initial launch), any active worktree
    or locked-by-another-process conflict aborts the whole sprint — the caller
    returns ``exit_code`` to the shell.

    When ``allow_drop`` is True (re-exec path), conflicting stories are returned
    individually in ``dropped_slugs`` (slug -> reason) and the remaining,
    unconflicted stories have their locks acquired normally.  The caller is
    expected to propagate the dropped set into ``run_sprint`` so those stories
    are recorded in sprint-audit and appear in ``forge sprint-status``.
    """
    if not allow_drop:
        active_worktree_error = check_active_worktrees_or_continue(
            slugs=slugs,
            config=config,
            resume=resume,
        )
        if active_worktree_error is not None:
            return [], active_worktree_error, {}
        locked_fds, conflicted = acquire_story_locks(slugs, config.project_root)
        if conflicted:
            return [], abort_for_running_stories(conflicted), {}
        return locked_fds, None, {}

    dropped: dict[str, str] = {}
    if resume:
        active_worktrees: list[str] = []
    else:
        active_worktrees = check_active_worktrees(
            slugs,
            config.workspace.path_pattern,
            config.workspace.base_branch,
            config.project_root,
        )
    for slug in active_worktrees:
        dropped[slug] = "active-worktree-collision"

    remaining = [s for s in slugs if s not in dropped]
    if active_worktrees:
        # Visible, operator-facing marker: separate from the routine log stream.
        print(
            f"[forge] DROPPED {', '.join(active_worktrees)}: active worktree "
            f"collision after re-exec; continuing with remaining stories.",
            file=sys.stderr,
            flush=True,
        )
        abort_for_active_worktrees(active_worktrees)

    locked_fds, conflicted = acquire_story_locks(remaining, config.project_root)
    for slug in conflicted:
        dropped[slug] = "story-lock-held-by-other-process"
    if conflicted:
        print(
            f"[forge] DROPPED {', '.join(conflicted)}: story lock held by "
            "another process after re-exec; continuing with remaining stories.",
            file=sys.stderr,
            flush=True,
        )
        abort_for_running_stories(conflicted)

    return locked_fds, None, dropped
