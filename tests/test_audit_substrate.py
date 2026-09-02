"""Tests for the SQLite audit substrate (Phase B)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.config import ModelProfile
from theforge.coordinator import audit_storage
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
        """``require_landed`` filters on projected landing evidence (#2849).

        Previously this filtered on ``audit_records.landing_status``. It now
        filters on the assertion published for the run, which is why ``r1``
        gets a ``.landed.json`` artifact here — the flattened column it also
        carries is no longer what the query reads.
        """
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
        landing = tmp_path / ".forge" / "audits" / "landing"
        landing.mkdir(parents=True)
        (landing / "r1.landed.json").write_text(
            json.dumps(
                {
                    "kind": "landing_assertion",
                    "schema_version": 1,
                    "run_id": "r1",
                    "slug": "demo",
                    "landing_mode": "merge-pr",
                    "target_branch": "main",
                    "reviewed_commit": "aaa",
                    "gated_commit": "bbb",
                    "carrier_kind": "pull_request",
                    "carrier_ref": "#1",
                    "landed_commit": "ccc",
                    "observer": "sprint.queued-pr",
                    "observed_at": "2026-03-01T12:00:00+00:00",
                }
            ),
            encoding="utf-8",
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

    def test_projection_yields_canonical_identity_and_promotion_band(self, tmp_path: Path) -> None:
        """Substrate ESCALATE rows project to a canonical dev identity + promotion band.

        Regression guard: pre-fix the projection emitted ``medium`` while the
        promotion comparator used ``MEDIUM``, so the rows never matched. Cross-run
        dev promotion now flows through the capability profiles (#158) seeded from
        this same history, so the projection must still surface a canonical
        ``dev_model`` and the normalized promotion band the profile reader keys on.
        """
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
        # All projected rows agree on the canonical dev model identity and the
        # normalized promotion band (MEDIUM), not the raw lower-case audit band.
        assert all(r.dev_model == "anthropic/sonnet/cli" for r in history)
        assert {r.complexity for r in history} == {"MEDIUM"}


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


class TestIndexedDimensions:
    """Issue #1522: indexed columns for ADR-0002 query obligations."""

    def _record_with(
        self,
        *,
        run_id: str = "r1",
        slug: str = "demo",
        milestone: str | None = None,
        github_issue: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        cli: str = "",
        schema_version: int | None = 2,
    ) -> dict:
        rec = _make_record(run_id=run_id, slug=slug)
        if schema_version is not None:
            rec["schema_version"] = schema_version
        else:
            rec.pop("schema_version", None)
        if milestone is not None:
            rec["milestone"] = milestone
        if github_issue is not None:
            rec["task"]["github_issue"] = github_issue
        if provider or model:
            rec["cost"]["agents"] = [
                {
                    "phase": "dev",
                    "provider": provider or "",
                    "model": model or "",
                    "cli": cli,
                }
            ]
        return rec

    def test_indexed_columns_populated_from_record(self, tmp_path: Path) -> None:
        rec = self._record_with(
            milestone="v0.11",
            github_issue=1522,
            provider="anthropic",
            model="opus",
            cli="claude",
            schema_version=2,
        )
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            row = conn.execute(
                "SELECT record_schema_version, milestone, issue_id, dev_model "
                "FROM audit_records WHERE run_id = 'r1'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == 2
        assert row[1] == "v0.11"
        assert row[2] == 1522
        assert row[3] == "anthropic/opus/cli"

    def test_missing_optional_dimensions_store_null(self, tmp_path: Path) -> None:
        rec = self._record_with(schema_version=None)
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            row = conn.execute(
                "SELECT record_schema_version, milestone, issue_id, dev_model "
                "FROM audit_records WHERE run_id = 'r1'"
            ).fetchone()
        finally:
            conn.close()
        # Pre-slice record (no schema_version) is treated as version 1.
        assert row[0] == 1
        assert row[1] is None
        assert row[2] is None
        assert row[3] is None

    def test_github_issue_string_form_coerces_to_int(self, tmp_path: Path) -> None:
        rec = self._record_with(run_id="r2", slug="s2")
        rec["task"]["github_issue"] = "#1234"
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            row = conn.execute("SELECT issue_id FROM audit_records WHERE run_id = 'r2'").fetchone()
        finally:
            conn.close()
        assert row[0] == 1234

    def test_indexes_exist_for_new_dimensions(self, tmp_path: Path) -> None:
        conn = sub.create_or_open(tmp_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_records'"
            ).fetchall()
        finally:
            conn.close()
        names = {r[0] for r in rows}
        assert "idx_audit_records_milestone" in names
        assert "idx_audit_records_issue_id" in names
        assert "idx_audit_records_dev_model" in names
        assert "idx_audit_records_final_phase" in names
        assert "idx_audit_records_outcome" in names

    def test_refusal_economics_groupby_milestone(self, tmp_path: Path) -> None:
        """ADR-0002 §3 example: SUM cost GROUP BY milestone runs without parsing JSON."""
        records = [
            _make_record(run_id="r1", slug="a", cost=1.0),
            _make_record(run_id="r2", slug="b", cost=2.0),
            _make_record(run_id="r3", slug="c", cost=4.0),
        ]
        records[0]["milestone"] = "v0.11"
        records[1]["milestone"] = "v0.11"
        records[2]["milestone"] = "v0.12"
        conn = sub.create_or_open(tmp_path)
        try:
            for rec in records:
                sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            rows = [
                tuple(r)
                for r in conn.execute(
                    "SELECT milestone, SUM(total_cost_usd) FROM audit_records "
                    "WHERE milestone IS NOT NULL GROUP BY milestone ORDER BY milestone"
                ).fetchall()
            ]
        finally:
            conn.close()
        assert rows == [("v0.11", 3.0), ("v0.12", 4.0)]

    def test_dev_model_queryable_by_index(self, tmp_path: Path) -> None:
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(
                conn,
                self._record_with(
                    run_id="r1",
                    provider="anthropic",
                    model="sonnet",
                    cli="claude",
                ),
                provenance="native",
            )
            sub.upsert_run_record(
                conn,
                self._record_with(
                    run_id="r2",
                    provider="openai",
                    model="gpt-5",
                ),
                provenance="native",
            )
            conn.commit()
            rows = [
                tuple(r)
                for r in conn.execute(
                    "SELECT dev_model, COUNT(*) FROM audit_records "
                    "WHERE dev_model IS NOT NULL GROUP BY dev_model ORDER BY dev_model"
                ).fetchall()
            ]
        finally:
            conn.close()
        assert rows == [
            ("anthropic/sonnet/cli", 1),
            ("openai/gpt-5/api", 1),
        ]


class TestRecordLevelVerdict:
    """ADR-0002 §3: verdict at record level, not only per-cycle."""

    def test_record_verdict_populated_from_final_cycle(self, tmp_path: Path) -> None:
        rec = _make_record(
            run_id="r1",
            reviews=[
                {"cycle": 1, "verdict": "REQUEST_CHANGES"},
                {"cycle": 2, "verdict": "APPROVE"},
            ],
        )
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            row = conn.execute("SELECT verdict FROM audit_records WHERE run_id='r1'").fetchone()
        finally:
            conn.close()
        assert row[0] == "APPROVE"

    def test_record_verdict_null_when_no_reviews(self, tmp_path: Path) -> None:
        rec = _make_record(run_id="r1", reviews=[])
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            row = conn.execute("SELECT verdict FROM audit_records WHERE run_id='r1'").fetchone()
        finally:
            conn.close()
        assert row[0] is None

    def test_record_verdict_groupby_serves_from_audit_records(self, tmp_path: Path) -> None:
        """COUNT/GROUP BY verdict runs at record granularity without joining reviews."""
        records = [
            _make_record(
                run_id="r1",
                slug="a",
                reviews=[{"cycle": 1, "verdict": "APPROVE"}],
            ),
            _make_record(
                run_id="r2",
                slug="b",
                reviews=[
                    {"cycle": 1, "verdict": "REQUEST_CHANGES"},
                    {"cycle": 2, "verdict": "APPROVE"},
                ],
            ),
            _make_record(
                run_id="r3",
                slug="c",
                reviews=[{"cycle": 1, "verdict": "REQUEST_CHANGES"}],
            ),
        ]
        conn = sub.create_or_open(tmp_path)
        try:
            for rec in records:
                sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            rows = [
                tuple(r)
                for r in conn.execute(
                    "SELECT verdict, COUNT(*) FROM audit_records "
                    "WHERE verdict IS NOT NULL GROUP BY verdict ORDER BY verdict"
                ).fetchall()
            ]
        finally:
            conn.close()
        assert rows == [("APPROVE", 2), ("REQUEST_CHANGES", 1)]

    def test_record_verdict_index_exists(self, tmp_path: Path) -> None:
        conn = sub.create_or_open(tmp_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_records'"
            ).fetchall()
        finally:
            conn.close()
        names = {r[0] for r in rows}
        assert "idx_audit_records_verdict" in names


class TestRecordSchemaMigration:
    """Issue #1522: reader-side per-record schema-version dispatch seam."""

    def test_pre_slice_record_treated_as_v1(self, tmp_path: Path) -> None:
        rec = _make_record(run_id="r1")
        rec.pop("schema_version", None)
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            row = conn.execute(
                "SELECT record_schema_version FROM audit_records WHERE run_id='r1'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == 1

    def test_migrate_record_is_noop_for_current_version(self) -> None:
        rec = {"schema_version": sub.CURRENT_RECORD_SCHEMA_VERSION, "task": {"slug": "x"}}
        out = sub._migrate_record(rec, from_version=sub.CURRENT_RECORD_SCHEMA_VERSION)
        assert out is rec

    @pytest.mark.parametrize(
        "reader",
        ["iter_records", "tail_records", "iter_escalation_records", "has_review_approve"],
    )
    def test_all_readers_route_through_migration_seam(
        self, tmp_path: Path, monkeypatch, reader: str
    ) -> None:
        """Every reader consults record_schema_version via _migrate_record."""
        rec_v1 = _make_record(
            run_id="r1",
            slug="demo",
            reviews=[{"cycle": 1, "verdict": "APPROVE"}],
        )
        rec_v1.pop("schema_version", None)
        rec_v2 = _make_record(
            run_id="r2",
            slug="demo",
            reviews=[{"cycle": 1, "verdict": "APPROVE"}],
        )
        rec_v2["schema_version"] = 2
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec_v1, provenance="native")
            sub.upsert_run_record(conn, rec_v2, provenance="native")
            conn.commit()
        finally:
            conn.close()

        seen_versions: list[int] = []
        original = sub._migrate_record

        def tracking(record: dict, *, from_version: int) -> dict:
            seen_versions.append(from_version)
            return original(record, from_version=from_version)

        # ``audit_storage`` owns the decoder; the read-model readers reach it
        # through ``_load_migrated``, which resolves its own module global.
        # Patching the ``audit_substrate`` re-export would be a no-op (#2350).
        monkeypatch.setattr(audit_storage, "_migrate_record", tracking)
        conn = sub.create_or_open(tmp_path)
        try:
            if reader == "iter_records":
                list(sub.iter_records(conn))
            elif reader == "tail_records":
                sub.tail_records(conn, 10)
            elif reader == "iter_escalation_records":
                list(sub.iter_escalation_records(conn))
            elif reader == "has_review_approve":
                list(sub.has_review_approve_in_substrate(conn, "demo"))
        finally:
            conn.close()
        assert sorted(seen_versions) == [1, 2]

    def test_iter_records_routes_through_migration(self, tmp_path: Path, monkeypatch) -> None:
        rec_v1 = _make_record(run_id="r1")
        rec_v1.pop("schema_version", None)
        rec_v2 = _make_record(run_id="r2")
        rec_v2["schema_version"] = 2
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec_v1, provenance="native")
            sub.upsert_run_record(conn, rec_v2, provenance="native")
            conn.commit()
        finally:
            conn.close()

        seen_versions: list[int] = []
        original = sub._migrate_record

        def tracking(record: dict, *, from_version: int) -> dict:
            seen_versions.append(from_version)
            return original(record, from_version=from_version)

        # ``audit_storage`` owns the decoder; the read-model readers reach it
        # through ``_load_migrated``, which resolves its own module global.
        # Patching the ``audit_substrate`` re-export would be a no-op (#2350).
        monkeypatch.setattr(audit_storage, "_migrate_record", tracking)
        conn = sub.create_or_open(tmp_path)
        try:
            list(sub.iter_records(conn))
        finally:
            conn.close()
        # Both rows must have been routed through the helper; versions taken
        # from the indexed column rather than re-parsed from raw_json.
        assert sorted(seen_versions) == [1, 2]


class TestRebuildBackfillsNewColumns:
    """Existing audit_records rows pick up the new columns on next rebuild."""

    def test_rebuild_populates_indexed_dimensions(self, tmp_path: Path) -> None:
        rec = _make_record(run_id="r1", slug="demo")
        rec["milestone"] = "v0.11"
        rec["task"]["github_issue"] = 1522
        rec["cost"]["agents"] = [
            {
                "phase": "dev",
                "provider": "anthropic",
                "model": "opus",
                "cli": "claude",
            }
        ]
        rec["schema_version"] = 2
        _write_runs(tmp_path, [rec])
        sub.rebuild_from_runs(tmp_path)
        conn = sqlite3.connect(str(sub.substrate_path(tmp_path)))
        try:
            row = conn.execute(
                "SELECT milestone, issue_id, dev_model, record_schema_version "
                "FROM audit_records WHERE run_id='r1'"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("v0.11", 1522, "anthropic/opus/cli", 2)


class TestRedactionContract:
    """ADR-0002 §1 redaction-contract guarantees for substrate writers."""

    def _make_secret_record(self, run_id: str = "r-redact") -> dict:
        rec = _make_record(run_id=run_id)
        rec["api_key"] = "sk-supersecrettoken-from-env"
        rec["environment"] = {"FOO": "1", "BAR": "2"}
        rec["task"]["notes"] = "leaked: sk-supersecrettoken-from-env"
        return rec

    def test_upsert_redacts_secret_keyed_value(self, tmp_path: Path) -> None:
        """Secret-keyed values are scrubbed even when no env_file is provided."""
        rec = self._make_secret_record()
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            row = conn.execute(
                "SELECT raw_json FROM audit_records WHERE run_id='r-redact'"
            ).fetchone()
        finally:
            conn.close()
        stored = json.loads(row[0])
        assert stored["api_key"] == "[REDACTED]"
        assert stored["environment"] == ["BAR", "FOO"]

    def test_upsert_scrubs_env_file_secret_value(self, tmp_path: Path) -> None:
        """Values present in env_file are scrubbed wherever they appear."""
        env = sub.secrets_env_path(tmp_path)
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text('SECRET="sk-supersecrettoken-from-env"\n', encoding="utf-8")
        rec = self._make_secret_record()
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native", env_file=env)
            conn.commit()
            row = conn.execute(
                "SELECT raw_json FROM audit_records WHERE run_id='r-redact'"
            ).fetchone()
        finally:
            conn.close()
        stored = json.loads(row[0])
        # The secret value must be gone from every string field.
        assert "sk-supersecrettoken-from-env" not in row[0]
        assert "[REDACTED]" in stored["task"]["notes"]

    def test_sprint_rollup_path_redacts_into_substrate(self, tmp_path: Path) -> None:
        """The programmatic sprint-rollup writer must scrub secrets too.

        Regression test for the ADR-0002 §1 redaction-contract gap where
        sprint/audit._upsert_into_substrate() called the substrate without
        first redacting.
        """
        from theforge.sprint import audit as sprint_audit

        env = sub.secrets_env_path(tmp_path)
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text('SECRET="sk-sprint-rollup-secret-xyz"\n', encoding="utf-8")
        record = _make_record(run_id="r-sprint-rollup")
        record["api_key"] = "leaked-via-key"
        record["task"]["notes"] = "embedded sk-sprint-rollup-secret-xyz here"

        sprint_audit._upsert_into_substrate(tmp_path, record)

        conn = sqlite3.connect(str(sub.substrate_path(tmp_path)))
        try:
            row = conn.execute(
                "SELECT raw_json FROM audit_records WHERE run_id='r-sprint-rollup'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert "sk-sprint-rollup-secret-xyz" not in row[0]
        assert "leaked-via-key" not in row[0]
        stored = json.loads(row[0])
        assert stored["api_key"] == "[REDACTED]"

    def test_rebuild_from_runs_does_not_unredact(self, tmp_path: Path) -> None:
        """Rebuilding from per-run JSON yields a substrate with no unredacted secrets.

        Per-run JSON files are written already-redacted by cli/shared.py; this
        test asserts the rebuild path inherits that redaction rather than
        re-introducing raw values (defense-in-depth via the writer-side scrub).
        """
        env = sub.secrets_env_path(tmp_path)
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text('SECRET="rebuild-secret-abc12345"\n', encoding="utf-8")

        # Simulate a per-run JSON file that somehow contains a raw secret.
        # The writer-side redaction inside upsert_run_record should still
        # scrub it during rebuild.
        rec = _make_record(run_id="r-rebuild")
        rec["api_key"] = "rebuild-secret-abc12345"
        rec["task"]["notes"] = "contains rebuild-secret-abc12345"
        _write_runs(tmp_path, [rec])

        sub.rebuild_from_runs(tmp_path)

        conn = sqlite3.connect(str(sub.substrate_path(tmp_path)))
        try:
            row = conn.execute(
                "SELECT raw_json FROM audit_records WHERE run_id='r-rebuild'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert "rebuild-secret-abc12345" not in row[0]


def test_migrate_v12_to_v13_backfills_gate_runs_as_unrecorded() -> None:
    """A pre-v13 record has no honest gate-execution count, so it reads as unknown.

    ``len(gate_decisions)`` is precisely the wrong number ``gate_runs`` exists to
    replace (it drops timeouts and counts skipped gates), so the migration must
    not infer a count from it (#1984).
    """
    record = {
        "schema_version": 12,
        "iterations": {"gate_decisions": ["PASS", "FAIL"], "dev_iterations": 2},
    }

    migrated = sub._migrate_v12_to_v13(record)

    assert migrated["iterations"]["gate_runs"] is None
    assert migrated["iterations"]["gate_decisions"] == ["PASS", "FAIL"]
    assert record["iterations"] == {"gate_decisions": ["PASS", "FAIL"], "dev_iterations": 2}


def test_migrate_v12_to_v13_leaves_an_existing_gate_runs_alone() -> None:
    """Migration never overwrites a count the writer already recorded."""
    record = {"schema_version": 12, "iterations": {"gate_runs": 4}}

    assert sub._migrate_v12_to_v13(record)["iterations"]["gate_runs"] == 4


def test_migrate_v13_to_v14_renames_gate_iteration_to_trace_index() -> None:
    """The gate telemetry counter was always the trace counter, so the rename is faithful.

    A v13 record called it ``iteration`` — the same name ``dev_loop`` uses for a
    counter that resets each review cycle. v14 names it ``trace_index`` and adds
    the trace path it identifies (#1986).
    """
    record = {
        "schema_version": 13,
        "iterations": {
            "gate_debug": [{"iteration": 3, "command": "make gate-debug", "ran": True}],
            "gate_diagnostic": [{"iteration": 3, "command": "pytest -n 0", "ran": True}],
        },
    }

    migrated = sub._migrate_v13_to_v14(record)

    debug = migrated["iterations"]["gate_debug"][0]
    assert "iteration" not in debug
    assert debug["trace_index"] == 3
    assert debug["trace_path"] == ".forge/traces/3-gate-debug.txt"
    assert debug["command"] == "make gate-debug"
    diagnostic = migrated["iterations"]["gate_diagnostic"][0]
    assert diagnostic["trace_index"] == 3
    assert diagnostic["trace_path"] == ".forge/traces/3-gate-diagnostic.txt"
    # Source record untouched (ADR-0002 refusal-to-forget).
    assert record["iterations"]["gate_debug"][0]["iteration"] == 3


def test_migrate_v13_to_v14_leaves_an_already_renamed_entry_alone() -> None:
    """Migration never rewrites an entry the writer already emitted at v14."""
    record = {
        "schema_version": 13,
        "iterations": {
            "gate_debug": [{"trace_index": 7, "trace_path": ".forge/traces/7-gate-debug.txt"}]
        },
    }

    entry = sub._migrate_v13_to_v14(record)["iterations"]["gate_debug"][0]
    assert entry == {"trace_index": 7, "trace_path": ".forge/traces/7-gate-debug.txt"}


def test_migrate_v38_to_v39_leaves_ambiguous_gate_diagnostic_without_alias() -> None:
    """Ambiguous legacy ``ran`` values do not become affirmative evidence."""
    record = {
        "schema_version": 38,
        "iterations": {
            "gate_diagnostic": [{"trace_index": 3, "command": "pytest -n 0", "ran": False}]
        },
    }

    migrated = sub._migrate_v38_to_v39(record)

    diagnostic = migrated["iterations"]["gate_diagnostic"][0]
    assert diagnostic["ran"] is False
    assert "workload_executed" not in diagnostic
    assert "workload_executed" not in record["iterations"]["gate_diagnostic"][0]


def test_migrate_v38_to_v39_leaves_text_only_success_without_alias() -> None:
    """Legacy runner text alone is not enough to infer executed workload."""
    record = {
        "schema_version": 38,
        "iterations": {
            "gate_diagnostic": [
                {
                    "trace_index": 4,
                    "command": "pytest -n 0",
                    "ran": True,
                    "timed_out": False,
                    "hanging_test": None,
                    "output_tail": "captured stderr: helper not found\n500 passed in 30.2s\n",
                }
            ]
        },
    }

    migrated = sub._migrate_v38_to_v39(record)

    diagnostic = migrated["iterations"]["gate_diagnostic"][0]
    assert diagnostic["ran"] is True
    assert "workload_executed" not in diagnostic
    assert "workload_executed" not in record["iterations"]["gate_diagnostic"][0]


def test_migrate_v38_to_v39_backfills_gate_diagnostic_true_when_hanging_test_was_recorded() -> (
    None
):
    """A recorded hanging test is structured evidence that workload executed."""
    record = {
        "schema_version": 38,
        "iterations": {
            "gate_diagnostic": [
                {
                    "trace_index": 5,
                    "command": "pytest -n 0 --timeout=10",
                    "ran": True,
                    "timed_out": False,
                    "hanging_test": "tests/test_hang.py::test_deadlock",
                    "output_tail": "unparseable runner text\n",
                }
            ]
        },
    }

    migrated = sub._migrate_v38_to_v39(record)

    diagnostic = migrated["iterations"]["gate_diagnostic"][0]
    assert diagnostic["ran"] is True
    assert diagnostic["workload_executed"] is True


def test_migrate_v38_to_v39_leaves_argument_rejection_without_alias() -> None:
    """Legacy launcher failures remain inconclusive when only runner text says so."""
    record = {
        "schema_version": 38,
        "iterations": {
            "gate_diagnostic": [
                {
                    "trace_index": 5,
                    "command": "pytest -n 0 --timeout=10",
                    "ran": True,
                    "exit_code": 4,
                    "timed_out": False,
                    "hanging_test": None,
                    "output_tail": (
                        "ERROR: usage: __main__.py [options] [file_or_dir] [file_or_dir] [...]\n"
                        "__main__.py: error: unrecognized arguments: --timeout=10"
                        " --timeout-method=thread\n"
                    ),
                }
            ]
        },
    }

    migrated = sub._migrate_v38_to_v39(record)

    diagnostic = migrated["iterations"]["gate_diagnostic"][0]
    assert diagnostic["ran"] is True
    assert "workload_executed" not in diagnostic


def test_migrate_v39_to_v40_backfills_workspace_setup_timeout_entry() -> None:
    record = {
        "schema_version": 39,
        "configuration": {
            "recorded_values": {
                "entries": {
                    "workspace": {
                        "setup_command": {"value": "pip install -e .", "source": "forge.yaml"}
                    }
                }
            }
        },
    }

    migrated = sub._migrate_v39_to_v40(record)

    workspace = migrated["configuration"]["recorded_values"]["entries"]["workspace"]
    assert workspace["setup_timeout"] == {"value": 120, "source": "default"}
    assert (
        "setup_timeout" not in (record["configuration"]["recorded_values"]["entries"]["workspace"])
    )


def test_migrate_record_chains_up_to_v14() -> None:
    """The registry reaches the current version, so v12 records load migrated."""
    record = {
        "schema_version": 12,
        "iterations": {"gate_decisions": [], "gate_debug": [{"iteration": 1}]},
    }

    migrated = sub._migrate_record(record, from_version=12)

    assert migrated["iterations"]["gate_runs"] is None
    assert migrated["iterations"]["gate_debug"][0]["trace_index"] == 1


class TestRendererIndexerModelIdentitySeam:
    """#2201: the fields the renderer writes are the fields the indexer reads.

    These build the ``cost.agents`` entry with the *real* writer
    (:func:`audit_render._agent_entry`) rather than a hand-shaped dict, so a
    future rename on either side of the seam fails here instead of silently
    emptying the ``dev_model`` column on every indexed record.
    """

    def _agent_result(self, **overrides) -> object:
        from theforge.agent_types import AgentResult

        kwargs = {
            "success": True,
            "output": "done",
            "session_id": None,
            "cost_usd": 1.0,
            "exit_code": 0,
            "raw": {},
            "profile_name": "dev",
        }
        kwargs.update(overrides)
        return AgentResult(**kwargs)

    def _record_with_agent_entry(self, entry: dict, run_id: str = "seam-1") -> dict:
        rec = _make_record(run_id=run_id)
        rec["cost"]["agents"] = [entry]
        return rec

    def _index(self, tmp_path: Path, record: dict) -> tuple:
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, record, provenance="native")
            conn.commit()
            return tuple(
                conn.execute(
                    "SELECT dev_model, dev_model_source FROM audit_records WHERE run_id = ?",
                    (record["run_id"],),
                ).fetchone()
            )
        finally:
            conn.close()

    def test_model_used_from_renderer_indexes_as_direct_identity(self, tmp_path: Path) -> None:
        from theforge.coordinator import audit_render

        entry = audit_render._agent_entry(
            self._agent_result(model_used="sonnet"), "dev", "dev", 12.0
        )
        assert "model_used" in entry, "renderer no longer emits the key the indexer reads"

        assert self._index(tmp_path, self._record_with_agent_entry(entry)) == (
            "anthropic/sonnet/cli",
            "direct",
        )

    def _index_detail(self, tmp_path: Path, record: dict) -> tuple:
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, record, provenance="native")
            conn.commit()
            return tuple(
                conn.execute(
                    "SELECT dev_model, dev_model_source, dev_model_resolution "
                    "FROM audit_records WHERE run_id = ?",
                    (record["run_id"],),
                ).fetchone()
            )
        finally:
            conn.close()

    def test_unresolvable_model_used_is_recorded_verbatim_and_marked_unresolved(
        self, tmp_path: Path
    ) -> None:
        """An identity the registry cannot canonicalize is still what ran (#2225).

        Keeping it verbatim is correct; presenting it in the same form as a
        canonical id is what made the fragmentation undetectable, so the row
        also records that it was never normalized.
        """
        from theforge.coordinator import audit_render

        entry = audit_render._agent_entry(
            self._agent_result(model_used="brand-new-model-9"), "dev", "dev", 12.0
        )

        assert self._index_detail(tmp_path, self._record_with_agent_entry(entry)) == (
            "brand-new-model-9",
            "direct",
            "unresolved",
        )

    def test_canonicalized_identity_is_marked_canonical(self, tmp_path: Path) -> None:
        from theforge.coordinator import audit_render

        entry = audit_render._agent_entry(
            self._agent_result(model_used="claude-sonnet-4-6"), "dev", "dev", 12.0
        )

        # #2226: the served version resolves to its OWN pinned identity, never
        # onto the ``anthropic/sonnet/cli`` shorthand that selected it.
        assert self._index_detail(tmp_path, self._record_with_agent_entry(entry)) == (
            "anthropic/claude-sonnet-4-6/cli",
            "direct",
            "canonical",
        )

    def test_production_shaped_api_result_indexes_as_the_api_identity(
        self, tmp_path: Path
    ) -> None:
        """The whole runner → renderer → indexer path for a single-model API profile.

        This is the shape that fragmented in production: an API result carries
        no ``model_used`` at all, so the renderer back-fills it from per-model
        billing and gets the bare ``gpt-5.4``. The runner's recorded transport
        is the only thing that keeps it from indexing under its own spelling
        (#2225), so the runner is driven for real here rather than the entry
        being hand-shaped.
        """
        from theforge.agent_types import ModelUsage
        from theforge.coordinator import audit_render
        from theforge.runners.api import run_api_agent

        api_profile = ModelProfile(
            name="dev",
            provider="openai",
            cli=None,
            model="gpt-5.4",
            budget_usd=1.0,
            timeout_seconds=60,
            allowed_tools=(),
        )
        billed_only = self._agent_result(
            model_usage=(
                ModelUsage(
                    model="gpt-5.4",
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    cost_usd=1.0,
                ),
            )
        )
        with patch.dict(
            "theforge.runners.api.PROVIDER_RUNNERS",
            {"openai": lambda prompt, prof, secrets: billed_only},
        ):
            result = run_api_agent(
                prompt="go", profile=api_profile, working_dir=tmp_path, quiet=True
            )

        entry = audit_render._agent_entry(result, "dev", "dev", 12.0)
        assert entry["model_used"] == "gpt-5.4", "renderer no longer back-fills from billing"

        assert self._index_detail(tmp_path, self._record_with_agent_entry(entry)) == (
            "openai/gpt-5.4/api",
            "direct",
            "canonical",
        )

    def test_transport_used_disambiguates_a_bare_model_name(self, tmp_path: Path) -> None:
        """``gpt-5.4`` exists over both transports; the recorded one resolves it."""
        from theforge.coordinator import audit_render

        entry = audit_render._agent_entry(
            self._agent_result(model_used="gpt-5.4", transport_used="api"), "dev", "dev", 12.0
        )
        assert entry["transport_used"] == "api", "renderer dropped the disambiguating hint"

        assert self._index_detail(tmp_path, self._record_with_agent_entry(entry)) == (
            "openai/gpt-5.4/api",
            "direct",
            "canonical",
        )

    def test_bare_name_without_a_transport_hint_stays_unresolved(self, tmp_path: Path) -> None:
        """No hint and two registry transports → guessing would be worse than verbatim."""
        from theforge.coordinator import audit_render

        entry = audit_render._agent_entry(
            self._agent_result(model_used="gpt-5.4"), "dev", "dev", 12.0
        )

        assert self._index_detail(tmp_path, self._record_with_agent_entry(entry)) == (
            "gpt-5.4",
            "direct",
            "unresolved",
        )

    def test_alias_spellings_agree_but_a_served_version_stays_distinct(
        self, tmp_path: Path
    ) -> None:
        """#2225 de-fragmentation holds for alias spellings; #2226 splits versions off.

        ``anthropic/sonnet/cli`` and ``sonnet`` are two spellings of one subject
        and must index together. ``claude-sonnet-4-6`` is a different subject —
        one specific model — and indexing it with them is what let an alias's
        history describe two models at once.
        """
        conn = sub.create_or_open(tmp_path)
        try:
            for i, spelling in enumerate(("anthropic/sonnet/cli", "sonnet", "claude-sonnet-4-6")):
                rec = _make_record(run_id=f"frag-{i}")
                rec["cost"]["agents"] = [{"role": "dev", "model_used": spelling}]
                sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            rows = conn.execute(
                "SELECT dev_model, COUNT(*) FROM audit_records "
                "WHERE run_id LIKE 'frag-%' GROUP BY dev_model ORDER BY dev_model"
            ).fetchall()
        finally:
            conn.close()

        assert [tuple(r) for r in rows] == [
            ("anthropic/claude-sonnet-4-6/cli", 1),
            ("anthropic/sonnet/cli", 2),
        ]

    def test_model_config_only_entry_indexes_as_recovered_identity(self, tmp_path: Path) -> None:
        """No recorded model_used → reconstruct from invocation config, marked recovered."""
        from theforge.coordinator import audit_render

        entry = audit_render._agent_entry(
            self._agent_result(model_config=("opus", "sonnet")), "dev", "dev", 12.0
        )
        assert "model_used" not in entry

        assert self._index(tmp_path, self._record_with_agent_entry(entry)) == (
            "anthropic/opus/cli",
            "recovered",
        )

    def test_component_billing_is_not_treated_as_invocation_identity(self, tmp_path: Path) -> None:
        """A model billed inside an invocation is not the model that was invoked.

        ``_agent_entry`` back-fills ``model_used`` from ``model_usage`` itself,
        so the indexer must not reach into per-component billing on its own —
        an entry carrying only ``model_usage`` has no invocation identity.
        """
        entry = {
            "role": "dev",
            "profile": "dev",
            "cost_usd": 1.0,
            "duration_seconds": 12.0,
            "success": True,
            "exit_code": 0,
            "model_usage": [{"model": "claude-haiku-4-5", "cost_usd": 0.01}],
        }

        assert self._index(tmp_path, self._record_with_agent_entry(entry)) == (None, None)

    def test_direct_identity_wins_over_a_recovered_one(self, tmp_path: Path) -> None:
        """A later dev attempt that recorded model_used is not reported as recovered."""
        rec = _make_record(run_id="seam-multi")
        rec["cost"]["agents"] = [
            {"role": "dev", "model_config": ["opus", "sonnet"]},
            {"role": "dev", "model_used": "sonnet"},
        ]

        assert self._index(tmp_path, rec) == ("anthropic/sonnet/cli", "direct")

    def test_legacy_identity_keys_still_index_as_recovered(self, tmp_path: Path) -> None:
        """Older records carry provider/model/cli; keep reading them, marked recovered."""
        rec = _make_record(run_id="seam-legacy")
        rec["cost"]["agents"] = [
            {"phase": "dev", "provider": "openai", "model": "gpt-5", "cli": ""}
        ]

        assert self._index(tmp_path, rec) == ("openai/gpt-5/api", "recovered")

    def test_escalation_projection_uses_the_same_identity(self, tmp_path: Path) -> None:
        """The adaptive-routing view reads the renderer's keys, not the stale ones."""
        from theforge.coordinator import audit_render

        entry = audit_render._agent_entry(
            self._agent_result(model_used="sonnet"), "dev", "dev", 12.0
        )
        rec = self._record_with_agent_entry(entry, run_id="esc-seam")
        rec["outcome"] = {"success": False, "final_phase": "ESCALATE"}
        rec["preflight"] = {"complexity": "medium", "complexity_score": 5}

        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            conn.commit()
            projected = list(sub.iter_escalation_records(conn))
        finally:
            conn.close()

        assert [p["dev_model"] for p in projected] == ["anthropic/sonnet/cli"]

    def test_reindex_repairs_rows_indexed_under_the_broken_projection(
        self, tmp_path: Path
    ) -> None:
        """Opening a pre-#2201 substrate re-derives dev_model from raw_json.

        Simulates the observed failure: rows present, identity in the record,
        column empty. The substrate schema version is what marks them stale.
        """
        from theforge.coordinator import audit_render

        entry = audit_render._agent_entry(
            self._agent_result(model_used="sonnet"), "dev", "dev", 12.0
        )
        rec = self._record_with_agent_entry(entry, run_id="stale-1")
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            conn.execute(
                "UPDATE audit_records SET dev_model = NULL, dev_model_source = NULL "
                "WHERE run_id = 'stale-1'"
            )
            conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
            conn.commit()
        finally:
            conn.close()

        conn = sub.create_or_open(tmp_path)
        try:
            row = tuple(
                conn.execute(
                    "SELECT dev_model, dev_model_source FROM audit_records WHERE run_id='stale-1'"
                ).fetchone()
            )
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert row == ("anthropic/sonnet/cli", "direct")
        assert version == str(sub.SUBSTRATE_SCHEMA_VERSION)

    def test_reindex_recanonicalizes_a_schema_5_substrate(self, tmp_path: Path) -> None:
        """A version-5 row holds the runner's spelling; opening it normalizes (#2225).

        Under #2226 the normalized target is the served version's own pinned
        identity rather than the family shorthand.
        """
        rec = _make_record(run_id="v5-1")
        rec["cost"]["agents"] = [{"role": "dev", "model_used": "claude-sonnet-4-6"}]
        conn = sub.create_or_open(tmp_path)
        try:
            sub.upsert_run_record(conn, rec, provenance="native")
            conn.execute(
                "UPDATE audit_records SET dev_model = 'claude-sonnet-4-6', "
                "dev_model_source = 'direct', dev_model_resolution = NULL "
                "WHERE run_id = 'v5-1'"
            )
            conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
            conn.commit()
        finally:
            conn.close()

        conn = sub.create_or_open(tmp_path)
        try:
            row = tuple(
                conn.execute(
                    "SELECT dev_model, dev_model_source, dev_model_resolution "
                    "FROM audit_records WHERE run_id='v5-1'"
                ).fetchone()
            )
        finally:
            conn.close()

        assert row == ("anthropic/claude-sonnet-4-6/cli", "direct", "canonical")
