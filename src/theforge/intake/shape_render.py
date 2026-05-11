"""Render shape proposals as operator-facing text + body restructure.

Pure text-shaping helpers, stdlib only. No I/O.
"""

from __future__ import annotations

import difflib

from theforge.intake.shape_classify import (
    Classification,
    Confidence,
    DiagnosisState,
    ShapeProposal,
)


def restructure_body(proposal: ShapeProposal, current_body: str) -> str:
    """Return the proposed restructured body for the issue, or ``current_body``
    unchanged when no restructure is proposed.

    The restructure is intentionally minimal — it adds the scaffolding the
    downstream producers (diagnose/groom) expect to see, and never deletes
    existing operator text. The original body is appended under a quoted
    ``Original`` block so nothing is silently lost.
    """
    current_body = current_body or ""
    if proposal.classification is Classification.BUG:
        return _restructure_bug(proposal, current_body)
    if proposal.classification is Classification.ENHANCEMENT:
        return _restructure_enhancement(current_body)
    if proposal.classification is Classification.UNRESOLVED:
        return current_body
    return current_body


def _has_section(body: str, marker: str) -> bool:
    return marker.lower() in body.lower()


def _restructure_bug(proposal: ShapeProposal, current_body: str) -> str:
    parts: list[str] = []
    if not _has_section(current_body, "## Observed"):
        parts.append("## Observed\n\n<fill from current body>\n")
    if not _has_section(current_body, "## Expected"):
        parts.append("## Expected\n\n<state the category-level rule that should hold here>\n")
    if not _has_section(current_body, "### Diagnosis"):
        state = proposal.diagnosis_state or DiagnosisState.NO_DIAGNOSIS
        if state is DiagnosisState.NO_DIAGNOSIS:
            line = "Status: no diagnosis yet. Next step: run `forge diagnose`."
        elif state is DiagnosisState.DIAGNOSIS_CAUSE_UNKNOWN:
            line = (
                "Status: investigation in progress; cause unknown. Next step: continue diagnosis."
            )
        else:
            line = "Status: confirmed cause documented."
        parts.append(f"### Diagnosis\n\n{line}\n")
    if not parts:
        return current_body
    rendered = "\n".join(parts).rstrip() + "\n"
    if current_body.strip():
        rendered += "\n## Original\n\n" + _quote_block(current_body) + "\n"
    return rendered


def _restructure_enhancement(current_body: str) -> str:
    if _has_section(current_body, "## Acceptance criteria") or _has_section(
        current_body, "## Acceptance Criteria"
    ):
        return current_body
    suffix = "\n\n## Acceptance criteria\n\n- <state an observable, testable outcome>\n"
    return (current_body.rstrip() + suffix) if current_body.strip() else suffix.lstrip("\n")


def _quote_block(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


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


def next_command(proposal: ShapeProposal, issue_number: int | None) -> str:
    """Return the operator-facing recommended next command."""
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
__all__ = ["Confidence", "next_command", "render_proposal", "restructure_body"]
