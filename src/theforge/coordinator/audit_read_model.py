"""Analytical read model over the SQLite audit substrate.

This module owns the *read* side of the audit substrate: the SELECT queries and
the derivations built on top of them. It answers retrospective questions —
what did this path cost, which runs escalated, what did an alias resolve to,
which cost cohort does a complexity score fall in — and it owns nothing about
how the substrate is stored.

The boundary it depends on is :data:`theforge.coordinator.audit_storage.AuditConnection`:
a sqlite3 connection that storage has already opened, validated, and brought up
to the current schema. Given one, readers here issue SELECT SQL against it
directly rather than routing every query through a storage-owned accessor —
adding a query is a function in this file, not a new method on storage. When a
query needs the decoded record rather than the indexed columns it calls
storage's :func:`~theforge.coordinator.audit_storage._load_migrated`, so record
migration stays storage's responsibility here too.

What does *not* belong here: ``CREATE TABLE``/``ALTER TABLE``, the
``_migrate_*`` catalogue, ``upsert``/``rebuild``/``record_*`` writers, and
connection opening. Those live in :mod:`theforge.coordinator.audit_storage`.
The dependency runs one way — storage never imports this module — which is what
makes a new analytical query and a new record migration independent changes.

``audit_substrate`` re-exports this module's public names for compatibility.
"""

from __future__ import annotations

import datetime
import importlib
import json
import re
import sqlite3
import types
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Union, get_args, get_origin, get_type_hints

from theforge.config import ForgeConfig

from .agent_identity import (
    dev_identity_ledger,
    dev_model_identity_detail,
    entry_identity_ledger,
)
from .audit_storage import (
    _INVOCATION_IDENTITY_COLUMNS,
    LANDING_PROJECTION_SOURCE_COUNT_KEY,
    LANDING_PROJECTION_SYNCED_AT_KEY,
    AuditConnection,
    _load_migrated,
    _meta_get,
    has_audit_inputs,
    require_substrate,
    substrate_path,
)
from .landing_evidence import RESOLVED_NON_LANDING_OUTCOMES

# The three landing states, named once. ``LANDING_UNRESOLVED`` is the one the
# flattened ``landing_status`` column could never express: it means nobody has
# observed an outcome yet, which is a different fact from an observed failure to
# land, and collapsing the two is what let a completion-time snapshot read as an
# outcome (#2849).
LANDING_LANDED = "landed"
LANDING_NOT_LANDED = "not_landed"
LANDING_UNRESOLVED = "unresolved"


def verdict_outcome_counts(conn: AuditConnection) -> list[dict]:
    """Return run counts grouped by review verdict and landed outcome.

    Answers "how often does each verdict actually end in a successful run" —
    an APPROVE that keeps ending in an unsuccessful outcome means the verdict
    is not predicting what it claims to.

    This query is the worked example for the storage/read-model boundary
    (#2350): it reads columns ``audit_records`` already indexes, so it lands
    entirely in this module. No ``CREATE TABLE``, no ``ALTER TABLE``, no
    ``_migrate_*`` entry, no ``SUBSTRATE_SCHEMA_VERSION`` bump — nothing in
    :mod:`theforge.coordinator.audit_storage` changes to add it. Rows with a
    NULL ``verdict`` are reported under ``verdict=None`` rather than dropped,
    because "we never recorded one" is itself an answer.
    """
    rows = conn.execute(
        "SELECT verdict, outcome_success, COUNT(*) AS n, "
        "SUM(COALESCE(total_cost_usd, 0.0)) AS cost "
        "FROM audit_records GROUP BY verdict, outcome_success "
        "ORDER BY n DESC, verdict IS NULL, verdict"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            verdict, success, count, cost = (
                row["verdict"],
                row["outcome_success"],
                row["n"],
                row["cost"],
            )
        else:
            verdict, success, count, cost = row[0], row[1], row[2], row[3]
        out.append(
            {
                "verdict": verdict,
                "outcome_success": None if success is None else bool(success),
                "runs": int(count),
                "total_cost_usd": float(cost or 0.0),
            }
        )
    return out


def runs_touching_path(conn: AuditConnection, path: str) -> list[dict]:
    """Return the runs that changed ``path``, joined to their cost and outcome.

    The query this table exists for. It answers "what did this file cost" by
    index lookup on ``audit_changed_files.path`` joined to ``audit_records`` on
    ``run_id`` — no ``raw_json`` is deserialized, which is the difference
    between a query that is run and one that is not.
    """
    rows = conn.execute(
        "SELECT c.run_id, c.insertions, c.deletions, c.binary, c.base_ref, c.head_ref, "
        "r.slug, r.issue_id, r.total_cost_usd, r.complexity_score, r.outcome_success, "
        "r.verdict, r.started_at "
        "FROM audit_changed_files c JOIN audit_records r ON r.run_id = c.run_id "
        "WHERE c.path = ? ORDER BY r.started_at",
        (path,),
    ).fetchall()
    return [
        {
            "run_id": row[0],
            "insertions": row[1],
            "deletions": row[2],
            "binary": bool(row[3]),
            "base_ref": row[4],
            "head_ref": row[5],
            "slug": row[6],
            "issue_id": row[7],
            "total_cost_usd": row[8],
            "complexity_score": row[9],
            "outcome_success": row[10],
            "verdict": row[11],
            "started_at": row[12],
        }
        for row in rows
    ]


def changed_file_touch_rows(
    conn: AuditConnection,
    *,
    since: str | None = None,
    measured_cost_only: bool = True,
) -> list[dict]:
    """Return every ``(run, path)`` touch joined to that run's indexed columns.

    The bulk form of :func:`runs_touching_path`: one scan instead of one query
    per candidate path, which is what an analysis ranking *all* paths needs.
    Every column comes from the two tables' indexed fields — no ``raw_json`` is
    decoded — so the cost of the scan is the join, not deserialization.

    ``since`` filters on ``audit_records.started_at`` (inclusive, compared
    lexically; the stored format is zero-padded ISO-8601 so lexical order is
    chronological). ``measured_cost_only`` keeps only runs with a positive
    recorded cost: a cost-unknown run is a lower bound on what the work needed,
    not a measurement of what it cost, and averaging one in understates every
    path it touched.
    """
    clauses: list[str] = []
    params: list[object] = []
    if measured_cost_only:
        clauses.append("r.total_cost_usd IS NOT NULL AND r.total_cost_usd > 0")
    if since is not None:
        clauses.append("r.started_at >= ?")
        params.append(since)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT c.run_id, c.path, c.insertions, c.deletions, c.binary, "
        "r.slug, r.issue_id, r.started_at, r.total_cost_usd, r.complexity_score, "
        "r.outcome_success, r.verdict, r.dev_model, r.dev_resolved_model, r.milestone "
        "FROM audit_changed_files c JOIN audit_records r ON r.run_id = c.run_id"
        f"{where} ORDER BY r.started_at, c.run_id, c.path",
        tuple(params),
    ).fetchall()
    return [
        {
            "run_id": row[0],
            "path": row[1],
            "insertions": row[2],
            "deletions": row[3],
            "binary": bool(row[4]),
            "slug": row[5],
            "issue_id": row[6],
            "started_at": row[7],
            "total_cost_usd": row[8],
            "complexity_score": row[9],
            "outcome_success": row[10],
            "verdict": row[11],
            "dev_model": row[12],
            "dev_resolved_model": row[13],
            "milestone": row[14],
        }
        for row in rows
    ]


def changed_file_coverage(conn: AuditConnection, *, since: str | None = None) -> dict:
    """Return how much measured spend is attributable to a changed-file set.

    The join in :func:`changed_file_touch_rows` is only as good as its coverage:
    a ranking computed over the joinable minority of runs describes that
    minority, not the codebase. This reports the denominator so a caller can say
    so rather than presenting a partial ranking as a complete one.

    Returned keys: ``measured_runs`` / ``measured_spend_usd`` (runs with a
    positive recorded cost), ``joinable_runs`` / ``joinable_spend_usd`` (of
    those, the ones with at least one ``audit_changed_files`` row), the two
    derived ratios, and the ``first_joinable_at`` / ``last_joinable_at`` window
    the joinable rows span. Ratios are 0.0 when there is nothing to divide.
    """
    clauses = ["total_cost_usd IS NOT NULL", "total_cost_usd > 0"]
    params: list[object] = []
    if since is not None:
        clauses.append("started_at >= ?")
        params.append(since)
    where = " WHERE " + " AND ".join(clauses)
    joinable_clause = (
        " AND EXISTS (SELECT 1 FROM audit_changed_files c WHERE c.run_id = audit_records.run_id)"
    )
    measured = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_cost_usd), 0.0) FROM audit_records" + where,
        tuple(params),
    ).fetchone()
    joinable = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_cost_usd), 0.0), "
        "MIN(started_at), MAX(started_at) FROM audit_records" + where + joinable_clause,
        tuple(params),
    ).fetchone()
    measured_runs, measured_spend = int(measured[0]), float(measured[1] or 0.0)
    joinable_runs, joinable_spend = int(joinable[0]), float(joinable[1] or 0.0)
    return {
        "measured_runs": measured_runs,
        "measured_spend_usd": measured_spend,
        "joinable_runs": joinable_runs,
        "joinable_spend_usd": joinable_spend,
        "run_coverage_ratio": (joinable_runs / measured_runs) if measured_runs else 0.0,
        "spend_coverage_ratio": (joinable_spend / measured_spend) if measured_spend else 0.0,
        "first_joinable_at": joinable[2],
        "last_joinable_at": joinable[3],
    }


# ── Landing evidence projection ──────────────────────────────────────────
#
# Which reader reads what (#2849). Two questions look alike and are not:
#
# * **The landed query.** ``has_review_approve_in_substrate(require_landed=True)``
#   and the ``landing_*`` readers below answer "did this actually land?" They
#   read the projected assertion rows, because that is where the observation of
#   the landing lives, with the ``observed_at`` the observer recorded.
# * **The flattened-column readers.** ``latest_run_outcome_in_substrate`` and the
#   merge-evidence resolution downstream of it read
#   ``audit_records.landing_status`` and are deliberately untouched here. That
#   column is the sprint scheduler's completion-time answer, and re-pointing its
#   consumers is sequenced as its own follow-on. Do not "fix" it in passing: the
#   two answers legitimately differ for a run whose pull request merged after the
#   record was written, and the follow-on needs that difference observable.


def _landing_state_from_rows(has_assertion: bool, last_outcome: str | None) -> str:
    """The three-state landing answer for one run, from its projected rows.

    Mirrors :func:`theforge.coordinator.landing_evidence.landing_state` over the
    files, using the same shared outcome partition, so a SQL reader and a
    filesystem reader cannot disagree about the same run.
    """
    if has_assertion:
        return LANDING_LANDED
    if last_outcome is not None and last_outcome in RESOLVED_NON_LANDING_OUTCOMES:
        return LANDING_NOT_LANDED
    return LANDING_UNRESOLVED


# One row per run known to *either* side: every audit record, plus any run that
# has landing evidence but no indexed record (evidence for a run whose record
# was never published is still evidence). The attempt sub-select takes the last
# artifact by name, which is the sequence order the writer assigns.
_LANDING_STATE_SQL = """
SELECT
    ids.run_id                     AS run_id,
    COALESCE(la.slug, att.slug, a.slug) AS slug,
    a.started_at                   AS run_started_at,
    a.finished_at                  AS run_finished_at,
    a.landing_status               AS flattened_landing_status,
    la.run_id IS NOT NULL          AS has_assertion,
    la.observed_at                 AS observed_at,
    la.landing_mode                AS landing_mode,
    la.target_branch               AS target_branch,
    la.observer                    AS observer,
    la.carrier_kind                AS carrier_kind,
    la.carrier_ref                 AS carrier_ref,
    la.landed_commit               AS landed_commit,
    att.outcome                    AS last_attempt_outcome,
    att.observed_at                AS last_attempt_observed_at,
    att.landing_mode               AS last_attempt_landing_mode,
    att.target_branch              AS last_attempt_target_branch,
    att.observer                   AS last_attempt_observer
FROM (
    SELECT run_id FROM audit_records
    UNION SELECT run_id FROM landing_assertions
    UNION SELECT run_id FROM landing_attempts
) ids
LEFT JOIN audit_records a ON a.run_id = ids.run_id
LEFT JOIN landing_assertions la ON la.run_id = ids.run_id
LEFT JOIN landing_attempts att ON att.run_id = ids.run_id AND att.artifact_name = (
    SELECT MAX(inner_att.artifact_name) FROM landing_attempts inner_att
    WHERE inner_att.run_id = ids.run_id
)
"""

# Column order of ``_LANDING_STATE_SQL``, so a row from a connection without a
# ``sqlite3.Row`` factory reads the same as one with it.
_LANDING_STATE_COLUMNS = (
    "run_id",
    "slug",
    "run_started_at",
    "run_finished_at",
    "flattened_landing_status",
    "has_assertion",
    "observed_at",
    "landing_mode",
    "target_branch",
    "observer",
    "carrier_kind",
    "carrier_ref",
    "landed_commit",
    "last_attempt_outcome",
    "last_attempt_observed_at",
    "last_attempt_landing_mode",
    "last_attempt_target_branch",
    "last_attempt_observer",
)


def _parse_iso(value: object) -> datetime.datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed


def _landing_row(row: object) -> dict:
    if isinstance(row, sqlite3.Row):
        values = {name: row[name] for name in _LANDING_STATE_COLUMNS}
    else:
        values = dict(zip(_LANDING_STATE_COLUMNS, row))

    def get(name: str):
        return values.get(name)

    state = _landing_state_from_rows(bool(get("has_assertion")), get("last_attempt_outcome"))
    observed_at = get("observed_at")
    finished_at = get("run_finished_at")
    # AC2: the interval is derivable only when the run holds *both* endpoints.
    # A missing endpoint is reported as such — never defaulted to now, to the
    # run's start, or to a neighbouring run's timestamp, all of which would
    # manufacture a latency nobody measured.
    missing: list[str] = []
    if finished_at is None:
        missing.append("run_finished_at")
    if observed_at is None:
        missing.append("landing_observed_at")
    seconds: float | None = None
    if not missing:
        start, end = _parse_iso(finished_at), _parse_iso(observed_at)
        if start is None or end is None:
            missing.append("unparseable_timestamp")
        else:
            seconds = (end - start).total_seconds()
    interval = {
        "state": LANDING_UNRESOLVED if missing else "resolved",
        "seconds": seconds,
        "missing_endpoints": missing,
    }
    return {
        "run_id": get("run_id"),
        "slug": get("slug"),
        "landing_state": state,
        "observed_at": observed_at,
        "run_started_at": get("run_started_at"),
        "run_finished_at": finished_at,
        "landing_mode": get("landing_mode"),
        "target_branch": get("target_branch"),
        "observer": get("observer"),
        "carrier_kind": get("carrier_kind"),
        "carrier_ref": get("carrier_ref"),
        "landed_commit": get("landed_commit"),
        "last_attempt": (
            None
            if get("last_attempt_outcome") is None
            else {
                "outcome": get("last_attempt_outcome"),
                "observed_at": get("last_attempt_observed_at"),
                "landing_mode": get("last_attempt_landing_mode"),
                "target_branch": get("last_attempt_target_branch"),
                "observer": get("last_attempt_observer"),
            }
        ),
        "landing_interval": interval,
        "flattened_landing_status": get("flattened_landing_status"),
    }


def landing_state_for_run(conn: AuditConnection, run_id: str) -> dict | None:
    """The projected landing state of one run, or ``None`` if it is unknown here.

    ``None`` means the substrate has neither a record nor evidence for this run
    — a different answer from a row reporting ``unresolved``, which means the run
    is known and nobody has observed its landing yet.
    """
    row = conn.execute(f"{_LANDING_STATE_SQL} WHERE ids.run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return _landing_row(row)


def landing_states(conn: AuditConnection, *, slug: str | None = None) -> list[dict]:
    """Every run's projected landing state, newest run first.

    The row keeps ``landed`` / ``not_landed`` / ``unresolved`` distinct all the
    way out: a run with no evidence is ``unresolved`` and a run whose last
    attempt resolved without landing is ``not_landed``, and no caller can
    collapse them by accident because they are different string values rather
    than a nullable boolean.

    Each row also carries the assertion's own ``observed_at`` alongside the
    run's ``run_started_at`` / ``run_finished_at``, plus a ``landing_interval``
    that is ``resolved`` with a measured ``seconds`` only when the run holds
    both endpoints. That is what makes finished-to-landed latency answerable
    from the substrate alone.

    Note for read-only openings: :func:`theforge.coordinator.audit_storage.open_readonly`
    cannot re-sync the projection, so it answers from the last-indexed state.
    Pair this with :func:`landing_projection_status` when that matters.
    """
    sql = _LANDING_STATE_SQL
    params: tuple = ()
    if slug is not None:
        sql += " WHERE COALESCE(la.slug, att.slug, a.slug) = ?"
        params = (slug,)
    sql += " ORDER BY COALESCE(a.started_at, la.observed_at, '') DESC, ids.run_id DESC"
    return [_landing_row(row) for row in conn.execute(sql, params).fetchall()]


def landed_run_ids_in_substrate(conn: AuditConnection) -> set[str]:
    """Run ids carrying a projected positive landing assertion."""
    rows = conn.execute("SELECT run_id FROM landing_assertions").fetchall()
    return {str(row[0]) for row in rows}


def landing_projection_status(conn: AuditConnection) -> dict:
    """What the landing projection currently holds, and when it was last synced.

    Exists so a read-only surface can say *why* it has no landed rows. Storage
    syncs the projection on every writable open; a read-only opening cannot, so
    ``synced_at`` older than the evidence an operator just watched being written
    means "re-index", not "never landed".
    """
    assertions = conn.execute("SELECT COUNT(*) FROM landing_assertions").fetchone()
    attempts = conn.execute("SELECT COUNT(*) FROM landing_attempts").fetchone()
    raw_count = _meta_get(conn, LANDING_PROJECTION_SOURCE_COUNT_KEY)
    try:
        source_count = None if raw_count is None else int(raw_count)
    except ValueError:
        source_count = None
    return {
        "assertions": int(assertions[0]) if assertions else 0,
        "attempts": int(attempts[0]) if attempts else 0,
        "synced_at": _meta_get(conn, LANDING_PROJECTION_SYNCED_AT_KEY),
        "source_artifacts": source_count,
    }


# ── Query helpers ────────────────────────────────────────────────────────


def has_review_approve_in_substrate(
    conn: AuditConnection,
    slug: str,
    *,
    require_landed: bool = False,
) -> Iterable[dict]:
    """Yield raw_json dicts for matching APPROVE records.

    The caller is responsible for branch-staleness / unmerged-commits
    checks — those rely on git state and are not part of substrate
    semantics.

    ``require_landed`` is *the* landed query (#2849), so it filters on the
    projected landing assertion for the run rather than on
    ``audit_records.landing_status``. The difference is the point: the flattened
    column is written at story completion, before a queued pull request
    resolves, so a run that landed an hour later reads as not landed there and
    as landed here — and a run nobody has observed yields no row at all, which
    is "unresolved", not "did not land".
    """
    sql = (
        "SELECT a.raw_json, a.record_schema_version FROM audit_records a "
        "JOIN reviews r ON r.run_id = a.run_id "
        "WHERE a.slug = ? AND r.verdict = 'APPROVE'"
    )
    params: tuple = (slug,)
    if require_landed:
        sql += " AND EXISTS (SELECT 1 FROM landing_assertions la WHERE la.run_id = a.run_id)"
    for row in conn.execute(sql, params):
        if isinstance(row, sqlite3.Row):
            raw, ver = row["raw_json"], row["record_schema_version"]
        else:
            raw, ver = row[0], row[1]
        record = _load_migrated(raw, ver)
        if record is not None:
            yield record


def latest_run_outcome_in_substrate(
    conn: AuditConnection,
    slug: str,
) -> dict | None:
    """Return the most recent run's recorded outcome fields for ``slug``.

    Yields the three record-level dimensions merge-evidence resolution needs to
    tell "this story landed" from "this story's last run ended badly":
    ``outcome_success`` (1/0/None), ``verdict`` (run-level final review verdict)
    and ``landing_status``. Returns ``None`` when no record exists for the slug.

    Ordering is by ``started_at`` descending with ``run_id`` as a deterministic
    tiebreak, so records written within the same timestamp resolve stably.

    This is a **flattened-column reader** and stays one (#2849). It returns
    ``landing_status`` exactly as ``audit_records`` stores it, even where the
    landing projection disagrees because the work landed after the record was
    written. Re-pointing this reader and the merge-evidence resolution
    downstream of it is sequenced as its own follow-on; until then the two
    answers differing is information, not a bug. For the evidence-backed answer
    use :func:`landing_states` / :func:`landing_state_for_run`.
    """
    row = conn.execute(
        "SELECT outcome_success, verdict, landing_status FROM audit_records "
        "WHERE slug = ? ORDER BY started_at DESC, run_id DESC LIMIT 1",
        (slug,),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        outcome, verdict, landing = row["outcome_success"], row["verdict"], row["landing_status"]
    else:
        outcome, verdict, landing = row[0], row[1], row[2]
    return {
        "outcome_success": outcome,
        "verdict": verdict,
        "landing_status": landing,
    }


def iter_records(conn: AuditConnection, *, order_by_started: bool = True) -> Iterable[dict]:
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


def tail_records(conn: AuditConnection, limit: int) -> list[dict]:
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


def count_records(conn: AuditConnection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()
    if row is None:
        return 0
    return int(row[0])


def latest_record_for(
    conn: AuditConnection,
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


_INDEX_TOKEN = object()


def _split_config_path(path: str) -> tuple[object, ...]:
    tokens: list[object] = []
    if not path:
        return ()
    for part in path.split("."):
        if not part:
            return ()
        head = re.match(r"^[^\[]+", part)
        if head is not None:
            tokens.append(head.group(0))
        for match in re.finditer(r"\[(\d+)\]", part):
            tokens.append(_INDEX_TOKEN)
    return tuple(tokens)


def _recorded_path_tokens(entry: dict, *, fallback_key: str) -> tuple[object, ...]:
    raw_tokens = entry.get("path_tokens")
    if isinstance(raw_tokens, list) and raw_tokens:
        tokens: list[object] = []
        for token in raw_tokens:
            if type(token) is int:
                tokens.append(_INDEX_TOKEN)
                continue
            if isinstance(token, str) and token:
                tokens.append(token)
                continue
            return _split_config_path(fallback_key)
        return tuple(tokens)
    return _split_config_path(fallback_key)


def _unwrap_type(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin not in {Union, types.UnionType}:
        return annotation
    raw_args = get_args(annotation)
    args = tuple(arg for arg in raw_args if arg is not type(None))
    if len(args) == 1 and len(args) != len(raw_args):
        return _unwrap_type(args[0])
    return annotation


@lru_cache(maxsize=1)
def _config_type_globals() -> dict[str, Any]:
    types_mod = importlib.import_module("theforge.config.types")
    model_identity_mod = importlib.import_module("theforge.config.model_identity")
    model_duplicates_mod = importlib.import_module("theforge.config.model_duplicates")
    models_mod = importlib.import_module("theforge.config.models")
    globalns = dict(vars(types_mod))
    globalns.update(vars(model_identity_mod))
    globalns.update(vars(model_duplicates_mod))
    globalns.update(vars(models_mod))
    return globalns


@lru_cache(maxsize=None)
def _type_hints(cls: type) -> dict[str, Any]:
    globalns = _config_type_globals()
    return get_type_hints(cls, globalns=globalns, localns=globalns)


@lru_cache(maxsize=1)
def _forge_config_type_hints() -> dict[str, Any]:
    return _type_hints(ForgeConfig)


def _next_annotation(annotation: Any, token: object) -> Any | None:
    annotation = _unwrap_type(annotation)
    if annotation in {Any, object}:
        return annotation
    if token is _INDEX_TOKEN:
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin in {list, tuple, set, frozenset} and args:
            return _unwrap_type(args[0])
        return None
    if isinstance(annotation, str):
        return None
    if hasattr(annotation, "__dataclass_fields__"):
        hints = _type_hints(annotation)
        field_annotation = hints.get(str(token))
        return _unwrap_type(field_annotation) if field_annotation is not None else None
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is dict and len(args) == 2:
        return _unwrap_type(args[1])
    return None


def _config_tokens_are_interpretable(tokens: tuple[object, ...]) -> bool:
    if not tokens:
        return False
    annotation: Any | None = ForgeConfig
    for token in tokens:
        if annotation is ForgeConfig and token is not _INDEX_TOKEN:
            annotation = _forge_config_type_hints().get(str(token))
        else:
            annotation = _next_annotation(annotation, token)
        if annotation is None:
            return False
    return True


def _config_path_is_interpretable(path: str) -> bool:
    return _config_tokens_are_interpretable(_split_config_path(path))


def lookup_recorded_configuration_value(record: dict, key: str) -> dict:
    """Return a recorded config value lookup without consulting local config."""
    forge_version = record.get("forge_version")
    configuration = record.get("configuration")
    if not isinstance(configuration, dict):
        return {"status": "absent", "forge_version": forge_version}
    recorded_values = configuration.get("recorded_values")
    if not isinstance(recorded_values, dict):
        return {"status": "absent", "forge_version": forge_version}
    entries = recorded_values.get("entries")
    if not isinstance(entries, dict):
        return {"status": "absent", "forge_version": forge_version}
    entry = entries.get(key)
    if not isinstance(entry, dict):
        return {"status": "missing", "forge_version": forge_version, "key": key}
    status = (
        "resolved"
        if _config_tokens_are_interpretable(_recorded_path_tokens(entry, fallback_key=key))
        else "uninterpreted"
    )
    return {
        "status": status,
        "forge_version": forge_version,
        "key": key,
        "value": entry.get("value"),
        "source": entry.get("source"),
        "format_version": recorded_values.get("format_version"),
    }


# ── Alias-resolution drift ───────────────────────────────────────────────


def iter_invocation_identities(
    conn: AuditConnection,
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


def alias_resolution_timeline(conn: AuditConnection) -> list[dict]:
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
    conn: AuditConnection,
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
    story of its kind costs. Runs that did not end successfully are excluded
    before the taint gate: an unsuccessful spend observation is a lower bound
    on what the work needed, not a measurement of what the work costs. When
    ``stats`` is provided its ``"excluded_for_taint"`` and
    ``"excluded_for_unsuccessful_outcome"`` keys are incremented by the number
    of rows set aside for those reasons.

    Rows with a null score, a null cost (cost-unknown runs, which are a lower
    bound rather than a measurement), or a non-positive cost are skipped: none
    of them carry a usable spend observation.
    """
    from .trust_status import filter_tainted_records  # noqa: PLC0415

    rows = conn.execute(
        "SELECT complexity_score, total_cost_usd, outcome_success, raw_json FROM audit_records "
        "WHERE complexity_score IS NOT NULL AND total_cost_usd IS NOT NULL"
    ).fetchall()
    candidates: list[dict] = []
    excluded_for_unsuccessful_outcome = 0
    for row in rows:
        if isinstance(row, sqlite3.Row):
            score = row["complexity_score"]
            cost = row["total_cost_usd"]
            outcome_success = row["outcome_success"]
            raw = row["raw_json"]
        else:
            score, cost, outcome_success, raw = row[0], row[1], row[2], row[3]
        try:
            cost_value = float(cost)
        except (TypeError, ValueError):
            continue
        if cost_value <= 0.0:
            continue
        score_value = _coerce_complexity_score(score)
        if score_value is None:
            continue
        if outcome_success != 1:
            excluded_for_unsuccessful_outcome += 1
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
        stats["excluded_for_unsuccessful_outcome"] = (
            int(stats.get("excluded_for_unsuccessful_outcome", 0))
            + excluded_for_unsuccessful_outcome
        )
    out: dict[int, list[float]] = {}
    for entry in admissible:
        out.setdefault(int(entry["complexity_score"]), []).append(float(entry["total_cost_usd"]))
    return out


def derive_observed_cost_cohorts(
    conn: AuditConnection,
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
    conn: AuditConnection,
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


def iter_escalation_records(conn: AuditConnection) -> Iterable[dict]:
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


def iter_readiness_events(conn: AuditConnection, *, kind: str | None = None) -> Iterable[dict]:
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


def iter_shape_verdict_events(
    conn: AuditConnection,
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


def iter_shape_skip_events(
    conn: AuditConnection,
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
    conn: AuditConnection,
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


def iter_inline_remediation_events(
    conn: AuditConnection,
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


def inline_remediation_rollup_by_milestone(conn: AuditConnection) -> dict[str, dict]:
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


# ── Triage proposals (#2228) ─────────────────────────────────────────────


def iter_triage_proposal_events(
    conn: AuditConnection,
    *,
    finding_id: str | None = None,
    triage_run_id: str | None = None,
) -> Iterable[dict]:
    """Yield ``forge triage`` proposal events (parsed from raw_json), oldest first."""
    sql = "SELECT raw_json FROM triage_proposal_events"
    clauses: list[str] = []
    params: list[object] = []
    if finding_id is not None:
        clauses.append("finding_id = ?")
        params.append(finding_id)
    if triage_run_id is not None:
        clauses.append("triage_run_id = ?")
        params.append(triage_run_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY emitted_at ASC, event_id ASC"
    for row in conn.execute(sql, tuple(params)):
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def load_triage_proposal_run(conn: AuditConnection, triage_run_id: str) -> dict | None:
    """Return one recorded triage proposal run summary, or ``None`` when absent."""
    row = conn.execute(
        "SELECT raw_json FROM triage_proposal_runs "
        "WHERE triage_run_id = ? ORDER BY emitted_at DESC, run_row_id DESC LIMIT 1",
        (triage_run_id,),
    ).fetchone()
    if row is None:
        return None
    raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def iter_triage_application_records(
    conn: AuditConnection,
    *,
    triage_run_id: str | None = None,
    status: str | None = None,
) -> Iterable[dict]:
    """Yield ratified/application rows (parsed from raw_json), oldest first."""
    sql = "SELECT raw_json FROM triage_application_records"
    clauses: list[str] = []
    params: list[object] = []
    if triage_run_id is not None:
        clauses.append("triage_run_id = ?")
        params.append(triage_run_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY emitted_at ASC, finding_id ASC"
    for row in conn.execute(sql, tuple(params)):
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["raw_json"]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            yield data


def triage_disposition_history(conn: AuditConnection, finding_id: str) -> list[dict]:
    """Return the disposition rows recorded for ``finding_id``, oldest first.

    This is what a later triage packet carries as history, so it is projected
    from indexed columns rather than from ``raw_json``: a row that failed to
    serialise its payload still counts as "this finding was proposed on before",
    and that is precisely the fact a fresh proposer must not be denied.
    """
    rows = conn.execute(
        "SELECT triage_run_id, disposition, target_milestone, punt_reason_code, "
        "packet_hash, emitted_at FROM triage_proposal_events "
        "WHERE finding_id = ? ORDER BY emitted_at ASC, event_id ASC",
        (finding_id,),
    ).fetchall()
    return [
        {
            "triage_run_id": row[0],
            "disposition": row[1],
            "target_milestone": row[2],
            "punt_reason_code": row[3],
            "packet_hash": row[4],
            "emitted_at": row[5],
        }
        for row in rows
    ]


def triage_proposal_run_spend(conn: AuditConnection) -> list[dict]:
    """Return every recorded triage proposal run with its findings count and spend."""
    rows = conn.execute(
        "SELECT triage_run_id, findings_count, total_cost_usd, cost_provenance, "
        "report_path, emitted_at, raw_json FROM triage_proposal_runs "
        "ORDER BY emitted_at ASC, run_row_id ASC"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            raw = json.loads(row[6])
        except json.JSONDecodeError:
            raw = {}
        review_stage = raw.get("review_stage") if isinstance(raw, dict) else None
        out.append(
            {
                "triage_run_id": row[0],
                "findings_count": int(row[1] or 0),
                "total_cost_usd": row[2],
                "cost_provenance": row[3],
                "report_path": row[4],
                "emitted_at": row[5],
                "review_stage": review_stage,
            }
        )
    return out
