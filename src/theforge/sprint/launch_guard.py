"""Sprint launch concurrency guards for active worktrees and story locks."""

from __future__ import annotations

import sys
from typing import Any

from theforge.sprint.lock import (
    acquire_story_locks,
    check_active_worktrees,
    check_escalated_worktrees,
)
from theforge.sprint.preflight import (
    abort_for_active_worktrees,
    abort_for_running_stories,
    check_active_worktrees_or_continue,
)

# Reason codes used as the value in the ``dropped_slugs`` mapping.  These are
# consumed by the sprint runner for audit visibility and by tests.
REASON_PRESERVED_ESCALATED = "preserved-escalated"
REASON_ACTIVE_WORKTREE = "active-worktree-collision"
REASON_LOCK_HELD = "story-lock-held-by-other-process"


def acquire_launch_story_locks(
    *,
    slugs: list[str],
    config: Any,
    resume: bool,
    allow_drop: bool = False,
) -> tuple[list, int | None, dict[str, str]]:
    """Acquire launch-time story locks after checking for active worktrees.

    Returns ``(locked_fds, exit_code, dropped_slugs)``.

    ``dropped_slugs`` maps slug -> reason for every story that will NOT be
    scheduled this launch.  The caller passes this mapping into ``run_sprint``
    so the dropped stories still appear in sprint-audit and live status with
    a distinct outcome (``dropped`` / ``preserved``) rather than silently
    vanishing or falling back to a generic SKIPPED entry.

    When ``allow_drop`` is False (default, initial launch), any active worktree
    or locked-by-another-process conflict aborts the whole sprint — the caller
    returns ``exit_code`` to the shell.  Escalated worktrees are still treated
    as preserved (never collisions) and are removed from scheduling even on
    the initial-launch path; otherwise a sprint resumed over an already-
    escalated worktree would re-run it.

    When ``allow_drop`` is True (re-exec path), conflicting stories are
    individually recorded in ``dropped_slugs`` and the remaining, unconflicted
    stories have their locks acquired normally.

    Invariants the callers rely on:

    - Every slug in the returned ``dropped_slugs`` is *not* counted in
      ``locked_fds`` — we never hold a lock for a dropped story.
    - Every slug *not* in ``dropped_slugs`` and present in the input ``slugs``
      list is either present in ``locked_fds`` or the function returns with a
      non-None exit code.  In particular, the non-re-exec path never returns a
      partial lock set: if any story conflicts, no locks are returned.
    """
    # ── Escalated worktrees: always preserved, on every launch path ─────
    if resume:
        escalated_slugs: list[str] = []
    else:
        escalated_slugs = check_escalated_worktrees(
            slugs,
            config.workspace.path_pattern,
            config.project_root,
        )
    dropped: dict[str, str] = {s: REASON_PRESERVED_ESCALATED for s in escalated_slugs}
    schedulable = [s for s in slugs if s not in dropped]

    if escalated_slugs:
        print(
            f"[forge] PRESERVED {', '.join(escalated_slugs)}: escalated worktree "
            "preserved for human review; not rescheduled.",
            file=sys.stderr,
            flush=True,
        )

    if not allow_drop:
        # Initial-launch path: escalated stories are silently excluded from
        # scheduling, but any other collision still aborts the whole sprint.
        active_worktree_error = check_active_worktrees_or_continue(
            slugs=schedulable,
            config=config,
            resume=resume,
        )
        if active_worktree_error is not None:
            return [], active_worktree_error, dropped
        locked_fds, conflicted = acquire_story_locks(schedulable, config.project_root)
        if conflicted:
            return [], abort_for_running_stories(conflicted), dropped
        return locked_fds, None, dropped

    # ── Re-exec path: convert every collision into a per-story drop ─────
    active_worktrees = check_active_worktrees(
        schedulable,
        config.workspace.path_pattern,
        config.workspace.base_branch,
        config.project_root,
    )
    for slug in active_worktrees:
        dropped[slug] = REASON_ACTIVE_WORKTREE
    remaining = [s for s in schedulable if s not in dropped]
    if active_worktrees:
        print(
            f"[forge] DROPPED {', '.join(active_worktrees)}: active worktree "
            "collision after re-exec; continuing with remaining stories.",
            file=sys.stderr,
            flush=True,
        )
        abort_for_active_worktrees(active_worktrees)

    # Acquire locks for everything that survived worktree checks.  On a lock
    # conflict, acquire_story_locks releases the partial set it was holding —
    # so we *must* retry for the non-conflicted remainder, otherwise the live
    # stories would run without launch locks and a concurrent forge invocation
    # could race them.
    live_slugs: list[str] = list(remaining)
    locked_fds: list = []
    while live_slugs:
        attempt_fds, conflicted = acquire_story_locks(live_slugs, config.project_root)
        if not conflicted:
            locked_fds = attempt_fds
            break
        for slug in conflicted:
            dropped[slug] = REASON_LOCK_HELD
        print(
            f"[forge] DROPPED {', '.join(conflicted)}: story lock held by "
            "another process after re-exec; continuing with remaining stories.",
            file=sys.stderr,
            flush=True,
        )
        abort_for_running_stories(conflicted)
        live_slugs = [s for s in live_slugs if s not in dropped]
        # attempt_fds is already empty/released by acquire_story_locks on
        # conflict — loop around and reacquire for the surviving subset.

    return locked_fds, None, dropped
