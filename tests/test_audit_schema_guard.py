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
from theforge.coordinator.audit import (
    MAX_KNOWN_VERSION,
    MIGRATION_HELPERS,
    SCHEMA_VERSION,
    generate_audit_log,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
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


def _build_canonical_record(tmp_path: Path) -> dict:
    """Generate an audit record from a minimal-but-deterministic state."""
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# spec", encoding="utf-8")
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
    task = TaskStory(name="Test", slug="test", story_path=spec_path)
    state = CoordinatorState()
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.run_id = "deadbeefcafe"
    result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")
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


def test_migration_helpers_cover_current_version() -> None:
    """Bumping ``SCHEMA_VERSION`` must add a matching ``MIGRATION_HELPERS`` entry.

    See ADR-0002 §"Schema versioning is load-bearing". Reaching version K
    from v1 requires helpers for keys 1..K-1, so ``max(MIGRATION_HELPERS)``
    must equal ``SCHEMA_VERSION - 1``.
    """
    assert MIGRATION_HELPERS, "MIGRATION_HELPERS must contain at least one entry"
    assert MAX_KNOWN_VERSION == max(MIGRATION_HELPERS.keys())
    assert MAX_KNOWN_VERSION == SCHEMA_VERSION - 1, (
        f"SCHEMA_VERSION={SCHEMA_VERSION} but MIGRATION_HELPERS only covers "
        f"up to version {MAX_KNOWN_VERSION}. Add "
        f"MIGRATION_HELPERS[{SCHEMA_VERSION - 1}] = _migrate_v"
        f"{SCHEMA_VERSION - 1}_to_v{SCHEMA_VERSION} so readers can lift "
        f"older records to the current shape. "
        'See ADR-0002 §"Schema versioning is load-bearing".'
    )
