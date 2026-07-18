"""Tests for src/theforge/model_profiles.py — pure aggregation + thin I/O."""

from __future__ import annotations

import yaml

from theforge.model_profiles import (
    COMPLEXITY_BANDS,
    RunOutcome,
    apply_run,
    backfill_from_history,
    get_dev_complexity_stats,
    get_dev_success_rate,
    load_profiles,
    save_profiles,
    update_from_run,
)

# ── Pure aggregation ──────────────────────────────────────────────────────


def test_apply_run_records_dev_stats():
    data: dict = {"models": {}}
    outcome = RunOutcome(
        complexity="medium",
        dev_model="sonnet",
        dev_success=True,
        dev_iterations=2,
        dev_cost_usd=1.50,
    )
    apply_run(data, outcome)

    dev = data["models"]["sonnet"]["dev"]
    assert dev["runs"] == 1
    assert dev["success_rate"] == 1.0
    assert dev["avg_iterations"] == 2.0
    assert dev["avg_cost_usd"] == 1.5
    assert dev["by_complexity"]["medium"]["runs"] == 1
    assert dev["by_complexity"]["medium"]["success_rate"] == 1.0
    assert dev["by_complexity"]["medium"]["avg_iterations"] == 2.0
    assert dev["by_complexity"]["medium"]["avg_cost_usd"] == 1.5


def test_apply_run_records_unmeasured_dev_cost_distinct_from_zero():
    """A None dev cost is tallied in _cost_unknown_runs, never folded into
    _cost_sum, and avg_cost_usd is averaged over measured runs only."""
    data: dict = {"models": {}}
    # First run: cost unmeasured (None).
    apply_run(
        data,
        RunOutcome(
            complexity="medium",
            dev_model="sonnet",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=None,
        ),
    )
    dev = data["models"]["sonnet"]["dev"]
    assert dev["runs"] == 1
    assert dev["_cost_unknown_runs"] == 1
    assert dev["_cost_sum"] == 0.0
    assert dev["avg_cost_usd"] == 0.0

    # Second run: a real $2.00 measured cost. avg is over the ONE measured run.
    apply_run(
        data,
        RunOutcome(
            complexity="medium",
            dev_model="sonnet",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=2.00,
        ),
    )
    dev = data["models"]["sonnet"]["dev"]
    assert dev["runs"] == 2
    assert dev["_cost_unknown_runs"] == 1
    assert dev["_cost_sum"] == 2.0
    assert dev["avg_cost_usd"] == 2.0  # 2.0 / (2 runs - 1 unmeasured)


def test_get_dev_complexity_stats_averages_cost_over_measured_runs():
    """avg_cost_usd from the reader divides by measured runs, so unmeasured runs
    do not dilute the average toward zero for downstream budget sizing."""
    data: dict = {"models": {}}
    for cost in (2.0, None, None):
        apply_run(
            data,
            RunOutcome(
                complexity="medium",
                dev_model="sonnet",
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=cost,
            ),
        )
    stats = get_dev_complexity_stats(data, "sonnet", "medium", min_runs=1)
    assert stats is not None
    assert stats["runs"] == 3.0
    # $2.00 over the single measured run, not $2.00/3 = $0.67.
    assert stats["avg_cost_usd"] == 2.0


def test_apply_run_averages_across_runs():
    data: dict = {"models": {}}
    apply_run(
        data,
        RunOutcome(
            complexity="small",
            dev_model="haiku",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=0.10,
        ),
    )
    apply_run(
        data,
        RunOutcome(
            complexity="large",
            dev_model="haiku",
            dev_success=False,
            dev_iterations=3,
            dev_cost_usd=0.50,
        ),
    )
    dev = data["models"]["haiku"]["dev"]
    assert dev["runs"] == 2
    assert dev["success_rate"] == 0.5
    assert dev["avg_iterations"] == 2.0
    assert dev["avg_cost_usd"] == 0.30
    # Per-complexity breakdown
    by = dev["by_complexity"]
    assert by["small"]["runs"] == 1 and by["small"]["success_rate"] == 1.0
    assert by["large"]["runs"] == 1 and by["large"]["success_rate"] == 0.0


def test_apply_run_review_attribution():
    data: dict = {"models": {}}
    outcome = RunOutcome(
        complexity="medium",
        dev_model="sonnet",
        dev_success=True,
        dev_iterations=1,
        dev_cost_usd=0.0,
        reviewers={
            "gemini": (2, 6, 0.40),  # participated in 2 cycles, 6 findings, $0.40
            "opus": (1, 3, 0.25),
        },
    )
    apply_run(data, outcome)

    gem = data["models"]["gemini"]["review"]
    assert gem["runs"] == 2
    assert gem["avg_findings"] == 3.0
    assert gem["avg_cost_usd"] == 0.20

    opus = data["models"]["opus"]["review"]
    assert opus["runs"] == 1
    assert opus["avg_findings"] == 3.0


def test_apply_run_preflight():
    data: dict = {"models": {}}
    outcome = RunOutcome(
        complexity="small",
        dev_model="haiku",
        dev_success=True,
        dev_iterations=1,
        dev_cost_usd=0.1,
        preflight_model="deepseek-r1",
        preflight_cost_usd=0.30,
    )
    apply_run(data, outcome)
    pf = data["models"]["deepseek-r1"]["preflight"]
    assert pf["runs"] == 1
    assert pf["avg_cost_usd"] == 0.30


def test_apply_run_stamps_identity_metadata():
    data: dict = {"models": {}}
    outcome = RunOutcome(
        complexity="small",
        dev_model="claude-sonnet",
        dev_success=True,
        dev_iterations=1,
        dev_cost_usd=0.1,
        dev_actual_model="sonnet",
        dev_cli="claude",
        preflight_model="openai-gpt-5.4",
        preflight_actual_model="gpt-5.4",
        preflight_provider="openai",
        preflight_cost_usd=0.2,
    )
    apply_run(data, outcome)

    # Storage now keys by canonical ID (provider/model/transport.kind), not by
    # the legacy profile name. The legacy `dev_model` and `preflight_model`
    # arguments are display labels only.
    assert data["models"]["anthropic/sonnet/cli"]["_identity"] == {
        "provider": "anthropic",
        "model": "sonnet",
        "transport": "cli",
        "cli": "claude",
    }
    assert data["models"]["openai/gpt-5.4/api"]["_identity"] == {
        "provider": "openai",
        "model": "gpt-5.4",
        "transport": "api",
    }
    assert "claude-sonnet" not in data["models"]
    assert "openai-gpt-5.4" not in data["models"]


def test_complexity_normalization_handles_legacy_enums():
    data: dict = {"models": {}}
    # LOW / HIGH should map to small / large
    apply_run(
        data,
        RunOutcome(
            complexity="LOW",
            dev_model="m",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=0.0,
        ),
    )
    apply_run(
        data,
        RunOutcome(
            complexity="HIGH",
            dev_model="m",
            dev_success=False,
            dev_iterations=1,
            dev_cost_usd=0.0,
        ),
    )
    by = data["models"]["m"]["dev"]["by_complexity"]
    assert "small" in by and "large" in by
    assert by["small"]["runs"] == 1
    assert by["large"]["runs"] == 1


# ── I/O round-trip ────────────────────────────────────────────────────────


def test_load_missing_returns_empty_skeleton(tmp_path):
    path = tmp_path / "missing.yaml"
    assert load_profiles(path) == {"models": {}}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "profiles.yaml"
    data = {"models": {"m1": {"dev": {"runs": 1}}}}
    save_profiles(path, data)
    assert path.exists()
    reloaded = load_profiles(path)
    assert reloaded == data


def test_load_tolerates_malformed_yaml(tmp_path):
    path = tmp_path / "profiles.yaml"
    path.write_text("not a dict", encoding="utf-8")
    # Scalar yaml → falls back to empty skeleton.
    assert load_profiles(path) == {"models": {}}


# ── Backfill from assignment_history.yaml ─────────────────────────────────


def test_backfill_aggregates_dev_outcomes(tmp_path):
    history = tmp_path / "assignment_history.yaml"
    history.write_text(
        yaml.safe_dump(
            {
                "escalations": [
                    {
                        "story": "a",
                        "complexity": "MEDIUM",
                        "dev_model": "sonnet",
                        "outcome": "DONE",
                    },
                    {
                        "story": "b",
                        "complexity": "MEDIUM",
                        "dev_model": "sonnet",
                        "outcome": "ESCALATE",
                    },
                    {"story": "c", "complexity": "HIGH", "dev_model": "opus", "outcome": "DONE"},
                ]
            }
        ),
        encoding="utf-8",
    )
    data = backfill_from_history(history)
    sonnet = data["models"]["sonnet"]["dev"]
    assert sonnet["runs"] == 2
    assert sonnet["success_rate"] == 0.5
    assert sonnet["avg_iterations"] == 0.0  # history carries no iteration data
    assert sonnet["avg_cost_usd"] == 0.0
    assert sonnet["by_complexity"]["medium"]["runs"] == 2

    opus = data["models"]["opus"]["dev"]
    assert opus["by_complexity"]["large"]["runs"] == 1


def test_backfill_missing_history_returns_empty(tmp_path):
    data = backfill_from_history(tmp_path / "nope.yaml")
    assert data == {"models": {}}


# ── update_from_run: backfill on first run ────────────────────────────────


def test_update_from_run_backfills_when_profiles_absent(tmp_path):
    history = tmp_path / "assignment_history.yaml"
    history.write_text(
        yaml.safe_dump(
            {
                "escalations": [
                    {"complexity": "small", "dev_model": "haiku", "outcome": "DONE"},
                    {"complexity": "small", "dev_model": "haiku", "outcome": "DONE"},
                ]
            }
        ),
        encoding="utf-8",
    )
    profiles_path = tmp_path / "model_profiles.yaml"

    data = update_from_run(
        profiles_path,
        history,
        RunOutcome(
            complexity="small",
            dev_model="haiku",
            dev_success=False,
            dev_iterations=2,
            dev_cost_usd=0.20,
        ),
    )
    assert profiles_path.exists()
    dev = data["models"]["haiku"]["dev"]
    # 2 from backfill (DONE) + 1 new (failed)
    assert dev["runs"] == 3
    assert round(dev["success_rate"], 4) == round(2 / 3, 4)
    # avg_iterations = (0 + 0 + 2) / 3
    assert round(dev["avg_iterations"], 4) == round(2 / 3, 4)


def test_update_from_run_uses_existing_profiles_not_history(tmp_path):
    profiles_path = tmp_path / "model_profiles.yaml"
    # Pre-existing profiles file: backfill should NOT run.
    save_profiles(profiles_path, {"models": {"haiku": {"dev": {"runs": 5, "_successes": 5}}}})
    history = tmp_path / "assignment_history.yaml"
    history.write_text(
        yaml.safe_dump(
            {"escalations": [{"complexity": "small", "dev_model": "haiku", "outcome": "DONE"}]}
        ),
        encoding="utf-8",
    )
    data = update_from_run(
        profiles_path,
        history,
        RunOutcome(
            complexity="small",
            dev_model="haiku",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=0.0,
        ),
    )
    # Previous runs=5 + 1 new = 6 (not 5+1+1 from history)
    assert data["models"]["haiku"]["dev"]["runs"] == 6


# ── Reader helper ─────────────────────────────────────────────────────────


def test_get_dev_success_rate_requires_min_runs():
    profiles = {
        "models": {
            "sonnet": {
                "dev": {
                    "runs": 2,
                    "success_rate": 1.0,
                    "by_complexity": {
                        "medium": {"runs": 2, "success_rate": 1.0},
                    },
                }
            },
            "opus": {
                "dev": {
                    "runs": 10,
                    "success_rate": 0.8,
                    "by_complexity": {
                        "medium": {"runs": 10, "success_rate": 0.8},
                    },
                }
            },
        }
    }
    # sonnet has fewer runs than default min_runs=3 → None
    assert get_dev_success_rate(profiles, "sonnet", "medium") is None
    assert get_dev_success_rate(profiles, "opus", "medium") == 0.8
    # Unknown model / role → None
    assert get_dev_success_rate(profiles, "gemini", "medium") is None


def test_get_dev_success_rate_aggregates_fragmented_aliases():
    profiles = {
        "models": {
            "claude-sonnet": {
                "dev": {
                    "by_complexity": {
                        "small": {"runs": 2, "success_rate": 0.5},
                    }
                }
            },
            "sonnet-cli": {
                "dev": {
                    "by_complexity": {
                        "small": {"runs": 3, "success_rate": round(2 / 3, 4)},
                    }
                }
            },
            "openai-gpt-5.4": {
                "dev": {
                    "by_complexity": {
                        "medium": {"runs": 2, "success_rate": 0.5},
                    }
                }
            },
            "openai-api-gpt-5.4": {
                "dev": {
                    "by_complexity": {
                        "medium": {"runs": 2, "success_rate": 1.0},
                    }
                }
            },
        }
    }

    assert (
        get_dev_success_rate(
            profiles,
            "claude-sonnet",
            "small",
            actual_model="sonnet",
            cli="claude",
        )
        == 0.6
    )
    assert (
        get_dev_success_rate(
            profiles,
            "openai-gpt-5.4",
            "medium",
            actual_model="gpt-5.4",
            provider="openai",
            cli="codex",
        )
        == 0.75
    )


def test_get_dev_complexity_stats_requires_band_averages():
    profiles = {
        "models": {
            "sonnet": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 3,
                            "avg_iterations": 2.5,
                            "avg_cost_usd": 1.25,
                        }
                    }
                }
            }
        }
    }
    assert get_dev_complexity_stats(profiles, "sonnet", "medium") == {
        "runs": 3.0,
        "avg_iterations": 2.5,
        "avg_cost_usd": 1.25,
        "max_duration_s": 0.0,
        "duration_runs": 0.0,
        "max_killed_timeout_s": 0.0,
    }


def test_get_dev_complexity_stats_aggregates_fragmented_aliases():
    profiles = {
        "models": {
            "claude-sonnet": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 2,
                            "avg_iterations": 3.0,
                            "avg_cost_usd": 1.0,
                        }
                    }
                }
            },
            "sonnet-cli": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 3,
                            "avg_iterations": 5.0,
                            "avg_cost_usd": 2.0,
                        }
                    }
                }
            },
        }
    }

    assert get_dev_complexity_stats(
        profiles,
        "claude-sonnet",
        "medium",
        actual_model="sonnet",
        cli="claude",
    ) == {
        "runs": 5.0,
        "avg_iterations": 4.2,
        "avg_cost_usd": 1.6,
        "max_duration_s": 0.0,
        "duration_runs": 0.0,
        "max_killed_timeout_s": 0.0,
    }


def test_get_dev_complexity_stats_returns_none_under_min_runs():
    profiles = {
        "models": {
            "sonnet": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 2,
                            "avg_iterations": 2.5,
                            "avg_cost_usd": 1.25,
                        }
                    }
                }
            }
        }
    }
    assert get_dev_complexity_stats(profiles, "sonnet", "medium") is None


def test_complexity_bands_constant():
    assert COMPLEXITY_BANDS == ("small", "medium", "large")


# ── Duration aggregation + censored-kill floor ────────────────────────────


def test_completed_run_records_duration():
    """A completed run folds its wall-clock into avg/max duration accumulators."""
    data: dict = {"models": {}}
    for dur in (100.0, 300.0):
        apply_run(
            data,
            RunOutcome(
                complexity="medium",
                dev_model="sonnet",
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=1.0,
                dev_duration_s=dur,
            ),
        )
    dev = data["models"]["sonnet"]["dev"]
    assert dev["_duration_runs"] == 2
    assert dev["_duration_sum"] == 400.0
    assert dev["avg_duration_s"] == 200.0
    assert dev["max_duration_s"] == 300.0
    # No kill occurred, so the kill-floor key is never written (lazy schema).
    assert dev.get("max_killed_timeout_s", 0.0) == 0.0
    bc = dev["by_complexity"]["medium"]
    assert bc["_duration_runs"] == 2
    assert bc["avg_duration_s"] == 200.0
    assert bc["max_duration_s"] == 300.0


def test_killed_run_raises_kill_limit_without_touching_duration():
    """A harness-killed run is a censored observation: it only raises
    max_killed_timeout_s and never lowers learned duration."""
    data: dict = {"models": {}}
    # A fast completed run establishes a low learned duration.
    apply_run(
        data,
        RunOutcome(
            complexity="medium",
            dev_model="sonnet",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=1.0,
            dev_duration_s=120.0,
        ),
    )
    # A run killed at a 1350s limit: duration unknown, only the floor moves.
    apply_run(
        data,
        RunOutcome(
            complexity="medium",
            dev_model="sonnet",
            dev_success=False,
            dev_iterations=1,
            dev_cost_usd=None,
            dev_duration_s=None,
            dev_timeout_killed=True,
            dev_timeout_limit_s=1350,
        ),
    )
    bc = data["models"]["sonnet"]["dev"]["by_complexity"]["medium"]
    # Duration learning untouched by the kill.
    assert bc["_duration_runs"] == 1
    assert bc["avg_duration_s"] == 120.0
    assert bc["max_duration_s"] == 120.0
    # The kill only bounds the timeout from below.
    assert bc["max_killed_timeout_s"] == 1350.0


def test_get_dev_complexity_stats_surfaces_duration_and_kill_floor():
    data: dict = {"models": {}}
    for dur in (100.0, 400.0, 250.0):
        apply_run(
            data,
            RunOutcome(
                complexity="medium",
                dev_model="sonnet",
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=1.0,
                dev_duration_s=dur,
            ),
        )
    apply_run(
        data,
        RunOutcome(
            complexity="medium",
            dev_model="sonnet",
            dev_success=False,
            dev_iterations=1,
            dev_cost_usd=None,
            dev_timeout_killed=True,
            dev_timeout_limit_s=900,
        ),
    )
    stats = get_dev_complexity_stats(data, "sonnet", "medium", min_runs=1)
    assert stats is not None
    assert stats["max_duration_s"] == 400.0
    assert stats["duration_runs"] == 3.0
    assert stats["max_killed_timeout_s"] == 900.0


def test_legacy_profile_without_duration_still_yields_iteration_stats():
    """Profiles predating the duration fields must still return iteration stats
    (duration/kill floors default to 0.0, they do not void the result)."""
    profiles = {
        "models": {
            "sonnet": {
                "dev": {
                    "by_complexity": {
                        "medium": {
                            "runs": 4,
                            "avg_iterations": 3.0,
                            "avg_cost_usd": 1.0,
                        }
                    }
                }
            }
        }
    }
    stats = get_dev_complexity_stats(profiles, "sonnet", "medium", min_runs=1)
    assert stats is not None
    assert stats["avg_iterations"] == 3.0
    assert stats["max_duration_s"] == 0.0
    assert stats["duration_runs"] == 0.0
    assert stats["max_killed_timeout_s"] == 0.0


def test_reset_zeros_duration_fields():
    from theforge.model_profiles import reset_profile_data

    data: dict = {"models": {}}
    apply_run(
        data,
        RunOutcome(
            complexity="medium",
            dev_model="sonnet",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=1.0,
            dev_duration_s=300.0,
        ),
    )
    updated, _ = reset_profile_data(data, "sonnet", role="dev", complexity="medium")
    bc = updated["models"]["sonnet"]["dev"]["by_complexity"]["medium"]
    assert bc["_duration_runs"] == 0
    assert bc["_duration_sum"] == 0.0
    assert bc["avg_duration_s"] == 0.0
    assert bc["max_duration_s"] == 0.0
    assert bc["max_killed_timeout_s"] == 0.0
    # Recomputed dev section reflects the zeroed band.
    assert updated["models"]["sonnet"]["dev"]["max_duration_s"] == 0.0


def test_merge_preserves_duration_and_kill_floor():
    """Merging two dev sections sums duration runs and maxes the maxima."""
    from theforge.model_profiles import _merge_dev

    src_a: dict = {"models": {}}
    apply_run(
        src_a,
        RunOutcome(
            complexity="medium",
            dev_model="sonnet",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=1.0,
            dev_duration_s=200.0,
        ),
    )
    apply_run(
        src_a,
        RunOutcome(
            complexity="medium",
            dev_model="sonnet",
            dev_success=False,
            dev_iterations=1,
            dev_cost_usd=None,
            dev_timeout_killed=True,
            dev_timeout_limit_s=800,
        ),
    )
    src_b: dict = {"models": {}}
    apply_run(
        src_b,
        RunOutcome(
            complexity="medium",
            dev_model="sonnet",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=1.0,
            dev_duration_s=500.0,
        ),
    )
    target = src_a["models"]["sonnet"]["dev"]
    _merge_dev(target, src_b["models"]["sonnet"]["dev"])
    assert target["_duration_runs"] == 2  # two completed runs (kill excluded)
    assert target["max_duration_s"] == 500.0
    assert target["max_killed_timeout_s"] == 800.0
    bc = target["by_complexity"]["medium"]
    assert bc["_duration_runs"] == 2
    assert bc["max_duration_s"] == 500.0
    assert bc["max_killed_timeout_s"] == 800.0


def test_merge_tolerates_legacy_entry_without_duration_keys():
    """A legacy src dev section lacking the new keys merges cleanly (keys default
    to 0) without erasing the target's learned duration."""
    from theforge.model_profiles import _merge_dev

    holder: dict = {"models": {}}
    apply_run(
        holder,
        RunOutcome(
            complexity="medium",
            dev_model="sonnet",
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=1.0,
            dev_duration_s=350.0,
        ),
    )
    target = holder["models"]["sonnet"]["dev"]
    legacy_src = {
        "runs": 2,
        "success_rate": 1.0,
        "avg_iterations": 3.0,
        "avg_cost_usd": 1.0,
        "by_complexity": {
            "medium": {"runs": 2, "avg_iterations": 3.0, "avg_cost_usd": 1.0},
        },
    }
    _merge_dev(target, legacy_src)
    assert target["max_duration_s"] == 350.0  # target's learned value preserved
    assert target["_duration_runs"] == 1  # legacy src contributes 0 duration runs
