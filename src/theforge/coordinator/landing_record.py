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
    # A fresh PR that GitHub queued for auto-merge has not landed yet; keep the
    # distinction so a queued PR is not conflated with already-shipped code.
    if landing_path == "fresh-merge" and merge_queued:
        outcome = "merge-queued"

    return {
        "outcome": outcome,
        "landing_path": landing_path,
        "fresh_pr_created": landing_path == "fresh-merge",
        "merged": merged,
        "merge_queued": merge_queued,
        "pr_url": merge.get("pr_url") or guard.get("pr_url"),
        "pr_merged_at": guard.get("merged_at"),
    }
