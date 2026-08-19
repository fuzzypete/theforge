"""Land the last gate-green commit when a later iteration turns the gate red (#2028).

A story can be driven from a passing, reviewed-and-approved state into a failing
one by its own last dev iteration. The shape that produced this issue: the gate
returned PASS at commit ``C``, the review cycle that judged ``C`` returned
APPROVE with a P2 finding, the P2 policy spent the story's last dev iteration
fixing that P2, and the gate then returned FAIL on the result. With the dev pool
spent, forge reported the whole story FAILED and discarded ``C``.

The P2 policy is worth keeping — an unfixed P2 comes back as a review finding
later and is re-fixed from scratch at higher cost. What was missing is a floor
under it: attempting a P2 should not be able to cost the story.

This module is that floor. It has three responsibilities, in run order:

1. :func:`record_gate_green_checkpoint` — at the moment an approving review
   routes back into DEV for P2 cleanup, remember the commit that was both
   gate-green and review-approved.
2. :func:`salvage_gate_green_landing` — when VALIDATE later escalates on a
   terminal gate failure, convert that failure into the existing deferred
   landing path for the checkpoint, or record why it declined to.
3. :func:`apply_gate_green_rollback` — at landing time, reset the story branch
   back to the checkpoint and report the commits that were dropped.

Every step fails closed. The default outcome is the pre-existing one: the story
fails, its work is discarded, and RCA classifies it as iteration exhaustion.
Salvage happens only when forge can name a specific commit that a gate passed
and a reviewer approved, and can prove that commit is an ancestor of what the
worktree holds now.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .state import (
    CoordinatorResult,
    CoordinatorState,
    GateGreenCheckpoint,
    Phase,
    RetryReason,
)
from .util import _log, _run_shell

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import ForgeConfig
    from ..task.story import TaskStory
    from .state import ReviewResult

#: Terminal routing reasons from ``validate_phase._blocking_finding_route`` that
#: mean "no dev remedy remains for this gate failure". ``p2_cleanup`` is the
#: shape this issue was filed on; the other two are the same situation reached by
#: a different budget. Any other terminal reason (or a non-terminal one) leaves
#: the failure alone.
SALVAGEABLE_BLOCK_REASONS = frozenset(
    {
        "p2_cleanup",
        "budgets_exhausted",
        "gate_signature_stalled",
    }
)

#: ``landing_path`` recorded on a merge_info that landed a checkpoint rather than
#: the worktree HEAD. Distinct from ``fresh-merge`` because the operator-facing
#: question is different: not "did code ship" but "which code shipped, and what
#: did forge throw away to ship it".
GATE_GREEN_LANDING_PATH = "gate-green-rollback"

#: Sentinel written to ``state.gate_green_salvage['status']`` between the
#: coordinator's decision and the landing that carries it out.
SALVAGE_PENDING = "pending"


# ── 1. Checkpoint capture ─────────────────────────────────────────────────────


def record_gate_green_checkpoint(
    state: CoordinatorState,
    parsed_review: "ReviewResult",
    *,
    carried_p2_count: int,
) -> GateGreenCheckpoint | None:
    """Remember the reviewed, gate-green commit before P2 cleanup re-enters DEV.

    Returns the recorded checkpoint, or ``None`` when this cycle does not
    establish one. The bar is deliberately higher than "a gate passed and a
    review approved": the *same commit* must be both. VALIDATE records the gate's
    commit before its own post-PASS dirty-worktree auto-commit, so a PASS
    followed by that auto-commit leaves ``last_gate_commit`` pointing at the
    parent of what the reviewers then judged. ``ReviewedCommitVerification``
    already derives that mismatch as ``gate_stale``; this reads its verdict
    rather than re-deriving a weaker one from the raw fields.
    """
    meta = state.review_cycle_metadata[-1] if state.review_cycle_metadata else None
    verification = getattr(meta, "verification", None) if meta is not None else None
    if verification is None or verification.state != "gate_passed":
        return None
    reviewed_commit = getattr(meta, "reviewed_commit", None)
    # ``gate_passed`` already implies this equality, but the checkpoint is the
    # one place where being wrong means resetting a branch onto the wrong tree.
    if not reviewed_commit or reviewed_commit != verification.gate_commit:
        return None

    checkpoint = GateGreenCheckpoint(
        commit=reviewed_commit,
        review_cycle=state.review_cycle,
        dev_iterations_spent=state.budget.total_count,
        review_verdict=parsed_review.verdict,
        carried_p2_count=carried_p2_count,
        branch_name=state.branch_name,
        review_result=parsed_review,
    )
    state.gate_green_checkpoint = checkpoint
    return checkpoint


# ── 2. Terminal-failure conversion ────────────────────────────────────────────


def _terminal_block_reason(state: CoordinatorState) -> str | None:
    """Return the routing reason of the terminal VALIDATE block, if the last one is."""
    if not state.validate_blocks:
        return None
    last = state.validate_blocks[-1]
    if not isinstance(last, dict) or last.get("outcome") != "terminal":
        return None
    reason = last.get("reason")
    return reason if isinstance(reason, str) else None


def _decline(state: CoordinatorState, reason: str, detail: str) -> None:
    """Record why a terminal gate failure was NOT salvaged, and say so once."""
    state.gate_green_salvage_declined = {"reason": reason, "detail": detail}
    _log(f"  ↷ SALVAGE declined ({reason}): {detail}")


def _head_commit(workspace_path: Path) -> str | None:
    ok, out = _run_shell("git rev-parse HEAD", workspace_path)
    sha = out.strip() if ok else ""
    return sha or None


def _tracked_dirty(workspace_path: Path) -> list[str]:
    """Tracked (non-untracked) dirty paths in the worktree, or [] when clean."""
    ok, out = _run_shell("git status --porcelain", workspace_path)
    if not ok:
        # Cannot prove the worktree is clean — treat as dirty so callers fail closed.
        return ["<git status unavailable>"]
    return [line[3:].strip() for line in out.splitlines() if line and not line.startswith("??")]


def salvage_gate_green_landing(
    state: CoordinatorState,
    config: "ForgeConfig",
    task: "TaskStory",
    result: CoordinatorResult,
    *,
    workspace_path: Path,
    branch_name: str,
    auto_merge: bool,
    logger: Any = None,
) -> CoordinatorResult:
    """Convert a terminal gate failure into a pending landing of the checkpoint.

    Returns ``result`` unchanged when this run has nothing safely landable —
    which is the common case and the one AC4 protects: a story with no
    gate-green commit in its history still fails exactly as it did before.

    On success the result is mutated in place to the same shape
    ``_finalize_approve`` produces for a deferred landing (success, ``Phase.DONE``,
    ``landing_status='pending_integration'``, a pending ``merge`` action), so
    every existing landing, audit, and failure-handling path downstream applies
    unchanged. ``state.phase`` moves to ``Phase.DONE`` alongside it: the audit
    and state serializers read the *state's* phase, and a story reported LANDED
    must not carry an ESCALATE phase.
    """
    checkpoint = state.gate_green_checkpoint

    if state.retry_reason is not RetryReason.GATE_FAIL:
        return result
    block_reason = _terminal_block_reason(state)
    if block_reason not in SALVAGEABLE_BLOCK_REASONS:
        return result

    # From here the run *is* the shape this feature exists for: a terminal gate
    # failure with no dev remedy left. Every exit below is a decline worth
    # recording, because "nothing to salvage" and "salvageable but unsafe" are
    # different answers to the operator's question.
    if checkpoint is None or not checkpoint.commit:
        # Separate the two absences: a gate that never went green is untouched by
        # this feature; a gate that went green on a commit no review approved is
        # something forge could see but deliberately would not land.
        if state.last_gate_commit and "PASS" in state.gate_decisions:
            _decline(
                state,
                "gate_green_never_approved",
                f"the gate passed at {state.last_gate_commit[:8]} but no review cycle "
                "approved that exact commit, so no reviewed commit exists to land",
            )
        return result

    effective_on_approve = "merge" if auto_merge else config.workspace.on_approve
    if effective_on_approve not in ("merge", "merge-pr"):
        _decline(
            state,
            "landing_disabled",
            f"on_approve={effective_on_approve!r} does not land work, so there is "
            "nothing to land the gate-green commit through",
        )
        return result

    if task.batch_group:
        # A batch group shares one branch: the leader's landing carries every
        # member's commits, and member reviews run against the branch *after*
        # this returns. Resetting that branch would review one tree and land
        # another, and could drop a member's work under the leader's name.
        _decline(
            state,
            "batch_group_leader",
            f"story leads batch group {task.batch_group!r}; its branch carries other "
            "stories' commits, which a rollback would discard without review",
        )
        return result

    head = _head_commit(workspace_path)
    if head is None:
        _decline(state, "head_unresolvable", "could not resolve worktree HEAD")
        return result
    if head == checkpoint.commit:
        # The gate-red work is not on the branch (uncommitted, or already
        # discarded). Nothing to roll back to — and nothing this path improves.
        _decline(
            state,
            "head_is_checkpoint",
            "worktree HEAD already is the gate-green commit; the failing changes were "
            "never committed, so there is no earlier commit to fall back to",
        )
        return result
    ancestor_ok, _ = _run_shell(
        f"git merge-base --is-ancestor {shlex.quote(checkpoint.commit)} HEAD", workspace_path
    )
    if not ancestor_ok:
        _decline(
            state,
            "checkpoint_not_ancestor",
            f"{checkpoint.commit[:8]} is not an ancestor of HEAD {head[:8]}; the branch "
            "was rewritten, so resetting to it would discard unrelated history",
        )
        return result
    dirty = _tracked_dirty(workspace_path)
    if dirty:
        _decline(
            state,
            "dirty_worktree",
            "uncommitted tracked changes present ("
            + ", ".join(dirty[:5])
            + "); refusing to reset the branch over work that was never committed",
        )
        return result

    discard_reason = (
        "the final dev iteration left the gate red and no dev iterations remained "
        f"to recover it ({block_reason})"
    )
    state.gate_green_salvage = {
        "status": SALVAGE_PENDING,
        "checkpoint": checkpoint.to_audit_dict(),
        "checkpoint_commit": checkpoint.commit,
        "dropped_head": head,
        "dropped_reason": discard_reason,
        "block_reason": block_reason,
        "outstanding_p2_count": checkpoint.carried_p2_count,
        # The escalation this supersedes. The escalate event was already emitted
        # by VALIDATE before this ran, so the audit must carry the text that was
        # escalated on rather than leave a story reported LANDED with an
        # unexplained escalation in its event stream.
        "superseded_escalation": state.error,
    }
    # The approval this lands on is the checkpoint's, not the (nonexistent) one
    # for HEAD. merge-pr fails closed without a review, so this is also what
    # keeps a salvage from landing unreviewed.
    state.landing_review_result = checkpoint.review_result
    state.landing_review_source = "gate_green_checkpoint"

    state.phase = Phase.DONE
    state.error = None

    result.success = True
    result.phase = Phase.DONE
    result.landing_status = "pending_integration"
    result.merge = {"action": effective_on_approve, "pending": True}
    result.message = (
        f"Task '{task.name}' landed its last gate-green commit "
        f"{checkpoint.commit[:8]} (review-approved, cycle {checkpoint.review_cycle}); "
        f"later work discarded because {discard_reason}. "
        f"{checkpoint.carried_p2_count} P2 finding(s) returned to the backlog. "
        f"Branch: {branch_name}"
    )

    _log(
        f"  ⤺ SALVAGE   landing gate-green commit {checkpoint.commit[:8]} "
        f"(review-approved, cycle {checkpoint.review_cycle}); dropping later work at "
        f"{head[:8]} — {discard_reason}"
    )
    if logger is not None:
        logger._safe_emit(
            "gate_green_salvage",
            checkpoint_commit=checkpoint.commit,
            dropped_head=head,
            review_cycle=checkpoint.review_cycle,
            outstanding_p2_count=checkpoint.carried_p2_count,
            block_reason=block_reason,
            supersedes="escalate",
        )
    return result


# ── 3. Landing-time rollback ──────────────────────────────────────────────────


def _dropped_commits(workspace_path: Path, checkpoint: str, head: str) -> list[dict[str, str]]:
    """List the commits ``checkpoint..head`` that the rollback discards."""
    ok, out = _run_shell(
        f"git log --format=%H%x1f%s {shlex.quote(checkpoint)}..{shlex.quote(head)}",
        workspace_path,
    )
    if not ok:
        return []
    dropped: list[dict[str, str]] = []
    for line in out.splitlines():
        if "\x1f" not in line:
            continue
        sha, _, subject = line.partition("\x1f")
        if sha.strip():
            dropped.append({"sha": sha.strip(), "subject": subject.strip()})
    return dropped


def apply_gate_green_rollback(
    state: CoordinatorState,
    workspace_path: Path,
    *,
    effective_on_approve: str,
    base_branch: str,
) -> dict:
    """Reset the story branch to the gate-green checkpoint before landing runs.

    Returns ``{"ok": True, ...}`` with the rollback evidence, or
    ``{"ok": False, "error": ...}``. The caller must abandon the landing on a
    failure rather than fall through to landing the gate-red HEAD.

    Re-verifies the ancestry and cleanliness the salvage decision checked: the
    decision and the landing happen in different calls (and, for sprints,
    different threads), so nothing the decision proved is still proven here.
    """
    salvage = state.gate_green_salvage or {}
    commit = salvage.get("checkpoint_commit")
    if not isinstance(commit, str) or not commit:
        return {"ok": False, "error": "gate-green salvage requested without a checkpoint commit"}

    head = _head_commit(workspace_path)
    if head is None:
        return {"ok": False, "error": "gate-green rollback: could not resolve worktree HEAD"}
    ancestor_ok, ancestor_out = _run_shell(
        f"git merge-base --is-ancestor {shlex.quote(commit)} HEAD", workspace_path
    )
    if not ancestor_ok:
        return {
            "ok": False,
            "error": (
                f"gate-green rollback: {commit[:8]} is not an ancestor of HEAD {head[:8]} "
                f"({ancestor_out.strip()})"
            ),
        }
    dirty = _tracked_dirty(workspace_path)
    if dirty:
        return {
            "ok": False,
            "error": (
                "gate-green rollback: refusing to reset over uncommitted tracked changes ("
                + ", ".join(dirty[:5])
                + ")"
            ),
        }

    dropped = _dropped_commits(workspace_path, commit, head) if head != commit else []
    if head != commit:
        reset_ok, reset_out = _run_shell(f"git reset --hard {shlex.quote(commit)}", workspace_path)
        if not reset_ok:
            return {
                "ok": False,
                "error": f"gate-green rollback: git reset --hard {commit[:8]} failed: "
                f"{reset_out.strip()}",
            }
        _log(
            f"  ⤺ LANDING gate-green rollback: branch reset to {commit[:8]}, "
            f"dropping {len(dropped)} commit(s) from {head[:8]}"
        )

    # merge-pr rebases onto the fetched base before force-pushing, which rewrites
    # the checkpoint SHA whenever the base has advanced. Claiming the original
    # SHA landed would then be false, so establish up front whether the identity
    # survives and let the record say which it is.
    landed_commit: str | None = commit
    rebase_expected = False
    if effective_on_approve == "merge-pr":
        _run_shell(f"git fetch origin {shlex.quote(base_branch)}", workspace_path)
        up_to_date, _ = _run_shell(
            f"git merge-base --is-ancestor origin/{shlex.quote(base_branch)} HEAD",
            workspace_path,
        )
        if not up_to_date:
            rebase_expected = True
            landed_commit = None

    return {
        "ok": True,
        "checkpoint_commit": commit,
        "landed_commit": landed_commit,
        "rebase_expected": rebase_expected,
        "dropped_head": head,
        "dropped_commits": dropped,
        "dropped_commit_count": len(dropped),
        "dropped_reason": salvage.get("dropped_reason") or "",
        "outstanding_p2_count": salvage.get("outstanding_p2_count") or 0,
        "review_cycle": (salvage.get("checkpoint") or {}).get("review_cycle"),
    }


def annotate_gate_green_landing(merge_info: dict, rollback: dict) -> dict:
    """Fold rollback evidence into a completed ``merge_info``.

    The underlying landing path is preserved rather than replaced: a rollback
    that then hit the zero-delta or already-merged guard did not ship the
    checkpoint, and rewriting its path to ``gate-green-rollback`` would report a
    guard short-circuit as a successful salvage. Only a fresh merge — the one
    path that actually ships the reset branch — takes the new label.
    """
    merged = dict(merge_info)
    underlying = merged.get("landing_path")
    merged["gate_green_rollback"] = {
        key: rollback[key]
        for key in (
            "checkpoint_commit",
            "landed_commit",
            "rebase_expected",
            "dropped_head",
            "dropped_commits",
            "dropped_commit_count",
            "dropped_reason",
            "outstanding_p2_count",
            "review_cycle",
        )
        if key in rollback
    }
    if underlying == "fresh-merge":
        merged["underlying_landing_path"] = underlying
        merged["landing_path"] = GATE_GREEN_LANDING_PATH
    return merged
