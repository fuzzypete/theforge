"""Writer-side schema-drift guard for the per-run audit record.

See ADR-0002 §"Schema versioning is load-bearing". The substrate's reader-side
lazy migration only holds if writers bump ``SCHEMA_VERSION`` whenever the
record's serialized shape changes. This test snapshots the field set + types
the writer produces and fails when that drifts without a corresponding
``SCHEMA_VERSION`` bump and ``MIGRATION_HELPERS`` entry.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator import audit as audit_writer
from theforge.coordinator import audit_substrate
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.audit_substrate import (
    CURRENT_RECORD_SCHEMA_VERSION as SCHEMA_VERSION,
)
from theforge.coordinator.audit_substrate import MIGRATION_HELPERS
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.runners import AgentResult
from theforge.task import TaskStory

FIXTURES = Path(__file__).parent / "fixtures"


def _collect_schema(value: object, path: str = "") -> dict[str, str]:
    """Walk a record and return ``{path: type_name}`` for every node.

    Nested dicts recurse with dotted paths. Lists are recorded as ``"list"``
    without recursing into elements (element shapes vary by phase outcome
    and are not part of the stable per-record schema this guard pins).
    """
    out: dict[str, str] = {}
    key = path or "<root>"
    if isinstance(value, dict):
        out[key] = "dict"
        for k, v in value.items():
            child_path = f"{path}.{k}" if path else k
            out.update(_collect_schema(v, child_path))
    elif isinstance(value, list):
        out[key] = "list"
    elif isinstance(value, bool):
        out[key] = "bool"
    else:
        out[key] = type(value).__name__
    return out


def _make_config(tmp_path: Path) -> ForgeConfig:
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# spec", encoding="utf-8")
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(gate_command="make gate"),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


def _build_canonical_record(tmp_path: Path) -> dict:
    """Generate an audit record from a minimal-but-deterministic state."""
    config = _make_config(tmp_path)
    task = TaskStory(name="Test", slug="test", story_path=tmp_path / "spec.md")
    state = CoordinatorState()
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.run_id = "deadbeefcafe"
    result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")
    return generate_audit_log(config, task, result)


def _killed(profile_name: str) -> AgentResult:
    """An AgentResult whose cost is unmeasured (None) — e.g. killed at timeout."""
    return AgentResult(
        success=False,
        output="TIMEOUT: Agent exceeded limit",
        session_id=None,
        cost_usd=None,
        exit_code=-9,
        raw={},
        profile_name=profile_name,
    )


def _build_unmeasured_record(tmp_path: Path) -> dict:
    """Generate an audit record where every phase cost is unmeasured (None).

    Exercises the kill-path shape the timeout-cost fix introduced: cost fields
    that are ``float`` in a measured run become ``null`` here. Pins that shape so
    future drift on the nullable path is caught, and feeds the substrate
    round-trip test.
    """
    config = _make_config(tmp_path)
    task = TaskStory(name="Test", slug="test", story_path=tmp_path / "spec.md")
    state = CoordinatorState()
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.run_id = "deadbeefcafe"
    # preflight, plan, plan_review, dev, review — each killed with unmeasured cost.
    state.preflight_verdict = "PROCEED"
    state.preflight_result = _killed("preflight")
    state.plan_results.append(_killed("planner"))
    state.plan_durations.append(5.0)
    state.plan_review_decision = "APPROVE"
    state.plan_review_results.append(_killed("plan-reviewer"))
    state.plan_review_durations.append(5.0)
    state.dev_results.append(_killed("dev"))
    state.dev_durations.append(5.0)
    state.review_agent_results.append(_killed("reviewer"))
    state.review_durations.append(5.0)
    result = CoordinatorResult(success=False, phase=Phase.DONE, state=state, message="killed")
    return generate_audit_log(config, task, result)


def _snapshot_path(version: int) -> Path:
    return FIXTURES / f"audit_record_schema_v{version}.json"


def test_audit_record_schema_unchanged(tmp_path: Path) -> None:
    """Writer-side guard: per-run audit record shape must match the snapshot.

    See ADR-0002 §"Schema versioning is load-bearing". Reader-side lazy
    migration depends on every shape change being paired with a
    ``SCHEMA_VERSION`` bump and a new ``MIGRATION_HELPERS`` entry.
    """
    record = _build_canonical_record(tmp_path)
    actual = _collect_schema(record)

    snap_path = _snapshot_path(SCHEMA_VERSION)

    if os.environ.get("REGENERATE_AUDIT_SCHEMA_SNAPSHOT"):
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"regenerated snapshot at {snap_path}")

    if not snap_path.exists():
        pytest.fail(
            f"No snapshot for schema_version={SCHEMA_VERSION}. "
            f"Run with REGENERATE_AUDIT_SCHEMA_SNAPSHOT=1 to create "
            f"{snap_path.relative_to(Path(__file__).parent.parent)}, then commit it."
        )

    expected = json.loads(snap_path.read_text(encoding="utf-8"))

    if actual == expected:
        return

    actual_keys = set(actual)
    expected_keys = set(expected)
    added = sorted(actual_keys - expected_keys)
    removed = sorted(expected_keys - actual_keys)
    common = actual_keys & expected_keys
    type_changed = sorted(
        f"{k}: {expected[k]!r} -> {actual[k]!r}" for k in common if expected[k] != actual[k]
    )

    diff_lines = []
    for path in added:
        diff_lines.append(f"  + {path} ({actual[path]})")
    for path in removed:
        diff_lines.append(f"  - {path} ({expected[path]})")
    for line in type_changed:
        diff_lines.append(f"  ~ {line}")

    pytest.fail(
        "Field set for schema_version={ver} has drifted:\n{diff}\n\n"
        "If this change is intentional:\n"
        "  1. Bump SCHEMA_VERSION in src/theforge/coordinator/audit.py "
        "(and CURRENT_RECORD_SCHEMA_VERSION in audit_substrate.py)\n"
        "  2. Add MIGRATION_HELPERS[{ver}] = _migrate_v{ver}_to_v{nxt} that "
        "translates the old shape to the new one\n"
        "  3. Regenerate the snapshot: "
        "REGENERATE_AUDIT_SCHEMA_SNAPSHOT=1 .venv/bin/python -m pytest "
        "tests/test_audit_schema_guard.py::test_audit_record_schema_unchanged\n"
        "  4. Commit tests/fixtures/audit_record_schema_v{nxt}.json\n"
        '  5. See ADR-0002 §"Schema versioning is load-bearing"\n\n'
        "If unintentional, revert the field change.".format(
            ver=SCHEMA_VERSION,
            nxt=SCHEMA_VERSION + 1,
            diff="\n".join(diff_lines) or "  (no field-level diff; types-only change)",
        )
    )


def test_unmeasured_cost_fields_serialize_null_under_same_schema_version(
    tmp_path: Path,
) -> None:
    """Kill-path cost fields serialize null, and that is an intentional widening.

    The timeout-cost fix made every audit cost field ``float | None`` — a
    measured run records a number, an unmeasured (killed) run records null.
    This is a backward-compatible value-domain widening, NOT a breaking field
    rename/removal, so it does not bump the record schema version: old records
    (all-numeric) still read, and the reader tolerates null (verified by the
    round-trip test below). The paths asserted here are the exact ones the
    canonical measured record types as ``float`` — pinning the polymorphism so a
    future accidental re-coercion to 0.0 (or a new cost field that forgets to
    preserve None) is caught.
    """
    record = _build_unmeasured_record(tmp_path)

    # Same record version as the measured canonical record — no bump.
    assert record["schema_version"] == SCHEMA_VERSION

    null_cost_paths = [
        record["phases"]["preflight"]["cost_usd"],
        record["phases"]["plan"]["cost_usd"],
        record["phases"]["plan_review"]["cost_usd"],
        record["phases"]["dev"]["cost_usd"],
        record["phases"]["review"]["cost_usd"],
        record["phases"]["review"]["per_reviewer"]["reviewer"]["cost"],
        record["plan_review"]["cost_usd"],
        record["totals"]["cost_usd"],
        record["cost"]["total_usd"],
        record["cost"]["dev_usd"],
        record["cost"]["review_usd"],
    ]
    for value in null_cost_paths:
        assert value is None


def test_unmeasured_cost_record_round_trips_through_substrate(tmp_path: Path) -> None:
    """The reader must persist and read back a null-cost record without error.

    This is the seam that the schema concern turns on: a nullable cost field must
    survive write -> flatten -> sqlite (REAL column) -> read. total_cost_usd lands
    as SQL NULL, not a coerced 0.0, so cost-based views stay honest.
    """
    record = _build_unmeasured_record(tmp_path)
    audit_substrate.seed_records(tmp_path, [record])

    conn = audit_substrate.create_or_open(tmp_path)
    try:
        row = conn.execute(
            "SELECT total_cost_usd, record_schema_version FROM audit_records WHERE run_id = ?",
            (record["run_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] is None  # SQL NULL, not 0.0 — unmeasured stays distinct from free
    assert row[1] == SCHEMA_VERSION


def test_migration_helpers_cover_current_version() -> None:
    """Bumping ``SCHEMA_VERSION`` must add a matching ``MIGRATION_HELPERS`` entry.

    The registry asserted here is the same one the reader-side migration
    dispatch (``audit_substrate._migrate_record``) iterates over, so a
    forgotten helper fails this guard AND breaks runtime migration of older
    records — not just the unit test. See ADR-0002 §"Schema versioning is
    load-bearing".
    """
    assert MIGRATION_HELPERS, "MIGRATION_HELPERS must contain at least one entry"
    max_known = max(MIGRATION_HELPERS.keys())
    assert max_known == SCHEMA_VERSION - 1, (
        f"SCHEMA_VERSION={SCHEMA_VERSION} but MIGRATION_HELPERS only covers "
        f"up to version {max_known}. Add "
        f"MIGRATION_HELPERS[{SCHEMA_VERSION - 1}] = _migrate_v"
        f"{SCHEMA_VERSION - 1}_to_v{SCHEMA_VERSION} in audit_substrate.py so "
        "readers lift older records to the current shape. "
        'See ADR-0002 §"Schema versioning is load-bearing".'
    )


def test_writer_and_reader_share_migration_registry() -> None:
    """The writer's re-exported registry must be the reader's runtime registry.

    Prevents the failure mode where MIGRATION_HELPERS becomes a writer-local
    copy that satisfies the guard while ``audit_substrate._migrate_record``
    still returns older records unchanged. See ADR-0002 §"Schema versioning
    is load-bearing".
    """
    assert audit_writer.MIGRATION_HELPERS is audit_substrate.MIGRATION_HELPERS
    assert audit_writer.SCHEMA_VERSION == audit_substrate.CURRENT_RECORD_SCHEMA_VERSION


def test_migrate_record_dispatches_through_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_migrate_record`` must apply ``MIGRATION_HELPERS`` entries in chain.

    Proves the registry the guard asserts against is the same code path
    used at runtime when a reader loads an older record.
    """
    called: list[int] = []

    def fake_v1_to_v2(record: dict) -> dict:
        called.append(1)
        return {**record, "migrated_v2": True}

    fake_registry = {1: fake_v1_to_v2}
    monkeypatch.setattr(audit_substrate, "MIGRATION_HELPERS", fake_registry)

    out = audit_substrate._migrate_record({"marker": "untouched"}, from_version=1)

    assert called == [1]
    assert out["migrated_v2"] is True
    assert out["marker"] == "untouched"


def test_migrate_v3_to_v4_backfills_readiness_fields() -> None:
    """v3 records lack task.fix_ready/readiness_warnings; v4 backfills them.

    Older records have no equivalent signal to recover, so the migration
    defaults to the "unknown" shape (None / empty list) rather than
    guessing a readiness verdict. See issue #1253.
    """
    v3_record = {"task": {"name": "Test", "slug": "test"}}

    migrated = audit_substrate._migrate_v3_to_v4(v3_record)

    assert migrated["task"]["fix_ready"] is None
    assert migrated["task"]["readiness_warnings"] == []
    assert migrated["task"]["name"] == "Test"


def test_migrate_v3_to_v4_is_idempotent_when_fields_already_present() -> None:
    """Does not clobber existing fix_ready/readiness_warnings values."""
    v3_record = {
        "task": {"name": "Test", "slug": "test", "fix_ready": True, "readiness_warnings": []}
    }

    migrated = audit_substrate._migrate_v3_to_v4(v3_record)

    assert migrated["task"]["fix_ready"] is True
