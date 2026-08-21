"""Sprint launch concurrency guards for active worktrees and story locks."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

from theforge.sprint.lock import (
    acquire_story_locks_detailed,
    check_active_worktrees,
    check_escalated_worktrees,
    sweep_story_locks,
)
from theforge.sprint.preflight import (
    abort_for_active_worktrees,
    check_active_worktrees_or_continue,
    drop_conflicting_running_stories,
    warn_for_running_stories,
)
from theforge.sprint.preserved_resume import preserved_escalated_message
from theforge.sprint.prior_landing import (
    PRIOR_SUCCEEDED_OUTCOMES,
    as_prior_record,
    reconcilable_prior_success,
)
from theforge.sprint.story_executions import sweep_story_executions

# Reason codes used as the value in the ``dropped_slugs`` mapping.  These are
# consumed by the sprint runner for audit visibility and by tests.
REASON_PRESERVED_ESCALATED = "preserved-escalated"
REASON_ACTIVE_WORKTREE = "active-worktree-collision"
REASON_LOCK_HELD = "story-lock-held-by-other-process"
# Re-exec drop reasons that distinguish a prior-generation worktree from a
# genuine fresh collision. When a parallel sprint re-execs, a worktree left on
# disk may correspond to a story the *prior generation* already finished (DONE)
# or left partially complete (stranded). Classifying those against the prior
# generation's recorded outcomes prevents a completed story from being reported
# as a fresh ``active-worktree-collision`` and re-consumed.
REASON_RECONCILE_PRIOR_DONE = "reconciled-prior-generation-done"
REASON_STRANDED_WORKTREE = "stranded-prior-generation-worktree"
# Not a drop reason: the deferral marker for a story of *this* sprint generation
# that is still this run's own in-flight work across the re-exec boundary (the pid
# survives ``os.execv``). The evidence is either an agent process group this pid
# spawned and that is still alive, or an ownership record the scheduler wrote
# before dispatching the story and has not yet settled — a story executing
# in-process at the instant of the exec has the second and never the first
# (#2617). Its worktree is this run's own live work, not a foreign collision. The
# runner surfaces this string on the story's live status while it waits for any
# inherited agent to finish, then resumes the story.
REASON_IN_FLIGHT = "in-flight-current-sprint"
# Not a drop reason either: the deferral marker for a story whose liveness could
# not be established (an unreadable/absent agent sidecar) but whose worktree is
# present. Failing to resolve liveness is not evidence that nothing is running,
# so such a story is treated exactly like a confirmed in-flight one — deferred
# and resumed by this sprint — instead of falling through to the branch whose
# remediation is to delete the worktree (#2079).
REASON_IN_FLIGHT_UNRESOLVED = "in-flight-liveness-unresolved"

# Prior-generation outcome strings (upper-cased) that *claim* the story
# succeeded. Claiming it is not sufficient: a succeeded outcome is only
# reconcilable when the record also shows its landing obligation resolved (see
# ``prior_landing.reconcilable_prior_success``). A coordinator ``DONE`` is
# persisted the moment review approves, before the sprint's landing step runs,
# so a re-exec in that window leaves a DONE behind for a story still sitting
# unmerged on a branch (#2189).
_PRIOR_SUCCEEDED_OUTCOMES = PRIOR_SUCCEEDED_OUTCOMES


def acquire_launch_story_locks(
    *,
    slugs: list[str],
    config: Any,
    resume: bool,
    allow_drop: bool = False,
    force: bool = False,
    prior_outcomes: dict[str, str | dict] | None = None,
    live_slugs: set[str] | None = None,
    unresolved_slugs: set[str] | None = None,
    registered_slugs: set[str] | None = None,
    canonical_refs_by_slug: Mapping[str, str] | None = None,
) -> tuple[list, int | None, dict[str, str]]:
    """Acquire launch-time story locks after checking for active worktrees.

    Returns ``(locked_fds, exit_code, dropped_slugs)``.

    ``dropped_slugs`` maps slug -> reason for every story that will NOT be
    scheduled this launch.  The caller passes this mapping into ``run_sprint``
    so the dropped stories still appear in sprint-audit and live status with
    a distinct outcome (``dropped`` / ``preserved``) rather than silently
    vanishing or falling back to a generic SKIPPED entry.

    When ``allow_drop`` is False (default, initial launch), active-worktree
    collisions still abort the whole sprint. Story-lock conflicts are resolved
    per story: conflicting stories are dropped and the rest continue, unless
    ``force`` is True, in which case the conflict is warned about and the story
    proceeds without a launch lock.

    When ``allow_drop`` is True (re-exec path), conflicting stories are
    individually recorded in ``dropped_slugs`` and the remaining, unconflicted
    stories have their locks acquired normally.

    ``live_slugs`` names the stories this same process still has in flight across
    a re-exec boundary (see :mod:`theforge.sprint.live_stories`). They are
    excluded from every reconciliation that would treat their worktree as foreign
    (escalation scan, collision classification, lock sweeping) but remain
    schedulable and keep their launch lock: the runner defers each one until any
    inherited agent exits and then resumes it. Dropping them instead would strand
    the story — no other process can adopt an agent group this pid owns, and no
    other process is responsible for a story this run declared itself executing.

    ``registered_slugs`` names the subset of ``live_slugs`` vouched for only by an
    ownership record — this run's work with no surviving agent process group. The
    distinction changes exactly one decision here: an ownership record can outlive
    the story it describes by the width of the window between the scheduler
    writing a story's terminal outcome and clearing the record, so where such a
    record collides with a prior generation's *reconcilable* success, the
    recorded landing wins and the story is reconciled rather than re-dispatched
    (#2189's finished work must not be paid for twice). A live agent group is not
    subject to that: a process that is running now outranks any record of a past
    outcome.

    ``unresolved_slugs`` names the stories whose liveness could *not* be
    established (see :class:`theforge.sprint.live_stories.LivenessResolution`).
    They are handled identically to ``live_slugs``: an unresolved lookup is not
    evidence that no agent is running, and classifying such a story as a foreign
    ``REASON_ACTIVE_WORKTREE`` collision is what produced #2079 — a sprint
    dropping its own committed work and advising the operator to delete it.

    ``canonical_refs_by_slug`` is best-effort operator context for launch-time
    preserved messages only. Lock ownership and collision classification remain
    keyed by slug; the extra ref lets file-backed and custom-slug issue stories
    name a runnable ``forge review`` command before the full sprint resolves.

    Invariants the callers rely on:

    - Every slug in the returned ``dropped_slugs`` is *not* counted in
      ``locked_fds`` — we never hold a lock for a dropped story.
    - Every slug *not* in ``dropped_slugs`` and present in the input ``slugs``
      list is either present in ``locked_fds`` or the function returns with a
      non-None exit code.  In particular, the non-re-exec path never returns a
      partial lock set: if any story conflicts, no locks are returned.
    """
    # ── In-flight stories: this process's own live work, untouchable ────
    #
    # Identified before every other check so no reconciliation step — escalation
    # scan, worktree collision classification, lock sweep — ever gets to look at
    # a worktree whose agent is still running inside it. They are NOT dropped:
    # the story stays scheduled and keeps its launch lock, because this process
    # is the only one that can still finish it (the runner waits for the
    # inherited agent, then resumes the story through triage).
    #
    # A story whose liveness could not be resolved is held to the same rule: the
    # lookup failing is not evidence that its agent is gone, so it is deferred
    # rather than reconciled against a worktree that may be under active write.
    prior_outcomes = prior_outcomes or {}
    # Precedence, decided before anything reads these sets: an ownership record
    # is cleared *after* the scheduler settles a story, so a crash or a re-exec
    # inside that window leaves a record describing work that already finished
    # and landed. Where the prior generation recorded exactly that, its landing
    # is the better evidence and the record is disregarded — otherwise finished,
    # paid-for work would be re-dispatched and #2189's reconciliation bypassed.
    _superseded = {
        s for s in (registered_slugs or set()) if reconcilable_prior_success(prior_outcomes.get(s))
    }
    confirmed_live = [s for s in slugs if s in (live_slugs or set()) and s not in _superseded]
    unresolved_live = [
        s
        for s in slugs
        if s in (unresolved_slugs or set()) and s not in confirmed_live and s not in _superseded
    ]
    in_flight_slugs = confirmed_live + unresolved_live
    registered_only = [s for s in confirmed_live if s in (registered_slugs or set())]
    with_agents = [s for s in confirmed_live if s not in (registered_slugs or set())]
    dropped: dict[str, str] = {}
    if with_agents:
        print(
            f"[forge] IN-FLIGHT {', '.join(with_agents)}: agent process group "
            "from this sprint survived the re-exec; preserving the live worktree and "
            "deferring the story until its agent finishes.",
            file=sys.stderr,
            flush=True,
        )
    if registered_only:
        print(
            f"[forge] IN-FLIGHT {', '.join(registered_only)}: this sprint dispatched "
            "the story and has not settled it, so the worktree is this run's own "
            "unfinished work rather than a collision; preserving it and resuming the "
            "story.",
            file=sys.stderr,
            flush=True,
        )
    if unresolved_live:
        print(
            f"[forge] IN-FLIGHT {', '.join(unresolved_live)}: liveness of this "
            "sprint's own agent could not be resolved; preserving the worktree and "
            "deferring the story rather than treating it as a foreign collision.",
            file=sys.stderr,
            flush=True,
        )

    # ── Escalated worktrees: always preserved, on every launch path ─────
    considered = [s for s in slugs if s not in in_flight_slugs]
    if resume:
        escalated_slugs: list[str] = []
    else:
        escalated_slugs = check_escalated_worktrees(
            considered,
            config.workspace.path_pattern,
            config.workspace.branch_pattern,
            config.workspace.base_branch,
            config.project_root,
        )
    dropped.update({s: REASON_PRESERVED_ESCALATED for s in escalated_slugs})
    schedulable = [s for s in slugs if s not in dropped]

    for slug in escalated_slugs:
        canonical_ref = (canonical_refs_by_slug or {}).get(slug)
        print(
            f"[forge] {preserved_escalated_message(slug, canonical_ref=canonical_ref)}.",
            file=sys.stderr,
            flush=True,
        )

    # A live story's lock file is unlocked (``os.execv`` closes the flock fd), so
    # the generic sweep would read it as stale and delete it — and the worktree
    # sweep then loses the marker that says the directory is claimed. Keep it.
    reaped_lock_paths = sweep_story_locks(config.project_root, exclude_slugs=set(in_flight_slugs))
    if reaped_lock_paths:
        print(
            f"[forge] Reaped {len(reaped_lock_paths)} stale story lock file(s) at sprint launch.",
            file=sys.stderr,
            flush=True,
        )

    # Ownership records left by runs that are gone. Swept *after* the liveness
    # resolution that produced ``live_slugs`` has already read them, and never
    # for a story this launch is deferring: a record only describes work nothing
    # is doing once its owning process is provably not this one and not alive.
    try:
        sweep_story_executions(config.project_root, exclude_slugs=set(in_flight_slugs))
    except OSError:
        # A sweep that could not run leaves stale records, which cost a later
        # launch a deferral it can reconcile — never a reason to fail this one.
        pass

    if not allow_drop:
        # Initial-launch path: escalated stories are silently excluded from
        # scheduling, active worktree collisions still abort, and story-lock
        # collisions are resolved per story.
        active_worktree_error = check_active_worktrees_or_continue(
            slugs=schedulable,
            config=config,
            resume=resume,
        )
        if active_worktree_error is not None:
            return [], active_worktree_error, dropped
        remaining = list(schedulable)
        locked_fds: list = []
        while remaining:
            attempt_fds, conflicted = acquire_story_locks_detailed(remaining, config.project_root)
            if not conflicted:
                locked_fds = attempt_fds
                break
            conflicted_slugs = {conflict.slug for conflict in conflicted}
            if force:
                warn_for_running_stories(conflicted)
            else:
                for slug in conflicted_slugs:
                    dropped[slug] = REASON_LOCK_HELD
                drop_conflicting_running_stories(conflicted)
            remaining = [slug for slug in remaining if slug not in conflicted_slugs]
        return locked_fds, None, dropped

    # ── Re-exec path: convert every collision into a per-story drop ─────
    #
    # A worktree with commits ahead of base is not necessarily a fresh
    # collision after a re-exec: the *prior generation* may have already
    # finished this story (DONE) or left it partially complete (stranded).
    # Classify each active worktree against the prior generation's recorded
    # outcomes (``prior_outcomes``, slug -> recorded story entry, or a bare
    # upper-cased outcome string) so a completed story is reconciled instead of
    # being flattened into a launch collision. A succeeded outcome whose landing
    # was owed and never completed is NOT a completed story: it is reported as
    # stranded, so a later generation can resume it to actual landing (#2189).
    active_worktrees = check_active_worktrees(
        [s for s in schedulable if s not in in_flight_slugs],
        config.workspace.path_pattern,
        config.workspace.base_branch,
        config.project_root,
    )
    reconciled_slugs: list[str] = []
    stranded_slugs: list[str] = []
    collision_slugs: list[str] = []
    for slug in active_worktrees:
        if slug in dropped or slug in in_flight_slugs:
            # Already classified (escalated), or live work of this same run. A
            # later, coarser classification must never overwrite either.
            continue
        prior_raw = prior_outcomes.get(slug)
        prior_outcome = as_prior_record(prior_raw)["outcome"]
        if reconcilable_prior_success(prior_raw):
            dropped[slug] = REASON_RECONCILE_PRIOR_DONE
            reconciled_slugs.append(slug)
        elif prior_outcome:
            # A prior-generation record exists but the story did not succeed, or
            # claimed success while its landing was still owed and unresolved —
            # recoverable stranded sprint state, not a fresh collision.
            dropped[slug] = REASON_STRANDED_WORKTREE
            stranded_slugs.append(slug)
        else:
            dropped[slug] = REASON_ACTIVE_WORKTREE
            collision_slugs.append(slug)
    remaining = [s for s in schedulable if s not in dropped]
    if reconciled_slugs:
        print(
            f"[forge] RECONCILED {', '.join(reconciled_slugs)}: prior generation "
            "already completed these stories; preserving their outcome instead of "
            "re-running.",
            file=sys.stderr,
            flush=True,
        )
    if stranded_slugs:
        print(
            f"[forge] STRANDED {', '.join(stranded_slugs)}: prior generation left "
            "unfinished sprint state; reporting as recoverable stranded work.",
            file=sys.stderr,
            flush=True,
        )
    if collision_slugs:
        print(
            f"[forge] DROPPED {', '.join(collision_slugs)}: active worktree "
            "collision after re-exec; continuing with remaining stories.",
            file=sys.stderr,
            flush=True,
        )
        abort_for_active_worktrees(collision_slugs)

    # Acquire locks for everything that survived worktree checks.  On a lock
    # conflict, acquire_story_locks releases the partial set it was holding —
    # so we *must* retry for the non-conflicted remainder, otherwise the live
    # stories would run without launch locks and a concurrent forge invocation
    # could race them.
    live_slugs: list[str] = list(remaining)
    locked_fds: list = []
    while live_slugs:
        attempt_fds, conflicted = acquire_story_locks_detailed(live_slugs, config.project_root)
        if not conflicted:
            locked_fds = attempt_fds
            break
        for conflict in conflicted:
            slug = conflict.slug
            dropped[slug] = REASON_LOCK_HELD
        drop_conflicting_running_stories(conflicted)
        live_slugs = [s for s in live_slugs if s not in dropped]
        # attempt_fds is already empty/released by acquire_story_locks on
        # conflict — loop around and reacquire for the surviving subset.

    return locked_fds, None, dropped
