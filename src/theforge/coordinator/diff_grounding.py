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

That identity — branch diff *is* story diff — holds for every story that owns
its worktree, and breaks for exactly one case: a cost-aware batch group, where
several independent stories share one branch. There the caller supplies the
story's own file set as a :class:`StoryDiff`, and grounding uses it in place of
the branch diff. Batching is a scheduling decision; it must not change what a
story is judged against.

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

#: The story's file set is the whole branch diff — every story that owns its
#: worktree.
SOURCE_BRANCH_DIFF = "branch_diff"

#: What a P1 is set back to when grounding clears a stale ``diff_ungrounded``.
#: The blocking default, matching what the classifier produces for a finding
#: seen in a prior cycle and reported again.
RESTORED_DISPOSITION = "unresolved"


@dataclass(frozen=True)
class StoryDiff:
    """The changed-file set a story's findings are grounded against.

    ``files=None`` means the set could not be established, which is a distinct
    claim from an empty set and is never collapsed into one: an unknown file set
    grounds nothing, so no finding can decide the story's outcome, whereas an
    empty one would say the story changed nothing.

    ``source`` and ``detail`` name where the set came from so the audit record
    can say what a finding was judged against — a run that suppressed findings
    against a set nobody can reconstruct is not auditable.
    """

    files: frozenset[str] | None
    source: str
    detail: str | None = None

    def as_audit_record(self) -> dict:
        return {
            "source": self.source,
            "detail": self.detail,
            "available": self.files is not None,
            "files": sorted(self.files) if self.files is not None else None,
        }


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

    story_diff: StoryDiff
    ungrounded: tuple[FindingRecord, ...]
    p1_records: tuple[FindingRecord, ...]
    #: Records that arrived carrying a stale ``diff_ungrounded`` and now ground,
    #: so grounding cleared it. Surfaced for the audit trail: a finding that
    #: stops being suppressed is as much a decision as one that starts.
    restored: tuple[FindingRecord, ...] = ()

    @property
    def changed_files(self) -> frozenset[str] | None:
        return self.story_diff.files

    @property
    def only_ungrounded(self) -> bool:
        return bool(self.p1_records) and len(self.ungrounded) == len(self.p1_records)

    @property
    def diff_available(self) -> bool:
        return self.story_diff.files is not None

    @property
    def story_changed_nothing(self) -> bool:
        """True when the story's file set is known and empty.

        Distinct from an unknown set, and the one case where an all-ungrounded
        cycle must NOT be waved through: "no finding is about this change" is a
        reason to stop blocking only when there *is* a change. A story that
        demonstrably produced nothing has no work to approve, and its review
        must be free to say so.
        """
        return self.story_diff.files is not None and not self.story_diff.files


def ground_p1_records(
    records: list[FindingRecord],
    workspace_path: Path,
    base_branch: str,
    *,
    story_diff: StoryDiff | None = None,
    log: Callable[[str], None] | None = None,
) -> GroundingResult:
    """Decide, for this cycle, which P1 records are about this story's change.

    The single eligibility check every review path runs before anything is
    allowed to block, and the **sole** authority on the ``diff_ungrounded``
    disposition. Records are mutated in place, so the caller's classified list
    and ``state.finding_registry`` see the same dispositions and the audit
    record carries them without further work.

    Grounding is a property of *a cycle*, not of a finding. The same P1 can be
    ungrounded in cycle 1 and grounded in cycle 2 because the dev touched the
    file it cites in between, and it must block the moment that happens. So this
    decides **both** directions every cycle: it marks records that do not ground,
    and it clears a ``diff_ungrounded`` left on a record that now does. Writing
    only the suppressing direction is what made a per-cycle verdict stick and
    left a genuinely recurring P1 permanently unblockable (#2525).

    Without ``story_diff`` the file set is the branch's own merge-base-to-HEAD
    diff, never the latest dev iteration's base: a finding about a file touched
    in iteration 1 is still about this story when iteration 2 did not touch it.
    Callers whose branch carries more than one story — a batch group's shared
    worktree — pass the story's own set explicitly, because there the branch
    diff would ground a sibling member's findings against this member.
    """
    if story_diff is None:
        story_diff = StoryDiff(
            files=story_changed_files(workspace_path, base_branch),
            source=SOURCE_BRANCH_DIFF,
        )
    changed_files = story_diff.files
    if changed_files is None and log is not None:
        log(
            f"  ⚠ diff grounding unavailable — this story's file set could not be"
            f" established ({story_diff.detail or story_diff.source});"
            f" P1s cannot be checked against this change"
        )
    p1_records = tuple(record for record in records if record.severity == "P1")
    ungrounded: list[FindingRecord] = []
    restored: list[FindingRecord] = []
    for record in p1_records:
        if not is_diff_grounded(record.file, changed_files, workspace_path):
            record.disposition = "diff_ungrounded"  # type: ignore[assignment]
            ungrounded.append(record)
            if log is not None:
                log(
                    f"  ↷ diff_ungrounded: P1 cannot be checked against this story's diff"
                    f" ({ungrounded_reason(record.file, changed_files, workspace_path)}):"
                    f" {record.description[:80]}"
                )
            continue
        if record.disposition == "diff_ungrounded":
            # This record grounds now and carries a verdict from a cycle where it
            # did not. Only this function writes that value, and it has not
            # written it for this record this cycle, so it is stale by
            # construction. Restore the blocking default rather than leaving a
            # finding about this story's own change unable to decide anything —
            # the conservative direction, and the one the classifier produces for
            # a recurrence.
            record.disposition = RESTORED_DISPOSITION  # type: ignore[assignment]
            restored.append(record)
            if log is not None:
                log(
                    f"  ↥ diff_ungrounded cleared: P1 now cites a file in this"
                    f" story's diff and blocks again: {record.description[:80]}"
                )
    return GroundingResult(
        story_diff=story_diff,
        ungrounded=tuple(ungrounded),
        restored=tuple(restored),
        p1_records=p1_records,
    )
