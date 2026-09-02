"""Storage ownership for the SQLite audit substrate.

This module owns everything on the *write* side of the audit substrate at
``.forge/audits/index.sqlite``:

- **Connection management** — :func:`create_or_open`, :func:`require_substrate`,
  :func:`open_readonly`, and the validation/staleness handling they apply.
- **Schema definition** — ``_SCHEMA`` and :func:`_apply_schema`, plus the
  ``SUBSTRATE_SCHEMA_VERSION`` counter and the re-index passes that carry a
  schema bump onto already-indexed history.
- **Migrations** — the sequential ``_migrate_vN_to_vN+1`` catalogue, its
  ``MIGRATION_HELPERS`` registry, and the record decoders
  :func:`_migrate_record` / :func:`_load_migrated`. The catalogue is one
  uninterrupted unit and is meant to be read in order; it is deliberately not
  subdivided.
- **Record writes** — :func:`upsert_run_record`, :func:`rebuild_from_runs`,
  :func:`import_history_jsonl`, the ``record_*`` event writers, and
  :func:`seed_records`.

The named interface it publishes to readers is :data:`AuditConnection`: a
sqlite3 connection that storage has opened, validated, and brought up to the
current schema. Analytical queries and derivations live in
:mod:`theforge.coordinator.audit_read_model`, which takes an ``AuditConnection``
and issues SELECT SQL against it directly. Storage does not import the read
model; the dependency runs one way.

``audit_substrate`` re-exports both halves for compatibility.

Everything here is stdlib (sqlite3, json, hashlib, pathlib).
"""

from __future__ import annotations

import copy
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
    invocation_identity_rows,
)
from .landing_evidence import (
    LANDING_EVIDENCE_RELPATH,
    is_landing_assertion,
    is_landing_attempt,
    landing_evidence_read_dirs,
)

# The named interface between storage and the analytical read model.
#
# An ``AuditConnection`` is a sqlite3 connection that *this module* opened —
# meaning the substrate file was validated, the schema was applied, and any
# pending re-index passes have run. Readers in
# :mod:`theforge.coordinator.audit_read_model` accept one of these and may
# issue SELECT SQL against it directly; what they may not do is open a
# connection, define or migrate schema, or write records. Those remain storage's
# alone, which is what lets a new analytical query land without touching
# migration code.
#
# It is a plain alias rather than a wrapper class on purpose: wrapping would
# force every one of the existing consumers to be re-pointed at a new object,
# and the ownership rule this names is enforced by which module a function
# lives in, not by what the connection object refuses to do.
AuditConnection = sqlite3.Connection

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
#
# Bumped to 9 by #2347: a run recorded what it cost and never what it changed,
# so spend could not be attributed to code. ``audit_changed_files`` indexes the
# record's new ``changed_files`` block one row per (run_id, path), which is what
# makes "what did this module cost" a join instead of a scan that deserializes
# every ``raw_json``. A version-8 substrate is re-derived on open so the table
# covers already-indexed history wherever the record carries the block.
#
# Bumped to 10 by #2228: ``forge triage`` proposes a disposition per backlog
# finding, and a proposal that leaves no row is not auditable — the operator
# cannot see what was proposed, what it cost, or what the same finding was
# proposed last time. ``triage_proposal_runs`` and ``triage_proposal_events``
# index one row per run and per finding-proposal, which is also what lets a
# later packet carry this finding's own disposition history.
#
# Unlike versions 5-9 this bump implies NO re-index pass: both tables are
# populated by the triage command as it runs, not derived from fields inside
# existing ``audit_records.raw_json``, so there is nothing in already-indexed
# history to re-derive. Opening an older substrate creates the empty tables and
# stops there. The per-record shape is untouched, so no
# ``CURRENT_RECORD_SCHEMA_VERSION`` bump and no ``MIGRATION_HELPERS`` entry is
# implied either.
#
# Bumped to 11 by #2229: punt proposals now carry an adversarial second review.
# ``triage_proposal_events`` indexes the review verdict, cited refs, retry /
# fallback state, and reviewer spend so the challenged-safe-default path is
# queryable. ``triage_proposal_runs`` keeps the run-level review-stage summary in
# ``raw_json``; there is no separate per-record migration because older rows had
# no review stage to backfill.
#
# Bumped to 12 by #2230: proposal runs now have an operator-ratified application
# stage. ``triage_application_records`` indexes one upserted row per
# ``(triage_run_id, finding_id)`` with the operator decision, the actually
# applied disposition payload, idempotency marker, stale/failure state, and the
# external effect summary so resume can continue from durable application state
# rather than re-deriving it from raw proposal rows. Proposal-event writes also
# now carry their richer snapshot only in ``raw_json``, so no re-index pass is
# required for older rows.
#
# Bumped to 13 by #2849: landing evidence was written one artifact per run under
# ``.forge/audits/landing`` and never indexed, so every landed question the
# substrate could answer was answered from ``audit_records.landing_status`` — a
# completion-time snapshot taken before a queued pull request resolves.
# ``landing_assertions`` and ``landing_attempts`` project those artifacts one row
# per artifact, carrying each one's own ``observed_at``, so a landed query is
# answered by the evidence that records the event and "no evidence yet" stays
# distinguishable from "recorded as not landed".
#
# The projection is derived from files beside the substrate rather than from
# fields inside ``audit_records.raw_json``, so it gets no re-index pass in the
# ``_migrate_*`` sense. What the bump does instead is clear the projection
# fingerprint (see :func:`_apply_schema`), which forces the next
# :func:`sync_landing_evidence` on an older substrate to re-project from scratch
# rather than trusting a fingerprint written by a schema that had nowhere to put
# the rows.
SUBSTRATE_SCHEMA_VERSION = 13
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
CURRENT_RECORD_SCHEMA_VERSION = 45
SUBSTRATE_RELPATH = (".forge", "audits", "index.sqlite")
HISTORY_RELPATH = (".forge", "audits", "history.jsonl")
RUNS_RELPATH = (".forge", "audits", "runs")
AUDITS_RELPATH = (".forge", "audits")
SECRETS_RELPATH = (".forge", ".env")
UNPUBLISHED_STORY_RUN_ARTIFACTS_RELPATH = (".forge", "unpublished-story-run-artifacts")


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
        "Landing evidence (JSON; '<run>.landed.json' asserts an observed landing, "
        "'<run>.attempt-NNN.json' records an attempt that did not land)",
        LANDING_EVIDENCE_RELPATH,
        "/*.json",
    ),
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


def _preserved_story_run_roots(project_root: Path) -> list[Path]:
    root = project_root.joinpath(*UNPUBLISHED_STORY_RUN_ARTIFACTS_RELPATH)
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _run_record_paths(project_root: Path) -> list[Path]:
    paths: list[Path] = []
    canonical_runs = runs_dir(project_root)
    if canonical_runs.exists():
        paths.extend(sorted(canonical_runs.glob("*.json")))
    for preserved_root in _preserved_story_run_roots(project_root):
        preserved_runs = preserved_root.joinpath(*RUNS_RELPATH)
        if preserved_runs.exists():
            paths.extend(sorted(preserved_runs.glob("*.json")))
    return paths


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
CREATE TABLE IF NOT EXISTS audit_changed_files (
    run_id TEXT NOT NULL,
    path TEXT NOT NULL,
    base_ref TEXT,
    head_ref TEXT,
    insertions INTEGER,
    deletions INTEGER,
    binary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, path)
);
CREATE INDEX IF NOT EXISTS idx_audit_changed_files_path ON audit_changed_files(path);
CREATE TABLE IF NOT EXISTS triage_proposal_runs (
    run_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    triage_run_id TEXT NOT NULL,
    findings_count INTEGER NOT NULL,
    total_cost_usd REAL,
    cost_provenance TEXT,
    report_path TEXT,
    emitted_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triage_proposal_runs_run
    ON triage_proposal_runs(triage_run_id);
CREATE TABLE IF NOT EXISTS triage_proposal_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    triage_run_id TEXT,
    finding_id TEXT NOT NULL,
    issue_ref TEXT,
    packet_hash TEXT,
    disposition TEXT NOT NULL,
    target_milestone TEXT,
    punt_reason_code TEXT,
    evidence_refs TEXT,
    validation_errors TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    review_verdict TEXT,
    review_evidence_refs TEXT,
    review_validation_errors TEXT,
    review_retry_count INTEGER NOT NULL DEFAULT 0,
    review_fallback_reason TEXT,
    cost_usd REAL,
    cost_provenance TEXT,
    review_cost_usd REAL,
    review_cost_provenance TEXT,
    emitted_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_triage_proposal_events_finding
    ON triage_proposal_events(finding_id);
CREATE INDEX IF NOT EXISTS idx_triage_proposal_events_disposition
    ON triage_proposal_events(disposition);
CREATE INDEX IF NOT EXISTS idx_triage_proposal_events_run
    ON triage_proposal_events(triage_run_id);
CREATE TABLE IF NOT EXISTS triage_application_records (
    triage_run_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    issue_ref TEXT,
    proposed_payload TEXT,
    operator_decision TEXT NOT NULL,
    applied_disposition TEXT,
    target_milestone TEXT,
    punt_reason_code TEXT,
    evidence_refs TEXT,
    operator_note TEXT,
    status TEXT NOT NULL,
    stale_reason TEXT,
    idempotency_marker TEXT,
    external_effect_summary TEXT,
    emitted_at TEXT NOT NULL,
    applied_at TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (triage_run_id, finding_id)
);
CREATE INDEX IF NOT EXISTS idx_triage_application_records_run
    ON triage_application_records(triage_run_id);
CREATE INDEX IF NOT EXISTS idx_triage_application_records_status
    ON triage_application_records(status);
CREATE TABLE IF NOT EXISTS landing_assertions (
    run_id TEXT NOT NULL,
    artifact_name TEXT NOT NULL,
    slug TEXT,
    landing_mode TEXT,
    target_branch TEXT,
    reviewed_commit TEXT,
    gated_commit TEXT,
    carrier_kind TEXT,
    carrier_ref TEXT,
    landed_commit TEXT,
    pr_url TEXT,
    observer TEXT,
    observed_at TEXT,
    source_path TEXT,
    source_mtime REAL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id)
);
CREATE INDEX IF NOT EXISTS idx_landing_assertions_slug ON landing_assertions(slug);
CREATE INDEX IF NOT EXISTS idx_landing_assertions_observed
    ON landing_assertions(observed_at);
CREATE INDEX IF NOT EXISTS idx_landing_assertions_target
    ON landing_assertions(target_branch);
CREATE INDEX IF NOT EXISTS idx_landing_assertions_mode
    ON landing_assertions(landing_mode);
CREATE INDEX IF NOT EXISTS idx_landing_assertions_observer
    ON landing_assertions(observer);
CREATE TABLE IF NOT EXISTS landing_attempts (
    run_id TEXT NOT NULL,
    artifact_name TEXT NOT NULL,
    slug TEXT,
    landing_mode TEXT,
    target_branch TEXT,
    outcome TEXT,
    carrier_kind TEXT,
    carrier_ref TEXT,
    pr_url TEXT,
    detail TEXT,
    observer TEXT,
    observed_at TEXT,
    source_path TEXT,
    source_mtime REAL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, artifact_name)
);
CREATE INDEX IF NOT EXISTS idx_landing_attempts_run ON landing_attempts(run_id);
CREATE INDEX IF NOT EXISTS idx_landing_attempts_outcome ON landing_attempts(outcome);
CREATE INDEX IF NOT EXISTS idx_landing_attempts_observed ON landing_attempts(observed_at);
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
        "ALTER TABLE triage_proposal_events ADD COLUMN review_verdict TEXT",
        "ALTER TABLE triage_proposal_events ADD COLUMN review_evidence_refs TEXT",
        "ALTER TABLE triage_proposal_events ADD COLUMN review_validation_errors TEXT",
        "ALTER TABLE triage_proposal_events ADD COLUMN review_retry_count "
        "INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE triage_proposal_events ADD COLUMN review_fallback_reason TEXT",
        "ALTER TABLE triage_proposal_events ADD COLUMN review_cost_usd REAL",
        "ALTER TABLE triage_proposal_events ADD COLUMN review_cost_provenance TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN issue_ref TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN proposed_payload TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN operator_decision TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN applied_disposition TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN target_milestone TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN punt_reason_code TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN evidence_refs TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN operator_note TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN status TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN stale_reason TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN idempotency_marker TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN external_effect_summary TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN emitted_at TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN applied_at TEXT",
        "ALTER TABLE triage_application_records ADD COLUMN raw_json TEXT",
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
        _reindex_changed_files(conn)
        # The landing projection (#2849) is derived from artifacts on disk, not
        # from raw_json, so it cannot be re-derived here — this function has no
        # project root. Dropping the fingerprint is the equivalent move: the
        # next :func:`sync_landing_evidence` sees no match and re-projects the
        # whole evidence tree, so an existing substrate upgrades on first open
        # rather than waiting for an operator to run `forge audits rebuild`.
        conn.execute("DELETE FROM meta WHERE key = ?", (LANDING_PROJECTION_FINGERPRINT_KEY,))
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


_CHANGED_FILE_COLUMNS: tuple[str, ...] = (
    "run_id",
    "path",
    "base_ref",
    "head_ref",
    "insertions",
    "deletions",
    "binary",
)


def _changed_file_params(run_id: str, record: dict) -> list[tuple]:
    """Project a record's ``changed_files`` block to its per-file rows (#2347).

    ``changed_files`` is ``None`` on a run whose file set could not be captured
    and on every record written before the block existed. Both contribute no
    rows — an absent comparison must not be indexed as one that found nothing.
    A captured comparison with an empty ``files`` list likewise writes no rows;
    the empty-vs-absent distinction is preserved in ``raw_json``, which is the
    canonical record, rather than by fabricating a sentinel row here.
    """
    block = record.get("changed_files")
    if not isinstance(block, dict):
        return []
    files = block.get("files")
    if not isinstance(files, list):
        return []
    base_ref = block.get("base_ref") if isinstance(block.get("base_ref"), str) else None
    head_ref = block.get("head_ref") if isinstance(block.get("head_ref"), str) else None
    params: list[tuple] = []
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path or path in seen:
            continue
        seen.add(path)
        insertions = entry.get("insertions")
        deletions = entry.get("deletions")
        params.append(
            (
                run_id,
                path,
                base_ref,
                head_ref,
                insertions if isinstance(insertions, int) else None,
                deletions if isinstance(deletions, int) else None,
                1 if entry.get("binary") else 0,
            )
        )
    return params


def _write_changed_files(conn: sqlite3.Connection, run_id: str, record: dict) -> int:
    """Rewrite the changed-file rows for one run. Returns rows written."""
    conn.execute("DELETE FROM audit_changed_files WHERE run_id = ?", (run_id,))
    params = _changed_file_params(run_id, record)
    if not params:
        return 0
    names = ", ".join(_CHANGED_FILE_COLUMNS)
    placeholders = ", ".join(["?"] * len(_CHANGED_FILE_COLUMNS))
    conn.executemany(
        f"INSERT OR REPLACE INTO audit_changed_files({names}) VALUES ({placeholders})",
        params,
    )
    return len(params)


def _reindex_changed_files(conn: sqlite3.Connection) -> int:
    """Derive ``audit_changed_files`` for every already-indexed run (#2347).

    Same discipline as :func:`_reindex_invocation_identities`: ``raw_json`` is
    the record, so the backfill reaches any row whose stored JSON already
    carries a ``changed_files`` block — including records indexed before the
    table existed. Returns the number of runs that produced rows.
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
        if _write_changed_files(conn, str(run_id), record):
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


# ── Landing-evidence projection ──────────────────────────────────────────
#
# Landing evidence (#2598) is published one JSON artifact per observation under
# ``.forge/audits/landing`` (and, between a memory publish and its pull request
# merging, under ``.forge/memory-staging``). Each artifact carries its own
# ``observed_at``, recorded by whichever observer saw the landing — which is
# routinely long after the run record was written.
#
# This section indexes those artifacts, one row per artifact. It reads only what
# the artifact records; it never consults ``audit_records.landing_status``, which
# is a completion-time snapshot of a different question (#2849). The projection
# is a *derived* view of files on disk, so it is rewritten wholesale whenever the
# evidence tree changes rather than mutated in place — there is no incremental
# state to get wrong, and a rebuilt-from-scratch substrate and an incrementally
# maintained one converge by construction because both call the same function.

# Meta keys recording the state of the projection. They are what a *read-only*
# opening (:func:`open_readonly`, which may not write and therefore may not
# sync) can report so an operator can tell "no landing evidence" from "this
# index has not been refreshed since the evidence was written".
LANDING_PROJECTION_FINGERPRINT_KEY = "landing_projection_fingerprint"
LANDING_PROJECTION_SYNCED_AT_KEY = "landing_projection_synced_at"
LANDING_PROJECTION_SOURCE_COUNT_KEY = "landing_projection_source_count"

_LANDING_ASSERTION_COLUMNS = (
    "run_id",
    "artifact_name",
    "slug",
    "landing_mode",
    "target_branch",
    "reviewed_commit",
    "gated_commit",
    "carrier_kind",
    "carrier_ref",
    "landed_commit",
    "pr_url",
    "observer",
    "observed_at",
    "source_path",
    "source_mtime",
    "raw_json",
)

_LANDING_ATTEMPT_COLUMNS = (
    "run_id",
    "artifact_name",
    "slug",
    "landing_mode",
    "target_branch",
    "outcome",
    "carrier_kind",
    "carrier_ref",
    "pr_url",
    "detail",
    "observer",
    "observed_at",
    "source_path",
    "source_mtime",
    "raw_json",
)


def _landing_evidence_sources(project_root: Path) -> list[tuple[Path, str, float]]:
    """Every landing-evidence artifact on disk as ``(path, relpath, mtime)``.

    Ordered by relative path, which puts the canonical
    ``.forge/audits/landing`` tree ahead of ``.forge/memory-staging`` — the
    ordering the ``INSERT OR IGNORE`` below relies on, so a run whose evidence
    exists in both places is projected from the canonical copy.
    """
    sources: list[tuple[Path, str, float]] = []
    for directory in landing_evidence_read_dirs(project_root):
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            try:
                relpath = str(path.relative_to(project_root))
            except ValueError:
                relpath = str(path)
            sources.append((path, relpath, mtime))
    return sorted(sources, key=lambda item: item[1])


def _landing_projection_fingerprint(sources: list[tuple[Path, str, float]]) -> str:
    """A digest over the evidence tree's (path, mtime) set.

    Cheap enough to compute on every substrate open, which is what lets the
    projection stay current without the destructive whole-database rebuild that
    :func:`_native_rows_are_stale` triggers for run records. That rebuild drops
    tables nothing on disk can reconstruct (readiness, shape-verdict and
    inline-remediation events, triage rows); routing routine landing observation
    through it would erase them on every landed story.
    """
    digest = hashlib.sha1()
    for _path, relpath, mtime in sources:
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(repr(float(mtime)).encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _landing_evidence_payload(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _text_or_none(payload: dict, field_name: str) -> str | None:
    """The artifact's recorded value for ``field_name``, verbatim.

    Verbatim is the point (#2849 AC4): a landing mode, observer or target branch
    this corpus has never seen before is stored exactly as recorded. There is no
    allow-list here and none in the schema — projecting only known values would
    make the projection a description of the past rather than of the artifact.
    """
    value = payload.get(field_name)
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _landing_assertion_row(payload: dict, artifact_name: str, relpath: str, mtime: float) -> tuple:
    return (
        str(payload["run_id"]),
        artifact_name,
        _text_or_none(payload, "slug"),
        _text_or_none(payload, "landing_mode"),
        _text_or_none(payload, "target_branch"),
        _text_or_none(payload, "reviewed_commit"),
        _text_or_none(payload, "gated_commit"),
        _text_or_none(payload, "carrier_kind"),
        _text_or_none(payload, "carrier_ref"),
        _text_or_none(payload, "landed_commit"),
        _text_or_none(payload, "pr_url"),
        _text_or_none(payload, "observer"),
        _text_or_none(payload, "observed_at"),
        relpath,
        float(mtime),
        json.dumps(payload, sort_keys=True),
    )


def _landing_attempt_row(payload: dict, artifact_name: str, relpath: str, mtime: float) -> tuple:
    return (
        str(payload["run_id"]),
        artifact_name,
        _text_or_none(payload, "slug"),
        _text_or_none(payload, "landing_mode"),
        _text_or_none(payload, "target_branch"),
        _text_or_none(payload, "outcome"),
        _text_or_none(payload, "carrier_kind"),
        _text_or_none(payload, "carrier_ref"),
        _text_or_none(payload, "pr_url"),
        _text_or_none(payload, "detail"),
        _text_or_none(payload, "observer"),
        _text_or_none(payload, "observed_at"),
        relpath,
        float(mtime),
        json.dumps(payload, sort_keys=True),
    )


def sync_landing_evidence(
    conn: sqlite3.Connection, project_root: Path, *, force: bool = False
) -> int:
    """Bring the landing projection in line with the evidence tree on disk.

    Returns the number of artifacts projected (0 when the fingerprint matched
    and nothing was rewritten). Malformed artifacts are skipped rather than
    raising: a corrupt file must leave its run *unresolved*, which is exactly
    what an absent row means, and must not take down every caller that opens the
    substrate.

    This is deliberately **not** wired into :func:`_native_rows_are_stale`. That
    predicate's only remedy is :func:`rebuild_from_runs`, which drops the whole
    database and can restore only what per-run JSON and its two snapshots can
    reconstruct; a landing observation arriving on a healthy substrate must not
    cost the readiness, shape-verdict, inline-remediation and triage rows nothing
    else holds.
    """
    sources = _landing_evidence_sources(project_root)
    fingerprint = _landing_projection_fingerprint(sources)
    if not force and _meta_get(conn, LANDING_PROJECTION_FINGERPRINT_KEY) == fingerprint:
        return 0
    assertions: list[tuple] = []
    attempts: list[tuple] = []
    for path, relpath, mtime in sources:
        payload = _landing_evidence_payload(path)
        if payload is None:
            continue
        if is_landing_assertion(payload):
            assertions.append(_landing_assertion_row(payload, path.name, relpath, mtime))
        elif is_landing_attempt(payload):
            attempts.append(_landing_attempt_row(payload, path.name, relpath, mtime))
    conn.execute("DELETE FROM landing_assertions")
    conn.execute("DELETE FROM landing_attempts")
    if assertions:
        columns = ", ".join(_LANDING_ASSERTION_COLUMNS)
        placeholders = ", ".join("?" for _ in _LANDING_ASSERTION_COLUMNS)
        conn.executemany(
            f"INSERT OR IGNORE INTO landing_assertions ({columns}) VALUES ({placeholders})",
            assertions,
        )
    if attempts:
        columns = ", ".join(_LANDING_ATTEMPT_COLUMNS)
        placeholders = ", ".join("?" for _ in _LANDING_ATTEMPT_COLUMNS)
        conn.executemany(
            f"INSERT OR IGNORE INTO landing_attempts ({columns}) VALUES ({placeholders})",
            attempts,
        )
    _meta_set(conn, LANDING_PROJECTION_FINGERPRINT_KEY, fingerprint)
    _meta_set(conn, LANDING_PROJECTION_SYNCED_AT_KEY, _now_iso())
    _meta_set(conn, LANDING_PROJECTION_SOURCE_COUNT_KEY, str(len(sources)))
    conn.commit()
    return len(assertions) + len(attempts)


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
    sync_landing_evidence(conn, project_root)
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
      - Landing evidence written since the last open → the landing projection
        is re-synced in place (#2849). This is a targeted refresh, *not* a
        staleness rebuild: landing observations arrive routinely, and routing
        them through :func:`rebuild_from_runs` would drop the readiness,
        shape-verdict, inline-remediation and triage rows that no file on disk
        can reconstruct, every time a story landed.
    """
    path = substrate_path(project_root)
    if not path.exists():
        if not has_audit_inputs(project_root):
            raise SubstrateMissingError(
                f"audit substrate not found at {path}. Run `forge audits rebuild` to create it."
            )
        if runs_dir(project_root).exists() and any(runs_dir(project_root).glob("*.json")):
            rebuild_from_runs(project_root)
            conn = _open_validated(path)
            sync_landing_evidence(conn, project_root)
            return conn
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
    sync_landing_evidence(conn, project_root)
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

    Consequence for the landing projection (#2849): because this opening cannot
    write, it cannot run :func:`sync_landing_evidence`, so it answers landing
    questions from whatever was last indexed. That is a real answer, not a
    silent one — the projection records when it was last synced and over how
    many artifacts, and
    :func:`~theforge.coordinator.audit_read_model.landing_projection_status`
    reports both, so a reader can tell "no evidence" from "not re-indexed since
    the evidence was written" and knows to run ``forge audits rebuild``.
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
      - any native row whose source_path still points at the canonical
        ``.forge/audits/runs/*.json`` tree now references a file that has been
        deleted, or whose mtime has changed since indexing;
        or
      - per-run JSON files exist on disk for which there is no native row
        with that source_path (a new run was emitted while this process
        was not the writer, or the index was hand-edited).

    Native rows without a source_path (e.g. programmatic inserts from
    sprint rollup) are intentionally NOT validated here — they have no
    canonical file to compare against. Native rows whose source_path was
    deliberately repointed outside the canonical tree (for example preserved
    single-story artifacts after a publish failure) are likewise left alone:
    they remain readable, but only canonical run files participate in stale
    rebuild detection.
    """
    runs = runs_dir(project_root)
    on_disk = list(runs.glob("*.json")) if runs.exists() else []
    cur = conn.execute(
        "SELECT source_path, source_mtime FROM audit_records "
        "WHERE provenance = 'native' AND source_path IS NOT NULL"
    )
    indexed: dict[str, float | None] = {}
    runs_rel = Path(*RUNS_RELPATH)
    for row in cur:
        rel = row[0] if not isinstance(row, sqlite3.Row) else row["source_path"]
        mtime = row[1] if not isinstance(row, sqlite3.Row) else row["source_mtime"]
        try:
            Path(str(rel)).relative_to(runs_rel)
        except ValueError:
            continue
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


def _migrate_v26_to_v27(record: dict) -> dict:
    """Backfill the absent topology-walk signal (issue #2372).

    v27 records, per run, the deterministic topology-walk evidence that routed a
    review loop to the escalate gate before its cycle ceiling — or ``null`` when
    the pattern was not detected. A v26 record predates the detector, so ``null``
    is the honest backfill: not "unknown", but known not to have been detected,
    because nothing could detect it. The stored record is never rewritten
    (ADR-0002 refusal-to-forget); this is the reader-side lift applied on load.
    """
    if "review_topology_signal" in record:
        return record
    return {**record, "review_topology_signal": None}


def _migrate_v27_to_v28(record: dict) -> dict:
    """Backfill the absent changed-file set (issue #2347).

    v28 records the file set the run changed against its base ref. A v27 record
    predates the capture entirely, and the comparison is not recoverable after
    the fact — the worktree and branch are gone, and commit-message attribution
    recovers a small minority of runs. So the honest backfill is ``null``:
    "no comparison was recorded", which is deliberately NOT the same claim as a
    recorded comparison that found nothing (``{"files": []}``). Conflating the
    two would make every pre-#2347 run look like a run that changed no files.
    The stored record is never rewritten (ADR-0002 refusal-to-forget); this is
    the reader-side lift applied on load.
    """
    if "changed_files" in record:
        return record
    return {**record, "changed_files": None}


def _migrate_v28_to_v29(record: dict) -> dict:
    """Backfill the absent validation-run provenance (issue #2358).

    v29 records, per validation run, which profile produced the result and what
    authority that profile carried. A v28 record predates profiles: every
    validation it performed was the single gate command, whose result was the
    merge decision. So the backfill is an empty list rather than an invented
    entry — the record's ``gate_decisions``/``gate_runs`` already say what ran,
    and reconstructing a per-run profile from them would fabricate provenance
    that was never observed. Readers treat an absent profile as the legacy
    complete/merge shape, so an old record keeps exactly the standing it had.
    The stored record is never rewritten (ADR-0002 refusal-to-forget).
    """
    block = record.get("iterations")
    if not isinstance(block, dict) or "validation_runs" in block:
        return record
    return {**record, "iterations": {**block, "validation_runs": []}}


def _migrate_v29_to_v30(record: dict) -> dict:
    """Backfill absent knowledge-summary reporting as explicitly unavailable.

    v30 records persist the post-DONE knowledge-summary generation outcome.
    Older records never carried that signal outside the raw run log, and there
    is no durable evidence from which to reconstruct whether generation was
    attempted or why it failed. Rather than inventing success or failure, the
    reader treats the field as "not attempted" with a legacy marker: newer
    runs distinguish the real attempted/written states, while older runs keep
    their pre-v30 uncertainty explicit.
    """
    if "knowledge_summary" in record:
        return record
    return {
        **record,
        "knowledge_summary": {
            "status": "not_attempted",
            "attempted": False,
            "written": False,
            "reason": "legacy_record",
        },
    }


def _migrate_v30_to_v31(record: dict) -> dict:
    """Backfill absent review diff-grounding as explicitly not established (#2525).

    v31 records what a review cycle's P1s were checked against before any of
    them was allowed to block: the story's own file set, where it came from, and
    which findings failed to ground. A v30 record predates the check entirely —
    every P1 it carries was eligible to block regardless of whether it named a
    file the story touched. Recomputing the set now would need the worktree,
    which landing deletes, so the backfill states the absence rather than
    inventing a set: ``available: false`` with a legacy source, which readers
    treat as "this run's findings were never diff-grounded". Reconstructing one
    would misreport an old run as having passed a check that did not exist.
    The stored record is never rewritten (ADR-0002 refusal-to-forget).
    """
    if "review_diff_grounding" in record:
        return record
    return {**record, "review_diff_grounding": None}


def _migrate_v31_to_v32(record: dict) -> dict:
    """Backfill absent gate-green salvage as "no checkpoint was ever held" (#2028).

    v32 records whether the run held a reviewed, gate-green commit as a landing
    floor, whether it landed that commit instead of a gate-red HEAD, and why it
    declined to. A v31 record predates the capability entirely: nothing about it
    retained a checkpoint, so every run it describes that failed on a terminal
    gate failure really did discard its work. ``None`` states that absence
    rather than inventing a checkpoint the run never had — which would misread
    an old exhaustion failure as a salvage forge chose not to take. The stored
    record is never rewritten (ADR-0002 refusal-to-forget).
    """
    if "gate_green_salvage" in record:
        return record
    return {**record, "gate_green_salvage": None}


#: Policy-provenance keys v33 adds to the preflight block, with the value that
#: states "this run had no such record" (#2137).
_V33_POLICY_DEFAULTS: dict[str, object] = {
    "blocking_basis": None,
    "policy_assertions_cited": [],
    "policy_assertions_resolved": [],
    "policy_retraction_candidates": [],
    "policy_ratification_candidates": [],
    "policy_blocking_authority": False,
    "policy_adjudication": {},
}


def _migrate_v32_to_v33(record: dict) -> dict:
    """Backfill absent policy-assertion provenance as "nothing was adjudicated" (#2137).

    v33 records which kind of blocker a BLOCKED preflight declared, the standing
    policy assertions it cited, and how each resolved against the ratified-policy
    registry. A v32 record predates the capability: its preflight cited nothing
    structurally, and no provenance was ever weighed. Empty collections state that
    absence rather than inventing citations — which would misread an old refusal as
    having been checked against a registry that did not exist. A record whose
    preflight block is absent (preflight never ran, or was skipped) is left alone
    for the same reason. The stored record is never rewritten (ADR-0002
    refusal-to-forget).
    """
    preflight = record.get("preflight")
    if not isinstance(preflight, dict):
        return record
    migrated = dict(preflight)
    for key, default in _V33_POLICY_DEFAULTS.items():
        if key not in migrated:
            migrated[key] = copy.deepcopy(default)
    return {**record, "preflight": migrated}


def _migrate_v33_to_v34(record: dict) -> dict:
    """Backfill structured prior-run index state and summary index maintenance (#2654).

    v34 persists two new facts that older records never wrote:

    - ``context_manifests[*].prior_run_context.index_state`` carries the
      selector's structured read state so analytics can distinguish
      readable-empty from failed-closed maintenance.
    - ``knowledge_summary.index_rebuild`` carries the post-write knowledge-index
      maintenance result.

    A v33 record predates both. For legacy prior-run manifests, absence means
    "preserve the old classifier semantics": enabled manifests read as
    ``ready``-equivalent controls rather than being retroactively reclassified
    from their prose note, and disabled manifests record ``None`` because the
    selector never ran. Summary maintenance is ``None`` because the run never
    recorded it. The stored record is never rewritten (ADR-0002
    refusal-to-forget).
    """
    migrated = dict(record)

    manifests = record.get("context_manifests")
    if isinstance(manifests, list):
        new_manifests: list[object] = []
        for entry in manifests:
            if not isinstance(entry, dict):
                new_manifests.append(entry)
                continue
            prior = entry.get("prior_run_context")
            if not isinstance(prior, dict) or "index_state" in prior:
                new_manifests.append(entry)
                continue
            updated_prior = dict(prior)
            enabled = updated_prior.get("enabled")
            updated_prior["index_state"] = "ready" if enabled is True else None
            new_manifests.append({**entry, "prior_run_context": updated_prior})
        migrated["context_manifests"] = new_manifests

    knowledge_summary = record.get("knowledge_summary")
    if isinstance(knowledge_summary, dict) and "index_rebuild" not in knowledge_summary:
        migrated["knowledge_summary"] = {**knowledge_summary, "index_rebuild": None}

    return migrated


def _migrate_v34_to_v35(record: dict) -> dict:
    """Backfill explicit absence for recorded config values (issue #2669).

    v35 records extend ``configuration`` with a versioned ``recorded_values``
    section containing per-key resolved values and their source labels. A v34
    record may have the older digest-only ``configuration`` block, or no block
    at all on older migrated history. The reader must distinguish "this run
    predates value capture" from "this run captured values and the queried key
    was absent", so legacy digest-only blocks are lifted to
    ``recorded_values: None`` rather than being silently treated as an empty
    value map.
    """
    configuration = record.get("configuration")
    if not isinstance(configuration, dict):
        return record
    if "recorded_values" in configuration:
        return record
    return {
        **record,
        "configuration": {**configuration, "recorded_values": None},
    }


def _migrate_v35_to_v36(record: dict) -> dict:
    """Advance v35 records to v36 without rewriting recorded config entries.

    v36 adds optional ``path_tokens`` metadata on ambiguous recorded-config
    entries so the reader can classify keys containing ``.`` or ``[]`` inside
    a mapping segment. Older records did not persist those tokens; the v36
    reader falls back to the legacy display-path split for them, so migration is
    intentionally a no-op.
    """
    return record


def _migrate_v36_to_v37(record: dict) -> dict:
    """Advance v36 records across the ``sprint.post_sprint_triage`` key (#2231).

    ``configuration.recorded_values.entries`` enumerates every resolved config
    key, so adding one changes the record's field set. v37 carries
    ``sprint.post_sprint_triage``; a v36 record predates the setting entirely.
    Backfilling a ``false`` here would claim the run resolved a key it never
    had — the reader already distinguishes an absent key ("missing") from a
    recorded one, and that is the honest answer for a run that could not have
    triggered a post-sprint triage pass. So the migration is a no-op and the
    stored record is never rewritten (ADR-0002 refusal-to-forget).
    """
    return record


def _migrate_v37_to_v38(record: dict) -> dict:
    """Advance v37 records across the ``spec_gaps`` block (#2122).

    v38 carries the specification-gap backchannel: every gap a dev agent raised
    and how each resolved. A v37 record predates the channel, so no gap could
    have been raised on it. Backfilling empty lists would be harmless but
    dishonest in the one way that matters here — an empty ``resolutions`` list
    asserts "this run had the channel and nothing was ambiguous", which is not
    what a pre-channel run observed. The reader distinguishes an absent block
    from an empty one, so the migration is a no-op and the stored record is
    never rewritten (ADR-0002 refusal-to-forget).
    """
    return record


def _migrate_v38_to_v39(record: dict) -> dict:
    """Advance v38 records across explicit gate-diagnostic workload meaning.

    v39 keeps the historical ``ran`` field for compatibility but also writes
    ``workload_executed`` so the audit record itself states what that value
    means. Some v38 records, however, persisted ``ran`` for attempted
    invocations whose output never proved workload execution, so migration only
    backfills the clearer alias when the legacy entry already carries
    independent structured evidence strong enough to classify honestly.
    """
    migrated = copy.deepcopy(record)
    iterations = migrated.get("iterations")
    if not isinstance(iterations, dict):
        return migrated
    diagnostics = iterations.get("gate_diagnostic")
    if not isinstance(diagnostics, list):
        return migrated
    for entry in diagnostics:
        if not isinstance(entry, dict) or "workload_executed" in entry:
            continue
        # v38 ``ran`` sometimes meant only "the diagnostic invocation was
        # attempted", so migration re-derives the clearer alias from evidence
        # that independently proves or refutes test execution.
        if entry.get("timed_out") is True:
            entry["workload_executed"] = True
            continue
        hanging_test = entry.get("hanging_test")
        if isinstance(hanging_test, str) and hanging_test.strip():
            entry["workload_executed"] = True
            continue
    return migrated


def _migrate_v39_to_v40(record: dict) -> dict:
    """Advance v39 records across ``workspace.setup_timeout`` config provenance.

    v40 records the setup-command timeout inside
    ``configuration.recorded_values.entries`` so operators can see the bound the
    run executed under. v39 predates that config field, but its runtime default
    was the long-standing 120-second ceiling. Migration backfills the defaulted
    recorded-value entry only when the configuration-provenance structure is
    present and the workspace block exists; records without that structure stay
    untouched rather than inventing provenance they never carried.
    """
    migrated = copy.deepcopy(record)
    configuration = migrated.get("configuration")
    if not isinstance(configuration, dict):
        return migrated
    recorded_values = configuration.get("recorded_values")
    if not isinstance(recorded_values, dict):
        return migrated
    entries = recorded_values.get("entries")
    if not isinstance(entries, dict):
        return migrated
    workspace = entries.get("workspace")
    if not isinstance(workspace, dict) or "setup_timeout" in workspace:
        return migrated
    workspace["setup_timeout"] = {"value": 120, "source": "default"}
    return migrated


def _migrate_v40_to_v41(record: dict) -> dict:
    """Advance v40 records across the preflight complexity gate (#2681).

    v41 carries ``preflight_complexity_gate`` (whether the end-of-preflight
    scope decision was put to an operator, and how it resolved), the
    ``outcome.returned_for_decomposition`` flag, and the two
    ``retry.preflight_complexity_gate_*`` keys that
    ``configuration.recorded_values`` enumerates. A v40 record predates the gate
    entirely, so no such decision could have been made on it. Backfilling
    ``opened: false`` would look harmless and assert the wrong thing — that the
    run had the gate and scored under it — when in fact the run was never
    offered the choice. The reader distinguishes an absent block from a present
    one, so the migration is a no-op and the stored record is never rewritten
    (ADR-0002 refusal-to-forget).
    """
    return record


def _migrate_v41_to_v42(record: dict) -> dict:
    """Advance v41 records across the preflight decomposition assessment (#2686).

    v42 adds the assessment the complexity pause now carries — the artifact,
    whether one was produced, the reason when none was, its cost/duration/model
    identity, and the operator's disposition of it — inside the existing
    ``preflight_complexity_gate`` block.

    A v41 record predates the assessment step entirely: its gate paused (or did
    not) without one ever being attempted. Backfilling ``assessment_generated:
    false`` would be indistinguishable from a run where the step ran and found
    nothing to split, which is precisely the distinction the audit exists to
    keep. The reader tells an absent key from a present one, so the migration is
    a no-op and the stored record is never rewritten (ADR-0002
    refusal-to-forget).
    """
    return record


def _migrate_v42_to_v43(record: dict) -> dict:
    """Advance v42 records across the entry-gate outcome (#2796).

    v43 adds ``workspace.entry_gate`` — the gate that ran *before* the run and
    routed it to DEV, with the budget it was killed at and the time it took —
    and ``workspace.entry_gate_surfaced_to_dev``.

    A v42 record predates the handoff entirely. Backfilling ``null``/``false``
    would be indistinguishable from a run whose reuse gate passed, or one whose
    dev agent was deliberately not told, and telling those apart is the whole
    point of the pair. The reader distinguishes an absent key from a present
    one, so the migration is a no-op and the stored record is never rewritten
    (ADR-0002 refusal-to-forget).
    """
    return record


def _migrate_v43_to_v44(record: dict) -> dict:
    """Backfill prior-run rendered-size telemetry as explicitly unmeasured (#2687).

    v44 adds ``rendered_size`` to *included* prior-run summary manifest entries.
    The field describes the rendered summary's prompt footprint and is
    descriptive telemetry, not attributed spend. A v43 record predates that
    capture entirely, so the reader must not let a missing key read as "zero
    size". Legacy included entries are therefore lifted to an explicit
    unmeasured payload; runs with no included summaries remain unchanged, which
    preserves the distinction between "no summaries were injected" and
    "summaries were injected but their rendered size was not measured".
    """
    manifests = record.get("context_manifests")
    if not isinstance(manifests, list):
        return record

    migrated_manifests: list[object] = []
    changed = False
    for entry in manifests:
        if not isinstance(entry, dict):
            migrated_manifests.append(entry)
            continue
        prior = entry.get("prior_run_context")
        if not isinstance(prior, dict):
            migrated_manifests.append(entry)
            continue
        included = prior.get("included")
        if not isinstance(included, list) or not included:
            migrated_manifests.append(entry)
            continue

        migrated_included: list[object] = []
        included_changed = False
        for item in included:
            if not isinstance(item, dict) or "rendered_size" in item:
                migrated_included.append(item)
                continue
            migrated_included.append(
                {
                    **item,
                    "rendered_size": {
                        "value": None,
                        "kind": "rendered_prompt_contribution",
                        "unavailable_reason": "unmeasured_legacy_record",
                    },
                }
            )
            included_changed = True

        if not included_changed:
            migrated_manifests.append(entry)
            continue

        changed = True
        migrated_manifests.append(
            {**entry, "prior_run_context": {**prior, "included": migrated_included}}
        )

    if not changed:
        return record
    return {**record, "context_manifests": migrated_manifests}


def _migrate_v44_to_v45(record: dict) -> dict:
    """Mark pre-capture runs uncomparable for prior-run uptake (#2684).

    v45 records which claims were rendered to which agent role, in which
    iteration of which phase, and when — and compares the review's findings
    against them. A v44 record captured none of that. The one thing this
    migration must not do is let that silence read as a comparison that found
    nothing: a run whose exposure was never recorded has findings that
    correspond to *unknown*, not to zero claims.

    So legacy records are lifted to an explicit ``uncomparable_pre_capture``
    report with null counts, and their prior-run manifests are left exactly as
    written (ADR-0002 refusal-to-forget) — absent ``claim_exposure`` is itself
    the evidence that this run predates capture.
    """
    if isinstance(record.get("prior_run_uptake"), dict):
        return record

    findings = record.get("finding_registry")
    finding_count = len(findings) if isinstance(findings, list) else 0
    return {
        **record,
        "prior_run_uptake": {
            "status": "uncomparable_pre_capture",
            "method": {"name": "rendered-claim-overlap", "version": "v1"},
            "author_role": "dev",
            "validation": {
                "status": "unvalidated",
                "reason": "not_measured",
                "agreement": None,
                "n": 0,
            },
            "interpretation": (
                "missed-uptake indicator only; contributes to no effectiveness verdict"
            ),
            "note": (
                "this run predates claim-exposure capture; what each agent was shown "
                "is not recorded, so its findings cannot be compared against injected claims"
            ),
            "claims_rendered": None,
            "claims_eligible": None,
            "review_findings": finding_count,
            "counts": None,
            "correspondences": None,
        },
    }


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
    26: _migrate_v26_to_v27,
    27: _migrate_v27_to_v28,
    28: _migrate_v28_to_v29,
    29: _migrate_v29_to_v30,
    30: _migrate_v30_to_v31,
    31: _migrate_v31_to_v32,
    32: _migrate_v32_to_v33,
    33: _migrate_v33_to_v34,
    34: _migrate_v34_to_v35,
    35: _migrate_v35_to_v36,
    36: _migrate_v36_to_v37,
    37: _migrate_v37_to_v38,
    38: _migrate_v38_to_v39,
    39: _migrate_v39_to_v40,
    40: _migrate_v40_to_v41,
    41: _migrate_v41_to_v42,
    42: _migrate_v42_to_v43,
    43: _migrate_v43_to_v44,
    44: _migrate_v44_to_v45,
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
    # Rewrite the per-file changed-set rows for this run_id (#2347), on the same
    # delete-then-insert discipline: a re-upsert of the same run replaces its
    # file rows rather than leaving a stale path indexed against it.
    _write_changed_files(conn, run_id, record)
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

    Scans the canonical ``.forge/audits/runs/*.json`` tree plus any preserved
    single-story run records under
    ``.forge/unpublished-story-run-artifacts/*/.forge/audits/runs/*.json``,
    validates ``run_id`` presence, and upserts each into a freshly recreated
    substrate with ``provenance='native'``. Records lacking a ``run_id`` are
    counted as failures (they cannot key the substrate deterministically).

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
    env_file = secrets_env_path(project_root)
    env_file_arg: Path | None = env_file if env_file.exists() else None
    for run_file in _run_record_paths(project_root):
        summary.runs_seen += 1
        relpath = run_file.relative_to(project_root)
        try:
            with open(run_file, encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            summary.failed += 1
            summary.failures.append(f"{relpath}: {exc}")
            continue
        if not isinstance(record, dict) or not record.get("run_id"):
            summary.failed += 1
            summary.failures.append(f"{relpath}: missing run_id")
            continue
        try:
            stat = run_file.stat()
            upsert_run_record(
                conn,
                record,
                provenance="native",
                source_path=str(relpath),
                source_mtime=stat.st_mtime,
                env_file=env_file_arg,
            )
            summary.imported += 1
        except sqlite3.DatabaseError as exc:
            summary.failed += 1
            summary.failures.append(f"{relpath}: {exc}")
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
    # Rebuild the landing projection from the canonical evidence artifacts
    # (#2849). Forced rather than fingerprint-guarded: ``create_or_open`` above
    # already projected into the fresh database, and the force makes the
    # reconstruction unconditional so this path cannot come to depend on
    # whatever a prior open happened to leave in ``meta``. A substrate rebuilt
    # from scratch therefore answers landing queries identically to one kept
    # current incrementally — both run this same function over the same files.
    sync_landing_evidence(conn, project_root, force=True)
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


# ── Event writers ────────────────────────────────────────────────────────


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


def _optional_float(value: object) -> float | None:
    """Coerce a cost/duration field to float, or None when it is not a number.

    An unmeasured cost is ``None`` here, never ``0.0``: "nothing was spent" and
    "nobody knows what was spent" are different facts and the substrate has to
    keep them apart (#1596).
    """
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def record_triage_proposal_event(project_root: Path, event: dict) -> int:
    """Insert one ``forge triage`` per-finding proposal row.

    Each row is a single finding's proposed disposition with the payload the
    taxonomy required, the packet hash it was proposed from, the evidence ids it
    cited, any validation errors that forced the ``needs_verification``
    fallback, the retry count, and what the proposal cost. Indexing
    ``finding_id`` is what makes a finding's disposition history a lookup rather
    than a scan, so a later run's packet can include what was proposed before.

    Required keys: ``finding_id``, ``disposition``. Returns the inserted row's
    ``event_id``. Raises :class:`SubstrateError` on missing required keys or I/O
    failure; the triage command treats audit failure as reportable, not fatal.
    """
    required = {"finding_id", "disposition"}
    missing = required - set(event)
    if missing:
        raise SubstrateError(f"triage proposal event missing required keys: {sorted(missing)}")
    raw_json = _canonical_json(event)
    emitted_at = event.get("emitted_at") or _now_iso()
    conn = create_or_open(project_root)
    try:
        cur = conn.execute(
            "INSERT INTO triage_proposal_events "
            "(triage_run_id, finding_id, issue_ref, packet_hash, disposition, "
            "target_milestone, punt_reason_code, evidence_refs, validation_errors, "
            "retry_count, review_verdict, review_evidence_refs, review_validation_errors, "
            "review_retry_count, review_fallback_reason, cost_usd, cost_provenance, "
            "review_cost_usd, review_cost_provenance, emitted_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.get("triage_run_id"),
                str(event["finding_id"]),
                event.get("issue_ref"),
                event.get("packet_hash"),
                str(event["disposition"]),
                event.get("target_milestone"),
                event.get("punt_reason_code"),
                _canonical_json(list(event.get("evidence_refs") or [])),
                _canonical_json(list(event.get("validation_errors") or [])),
                int(event.get("retry_count") or 0),
                (event.get("punt_review") or {}).get("verdict"),
                _canonical_json(list((event.get("punt_review") or {}).get("evidence_refs") or [])),
                _canonical_json(list(event.get("review_validation_errors") or [])),
                int(event.get("review_retry_count") or 0),
                event.get("review_fallback_reason"),
                _optional_float(event.get("cost_usd")),
                event.get("cost_provenance"),
                _optional_float(event.get("review_cost_usd")),
                event.get("review_cost_provenance"),
                emitted_at,
                raw_json,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def record_triage_proposal_run(project_root: Path, summary: dict) -> int:
    """Insert one ``forge triage`` proposal-run summary row.

    Written for every run including the empty-backlog one, which records an
    explicit zero cost rather than nothing at all: "the backlog was empty and
    this run spent $0.00" is an auditable fact, and its absence would be
    indistinguishable from a run that never happened.

    Required key: ``triage_run_id``. Returns the inserted row's ``run_row_id``.
    """
    if "triage_run_id" not in summary:
        raise SubstrateError("triage proposal run summary missing required key: triage_run_id")
    raw_json = _canonical_json(summary)
    emitted_at = summary.get("emitted_at") or _now_iso()
    conn = create_or_open(project_root)
    try:
        cur = conn.execute(
            "INSERT INTO triage_proposal_runs "
            "(triage_run_id, findings_count, total_cost_usd, cost_provenance, "
            "report_path, emitted_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(summary["triage_run_id"]),
                int(summary.get("findings_count") or 0),
                _optional_float(summary.get("total_cost_usd")),
                summary.get("cost_provenance"),
                summary.get("report_path"),
                emitted_at,
                raw_json,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def upsert_triage_application_record(project_root: Path, record: dict) -> int:
    """Insert or update one ratified/application state row for ``forge triage``.

    Required keys: ``triage_run_id``, ``finding_id``, ``operator_decision``,
    ``status``. The row is keyed by ``(triage_run_id, finding_id)`` so resume
    can update the same finding from ``ratified`` to ``applied`` / ``stale`` /
    ``failed`` without duplicating state.
    """
    required = {"triage_run_id", "finding_id", "operator_decision", "status"}
    missing = required - set(record)
    if missing:
        raise SubstrateError(f"triage application record missing required keys: {sorted(missing)}")
    raw_json = _canonical_json(record)
    emitted_at = record.get("emitted_at") or _now_iso()
    conn = create_or_open(project_root)
    try:
        conn.execute(
            "INSERT INTO triage_application_records "
            "(triage_run_id, finding_id, issue_ref, proposed_payload, operator_decision, "
            "applied_disposition, target_milestone, punt_reason_code, evidence_refs, "
            "operator_note, status, stale_reason, idempotency_marker, external_effect_summary, "
            "emitted_at, applied_at, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(triage_run_id, finding_id) DO UPDATE SET "
            "issue_ref=excluded.issue_ref, "
            "proposed_payload=excluded.proposed_payload, "
            "operator_decision=excluded.operator_decision, "
            "applied_disposition=excluded.applied_disposition, "
            "target_milestone=excluded.target_milestone, "
            "punt_reason_code=excluded.punt_reason_code, "
            "evidence_refs=excluded.evidence_refs, "
            "operator_note=excluded.operator_note, "
            "status=excluded.status, "
            "stale_reason=excluded.stale_reason, "
            "idempotency_marker=excluded.idempotency_marker, "
            "external_effect_summary=excluded.external_effect_summary, "
            "emitted_at=excluded.emitted_at, "
            "applied_at=excluded.applied_at, "
            "raw_json=excluded.raw_json",
            (
                str(record["triage_run_id"]),
                str(record["finding_id"]),
                record.get("issue_ref"),
                _canonical_json(record.get("proposed_payload") or {}),
                str(record["operator_decision"]),
                record.get("applied_disposition"),
                record.get("target_milestone"),
                record.get("punt_reason_code"),
                _canonical_json(list(record.get("evidence_refs") or [])),
                record.get("operator_note"),
                str(record["status"]),
                record.get("stale_reason"),
                record.get("idempotency_marker"),
                record.get("external_effect_summary"),
                emitted_at,
                record.get("applied_at"),
                raw_json,
            ),
        )
        conn.commit()
        return 1
    finally:
        conn.close()


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
