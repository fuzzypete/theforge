"""Tests for src/theforge/model_profiles.py — pure aggregation + thin I/O."""

from __future__ import annotations

import yaml

from theforge.model_profiles import (
    COMPLEXITY_BANDS,
    RunOutcome,
    apply_run,
    backfill_from_history,
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


def test_complexity_bands_constant():
    assert COMPLEXITY_BANDS == ("small", "medium", "large")
