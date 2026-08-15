"""The ``model_profiles`` facade — accumulate through it, then read through it.

After the #2467 split the implementation lives in ``model_profiles_storage``
(accumulation), ``model_profiles_read_model`` (the signals routing consults) and
``model_profiles_identity`` (what both share). ``model_profiles`` remains the
compatibility surface older callers import, and these tests are what keeps it
honest: each one applies a run outcome and then reads a signal back, using only
names re-exported by the facade, so a re-export that stopped resolving or
drifted to a stale binding fails here.

They are also the seam coverage the split had to preserve — ``RunOutcome`` →
``apply_run`` → ``get_*`` over the same stored profile data. Accumulation
behaviour on its own lives in ``test_model_profiles_storage.py``; signals read
from directly-constructed state live in ``test_model_profiles_read_model.py``;
the ownership boundary itself is pinned by ``test_model_profiles_boundary.py``.
"""

from __future__ import annotations

from theforge.model_profiles import (
    COMPLEXITY_BANDS,
    RunOutcome,
    apply_run,
    get_dev_complexity_stats,
    get_dev_score_cost_stats,
)


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


def _score_run(data: dict, *, model: str, score: int, band: str, cost: float | None) -> None:
    apply_run(
        data,
        RunOutcome(
            complexity=band,
            complexity_score=score,
            dev_model=model,
            dev_success=True,
            dev_iterations=1,
            dev_cost_usd=cost,
        ),
    )


def test_get_dev_score_cost_stats_spans_models_and_keeps_the_score():
    """Score-scoped cost: one complexity score, every model (#2284).

    The per-story allocation is drawn this way, so a dev estimate that will be
    subtracted from it has to be drawn the same way — unlike
    ``get_dev_complexity_stats``, which widens to a band and narrows to one
    model's identity.
    """
    data: dict = {"models": {}}
    _score_run(data, model="sonnet", score=4, band="medium", cost=2.0)
    _score_run(data, model="sonnet", score=4, band="medium", cost=3.0)
    _score_run(data, model="opus", score=4, band="medium", cost=4.0)
    # Same band, different score — must not be folded in.
    _score_run(data, model="sonnet", score=6, band="medium", cost=40.0)

    stats = get_dev_score_cost_stats(data, 4, min_runs=3)

    assert stats is not None
    assert stats["runs"] == 3.0
    assert stats["avg_cost_usd"] == 3.0
    # The band-and-model reader over the same profiles sees a costlier set.
    band_stats = get_dev_complexity_stats(data, "sonnet", "medium", min_runs=1)
    assert band_stats is not None
    assert band_stats["avg_cost_usd"] == 15.0


def test_get_dev_score_cost_stats_averages_over_measured_runs_only():
    data: dict = {"models": {}}
    _score_run(data, model="sonnet", score=4, band="medium", cost=2.0)
    _score_run(data, model="sonnet", score=4, band="medium", cost=None)
    _score_run(data, model="sonnet", score=4, band="medium", cost=None)

    stats = get_dev_score_cost_stats(data, 4, min_runs=1)

    assert stats is not None
    assert stats["runs"] == 3.0
    assert stats["measured_runs"] == 1.0
    # $2.00 over the one measured run, not $2.00/3.
    assert stats["avg_cost_usd"] == 2.0


def test_get_dev_score_cost_stats_returns_none_without_enough_history():
    data: dict = {"models": {}}
    _score_run(data, model="sonnet", score=4, band="medium", cost=2.0)

    assert get_dev_score_cost_stats(data, 4, min_runs=3) is None
    assert get_dev_score_cost_stats(data, 9, min_runs=1) is None
    assert get_dev_score_cost_stats(data, None, min_runs=1) is None
    assert get_dev_score_cost_stats({}, 4, min_runs=1) is None


def test_get_dev_score_cost_stats_returns_none_when_no_run_had_a_measured_cost():
    """An unmeasured population cannot be averaged into a dollar figure."""
    data: dict = {"models": {}}
    for _ in range(3):
        _score_run(data, model="sonnet", score=4, band="medium", cost=None)

    assert get_dev_score_cost_stats(data, 4, min_runs=1) is None


def test_complexity_bands_constant():
    assert COMPLEXITY_BANDS == ("small", "medium", "large")


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
