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
from theforge.shape_check.heuristics import diagnosis_completeness


def _bug_proposal(state: DiagnosisState = DiagnosisState.NO_DIAGNOSIS) -> ShapeProposal:
    return ShapeProposal(
        classification=Classification.BUG,
        confidence=Confidence.HIGH,
        diagnosis_state=state,
        proposed_labels=("bug",),
        removed_labels=("todo:draft",),
        rationale="bug label present",
    )


def _complete_bug_body() -> str:
    """A bug body the shape gate already accepts (diagnosis_completeness True)."""
    return (
        "## Observed behavior\n\n"
        "`forge shape --apply` rewrites a passing body into a failing one.\n\n"
        "## Expected behavior\n\n"
        "A remediation must be a no-op on input that already passes.\n\n"
        "## Diagnosis\n\n"
        "- **Observed symptom:** the applied body no longer passes the gate.\n"
        "- **Evidence:** issue #2050 at baseline `f2caf7d`.\n"
        "- **Confirmed cause:** the renderer probed for an H3 heading.\n"
        "- **Affected code path:** `intake.shape_render._restructure_bug`.\n"
        "- **Fix-success criterion:** the body is returned unchanged.\n\n"
        "## Notes\n\n"
        "Found while auditing intake.\n"
    )


def test_restructure_bug_adds_observed_expected_diagnosis():
    new = restructure_body(_bug_proposal(), "original body")
    assert "## Observed" in new
    assert "## Expected" in new
    assert "## Diagnosis" in new
    assert "### Diagnosis" not in new  # gate requires H2, not H3
    assert "## Original" not in new  # original is never demoted into a quote block
    assert new.startswith("original body")  # original preserved verbatim, in place


def test_restructure_bug_is_noop_on_gate_passing_body():
    body = _complete_bug_body()
    assert diagnosis_completeness(body) == (True, [])
    assert restructure_body(_bug_proposal(DiagnosisState.DIAGNOSIS_CONFIRMED_CAUSE), body) == body


def test_restructure_bug_adds_only_missing_component_in_place():
    body = _complete_bug_body().replace("- **Evidence:** issue #2050 at baseline `f2caf7d`.\n", "")
    assert diagnosis_completeness(body) == (False, ["evidence"])

    new = restructure_body(_bug_proposal(DiagnosisState.DIAGNOSIS_CONFIRMED_CAUSE), body)

    # Every original heading survives at its original level.
    for heading in (
        "## Observed behavior",
        "## Expected behavior",
        "## Diagnosis",
        "## Notes",
    ):
        assert heading in new
    # The only delta is the added component bullet.
    added = [line for line in new.splitlines() if line not in body.splitlines()]
    assert added == ["- **Evidence:** <fill in>"]
    # ...and the addition lands inside the Diagnosis section, so the gate sees it.
    assert diagnosis_completeness(new) == (True, [])


def test_restructure_bug_appends_diagnosis_when_section_absent():
    body = "## Observed behavior\n\nthing broke\n\n## Expected behavior\n\nit should not\n"
    new = restructure_body(_bug_proposal(), body)
    assert new.startswith(body.rstrip("\n"))
    assert "## Diagnosis" in new
    assert diagnosis_completeness(new) == (True, [])


def test_restructure_bug_keeps_h3_observed_heading():
    """A heading at a non-H2 level still counts as present — no duplicate stub."""
    body = "### Observed\n\nx\n\n### Expected\n\ny\n"
    new = restructure_body(_bug_proposal(), body)
    assert new.startswith(body.rstrip("\n"))
    assert "<fill from current body>" not in new
    assert "<state the category-level rule" not in new


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
