"""Tests for the #2348 spike's substrate access, rendering and CLI (``report.py``).

Covers the half of the POC that touches the world: the read-only substrate
contract, the two SELECT-only read-model helpers it depends on, and the rendered
report an operator actually reads — including that an untrustworthy ranking says
so on its face rather than printing a bare table.

The ranking arithmetic these exercise is covered by
``test_structural_decay_observer_ranking.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structural_decay_test_helpers import coverage as _coverage
from structural_decay_test_helpers import seed_record
from structural_decay_test_helpers import touch_rows as _touch_rows

from theforge.coordinator import audit_storage
from theforge.coordinator.audit_read_model import (
    changed_file_coverage,
    changed_file_touch_rows,
)
from theforge.structural_decay_observer import (
    build_runs,
    compare_to_line_counts,
    load_report,
    rank_candidates,
    render,
    resolve_controls,
    threshold_status,
)


def _upsert(conn, record: dict) -> None:
    record.setdefault("schema_version", audit_storage.CURRENT_RECORD_SCHEMA_VERSION)
    audit_storage.upsert_run_record(conn, record, provenance="native")


class TestRendering:
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

    def test_every_rendered_entry_carries_its_evidence(self) -> None:
        rows: list[dict] = []
        for i in range(4):
            rows += _touch_rows(f"r{i}", ["hot.py", "pair.py"], cost=20.0)
        line_counts = {"hot.py": 800, "pair.py": 40}
        candidates = rank_candidates(build_runs(rows), line_counts=line_counts)
        coverage = _coverage(4, 4)
        text = render(
            {
                "coverage": coverage,
                "controls": resolve_controls(build_runs(rows)),
                "runs": 4,
                "candidates": candidates,
                "threshold": threshold_status(coverage, candidates),
                "line_count_comparison": compare_to_line_counts(candidates, line_counts),
                "top": 5,
            }
        )

        assert "touching run(s)" in text
        assert "attributed spend" in text
        assert "800 lines" in text
        assert "co-touched with" in text
        # One weakest-signal line per rendered candidate, no exceptions.
        assert text.count("weakest signal:") == len(candidates)


class TestSubstrateIntegration:
    """The POC must read a real substrate read-only and report on it."""

    def _seed(self, project_root: Path) -> None:
        (project_root / "src").mkdir(parents=True, exist_ok=True)
        (project_root / "src" / "hot.py").write_text("x = 1\n" * 120, encoding="utf-8")
        (project_root / "src" / "cold.py").write_text("y = 1\n" * 900, encoding="utf-8")
        conn = audit_storage.create_or_open(project_root)
        try:
            for i in range(6):
                _upsert(
                    conn,
                    seed_record(
                        f"{i:012d}",
                        ["src/hot.py"] if i % 2 == 0 else ["src/cold.py"],
                        cost=30.0 if i % 2 == 0 else 10.0,
                        started_at=f"2026-07-0{i + 1}T00:00:00Z",
                        reviews=[{"pool_models": ["a", "b"], "findings": []}],
                    ),
                )
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

    def test_panel_size_is_derived_from_the_decoded_record(self, tmp_path: Path) -> None:
        """Panel size is not an indexed column; it must come off raw_json."""
        self._seed(tmp_path)

        report = load_report(tmp_path, top=5)

        by_key = {c.key: c for c in report["controls"]}
        assert by_key["panel_size"].availability == "derived"

    def test_reading_the_substrate_does_not_mutate_it(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        path = audit_storage.substrate_path(tmp_path)
        before = path.read_bytes()

        load_report(tmp_path, top=5)

        assert path.read_bytes() == before

    def test_missing_substrate_is_an_error_not_a_silent_rebuild(self, tmp_path: Path) -> None:
        with pytest.raises(audit_storage.SubstrateMissingError):
            load_report(tmp_path)

        assert not audit_storage.substrate_path(tmp_path).exists()


class TestReadModelHelpers:
    def test_touch_rows_and_coverage_agree_on_the_joinable_set(self, tmp_path: Path) -> None:
        conn = audit_storage.create_or_open(tmp_path)
        try:
            for i, (cost, files) in enumerate(
                [(10.0, ["a.py"]), (20.0, ["a.py", "b.py"]), (None, ["c.py"])]
            ):
                _upsert(
                    conn,
                    seed_record(
                        f"{i:012d}",
                        files,
                        cost=cost,
                        started_at=f"2026-07-0{i + 1}T00:00:00Z",
                    ),
                )
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
        conn = audit_storage.create_or_open(tmp_path)
        try:
            for i, started in enumerate(["2026-06-01T00:00:00Z", "2026-08-01T00:00:00Z"]):
                _upsert(
                    conn,
                    seed_record(f"{i:012d}", [f"m{i}.py"], cost=5.0, started_at=started),
                )
            conn.commit()

            recent = changed_file_touch_rows(conn, since="2026-07-01T00:00:00Z")
        finally:
            conn.close()

        assert [row["path"] for row in recent] == ["m1.py"]

    def test_coverage_counts_measured_runs_that_do_not_join(self, tmp_path: Path) -> None:
        """The denominator is the point: a run with no file set still counts."""
        conn = audit_storage.create_or_open(tmp_path)
        try:
            _upsert(
                conn,
                seed_record("0" * 12, ["a.py"], cost=10.0, started_at="2026-07-01T00:00:00Z"),
            )
            unjoinable = seed_record("1" * 12, [], cost=90.0, started_at="2026-07-02T00:00:00Z")
            unjoinable["changed_files"] = None
            _upsert(conn, unjoinable)
            conn.commit()

            coverage = changed_file_coverage(conn)
        finally:
            conn.close()

        assert coverage["measured_runs"] == 2
        assert coverage["joinable_runs"] == 1
        assert coverage["run_coverage_ratio"] == 0.5
        assert coverage["measured_spend_usd"] == 100.0
        assert coverage["joinable_spend_usd"] == 10.0
        assert coverage["spend_coverage_ratio"] == 0.1


def test_package_is_not_imported_by_the_shipped_runtime() -> None:
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
