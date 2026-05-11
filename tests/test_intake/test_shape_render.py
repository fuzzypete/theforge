"""Unit tests for shape proposal rendering + body restructure."""

from __future__ import annotations

from theforge.intake.shape_classify import (
    AmbiguityQuestion,
    Classification,
    Confidence,
    DiagnosisState,
    ShapeProposal,
)
from theforge.intake.shape_render import (
    next_command,
    render_proposal,
    restructure_body,
)


def _bug_proposal(state: DiagnosisState = DiagnosisState.NO_DIAGNOSIS) -> ShapeProposal:
    return ShapeProposal(
        classification=Classification.BUG,
        confidence=Confidence.HIGH,
        diagnosis_state=state,
        proposed_labels=("bug",),
        removed_labels=("todo:draft",),
        rationale="bug label present",
    )


def test_restructure_bug_adds_observed_expected_diagnosis():
    new = restructure_body(_bug_proposal(), "original body")
    assert "## Observed" in new
    assert "## Expected" in new
    assert "### Diagnosis" in new
    assert "original body" in new  # original preserved in quote block


def test_restructure_bug_with_confirmed_cause_says_so():
    new = restructure_body(
        _bug_proposal(DiagnosisState.DIAGNOSIS_CONFIRMED_CAUSE),
        "",
    )
    assert "confirmed cause" in new.lower()


def test_restructure_enhancement_adds_acceptance_criteria():
    proposal = ShapeProposal(
        classification=Classification.ENHANCEMENT,
        confidence=Confidence.HIGH,
        proposed_labels=("enhancement",),
    )
    new = restructure_body(proposal, "rough idea\n")
    assert "## Acceptance criteria" in new
    assert "rough idea" in new


def test_restructure_enhancement_idempotent_if_section_present():
    proposal = ShapeProposal(
        classification=Classification.ENHANCEMENT,
        confidence=Confidence.HIGH,
        proposed_labels=("enhancement",),
    )
    body = "## Acceptance criteria\n- thing\n"
    assert restructure_body(proposal, body) == body


def test_render_unresolved_lists_ambiguity_questions():
    proposal = ShapeProposal(
        classification=Classification.UNRESOLVED,
        confidence=Confidence.LOW,
        ambiguity_questions=(
            AmbiguityQuestion(code="x", text="Is the deliverable code or docs?"),
        ),
        rationale="could be either operator-action or enhancement",
    )
    out = render_proposal(proposal, source_label="brief: 'x'")
    assert "Kept as: todo:draft" in out
    assert "Is the deliverable code or docs?" in out


def test_render_bug_includes_diff():
    proposal = _bug_proposal()
    out = render_proposal(proposal, source_label="#42 'foo'", current_body="raw")
    assert "Proposed classification: bug" in out
    assert "Confidence: high" in out
    assert "+## Observed" in out


def test_next_command_bug_no_diagnosis_routes_to_diagnose():
    assert next_command(_bug_proposal(), 1497) == "Next: forge diagnose 1497"


def test_next_command_bug_confirmed_cause_routes_to_groom():
    proposal = _bug_proposal(DiagnosisState.DIAGNOSIS_CONFIRMED_CAUSE)
    assert next_command(proposal, 1497) == "Next: forge groom 1497"


def test_next_command_unresolved():
    proposal = ShapeProposal(classification=Classification.UNRESOLVED, confidence=Confidence.LOW)
    assert "ambiguity" in next_command(proposal, None).lower()


def test_next_command_adr_uses_slug():
    proposal = ShapeProposal(
        classification=Classification.ADR_CANDIDATE,
        confidence=Confidence.HIGH,
        proposed_adr_slug="adopt-sqlite",
        proposed_adr_title="Adopt SQLite",
    )
    assert "adopt-sqlite" in next_command(proposal, None)
