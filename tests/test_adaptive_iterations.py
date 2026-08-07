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
    band: str = "medium",
    runs: int = 3,
    avg_iterations: float = 4.0,
    avg_cost: float = 2.0,
):
    return {
        "models": {
            model: {
                "dev": {
                    "by_complexity": {
                        band: {
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
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles(),
    )
    assert isinstance(result, AdaptiveLimits)
    assert result.dev_max == 3
    assert result.review_max == 2
    assert result.dev_timeout_seconds == 900
    assert result.dev_cost_estimate_usd == 10.0
    assert result.audit["enabled"] is False


def test_no_score_uses_static_limits(tmp_path: Path):
    result = derive_limits(
        None,
        None,
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
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
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "missing.jsonl",
        model_profiles=_profiles(avg_iterations=3.2, avg_cost=1.2),
    )
    # ceil(3.2 * 1.5) = 5; timeout scales from 900 / 3 = 300s per iteration.
    # Budget: medium band uses 2.0x headroom → 1.2 * 2.0 = 2.4
    assert result.dev_max == 5
    assert result.dev_timeout_seconds == 1500
    assert result.dev_cost_estimate_usd == 2.4
    assert result.audit["profile_history_runs"] == 3
    assert result.audit["estimate_headroom_factor"] == 2.0


def test_headroom_clamps_dev_iterations_to_cap(tmp_path: Path):
    result = derive_limits(
        5,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
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
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "missing.jsonl",
        model_profiles=_profiles(runs=2, avg_iterations=9.0, avg_cost=9.0),
    )
    assert result.dev_max == 3
    assert result.dev_timeout_seconds == 900
    assert result.dev_cost_estimate_usd == 10.0
    assert result.review_max == 3
    assert result.audit["base_review"] == 3
    assert result.audit["profile_history_runs"] == 0
    assert result.audit["chosen_review_max"] == 3
    assert "complexity-derived review limit" in result.audit["rationale"]


def test_deterministic_same_inputs_same_output(tmp_path: Path):
    profiles = _profiles(avg_iterations=4.0, avg_cost=1.0)
    a = derive_limits(
        6,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
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
        base_cost_estimate_usd=10.0,
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
        base_cost_estimate_usd=10.0,
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
        base_cost_estimate_usd=10.0,
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


def test_corrupt_substrate_propagates_error(tmp_path: Path):
    """A corrupt substrate must surface, not silently mask the problem."""
    import pytest

    from theforge.coordinator import audit_substrate

    sub_path = audit_substrate.substrate_path(tmp_path)
    sub_path.parent.mkdir(parents=True, exist_ok=True)
    sub_path.write_bytes(b"not a sqlite database" * 50)
    with pytest.raises(audit_substrate.SubstrateCorruptError):
        derive_limits(
            5,
            "medium",
            _policy(),
            model_name="dev",
            base_timeout_seconds=900,
            base_cost_estimate_usd=10.0,
            static_dev_max=3,
            review_history_path=tmp_path,
            model_profiles=_profiles(),
        )


def test_insufficient_profile_history_still_uses_review_history_signal(tmp_path: Path):
    """The review-cycle uplift signal flows from the SQLite substrate."""
    from theforge.coordinator import audit_substrate

    conn = audit_substrate.create_or_open(tmp_path)
    try:
        for run_id, score, cycles in (("r1", 5, 3), ("r2", 6, 4)):
            audit_substrate.upsert_run_record(
                conn,
                {
                    "run_id": run_id,
                    "task": {"slug": run_id},
                    "timing": {"started_at": f"2026-03-0{int(run_id[-1])}T00:00:00+00:00"},
                    "preflight": {"complexity_score": score},
                    "iterations": {"review_cycles_total": cycles},
                },
                provenance="native",
            )
        conn.commit()
    finally:
        conn.close()
    result = derive_limits(
        5,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path,
        model_profiles=_profiles(runs=2, avg_iterations=9.0, avg_cost=9.0),
    )
    assert result.dev_max == 3
    assert result.review_max == 4
    assert result.audit["review_history_sample_size"] == 2
    assert result.audit["p75_review"] == 4
    assert result.audit["chosen_review_max"] == 4
    assert "review history raised review_max to 4" in result.audit["rationale"]


# ── Score-scoped dev cost estimate (#2284) ────────────────────────────────


def _profiles_with_score_history(
    *,
    score: int = 4,
    score_runs: int = 26,
    score_avg_cost: float = 2.32,
    band: str = "medium",
    band_avg_cost: float = 4.947,
    band_runs: int = 194,
    other_model_runs: int = 0,
    other_model_avg_cost: float = 0.0,
):
    """The issue-2252 shape: cheap score history, expensive band/model history."""
    profiles = {
        "models": {
            "dev": {
                "dev": {
                    "by_complexity": {
                        band: {
                            "runs": band_runs,
                            "avg_iterations": 4.0,
                            "avg_cost_usd": band_avg_cost,
                        }
                    },
                    "by_complexity_score": {
                        str(score): {
                            "runs": score_runs,
                            "avg_iterations": 4.0,
                            "avg_cost_usd": score_avg_cost,
                        }
                    },
                }
            }
        }
    }
    if other_model_runs:
        profiles["models"]["other"] = {
            "dev": {
                "by_complexity_score": {
                    str(score): {
                        "runs": other_model_runs,
                        "avg_iterations": 4.0,
                        "avg_cost_usd": other_model_avg_cost,
                    }
                }
            }
        }
    return profiles


def test_dev_estimate_uses_score_history_not_the_band_and_model_average(tmp_path: Path):
    """The two figures seating subtracts must describe the same population.

    Run 076fa19d5fc3 estimated $9.89 (medium band, one model, x2.0) against a
    $10.18 allocation drawn from 26 score-4 samples whose max was $8.14. The
    estimate now comes from those same score-4 runs: $2.32 x 1.25.
    """
    result = derive_limits(
        4,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles_with_score_history(),
    )

    assert result.dev_cost_estimate_usd == 2.9
    basis = result.audit["dev_cost_estimate_basis"]
    assert basis["source"] == "profile_observed_score"
    assert basis["complexity_score"] == 4
    assert basis["statistic"] == "avg_cost_usd"
    assert basis["headroom_basis"] == "allocation_headroom"
    assert basis["headroom_multiplier"] == 1.25
    assert basis["sample_count"] == 26
    # The estimate prices the dev phase; the allocation prices the whole story.
    assert basis["scope"] == "dev_phase"
    assert basis["allocation_comparable"] is True
    assert result.audit["score_cost_history_runs"] == 26
    assert "score-4 run(s) across all models" in result.audit["rationale"]


def test_iterations_and_timeout_still_come_from_the_band_and_model_history(tmp_path: Path):
    """Only the dollar figure moved. Sizing the work stays a model property."""
    result = derive_limits(
        4,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles_with_score_history(),
    )

    # ceil(4.0 * 1.5) = 6 → clamped to the cap of 6; timeout 6 * 300 = 1800.
    assert result.dev_max == 6
    assert result.dev_timeout_seconds == 1800
    assert result.audit["profile_history_runs"] == 194
    assert result.audit["profile_avg_cost_usd"] == 4.947


def test_score_history_is_aggregated_across_models_like_the_allocation(tmp_path: Path):
    """The allocation spans all models at the score, so the estimate must too."""
    result = derive_limits(
        4,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles_with_score_history(
            score_runs=2, score_avg_cost=4.0, other_model_runs=2, other_model_avg_cost=1.0
        ),
    )

    # (2 x $4.00 + 2 x $1.00) / 4 = $2.50, x 1.25 = $3.125.
    assert result.audit["score_cost_history_runs"] == 4
    assert result.dev_cost_estimate_usd == 3.125


def test_without_score_history_the_estimate_is_recorded_as_not_comparable(tmp_path: Path):
    """The band estimate is still the best available figure — just not subtractable."""
    result = derive_limits(
        5,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles(band="medium", avg_cost=3.0),
    )

    assert result.dev_cost_estimate_usd == 6.0  # band estimate retained for audit
    basis = result.audit["dev_cost_estimate_basis"]
    assert basis["source"] == "profile_observed_band"
    assert basis["allocation_comparable"] is False
    assert result.audit["score_cost_history_runs"] == 0
    assert "not comparable with the story allocation" in result.audit["rationale"]


def test_score_history_below_the_run_floor_is_not_used(tmp_path: Path):
    result = derive_limits(
        4,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles_with_score_history(score_runs=2),
    )

    assert result.audit["score_cost_history_runs"] == 0
    assert result.audit["dev_cost_estimate_basis"]["allocation_comparable"] is False


def test_static_fallback_estimate_is_not_comparable_either(tmp_path: Path):
    result = derive_limits(
        5,
        "medium",
        _policy(adaptive_iterations=False),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles(),
    )

    basis = result.audit["dev_cost_estimate_basis"]
    assert basis["source"] == "configured_fallback"
    assert basis["allocation_comparable"] is False
    assert basis["reason"] == "adaptive_iterations_disabled"


# ── Budget headroom scaling by complexity band ────────────────────────────


def test_budget_headroom_large_band_uses_2_5x(tmp_path: Path):
    result = derive_limits(
        9,
        "large",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles(band="large", avg_cost=4.0),
    )
    # large band: 4.0 * 2.5 = 10.0
    assert result.dev_cost_estimate_usd == 10.0
    assert result.audit["estimate_headroom_factor"] == 2.5


def test_budget_headroom_medium_band_uses_2_0x(tmp_path: Path):
    result = derive_limits(
        5,
        "medium",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles(band="medium", avg_cost=3.0),
    )
    # medium band: 3.0 * 2.0 = 6.0
    assert result.dev_cost_estimate_usd == 6.0
    assert result.audit["estimate_headroom_factor"] == 2.0


def test_budget_headroom_small_band_uses_1_5x(tmp_path: Path):
    result = derive_limits(
        2,
        "small",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles(band="small", avg_cost=1.0),
    )
    # small band: 1.0 * 1.5 = 1.5
    assert result.dev_cost_estimate_usd == 1.5
    assert result.audit["estimate_headroom_factor"] == 1.5


def test_budget_headroom_unknown_band_falls_back_to_default(tmp_path: Path):
    result = derive_limits(
        5,
        None,
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles(band="medium", avg_cost=2.0),
    )
    # No band → falls back to _HEADROOM_FACTOR (1.5x): 2.0 * 1.5 = 3.0
    assert result.dev_cost_estimate_usd == 3.0
    assert result.audit["estimate_headroom_factor"] == 1.5


def test_estimate_headroom_concrete_large_scenario(tmp_path: Path):
    # Documents that the per-story dollar value is a historical-cost ESTIMATE,
    # not a hard cap (issue #1148): avg_cost $4.13, actual $8.44. The old 1.5x
    # estimate ($6.20) was low; the band-scaled 2.5x estimate ($10.33) tracks
    # real large-story spend. Post-hoc dollar enforcement no longer lives here
    # (sprint-level governance owns that), so a low estimate never escalates.
    result = derive_limits(
        9,
        "large",
        _policy(),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles(band="large", avg_cost=4.13),
    )
    assert result.dev_cost_estimate_usd >= 8.44, (
        f"Estimate {result.dev_cost_estimate_usd:.4f} should track the $8.44 run from issue #1148"
    )


def test_read_history_tail_raises_when_substrate_missing_with_legacy(tmp_path: Path):
    """Legacy history.jsonl present, substrate absent → operator-facing error.

    Adaptive iteration must not silently fall back to no-history routing
    when audit inputs exist on disk; the spec mandates a clear
    SubstrateMissingError so the operator runs `forge audits rebuild
    --include-legacy-history`.
    """
    import pytest

    from theforge.coordinator import audit_substrate
    from theforge.coordinator.adaptive_iterations import _read_history_tail

    audits = tmp_path / ".forge" / "audits"
    audits.mkdir(parents=True)
    (audits / "history.jsonl").write_text('{"run_id": "x"}\n', encoding="utf-8")

    with pytest.raises(audit_substrate.SubstrateMissingError):
        _read_history_tail(tmp_path)


def test_read_history_tail_returns_empty_for_truly_fresh_repo(tmp_path: Path):
    """No substrate, no audit inputs → empty list (the legitimate fresh-repo path)."""
    from theforge.coordinator.adaptive_iterations import _read_history_tail

    assert _read_history_tail(tmp_path) == []


# ── Duration-aware timeout floor (issue #1762) ────────────────────────────


def _profiles_with_duration(
    *,
    band: str = "medium",
    runs: int = 7,
    avg_iterations: float = 1.0,
    avg_cost: float = 1.0,
    max_duration_s: float = 0.0,
    duration_runs: int = 0,
    max_killed_timeout_s: float = 0.0,
):
    return {
        "models": {
            "dev": {
                "dev": {
                    "by_complexity": {
                        band: {
                            "runs": runs,
                            "avg_iterations": avg_iterations,
                            "avg_cost_usd": avg_cost,
                            "_duration_runs": duration_runs,
                            "max_duration_s": max_duration_s,
                            "max_killed_timeout_s": max_killed_timeout_s,
                        }
                    }
                }
            }
        }
    }


def test_timeout_floored_by_observed_duration_for_fast_converging_runs(tmp_path: Path):
    """Fast-converging (low-iteration) runs whose observed durations approach a
    large value must not shrink the timeout below observed duration + headroom."""
    result = derive_limits(
        5,
        "medium",
        _policy(max_dev_iterations=1, max_dev_iterations_cap=6),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles_with_duration(
            avg_iterations=1.0, max_duration_s=1207.0, duration_runs=7
        ),
    )
    # avg_iterations=1.0 → iteration-derived timeout would be ~600s; the observed
    # duration floor (1207 * 1.5 = 1811) dominates.
    assert result.dev_timeout_seconds >= 1207
    assert result.dev_timeout_seconds == 1811
    assert result.audit["timeout_floored_on_observation"] is True
    assert result.audit["duration_floor_seconds"] == 1811
    assert result.audit["profile_max_duration_s"] == 1207.0
    assert "observed run duration" in result.audit["rationale"]


def test_timeout_never_below_kill_limit(tmp_path: Path):
    """A profile containing a timeout-killed run yields a timeout >= the kill
    limit — a censored observation can never drive the limit below where it died."""
    result = derive_limits(
        5,
        "medium",
        _policy(max_dev_iterations=1, max_dev_iterations_cap=6),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles_with_duration(
            avg_iterations=1.0,
            max_duration_s=200.0,
            duration_runs=6,
            max_killed_timeout_s=1350.0,
        ),
    )
    assert result.dev_timeout_seconds >= 1350
    assert result.audit["kill_floor_seconds"] == 1350
    assert result.audit["profile_max_killed_timeout_s"] == 1350.0
    assert "killed comparable runs" in result.audit["rationale"]


def test_timeout_never_below_base_timeout(tmp_path: Path):
    """chosen_timeout is floored by the operator-configured base timeout."""
    result = derive_limits(
        5,
        "medium",
        _policy(max_dev_iterations=1, max_dev_iterations_cap=6),
        model_name="dev",
        base_timeout_seconds=900,
        base_cost_estimate_usd=10.0,
        static_dev_max=3,
        review_history_path=tmp_path / "none",
        model_profiles=_profiles_with_duration(
            avg_iterations=1.0, max_duration_s=50.0, duration_runs=6
        ),
    )
    # Iteration-derived (~600) and duration floor (~75) both below base 900.
    assert result.dev_timeout_seconds == 900


def test_read_history_tail_reads_native_substrate_rows(tmp_path: Path):
    """Substrate-only fixture: native rows surface as story-level history records."""
    from theforge.coordinator import audit_substrate
    from theforge.coordinator.adaptive_iterations import _read_history_tail

    audit_substrate.seed_records(
        tmp_path,
        [
            {
                "run_id": "rec-1",
                "task": {"slug": "story-a"},
                "outcome": {"success": True, "final_phase": "DONE"},
                "timing": {"started_at": "2026-04-01T10:00:00+00:00"},
                "preflight": {"complexity": "medium", "complexity_score": 5},
                "iterations": {"dev_iterations_productive": 2, "review_cycles_total": 1},
            }
        ],
    )
    records = _read_history_tail(tmp_path)
    assert len(records) == 1
    assert records[0]["run_id"] == "rec-1"
