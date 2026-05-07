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

SUBSTRATE_SCHEMA_VERSION = 2
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
    complexity_score INTEGER,
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
    # Idempotent column adds for substrates created under an older schema.
    # SQLite < 3.35 lacks ADD COLUMN IF NOT EXISTS, so swallow the duplicate
    # error from re-runs.
    try:
        conn.execute("ALTER TABLE audit_records ADD COLUMN complexity_score INTEGER")
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
    skipped_protected: bool = False  # legacy attempted to overwrite a native row
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
        raw_json,
    )
    conn.execute(
        "INSERT INTO audit_records "
        "(run_id, slug, started_at, finished_at, total_cost_usd, final_phase, "
        "outcome_success, branch, landing_status, provenance, source_path, "
        "source_mtime, complexity_score, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET "
        "slug=excluded.slug, started_at=excluded.started_at, "
        "finished_at=excluded.finished_at, total_cost_usd=excluded.total_cost_usd, "
        "final_phase=excluded.final_phase, outcome_success=excluded.outcome_success, "
        "branch=excluded.branch, landing_status=excluded.landing_status, "
        "provenance=excluded.provenance, source_path=excluded.source_path, "
        "source_mtime=excluded.source_mtime, "
        "complexity_score=excluded.complexity_score, raw_json=excluded.raw_json",
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
                _import_history_classify(result, summary)
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
    rows = conn.execute("SELECT raw_json FROM audit_records ORDER BY COALESCE(started_at, '') ASC")
    for row in rows:
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        derived = _derive_escalation(record)
        if derived is not None:
            yield derived


def _derive_escalation(record: dict) -> dict | None:
    """Project an audit record to its escalation-shape projection."""
    task = record.get("task") or {}
    slug = str(task.get("slug") or "").strip()
    outcome = record.get("outcome") or {}
    success = outcome.get("success")
    if not isinstance(success, bool):
        return None
    preflight = record.get("preflight") if isinstance(record.get("preflight"), dict) else {}
    complexity = str(preflight.get("complexity") or "").strip()

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
