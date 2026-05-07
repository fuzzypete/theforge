"""Tests for the audit-substrate health check in `forge check-config`."""

from __future__ import annotations

from pathlib import Path

from theforge.cli.check_config import _check_audit_substrate
from theforge.coordinator import audit_substrate as sub


def test_no_audits_dir_is_clean(tmp_path: Path) -> None:
    """A fresh repo (no .forge/audits) does not warn."""
    assert _check_audit_substrate(tmp_path) == []


def test_runs_without_substrate_warns(tmp_path: Path) -> None:
    """Per-run JSON files present without index.sqlite triggers a warning."""
    runs = sub.runs_dir(tmp_path)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "r1.json").write_text("{}", encoding="utf-8")
    warnings = _check_audit_substrate(tmp_path)
    assert len(warnings) == 1
    assert "per-run audit files exist" in warnings[0]
    assert "forge audits rebuild" in warnings[0]


def test_legacy_history_without_substrate_warns(tmp_path: Path) -> None:
    audits = sub.audits_dir(tmp_path)
    audits.mkdir(parents=True, exist_ok=True)
    (audits / "history.jsonl").write_text("{}\n", encoding="utf-8")
    warnings = _check_audit_substrate(tmp_path)
    assert len(warnings) == 1
    assert "history.jsonl present" in warnings[0]
    assert "forge audits rebuild" in warnings[0]


def test_corrupt_substrate_warns(tmp_path: Path) -> None:
    path = sub.substrate_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database" * 50)
    warnings = _check_audit_substrate(tmp_path)
    assert len(warnings) == 1
    assert "forge audits rebuild" in warnings[0]


def test_healthy_substrate_no_warning(tmp_path: Path) -> None:
    conn = sub.create_or_open(tmp_path)
    conn.commit()
    conn.close()
    assert _check_audit_substrate(tmp_path) == []
