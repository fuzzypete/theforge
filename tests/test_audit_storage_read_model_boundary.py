"""The storage / read-model ownership boundary in the audit substrate (#2350).

``audit_substrate`` used to hold four change-reasons — schema definition, schema
migration, record persistence, and analytics — behind one ownership. It is now a
re-export facade over two owners:

- ``audit_storage``    — connection, schema, migrations, record writes
- ``audit_read_model`` — SELECT queries and the derivations over them

These tests pin the *ownership*, not the file sizes. The property that matters
is that a new analytical query and a new record migration are independent
changes: neither module has to be opened to make the other kind of change. The
behavioural equivalence of the moved queries is covered by
``test_audit_substrate.py``, which continues to exercise them through the
facade.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

from theforge.coordinator import audit_read_model, audit_storage
from theforge.coordinator import audit_substrate as sub


def _make_record(
    *,
    run_id: str,
    slug: str = "demo",
    started_at: str = "2026-03-01T10:00:00+00:00",
    success: bool = True,
    cost: float = 1.23,
    reviews: list[dict] | None = None,
) -> dict:
    return {
        "run_id": run_id,
        "task": {"slug": slug, "name": slug},
        "outcome": {"success": success, "final_phase": "DONE"},
        "timing": {"started_at": started_at, "duration_seconds": 60.0},
        "cost": {"total_usd": cost},
        "totals": {"cost_usd": cost, "duration_s": 60.0},
        "iterations": {"dev_iterations": 1, "review_cycles": 1},
        "reviews": reviews or [],
        "phases": {"dev": {"cost_usd": cost, "duration_s": 60.0, "outcome": "success"}},
    }


class TestReadModelOwnsNoStorage:
    """The read model may query. It may not define, migrate, or write."""

    def test_read_model_declares_no_schema(self) -> None:
        """No DDL in the read model.

        A new analytical query must not be able to arrive alongside a silent
        table or column change — that is the coupling the split removes.
        """
        source = Path(inspect.getfile(audit_read_model)).read_text()
        # Strip docstrings/comments: the module documents the boundary in prose
        # and would otherwise match its own description of what it excludes.
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        code = re.sub(r'"""[\s\S]*?"""', "", code)
        for statement in ("CREATE TABLE", "ALTER TABLE", "CREATE INDEX", "DROP TABLE"):
            assert statement not in code.upper(), (
                f"{statement} found in audit_read_model — schema is audit_storage's"
            )

    def test_read_model_defines_no_migrations(self) -> None:
        """The ``_migrate_*`` catalogue belongs to storage, whole."""
        names = dir(audit_read_model)
        assert not [n for n in names if n.startswith("_migrate_v")]
        assert "MIGRATION_HELPERS" not in vars(audit_read_model)
        assert "_migrate_record" not in vars(audit_read_model)

    def test_read_model_issues_no_writes(self) -> None:
        """No INSERT/UPDATE/DELETE, and no writer functions."""
        source = Path(inspect.getfile(audit_read_model)).read_text()
        code = re.sub(r'"""[\s\S]*?"""', "", source)
        code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
        for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
            assert statement not in code.upper(), (
                f"{statement} found in audit_read_model — writes are audit_storage's"
            )
        assert not [n for n in vars(audit_read_model) if n.startswith(("record_", "upsert_"))]

    def test_read_model_opens_no_connections_of_its_own(self) -> None:
        """Connections come from storage; the read model never calls sqlite3.connect."""
        tree = ast.parse(Path(inspect.getfile(audit_read_model)).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "connect":
                pytest.fail("audit_read_model calls .connect(); opening is storage's")

    def test_storage_does_not_import_the_read_model(self) -> None:
        """The dependency runs one way, which is what makes the split real.

        If storage imported the read model the two would be mutually
        dependent and neither could change without the other.
        """
        tree = ast.parse(Path(inspect.getfile(audit_storage)).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module is None or "audit_read_model" not in node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "audit_read_model" not in alias.name


class TestNamedInterface:
    """``AuditConnection`` is the named boundary between the two modules."""

    def test_storage_publishes_the_named_interface(self) -> None:
        assert hasattr(audit_storage, "AuditConnection")
        assert sub.AuditConnection is audit_storage.AuditConnection

    def test_read_model_consumes_it(self, tmp_path: Path) -> None:
        """A storage-opened connection is what the readers accept."""
        conn = audit_storage.create_or_open(tmp_path)
        try:
            audit_storage.upsert_run_record(conn, _make_record(run_id="r1"), provenance="native")
            conn.commit()
            assert isinstance(conn, audit_storage.AuditConnection)
            assert audit_read_model.count_records(conn) == 1
        finally:
            conn.close()

    def test_readers_are_annotated_against_the_interface(self) -> None:
        """Public readers name ``AuditConnection``, not a bare sqlite3 handle.

        The annotation is how the contract is discoverable at a call site:
        "a connection storage opened and validated", not "any database".
        """
        tree = ast.parse(Path(inspect.getfile(audit_read_model)).read_text())
        annotated = 0
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.arg == "conn":
                    assert arg.annotation is not None
                    assert ast.unparse(arg.annotation) == "AuditConnection", (
                        f"{node.name} does not take an AuditConnection"
                    )
                    annotated += 1
        assert annotated > 10, "expected the read model's queries to be annotated"


class TestNewQueryNeedsNoSchemaChange:
    """AC3: a new analytical query lands in the read model alone."""

    def test_verdict_outcome_counts_is_read_model_owned(self) -> None:
        assert "verdict_outcome_counts" in vars(audit_read_model)
        assert "verdict_outcome_counts" not in vars(audit_storage)
        # and it is reachable through the compatibility facade
        assert sub.verdict_outcome_counts is audit_read_model.verdict_outcome_counts

    def test_verdict_outcome_counts_groups_over_existing_columns(self, tmp_path: Path) -> None:
        """The query works against a substrate at the *current* schema version.

        No bump, no migration entry — it reads columns ``audit_records``
        already indexes. That is the demonstration the AC asks for.
        """
        before = audit_storage.SUBSTRATE_SCHEMA_VERSION
        conn = audit_storage.create_or_open(tmp_path)
        try:
            approve = [{"cycle": 1, "verdict": "APPROVE"}]
            reject = [{"cycle": 1, "verdict": "REJECT"}]
            audit_storage.upsert_run_record(
                conn, _make_record(run_id="a1", reviews=approve, cost=1.0), provenance="native"
            )
            audit_storage.upsert_run_record(
                conn, _make_record(run_id="a2", reviews=approve, cost=2.0), provenance="native"
            )
            audit_storage.upsert_run_record(
                conn,
                _make_record(run_id="r1", reviews=reject, success=False, cost=4.0),
                provenance="native",
            )
            conn.commit()

            rows = audit_read_model.verdict_outcome_counts(conn)
            by_verdict = {row["verdict"]: row for row in rows}

            assert by_verdict["APPROVE"]["runs"] == 2
            assert by_verdict["APPROVE"]["outcome_success"] is True
            assert by_verdict["APPROVE"]["total_cost_usd"] == pytest.approx(3.0)
            assert by_verdict["REJECT"]["runs"] == 1
            assert by_verdict["REJECT"]["outcome_success"] is False
            assert by_verdict["REJECT"]["total_cost_usd"] == pytest.approx(4.0)
        finally:
            conn.close()
        assert audit_storage.SUBSTRATE_SCHEMA_VERSION == before

    def test_null_verdict_is_reported_not_dropped(self, tmp_path: Path) -> None:
        """ "We never recorded a verdict" is an answer, not a row to discard."""
        conn = audit_storage.create_or_open(tmp_path)
        try:
            audit_storage.upsert_run_record(conn, _make_record(run_id="n1"), provenance="native")
            conn.commit()
            rows = audit_read_model.verdict_outcome_counts(conn)
        finally:
            conn.close()
        assert [row["verdict"] for row in rows] == [None]
        assert rows[0]["runs"] == 1

    def test_empty_substrate_yields_no_groups(self, tmp_path: Path) -> None:
        conn = audit_storage.create_or_open(tmp_path)
        try:
            assert audit_read_model.verdict_outcome_counts(conn) == []
        finally:
            conn.close()


class TestMigrationCatalogueIntact:
    """AC2: the catalogue is one sequential unit and was not divided."""

    def test_every_migration_lives_in_storage(self) -> None:
        migrations = [n for n in dir(audit_storage) if n.startswith("_migrate_v")]
        assert len(migrations) == audit_storage.CURRENT_RECORD_SCHEMA_VERSION - 1
        for version in range(1, audit_storage.CURRENT_RECORD_SCHEMA_VERSION):
            assert hasattr(audit_storage, f"_migrate_v{version}_to_v{version + 1}")

    def test_catalogue_is_contiguous_and_in_order(self) -> None:
        """Defined consecutively, ascending, with nothing interleaved.

        The catalogue is meant to be read top to bottom. A split that
        scattered it — or that dropped an unrelated function into the middle
        of it — would make it harder to read while making a line count look
        better, which is the failure mode this AC exists to prevent.
        """
        tree = ast.parse(Path(inspect.getfile(audit_storage)).read_text())
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        indices = [i for i, name in enumerate(names) if name.startswith("_migrate_v")]
        assert indices == list(range(indices[0], indices[-1] + 1)), (
            "the migration catalogue has been interrupted by other functions"
        )
        versions = [int(names[i].removeprefix("_migrate_v").split("_")[0]) for i in indices]
        assert versions == list(range(1, audit_storage.CURRENT_RECORD_SCHEMA_VERSION))

    def test_registry_covers_the_catalogue(self) -> None:
        helpers = audit_storage.MIGRATION_HELPERS
        assert sorted(helpers) == list(range(1, audit_storage.CURRENT_RECORD_SCHEMA_VERSION))
        for version, helper in helpers.items():
            assert helper is getattr(audit_storage, f"_migrate_v{version}_to_v{version + 1}")


class TestFacadeCompatibility:
    """Every name the old module exported is still importable from it."""

    @pytest.mark.parametrize(
        "name",
        [
            # constants and errors
            "SUBSTRATE_SCHEMA_VERSION",
            "CURRENT_RECORD_SCHEMA_VERSION",
            "SUBSTRATE_RELPATH",
            "AUDIT_PATH_REGISTRY",
            "AuditPathInfo",
            "SubstrateError",
            "SubstrateMissingError",
            "SubstrateCorruptError",
            # storage
            "create_or_open",
            "require_substrate",
            "open_readonly",
            "upsert_run_record",
            "rebuild_from_runs",
            "import_history_jsonl",
            "seed_records",
            "record_readiness_event",
            "record_shape_skip_event",
            "MIGRATION_HELPERS",
            # read model
            "iter_records",
            "tail_records",
            "count_records",
            "runs_touching_path",
            "derive_observed_cost_cohorts",
            "derive_assignment_history",
            "iter_escalation_records",
            "repeated_shape_skip_blocks",
            "inline_remediation_rollup_by_milestone",
        ],
    )
    def test_public_name_still_reachable(self, name: str) -> None:
        assert hasattr(sub, name)

    @pytest.mark.parametrize(
        "name",
        # Underscore-prefixed names other modules and tests import today. They
        # are private to the substrate, not to the process, and dropping them
        # from the facade would break callers silently at import time —
        # e.g. cli/audits.py imports _meta_set.
        ["_meta_set", "_meta_get", "_migrate_record", "_load_migrated", "_open_validated"],
    )
    def test_private_name_still_reachable(self, name: str) -> None:
        assert hasattr(sub, name)

    def test_facade_re_exports_rather_than_redefines(self) -> None:
        """The facade must not carry a second copy of anything.

        A re-export keeps one implementation; a copy would let the two drift.
        """
        assert sub.upsert_run_record is audit_storage.upsert_run_record
        assert sub.MIGRATION_HELPERS is audit_storage.MIGRATION_HELPERS
        assert sub._migrate_record is audit_storage._migrate_record
        assert sub.iter_records is audit_read_model.iter_records
        assert sub.count_records is audit_read_model.count_records

    def test_facade_defines_no_logic_of_its_own(self) -> None:
        """Nothing but imports and ``__all__`` lives in the facade."""
        tree = ast.parse(Path(inspect.getfile(sub)).read_text())
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.Expr)):
                continue
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                assert targets == ["__all__"], f"unexpected assignment {targets}"
                continue
            pytest.fail(f"audit_substrate should hold no {type(node).__name__}")


class TestRebuildStillWorksAcrossRecordVersions:
    """AC5: rebuild covers records written before this change.

    The real corpus spans record schema versions 9 through 28; this pins the
    same property on a fixture set so it holds without one.
    """

    def test_rebuild_migrates_records_from_older_versions(self, tmp_path: Path) -> None:
        runs = tmp_path / ".forge" / "audits" / "runs"
        runs.mkdir(parents=True)
        # One record per historical version, including unversioned (read as v1).
        versions = [None, 1, 5, 12, 20, audit_storage.CURRENT_RECORD_SCHEMA_VERSION]
        for idx, version in enumerate(versions):
            record = _make_record(
                run_id=f"run-{idx:03d}",
                slug=f"story-{idx}",
                started_at=f"2026-03-{idx + 1:02d}T10:00:00+00:00",
                reviews=[{"cycle": 1, "verdict": "APPROVE"}],
            )
            if version is not None:
                record["schema_version"] = version
            (runs / f"run-{idx:03d}.json").write_text(json.dumps(record))

        summary = audit_storage.rebuild_from_runs(tmp_path)

        assert summary.runs_seen == len(versions)
        assert summary.imported == len(versions)
        assert summary.failed == 0

        conn = audit_storage.require_substrate(tmp_path)
        try:
            # Read back through the read model: every record, whatever version
            # it was written at, is decoded through storage's migration chain.
            records = list(audit_read_model.iter_records(conn))
            assert len(records) == len(versions)
            assert audit_read_model.count_records(conn) == len(versions)
            counts = audit_read_model.verdict_outcome_counts(conn)
            assert sum(row["runs"] for row in counts) == len(versions)
        finally:
            conn.close()

    def test_rebuild_is_reachable_through_the_facade(self, tmp_path: Path) -> None:
        """Callers that still import from ``audit_substrate`` keep working."""
        runs = tmp_path / ".forge" / "audits" / "runs"
        runs.mkdir(parents=True)
        (runs / "run-001.json").write_text(json.dumps(_make_record(run_id="run-001")))

        summary = sub.rebuild_from_runs(tmp_path)

        assert summary.imported == 1
        conn = sub.require_substrate(tmp_path)
        try:
            assert sub.count_records(conn) == 1
        finally:
            conn.close()


class TestPriorRunRenderedSizeMigration:
    def test_legacy_included_prior_run_entries_are_marked_unmeasured(self) -> None:
        record = {
            "context_manifests": [
                {
                    "phase": "dev",
                    "prior_run_context": {
                        "enabled": True,
                        "index_state": "ready",
                        "included": [{"run_id": "prior-1", "reason": "file_overlap", "score": 14}],
                        "dropped": [],
                        "note": "1 prior summaries included",
                    },
                }
            ]
        }

        migrated = audit_storage._migrate_v43_to_v44(record)

        assert migrated["context_manifests"][0]["prior_run_context"]["included"][0][
            "rendered_size"
        ] == {
            "value": None,
            "unit": "tokens",
            "method": "cl100k_base",
            "kind": "rendered_prompt_contribution",
            "unavailable_reason": "unmeasured_legacy_record",
        }

    def test_legacy_run_without_included_summaries_gains_no_rendered_size(self) -> None:
        record = {
            "context_manifests": [
                {
                    "phase": "dev",
                    "prior_run_context": {
                        "enabled": True,
                        "index_state": "ready",
                        "included": [],
                        "dropped": [{"run_id": "prior-1", "reason": "budget_pressure"}],
                        "note": "1 relevant summaries dropped under budget pressure",
                    },
                }
            ]
        }

        migrated = audit_storage._migrate_v43_to_v44(record)

        assert migrated == record
