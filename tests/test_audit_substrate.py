"""Tests for the SQLite audit substrate (Phase B)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from theforge.coordinator import audit_substrate as sub


def _make_record(
    *,
    run_id: str | None = "run-001",
    slug: str = "demo",
    started_at: str = "2026-03-01T10:00:00+00:00",
    final_phase: str = "DONE",
    success: bool = True,
    cost: float = 1.23,
    reviews: list[dict] | None = None,
    landing_status: str | None = None,
) -> dict:
    rec: dict = {
        "task": {"slug": slug, "name": slug},
        "outcome": {"success": success, "final_phase": final_phase},
        "timing": {"started_at": started_at, "duration_seconds": 60.0},
        "cost": {"total_usd": cost},
        "totals": {"cost_usd": cost, "duration_s": 60.0},
        "iterations": {"dev_iterations": 1, "review_cycles": 1},
        "reviews": reviews or [],
        "phases": {"dev": {"cost_usd": cost, "duration_s": 60.0, "outcome": "success"}},
    }
    if run_id is not None:
        rec["run_id"] = run_id
    if landing_status is not None:
        rec["landing_status"] = landing_status
    return rec


def _write_runs(project_root: Path, records: list[dict]) -> None:
    runs = sub.runs_dir(project_root)
    runs.mkdir(parents=True, exist_ok=True)
    for rec in records:
        rid = rec["run_id"]
        (runs / f"{rid}.json").write_text(json.dumps(rec), encoding="utf-8")


def _write_history(project_root: Path, records: list[dict]) -> None:
    audits = sub.audits_dir(project_root)
    audits.mkdir(parents=True, exist_ok=True)
    with open(audits / "history.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class TestSchemaBootstrap:
    def test_create_or_open_creates_schema(self, tmp_path: Path) -> None:
        conn = sub.create_or_open(tmp_path)
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        assert "audit_records" in tables
        assert "reviews" in tables
        assert "meta" in tables

    def test_substrate_path_is_under_dot_forge_audits(self, tmp_path: Path) -> None:
        p = sub.substrate_path(tmp_path)
        assert p.name == "index.sqlite"
        assert p.parent.name == "audits"


class TestUpsertAndQuery:
    def test_write_then_query_review_approve(self, tmp_path: Path) -> None:
        rec = _make_record(reviews=[{"cycle": 1, "verdict": "APPROVE"}])
        conn = sub.create_or_open(tmp_path)
        try:
            r = sub.upsert_run_record(conn, rec, provenance="native")
            assert r.inserted
            conn.commit()
            results = list(sub.has_review_approve_in_substrate(conn, "demo"))
        finally:
            conn.close()
        assert len(results) == 1
        assert results[0]["task"]["slug"] == "demo"

    def test_upsert_idempotent_unchanged(self, tmp_path: Path) -> None:
        rec = _make_record()
        conn = sub.create_or_open(tmp_path)
        try:
            r1 = sub.upsert_run_record(conn, rec, provenance="native")
            r2 = sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
        finally:
            conn.close()
        assert r1.inserted
        assert r2.unchanged

    def test_upsert_updates_changed_record(self, tmp_path: Path) -> None:
        rec = _make_record(cost=1.0)
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            rec2 = _make_record(cost=2.0)
            r = sub.upsert_run_record(conn, rec2, provenance="native")
            conn.commit()
        finally:
            conn.close()
        assert r.updated

    def test_provenance_distinguishable(self, tmp_path: Path) -> None:
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, _make_record(run_id="r1"), provenance="native")
            sub.upsert_run_record(
                conn,
                _make_record(run_id="r2"),
                provenance="legacy_history_jsonl",
            )
            conn.commit()
            rows = conn.execute(
                "SELECT run_id, provenance FROM audit_records ORDER BY run_id"
            ).fetchall()
        finally:
            conn.close()
        provs = {r[0]: r[1] for r in rows}
        assert provs == {"r1": "native", "r2": "legacy_history_jsonl"}


class TestRebuildFromRuns:
    def test_rebuild_picks_up_runs(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_make_record(run_id="r1"), _make_record(run_id="r2", slug="other")])
        summary = sub.rebuild_from_runs(tmp_path)
        assert summary.runs_seen == 2
        assert summary.imported == 2
        assert summary.failed == 0
        conn = sqlite3.connect(str(sub.substrate_path(tmp_path)))
        try:
            count = conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0]
        finally:
            conn.close()
        assert count == 2

    def test_rebuild_skips_records_missing_run_id(self, tmp_path: Path) -> None:
        runs = sub.runs_dir(tmp_path)
        runs.mkdir(parents=True, exist_ok=True)
        rec = _make_record(run_id=None)
        # Without a run_id field at all.
        rec.pop("run_id", None)
        (runs / "stray.json").write_text(json.dumps(rec), encoding="utf-8")
        summary = sub.rebuild_from_runs(tmp_path)
        assert summary.failed == 1
        assert summary.imported == 0

    def test_rebuild_recreates_drops_orphans(self, tmp_path: Path) -> None:
        _write_runs(tmp_path, [_make_record(run_id="r1")])
        sub.rebuild_from_runs(tmp_path)
        # Now remove the run file and rebuild — orphan should not survive.
        (sub.runs_dir(tmp_path) / "r1.json").unlink()
        summary = sub.rebuild_from_runs(tmp_path)
        assert summary.imported == 0
        conn = sqlite3.connect(str(sub.substrate_path(tmp_path)))
        try:
            count = conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0]
        finally:
            conn.close()
        assert count == 0


class TestImportHistoryJsonl:
    def test_import_idempotent(self, tmp_path: Path) -> None:
        records = [
            _make_record(run_id="r1"),
            _make_record(run_id="r2", slug="b"),
        ]
        _write_history(tmp_path, records)
        s1 = sub.import_history_jsonl(tmp_path)
        s2 = sub.import_history_jsonl(tmp_path)
        assert s1.imported == 2 and s1.skipped_existing == 0
        assert s2.imported == 0 and s2.skipped_existing == 2
        assert s2.updated_repaired == 0

    def test_import_repair_path(self, tmp_path: Path) -> None:
        rec = _make_record(run_id="r1", cost=1.0)
        _write_history(tmp_path, [rec])
        sub.import_history_jsonl(tmp_path)
        # Mutate the source line and reimport.
        rec2 = _make_record(run_id="r1", cost=99.0)
        _write_history(tmp_path, [rec2])
        s = sub.import_history_jsonl(tmp_path)
        assert s.imported == 0
        assert s.updated_repaired == 1
        assert s.skipped_existing == 0

    def test_legacy_record_without_run_id_uses_synthetic_id(self, tmp_path: Path) -> None:
        rec = _make_record(run_id=None)
        rec.pop("run_id", None)
        _write_history(tmp_path, [rec])
        s = sub.import_history_jsonl(tmp_path)
        assert s.imported == 1
        # Re-importing the same record yields skipped (stable identity).
        s2 = sub.import_history_jsonl(tmp_path)
        assert s2.imported == 0
        assert s2.skipped_existing == 1


class TestRequireSubstrate:
    def test_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(sub.SubstrateMissingError):
            sub.require_substrate(tmp_path)

    def test_legacy_history_present_auto_imports(self, tmp_path: Path) -> None:
        _write_history(tmp_path, [_make_record(run_id="r1")])
        conn = sub.require_substrate(tmp_path)
        try:
            count = sub.count_records(conn)
            row = conn.execute("SELECT value FROM meta WHERE key='legacy_import_done'").fetchone()
        finally:
            conn.close()
        assert count == 1
        assert row is not None
        assert (row[0] if not isinstance(row, sqlite3.Row) else row["value"]) == "1"

    def test_corrupt_substrate_raises(self, tmp_path: Path) -> None:
        path = sub.substrate_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a sqlite database, just garbage" * 50)
        with pytest.raises(sub.SubstrateCorruptError):
            sub.require_substrate(tmp_path)


class TestQueryHelpers:
    def test_tail_records_orders_desc(self, tmp_path: Path) -> None:
        records = [
            _make_record(run_id="r1", started_at="2026-01-01T00:00:00+00:00"),
            _make_record(run_id="r2", started_at="2026-02-01T00:00:00+00:00"),
            _make_record(run_id="r3", started_at="2026-03-01T00:00:00+00:00"),
        ]
        conn = sub.create_or_open(tmp_path)
        try:
            for rec in records:
                sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            tail = sub.tail_records(conn, 2)
        finally:
            conn.close()
        assert len(tail) == 2
        assert tail[0]["run_id"] == "r3"
        assert tail[1]["run_id"] == "r2"

    def test_has_review_approve_landing_filter(self, tmp_path: Path) -> None:
        landed = _make_record(
            run_id="r1",
            reviews=[{"cycle": 1, "verdict": "APPROVE"}],
            landing_status="landed",
        )
        unlanded = _make_record(
            run_id="r2",
            slug="demo",
            reviews=[{"cycle": 1, "verdict": "APPROVE"}],
        )
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, landed, provenance="native")
            sub.upsert_run_record(conn, unlanded, provenance="native")
            conn.commit()
            with_landed = list(
                sub.has_review_approve_in_substrate(conn, "demo", require_landed=True)
            )
            without = list(sub.has_review_approve_in_substrate(conn, "demo"))
        finally:
            conn.close()
        assert len(with_landed) == 1
        assert with_landed[0]["run_id"] == "r1"
        assert len(without) == 2
