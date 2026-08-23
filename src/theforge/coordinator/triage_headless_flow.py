"""Headless ``forge triage``: propose, review, and persist — never apply.

A triage run with nobody at the keyboard (a cron entry, a post-sprint pass, any
non-interactive invocation) stops at the reviewed-proposal stage and writes the
whole package to ``.forge/pending`` as a durable operator decision. The safety
property is inherited rather than newly asserted: application already requires
:func:`~theforge.coordinator.triage_ratification_flow.ratify_triage_run`, which
prompts, so a run that cannot prompt can only propose and persist.

Two decisions live here and neither is delegated to a model:

* **An unresolved triage decision blocks a new run.** Proposing again over a
  backlog whose last proposal nobody has looked at spends money to bury the
  package the operator has yet to act on, so the second run refuses and names
  the first.
* **Nothing is applied.** This module never calls ratification, never touches a
  tracker, and never writes a ``decision`` field. The only mutation it makes
  outside the audit substrate is the pending artifact itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from theforge import pending as _pending

from . import util as _cu

if TYPE_CHECKING:
    from theforge.triage_proposal import ProposalRunSummary

_log = _cu._log

#: Outcome discriminators for :class:`HeadlessTriageOutcome`.
HEADLESS_WRITTEN = "written"
HEADLESS_SUPERSEDED = "superseded"
HEADLESS_FAILED = "failed"


class HeadlessTriageError(RuntimeError):
    """A headless triage pass could not produce a pending decision."""


@dataclass(frozen=True)
class HeadlessTriageOutcome:
    """What one headless triage pass produced, in operator-readable terms."""

    status: str
    message: str
    triage_run_id: str = ""
    pending_id: str = ""
    findings_count: int = 0
    flagged_count: int = 0
    existing_pending_id: str = ""
    error: str = ""
    lines: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status != HEADLESS_FAILED


def _config_current_milestone(config: object) -> str | None:
    milestone = getattr(
        getattr(getattr(config, "conventions_advisory", None), "issue_filing", None),
        "milestone",
        None,
    )
    if isinstance(milestone, str) and milestone.strip():
        return milestone.strip()
    return None


def superseded_outcome(existing: dict[str, Any]) -> HeadlessTriageOutcome:
    """The refusal a run returns when an unresolved decision already exists."""
    pending_id = str(existing.get("run_id") or "")
    triage_run_id = str(existing.get("triage_run_id") or "")
    created_at = str(existing.get("created_at") or "?")
    findings = int(existing.get("findings_count") or 0)
    message = (
        f"an unresolved triage decision is already pending: {pending_id} "
        f"({findings} finding(s), created {created_at}); resolve it with "
        f"'forge triage --ratify {triage_run_id or pending_id}' or discard it with "
        f"'forge triage --discard {pending_id}' before proposing again"
    )
    return HeadlessTriageOutcome(
        status=HEADLESS_SUPERSEDED,
        message=message,
        triage_run_id=triage_run_id,
        pending_id=pending_id,
        findings_count=findings,
        flagged_count=int(existing.get("flagged_count") or 0),
        existing_pending_id=pending_id,
        lines=(f"triage: no new proposal pass — {message}",),
    )


def _recorded_events(project_root: Path, triage_run_id: str) -> list[dict[str, Any]]:
    """Reload the proposal events this run recorded, degrading to empty.

    The pending artifact carries what was *recorded*, not what was returned in
    memory: ratification reads the substrate, so a package built from anything
    else could describe a run ratification would not recognise. An empty result
    is not interpreted here — :func:`run_headless_triage` decides what it means
    for the run that asked, because "no events" is correct for an empty backlog
    and a broken promise for anything else.
    """
    from . import audit_read_model, audit_storage  # noqa: PLC0415

    try:
        conn = audit_storage.open_readonly(project_root)
    except audit_storage.SubstrateError:
        return []
    try:
        return [
            dict(event)
            for event in audit_read_model.iter_triage_proposal_events(
                conn, triage_run_id=triage_run_id
            )
        ]
    except Exception:  # noqa: BLE001 - the caller decides what an empty reload means
        return []
    finally:
        conn.close()


def _unratifiable_reason(summary: "ProposalRunSummary", events: list[dict[str, Any]]) -> str:
    """Why this run's package could not be ratified later, or "" if it can.

    A pending triage decision makes exactly one promise: an operator can come
    back to it and ``forge triage --ratify`` will apply what was reviewed. That
    promise rests entirely on the audit substrate — ``ratify_triage_run`` reads
    the recorded run and its proposal events, and refuses a run it cannot find.
    So a package whose recording did not survive is not a decision the operator
    can act on; publishing it anyway would put an unresolvable record on the
    status surface and spend the operator's attention discovering that at the
    ratify prompt rather than here (#2231, review cycle 1).

    Both halves of the recording are checked, because they fail separately: the
    writer can report an error, and it can report none while the reload comes
    back empty (an unreadable substrate, a failed open). The empty reload is a
    problem only for a run that actually proposed something — an empty backlog
    records no events by construction and ratifies as an empty run.
    """
    if summary.audit_error:
        return (
            f"the proposal run could not be recorded in the audit substrate: {summary.audit_error}"
        )
    if summary.findings_count > 0 and not events:
        return (
            "the recorded proposal events for this run could not be read back from the "
            "audit substrate"
        )
    return ""


def _proposal_entries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for event in events:
        proposal = event.get("proposal")
        proposal_map = proposal if isinstance(proposal, dict) else {}
        entries.append(
            {
                "finding_id": str(event.get("finding_id") or ""),
                "issue_ref": str(event.get("issue_ref") or ""),
                "disposition": str(
                    event.get("disposition") or proposal_map.get("disposition") or ""
                ),
                "target_milestone": event.get("target_milestone")
                or proposal_map.get("target_milestone"),
                "punt_reason_code": event.get("punt_reason_code")
                or proposal_map.get("punt_reason_code"),
                "evidence_refs": [
                    str(ref)
                    for ref in (
                        event.get("evidence_refs") or proposal_map.get("evidence_refs") or []
                    )
                ],
                "rationale": str(proposal_map.get("rationale") or ""),
            }
        )
    return entries


def _punt_review_entries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for event in events:
        review = event.get("punt_review")
        if not isinstance(review, dict):
            continue
        entries.append(
            {
                "finding_id": str(event.get("finding_id") or ""),
                "issue_ref": str(event.get("issue_ref") or ""),
                "verdict": str(review.get("verdict") or ""),
                "evidence_refs": [str(ref) for ref in (review.get("evidence_refs") or [])],
                "rationale": str(review.get("rationale") or ""),
            }
        )
    return entries


def _evidence_refs(proposals: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for entry in (*proposals, *reviews):
        for ref in entry.get("evidence_refs") or []:
            if ref and ref not in seen:
                seen.append(ref)
    return seen


def build_pending_payload(
    summary: "ProposalRunSummary",
    *,
    events: list[dict[str, Any]],
    report_path: str,
) -> dict[str, Any]:
    """Assemble the reviewed package a persisted triage decision carries."""
    proposals = _proposal_entries(events)
    punt_reviews = _punt_review_entries(events)
    review_stage = summary.review_stage
    return {
        "story": "triage",
        "phase": "TRIAGE",
        "options": [],
        "report_path": report_path or summary.report_path,
        "findings_count": summary.findings_count,
        "flagged_count": review_stage.challenged_punt_count,
        "reviewed_punt_count": review_stage.reviewed_punt_count,
        "total_cost_usd": summary.total_cost_usd,
        "cost_provenance": summary.cost_provenance,
        "audit_error": summary.audit_error,
        "run_summary": summary.to_dict(),
        "proposals": proposals,
        "punt_reviews": punt_reviews,
        "evidence_refs": _evidence_refs(proposals, punt_reviews),
        "reason": (
            f"headless triage run {summary.triage_run_id}: {summary.findings_count} finding(s) "
            f"proposed and reviewed, awaiting operator ratification"
        ),
    }


def _dispatch_failed_outcome(summary: "ProposalRunSummary") -> HeadlessTriageOutcome:
    """Return the headless outcome for a run that never reached agent dispatch."""
    message = summary.run_level_failure
    return HeadlessTriageOutcome(
        status=HEADLESS_FAILED,
        message=message,
        error=message,
        triage_run_id=summary.triage_run_id,
        findings_count=summary.findings_count,
        lines=(f"triage: {message}",),
    )


def run_headless_triage(
    config: object,
    *,
    project_root: Path | None = None,
    report: object | None = None,
    current_milestone: str | None = None,
    output_path: Path | None = None,
) -> HeadlessTriageOutcome:
    """Run the proposal + punt-review stages and persist the result.

    ``report`` is an already-loaded :class:`~theforge.triage_report.BacklogReport`
    when the caller supplied one (``forge triage --report``); otherwise the
    backlog is collected and written here first, so a single non-interactive
    invocation produces both the report artifact and the pending decision.

    Never prompts, never blocks on input, never applies a disposition.
    """
    from theforge.triage_backlog_report import (  # noqa: PLC0415
        TriageBacklogReportError,
        collect_backlog_report,
        write_backlog_report,
    )
    from theforge.triage_report import BacklogReportError, load_backlog_report  # noqa: PLC0415

    from .triage_proposal_flow import run_triage_proposals  # noqa: PLC0415

    root = Path(project_root or getattr(config, "project_root"))

    existing = _pending.unresolved_triage_pending(root)
    if existing is not None:
        outcome = superseded_outcome(existing)
        _log(f"  {outcome.lines[0]}")
        return outcome

    effective_milestone = current_milestone or _config_current_milestone(config)

    if report is None:
        try:
            collected = collect_backlog_report(root, current_milestone=effective_milestone)
            artifact_path = write_backlog_report(root, collected, output_path=output_path)
            report = load_backlog_report(artifact_path)
        except (TriageBacklogReportError, BacklogReportError) as exc:
            raise HeadlessTriageError(f"backlog report could not be produced: {exc}") from exc

    summary = run_triage_proposals(
        report,
        config,
        project_root=root,
        current_milestone=current_milestone,
        record=True,
    )
    if summary.run_level_failure:
        outcome = _dispatch_failed_outcome(summary)
        _log(f"  {outcome.lines[0]}")
        return outcome

    events = _recorded_events(root, summary.triage_run_id)

    # Checked BEFORE the artifact is written: an unratifiable package must not
    # reach the operator's status surface at all. The spend is named because it
    # already happened — the run is lost, and saying so is the difference
    # between "nothing was proposed" and "proposals were made and could not be
    # kept".
    unratifiable = _unratifiable_reason(summary, events)
    if unratifiable:
        raise HeadlessTriageError(
            f"triage run {summary.triage_run_id} proposed {summary.findings_count} finding(s) "
            f"but {unratifiable}; no pending decision was written because it could not be "
            "ratified later"
        )

    payload = build_pending_payload(
        summary,
        events=events,
        report_path=getattr(report, "source_path", "") or "",
    )
    _pending.write_triage_pending(summary.triage_run_id, payload, project_root=root)
    pending_id = _pending.triage_pending_id(summary.triage_run_id)

    findings = summary.findings_count
    flagged = summary.review_stage.challenged_punt_count
    reviewed = summary.review_stage.reviewed_punt_count
    punt_text = (
        f" ({reviewed} punt, {'concurred' if flagged == 0 else f'{flagged} challenged'})"
        if reviewed
        else ""
    )
    lines = (
        f"triage: proposal pass proposed {findings} disposition(s){punt_text}",
        (
            f"triage: pending operator decision written — {pending_id}; "
            f"resolve with 'forge triage --ratify {summary.triage_run_id}'"
        ),
    )
    for line in lines:
        _log(f"  {line}")

    return HeadlessTriageOutcome(
        status=HEADLESS_WRITTEN,
        message=f"pending triage decision {pending_id} written",
        triage_run_id=summary.triage_run_id,
        pending_id=pending_id,
        findings_count=findings,
        flagged_count=flagged,
        lines=lines,
    )


__all__ = [
    "HEADLESS_FAILED",
    "HEADLESS_SUPERSEDED",
    "HEADLESS_WRITTEN",
    "HeadlessTriageError",
    "HeadlessTriageOutcome",
    "build_pending_payload",
    "run_headless_triage",
    "superseded_outcome",
]
