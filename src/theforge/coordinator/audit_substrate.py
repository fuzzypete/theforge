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

from .agent_identity import (
    dev_identity_ledger,
    dev_model_identity_detail,
    entry_identity_ledger,
    invocation_identity_rows,
)

# Substrate (SQLite) schema version. Bumped to 5 by #2201, which added
# ``audit_records.dev_model_source`` and repaired the dev-model projection:
# substrates written under version <= 4 carry an empty ``dev_model`` on every
# row, so opening an older one re-derives both columns from the stored
# ``raw_json`` (see :func:`_reindex_dev_model_identity`) instead of leaving the
# repaired projection unapplied to already-indexed history.
#
# Bumped to 6 by #2225: ``dev_model`` had been indexing whatever spelling the
# runner recorded, so one model split across several values. Canonicalization
# now resolves more spellings, and ``audit_records.dev_model_resolution``
# records whether a stored value is canonical or a verbatim fallback. Both
# changes have to reach already-indexed history, so a version-5 substrate is
# re-derived on open.
#
# Bumped to 7 by #2205: ``dev_model`` was one column standing in for three
# different facts — the identity the operator configured, the concrete one it
# resolved to, and the models actually billed. The configured/resolved pair now
# has its own columns (``dev_configured_model*`` / ``dev_resolved_model*``)
# alongside the ledger marker, so a consumer can attribute cost and outcome to
# each independently and can tell when they differ. ``dev_model`` stays as the
# resolved-identity compatibility column. A version-6 substrate is re-derived on
# open so the new columns reach already-indexed history wherever the record
# carries a ledger.
#
# Bumped to 8 by #2226: the configured/resolved pair existed only for the dev
# role and only one per run, so "what did this alias resolve to" was
# unanswerable for every other phase and for a run whose phases resolved
# differently. ``invocation_identities`` indexes that pair once per *recorded
# invocation* (role-neutral, drawn from ``cost.agents`` and the earlier
# ``preflight.attempts`` ledgers), leaving the ``dev_*`` columns in place as
# compatibility projections. A version-7 substrate is re-derived on open so the
# new table covers already-indexed history wherever the record carries ledgers.
#
# This is the DB-schema counter only: the new table is derived from the same
# record fields readers already parse, so no per-record ``MIGRATION_HELPERS``
# entry and no ``CURRENT_RECORD_SCHEMA_VERSION`` bump is implied.
SUBSTRATE_SCHEMA_VERSION = 8
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
CURRENT_RECORD_SCHEMA_VERSION = 26
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
    dev_model_source TEXT,
    dev_model_resolution TEXT,
    dev_configured_model TEXT,
    dev_configured_model_resolution TEXT,
    dev_resolved_model TEXT,
    dev_resolved_model_resolution TEXT,
    dev_identity_ledger_version INTEGER,
    dev_identity_ledger_full INTEGER,
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
CREATE TABLE IF NOT EXISTS invocation_identities (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    source TEXT NOT NULL,
    role TEXT,
    profile TEXT,
    configured_model TEXT,
    configured_model_resolution TEXT,
    resolved_model TEXT,
    resolved_model_resolution TEXT,
    configured_differs_from_resolved INTEGER,
    ledger_full INTEGER NOT NULL DEFAULT 0,
    ledger_version INTEGER,
    started_at TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_invocation_identities_configured
    ON invocation_identities(configured_model);
CREATE INDEX IF NOT EXISTS idx_invocation_identities_resolved
    ON invocation_identities(resolved_model);
CREATE INDEX IF NOT EXISTS idx_invocation_identities_role
    ON invocation_identities(role);
CREATE INDEX IF NOT EXISTS idx_invocation_identities_started
    ON invocation_identities(started_at);
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
    last_status TEXT,
    emitted_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shape_skip_events_issue ON shape_skip_events(issue_id);
CREATE INDEX IF NOT EXISTS idx_shape_skip_events_code ON shape_skip_events(reason_code);
CREATE INDEX IF NOT EXISTS idx_shape_skip_events_category ON shape_skip_events(category);
CREATE INDEX IF NOT EXISTS idx_shape_skip_events_run ON shape_skip_events(run_id);
CREATE INDEX IF NOT EXISTS idx_shape_skip_events_emitted ON shape_skip_events(emitted_at);
CREATE TABLE IF NOT EXISTS inline_remediation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    sprint_id TEXT,
    milestone TEXT,
    shape_verdict TEXT,
    action TEXT NOT NULL,
    succeeded INTEGER NOT NULL,
    cost_usd REAL,
    duration_seconds REAL,
    emitted_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inline_remediation_events_milestone
    ON inline_remediation_events(milestone);
CREATE INDEX IF NOT EXISTS idx_inline_remediation_events_issue
    ON inline_remediation_events(issue_id);
CREATE INDEX IF NOT EXISTS idx_inline_remediation_events_action
    ON inline_remediation_events(action);
"""


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    prior_version = _stored_schema_version(conn)
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
        "ALTER TABLE audit_records ADD COLUMN dev_model_source TEXT",
        "ALTER TABLE audit_records ADD COLUMN dev_model_resolution TEXT",
        "ALTER TABLE audit_records ADD COLUMN dev_configured_model TEXT",
        "ALTER TABLE audit_records ADD COLUMN dev_configured_model_resolution TEXT",
        "ALTER TABLE audit_records ADD COLUMN dev_resolved_model TEXT",
        "ALTER TABLE audit_records ADD COLUMN dev_resolved_model_resolution TEXT",
        "ALTER TABLE audit_records ADD COLUMN dev_identity_ledger_version INTEGER",
        "ALTER TABLE audit_records ADD COLUMN dev_identity_ledger_full INTEGER",
        "ALTER TABLE audit_records ADD COLUMN verdict TEXT",
        "ALTER TABLE readiness_events ADD COLUMN staleness_verdict TEXT",
        "ALTER TABLE readiness_events ADD COLUMN diagnosis_baseline_sha TEXT",
        "ALTER TABLE shape_skip_events ADD COLUMN last_status TEXT",
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
        "CREATE INDEX IF NOT EXISTS idx_audit_records_dev_configured_model "
        "ON audit_records(dev_configured_model)",
        "CREATE INDEX IF NOT EXISTS idx_audit_records_dev_resolved_model "
        "ON audit_records(dev_resolved_model)",
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
    # A substrate indexed under an older schema carries values derived by the
    # older projection — for dev_model that meant "empty on every row" (#2201).
    # Re-derive from the stored raw_json so the repair reaches history rather
    # than only new writes.
    if prior_version is not None and prior_version < SUBSTRATE_SCHEMA_VERSION:
        _reindex_dev_model_identity(conn)
        _reindex_invocation_identities(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("schema_version", str(SUBSTRATE_SCHEMA_VERSION)),
    )
    conn.commit()


def _stored_schema_version(conn: sqlite3.Connection) -> int | None:
    """Return the substrate schema version recorded in ``meta``, if any.

    ``None`` means a freshly created substrate (nothing indexed under an older
    projection), which needs no re-derivation.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    value = row[0] if not isinstance(row, sqlite3.Row) else row["value"]
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _dev_identity_columns(record: dict) -> dict:
    """Project a record to every indexed dev-identity column (#2205).

    One helper so the write path (:func:`_flat_fields`) and the repair path
    (:func:`_reindex_dev_model_identity`) cannot derive the index differently —
    the divergence that left ``dev_model`` empty on every row in #2201.

    ``dev_model`` remains the *resolved* identity, so existing consumers keep
    reading the model that actually ran. The configured identity is indexed
    beside it rather than folded into it, because attributing cost to the
    identity the operator selected and to the one it resolved to are different
    questions and a single column can only answer one.
    """
    identity, source, resolution = dev_model_identity_detail(record)
    ledger = dev_identity_ledger(record)
    configured = ledger["configured"]
    resolved = ledger["resolved"]
    return {
        "dev_model": identity,
        "dev_model_source": source,
        "dev_model_resolution": resolution,
        "dev_configured_model": configured[0] if configured else None,
        "dev_configured_model_resolution": configured[2] if configured else None,
        # Falls back to the single-identity projection for a legacy record: that
        # value *is* the resolved identity, just recovered rather than recorded.
        "dev_resolved_model": resolved[0] if resolved else identity,
        "dev_resolved_model_resolution": resolved[2] if resolved else resolution,
        "dev_identity_ledger_version": ledger["version"],
        # The "can a consumer tell a full record from a partial one" marker,
        # indexed so that question is answerable by query rather than by
        # re-parsing every raw_json.
        "dev_identity_ledger_full": 1 if ledger["full_ledger"] else 0,
    }


_DEV_IDENTITY_COLUMNS: tuple[str, ...] = (
    "dev_model",
    "dev_model_source",
    "dev_model_resolution",
    "dev_configured_model",
    "dev_configured_model_resolution",
    "dev_resolved_model",
    "dev_resolved_model_resolution",
    "dev_identity_ledger_version",
    "dev_identity_ledger_full",
)


_INVOCATION_IDENTITY_COLUMNS: tuple[str, ...] = (
    "run_id",
    "seq",
    "source",
    "role",
    "profile",
    "configured_model",
    "configured_model_resolution",
    "resolved_model",
    "resolved_model_resolution",
    "configured_differs_from_resolved",
    "ledger_full",
    "ledger_version",
    "started_at",
)


def _invocation_identity_params(run_id: str, record: dict) -> list[tuple]:
    """Project a record to the ``invocation_identities`` rows it justifies.

    ``seq`` is assigned by the extraction order (``cost.agents`` first, then the
    ``preflight.attempts`` entries that are not already covered by it), so it is
    stable for a given record and a rewrite of the same run produces the same
    keys rather than accumulating.
    """
    started_at = (record.get("timing") or {}).get("started_at")
    params: list[tuple] = []
    for seq, row in enumerate(invocation_identity_rows(record)):
        configured = row["configured"]
        resolved = row["resolved"]
        differs = row["differs"]
        params.append(
            (
                run_id,
                seq,
                row["source"],
                row["role"],
                row["profile"],
                configured[0] if configured else None,
                configured[2] if configured else None,
                resolved[0] if resolved else None,
                resolved[2] if resolved else None,
                None if differs is None else (1 if differs else 0),
                1 if row["full_ledger"] else 0,
                row["version"],
                started_at if isinstance(started_at, str) else None,
            )
        )
    return params


def _write_invocation_identities(conn: sqlite3.Connection, run_id: str, record: dict) -> int:
    """Rewrite the invocation-identity rows for one run. Returns rows written."""
    conn.execute("DELETE FROM invocation_identities WHERE run_id = ?", (run_id,))
    params = _invocation_identity_params(run_id, record)
    if not params:
        return 0
    names = ", ".join(_INVOCATION_IDENTITY_COLUMNS)
    placeholders = ", ".join(["?"] * len(_INVOCATION_IDENTITY_COLUMNS))
    conn.executemany(
        f"INSERT OR REPLACE INTO invocation_identities({names}) VALUES ({placeholders})",
        params,
    )
    return len(params)


def _reindex_invocation_identities(conn: sqlite3.Connection) -> int:
    """Derive ``invocation_identities`` for every already-indexed run (#2226).

    Modelled on :func:`_reindex_dev_model_identity`: ``raw_json`` is the record,
    so a row imported from legacy history is covered alongside a native one, and
    a record whose JSON carries no ledger simply contributes no rows rather than
    contributing guessed ones. Returns the number of *runs* that produced rows.
    """
    try:
        rows = conn.execute("SELECT run_id, raw_json FROM audit_records").fetchall()
    except sqlite3.OperationalError:
        return 0
    updated = 0
    for row in rows:
        run_id = row[0] if not isinstance(row, sqlite3.Row) else row["run_id"]
        raw = row[1] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if _write_invocation_identities(conn, str(run_id), record):
            updated += 1
    return updated


def _reindex_dev_model_identity(conn: sqlite3.Connection) -> int:
    """Re-derive the ``dev_model*`` columns from each row's raw_json.

    The canonical per-run JSON is not needed — ``raw_json`` is the record, so
    rows imported from legacy history are repaired alongside native ones. Rows
    whose record carries no trustworthy invocation identity are left alone:
    the projection has nothing truthful to write for them, and clearing an
    existing value would destroy data the record still justifies.

    Returns the number of rows updated (used by tests and callers that want to
    report the repair).
    """
    try:
        rows = conn.execute("SELECT run_id, raw_json FROM audit_records").fetchall()
    except sqlite3.OperationalError:
        return 0
    updated = 0
    for row in rows:
        run_id = row[0] if not isinstance(row, sqlite3.Row) else row["run_id"]
        raw = row[1] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        columns = _dev_identity_columns(record)
        if not columns["dev_model"] and not columns["dev_configured_model"]:
            continue
        assignments = ", ".join(f"{name} = ?" for name in _DEV_IDENTITY_COLUMNS)
        conn.execute(
            f"UPDATE audit_records SET {assignments} WHERE run_id = ?",
            (*(columns[name] for name in _DEV_IDENTITY_COLUMNS), run_id),
        )
        updated += 1
    return updated


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


def open_readonly(project_root: Path) -> sqlite3.Connection:
    """Open the substrate strictly read-only — never create, migrate, or rebuild.

    For operator-facing query surfaces (e.g. ``forge explain``) that must not
    mutate the substrate as a side effect of reading it. Unlike
    :func:`require_substrate` (which rebuilds stale/missing indexes) and
    :func:`create_or_open` (which bootstraps a fresh DB and applies schema),
    this opens the existing file with SQLite ``mode=ro`` so no write — not even
    a schema migration — is possible. Raises :class:`SubstrateMissingError`
    when the index file is absent, pointing the operator at
    ``forge audits rebuild`` rather than silently regenerating it.
    """
    path = substrate_path(project_root)
    if not path.exists():
        raise SubstrateMissingError(
            f"audit substrate not found at {path}. Run `forge audits rebuild` to create it."
        )
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.DatabaseError as exc:
        raise SubstrateCorruptError(
            f"audit substrate at {path} could not be opened read-only: {exc}. "
            "Run `forge audits rebuild [--include-legacy-history]` to recover."
        ) from exc
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
        **_dev_identity_columns(record),
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


def _migrate_v5_to_v6(record: dict) -> dict:
    """Add top-level ``symptom_test_escalations`` (issue #1560).

    v5 records never recorded the P2→P1 escalations applied when a bug-fix PR's
    reviewer flagged an absent seam-level test for the closing bug's symptom
    path. v6 adds the field so the rule's hit-rate becomes queryable. Older
    records ran before the rule existed and never escalated, so backfill None
    (the "no escalations" shape) rather than fabricating one.
    """
    if "symptom_test_escalations" in record:
        return record
    return {**record, "symptom_test_escalations": None}


def _migrate_v6_to_v7(record: dict) -> dict:
    """Add top-level ``routing_decision`` (issue #1391).

    v6 records captured only the outcome-oriented ``preflight.complexity_routing``
    summary (final per-role picks + free-text rationale). v7 adds the top-level
    ``routing_decision`` explainability block — candidate pool, canonical
    exclusion reasons, profile signals, adaptive-check outcomes, exploration
    state, and origin-labeled final rationale (ADR-0006 clause 7). Older records
    ran before the block existed and cannot be reconstructed after the fact, so
    backfill None (the "not recorded" shape) rather than fabricating one.
    """
    if "routing_decision" in record:
        return record
    return {**record, "routing_decision": None}


def _migrate_v7_to_v8(record: dict) -> dict:
    """Add top-level ``trust_status``/``trust_checks`` (issue #1851).

    v7 records carried no machine-readable marker for whether a run failed its
    own trust checks — the reviewer tree-currency / certainty verification
    (#1826) existed only as prose in review output. v8 promotes it to structured
    telemetry so v0.13 routing can exclude tainted runs from aggregates
    (ADR-0006 clause 4). Older records ran before any trust check existed and
    cannot be reconstructed after the fact, so backfill the "unchecked" default
    (empty ``trust_checks``) rather than fabricating a verdict — taint requires
    an affirmative failed check, not the absence of one. The record itself is
    never rewritten in the substrate (ADR-0002 refusal-to-forget); this is the
    reader-side lift applied on load.
    """
    from .trust_status import TRUST_UNCHECKED  # noqa: PLC0415

    migrated = record
    if "trust_status" not in migrated:
        migrated = {**migrated, "trust_status": TRUST_UNCHECKED}
    if "trust_checks" not in migrated:
        migrated = {**migrated, "trust_checks": []}
    return migrated


def _migrate_v8_to_v9(record: dict) -> dict:
    """Add top-level ``reviewer_attempts`` (issue #1388).

    v8 records carried reviewer data only as run-level review *cycles*
    (verdict/p1/p2) — a survivorship-biased view in which a reviewer that timed
    out, crashed, or returned unparseable output simply vanished from the record.
    v9 promotes every reviewer invocation to a structured attempt record with a
    ``completed_parseable_verdict`` boolean so reviewer completion-rate routing
    (ADR-0006 clause 2) has complete-over-attempts evidence. Older records ran
    before any reviewer-attempt telemetry existed and cannot be reconstructed
    after the fact, so backfill an empty list rather than fabricating attempts —
    an absent record is "no evidence", not "zero completions". The record itself
    is never rewritten in the substrate (ADR-0002 refusal-to-forget); this is the
    reader-side lift applied on load.
    """
    if "reviewer_attempts" not in record:
        return {**record, "reviewer_attempts": []}
    return record


def _migrate_v9_to_v10(record: dict) -> dict:
    """Add sandbox capability-profile grants to the substrate record (issue #1947).

    v9 records captured *whether* a dev run was contained (``sandboxed`` /
    ``containment``) but not *to what* — the containment boundary was a fixed
    allow-set, so there was nothing to record. v10 lets a project widen that
    boundary by selecting a forge-owned capability preset, which makes the
    granted write roots and mach services a substrate decision a reviewer must
    be able to see.

    Every v9 record ran under the default (unwidened) boundary by construction,
    so backfilling the explicit null-profile/empty-grant payload is a faithful
    statement of what those runs were granted, not a fabricated default. The
    record itself is never rewritten in the substrate (ADR-0002
    refusal-to-forget); this is the reader-side lift applied on load.
    """
    from theforge.config.sandbox_capabilities import resolve_capabilities  # noqa: PLC0415

    def default_capability_payload() -> dict:
        return resolve_capabilities(None).audit_payload()

    migrated = record
    workspace = migrated.get("workspace")
    if isinstance(workspace, dict) and "sandbox_capabilities" not in workspace:
        migrated = {
            **migrated,
            "workspace": {**workspace, "sandbox_capabilities": default_capability_payload()},
        }
    iterations = migrated.get("iterations")
    if isinstance(iterations, dict) and isinstance(iterations.get("dev_loop"), list):
        dev_loop = [
            item
            if not isinstance(item, dict) or "sandbox_capabilities" in item
            else {**item, "sandbox_capabilities": default_capability_payload()}
            for item in iterations["dev_loop"]
        ]
        migrated = {**migrated, "iterations": {**iterations, "dev_loop": dev_loop}}
    return migrated


def _migrate_v10_to_v11(record: dict) -> dict:
    """Add top-level ``agent_invocation`` (issue #1951).

    v10 records could not distinguish an agent that judged the work from one
    that never answered: a transport/auth failure was folded into whatever
    verdict the phase could express (PROCEED / REJECT / ESCALATE) and the
    substrate saw only the manufactured verdict. v11 records the distinction
    directly — which invocations produced no model output, whether the run
    itself aborted for want of a judgment, and which pools completed degraded.

    Older records cannot be reclassified after the fact (the evidence that would
    separate the two was never written), so backfill the "not recorded" shape —
    null infrastructure failure, empty lists — rather than inferring a cause
    from an outcome. The record itself is never rewritten in the substrate
    (ADR-0002 refusal-to-forget); this is the reader-side lift applied on load.
    """
    if "agent_invocation" in record:
        return record
    return {
        **record,
        "agent_invocation": {
            "infrastructure_failure": None,
            "no_judgment_failures": [],
            "degraded_pools": [],
        },
    }


def _migrate_v11_to_v12(record: dict) -> dict:
    """Split VALIDATE-opened review cycles out of the reviewer counts (issue #1981).

    v11 recorded one number for review cycles, so a cycle the coordinator opened
    for its own gate or convention finding was indistinguishable from one a
    reviewer verdict opened — and the adaptive iteration learner read that number
    as reviewer demand. v12 reports ``review_cycles_opened_by_validate``
    alongside, keeps ``review_cycles_total`` reviewer-only, and adds top-level
    ``validate_blocks`` describing each coordinator-raised block.

    A v11 record cannot be re-attributed after the fact: the evidence that would
    separate the two was never written. Backfill the "none recorded" shape — zero
    VALIDATE-opened cycles, null blocks — which leaves ``review_cycles_total``
    reading exactly as it did when written, rather than inferring cycles from an
    outcome. The stored record is never rewritten (ADR-0002 refusal-to-forget);
    this is the reader-side lift applied on load.
    """
    migrated = dict(record)
    migrated.setdefault("validate_blocks", None)
    for block_key in ("iterations", "totals"):
        block = migrated.get(block_key)
        if isinstance(block, dict) and "review_cycles_opened_by_validate" not in block:
            migrated[block_key] = {**block, "review_cycles_opened_by_validate": 0}
    return migrated


def _migrate_v12_to_v13(record: dict) -> dict:
    """Record gate executions separately from gate decisions (issue #1984).

    Through v12 the only gate counter was ``iterations.gate_decisions``, which
    gains an entry only when the gate returned a decision (so timeouts and
    errors are absent) and gains a synthetic ``PASS`` when ``gate_override``
    skipped the gate (so non-runs are present). v13 adds ``iterations.gate_runs``:
    the number of times the gate command actually executed.

    A v12 record cannot be back-derived: neither the missing timeout runs nor
    the skipped-gate entries were distinguishable in what was written, so
    ``len(gate_decisions)`` is exactly the wrong number this field exists to
    replace. Backfill ``None`` — "not recorded" — rather than inferring a count
    the evidence does not support. The stored record is never rewritten
    (ADR-0002 refusal-to-forget); this is the reader-side lift applied on load.
    """
    migrated = dict(record)
    block = migrated.get("iterations")
    if isinstance(block, dict) and "gate_runs" not in block:
        migrated["iterations"] = {**block, "gate_runs": None}
    return migrated


def _migrate_v13_to_v14(record: dict) -> dict:
    """Rename the gate telemetry ``iteration`` to ``trace_index`` (issue #1986).

    Through v13 one record used the field name ``iteration`` for two different
    counters: ``iterations.dev_loop[].iteration`` is per review cycle and resets,
    while ``iterations.gate_debug[].iteration`` /
    ``iterations.gate_diagnostic[].iteration`` is the monotonic counter that also
    names ``.forge/traces/{n}-gate-*.txt``. From the second review cycle on the
    two disagreed, so a trace path quoted in an escalation no longer matched the
    dev entry it belonged to. v14 names the monotonic counter ``trace_index`` and
    adds ``trace_path``, the exact file the entry's output was written to.

    The old value *is* the trace counter, so renaming it in place is faithful
    rather than inferred, and the trace path it named is reconstructible from it
    by the same rule the writer used. The stored record is never rewritten
    (ADR-0002 refusal-to-forget); this is the reader-side lift applied on load.
    """
    iterations = record.get("iterations")
    if not isinstance(iterations, dict):
        return record
    migrated_blocks: dict[str, list] = {}
    for key, suffix in (("gate_debug", "gate-debug"), ("gate_diagnostic", "gate-diagnostic")):
        entries = iterations.get(key)
        if not isinstance(entries, list):
            continue
        lifted = []
        for entry in entries:
            if not isinstance(entry, dict) or "trace_index" in entry:
                lifted.append(entry)
                continue
            new_entry = {k: v for k, v in entry.items() if k != "iteration"}
            index = entry.get("iteration")
            new_entry["trace_index"] = index
            new_entry["trace_path"] = (
                f".forge/traces/{index}-{suffix}.txt" if isinstance(index, int) else None
            )
            lifted.append(new_entry)
        migrated_blocks[key] = lifted
    if not migrated_blocks:
        return record
    return {**record, "iterations": {**iterations, **migrated_blocks}}


def _migrate_v14_to_v15(record: dict) -> dict:
    """Add the dev verification request trail (ADR-0007 / issue #2050).

    v15 records every project-declared verification command the coordinator ran
    *outside* the dev sandbox on the agent's behalf: per iteration in
    ``iterations.dev_loop[].verification_requests`` and as a run-level roll-up in
    ``workspace.dev_verification_requests``.

    A v14 record predates the capability entirely — no such command could have
    run — so backfilling an empty list is a faithful statement rather than an
    inference: nothing ran unconfined at the dev agent's request, because nothing
    could. The stored record is never rewritten (ADR-0002 refusal-to-forget);
    this is the reader-side lift applied on load.
    """
    migrated = dict(record)
    workspace = migrated.get("workspace")
    if isinstance(workspace, dict) and "dev_verification_requests" not in workspace:
        migrated["workspace"] = {**workspace, "dev_verification_requests": []}
    iterations = migrated.get("iterations")
    if isinstance(iterations, dict) and isinstance(iterations.get("dev_loop"), list):
        migrated["iterations"] = {
            **iterations,
            "dev_loop": [
                entry
                if not isinstance(entry, dict) or "verification_requests" in entry
                else {**entry, "verification_requests": []}
                for entry in iterations["dev_loop"]
            ],
        }
    return migrated


def _migrate_v15_to_v16(record: dict) -> dict:
    """Add the run-level configuration-provenance block (issue #2056).

    v16 records name the configuration the run executed under: a resolved-config
    digest, the ``forge.yaml`` path and its digest at load time, and whether that
    file changed while the run was in flight.

    A v15 record has no such identity to recover — the configuration was never
    fingerprinted, so there is nothing to reconstruct without leaving the audit
    trail. Backfilling ``None`` (rather than an empty block of nulls) keeps the
    two cases distinguishable on read: a historical record that *cannot* name its
    configuration versus a new record that explicitly could not determine one.
    The stored record is never rewritten (ADR-0002 refusal-to-forget).
    """
    if "configuration" in record:
        return record
    return {**record, "configuration": None}


def _migrate_v16_to_v17(record: dict) -> dict:
    """Add the shared run-infrastructure failure ledger (issue #2107).

    v17 records list failures of resources every story of a sprint shares — a
    path outside the workspace that all workers write, such as the rolling
    advisory-conventions artifact — so such a failure is attributable to the
    infrastructure rather than to whichever story was executing when it
    surfaced.

    A v16 record predates the ledger: the failure was not distinguished from the
    story's own outcome, so no such list could exist. Backfilling an empty list
    states exactly that — nothing was recorded — without inventing entries the
    writer never observed. The stored record is never rewritten (ADR-0002
    refusal-to-forget); this is the reader-side lift applied on load.
    """
    if "shared_infrastructure_failures" in record:
        return record
    return {**record, "shared_infrastructure_failures": []}


def _migrate_v17_to_v18(record: dict) -> dict:
    """Add the abnormal-termination record (issue #2030).

    v18 records name how a run ended when it did not end by its own state
    machine — dropped at launch, killed by a worker exception, cancelled at the
    worker deadline — so the cause of such a failure is the run's own structured
    telemetry instead of the agent's prose about itself.

    A v17 record predates the field. ``None`` is the honest backfill: it says the
    writer recorded no abnormal termination, which is true of every normally
    terminating run and is not a claim that an abnormal one ended cleanly — the
    ``error`` / ``error_type`` fields those records already carry stay
    authoritative. The stored record is never rewritten (ADR-0002
    refusal-to-forget); this is the reader-side lift applied on load.
    """
    if "abnormal_termination" in record:
        return record
    return {**record, "abnormal_termination": None}


def _migrate_v18_to_v19(record: dict) -> dict:
    """Add the durable-phase-recovery block (issue #2155).

    v19 records name which phase outputs a resumed attempt lifted off the
    durable phase record — and, when none was usable, say that instead of
    letting an absent ``preflight`` block read as a phase that never ran.

    A v18 record predates both the field and the sidecar that feeds it. ``None``
    is the honest backfill: it says the writer recorded no recovery, which is
    true of every run that produced its own phase outputs. It is emphatically
    NOT a claim that a v18 resumed run's ``preflight.verdict: SKIPPED`` was a
    real bypass — that ambiguity is exactly what this version exists to end, and
    it cannot be resolved retroactively from the record alone. The stored record
    is never rewritten (ADR-0002 refusal-to-forget); this is the reader-side lift
    applied on load.
    """
    if "phase_recovery" in record:
        return record
    return {**record, "phase_recovery": None}


def _migrate_v19_to_v20(record: dict) -> dict:
    """Add the per-story budget allocation block under ``cost`` (issue #2169).

    v20 records carry the allocation a story was governed by — derived from the
    observed cost distribution for its complexity band — alongside what it
    actually spent, plus any allocation-exhausted condition and per-reviewer
    share overruns.

    A v19 record was governed by the flat configured ceiling, so there is no
    band-derived allocation to reconstruct. ``None`` is the honest backfill: it
    says this run's spend was never measured against a band, which is exactly
    true. ``reviewer_budget_overruns`` backfills to ``[]`` — a v19 run that went
    over a reviewer share recorded that as an *exclusion* in
    ``reviewer_attempts``, not as a retained-verdict overrun, and relabelling it
    here would assert a decision the run did not make.
    """
    cost = record.get("cost")
    if not isinstance(cost, dict) or "story_allocation" in cost:
        return record
    return {
        **record,
        "cost": {
            **cost,
            "story_allocation": None,
            "allocation_exhausted": None,
            "reviewer_budget_overruns": [],
        },
    }


def _migrate_v20_to_v21(record: dict) -> dict:
    """Add the per-invocation identity ledger to ``cost.agents`` (issue #2205).

    v21 entries carry a ``ledger`` block naming three things a v20 entry
    collapsed into one: the identity the run was configured with, the concrete
    identity that configuration resolved to at invocation time, and every model
    actually billed inside the invocation — plus the conditions it ran under
    (role, complexity, reasoning effort), its usage counts by class, and whether
    the observed cost was reported by the provider or estimated by forge.

    No ledger is fabricated here. A v20 entry recorded one identity with no
    statement of *which* of the three it was, so synthesizing a block would
    assert a configured-equals-resolved fact the record never established — and
    that fabricated equality is precisely the signal #2205 exists to make
    readable. Older entries are left exactly as written; the reader
    (:func:`agent_identity.entry_identity_ledger`) reports ``full_ledger:
    False`` for them, which is what lets a consumer tell a record that carries
    the full set from one that does not. The stored record is never rewritten
    (ADR-0002 refusal-to-forget); this is the reader-side lift applied on load.
    """
    return record


def _migrate_v21_to_v22(record: dict) -> dict:
    """Add ``landing_review`` — which review a landing was taken on (issue #2300).

    v22 records name the provenance of the ReviewResult that landed:
    ``merged_cycle_review`` for the ordinary path, or ``escalate_gate_selection``
    when an operator accepted at the escalate gate on a reviewer verdict that
    survived a quorum collapse and therefore never became a merged review.

    ``None`` is the honest backfill for a v21 record, and no provenance is
    inferred from the presence of review results. A v21 landing was resolved by
    re-reading ``review_results[-1]`` at landing time, so the record never
    established WHICH review the merge actually carried — synthesizing
    ``merged_cycle_review`` here would assert exactly the fact this field exists
    to make checkable. Older records read as "this run did not say", which is
    true. The stored record is never rewritten (ADR-0002 refusal-to-forget).
    """
    if "landing_review" in record:
        return record
    return {**record, "landing_review": None}


def _migrate_v22_to_v23(record: dict) -> dict:
    """Add workspace story provenance — which story text produced the tree (issue #2288).

    v23 records say whether the workspace the run executed in was created fresh,
    adopted from an earlier attempt against the same story text, or adopted
    against text that has since changed — and whether the dev agent was told it
    had inherited superseded work.

    ``None`` is the honest backfill for ``story_provenance``: a v22 run adopted
    an existing worktree on identity alone and never asked the question, so
    claiming ``fresh_worktree`` or ``story_content_match`` would assert exactly
    the fact this field exists to make checkable. The two booleans backfill
    False because no v22 run could have detected the condition or told an agent
    about it — neither channel existed. The stored record is never rewritten
    (ADR-0002 refusal-to-forget).
    """
    workspace = record.get("workspace")
    if not isinstance(workspace, dict) or "story_provenance" in workspace:
        return record
    return {
        **record,
        "workspace": {
            **workspace,
            "story_provenance": None,
            "inherited_superseded_work": False,
            "inherited_work_surfaced_to_dev": False,
        },
    }


def _migrate_v23_to_v24(record: dict) -> dict:
    """Add per-invocation process teardown to ``cost.agents`` (issue #2309).

    v24 entries say whether the invocation's process group outlived it and had
    to be killed — the fact a leaked ``pytest -n auto`` tree previously left no
    trace of anywhere, since a surviving process produces no artifact and no
    cost of its own.

    ``None`` is the honest backfill: a v23 run never checked, so its group could
    have emptied on its own or could have leaked, and writing "no teardown" here
    would assert the very thing this field exists to establish. Older entries
    read as "this run did not say". The stored record is never rewritten
    (ADR-0002 refusal-to-forget); this is the reader-side lift applied on load.
    """
    cost = record.get("cost")
    if not isinstance(cost, dict):
        return record
    agents = cost.get("agents")
    if not isinstance(agents, list):
        return record
    migrated_agents = []
    for entry in agents:
        if isinstance(entry, dict) and "process_teardown" not in entry:
            migrated_agents.append({**entry, "process_teardown": None})
        else:
            migrated_agents.append(entry)
    return {**record, "cost": {**cost, "agents": migrated_agents}}


def _migrate_v24_to_v25(record: dict) -> dict:
    """Name which validation shell each recorded teardown came from (issue #2309).

    Four commands run in the validate phase — the gate, the debug command and the
    diagnostic pass after a timeout, and the pre-validate command after a pass —
    and any of them can leave workers behind. v24 recorded the teardown without
    saying which, so a reader could only guess which trace to open.

    ``"gate"`` is the correct backfill rather than a null: v24 only ever
    collected teardowns from the gate-family shells, and the pre-validate path
    that made the distinction necessary did not record at all. So an older entry
    is not "unknown" — it is known to be one of those, and the field names the
    one the record already implied. The stored record is never rewritten
    (ADR-0002 refusal-to-forget); this is the reader-side lift applied on load.
    """
    iterations = record.get("iterations")
    if not isinstance(iterations, dict):
        return record
    teardowns = iterations.get("gate_process_teardowns")
    if not isinstance(teardowns, list):
        return record
    migrated = [
        {**entry, "source": "gate"} if isinstance(entry, dict) and "source" not in entry else entry
        for entry in teardowns
    ]
    return {**record, "iterations": {**iterations, "gate_process_teardowns": migrated}}


def _migrate_v25_to_v26(record: dict) -> dict:
    """Backfill the empty development-timeout clamp ledger (issue #2333).

    v26 records, per development invocation, any allowance that was shortened to
    fit the enclosing story deadline. A v25 record predates the clamp entirely,
    so the honest backfill is the empty list: not "unknown", but known to have
    had no clamps, because the mechanism that produces them did not exist. The
    stored record is never rewritten (ADR-0002 refusal-to-forget); this is the
    reader-side lift applied on load.
    """
    iterations = record.get("iterations")
    if not isinstance(iterations, dict) or "dev_timeout_clamps" in iterations:
        return record
    return {**record, "iterations": {**iterations, "dev_timeout_clamps": []}}


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
    5: _migrate_v5_to_v6,
    6: _migrate_v6_to_v7,
    7: _migrate_v7_to_v8,
    8: _migrate_v8_to_v9,
    9: _migrate_v9_to_v10,
    10: _migrate_v10_to_v11,
    11: _migrate_v11_to_v12,
    12: _migrate_v12_to_v13,
    13: _migrate_v13_to_v14,
    14: _migrate_v14_to_v15,
    15: _migrate_v15_to_v16,
    16: _migrate_v16_to_v17,
    17: _migrate_v17_to_v18,
    18: _migrate_v18_to_v19,
    19: _migrate_v19_to_v20,
    20: _migrate_v20_to_v21,
    21: _migrate_v21_to_v22,
    22: _migrate_v22_to_v23,
    23: _migrate_v23_to_v24,
    24: _migrate_v24_to_v25,
    25: _migrate_v25_to_v26,
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
        *(flat[name] for name in _DEV_IDENTITY_COLUMNS),
        flat["verdict"],
        raw_json,
    )
    _dev_identity_names = ", ".join(_DEV_IDENTITY_COLUMNS)
    _dev_identity_updates = ", ".join(f"{name}=excluded.{name}" for name in _DEV_IDENTITY_COLUMNS)
    _placeholders = ", ".join(["?"] * (16 + len(_DEV_IDENTITY_COLUMNS) + 2))
    conn.execute(
        "INSERT INTO audit_records "
        "(run_id, slug, started_at, finished_at, total_cost_usd, final_phase, "
        "outcome_success, branch, landing_status, provenance, source_path, "
        "source_mtime, complexity_score, record_schema_version, milestone, "
        f"issue_id, {_dev_identity_names}, verdict, raw_json) "
        f"VALUES ({_placeholders}) "
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
        f"{_dev_identity_updates}, verdict=excluded.verdict, "
        "raw_json=excluded.raw_json",
        params,
    )
    # Rewrite the per-invocation identity rows for this run_id (#2226), on the
    # same delete-then-insert discipline as reviews below so a re-upsert of the
    # same run replaces rather than accumulates.
    _write_invocation_identities(conn, run_id, record)
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

    Shape-gate skip events (``shape_skip_events``, issue #1453) are likewise
    snapshotted and re-applied. They are the canonical per-skip audit record
    and are not derivable from the per-run JSON files, so a rebuild that
    dropped them would lose the very skip history the observability layer
    exists to expose — including the repeated-block patterns that surfaced
    #1135 and #1405. Rows are restored verbatim (prior-block counts and
    computed status preserved) rather than recomputed.
    """
    path = substrate_path(project_root)
    legacy_snapshot: list[tuple[str, str, str | None, float | None]] = []
    skip_event_snapshot: list[tuple] = []
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
                skip_event_snapshot = _snapshot_shape_skip_events(existing_conn)
                legacy_import_done = _meta_get(existing_conn, "legacy_import_done")
            finally:
                existing_conn.close()
        except sqlite3.DatabaseError:
            # Corrupt substrate — nothing usable to preserve. Operator
            # must run `forge audits rebuild --include-legacy-history`
            # explicitly to recover legacy data.
            legacy_snapshot = []
            skip_event_snapshot = []
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
    # Restore preserved shape-skip events. Re-applied verbatim (no history
    # recomputation) so counts/status match what was recorded at emit time.
    _restore_shape_skip_events(conn, skip_event_snapshot)
    if legacy_import_done is not None:
        _meta_set(conn, "legacy_import_done", legacy_import_done)
    _meta_set(conn, "last_rebuild_at", _now_iso())
    conn.commit()
    conn.close()
    return summary


# Column order shared by the shape-skip snapshot/restore pair so the two stay in
# lockstep — a column added to one must be added to the other.
_SHAPE_SKIP_SNAPSHOT_COLUMNS = (
    "issue_id",
    "reason_code",
    "source",
    "severity",
    "category",
    "four_question_axis",
    "run_id",
    "sprint_id",
    "sprint_name",
    "milestone",
    "prior_block_count",
    "first_blocked_at",
    "last_blocked_at",
    "last_status",
    "emitted_at",
    "raw_json",
)


def _snapshot_shape_skip_events(conn: sqlite3.Connection) -> list[tuple]:
    """Return every ``shape_skip_events`` row (sans ``event_id``) for rebuild.

    Ordered by ``emitted_at`` so restore re-inserts them chronologically and any
    auto-assigned ``event_id`` preserves emission order.
    """
    cols = ", ".join(_SHAPE_SKIP_SNAPSHOT_COLUMNS)
    try:
        rows = conn.execute(
            f"SELECT {cols} FROM shape_skip_events ORDER BY emitted_at ASC, event_id ASC"
        ).fetchall()
    except sqlite3.OperationalError:
        # Substrate predates the table — nothing to preserve.
        return []
    return [tuple(row) for row in rows]


def _restore_shape_skip_events(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Re-insert snapshotted ``shape_skip_events`` rows verbatim after a rebuild."""
    if not rows:
        return
    cols = ", ".join(_SHAPE_SKIP_SNAPSHOT_COLUMNS)
    placeholders = ", ".join("?" for _ in _SHAPE_SKIP_SNAPSHOT_COLUMNS)
    conn.executemany(
        f"INSERT INTO shape_skip_events ({cols}) VALUES ({placeholders})",
        rows,
    )


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


def latest_record_for(
    conn: sqlite3.Connection,
    *,
    slug: str | None = None,
    issue_id: int | None = None,
    run_id: str | None = None,
) -> dict | None:
    """Return the most-recent migrated record matching a story/run identifier.

    Read-only lookup for operator-facing query surfaces (e.g. ``forge explain``).
    Exactly one of ``slug`` / ``issue_id`` / ``run_id`` selects the record; a
    ``run_id`` addresses a single run, while ``slug``/``issue_id`` return the
    newest run for that story (ordered by ``started_at`` DESC). Returns ``None``
    when nothing matches. Never writes.
    """
    if run_id is not None:
        clause, param = "run_id = ?", run_id
    elif slug is not None:
        clause, param = "slug = ?", slug
    elif issue_id is not None:
        clause, param = "issue_id = ?", issue_id
    else:
        return None
    row = conn.execute(
        "SELECT raw_json, record_schema_version FROM audit_records "
        f"WHERE {clause} ORDER BY COALESCE(started_at, '') DESC LIMIT 1",
        (param,),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        raw, ver = row["raw_json"], row["record_schema_version"]
    else:
        raw, ver = row[0], row[1]
    return _load_migrated(raw, ver)


# ── Alias-resolution drift ───────────────────────────────────────────────


def iter_invocation_identities(
    conn: sqlite3.Connection,
    *,
    configured_model: str | None = None,
    role: str | None = None,
) -> Iterable[dict]:
    """Yield indexed invocation-identity rows, oldest first. Never writes."""
    clauses: list[str] = []
    params: list[object] = []
    if configured_model is not None:
        clauses.append("configured_model = ?")
        params.append(configured_model)
    if role is not None:
        clauses.append("role = ?")
        params.append(role)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    names = ", ".join(_INVOCATION_IDENTITY_COLUMNS)
    try:
        rows = conn.execute(
            f"SELECT {names} FROM invocation_identities{where} "
            "ORDER BY COALESCE(started_at, ''), run_id, seq",
            tuple(params),
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        yield {
            name: (row[name] if isinstance(row, sqlite3.Row) else row[idx])
            for idx, name in enumerate(_INVOCATION_IDENTITY_COLUMNS)
        }


def alias_resolution_timeline(conn: sqlite3.Connection) -> list[dict]:
    """Group recorded invocations by configured identity and report drift (#2226).

    Answers the question a family alias makes unanswerable from the identity
    alone: *what did this actually run, and did that change?* Each returned entry
    is one configured identity::

        {
          "configured_model": str,
          "invocations": int,
          "resolved_models": [            # ordered by first appearance
              {"resolved_model": str,
               "resolution": str | None,
               "invocations": int,
               "first_seen": str | None,
               "last_seen": str | None,
               "first_run_id": str,
               "last_run_id": str},
              ...
          ],
          "distinct_resolved": int,
          "changed": bool,                # resolved to more than one identity
          "current": str | None,          # the NEWEST recorded resolution
        }

    ``resolved_models`` is ordered by first appearance, which is the readable
    order for a drift history. ``current`` is deliberately not its last element:
    an alias that resolved A → B → A is currently on A, and reading the tail of a
    first-appearance list would report B.

    ``changed`` is the detection this exists for: two runs naming the same alias
    that resolved to different concrete versions produce ``changed: True``,
    which is a recorded fact rather than a behavioural surprise. Entries are
    ordered most-drifted first, then by invocation count, so the aliases whose
    subject moved surface at the top. Rows with no configured identity (a
    pre-ledger record, which could never name one) are skipped: they cannot
    attest to what an alias resolved to.
    """
    grouped: dict[str, dict] = {}
    for row in iter_invocation_identities(conn):
        configured = row["configured_model"]
        resolved = row["resolved_model"]
        if not configured or not resolved:
            continue
        bucket = grouped.setdefault(
            configured,
            {"configured_model": configured, "invocations": 0, "_resolved": {}, "_current": None},
        )
        bucket["invocations"] += 1
        # Rows arrive oldest-first, so the last one seen for this alias IS the
        # newest resolution. Tracked here rather than read off the tail of
        # ``resolved_models``: that list is ordered by FIRST appearance, so an
        # alias that went A → B → A would report B as current when the newest
        # invocation resolved to A.
        bucket["_current"] = resolved
        seen = bucket["_resolved"].get(resolved)
        started = row["started_at"]
        if seen is None:
            bucket["_resolved"][resolved] = {
                "resolved_model": resolved,
                "resolution": row["resolved_model_resolution"],
                "invocations": 1,
                "first_seen": started,
                "last_seen": started,
                "first_run_id": row["run_id"],
                "last_run_id": row["run_id"],
            }
        else:
            seen["invocations"] += 1
            seen["last_seen"] = started
            seen["last_run_id"] = row["run_id"]
    out: list[dict] = []
    for bucket in grouped.values():
        resolved_models = list(bucket.pop("_resolved").values())
        bucket["resolved_models"] = resolved_models
        bucket["distinct_resolved"] = len(resolved_models)
        bucket["changed"] = len(resolved_models) > 1
        bucket["current"] = bucket.pop("_current")
        out.append(bucket)
    out.sort(key=lambda b: (-b["distinct_resolved"], -b["invocations"], b["configured_model"]))
    return out


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


def derive_cost_samples_by_score(
    conn: sqlite3.Connection,
    *,
    stats: dict | None = None,
) -> dict[int, list[float]]:
    """Return ``{complexity_score: [total_cost_usd, ...]}`` over admissible runs.

    The per-story budget allocator (see ``coordinator.story_budget``) needs the
    observed cost distribution per complexity band. Reads the indexed flat
    columns directly — no record migration is needed, because both fields are
    projected into columns at upsert time — and routes admissibility through the
    same centralized taint gate every other history consumer uses (ADR-0006
    clause 4): a run that failed its own trust checks does not teach what a
    story of its kind costs. When ``stats`` is provided its
    ``"excluded_for_taint"`` key is incremented by the number of rows set aside.

    Rows with a null score, a null cost (cost-unknown runs, which are a lower
    bound rather than a measurement), or a non-positive cost are skipped: none
    of them carry a usable spend observation.
    """
    from .trust_status import filter_tainted_records  # noqa: PLC0415

    rows = conn.execute(
        "SELECT complexity_score, total_cost_usd, raw_json FROM audit_records "
        "WHERE complexity_score IS NOT NULL AND total_cost_usd IS NOT NULL"
    ).fetchall()
    candidates: list[dict] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            score, cost, raw = row["complexity_score"], row["total_cost_usd"], row["raw_json"]
        else:
            score, cost, raw = row[0], row[1], row[2]
        try:
            cost_value = float(cost)
        except (TypeError, ValueError):
            continue
        if cost_value <= 0.0:
            continue
        score_value = _coerce_complexity_score(score)
        if score_value is None:
            continue
        trust: str | None = None
        try:
            parsed = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            status = parsed.get("trust_status")
            trust = status if isinstance(status, str) else None
        candidates.append(
            {"complexity_score": score_value, "total_cost_usd": cost_value, "trust_status": trust}
        )

    admissible, excluded = filter_tainted_records(candidates)
    if stats is not None:
        stats["excluded_for_taint"] = int(stats.get("excluded_for_taint", 0)) + excluded
    out: dict[int, list[float]] = {}
    for entry in admissible:
        out.setdefault(int(entry["complexity_score"]), []).append(float(entry["total_cost_usd"]))
    return out


def derive_observed_cost_cohorts(
    conn: sqlite3.Connection,
    *,
    stats: dict | None = None,
) -> dict[str, dict[str, dict[str, object]]]:
    """Return observed invocation-cost cohorts keyed by model and cohort tuple.

    The final assignment tie-break compares spend only within like-for-like
    cohorts: role, complexity band, and requested reasoning effort. This reader
    projects those cohorts from the invocation ledger (#2226), not from the
    profile aggregates, because the ledger is the only substrate that carries
    all three cohort dimensions together with the measured invocation cost.

    Returned shape::

        {
          "<model identity>": {
            "dev|MEDIUM|high": {
              "role": "dev",
              "complexity": "MEDIUM",
              "reasoning_effort": "high",
              "observations": [
                {"cost_usd": 2.1, "started_at": "...", "cost_provenance": "..."},
                ...
              ],
            },
          },
        }

    Only complete observations are kept: a full ledger, a configured-or-resolved
    model identity, usage metadata, a numeric ``cost_usd``, and the cohort
    dimensions required by the selector. Tainted runs are filtered by the same
    centralized gate every other router-consumed history projection uses.
    """
    from .trust_status import filter_tainted_records  # noqa: PLC0415

    rows = conn.execute(
        "SELECT raw_json, record_schema_version, started_at FROM audit_records "
        "ORDER BY COALESCE(started_at, '') ASC"
    ).fetchall()
    candidates: list[dict[str, object]] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            raw = row["raw_json"]
            ver = row["record_schema_version"]
            started_at = row["started_at"]
        else:
            raw, ver, started_at = row[0], row[1], row[2]
        record = _load_migrated(raw, ver)
        if not isinstance(record, dict):
            continue
        trust = record.get("trust_status")
        timing = record.get("timing") if isinstance(record.get("timing"), dict) else {}
        candidates.append(
            {
                "record": record,
                "trust_status": trust if isinstance(trust, str) else None,
                "started_at": (
                    started_at
                    or timing.get("started_at")
                    or timing.get("finished_at")
                    or record.get("started_at")
                    or record.get("finished_at")
                ),
            }
        )

    admissible, excluded = filter_tainted_records(candidates)
    if stats is not None:
        stats["excluded_for_taint"] = int(stats.get("excluded_for_taint", 0)) + excluded

    out: dict[str, dict[str, dict[str, object]]] = {}
    for item in admissible:
        record = item.get("record")
        if not isinstance(record, dict):
            continue
        started_at = item.get("started_at")
        cost = record.get("cost") if isinstance(record.get("cost"), dict) else {}
        agents = cost.get("agents") if isinstance(cost, dict) else None
        if not isinstance(agents, list):
            continue
        for entry in agents:
            if not isinstance(entry, dict):
                continue
            projection = entry_identity_ledger(entry)
            if not isinstance(projection, dict) or not projection.get("full_ledger"):
                continue
            ledger = entry.get("ledger")
            if not isinstance(ledger, dict):
                continue
            usage = ledger.get("usage")
            cost_usd = projection.get("cost_usd")
            role = projection.get("role")
            complexity = projection.get("complexity")
            effort = projection.get("reasoning_effort")
            provenance = projection.get("cost_provenance")
            if not isinstance(usage, dict) or not usage:
                continue
            if not isinstance(cost_usd, (int, float)):
                continue
            if not isinstance(role, str) or not role.strip():
                continue
            if not isinstance(complexity, str) or not complexity.strip():
                continue
            if not isinstance(effort, str) or not effort.strip():
                continue
            if not isinstance(provenance, str) or not provenance.strip():
                continue
            configured = projection.get("configured")
            resolved = projection.get("resolved")
            identity = None
            if isinstance(configured, tuple) and configured and configured[0]:
                identity = str(configured[0])
            elif isinstance(resolved, tuple) and resolved and resolved[0]:
                identity = str(resolved[0])
            if not identity:
                continue
            cohort_key = f"{role.upper()}|{complexity.upper()}|{effort.lower()}"
            cohort = out.setdefault(identity, {}).setdefault(
                cohort_key,
                {
                    "role": str(role).upper(),
                    "complexity": str(complexity).upper(),
                    "reasoning_effort": str(effort).lower(),
                    "observations": [],
                },
            )
            observations = cohort.get("observations")
            if not isinstance(observations, list):
                observations = []
                cohort["observations"] = observations
            observations.append(
                {
                    "cost_usd": round(float(cost_usd), 6),
                    "started_at": str(started_at or ""),
                    "cost_provenance": str(provenance),
                }
            )
    return out


def load_observed_cost_cohorts(
    project_root: Path,
) -> tuple[dict[str, dict[str, dict[str, object]]], int]:
    """Return ``(observed_cost_cohorts, excluded_for_taint)`` from substrate.

    Mirrors the escalation-history loader's runtime contract: a genuinely fresh
    repo returns an empty mapping; an existing but unreadable substrate remains
    an operator-facing error rather than silently degrading to no history.
    """
    sub_path = substrate_path(project_root)
    if not sub_path.exists() and not has_audit_inputs(project_root):
        return {}, 0
    conn = require_substrate(project_root)
    try:
        stats: dict[str, object] = {}
        cohorts = derive_observed_cost_cohorts(conn, stats=stats)
        return cohorts, int(stats.get("excluded_for_taint", 0))
    finally:
        conn.close()


def derive_assignment_history(
    conn: sqlite3.Connection,
    *,
    stats: dict | None = None,
) -> list[dict]:
    """Return assignment-history records derived from per-run audit records.

    Replaces the YAML snapshot at ``.forge/assignment_history.yaml`` as the
    source of truth: each emitted dict has the same shape consumed by
    :func:`theforge.assignment.load_escalation_history` (story, complexity,
    dev_model, outcome, reason, timestamp, complexity_score). Records are
    ordered chronologically by ``timing.started_at``.

    Audit records that lack the routing/complexity fields needed to
    reconstruct an :class:`EscalationRecord` are skipped — they predate
    adaptive routing and were never represented in the legacy YAML either.

    Runs marked ``tainted`` by the trust-status marker (ADR-0006 clause 4) are
    excluded before any projection: a run that failed its own trust checks
    "doesn't teach", so it must not carry routing weight. Filtering routes
    through the centralized :func:`trust_status.filter_tainted_records` gate so
    the rule stays identical across every consumer. The tainted rows remain in
    the substrate untouched (ADR-0002 refusal-to-forget); this is a read-time
    gate. When ``stats`` is provided, its ``"excluded_for_taint"`` key is
    incremented by the number of records set aside so callers (preflight) can
    surface the count in ``routing_decision``.

    This is the CLI/export view (mapped complexity bands, derives dev model
    from preflight routing assignments). Adaptive routing uses the more
    runtime-faithful :func:`iter_escalation_records` instead, which derives
    dev model from ``cost.agents`` (the model that actually ran).
    """
    from .trust_status import filter_tainted_records  # noqa: PLC0415

    admissible, excluded = filter_tainted_records(iter_records(conn, order_by_started=True))
    if stats is not None:
        stats["excluded_for_taint"] = int(stats.get("excluded_for_taint", 0)) + excluded
    out: list[dict] = []
    for record in admissible:
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
        dev_model_resolution: str | None = None
        # Fallback: when the audit record lacks the preflight.complexity_routing
        # block (older audits, or audits seeded by tests that bypass routing),
        # derive the canonical dev model from cost.agents — the model that
        # actually ran, canonicalized to the same provider/model/transport shape
        # the routing block would have provided. Shares the one reading of the
        # agent-entry identity contract (#2201).
        if not (isinstance(dev_model, str) and dev_model):
            dev_model, _source, dev_model_resolution = dev_model_identity_detail(record)
        if not isinstance(dev_model, str) or not dev_model:
            continue
        ledger = dev_identity_ledger(record)
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
                # Only set on the cost.agents fallback: a routing-block model is
                # canonical by construction and carries no resolution status.
                "dev_model_resolution": dev_model_resolution,
                # Configured and resolved carried separately beside the
                # compatibility ``dev_model`` above, so a consumer can group cost
                # and outcome by either one independently (#2205). Null on a
                # pre-ledger record — which ``dev_identity_ledger_full`` says
                # explicitly, so an absent value never reads as "same as
                # resolved".
                "dev_configured_model": ledger["configured"][0] if ledger["configured"] else None,
                "dev_resolved_model": (
                    ledger["resolved"][0] if ledger["resolved"] else str(dev_model)
                ),
                "dev_identity_ledger_full": ledger["full_ledger"],
                "dev_configured_differs_from_resolved": ledger["differs"],
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

    Runs marked ``tainted`` by the trust-status marker (ADR-0006 clause 4) are
    dropped before ``_derive_escalation`` so a run that failed its own trust
    checks never carries routing weight. The exclusion routes through the same
    centralized :func:`trust_status.is_tainted` gate every other consumer uses.
    """
    from .trust_status import is_tainted  # noqa: PLC0415

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
        if is_tainted(record.get("trust_status")):
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
    # present, through the one shared reading of that contract (#2201).
    dev_model, _dev_model_source, dev_model_resolution = dev_model_identity_detail(record)
    dev_model = dev_model or ""
    ledger = dev_identity_ledger(record)

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
        # Whether ``dev_model`` is a canonical identity or the runner's verbatim
        # spelling — a consumer aggregating per model needs to be able to tell
        # an unrecognized identity from a normalized one (#2225).
        "dev_model_resolution": dev_model_resolution,
        # Adaptive routing keys off ``dev_model`` (the resolved identity) as it
        # always has — that is the model whose behaviour the outcome is evidence
        # about. The configured identity is carried alongside so cost/outcome can
        # also be grouped by what the operator selected, without either grouping
        # silently standing in for the other (#2205).
        "dev_configured_model": ledger["configured"][0] if ledger["configured"] else None,
        "dev_resolved_model": ledger["resolved"][0] if ledger["resolved"] else dev_model,
        "dev_identity_ledger_full": ledger["full_ledger"],
        "dev_configured_differs_from_resolved": ledger["differs"],
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


# Shape-skip prior-status values (issue #1453 AC1: "last unblocked-or-still-blocked
# status"). ``NO_PRIOR_BLOCK`` — this is the issue's first block on this code.
# ``UNBLOCKED`` — the issue cleared the gate (a RUNNABLE shape verdict) since the
# previous block, then tripped this code again. ``STILL_BLOCKED`` — the issue has
# been continuously blocked since its previous block with no intervening pass.
SKIP_STATUS_NO_PRIOR = "no_prior_block"
SKIP_STATUS_UNBLOCKED = "unblocked"
SKIP_STATUS_STILL_BLOCKED = "still_blocked"


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


def _skip_last_status(
    conn: sqlite3.Connection,
    issue_id: str,
    prior_count: int,
    last_blocked_at: str | None,
    current_emitted_at: str,
) -> str:
    """Return the issue's last unblocked-or-still-blocked status for this skip code.

    ``NO_PRIOR_BLOCK`` when this is the first block. Otherwise the issue is
    ``UNBLOCKED`` when *any* ``RUNNABLE`` shape-verdict event was emitted in the
    open interval ``(last_blocked_at, current_emitted_at)`` — the gate cleared it
    between the previous block and this one, and it has now tripped again (the
    #1135/#1405 pass-then-reblock shape); else ``STILL_BLOCKED`` (continuously
    blocked since the previous block).

    Checking for *any* runnable in the window — not the latest verdict — is what
    makes the clear-and-reblock case correct: at sprint runtime the current
    non-runnable verdict is emitted before this skip record, and a later
    non-runnable verdict must not mask an earlier runnable one. Bounding the
    upper end at ``current_emitted_at`` excludes the current block's own verdict
    from the window. A RUNNABLE verdict clears every code, so it is a sound
    per-code unblock signal.
    """
    if prior_count <= 0:
        return SKIP_STATUS_NO_PRIOR
    row = conn.execute(
        "SELECT 1 FROM shape_verdict_events "
        "WHERE issue_id = ? AND emitted_at > ? AND emitted_at < ? "
        "AND LOWER(verdict) = 'runnable' LIMIT 1",
        (issue_id, last_blocked_at or "", current_emitted_at),
    ).fetchone()
    if row is not None:
        return SKIP_STATUS_UNBLOCKED
    return SKIP_STATUS_STILL_BLOCKED


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
        last_status = _skip_last_status(conn, issue_id, prior_count, last_blocked, emitted_at)
        # Fold the computed history back into the persisted raw_json so the
        # canonical record and the indexed columns agree.
        enriched = dict(event)
        enriched["prior_block_count"] = prior_count
        enriched["first_blocked_at"] = first_blocked
        enriched["last_blocked_at"] = last_blocked
        enriched["last_status"] = last_status
        enriched["emitted_at"] = emitted_at
        raw_json = _canonical_json(enriched)
        cur = conn.execute(
            "INSERT INTO shape_skip_events "
            "(issue_id, reason_code, source, severity, category, four_question_axis, "
            "run_id, sprint_id, sprint_name, milestone, prior_block_count, "
            "first_blocked_at, last_blocked_at, last_status, emitted_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                last_status,
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


def record_inline_remediation_event(project_root: Path, event: dict) -> int:
    """Insert an inline-remediation event row into the audit substrate.

    Inline intake remediation is the opt-in ``intake.grooming`` fallback that
    fires at sprint entry when pre-sprint ``forge groom`` was skipped
    (ADR-0001, "Inline intake remediation posture"). Each firing writes one
    structured record here so the refusal-economics metric — the
    remediation-to-runnable cost ratio — can be queried from a single table
    rather than reconstructed from scattered WARNING logs.

    The ``milestone`` and ``cost_usd`` columns are indexed/summable so
    ``count(inline_remediation_events) per milestone`` and
    ``total_cost(inline_remediation_events) per milestone`` are direct SQL
    (see :func:`inline_remediation_rollup_by_milestone`).

    Required keys: ``issue_id``, ``action``. ``succeeded`` is coerced to a
    0/1 integer. Returns the inserted row's ``event_id``. Raises
    :class:`SubstrateError` on missing required keys or I/O failure so the
    caller can decide whether to swallow (sprint treats this as
    observability, not gating).
    """
    required = {"issue_id", "action"}
    missing = required - set(event)
    if missing:
        raise SubstrateError(f"inline remediation event missing required keys: {sorted(missing)}")
    raw_json = _canonical_json(event)
    emitted_at = event.get("emitted_at") or _now_iso()
    raw_cost = event.get("cost_usd")
    try:
        cost_usd = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        cost_usd = None
    raw_duration = event.get("duration_seconds")
    try:
        duration_seconds = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration_seconds = None
    conn = create_or_open(project_root)
    try:
        cur = conn.execute(
            "INSERT INTO inline_remediation_events "
            "(issue_id, sprint_id, milestone, shape_verdict, action, succeeded, "
            "cost_usd, duration_seconds, emitted_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(event["issue_id"]),
                event.get("sprint_id"),
                event.get("milestone"),
                event.get("shape_verdict"),
                str(event["action"]),
                1 if event.get("succeeded") else 0,
                cost_usd,
                duration_seconds,
                emitted_at,
                raw_json,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def iter_inline_remediation_events(
    conn: sqlite3.Connection,
    *,
    milestone: str | None = None,
) -> Iterable[dict]:
    """Yield inline-remediation event dicts (parsed from raw_json), oldest first."""
    sql = "SELECT raw_json FROM inline_remediation_events"
    params: tuple = ()
    if milestone is not None:
        sql += " WHERE milestone = ?"
        params = (milestone,)
    sql += " ORDER BY emitted_at ASC, event_id ASC"
    for row in conn.execute(sql, params):
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def inline_remediation_rollup_by_milestone(conn: sqlite3.Connection) -> dict[str, dict]:
    """Return ``{milestone: {"count": int, "total_cost_usd": float}}``.

    The postmortem refusal-economics numerator: how many inline-remediation
    events fired per milestone and what they cost. A ``None`` milestone is
    keyed under the empty string so callers can still surface un-milestoned
    events. Costs recorded as NULL contribute 0.0.
    """
    rows = conn.execute(
        "SELECT COALESCE(milestone, ''), COUNT(*), COALESCE(SUM(cost_usd), 0.0) "
        "FROM inline_remediation_events GROUP BY milestone"
    ).fetchall()
    out: dict[str, dict] = {}
    for milestone, count, total_cost in rows:
        out[str(milestone)] = {
            "count": int(count),
            "total_cost_usd": float(total_cost),
        }
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
