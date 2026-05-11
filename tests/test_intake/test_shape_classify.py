"""Unit tests for the heuristic shape classifier."""

from __future__ import annotations

from theforge.intake.shape_classify import (
    Classification,
    Confidence,
    DiagnosisState,
    classify,
)


def test_classifies_bug_by_label():
    proposal = classify(
        title="cut-rc.sh broke something",
        body="happens on every release",
        labels=["bug"],
    )
    assert proposal.classification is Classification.BUG
    assert proposal.confidence is Confidence.HIGH
    assert proposal.diagnosis_state is DiagnosisState.NO_DIAGNOSIS


def test_bug_detects_diagnosis_confirmed_cause():
    body = (
        "## Observed\nfoo\n\n## Expected\nbar\n\n"
        "### Diagnosis\nRoot cause: regression in module X.\n"
    )
    proposal = classify("bug: regression", body, ["bug"])
    assert proposal.classification is Classification.BUG
    assert proposal.diagnosis_state is DiagnosisState.DIAGNOSIS_CONFIRMED_CAUSE


def test_classifies_enhancement_by_ac_section():
    proposal = classify(
        title="add forge shape command",
        body="## Acceptance criteria\n- new CLI command exists\n",
        labels=[],
    )
    assert proposal.classification is Classification.ENHANCEMENT
    assert "enhancement" in proposal.proposed_labels


def test_classifies_epic_with_child_stories():
    body = "## Child stories\n- story one\n- story two\n- story three\n"
    proposal = classify("epic: multi-tenant", body, ["epic"])
    assert proposal.classification is Classification.EPIC
    assert proposal.proposed_child_stories == ("story one", "story two", "story three")


def test_classifies_adr_candidate_proposes_slug_and_title():
    proposal = classify(
        title="ADR: Adopt SQLite for audit substrate",
        body="## Decision\nWe will use SQLite.\n## Alternatives considered\n- DuckDB\n",
        labels=[],
    )
    assert proposal.classification is Classification.ADR_CANDIDATE
    assert proposal.proposed_adr_slug
    assert proposal.proposed_adr_title is not None
    assert "sqlite" in proposal.proposed_adr_slug.lower()


def test_low_confidence_keeps_as_todo_draft_with_questions():
    # Conflict between operator-action and enhancement → unresolved.
    proposal = classify(
        title="operator should bump the release floor and we also need a CLI",
        body=(
            "## Acceptance criteria\n- operator action: bump floor\n"
            "## Operator action\n- bump floor\n"
        ),
        labels=[],
    )
    assert proposal.classification is Classification.UNRESOLVED
    assert proposal.kept_as_todo_draft is True
    assert len(proposal.ambiguity_questions) >= 2


def test_no_signal_returns_unresolved():
    proposal = classify("???", "", [])
    assert proposal.classification is Classification.UNRESOLVED
    assert proposal.confidence is Confidence.LOW
    assert proposal.ambiguity_questions


def test_duplicate_detected_by_label():
    proposal = classify("foo", "Duplicate of #99\n", ["duplicate"])
    assert proposal.classification is Classification.DUPLICATE_OR_STALE


def test_documentation_detected_by_title():
    proposal = classify("docs: update CLI reference", "Add forge shape section.", [])
    assert proposal.classification is Classification.DOCUMENTATION
