"""Tests for the audit-storage section in `forge check-config`."""

from __future__ import annotations

from pathlib import Path

from theforge.cli.check_config import _format_audit_storage
from theforge.coordinator import audit_substrate as sub


def _joined(lines: list[str]) -> str:
    return "\n".join(lines)


def test_format_audit_storage_healthy(tmp_path: Path) -> None:
    conn = sub.create_or_open(tmp_path)
    conn.commit()
    conn.close()
    lines, errors, warnings = _format_audit_storage(tmp_path)
    body = _joined(lines)
    assert errors == []
    assert warnings == []
    assert "AUDIT STORAGE" in body
    assert "mode:" in body and "sqlite" in body
    assert str(sub.substrate_path(tmp_path)) in body
    assert "schema version:" in body
    assert str(sub.SUBSTRATE_SCHEMA_VERSION) in body
    assert "size:" in body
    assert "last write:" in body


def test_format_audit_storage_missing_fresh(tmp_path: Path) -> None:
    lines, errors, warnings = _format_audit_storage(tmp_path)
    body = _joined(lines)
    assert errors == []
    assert "AUDIT STORAGE" in body
    assert "not initialized" in body
    assert "forge audits rebuild" in body or "forge sprint" in body
    assert str(sub.substrate_path(tmp_path)) in body


def test_format_audit_storage_missing_with_legacy_history(tmp_path: Path) -> None:
    audits = sub.audits_dir(tmp_path)
    audits.mkdir(parents=True, exist_ok=True)
    (audits / "history.jsonl").write_text("{}\n", encoding="utf-8")
    lines, errors, warnings = _format_audit_storage(tmp_path)
    body = _joined(lines)
    assert errors == []
    assert "not initialized" in body
    assert "history.jsonl" in body
    assert "--include-legacy-history" in body


def test_format_audit_storage_missing_with_run_files(tmp_path: Path) -> None:
    runs = sub.runs_dir(tmp_path)
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "r1.json").write_text("{}", encoding="utf-8")
    lines, errors, warnings = _format_audit_storage(tmp_path)
    body = _joined(lines)
    assert errors == []
    assert "not initialized" in body
    assert "per-run audit files" in body
    assert "forge audits rebuild" in body


def test_format_audit_storage_unreadable_is_hard_error(tmp_path: Path) -> None:
    path = sub.substrate_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database" * 50)
    lines, errors, warnings = _format_audit_storage(tmp_path)
    body = _joined(lines)
    assert len(errors) == 1
    assert "ERROR" in body
    assert str(path) in errors[0]
    assert "forge audits rebuild" in errors[0]
