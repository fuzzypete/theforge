"""SQLite audit substrate — local index over per-run audit records.

This module owns all sqlite3 access for the audit substrate at
``.forge/audits/index.sqlite``. It provides:

- Schema bootstrap and connection management.
- ``upsert_run_record()`` — write a single audit record (used by the
  per-run write path and by importers).
- ``rebuild_from_runs()`` — regenerate the substrate from the canonical
  per-run JSON files under ``.forge/audits/runs/``.
- ``import_history_jsonl()`` — one-shot / repair-safe backfill from the
  legacy ``history.jsonl`` file. Stable-identity reconciliation means
  reruns repair rather than duplicate.
- ``require_substrate()`` — runtime readers' entry point. Refuses a
  missing or corrupt index with a clear, operator-facing exception.
- Query helpers used by the four runtime consumers
  (telemetry, ``has_review_approve``, adaptive iterations, sprint rollup).

Everything here is stdlib (sqlite3, json, hashlib, pathlib).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SUBSTRATE_SCHEMA_VERSION = 3
# Current per-record schema version. Records pre-dating the indexed-dimensions
# slice (#1522) are treated as version 1. The reader-side migration helper
# (`_migrate_record`) is the seam future breaking changes hang off — a version
# bump is warranted only for a breaking field rename/removal that a reader must
# migrate an old record across.
#
# Note (#1596): audit cost fields are ``float | None`` — an unmeasured
# (killed-at-timeout) run records ``null`` where a measured run records a
# number. That is a backward-compatible value-domain *widening*, not a breaking
# shape change: old all-numeric records still read unchanged, and this reader
# stores the null straight into the nullable ``total_cost_usd`` REAL column. So
# it does NOT bump this version. The schema guard pins both the measured and the
# unmeasured shapes so a future accidental re-coercion is still caught.
CURRENT_RECORD_SCHEMA_VERSION = 5
SUBSTRATE_RELPATH = (".forge", "audits", "index.sqlite")
HISTORY_RELPATH = (".forge", "audits", "history.jsonl")
RUNS_RELPATH = (".forge", "audits", "runs")
AUDITS_RELPATH = (".forge", "audits")
SECRETS_RELPATH = (".forge", ".env")


@dataclass(frozen=True)
class AuditPathInfo:
    """One canonical audit-trail path plus a human label for briefings.

    ``relpath`` is the repo-relative path-part tuple (the same shape as the
    ``*_RELPATH`` constants); ``suffix`` is an optional literal appended after
    the joined path when rendered (e.g. a ``/*.json`` glob) so the display
    string can be more specific than the bare directory.
    """

    label: str
    relpath: tuple[str, ...]
    suffix: str = ""

    @property
    def display(self) -> str:
        return "/".join(self.relpath) + self.suffix


# Iterable registry of the canonical audit-trail paths, owned here so that
# consumers (e.g. the diagnose environment briefing) render the *full* set by
# iterating this tuple instead of re-listing constants by hand. Adding a new
# audit path is a one-line append here and it surfaces everywhere the registry
# is rendered — no downstream prompt/template edit required (issue #1425 AC2).
AUDIT_PATH_REGISTRY: tuple[AuditPathInfo, ...] = (
    AuditPathInfo("Audit index (SQLite, canonical, queryable)", SUBSTRATE_RELPATH),
    AuditPathInfo("Per-run audit records (JSON, one file per run)", RUNS_RELPATH, "/*.json"),
    AuditPathInfo(
        "Legacy cross-run audit history (JSONL — superseded by the index above "
        "but still present on older repos)",
        HISTORY_RELPATH,
    ),
    AuditPathInfo(
        "Sprint audit + summary YAML (also run-<run-id>-sprint-audit.yaml)",
        AUDITS_RELPATH,
        "/sprint-audit.yaml",
    ),
)


class SubstrateError(Exception):
    """Base class for substrate-related errors."""


class SubstrateMissingError(SubstrateError):
    """Raised when a runtime reader requires the substrate but it is absent."""


class SubstrateCorruptError(SubstrateError):
    """Raised when the substrate file exists but cannot be opened/queried."""


# ── Path helpers ─────────────────────────────────────────────────────────


def substrate_path(project_root: Path) -> Path:
    return project_root.joinpath(*SUBSTRATE_RELPATH)


def history_jsonl_path(project_root: Path) -> Path:
    return project_root.joinpath(*HISTORY_RELPATH)


def runs_dir(project_root: Path) -> Path:
    return project_root.joinpath(*RUNS_RELPATH)


def audits_dir(project_root: Path) -> Path:
    return project_root.joinpath(*AUDITS_RELPATH)


def secrets_env_path(project_root: Path) -> Path:
    """Path to the project's ``.forge/.env`` secrets file.

    Substrate writers pass this (or ``None`` when not applicable) into
    :func:`upsert_run_record` so the secret values it contains are
    redacted from every record before it lands in the SQLite index.
    """
    return project_root.joinpath(*SECRETS_RELPATH)


# ── Schema ───────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_records (
    run_id TEXT PRIMARY KEY,
    slug TEXT,
    started_at TEXT,
    finished_at TEXT,
    total_cost_usd REAL,
    final_phase TEXT,
    outcome_success INTEGER,
    branch TEXT,
    landing_status TEXT,
    provenance TEXT NOT NULL,
    source_path TEXT,
    source_mtime REAL,
    complexity_score INTEGER,
    record_schema_version INTEGER NOT NULL DEFAULT 1,
    milestone TEXT,
    issue_id INTEGER,
    dev_model TEXT,
    verdict TEXT,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_records_slug ON audit_records(slug);
CREATE INDEX IF NOT EXISTS idx_audit_records_started_at ON audit_records(started_at);
CREATE TABLE IF NOT EXISTS reviews (
    run_id TEXT NOT NULL,
    cycle INTEGER NOT NULL,
    verdict TEXT,
    p1_count INTEGER,
    p2_count INTEGER,
    PRIMARY KEY (run_id, cycle)
);
CREATE INDEX IF NOT EXISTS idx_reviews_verdict ON reviews(verdict);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS readiness_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    issue_ref TEXT NOT NULL,
    issue_type TEXT,
    pre_verdict TEXT,
    post_verdict TEXT,
    action TEXT NOT NULL,
    applied INTEGER NOT NULL,
    bug_diagnosis_state TEXT,
    refusal_reason TEXT,
    emitted_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    staleness_verdict TEXT,
    diagnosis_baseline_sha TEXT
);
CREATE INDEX IF NOT EXISTS idx_readiness_events_kind ON readiness_events(kind);
CREATE INDEX IF NOT EXISTS idx_readiness_events_issue ON readiness_events(issue_ref);
CREATE INDEX IF NOT EXISTS idx_readiness_events_action ON readiness_events(action);
CREATE INDEX IF NOT EXISTS idx_readiness_events_staleness ON readiness_events(staleness_verdict);
CREATE TABLE IF NOT EXISTS shape_verdict_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    base_branch_sha TEXT,
    run_id TEXT,
    sprint_name TEXT,
    milestone TEXT,
    source TEXT,
    emitted_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shape_verdict_events_verdict ON shape_verdict_events(verdict);
CREATE INDEX IF NOT EXISTS idx_shape_verdict_events_issue ON shape_verdict_events(issue_id);
CREATE INDEX IF NOT EXISTS idx_shape_verdict_events_milestone ON shape_verdict_events(milestone);
CREATE INDEX IF NOT EXISTS idx_shape_verdict_events_run ON shape_verdict_events(run_id);
CREATE TABLE IF NOT EXISTS shape_skip_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    source TEXT,
    severity TEXT,
    category TEXT,
    four_question_axis TEXT,
    run_id TEXT,
    sprint_id TEXT,
    sprint_name TEXT,
    milestone TEXT,
    prior_block_count INTEGER NOT NULL DEFAULT 0,
    first_blocked_at TEXT,
    last_blocked_at TEXT,
    emitted_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shape_skip_events_issue ON shape_skip_events(issue_id);
CREATE INDEX IF NOT EXISTS idx_shape_skip_events_code ON shape_skip_events(reason_code);
CREATE INDEX IF NOT EXISTS idx_shape_skip_events_category ON shape_skip_events(category);
CREATE INDEX IF NOT EXISTS idx_shape_skip_events_run ON shape_skip_events(run_id);
CREATE INDEX IF NOT EXISTS idx_shape_skip_events_emitted ON shape_skip_events(emitted_at);
"""


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # Idempotent column adds for substrates created under an older schema.
    # SQLite < 3.35 lacks ADD COLUMN IF NOT EXISTS, so swallow the duplicate
    # error from re-runs.
    try:
        conn.execute("ALTER TABLE audit_records ADD COLUMN complexity_score INTEGER")
    except sqlite3.OperationalError:
        pass
    for stmt in (
        "ALTER TABLE audit_records ADD COLUMN record_schema_version INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE audit_records ADD COLUMN milestone TEXT",
        "ALTER TABLE audit_records ADD COLUMN issue_id INTEGER",
        "ALTER TABLE audit_records ADD COLUMN dev_model TEXT",
        "ALTER TABLE audit_records ADD COLUMN verdict TEXT",
        "ALTER TABLE readiness_events ADD COLUMN staleness_verdict TEXT",
        "ALTER TABLE readiness_events ADD COLUMN diagnosis_baseline_sha TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    # Indexes on columns added by ALTER TABLE above. Must run after the
    # ALTER statements so legacy substrates don't fail with
    # ``no such column`` on first open.
    for idx_stmt in (
        "CREATE INDEX IF NOT EXISTS idx_audit_records_milestone ON audit_records(milestone)",
        "CREATE INDEX IF NOT EXISTS idx_audit_records_issue_id ON audit_records(issue_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_records_dev_model ON audit_records(dev_model)",
        "CREATE INDEX IF NOT EXISTS idx_audit_records_verdict ON audit_records(verdict)",
        "CREATE INDEX IF NOT EXISTS idx_audit_records_final_phase ON audit_records(final_phase)",
        "CREATE INDEX IF NOT EXISTS idx_audit_records_outcome ON audit_records(outcome_success)",
        "CREATE INDEX IF NOT EXISTS idx_audit_records_record_schema_version "
        "ON audit_records(record_schema_version)",
    ):
        try:
            conn.execute(idx_stmt)
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("schema_version", str(SUBSTRATE_SCHEMA_VERSION)),
    )
    conn.commit()


# ── Connection management ────────────────────────────────────────────────


def create_or_open(project_root: Path) -> sqlite3.Connection:
    """Open (creating if missing) the substrate. Use for write/rebuild paths.

    Runtime readers should prefer :func:`require_substrate` instead — this
    function silently bootstraps a fresh empty DB, which is the wrong
    answer for callers that need to surface a missing-substrate error.
    """
    path = substrate_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _apply_schema(conn)
    return conn


def has_audit_inputs(project_root: Path) -> bool:
    """Return True when any canonical audit input source exists on disk.

    Audit inputs that require a substrate to read:
      - any per-run JSON file under ``.forge/audits/runs/``
      - the legacy ``.forge/audits/history.jsonl``

    A repo with neither is treated as "fresh" — readers are allowed to
    return empty/safe defaults without failing.
    """
    runs = runs_dir(project_root)
    if runs.exists() and any(runs.glob("*.json")):
        return True
    return history_jsonl_path(project_root).exists()


def require_substrate(project_root: Path) -> sqlite3.Connection:
    """Open a substrate that must exist and be valid for runtime readers.

    Behavior:
      - Substrate missing + per-run files exist → rebuild from runs/*.json.
      - Substrate missing + only legacy history.jsonl exists →
        SubstrateMissingError pointing the operator at
        ``forge audits rebuild --include-legacy-history``. Runtime readers
        never silently import legacy history.
      - Substrate missing + no audit inputs at all → SubstrateMissingError.
        Callers that should treat fresh repos as "no history" must check
        :func:`has_audit_inputs` first.
      - Substrate present but stale (native source files removed or
        mtime-mismatched) → rebuild from runs/*.json before returning.
      - Substrate present but corrupt → SubstrateCorruptError.
    """
    path = substrate_path(project_root)
    if not path.exists():
        if not has_audit_inputs(project_root):
            raise SubstrateMissingError(
                f"audit substrate not found at {path}. Run `forge audits rebuild` to create it."
            )
        if runs_dir(project_root).exists() and any(runs_dir(project_root).glob("*.json")):
            rebuild_from_runs(project_root)
            return _open_validated(path)
        # Only legacy history.jsonl present — refuse to silently import.
        raise SubstrateMissingError(
            f"audit substrate not found at {path} but legacy history.jsonl exists. "
            f"Run `forge audits rebuild --include-legacy-history` to backfill."
        )
    conn = _open_validated(path)
    # Validate native rows against the canonical per-run files. Stale
    # state (deleted file or mtime mismatch) triggers a rebuild.
    if _native_rows_are_stale(conn, project_root):
        conn.close()
        rebuild_from_runs(project_root)
        conn = _open_validated(path)
    return conn


def _open_validated(path: Path) -> sqlite3.Connection:
    """Connect, run integrity_check, apply schema (idempotent)."""
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or (row[0] != "ok"):
            detail = row[0] if row else "no result"
            raise SubstrateCorruptError(
                f"audit substrate at {path} failed integrity check: {detail}. "
                "Run `forge audits rebuild [--include-legacy-history]` to recover."
            )
        _apply_schema(conn)
    except sqlite3.DatabaseError as exc:
        raise SubstrateCorruptError(
            f"audit substrate at {path} could not be opened: {exc}. "
            "Run `forge audits rebuild [--include-legacy-history]` to recover."
        ) from exc
    return conn


def _native_rows_are_stale(conn: sqlite3.Connection, project_root: Path) -> bool:
    """Return True when indexed native rows diverge from on-disk run files.

    Triggers a rebuild when:
      - any native row that records a source_path now references a file
        that has been deleted, or whose mtime has changed since indexing;
        or
      - per-run JSON files exist on disk for which there is no native row
        with that source_path (a new run was emitted while this process
        was not the writer, or the index was hand-edited).

    Native rows without a source_path (e.g. programmatic inserts from
    sprint rollup) are intentionally NOT validated here — they have no
    canonical file to compare against.
    """
    runs = runs_dir(project_root)
    on_disk = list(runs.glob("*.json")) if runs.exists() else []
    cur = conn.execute(
        "SELECT source_path, source_mtime FROM audit_records "
        "WHERE provenance = 'native' AND source_path IS NOT NULL"
    )
    indexed: dict[str, float | None] = {}
    for row in cur:
        rel = row[0] if not isinstance(row, sqlite3.Row) else row["source_path"]
        mtime = row[1] if not isinstance(row, sqlite3.Row) else row["source_mtime"]
        indexed[str(rel)] = mtime
    # File-on-disk → row-in-index check.
    for path in on_disk:
        try:
            rel = str(path.relative_to(project_root))
        except ValueError:
            continue
        if rel not in indexed:
            return True
        recorded = indexed[rel]
        if recorded is None:
            return True
        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            return True
        if abs(current_mtime - float(recorded)) > 1e-6:
            return True
    # Row-in-index → file-on-disk check (deletions).
    on_disk_rels = set()
    for path in on_disk:
        try:
            on_disk_rels.add(str(path.relative_to(project_root)))
        except ValueError:
            continue
    for rel in indexed:
        if rel not in on_disk_rels:
            return True
    return False


# ── Meta helpers ─────────────────────────────────────────────────────────


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return row[0] if not isinstance(row, sqlite3.Row) else row["value"]


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ── Stable identity ──────────────────────────────────────────────────────


def _canonical_json(record: dict) -> str:
    """Return a stable canonical JSON encoding for hashing."""
    return json.dumps(record, sort_keys=True, default=str, ensure_ascii=False)


def derive_run_id(record: dict) -> str:
    """Return a stable identifier for an audit record.

    Uses the embedded ``run_id`` when present; otherwise synthesises a
    deterministic ``legacy:<sha1[:16]>`` identifier from durable identity
    fields only — slug, started_at, finished_at, and final_phase. The
    full canonical JSON is *not* included so that re-importing a record
    whose body was repaired (e.g. a cost or message correction) maps to
    the same row instead of inserting a duplicate.

    Records without slug/started_at fall back to a hash that includes
    raw content as a last-resort identifier (these are extremely old or
    malformed records — duplication risk is preferable to dropping them).
    """
    rid = record.get("run_id")
    if isinstance(rid, str) and rid:
        return rid
    sprint_block = record.get("sprint") if isinstance(record.get("sprint"), dict) else {}
    slug = (
        ((record.get("task") or {}).get("slug"))
        or sprint_block.get("slug")
        or sprint_block.get("name")
        or ""
    )
    timing = record.get("timing") or {}
    started = (
        timing.get("started_at")
        or sprint_block.get("started_at")
        or record.get("started_at")
        or ""
    )
    finished = (
        timing.get("finished_at")
        or sprint_block.get("finished_at")
        or record.get("finished_at")
        or ""
    )
    final_phase = (record.get("outcome") or {}).get("final_phase") or ""
    if slug or started:
        identity = f"{slug}|{started}|{finished}|{final_phase}"
    else:
        # Last-resort: no stable fields present at all.
        identity = _canonical_json(record)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()
    return f"legacy:{digest[:16]}"


# ── Record extraction ────────────────────────────────────────────────────


def _flat_fields(record: dict) -> dict:
    """Extract indexed columns from an audit record dict."""
    task = record.get("task") or {}
    outcome = record.get("outcome") or {}
    timing = record.get("timing") or {}
    workspace = record.get("workspace") or {}
    cost = record.get("cost") or {}
    totals = record.get("totals") or {}
    preflight = record.get("preflight") if isinstance(record.get("preflight"), dict) else {}
    raw_landing = record.get("landing_event")
    landing_event = raw_landing if isinstance(raw_landing, dict) else {}

    raw_score = preflight.get("complexity_score") if isinstance(preflight, dict) else None
    complexity_score: int | None
    if isinstance(raw_score, bool):
        complexity_score = None
    elif isinstance(raw_score, int):
        complexity_score = raw_score
    elif isinstance(raw_score, float):
        complexity_score = int(raw_score)
    else:
        complexity_score = None

    final_phase = outcome.get("final_phase")
    success = outcome.get("success")
    if isinstance(success, bool):
        outcome_success = 1 if success else 0
    else:
        outcome_success = None

    total_cost_usd = totals.get("cost_usd")
    if total_cost_usd is None:
        total_cost_usd = cost.get("total_usd")

    landing_status = record.get("landing_status")
    if (
        landing_status is None
        and isinstance(landing_event, dict)
        and landing_event.get("landed") is True
    ):
        landing_status = "landed"

    raw_record_version = record.get("schema_version")
    if isinstance(raw_record_version, bool):
        record_schema_version = 1
    elif isinstance(raw_record_version, int):
        record_schema_version = raw_record_version
    elif isinstance(raw_record_version, float):
        record_schema_version = int(raw_record_version)
    else:
        record_schema_version = 1

    milestone = record.get("milestone")
    if not isinstance(milestone, str) or not milestone:
        task_milestone = task.get("milestone") if isinstance(task, dict) else None
        milestone = task_milestone if isinstance(task_milestone, str) and task_milestone else None

    raw_issue = task.get("github_issue") if isinstance(task, dict) else None
    if isinstance(raw_issue, bool):
        issue_id: int | None = None
    elif isinstance(raw_issue, int):
        issue_id = raw_issue
    elif isinstance(raw_issue, float):
        issue_id = int(raw_issue)
    elif isinstance(raw_issue, str) and raw_issue.lstrip("#").strip().isdigit():
        issue_id = int(raw_issue.lstrip("#").strip())
    else:
        issue_id = None

    return {
        "slug": task.get("slug"),
        "started_at": timing.get("started_at"),
        "finished_at": timing.get("finished_at"),
        "total_cost_usd": total_cost_usd,
        "final_phase": final_phase,
        "outcome_success": outcome_success,
        "branch": workspace.get("branch"),
        "landing_status": landing_status,
        "complexity_score": complexity_score,
        "record_schema_version": record_schema_version,
        "milestone": milestone,
        "issue_id": issue_id,
        "dev_model": _derive_dev_model(record),
        "verdict": _derive_record_verdict(record),
    }


def _derive_record_verdict(record: dict) -> str | None:
    """Return the run-level verdict for indexed record-level verdict queries.

    ADR-0002 §3 names ``verdict`` as a record-level query dimension distinct
    from ``reviews.verdict`` (which is per-cycle). The run-level value is the
    verdict of the final review cycle — the verdict that actually decided the
    run's outcome. Returns ``None`` when no reviews were recorded.
    """
    reviews = record.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        return None
    last: dict | None = None
    last_cycle = -1
    for idx, rev in enumerate(reviews, start=1):
        if not isinstance(rev, dict):
            continue
        cycle = rev.get("cycle") if isinstance(rev.get("cycle"), int) else idx
        if cycle >= last_cycle:
            last_cycle = cycle
            last = rev
    if last is None:
        return None
    verdict = last.get("verdict")
    return verdict if isinstance(verdict, str) and verdict else None


def _derive_dev_model(record: dict) -> str | None:
    """Return the canonical dev model identity recorded for this run.

    Mirrors the `cost.agents` parsing logic in :func:`_derive_escalation` so
    the indexed `dev_model` column matches the model the adaptive router
    treats as authoritative (the one that actually ran).
    """
    cost_block = record.get("cost") if isinstance(record.get("cost"), dict) else {}
    agents = cost_block.get("agents") if isinstance(cost_block.get("agents"), list) else []
    for entry in agents:
        if not isinstance(entry, dict):
            continue
        if entry.get("phase") != "dev" and entry.get("role") != "dev":
            continue
        provider = (entry.get("provider") or "").strip()
        model = (entry.get("model") or "").strip()
        cli = (entry.get("cli") or "").strip()
        if model and provider:
            transport = "cli" if cli else "api"
            return f"{provider}/{model}/{transport}"
        if entry.get("name"):
            return str(entry["name"])
    return None


# ── Per-record schema migration ──────────────────────────────────────────


def _migrate_v1_to_v2(record: dict) -> dict:
    """No-op: v1 → v2 introduced no breaking field rename or removal.

    The seam exists so future structural changes register a chained
    ``MIGRATION_HELPERS[N] = _migrate_vN_to_vN+1`` entry. The writer-side
    schema-drift guard (tests/test_audit_schema_guard.py) asserts against
    this same registry so that a SCHEMA_VERSION bump cannot land without a
    runtime migration.
    """
    return record


def _migrate_v2_to_v3(record: dict) -> dict:
    """Add the structured ``landing`` field derived from ``merge`` (issue #1424).

    v2 records stored only the raw ``land_story`` merge_info dict under
    ``merge``; the sprint summary collapsed it to a boolean that could not tell
    a fresh-PR merge from an already-merged short-circuit. v3 adds an
    operator-facing ``landing`` record. Derive it from the persisted merge_info
    so older records gain the field without re-running ``land_story``; the raw
    ``merge`` dict already carries ``landing_path``/``guard_evidence``.
    """
    from .landing_record import build_landing_record  # noqa: PLC0415

    if "landing" in record:
        return record
    return {**record, "landing": build_landing_record(record.get("merge"))}


def _migrate_v3_to_v4(record: dict) -> dict:
    """Add ``task.fix_ready``/``task.readiness_warnings`` (issue #1253).

    v3 records never captured the shape-gate readiness signal in the audit
    trail, forcing operators to reconstruct it from shape-check reason
    codes. v4 adds the fields directly to ``task``; older records have no
    equivalent data to backfill, so default to the "unknown" shape (None /
    empty list) rather than guessing a readiness verdict.
    """
    task = record.get("task")
    if not isinstance(task, dict):
        return record
    if "fix_ready" in task and "readiness_warnings" in task:
        return record
    migrated_task = {**task}
    migrated_task.setdefault("fix_ready", None)
    migrated_task.setdefault("readiness_warnings", [])
    return {**record, "task": migrated_task}


def _migrate_v4_to_v5(record: dict) -> dict:
    """Add ``iterations.gate_diagnostic`` (issue #1217).

    v4 records captured only ``iterations.gate_debug`` (the free-form
    user-configured diagnostic). v5 adds ``iterations.gate_diagnostic``, the
    hardcoded pytest ``-n 0`` diagnostic re-run pass that surfaces the hanging
    test on a gate timeout. Older records ran before the pass existed, so
    backfill an empty list rather than fabricating a diagnostic result.
    """
    iterations = record.get("iterations")
    if not isinstance(iterations, dict):
        return record
    if "gate_diagnostic" in iterations:
        return record
    return {**record, "iterations": {**iterations, "gate_diagnostic": []}}


# Reader-side migration registry. Keys are the FROM version; each helper
# translates a record at version N into the shape expected at version N+1.
# ``_migrate_record`` chains these from the record's persisted version up to
# ``CURRENT_RECORD_SCHEMA_VERSION``. The CI guard asserts
# ``max(MIGRATION_HELPERS) == CURRENT_RECORD_SCHEMA_VERSION - 1`` so that
# bumping the version requires both a writer-side shape change AND a
# reader-side translation entry in the same PR. See ADR-0002 §"Schema
# versioning is load-bearing".
MIGRATION_HELPERS: dict[int, Callable[[dict], dict]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
    3: _migrate_v3_to_v4,
    4: _migrate_v4_to_v5,
}


def _migrate_record(record: dict, *, from_version: int) -> dict:
    """Bring an older per-run record up to ``CURRENT_RECORD_SCHEMA_VERSION``.

    Applies ``MIGRATION_HELPERS[from_version]``, ``MIGRATION_HELPERS[from_version+1]``,
    … until the record reaches the current version. Reader paths must call
    this with ``from_version`` taken from the indexed ``record_schema_version``
    column rather than parsing ``raw_json`` speculatively.
    """
    version = from_version
    migrated = record
    while version < CURRENT_RECORD_SCHEMA_VERSION:
        helper = MIGRATION_HELPERS.get(version)
        if helper is None:
            # No registered translator for this step — return what we have
            # and let downstream parsing surface any incompatibility. The
            # CI guard prevents this state from shipping; reaching it at
            # runtime means a malformed/forward-version record.
            return migrated
        migrated = helper(migrated)
        version += 1
    return migrated


def _load_migrated(raw_json: str, record_schema_version: int | None) -> dict | None:
    """Parse ``raw_json`` and route through :func:`_migrate_record`.

    Returns ``None`` when ``raw_json`` cannot be decoded so callers can
    skip rather than abort the iteration.
    """
    try:
        record = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    ver = record_schema_version if isinstance(record_schema_version, int) else 1
    return _migrate_record(record, from_version=ver)


def _extract_reviews(record: dict) -> list[tuple[int, str | None, int | None, int | None]]:
    out = []
    reviews = record.get("reviews")
    if not isinstance(reviews, list):
        return out
    for idx, rev in enumerate(reviews, start=1):
        if not isinstance(rev, dict):
            continue
        cycle = rev.get("cycle") if isinstance(rev.get("cycle"), int) else idx
        out.append(
            (
                int(cycle),
                rev.get("verdict"),
                rev.get("p1_count") if isinstance(rev.get("p1_count"), int) else None,
                rev.get("p2_count") if isinstance(rev.get("p2_count"), int) else None,
            )
        )
    return out


# ── Writes ───────────────────────────────────────────────────────────────


@dataclass
class UpsertResult:
    inserted: bool = False
    updated: bool = False
    unchanged: bool = False
    skipped_protected: bool = False  # legacy attempted to overwrite a native row
    run_id: str = ""


def upsert_run_record(
    conn: sqlite3.Connection,
    record: dict,
    *,
    provenance: str,
    source_path: str | None = None,
    source_mtime: float | None = None,
    env_file: Path | None = None,
) -> UpsertResult:
    """Insert-or-replace a single audit record.

    Redaction contract (ADR-0002 §1): every record written to the
    substrate is passed through :func:`coordinator.redact.redact`
    inside this function before any persistence happens. This is the
    *only* sanctioned write path into the audit substrate, so the
    redaction guarantee holds for every caller — direct dict bypass is
    not possible. Callers SHOULD pass ``env_file`` (typically
    :func:`secrets_env_path`) so values from ``.forge/.env`` are also
    scrubbed; secret-key-pattern and ``environment``-dict scrubbing
    happen unconditionally even when ``env_file`` is ``None``.

    Returns :class:`UpsertResult` indicating whether this was a fresh
    insert, an update of an existing row with different content, or an
    unchanged row. Reviews flat-table is rewritten on each upsert.
    """
    from .redact import redact

    record = redact(record, env_file)
    run_id = derive_run_id(record)
    raw_json = _canonical_json(record)
    existing = conn.execute(
        "SELECT raw_json, provenance FROM audit_records WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    # Native per-run files are canonical. Refuse to let a legacy import
    # downgrade an existing native row's provenance / content.
    if existing is not None and provenance == "legacy_history_jsonl":
        prev_provenance = (
            existing[1] if not isinstance(existing, sqlite3.Row) else existing["provenance"]
        )
        if prev_provenance == "native":
            return UpsertResult(skipped_protected=True, run_id=run_id)
    flat = _flat_fields(record)
    params = (
        run_id,
        flat["slug"],
        flat["started_at"],
        flat["finished_at"],
        flat["total_cost_usd"],
        flat["final_phase"],
        flat["outcome_success"],
        flat["branch"],
        flat["landing_status"],
        provenance,
        source_path,
        source_mtime,
        flat["complexity_score"],
        flat["record_schema_version"],
        flat["milestone"],
        flat["issue_id"],
        flat["dev_model"],
        flat["verdict"],
        raw_json,
    )
    conn.execute(
        "INSERT INTO audit_records "
        "(run_id, slug, started_at, finished_at, total_cost_usd, final_phase, "
        "outcome_success, branch, landing_status, provenance, source_path, "
        "source_mtime, complexity_score, record_schema_version, milestone, "
        "issue_id, dev_model, verdict, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET "
        "slug=excluded.slug, started_at=excluded.started_at, "
        "finished_at=excluded.finished_at, total_cost_usd=excluded.total_cost_usd, "
        "final_phase=excluded.final_phase, outcome_success=excluded.outcome_success, "
        "branch=excluded.branch, landing_status=excluded.landing_status, "
        "provenance=excluded.provenance, source_path=excluded.source_path, "
        "source_mtime=excluded.source_mtime, "
        "complexity_score=excluded.complexity_score, "
        "record_schema_version=excluded.record_schema_version, "
        "milestone=excluded.milestone, issue_id=excluded.issue_id, "
        "dev_model=excluded.dev_model, verdict=excluded.verdict, "
        "raw_json=excluded.raw_json",
        params,
    )
    # Rewrite reviews for this run_id.
    conn.execute("DELETE FROM reviews WHERE run_id = ?", (run_id,))
    review_rows = _extract_reviews(record)
    for cycle, verdict, p1, p2 in review_rows:
        conn.execute(
            "INSERT OR REPLACE INTO reviews(run_id, cycle, verdict, p1_count, p2_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, cycle, verdict, p1, p2),
        )
    if existing is None:
        return UpsertResult(inserted=True, run_id=run_id)
    prev_raw = existing[0] if not isinstance(existing, sqlite3.Row) else existing["raw_json"]
    if prev_raw == raw_json:
        return UpsertResult(unchanged=True, run_id=run_id)
    return UpsertResult(updated=True, run_id=run_id)


def _import_history_classify(result: UpsertResult, summary: "ImportSummary") -> None:
    """Update an ImportSummary based on a single upsert outcome."""
    if result.inserted:
        summary.imported += 1
    elif result.updated:
        summary.updated_repaired += 1
    elif result.skipped_protected:
        # Native row already covers this identity — count as skipped.
        summary.skipped_existing += 1
    else:
        summary.skipped_existing += 1


# ── Rebuild ──────────────────────────────────────────────────────────────


@dataclass
class RebuildSummary:
    runs_seen: int = 0
    imported: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)


def rebuild_from_runs(project_root: Path) -> RebuildSummary:
    """Drop-and-recreate the substrate from per-run JSON files.

    Scans ``.forge/audits/runs/*.json``, validates ``run_id`` presence,
    and upserts each into a freshly recreated substrate with
    ``provenance='native'``. Records lacking a ``run_id`` are counted as
    failures (they cannot key the substrate deterministically).

    Legacy rows imported from ``history.jsonl`` (provenance =
    ``legacy_history_jsonl``) are snapshotted before the destructive
    rebuild and re-applied afterward. Without this preservation, a
    runtime stale-rebuild triggered by a deleted/mtime-mismatched
    per-run file would silently drop historical upgrade data, breaking
    the contract that imported legacy rows stay visible to migrated
    readers across normal runtime operations. The legacy import flag
    is also carried forward so a subsequent operator-driven import is
    still considered already-done.
    """
    path = substrate_path(project_root)
    legacy_snapshot: list[tuple[str, str, str | None, float | None]] = []
    legacy_import_done: str | None = None
    if path.exists():
        try:
            existing_conn = sqlite3.connect(str(path))
            try:
                existing_conn.row_factory = sqlite3.Row
                # Best-effort schema apply so the SELECT below works on
                # older substrates that pre-date columns we touch.
                _apply_schema(existing_conn)
                rows = existing_conn.execute(
                    "SELECT raw_json, provenance, source_path, source_mtime "
                    "FROM audit_records WHERE provenance = 'legacy_history_jsonl'"
                ).fetchall()
                for row in rows:
                    legacy_snapshot.append(
                        (
                            row["raw_json"],
                            row["provenance"],
                            row["source_path"],
                            row["source_mtime"],
                        )
                    )
                legacy_import_done = _meta_get(existing_conn, "legacy_import_done")
            finally:
                existing_conn.close()
        except sqlite3.DatabaseError:
            # Corrupt substrate — nothing usable to preserve. Operator
            # must run `forge audits rebuild --include-legacy-history`
            # explicitly to recover legacy data.
            legacy_snapshot = []
            legacy_import_done = None
        path.unlink()
    conn = create_or_open(project_root)
    summary = RebuildSummary()
    runs = runs_dir(project_root)
    env_file = secrets_env_path(project_root)
    env_file_arg: Path | None = env_file if env_file.exists() else None
    if runs.exists():
        for run_file in sorted(runs.glob("*.json")):
            summary.runs_seen += 1
            try:
                with open(run_file, encoding="utf-8") as fh:
                    record = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                summary.failed += 1
                summary.failures.append(f"{run_file.name}: {exc}")
                continue
            if not isinstance(record, dict) or not record.get("run_id"):
                summary.failed += 1
                summary.failures.append(f"{run_file.name}: missing run_id")
                continue
            try:
                stat = run_file.stat()
                upsert_run_record(
                    conn,
                    record,
                    provenance="native",
                    source_path=str(run_file.relative_to(project_root)),
                    source_mtime=stat.st_mtime,
                    env_file=env_file_arg,
                )
                summary.imported += 1
            except sqlite3.DatabaseError as exc:
                summary.failed += 1
                summary.failures.append(f"{run_file.name}: {exc}")
    # Re-apply preserved legacy rows so a runtime rebuild does not silently
    # drop history.jsonl-imported records. ``upsert_run_record`` with
    # provenance=legacy_history_jsonl respects the native-row protection
    # rule, so any identity collision with a freshly-rebuilt native row
    # leaves the native row in place.
    for raw_json, provenance, source_path, source_mtime in legacy_snapshot:
        try:
            record = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        try:
            upsert_run_record(
                conn,
                record,
                provenance=provenance,
                source_path=source_path,
                source_mtime=source_mtime,
                env_file=env_file_arg,
            )
        except sqlite3.DatabaseError:
            continue
    if legacy_import_done is not None:
        _meta_set(conn, "legacy_import_done", legacy_import_done)
    _meta_set(conn, "last_rebuild_at", _now_iso())
    conn.commit()
    conn.close()
    return summary


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ── Legacy history.jsonl import ──────────────────────────────────────────


@dataclass
class ImportSummary:
    imported: int = 0
    skipped_existing: int = 0
    updated_repaired: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)


def _import_history_jsonl_into(
    conn: sqlite3.Connection,
    history_path: Path,
    env_file: Path | None = None,
) -> ImportSummary:
    """Stream ``history.jsonl`` into the open substrate connection."""
    summary = ImportSummary()
    if not history_path.exists():
        return summary
    try:
        with open(history_path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    summary.failed += 1
                    summary.failures.append(f"line {lineno}: {exc}")
                    continue
                if not isinstance(record, dict):
                    summary.failed += 1
                    summary.failures.append(f"line {lineno}: not an object")
                    continue
                try:
                    result = upsert_run_record(
                        conn,
                        record,
                        provenance="legacy_history_jsonl",
                        source_path=str(history_path.name),
                        env_file=env_file,
                    )
                except sqlite3.DatabaseError as exc:
                    summary.failed += 1
                    summary.failures.append(f"line {lineno}: {exc}")
                    continue
                _import_history_classify(result, summary)
    except OSError as exc:
        summary.failed += 1
        summary.failures.append(f"open: {exc}")
    conn.commit()
    return summary


def import_history_jsonl(project_root: Path) -> ImportSummary:
    """Public entry: open (or create) substrate and import legacy history."""
    conn = create_or_open(project_root)
    env_file = secrets_env_path(project_root)
    env_file_arg: Path | None = env_file if env_file.exists() else None
    try:
        summary = _import_history_jsonl_into(
            conn, history_jsonl_path(project_root), env_file=env_file_arg
        )
        _meta_set(conn, "legacy_import_done", "1")
        conn.commit()
        return summary
    finally:
        conn.close()


# ── Query helpers ────────────────────────────────────────────────────────


def has_review_approve_in_substrate(
    conn: sqlite3.Connection,
    slug: str,
    *,
    require_landed: bool = False,
) -> Iterable[dict]:
    """Yield raw_json dicts for matching APPROVE records.

    The caller is responsible for branch-staleness / unmerged-commits
    checks — those rely on git state and are not part of substrate
    semantics.
    """
    sql = (
        "SELECT a.raw_json, a.record_schema_version FROM audit_records a "
        "JOIN reviews r ON r.run_id = a.run_id "
        "WHERE a.slug = ? AND r.verdict = 'APPROVE'"
    )
    params: tuple = (slug,)
    if require_landed:
        sql += " AND a.landing_status = 'landed'"
    for row in conn.execute(sql, params):
        if isinstance(row, sqlite3.Row):
            raw, ver = row["raw_json"], row["record_schema_version"]
        else:
            raw, ver = row[0], row[1]
        record = _load_migrated(raw, ver)
        if record is not None:
            yield record


def iter_records(conn: sqlite3.Connection, *, order_by_started: bool = True) -> Iterable[dict]:
    """Iterate raw_json dicts for all audit records."""
    sql = "SELECT raw_json, record_schema_version FROM audit_records"
    if order_by_started:
        sql += " ORDER BY started_at ASC"
    for row in conn.execute(sql):
        if isinstance(row, sqlite3.Row):
            raw, ver = row["raw_json"], row["record_schema_version"]
        else:
            raw, ver = row[0], row[1]
        record = _load_migrated(raw, ver)
        if record is not None:
            yield record


def tail_records(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Return the most-recent ``limit`` records ordered by started_at DESC."""
    rows = conn.execute(
        "SELECT raw_json, record_schema_version FROM audit_records "
        "ORDER BY COALESCE(started_at, '') DESC LIMIT ?",
        (max(0, int(limit)),),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            raw, ver = row["raw_json"], row["record_schema_version"]
        else:
            raw, ver = row[0], row[1]
        record = _load_migrated(raw, ver)
        if record is not None:
            out.append(record)
    return out


def count_records(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()
    if row is None:
        return 0
    return int(row[0])


# ── Derived assignment-history view ──────────────────────────────────────


_COMPLEXITY_TO_BAND = {"small": "LOW", "medium": "MEDIUM", "large": "HIGH"}


def _coerce_complexity_score(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def derive_assignment_history(conn: sqlite3.Connection) -> list[dict]:
    """Return assignment-history records derived from per-run audit records.

    Replaces the YAML snapshot at ``.forge/assignment_history.yaml`` as the
    source of truth: each emitted dict has the same shape consumed by
    :func:`theforge.assignment.load_escalation_history` (story, complexity,
    dev_model, outcome, reason, timestamp, complexity_score). Records are
    ordered chronologically by ``timing.started_at``.

    Audit records that lack the routing/complexity fields needed to
    reconstruct an :class:`EscalationRecord` are skipped — they predate
    adaptive routing and were never represented in the legacy YAML either.

    This is the CLI/export view (mapped complexity bands, derives dev model
    from preflight routing assignments). Adaptive routing uses the more
    runtime-faithful :func:`iter_escalation_records` instead, which derives
    dev model from ``cost.agents`` (the model that actually ran).
    """
    out: list[dict] = []
    for record in iter_records(conn, order_by_started=True):
        slug = (record.get("task") or {}).get("slug")
        if not slug:
            continue
        outcome_block = record.get("outcome") or {}
        success = outcome_block.get("success")
        if not isinstance(success, bool):
            continue
        pre = record.get("preflight") if isinstance(record.get("preflight"), dict) else {}
        complexity_raw = pre.get("complexity")
        if not isinstance(complexity_raw, str) or not complexity_raw:
            continue
        complexity = _COMPLEXITY_TO_BAND.get(complexity_raw.lower(), complexity_raw.upper())
        routing_raw = pre.get("complexity_routing")
        routing = routing_raw if isinstance(routing_raw, dict) else {}
        assignments_raw = routing.get("assignments")
        assignments = assignments_raw if isinstance(assignments_raw, dict) else {}
        dev_raw = assignments.get("dev")
        dev = dev_raw if isinstance(dev_raw, dict) else {}
        dev_model = dev.get("canonical_id") or dev.get("model")
        # Fallback: when the audit record lacks the preflight.complexity_routing
        # block (older audits, or audits seeded by tests that bypass routing),
        # derive the canonical dev model from cost.agents — the model that
        # actually ran. provider/model/transport gives the same canonical_id
        # shape the routing block would have provided.
        if not (isinstance(dev_model, str) and dev_model):
            cost_block = record.get("cost") if isinstance(record.get("cost"), dict) else {}
            agents = cost_block.get("agents") if isinstance(cost_block.get("agents"), list) else []
            for entry in agents:
                if not isinstance(entry, dict):
                    continue
                if entry.get("phase") != "dev" and entry.get("role") != "dev":
                    continue
                provider = (entry.get("provider") or "").strip()
                model = (entry.get("model") or "").strip()
                cli = (entry.get("cli") or "").strip()
                if model and provider:
                    transport = "cli" if cli else "api"
                    dev_model = f"{provider}/{model}/{transport}"
                    break
                if entry.get("name"):
                    dev_model = str(entry["name"])
                    break
        if not isinstance(dev_model, str) or not dev_model:
            continue
        timing = record.get("timing") or {}
        timestamp = timing.get("started_at") or timing.get("finished_at") or ""
        escalation = record.get("escalation") if isinstance(record.get("escalation"), dict) else {}
        reason = escalation.get("reason") if isinstance(escalation.get("reason"), str) else ""
        out.append(
            {
                "story": str(slug),
                "complexity": complexity,
                "dev_model": str(dev_model),
                "outcome": "DONE" if success else "ESCALATE",
                "reason": reason or "",
                "timestamp": str(timestamp),
                "complexity_score": _coerce_complexity_score(pre.get("complexity_score")),
            }
        )
    return out


def iter_escalation_records(conn: sqlite3.Connection) -> Iterable[dict]:
    """Yield escalation-shaped dicts derived from audit records.

    Each row exposes the fields the adaptive router cares about for promotion
    bookkeeping: ``story``, ``complexity``, ``dev_model`` (canonicalized when
    available, else falls back to embedded dev profile name), ``outcome``
    ("DONE" if the run succeeded else "ESCALATE"), ``timestamp``, and
    ``complexity_score``. Records ordered by ``started_at`` ascending so
    callers can take the trailing window directly.

    This is the runtime-routing view (raw complexity string, derives dev
    model from ``cost.agents`` — the model that actually ran). The CLI/export
    view :func:`derive_assignment_history` uses preflight assignments
    instead, which is the *intended* dev model rather than the executed one.
    """
    rows = conn.execute(
        "SELECT raw_json, record_schema_version FROM audit_records "
        "ORDER BY COALESCE(started_at, '') ASC"
    )
    for row in rows:
        if isinstance(row, sqlite3.Row):
            raw, ver = row["raw_json"], row["record_schema_version"]
        else:
            raw, ver = row[0], row[1]
        record = _load_migrated(raw, ver)
        if record is None:
            continue
        derived = _derive_escalation(record)
        if derived is not None:
            yield derived


_COMPLEXITY_BAND_NORMALIZE = {
    "small": "LOW",
    "medium": "MEDIUM",
    "large": "HIGH",
    "low": "LOW",
    "high": "HIGH",
}


def _normalize_complexity_band(value: str) -> str:
    """Map small/medium/large (and other casing) to LOW/MEDIUM/HIGH.

    Mirrors ``theforge.assignment._normalize_complexity`` so substrate-derived
    escalation history uses the same key the promotion logic compares against.
    Empty input returns "" so callers can distinguish "no band recorded" from
    a normalized band.
    """
    if not value:
        return ""
    return _COMPLEXITY_BAND_NORMALIZE.get(value.lower(), value.upper())


def _derive_escalation(record: dict) -> dict | None:
    """Project an audit record to its escalation-shape projection."""
    task = record.get("task") or {}
    slug = str(task.get("slug") or "").strip()
    outcome = record.get("outcome") or {}
    success = outcome.get("success")
    if not isinstance(success, bool):
        return None
    preflight = record.get("preflight") if isinstance(record.get("preflight"), dict) else {}
    raw_complexity = str(preflight.get("complexity") or "").strip()
    # Promotion checks compare against LOW/MEDIUM/HIGH (see assignment._normalize_complexity).
    # Audit records persist the lower-case band (small/medium/large), so normalize on the
    # projection boundary so two ESCALATE rows for the same model actually match.
    complexity = _normalize_complexity_band(raw_complexity)

    # Canonical dev model identity, derived from the cost.agents block when
    # present (carries provider/model/cli identity per phase).
    dev_model = ""
    cost_block = record.get("cost") if isinstance(record.get("cost"), dict) else {}
    agents = cost_block.get("agents") if isinstance(cost_block.get("agents"), list) else []
    for entry in agents:
        if not isinstance(entry, dict):
            continue
        if entry.get("phase") != "dev" and entry.get("role") != "dev":
            continue
        provider = (entry.get("provider") or "").strip()
        model = (entry.get("model") or "").strip()
        cli = (entry.get("cli") or "").strip()
        if model and provider:
            transport = "cli" if cli else "api"
            dev_model = f"{provider}/{model}/{transport}"
            break
        if entry.get("name"):
            dev_model = str(entry["name"])
            break

    raw_score = preflight.get("complexity_score") if isinstance(preflight, dict) else None
    if isinstance(raw_score, bool):
        complexity_score: int | None = None
    elif isinstance(raw_score, int):
        complexity_score = raw_score
    elif isinstance(raw_score, float):
        complexity_score = int(raw_score)
    else:
        complexity_score = None

    timing = record.get("timing") or {}
    timestamp = str(timing.get("finished_at") or timing.get("started_at") or "")

    escalation_block = (
        record.get("escalation") if isinstance(record.get("escalation"), dict) else {}
    )
    reason = str(escalation_block.get("reason") or "")

    return {
        "story": slug,
        "complexity": complexity,
        "dev_model": dev_model,
        "outcome": "DONE" if success else "ESCALATE",
        "reason": reason,
        "timestamp": timestamp,
        "complexity_score": complexity_score,
    }


def record_readiness_event(project_root: Path, event: dict) -> int:
    """Insert a readiness event row into the audit substrate.

    Used by intake-readiness commands (``forge groom`` today; ``forge shape``
    and ``forge diagnose`` per ADR-0002 clause 6) to record what happened
    per invocation so refusal-economics and intake-trust queries can run
    against a single substrate table rather than parsing scattered logs.

    Returns the inserted row's ``event_id``. Stdlib only; raises
    :class:`SubstrateError` on I/O failure so callers can decide whether
    audit failure should fail the user's command.
    """
    required = {"kind", "issue_ref", "action"}
    missing = required - set(event)
    if missing:
        raise SubstrateError(f"readiness event missing required keys: {sorted(missing)}")
    raw_json = _canonical_json(event)
    emitted_at = _now_iso()
    conn = create_or_open(project_root)
    try:
        cur = conn.execute(
            "INSERT INTO readiness_events "
            "(kind, issue_ref, issue_type, pre_verdict, post_verdict, action, "
            "applied, bug_diagnosis_state, refusal_reason, emitted_at, raw_json, "
            "staleness_verdict, diagnosis_baseline_sha) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(event["kind"]),
                str(event["issue_ref"]),
                event.get("issue_type"),
                event.get("pre_verdict"),
                event.get("post_verdict"),
                str(event["action"]),
                1 if event.get("applied") else 0,
                event.get("bug_diagnosis_state"),
                event.get("refusal_reason"),
                emitted_at,
                raw_json,
                event.get("staleness_verdict"),
                event.get("diagnosis_baseline_sha"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def iter_readiness_events(conn: sqlite3.Connection, *, kind: str | None = None) -> Iterable[dict]:
    """Yield readiness-event dicts (parsed from raw_json), newest first."""
    if kind is not None:
        cur = conn.execute(
            "SELECT raw_json FROM readiness_events WHERE kind = ? ORDER BY event_id DESC",
            (kind,),
        )
    else:
        cur = conn.execute("SELECT raw_json FROM readiness_events ORDER BY event_id DESC")
    for row in cur:
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def record_shape_verdict_event(project_root: Path, event: dict) -> int:
    """Insert a shape-gate verdict row into the audit substrate.

    Each event captures the verdict assigned to a single issue at sprint
    entry so refusal-economics queries (verdict distribution per milestone,
    per-issue verdict trajectory) can run against the substrate without
    re-deriving from YAML/log surfaces. Returns the inserted row's
    ``event_id``. Raises :class:`SubstrateError` on missing required keys
    or I/O failure so callers can decide whether to swallow (sprint gate
    treats this as observability, not gating).
    """
    required = {"issue_id", "verdict"}
    missing = required - set(event)
    if missing:
        raise SubstrateError(f"shape verdict event missing required keys: {sorted(missing)}")
    raw_json = _canonical_json(event)
    emitted_at = event.get("emitted_at") or _now_iso()
    conn = create_or_open(project_root)
    try:
        cur = conn.execute(
            "INSERT INTO shape_verdict_events "
            "(issue_id, verdict, base_branch_sha, run_id, sprint_name, "
            "milestone, source, emitted_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(event["issue_id"]),
                str(event["verdict"]),
                event.get("base_branch_sha"),
                event.get("run_id"),
                event.get("sprint_name"),
                event.get("milestone"),
                event.get("source"),
                emitted_at,
                raw_json,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def iter_shape_verdict_events(
    conn: sqlite3.Connection,
    *,
    issue_id: str | None = None,
    verdict: str | None = None,
) -> Iterable[dict]:
    """Yield shape-verdict event dicts (parsed from raw_json), oldest first."""
    sql = "SELECT raw_json FROM shape_verdict_events"
    clauses: list[str] = []
    params: list = []
    if issue_id is not None:
        clauses.append("issue_id = ?")
        params.append(issue_id)
    if verdict is not None:
        clauses.append("verdict = ?")
        params.append(verdict)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY emitted_at ASC, event_id ASC"
    for row in conn.execute(sql, tuple(params)):
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def _skip_prior_block_history(
    conn: sqlite3.Connection, issue_id: str, reason_code: str
) -> tuple[int, str | None, str | None]:
    """Return ``(count, first_blocked_at, last_blocked_at)`` for prior blocks.

    Counts only ``severity='blocking'`` rows for this ``(issue_id, reason_code)``
    pair — advisories are not blocks and must not inflate the stuck-issue count.
    Used at write time to embed the prior skip history directly into a new
    record (issue #1453 AC1) so each record is self-describing without a
    follow-up query.
    """
    row = conn.execute(
        "SELECT COUNT(*), MIN(emitted_at), MAX(emitted_at) FROM shape_skip_events "
        "WHERE issue_id = ? AND reason_code = ? AND severity = 'blocking'",
        (issue_id, reason_code),
    ).fetchone()
    if row is None:
        return 0, None, None
    count = int(row[0] or 0)
    return count, row[1], row[2]


def record_shape_skip_event(project_root: Path, event: dict) -> int:
    """Insert a per-skip classification row into the audit substrate.

    Each event captures one ``(issue, reason_code)`` skip at sprint entry with
    its category / four-question axis / severity plus the issue's prior skip
    history on the same code (issue #1453 AC1). The prior-history fields
    (``prior_block_count``, ``first_blocked_at``, ``last_blocked_at``) are
    computed here from existing rows and embedded so the record is
    self-describing. Returns the inserted row's ``event_id``. Raises
    :class:`SubstrateError` on missing required keys so callers can decide
    whether to swallow (the sprint gate treats this as observability, not
    gating).
    """
    required = {"issue_id", "reason_code"}
    missing = required - set(event)
    if missing:
        raise SubstrateError(f"shape skip event missing required keys: {sorted(missing)}")
    issue_id = str(event["issue_id"])
    reason_code = str(event["reason_code"])
    emitted_at = event.get("emitted_at") or _now_iso()
    conn = create_or_open(project_root)
    try:
        prior_count, first_blocked, last_blocked = _skip_prior_block_history(
            conn, issue_id, reason_code
        )
        # Fold the computed history back into the persisted raw_json so the
        # canonical record and the indexed columns agree.
        enriched = dict(event)
        enriched["prior_block_count"] = prior_count
        enriched["first_blocked_at"] = first_blocked
        enriched["last_blocked_at"] = last_blocked
        enriched["emitted_at"] = emitted_at
        raw_json = _canonical_json(enriched)
        cur = conn.execute(
            "INSERT INTO shape_skip_events "
            "(issue_id, reason_code, source, severity, category, four_question_axis, "
            "run_id, sprint_id, sprint_name, milestone, prior_block_count, "
            "first_blocked_at, last_blocked_at, emitted_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                issue_id,
                reason_code,
                event.get("source"),
                event.get("severity"),
                event.get("category"),
                event.get("four_question_axis"),
                event.get("run_id"),
                event.get("sprint_id"),
                event.get("sprint_name"),
                event.get("milestone"),
                prior_count,
                first_blocked,
                last_blocked,
                emitted_at,
                raw_json,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def iter_shape_skip_events(
    conn: sqlite3.Connection,
    *,
    issue_id: str | None = None,
    reason_code: str | None = None,
    category: str | None = None,
    run_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> Iterable[dict]:
    """Yield shape-skip event dicts (parsed from raw_json), oldest first.

    Serves the AC6 query surface: filter by ``reason_code`` and an
    ``[since, until]`` ``emitted_at`` window to answer "all sprints in date
    range D where skip code C fired" in one call. ``since``/``until`` are
    inclusive ISO-8601 strings compared lexically (the emitted_at format is
    zero-padded UTC, so lexical order equals chronological order).
    """
    clauses: list[str] = []
    params: list = []
    if issue_id is not None:
        clauses.append("issue_id = ?")
        params.append(issue_id)
    if reason_code is not None:
        clauses.append("reason_code = ?")
        params.append(reason_code)
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if since is not None:
        clauses.append("emitted_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("emitted_at <= ?")
        params.append(until)
    sql = "SELECT raw_json FROM shape_skip_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY emitted_at ASC, event_id ASC"
    for row in conn.execute(sql, tuple(params)):
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def repeated_shape_skip_blocks(
    conn: sqlite3.Connection,
    *,
    threshold: int,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Return ``(issue_id, reason_code)`` pairs blocked ``>= threshold`` times.

    This is the stuck-issue detector (issue #1453 AC3): a pattern that surfaced
    #1135 and #1405 only via manual log-walking becomes a single grouped query.
    Only ``severity='blocking'`` rows count toward the threshold. Each result
    carries the block count, first/last block timestamps, and the distinct run
    ids that hit the block. Ordered by descending block count.
    """
    clauses = ["severity = 'blocking'"]
    params: list = []
    if since is not None:
        clauses.append("emitted_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("emitted_at <= ?")
        params.append(until)
    where = " WHERE " + " AND ".join(clauses)
    sql = (
        "SELECT issue_id, reason_code, COUNT(*) AS n, "
        "MIN(emitted_at) AS first_seen, MAX(emitted_at) AS last_seen "
        "FROM shape_skip_events" + where + " "
        "GROUP BY issue_id, reason_code HAVING n >= ? "
        "ORDER BY n DESC, issue_id ASC, reason_code ASC"
    )
    out: list[dict] = []
    for row in conn.execute(sql, tuple(params) + (int(threshold),)):
        issue_id = row[0] if not isinstance(row, sqlite3.Row) else row["issue_id"]
        reason_code = row[1] if not isinstance(row, sqlite3.Row) else row["reason_code"]
        n = row[2] if not isinstance(row, sqlite3.Row) else row["n"]
        first_seen = row[3] if not isinstance(row, sqlite3.Row) else row["first_seen"]
        last_seen = row[4] if not isinstance(row, sqlite3.Row) else row["last_seen"]
        run_rows = conn.execute(
            "SELECT DISTINCT run_id FROM shape_skip_events "
            "WHERE issue_id = ? AND reason_code = ? AND severity = 'blocking' "
            "AND run_id IS NOT NULL ORDER BY run_id",
            (issue_id, reason_code),
        ).fetchall()
        run_ids = [r[0] if not isinstance(r, sqlite3.Row) else r["run_id"] for r in run_rows]
        out.append(
            {
                "issue_id": issue_id,
                "reason_code": reason_code,
                "block_count": int(n),
                "first_seen": first_seen,
                "last_seen": last_seen,
                "run_ids": run_ids,
            }
        )
    return out


def seed_records(project_root: Path, records: Iterable[dict]) -> None:
    """Test helper: write records directly into the substrate as native rows.

    This is the supported way to bootstrap a substrate fixture in tests
    without going through the legacy ``history.jsonl`` import path.
    """
    conn = create_or_open(project_root)
    try:
        for record in records:
            upsert_run_record(conn, record, provenance="native")
        conn.commit()
    finally:
        conn.close()
