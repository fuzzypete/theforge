"""Diff-grounding for review findings: is a finding about *this* story's change?

A blocking finding asserts something about the change under review. When the
file it cites is not part of the story's own diff, the finding is describing
something other than this change — a sibling story batched into the same
sprint, a pre-existing condition, or a reviewer's own confusion — and a verdict
resting on it does not describe this story's quality (#2525).

Grounding is computed against the story's *merge base to HEAD* diff, not the
latest dev iteration's base: the story owns every commit on its branch, so a
finding about a file touched in dev iteration 1 is still about this story when
iteration 2 did not touch it.

This module is pure: it answers "is this path in that set", and returns None
for "the comparison could not be made" so callers can distinguish an
unavailable diff from an empty one. Deciding what a non-grounded finding means
belongs to the caller.
"""

from __future__ import annotations

from pathlib import Path

from .changed_files import collect_changed_files


def story_changed_files(workspace_path: Path, base_branch: str) -> frozenset[str] | None:
    """Return the story's merge-base-to-HEAD changed paths, or None if unavailable.

    None means the comparison itself failed (unresolvable ref, missing
    workspace, git error) — it must never be read as "the story changed
    nothing", which would ground no finding at all.

    Renames are decomposed by ``collect_changed_files`` (``--no-renames``), so
    both the old and the new path appear as separate entries and a finding
    citing either side grounds without special handling.
    """
    snapshot = collect_changed_files(workspace_path, base_branch)
    if snapshot is None:
        return None
    files = snapshot.get("files") or []
    return frozenset(
        str(entry["path"]) for entry in files if isinstance(entry, dict) and entry.get("path")
    )


def normalize_finding_path(raw: str | None, workspace_path: Path) -> str | None:
    """Return ``raw`` as a repository-relative POSIX path, or None if it cannot be.

    Reviewers cite paths inconsistently: repo-relative, ``./``-prefixed, or
    absolute inside the worktree. All three are normalized to the form
    ``git diff --numstat`` emits. Anything that cannot be expressed relative to
    the workspace (an absolute path outside it, an empty or whitespace-only
    field) returns None — an unresolvable citation, which the caller treats as
    ungrounded rather than silently grounding it.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(workspace_path.resolve())
        except (ValueError, OSError):
            return None
    normalized = candidate.as_posix().lstrip("/")
    return normalized or None


def is_diff_grounded(
    raw_file: str | None,
    changed_files: frozenset[str] | None,
    workspace_path: Path,
) -> bool:
    """Return True only when ``raw_file`` resolves into ``changed_files``.

    Fails closed on every uncertainty — an unavailable diff, a missing or
    unresolvable path — because "cannot be checked against this change" is
    exactly the condition that must not decide the story's outcome.
    """
    if changed_files is None:
        return False
    normalized = normalize_finding_path(raw_file, workspace_path)
    if normalized is None:
        return False
    return normalized in changed_files


def ungrounded_reason(
    raw_file: str | None,
    changed_files: frozenset[str] | None,
    workspace_path: Path,
) -> str:
    """Return a short human-readable reason a finding failed to ground.

    Used for the log line and audit narrative; callers only reach it when
    :func:`is_diff_grounded` returned False.
    """
    if changed_files is None:
        return "story diff unavailable"
    normalized = normalize_finding_path(raw_file, workspace_path)
    if normalized is None:
        return "no resolvable file cited"
    return f"{normalized} not in story diff ({len(changed_files)} file(s) changed)"
