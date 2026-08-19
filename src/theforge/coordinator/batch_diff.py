"""Per-story file attribution inside a cost-aware batch group's shared worktree.

A batch group packs several *independent* stories into one dev pass to amortise
orchestration cost. They share a worktree and a branch, so the branch diff is
the group's combined change, not any one member's. Grounding a member's review
findings against that combined set would let a sibling member's criteria decide
this member's outcome — the same failure #2525 reported at sprint level, one
scope down. Batching is a scheduling decision and must not change what a story
is judged against.

The split comes from attribution the batch dev prompt already requires: every
``commits`` entry in the shared handoff carries a ``slug`` naming which member
it implements (``task/dev_prompts.py``, "Per-Story Handoff"). This module turns
that into a per-member file set.

Attribution that is absent, malformed, or names shas git cannot resolve yields
an *unknown* file set rather than a guessed one. Unknown grounds nothing, so no
finding can decide that member's outcome — the direction the story asks for,
since a false rejection costs both the discarded work and an audit trail that
misreports it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .changed_files import collect_commit_files
from .diff_grounding import StoryDiff

if TYPE_CHECKING:
    from .state import CoordinatorState

#: The story's file set came from commits the shared handoff attributed to it.
SOURCE_BATCH_COMMITS = "batch_commit_attribution"


def latest_dev_handoff(state: CoordinatorState) -> dict | None:
    """Return the most recent dev handoff mapping captured on ``state``, if any.

    Skips snapshots recorded for a dev attempt that produced no structured
    output, so a later failed iteration does not hide the attribution an earlier
    successful one wrote.
    """
    for snapshot in reversed(state.dev_handoff_snapshots):
        if isinstance(snapshot, dict) and isinstance(snapshot.get("handoff"), dict):
            return snapshot["handoff"]
    return None


def member_commit_revs(handoff: dict | None, slug: str) -> list[str] | None:
    """Return the commit revs the handoff attributes to ``slug``, or None.

    None means the handoff carries no usable attribution at all — it is missing,
    is not a mapping, has no ``commits`` list, or has one where no entry names a
    slug. An empty list is different and is returned as such: attribution exists
    and assigns this member nothing.
    """
    if not isinstance(handoff, dict):
        return None
    raw_commits = handoff.get("commits")
    if not isinstance(raw_commits, list):
        return None

    wanted = slug.strip().lower()
    revs: list[str] = []
    saw_any_slug = False
    for entry in raw_commits:
        if not isinstance(entry, dict):
            continue
        entry_slug = entry.get("slug")
        if entry_slug is None or not str(entry_slug).strip():
            continue
        saw_any_slug = True
        if str(entry_slug).strip().lower() != wanted:
            continue
        sha = str(entry.get("sha") or "").strip()
        if not sha:
            # An attributed commit whose sha is unusable makes this member's set
            # incomplete, and an incomplete set grounds findings that belong to
            # commits it failed to name. Refuse the whole attribution.
            return None
        revs.append(sha)
    if not saw_any_slug:
        # Unattributed commits cannot be split by story: every member would see
        # the group's whole change, which is the bug this module exists for.
        return None
    return revs


def batch_member_story_diff(
    workspace_path: Path,
    handoff: dict | None,
    slug: str,
) -> StoryDiff:
    """Return the file set belonging to batch member ``slug`` in a shared worktree.

    ``StoryDiff.files`` is None when attribution could not be established, and
    an empty frozenset when attribution exists and assigns this member no
    commits — a real claim (the member changed nothing) that callers treat
    differently from an unknown set.
    """
    revs = member_commit_revs(handoff, slug)
    if revs is None:
        return StoryDiff(
            files=None,
            source=SOURCE_BATCH_COMMITS,
            detail=(
                "shared batch handoff carries no per-story commit attribution; "
                f"cannot separate {slug}'s change from its batch group's"
            ),
        )
    if not revs:
        return StoryDiff(
            files=frozenset(),
            source=SOURCE_BATCH_COMMITS,
            detail=f"shared batch handoff attributes no commits to {slug}",
        )
    snapshot = collect_commit_files(workspace_path, revs)
    if snapshot is None:
        return StoryDiff(
            files=None,
            source=SOURCE_BATCH_COMMITS,
            detail=(
                f"{len(revs)} commit(s) attributed to {slug} could not be read "
                f"from the shared worktree"
            ),
        )
    return StoryDiff(
        files=frozenset(str(entry["path"]) for entry in snapshot["files"]),
        source=SOURCE_BATCH_COMMITS,
        detail=f"{len(snapshot['commits'])} commit(s) attributed to {slug}",
    )
