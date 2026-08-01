"""SQLite event emission for `forge shape` invocations.

The audit substrate's primary table (``audit_records``) is keyed on
per-sprint runs; a shape invocation is too lightweight to fit that shape.
This module adds a separate ``shape_events`` table to the same substrate
file so the v0.11 audit consumers can query ``forge shape`` activity —
in particular the kept-as-``todo:draft`` rate that signals refusal-
capability for compound-engineering metrics.

Stdlib only.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

from theforge.coordinator.audit_substrate import substrate_path

_SHAPE_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS shape_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emitted_at TEXT NOT NULL,
    issue_number INTEGER,
    input_source TEXT NOT NULL,
    classification TEXT NOT NULL,
    confidence TEXT NOT NULL,
    ambiguity_question_count INTEGER NOT NULL DEFAULT 0,
    apply_mutated INTEGER NOT NULL DEFAULT 0,
    diagnosis_state TEXT,
    gate_verdict TEXT
);
CREATE INDEX IF NOT EXISTS idx_shape_events_emitted_at
    ON shape_events(emitted_at);
CREATE INDEX IF NOT EXISTS idx_shape_events_classification
    ON shape_events(classification);
"""

VALID_INPUT_SOURCES: frozenset[str] = frozenset({"issue", "file", "stdin", "none"})


def _ensure_shape_events(conn: sqlite3.Connection) -> None:
    conn.executescript(_SHAPE_EVENTS_SCHEMA)
    # Idempotent column add for substrates created under an older schema.
    # SQLite < 3.35 lacks ADD COLUMN IF NOT EXISTS, so swallow the duplicate
    # error from re-runs (same pattern as audit_substrate._apply_schema).
    try:
        conn.execute("ALTER TABLE shape_events ADD COLUMN gate_verdict TEXT")
    except sqlite3.OperationalError:
        pass


def _open_or_create(project_root: Path) -> sqlite3.Connection:
    path = substrate_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_shape_events(conn)
    return conn


def emit_shape_event(
    project_root: Path,
    *,
    issue_number: int | None,
    input_source: str,
    classification: str,
    confidence: str,
    ambiguity_question_count: int,
    apply_mutated: bool,
    diagnosis_state: str | None = None,
    gate_verdict: str | None = None,
) -> int:
    """Insert a single shape event row. Returns the new ``id``.

    ``gate_verdict`` is the shape-gate verdict observed on the issue's state at
    invocation time (``None`` when the invocation failed before the issue could
    be read). It is what makes a "no action needed" report reconstructable from
    the audit trail rather than only from the operator's terminal (#2054).

    Emission is best-effort wrt schema migrations on older substrates: the
    ``shape_events`` table is created idempotently before insert. Callers
    should treat this as fire-and-forget — they must not let an audit
    failure interrupt the CLI exit path.
    """
    if input_source not in VALID_INPUT_SOURCES:
        raise ValueError(f"input_source {input_source!r} not in {sorted(VALID_INPUT_SOURCES)}")
    conn = _open_or_create(project_root)
    try:
        cur = conn.execute(
            "INSERT INTO shape_events("
            "emitted_at, issue_number, input_source, classification, "
            "confidence, ambiguity_question_count, apply_mutated, diagnosis_state, "
            "gate_verdict"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now_iso(),
                issue_number,
                input_source,
                classification,
                confidence,
                int(ambiguity_question_count),
                1 if apply_mutated else 0,
                diagnosis_state,
                gate_verdict,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def count_kept_as_draft(project_root: Path) -> int:
    """Return the number of shape events whose outcome was UNRESOLVED.

    Used by audit consumers checking refusal-capability metrics.
    """
    path = substrate_path(project_root)
    if not path.exists():
        return 0
    conn = sqlite3.connect(str(path))
    try:
        _ensure_shape_events(conn)
        row = conn.execute(
            "SELECT COUNT(*) FROM shape_events WHERE classification = ?",
            ("unresolved",),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


__all__ = ["emit_shape_event", "count_kept_as_draft", "VALID_INPUT_SOURCES"]
