"""Tests for the reusable clarification Q&A engine."""

from __future__ import annotations

import pytest

from theforge.intake import clarification
from theforge.intake.clarification import (
    ClarificationQuestion,
    QUESTIONS,
    SITUATIONS,
    build,
    for_situation,
)


def test_build_returns_records_for_known_codes():
    out = build(("what_kind_of_work", "observable_outcome"))
    assert all(isinstance(q, ClarificationQuestion) for q in out)
    assert tuple(q.code for q in out) == ("what_kind_of_work", "observable_outcome")
    assert out[0].text == QUESTIONS["what_kind_of_work"]


def test_build_raises_on_unknown_code():
    with pytest.raises(KeyError):
        build(("does_not_exist",))


def test_for_situation_returns_named_set():
    out = for_situation("no_signal")
    codes = tuple(q.code for q in out)
    assert codes == SITUATIONS["no_signal"]


def test_for_situation_unknown_raises():
    with pytest.raises(KeyError):
        for_situation("nonexistent_situation")


def test_situations_only_reference_known_codes():
    # Every code referenced by a named situation must be in the question
    # registry, otherwise consumers would silently raise KeyError at runtime.
    for name, codes in SITUATIONS.items():
        for code in codes:
            assert code in QUESTIONS, f"situation {name!r} references missing code {code!r}"


def test_shape_classify_reuses_clarification_engine():
    """Regression: shape_classify must source questions from the shared engine,
    not re-define them inline."""
    from theforge.intake.shape_classify import classify

    proposal = classify("???", "", [])
    expected = for_situation("no_signal")
    assert proposal.ambiguity_questions == expected
    # Type identity confirms the alias points at the canonical record type.
    assert ClarificationQuestion is clarification.ClarificationQuestion
