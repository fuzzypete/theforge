"""Tests for the `forge audits skips` query CLI (issue #1453 AC6).

The command turns "show me all sprints in date range D where skip code C fired"
— which took manual log-walking this week — into a one-line query, and exposes
stuck-issue patterns via ``--stuck``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from theforge.cli import audits as audits_cli
from theforge.coordinator.audit_substrate import record_shape_skip_event


def _setup_project(tmp_path: Path) -> Path:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    return forge_yaml


def _emit(tmp_path: Path, issue: str, code: str, run: str, when: str) -> None:
    record_shape_skip_event(
        tmp_path,
        {
            "issue_id": issue,
            "reason_code": code,
            "severity": "blocking",
            "category": "blocked_by_semantic_gate",
            "four_question_axis": "response_not_yet_attempted",
            "source": "local_check",
            "run_id": run,
            "emitted_at": when,
        },
    )


def _args(forge_yaml: Path, **kw) -> SimpleNamespace:
    base = dict(
        config=str(forge_yaml),
        audits_command="skips",
        code=None,
        issue=None,
        category=None,
        since=None,
        until=None,
        stuck=False,
        threshold=3,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_code_and_date_range_query(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    _emit(tmp_path, "1135", "reopened_stale_contract", "r1", "2026-05-04T00:00:00Z")
    _emit(tmp_path, "1135", "reopened_stale_contract", "r2", "2026-05-06T00:00:00Z")
    _emit(tmp_path, "1135", "reopened_stale_contract", "r3", "2026-05-09T00:00:00Z")

    rc = audits_cli.cmd_audits(
        _args(
            forge_yaml,
            code="reopened_stale_contract",
            since="2026-05-05T00:00:00Z",
            until="2026-05-07T00:00:00Z",
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 skip event(s) shown" in out
    assert "reopened_stale_contract" in out


def test_stuck_query(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    for i in range(4):
        _emit(tmp_path, "1135", "reopened_stale_contract", f"r{i}", f"2026-05-0{i + 4}T00:00:00Z")
    _emit(tmp_path, "7", "needs_grooming_label", "r1", "2026-05-04T00:00:00Z")

    rc = audits_cli.cmd_audits(_args(forge_yaml, stuck=True, threshold=3))
    assert rc == 0
    out = capsys.readouterr().out
    assert "#1135" in out
    assert "1 stuck pattern(s)" in out


def test_no_matches(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    _emit(tmp_path, "1135", "reopened_stale_contract", "r1", "2026-05-04T00:00:00Z")
    rc = audits_cli.cmd_audits(_args(forge_yaml, code="nonexistent_code"))
    assert rc == 0
    assert "no shape-gate skip events matched" in capsys.readouterr().out


def test_missing_substrate_errors(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    rc = audits_cli.cmd_audits(_args(forge_yaml))
    assert rc == 1
    assert "audit substrate not found" in capsys.readouterr().err
