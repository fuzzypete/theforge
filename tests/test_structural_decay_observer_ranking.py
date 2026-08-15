"""Tests for the #2348 spike's pure ranking math (``ranking.py``).

The POC's whole value is that its numbers are *qualified* — an excess figure
computed off three runs and one that survived a controlled comparison must not
read identically. So these tests pin the qualifications as hard as the
arithmetic:

- attribution: a run's cost is split across the files it changed, never charged
  whole to each
- comparable-cohort excess: a run costing more than its like-for-like peers
  produces positive excess, and a run with no peers contributes none
- ``weakest_signal`` selection, including a control that is *unavailable* being
  reported rather than silently treated as controlled
- the ship gate: a small hot file must be able to outrank a large cold one, or
  the ranking has rediscovered ``wc -l``

Rendering and substrate access are covered by
``test_structural_decay_observer_report.py``.
"""

from __future__ import annotations

from structural_decay_test_helpers import coverage as _coverage
from structural_decay_test_helpers import touch_rows as _touch_rows

from theforge.structural_decay_observer import (
    MIN_COHORT_RUNS,
    MIN_TOUCHING_RUNS,
    build_runs,
    compare_to_line_counts,
    rank_candidates,
    resolve_controls,
    threshold_status,
)


class TestRunProjection:
    def test_attributed_cost_splits_run_spend_across_touched_files(self) -> None:
        runs = build_runs(_touch_rows("r1", ["a.py", "b.py", "c.py", "d.py"], cost=40.0))

        assert len(runs) == 1
        assert runs[0].files_changed == 4
        # The "$40 story touching ten files does not attribute $40 to each" rule.
        assert runs[0].attributed_cost == 10.0

    def test_insertions_accumulate_across_the_runs_files(self) -> None:
        runs = build_runs(_touch_rows("r1", ["a.py", "b.py"], cost=10.0, insertions=7))

        assert runs[0].insertions == 14
        assert runs[0].deletions == 2

    def test_extras_supply_controls_that_are_not_indexed_columns(self) -> None:
        runs = build_runs(
            _touch_rows("r1", ["a.py"], cost=10.0),
            record_extras={
                "r1": {"panel_size": 3, "review_cycles": 2, "finding_files": ("a.py",)}
            },
        )

        assert runs[0].panel_size == 3
        assert runs[0].review_cycles == 2
        assert runs[0].finding_files == ("a.py",)


class TestControlAvailability:
    def test_each_control_reports_where_its_value_came_from(self) -> None:
        runs = build_runs(
            _touch_rows("r1", ["a.py"], cost=10.0),
            record_extras={"r1": {"panel_size": 3, "review_cycles": 1, "finding_files": ()}},
        )

        by_key = {c.key: c for c in resolve_controls(runs)}

        assert by_key["complexity"].availability == "indexed"
        assert by_key["dev_model"].availability == "indexed"
        assert by_key["panel_size"].availability == "derived"
        assert by_key["intended_size"].availability == "derived"

    def test_a_control_no_run_carries_is_reported_unavailable(self) -> None:
        # No extras at all: panel size cannot be recovered for this dataset.
        runs = build_runs(_touch_rows("r1", ["a.py"], cost=10.0, complexity=None))

        by_key = {c.key: c for c in resolve_controls(runs)}

        assert by_key["panel_size"].availability == "unavailable"
        assert by_key["panel_size"].usable is False
        assert by_key["complexity"].availability == "unavailable"

    def test_unavailable_control_is_named_in_the_weakest_signal(self) -> None:
        """A missing control must be reported, never treated as controlled."""
        rows: list[dict] = []
        # Enough touching runs and comparables that no earlier caveat fires,
        # so the unavailable-control branch is what surfaces.
        for i in range(MIN_TOUCHING_RUNS):
            rows += _touch_rows(f"hot{i}", ["hot.py", "x.py"], cost=20.0)
        for i in range(MIN_COHORT_RUNS + 2):
            rows += _touch_rows(f"cold{i}", ["cold.py", "y.py"], cost=10.0)
        # No record_extras -> panel size unavailable.
        candidates = rank_candidates(build_runs(rows))

        hot = next(c for c in candidates if c.path == "hot.py")
        assert hot.controlled_comparisons == hot.touching_runs
        assert "reviewer panel size" in hot.weakest_signal
        assert "uncontrolled for" in hot.weakest_signal


class TestControlledExcess:
    def test_costlier_than_comparable_cohort_yields_positive_excess(self) -> None:
        rows: list[dict] = []
        for i in range(MIN_TOUCHING_RUNS):
            rows += _touch_rows(f"hot{i}", ["hot.py", "pair.py"], cost=40.0)
        for i in range(MIN_COHORT_RUNS + 1):
            rows += _touch_rows(f"base{i}", ["other.py", "pair2.py"], cost=20.0)

        candidates = rank_candidates(build_runs(rows))
        hot = next(c for c in candidates if c.path == "hot.py")

        # Attributed $20/run against a $10/run cohort median => +$10 each.
        assert hot.controlled_comparisons == MIN_TOUCHING_RUNS
        assert hot.excess_usd == 100.0

    def test_run_with_no_comparable_cohort_contributes_no_excess(self) -> None:
        # Two runs total: nothing can reach a MIN_COHORT_RUNS cohort at any
        # control level, so excess must be 0.0 rather than a fabricated number.
        rows = _touch_rows("a", ["one.py"], cost=50.0) + _touch_rows("b", ["two.py"], cost=1.0)

        candidates = rank_candidates(build_runs(rows))
        one = next(c for c in candidates if c.path == "one.py")

        assert one.controlled_comparisons == 0
        assert one.excess_usd == 0.0
        assert one.joinable_spend_usd == 50.0

    def test_controls_relax_until_a_cohort_is_reachable_and_say_which_survived(self) -> None:
        rows: list[dict] = []
        for i in range(MIN_TOUCHING_RUNS):
            # Distinctive intended-size band (many files) so the strict cohort is empty.
            rows += _touch_rows(
                f"hot{i}", ["hot.py"] + [f"f{i}_{j}.py" for j in range(11)], cost=60.0
            )
        for i in range(MIN_COHORT_RUNS + 1):
            rows += _touch_rows(f"base{i}", ["other.py", "pair.py"], cost=20.0)

        candidates = rank_candidates(build_runs(rows))
        hot = next(c for c in candidates if c.path == "hot.py")
        comparison = hot.comparisons[0]

        assert comparison.expected_cost is not None
        # intended_size was dropped to reach a cohort; complexity/dev_model held.
        assert "intended_size" not in comparison.controls_applied
        assert "complexity" in comparison.controls_applied


class TestWeakestSignal:
    def test_below_sample_floor_is_the_first_thing_reported(self) -> None:
        rows = _touch_rows("a", ["thin.py"], cost=10.0)
        for i in range(MIN_COHORT_RUNS + 1):
            rows += _touch_rows(f"b{i}", ["other.py"], cost=5.0)

        candidates = rank_candidates(build_runs(rows))
        thin = next(c for c in candidates if c.path == "thin.py")

        assert str(MIN_TOUCHING_RUNS) in thin.weakest_signal
        assert "directional" in thin.weakest_signal

    def test_no_cohort_at_any_level_says_the_figure_is_not_excess(self) -> None:
        rows = _touch_rows("a", ["one.py"], cost=50.0) + _touch_rows("b", ["two.py"], cost=1.0)

        candidates = rank_candidates(build_runs(rows))
        one = next(c for c in candidates if c.path == "one.py")

        # Below the touching floor too, so the floor message wins - the point is
        # that *something* disqualifying is always named.
        assert one.weakest_signal
        assert "measured" in one.weakest_signal or "not excess" in one.weakest_signal

    def test_every_candidate_carries_a_weakest_signal(self) -> None:
        rows: list[dict] = []
        for i in range(MIN_TOUCHING_RUNS + 3):
            rows += _touch_rows(f"r{i}", [f"m{i % 4}.py", "shared.py"], cost=10.0 + i)

        for candidate in rank_candidates(build_runs(rows)):
            assert candidate.weakest_signal.strip()


class TestLineCountComparison:
    def test_small_hot_file_can_outrank_large_cold_file(self) -> None:
        """The ship gate: if this cannot happen, the ranking is ``wc -l``."""
        rows: list[dict] = []
        for i in range(MIN_TOUCHING_RUNS):
            rows += _touch_rows(f"hot{i}", ["small_hot.py", "pair.py"], cost=40.0)
        # The big module is touched once, cheaply.
        rows += _touch_rows("cold", ["big_cold.py", "pair.py"], cost=4.0)
        for i in range(MIN_COHORT_RUNS + 1):
            rows += _touch_rows(f"base{i}", ["other.py", "pair2.py"], cost=20.0)

        line_counts = {
            "small_hot.py": 1200,
            "big_cold.py": 3200,
            "other.py": 100,
            "pair.py": 50,
            "pair2.py": 50,
        }
        candidates = rank_candidates(build_runs(rows), line_counts=line_counts)
        order = [c.path for c in candidates]

        assert order.index("small_hot.py") < order.index("big_cold.py")

        comparison = compare_to_line_counts(candidates, line_counts, top=2)
        assert comparison["top_line_count"][0] == "big_cold.py"
        assert comparison["top_excess"][0] == "small_hot.py"
        assert comparison["spearman"] is not None

    def test_ranking_identical_to_line_count_reports_perfect_correlation(self) -> None:
        """A kill signal must be visible as one, not buried."""
        rows: list[dict] = []
        for i in range(MIN_TOUCHING_RUNS):
            rows += _touch_rows(f"big{i}", ["big.py", "pair.py"], cost=40.0)
        for i in range(MIN_COHORT_RUNS + 1):
            rows += _touch_rows(f"base{i}", ["small.py", "pair2.py"], cost=20.0)

        line_counts = {"big.py": 3000, "small.py": 100, "pair.py": 90, "pair2.py": 80}
        candidates = rank_candidates(build_runs(rows), line_counts=line_counts)
        comparison = compare_to_line_counts(candidates, line_counts, top=1)

        assert comparison["top_excess"] == ["big.py"] == comparison["top_line_count"]
        assert comparison["overlap_ratio"] == 1.0


class TestThresholdReporting:
    def test_threshold_not_met_when_coverage_is_thin(self) -> None:
        rows = _touch_rows("a", ["one.py"], cost=10.0)
        candidates = rank_candidates(build_runs(rows))

        status = threshold_status(_coverage(31, 160), candidates)

        assert status["met"] is False
        failed = {c["name"] for c in status["checks"] if not c["met"]}
        assert "run coverage" in failed
        assert "ranked candidate floor" in failed

    def test_threshold_met_when_every_floor_clears(self) -> None:
        rows: list[dict] = []
        for i in range(MIN_TOUCHING_RUNS):
            rows += _touch_rows(f"hot{i}", ["hot.py", "pair.py"], cost=40.0)
        for i in range(MIN_COHORT_RUNS + 1):
            rows += _touch_rows(f"base{i}", ["other.py", "pair2.py"], cost=20.0)
        candidates = rank_candidates(build_runs(rows))

        status = threshold_status(_coverage(40, 45), candidates)

        assert status["met"] is True
