"""Pre-launch sprint guards for active worktrees and durable daemon locks."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from theforge.sprint.lock import acquire_story_locks, check_active_worktrees


def _abort_for_active_worktrees(slugs: list[str]) -> int:
    print(
        f"[forge] Stories already have active worktrees: {', '.join(slugs)}. Aborting.",
        file=sys.stderr,
    )
    return 1


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
        return _abort_for_active_worktrees(active_worktrees)
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
        print(
            f"[forge] Stories already running: {', '.join(conflicted)}. Aborting.",
            file=sys.stderr,
        )
        sys.exit(1)
    return refreshed_fds
