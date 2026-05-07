"""Tests for the `forge audits rebuild` CLI command."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from theforge.cli import audits as audits_cli
from theforge.coordinator import audit_substrate as sub


def _make_record(run_id: str, slug: str = "demo", cost: float = 1.0) -> dict:
    return {
        "run_id": run_id,
        "task": {"slug": slug, "name": slug},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": "2026-03-01T10:00:00+00:00"},
        "totals": {"cost_usd": cost},
        "iterations": {"dev_iterations": 1, "review_cycles": 1},
        "reviews": [{"cycle": 1, "verdict": "APPROVE"}],
        "phases": {"dev": {"cost_usd": cost, "duration_s": 60.0, "outcome": "success"}},
    }


def _setup_project(tmp_path: Path) -> Path:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    return forge_yaml


def _make_args(forge_yaml: Path, *, include_legacy: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        config=str(forge_yaml),
        audits_command="rebuild",
        include_legacy_history=include_legacy,
    )


def test_rebuild_from_runs_only(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    runs = sub.runs_dir(tmp_path)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "r1.json").write_text(json.dumps(_make_record("r1")), encoding="utf-8")
    (runs / "r2.json").write_text(json.dumps(_make_record("r2", "other")), encoding="utf-8")

    rc = audits_cli.cmd_audits(_make_args(forge_yaml))
    assert rc == 0
    out = capsys.readouterr().out
    assert "rebuild scanned 2 run files" in out
    assert "imported 2" in out
    assert "substrate ready" in out
    conn = sqlite3.connect(str(sub.substrate_path(tmp_path)))
    try:
        count = conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0]
    finally:
        conn.close()
    assert count == 2


def test_rebuild_with_legacy_history_repair_safe(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    audits = sub.audits_dir(tmp_path)
    audits.mkdir(parents=True, exist_ok=True)
    rec = _make_record("legacy-r1")
    (audits / "history.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    # First rebuild --include-legacy-history reports all imported.
    rc = audits_cli.cmd_audits(_make_args(forge_yaml, include_legacy=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "imported 1, skipped existing 0, updated/repaired 0" in out

    # Second rebuild with same history: pre-rebuild snapshot lets us classify
    # it as skipped_existing rather than re-imported.
    rc2 = audits_cli.cmd_audits(_make_args(forge_yaml, include_legacy=True))
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "imported 0, skipped existing 1, updated/repaired 0" in out2

    # Now mutate the legacy line and rerun: should be reported as repaired.
    rec_mutated = _make_record("legacy-r1", cost=999.0)
    (audits / "history.jsonl").write_text(json.dumps(rec_mutated) + "\n", encoding="utf-8")
    rc3 = audits_cli.cmd_audits(_make_args(forge_yaml, include_legacy=True))
    assert rc3 == 0
    out3 = capsys.readouterr().out
    assert "updated/repaired 1" in out3


def test_rebuild_no_runs_no_history(tmp_path: Path, capsys) -> None:
    forge_yaml = _setup_project(tmp_path)
    rc = audits_cli.cmd_audits(_make_args(forge_yaml))
    assert rc == 0
    out = capsys.readouterr().out
    assert "rebuild scanned 0 run files" in out
