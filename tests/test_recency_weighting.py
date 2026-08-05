"""Recency-weighted dev success rate (#1392, ADR-0006 clause 2.4).

Covers the shared weighting helper as it is exercised through the profile
reader API: recent-vs-old identical raw rates, strong long-term history
surviving one recent bad run, the cold-start floor short-circuit, taint
exclusion from the weighted aggregate, and the operator-facing config knobs.
"""

from __future__ import annotations

import pytest

from theforge.config.models import _parse_recency
from theforge.config.types import RecencyConfig
from theforge.model_profiles import (
    RunOutcome,
    apply_run,
    get_dev_signal,
    get_dev_success_rate,
)


def _fold(
    data: dict, model: str, success: bool, *, tainted: bool = False, complexity: str = "medium"
) -> None:
    """Fold one genuine dev run for ``model`` at the given complexity band."""
    apply_run(
        data,
        RunOutcome(
            complexity=complexity,
            dev_model=model,
            dev_success=success,
            dev_iterations=1,
            dev_cost_usd=0.0,
            dev_tainted=tainted,
        ),
    )


# ── Recency weighting behavior ─────────────────────────────────────────────


def test_recent_clean_outranks_recent_noisy_at_identical_raw_rate():
    """Two models, identical lifetime raw rate; the one whose *recent* runs are
    clean carries the higher weighted (and ranked) rate. This is the run-count
    analog of "runs from this week vs a year ago": position in the outcome ring
    is the deterministic proxy for recency (profiles store no wall-clock)."""
    data: dict = {"models": {}}
    # A: 10 old failures, then 10 recent successes.
    for _ in range(10):
        _fold(data, "recent_clean", False)
    for _ in range(10):
        _fold(data, "recent_clean", True)
    # B: 10 old successes, then 10 recent failures — same raw 0.5.
    for _ in range(10):
        _fold(data, "recent_noisy", True)
    for _ in range(10):
        _fold(data, "recent_noisy", False)

    a = get_dev_signal(data, "recent_clean", "medium")
    b = get_dev_signal(data, "recent_noisy", "medium")
    assert a["raw"] == b["raw"] == 0.5
    assert a["weighted"] > b["weighted"]
    # The ranked value follows the weighted view, not the tied raw rate.
    assert a["rate"] > b["rate"]


def test_strong_history_survives_one_recent_escalation():
    """A single recent bad run must not catastrophically swing a model with a
    strong long-term history (recency composes with, not replaces, stability)."""
    data: dict = {"models": {}}
    for _ in range(40):
        _fold(data, "veteran", True)
    _fold(data, "veteran", False)  # one recent escalation

    sig = get_dev_signal(data, "veteran", "medium")
    assert sig["raw"] == round(40 / 41, 4)
    # Weighted stays high — the lone recent failure barely dents 40 clean runs.
    assert sig["weighted"] > 0.9


def _legacy_bucket_profile() -> dict:
    """A legacy/migrated bucket: cumulative counts, no ``_recent`` ring.

    Mirrors a profile written before #1392 (or produced by migrating cumulative
    history) — 40 admissible successes recorded only as accumulators, with no
    per-run outcome ring to weight.
    """
    return {
        "models": {
            "legacy": {
                "dev": {
                    "runs": 40,
                    "_successes": 40,
                    "success_rate": 1.0,
                    "by_complexity": {
                        "large": {"runs": 40, "_successes": 40, "success_rate": 1.0}
                    },
                }
            }
        }
    }


def test_legacy_bucket_not_swung_to_zero_by_single_new_run():
    """A legacy bucket that passes the lifetime floor on 40 cumulative successes
    must not collapse to weighted 0.0 when a single new failure is folded — the
    one new outcome would otherwise be the entire weighted ring (#1392 review)."""
    data = _legacy_bucket_profile()
    _fold(data, "legacy", False, complexity="large")  # first ring outcome = [0]

    sig = get_dev_signal(data, "legacy", "large")
    assert sig["raw"] == round(40 / 41, 4)  # 41 lifetime runs, 40 successes
    # Ring holds a single outcome (< min_runs) → weighted falls back to raw, so
    # the strong long-term history still drives routing.
    assert sig["weighted"] == sig["raw"]
    assert sig["rate"] == sig["raw"]


def test_legacy_bucket_weighted_takes_over_once_ring_reaches_floor():
    """The fallback is bounded: once the ring itself reaches min_runs outcomes,
    recency legitimately drives the weighted value (the floor governs the ring
    exactly as it governs the lifetime rate)."""
    data = _legacy_bucket_profile()
    for _ in range(3):  # ring grows to [0, 0, 0] == min_runs
        _fold(data, "legacy", False, complexity="large")

    sig = get_dev_signal(data, "legacy", "large", 3)
    assert sig["raw"] == round(40 / 43, 4)
    # Three recent failures now constitute a full-floor ring → recency drives.
    assert sig["weighted"] == 0.0
    assert sig["rate"] == 0.0


def test_below_min_runs_returns_none_regardless_of_weighting():
    """Cold-start behavior is unchanged: below the sample floor the signal is
    None for every weighting mode, so routing falls through to tier/budget."""
    data: dict = {"models": {}}
    _fold(data, "cold", True)
    _fold(data, "cold", True)  # 2 admissible runs < min_runs=3

    for mode in ("exponential", "window", "off"):
        sig = get_dev_signal(data, "cold", "medium", 3, recency=RecencyConfig(mode=mode))
        assert sig["rate"] is None
        assert sig["floor"] == "fail"
        # raw/weighted are still reported so the audit shows the cleared floor.
        assert sig["raw"] == 1.0
    assert get_dev_success_rate(data, "cold", "medium", 3) is None


def test_tainted_runs_excluded_from_weighted_aggregate_and_counted():
    """Tainted runs don't influence routing (clause 4): they never enter the weighted ring
    and are tallied under tainted_runs so the exclusion stays visible."""
    data: dict = {"models": {}}
    for _ in range(5):
        _fold(data, "trusted", True)
    for _ in range(3):
        _fold(data, "trusted", False, tainted=True)

    sig = get_dev_signal(data, "trusted", "medium")
    assert sig["runs"] == 5  # admissible sample count only
    assert sig["raw"] == 1.0
    assert sig["weighted"] == 1.0  # tainted failures never dragged the ring down
    assert sig["tainted_runs"] == 3


def test_off_mode_weighted_mirrors_raw():
    """mode=off is the operator kill-switch: weighting collapses to the lifetime
    cumulative rate."""
    data: dict = {"models": {}}
    for _ in range(10):
        _fold(data, "m", False)
    for _ in range(10):
        _fold(data, "m", True)

    sig = get_dev_signal(data, "m", "medium", recency=RecencyConfig(mode="off"))
    assert sig["weighted"] == sig["raw"] == 0.5


def test_weighting_params_are_reported_in_signal():
    """The signal records the parameters actually applied so an operator can
    reproduce the weighted value from raw history."""
    data: dict = {"models": {}}
    for _ in range(5):
        _fold(data, "m", True)
    sig = get_dev_signal(
        data,
        "m",
        "medium",
        recency=RecencyConfig(mode="exponential", half_life_runs=25, window=100),
    )
    assert sig["weighting"] == {"mode": "exponential", "half_life_runs": 25.0, "window": 100}


# ── Config validation (integrity boundary) ─────────────────────────────────


def test_recency_config_defaults():
    cfg = _parse_recency(None)
    assert cfg.mode == "exponential"
    assert cfg.half_life_runs == 50.0
    assert cfg.window == 200


def test_recency_config_rejects_unknown_mode():
    with pytest.raises(ValueError, match="recency.mode"):
        _parse_recency({"mode": "bogus"})


def test_recency_config_rejects_nonpositive_half_life():
    with pytest.raises(ValueError, match="half_life_runs"):
        _parse_recency({"mode": "exponential", "half_life_runs": 0})


def test_recency_config_rejects_nonpositive_window():
    with pytest.raises(ValueError, match="window"):
        _parse_recency({"window": 0})


def test_recency_config_parses_valid_overrides():
    cfg = _parse_recency({"mode": "Window", "half_life_runs": 10, "window": 50})
    assert cfg.mode == "window"  # normalized
    assert cfg.half_life_runs == 10.0
    assert cfg.window == 50
