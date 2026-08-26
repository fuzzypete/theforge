"""Render shape proposals as operator-facing text + body restructure.

Pure text-shaping helpers, no I/O. The repaired body is not assembled by
string-appending: it is built by parsing the current body into a typed
:class:`~theforge.shape_check.document.IssueDocument`, adding the modeled
sections the specification asks for, and rendering that document back through
the one canonical renderer (ADR-0009 clause 7 — every producer renders through
the specification).

Two properties follow, and both are what #2053 broke: a body that already
conforms is returned byte-for-byte unchanged, and everything the specification
does not model — the operator's prose, worked examples, asides, headings we
have never heard of — is written back intact.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from dataclasses import dataclass

from theforge.intake.shape_classify import (
    Classification,
    Confidence,
    DiagnosisState,
    ShapeProposal,
)
from theforge.shape_check.check import check as shape_gate_check
from theforge.shape_check.diagnosis_spec import (
    REQUIRED_DIAGNOSIS_COMPONENTS,
)
from theforge.shape_check.document import (
    parse_issue_document,
    render_issue_document,
    with_section,
)
from theforge.shape_check.heuristics import (
    DIAGNOSIS_CANONICAL_HEADING_TEXTS,
    DIAGNOSIS_HEADING_PATTERN,
    diagnosis_completeness,
    diagnosis_completeness_score,
)
from theforge.shape_check.issue_spec import (
    ACCEPTANCE_CRITERIA_SECTION,
    BUG_SPEC,
    DIAGNOSIS_SECTION,
    ENHANCEMENT_SPEC,
    EXPECTED_SECTION,
    OBSERVED_SECTION,
)
from theforge.shape_check.parsing import (
    ACCEPTANCE_CRITERIA_HEADING_PATTERN,
    authoritative_section_span,
    find_authoritative_heading,
    has_heading,
)
from theforge.shape_check.types import ShapeVerdict

# Presence is decided by the specification's recognition patterns — the same
# ones the gate probes with — and heading-level-agnostically, so a conformant
# ``## Observed behavior`` counts as present (#2053: a private substring probe
# for a hard-coded heading level is exactly the producer/validator drift the
# specification exists to eliminate).
_OBSERVED_HEADING_PATTERN = OBSERVED_SECTION.recognition_pattern
_EXPECTED_HEADING_PATTERN = EXPECTED_SECTION.recognition_pattern


def restructure_body(proposal: ShapeProposal, current_body: str) -> str:
    """Return the proposed restructured body for the issue, or ``current_body``
    unchanged when no restructure is proposed.

    The restructure is strictly additive: it adds the modeled sections the
    specification asks of this type and nothing else. Existing operator
    structure is never rewritten, re-levelled, or demoted into quoted prose — a
    remediation offered toward a gate must be a no-op on input that gate already
    accepts, and additive on input that is partially conformant (#2053).
    """
    current_body = current_body or ""
    if proposal.classification is Classification.BUG:
        return _restructure_bug(proposal, current_body)
    if proposal.classification is Classification.ENHANCEMENT:
        return _restructure_enhancement(current_body)
    if proposal.classification is Classification.UNRESOLVED:
        return current_body
    return current_body


def _restructure_bug(proposal: ShapeProposal, current_body: str) -> str:
    body = current_body
    missing_observed = not has_heading(body, _OBSERVED_HEADING_PATTERN)
    missing_expected = not has_heading(body, _EXPECTED_HEADING_PATTERN)

    # The gate is the authority on what a complete Diagnosis section is, so ask
    # it directly instead of probing for a heading string of our own choosing.
    # The heading it locates must be the same one the gate itself reads — a raw
    # first-match probe here would fill gaps into an earlier lookalike heading
    # while the real Diagnosis section stays untouched and incomplete (#2263).
    complete, missing = diagnosis_completeness(body)
    authoritative = (
        None
        if complete
        else find_authoritative_heading(
            body,
            DIAGNOSIS_HEADING_PATTERN,
            DIAGNOSIS_CANONICAL_HEADING_TEXTS,
            diagnosis_completeness_score,
        )
    )

    if complete and not missing_observed and not missing_expected:
        # Nothing to repair. A conforming document is not rewritten because a
        # producer touched it, so it is returned verbatim rather than re-rendered
        # — recognizing a legacy spelling on input is not a licence to rewrite it.
        return body

    if not complete and authoritative is not None:
        # Section exists but is incomplete: add only the missing components,
        # in place, leaving every original heading at its original level.
        body = _fill_diagnosis_gaps(body, missing)

    document = parse_issue_document(body, type_spec=BUG_SPEC)
    if missing_observed:
        document = with_section(
            document, OBSERVED_SECTION.key, "<fill from current body>", type_spec=BUG_SPEC
        )
    if missing_expected:
        document = with_section(
            document,
            EXPECTED_SECTION.key,
            "<state the category-level rule that should hold here>",
            type_spec=BUG_SPEC,
        )
    if not complete and authoritative is None:
        document = with_section(
            document, DIAGNOSIS_SECTION.key, _diagnosis_section_body(proposal), type_spec=BUG_SPEC
        )
    return render_issue_document(document)


def _diagnosis_status_line(proposal: ShapeProposal) -> str:
    state = proposal.diagnosis_state or DiagnosisState.NO_DIAGNOSIS
    if state is DiagnosisState.NO_DIAGNOSIS:
        return "Status: no diagnosis yet. Next step: run `forge diagnose`."
    if state is DiagnosisState.DIAGNOSIS_CAUSE_UNKNOWN:
        return "Status: investigation in progress; cause unknown. Next step: continue diagnosis."
    return "Status: confirmed cause documented."


def _diagnosis_bullets(missing_tokens: list[str]) -> list[str]:
    """Placeholder bullets for the components the gate reports missing.

    Labels come from :mod:`theforge.shape_check.diagnosis_spec` so the scaffold
    names exactly the components the validator scans for.
    """
    tokens = {token.lower() for token in missing_tokens}
    return [
        f"- **{component.label}:** <fill in>"
        for component in REQUIRED_DIAGNOSIS_COMPONENTS
        if component.token in tokens
    ]


def _diagnosis_section_body(proposal: ShapeProposal) -> str:
    """The content of a whole ``## Diagnosis`` scaffold, without its heading.

    The heading itself is the renderer's job — it comes from the specification,
    so the scaffold cannot be written at a level the gate does not read (#2053).
    """
    lines = [_diagnosis_status_line(proposal), ""]
    lines.extend(_diagnosis_bullets([c.token for c in REQUIRED_DIAGNOSIS_COMPONENTS]))
    return "\n".join(lines) + "\n"


def _fill_diagnosis_gaps(body: str, missing_tokens: list[str]) -> str:
    """Append the missing component bullets to the end of the Diagnosis section."""
    span = authoritative_section_span(
        body,
        DIAGNOSIS_HEADING_PATTERN,
        DIAGNOSIS_CANONICAL_HEADING_TEXTS,
        diagnosis_completeness_score,
    )
    bullets = _diagnosis_bullets(missing_tokens)
    if span is None or not bullets:
        return body
    head = body[: span[1]].rstrip("\n")
    tail = body[span[1] :].lstrip("\n")
    block = "\n".join(bullets)
    if tail:
        return f"{head}\n{block}\n\n{tail}"
    return f"{head}\n{block}\n"


def _restructure_enhancement(current_body: str) -> str:
    if has_heading(current_body, ACCEPTANCE_CRITERIA_HEADING_PATTERN):
        return current_body
    document = parse_issue_document(current_body, type_spec=ENHANCEMENT_SPEC)
    document = with_section(
        document,
        ACCEPTANCE_CRITERIA_SECTION.key,
        "- <state an observable, testable outcome>",
        type_spec=ENHANCEMENT_SPEC,
    )
    return render_issue_document(document)


def render_proposal(
    proposal: ShapeProposal,
    *,
    source_label: str,
    current_body: str = "",
    show_diff: bool = True,
) -> str:
    """Render the proposal as a human-readable block for stdout."""
    lines: list[str] = []
    lines.append(f"Loaded: {source_label}")
    lines.append("")
    if proposal.classification is Classification.UNRESOLVED:
        lines.append("Classification: unresolved")
        lines.append("Kept as: todo:draft")
        if proposal.rationale:
            lines.append(f"Reason: {proposal.rationale}")
        if proposal.ambiguity_questions:
            lines.append("Ambiguity questions:")
            for q in proposal.ambiguity_questions:
                lines.append(f"- {q.text}")
        return "\n".join(lines) + "\n"

    lines.append(f"Proposed classification: {proposal.classification.value}")
    if proposal.diagnosis_state is not None:
        lines.append(f"Proposed diagnosis state: {proposal.diagnosis_state.value}")
    lines.append(f"Confidence: {proposal.confidence.value}")
    if proposal.rationale:
        lines.append(f"Rationale: {proposal.rationale}")
    if proposal.proposed_labels:
        lines.append(f"Add labels: {', '.join(proposal.proposed_labels)}")
    if proposal.removed_labels:
        lines.append(f"Remove labels: {', '.join(proposal.removed_labels)}")
    if proposal.proposed_adr_slug:
        lines.append(f"Proposed ADR slug: {proposal.proposed_adr_slug}")
        lines.append(f"Proposed ADR title: {proposal.proposed_adr_title}")
        lines.append("(ADR file is NOT written — operator confirms and authors it.)")
    if proposal.proposed_child_stories:
        lines.append("Proposed child stories (prose only — no issues will be created):")
        for child in proposal.proposed_child_stories:
            lines.append(f"  - {child}")

    if show_diff:
        new_body = restructure_body(proposal, current_body)
        if new_body != current_body:
            lines.append("")
            lines.append("Proposed body restructure:")
            diff = difflib.unified_diff(
                (current_body or "").splitlines(),
                new_body.splitlines(),
                fromfile="current",
                tofile="proposed",
                lineterm="",
            )
            for d in diff:
                lines.append(d)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ShapeReadiness:
    """Whether the issue, as it stands, already satisfies the shape gate.

    ``ready`` is the single readiness signal ``forge shape`` reports: it is true
    only when the gate's verdict on the *current* title/body/labels is
    ``RUNNABLE`` **and** the proposal asks nothing further of the operator. A
    proposal that restates state the issue already has is not a change, so it
    must not keep the command from reporting a terminal state (#2054).
    """

    verdict: ShapeVerdict
    ready: bool


def evaluate_readiness(
    proposal: ShapeProposal,
    *,
    title: str,
    body: str,
    labels: Iterable[str],
) -> ShapeReadiness:
    """Return the shape-gate verdict for the current issue state + readiness.

    The verdict comes from the same ``shape_check.check`` entry point the sprint
    gate and ``forge groom`` derive their own verdicts from, so a "ready" report
    here means exactly what "runnable" means everywhere else — there is no
    second, shape-private definition of readiness to drift from it.
    """
    result = shape_gate_check(title or "", body or "", list(labels))
    verdict = result.verdict
    ready = (
        proposal.classification is not Classification.UNRESOLVED
        and not proposal.proposed_labels
        and not proposal.removed_labels
        and restructure_body(proposal, body or "") == (body or "")
        and result.admits_implementation_sprint
    )
    return ShapeReadiness(verdict=verdict, ready=ready)


def next_command(
    proposal: ShapeProposal,
    issue_number: int | None,
    *,
    ready: bool = False,
) -> str:
    """Return the operator-facing recommended next command.

    When ``ready``, the recommendation is terminal: the pipeline has nothing
    left to do for this issue, and saying so is what makes the non-terminal
    recommendations meaningful (#2054).
    """
    if ready:
        subject = f"#{issue_number}" if issue_number is not None else "the draft"
        return (
            f"Next: none — {subject} already satisfies the shape gate "
            "(verdict: runnable); no further action is needed."
        )
    if proposal.classification is Classification.UNRESOLVED:
        return "Next: resolve ambiguity questions, then re-run `forge shape`."
    target = f" {issue_number}" if issue_number is not None else ""
    cls = proposal.classification
    if cls is Classification.BUG:
        if proposal.diagnosis_state is DiagnosisState.DIAGNOSIS_CONFIRMED_CAUSE:
            return f"Next: forge groom{target}"
        return f"Next: forge diagnose{target}"
    if cls is Classification.ENHANCEMENT:
        return f"Next: forge groom{target}"
    if cls is Classification.EPIC:
        return "Next: split into child stories manually, then `forge shape` each."
    if cls is Classification.OPERATOR_ACTION:
        return "Next: perform the operator action; do not enter the dev pipeline."
    if cls is Classification.DOCUMENTATION:
        return f"Next: forge groom{target}"
    if cls is Classification.ADR_CANDIDATE:
        slug = proposal.proposed_adr_slug or "<slug>"
        return f"Next: author docs/adr/{slug}.md (operator confirms before writing)."
    if cls is Classification.DUPLICATE_OR_STALE:
        return f"Next: gh issue close{target}"
    return "Next: (no recommendation)"


# Confidence is only used through ShapeProposal, but re-export so consumers
# don't have to import the classify module separately when assembling text.
__all__ = [
    "Confidence",
    "ShapeReadiness",
    "evaluate_readiness",
    "next_command",
    "render_proposal",
    "restructure_body",
]
