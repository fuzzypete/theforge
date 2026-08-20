"""Tests for the #1848 finding-fate spike POC."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from structural_decay_test_helpers import seed_record

from theforge.coordinator import audit_storage
from theforge.finding_fate_proxy import analyze_records, derive_finding_fate, load_report, render


def _finding(
    reporter: str | None,
    disposition: str,
    *,
    first_seen: int = 1,
    last_seen: int = 1,
    severity: str = "P1",
) -> dict:
    return {
        "finding_id": f"{reporter or 'none'}-{disposition}-{first_seen}-{last_seen}",
        "cycle_first_seen": first_seen,
        "cycle_last_seen": last_seen,
        "file": "src/foo.py",
        "line": 10,
        "severity": severity,
        "description": f"{disposition} finding",
        "reporter": reporter,
        "disposition": disposition,
    }


def _record(
    run_id: str,
    *,
    started_at: str,
    trust_status: str = "unchecked",
    finding_registry: list[dict] | None = None,
    plan_finding_registry: list[dict] | None = None,
) -> dict:
    return {
        "run_id": run_id,
        "started_at": started_at,
        "trust_status": trust_status,
        "finding_registry": finding_registry or [],
        "plan_finding_registry": plan_finding_registry or [],
    }


class TestDerivation:
    def test_fixed_is_addressed(self) -> None:
        assert derive_finding_fate(_finding("rev-a", "fixed")) == "addressed"

    def test_multi_cycle_blocking_is_survived(self) -> None:
        assert (
            derive_finding_fate(_finding("rev-a", "unresolved", first_seen=1, last_seen=2))
            == "survived"
        )

    def test_same_cycle_blocking_end_state_has_no_downstream_fate(self) -> None:
        assert (
            derive_finding_fate(_finding("rev-a", "ac_blocking", first_seen=2, last_seen=2))
            is None
        )

    def test_gate_contradicted_and_downgraded_do_not_become_contradicted(self) -> None:
        assert (
            derive_finding_fate(_finding("rev-a", "gate_contradicted", first_seen=1, last_seen=2))
            is None
        )
        assert (
            derive_finding_fate(_finding("rev-a", "downgraded", first_seen=1, last_seen=2)) is None
        )


class TestAnalysis:
    def test_aggregates_addressed_survived_and_excluded_counts(self) -> None:
        report = analyze_records(
            [
                _record(
                    "run-1",
                    started_at="2026-08-01T00:00:00Z",
                    finding_registry=[
                        _finding("rev-a", "fixed"),
                        _finding("rev-a", "unresolved", first_seen=1, last_seen=2),
                        _finding("rev-a", "net_new"),
                        _finding(None, "fixed"),
                        _finding("rev-b", "gate_contradicted", first_seen=1, last_seen=2),
                    ],
                    plan_finding_registry=[
                        {
                            "description": "plan-only",
                            "severity": "P1",
                            "cycle_first_seen": 1,
                            "cycle_last_seen": 1,
                            "disposition": "new",
                        }
                    ],
                )
            ],
            min_runs=1,
        )

        corpus = report["corpus"]
        assert corpus["records_with_plan_review_findings"] == 1
        assert corpus["excluded_missing_attribution"] == 1
        assert corpus["excluded_missing_fate"] == 2
        assert corpus["gate_contradicted_markers"] == 1

        by_reviewer = {row["reviewer"]: row for row in report["reviewers"]}
        rev_a = by_reviewer["rev-a"]
        assert rev_a["findings"] == 3
        assert rev_a["derivable_findings"] == 2
        assert rev_a["excluded_missing_fate"] == 1
        assert rev_a["addressed_rate"]["rate"] == 0.5
        assert rev_a["survived_rate"]["rate"] == 0.5

        rev_b = by_reviewer["rev-b"]
        assert rev_b["findings"] == 1
        assert rev_b["derivable_findings"] == 0
        assert rev_b["runs"] == 0
        assert rev_b["gate_contradicted_markers"] == 1

    def test_tainted_runs_are_excluded_and_counted_per_reviewer(self) -> None:
        report = analyze_records(
            [
                _record(
                    "run-tainted",
                    started_at="2026-08-01T00:00:00Z",
                    trust_status="tainted",
                    finding_registry=[_finding("rev-a", "fixed")],
                ),
                _record(
                    "run-good",
                    started_at="2026-08-02T00:00:00Z",
                    trust_status="trusted",
                    finding_registry=[_finding("rev-a", "fixed")],
                ),
            ],
            min_runs=1,
        )

        rev_a = report["reviewers"][0]
        assert report["corpus"]["tainted_records_excluded"] == 1
        assert rev_a["tainted_runs_excluded"] == 1
        assert rev_a["tainted_findings_excluded"] == 1
        assert rev_a["findings"] == 1
        assert rev_a["runs"] == 1

    def test_sample_floor_gates_the_rate_even_when_raw_data_exists(self) -> None:
        report = analyze_records(
            [
                _record(
                    f"run-{i}",
                    started_at=f"2026-08-0{i + 1}T00:00:00Z",
                    finding_registry=[_finding("rev-a", "fixed")],
                )
                for i in range(4)
            ],
            min_runs=5,
        )

        rev_a = report["reviewers"][0]
        assert rev_a["runs"] == 4
        assert rev_a["floor"] == "fail"
        assert rev_a["addressed_rate"]["raw"] == 1.0
        assert rev_a["addressed_rate"]["rate"] is None

    def test_recency_weighting_uses_one_observation_per_run(self) -> None:
        recency = SimpleNamespace(mode="exponential", half_life_runs=1.0, window=10)
        report = analyze_records(
            [
                _record(
                    "run-1",
                    started_at="2026-08-01T00:00:00Z",
                    finding_registry=[
                        _finding("rev-a", "fixed"),
                        _finding("rev-a", "unresolved", first_seen=1, last_seen=2),
                    ],
                ),
                _record(
                    "run-2",
                    started_at="2026-08-02T00:00:00Z",
                    finding_registry=[_finding("rev-a", "fixed"), _finding("rev-a", "fixed")],
                ),
            ],
            min_runs=1,
            recency=recency,
        )

        rev_a = report["reviewers"][0]
        assert rev_a["runs"] == 2
        assert rev_a["addressed_rate"]["raw"] == 0.75
        # Run shares are [0.5, 1.0]; the newer all-addressed run carries more weight.
        assert rev_a["addressed_rate"]["rate"] == 0.8333

    def test_dismissed_is_not_approximated_from_net_new_diff_ungrounded_or_done(self) -> None:
        report = analyze_records(
            [
                {
                    **_record(
                        "run-1",
                        started_at="2026-08-01T00:00:00Z",
                        finding_registry=[
                            _finding("rev-a", "net_new"),
                            _finding("rev-a", "diff_ungrounded", first_seen=1, last_seen=2),
                        ],
                    ),
                    "verdict": "DONE",
                    "outcome_success": True,
                }
            ],
            min_runs=1,
        )

        rev_a = report["reviewers"][0]
        assert rev_a["derivable_findings"] == 0
        assert rev_a["excluded_missing_fate"] == 2
        assert report["fate_determination"]["dismissed"]["status"] == "underivable"
        assert report["fate_determination"]["contradicted"]["status"] == "underivable"


class TestReportIntegration:
    def _upsert(self, conn, record: dict) -> None:
        record.setdefault("schema_version", audit_storage.CURRENT_RECORD_SCHEMA_VERSION)
        audit_storage.upsert_run_record(conn, record, provenance="native")

    def test_load_report_reads_real_substrate_and_render_labels_underivable(
        self, tmp_path: Path
    ) -> None:
        conn = audit_storage.create_or_open(tmp_path)
        try:
            record = seed_record(
                "000000000001",
                ["src/foo.py"],
                cost=12.0,
                started_at="2026-08-01T00:00:00Z",
                reviews=[{"pool_models": ["a", "b"], "findings": []}],
            )
            record["finding_registry"] = [
                _finding("rev-a", "fixed"),
                _finding("rev-a", "unresolved", first_seen=1, last_seen=2),
                _finding("rev-b", "gate_contradicted", first_seen=1, last_seen=2),
            ]
            record["trust_status"] = "trusted"
            record["plan_finding_registry"] = [
                {
                    "description": "plan-only",
                    "severity": "P1",
                    "cycle_first_seen": 1,
                    "cycle_last_seen": 1,
                    "disposition": "new",
                }
            ]
            self._upsert(conn, record)
            conn.commit()
        finally:
            conn.close()

        report = load_report(tmp_path)
        text = render(report)

        assert report["corpus"]["records_with_code_review_findings"] == 1
        assert report["corpus"]["records_with_plan_review_findings"] == 1
        assert "dismissed: UNDERIVABLE" in text
        assert "contradicted: UNDERIVABLE" in text
        assert "rev-a" in text
