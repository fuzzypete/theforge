"""Tests for the `forge audits show` CLI command."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from theforge.cli import audits as audits_cli
from theforge.coordinator import audit_substrate as sub


def _make_record(run_id: str, slug: str, *, cost: float = 1.0, score: int | None = 5) -> dict:
    rec: dict = {
        "run_id": run_id,
        "task": {"slug": slug, "name": slug},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": f"2026-03-01T10:00:00+00:00"},
        "totals": {"cost_usd": cost},
        "iterations": {"dev_iterations": 1, "review_cycles": 1},
        "reviews": [{"cycle": 1, "verdict": "APPROVE"}],
        "phases": {"dev": {"cost_usd": cost, "duration_s": 60.0, "outcome": "success"}},
    }
    if score is not None:
        rec["preflight"] = {"complexity_score": score}
    return rec


def _setup_project(tmp_path: Path) -> Path:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    return forge_yaml


def _show_args(forge_yaml: Path, *, slug: str | None = None, limit: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        config=str(forge_yaml),
        audits_command="show",
        slug=slug,
        limit=limit,
    )


def _populate_substrate(tmp_path: Path, records: list[dict]) -> None:
    audits = sub.audits_dir(tmp_path)
    audits.mkdir(parents=True, exist_ok=True)
    history = audits / "history.jsonl"
    history.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )
    rebuild_args = SimpleNamespace(
        config=str(tmp_path / "forge.yaml"),
        audits_command="rebuild",
        include_legacy_history=True,
    )
    rc = audits_cli.cmd_audits(rebuild_args)
    assert rc == 0


def test_show_renders_recent_rows(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    _populate_substrate(
        tmp_path,
        [_make_record("r1", "issue-100"), _make_record("r2", "issue-101")],
    )
    capsys.readouterr()  # discard rebuild noise

    rc = audits_cli.cmd_audits(_show_args(forge_yaml))
    assert rc == 0
    out = capsys.readouterr().out
    assert "issue-100" in out
    assert "issue-101" in out
    assert "legacy_history_jsonl" in out
    assert "2 record(s) shown" in out


def test_show_filters_by_slug(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    _populate_substrate(
        tmp_path,
        [_make_record("r1", "issue-100"), _make_record("r2", "issue-101")],
    )
    capsys.readouterr()

    rc = audits_cli.cmd_audits(_show_args(forge_yaml, slug="issue-100"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "issue-100" in out
    assert "issue-101" not in out
    assert "1 record(s) shown" in out
    assert "slug=issue-100" in out


def test_show_renders_imported_legacy_rows(tmp_path: Path, capsys) -> None:
    """Regression for the AC: substrate-only imported rows must render."""
    forge_yaml = _setup_project(tmp_path)
    _populate_substrate(tmp_path, [_make_record("legacy-1", "issue-old", score=3)])
    capsys.readouterr()

    rc = audits_cli.cmd_audits(_show_args(forge_yaml))
    assert rc == 0
    out = capsys.readouterr().out
    assert "issue-old" in out
    assert "legacy_history_jsonl" in out


def test_show_handles_null_complexity_score(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    _populate_substrate(tmp_path, [_make_record("r1", "issue-pre-score", score=None)])
    capsys.readouterr()

    rc = audits_cli.cmd_audits(_show_args(forge_yaml))
    assert rc == 0
    out = capsys.readouterr().out
    assert "issue-pre-score" in out
    # Null score renders as "-", not "None" or "null".
    assert "None" not in out
    assert "null" not in out


def test_show_no_records_for_unknown_slug(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    _populate_substrate(tmp_path, [_make_record("r1", "issue-100")])
    capsys.readouterr()

    rc = audits_cli.cmd_audits(_show_args(forge_yaml, slug="nope"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no audit records found for slug 'nope'" in out


def test_show_empty_substrate(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    # Create empty substrate by opening it once.
    sub.create_or_open(tmp_path).close()

    rc = audits_cli.cmd_audits(_show_args(forge_yaml))
    assert rc == 0
    out = capsys.readouterr().out
    assert "no audit records found" in out


def test_show_missing_substrate_errors_with_remediation(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    rc = audits_cli.cmd_audits(_show_args(forge_yaml))
    assert rc == 1
    err = capsys.readouterr().err
    assert "audit substrate not found" in err
    assert "forge audits rebuild" in err


def test_show_rejects_nonpositive_limit(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    sub.create_or_open(tmp_path).close()

    rc = audits_cli.cmd_audits(_show_args(forge_yaml, limit=0))
    assert rc == 2
    err = capsys.readouterr().err
    assert "--limit must be a positive integer" in err
