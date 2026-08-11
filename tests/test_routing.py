"""Tests for the canonical adaptive routing policy helpers."""

from __future__ import annotations

from theforge.coordinator.preflight import score_to_dev_tier as preflight_score_to_dev_tier
from theforge.routing import (
    DEFAULT_EFFORT_TOKEN_BUDGETS,
    DEV_COMPLEXITY_TIER,
    DEV_SCORE_TIER_BUCKETS,
    KNOB_BUDGET,
    KNOB_EFFORT,
    KNOB_NONE,
    PLAN_SCORE_TIER_BUCKETS,
    PROVIDER_UNSUPPORTED,
    REASONING_EFFORT_PHASE_BUCKETS,
    REASONING_EFFORT_TRANSPORT_SUPPORT,
    REVIEWER_SCORE_COUNT_BUCKETS,
    ROUTING_POLICY,
    SUPPORTED_METERED,
    SUPPORTED_UNMETERED,
    VALID_REASONING_EFFORTS,
    axis_decision,
    effort_knob_for,
    provider_support_status,
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
    # The canonical SSOT names every score-derived axis. reasoning_effort joined
    # them in #1108 (the #1019-era exclusion is retracted).
    assert set(ROUTING_POLICY) == {
        "dev_tier",
        "plan_tier",
        "reviewer_count",
        "reasoning_effort",
    }
    for key in ("dev_tier", "plan_tier", "reviewer_count", "reasoning_effort"):
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


def test_reasoning_effort_default_table_is_per_phase():
    """The default table is deliberately asymmetric: plan leads dev by one band
    at the same score (#1108)."""
    assert REASONING_EFFORT_PHASE_BUCKETS["plan"] == ((3, "medium"), (6, "high"), (10, "high"))
    assert REASONING_EFFORT_PHASE_BUCKETS["dev"] == ((3, "low"), (6, "medium"), (10, "high"))
    assert REASONING_EFFORT_PHASE_BUCKETS["review"] == ((3, "low"), (6, "medium"), (10, "high"))
    assert VALID_REASONING_EFFORTS == ("low", "medium", "high")
    for score, (plan, dev, review) in {
        1: ("medium", "low", "low"),
        3: ("medium", "low", "low"),
        4: ("high", "medium", "medium"),
        6: ("high", "medium", "medium"),
        7: ("high", "high", "high"),
        10: ("high", "high", "high"),
    }.items():
        assert axis_decision("reasoning_effort", score, phase="plan")["output"] == plan
        assert axis_decision("reasoning_effort", score, phase="dev")["output"] == dev
        assert axis_decision("reasoning_effort", score, phase="review")["output"] == review
        # Plan is never *below* dev at the same score.
        assert VALID_REASONING_EFFORTS.index(plan) >= VALID_REASONING_EFFORTS.index(dev)


def test_axis_decision_reasoning_effort_is_score_controlled_per_phase():
    dev = axis_decision("reasoning_effort", 8, phase="dev")
    assert dev["score_controlled"] is True
    assert dev["applied"] is True
    assert dev["phase"] == "dev"
    assert dev["bucket"] == "high"
    assert dev["range"] == [7, 10]
    assert dev["thresholds"] == [3, 6, 10]
    assert dev["output"] == "high"

    # Same score, different phase → the plan band, resolved independently.
    low_dev = axis_decision("reasoning_effort", 2, phase="dev")
    low_plan = axis_decision("reasoning_effort", 2, phase="plan")
    assert low_dev["output"] == "low"
    assert low_plan["output"] == "medium"
    assert low_plan["range"] == [1, 3]


def test_axis_decision_reasoning_effort_honors_configured_overrides():
    overrides = {"dev": ((5, "high"), (10, "high"))}
    dec = axis_decision("reasoning_effort", 2, phase="dev", overrides=overrides)
    assert dec["output"] == "high"
    assert dec["range"] == [1, 5]
    assert dec["thresholds"] == [5, 10]
    # A phase with no override keeps the default table.
    assert axis_decision("reasoning_effort", 2, phase="plan", overrides=overrides)["output"] == (
        "medium"
    )


def test_axis_decision_reasoning_effort_without_score_records_fallback():
    dec = axis_decision("reasoning_effort", None, phase="dev")
    assert dec["applied"] is False
    assert dec["output"] is None
    assert dec["reason"] == "no_numeric_score_static_band_routing"


def test_transport_support_metadata_classifies_every_known_runner():
    """Provider capability metadata lives in the routing SSOT, not in runners."""
    assert effort_knob_for("codex").kind == KNOB_EFFORT
    # DeepSeek takes an effort string too: it expresses reasoning as a request
    # parameter rather than as a distinct model name, and meters it back as
    # reasoning tokens (#2352).
    assert effort_knob_for("deepseek").kind == KNOB_EFFORT
    assert effort_knob_for("deepseek").captures_thinking_spend is True
    assert effort_knob_for("google").kind == KNOB_BUDGET
    for runner in ("claude", "gemini", "ghaw", "anthropic", "openai"):
        assert effort_knob_for(runner).kind == KNOB_NONE
    # An unknown or absent transport must opt in explicitly, never inherit a knob.
    assert effort_knob_for("brand-new-runner").kind == KNOB_NONE
    assert effort_knob_for(None).kind == KNOB_NONE


def test_provider_support_status_separates_knob_from_metering():
    unsupported = REASONING_EFFORT_TRANSPORT_SUPPORT["claude"]
    assert provider_support_status(unsupported, cost_captured=True) == PROVIDER_UNSUPPORTED
    codex = REASONING_EFFORT_TRANSPORT_SUPPORT["codex"]
    assert provider_support_status(codex, cost_captured=True) == SUPPORTED_METERED
    assert provider_support_status(codex, cost_captured=False) == SUPPORTED_UNMETERED


def test_default_token_budgets_are_canonical():
    assert DEFAULT_EFFORT_TOKEN_BUDGETS == {"low": 2048, "medium": 8192, "high": 24576}


def test_axis_decision_without_score_records_static_fallback():
    dec = axis_decision("dev_tier", None)
    assert dec["applied"] is False
    assert dec["bucket"] is None
    assert dec["reason"] == "no_numeric_score_static_band_routing"
    # Thresholds are still surfaced so the table is reconstructable even in
    # static mode.
    assert dec["thresholds"] == [3, 6, 10]
