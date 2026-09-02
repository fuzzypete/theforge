"""Sprint audit and summary YAML writers."""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..advisory_conventions import noteworthy_advisory_entries
from ..coordinator.config_snapshot import load_audit_record as load_config_snapshot_record
from ..coordinator.iteration_usage import dev_usage as _dev_usage
from ..coordinator.landing_record import build_landing_record
from ..log_util import _log_line
from .abnormal import accumulate_failure_history, carry_failure_cause
from .budget import budget_overrun_usd, budget_status
from .launch_guard import REASON_RECONCILE_PRIOR_DONE, REASON_STRANDED_WORKTREE
from .manifest import ResolvedSprint, SprintManifest, SprintResult


def _optional_cost(value: object) -> float | None:
    """Round a cost for a sprint record, preserving an unmeasured ``None``.

    A ``None`` cost means the story had at least one phase whose spend the
    transport could not measure. Rounding it to ``0.0`` would record unpriced
    work as free in sprint-audit.yaml and sprint-summary.yaml (#1992).
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 4)
    return None


#: Story-row key → key of the same fact in the per-story audit ``preflight``
#: block (#2346). The row spells the fields flat, matching the surrounding
#: ``preflight*`` fields, so a reader of sprint-summary.yaml can count degraded
#: runs without opening a single per-story audit.yaml; the audit block nests
#: them under shorter names. Both writers and every reader build from this map,
#: so the two spellings cannot drift apart. The coordinator state happens to use
#: the row spelling, which is why the state reader keys off it directly.
#:
#: Rows with no degradation carry ``False`` explicitly: "this run's preflight was
#: founded" is the claim being recorded, and a missing key cannot make it.
PREFLIGHT_DEGRADED_ROW_KEYS: dict[str, str] = {
    "preflight_degraded": "degraded",
    "preflight_degraded_reason": "degraded_reason",
    "preflight_failure_action": "failure_action",
    "preflight_risk_signals": "risk_signals",
}


def _coerce_degraded_field(row_key: str, value: object) -> object:
    """Coerce one degraded-preflight field to a YAML-representable value.

    Story rows are serialized to YAML, so every field that reaches them must be
    a representable scalar. Duck-typed states (and test doubles) hand back an
    arbitrary object for an attribute that was never set; recording that would
    fail the whole summary write rather than the one field.
    """
    if row_key == "preflight_degraded":
        return value is True
    if row_key == "preflight_risk_signals":
        return [str(item) for item in value] if isinstance(value, (list, tuple)) else []
    return value if isinstance(value, str) and value else None


def preflight_degraded_row_fields(state: object) -> dict:
    """Degraded-preflight story-row fields read from a coordinator state."""
    return {
        row_key: _coerce_degraded_field(row_key, getattr(state, row_key, None))
        for row_key in PREFLIGHT_DEGRADED_ROW_KEYS
    }


def preflight_likely_files_row_field(state: object) -> dict:
    """Story-row field naming the file footprint scheduling actually used.

    Instrumentation only (#2610): collision edges are derived from this set, so
    a resume that lost an edge is diagnosable from the run record — which files
    each story claimed — instead of being reconstructed from log lines. None
    means the story made no file claim at all, which is materially different
    from claiming nothing overlapped.
    """
    files = getattr(state, "preflight_likely_files", None)
    if not isinstance(files, (list, tuple)):
        return {"preflight_likely_files": None}
    return {"preflight_likely_files": [str(f) for f in files]}


def preflight_degraded_row_fields_from_row(story_row: object) -> dict:
    """Same fields, re-read from an already-written summary story row.

    Readers go through this rather than indexing the row directly, so a row
    written by an older forge (missing the keys entirely) normalizes to the same
    shape as a current one.
    """
    row = story_row if isinstance(story_row, dict) else {}
    return {
        row_key: _coerce_degraded_field(row_key, row.get(row_key))
        for row_key in PREFLIGHT_DEGRADED_ROW_KEYS
    }


def preflight_degraded_row_fields_from_audit(preflight_block: object) -> dict:
    """Same fields, read from a persisted per-story audit ``preflight`` block."""
    block = preflight_block if isinstance(preflight_block, dict) else {}
    return {
        row_key: _coerce_degraded_field(row_key, block.get(audit_key))
        for row_key, audit_key in PREFLIGHT_DEGRADED_ROW_KEYS.items()
    }


def _state_reported_cost(state: object) -> float | None:
    """Per-story cost from a coordinator state, preserving cost-unknown.

    Reads ``total_cost_measured`` — the None-preserving aggregate — so a story
    with any unmeasured phase is recorded as cost-unknown instead of as the
    measured remainder. Falls back to ``total_cost`` only for duck-typed states
    that predate the measured aggregate.
    """
    if hasattr(state, "total_cost_measured"):
        return _optional_cost(state.total_cost_measured)
    return _optional_cost(getattr(state, "total_cost", None))


# Rounding slack for the sprint-total-vs-story-rows comparison. Both sides are
# reported to four places, so anything at or under a cent is the arithmetic of
# rounding rather than spend nothing accounts for.
_COST_ACCOUNTING_TOLERANCE_USD = 0.01


def build_cost_accounting_discrepancy(
    measured_total_usd: float,
    story_costs: "list[tuple[str | None, float | None]]",
    *,
    declared_non_story_usd: float = 0.0,
) -> dict | None:
    """Spend admitted into the sprint total that no per-story row explains.

    The sprint total and the sum of the per-story rows are meant to be the same
    money counted twice: every path that advances the ledger outside a story's
    own coordinator state also attributes that spend to a slug, precisely so the
    two stay equal. When they do not, some amount is in the total with nothing
    accountable behind it — the shape of #2847, where a landed story was written
    out of its own sprint's record while its $29.20 stayed in the total.

    Returns the discrepancy block, or ``None`` when the rows explain the total.
    Only an *excess* on the total's side is a discrepancy: rows summing higher
    happens legitimately when a resume carries forward stories from earlier
    generations whose spend the current ledger does not hold.

    ``story_costs`` is ``(slug, cost_usd)`` per row, with ``cost_usd`` ``None``
    for a row whose cost was never measured — those slugs are named in the block
    because they are where an unexplained amount most plausibly belongs.

    ``declared_non_story_usd`` is spend the sprint has already accounted for at
    the sprint level because it belongs to no story of the sprint (intake
    remediation on an issue that was never scheduled). It is explained — just
    not by a story row — so it counts on the explained side.
    """
    measured = round(float(measured_total_usd or 0.0), 4)
    non_story = round(float(declared_non_story_usd or 0.0), 4)
    story_total = round(sum(cost for _slug, cost in story_costs if cost is not None), 4)
    explained = round(story_total + non_story, 4)
    unexplained = round(measured - explained, 4)
    if unexplained <= _COST_ACCOUNTING_TOLERANCE_USD:
        return None
    return {
        "sprint_measured_usd": measured,
        "explained_story_usd": story_total,
        "declared_non_story_usd": non_story,
        "unexplained_usd": unexplained,
        "stories_without_measured_cost": sorted(
            slug for slug, cost in story_costs if cost is None and slug
        ),
        "detail": (
            f"${unexplained:.2f} of measured sprint spend has no per-story record; "
            "the sprint total is reported as unavailable rather than as a complete "
            "figure assembled from an incomplete set of stories."
        ),
    }


def _budget_cap_of(result: "SprintResult") -> float:
    """The cap *result* ran under, ``0.0`` when it carries none.

    Read defensively, like every other field these writers project: a record
    built without a cap has nothing to be within or over, which is exactly what
    a zero cap means to ``sprint.budget``.
    """
    raw = getattr(result, "budget_usd", 0.0)
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _budget_spend_against_cap(result: "SprintResult", spend_usd: float | None) -> float:
    """The spend figure a cap comparison is made against for *result*."""
    reported = getattr(result, "budget_spend_against_cap", None)
    if not isinstance(reported, (int, float)):
        raw_total = getattr(result, "total_cost_usd", 0.0)
        reported = float(raw_total) if isinstance(raw_total, (int, float)) else 0.0
    if spend_usd is None:
        return float(reported)
    return max(float(reported), float(spend_usd))


def _budget_status_of(result: "SprintResult", spend_usd: float | None = None) -> str:
    """Where the run stands against its cap: within, over, or unset (#2547)."""
    return budget_status(
        budget_usd=_budget_cap_of(result), spend_usd=_budget_spend_against_cap(result, spend_usd)
    )


def _budget_overrun_of(result: "SprintResult", spend_usd: float | None = None) -> float:
    """How far the run passed its cap, or ``0.0`` when it did not (#2547)."""
    return round(
        budget_overrun_usd(
            budget_usd=_budget_cap_of(result),
            spend_usd=_budget_spend_against_cap(result, spend_usd),
        ),
        4,
    )


def _story_allocation_summary(
    state: object, result: "SprintResult", reported_cost: float | None
) -> dict | None:
    """Return the story's allocation block joined with the sprint ceiling (#2169).

    The coordinator derives the allocation and knows only its own spend; the
    sprint owns the ceiling. Reporting both on the story row is what makes an
    allocation shortfall that occurred *while sprint headroom remained* visible
    as such, instead of as an unexplained escalation. Returns ``None`` for a run
    that carried no allocation (pre-#2169 records, or a run that never reached
    preflight).
    """
    from theforge.coordinator import story_budget as _story_budget

    allocation = getattr(state, "story_allocation", None)
    exhausted = getattr(state, "allocation_exhausted", None)
    block = _story_budget.evaluate_allocation_dict(allocation, _state_reported_cost(state))
    if block is None and not exhausted:
        return None
    block = block or {}
    remaining = round(result.budget_usd - result.total_cost_usd, 4)
    block["reported_cost_usd"] = reported_cost
    block["sprint_budget_usd"] = result.budget_usd
    block["sprint_spent_usd"] = round(result.total_cost_usd, 4)
    block["sprint_remaining_usd"] = remaining
    block["sprint_cost_measured"] = bool(result.cost_complete)
    block["sprint_headroom_remained"] = remaining > 0 if result.cost_complete else None
    if exhausted:
        block["allocation_exhausted"] = exhausted
        block["status"] = "allocation_exhausted"
    return block


def _review_usage(state: object) -> tuple[int, int | None, bool]:
    """Return ``(cycles_spent, cycle_cap, budget_exhausted)`` for a story's review budget.

    Cycles spent counts reviewer cycles plus any cycle VALIDATE opened for a
    coordinator-raised gate or convention finding, so a story terminated with
    both budgets gone is not reported as having spent zero review cycles. The cap
    prefers the adaptive value, which is set even when no reviewer ever ran —
    falling back to reviewer telemetry alone left ``max`` null and ``hit_limit``
    false on exactly that path (#1981).
    """
    telemetry = list(getattr(state, "review_iteration_telemetry", []) or [])
    spent = getattr(state, "review_cycles_spent", None)
    if not isinstance(spent, int):
        spent = len(telemetry)
    exhausted = bool(getattr(state, "review_budget_exhausted", False) is True)
    cap = getattr(state, "adaptive_review_max", None)
    if not isinstance(cap, int) or cap <= 0:
        cap = getattr(telemetry[0], "max_iterations", None) if telemetry else None
    if not isinstance(cap, int):
        cap = None
    return spent, cap, exhausted


def persist_accumulated_story_state(
    sprint_id: str | None,
    sprint_name: str,
    project_root: Path | None,
    stories: list[dict],
) -> None:
    """Persist sprint-level accumulated story state when identity and root are known."""
    if sprint_id and project_root:
        _save_accumulated_stories(sprint_id, sprint_name, project_root, stories)


def persist_accepted_unmeasured_spend(
    sprint_id: str | None,
    sprint_name: str,
    project_root: Path | None,
    records: list[dict],
) -> bool:
    """Persist operator acceptances of unmeasured spend, keeping stories intact.

    Written on its own rather than through the story-state path so a resolution
    an operator made once survives every later run of the sprint without the
    flag being repeated (#2310) — and so the story writer, which knows nothing
    about acceptances, cannot erase one by omission.

    Returns whether the resolution actually reached disk. An acceptance that
    exists only in memory is a decision the operator will have to make again
    without being told, so the caller must report a failure rather than log the
    acceptance as recorded.
    """
    if not sprint_id or not project_root:
        return False
    return _save_accumulated_stories(
        sprint_id,
        sprint_name,
        project_root,
        _load_accumulated_stories(sprint_id, project_root),
        accepted_unmeasured_spend=list(records),
    )


if TYPE_CHECKING:
    from ..config import ForgeConfig
    from ..coordinator.state import CoordinatorResult, CoordinatorState
    from ..task import TaskStory


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


def _upsert_into_substrate(project_root: Path, record: dict) -> None:
    """Best-effort mirror of a sprint/story audit dict into the SQLite substrate.

    Redaction is enforced inside ``upsert_run_record`` (ADR-0002 §1); we
    pass the project's ``.forge/.env`` path so env-defined secrets are
    also scrubbed before the record is indexed.

    Failure is logged but not fatal — the per-run JSON / sprint-audit.yaml
    is canonical and `forge audits rebuild` can recover.
    """
    try:
        from ..coordinator import audit_substrate

        env_file = audit_substrate.secrets_env_path(project_root)
        conn = audit_substrate.create_or_open(project_root)
        try:
            audit_substrate.upsert_run_record(
                conn,
                record,
                provenance="native",
                env_file=env_file if env_file.exists() else None,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        _log(f"warning: failed to update audit substrate: {exc}")


def _carried_story_record(
    entry: dict,
    *,
    sprint_id: str | None,
    sprint_name: str | None,
) -> dict:
    """A minimal story-shaped run record built from an accumulated story entry.

    Only fields the entry already holds are used — nothing about the run is
    inferred. The shape is the subset the substrate indexes on (``task.slug``,
    ``timing``, ``outcome``, ``totals``) plus the landing fields when the entry
    carries them, so the record answers ``forge audits show --slug <slug>`` the
    same way a natively written one does. Optional fields are emitted as their
    null representation rather than omitted, so a consumer reading the record
    never has to distinguish "absent" from "not recorded".
    """
    outcome_name = str(entry.get("outcome") or "").upper() or None
    cost = entry.get("cost_usd")
    cost = None if isinstance(cost, bool) or not isinstance(cost, (int, float)) else float(cost)
    issue = entry.get("github_issue")
    if issue is None:
        path = str(entry.get("path") or "")
        if path.startswith("Issue #") and path[7:].strip().isdigit():
            issue = int(path[7:].strip())
    record: dict = {
        "run_id": entry.get("story_run_id"),
        "sprint_id": sprint_id,
        "sprint_name": sprint_name,
        "task": {
            "slug": entry.get("slug"),
            "path": entry.get("path"),
            "github_issue": issue,
        },
        "timing": {
            "started_at": entry.get("started_at"),
            "finished_at": entry.get("finished_at"),
        },
        "outcome": {
            "final_phase": outcome_name,
            "success": outcome_name in ("DONE", "ALREADY_DONE"),
            "cost_usd": cost,
            "error_type": entry.get("error_type"),
            "message": entry.get("error"),
        },
        "totals": {"cost_usd": cost},
        "reviews": [],
        # Named so a reader can tell this record was reconstructed from the
        # sprint's accumulated state rather than flushed by the run itself.
        "carried_from_accumulated_state": True,
    }
    for field_name in _LANDING_CLAIM_FIELDS:
        if field_name in entry:
            record[field_name] = entry[field_name]
    return record


def _ensure_carried_story_records(
    project_root: Path,
    entries: "list[dict]",
    *,
    sprint_id: str | None,
    sprint_name: str | None,
    sprint_run_id: str | None,
) -> None:
    """Give every carried story row a run record of its own, if it lacks one.

    A story whose work happened in an earlier generation of the same sprint has
    its spend counted in this sprint's total, so it must also be individually
    addressable — otherwise the total is composed of an amount no queryable
    record explains (#2847).

    Deliberately conservative, and idempotent:

    * a row with no ``slug`` or no ``story_run_id`` has no identity to write a
      record under, so none is invented; the cost-accounting discrepancy block
      is what reports it;
    * a row whose ``story_run_id`` is the *sprint's* run id is skipped — that id
      belongs to the sprint-level record, and writing a story record over it
      would replace the sprint's own account of itself;
    * a row that already has a canonical run record is left exactly as written.

    Best-effort throughout: the sprint summary is canonical and
    ``forge audits rebuild`` can recover, so a failure here never disturbs the
    run it is only recording.
    """
    try:
        from ..coordinator import audit_substrate  # noqa: PLC0415

        runs_dir = audit_substrate.runs_dir(project_root)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            slug = entry.get("slug")
            story_run_id = entry.get("story_run_id")
            if not isinstance(slug, str) or not slug:
                continue
            if not isinstance(story_run_id, str) or not story_run_id:
                continue
            if sprint_run_id and story_run_id == sprint_run_id:
                continue
            run_file = runs_dir / f"{story_run_id}.json"
            if _read_canonical_run_file(project_root, run_file) is not None:
                continue
            _write_native_story_record(
                project_root,
                _carried_story_record(entry, sprint_id=sprint_id, sprint_name=sprint_name),
            )
            _log(f"Carried story record written for {slug} (run {story_run_id})")
    except Exception as exc:  # noqa: BLE001 — recording must never break the run
        _log(f"warning: failed to write carried story records: {exc}")


def _replace_canonical_run_file(run_file: Path, record: dict) -> None:
    """Atomically replace a canonical per-run JSON file.

    The temporary file is written *outside* the canonical runs tree (#2598).
    That tree is re-included by forge's generated ``.gitignore`` precisely so it
    is tracked, which made a write-in-progress ``<run>.tmp`` sitting next to the
    record indistinguishable from project memory: it dirtied the shared checkout
    for as long as the write took — enough to refuse a sibling story's landing
    — and the publication transport would have carried it into the corpus.
    ``.forge/audits/.tmp`` is denied by the same ``.forge/**`` rule and
    re-included by nothing, so nothing transient is ever visible to git.

    Same filesystem as the destination, so ``replace`` is still atomic.
    """
    tmp_dir = run_file.parent.parent / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{run_file.stem}.json.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(record, f, default=str, indent=2)
    tmp_path.replace(run_file)


# Fields of a canonical run record that describe the *world's response* to the
# run rather than the run itself (#2598). Everything else in the record is a
# property of what the run did, and is settled the moment the run finishes.
_LANDING_CLAIM_FIELDS = ("landing_status", "landing", "landing_event", "merge")


def _read_json_record(path: Path) -> dict | None:
    """A record file's mapping, or ``None`` when there is nothing usable there.

    Absent, unreadable and not-a-mapping collapse to the same answer on purpose:
    every caller's response to all three is to write the record it has.
    """
    try:
        with open(path, encoding="utf-8") as f:
            persisted = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return persisted if isinstance(persisted, dict) else None


def _read_canonical_run_file(project_root: Path, run_file: Path) -> dict | None:
    """The record already written for this run, wherever it currently lives.

    Not just the canonical tree. The publication transport *drains* that tree to
    publish it (#2598), so between a publish and the memory pull request merging
    the record sits in ``.forge/memory-staging`` and the canonical path is empty.
    A reader that looked only at the canonical path would conclude the record had
    never been written and recreate it from whatever the current in-memory result
    says — which is exactly how a landing outcome would slip back into a record
    the drain had already published.
    """
    from ..coordinator import audit_substrate  # noqa: PLC0415
    from ..coordinator.landing_evidence import (  # noqa: PLC0415
        PROJECT_MEMORY_STAGING_RELPATH,
    )

    persisted = _read_json_record(run_file)
    if persisted is not None:
        return persisted
    staged = project_root.joinpath(*PROJECT_MEMORY_STAGING_RELPATH).joinpath(
        *audit_substrate.RUNS_RELPATH, run_file.name
    )
    return _read_json_record(staged)


# The scheduler's word for "a landing was owed and has not resolved". It is what
# a run record says about landing, because a record never speaks to how a landing
# resolved unless it resolved successfully.
_LANDING_OWED = "pending_integration"


def _without_negative_landing_claim(incoming: dict, existing: dict | None) -> dict:
    """Keep a *negative* landing claim out of a canonical run record.

    A run record is an attestation of what a run did. A landing that fails, is
    refused, or times out changes nothing about the run — only the world's
    response to it — so a record saying ``landing_status: failed`` is an
    attestation carrying something that is not about its subject. The spike for
    #2598 makes that explicit: a landing that does not happen produces an
    *attempt artifact* under ``.forge/audits/landing``, which is where the error,
    the carrier and the outcome live.

    The rule is uniform across first writes and rewrites, and that matters. A
    story whose landing fails on its first attempt has no earlier record to
    preserve, and guarding only rewrites would let exactly that story's record be
    *created* carrying the negative — the same claim, arrived at by a different
    route. So a resolved-negative landing is either replaced by the record the
    run already had, or demoted to "a landing was owed", which is all the record
    ever knew.

    A landing that *did* happen is allowed through untouched. That asymmetry is
    the whole model: the record may be advanced to a fact, never turned into a
    denial. The positive assertion is the durable evidence either way; letting
    the flattened field carry ``landed`` keeps it usable for the readers that
    have not moved to the evidence artifacts yet, without ever letting it carry a
    claim those artifacts would refuse to make.

    Rewrites also happen for reasons unrelated to landing — the knowledge summary
    is folded in after the record is first written — so this rewrites the landing
    fields rather than skipping the write.
    """
    if str(incoming.get("landing_status") or "") != "failed":
        return incoming
    safe = dict(incoming)
    if existing is not None:
        for field in _LANDING_CLAIM_FIELDS:
            if field in existing:
                safe[field] = existing[field]
            else:
                safe.pop(field, None)
        return safe
    safe["landing_status"] = _LANDING_OWED
    for field in ("landing", "landing_event", "merge"):
        if field in safe:
            safe[field] = None
    return safe


def _write_native_story_record(
    project_root: Path,
    audit_data: dict,
    *,
    force_replace: bool = False,
) -> None:
    """Write the canonical per-story run JSON and mirror it into the substrate.

    A record never carries a negative landing claim, whether it is being created
    or replaced — see :func:`_without_negative_landing_claim`.
    """
    run_id = audit_data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        _upsert_into_substrate(project_root, audit_data)
        return

    try:
        from ..coordinator import audit_substrate
        from ..coordinator.redact import redact

        # ``parent_run_id`` is preserved from the audit when it carries one: a
        # record written for a story whose work happened in an earlier generation
        # names that generation as its parent, and flattening it to null severs
        # the only link back to the run that did the work (#2214).
        parent_run_id = audit_data.get("parent_run_id")
        if not isinstance(parent_run_id, str) or not parent_run_id:
            parent_run_id = None
        record = {
            "schema_version": audit_substrate.CURRENT_RECORD_SCHEMA_VERSION,
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "forge_version": audit_data.get("forge_version"),
        }
        record.update(audit_data)
        record["schema_version"] = audit_substrate.CURRENT_RECORD_SCHEMA_VERSION
        record["run_id"] = run_id
        record["parent_run_id"] = parent_run_id
        record["forge_version"] = audit_data.get("forge_version")

        env_file = audit_substrate.secrets_env_path(project_root)
        env_file_arg = env_file if env_file.exists() else None
        redacted = redact(record, env_file_arg)

        runs_dir = audit_substrate.runs_dir(project_root)
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_file = runs_dir / f"{run_id}.json"
        existing = _read_canonical_run_file(project_root, run_file)
        if existing is None or force_replace:
            # Absent, unreadable, not a mapping, or a deliberate replacement.
            redacted = _without_negative_landing_claim(redacted, existing)
            _replace_canonical_run_file(run_file, redacted)
            persisted = redacted
        else:
            persisted = existing

        stat = run_file.stat()
        conn = audit_substrate.create_or_open(project_root)
        try:
            audit_substrate.upsert_run_record(
                conn,
                persisted,
                provenance="native",
                source_path=str(run_file.relative_to(project_root)),
                source_mtime=stat.st_mtime,
                env_file=env_file_arg,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        _log(f"warning: failed to write native story audit record: {exc}")


def _build_advisory_summary(config: ForgeConfig | None) -> dict | None:
    """Return a sprint-summary section for current advisory convention debt."""
    if config is None:
        return None
    entries = noteworthy_advisory_entries(config)
    if not entries:
        return {
            "noteworthy_threshold_percent": (
                config.conventions_advisory.noteworthy_threshold_percent
            ),
            "top_n": config.conventions_advisory.summary_top_n,
            "entries": [],
        }
    return {
        "noteworthy_threshold_percent": config.conventions_advisory.noteworthy_threshold_percent,
        "top_n": config.conventions_advisory.summary_top_n,
        "entries": [
            {
                "rule": entry["rule"],
                "file": entry["file"],
                "line_count": entry["line_count"],
                "limit": entry["limit"],
                "gap": entry["gap"],
                "last_seen": entry["last_seen"],
                "first_seen": entry.get("first_seen"),
            }
            for entry in entries
        ],
    }


# ── Sprint-level stable identity ──────────────────────────────────────────────


def _get_or_create_sprint_id(sprint_name: str, project_root: Path) -> str:
    """Return stable sprint_id for this logical sprint, creating it on first call.

    Stored at .forge/logs/<sprint-name>/.sprint_id so it persists across
    worker restarts, detach/attach, and --resume invocations.
    """
    sprint_log_dir = project_root / ".forge" / "logs" / sprint_name
    sprint_id_path = sprint_log_dir / ".sprint_id"
    try:
        sprint_log_dir.mkdir(parents=True, exist_ok=True)
        if sprint_id_path.exists():
            return sprint_id_path.read_text(encoding="utf-8").strip()
        from ..coordinator.util import _generate_run_id  # noqa: PLC0415

        new_id = _generate_run_id()
        sprint_id_path.write_text(new_id, encoding="utf-8")
        return new_id
    except OSError:
        from ..coordinator.util import _generate_run_id  # noqa: PLC0415

        return _generate_run_id()


def _load_accumulated_stories(sprint_id: str, project_root: Path) -> list[dict]:
    """Load per-story data from .forge/sprints/<sprint_id>/state.yaml.

    Returns the stories list, or [] if the file does not exist or cannot be read.
    Each entry includes a ``canonical_ref`` key used for cross-run matching.
    """
    state_path = project_root / ".forge" / "sprints" / sprint_id / "state.yaml"
    if not state_path.exists():
        return []
    try:
        with open(state_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("stories", [])
    except Exception:
        return []


def _load_accepted_unmeasured_spend(sprint_id: str, project_root: Path) -> list[dict]:
    """Load operator acceptances of unmeasured spend for a sprint.

    Persisted alongside the accumulated stories because that is where the
    carried unmeasured sources come from: one read gives both what is unpriced
    and what has already been resolved about it (#2310).
    """
    state_path = project_root / ".forge" / "sprints" / sprint_id / "state.yaml"
    if not state_path.exists():
        return []
    try:
        with open(state_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        records = data.get("accepted_unmeasured_spend") or []
        return [r for r in records if isinstance(r, dict)]
    except Exception:
        return []


def _save_accumulated_stories(
    sprint_id: str,
    sprint_name: str,
    project_root: Path,
    stories: list[dict],
    accepted_unmeasured_spend: list[dict] | None = None,
) -> bool:
    """Save story entries to .forge/sprints/<sprint_id>/state.yaml.

    Each entry should have a ``canonical_ref`` field for cross-run matching.
    Writes atomically via a temp file. Returns whether the write landed —
    failure stays non-fatal for the story path, which can rebuild its state, but
    a caller persisting something that only exists here has to be able to tell.

    ``accepted_unmeasured_spend`` of ``None`` carries the persisted acceptances
    forward unchanged: a caller that has nothing to say about them must not
    silently erase a resolution the operator made.
    """
    state_dir = project_root / ".forge" / "sprints" / sprint_id
    if accepted_unmeasured_spend is None:
        accepted_unmeasured_spend = _load_accepted_unmeasured_spend(sprint_id, project_root)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "state.yaml"
        tmp_path = state_path.with_suffix(".tmp")
        data: dict = {
            "sprint_id": sprint_id,
            "sprint_name": sprint_name,
            "stories": stories,
            "accepted_unmeasured_spend": list(accepted_unmeasured_spend),
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        tmp_path.replace(state_path)
        return True
    except Exception:
        return False


def _load_story_summary_entry_from_audit(
    sprint_log_dir: Path,
    canonical_ref: str,
    slug: str,
) -> dict | None:
    """Return a sprint-summary story entry derived from per-story audit.yaml."""
    audit_path = sprint_log_dir / slug / "audit.yaml"
    if not audit_path.exists():
        return None

    try:
        with open(audit_path, encoding="utf-8") as f:
            audit_data = yaml.safe_load(f) or {}
    except Exception:
        return None

    if not isinstance(audit_data, dict):
        return None

    outcome_block = audit_data.get("outcome")
    timing_block = audit_data.get("timing")
    preflight_block = audit_data.get("preflight")
    cost_block = audit_data.get("cost")
    iteration_block = audit_data.get("iterations")

    if not isinstance(outcome_block, dict):
        return None

    final_phase = outcome_block.get("final_phase")
    if not isinstance(final_phase, str) or not final_phase:
        return None
    if isinstance(preflight_block, dict) and preflight_block.get("verdict") == "ALREADY_DONE":
        final_phase = "ALREADY_DONE"

    display_key = (
        f"Issue #{canonical_ref.split(':')[1]}"
        if canonical_ref.startswith("issue:")
        else canonical_ref
    )

    reviews = audit_data.get("reviews")
    verdict = None
    # Provenance of the verdict reported below: the commit that verdict judged
    # and that commit's verification state. Carried alongside the verdict so a
    # summary cannot present a superseded verdict as a current one (#2052).
    verdict_commit: str | None = None
    verdict_verification_state: str | None = None
    if isinstance(reviews, list) and reviews:
        last_review = reviews[-1]
        if isinstance(last_review, dict):
            raw_verdict = last_review.get("verdict")
            if isinstance(raw_verdict, str) and raw_verdict:
                verdict = raw_verdict
            raw_commit = last_review.get("commit")
            if isinstance(raw_commit, str) and raw_commit:
                verdict_commit = raw_commit
            verification = last_review.get("verification")
            raw_state = verification.get("state") if isinstance(verification, dict) else None
            verdict_verification_state = (
                raw_state if isinstance(raw_state, str) and raw_state else "unknown"
            )
    if verdict is None and outcome_block.get("success") is True:
        verdict = "APPROVE"

    usage_summary = (
        iteration_block.get("usage_summary") if isinstance(iteration_block, dict) else {}
    )
    if not isinstance(usage_summary, dict):
        usage_summary = {}

    dev_usage = usage_summary.get("dev") if isinstance(usage_summary.get("dev"), dict) else {}
    review_usage = (
        usage_summary.get("review") if isinstance(usage_summary.get("review"), dict) else {}
    )

    preflight_verdict = (
        preflight_block.get("verdict") if isinstance(preflight_block, dict) else None
    )

    # Tag the source of an ALREADY_DONE outcome so downstream renderers can
    # distinguish a preflight short-circuit verdict from a resume-skip-merged
    # classification — the two paths have materially different trust
    # properties for operators (preflight verdict is the historically suspect
    # path; resume-skip-merged is mechanical and trustworthy).
    outcome_source: str | None = None
    if final_phase == "ALREADY_DONE" and preflight_verdict == "ALREADY_DONE":
        outcome_source = "preflight_verdict"

    return {
        "canonical_ref": canonical_ref,
        "path": display_key,
        "slug": slug,
        "outcome": final_phase,
        "outcome_source": outcome_source,
        "verdict": verdict,
        "verdict_commit": verdict_commit,
        "verdict_verification_state": verdict_verification_state,
        "cost_usd": _optional_cost(cost_block.get("total_usd"))
        if isinstance(cost_block, dict)
        else 0.0,
        "story_run_id": audit_data.get("run_id"),
        "preflight": preflight_verdict,
        "preflight_reason": (
            preflight_block.get("reason") if isinstance(preflight_block, dict) else None
        ),
        "preflight_original_verdict": (
            preflight_block.get("original_verdict") if isinstance(preflight_block, dict) else None
        ),
        "preflight_source_run_id": (
            preflight_block.get("source_run_id") if isinstance(preflight_block, dict) else None
        ),
        **preflight_degraded_row_fields_from_audit(preflight_block),
        "error": audit_data.get("error"),
        "error_type": audit_data.get("error_type") or outcome_block.get("error_type"),
        "outcome_code": (
            audit_data.get("error_type") or outcome_block.get("error_type") or final_phase.lower()
        ),
        "merge": bool((audit_data.get("merge") or {}).get("merged", False)),
        "landing": audit_data.get("landing") or build_landing_record(audit_data.get("merge")),
        "iteration_usage": {
            "dev": {
                "used": dev_usage.get("used", 0),
                "max": dev_usage.get("max"),
                "hit_limit": bool(dev_usage.get("hit_limit", False)),
                "early_finish": bool(dev_usage.get("early_finish", False)),
            },
            "review": {
                "used": review_usage.get("used", 0),
                "max": review_usage.get("max"),
                "hit_limit": bool(review_usage.get("hit_limit", False)),
                "early_finish": bool(review_usage.get("early_finish", False)),
            },
        },
        "started_at": timing_block.get("started_at") if isinstance(timing_block, dict) else None,
        "finished_at": timing_block.get("finished_at") if isinstance(timing_block, dict) else None,
    }


def _preflight_fallback(state: object) -> str:
    """Rendering for a run whose state carries no preflight verdict.

    ``PROCEED`` is the historical default and stays correct for runs that
    genuinely proceeded without recording one. A run whose preflight produced no
    model output records no verdict on purpose (#1951) — report that explicitly
    instead of inventing the decision the phase would have made.
    """
    from ..coordinator.agent_failure import NO_JUDGMENT  # noqa: PLC0415

    failure = getattr(state, "infrastructure_failure", None)
    if isinstance(failure, dict) and failure.get("phase") == "PREFLIGHT":
        return NO_JUDGMENT
    return "PROCEED"


def _parse_summary_timestamp(value: object) -> datetime.datetime | None:
    """Parse sprint-summary timestamps of the form 2026-04-25T12:05:00Z."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _story_outcome_rank(entry: dict) -> int:
    """Higher rank wins when timing information is unavailable or tied."""
    outcome = entry.get("outcome")
    if outcome == "DONE":
        return 5
    if outcome == "ALREADY_DONE":
        return 4
    if outcome in {"SKIPPED", "PRESERVED", "DROPPED"}:
        return 3
    if outcome == "ESCALATE":
        return 1
    return 2


def _select_historical_story_entry(prior: dict | None, audit_entry: dict | None) -> dict | None:
    """Choose the best historical story entry between accumulated state and audit.yaml."""
    if prior is None:
        return audit_entry
    if audit_entry is None:
        return prior

    prior_finished = _parse_summary_timestamp(prior.get("finished_at"))
    audit_finished = _parse_summary_timestamp(audit_entry.get("finished_at"))
    if (
        prior_finished is not None
        and audit_finished is not None
        and prior_finished != audit_finished
    ):
        return audit_entry if audit_finished > prior_finished else prior
    if prior_finished is not None and audit_finished is None:
        return prior
    if audit_finished is not None and prior_finished is None:
        return (
            audit_entry if _story_outcome_rank(audit_entry) > _story_outcome_rank(prior) else prior
        )

    if _story_outcome_rank(audit_entry) > _story_outcome_rank(prior):
        return audit_entry
    return prior


def _write_sprint_audit(
    manifest: SprintManifest | ResolvedSprint,
    result: SprintResult,
    canonical_refs: list[str],
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    duration: float,
    project_root: Path,
    story_times: "dict[str, tuple[datetime.datetime, datetime.datetime]] | None" = None,
    batch_assignments: "dict[str, int] | None" = None,
    slug_map: "dict[str, str] | None" = None,
    tasks_by_slug: "dict[str, TaskStory] | None" = None,
    ci_break_slug: str | None = None,
    sprint_id: str | None = None,
    dropped_slugs: "dict[str, str] | None" = None,
    skipped_issues: "list | None" = None,
    current_story_entries_by_ref: "dict[str, dict] | None" = None,
    triage_actions_by_ref: "dict[str, str] | None" = None,
    run_id: str | None = None,
    live_telemetry_snapshots: "dict[str, dict] | None" = None,
    story_state: "object | None" = None,
) -> None:
    """Write sprint-audit.yaml to the project root.

    ``story_state`` is the canonical ``SprintStoryState`` when the caller has
    one. It is read only for the cost-accounting cross-check: the canonical
    per-story costs are the post-attribution figures (they include cross-phase
    spend such as intake remediation), so comparing the sprint total against
    them is the comparison that does not fire on ordinary sprints.
    """
    story_times = story_times or {}
    batch_assignments = batch_assignments or {}
    slug_map = slug_map or {}
    tasks_by_slug = tasks_by_slug or {}
    dropped_slugs = dropped_slugs or {}
    skipped_issues = skipped_issues or []
    current_story_entries_by_ref = current_story_entries_by_ref or {}
    triage_actions_by_ref = triage_actions_by_ref or {}
    live_telemetry_snapshots = live_telemetry_snapshots or {}

    # Build per-spec entries
    spec_entries = []
    # The slug behind each emitted row, positionally aligned with
    # ``spec_entries``. The rows themselves carry a display path rather than a
    # slug, and the cost-accounting cross-check below has to name the stories it
    # is accounting for (#2847).
    spec_slugs: list[str | None] = []
    results_by_spec = {spec_str: res for spec_str, res in result.results}

    for canonical_ref in canonical_refs:
        display_key = (
            f"Issue #{canonical_ref.split(':')[1]}"
            if canonical_ref.startswith("issue:")
            else canonical_ref
        )
        slug = slug_map.get(canonical_ref, Path(canonical_ref).stem)
        task = tasks_by_slug.get(slug)
        if canonical_ref in results_by_spec:
            res = results_by_spec[canonical_ref]
            preflight = (
                "cached"
                if getattr(res.state, "preflight_cached", False)
                # An unset verdict is not a PROCEED. A run whose preflight
                # obtained no model output recorded no verdict at all (#1951);
                # defaulting it to PROCEED reports a decision no agent made.
                else (res.state.preflight_verdict or _preflight_fallback(res.state))
            )
            outcome = "ALREADY_DONE" if preflight == "ALREADY_DONE" else res.phase.name

            # Build reviews summary for this spec
            reviews_summary = []
            for i, meta in enumerate(res.state.review_cycle_metadata):
                cycle_entry: dict = {
                    "cycle": i + 1,
                    # Reviewed-commit provenance (#2052). A cycle summary that
                    # carries the verdict but drops the commit and its gate
                    # state reintroduces the ambiguity at the sprint level.
                    "commit": meta.reviewed_commit,
                    "verification_state": (
                        meta.verification.state if meta.verification is not None else "unknown"
                    ),
                    "gate_decision": (
                        meta.verification.gate_decision if meta.verification is not None else None
                    ),
                    "pool": meta.pool_models,
                    "successful": meta.successful,
                    "failed": meta.failed,
                    "synthesized": meta.synthesized,
                    "parse_retries": meta.parse_retries,
                }
                if i < len(res.state.review_results):
                    r = res.state.review_results[i]
                    cycle_entry["verdict"] = r.verdict
                    cycle_entry["p1_count"] = sum(1 for f in r.findings if f.severity == "P1")
                    cycle_entry["p2_count"] = sum(1 for f in r.findings if f.severity == "P2")
                reviews_summary.append(cycle_entry)

            dev_used, dev_max = _dev_usage(res.state)
            review_used, review_max, review_exhausted = _review_usage(res.state)
            # Tag the source of an ALREADY_DONE outcome so audit / postmortem
            # consumers can distinguish a preflight short-circuit verdict from
            # the resume-skip-merged classification without parsing strings.
            outcome_source: str | None = None
            if outcome == "ALREADY_DONE" and preflight == "ALREADY_DONE":
                outcome_source = "preflight_verdict"
            entry: dict = {
                "path": display_key,
                "outcome": outcome,
                "outcome_source": outcome_source,
                "cost_usd": _state_reported_cost(res.state),
                "preflight": preflight,
                "preflight_reason": getattr(res.state, "preflight_reason", None),
                "preflight_original_verdict": getattr(
                    res.state, "preflight_cached_original_verdict", None
                ),
                "preflight_source_run_id": getattr(
                    res.state, "preflight_cached_from_run_id", None
                ),
                **preflight_degraded_row_fields(res.state),
                **preflight_likely_files_row_field(res.state),
                "error": res.state.error,
                "error_type": res.state.error_type,
                "outcome_code": res.state.error_type or outcome.lower(),
                "merge": res.merge is not None and res.merge.get("merged", False),
                "landing": build_landing_record(res.merge),
                "iteration_usage": {
                    "dev": {
                        "used": dev_used,
                        "max": dev_max,
                        "hit_limit": (dev_used >= dev_max)
                        if dev_max is not None and dev_used > 0
                        else False,
                        "early_finish": (0 < dev_used < dev_max) if dev_max is not None else False,
                    },
                    "review": {
                        "used": review_used,
                        "max": review_max,
                        "hit_limit": review_exhausted
                        or (
                            (review_used >= review_max)
                            if review_max is not None and review_used > 0
                            else False
                        ),
                        "early_finish": (
                            (not review_exhausted) and (0 < review_used < review_max)
                            if review_max is not None
                            else False
                        ),
                    },
                },
                "reviews": reviews_summary,
                "depends_on": task.depends_on if task else [],
                "dependency_warnings": task.dependency_warnings if task else [],
                "inferred_dependencies": {
                    "manifest": [
                        dep
                        for dep in (task.depends_on if task else [])
                        if dep not in (task.inferred_dependencies if task else [])
                    ],
                    "github_blockers": task.inferred_dependencies if task else [],
                },
            }
            if slug in story_times:
                entry["started_at"] = story_times[slug][0].strftime("%Y-%m-%dT%H:%M:%SZ")
                entry["finished_at"] = story_times[slug][1].strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                entry["started_at"] = None
                entry["finished_at"] = None
            entry["batch"] = batch_assignments.get(slug, 0)
            snapshot = live_telemetry_snapshots.get(slug)
            if snapshot:
                last_cost = snapshot.get("last_cost")
                if (
                    entry.get("cost_usd") is not None
                    and not entry.get("cost_usd")
                    and isinstance(last_cost, (int, float))
                    and last_cost > 0
                ):
                    # Only fills in a genuine zero. A cost-unknown (None) entry
                    # stays unknown — a live snapshot subtotal is not the
                    # story's total (#1992).
                    entry["cost_usd"] = round(float(last_cost), 4)
                last_phase_val = snapshot.get("last_phase")
                if last_phase_val:
                    entry["last_phase"] = last_phase_val
        elif canonical_ref in current_story_entries_by_ref:
            entry = dict(current_story_entries_by_ref[canonical_ref])
        else:
            # Dropped by launch guard (active-worktree collision, lock held,
            # or preserved-escalated) takes precedence over the generic
            # SKIPPED path — operators need to see drop reasons explicitly.
            drop_reason = dropped_slugs.get(slug)
            triage_action = triage_actions_by_ref.get(canonical_ref)
            if drop_reason == "preserved-escalated":
                drop_outcome = "PRESERVED"
            elif drop_reason == REASON_RECONCILE_PRIOR_DONE:
                drop_outcome = "ALREADY_DONE"
            elif drop_reason == REASON_STRANDED_WORKTREE:
                drop_outcome = "DROPPED"
            elif drop_reason is not None:
                drop_outcome = "DROPPED"
            elif triage_action == "skip_merged":
                drop_outcome = "ALREADY_DONE"
            else:
                drop_outcome = "SKIPPED"
            entry = {
                "path": display_key,
                "outcome": drop_outcome,
                "outcome_source": (
                    "resume_skip_merged" if triage_action == "skip_merged" else None
                ),
                "cost_usd": 0.0,
                "preflight": None,
                "error": drop_reason,
                "error_type": "dropped" if drop_reason else None,
                "merge": False,
                "reviews": [],
                "depends_on": task.depends_on if task else [],
                "dependency_warnings": task.dependency_warnings if task else [],
                "inferred_dependencies": {
                    "manifest": [
                        dep
                        for dep in (task.depends_on if task else [])
                        if dep not in (task.inferred_dependencies if task else [])
                    ],
                    "github_blockers": task.inferred_dependencies if task else [],
                },
                "started_at": None,
                "finished_at": None,
                "batch": batch_assignments.get(slug, 0),
            }
            if drop_reason:
                entry["drop_reason"] = drop_reason
        spec_entries.append(entry)
        spec_slugs.append(slug)

    # A story the sprint holds in its canonical state but whose ref this
    # process no longer resolves — the story that landed just before a re-exec
    # and left the re-exec's issue query — still has its spend inside the
    # sprint total. It is written into ``specs:`` here so the total is never a
    # figure assembled from a set of stories that omits one of its own
    # contributors (#2847). This mirrors the projection the sprint summary
    # already performs; the two files must account for the same stories.
    if story_state is not None and hasattr(story_state, "stories"):
        _emitted = {s for s in spec_slugs if s}
        for canonical_entry in story_state.stories():
            if canonical_entry.slug in _emitted:
                continue
            spec_entries.append(
                {
                    "path": canonical_entry.path,
                    "slug": canonical_entry.slug,
                    "outcome": canonical_entry.outcome.name,
                    "outcome_source": "carried_from_accumulated_state",
                    "cost_usd": canonical_entry.cost_usd,
                    "preflight": None,
                    "error": canonical_entry.reason,
                    "error_type": None,
                    "outcome_code": canonical_entry.outcome.name.lower(),
                    "merge": False,
                    "reviews": [],
                    "depends_on": list(canonical_entry.depends_on),
                    "started_at": None,
                    "finished_at": None,
                    "batch": 0,
                }
            )
            spec_slugs.append(canonical_entry.slug)

    usage_distribution = []
    for spec_str, res in result.results:
        dev_used, dev_max = _dev_usage(res.state)
        review_used, review_max, review_exhausted = _review_usage(res.state)
        usage_distribution.append(
            {
                "spec": spec_str,
                "slug": slug_map.get(spec_str, Path(spec_str).stem),
                "dev": {"used": dev_used, "max": dev_max},
                "review": {"used": review_used, "max": review_max},
            }
        )

    # Spend the sprint total admits that no per-story row explains (#2847).
    # Skipped when the total is already declared incomplete: an unmeasured-spend
    # sprint has stated that its figure is a lower bound, and restating it as a
    # discrepancy would report the same gap twice.
    _cost_complete = bool(getattr(result, "cost_complete", True))
    _cost_discrepancy: dict | None = None
    if _cost_complete:
        # Only the rows this file actually publishes count as explanation. A
        # story accounted for in canonical state but absent from ``specs:``
        # explains nothing to a reader of this audit — which is why the
        # projection above puts it in the rows first, and why the check reads
        # the rows rather than the state (#2847).
        #
        # Canonical cost wins where the state holds one: those are the
        # post-attribution figures (they include cross-phase spend such as
        # intake remediation) and the sprint total is built from the same
        # figures, so comparing against them is the comparison that does not
        # fire on ordinary sprints.
        _canonical_cost_by_slug: dict[str, float | None] = {}
        if story_state is not None and hasattr(story_state, "stories"):
            _canonical_cost_by_slug = {
                e.slug: getattr(e, "cost_usd", None) for e in story_state.stories() if e.slug
            }
        _explained_costs = [
            (slug, _canonical_cost_by_slug.get(slug, entry.get("cost_usd")))
            for slug, entry in zip(spec_slugs, spec_entries)
        ]
        _cost_discrepancy = build_cost_accounting_discrepancy(
            getattr(result, "total_cost_usd", 0.0) or 0.0,
            _explained_costs,
            declared_non_story_usd=getattr(result, "non_story_spend_usd", 0.0) or 0.0,
        )
        if _cost_discrepancy is not None:
            _cost_complete = False

    audit = {
        "sprint": {
            "name": manifest.name,
            "budget_usd": manifest.budget_usd,
            "max_parallel": manifest.max_parallel,
            "sprint_id": sprint_id,
            # ``None`` when any story's cost was unmeasured: a sprint total over
            # partially unpriced work is a different statement from a complete
            # one and must not render as a confident figure (#1992). The measured
            # lower bound stays available under ``total_cost_measured_usd``.
            "total_cost_usd": (round(result.total_cost_usd, 4) if _cost_complete else None),
            "total_cost_measured_usd": round(result.total_cost_usd, 4),
            "cost_complete": _cost_complete,
            # Present only when measured spend exceeds what the per-story rows
            # account for. A total is never composed of amounts no record
            # explains: where one cannot be produced, the gap is reported here
            # rather than absorbed into a confident figure (#2847).
            "cost_accounting_discrepancy": _cost_discrepancy,
            # Which work is unpriced, not merely that some is — the budget check
            # refuses on this list, so it must be traceable (#1992).
            "unmeasured_spend_sources": list(
                getattr(result, "unmeasured_spend_sources", ()) or []
            ),
            # Of those, the ones no operator has resolved — the list the budget
            # guard actually refuses on — beside the acceptances standing in for
            # the rest and the figure the cap was verified against (#2310).
            # Kept distinct from ``unmeasured_spend_sources`` so an acceptance is
            # never mistaken for a measurement.
            "unresolved_unmeasured_spend_sources": list(
                getattr(result, "unresolved_unmeasured_spend_sources", ()) or []
            ),
            "accepted_unmeasured_spend": [
                dict(r) for r in (getattr(result, "accepted_unmeasured_spend", ()) or [])
            ],
            "budget_verification_spend_usd": round(
                float(getattr(result, "budget_verification_spend_usd", 0.0) or 0.0), 4
            ),
            # Where the run finished relative to its cap, stated rather than left
            # to be inferred from two numbers in the same block. A run can land
            # over the cap legitimately — enforcement stops it at the first
            # checkpoint past the limit, and the phase already running when that
            # happened still finishes — so "over" is reported, not suppressed
            # (#2547).
            "budget_status": _budget_status_of(result),
            "budget_overrun_usd": _budget_overrun_of(result),
            "budget_note": "Costs reflect Claude invocations only; Codex/Gemini report $0.00",
            "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": round(duration, 1),
            "specs_total": result.specs_total,
            "specs_succeeded": result.specs_succeeded,
            "specs_failed": result.specs_failed,
            "specs_skipped": result.specs_skipped,
            "stopped_reason": result.stopped_reason,
            "ci_break_slug": ci_break_slug,
        },
        "baseline_check": (
            getattr(manifest, "baseline_gate", None)
            if isinstance(getattr(manifest, "baseline_gate", None), dict)
            else None
        ),
        # Which forge.yaml this sprint ran under, and every point at which the
        # project-root file moved off it (#1980). Without this, two issues filed
        # from one sprint can describe its configuration contradictorily and
        # both be right about the runs their authors read.
        "config_snapshot": load_config_snapshot_record(project_root, sprint_id),
        # TODO(issue-817): Distinguishing externally closed dependencies from
        # earlier-resume completion would require finer provenance on ResolvedSprint.
        "closed_dependency_slugs": [
            {"slug": slug, "source": "remote_closed"}
            for slug in sorted(getattr(manifest, "closed_dependency_slugs", set()))
        ],
        "specs": spec_entries,
        "skipped": [s.as_dict() if hasattr(s, "as_dict") else dict(s) for s in skipped_issues],
        "iteration_usage_distribution": usage_distribution,
    }

    # Shape-gate skip classification (issue #1453): project this run's skip
    # events into a category-grouped block with stuck-issue flags so operators
    # read gate friction from audit output rather than reconstructing it from
    # log files. Best-effort — omitted when this run recorded no skip events.
    from ..shape_check.skip_taxonomy import DEFAULT_STUCK_ISSUE_THRESHOLD
    from .skip_report import build_shape_gate_skip_block

    skip_block = build_shape_gate_skip_block(
        project_root, run_id, threshold=DEFAULT_STUCK_ISSUE_THRESHOLD
    )
    if skip_block is not None:
        audit["shape_gate_skips"] = skip_block

    audits_dir = project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audits_dir / "sprint-audit.yaml"
    with open(audit_path, "w", encoding="utf-8") as f:
        yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    # Run-id-keyed canonical copy: the name-keyed file at sprint-audit.yaml
    # is overwritten every sprint, so historical audits would otherwise be
    # lost. Keep the legacy file as a "latest" pointer for convenience and
    # back-compat; treat the per-run file as the durable record.
    if run_id:
        per_run_audit_path = audits_dir / f"run-{run_id}-sprint-audit.yaml"
        with open(per_run_audit_path, "w", encoding="utf-8") as f:
            yaml.dump(audit, f, default_flow_style=False, sort_keys=False)
    _upsert_into_substrate(project_root, audit)
    _log(f"Audit written: {audit_path}")


def _write_sprint_summary(
    manifest: SprintManifest | ResolvedSprint,
    result: SprintResult,
    canonical_refs: list[str],
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
    duration: float,
    sprint_log_dir: Path,
    story_times: "dict[str, tuple[datetime.datetime, datetime.datetime]] | None" = None,
    batch_assignments: "dict[str, int] | None" = None,
    slug_map: "dict[str, str] | None" = None,
    run_id: str | None = None,
    tasks_by_slug: "dict[str, TaskStory] | None" = None,
    ci_break_slug: str | None = None,
    sprint_id: str | None = None,
    project_root: Path | None = None,
    dropped_slugs: "dict[str, str] | None" = None,
    skipped_issues: "list | None" = None,
    triage_actions_by_ref: "dict[str, str] | None" = None,
    current_story_entries_by_ref: "dict[str, dict] | None" = None,
    story_state: "object | None" = None,
    config: "ForgeConfig | None" = None,
    live_telemetry_snapshots: "dict[str, dict] | None" = None,
) -> None:
    """Write sprint-summary.yaml to <project_root>/.forge/logs/<sprint-name>/.

    When sprint_id and project_root are provided, prior story entries from
    .forge/sprints/<sprint_id>/state.yaml are merged in for stories that did
    not run in this invocation (e.g., completed under an earlier run_id).
    This ensures the summary reflects the full logical sprint across all
    worker-process boundaries.
    """
    story_times = story_times or {}
    batch_assignments = batch_assignments or {}
    slug_map = slug_map or {}
    tasks_by_slug = tasks_by_slug or {}
    dropped_slugs = dropped_slugs or {}
    skipped_issues = skipped_issues or []
    triage_actions_by_ref = triage_actions_by_ref or {}
    current_story_entries_by_ref = current_story_entries_by_ref or {}
    live_telemetry_snapshots = live_telemetry_snapshots or {}

    # Load prior accumulated story entries from the sprint-level state file.
    # Keyed by canonical_ref so we can substitute them for stories not in
    # this invocation's results (e.g., stories completed under an earlier run_id).
    prior_by_ref: dict[str, dict] = {}
    prior_stories: list[dict] = []
    if sprint_id and project_root:
        prior_stories = _load_accumulated_stories(sprint_id, project_root)
        prior_by_ref = {s["canonical_ref"]: s for s in prior_stories if "canonical_ref" in s}

    spec_entries = []
    # Tracks story entries with canonical_ref for saving to accumulated state.
    accumulated_for_state: list[dict] = []
    results_by_spec = {spec_str: res for spec_str, res in result.results}

    def _accumulated_entry(canonical_ref: str, entry: dict) -> dict:
        """This generation's entry, with every attempt's failure cause retained.

        The end-of-sprint write is the last one to touch
        ``.forge/sprints/<id>/state.yaml``, and it replaced the prior
        generation's entry wholesale — so resuming a sprint destroyed the record
        of why the attempt being resumed from had failed (#2030). The rest of the
        entry is still this generation's; only the failure history accumulates.
        """
        merged = {"canonical_ref": canonical_ref, **entry}
        history = accumulate_failure_history(prior_by_ref.get(canonical_ref), merged)
        if history:
            merged["failure_history"] = history
        return merged

    seen_refs: set[str] = set()
    for canonical_ref in canonical_refs:
        seen_refs.add(canonical_ref)
        display_key = (
            f"Issue #{canonical_ref.split(':')[1]}"
            if canonical_ref.startswith("issue:")
            else canonical_ref
        )
        slug = slug_map.get(canonical_ref, Path(canonical_ref).stem)
        if canonical_ref in results_by_spec:
            res = results_by_spec[canonical_ref]
            preflight = (
                "cached"
                if getattr(res.state, "preflight_cached", False)
                # An unset verdict is not a PROCEED. A run whose preflight
                # obtained no model output recorded no verdict at all (#1951);
                # defaulting it to PROCEED reports a decision no agent made.
                else (res.state.preflight_verdict or _preflight_fallback(res.state))
            )
            outcome = "ALREADY_DONE" if preflight == "ALREADY_DONE" else res.phase.name
            last_verdict = ""
            if res.state.review_results:
                last_verdict = res.state.review_results[-1].verdict
            elif res.success:
                last_verdict = "APPROVE"
            dev_used, dev_max = _dev_usage(res.state)
            review_used, review_max, review_exhausted = _review_usage(res.state)
            _dev_results_list = list(getattr(res.state, "dev_results", []) or [])
            _dev_model: str | None = None
            if _dev_results_list:
                _last_dev = _dev_results_list[-1]
                _model_used = getattr(_last_dev, "model_used", None)
                if isinstance(_model_used, str) and _model_used:
                    _dev_model = _model_used
            # Tag the source of an ALREADY_DONE outcome so renderers can
            # distinguish a preflight short-circuit verdict from the
            # resume-skip-merged classification — different trust properties
            # (see status_reader._already_done_detail).
            outcome_source: str | None = None
            if outcome == "ALREADY_DONE" and preflight == "ALREADY_DONE":
                outcome_source = "preflight_verdict"
            _snapshot = live_telemetry_snapshots.get(slug)
            _entry_cost = _state_reported_cost(res.state)
            _last_phase_val: str | None = None
            if _snapshot:
                _snap_cost = _snapshot.get("last_cost")
                if _entry_cost == 0 and isinstance(_snap_cost, (int, float)) and _snap_cost > 0:
                    _entry_cost = round(float(_snap_cost), 4)
                if _dev_model is None:
                    _snap_model = _snapshot.get("last_model")
                    if isinstance(_snap_model, str) and _snap_model:
                        _dev_model = _snap_model
                _snap_phase = _snapshot.get("last_phase")
                if isinstance(_snap_phase, str) and _snap_phase:
                    _last_phase_val = _snap_phase
            entry: dict = {
                "path": display_key,
                "slug": slug,
                "outcome": outcome,
                "outcome_source": outcome_source,
                "verdict": last_verdict or None,
                "cost_usd": _entry_cost,
                "dev_model": _dev_model,
                "story_run_id": getattr(res.state, "run_id", None) or run_id,
                "preflight": preflight,
                "preflight_reason": getattr(res.state, "preflight_reason", None),
                "preflight_original_verdict": getattr(
                    res.state, "preflight_cached_original_verdict", None
                ),
                "preflight_source_run_id": getattr(
                    res.state, "preflight_cached_from_run_id", None
                ),
                **preflight_degraded_row_fields(res.state),
                **preflight_likely_files_row_field(res.state),
                "error": res.state.error,
                "error_type": res.state.error_type,
                "outcome_code": res.state.error_type or outcome.lower(),
                "merge": res.merge is not None and res.merge.get("merged", False),
                "landing": build_landing_record(res.merge),
                "iteration_usage": {
                    "dev": {
                        "used": dev_used,
                        "max": dev_max,
                        "hit_limit": (dev_used >= dev_max)
                        if dev_max is not None and dev_used > 0
                        else False,
                        "early_finish": (0 < dev_used < dev_max) if dev_max is not None else False,
                    },
                    "review": {
                        "used": review_used,
                        "max": review_max,
                        "hit_limit": review_exhausted
                        or (
                            (review_used >= review_max)
                            if review_max is not None and review_used > 0
                            else False
                        ),
                        "early_finish": (
                            (not review_exhausted) and (0 < review_used < review_max)
                            if review_max is not None
                            else False
                        ),
                    },
                },
            }
            # Per-story allocation joined against the sprint ceiling (#2169), so
            # a shortfall that happened while sprint headroom remained reads as
            # one fact on one row.
            _allocation_entry = _story_allocation_summary(res.state, result, _entry_cost)
            if _allocation_entry is not None:
                entry["story_allocation"] = _allocation_entry
            if slug in story_times:
                entry["started_at"] = story_times[slug][0].strftime("%Y-%m-%dT%H:%M:%SZ")
                entry["finished_at"] = story_times[slug][1].strftime("%Y-%m-%dT%H:%M:%SZ")
            # A run that ended abnormally carries its cause on the state. Stamp
            # it onto the row here rather than leaving it to be re-derived from
            # the ``error`` prose, so the kind, run id, and observing code path
            # survive into the accumulated state a later generation rewrites.
            carry_failure_cause(
                entry,
                getattr(res.state, "abnormal_termination", None),
                prior_history=(prior_by_ref.get(canonical_ref) or {}).get("failure_history"),
            )
            entry["batch"] = batch_assignments.get(slug, 0)
            entry["depends_on"] = list(getattr(tasks_by_slug.get(slug), "depends_on", None) or [])
            if _last_phase_val and outcome != "DONE":
                entry["last_phase"] = _last_phase_val
            spec_entries.append(entry)
            accumulated_for_state.append(_accumulated_entry(canonical_ref, entry))
        elif canonical_ref in current_story_entries_by_ref:
            current_entry = dict(current_story_entries_by_ref[canonical_ref])
            spec_entries.append(current_entry)
            accumulated_for_state.append(_accumulated_entry(canonical_ref, current_entry))
        elif canonical_ref in prior_by_ref:
            # Story ran under an earlier run_id — use its accumulated data instead
            # of emitting a SKIPPED entry (which would hide a completed story).
            historical_entry = _select_historical_story_entry(
                prior_by_ref[canonical_ref],
                _load_story_summary_entry_from_audit(sprint_log_dir, canonical_ref, slug),
            )
            if historical_entry is None:
                historical_entry = prior_by_ref[canonical_ref]
            entry = {k: v for k, v in historical_entry.items() if k != "canonical_ref"}
            spec_entries.append(entry)
            accumulated_for_state.append(_accumulated_entry(canonical_ref, entry))
        else:
            drop_reason = dropped_slugs.get(slug)
            triage_action = triage_actions_by_ref.get(canonical_ref)
            if drop_reason == "preserved-escalated":
                drop_outcome = "PRESERVED"
            elif drop_reason == REASON_RECONCILE_PRIOR_DONE:
                drop_outcome = "ALREADY_DONE"
            elif drop_reason == REASON_STRANDED_WORKTREE:
                drop_outcome = "DROPPED"
            elif drop_reason is not None:
                drop_outcome = "DROPPED"
            elif triage_action == "skip_merged":
                drop_outcome = "ALREADY_DONE"
            else:
                drop_outcome = "SKIPPED"
            entry = {
                "path": display_key,
                "slug": slug,
                "outcome": drop_outcome,
                "outcome_source": (
                    "resume_skip_merged" if triage_action == "skip_merged" else None
                ),
                "verdict": None,
                "cost_usd": 0.0,
                "dev_model": None,
                "preflight": None,
                "error": drop_reason,
                "error_type": "dropped" if drop_reason else None,
                "merge": False,
                "batch": batch_assignments.get(slug, 0),
                "depends_on": list(getattr(tasks_by_slug.get(slug), "depends_on", None) or []),
            }
            if drop_reason:
                entry["drop_reason"] = drop_reason
            spec_entries.append(entry)

    # Carry forward stories completed in earlier resumes whose canonical_ref
    # is absent from this resume's manifest (e.g., issues that closed between
    # resumes and were dropped at manifest re-resolution). Without this, the
    # final resume's summary would silently omit stories that already landed
    # under earlier run_ids, and totals would reflect only the last resume's
    # working set instead of the full sprint lifespan. See issue #958.
    for canonical_ref, prior in prior_by_ref.items():
        if canonical_ref in seen_refs:
            continue
        entry = {k: v for k, v in prior.items() if k != "canonical_ref"}
        spec_entries.append(entry)
        accumulated_for_state.append(prior)

    # Persistence is deferred until after canonical projection so the
    # accumulated state on disk reflects per-story costs that include
    # cross-phase spend attributed by the runner (e.g. intake remediation).
    # Persisting before projection saves the stale CoordinatorState-only
    # cost and a later --resume reloads the wrong total.

    usage_distribution = []
    for spec_str, res in result.results:
        dev_used, dev_max = _dev_usage(res.state)
        review_used, review_max, review_exhausted = _review_usage(res.state)
        usage_distribution.append(
            {
                "spec": spec_str,
                "slug": slug_map.get(spec_str, Path(spec_str).stem),
                "dev": {"used": dev_used, "max": dev_max},
                "review": {"used": review_used, "max": review_max},
            }
        )

    # Project totals from the canonical SprintStoryState when supplied — by
    # construction these counts equal the banner counts in the same run. If
    # no canonical state was passed (legacy callers), fall back to recomputing
    # from spec_entries; the canonical path is the single source of truth.
    if story_state is not None and hasattr(story_state, "counts"):
        # First, propagate canonical outcomes AND cost_usd to per-story rows
        # so that terminal-to-terminal corrections (e.g., DONE→FAILED for a
        # queued PR that did not land) and cross-phase spend attribution
        # (e.g. intake remediation) appear in the summary rows AND aggregate
        # counts AND the persisted accumulated state. The summary stories
        # list, persisted state, and summary totals must come from the same
        # source — this loop ensures all three project from story_state.
        accumulated_by_slug = {e.get("slug"): e for e in accumulated_for_state if e.get("slug")}
        for entry in spec_entries:
            slug = entry.get("slug")
            if not slug:
                continue
            canonical_entry = story_state.get(slug)
            if canonical_entry is None:
                continue
            entry["outcome"] = canonical_entry.outcome.name
            outcome_lower = canonical_entry.outcome.name.lower()
            entry["outcome_code"] = entry.get("error_type") or outcome_lower
            entry["cost_usd"] = canonical_entry.cost_usd
            # The canonical outcome arrives with the sentence the sprint recorded
            # when it set it. Projecting the outcome without that sentence leaves
            # a row saying SKIPPED and nothing else, which every downstream
            # reader has to explain from something other than the run's own
            # words: an approved-then-dependency-skipped story reached the
            # operator as unclassifiable work needing a paid investigation
            # (#2373). Only filled when the row carries no error of its own — a
            # cause the story itself recorded is never overwritten.
            if (
                canonical_entry.outcome.is_skipped
                and canonical_entry.reason
                and not entry.get("error")
            ):
                entry["error"] = canonical_entry.reason
                if accumulated_by_slug.get(slug) is not None:
                    accumulated_by_slug[slug]["error"] = canonical_entry.reason
            accumulated = accumulated_by_slug.get(slug)
            if accumulated is not None:
                accumulated["cost_usd"] = canonical_entry.cost_usd
        canonical_counts = story_state.counts()
        effective_specs_total = canonical_counts["total"]
        effective_succeeded = canonical_counts["succeeded"]
        effective_failed = canonical_counts["failed"]
        effective_skipped = canonical_counts["skipped"]
        _canonical_costs = [getattr(e, "cost_usd", 0.0) for e in story_state.stories()]
        effective_cost_complete = all(c is not None for c in _canonical_costs)
        effective_cost_usd = round(sum(c for c in _canonical_costs if c is not None), 4)
        # Inject any shape-gate-skipped stories (and other canonical-only
        # entries that aren't in canonical_refs) so the summary surfaces them.
        canonical_slugs_in_entries = {e.get("slug") for e in spec_entries if e.get("slug")}
        for entry in story_state.stories():
            if entry.slug in canonical_slugs_in_entries:
                continue
            outcome_name = entry.outcome.name
            spec_entries.append(
                {
                    "path": entry.path,
                    "slug": entry.slug,
                    "outcome": outcome_name,
                    "verdict": None,
                    "cost_usd": entry.cost_usd,
                    "preflight": None,
                    "error": entry.reason,
                    "error_type": None,
                    "merge": False,
                    "batch": 0,
                    "depends_on": list(entry.depends_on),
                    "drop_reason": entry.reason,
                    "detail": dict(entry.detail) if entry.detail else None,
                }
            )
    else:
        effective_specs_total = len(spec_entries)
        _entry_costs = [e.get("cost_usd", 0.0) for e in spec_entries]
        effective_cost_complete = all(c is not None for c in _entry_costs)
        effective_cost_usd = round(sum(c for c in _entry_costs if c is not None), 4)
        effective_succeeded = sum(1 for e in spec_entries if e.get("outcome") == "DONE")
        effective_failed = sum(
            1
            for e in spec_entries
            if e.get("outcome") not in ("DONE", "ALREADY_DONE", "SKIPPED", "PRESERVED", None)
        )
        effective_skipped = sum(
            1
            for e in spec_entries
            if e.get("outcome") in ("ALREADY_DONE", "SKIPPED", "PRESERVED", None)
        )

    # Persist accumulated state so future runs can find stories from this
    # invocation. Persistence runs after canonical projection so cost_usd
    # values reflect cross-phase spend (e.g. intake remediation) attributed
    # by the runner; otherwise --resume would reload stale per-story totals
    # and sprint-summary.yaml would silently drop the prior intake spend.
    persist_accumulated_story_state(
        sprint_id,
        manifest.name,
        project_root,
        accumulated_for_state,
    )

    # Intake remediation spends the sprint budget outside any story's entry, so
    # an unmeasured intake pass makes the sprint total incomplete even when every
    # per-story cost is known (#1992).
    _unmeasured_sources = list(getattr(result, "unmeasured_spend_sources", ()) or [])
    if _unmeasured_sources:
        effective_cost_complete = False

    # The same cross-check the audit writer runs, against the rows this file
    # actually publishes (#2847). The ledger's measured total is the figure the
    # sprint spent; ``stories:`` is the account of where it went. When the first
    # exceeds the second, the difference is spend with no addressable record and
    # the total is withheld rather than rendered as complete.
    _cost_discrepancy = None
    if effective_cost_complete:
        _cost_discrepancy = build_cost_accounting_discrepancy(
            getattr(result, "total_cost_usd", 0.0) or 0.0,
            [(e.get("slug"), e.get("cost_usd")) for e in spec_entries],
            declared_non_story_usd=getattr(result, "non_story_spend_usd", 0.0) or 0.0,
        )
        if _cost_discrepancy is not None:
            effective_cost_complete = False

    # Every row whose spend the total admits must be individually addressable
    # (#2847). A story carried in from an earlier generation has no run record
    # of its own in this process, so one is synthesised from the record the
    # sprint already holds — never invented where the identity fields are
    # missing, which the discrepancy block above reports instead.
    if project_root is not None:
        _ensure_carried_story_records(
            project_root,
            spec_entries,
            sprint_id=sprint_id,
            sprint_name=manifest.name,
            sprint_run_id=run_id,
        )

    summary = {
        "sprint": {
            "name": manifest.name,
            "budget_usd": manifest.budget_usd,
            "max_parallel": manifest.max_parallel,
            "run_id": run_id,
            "sprint_id": sprint_id,
            "run_log": f"run-{run_id}.log" if run_id else None,
            # Null when any story's cost is unknown; the measured lower bound is
            # reported separately so an incomplete total is never mistaken for a
            # complete one (#1992).
            "total_cost_usd": effective_cost_usd if effective_cost_complete else None,
            "total_cost_measured_usd": effective_cost_usd,
            "cost_complete": effective_cost_complete,
            # See the audit writer: measured spend the per-story rows do not
            # account for, reported rather than absorbed (#2847).
            "cost_accounting_discrepancy": _cost_discrepancy,
            "unmeasured_spend_sources": _unmeasured_sources,
            # See the audit writer: which unmeasured sources are still
            # unresolved, which were accepted with a recorded ceiling and
            # origin, and the figure the cap was verified against (#2310).
            "unresolved_unmeasured_spend_sources": list(
                getattr(result, "unresolved_unmeasured_spend_sources", ()) or []
            ),
            "accepted_unmeasured_spend": [
                dict(r) for r in (getattr(result, "accepted_unmeasured_spend", ()) or [])
            ],
            "budget_verification_spend_usd": round(
                float(getattr(result, "budget_verification_spend_usd", 0.0) or 0.0), 4
            ),
            # See the audit writer: the run's standing against its cap, reported
            # beside the cost it is a statement about (#2547). Measured against
            # the summary's own effective cost, which can exceed the result's
            # when per-story attribution recovered spend the ledger did not hold.
            "budget_status": _budget_status_of(result, spend_usd=effective_cost_usd),
            "budget_overrun_usd": _budget_overrun_of(result, spend_usd=effective_cost_usd),
            "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_seconds": round(duration, 1),
            "specs_total": effective_specs_total,
            "specs_succeeded": effective_succeeded,
            "specs_failed": effective_failed,
            "specs_skipped": effective_skipped,
            "stopped_reason": result.stopped_reason,
            "ci_break_slug": ci_break_slug,
        },
        "advisory_convention_violations": _build_advisory_summary(config),
        "stories": spec_entries,
        "skipped": [s.as_dict() if hasattr(s, "as_dict") else dict(s) for s in skipped_issues],
        "iteration_usage_distribution": usage_distribution,
    }

    # Shape-gate skip classification (issue #1453). The postmortem digest and
    # sprint RCA read this block from the summary, so the stuck-issue threshold
    # honours the operator's ``shape_check.stuck_issue_threshold`` config here
    # (the audit-YAML copy uses the default). Best-effort — omitted when this
    # run recorded no skip events.
    from ..shape_check.skip_taxonomy import DEFAULT_STUCK_ISSUE_THRESHOLD
    from .skip_report import build_shape_gate_skip_block

    _threshold = DEFAULT_STUCK_ISSUE_THRESHOLD
    _shape_cfg = getattr(config, "shape_check", None)
    if _shape_cfg is not None:
        _threshold = getattr(_shape_cfg, "stuck_issue_threshold", DEFAULT_STUCK_ISSUE_THRESHOLD)
    if project_root is not None:
        skip_block = build_shape_gate_skip_block(project_root, run_id, threshold=_threshold)
        if skip_block is not None:
            summary["shape_gate_skips"] = skip_block

    try:
        sprint_log_dir.mkdir(parents=True, exist_ok=True)
        summary_path = sprint_log_dir / "sprint-summary.yaml"
        with open(summary_path, "w", encoding="utf-8") as f:
            yaml.dump(summary, f, default_flow_style=False, sort_keys=False)
        # Run-id-keyed canonical copy. The legacy name-keyed path is
        # overwritten by every later run sharing the sprint name, so it
        # cannot be the durable per-run record (issue #1480). The legacy
        # file remains as a convenience "latest run" pointer.
        if run_id:
            per_run_summary_path = sprint_log_dir / f"run-{run_id}-summary.yaml"
            with open(per_run_summary_path, "w", encoding="utf-8") as f:
                yaml.dump(summary, f, default_flow_style=False, sort_keys=False)
            _log(f"Sprint summary written: {summary_path} (and {per_run_summary_path.name})")
        else:
            _log(f"Sprint summary written: {summary_path}")
    except Exception as exc:
        _log(f"Warning: sprint summary write failed: {exc}")


def _write_story_audit(
    config: "ForgeConfig",
    task: "TaskStory",
    result: "CoordinatorResult",
    sprint_id: str | None = None,
    telemetry_snapshot: dict | None = None,
    overwrite_story_audit: bool = True,
    prior_generation: "PriorGeneration | None" = None,
) -> None:
    """Write per-story audit.yaml to the durable log directory and preserve ESCALATE worktrees.

    ``overwrite_story_audit=False`` refuses to replace an existing
    ``audit.yaml``: a story dropped at launch shares its log directory with the
    generation that actually ran it, and a synthetic drop record must never
    overwrite a real run's evidence. The drop record is written beside it
    instead.

    ``prior_generation`` is that earlier generation's flushed audit, when this
    record is being written for a story whose work happened before the
    boundary. Its accounting is folded into the record rather than replaced by
    the synthetic post-boundary state (#2214) — see
    :func:`carry_prior_generation_work`.

    Best-effort: silently ignores missing workspace or log dir.
    """
    from ..artifacts import AUDIT_PATH, ESCALATED_MARKER_PATH, ensure_parent_dir  # noqa: PLC0415
    from ..coordinator import audit as coordinator_audit  # noqa: PLC0415

    try:
        audit_data = coordinator_audit.generate_audit_log(config, task, result)
    except Exception as exc:
        _log(f"Warning: failed to generate story audit log for {task.slug}: {exc}")
        return

    if prior_generation:
        carry_prior_generation_work(audit_data, prior_generation)

    if telemetry_snapshot:
        last_phase = telemetry_snapshot.get("last_phase")
        last_model = telemetry_snapshot.get("last_model")
        last_cost = telemetry_snapshot.get("last_cost")
        if last_phase:
            audit_data["last_phase"] = last_phase
        if last_model:
            audit_data["last_model"] = last_model
        if isinstance(last_cost, (int, float)) and last_cost > 0:
            outcome_block = audit_data.get("outcome")
            if isinstance(outcome_block, dict):
                if not outcome_block.get("cost_usd"):
                    outcome_block["cost_usd"] = round(float(last_cost), 4)

    if sprint_id is not None:
        audit_data["sprint_id"] = sprint_id
        sprint_name = result.state.log_dir.parent.name if result.state.log_dir else None
        if not sprint_name:
            sprint_name = audit_data.get("sprint_name")
        if not sprint_name:
            sprint_name = "Parallel Sprint"
        audit_data["sprint_name"] = sprint_name

    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)
    final_phase = result.phase.name
    if workspace_path.exists() and final_phase == "ESCALATE":
        audit_path = workspace_path / AUDIT_PATH
        ensure_parent_dir(audit_path)
        with open(audit_path, "w", encoding="utf-8") as f:
            yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
        marker_path = workspace_path / ESCALATED_MARKER_PATH
        ensure_parent_dir(marker_path)
        timestamp = audit_data.get("ended_at") or audit_data.get("started_at") or ""
        marker_path.write_text(
            f"slug: {task.slug}\nfinal_phase: {final_phase}\ntimestamp: {timestamp}\n",
            encoding="utf-8",
        )
        _log(f"Per-story audit written: {audit_path}")

    audits_dir = config.project_root / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    # Post-DONE knowledge summary (#1859). A sprint calls this writer more than
    # once for the same finished story (pending integration, landing, wrap-up);
    # generation is guarded on the artifact's own existence, so the story is
    # summarised once. Never raises.
    from ..coordinator.knowledge_summary_flow import (  # noqa: PLC0415
        maybe_generate_run_summary,
    )

    _write_native_story_record(config.project_root, audit_data)
    maybe_generate_run_summary(config, result, audit_data)
    _write_native_story_record(config.project_root, audit_data, force_replace=True)

    log_dir = result.state.log_dir
    if log_dir is None:
        sprint_name = audit_data.get("sprint_name")
        if not isinstance(sprint_name, str) or not sprint_name:
            sprint_name = None
        if isinstance(sprint_name, str) and sprint_name:
            log_dir = config.project_root / ".forge" / "logs" / sprint_name / task.slug
    if log_dir is not None:
        try:
            _story_audit_path = log_dir / "audit.yaml"
            if not overwrite_story_audit and _story_audit_path.exists():
                _run_id = audit_data.get("run_id")
                _suffix = _run_id if isinstance(_run_id, str) and _run_id else "unknown-run"
                _story_audit_path = log_dir / f"audit-abnormal-{_suffix}.yaml"
            _story_audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_story_audit_path, "w", encoding="utf-8") as f:
                yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
        except Exception:
            pass  # best-effort


# ── In-flight story audits ────────────────────────────────────────────
#
# A story's audit.yaml is normally written once, from the CoordinatorResult its
# worker returns. A story that never returns one — killed by ``forge stop``,
# which runs in a *different* process and cannot see the worker's in-memory
# state — therefore had no audit at all, or a stale one from a prior generation:
# the dev iterations and gate decisions the run log shows really happened were
# unrecoverable after the fact (#2013).
#
# So the sprint process flushes the story's audit as it goes, marked
# ``in_flight: true``, and the stop path stamps that same file terminal. The
# marker is what keeps the two apart: only an in-flight audit may be finalized
# by an outside process, so a completed story's real audit is never overwritten.

AUDIT_IN_FLIGHT_KEY = "in_flight"


def _story_audit_path(project_root: Path, sprint_name: str, slug: str) -> Path:
    return project_root / ".forge" / "logs" / sprint_name / slug / "audit.yaml"


def _dump_audit_atomically(path: Path, audit_data: dict) -> None:
    """Write an audit mapping so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(audit_data, f, default_flow_style=False, sort_keys=False)
        tmp_path.replace(path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def write_live_story_audit(
    config: "ForgeConfig",
    task: "TaskStory",
    state: "CoordinatorState",
    *,
    sprint_id: str | None = None,
    sprint_name: str | None = None,
) -> Path | None:
    """Flush a still-running story's audit to its log dir; return the path written.

    Deliberately narrower than :func:`_write_story_audit`: it writes only the
    per-story ``audit.yaml``, never the substrate record or the ESCALATE marker.
    Those describe a finished story, and this one is not finished — the whole
    point is that it may never be.

    Best-effort: returns ``None`` on any failure rather than disturbing the run
    it is only observing.
    """
    from ..coordinator import audit as coordinator_audit  # noqa: PLC0415
    from ..coordinator.state import CoordinatorResult  # noqa: PLC0415

    try:
        in_flight_result = CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=f"story in flight (phase {state.phase.name})",
        )
        audit_data = coordinator_audit.generate_audit_log(config, task, in_flight_result)
        audit_data[AUDIT_IN_FLIGHT_KEY] = True
        if sprint_id is not None:
            audit_data["sprint_id"] = sprint_id
        resolved_sprint_name = sprint_name or state.sprint_name
        if resolved_sprint_name:
            audit_data["sprint_name"] = resolved_sprint_name
        log_dir = state.log_dir
        if log_dir is None:
            if not resolved_sprint_name:
                return None
            log_dir = _story_audit_path(
                config.project_root, resolved_sprint_name, task.slug
            ).parent
        path = log_dir / "audit.yaml"
        _dump_audit_atomically(path, audit_data)
        return path
    except Exception:  # noqa: BLE001 — an observer must never break the run
        return None


def finalize_interrupted_story_audit(
    project_root: Path,
    sprint_name: str,
    slug: str,
    *,
    reason: str = "stopped",
) -> Path | None:
    """Stamp an in-flight story audit terminal; return the path, or None if skipped.

    Called by ``forge stop`` after the owning sprint process is gone. Only
    touches an audit still marked ``in_flight`` — a story that finished on its
    own already wrote its real audit, and overwriting that with an
    outside-the-run guess would destroy the record it exists to be.

    The accumulated history (run_id, dev_loop, gate_decisions, phases) is left
    exactly as the sprint process flushed it; only the outcome and end timing
    are rewritten, so what the operator reads is what actually happened up to
    the stop.
    """
    path = _story_audit_path(project_root, sprint_name, slug)
    try:
        with open(path, encoding="utf-8") as f:
            audit_data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(audit_data, dict) or not audit_data.get(AUDIT_IN_FLIGHT_KEY):
        return None

    ended_at = datetime.datetime.now(datetime.timezone.utc)
    audit_data[AUDIT_IN_FLIGHT_KEY] = False
    audit_data["interrupted_by"] = reason

    outcome = audit_data.get("outcome")
    if not isinstance(outcome, dict):
        outcome = {}
        audit_data["outcome"] = outcome
    reached_phase = outcome.get("final_phase") or "UNKNOWN"
    outcome["success"] = False
    outcome["error_type"] = "OperatorStopped"
    outcome["message"] = f"Run {reason} by operator while the story was in {reached_phase}"

    timing = audit_data.get("timing")
    if isinstance(timing, dict):
        timing["finished_at"] = ended_at.isoformat()
        started_raw = timing.get("started_at")
        if isinstance(started_raw, str) and started_raw:
            try:
                started = datetime.datetime.fromisoformat(started_raw)
                timing["duration_seconds"] = (ended_at - started).total_seconds()
            except ValueError:
                pass

    try:
        _dump_audit_atomically(path, audit_data)
    except OSError:
        return None
    return path


# ── Cross-generation accounting (#2214) ───────────────────────────────
#
# A sprint that re-execs begins a new generation of the same run. A story the
# launch guard drops in the new generation may already have run in the old one —
# dev, review, a committed implementation, real spend. The drop record is
# synthesized from a state that never entered the state machine, so written as
# it stands it says INIT, $0.00, unsuccessful: indistinguishable from a story
# that never began, and every record derived from it inherits that silently.
#
# The generation that ran flushed its own evidence to the story's audit.yaml as
# it went (#2013). That evidence is what the drop record carries forward, so the
# record accounts for the work its run performed instead of being replaced by
# the state of the generation that dropped it.

#: Audit sections that describe work a run performed, rather than the
#: circumstances of its exit. These are the ones an abnormal-exit record must
#: take from the generation that did the work.
PRIOR_GENERATION_WORK_KEYS = (
    "iterations",
    "cost",
    "preflight",
    "context_manifests",
    "dev_handoffs",
    "dev_prompt_injections",
    "reviews",
    "reviewer_attempts",
    "human_review",
    "plan_review",
    "plan_validation",
    "story_validation",
    "convention_violations",
    "validate_blocks",
    "finding_registry",
    "review_topology_signal",
    "non_blocking_p1s",
    "routing_decision",
    "trust_checks",
    "trust_status",
    "phase_recovery",
    "phases",
    "totals",
    "workspace",
)


def _is_informative(value: object) -> bool:
    """True when ``value`` says something a record would otherwise be missing.

    A synthetic exit record is not empty — it carries zeroed counters, null
    phases and empty lists. Only values that assert something are carried, so a
    prior generation's silence on a section never overwrites the current
    record's own.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, dict):
        return any(_is_informative(v) for v in value.values())
    if isinstance(value, (list, tuple, set, str)):
        return len(value) > 0
    return True


def records_performed_work(audit: dict) -> bool:
    """True when an audit records a run that actually did something.

    Deliberately explicit rather than derived from :func:`_is_informative`: a
    run that never started still carries configured limits (``usage_summary``'s
    ``max``), so "this section is non-empty" is not the same claim as "this run
    performed work". The test is dev/review/gate activity, measured spend, or a
    phase that produced an outcome.
    """
    if not isinstance(audit, dict):
        return False

    iterations = audit.get("iterations")
    if isinstance(iterations, dict):
        for key in (
            "dev_attempts_total",
            "dev_iterations",
            "review_cycles_total",
            "review_cycles",
            "gate_runs",
        ):
            count = iterations.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                return True
        for key in ("dev_loop", "review_loop", "gate_decisions"):
            if iterations.get(key):
                return True

    cost = audit.get("cost")
    if isinstance(cost, dict):
        total = cost.get("total_usd")
        if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
            return True
        if cost.get("agents"):
            return True

    for key in ("reviews", "dev_handoffs", "preflight", "plan_review", "reviewer_attempts"):
        if audit.get(key):
            return True

    phases = audit.get("phases")
    if isinstance(phases, dict) and any(_is_informative(v) for v in phases.values()):
        return True

    return False


def prior_generation_summary(audit: dict) -> dict:
    """What an earlier generation's audit says it reached and spent.

    ``cost_measured`` distinguishes a recovered amount from an unmeasured one:
    a ``None`` ``cost_usd`` with ``cost_measured`` false means the prior
    generation's spend is unknown, which must never be read as zero (#1992).
    """
    cost = audit.get("cost") if isinstance(audit.get("cost"), dict) else {}
    raw_cost = cost.get("total_usd")
    measured = isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
    outcome = audit.get("outcome") if isinstance(audit.get("outcome"), dict) else {}
    timing = audit.get("timing") if isinstance(audit.get("timing"), dict) else {}
    run_id = audit.get("run_id")
    return {
        "run_id": run_id if isinstance(run_id, str) and run_id else None,
        "final_phase": outcome.get("final_phase"),
        "cost_usd": round(float(raw_cost), 4) if measured else None,
        "cost_measured": measured,
        "in_flight": bool(audit.get(AUDIT_IN_FLIGHT_KEY)),
        "started_at": timing.get("started_at"),
    }


@dataclass(frozen=True)
class PriorGeneration:
    """An earlier generation's flushed story audit, and how to account for it.

    ``independently_recorded`` says the prior generation already wrote its own
    per-run record. Its work is then visible on its own, so this record links to
    it and reports the phase it reached but does not restate its spend —
    counting the same dollars in two records would overstate the run.
    """

    audit: dict
    independently_recorded: bool = False

    @property
    def summary(self) -> dict:
        return {
            **prior_generation_summary(self.audit),
            "independently_recorded": self.independently_recorded,
        }

    @property
    def recoverable_cost_usd(self) -> float | None:
        """The prior generation's spend when this record is the one to carry it."""
        if self.independently_recorded:
            return None
        summary = prior_generation_summary(self.audit)
        return summary["cost_usd"] if summary["cost_measured"] else None


def carry_prior_generation_work(audit_data: dict, prior: PriorGeneration) -> list[str]:
    """Fold an earlier generation's recorded work into this record; return the keys carried.

    The record keeps its own identity and its own account of how it ended — the
    drop is what happened to *this* generation — but reports the phase the work
    reached and the budget it consumed, because those are properties of the run,
    not of the generation that observed its end. ``prior_generation`` names where
    the accounting came from so a reader is never left to infer it, and
    ``parent_run_id`` links the record back to the run that produced the work.
    """
    source = prior.audit
    summary = prior.summary
    skip = {"cost"} if prior.independently_recorded else set()

    carried: list[str] = []
    for key in PRIOR_GENERATION_WORK_KEYS:
        if key in skip or key not in source:
            continue
        value = source[key]
        if not _is_informative(value):
            continue
        audit_data[key] = value
        carried.append(key)

    outcome = audit_data.get("outcome")
    if isinstance(outcome, dict):
        # The phase this record reports is the furthest phase the run reached.
        # Reporting the post-boundary INIT is the specific falsehood being fixed:
        # it renders a story that ran dev and review as one that never started.
        if summary["final_phase"]:
            outcome["dropped_at_phase"] = outcome.get("final_phase")
            outcome["final_phase"] = summary["final_phase"]
        if summary["cost_measured"] and not prior.independently_recorded:
            outcome["cost_usd"] = summary["cost_usd"]
    if summary["run_id"]:
        audit_data["parent_run_id"] = summary["run_id"]
    audit_data["prior_generation"] = {**summary, "carried_keys": carried}
    return carried


def load_prior_generation_story_audit(
    project_root: Path,
    sprint_name: str | None,
    slug: str,
    *,
    exclude_run_id: str | None = None,
) -> PriorGeneration | None:
    """The audit an earlier generation flushed for ``slug``, when it holds work to carry.

    Returns ``None`` when there is no readable audit, when the audit belongs to
    *this* generation (``exclude_run_id``), or when it records no performed work
    — there is nothing to carry from a generation that did nothing.

    Best-effort: an unreadable or malformed audit yields ``None``.
    """
    if not sprint_name:
        return None
    path = _story_audit_path(project_root, sprint_name, slug)
    try:
        with open(path, encoding="utf-8") as f:
            audit = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(audit, dict):
        return None

    run_id = audit.get("run_id")
    if exclude_run_id is not None and run_id == exclude_run_id:
        return None
    if not records_performed_work(audit):
        return None

    independently_recorded = False
    if isinstance(run_id, str) and run_id:
        try:
            from ..coordinator import audit_substrate  # noqa: PLC0415

            independently_recorded = (
                audit_substrate.runs_dir(project_root) / f"{run_id}.json"
            ).exists()
        except Exception:  # noqa: BLE001 - identity lookup must not block the record
            independently_recorded = False
    return PriorGeneration(audit=audit, independently_recorded=independently_recorded)
