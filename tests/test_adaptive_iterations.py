"""Unit tests for the adaptive_iterations pure module."""

from __future__ import annotations

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


def _profiles(
    *,
    model: str = "dev",
    runs: int = 3,
    avg_iterations: float = 4.0,
    avg_cost: float = 2.0,
):
    return {
        "models": {
            model: {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": runs,
                            "avg_iterations": avg_iterations,
                            "avg_cost_usd": avg_cost,
                        }
                    }
                }
            }
        }
    }


def test_adaptive_disabled_returns_policy_floors(tmp_path: Path):
    result = derive_limits(
        7,
        "medium",
        _policy(adaptive_iterations=False),
        model_name="dev",
        base_timeout_seconds=900,
        base_budget_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles(),
    )
    assert isinstance(result, AdaptiveLimits)
    assert result.dev_max == 3
    assert result.review_max == 2
    assert result.dev_timeout_seconds == 900
    assert result.dev_budget_usd == 10.0
    assert result.audit["enabled"] is False


def test_no_score_uses_static_limits(tmp_path: Path):
    result = derive_limits(
        None,
        None,
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_budget_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "missing.jsonl",
        model_profiles=_profiles(),
    )
    assert result.dev_max == 3
    assert result.review_max == 2
    assert result.audit["rationale"].startswith("no complexity score")


def test_profile_history_derives_dev_limits_from_headroom(tmp_path: Path):
    result = derive_limits(
        5,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_budget_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "missing.jsonl",
        model_profiles=_profiles(avg_iterations=3.2, avg_cost=1.2),
    )
    # ceil(3.2 * 1.5) = 5; timeout scales from 900 / 3 = 300s per iteration.
    assert result.dev_max == 5
    assert result.dev_timeout_seconds == 1500
    assert result.dev_budget_usd == 1.8
    assert result.audit["profile_history_runs"] == 3


def test_headroom_clamps_dev_iterations_to_cap(tmp_path: Path):
    result = derive_limits(
        5,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_budget_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "missing.jsonl",
        model_profiles=_profiles(avg_iterations=9.0, avg_cost=9.0),
    )
    assert result.dev_max == 6
    assert result.dev_timeout_seconds == 1800


def test_insufficient_profile_history_falls_back_to_static_limits(tmp_path: Path):
    result = derive_limits(
        5,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_budget_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "missing.jsonl",
        model_profiles=_profiles(runs=2, avg_iterations=9.0, avg_cost=9.0),
    )
    assert result.dev_max == 3
    assert result.dev_timeout_seconds == 900
    assert result.dev_budget_usd == 10.0
    assert result.audit["profile_history_runs"] == 0


def test_deterministic_same_inputs_same_output(tmp_path: Path):
    profiles = _profiles(avg_iterations=4.0, avg_cost=1.0)
    a = derive_limits(
        6,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_budget_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "missing.jsonl",
        model_profiles=profiles,
    )
    b = derive_limits(
        6,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_budget_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "missing.jsonl",
        model_profiles=profiles,
    )
    assert a.dev_max == b.dev_max
    assert a.review_max == b.review_max
    assert a.audit == b.audit


def test_band_fallback_when_score_none(tmp_path: Path):
    result = derive_limits(
        None,
        "large",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_budget_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles(),
    )
    assert result.dev_max == 3
    assert result.audit["complexity_score_used"] == 9


def test_out_of_range_score_falls_back_to_band(tmp_path: Path):
    result = derive_limits(
        99,
        "small",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_budget_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles={
            "models": {
                "dev": {
                    "dev": {
                        "by_complexity": {
                            "small": {"runs": 3, "avg_iterations": 2.0, "avg_cost_usd": 0.5}
                        }
                    }
                }
            }
        },
    )
    assert result.audit["complexity_score_used"] == 2
    assert result.dev_max == 3


def test_oversized_history_skipped(tmp_path: Path, monkeypatch):
    history = tmp_path / "h.jsonl"
    history.write_text(
        '{"preflight":{"complexity_score":5},"iterations":{"review_cycles_total":3}}\n'
    )
    monkeypatch.setattr("theforge.coordinator.adaptive_iterations._HISTORY_MAX_BYTES", 1)
    result = derive_limits(
        5,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_budget_usd=10.0,
        static_dev_max=3,
        review_history_path=history,
        model_profiles=_profiles(),
    )
    assert result.audit["review_history_sample_size"] == 0
