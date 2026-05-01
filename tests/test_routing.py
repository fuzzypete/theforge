"""Tests for the canonical adaptive routing policy helpers."""

from __future__ import annotations

from theforge.coordinator.preflight import score_to_dev_tier as preflight_score_to_dev_tier
from theforge.routing import DEV_COMPLEXITY_TIER, DEV_SCORE_TIER_BUCKETS, score_to_dev_tier


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
