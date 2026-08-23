"""Tests for the backlog-report contract ``forge triage`` consumes.

The report slice of epic #1033 produces this artifact; this module is the only
place its shape is interpreted. What matters here is that a malformed or absent
report is an operator-legible refusal rather than a silently empty backlog —
proposing nothing and proposing against nothing must not look the same.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from theforge.triage_report import (
    BacklogReportError,
    load_backlog_report,
    parse_backlog_report,
)

_REPORT = {
    "current_milestone": "v0.12.0",
    "named_milestones": ["v0.13.0", "v0.14.0"],
    "findings": [
        {
            "finding_id": "1312:audit-count",
            "issue_ref": "#1312",
            "body": "audit count is off by one",
            "evidence": [
                {
                    "id": "symbol-absent",
                    "kind": "staleness",
                    "summary": "cited symbol absent from current tree",
                    "checkable": True,
                    "detail": "rg at HEAD returns no match",
                }
            ],
        }
    ],
}


class TestParsing:
    def test_full_report_round_trips(self) -> None:
        report = parse_backlog_report(_REPORT, source_path="r.json")
        assert report.current_milestone == "v0.12.0"
        assert report.named_milestones == ("v0.13.0", "v0.14.0")
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert finding.finding_id == "1312:audit-count"
        assert finding.issue_ref == "#1312"
        assert finding.evidence[0].evidence_id == "symbol-absent"
        assert finding.evidence[0].checkable is True

    def test_finding_id_falls_back_to_issue_ref(self) -> None:
        report = parse_backlog_report({"findings": [{"issue_ref": "#659", "body": "b"}]})
        assert report.findings[0].finding_id == "#659"

    def test_evidence_without_a_checkable_flag_is_not_checkable(self) -> None:
        report = parse_backlog_report(
            {"findings": [{"issue_ref": "#659", "evidence": [{"id": "e1", "summary": "s"}]}]}
        )
        assert report.findings[0].evidence[0].checkable is False

    def test_empty_findings_list_is_a_valid_empty_backlog(self) -> None:
        report = parse_backlog_report({"findings": []})
        assert report.findings == ()

    def test_missing_findings_key_is_an_empty_backlog(self) -> None:
        assert parse_backlog_report({"current_milestone": "v1"}).findings == ()

    def test_no_current_milestone_is_allowed(self) -> None:
        assert parse_backlog_report({"findings": []}).current_milestone is None

    def test_generated_report_metadata_is_ignored_by_the_consumer(self) -> None:
        report = parse_backlog_report(
            {
                **_REPORT,
                "generated_at": "2026-08-23T00:00:00Z",
                "summary": {"total_open": 1, "by_state": {"Hygiene": 1}},
            }
        )
        assert report.findings[0].issue_ref == "#1312"

    def test_optional_finding_snapshot_fields_are_retained(self) -> None:
        report = parse_backlog_report(
            {
                "findings": [
                    {
                        **_REPORT["findings"][0],
                        "issue_number": 1312,
                        "title": "audit count is off by one",
                        "labels": ["bug", "forge-finding"],
                        "pool_state": "Hygiene",
                        "verification_status": "stale_evidence",
                    }
                ]
            }
        )
        finding = report.findings[0]
        assert finding.issue_number == 1312
        assert finding.title == "audit count is off by one"
        assert finding.labels == ("bug", "forge-finding")
        assert finding.pool_state == "Hygiene"
        assert finding.verification_status == "stale_evidence"


class TestRejections:
    def test_non_mapping_report_is_rejected(self) -> None:
        with pytest.raises(BacklogReportError, match="must be a mapping"):
            parse_backlog_report([1, 2, 3])

    def test_findings_must_be_a_list(self) -> None:
        with pytest.raises(BacklogReportError, match="'findings' must be a list"):
            parse_backlog_report({"findings": {"a": 1}})

    def test_finding_without_identity_is_rejected(self) -> None:
        with pytest.raises(BacklogReportError, match="stable identity"):
            parse_backlog_report({"findings": [{"body": "orphan"}]})

    def test_evidence_entry_without_an_id_is_rejected(self) -> None:
        with pytest.raises(BacklogReportError, match="no 'id'"):
            parse_backlog_report(
                {"findings": [{"issue_ref": "#1", "evidence": [{"summary": "s"}]}]}
            )

    def test_duplicate_evidence_ids_are_rejected(self) -> None:
        with pytest.raises(BacklogReportError, match="duplicate evidence id"):
            parse_backlog_report(
                {"findings": [{"issue_ref": "#1", "evidence": [{"id": "e"}, {"id": "e"}]}]}
            )

    def test_duplicate_finding_ids_are_rejected(self) -> None:
        with pytest.raises(BacklogReportError, match="duplicate finding_id"):
            parse_backlog_report({"findings": [{"issue_ref": "#1"}, {"issue_ref": "#1"}]})


class TestLoading:
    def test_json_artifact_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "report.json"
        path.write_text(json.dumps(_REPORT), encoding="utf-8")
        report = load_backlog_report(path)
        assert report.findings[0].issue_ref == "#1312"
        assert report.source_path == str(path)

    def test_yaml_artifact_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "report.yaml"
        path.write_text(yaml.safe_dump(_REPORT), encoding="utf-8")
        assert load_backlog_report(path).findings[0].issue_ref == "#1312"

    def test_missing_artifact_names_the_path_and_the_flag(self, tmp_path: Path) -> None:
        with pytest.raises(BacklogReportError) as exc:
            load_backlog_report(tmp_path / "nope.json")
        message = str(exc.value)
        assert "not found" in message
        assert "nope.json" in message
        assert "--report" in message

    def test_empty_artifact_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "report.json"
        path.write_text("   \n", encoding="utf-8")
        with pytest.raises(BacklogReportError, match="empty"):
            load_backlog_report(path)

    def test_unparseable_artifact_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "report.json"
        path.write_text("{not: [valid: json", encoding="utf-8")
        with pytest.raises(BacklogReportError, match="not valid JSON or YAML"):
            load_backlog_report(path)
