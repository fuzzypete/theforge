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

    def test_legacy_history_alone_raises_no_silent_import(self, tmp_path: Path) -> None:
        """Substrate missing + only legacy history.jsonl → operator-facing error.

        Runtime readers must never silently fall back to history.jsonl.
        Recovery requires `forge audits rebuild --include-legacy-history`.
        """
        _write_history(tmp_path, [_make_record(run_id="r1")])
        with pytest.raises(sub.SubstrateMissingError) as exc_info:
            sub.require_substrate(tmp_path)
        msg = str(exc_info.value)
        assert "history.jsonl" in msg
        assert "include-legacy-history" in msg

    def test_corrupt_substrate_raises(self, tmp_path: Path) -> None:
        path = sub.substrate_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a sqlite database, just garbage" * 50)
        with pytest.raises(sub.SubstrateCorruptError):
            sub.require_substrate(tmp_path)

    def test_missing_substrate_with_runs_rebuilds(self, tmp_path: Path) -> None:
        """When the substrate is absent but runs/*.json exists, require_substrate
        rebuilds from the canonical per-run files rather than raising."""
        _write_runs(tmp_path, [_make_record(run_id="r1"), _make_record(run_id="r2", slug="b")])
        conn = sub.require_substrate(tmp_path)
        try:
            assert sub.count_records(conn) == 2
            row = conn.execute("SELECT provenance FROM audit_records WHERE run_id='r1'").fetchone()
        finally:
            conn.close()
        assert (row[0] if not isinstance(row, sqlite3.Row) else row["provenance"]) == "native"

    def test_stale_native_source_triggers_rebuild(self, tmp_path: Path) -> None:
        """A native row whose source per-run file no longer exists is purged
        on the next require_substrate call."""
        _write_runs(tmp_path, [_make_record(run_id="r1"), _make_record(run_id="r2", slug="b")])
        # Initial build.
        sub.require_substrate(tmp_path).close()
        # Delete one source file.
        (sub.runs_dir(tmp_path) / "r1.json").unlink()
        conn = sub.require_substrate(tmp_path)
        try:
            count = sub.count_records(conn)
        finally:
            conn.close()
        assert count == 1


class TestNativeProvenanceProtection:
    def test_legacy_import_does_not_overwrite_native(self, tmp_path: Path) -> None:
        """Once a native per-run row exists for a run_id, a legacy import
        with the same run_id must not downgrade provenance/content."""
        native = _make_record(run_id="r1", cost=1.0)
        legacy = _make_record(run_id="r1", cost=999.0)
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, native, provenance="native")
            r = sub.upsert_run_record(conn, legacy, provenance="legacy_history_jsonl")
            conn.commit()
            row = conn.execute(
                "SELECT provenance, raw_json FROM audit_records WHERE run_id='r1'"
            ).fetchone()
        finally:
            conn.close()
        assert r.skipped_protected
        prov = row[0] if not isinstance(row, sqlite3.Row) else row["provenance"]
        raw = row[1] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        assert prov == "native"
        assert "1.0" in raw  # canonical native record content was preserved


class TestLegacyStableIdentity:
    def test_repaired_no_run_id_record_is_not_duplicated(self, tmp_path: Path) -> None:
        """Importing a legacy record without run_id, then re-importing a
        repaired version of the same logical run, must update in place."""
        rec = _make_record(run_id=None, cost=1.0)
        rec.pop("run_id", None)
        _write_history(tmp_path, [rec])
        s1 = sub.import_history_jsonl(tmp_path)
        assert s1.imported == 1
        rec2 = _make_record(run_id=None, cost=2.0)  # same slug + started_at
        rec2.pop("run_id", None)
        _write_history(tmp_path, [rec2])
        s2 = sub.import_history_jsonl(tmp_path)
        assert s2.imported == 0
        assert s2.updated_repaired == 1
        # Substrate now contains exactly one row for that logical record.
        conn = sqlite3.connect(str(sub.substrate_path(tmp_path)))
        try:
            count = conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()[0]
        finally:
            conn.close()
        assert count == 1


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


class TestEscalationProjection:
    """The substrate→escalation projection feeds adaptive routing's promotion logic."""

    def _make_run(
        self,
        *,
        run_id: str,
        slug: str,
        complexity_band: str,
        success: bool,
        provider: str = "anthropic",
        model: str = "sonnet",
        cli: str = "claude",
        started_at: str = "2026-03-01T10:00:00+00:00",
    ) -> dict:
        return {
            "run_id": run_id,
            "task": {"slug": slug},
            "outcome": {"success": success, "final_phase": "DONE" if success else "ESCALATE"},
            "timing": {"started_at": started_at, "duration_seconds": 1.0},
            "preflight": {"complexity": complexity_band, "complexity_score": 5},
            "cost": {
                "total_usd": 1.0,
                "agents": [
                    {
                        "phase": "dev",
                        "provider": provider,
                        "model": model,
                        "cli": cli,
                        "name": "dev",
                    }
                ],
            },
        }

    def test_complexity_normalized_to_promotion_keys(self, tmp_path: Path) -> None:
        """Lower-case audit bands (small/medium/large) project to LOW/MEDIUM/HIGH."""
        conn = sub.create_or_open(tmp_path)
        try:
            for i, band in enumerate(("small", "medium", "large")):
                sub.upsert_run_record(
                    conn,
                    self._make_run(
                        run_id=f"r{i}",
                        slug=f"s{i}",
                        complexity_band=band,
                        success=False,
                        started_at=f"2026-03-0{i + 1}T10:00:00+00:00",
                    ),
                    provenance="native",
                )
            conn.commit()
            projected = list(sub.iter_escalation_records(conn))
        finally:
            conn.close()
        complexities = [p["complexity"] for p in projected]
        assert complexities == ["LOW", "MEDIUM", "HIGH"]

    def test_promotion_fires_from_two_substrate_escalations(self, tmp_path: Path) -> None:
        """Two substrate ESCALATE rows for the same model trigger _check_promotion.

        Regression guard: pre-fix the projection emitted ``medium`` while the
        promotion comparator used ``MEDIUM``, so the rows never matched.
        """
        from theforge.assignment import _check_promotion
        from theforge.coordinator.escalation_history import (
            load_escalation_history_from_substrate,
        )

        conn = sub.create_or_open(tmp_path)
        try:
            for i in range(2):
                sub.upsert_run_record(
                    conn,
                    self._make_run(
                        run_id=f"esc-{i}",
                        slug=f"story-{i}",
                        complexity_band="medium",
                        success=False,
                        started_at=f"2026-03-0{i + 1}T10:00:00+00:00",
                    ),
                    provenance="native",
                )
            conn.commit()
        finally:
            conn.close()

        history = load_escalation_history_from_substrate(tmp_path)
        # All projected rows agree on the canonical dev model identity.
        assert all(r.dev_model == "anthropic/sonnet/cli" for r in history)
        result = _check_promotion(
            "MEDIUM",
            "anthropic/sonnet/cli",
            history,
            sprint_promotions=None,
            dev_canonical_id="anthropic/sonnet/cli",
        )
        assert result == "promoted"


class TestLoaderFailsLoudWhenAuditInputsExist:
    """Adaptive routing must not silently degrade when audit inputs say history exists."""

    def test_loader_raises_when_substrate_missing_with_legacy_history(
        self, tmp_path: Path
    ) -> None:
        """Legacy history.jsonl on disk + missing substrate → operator-facing error."""
        from theforge.coordinator.escalation_history import (
            load_escalation_history_from_substrate,
        )

        _write_history(tmp_path, [_make_record(run_id="r1")])
        with pytest.raises(sub.SubstrateMissingError):
            load_escalation_history_from_substrate(tmp_path)

    def test_loader_raises_on_corrupt_substrate(self, tmp_path: Path) -> None:
        """Corrupt substrate must not silently route without history."""
        from theforge.coordinator.escalation_history import (
            load_escalation_history_from_substrate,
        )

        path = sub.substrate_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a sqlite file" * 50)
        with pytest.raises(sub.SubstrateCorruptError):
            load_escalation_history_from_substrate(tmp_path)

    def test_loader_returns_empty_for_truly_fresh_repo(self, tmp_path: Path) -> None:
        """No substrate, no audit inputs → empty list (legitimate fresh-repo path)."""
        from theforge.coordinator.escalation_history import (
            load_escalation_history_from_substrate,
        )

        assert load_escalation_history_from_substrate(tmp_path) == []


class TestRebuildPreservesLegacyRows:
    """Runtime stale-rebuild must not silently drop history.jsonl-imported records."""

    def test_legacy_rows_survive_runtime_rebuild(self, tmp_path: Path) -> None:
        # Bootstrap: import legacy history once via the operator path.
        legacy = _make_record(run_id="legacy-r1", slug="legacy-slug")
        _write_history(tmp_path, [legacy])
        sub.import_history_jsonl(tmp_path)
        # Confirm the row is present with legacy provenance.
        conn = sqlite3.connect(str(sub.substrate_path(tmp_path)))
        try:
            count_before = conn.execute(
                "SELECT COUNT(*) FROM audit_records WHERE provenance = 'legacy_history_jsonl'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count_before == 1

        # Now write a native run and trigger a stale-rebuild by mutating the
        # native source file's mtime so require_substrate's stale check fires.
        _write_runs(tmp_path, [_make_record(run_id="native-r1", slug="native-slug")])
        # First require_substrate: rebuilds, indexes the native row.
        sub.require_substrate(tmp_path).close()
        # Bump mtime to force a stale check on the next call.
        run_file = sub.runs_dir(tmp_path) / "native-r1.json"
        new_mtime = run_file.stat().st_mtime + 10.0
        import os

        os.utime(run_file, (new_mtime, new_mtime))
        # Second require_substrate: detects mtime mismatch, rebuilds. Legacy
        # rows must survive.
        conn = sub.require_substrate(tmp_path)
        try:
            legacy_after = conn.execute(
                "SELECT raw_json FROM audit_records WHERE provenance = 'legacy_history_jsonl'"
            ).fetchall()
            native_after = conn.execute(
                "SELECT COUNT(*) FROM audit_records WHERE provenance = 'native'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert len(legacy_after) == 1, "legacy row was dropped by runtime rebuild"
        assert native_after == 1
