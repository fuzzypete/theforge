"""Unit tests for the heuristic shape classifier."""

from __future__ import annotations

import textwrap

import pytest

from theforge.intake.shape_classify import (
    Classification,
    Confidence,
    DiagnosisState,
    _looks_like_bug,
    classify,
)
from theforge.shape_check.heuristics import is_bug_format_issue


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


def test_landed_diagnosis_outranks_stale_placeholder_stub_above_it():
    """A `forge shape` placeholder left in place must not shadow a later,
    genuinely landed `forge diagnose` artifact (#2263, hdp#259)."""
    body = (
        "## Observed\nfoo\n\n## Expected\nbar\n\n"
        "### Diagnosis\n\nStatus: no diagnosis yet. Next step: run `forge diagnose`.\n\n"
        "## Diagnosis\n\n"
        "**Baseline:** `abc123`\n\n"
        "**Confirmed cause:** regression in module X.\n\n"
        "**Affected code path:** src/module_x.py\n"
    )
    proposal = classify("bug: regression", body, ["bug"])
    assert proposal.classification is Classification.BUG
    assert proposal.diagnosis_state is DiagnosisState.DIAGNOSIS_CONFIRMED_CAUSE


def test_landed_diagnosis_outranks_ordinary_prose_heading_above_it():
    """An operator-written heading that merely contains the word 'diagnosis'
    must not shadow a later, genuinely landed artifact (#2263,
    fuzzypete/theforge#2673)."""
    body = (
        "## Observed\nfoo\n\n## Expected\nbar\n\n"
        "## Further evidence — generated diagnosis text becomes "
        "scope-classification input on rerun\n\n"
        "some unrelated prose about the diagnose flow itself\n\n"
        "## Diagnosis\n\n"
        "**Baseline:** `abc123`\n\n"
        "**Confirmed cause:** regression in module X.\n\n"
        "**Affected code path:** src/module_x.py\n"
    )
    proposal = classify("bug: regression", body, ["bug"])
    assert proposal.classification is Classification.BUG
    assert proposal.diagnosis_state is DiagnosisState.DIAGNOSIS_CONFIRMED_CAUSE


def test_landed_diagnosis_outranks_stale_placeholder_below_it():
    """The reverse order of the hdp#259 repro: the real artifact appears
    first and a stale placeholder follows. Ordering must not flip which one
    is authoritative (#2263)."""
    body = (
        "## Observed\nfoo\n\n## Expected\nbar\n\n"
        "## Diagnosis\n\n"
        "**Baseline:** `abc123`\n\n"
        "**Confirmed cause:** regression in module X.\n\n"
        "**Affected code path:** src/module_x.py\n\n"
        "## Diagnosis\n\nStatus: no diagnosis yet. Next step: run `forge diagnose`.\n"
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
    assert "too thin to shape" in proposal.rationale


# A bug brief written strictly to docs/guides/authoring.md: no bug label
# (`--from-brief` supplies none), and a title that names the misbehavior
# rather than opening with a marker word (#2139).
DOCUMENTED_FORMAT_BRIEF = textwrap.dedent(
    """\
    ## What happened

    Ran `forge sprint --resume` on a sprint where two of three stories had
    already been merged. The resume run re-entered both at the dev phase.

    ## What was expected

    Resuming a sprint should never repeat work that has already reached a
    terminal merged state, regardless of the last phase in the audit log.
    """
)


def test_documented_bug_format_classifies_as_bug_without_label_or_title_token():
    proposal = classify(
        title="sprint resume re-runs already-merged stories",
        body=DOCUMENTED_FORMAT_BRIEF,
        labels=[],
    )
    assert proposal.classification is Classification.BUG
    assert proposal.confidence is Confidence.HIGH
    assert proposal.proposed_labels == ("bug",)


def test_documented_bug_format_never_reports_no_signal():
    proposal = classify(
        title="sprint resume re-runs already-merged stories",
        body=DOCUMENTED_FORMAT_BRIEF,
        labels=[],
    )
    assert "too thin to shape" not in proposal.rationale


def test_substantive_unclassifiable_draft_is_not_called_thin():
    body = textwrap.dedent(
        """\
        ## Background

        A long description of some situation, with measured evidence and a
        quoted artifact, that nonetheless names no work type anywhere.
        """
    )
    proposal = classify("something about the release process", body, [])
    assert proposal.classification is Classification.UNRESOLVED
    assert "too thin to shape" not in proposal.rationale
    assert "authoring.md" in proposal.rationale


BUG_BODY_CASES = [
    pytest.param(DOCUMENTED_FORMAT_BRIEF, True, id="documented-what-happened-pair"),
    pytest.param("## Observed\nfoo\n\n## Expected\nbar\n", True, id="corpus-observed-pair"),
    pytest.param(
        "## Observed behavior\nfoo\n\n## Expected behavior\nbar\n",
        True,
        id="observed-behavior-pair",
    ),
    pytest.param("## Steps to reproduce\n1. run it\n", True, id="reproduction-steps"),
    pytest.param("## Acceptance criteria\n- a thing happens\n", False, id="feature-body"),
    pytest.param("", False, id="empty"),
]


@pytest.mark.parametrize("body,expected", BUG_BODY_CASES)
def test_classifier_and_shape_gate_agree_on_bug_bodies(body: str, expected: bool):
    """The two intake gates must not disagree about what a bug body looks like."""
    assert _looks_like_bug("", body, set()) is expected
    assert is_bug_format_issue(body, []) is expected


def test_duplicate_detected_by_label():
    proposal = classify("foo", "Duplicate of #99\n", ["duplicate"])
    assert proposal.classification is Classification.DUPLICATE_OR_STALE


def test_documentation_detected_by_title():
    proposal = classify("docs: update CLI reference", "Add forge shape section.", [])
    assert proposal.classification is Classification.DOCUMENTATION
