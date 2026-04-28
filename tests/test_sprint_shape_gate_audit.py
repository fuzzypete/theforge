"""Audit/summary rendering for sprint-entry skipped issues."""

from __future__ import annotations

import datetime
from pathlib import Path

import yaml

from theforge.sprint.audit import _write_sprint_audit, _write_sprint_summary
from theforge.sprint.manifest import ResolvedSprint, SprintResult
from theforge.sprint.shape_gate import SkippedIssue


def _make_result() -> SprintResult:
    return SprintResult(
        name="test-sprint",
        specs_total=0,
        specs_succeeded=0,
        specs_failed=0,
        specs_skipped=0,
        total_cost_usd=0.0,
        budget_usd=10.0,
        results=[],
    )


def _make_manifest() -> ResolvedSprint:
    return ResolvedSprint(
        name="test-sprint",
        budget_usd=10.0,
        stories=[],
        max_parallel=1,
    )


def test_sprint_audit_writes_skipped_issues(tmp_path: Path) -> None:
    skipped = [
        SkippedIssue(
            issue_number=42,
            reason_codes=("too_many_behavioral_clusters",),
            source="local_check",
            title="Too big",
        ),
    ]
    now = datetime.datetime.now(datetime.timezone.utc)

    _write_sprint_audit(
        manifest=_make_manifest(),
        result=_make_result(),
        canonical_refs=[],
        started_at=now,
        finished_at=now,
        duration=0.0,
        project_root=tmp_path,
        skipped_issues=skipped,
    )

    audit_yaml = yaml.safe_load((tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text())
    assert audit_yaml["skipped"] == [
        {
            "issue_number": 42,
            "reason_codes": ["too_many_behavioral_clusters"],
            "source": "local_check",
            "title": "Too big",
            "detail": "",
        }
    ]


def test_sprint_summary_writes_skipped_issues(tmp_path: Path) -> None:
    skipped = [
        SkippedIssue(
            issue_number=7,
            reason_codes=("needs-grooming-label",),
            source="label",
            title="Stale",
        ),
    ]
    now = datetime.datetime.now(datetime.timezone.utc)
    log_dir = tmp_path / "logs"

    _write_sprint_summary(
        manifest=_make_manifest(),
        result=_make_result(),
        canonical_refs=[],
        started_at=now,
        finished_at=now,
        duration=0.0,
        sprint_log_dir=log_dir,
        skipped_issues=skipped,
    )

    summary = yaml.safe_load((log_dir / "sprint-summary.yaml").read_text())
    assert len(summary["skipped"]) == 1
    entry = summary["skipped"][0]
    assert entry["issue_number"] == 7
    assert entry["source"] == "label"
    assert entry["reason_codes"] == ["needs-grooming-label"]


def test_sprint_audit_skipped_empty_when_not_supplied(tmp_path: Path) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    _write_sprint_audit(
        manifest=_make_manifest(),
        result=_make_result(),
        canonical_refs=[],
        started_at=now,
        finished_at=now,
        duration=0.0,
        project_root=tmp_path,
    )
    audit_yaml = yaml.safe_load((tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text())
    assert audit_yaml["skipped"] == []


def test_sprint_audit_records_closed_dependency_slugs(tmp_path: Path) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    manifest = ResolvedSprint(
        name="test-sprint",
        budget_usd=10.0,
        stories=[],
        max_parallel=1,
        closed_dependency_slugs={"issue-99", "issue-42"},
    )

    _write_sprint_audit(
        manifest=manifest,
        result=_make_result(),
        canonical_refs=[],
        started_at=now,
        finished_at=now,
        duration=0.0,
        project_root=tmp_path,
    )

    audit_yaml = yaml.safe_load((tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text())
    assert audit_yaml["closed_dependency_slugs"] == [
        {"slug": "issue-42", "source": "remote_closed"},
        {"slug": "issue-99", "source": "remote_closed"},
    ]

    empty_root = tmp_path / "empty"
    _write_sprint_audit(
        manifest=_make_manifest(),
        result=_make_result(),
        canonical_refs=[],
        started_at=now,
        finished_at=now,
        duration=0.0,
        project_root=empty_root,
    )

    empty_audit_yaml = yaml.safe_load(
        (empty_root / ".forge" / "audits" / "sprint-audit.yaml").read_text()
    )
    assert empty_audit_yaml["closed_dependency_slugs"] == []
