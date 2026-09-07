"""Apply an accepted decomposition proposal to the issue tracker (#2824).

Producing a proposal (#2686) and applying one are different acts carrying
different risks. The proposal is advisory: an operator reads it and can ignore
it at no cost. This module is the *mutation* — it creates one issue per proposed
slice, writes the declared dependency edges into those issues at creation time
in the form the sprint scheduler reads, and closes the original as decomposed —
and it runs only after an explicit operator ``accept`` at the preflight
complexity gate.

Three properties the shape of this module exists to hold:

* **Nothing is created without an explicit acceptance.** The only caller is the
  gate's ``accept`` branch. ``decline``, ``decompose``, ``approve``, a timeout
  and a configured no-decision fallback all reach a path that never imports
  this module, and the no-decision vocabulary is deliberately narrower than the
  offered one so an expiry cannot resolve to ``accept``.

* **The original closes last, or not at all.** Creation runs in dependency
  order; the source issue is closed only once every slice exists and every
  declared edge has been written into a created body. A failure partway through
  returns what *was* created, leaves the original open and runnable, and is
  reported as such — a half-applied split the operator cannot see is the worst
  outcome available here.

* **Re-entry does not duplicate.** The created slice-id → issue-number map is
  persisted by the caller and passed back in on a retry or a resume, so a run
  interrupted after two of four creates finishes the remaining two rather than
  filing four more. Every created body also carries a stable marker naming the
  source issue and slice id, so the mapping is recoverable by eye when the
  record is not.

Pure Python over ``gh``, no model involvement (convention 1). The rendering is
validated against the shared issue-shape gate before anything is created, so a
slice that would be unrunnable at creation refuses the application instead of
landing as tracker litter.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from theforge.config import ForgeConfig
    from theforge.task import TaskStory

#: Marker written into every created slice body. Stable and machine-findable:
#: it names the issue the slice came from and which slice of the proposal it is,
#: so a partially applied split is identifiable from the tracker alone.
DECOMPOSITION_MARKER = "forge-decomposition-v1"

#: ``preflight_decomposition_application_status`` values. There are only two:
#: a proposal that was never accepted records no status at all, which is what
#: keeps "declined" distinguishable from "accepted and failed".
APPLY_STATUS_APPLIED = "applied"
APPLY_STATUS_FAILED = "failed"

_GH_TIMEOUT_SECONDS = 60

_ISSUE_URL_RE = re.compile(r"/issues/(\d+)\s*$")


class ApplicationRefused(Exception):
    """The proposal cannot be applied, and nothing has been created.

    Raised only from the validation that runs *before* the first mutation, so a
    refusal is always a no-op refusal.
    """


@dataclass(frozen=True)
class CreatedSlice:
    """One slice of the proposal that now exists as an issue."""

    slice_id: int
    title: str
    issue_number: int
    depends_on_issues: tuple[int, ...] = ()
    #: True when this slice was already created by an earlier attempt and was
    #: reused rather than filed again.
    reused: bool = False

    def to_dict(self) -> dict:
        return {
            "slice_id": self.slice_id,
            "title": self.title,
            "issue": self.issue_number,
            "depends_on_issues": list(self.depends_on_issues),
            "reused": self.reused,
        }


@dataclass(frozen=True)
class ApplicationOutcome:
    """What the application did, as a record the operator and audit can read."""

    status: str
    created: tuple[CreatedSlice, ...] = ()
    source_issue: int | None = None
    source_issue_closed: bool = False
    error: str | None = None
    applied_at: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == APPLY_STATUS_APPLIED

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "created": [item.to_dict() for item in self.created],
            "source_issue": self.source_issue,
            "source_issue_closed": self.source_issue_closed,
            "error": self.error,
            "applied_at": self.applied_at,
        }

    def summary(self) -> str:
        """One line naming what exists now, for the terminal result message."""
        if self.created:
            created = ", ".join(f"#{item.issue_number}" for item in self.created)
        else:
            created = "nothing"
        closed = (
            f"closed #{self.source_issue} as decomposed"
            if self.source_issue_closed
            else f"#{self.source_issue} left open"
        )
        return f"created {created}; {closed}"


@dataclass(frozen=True)
class _Slice:
    """A validated slice of the recorded assessment payload."""

    slice_id: int
    title: str
    scope: str
    depends_on: tuple[int, ...] = ()
    covers_criteria: tuple[int, ...] = ()


@dataclass
class _Context:
    """Tracker facts the rendering needs, read once before anything is created."""

    type_label: str
    milestone: str | None = None
    extra_labels: tuple[str, ...] = ()
    acceptance_criteria: list[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ── The ``gh`` boundary ───────────────────────────────────────────────────────


def _run_gh(args: list[str], project_root: Path) -> subprocess.CompletedProcess[str]:
    """Run one full ``gh`` argv in the project root. The single seam tests substitute.

    ``args`` carries the ``gh`` token itself rather than having it prepended
    here, so every issue-creating argv in this module is one literal list: that
    is the form the repository's issue-body seam scan recognises, and a
    mutation it cannot see is a mutation no producer conformance guard covers.
    """
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=_GH_TIMEOUT_SECONDS,
        check=False,
    )


GhRunner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def _gh_or_raise(runner: GhRunner, args: list[str], project_root: Path, what: str) -> str:
    try:
        proc = runner(args, project_root)
    except Exception as exc:  # noqa: BLE001 - any transport failure is one failure
        raise RuntimeError(f"{what} failed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "no output"
        raise RuntimeError(f"{what} failed (exit {proc.returncode}): {detail}")
    return (proc.stdout or "").strip()


def _issue_number_from_create_output(text: str) -> int:
    """``gh issue create`` prints the new issue's URL; take its number."""
    for line in reversed([ln.strip() for ln in (text or "").splitlines() if ln.strip()]):
        match = _ISSUE_URL_RE.search(line)
        if match is not None:
            return int(match.group(1))
    raise RuntimeError(f"could not read an issue number from gh output: {text!r}")


# ── Applicability ─────────────────────────────────────────────────────────────


def applicable_type_labels() -> frozenset[str]:
    """Issue types whose shape a rendered slice can actually satisfy.

    Derived from the declarative issue specification rather than listed here, so
    a type added or re-shaped there is answered by this function without a
    second edit. A slice body states a scope boundary and the acceptance
    criteria it inherits, which is exactly the dispatchable, AC-shaped types —
    a ``bug`` slice would need an observed/expected/diagnosis body this module
    has no evidence to write, so a bug original is not appliable.
    """
    from theforge.shape_check.issue_spec import ISSUE_TYPES, Presence  # noqa: PLC0415

    return frozenset(
        spec.label
        for spec in ISSUE_TYPES
        if spec.dispatchable
        and spec.declares_type
        and spec.presence_of("acceptance_criteria") is Presence.REQUIRED
    )


def proposal_is_appliable(assessment: dict | None, task: "TaskStory") -> bool:
    """Whether an ``accept`` action may be offered for this pause at all.

    Three facts must hold, and each one is a way the mutation would otherwise
    fail *after* the operator committed to it: the assessment must actually
    describe a split, the story must be tracker-backed (a file-backed story has
    no issue to create siblings beside or to close), and its type must be one a
    rendered slice can be a well-shaped instance of.
    """
    if getattr(task, "github_issue", None) is None:
        return False
    if str(getattr(task, "type", "") or "").strip().lower() not in applicable_type_labels():
        return False
    try:
        _validated_slices(assessment)
    except ApplicationRefused:
        return False
    return True


# ── Validation and ordering ───────────────────────────────────────────────────


def _validated_slices(assessment: dict | None) -> list[_Slice]:
    """Parse the recorded assessment payload into slices, or refuse.

    The payload was already validated when it was produced, but it has been
    through a state field and a resume record since then and it is about to
    drive tracker mutations. Re-checking it here is the difference between a
    refusal and a half-applied split.
    """
    if not isinstance(assessment, dict):
        raise ApplicationRefused("no decomposition assessment is recorded for this story")
    raw_slices = assessment.get("slices")
    if not isinstance(raw_slices, list) or len(raw_slices) < 2:
        raise ApplicationRefused("the recorded assessment declares fewer than two slices")

    slices: list[_Slice] = []
    for index, entry in enumerate(raw_slices):
        if not isinstance(entry, dict):
            raise ApplicationRefused(
                f"slice {index + 1} in the recorded assessment is not a mapping"
            )
        try:
            slice_id = int(entry.get("id", index + 1))
        except (TypeError, ValueError) as exc:
            raise ApplicationRefused(f"slice {index + 1} has a non-integer id") from exc
        title = str(entry.get("title") or "").strip()
        scope = str(entry.get("scope") or "").strip()
        if not title:
            raise ApplicationRefused(f"slice {slice_id} has no title")
        if not scope:
            raise ApplicationRefused(f"slice {slice_id} has no scope boundary")
        depends_on: list[int] = []
        for dep in entry.get("depends_on") or ():
            try:
                depends_on.append(int(dep))
            except (TypeError, ValueError) as exc:
                raise ApplicationRefused(
                    f"slice {slice_id} declares a non-integer dependency {dep!r}"
                ) from exc
        covers: list[int] = []
        for index_value in entry.get("covers_criteria") or ():
            try:
                covers.append(int(index_value))
            except (TypeError, ValueError):
                continue
        slices.append(
            _Slice(
                slice_id=slice_id,
                title=title,
                scope=scope,
                depends_on=tuple(dict.fromkeys(depends_on)),
                covers_criteria=tuple(dict.fromkeys(covers)),
            )
        )

    declared = {item.slice_id for item in slices}
    if len(declared) != len(slices):
        raise ApplicationRefused("the recorded assessment reuses a slice id")
    for item in slices:
        for dep in item.depends_on:
            if dep == item.slice_id:
                raise ApplicationRefused(f"slice {item.slice_id} depends on itself")
            if dep not in declared:
                raise ApplicationRefused(
                    f"slice {item.slice_id} depends on undeclared slice {dep}"
                )
    return slices


def dependency_order(slices: list[_Slice]) -> list[_Slice]:
    """Order slices so every dependency is created before its dependants.

    Creation order is not cosmetic: an edge is written into a dependant's body
    *at creation time*, which is only possible once the upstream issue has a
    number. A cycle therefore has no application at all and refuses before the
    first create.
    """
    by_id = {item.slice_id: item for item in slices}
    remaining = dict(by_id)
    ordered: list[_Slice] = []
    placed: set[int] = set()
    while remaining:
        ready = [
            item
            for item in sorted(remaining.values(), key=lambda s: s.slice_id)
            if all(dep in placed for dep in item.depends_on)
        ]
        if not ready:
            cycle = ", ".join(str(i) for i in sorted(remaining))
            raise ApplicationRefused(
                f"the declared dependency edges form a cycle among slices {cycle}; "
                "no creation order writes every edge at creation time"
            )
        for item in ready:
            ordered.append(item)
            placed.add(item.slice_id)
            del remaining[item.slice_id]
    return ordered


# ── Rendering ─────────────────────────────────────────────────────────────────


def _frontmatter(dep_issues: list[int]) -> str:
    """The scheduler-readable dependency declaration, or nothing.

    Leading YAML frontmatter with ``depends_on: [issue-N]`` is the one form the
    sprint's GitHub source parses as a hard edge (``sprint.sources``); a comment
    added afterwards is invisible to it, which is exactly why the edges are
    written here rather than in a follow-up pass.
    """
    if not dep_issues:
        return ""
    lines = ["---", "depends_on:"]
    lines.extend(f"  - issue-{number}" for number in dep_issues)
    lines.extend(["---", ""])
    return "\n".join(lines)


def render_slice_body(
    *,
    item: _Slice,
    position: int,
    total: int,
    source_issue: int,
    dep_issues: list[int],
    context: _Context,
    slice_titles: dict[int, str],
    created_numbers: dict[int, int],
) -> str:
    """The body of one created slice issue.

    Carries the four things a reader of the new issue needs and the scheduler
    reads two of: the declared edges as frontmatter, the slice's scope boundary,
    the prose half of each edge (what this slice consumes from the upstream one,
    per the repository's dependency-declaration convention), and the original
    acceptance criteria this slice was assessed to cover.
    """
    parts: list[str] = []
    frontmatter = _frontmatter(dep_issues)
    if frontmatter:
        parts.append(frontmatter)
    parts.append(f"<!-- {DECOMPOSITION_MARKER} source={source_issue} slice={item.slice_id} -->")
    parts.append(
        f"Slice {position} of {total}, split from #{source_issue} by TheForge's preflight "
        "complexity gate after the operator accepted its decomposition proposal."
    )
    parts.append("## What")
    parts.append(item.scope)
    parts.append("## Why")
    parts.append(
        f"#{source_issue} was scored past the preflight complexity gate's threshold and "
        "split rather than run as one story. This slice is one piece of that split; "
        f"#{source_issue} states the motivation in full."
    )
    for dep in item.depends_on:
        dep_issue = created_numbers.get(dep)
        if dep_issue is None:
            continue
        parts.append(
            f"Depends on #{dep_issue} ({slice_titles.get(dep, 'upstream slice')}): this slice "
            "consumes what that one produces, and building it first would mean re-deriving "
            "that work here."
        )
    parts.append("## Acceptance criteria")
    criteria = [
        context.acceptance_criteria[index - 1]
        for index in item.covers_criteria
        if 1 <= index <= len(context.acceptance_criteria)
    ]
    if criteria:
        parts.append("\n".join(f"- {text}" for text in criteria))
    else:
        # The assessment mapped no original criterion onto this slice — that is
        # a proposal the parser would have refused, but a recorded payload can
        # still reach here with an empty story body. Stating the boundary as the
        # criterion keeps the issue checkable rather than shipping an empty
        # section that reads as "no criteria".
        parts.append(f"- {item.scope}")
    parts.append("## Example")
    example = ["```", f"#{source_issue} split into {total} slices; this is slice {position}."]
    for other_id, title in slice_titles.items():
        marker = " <- this issue" if other_id == item.slice_id else ""
        number = created_numbers.get(other_id)
        ref = f"#{number}" if number is not None else "(pending)"
        example.append(f"  {other_id}. {title} -> {ref}{marker}")
    example.append("```")
    parts.append("\n".join(example))
    return "\n\n".join(parts) + "\n"


def _closing_comment(*, source_issue: int, created: list[CreatedSlice], is_spike: bool) -> str:
    """The durable record left on the original as it is closed.

    Says what replaced it and where, so the closure is legible from the issue
    alone rather than only from a run audit. For a spike original it also
    carries the recorded outcome the closure guard requires, naming the first
    created slice as the follow-on — a spike that has been split has answered
    its question with work, and closing it with nothing recorded is the decay
    that guard exists to prevent.
    """
    lines = [
        "TheForge applied an accepted decomposition proposal to this issue.",
        "",
        "The preflight complexity gate scored this story past its threshold, produced a "
        "candidate split, and the operator accepted it. The work now lives in:",
        "",
    ]
    lines.extend(f"- #{item.issue_number} — {item.title}" for item in created)
    lines.extend(
        [
            "",
            "Closing as decomposed, not as completed: nothing here was implemented, and "
            "the slices above carry the acceptance criteria this issue declared.",
        ]
    )
    if is_spike and created:
        from theforge.spike_guard import OUTCOME_MARKER  # noqa: PLC0415

        lines.extend(
            [
                "",
                f"<!-- {OUTCOME_MARKER}",
                "outcome: follow_up",
                f"follow-up: #{created[0].issue_number}",
                "-->",
            ]
        )
    return "\n".join(lines)


# ── Tracker context ───────────────────────────────────────────────────────────


def _read_context(*, task: "TaskStory", runner: GhRunner, project_root: Path) -> _Context:
    """Read what the created issues must inherit: type label and milestone.

    A created slice with no recognized type label is filtered out by intake and
    skipped by the sprint shape gate — it would exist and never be schedulable,
    which is precisely the failure "runnable at creation" names. The label is
    therefore a precondition of applying, not a nicety, and it is read from the
    tracker rather than assumed.
    """
    from theforge.task.story import extract_acceptance_criteria  # noqa: PLC0415

    number = task.github_issue
    assert number is not None  # guarded by proposal_is_appliable
    labels: list[str] = []
    milestone: str | None = None
    raw = _gh_or_raise(
        runner,
        ["gh", "issue", "view", str(number), "--json", "labels,milestone"],
        project_root,
        f"gh issue view #{number}",
    )
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise ApplicationRefused(
            f"gh issue view #{number} returned malformed JSON: {exc}"
        ) from exc
    if isinstance(data, dict):
        for entry in data.get("labels") or []:
            if isinstance(entry, dict) and str(entry.get("name") or "").strip():
                labels.append(str(entry["name"]).strip())
            elif isinstance(entry, str) and entry.strip():
                labels.append(entry.strip())
        milestone_data = data.get("milestone")
        if isinstance(milestone_data, dict):
            milestone = str(milestone_data.get("title") or "").strip() or None

    appliable = applicable_type_labels()
    type_labels = [name for name in labels if name.lower() in appliable]
    if len(type_labels) != 1:
        raise ApplicationRefused(
            f"#{number} carries {len(type_labels)} appliable type labels "
            f"({', '.join(sorted(type_labels)) or 'none'}); a created slice needs exactly "
            f"one of {', '.join(sorted(appliable))} to be runnable at creation"
        )
    return _Context(
        type_label=type_labels[0],
        milestone=milestone,
        acceptance_criteria=extract_acceptance_criteria(task.story_text or ""),
    )


# ── The application itself ────────────────────────────────────────────────────


def _validate_body(*, title: str, body: str, labels: list[str]) -> None:
    from theforge.shape_check.producer import validate_issue_body  # noqa: PLC0415
    from theforge.shape_check.types import ShapeVerdict  # noqa: PLC0415

    # The producer id is written as a literal rather than through a constant:
    # the repository's producer-conformance guard reads the declaration out of
    # this function's source, and an indirection would hide the seam from it.
    validation = validate_issue_body(
        producer="forge-decomposition-apply",
        title=title,
        body=body,
        labels=labels,
        declared=ShapeVerdict.RUNNABLE,
    )
    if not validation.conforms:
        raise ApplicationRefused(
            f"the rendered slice {title!r} would not be runnable at creation: "
            f"{validation.report()}"
        )


def _prior_map(prior_created: object) -> dict[int, CreatedSlice]:
    """Rebuild the slice-id → created-issue map an earlier attempt persisted."""
    out: dict[int, CreatedSlice] = {}
    for entry in prior_created or ():
        if not isinstance(entry, dict):
            continue
        try:
            slice_id = int(entry.get("slice_id"))
            issue_number = int(entry.get("issue"))
        except (TypeError, ValueError):
            continue
        out[slice_id] = CreatedSlice(
            slice_id=slice_id,
            title=str(entry.get("title") or ""),
            issue_number=issue_number,
            depends_on_issues=tuple(
                int(n)
                for n in entry.get("depends_on_issues") or ()
                if str(n).lstrip("-").isdigit()
            ),
            reused=True,
        )
    return out


def _may_close(
    *, number: int, project_root: Path, story_type: str | None, closing_comment: str
) -> tuple[bool, str]:
    """Route the close through the repository-wide spike closure guard."""
    from theforge.spike_guard import check_spike_closure  # noqa: PLC0415

    decision = check_spike_closure(
        number,
        project_root,
        known_type=story_type,
        closing_comment=closing_comment,
    )
    return bool(decision.allowed), str(getattr(decision, "reason", "") or "")


def apply_decomposition(
    *,
    assessment: dict | None,
    task: "TaskStory",
    config: "ForgeConfig",
    prior_created: object = (),
    source_issue_already_closed: bool = False,
    runner: GhRunner | None = None,
) -> ApplicationOutcome:
    """Create the proposal's slices, write its edges, and close the original.

    Returns an :class:`ApplicationOutcome` in every case — this never raises for
    a tracker failure, because the caller's job on failure is to *report* what
    exists, not to unwind it. ``prior_created`` carries what an earlier attempt
    already filed, which makes re-entry after a partial failure additive rather
    than duplicating.
    """
    runner = runner or _run_gh
    project_root = Path(config.project_root)
    source_issue = getattr(task, "github_issue", None)
    created_records = _prior_map(prior_created)
    created: list[CreatedSlice] = []

    try:
        if source_issue is None:
            raise ApplicationRefused(
                "this story is not backed by a GitHub issue, so a split cannot be applied"
            )
        slices = dependency_order(_validated_slices(assessment))
        if source_issue_already_closed and all(
            item.slice_id in created_records for item in slices
        ):
            # Already applied in full by an earlier attempt. Returning here
            # rather than after re-reading the tracker keeps a resumed run's
            # re-entry free of any gh call at all — there is nothing left to do
            # and nothing to check that the record does not already say.
            return ApplicationOutcome(
                status=APPLY_STATUS_APPLIED,
                created=tuple(created_records[item.slice_id] for item in slices),
                source_issue=source_issue,
                source_issue_closed=True,
                applied_at=_now_iso(),
            )
        context = _read_context(task=task, runner=runner, project_root=project_root)
    except ApplicationRefused as exc:
        return ApplicationOutcome(
            status=APPLY_STATUS_FAILED,
            created=tuple(created_records.values()),
            source_issue=source_issue,
            source_issue_closed=source_issue_already_closed,
            error=str(exc),
            applied_at=_now_iso(),
        )
    except Exception as exc:  # noqa: BLE001 - a read failure is a clean refusal too
        return ApplicationOutcome(
            status=APPLY_STATUS_FAILED,
            created=tuple(created_records.values()),
            source_issue=source_issue,
            source_issue_closed=source_issue_already_closed,
            error=str(exc),
            applied_at=_now_iso(),
        )

    slice_titles = {item.slice_id: item.title for item in slices}
    numbers = {sid: record.issue_number for sid, record in created_records.items()}
    labels = [context.type_label, *context.extra_labels]

    try:
        for position, item in enumerate(slices, start=1):
            existing = created_records.get(item.slice_id)
            if existing is not None:
                # Filed by an earlier attempt. Reused rather than re-created:
                # the whole point of persisting the map is that a retry after a
                # partial failure finishes the split instead of doubling it.
                created.append(existing)
                continue
            dep_issues = [numbers[dep] for dep in item.depends_on if dep in numbers]
            if len(dep_issues) != len(item.depends_on):
                raise ApplicationRefused(
                    f"slice {item.slice_id} declares an edge to a slice that has no issue yet"
                )
            body = render_slice_body(
                item=item,
                position=position,
                total=len(slices),
                source_issue=source_issue,
                dep_issues=dep_issues,
                context=context,
                slice_titles=slice_titles,
                created_numbers=numbers,
            )
            _validate_body(title=item.title, body=body, labels=labels)
            args = [
                "gh",
                "issue",
                "create",
                "--title",
                item.title,
                "--body",
                body,
                "--label",
                ",".join(labels),
            ]
            if context.milestone:
                args.extend(["--milestone", context.milestone])
            output = _gh_or_raise(
                runner, args, project_root, f"gh issue create for slice {item.slice_id}"
            )
            issue_number = _issue_number_from_create_output(output)
            record = CreatedSlice(
                slice_id=item.slice_id,
                title=item.title,
                issue_number=issue_number,
                depends_on_issues=tuple(dep_issues),
            )
            created.append(record)
            created_records[item.slice_id] = record
            numbers[item.slice_id] = issue_number
    except Exception as exc:  # noqa: BLE001 - report the partial state, never unwind
        return ApplicationOutcome(
            status=APPLY_STATUS_FAILED,
            created=tuple(created),
            source_issue=source_issue,
            source_issue_closed=source_issue_already_closed,
            error=str(exc),
            applied_at=_now_iso(),
        )

    if source_issue_already_closed:
        return ApplicationOutcome(
            status=APPLY_STATUS_APPLIED,
            created=tuple(created),
            source_issue=source_issue,
            source_issue_closed=True,
            applied_at=_now_iso(),
        )

    comment = _closing_comment(
        source_issue=source_issue,
        created=created,
        is_spike=context.type_label.lower() == "spike",
    )
    try:
        may_close, why = _may_close(
            number=source_issue,
            project_root=project_root,
            story_type=context.type_label,
            closing_comment=comment,
        )
        if not may_close:
            raise RuntimeError(f"the spike closure guard refused the close: {why}")
        _gh_or_raise(
            runner,
            [
                "gh",
                "issue",
                "close",
                str(source_issue),
                "--comment",
                comment,
                "--reason",
                "not planned",
            ],
            project_root,
            f"gh issue close #{source_issue}",
        )
    except Exception as exc:  # noqa: BLE001
        return ApplicationOutcome(
            status=APPLY_STATUS_FAILED,
            created=tuple(created),
            source_issue=source_issue,
            source_issue_closed=False,
            error=(f"every slice was created but #{source_issue} could not be closed: {exc}"),
            applied_at=_now_iso(),
        )

    return ApplicationOutcome(
        status=APPLY_STATUS_APPLIED,
        created=tuple(created),
        source_issue=source_issue,
        source_issue_closed=True,
        applied_at=_now_iso(),
    )
