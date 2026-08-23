"""Tests for deterministic backlog report generation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from theforge.triage_backlog_report import (
    STATUS_ACTIVE,
    STATUS_STALE,
    STATUS_UNVERIFIED,
    BacklogIssue,
    TriageBacklogReportError,
    build_backlog_report,
    fetch_backlog_issues,
    render_backlog_report,
    write_backlog_report,
)
from theforge.triage_report import load_backlog_report


def _issue(
    number: int,
    body: str,
    *,
    labels: tuple[str, ...] = ("forge-finding", "bug"),
    milestone: str | None = None,
    created_at: str = "2026-06-01T00:00:00Z",
) -> BacklogIssue:
    return BacklogIssue(
        number=number,
        title=f"finding {number}",
        body=body,
        labels=labels,
        created_at=created_at,
        milestone=milestone,
    )


class TestFetchBacklogIssues:
    def test_unions_open_findings_across_labels_without_duplicates(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def _proc(stdout: str) -> MagicMock:
            return MagicMock(returncode=0, stdout=stdout, stderr="")

        side_effect = [
            _proc(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "number": 12,
                                "title": "first",
                                "body": "body",
                                "createdAt": "2026-06-01T00:00:00Z",
                                "labels": ["bug", "forge-finding"],
                                "milestone": "Hygiene",
                            }
                        ),
                        json.dumps(
                            {
                                "number": 20,
                                "title": "second",
                                "body": "body",
                                "createdAt": "2026-06-02T00:00:00Z",
                                "labels": ["review-finding"],
                                "milestone": None,
                            }
                        ),
                    ]
                )
            ),
            _proc(
                json.dumps(
                    {
                        "number": 20,
                        "title": "second",
                        "body": "body",
                        "createdAt": "2026-06-02T00:00:00Z",
                        "labels": ["review-finding", "p2"],
                        "milestone": None,
                    }
                )
            ),
            _proc(
                json.dumps(
                    {
                        "number": 12,
                        "title": "first",
                        "body": "body",
                        "createdAt": "2026-06-01T00:00:00Z",
                        "labels": ["bug", "forge-finding", "needs-triage"],
                        "milestone": "Hygiene",
                    }
                )
            ),
        ]

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = side_effect
            issues = fetch_backlog_issues(tmp_path)
            calls = [call.args[0] for call in mock_run.call_args_list]

        assert [issue.number for issue in issues] == [12, 20]
        assert issues[0].labels == ("bug", "forge-finding", "needs-triage")
        assert issues[1].labels == ("p2", "review-finding")
        assert all("--paginate" in cmd for cmd in calls)
        assert all("issue" not in " ".join(cmd[0:3]) for cmd in calls)

    def test_gh_failure_is_reported_legibly(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="bad credentials")
            try:
                fetch_backlog_issues(tmp_path)
            except TriageBacklogReportError as exc:
                assert "bad credentials" in str(exc)
            else:
                raise AssertionError("expected backlog query failure")


class TestBuildBacklogReport:
    def test_marks_active_when_cited_file_exists_and_churn_is_counted(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def demo():\n    return 1\n", encoding="utf-8")

        report = build_backlog_report(
            [_issue(12, "Evidence: `src/demo.py`")],
            project_root=tmp_path,
            current_milestone="v0.12.0",
            named_milestones=("v0.13.0",),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 4,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert finding.verification_status == STATUS_ACTIVE
        assert any("present in current tree" in entry.summary for entry in finding.evidence)
        assert any("changed 4 time(s)" in entry.summary for entry in finding.evidence)
        assert finding.age_days == 83

    def test_marks_active_when_github_line_anchor_targets_existing_line(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def demo():\n    return 1\n", encoding="utf-8")

        report = build_backlog_report(
            [_issue(18, "Evidence: `src/demo.py#L2`")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 2,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert finding.verification_status == STATUS_ACTIVE
        assert any(
            entry.evidence_id == "path-line:src/demo.py:2:present" for entry in finding.evidence
        )
        assert any(entry.evidence_id == "path:src/demo.py:churn" for entry in finding.evidence)

    def test_marks_stale_when_cited_file_is_absent(self, tmp_path: Path) -> None:
        report = build_backlog_report(
            [_issue(13, "Evidence: `src/missing.py`")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert finding.verification_status == STATUS_STALE
        assert "absent from current tree" in finding.evidence[0].summary

    def test_marks_stale_when_cited_line_is_beyond_end_of_file(self, tmp_path: Path) -> None:
        target = tmp_path / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def demo():\n    return 1\n", encoding="utf-8")

        report = build_backlog_report(
            [_issue(19, "Evidence: `src/demo.py:9`")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert finding.verification_status == STATUS_STALE
        assert any(
            entry.evidence_id == "path-line:src/demo.py:9:absent" for entry in finding.evidence
        )

    def test_marks_stale_when_github_line_range_extends_beyond_end_of_file(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def demo():\n", encoding="utf-8")

        report = build_backlog_report(
            [_issue(24, "Evidence: `src/demo.py#L1-L3`")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert finding.verification_status == STATUS_STALE
        assert any(
            entry.evidence_id == "path-line:src/demo.py:1-3:absent" for entry in finding.evidence
        )

    def test_keeps_distinct_github_line_citations_that_share_a_start_line(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("one\n", encoding="utf-8")

        report = build_backlog_report(
            [_issue(27, "Evidence: `src/demo.py#L1` and `src/demo.py#L1-L3`")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        evidence_ids = {entry.evidence_id for entry in finding.evidence}
        assert "path-line:src/demo.py:1:present" in evidence_ids
        assert "path-line:src/demo.py:1-3:absent" in evidence_ids

    def test_marks_stale_when_cited_symbol_is_absent(self, tmp_path: Path) -> None:
        report = build_backlog_report(
            [_issue(14, "cited symbol `_validate_auto_api_fallback_schema` is gone")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert finding.verification_status == STATUS_STALE
        assert any(
            "cited symbol _validate_auto_api_fallback_schema absent" in e.summary
            for e in finding.evidence
        )

    def test_marks_unverified_when_no_checkable_citation_can_be_parsed(
        self, tmp_path: Path
    ) -> None:
        report = build_backlog_report(
            [_issue(15, "The finding is still probably valid but cites only prose.")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert finding.verification_status == STATUS_UNVERIFIED
        assert finding.evidence[0].checkable is False
        assert "no checkable artifact" in finding.evidence[0].summary

    def test_marks_unverified_when_churn_cannot_be_counted(self, tmp_path: Path) -> None:
        target = tmp_path / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def demo():\n    return 1\n", encoding="utf-8")

        report = build_backlog_report(
            [_issue(16, "Evidence: `src/demo.py`")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: (_ for _ in ()).throw(
                RuntimeError("git log failed")
            ),
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert finding.verification_status == STATUS_UNVERIFIED
        assert any("could not count churn" in entry.summary for entry in finding.evidence)

    def test_unsupported_non_line_anchor_still_collects_path_evidence(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def demo():\n    return 1\n", encoding="utf-8")

        report = build_backlog_report(
            [_issue(21, "Evidence: `src/demo.py#main`")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert finding.verification_status == STATUS_UNVERIFIED
        assert any(
            "could not mechanically verify cited path token src/demo.py#main" in entry.summary
            for entry in finding.evidence
        )
        assert any(entry.evidence_id == "path:src/demo.py:present" for entry in finding.evidence)
        assert any(entry.evidence_id == "path:src/demo.py:churn" for entry in finding.evidence)
        assert all(entry.observed_status != STATUS_STALE for entry in finding.evidence)

    def test_extensionless_module_path_resolves_against_known_source_extensions(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "theforge" / "cli" / "triage.py"
        target.parent.mkdir(parents=True)
        target.write_text("def main():\n    return 0\n", encoding="utf-8")

        report = build_backlog_report(
            [_issue(23, "Evidence: `src/theforge/cli/triage`")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert all(entry.observed_status != STATUS_STALE for entry in finding.evidence)
        assert any(
            entry.evidence_id == "path:src/theforge/cli/triage.py:present"
            for entry in finding.evidence
        )

    def test_unresolvable_extensionless_path_is_unverified_not_stale(self, tmp_path: Path) -> None:
        report = build_backlog_report(
            [_issue(24, "Evidence: `src/theforge/cli/ghost`")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert all(entry.observed_status != STATUS_STALE for entry in finding.evidence)
        assert any(
            entry.evidence_id == "path:src/theforge/cli/ghost:unverified"
            for entry in finding.evidence
        )

    def test_marks_unverified_when_bare_filename_does_not_resolve(self, tmp_path: Path) -> None:
        report = build_backlog_report(
            [_issue(22, "Operators were editing a stray config.yaml during the incident.")],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )

        finding = report.findings[0]
        assert finding.verification_status == STATUS_UNVERIFIED
        assert any(
            "could not attribute cited filename config.yaml" in entry.summary
            for entry in finding.evidence
        )
        assert all(entry.observed_status != STATUS_STALE for entry in finding.evidence)

    def test_generated_artifact_round_trips_into_the_existing_consumer(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def demo():\n    return 1\n", encoding="utf-8")

        report = build_backlog_report(
            [_issue(17, "Evidence: `src/demo.py`")],
            project_root=tmp_path,
            current_milestone="v0.12.0",
            named_milestones=("v0.13.0",),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 1,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )
        path = write_backlog_report(tmp_path, report)
        loaded = load_backlog_report(path)

        assert loaded.current_milestone == "v0.12.0"
        assert loaded.named_milestones == ("v0.13.0",)
        assert loaded.findings[0].issue_ref == "#17"
        assert any(
            entry.evidence_id.startswith("path:src/demo.py")
            for entry in loaded.findings[0].evidence
        )

    def test_generated_artifact_round_trips_when_same_path_is_cited_multiple_times(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "src" / "demo.py"
        target.parent.mkdir(parents=True)
        target.write_text("def demo():\n    return 1\n    return 2\n", encoding="utf-8")
        churn_calls: list[tuple[str, str]] = []

        def churn_counter(_root: Path, relpath: str, created_at: str) -> int:
            churn_calls.append((relpath, created_at))
            return 3

        report = build_backlog_report(
            [_issue(23, "Evidence: `src/demo.py:1`, `src/demo.py:3`, and `src/demo.py`")],
            project_root=tmp_path,
            current_milestone="v0.12.0",
            named_milestones=("v0.13.0",),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=churn_counter,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )
        path = write_backlog_report(tmp_path, report)
        loaded = load_backlog_report(path)

        assert churn_calls == [("src/demo.py", "2026-06-01T00:00:00Z")]
        assert [entry.evidence_id for entry in loaded.findings[0].evidence].count(
            "path:src/demo.py:churn"
        ) == 1

    def test_generated_artifact_round_trips_when_same_missing_path_is_cited_multiple_times(
        self, tmp_path: Path
    ) -> None:
        report = build_backlog_report(
            [
                _issue(
                    25,
                    "Evidence: `src/missing.py:12`, `src/missing.py:40`, and `src/missing.py`",
                )
            ],
            project_root=tmp_path,
            current_milestone="v0.12.0",
            named_milestones=("v0.13.0",),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 1,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )
        path = write_backlog_report(tmp_path, report)
        loaded = load_backlog_report(path)

        evidence_ids = [entry.evidence_id for entry in loaded.findings[0].evidence]
        assert evidence_ids.count("path:src/missing.py:absent") == 1
        assert len(evidence_ids) == len(set(evidence_ids))

    def test_round_trip_dedupes_repeated_unattributable_filename_citations(
        self, tmp_path: Path
    ) -> None:
        report = build_backlog_report(
            [
                _issue(
                    26,
                    "Evidence: `config.yaml:12`, `config.yaml#L9`, and `config.yaml`",
                )
            ],
            project_root=tmp_path,
            current_milestone="v0.12.0",
            named_milestones=("v0.13.0",),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 1,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )
        path = write_backlog_report(tmp_path, report)
        loaded = load_backlog_report(path)

        evidence_ids = [entry.evidence_id for entry in loaded.findings[0].evidence]
        assert evidence_ids.count("path:config.yaml:unverified") == 1
        assert len(evidence_ids) == len(set(evidence_ids))


class TestRendering:
    def test_empty_backlog_renders_successfully(self, tmp_path: Path) -> None:
        report = build_backlog_report(
            [],
            project_root=tmp_path,
            current_milestone=None,
            named_milestones=(),
            now=datetime(2026, 8, 23, tzinfo=UTC),
            churn_counter=lambda _root, _path, _created_at: 0,
            symbol_lookup=lambda _root, _symbol, _paths: [],
        )
        path = tmp_path / ".forge" / "triage" / "report.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(report.to_dict(), sort_keys=False), encoding="utf-8")

        rendered = render_backlog_report(report, path, tmp_path)
        assert "FINDING BACKLOG — 0 open" in rendered
        assert "No open finding-labeled issues matched the backlog query." in rendered
        assert "structured report: .forge/triage/report.yaml" in rendered
