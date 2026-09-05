"""What a run writes into the shared checkout, and how to recognise it (#2775).

Two subsystems have to agree on one fact. ``sprint`` publishes the tracked
story-run artifact trees — canonical run records, knowledge summaries, landing
evidence — out of the project-root checkout and onto the base branch.
``coordinator`` refuses a landing into that checkout when it is dirty, and must
*not* refuse over those same trees: they are written by a run as an ordinary
consequence of finishing, they stand uncommitted until a publish the operator
never sees scheduled, and no operator can be asked to commit, stash or revert
them.

Before this module the attribution lived in ``sprint.audit_publish`` and the
refusal in ``coordinator.workspace``, and the only way for the refusal to reuse
the attribution was to import across that boundary — which closes a cycle,
because the publisher already imports the coordinator's workspace machinery. So
the shared fact lives here instead, below both: the tree list and the pure
predicate over a porcelain status block, with no dependency beyond the path
constant that names where landing evidence goes.

Nothing here touches git or the filesystem. Callers supply the status text (or
use ``coordinator.workspace.project_root_dirt_is_story_run_artifacts_only``,
which asks git and applies the predicate). Keeping it pure is what lets the two
subsystems share one answer without sharing a dependency.
"""

from __future__ import annotations

from .coordinator.landing_evidence import LANDING_EVIDENCE_RELPATH

# The tracked project-memory trees, repo-relative. Adding a tree here is what
# makes it publishable *and* what excuses it from the landing precondition; the
# two must move together, which is why they read the same tuple. A tree added
# here must also be re-included by the generated ``.gitignore`` or git will
# never see it as pending in the first place.
STORY_RUN_ARTIFACT_DIRS: tuple[str, ...] = (
    ".forge/audits/runs",
    ".forge/knowledge/summaries",
    "/".join(LANDING_EVIDENCE_RELPATH),
)


def porcelain_paths(output: str) -> list[str]:
    """Paths named by a ``git status --porcelain`` block, one per entry.

    Deliberately does *not* slice a fixed two-character status column. Callers
    read porcelain output through ``_run_shell``, which strips the combined
    output — so the leading space of a worktree-only status (``" M path"``) is
    gone by the time it arrives and a fixed slice eats the first character of
    the path instead. Splitting on the first whitespace run is correct for a
    stripped and an unstripped line alike, and the status column itself is not
    something any caller needs.
    """
    paths: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        entry = parts[1]
        if " -> " in entry:  # rename: the destination is what is on disk now
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip().strip('"').rstrip("/")
        if entry:
            paths.append(entry)
    return paths


def story_run_artifact_dirt_only(dirty_status: str) -> bool:
    """Whether every path in a porcelain status block is a story-run artifact.

    A dirty project root refuses a landing, and under ``max_parallel > 1`` the
    dirt is routinely a *sibling* story's own canonical run record and knowledge
    summary, written between the losing story's entry check and its merge
    (#2602). Distinguishing that from operator dirt is what lets the integration
    seam republish and retry instead of discarding approved, paid-for work, and
    what lets the entry-time landing precondition admit a story whose only
    obstacle is bookkeeping its own sprint wrote (#2775) — while operator dirt
    still refuses exactly as before.

    Returns ``False`` for a clean status: there is nothing to attribute.
    """
    paths = porcelain_paths(dirty_status)
    if not paths:
        return False
    return all(
        any(
            path == artifact_dir or path.startswith(f"{artifact_dir}/")
            for artifact_dir in STORY_RUN_ARTIFACT_DIRS
        )
        for path in paths
    )
