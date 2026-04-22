"""Unit tests for the adaptive_iterations pure module."""

from __future__ import annotations

import json
from pathlib import Path

from theforge.config.types import RetryPolicy
from theforge.coordinator.adaptive_iterations import AdaptiveLimits, derive_limits


def _policy(**kw) -> RetryPolicy:
    defaults = dict(
        max_dev_iterations=3,
        max_review_cycles=2,
        max_dev_iterations_cap=6,
        max_review_cycles_cap=4,
        adaptive_iterations=True,
        review_zero_findings_stop=2,
    )
    defaults.update(kw)
    return RetryPolicy(**defaults)


def test_adaptive_disabled_returns_policy_floors(tmp_path: Path):
    result = derive_limits(7, "medium", _policy(adaptive_iterations=False), tmp_path / "none")
    assert isinstance(result, AdaptiveLimits)
    assert result.dev_max == 3
    assert result.review_max == 2
    assert result.audit["enabled"] is False


def test_no_score_no_history_returns_floors(tmp_path: Path):
    result = derive_limits(None, None, _policy(), tmp_path / "missing.jsonl")
    assert result.dev_max == 3
    assert result.review_max == 2
    assert result.audit["rationale"].startswith("no complexity score")


def test_low_score_maps_near_floor(tmp_path: Path):
    result = derive_limits(1, None, _policy(), tmp_path / "missing.jsonl")
    # Score=1 → base is floor.
    assert result.dev_max == 3
    assert result.review_max == 2


def test_high_score_maps_near_cap(tmp_path: Path):
    result = derive_limits(10, None, _policy(), tmp_path / "missing.jsonl")
    # Score=10 → full cap grant.
    assert result.dev_max == 6
    assert result.review_max == 4


def test_limits_never_exceed_cap(tmp_path: Path):
    # Build history where p75 dev usage is absurdly high; cap must still clamp.
    history = tmp_path / "history.jsonl"
    records = [
        {
            "preflight": {"complexity_score": 8},
            "iterations": {
                "dev_iterations_productive": 99,
                "review_cycles_total": 50,
            },
        }
        for _ in range(10)
    ]
    history.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    result = derive_limits(8, None, _policy(), history)
    assert result.dev_max <= 6
    assert result.review_max <= 4


def test_limits_never_below_floor(tmp_path: Path):
    # History p75 is lower than the complexity base — never decrease below
    # complexity-derived base, and never below policy floor.
    history = tmp_path / "history.jsonl"
    records = [
        {
            "preflight": {"complexity_score": 8},
            "iterations": {"dev_iterations_productive": 1, "review_cycles_total": 1},
        }
        for _ in range(5)
    ]
    history.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    result = derive_limits(8, None, _policy(), history)
    assert result.dev_max >= 3  # floor
    assert result.review_max >= 2


def test_history_raises_above_base(tmp_path: Path):
    # Score=5 base is mid-range; history p75=5 dev should push above the base.
    history = tmp_path / "history.jsonl"
    records = [
        {
            "preflight": {"complexity_score": 5},
            "iterations": {"dev_iterations_productive": 5, "review_cycles_total": 3},
        }
        for _ in range(6)
    ]
    history.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    result = derive_limits(5, None, _policy(), history)
    # p75 ≈ 5, so dev_max should be max(base, 6) clamped to cap=6.
    assert result.dev_max == 6
    assert result.audit["p75_dev"] == 5
    assert result.audit["history_sample_size"] == 6


def test_deterministic_same_inputs_same_output(tmp_path: Path):
    history = tmp_path / "h.jsonl"
    records = [
        {"preflight": {"complexity_score": 6}, "iterations": {"dev_iterations_productive": 4}}
        for _ in range(4)
    ]
    history.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    a = derive_limits(6, None, _policy(), history)
    b = derive_limits(6, None, _policy(), history)
    assert a.dev_max == b.dev_max
    assert a.review_max == b.review_max
    assert a.audit == b.audit


def test_malformed_jsonl_lines_skipped(tmp_path: Path):
    history = tmp_path / "h.jsonl"
    lines = [
        "not json",
        json.dumps({"no_iterations": True}),
        json.dumps(
            {
                "preflight": {"complexity_score": 5},
                "iterations": {"dev_iterations_productive": 3},
            }
        ),
        "{trailing garbage",
    ]
    history.write_text("\n".join(lines) + "\n")
    result = derive_limits(5, None, _policy(), history)
    # Only one valid matching record; should not crash and should reflect it.
    assert result.audit["history_sample_size"] == 1


def test_band_fallback_when_score_none(tmp_path: Path):
    # band='large' → representative score 9 → near cap.
    result = derive_limits(None, "large", _policy(), tmp_path / "none")
    assert result.dev_max == 6
    assert result.audit["complexity_score_used"] == 9


def test_out_of_range_score_falls_back_to_band(tmp_path: Path):
    result = derive_limits(99, "small", _policy(), tmp_path / "none")
    # Score is invalid, band='small' → rep score 2 → near floor.
    assert result.audit["complexity_score_used"] == 2
    # Score 2 is near the floor but just above — exact value depends on linear
    # scale, but it must not exceed the score=10 cap value.
    assert 3 <= result.dev_max <= 4


def test_oversized_history_skipped(tmp_path: Path, monkeypatch):
    # Simulate a huge history file by patching the byte cap.
    history = tmp_path / "h.jsonl"
    records = [
        {"preflight": {"complexity_score": 5}, "iterations": {"dev_iterations_productive": 5}}
        for _ in range(3)
    ]
    history.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    monkeypatch.setattr("theforge.coordinator.adaptive_iterations._HISTORY_MAX_BYTES", 1)
    result = derive_limits(5, None, _policy(), history)
    assert result.audit["history_sample_size"] == 0
