"""Tests for the #2348 structural-decay spike POC.

The POC's whole value is that its numbers are *qualified* — an excess figure
computed off three runs and one that survived a controlled comparison must not
render identically. So these tests pin the qualifications as hard as the
arithmetic:

- coverage and threshold reporting, including the case where the data is too
  thin to trust and the report has to say so
- comparable-cohort excess: a run costing more than its like-for-like peers
  produces positive excess, and a run with no peers contributes none
- ``weakest_signal`` selection, including a control that is *unavailable* being
  reported rather than silently treated as controlled
- the ship gate: a small hot file must be able to outrank a large cold one, or
  the ranking has rediscovered ``wc -l``
"""

from __future__ import annotations

from pathlib import Path

from theforge.coordinator import audit_storage
from theforge.structural_decay_observer import (
    MIN_COHORT_RUNS,
    MIN_TOUCHING_RUNS,
    build_runs,
    compare_to_line_counts,
    load_report,
    rank_candidates,
    render,
    resolve_controls,
    threshold_status,
)


def _touch_rows(
    run_id: str,
    paths: list[str],
    *,
    cost: float,
    complexity: int | None = 5,
    dev_model: str | None = "anthropic/opus",
    started_at: str = "2026-07-01T00:00:00Z",
    insertions: int = 10,
) -> list[dict]:
    return [
        {
            "run_id": run_id,
            "path": path,
            "insertions": insertions,
            "deletions": 1,
            "binary": False,
            "slug": f"issue-{run_id}",
            "issue_id": None,
            "started_at": started_at,
            "total_cost_usd": cost,
            "complexity_score": complexity,
            "outcome_success": 1,
            "verdict": "APPROVE",
            "dev_model": dev_model,
            "dev_resolved_model": dev_model,
            "milestone": "v0.15.0",
        }
        for path in paths
    ]


def _coverage(joinable: int, measured: int, *, spend: float = 100.0) -> dict:
    return {
        "measured_runs": measured,
        "measured_spend_usd": spend,
        "joinable_runs": joinable,
        "joinable_spend_usd": spend * (joinable / measured if measured else 0),
        "run_coverage_ratio": joinable / measured if measured else 0.0,
        "spend_coverage_ratio": joinable / measured if measured else 0.0,
        "first_joinable_at": "2026-07-01T00:00:00Z",
        "last_joinable_at": "2026-08-01T00:00:00Z",
    }


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

    def test_untrustworthy_ranking_is_labelled_in_the_rendered_report(self) -> None:
        rows = _touch_rows("a", ["one.py"], cost=10.0)
        candidates = rank_candidates(build_runs(rows), line_counts={"one.py": 500})
        coverage = _coverage(31, 160)
        report = {
            "coverage": coverage,
            "controls": resolve_controls(build_runs(rows)),
            "runs": 1,
            "candidates": candidates,
            "threshold": threshold_status(coverage, candidates),
            "line_count_comparison": compare_to_line_counts(candidates, {"one.py": 500}),
            "top": 5,
        }

        text = render(report)

        assert "TRUST THRESHOLD: NOT MET" in text
        assert "NOT trustworthy at this sample size" in text
        assert "weakest signal:" in text
        assert "COMPARISON AGAINST PURE LINE-COUNT RANKING" in text


class TestSubstrateIntegration:
    """The POC must read a real substrate read-only and report on it."""

    def _seed(self, project_root: Path) -> None:
        (project_root / "src").mkdir(parents=True, exist_ok=True)
        (project_root / "src" / "hot.py").write_text("x = 1\n" * 120, encoding="utf-8")
        (project_root / "src" / "cold.py").write_text("y = 1\n" * 900, encoding="utf-8")
        conn = audit_storage.create_or_open(project_root)
        try:
            for i in range(6):
                paths = ["src/hot.py"] if i % 2 == 0 else ["src/cold.py"]
                record = {
                    "run_id": f"{i:012d}",
                    "schema_version": audit_storage.CURRENT_RECORD_SCHEMA_VERSION,
                    "task": {"slug": f"issue-{i}"},
                    "timing": {"started_at": f"2026-07-0{i + 1}T00:00:00Z"},
                    "preflight": {"complexity": "medium", "complexity_score": 5},
                    "cost": {"total_usd": 30.0 if i % 2 == 0 else 10.0},
                    "outcome": {"success": True},
                    "reviews": [{"pool_models": ["a", "b"], "findings": []}],
                    "changed_files": {
                        "base_ref": "a" * 40,
                        "head_ref": "b" * 40,
                        "files": [
                            {"path": p, "insertions": 5, "deletions": 1, "binary": False}
                            for p in paths
                        ],
                    },
                }
                audit_storage.upsert_run_record(conn, record, provenance="native")
            conn.commit()
        finally:
            conn.close()

    def test_report_reads_real_substrate_and_reports_coverage(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        (tmp_path / "forge.yaml").write_text("", encoding="utf-8")

        report = load_report(tmp_path, top=5)

        assert report["coverage"]["measured_runs"] == 6
        assert report["coverage"]["joinable_runs"] == 6
        assert report["coverage"]["run_coverage_ratio"] == 1.0
        # Threshold still not met - six runs is far under the sample floors,
        # which is exactly the report the POC must be able to produce.
        assert report["threshold"]["met"] is False
        assert render(report)

    def test_reading_the_substrate_does_not_mutate_it(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        path = audit_storage.substrate_path(tmp_path)
        before = path.read_bytes()

        load_report(tmp_path, top=5)

        assert path.read_bytes() == before

    def test_missing_substrate_is_an_error_not_a_silent_rebuild(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(audit_storage.SubstrateMissingError):
            load_report(tmp_path)

        assert not audit_storage.substrate_path(tmp_path).exists()


class TestReadModelHelpers:
    def test_touch_rows_and_coverage_agree_on_the_joinable_set(self, tmp_path: Path) -> None:
        from theforge.coordinator.audit_read_model import (
            changed_file_coverage,
            changed_file_touch_rows,
        )

        conn = audit_storage.create_or_open(tmp_path)
        try:
            for i, (cost, files) in enumerate(
                [(10.0, ["a.py"]), (20.0, ["a.py", "b.py"]), (None, ["c.py"])]
            ):
                record = {
                    "run_id": f"{i:012d}",
                    "schema_version": audit_storage.CURRENT_RECORD_SCHEMA_VERSION,
                    "task": {"slug": f"issue-{i}"},
                    "timing": {"started_at": f"2026-07-0{i + 1}T00:00:00Z"},
                    "cost": {"total_usd": cost},
                    "outcome": {"success": True},
                    "changed_files": {
                        "base_ref": "a" * 40,
                        "head_ref": "b" * 40,
                        "files": [
                            {"path": p, "insertions": 1, "deletions": 0, "binary": False}
                            for p in files
                        ],
                    },
                }
                audit_storage.upsert_run_record(conn, record, provenance="native")
            conn.commit()

            rows = changed_file_touch_rows(conn)
            coverage = changed_file_coverage(conn)
        finally:
            conn.close()

        # The cost-unknown run is excluded: it is a lower bound, not a measurement.
        assert {row["path"] for row in rows} == {"a.py", "b.py"}
        assert coverage["measured_runs"] == 2
        assert coverage["joinable_runs"] == 2
        assert coverage["measured_spend_usd"] == 30.0

    def test_since_filter_bounds_the_window(self, tmp_path: Path) -> None:
        from theforge.coordinator.audit_read_model import changed_file_touch_rows

        conn = audit_storage.create_or_open(tmp_path)
        try:
            for i, started in enumerate(["2026-06-01T00:00:00Z", "2026-08-01T00:00:00Z"]):
                audit_storage.upsert_run_record(
                    conn,
                    {
                        "run_id": f"{i:012d}",
                        "schema_version": audit_storage.CURRENT_RECORD_SCHEMA_VERSION,
                        "task": {"slug": f"issue-{i}"},
                        "timing": {"started_at": started},
                        "cost": {"total_usd": 5.0},
                        "outcome": {"success": True},
                        "changed_files": {
                            "base_ref": "a" * 40,
                            "head_ref": "b" * 40,
                            "files": [
                                {
                                    "path": f"m{i}.py",
                                    "insertions": 1,
                                    "deletions": 0,
                                    "binary": False,
                                }
                            ],
                        },
                    },
                    provenance="native",
                )
            conn.commit()

            recent = changed_file_touch_rows(conn, since="2026-07-01T00:00:00Z")
        finally:
            conn.close()

        assert [row["path"] for row in recent] == ["m1.py"]


def test_module_is_not_imported_by_the_coordinator() -> None:
    """The spike is inert: nothing in the shipped runtime reaches it.

    If this ever fails, the POC has become a component and needs the design
    review the spike record says it has not had.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "theforge"
    package = src / "structural_decay_observer"
    importers = [
        path
        for path in src.rglob("*.py")
        if package not in path.parents
        and "structural_decay_observer" in path.read_text(encoding="utf-8")
    ]

    assert importers == []
