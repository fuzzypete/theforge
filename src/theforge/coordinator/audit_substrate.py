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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

SUBSTRATE_SCHEMA_VERSION = 1
SUBSTRATE_RELPATH = (".forge", "audits", "index.sqlite")
HISTORY_RELPATH = (".forge", "audits", "history.jsonl")
RUNS_RELPATH = (".forge", "audits", "runs")
AUDITS_RELPATH = (".forge", "audits")


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
"""


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
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


def require_substrate(project_root: Path) -> sqlite3.Connection:
    """Open a substrate that must already exist and be valid.

    Triggers the one-shot legacy import on first call when an existing
    ``history.jsonl`` is present and the substrate has not yet imported it.
    Raises :class:`SubstrateMissingError` when neither the substrate nor a
    legacy ``history.jsonl`` is present (fresh repo) — well, fresh-repo
    handling is left to the caller; here we treat that as missing.
    Raises :class:`SubstrateCorruptError` when the file exists but cannot
    be opened or fails ``PRAGMA integrity_check``.
    """
    path = substrate_path(project_root)
    history = history_jsonl_path(project_root)
    if not path.exists():
        # Auto-bootstrap on legacy upgrade: create + import.
        if history.exists():
            conn = create_or_open(project_root)
            _maybe_run_one_shot_legacy_import(project_root, conn)
            return conn
        raise SubstrateMissingError(
            f"audit substrate not found at {path}. Run `forge audits rebuild` to create it."
        )
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        # Cheap integrity check.
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or (row[0] != "ok"):
            detail = row[0] if row else "no result"
            raise SubstrateCorruptError(
                f"audit substrate at {path} failed integrity check: {detail}"
            )
        # Make sure schema is present (idempotent).
        _apply_schema(conn)
    except sqlite3.DatabaseError as exc:
        raise SubstrateCorruptError(
            f"audit substrate at {path} could not be opened: {exc}. "
            "Run `forge audits rebuild [--include-legacy-history]` to recover."
        ) from exc
    _maybe_run_one_shot_legacy_import(project_root, conn)
    return conn


def _maybe_run_one_shot_legacy_import(project_root: Path, conn: sqlite3.Connection) -> None:
    """Run the legacy import once per repo when conditions are met.

    Conditions:
      - ``history.jsonl`` exists.
      - meta key ``legacy_import_done`` is unset.
      - No rows with ``provenance='legacy_history_jsonl'`` exist (defensive).
    """
    history = history_jsonl_path(project_root)
    if not history.exists():
        return
    flag = _meta_get(conn, "legacy_import_done")
    if flag == "1":
        return
    existing = conn.execute(
        "SELECT 1 FROM audit_records WHERE provenance = 'legacy_history_jsonl' LIMIT 1"
    ).fetchone()
    if existing is not None:
        # Already imported in a prior session; just set the flag forward.
        _meta_set(conn, "legacy_import_done", "1")
        conn.commit()
        return
    print(
        "[forge] migrating history.jsonl into audit substrate (one-shot)",
        flush=True,
    )
    summary = _import_history_jsonl_into(conn, history)
    _meta_set(conn, "legacy_import_done", "1")
    conn.commit()
    print(
        f"[forge] imported {summary.imported} records from history.jsonl",
        flush=True,
    )
    print(
        f"[forge] skipped existing {summary.skipped_existing}, "
        f"updated/repaired {summary.updated_repaired}, failed {summary.failed}",
        flush=True,
    )
    print(
        f"[forge] substrate ready at {substrate_path(project_root)}",
        flush=True,
    )


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
    deterministic ``legacy:<sha1[:16]>`` identifier from slug, started_at
    and the canonical JSON.
    """
    rid = record.get("run_id")
    if isinstance(rid, str) and rid:
        return rid
    slug = ((record.get("task") or {}).get("slug")) or record.get("sprint", {}).get("slug") or ""
    started = ((record.get("timing") or {}).get("started_at")) or record.get("started_at") or ""
    digest = hashlib.sha1(
        f"{slug}|{started}|{_canonical_json(record)}".encode("utf-8")
    ).hexdigest()
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
    raw_landing = record.get("landing_event")
    landing_event = raw_landing if isinstance(raw_landing, dict) else {}

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

    return {
        "slug": task.get("slug"),
        "started_at": timing.get("started_at"),
        "finished_at": timing.get("finished_at"),
        "total_cost_usd": total_cost_usd,
        "final_phase": final_phase,
        "outcome_success": outcome_success,
        "branch": workspace.get("branch"),
        "landing_status": landing_status,
    }


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
    run_id: str = ""


def upsert_run_record(
    conn: sqlite3.Connection,
    record: dict,
    *,
    provenance: str,
    source_path: str | None = None,
    source_mtime: float | None = None,
) -> UpsertResult:
    """Insert-or-replace a single audit record.

    Returns :class:`UpsertResult` indicating whether this was a fresh
    insert, an update of an existing row with different content, or an
    unchanged row. Reviews flat-table is rewritten on each upsert.
    """
    run_id = derive_run_id(record)
    raw_json = _canonical_json(record)
    existing = conn.execute(
        "SELECT raw_json FROM audit_records WHERE run_id = ?", (run_id,)
    ).fetchone()
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
        raw_json,
    )
    conn.execute(
        "INSERT INTO audit_records "
        "(run_id, slug, started_at, finished_at, total_cost_usd, final_phase, "
        "outcome_success, branch, landing_status, provenance, source_path, "
        "source_mtime, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET "
        "slug=excluded.slug, started_at=excluded.started_at, "
        "finished_at=excluded.finished_at, total_cost_usd=excluded.total_cost_usd, "
        "final_phase=excluded.final_phase, outcome_success=excluded.outcome_success, "
        "branch=excluded.branch, landing_status=excluded.landing_status, "
        "provenance=excluded.provenance, source_path=excluded.source_path, "
        "source_mtime=excluded.source_mtime, raw_json=excluded.raw_json",
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
    """
    path = substrate_path(project_root)
    if path.exists():
        path.unlink()
    conn = create_or_open(project_root)
    summary = RebuildSummary()
    runs = runs_dir(project_root)
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
                )
                summary.imported += 1
            except sqlite3.DatabaseError as exc:
                summary.failed += 1
                summary.failures.append(f"{run_file.name}: {exc}")
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


def _import_history_jsonl_into(conn: sqlite3.Connection, history_path: Path) -> ImportSummary:
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
                    )
                except sqlite3.DatabaseError as exc:
                    summary.failed += 1
                    summary.failures.append(f"line {lineno}: {exc}")
                    continue
                if result.inserted:
                    summary.imported += 1
                elif result.updated:
                    summary.updated_repaired += 1
                else:
                    summary.skipped_existing += 1
    except OSError as exc:
        summary.failed += 1
        summary.failures.append(f"open: {exc}")
    conn.commit()
    return summary


def import_history_jsonl(project_root: Path) -> ImportSummary:
    """Public entry: open (or create) substrate and import legacy history."""
    conn = create_or_open(project_root)
    try:
        summary = _import_history_jsonl_into(conn, history_jsonl_path(project_root))
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
        "SELECT a.raw_json FROM audit_records a "
        "JOIN reviews r ON r.run_id = a.run_id "
        "WHERE a.slug = ? AND r.verdict = 'APPROVE'"
    )
    params: tuple = (slug,)
    if require_landed:
        sql += " AND a.landing_status = 'landed'"
    for row in conn.execute(sql, params):
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def iter_records(conn: sqlite3.Connection, *, order_by_started: bool = True) -> Iterable[dict]:
    """Iterate raw_json dicts for all audit records."""
    sql = "SELECT raw_json FROM audit_records"
    if order_by_started:
        sql += " ORDER BY started_at ASC"
    for row in conn.execute(sql):
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def tail_records(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Return the most-recent ``limit`` records ordered by started_at DESC."""
    rows = conn.execute(
        "SELECT raw_json FROM audit_records ORDER BY COALESCE(started_at, '') DESC LIMIT ?",
        (max(0, int(limit)),),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def count_records(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()
    if row is None:
        return 0
    return int(row[0])
