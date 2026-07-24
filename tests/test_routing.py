"""Tests for the canonical adaptive routing policy helpers."""

from __future__ import annotations

from theforge.coordinator.preflight import score_to_dev_tier as preflight_score_to_dev_tier
from theforge.routing import (
    DEV_COMPLEXITY_TIER,
    DEV_SCORE_TIER_BUCKETS,
    PLAN_SCORE_TIER_BUCKETS,
    REVIEWER_SCORE_COUNT_BUCKETS,
    ROUTING_POLICY,
    axis_decision,
    score_to_dev_tier,
    score_to_plan_tier,
    score_to_reviewer_target,
)


def test_dev_score_tier_buckets_are_canonical():
    assert DEV_SCORE_TIER_BUCKETS == (
        (3, "cheap"),
        (6, "mid"),
        (10, "strong"),
    )
    assert score_to_dev_tier(3) == "cheap"
    assert score_to_dev_tier(4) == "mid"
    assert score_to_dev_tier(6) == "mid"
    assert score_to_dev_tier(7) == "strong"
    assert DEV_COMPLEXITY_TIER == {"LOW": "cheap", "MEDIUM": "mid", "HIGH": "strong"}


def test_preflight_reexports_canonical_score_to_dev_tier():
    assert preflight_score_to_dev_tier is score_to_dev_tier


def test_plan_score_tier_buckets_are_canonical():
    assert PLAN_SCORE_TIER_BUCKETS == ((5, "mid"), (10, "strong"))
    assert score_to_plan_tier(1) == "mid"
    assert score_to_plan_tier(5) == "mid"
    assert score_to_plan_tier(6) == "strong"
    assert score_to_plan_tier(10) == "strong"


def test_reviewer_count_buckets_are_canonical():
    assert REVIEWER_SCORE_COUNT_BUCKETS == ((4, "min"), (7, "mid"), (10, "max"))
    assert score_to_reviewer_target(4) == "min"
    assert score_to_reviewer_target(5) == "mid"
    assert score_to_reviewer_target(7) == "mid"
    assert score_to_reviewer_target(8) == "max"


def test_routing_policy_covers_every_axis():
    # The canonical SSOT must name every score-derived axis plus the one axis
    # documented as intentionally NOT score-controlled (#1019).
    assert set(ROUTING_POLICY) == {
        "dev_tier",
        "plan_tier",
        "reviewer_count",
        "reasoning_effort",
    }
    assert ROUTING_POLICY["reasoning_effort"].score_controlled is False
    for key in ("dev_tier", "plan_tier", "reviewer_count"):
        assert ROUTING_POLICY[key].score_controlled is True
        assert ROUTING_POLICY[key].rationale  # every axis states its rationale


def test_axis_decision_records_bucket_range_threshold_and_output():
    dec = axis_decision("dev_tier", 8)
    assert dec["axis"] == "dev_tier"
    assert dec["applied"] is True
    assert dec["score"] == 8
    assert dec["bucket"] == "strong"
    assert dec["range"] == [7, 10]
    assert dec["thresholds"] == [3, 6, 10]
    assert dec["output"] == "strong"
    assert dec["rationale"]

    # Boundary story in the mid dev bucket resolves the covering range exactly.
    mid = axis_decision("dev_tier", 4)
    assert mid["bucket"] == "mid"
    assert mid["range"] == [4, 6]

    plan = axis_decision("plan_tier", 3)
    assert plan["bucket"] == "mid"
    assert plan["range"] == [1, 5]
    assert plan["thresholds"] == [5, 10]


def test_axis_decision_reasoning_effort_is_not_score_controlled():
    dec = axis_decision("reasoning_effort", 9)
    assert dec["score_controlled"] is False
    assert dec["applied"] is False
    assert dec["bucket"] is None
    assert dec["output"] is None
    assert dec["reason"] == "not_score_controlled"


def test_axis_decision_without_score_records_static_fallback():
    dec = axis_decision("dev_tier", None)
    assert dec["applied"] is False
    assert dec["bucket"] is None
    assert dec["reason"] == "no_numeric_score_static_band_routing"
    # Thresholds are still surfaced so the table is reconstructable even in
    # static mode.
    assert dec["thresholds"] == [3, 6, 10]
