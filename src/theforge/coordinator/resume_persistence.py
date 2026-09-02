"""Durable per-story record of the coordinator phase outputs a resume would lose.

A resumed coordinator attempt (``forge run --resume``, a worker-timeout FULL
restart, a mid-sprint ``os.execv`` re-exec) allocates a *fresh*
``CoordinatorState`` and restores only two narrow slices of the prior attempt:
trajectory bookkeeping (``trajectory.yaml``) and the parsed plan (``plan.md``).
Everything preflight, plan review, and the escalate gate produced lived only on
the in-memory state of the process that produced it, so the audit ultimately
written for the story reported those phases as skipped or absent even though the
run log shows them completing and the coordinator acting on their output
(#2155).

This module is the sidecar for exactly that data.  It is deliberately *not* the
routing record (:mod:`theforge.coordinator.routing_persistence`): that record
exists to re-seat the routed review panel and is keyed to what routing consumed
and produced.  This one exists to keep the *audit* honest about which phases
ran.  The two are written from the same call sites so they never disagree, and
``routing_decision`` has one authoritative restore order — the live
``cached_preflight_state`` first, this sidecar second, the routing record last
(it is applied later, in the REVIEW-seating path, and re-derives the block from
the same complexity signal recorded here).

Design rules, all of which exist because this record is read at the moment the
run is least healthy:

* **Merge, never replace.** Each phase writes its own block as it produces it.
  A later save must not erase an earlier phase's block just because the state it
  was called with no longer carries that phase's fields.
* **Best-founded wins, not latest.** Where two saves both carry a block for the
  same phase, the one that examined more of the story is kept, and a degraded
  attempt never displaces a non-degraded one.  The record exists so later work
  can proceed, and a failed attempt carries strictly less of what later work
  needs — keeping it because it happened to be written last is how a resumed
  story deterministically replays the failure it was resumed to get past
  (#2351).  Every such selection is recorded in :data:`RESUME_SELECTION_KEY` so
  what the story resumes from is readable without diffing run directories.
* **Never overwrite a real value with a null.** Both when merging on save and
  when applying on load.
* **Best-effort.** A sidecar that cannot be written or read must never take down
  the run — a recovery aid that can fail the run it protects is worse than none.
* **Recovery is stated, not implied.** Applying the record returns which phases
  it recovered, and the caller records that in the audit, so a reader can always
  tell a phase this attempt executed from one lifted off disk.

Validity is keyed on the story text alone, matching ``routing_persistence``: a
resumed story has commits its preflight never saw, so a git-state fingerprint
would reject the record at exactly the moment it is needed.

Stdlib-only imports (project convention 4).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .state import CoordinatorState

RECORD_VERSION = 1

#: Phase blocks this record carries, in the order they are reported.
PHASE_BLOCKS: tuple[str, ...] = (
    "preflight",
    "routing_decision",
    "plan_review",
    "escalation",
    "timeout_escalation",
)

#: Sibling key carrying what review has and has not done for the story.
#:
#: Deliberately *not* a member of :data:`PHASE_BLOCKS`: nothing restores it onto
#: a resumed ``CoordinatorState``, so it must never appear in
#: ``recovered_phases`` (which names phases lifted off disk) nor decide whether
#: a record is usable.  It exists so a reader can tell "the next review cycle
#: has not run" from "review completed" without re-deriving either from the
#: worktree (#2239).  Additive: a record written before this key existed simply
#: lacks it, and classification then reports the obligation as *unknown* rather
#: than inventing a completed review.
REVIEW_PROGRESS_KEY = "review_progress"

#: Sibling key carrying every specification gap resolved for this story (#2122).
#:
#: Not a phase block: nothing reports it as a recovered phase, and its presence
#: alone never makes a record "usable" for phase recovery.  It is nonetheless
#: the one block written for durability rather than for the audit — an operator
#: answered a question, and that answer must survive the process that received
#: it, a resume, and a later fresh run of the same story.  Merged by gap
#: identity rather than incoming-wins so a save from a state that no longer
#: carries an earlier gap cannot erase its answer.
SPEC_GAP_KEY = "spec_gaps"

#: Sibling key carrying the complexity/routing audit — and, nested inside it,
#: the story allocation.  Not a phase block (nothing reports it as a recovered
#: phase) but merged and restored alongside them.
ROUTING_AUDIT_KEY = "complexity_routing_audit"

#: Non-phase key holding the merge's block-selection decisions (#2351).  A
#: bounded list, oldest first, so an operator reading the record can see that a
#: less-founded save was refused — or that a better-founded one displaced what
#: was there — without comparing files across run directories.
RESUME_SELECTION_KEY = "resume_selection"

#: Cap on retained selection entries.  The record is a recovery aid read when
#: the run is least healthy; an unbounded audit trail inside it is a liability.
_MAX_SELECTION_ENTRIES = 20

#: Verdicts that end the review loop.  Anything else recorded as the latest
#: verdict means the loop was still owed another cycle when the attempt stopped.
_TERMINAL_REVIEW_VERDICTS = frozenset({"APPROVE", "REJECT"})

#: Values of ``review_obligation`` in :func:`classify_review_obligation`.
REVIEW_OBLIGATION_UNKNOWN = "unknown"
REVIEW_OBLIGATION_NONE = "none"
REVIEW_OBLIGATION_CYCLE_NOT_RUN = "cycle_not_run"

_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_slug(slug: str) -> str:
    """Filesystem-safe filename stem for a story slug."""
    cleaned = _SAFE_SLUG_RE.sub("-", (slug or "").strip()).strip("-.")
    return cleaned or "story"


def resume_records_dir(project_root: Path) -> Path:
    return Path(project_root) / ".forge" / "resume_state"


def resume_record_path(project_root: Path, slug: str) -> Path:
    return resume_records_dir(project_root) / f"{_safe_slug(slug)}.json"


def story_content_hash(story_content: str) -> str:
    return hashlib.sha256((story_content or "").encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    """Return ``value`` if it survives a JSON round-trip, else None.

    Phase outputs are assembled from parsed agent YAML and coordinator dicts, so
    they are ordinarily JSON-safe — but a single un-serializable object must
    degrade that one block rather than lose the whole record.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return None
    return value


# ── Block builders ───────────────────────────────────────────────────────


def _preflight_block(state: "CoordinatorState") -> dict[str, Any] | None:
    """The preflight judgement, or None when preflight produced none.

    ``verdict`` is recorded verbatim, including the deliberate ``SKIPPED`` the
    ``--from <phase>`` bypass sets: a resume past a genuinely skipped preflight
    must still report SKIPPED rather than degrading to absent.
    """
    if state.preflight_verdict is None and state.preflight_complexity is None:
        return None
    return {
        "verdict": state.preflight_verdict,
        "reason": state.preflight_reason,
        "complexity": state.preflight_complexity,
        "complexity_score": state.preflight_complexity_score,
        "implementation_complexity_score": state.preflight_implementation_complexity_score,
        "validation_complexity_score": state.preflight_validation_complexity_score,
        "complexity_projection": state.preflight_complexity_projection,
        "complexity_evidence": _jsonable(list(state.preflight_complexity_evidence or [])),
        "scope_exceeded": bool(state.preflight_scope_exceeded),
        # Preflight complexity gate (#2681). A resumed attempt allocates a fresh
        # CoordinatorState, so without these the operator's scope decision would
        # be absent from the resumed run's audit — and the gate would re-ask a
        # question they already answered for this story text.
        "complexity_gate_opened": bool(state.preflight_complexity_gate_opened),
        "complexity_gate_score": state.preflight_complexity_gate_score,
        "complexity_gate_implementation_score": (
            state.preflight_complexity_gate_implementation_score
        ),
        "complexity_gate_validation_score": state.preflight_complexity_gate_validation_score,
        "complexity_gate_threshold": state.preflight_complexity_gate_threshold,
        "complexity_gate_decision": state.preflight_complexity_gate_decision,
        "complexity_gate_decision_source": state.preflight_complexity_gate_decision_source,
        "complexity_gate_no_decision_fallback": (
            state.preflight_complexity_gate_no_decision_fallback
        ),
        "complexity_gate_waited_seconds": state.preflight_complexity_gate_waited_seconds,
        "complexity_gate_decided_at": state.preflight_complexity_gate_decided_at,
        "complexity_gate_score_provenance": state.preflight_complexity_gate_score_provenance,
        # The decomposition assessment carried on that pause (#2686). Restored
        # so a resumed attempt neither loses an artifact that was paid for nor
        # pays to produce it a second time.
        "complexity_gate_assessment": _jsonable(state.preflight_complexity_gate_assessment),
        "complexity_gate_assessment_generated": bool(
            state.preflight_complexity_gate_assessment_generated
        ),
        "complexity_gate_assessment_none_reason": (
            state.preflight_complexity_gate_assessment_none_reason
        ),
        "complexity_gate_assessment_errors": [
            str(e) for e in state.preflight_complexity_gate_assessment_errors or []
        ],
        "complexity_gate_assessment_duration_s": (
            state.preflight_complexity_gate_assessment_duration_s
        ),
        "complexity_gate_assessment_model": state.preflight_complexity_gate_assessment_model,
        "complexity_gate_assessment_profile": state.preflight_complexity_gate_assessment_profile,
        "complexity_gate_assessment_cost_provenance": (
            state.preflight_complexity_gate_assessment_cost_provenance
        ),
        # Provenance only, for the same reason as preflight's own cost below: a
        # restored figure describes what a PRIOR attempt spent, and folding it
        # into this process's aggregates would double-count it at the sprint
        # roll-up. ``_apply_preflight`` lands it on the prior-cost field.
        "complexity_gate_assessment_cost_usd": (
            state.preflight_complexity_gate_assessment_cost_usd
            if state.preflight_complexity_gate_assessment_invoked
            else state.preflight_complexity_gate_assessment_prior_cost_usd
        ),
        "sufficiency": state.preflight_sufficiency,
        "work_type": state.preflight_work_type,
        "domains": list(state.preflight_domains or []),
        "contract_change": bool(state.preflight_contract_change),
        "bundle_candidate": bool(state.preflight_bundle_candidate),
        "batch_group": state.preflight_batch_group,
        "warnings": [str(w) for w in state.preflight_warnings or []],
        "likely_files": (
            None
            if state.preflight_likely_files is None
            else [str(f) for f in state.preflight_likely_files]
        ),
        "criteria_checked": _jsonable(list(state.preflight_criteria_checked or [])),
        "symptom_verification": _jsonable(dict(state.preflight_symptom_verification or {})),
        "risk_signals": [str(s) for s in state.preflight_risk_signals or []],
        "degraded": bool(state.preflight_degraded),
        "degraded_reason": state.preflight_degraded_reason,
        "failure_action": state.preflight_failure_action,
        "partial_evidence": _jsonable(state.preflight_partial_evidence),
        # Policy-assertion provenance (#2137). A resumed attempt allocates a fresh
        # CoordinatorState, so without these a ratified refusal could not name its
        # assertion on resume and a downgrade's retraction/ratification candidates
        # would be absent from the final run audit.
        "blocking_basis": state.preflight_blocking_basis,
        "policy_assertions_cited": _jsonable(list(state.preflight_policy_assertions_cited or [])),
        "policy_assertions_resolved": _jsonable(
            list(state.preflight_policy_assertions_resolved or [])
        ),
        "policy_retraction_candidates": _jsonable(
            list(state.preflight_policy_retraction_candidates or [])
        ),
        "policy_ratification_candidates": _jsonable(
            list(state.preflight_policy_ratification_candidates or [])
        ),
        "policy_blocking_authority": bool(state.preflight_policy_blocking_authority),
        "policy_adjudication": _jsonable(dict(state.preflight_policy_adjudication or {})),
        "duration_s": state.preflight_duration_s,
        "cache_snapshot": dict(state.preflight_cache_snapshot or {}),
        # Provenance only. Deliberately NOT restored onto the resumed state:
        # cost aggregates describe what *this* process spent, and folding a
        # prior attempt's spend into them would double-count it at the sprint
        # roll-up. Recorded so the operator can still see the phase was charged.
        "cost_usd": (
            state.preflight_result.cost_usd if state.preflight_result is not None else None
        ),
    }


def _plan_review_block(state: "CoordinatorState") -> dict[str, Any] | None:
    """The plan-review outcome, or None when no plan review reached a decision."""
    if state.plan_review_decision is None and not state.plan_review_results:
        return None
    return {
        "decision": state.plan_review_decision,
        "mode": state.plan_review_mode,
        "waited_seconds": state.plan_review_waited_seconds,
        "regen_count": state.plan_regen_count,
        "agent_review_findings": state.plan_agent_review_findings,
        "durations": [float(d) for d in state.plan_review_durations or []],
        "iterations": len(state.plan_review_results),
        "match_provenance": _jsonable(state.plan_match_provenance),
        # Provenance only, for the same reason as preflight's cost.
        "cost_usd": state.total_plan_review_cost_measured,
    }


def _escalation_block(state: "CoordinatorState") -> dict[str, Any] | None:
    """The escalate-gate decision and its advisory, or None when none was made."""
    if (
        state.escalate_decision is None
        and state.escalate_declined_action is None
        and not state.advisory_generated
        and not state.advisory_launch_failure
    ):
        return None
    return {
        "decision": state.escalate_decision,
        "selected_action": state.escalate_selected_action,
        # Provenance of the decision, and — for an expiry — whether the advisory
        # recommendation was applied or which absence stopped it (#2279). Kept in
        # the durable record so a resumed run reports the same "who decided this"
        # as the live one did.
        "decision_source": state.escalate_decision_source,
        "timeout_advice": state.escalate_timeout_advice,
        "awaiting_operator": state.escalate_decision == "advisory_pending",
        # What the operator chose that the gate could not carry out (#2300).
        # Survives the process so the selection is recoverable from the run
        # rather than existing only as a log line.
        "declined_action": state.escalate_declined_action,
        "declined_reason": state.escalate_declined_reason,
        "reason": state.escalate_reason,
        "advisory_generated": bool(state.advisory_generated),
        "advisory_report": _jsonable(state.advisory_report),
        "advisory_unavailable_reason": state.advisory_unavailable_reason,
        "advisory_launch_failure": bool(state.advisory_launch_failure),
        "advisory_launch_reason": state.advisory_launch_reason,
        "waited_seconds": state.human_review_waited_seconds,
    }


def _review_progress_block(state: "CoordinatorState") -> dict[str, Any] | None:
    """What review had done for this story when the attempt stopped, or None.

    Counters and the latest verdict only — never a judgement.  The judgement is
    :func:`classify_review_obligation`, so every surface that reads this record
    (resume startup, ``forge status``, the pending-decision list) derives the
    same answer from the same facts instead of each inventing its own.

    Returns None when review has neither run nor been budgeted, so a record for
    a story that never reached REVIEW carries no misleading zeroes.
    """
    latest_verdict: str | None = None
    if state.review_results:
        latest_verdict = getattr(state.review_results[-1], "verdict", None)
    reviewer_cycles = state.reviewer_cycles_run
    if not reviewer_cycles and not state.review_results and latest_verdict is None:
        return None
    return {
        "reviewer_cycles_run": reviewer_cycles,
        "review_cycles_spent": state.review_cycles_spent,
        "review_cycle": state.review_cycle,
        "validate_opened_review_cycles": state.validate_opened_review_cycles,
        "recorded_review_results": len(state.review_results),
        "latest_review_verdict": latest_verdict,
        "last_gate_decision": state.last_gate_decision,
        "last_gate_commit": state.last_gate_commit,
        "gate_runs": state.gate_runs,
    }


def _spec_gap_block(state: "CoordinatorState") -> dict[str, Any] | None:
    """Specification gaps resolved for this story, or None when there are none.

    Only *resolutions* are persisted, not the raise events: an event describes
    what one run did (which belongs to that run's audit), while a resolution is
    a settled answer about the story itself, which every later attempt needs.
    """
    resolutions = _jsonable(list(getattr(state, "spec_gap_resolutions", []) or []))
    if not resolutions:
        return None
    return {"resolutions": resolutions}


def spec_gap_resolutions(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read the resolved specification gaps out of a loaded record."""
    if not isinstance(record, dict):
        return []
    block = record.get(SPEC_GAP_KEY)
    if not isinstance(block, dict):
        return []
    resolutions = block.get("resolutions")
    if not isinstance(resolutions, list):
        return []
    return [entry for entry in resolutions if isinstance(entry, dict)]


def _merge_spec_gap_blocks(existing: Any, incoming: Any) -> dict[str, Any]:
    """Union two spec-gap blocks by gap identity, newest answer winning.

    Incoming-wins (the rule for every other non-phase block) is wrong here: a
    save from a phase whose state carries only this run's gaps would drop an
    answer recorded by an earlier attempt of the same story, which is exactly the
    loss this record exists to prevent.
    """
    order: list[tuple[str, str]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in (
        *spec_gap_resolutions({SPEC_GAP_KEY: existing} if existing else None),
        *spec_gap_resolutions({SPEC_GAP_KEY: incoming} if incoming else None),
    ):
        key = (
            str(entry.get("criterion") or "").strip(),
            str(entry.get("undefined_case") or "").strip(),
        )
        if key == ("", ""):
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = dict(entry)
    return {"resolutions": [by_key[key] for key in order]}


def capture_phase_blocks(state: "CoordinatorState") -> dict[str, Any]:
    """Return every phase block this state currently carries (absent ones omitted)."""
    blocks: dict[str, Any] = {}
    preflight = _preflight_block(state)
    if preflight is not None:
        blocks["preflight"] = preflight
    routing = _jsonable(state.routing_decision)
    if routing:
        blocks["routing_decision"] = routing
    routing_audit = _jsonable(state.complexity_routing_audit)
    if routing_audit:
        blocks["complexity_routing_audit"] = routing_audit
    plan_review = _plan_review_block(state)
    if plan_review is not None:
        blocks["plan_review"] = plan_review
    escalation = _escalation_block(state)
    if escalation is not None:
        blocks["escalation"] = escalation
    timeout_escalation = _jsonable(state.timeout_escalation_audit)
    if timeout_escalation:
        blocks["timeout_escalation"] = timeout_escalation
    review_progress = _review_progress_block(state)
    if review_progress is not None:
        blocks[REVIEW_PROGRESS_KEY] = review_progress
    spec_gaps = _spec_gap_block(state)
    if spec_gaps is not None:
        blocks[SPEC_GAP_KEY] = spec_gaps
    return blocks


# ── Persistence ──────────────────────────────────────────────────────────


_METADATA_KEYS = frozenset(
    {"version", "slug", "run_id", "story_content_hash", RESUME_SELECTION_KEY}
)


# ── Foundedness ──────────────────────────────────────────────────────────
#
# What makes one save's block more authoritative than another's for the same
# phase.  Two signals, in that order of weight:
#
# 1. *degraded* — an attempt the coordinator marked degraded produced its block
#    from a failure, not from an observation.
# 2. *examined* — how much of the story the phase actually inspected.  Preflight
#    records this directly as ``criteria_checked``; an attempt that checked
#    nothing cannot supersede one that checked something, whatever their order.
#
# Blocks carrying neither signal (``plan_review``, ``escalation``,
# ``timeout_escalation``, ``review_progress``) have no foundation to compare and
# are treated as equally founded, so ordinary incoming-wins merging applies.  A
# signal must not be invented for them: their content is a decision, not an
# observation, and the latest one is the one the coordinator acted on.

#: Mirror of ``preflight.COMPLEXITY_SOURCE_DEGRADED``.  Duplicated rather than
#: imported to keep this module stdlib-only (convention 4).
_DEGRADED_COMPLEXITY_SOURCE = "preflight_degraded_conservative"

#: Foundation ranks, low to high.  A tuple compares lexicographically, so a
#: non-degraded block outranks a degraded one whatever either examined.
_DEGRADED_RANK = 0
_FOUNDED_RANK = 1


def _preflight_foundation(block: Any) -> tuple[int, int] | None:
    """Return ``(degraded_rank, examined)`` for a preflight block, or None.

    None means the block carries no foundedness signal at all (a record written
    before either field existed), which the merge treats as "no opinion".
    """
    if not isinstance(block, dict) or not block:
        return None
    degraded = block.get("degraded")
    criteria = block.get("criteria_checked")
    examined = len(criteria) if isinstance(criteria, list) else None
    if degraded is None and examined is None:
        return None
    return (_DEGRADED_RANK if bool(degraded) else _FOUNDED_RANK, examined or 0)


def _derived_foundation(block: Any, preflight_block: Any) -> tuple[int, int] | None:
    """Foundation of a value *derived from* preflight rather than observed.

    ``complexity_routing_audit`` carries its own provenance (``complexity_source``
    / ``preflight_degraded``, stamped by ``preflight.stamp_complexity_provenance``);
    ``routing_decision`` carries none and inherits the foundation of the
    preflight block written in the same save.
    """
    inherited = _preflight_foundation(preflight_block)
    degraded: bool | None = None
    if isinstance(block, dict):
        provenance = block.get("preflight_degraded")
        if isinstance(provenance, dict) and "degraded" in provenance:
            degraded = bool(provenance.get("degraded"))
        else:
            source = block.get("complexity_source")
            if isinstance(source, str) and source:
                degraded = source == _DEGRADED_COMPLEXITY_SOURCE
    if degraded is None:
        return inherited
    return (
        _DEGRADED_RANK if degraded else _FOUNDED_RANK,
        inherited[1] if inherited is not None else 0,
    )


def block_foundation(
    name: str, block: Any, sibling_blocks: dict[str, Any] | None = None
) -> tuple[int, int] | None:
    """Return how well-founded ``block`` is, or None when it carries no signal.

    ``sibling_blocks`` are the other blocks written by the same save (or already
    on the record), which is where a derived block's provenance lives.
    """
    if name == "preflight":
        return _preflight_foundation(block)
    if name in ("routing_decision", ROUTING_AUDIT_KEY):
        return _derived_foundation(block, (sibling_blocks or {}).get("preflight"))
    return None


def _foundation_report(foundation: tuple[int, int] | None) -> dict[str, Any] | None:
    """Operator-readable form of a foundation tuple."""
    if foundation is None:
        return None
    return {"degraded": foundation[0] == _DEGRADED_RANK, "examined": foundation[1]}


def _merge_phase_blocks(
    base: dict[str, Any],
    blocks: dict[str, Any],
    *,
    prior_run_id: str | None,
    run_id: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fold ``blocks`` into ``base`` block by block, best-founded winning.

    Returns the merged blocks and the selection decisions worth disclosing.
    Equal foundation — including two blocks that both carry no signal — keeps
    the incoming block, so ordinary additive saves behave exactly as before.
    """
    merged = dict(base)
    decisions: list[dict[str, Any]] = []
    for name, value in blocks.items():
        if not value:
            continue
        existing_block = base.get(name)
        if not existing_block:
            merged[name] = value
            continue
        if name == SPEC_GAP_KEY:
            # Union, not replace. See _merge_spec_gap_blocks: an operator answer
            # already on the record survives a save whose state never held it.
            merged[name] = _merge_spec_gap_blocks(existing_block, value)
            continue
        prior = block_foundation(name, existing_block, base)
        incoming = block_foundation(name, value, blocks)
        if prior is not None and incoming is not None and incoming != prior:
            keep_existing = incoming < prior
            decisions.append(
                {
                    "phase": name,
                    "action": "kept_existing" if keep_existing else "replaced_existing",
                    "reason": (
                        "incoming_block_less_founded"
                        if keep_existing
                        else "incoming_block_more_founded"
                    ),
                    "prior_foundation": _foundation_report(prior),
                    "incoming_foundation": _foundation_report(incoming),
                    "prior_run_id": prior_run_id,
                    "incoming_run_id": run_id,
                }
            )
            if keep_existing:
                continue
        merged[name] = value
    return merged, decisions


def merge_resume_record(
    existing: dict[str, Any] | None,
    blocks: dict[str, Any],
    *,
    slug: str,
    story_hash: str | None,
    run_id: str | None,
) -> dict[str, Any]:
    """Fold ``blocks`` into ``existing`` without dropping earlier phases.

    A block already on disk is kept when this save carries nothing for it — the
    caller saves from whichever phase just finished, and a state that no longer
    holds an earlier phase's fields must not erase them.  When the recorded
    story hash disagrees with the current one the prior blocks describe a
    different story and are discarded rather than merged.

    Each block is decided on its own foundation rather than by save order (see
    the foundedness section above), so a degraded attempt that examined nothing
    does not displace a stored block from an attempt that succeeded.  Selections
    that changed or preserved what the story resumes from are recorded under
    :data:`RESUME_SELECTION_KEY`, which is metadata and never re-enters the
    block merge.
    """
    base: dict[str, Any] = {}
    prior_selection: list[dict[str, Any]] = []
    prior_run_id: str | None = None
    if isinstance(existing, dict) and existing.get("version") == RECORD_VERSION:
        recorded_hash = existing.get("story_content_hash")
        if not (story_hash and recorded_hash and recorded_hash != story_hash):
            base = {k: v for k, v in existing.items() if k not in _METADATA_KEYS}
            recorded_selection = existing.get(RESUME_SELECTION_KEY)
            if isinstance(recorded_selection, list):
                prior_selection = [e for e in recorded_selection if isinstance(e, dict)]
            recorded_run_id = existing.get("run_id")
            prior_run_id = recorded_run_id if isinstance(recorded_run_id, str) else None
    merged, decisions = _merge_phase_blocks(
        base,
        {k: v for k, v in blocks.items() if v},
        prior_run_id=prior_run_id,
        run_id=run_id,
    )
    record: dict[str, Any] = {
        "version": RECORD_VERSION,
        "slug": slug,
        "run_id": run_id or (existing or {}).get("run_id"),
        "story_content_hash": story_hash or (existing or {}).get("story_content_hash"),
        **merged,
    }
    selection = [*prior_selection, *decisions][-_MAX_SELECTION_ENTRIES:]
    if selection:
        record[RESUME_SELECTION_KEY] = selection
    return record


def save_resume_record(
    project_root: Path,
    state: "CoordinatorState",
    *,
    slug: str,
    story_content: str | None = None,
    run_id: str | None = None,
) -> Path | None:
    """Persist the phase blocks ``state`` carries; return the path, or None.

    Best-effort by design (see module docstring): every failure mode returns
    None instead of raising into the phase that called it.
    """
    blocks = capture_phase_blocks(state)
    # Review progress is evidence *about* a record, not a record of its own: a
    # story with review counters but no recovered phase output still gets no
    # sidecar, exactly as before this key existed.
    if not any(name != REVIEW_PROGRESS_KEY for name in blocks):
        return None
    try:
        path = resume_record_path(project_root, slug)
    except (TypeError, ValueError):
        return None
    record = merge_resume_record(
        load_resume_record(project_root, slug),
        blocks,
        slug=slug,
        story_hash=story_content_hash(story_content) if story_content else None,
        run_id=run_id,
    )
    try:
        payload = json.dumps(record, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return None
    return path


def load_resume_record(project_root: Path, slug: str) -> dict[str, Any] | None:
    """Read a story's persisted phase record, or None when unusable."""
    try:
        raw = resume_record_path(project_root, slug).read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("version") != RECORD_VERSION:
        return None
    return record


def load_spec_gap_resolutions(
    project_root: Path,
    slug: str,
    *,
    story_content: str | None = None,
) -> list[dict[str, Any]]:
    """Specification gaps already resolved for ``slug``, newest answer per gap.

    Read on *every* entry into the dev loop — fresh run, resume, and
    review-only re-entry alike — not only on the resume path (#2122).  The
    acceptance criterion is that an operator's answer is "not rediscovered on a
    later run *or* on a resume", and a fresh ``run_task`` builds a new
    ``CoordinatorState`` that no phase-recovery path touches.

    Deliberately independent of :func:`validate_resume_record`: that function
    asks whether a record can stand in for *phases that ran*, and a record
    carrying only an answered gap legitimately fails it.  What matters here is
    narrower — that the record describes the same story text.  A record with no
    recorded hash is accepted for the same reason it is elsewhere: rejecting it
    would discard an answer the operator demonstrably gave.  A record whose hash
    *disagrees* is refused, because the criterion the answer settles may no
    longer be the criterion in the spec.
    """
    record = load_resume_record(project_root, slug)
    if record is None:
        return []
    recorded_hash = record.get("story_content_hash")
    if recorded_hash and story_content is not None:
        if recorded_hash != story_content_hash(story_content):
            return []
    return spec_gap_resolutions(record)


def validate_resume_record(
    record: dict[str, Any],
    *,
    story_content: str | None,
) -> tuple[bool, str]:
    """Return ``(usable, reason)`` for a loaded record.

    A record with no recorded story hash is accepted as ``unverified_story``:
    it still names phases that demonstrably ran, and rejecting it would put the
    audit back to claiming they did not.
    """
    if not any(record.get(name) for name in (*PHASE_BLOCKS, "complexity_routing_audit")):
        return False, "no_phase_blocks"
    recorded_hash = record.get("story_content_hash")
    if not recorded_hash or story_content is None:
        return True, "unverified_story"
    if recorded_hash != story_content_hash(story_content):
        return False, "story_content_changed"
    return True, "story_content_match"


# ── Re-entry analysis ────────────────────────────────────────────────────
#
# Two commands re-enter a stopped story and do materially different things:
# ``forge review`` runs the review phase, ``forge sprint --resume`` recovers the
# phase records this module holds and continues from what they say.  When those
# records carry an escalate-gate decision *and* the review loop was still owed a
# cycle, the two paths disagree about whether the work gets reviewed.  The
# analysis below is the single derivation of that fact; every surface that tells
# an operator about it reads from here rather than re-deriving it (#2239).


def classify_review_obligation(record: dict[str, Any] | None) -> dict[str, Any]:
    """Return what the review loop still owes for the story ``record`` describes.

    ``review_obligation`` is one of:

    * ``cycle_not_run`` — a review cycle concluded without ending the loop, so
      the next cycle is outstanding.  ``outstanding_review_cycle`` names it.
    * ``none``          — review reached a terminal verdict; nothing outstanding.
    * ``unknown``       — the record carries no review progress (it predates
      :data:`REVIEW_PROGRESS_KEY`, or the story never reached REVIEW).  Reported
      as unknown rather than as ``none``: absence of evidence is not evidence
      that review completed.
    """
    unknown: dict[str, Any] = {
        "review_obligation": REVIEW_OBLIGATION_UNKNOWN,
        "outstanding_review_cycle": None,
        "latest_review_verdict": None,
        "reviewer_cycles_run": None,
        "last_gate_decision": None,
    }
    if not isinstance(record, dict):
        return unknown
    block = record.get(REVIEW_PROGRESS_KEY)
    if not isinstance(block, dict) or not block:
        return unknown

    verdict = block.get("latest_review_verdict")
    verdict = verdict.strip() if isinstance(verdict, str) else None
    cycles_run = block.get("reviewer_cycles_run")
    if not isinstance(cycles_run, int) or isinstance(cycles_run, bool):
        cycles_run = None
    gate_decision = block.get("last_gate_decision")
    gate_decision = gate_decision if isinstance(gate_decision, str) and gate_decision else None

    if not verdict:
        obligation = REVIEW_OBLIGATION_UNKNOWN
        outstanding = None
    elif verdict.upper() in _TERMINAL_REVIEW_VERDICTS:
        obligation = REVIEW_OBLIGATION_NONE
        outstanding = None
    else:
        obligation = REVIEW_OBLIGATION_CYCLE_NOT_RUN
        outstanding = (cycles_run or 0) + 1
    return {
        "review_obligation": obligation,
        "outstanding_review_cycle": outstanding,
        "latest_review_verdict": verdict,
        "reviewer_cycles_run": cycles_run,
        "last_gate_decision": gate_decision,
    }


def analyze_reentry(record: dict[str, Any] | None) -> dict[str, Any]:
    """Describe what re-entering the story ``record`` describes would do.

    Independent of any live ``CoordinatorState`` so the status surface can answer
    the question without starting a run — that is the point of the acceptance
    criterion it serves: the operator sees this *before* spending anything.

    ``skips_review`` is the disclosure that matters: a recorded escalate-gate
    decision plus an outstanding review cycle means ``forge sprint --resume``
    continues from the decision and never runs the cycle, while ``forge review``
    runs it.
    """
    review = classify_review_obligation(record)
    escalation = (record or {}).get("escalation") if isinstance(record, dict) else None
    escalation = escalation if isinstance(escalation, dict) else {}
    decision = escalation.get("decision") or None
    selected_action = escalation.get("selected_action") or None

    outstanding_phases: list[str] = []
    if review["review_obligation"] == REVIEW_OBLIGATION_CYCLE_NOT_RUN:
        outstanding_phases.append("REVIEW")

    return {
        **review,
        "escalation_decision": decision,
        "escalation_selected_action": selected_action,
        "outstanding_phases": outstanding_phases,
        "skips_review": bool(decision) and bool(outstanding_phases),
        "source_run_id": (record or {}).get("run_id") if isinstance(record, dict) else None,
    }


def describe_outstanding_phases(analysis: dict[str, Any]) -> list[str]:
    """Operator-facing names for the phases the story stopped still owing.

    One phrasing, so the status table, the completed-sprint digest and the
    pending-decision list cannot describe the same record differently.  Empty
    when nothing is outstanding.
    """
    phases = list(analysis.get("outstanding_phases") or [])
    cycle = analysis.get("outstanding_review_cycle")
    if phases == ["REVIEW"] and cycle:
        return [f"REVIEW cycle {cycle} not run"]
    return phases


def describe_reentry_paths(analysis: dict[str, Any]) -> str:
    """One-line operator-facing sentence naming each re-entry path's effect.

    Empty when nothing about the re-entry differs between the two paths — the
    line is only worth printing where the choice changes what runs.
    """
    if not analysis.get("skips_review"):
        return ""
    cycle = analysis.get("outstanding_review_cycle")
    cycle_str = f"REVIEW cycle {cycle}" if cycle else "the outstanding REVIEW cycle"
    decision = analysis.get("escalation_decision")
    action = analysis.get("escalation_selected_action")
    decision_str = f"{decision}/{action}" if action else str(decision)
    return (
        f"forge review runs {cycle_str}; "
        f"forge sprint --resume recovers escalation decision {decision_str} and skips REVIEW"
    )


def _reentry_impact(record: dict[str, Any], recovered: list[str]) -> dict[str, Any] | None:
    """The re-entry analysis, but only when *this* recovery acts on it.

    Returns None unless the escalate-gate decision was genuinely lifted off disk
    onto this attempt's state — a decision this attempt produced itself is not a
    recovery, and reporting it as one would make the disclosure noise.
    """
    if "escalation" not in recovered:
        return None
    analysis = analyze_reentry(record)
    if not analysis["skips_review"]:
        return None
    return analysis


def load_reentry_analysis(project_root: Path, slug: str) -> dict[str, Any] | None:
    """Re-entry analysis for a story's persisted record, or None when there is none.

    Best-effort like everything else here: a story with no record, or a record
    that says nothing about review, yields None rather than an error.
    """
    record = load_resume_record(project_root, slug)
    if record is None:
        return None
    analysis = analyze_reentry(record)
    if analysis["review_obligation"] == REVIEW_OBLIGATION_UNKNOWN and not analysis["skips_review"]:
        return None
    return analysis


# ── Restore ──────────────────────────────────────────────────────────────


def _set_if_unset(state: "CoordinatorState", attr: str, value: Any) -> bool:
    """Assign ``value`` only when it is real and the state has no value yet.

    Returns whether the assignment *changed* anything, so a record whose blocks
    only restate the state's own defaults is not reported as a recovery.
    """
    if value is None or value == [] or value == {} or value is False:
        return False
    current = getattr(state, attr, None)
    if current is not None and current != [] and current != {} and current is not False:
        return False
    setattr(state, attr, value)
    return True


#: Marker keys stamped on a value carried forward from a degraded attempt.
#: A degraded preflight still yields a conservative complexity score so routing,
#: timeouts, and allocation have a number — but nothing observed founded it, and
#: a resumed attempt that consumes it must be able to say so (#2351).
UNFOUNDED_KEY = "unfounded"
UNFOUNDED_REASON_KEY = "unfounded_reason"
UNFOUNDED_SOURCE_KEY = "unfounded_source"


def _degraded_provenance(record: dict[str, Any]) -> dict[str, str] | None:
    """Why this record's derived values are unfounded, or None when they are not.

    The preflight block is the direct signal; the routing audit's own
    provenance stamp is the fallback for a record whose preflight block was
    never written or predates the ``degraded`` field.
    """
    preflight = record.get("preflight")
    if isinstance(preflight, dict) and preflight.get("degraded"):
        return {
            "reason": str(preflight.get("degraded_reason") or "preflight_degraded"),
            "source": "preflight",
        }
    audit = record.get(ROUTING_AUDIT_KEY)
    if isinstance(audit, dict):
        stamped = audit.get("preflight_degraded")
        if isinstance(stamped, dict) and stamped.get("degraded"):
            return {
                "reason": str(stamped.get("degraded_reason") or "preflight_degraded"),
                "source": ROUTING_AUDIT_KEY,
            }
        if audit.get("complexity_source") == _DEGRADED_COMPLEXITY_SOURCE:
            return {"reason": _DEGRADED_COMPLEXITY_SOURCE, "source": ROUTING_AUDIT_KEY}
    return None


def _mark_unfounded(block: dict[str, Any], provenance: dict[str, str]) -> dict[str, Any]:
    """Return ``block`` copied with the unfounded markers stamped on it."""
    return {
        **block,
        UNFOUNDED_KEY: True,
        UNFOUNDED_REASON_KEY: provenance["reason"],
        UNFOUNDED_SOURCE_KEY: provenance["source"],
    }


def _apply_preflight(state: "CoordinatorState", block: dict[str, Any]) -> bool:
    """Restore preflight fields onto ``state``; return whether anything landed.

    Costs are not restored — see ``_preflight_block``.
    """
    if state.preflight_verdict is not None:
        # This attempt has its own preflight judgement; it wins outright.
        return False
    applied = False
    for attr, key in (
        ("preflight_verdict", "verdict"),
        ("preflight_reason", "reason"),
        ("preflight_complexity", "complexity"),
        ("preflight_complexity_score", "complexity_score"),
        ("preflight_implementation_complexity_score", "implementation_complexity_score"),
        ("preflight_validation_complexity_score", "validation_complexity_score"),
        ("preflight_complexity_projection", "complexity_projection"),
        ("preflight_complexity_evidence", "complexity_evidence"),
        ("preflight_scope_exceeded", "scope_exceeded"),
        ("preflight_complexity_gate_opened", "complexity_gate_opened"),
        ("preflight_complexity_gate_score", "complexity_gate_score"),
        (
            "preflight_complexity_gate_implementation_score",
            "complexity_gate_implementation_score",
        ),
        ("preflight_complexity_gate_validation_score", "complexity_gate_validation_score"),
        ("preflight_complexity_gate_threshold", "complexity_gate_threshold"),
        ("preflight_complexity_gate_decision", "complexity_gate_decision"),
        ("preflight_complexity_gate_decision_source", "complexity_gate_decision_source"),
        (
            "preflight_complexity_gate_no_decision_fallback",
            "complexity_gate_no_decision_fallback",
        ),
        ("preflight_complexity_gate_waited_seconds", "complexity_gate_waited_seconds"),
        ("preflight_complexity_gate_decided_at", "complexity_gate_decided_at"),
        (
            "preflight_complexity_gate_score_provenance",
            "complexity_gate_score_provenance",
        ),
        ("preflight_complexity_gate_assessment", "complexity_gate_assessment"),
        (
            "preflight_complexity_gate_assessment_generated",
            "complexity_gate_assessment_generated",
        ),
        (
            "preflight_complexity_gate_assessment_none_reason",
            "complexity_gate_assessment_none_reason",
        ),
        ("preflight_complexity_gate_assessment_errors", "complexity_gate_assessment_errors"),
        (
            "preflight_complexity_gate_assessment_duration_s",
            "complexity_gate_assessment_duration_s",
        ),
        ("preflight_complexity_gate_assessment_model", "complexity_gate_assessment_model"),
        ("preflight_complexity_gate_assessment_profile", "complexity_gate_assessment_profile"),
        (
            "preflight_complexity_gate_assessment_cost_provenance",
            "complexity_gate_assessment_cost_provenance",
        ),
        # Deliberately NOT ``..._cost_usd``: a restored cost is a prior attempt's
        # spend, and ``total_decomposition_assessment_cost`` reads the live field
        # only when THIS process invoked the assessment.
        (
            "preflight_complexity_gate_assessment_prior_cost_usd",
            "complexity_gate_assessment_cost_usd",
        ),
        ("preflight_sufficiency", "sufficiency"),
        ("preflight_work_type", "work_type"),
        ("preflight_domains", "domains"),
        ("preflight_contract_change", "contract_change"),
        ("preflight_bundle_candidate", "bundle_candidate"),
        ("preflight_batch_group", "batch_group"),
        ("preflight_warnings", "warnings"),
        ("preflight_likely_files", "likely_files"),
        ("preflight_criteria_checked", "criteria_checked"),
        ("preflight_symptom_verification", "symptom_verification"),
        ("preflight_risk_signals", "risk_signals"),
        ("preflight_degraded", "degraded"),
        ("preflight_degraded_reason", "degraded_reason"),
        ("preflight_failure_action", "failure_action"),
        ("preflight_partial_evidence", "partial_evidence"),
        ("preflight_duration_s", "duration_s"),
        ("preflight_cache_snapshot", "cache_snapshot"),
        ("preflight_blocking_basis", "blocking_basis"),
        ("preflight_policy_assertions_cited", "policy_assertions_cited"),
        ("preflight_policy_assertions_resolved", "policy_assertions_resolved"),
        ("preflight_policy_retraction_candidates", "policy_retraction_candidates"),
        ("preflight_policy_ratification_candidates", "policy_ratification_candidates"),
        ("preflight_policy_blocking_authority", "policy_blocking_authority"),
        ("preflight_policy_adjudication", "policy_adjudication"),
    ):
        applied = _set_if_unset(state, attr, block.get(key)) or applied
    return applied


def _apply_plan_review(state: "CoordinatorState", block: dict[str, Any]) -> bool:
    """Restore plan-review outcome fields onto ``state``.

    Skipped outright when this attempt ran its own plan review: the per-reviewer
    audit builder pairs ``plan_review_durations`` against
    ``plan_review_results`` by attempt, so splicing a prior attempt's durations
    into a state that has its own results would misattribute reviewers.
    """
    if state.plan_review_decision is not None or state.plan_review_results:
        return False
    applied = False
    applied = _set_if_unset(state, "plan_review_decision", block.get("decision")) or applied
    applied = _set_if_unset(state, "plan_review_mode", block.get("mode")) or applied
    applied = (
        _set_if_unset(state, "plan_review_waited_seconds", block.get("waited_seconds")) or applied
    )
    applied = (
        _set_if_unset(state, "plan_agent_review_findings", block.get("agent_review_findings"))
        or applied
    )
    applied = (
        _set_if_unset(state, "plan_match_provenance", block.get("match_provenance")) or applied
    )
    if not state.plan_review_durations and block.get("durations"):
        state.plan_review_durations = [float(d) for d in block["durations"]]
        applied = True
    if not state.plan_regen_count and isinstance(block.get("regen_count"), int):
        state.plan_regen_count = block["regen_count"]
    return applied


def _apply_escalation(state: "CoordinatorState", block: dict[str, Any]) -> bool:
    """Restore the escalate-gate decision and advisory onto ``state``."""
    if state.escalate_decision is not None:
        return False
    applied = False
    applied = _set_if_unset(state, "escalate_decision", block.get("decision")) or applied
    applied = (
        _set_if_unset(state, "escalate_selected_action", block.get("selected_action")) or applied
    )
    applied = (
        _set_if_unset(state, "escalate_declined_action", block.get("declined_action")) or applied
    )
    applied = (
        _set_if_unset(state, "escalate_declined_reason", block.get("declined_reason")) or applied
    )
    applied = _set_if_unset(state, "escalate_reason", block.get("reason")) or applied
    applied = (
        _set_if_unset(state, "escalate_decision_source", block.get("decision_source")) or applied
    )
    applied = (
        _set_if_unset(state, "escalate_timeout_advice", block.get("timeout_advice")) or applied
    )
    applied = _set_if_unset(state, "advisory_report", block.get("advisory_report")) or applied
    applied = (
        _set_if_unset(
            state, "advisory_unavailable_reason", block.get("advisory_unavailable_reason")
        )
        or applied
    )
    applied = (
        _set_if_unset(state, "human_review_waited_seconds", block.get("waited_seconds")) or applied
    )
    if block.get("advisory_generated") and not state.advisory_generated:
        state.advisory_generated = True
        applied = True
    if block.get("advisory_launch_failure") and not state.advisory_launch_failure:
        state.advisory_launch_failure = True
        applied = True
    applied = (
        _set_if_unset(state, "advisory_launch_reason", block.get("advisory_launch_reason"))
        or applied
    )
    return applied


def apply_resume_record_to_state(state: "CoordinatorState", record: dict[str, Any]) -> list[str]:
    """Fill missing phase outputs on ``state`` from ``record``.

    Returns the names of the phases that were actually recovered, in
    ``PHASE_BLOCKS`` order, so the caller can state recovery in the audit rather
    than leaving the reader to infer it.  Never overwrites a value this attempt
    produced: a live judgement always beats a recorded one.
    """
    recovered: list[str] = []

    preflight = record.get("preflight")
    if isinstance(preflight, dict) and _apply_preflight(state, preflight):
        recovered.append("preflight")

    routing = record.get("routing_decision")
    if isinstance(routing, dict) and routing and state.routing_decision is None:
        state.routing_decision = routing
        recovered.append("routing_decision")

    routing_audit = record.get(ROUTING_AUDIT_KEY)
    if isinstance(routing_audit, dict) and routing_audit and not state.complexity_routing_audit:
        # A value this attempt has already derived is never overwritten (the
        # guard above); what lands here is the record's copy being the *only*
        # available one. When the attempt that derived it was degraded, it is
        # carried forward marked as unfounded rather than silently taken as
        # founded, so every later consumer of the figure sees what it rests on.
        provenance = _degraded_provenance(record)
        if provenance is not None:
            routing_audit = _mark_unfounded(routing_audit, provenance)
        state.complexity_routing_audit = routing_audit
        # The story allocation rides inside the routing audit (#2169) so a
        # resumed attempt reports spend against the same basis the first
        # attempt derived, instead of re-deriving against a moved distribution.
        allocation = routing_audit.get("story_allocation")
        if isinstance(allocation, dict) and allocation and not state.story_allocation:
            if provenance is not None:
                allocation = _mark_unfounded(allocation, provenance)
                routing_audit["story_allocation"] = allocation
            state.story_allocation = allocation

    plan_review = record.get("plan_review")
    if isinstance(plan_review, dict) and _apply_plan_review(state, plan_review):
        recovered.append("plan_review")

    escalation = record.get("escalation")
    if isinstance(escalation, dict) and _apply_escalation(state, escalation):
        recovered.append("escalation")

    timeout_escalation = record.get("timeout_escalation")
    if (
        isinstance(timeout_escalation, dict)
        and timeout_escalation
        and state.timeout_escalation_audit is None
    ):
        state.timeout_escalation_audit = timeout_escalation
        recovered.append("timeout_escalation")

    return recovered


def recover_phase_state(
    project_root: Path,
    state: "CoordinatorState",
    *,
    slug: str,
    story_content: str | None,
) -> dict[str, Any]:
    """Load, validate and apply a story's durable phase record.

    Returns the recovery block recorded on ``state.phase_recovery`` and emitted
    as the audit's ``phase_recovery`` key.  ``status`` is one of:

    * ``recovered``   — a usable record filled at least one missing phase.
    * ``unavailable`` — no record on disk for this story.
    * ``rejected``    — a record exists but does not describe this story text.
    * ``not_needed``  — a usable record added nothing this attempt lacked.

    ``reentry_impact`` carries the re-entry analysis when the recovery changes
    which phases run — today, when a recovered escalate-gate decision means the
    outstanding review cycle will not be run.  None otherwise.

    ``block_selection`` carries the record's merge decisions, so the audit and
    the ``phase_recovery`` log event state which attempt's phase output the
    story is resuming from — a save that refused a degraded block, or one that
    was displaced by a better-founded one, is visible there rather than only by
    diffing run directories (#2351).
    """
    record = load_resume_record(project_root, slug)
    if record is None:
        return {
            "status": "unavailable",
            "reason": "no_record",
            "recovered_phases": [],
            "source_run_id": None,
            "reentry_impact": None,
            "block_selection": [],
        }

    usable, reason = validate_resume_record(record, story_content=story_content)
    if not usable:
        return {
            "status": "rejected",
            "reason": reason,
            "recovered_phases": [],
            "source_run_id": record.get("run_id"),
            "reentry_impact": None,
            "block_selection": [],
        }

    recovered = apply_resume_record_to_state(state, record)
    return {
        "status": "recovered" if recovered else "not_needed",
        "reason": reason,
        "recovered_phases": recovered,
        "source_run_id": record.get("run_id"),
        "recorded_phases": [name for name in PHASE_BLOCKS if record.get(name)],
        "reentry_impact": _reentry_impact(record, recovered),
        "block_selection": block_selection(record),
    }


def block_selection(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the merge's block-selection decisions for ``record``.

    One derivation, read by ``recover_phase_state`` and available to any status
    surface that wants to report what a resume would proceed from without
    starting a run.
    """
    if not isinstance(record, dict):
        return []
    entries = record.get(RESUME_SELECTION_KEY)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]
