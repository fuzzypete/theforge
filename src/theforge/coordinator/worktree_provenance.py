"""Which story text produced the contents of a story's worktree.

Phase records carry the story text that produced them, so recovery can refuse
records planned against text that no longer governs
(:mod:`theforge.coordinator.resume_persistence`).  The *working tree* those
phases produced carried no such provenance: workspace setup adopted an existing
worktree on identity and staleness alone, so a run whose issue body was
corrected while the story was stopped began against an implementation of the
superseded text, and the dev agent spent its first iteration undoing work the
new text ruled out (#2288).

This module is the missing half of that judgement.  It is deliberately narrow:

* **It records, it does not delete.**  A stopped story's tree is usually exactly
  what should continue; discarding it on a wording change would be worse than
  the behaviour it replaces.  What was missing is that the question is asked at
  all, so the answer is written down and surfaced — to the operator in the run
  log and audit, and to the dev agent in its prompt — and continuing becomes a
  decision rather than a default.
* **Keyed on story text alone**, matching ``resume_persistence`` and
  ``routing_persistence``: a reused worktree has commits its planning never saw,
  so a git-state fingerprint would fire on every healthy reuse.
* **Best-effort.**  Every read and write failure degrades to "provenance
  unknown"; a provenance aid that can fail the run it protects is worse than
  none.

Stdlib-only imports (project convention 4).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RECORD_VERSION = 1

#: No prior record names the story text that produced this tree — it predates
#: provenance recording, or its record was lost.  Not an error: reported, not acted on.
PROVENANCE_UNKNOWN = "unknown"
#: The recorded story text hash equals the current one; reuse is unremarkable.
PROVENANCE_MATCH = "story_content_match"
#: The tree was produced against story text that no longer governs.
PROVENANCE_CHANGED = "story_content_changed"
#: This run created the tree, so nothing was inherited.
PROVENANCE_FRESH = "fresh_worktree"

_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_slug(slug: str) -> str:
    """Filesystem-safe filename stem for a story slug."""
    cleaned = _SAFE_SLUG_RE.sub("-", (slug or "").strip()).strip("-.")
    return cleaned or "story"


def story_content_hash(story_content: str) -> str:
    return hashlib.sha256((story_content or "").encode("utf-8")).hexdigest()


def worktree_provenance_dir(project_root: Path) -> Path:
    return Path(project_root) / ".forge" / "worktree_state"


def worktree_provenance_path(project_root: Path, slug: str) -> Path:
    return worktree_provenance_dir(project_root) / f"{_safe_slug(slug)}.json"


@dataclass(frozen=True)
class WorktreeProvenance:
    """The provenance judgement made when a run took possession of a worktree."""

    status: str
    #: Hash of the story text that produced the adopted contents, when known.
    recorded_hash: str | None = None
    #: Hash of the story text this run is executing, when known.
    current_hash: str | None = None
    #: True when this run adopted contents an earlier attempt produced.
    adopted: bool = False

    @property
    def inherits_superseded_work(self) -> bool:
        return self.adopted and self.status == PROVENANCE_CHANGED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "recorded_story_content_hash": self.recorded_hash,
            "current_story_content_hash": self.current_hash,
            "adopted": self.adopted,
        }


def read_worktree_provenance(project_root: Path, slug: str) -> dict[str, Any] | None:
    """Read a story's persisted worktree provenance record, or None when unusable."""
    try:
        raw = worktree_provenance_path(project_root, slug).read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("version") != RECORD_VERSION:
        return None
    return record


def evaluate_worktree_provenance(
    project_root: Path,
    slug: str,
    story_content: str | None,
    *,
    adopted: bool,
) -> WorktreeProvenance:
    """Judge whether the story text that produced this tree still governs.

    ``adopted`` distinguishes taking possession of an existing tree from
    creating a fresh one; a fresh tree holds nothing to be superseded, so it is
    reported as :data:`PROVENANCE_FRESH` regardless of what the last record said.
    """
    current = story_content_hash(story_content) if story_content is not None else None
    record = read_worktree_provenance(project_root, slug)
    recorded = record.get("story_content_hash") if record else None
    if not adopted:
        return WorktreeProvenance(
            status=PROVENANCE_FRESH,
            recorded_hash=recorded,
            current_hash=current,
            adopted=False,
        )
    if not recorded or current is None:
        status = PROVENANCE_UNKNOWN
    elif recorded == current:
        status = PROVENANCE_MATCH
    else:
        status = PROVENANCE_CHANGED
    return WorktreeProvenance(
        status=status, recorded_hash=recorded, current_hash=current, adopted=True
    )


def record_worktree_provenance(
    project_root: Path,
    slug: str,
    provenance: WorktreeProvenance,
) -> Path | None:
    """Persist ``provenance`` as this story's worktree record; return the path or None.

    A run with no story text in hand writes nothing: overwriting a real hash
    with a null would make the next run's judgement worse, not better.
    """
    if provenance.current_hash is None:
        return None
    record = {
        "version": RECORD_VERSION,
        "slug": slug,
        "story_content_hash": provenance.current_hash,
        "adoption": provenance.as_dict(),
    }
    try:
        path = worktree_provenance_path(project_root, slug)
        payload = json.dumps(record, indent=2, sort_keys=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        return None
    return path


def clear_worktree_provenance(project_root: Path, slug: str) -> None:
    """Forget a story's worktree provenance (its tree was removed)."""
    try:
        worktree_provenance_path(project_root, slug).unlink(missing_ok=True)
    except (OSError, TypeError, ValueError):
        return


def last_worktree_provenance(project_root: Path, slug: str) -> WorktreeProvenance | None:
    """The judgement the most recent workspace setup recorded, or None."""
    record = read_worktree_provenance(project_root, slug)
    if not record:
        return None
    adoption = record.get("adoption")
    if not isinstance(adoption, dict) or not adoption.get("status"):
        return None
    return WorktreeProvenance(
        status=str(adoption["status"]),
        recorded_hash=adoption.get("recorded_story_content_hash"),
        current_hash=adoption.get("current_story_content_hash"),
        adopted=bool(adoption.get("adopted")),
    )


def _short(digest: str | None) -> str:
    return digest[:12] if digest else "unknown"


def provenance_log_lines(provenance: WorktreeProvenance) -> list[str]:
    """Operator-facing lines for a worktree adoption, or [] when unremarkable."""
    if not provenance.adopted:
        return []
    if provenance.status == PROVENANCE_CHANGED:
        return [
            "⚠ WORKSPACE  story text changed since this worktree's contents were produced",
            f"  produced against story {_short(provenance.recorded_hash)}, "
            f"now running story {_short(provenance.current_hash)}",
            "  the work is kept, not discarded — the dev agent is told it may be "
            "superseded so continuing is a decision, not a default",
        ]
    if provenance.status == PROVENANCE_UNKNOWN:
        return [
            "  worktree provenance unknown — no record of the story text that "
            "produced these contents"
        ]
    return ["  worktree provenance verified — produced against the story text now running"]


def inherited_work_note(provenance: WorktreeProvenance | None) -> str | None:
    """Dev-prompt text for an inherited tree, or None when there is nothing to say.

    Only a *superseded* tree gets a note: a tree produced against the story text
    now running is an ordinary continuation and needs no narration.
    """
    if provenance is None or not provenance.inherits_superseded_work:
        return None
    return (
        "This worktree already contains uncommitted or committed work from an "
        "earlier attempt at this story, and the story text has been edited since "
        "that work was produced (story content hash "
        f"{_short(provenance.recorded_hash)} → {_short(provenance.current_hash)}).\n\n"
        "The work was kept rather than discarded, because a stopped story's tree "
        "is often exactly what should continue — but it was produced against "
        "instructions that no longer govern.\n\n"
        "Before building on it:\n\n"
        "1. Inspect what is already there (`git status`, `git diff`, "
        "`git log <base>..HEAD`).\n"
        "2. Judge each part against the spec above: keep what the current text "
        "still calls for, and remove what it rules out.\n"
        "3. Say in your handoff `summary` which inherited work you kept and which "
        "you discarded, so the reviewer can tell your work from the earlier "
        "attempt's."
    )
