"""Writer-side schema-drift guard for the per-run audit record.

See ADR-0002 §"Schema versioning is load-bearing". The substrate's reader-side
lazy migration only holds if writers bump ``SCHEMA_VERSION`` whenever the
record's serialized shape changes. This test snapshots the field set + types
the writer produces and fails when that drifts without a corresponding
``SCHEMA_VERSION`` bump and ``MIGRATION_HELPERS`` entry.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from theforge.agent_types import COST_PROVIDER_REPORTED, ModelUsage
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
    build_provenance,
)
from theforge.coordinator import audit as audit_writer
from theforge.coordinator import audit_substrate
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.audit_substrate import (
    CURRENT_RECORD_SCHEMA_VERSION as SCHEMA_VERSION,
)
from theforge.coordinator.audit_substrate import MIGRATION_HELPERS
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    DevIterationTelemetry,
    GateDebugTelemetry,
    GateDiagnosticTelemetry,
    Phase,
    ReviewIterationTelemetry,
)
from theforge.coordinator.validate_phase import record_validate_block
from theforge.process_group import TEARDOWN_KILLED_SURVIVORS, ProcessTeardown
from theforge.runners import AgentResult
from theforge.task import TaskStory

FIXTURES = Path(__file__).parent / "fixtures"


# Lists whose element shape is fixed rather than outcome-dependent, and is
# therefore part of the schema this guard pins. Each entry is the dotted path of
# the list itself; the guard records its elements' fields as ``path[].field``.
#
# Lists are opaque to the guard by default because many element shapes genuinely
# do vary by phase outcome, and pinning those would fail on legitimate variation.
# But a blanket rule also covered the stable ones, which left an entire class of
# writer change — a field added inside a list entry — unversioned by construction
# (#1997). These are the structures whose elements come from a single dataclass or
# a single builder, so their fields drift only when the writer changes.
#
# Adding a path here is a deliberate claim that the shape is stable. Removing one
# is a claim that it is not; say why in the diff.
_PINNED_LIST_ELEMENTS: tuple[str, ...] = (
    "iterations.dev_loop",  # DevIterationTelemetry
    "iterations.gate_debug",  # GateDebugTelemetry
    "iterations.gate_diagnostic",  # GateDiagnosticTelemetry
    "iterations.review_loop",  # ReviewIterationTelemetry
    "iterations.budget_consumption_log",  # RetryBudgetConsumption
    "validate_blocks",  # validate_phase.record_validate_block
    "cost.agents",  # audit_render._agent_entry
    # The invocation ledger's billed-component list (#2205). Pinned because the
    # whole point of the ledger is that each billed identity is a first-class
    # record: a field silently dropped from an element would take a separately
    # attributable cost with it, and the guard would never see it if only the
    # outer list were pinned.
    "cost.agents[].ledger.billed_components",
)


def _collect_schema(value: object, path: str = "") -> dict[str, str]:
    """Walk a record and return ``{path: type_name}`` for every node.

    Nested dicts recurse with dotted paths. Lists are recorded as ``"list"``;
    the guard descends into elements only for the paths in
    ``_PINNED_LIST_ELEMENTS``, recording the union of element fields as
    ``path[].field`` so a field added inside an entry is caught. Element shapes
    outside that set vary by phase outcome and stay opaque.
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
        if path in _PINNED_LIST_ELEMENTS:
            # Union across entries: a field present on any entry is part of the
            # shape, so a writer that stops emitting it counts as drift.
            for element in value:
                out.update(_collect_schema(element, f"{path}[]"))
    elif isinstance(value, bool):
        out[key] = "bool"
    else:
        out[key] = type(value).__name__
    return out


def _make_config(tmp_path: Path) -> ForgeConfig:
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# spec", encoding="utf-8")
    # A real run always loads its config from a file, so the pinned record must
    # be the *populated* configuration-provenance shape (#2056) — pinning the
    # null shape would let a writer that stopped recording identity pass.
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: test\n", encoding="utf-8")
    config = ForgeConfig(
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
    return dataclasses.replace(config, provenance=build_provenance(config, config_path))


def _populate_pinned_lists(state: CoordinatorState) -> None:
    """Put one entry in every list whose element shape this guard pins.

    The guard can only see an element that exists, so the fixture has to produce
    one for each path in ``_PINNED_LIST_ELEMENTS``. Entries are built through the
    real dataclasses and the real recorder rather than hand-written dicts: a field
    added to ``DevIterationTelemetry`` or to ``record_validate_block`` then shows
    up here on its own, which is the point — the guard exists to notice writer
    changes nobody remembered to version (#1997).
    """
    state.dev_iteration_telemetry.append(
        DevIterationTelemetry(iteration=1, max_iterations=3, cost_usd=1.0, duration_s=2.0)
    )
    state.gate_debug_telemetry.append(
        GateDebugTelemetry(
            trace_index=1,
            trace_path=".forge/traces/1-gate-debug.txt",
            command="make gate-debug",
            ran=True,
            timeout_s=60,
            exit_code=1,
            output_tail="tail",
            output_truncated=False,
        )
    )
    state.gate_diagnostic_telemetry.append(
        GateDiagnosticTelemetry(
            trace_index=1,
            trace_path=".forge/traces/1-gate-diagnostic.txt",
            command="pytest -n 0",
            ran=True,
            budget_s=300,
            per_test_timeout_s=10,
            exit_code=1,
            timed_out=False,
            hanging_test=None,
            output_tail="tail",
            output_truncated=False,
        )
    )
    state.review_iteration_telemetry.append(
        ReviewIterationTelemetry(
            iteration=1,
            max_iterations=2,
            cost_usd=1.0,
            duration_s=2.0,
            verdict="APPROVE",
            findings_by_severity={"P1": 0, "P2": 0},
            new_findings_by_severity={"P1": 0, "P2": 0},
            repeated_findings_by_severity={"P1": 0, "P2": 0},
            novel_findings=0,
            restated_findings=0,
        )
    )
    state.budget.consume(review_cycle=0)
    record_validate_block(state, outcome="terminal", reason="budgets_exhausted")
    # One fully-populated dev invocation so the guard sees the invocation ledger
    # and its billed components (#2205). Built through the real AgentResult /
    # ModelUsage dataclasses so a field added to either shows up here on its own.
    state.preflight_complexity = "large"
    state.preflight_complexity_score = 8
    state.dev_results.append(
        AgentResult(
            success=True,
            output="done",
            session_id="sess",
            cost_usd=10.902,
            exit_code=0,
            raw={},
            profile_name="dev",
            model_usage=(
                ModelUsage(
                    model="claude-opus-5",
                    input_tokens=100,
                    output_tokens=200,
                    cache_read_tokens=10,
                    cache_creation_tokens=5,
                    cost_usd=10.892,
                    thinking_tokens=50,
                    cost_provenance=COST_PROVIDER_REPORTED,
                ),
            ),
            model_config=("opus", "sonnet"),
            model_used="claude-opus-5",
            transport_used="cli",
            configured_model="opus",
            configured_transport="cli",
            reasoning_effort="high",
            cost_provenance=COST_PROVIDER_REPORTED,
            # A forced process teardown (#2309), so the guard pins the nested
            # shape rather than just the null. Built through the real
            # ProcessTeardown dataclass so a field added to it shows up here.
            process_teardown=ProcessTeardown(
                pgid=4242,
                action=TEARDOWN_KILLED_SURVIVORS,
                member_count=2,
                members=(4242, 4243),
                escaped_pids=(4299,),
                sandbox_dir="/tmp/forge/worktrees/test",
                completed=True,
            ),
        )
    )
    state.dev_durations.append(12.0)


def _build_canonical_record(tmp_path: Path) -> dict:
    """Generate an audit record from a minimal-but-deterministic state."""
    config = _make_config(tmp_path)
    task = TaskStory(name="Test", slug="test", story_path=tmp_path / "spec.md")
    state = CoordinatorState()
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.run_id = "deadbeefcafe"
    _populate_pinned_lists(state)
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
        "If the writer did not change and you added a path to "
        "_PINNED_LIST_ELEMENTS, this is the guard seeing more of the same "
        "record, not a shape change: regenerate the snapshot for the CURRENT "
        "version and do NOT bump — there is nothing to migrate.\n\n"
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


def test_migrate_v4_to_v5_backfills_gate_diagnostic() -> None:
    """v4 records lack iterations.gate_diagnostic; v5 backfills an empty list.

    The diagnostic re-run pass did not exist when v4 records were written, so
    there is nothing to recover — default to an empty list rather than
    fabricating a diagnostic result. See issue #1217.
    """
    v4_record = {"iterations": {"gate_debug": [], "gate_decisions": []}}

    migrated = audit_substrate._migrate_v4_to_v5(v4_record)

    assert migrated["iterations"]["gate_diagnostic"] == []
    assert migrated["iterations"]["gate_debug"] == []


def test_migrate_v4_to_v5_is_idempotent_when_field_present() -> None:
    """Does not clobber an existing gate_diagnostic list."""
    existing = [{"iteration": 1, "hanging_test": "tests/test_x.py::test_hang"}]
    v4_record = {"iterations": {"gate_diagnostic": existing}}

    migrated = audit_substrate._migrate_v4_to_v5(v4_record)

    assert migrated["iterations"]["gate_diagnostic"] == existing


def test_migrate_v4_to_v5_noop_without_iterations_block() -> None:
    """A record with no iterations block passes through unchanged."""
    v4_record = {"task": {"slug": "test"}}

    migrated = audit_substrate._migrate_v4_to_v5(v4_record)

    assert migrated == v4_record


def test_migrate_v5_to_v6_backfills_symptom_test_escalations() -> None:
    """v5 records lack symptom_test_escalations; v6 backfills None.

    The bug-fix symptom-test escalation rule did not exist when v5 records were
    written, so there is nothing to recover — default to None (the "no
    escalations" shape) rather than fabricating one. See issue #1560.
    """
    v5_record = {"task": {"slug": "test"}}

    migrated = audit_substrate._migrate_v5_to_v6(v5_record)

    assert migrated["symptom_test_escalations"] is None
    assert migrated["task"] == {"slug": "test"}


def test_migrate_v5_to_v6_is_idempotent_when_field_present() -> None:
    """Does not clobber an existing symptom_test_escalations list."""
    existing = [{"file": "tests/test_x.py", "effective_severity": "P1"}]
    v5_record = {"symptom_test_escalations": existing}

    migrated = audit_substrate._migrate_v5_to_v6(v5_record)

    assert migrated["symptom_test_escalations"] == existing


def test_pinned_list_elements_are_walked_and_others_stay_opaque() -> None:
    """The guard's coverage is itself pinned, in both directions (#1997).

    Without this, the recursion could be removed and every nested field would go
    back to being invisible with the snapshot still passing — the failure mode
    that let two fields land unversioned in the first place.
    """
    record = {
        "iterations": {"dev_loop": [{"gate_result": "FAIL"}]},
        "reviews": [{"verdict": "APPROVE"}],
    }

    schema = _collect_schema(record)

    # Pinned: elements are walked, so a field inside an entry is part of the shape.
    assert schema["iterations.dev_loop[].gate_result"] == "str"
    # Not pinned: the list is recorded, its elements are not.
    assert schema["reviews"] == "list"
    assert not any(key.startswith("reviews[]") for key in schema)
