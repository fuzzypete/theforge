"""Decomposition assessment: pure-data contract, parser, and rendering (#2686).

The preflight complexity gate (#2681) stops an over-broad story before anything
past preflight is charged and asks the operator "approve, or decompose?". Asking
that question while showing nothing transfers forge's uncertainty to the
operator: answering it well means doing the split by hand, from evidence forge
already holds.

This module is the artifact that goes on the pause next to the question. An
assessment names **candidate slices** — each with a title and a scope boundary —
declares the **dependency edges** between them, states how the original story's
**acceptance criteria distribute** across them, and states the **decisions it
could not settle**. It is advisory and non-mutating: nothing here creates,
edits, or closes anything, and the original story stays intact and runnable
whichever way the pause is answered. Applying an assessment is separate work
(#2824).

Deliberately *not* the escalation advisor's vocabulary. The advisor already
carries ``decompose`` among its actions and has never selected it across
recorded escalations, so this artifact does not route into that taxonomy or
recommend an action at all. It describes a possible split; the operator still
answers the same two-action pause.

Layering follows the repo's split: this module is the pure-data + schema
integrity boundary (stdlib + yaml), prompt construction lives in
``theforge.task.decomposition_assessment_prompts``, and the coordinator control
flow that invokes the agent lives in
``theforge.coordinator.preflight_decomposition_flow``.

**No assessment is a first-class outcome.** A story that is genuinely atomic
despite a high score, an agent that could not be launched, and output that fails
validation all resolve to the same shape: no assessment, plus a recorded reason
why none was produced. That reason is rendered on the pause and written to the
audit. It never blocks the pause from being answered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

_OPEN_TAG = "<decomposition_assessment>"
_CLOSE_TAG = "</decomposition_assessment>"
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)

# Cap free-text fields so a runaway agent cannot balloon the pending record the
# operator has to read.
_FIELD_MAX_LEN = 600

# A "split" into a single slice is not a split — it is the original story with a
# new title. Two is the smallest assessment that can say anything.
MIN_SLICES = 2

# ── Reasons no assessment was produced ────────────────────────────────────────
#
# Every one of these is a recorded statement on the pause and in the audit. They
# are phrased as what happened, not as an apology, because the operator reads
# them while deciding.
NONE_ATOMIC = "the assessment judged the story atomic — it found no boundary to split on"
NONE_NO_BLOCK = "the assessment produced no <decomposition_assessment> block"
NONE_INVALID_OUTPUT = "the assessment output failed validation"
NONE_AGENT_FAILED = "the assessment agent returned failure before a usable artifact"
NONE_LAUNCH_FAILURE = "the assessment agent never launched"
NONE_UNAVAILABLE = "no assessment agent could be invoked"
NONE_NOT_ATTEMPTED = "no assessment was attempted"


@dataclass(frozen=True)
class AssessmentPacket:
    """What a decomposition assessment reads.

    Everything here was already assembled by preflight before the gate opened —
    the point of the artifact is that the split is derivable from evidence forge
    *already holds*, without paying for planning first.
    """

    story_name: str
    issue_ref: str
    story_body: str
    acceptance_criteria: list[str]
    complexity_score: int | None
    implementation_complexity_score: int | None
    validation_complexity_score: int | None
    scope_exceeded: bool = False
    score_provenance_note: str | None = None
    likely_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    criteria_checked: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "story_name": self.story_name,
            "issue_ref": self.issue_ref,
            "story_body": self.story_body,
            "acceptance_criteria": list(self.acceptance_criteria),
            "complexity_score": self.complexity_score,
            "implementation_complexity_score": self.implementation_complexity_score,
            "validation_complexity_score": self.validation_complexity_score,
            "scope_exceeded": bool(self.scope_exceeded),
            "score_provenance_note": self.score_provenance_note,
            "likely_files": list(self.likely_files),
            "warnings": list(self.warnings),
            "criteria_checked": list(self.criteria_checked),
        }


@dataclass(frozen=True)
class CandidateSlice:
    """One candidate story the original could be split into.

    ``scope`` is the boundary, not a restatement of the title: what this slice
    covers and — where the assessment can say it — what it deliberately leaves
    to another slice. ``covers_criteria`` holds 1-based indices into the
    original story's acceptance criteria, which is what makes "a criterion
    cannot be silently dropped by the split" checkable rather than asserted.
    """

    slice_id: int
    title: str
    scope: str
    depends_on: tuple[int, ...] = ()
    covers_criteria: tuple[int, ...] = ()

    def to_dict(self) -> dict:
        return {
            "id": self.slice_id,
            "title": self.title,
            "scope": self.scope,
            "depends_on": list(self.depends_on),
            "covers_criteria": list(self.covers_criteria),
        }


@dataclass(frozen=True)
class DecompositionAssessment:
    """A validated candidate split of one story.

    ``unsettled`` is not optional decoration: an assessment that presents a
    contested boundary as settled is worse than none, because the operator
    cannot tell which edges were actually derived from evidence.
    """

    slices: tuple[CandidateSlice, ...]
    unsettled: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "slices": [s.to_dict() for s in self.slices],
            "unsettled": list(self.unsettled),
        }


@dataclass(frozen=True)
class AssessmentResult:
    """The outcome of one assessment attempt: an artifact, or a recorded absence.

    Exactly one of the two is meaningful. ``assessment`` is None whenever no
    usable artifact survived, and ``none_produced_reason`` then says why in
    operator-facing prose. ``validation_errors`` carries the parser's own errors
    for the audit when the reason was invalid output.
    """

    assessment: DecompositionAssessment | None = None
    none_produced_reason: str | None = None
    validation_errors: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    @property
    def produced(self) -> bool:
        return self.assessment is not None


def no_assessment(
    reason: str, *, errors: tuple[str, ...] = (), raw: dict | None = None
) -> AssessmentResult:
    """The result for an attempt that produced no artifact, with the reason recorded."""
    return AssessmentResult(
        assessment=None,
        none_produced_reason=reason,
        validation_errors=errors,
        raw=raw or {},
    )


def _truncate(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > _FIELD_MAX_LEN:
        return text[: _FIELD_MAX_LEN - 1] + "…"
    return text


def _extract_block(text: str) -> str | None:
    """Return the YAML body inside the assessment tags, or None.

    Fenced code blocks are stripped first so an agent quoting the schema in
    prose does not produce a false match — the same discipline the advisory
    report parser keeps.
    """
    stripped = _FENCED_BLOCK_RE.sub("", text or "")
    open_pos = stripped.find(_OPEN_TAG)
    close_pos = stripped.find(_CLOSE_TAG)
    if open_pos < 0 or close_pos < open_pos + len(_OPEN_TAG):
        return None
    body = stripped[open_pos + len(_OPEN_TAG) : close_pos].strip()
    return body or None


def _int_list(value: object) -> tuple[list[int], bool]:
    """Coerce a YAML scalar-or-list into ints; the flag reports a malformed entry."""
    if value is None:
        return [], False
    items = value if isinstance(value, list) else [value]
    out: list[int] = []
    bad = False
    for item in items:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            bad = True
    return out, bad


def _parse_slices(raw_slices: object, errors: list[str]) -> list[CandidateSlice]:
    slices: list[CandidateSlice] = []
    if not isinstance(raw_slices, list):
        errors.append("slices must be a list")
        return slices
    for i, entry in enumerate(raw_slices):
        if not isinstance(entry, dict):
            errors.append(f"slices[{i}] must be a mapping")
            continue
        try:
            slice_id = int(entry.get("id"))
        except (TypeError, ValueError):
            errors.append(f"slices[{i}].id must be an integer")
            continue
        title = _truncate(entry.get("title"))
        scope = _truncate(entry.get("scope") or entry.get("scope_boundary"))
        if not title:
            errors.append(f"slices[{i}] (id {slice_id}) missing title")
        if not scope:
            errors.append(f"slices[{i}] (id {slice_id}) missing scope boundary")
        depends_on, bad_depends = _int_list(entry.get("depends_on"))
        if bad_depends:
            errors.append(f"slices[{i}] (id {slice_id}) depends_on must be slice ids")
        covers, bad_covers = _int_list(entry.get("covers_criteria") or entry.get("covers"))
        if bad_covers:
            errors.append(
                f"slices[{i}] (id {slice_id}) covers_criteria must be acceptance-criterion indices"
            )
        slices.append(
            CandidateSlice(
                slice_id=slice_id,
                title=title,
                scope=scope,
                depends_on=tuple(dict.fromkeys(depends_on)),
                covers_criteria=tuple(dict.fromkeys(covers)),
            )
        )
    return slices


def _validate_dependencies(slices: list[CandidateSlice], errors: list[str]) -> None:
    """Every declared edge must point at a declared slice, and never at itself."""
    declared = {s.slice_id for s in slices}
    if len(declared) != len(slices):
        errors.append("slice ids must be unique")
    for s in slices:
        for dep in s.depends_on:
            if dep == s.slice_id:
                errors.append(f"slice {s.slice_id} depends on itself")
            elif dep not in declared:
                errors.append(f"slice {s.slice_id} depends on undeclared slice {dep}")


def _validate_criteria_coverage(
    slices: list[CandidateSlice], criteria: list[str], errors: list[str]
) -> None:
    """Every original acceptance criterion must land in at least one slice.

    This is the check that makes the artifact worth reading: a split that drops
    a criterion is not a split of *this* story, and the operator cannot be
    expected to diff the criteria by hand while deciding. An out-of-range index
    fails for the same reason — it claims coverage of a criterion that does not
    exist while some real one goes uncovered.
    """
    if not criteria:
        # Nothing to distribute. The split is still checkable on its other axes,
        # and inventing a coverage failure here would refuse assessments for
        # stories whose criteria could not be extracted.
        return
    valid = set(range(1, len(criteria) + 1))
    covered: set[int] = set()
    for s in slices:
        for index in s.covers_criteria:
            if index not in valid:
                errors.append(
                    f"slice {s.slice_id} covers acceptance criterion {index}, "
                    f"which does not exist (1-{len(criteria)})"
                )
            else:
                covered.add(index)
    missing = sorted(valid - covered)
    if missing:
        errors.append(
            "acceptance criteria "
            + ", ".join(str(i) for i in missing)
            + " are covered by no slice — the split would drop them"
        )


def _parse_unsettled(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    return tuple(text for text in (_truncate(item) for item in items) if text)


def parse_decomposition_assessment(text: str, criteria: list[str]) -> AssessmentResult:
    """Parse and validate assessment output against the original criteria.

    This is the schema integrity boundary. An assessment survives only when:

    * a single ``<decomposition_assessment>`` block parses as a YAML mapping,
    * it declares at least :data:`MIN_SLICES` slices, each with a non-empty
      title and scope boundary,
    * every ``depends_on`` edge names a declared slice and not itself, and
    * every acceptance criterion passed in appears in at least one slice.

    Anything else returns "no assessment" with the reason recorded, because a
    partially-valid split is exactly the artifact an operator would act on
    without noticing what it dropped. ``atomic: true`` is a *result*, not a
    failure: it is how a genuinely indivisible story is reported.
    """
    body = _extract_block(text)
    if body is None:
        return no_assessment(NONE_NO_BLOCK)

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        return no_assessment(
            NONE_INVALID_OUTPUT, errors=(f"YAML parse error in assessment: {exc}",)
        )

    if not isinstance(data, dict):
        return no_assessment(
            NONE_INVALID_OUTPUT,
            errors=(f"assessment must be a YAML mapping, got {type(data).__name__}",),
        )

    if bool(data.get("atomic")):
        detail = _truncate(data.get("atomic_reason") or data.get("reason"))
        reason = f"{NONE_ATOMIC}: {detail}" if detail else NONE_ATOMIC
        return no_assessment(reason, raw=data)

    errors: list[str] = []
    slices = _parse_slices(data.get("slices"), errors)
    if len(slices) < MIN_SLICES:
        errors.append(
            f"a split needs at least {MIN_SLICES} slices; got {len(slices)} "
            "(report atomic: true instead when there is no boundary to split on)"
        )
    else:
        _validate_dependencies(slices, errors)
        _validate_criteria_coverage(slices, criteria, errors)

    if errors:
        return no_assessment(NONE_INVALID_OUTPUT, errors=tuple(errors), raw=data)

    return AssessmentResult(
        assessment=DecompositionAssessment(
            slices=tuple(slices),
            unsettled=_parse_unsettled(data.get("unsettled")),
            raw=data,
        ),
        raw=data,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────


def render_assessment_lines(assessment: DecompositionAssessment) -> list[str]:
    """Render the assessment as the compact block that goes on the pause.

    One line per slice, in declaration order, so the shape of the split is
    legible at a glance; the unsettled decisions follow, because an operator who
    reads only the slice list and stops has read the part that looks settled.
    """
    lines = [f"Decomposition assessment ({len(assessment.slices)} candidate slices):"]
    for index, item in enumerate(assessment.slices, start=1):
        parts = [f"  {index}. {item.title} — scope: {item.scope}"]
        if item.depends_on:
            parts.append("depends_on: " + ", ".join(str(d) for d in item.depends_on))
        if item.covers_criteria:
            parts.append("covers AC " + ", ".join(str(c) for c in item.covers_criteria))
        lines.append("     ".join(parts))
    if assessment.unsettled:
        lines.append("")
        lines.append("  Unsettled:")
        for item_text in assessment.unsettled:
            lines.append(f"    - {item_text}")
    return lines
