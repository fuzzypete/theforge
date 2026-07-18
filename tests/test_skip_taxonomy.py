"""Unit coverage for the shape-gate skip taxonomy (issue #1453 AC2/AC5).

The taxonomy is the pure-data layer every observability surface classifies
through. These tests pin the code → category / four-question-axis mapping and
the remediation-outcome overrides so a drift in one surface can't diverge from
another.
"""

from __future__ import annotations

from theforge.shape_check.skip_taxonomy import (
    FourQuestionAxis,
    RemediationOutcome,
    SkipCategory,
    SkipSeverity,
    classify_skip,
    group_by_category,
)


def test_stale_label_code_is_stale_label_category() -> None:
    c = classify_skip("needs_grooming_label", "label")
    assert c.category is SkipCategory.STALE_LABEL
    # A stale label is the "verification passed but another gate fired" axis.
    assert c.four_question_axis is FourQuestionAxis.VERIFICATION_PASSED_OTHER_GATE


def test_semantic_gate_codes_map_to_response_not_attempted() -> None:
    for code in ("needs_diagnosis", "diagnosis_cause_unknown", "reopened_stale_contract"):
        c = classify_skip(code, "local_check")
        assert c.category is SkipCategory.SEMANTIC_GATE, code
        assert c.four_question_axis is FourQuestionAxis.RESPONSE_NOT_ATTEMPTED, code


def test_structural_code_is_unrunnable_invariant_violated() -> None:
    c = classify_skip("missing_acceptance_criteria", "local_check")
    assert c.category is SkipCategory.UNRUNNABLE_SHAPE
    assert c.four_question_axis is FourQuestionAxis.INVARIANT_VIOLATED


def test_unknown_code_defaults_to_unrunnable() -> None:
    c = classify_skip("brand_new_check_code", "local_check")
    assert c.category is SkipCategory.UNRUNNABLE_SHAPE
    assert c.four_question_axis is FourQuestionAxis.INVARIANT_VIOLATED


def test_remediated_outcome_overrides_category() -> None:
    c = classify_skip(
        "reopened_stale_contract",
        "local_check",
        remediation=RemediationOutcome.REMEDIATED,
    )
    assert c.category is SkipCategory.REMEDIATED_PROCEEDED
    # Axis still reflects the underlying invariant that was then fixed.
    assert c.four_question_axis is FourQuestionAxis.RESPONSE_NOT_ATTEMPTED


def test_declined_outcome_is_response_attempted_gate_failed() -> None:
    c = classify_skip(
        "reopened_stale_contract",
        "local_check",
        remediation=RemediationOutcome.DECLINED,
    )
    assert c.category is SkipCategory.DECLINED_REMEDIATION
    assert c.four_question_axis is FourQuestionAxis.RESPONSE_ATTEMPTED_GATE_FAILED


def test_severity_is_carried_through() -> None:
    c = classify_skip("reopened_stale_contract", "local_check", severity=SkipSeverity.ADVISORY)
    assert c.severity is SkipSeverity.ADVISORY
    assert c.as_dict()["severity"] == "advisory"


def test_group_by_category_orders_cheapest_first() -> None:
    events = [
        {"category": "unrunnable_by_shape", "issue_id": "1"},
        {"category": "blocked_by_stale_label", "issue_id": "2"},
        {"category": "blocked_by_semantic_gate", "issue_id": "3"},
    ]
    grouped = group_by_category(events)
    # Stale-label (seconds to fix) must appear before semantic-gate before
    # unrunnable so the operator reads recovery cost top-down.
    keys = list(grouped)
    assert keys.index("blocked_by_stale_label") < keys.index("blocked_by_semantic_gate")
    assert keys.index("blocked_by_semantic_gate") < keys.index("unrunnable_by_shape")


def test_group_by_category_keeps_unknown_categories() -> None:
    events = [{"category": "some_future_category", "issue_id": "9"}]
    grouped = group_by_category(events)
    assert grouped["some_future_category"] == events


def test_group_by_category_defaults_missing_category() -> None:
    events = [{"issue_id": "9"}]
    grouped = group_by_category(events)
    assert "unrunnable_by_shape" in grouped
