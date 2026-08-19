"""Structured landing-record derivation for the sprint audit trail.

``land_story`` returns a ``merge_info`` dict whose ``landing_path`` names which
branch of the landing logic actually ran:

* ``fresh-merge`` — a new PR (or fast-forward merge) shipped this worktree's
  commits.
* ``already-merged`` — the "already merged" guard claimed a previously merged
  PR with the same branch name and discarded the current worktree's commits
  (see issue #1420).
* ``zero-delta`` — the branch had no commits ahead of base; nothing merged.
* ``missing-review`` — land was requested without a review result; skipped.
* ``gate-green-rollback`` — the branch was reset back to a commit the gate
  passed and a review approved, and *that* commit shipped; later commits, which
  turned the gate red with no dev iterations left to fix them, were dropped
  (see issue #2028). ``underlying_landing_path`` names the path that actually
  ran underneath.
* ``gate-green-rollback-failed`` — that reset could not be performed safely, so
  nothing landed.

Historically the sprint summary collapsed this to a single ``merged`` boolean,
which reads ``True`` both when a fresh PR shipped code and when the
already-merged guard silently threw the current work away. A boolean that lies
in failure modes is worse than no boolean.

This module maps the raw ``merge_info`` into an operator-facing structured
record so the audit trail can answer "did this sprint ship code?" directly. It
imports nothing beyond stdlib so it can be reused from both the sprint audit
serializer and the per-run coordinator audit record without import cycles.
"""

from __future__ import annotations

# Maps land_story's internal landing_path to an operator-facing outcome label.
# Unknown paths fall through to the raw landing_path so no signal is lost.
# Public so downstream consumers (e.g. the diagnose environment briefing) can
# template the landing-field semantics from this single source instead of
# re-listing them by hand.
LANDING_OUTCOME_BY_PATH = {
    "fresh-merge": "merged",
    "already-merged": "already-merged-short-circuit",
    "zero-delta": "zero-delta-short-circuit",
    "missing-review": "missing-review",
    # A gate-green checkpoint shipped after the branch was reset back to it,
    # discarding later commits that turned the gate red (#2028). Distinct from
    # ``merged`` because the story did not land what it built: it landed an
    # earlier, validated subset, and the operator has to be able to see that
    # from the outcome alone.
    "gate-green-rollback": "merged-gate-green-rollback",
    # The rollback could not be performed, so nothing landed. Kept separate from
    # a generic merge failure: the branch still holds gate-red work.
    "gate-green-rollback-failed": "gate-green-rollback-failed",
}


def build_landing_record(merge: object) -> dict | None:
    """Derive a structured landing record from a ``land_story`` merge_info dict.

    Returns ``None`` when ``merge`` is not a completed landing-attempt dict —
    e.g. ``None``, a ``{"pending": True}`` placeholder, a ``pr``/``none`` action
    that never enters the landing branch, or an early failure that returned
    before a ``landing_path`` was recorded. Callers keep the legacy ``merge``
    boolean for those cases.

    The record's ``fresh_pr_created`` flag is the key signal: ``True`` only when
    a fresh PR shipped this sprint's commits, ``False`` when a guard
    short-circuited (already-merged / zero-delta / missing-review). ``outcome``
    additionally distinguishes a queued auto-merge (``merge-queued``) from an
    already-landed merge so a not-yet-landed PR is not read as shipped code.
    """
    if not isinstance(merge, dict):
        return None
    landing_path = merge.get("landing_path")
    if not isinstance(landing_path, str) or not landing_path:
        return None

    merged = bool(merge.get("merged", False))
    merge_queued = bool(merge.get("merge_queued", False))
    guard = merge.get("guard_evidence")
    if not isinstance(guard, dict):
        guard = {}

    outcome = LANDING_OUTCOME_BY_PATH.get(landing_path, landing_path)
    # A gate-green rollback that shipped is still a fresh merge underneath; the
    # underlying path is what says whether a PR was created, and it is the only
    # path the rollback label is ever applied to.
    underlying = merge.get("underlying_landing_path")
    effective_path = underlying if isinstance(underlying, str) and underlying else landing_path
    # A fresh PR that GitHub queued for auto-merge has not landed yet; keep the
    # distinction so a queued PR is not conflated with already-shipped code.
    if effective_path == "fresh-merge" and merge_queued:
        outcome = "merge-queued" if landing_path == "fresh-merge" else f"{outcome}-queued"

    record = {
        "outcome": outcome,
        "landing_path": landing_path,
        "fresh_pr_created": effective_path == "fresh-merge",
        "merged": merged,
        "merge_queued": merge_queued,
        "pr_url": merge.get("pr_url") or guard.get("pr_url"),
        "pr_merged_at": guard.get("merged_at"),
    }
    if isinstance(underlying, str) and underlying:
        record["underlying_landing_path"] = underlying
    rollback = merge.get("gate_green_rollback")
    if isinstance(rollback, dict) and rollback:
        record["gate_green_rollback"] = _rollback_record(rollback)
    return record


def _rollback_record(rollback: dict) -> dict:
    """Operator-facing view of a gate-green rollback (#2028).

    ``checkpoint_commit`` is the commit the gate passed and the review approved —
    the one fact this outcome is named for. ``landed_commit`` is what actually
    reached the base branch, and is ``None`` when the merge-pr path had to rebase
    onto an advanced base and therefore rewrote the SHA. Reporting the checkpoint
    as landed in that case would name a commit GitHub never merged.
    """
    dropped = rollback.get("dropped_commits")
    dropped_list = [d for d in dropped if isinstance(d, dict)] if isinstance(dropped, list) else []
    count = rollback.get("dropped_commit_count")
    return {
        "checkpoint_commit": rollback.get("checkpoint_commit"),
        "landed_commit": rollback.get("landed_commit"),
        "rebased": bool(rollback.get("rebase_expected")),
        "gate_green": True,
        "review_approved": True,
        "review_cycle": rollback.get("review_cycle"),
        "dropped_head": rollback.get("dropped_head"),
        "dropped_commits": dropped_list,
        "dropped_commit_count": count if isinstance(count, int) else len(dropped_list),
        "dropped_reason": rollback.get("dropped_reason") or "",
        "outstanding_p2_count": rollback.get("outstanding_p2_count") or 0,
    }
