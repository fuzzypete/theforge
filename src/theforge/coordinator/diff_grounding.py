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

The path predicates here are pure: they answer "is this path in that set", and
return None for "the comparison could not be made" so callers can distinguish an
unavailable diff from an empty one. :func:`ground_p1_records` is the one shared
entry point that applies them to a review cycle's records — every review path
that can decide a story's outcome calls it, so the eligibility rule cannot drift
between the retry loop and the review-only path batched sprint members use.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .changed_files import collect_changed_files

if TYPE_CHECKING:
    from .state import FindingRecord


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


@dataclass(frozen=True)
class GroundingResult:
    """What grounding a review cycle's P1 records established.

    ``only_ungrounded`` is the load-bearing one for callers: it is True exactly
    when the cycle produced P1 evidence and none of it could be tied to this
    story's change. That is the condition under which a REQUEST_CHANGES verdict,
    a ``matches_spec=false`` flag, or any other signal *derived from those same
    findings* must not decide the story's outcome — the evidence behind it was
    just established to be about something else.
    """

    changed_files: frozenset[str] | None
    ungrounded: tuple[FindingRecord, ...]
    p1_records: tuple[FindingRecord, ...]

    @property
    def only_ungrounded(self) -> bool:
        return bool(self.p1_records) and len(self.ungrounded) == len(self.p1_records)

    @property
    def diff_available(self) -> bool:
        return self.changed_files is not None


def ground_p1_records(
    records: list[FindingRecord],
    workspace_path: Path,
    base_branch: str,
    *,
    log: Callable[[str], None] | None = None,
) -> GroundingResult:
    """Set ``diff_ungrounded`` on every P1 record not tied to this story's diff.

    The single eligibility check every review path runs before anything is
    allowed to block. Records are mutated in place, so the caller's classified
    list and ``state.finding_registry`` see the same dispositions and the audit
    record carries them without further work.

    Grounding is computed merge-base-to-HEAD, never from the latest dev
    iteration's base: a finding about a file touched in iteration 1 is still
    about this story when iteration 2 did not touch it.
    """
    changed_files = story_changed_files(workspace_path, base_branch)
    if changed_files is None and log is not None:
        log(
            "  ⚠ diff grounding unavailable — story diff could not be computed;"
            " P1s cannot be checked against this change"
        )
    p1_records = tuple(record for record in records if record.severity == "P1")
    ungrounded: list[FindingRecord] = []
    for record in p1_records:
        if is_diff_grounded(record.file, changed_files, workspace_path):
            continue
        record.disposition = "diff_ungrounded"  # type: ignore[assignment]
        ungrounded.append(record)
        if log is not None:
            log(
                f"  ↷ diff_ungrounded: P1 cannot be checked against this story's diff"
                f" ({ungrounded_reason(record.file, changed_files, workspace_path)}):"
                f" {record.description[:80]}"
            )
    return GroundingResult(
        changed_files=changed_files,
        ungrounded=tuple(ungrounded),
        p1_records=p1_records,
    )
