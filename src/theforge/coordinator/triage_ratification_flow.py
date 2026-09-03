"""Operator-driven ratification and application for ``forge triage``."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from theforge.coordinator import audit_read_model, audit_storage
from theforge.spike_guard import check_spike_closure
from theforge.triage_backlog_report import (
    collect_backlog_report,
    fetch_open_milestones,
)
from theforge.triage_proposal import (
    PUNT_REASON_CODES,
    FindingPacket,
    PacketEvidence,
    validate_disposition_payload,
)
from theforge.triage_ratification import (
    DECISION_ACCEPT,
    DECISION_OVERRIDE,
    DECISION_SKIP,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_RATIFIED,
    STATUS_SKIPPED,
    STATUS_STALE,
    OperatorChoice,
    RatificationFindingOutcome,
    RatificationSummary,
    render_reviewed_proposal,
)
from theforge.triage_snapshot import canonicalize_finding_snapshot

_GH_TIMEOUT_SECONDS = 30


class TriageRatificationError(RuntimeError):
    """The operator ratification/application flow could not proceed."""


def _config_current_milestone(config: object) -> str | None:
    milestone = getattr(
        getattr(getattr(config, "conventions_advisory", None), "issue_filing", None),
        "milestone",
        None,
    )
    if isinstance(milestone, str) and milestone.strip():
        return milestone.strip()
    return None


def _packet_from_event(event: Mapping[str, object]) -> FindingPacket:
    raw_packet = event.get("packet")
    if not isinstance(raw_packet, Mapping):
        raise TriageRatificationError(
            f"triage run {event.get('triage_run_id')}: finding {event.get('finding_id')} "
            "has no stored packet; proposal runs recorded before snapshot support "
            "cannot be ratified"
        )
    evidence_entries: list[PacketEvidence] = []
    for raw_entry in raw_packet.get("evidence") or []:
        if not isinstance(raw_entry, Mapping):
            continue
        evidence_entries.append(
            PacketEvidence(
                evidence_id=str(raw_entry.get("id") or raw_entry.get("evidence_id") or "").strip(),
                kind=str(raw_entry.get("kind") or "report").strip(),
                summary=str(raw_entry.get("summary") or "").strip(),
                checkable=bool(raw_entry.get("checkable", False)),
                detail=str(raw_entry.get("detail") or "").strip(),
            )
        )
    named_milestones = raw_packet.get("named_milestones") or []
    if isinstance(named_milestones, str):
        named_milestones = [named_milestones]
    disposition_history = raw_packet.get("disposition_history") or []
    if not isinstance(disposition_history, list):
        disposition_history = []
    return FindingPacket(
        finding_id=str(raw_packet.get("finding_id") or event.get("finding_id") or "").strip(),
        issue_ref=str(raw_packet.get("issue_ref") or event.get("issue_ref") or "").strip(),
        finding_body=str(raw_packet.get("finding_body") or "").strip(),
        evidence=tuple(entry for entry in evidence_entries if entry.evidence_id),
        current_milestone=(str(raw_packet.get("current_milestone") or "").strip() or None),
        named_milestones=tuple(
            str(item).strip() for item in named_milestones if str(item).strip()
        ),
        disposition_history=tuple(item for item in disposition_history if isinstance(item, dict)),
    )


def _proposed_payload(event: Mapping[str, object]) -> dict[str, object]:
    proposal = event.get("proposal")
    if not isinstance(proposal, Mapping):
        raise TriageRatificationError(
            f"triage run {event.get('triage_run_id')}: finding {event.get('finding_id')} "
            "has no stored proposal payload"
        )
    return {
        "disposition": str(proposal.get("disposition") or "").strip(),
        "target_milestone": str(proposal.get("target_milestone") or "").strip() or None,
        "punt_reason_code": str(proposal.get("punt_reason_code") or "").strip() or None,
        "evidence_refs": tuple(
            str(ref).strip() for ref in (proposal.get("evidence_refs") or []) if str(ref).strip()
        ),
        "citations": tuple(
            item for item in (proposal.get("citations") or []) if isinstance(item, Mapping)
        ),
    }


def _issue_number_from_snapshot(snapshot: Mapping[str, object]) -> int | None:
    raw = snapshot.get("issue_number")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _issue_number_from_issue_ref(issue_ref: str) -> int | None:
    text = str(issue_ref or "").strip()
    if not text.startswith("#"):
        return None
    try:
        return int(text[1:])
    except ValueError:
        return None


def _comment_quotes(
    packet: FindingPacket,
    *,
    evidence_refs: tuple[str, ...],
    proposal_citations: tuple[Mapping[str, object], ...],
) -> list[tuple[str, str]]:
    quotes_by_ref = {
        str(item.get("ref") or "").strip(): str(item.get("quote") or "").strip()
        for item in proposal_citations
        if str(item.get("ref") or "").strip()
    }
    evidence_by_id = {entry.evidence_id: entry for entry in packet.evidence}
    quoted: list[tuple[str, str]] = []
    for ref in evidence_refs:
        entry = evidence_by_id.get(ref)
        if entry is None:
            continue
        quote = quotes_by_ref.get(ref) or entry.summary or entry.citable_text()
        quoted.append((ref, quote))
    return quoted


def _idempotency_marker(triage_run_id: str, finding_id: str) -> str:
    return f"forge-triage:{triage_run_id}:{finding_id}"


def _canonical_refs(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [item.strip() for item in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _valid_evidence_refs(packet: FindingPacket, refs: tuple[str, ...]) -> tuple[str, ...]:
    if not refs:
        return ()
    valid = set(packet.evidence_ids())
    return tuple(ref for ref in refs if ref in valid)


def _proposal_citations(event: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return _proposed_payload(event)["citations"]  # type: ignore[return-value]


def _renderable_quotes(
    event: Mapping[str, object],
    *,
    packet: FindingPacket,
    evidence_refs: tuple[str, ...],
) -> list[tuple[str, str]]:
    return [
        (ref, quote.strip())
        for ref, quote in _comment_quotes(
            packet,
            evidence_refs=evidence_refs,
            proposal_citations=_proposal_citations(event),
        )
        if quote.strip()
    ]


def _choice_errors(
    event: Mapping[str, object],
    *,
    packet: FindingPacket,
    choice: OperatorChoice,
) -> tuple[str, ...]:
    errors = list(
        validate_disposition_payload(
            packet,
            disposition=choice.disposition or "",
            target_milestone=choice.target_milestone,
            punt_reason_code=choice.punt_reason_code,
        )
    )
    if choice.disposition == "punt" and not _renderable_quotes(
        event,
        packet=packet,
        evidence_refs=choice.evidence_refs,
    ):
        errors.append(
            "punt requires at least one cited evidence ref whose packet text can be rendered "
            "into the closing comment"
        )
    return tuple(errors)


def _legacy_stale_reason(event: Mapping[str, object]) -> str:
    has_packet = isinstance(event.get("packet"), Mapping)
    has_snapshot = isinstance(event.get("finding_snapshot"), Mapping)
    if has_packet and has_snapshot:
        return ""
    if not has_packet and not has_snapshot:
        return "proposal run predates stored finding packets and snapshots; fail closed"
    if not has_packet:
        return "proposal run predates stored finding packets; fail closed"
    return "proposal run predates stored finding snapshots; fail closed"


def _prompt_choice(
    event: Mapping[str, object],
    *,
    input_fn: Callable[[str], str],
    emit: Callable[[str], None],
) -> OperatorChoice:
    packet = _packet_from_event(event)
    proposal = _proposed_payload(event)
    default_refs = (
        proposal["evidence_refs"] if isinstance(proposal["evidence_refs"], tuple) else ()
    )

    while True:
        emit("")
        emit(render_reviewed_proposal(event))
        action = input_fn("Decision [accept/override/skip]: ").strip().lower()
        if action == DECISION_ACCEPT:
            note = input_fn("Operator note (optional): ").strip()
            choice = OperatorChoice(
                decision=DECISION_ACCEPT,
                disposition=str(proposal["disposition"]),
                target_milestone=proposal["target_milestone"],  # type: ignore[arg-type]
                punt_reason_code=proposal["punt_reason_code"],  # type: ignore[arg-type]
                evidence_refs=_valid_evidence_refs(packet, default_refs),
                operator_note=note,
            )
            errors = _choice_errors(event, packet=packet, choice=choice)
            if errors:
                for error in errors:
                    emit(error)
                continue
            return choice
        if action == DECISION_SKIP:
            note = input_fn("Skip note (optional): ").strip()
            return OperatorChoice(
                decision=DECISION_SKIP,
                operator_note=note,
            )
        if action != DECISION_OVERRIDE:
            emit("Enter accept, override, or skip.")
            continue

        disposition = input_fn(
            "Override disposition [fix_now/fix_later/punt/needs_verification]: "
        ).strip()
        target_milestone: str | None = None
        punt_reason_code: str | None = None
        if disposition == "fix_now":
            default_target = packet.current_milestone or ""
            prompt = (
                f"Target milestone [{default_target}]: "
                if default_target
                else "Target milestone: "
            )
            target = input_fn(prompt).strip()
            target_milestone = target or default_target or None
        elif disposition == "fix_later":
            allowed = ", ".join(packet.fix_later_targets())
            target_milestone = input_fn(f"Target milestone [{allowed}]: ").strip() or None
        elif disposition == "punt":
            allowed = ", ".join(PUNT_REASON_CODES)
            punt_reason_code = input_fn(f"Punt reason code [{allowed}]: ").strip() or None

        refs_prompt = (
            f"Evidence refs [{', '.join(default_refs)}]: "
            if default_refs
            else f"Evidence refs [{', '.join(packet.evidence_ids())}]: "
        )
        entered_refs = _canonical_refs(input_fn(refs_prompt).strip())
        evidence_refs = entered_refs or _valid_evidence_refs(packet, default_refs)
        if entered_refs and len(_valid_evidence_refs(packet, entered_refs)) != len(entered_refs):
            emit("Override cites unknown evidence refs; use ids from the stored packet.")
            continue
        note = input_fn("Operator note (optional): ").strip()
        choice = OperatorChoice(
            decision=DECISION_OVERRIDE,
            disposition=disposition,
            target_milestone=target_milestone,
            punt_reason_code=punt_reason_code,
            evidence_refs=evidence_refs,
            operator_note=note,
        )
        errors = _choice_errors(event, packet=packet, choice=choice)
        if errors:
            for error in errors:
                emit(error)
            continue
        return choice


def _gh_issue_view(number: int, project_root: Path) -> dict:
    proc = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--json",
            "number,title,body,state,labels,milestone,comments",
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=_GH_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "gh issue view failed"
        raise TriageRatificationError(f"gh issue view #{number} failed: {err}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise TriageRatificationError(
            f"gh issue view #{number} returned malformed JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise TriageRatificationError(f"gh issue view #{number} returned a non-mapping response")
    return data


def _gh_issue_comment(number: int, body: str, project_root: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        tmp_path = handle.name
    try:
        proc = subprocess.run(
            ["gh", "issue", "comment", str(number), "--body-file", tmp_path],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=_GH_TIMEOUT_SECONDS,
            check=False,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "gh issue comment failed"
        raise TriageRatificationError(f"gh issue comment #{number} failed: {err}")


def _gh_issue_edit_milestone(number: int, milestone: str, project_root: Path) -> None:
    proc = subprocess.run(
        ["gh", "issue", "edit", str(number), "--milestone", milestone],
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=_GH_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "gh issue edit failed"
        raise TriageRatificationError(f"gh issue edit #{number} failed: {err}")


def _gh_issue_close(number: int, project_root: Path) -> None:
    # Ratification is a repository-controlled close path, so it asks the same
    # spike guard every other one does (#2600). A spike with no recorded
    # outcome fails the ratification action rather than closing silently.
    decision = check_spike_closure(number, project_root)
    if not decision.allowed:
        raise TriageRatificationError(f"refusing to close #{number}: {decision.reason}")
    proc = subprocess.run(
        ["gh", "issue", "close", str(number)],
        capture_output=True,
        text=True,
        cwd=str(project_root),
        timeout=_GH_TIMEOUT_SECONDS,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or "gh issue close failed"
        raise TriageRatificationError(f"gh issue close #{number} failed: {err}")


def _live_finding_map(config: object, project_root: Path) -> dict[tuple[str, str], object]:
    report = collect_backlog_report(
        project_root,
        current_milestone=_config_current_milestone(config),
    )
    indexed: dict[tuple[str, str], object] = {}
    for finding in report.findings:
        if finding.issue_number is not None:
            indexed[("issue_number", str(finding.issue_number))] = finding
        indexed[("issue_ref", finding.issue_ref)] = finding
    return indexed


def _current_snapshot(
    event: Mapping[str, object],
    live_findings: Mapping[tuple[str, str], object],
) -> object | None:
    snapshot = event.get("finding_snapshot")
    snapshot_map = snapshot if isinstance(snapshot, Mapping) else {}
    issue_number = _issue_number_from_snapshot(snapshot_map)
    if issue_number is not None:
        current = live_findings.get(("issue_number", str(issue_number)))
        if current is not None:
            return current
    issue_ref = str(snapshot_map.get("issue_ref") or event.get("issue_ref") or "").strip()
    if issue_ref:
        return live_findings.get(("issue_ref", issue_ref))
    return None


def _stale_reason(
    event: Mapping[str, object],
    *,
    live_findings: Mapping[tuple[str, str], object],
    live_issue: Mapping[str, object],
) -> str:
    legacy = _legacy_stale_reason(event)
    if legacy:
        return legacy
    snapshot = canonicalize_finding_snapshot(event.get("finding_snapshot"))
    current = _current_snapshot(event, live_findings)
    if str(live_issue.get("state") or "").upper() == "CLOSED":
        return "issue closed after proposal review"
    if current is None:
        return "issue no longer appears in the finding backlog (labels or pool changed)"
    current_snapshot = canonicalize_finding_snapshot(current.snapshot_dict())
    if snapshot != current_snapshot:
        return "live finding state diverged from the reviewed snapshot"
    return ""


def _closing_comment_body(
    *,
    triage_run_id: str,
    finding_id: str,
    reason_code: str,
    quotes: list[tuple[str, str]],
    operator_note: str,
) -> str:
    marker = _idempotency_marker(triage_run_id, finding_id)
    lines = [
        "forge triage ratification",
        f"Marker: {marker}",
        f"Reason code: {reason_code}",
        "Evidence:",
    ]
    for ref, quote in quotes:
        lines.append(f'- {ref}: "{quote}"')
    lines.append(f"Operator note: {operator_note or 'none provided'}")
    return "\n".join(lines)


def _upsert_application(
    project_root: Path,
    *,
    event: Mapping[str, object],
    choice: OperatorChoice,
    status: str,
    stale_reason: str = "",
    external_effect_summary: str = "",
    applied_at: str | None = None,
    emitted_at: str | None = None,
) -> dict[str, object]:
    record = {
        "triage_run_id": str(event.get("triage_run_id") or ""),
        "finding_id": str(event.get("finding_id") or ""),
        "issue_ref": str(event.get("issue_ref") or ""),
        "proposed_payload": dict(_proposed_payload(event)),
        "operator_decision": choice.decision,
        "applied_disposition": choice.disposition,
        "target_milestone": choice.target_milestone,
        "punt_reason_code": choice.punt_reason_code,
        "evidence_refs": list(choice.evidence_refs),
        "operator_note": choice.operator_note,
        "status": status,
        "stale_reason": stale_reason,
        "idempotency_marker": _idempotency_marker(
            str(event.get("triage_run_id") or ""),
            str(event.get("finding_id") or ""),
        ),
        "external_effect_summary": external_effect_summary,
        "emitted_at": emitted_at or audit_storage._now_iso(),
        "applied_at": applied_at,
        "packet": event.get("packet"),
        "finding_snapshot": event.get("finding_snapshot"),
        "finding_snapshot_digest": event.get("finding_snapshot_digest"),
    }
    audit_storage.upsert_triage_application_record(project_root, record)
    return record


def _apply_choice(
    project_root: Path,
    *,
    event: Mapping[str, object],
    row: Mapping[str, object],
    live_findings: Mapping[tuple[str, str], object],
    open_milestones: tuple[str, ...],
) -> RatificationFindingOutcome:
    packet = _packet_from_event(event)
    choice = OperatorChoice(
        decision=str(row.get("operator_decision") or ""),
        disposition=str(row.get("applied_disposition") or "").strip() or None,
        target_milestone=str(row.get("target_milestone") or "").strip() or None,
        punt_reason_code=str(row.get("punt_reason_code") or "").strip() or None,
        evidence_refs=_canonical_refs(row.get("evidence_refs")),
        operator_note=str(row.get("operator_note") or "").strip(),
    )
    issue_ref = str(event.get("issue_ref") or event.get("finding_id") or "?")
    snapshot = event.get("finding_snapshot")
    snapshot_map = snapshot if isinstance(snapshot, Mapping) else {}
    try:
        issue_number = _issue_number_from_snapshot(snapshot_map)
        if issue_number is None:
            issue_number = _issue_number_from_issue_ref(issue_ref)
        if issue_number is None:
            raise TriageRatificationError(
                f"triage run {event.get('triage_run_id')}: finding {event.get('finding_id')} "
                "has no usable issue number"
            )
        live_issue = _gh_issue_view(issue_number, project_root)
        current_milestone = (
            (live_issue.get("milestone") or {}).get("title")
            if isinstance(live_issue.get("milestone"), Mapping)
            else None
        )
        marker = _idempotency_marker(
            str(event.get("triage_run_id") or ""),
            str(event.get("finding_id") or ""),
        )
        comments = live_issue.get("comments") or []
        has_comment = any(
            marker in str(comment.get("body") or "")
            for comment in comments
            if isinstance(comment, Mapping)
        )
        issue_closed = str(live_issue.get("state") or "").upper() == "CLOSED"
        if choice.disposition == "punt" and issue_closed and has_comment:
            summary = "Closing comment already posted and issue already closed."
            _upsert_application(
                project_root,
                event=event,
                choice=choice,
                status=STATUS_APPLIED,
                external_effect_summary=summary,
                applied_at=audit_storage._now_iso(),
                emitted_at=str(row.get("emitted_at") or "").strip() or None,
            )
            return RatificationFindingOutcome(
                finding_id=str(event.get("finding_id") or ""),
                issue_ref=issue_ref,
                decision=choice.decision,
                status=STATUS_APPLIED,
                disposition=choice.disposition,
                target_milestone=choice.target_milestone,
                punt_reason_code=choice.punt_reason_code,
                summary=summary,
            )
        stale = _stale_reason(event, live_findings=live_findings, live_issue=live_issue)
        if stale:
            _upsert_application(
                project_root,
                event=event,
                choice=choice,
                status=STATUS_STALE,
                stale_reason=stale,
                external_effect_summary="No mutation applied.",
                applied_at=audit_storage._now_iso(),
                emitted_at=str(row.get("emitted_at") or "").strip() or None,
            )
            return RatificationFindingOutcome(
                finding_id=str(event.get("finding_id") or ""),
                issue_ref=issue_ref,
                decision=choice.decision,
                status=STATUS_STALE,
                disposition=choice.disposition,
                target_milestone=choice.target_milestone,
                punt_reason_code=choice.punt_reason_code,
                summary="Skipped instead of applying.",
                stale_reason=stale,
            )
        if choice.disposition in {"fix_now", "fix_later"} and choice.target_milestone:
            if str(current_milestone or "").strip().lower() == choice.target_milestone.lower():
                summary = f"Milestone already {choice.target_milestone}; no edit needed."
                _upsert_application(
                    project_root,
                    event=event,
                    choice=choice,
                    status=STATUS_APPLIED,
                    external_effect_summary=summary,
                    applied_at=audit_storage._now_iso(),
                    emitted_at=str(row.get("emitted_at") or "").strip() or None,
                )
                return RatificationFindingOutcome(
                    finding_id=str(event.get("finding_id") or ""),
                    issue_ref=issue_ref,
                    decision=choice.decision,
                    status=STATUS_APPLIED,
                    disposition=choice.disposition,
                    target_milestone=choice.target_milestone,
                    punt_reason_code=choice.punt_reason_code,
                    summary=summary,
                )

        disposition = choice.disposition or ""
        if disposition == "needs_verification":
            summary = "Recorded operator decision only; no tracker mutation."
        elif disposition in {"fix_now", "fix_later"}:
            target = choice.target_milestone or ""
            if target not in open_milestones:
                raise TriageRatificationError(
                    f"target milestone {target!r} is not open on the repository; "
                    f"create it before ratifying this finding"
                )
            _gh_issue_edit_milestone(issue_number, target, project_root)
            summary = f"Set milestone to {target}."
        elif disposition == "punt":
            reason_code = choice.punt_reason_code or ""
            if reason_code not in PUNT_REASON_CODES:
                raise TriageRatificationError(
                    f"punt requires a recognized reason code: {list(PUNT_REASON_CODES)}"
                )
            quotes = _renderable_quotes(
                event,
                packet=packet,
                evidence_refs=choice.evidence_refs,
            )
            if not quotes:
                raise TriageRatificationError(
                    "punt requires at least one cited evidence ref whose packet text can be "
                    "rendered into the closing comment"
                )
            if not has_comment:
                _gh_issue_comment(
                    issue_number,
                    _closing_comment_body(
                        triage_run_id=str(event.get("triage_run_id") or ""),
                        finding_id=str(event.get("finding_id") or ""),
                        reason_code=reason_code,
                        quotes=quotes,
                        operator_note=choice.operator_note,
                    ),
                    project_root,
                )
            if not issue_closed:
                _gh_issue_close(issue_number, project_root)
            summary = "Posted closing comment and closed the issue."
        else:
            raise TriageRatificationError(f"unsupported ratified disposition {disposition!r}")
    except TriageRatificationError as exc:
        _upsert_application(
            project_root,
            event=event,
            choice=choice,
            status=STATUS_FAILED,
            external_effect_summary=str(exc),
            emitted_at=str(row.get("emitted_at") or "").strip() or None,
        )
        return RatificationFindingOutcome(
            finding_id=str(event.get("finding_id") or ""),
            issue_ref=issue_ref,
            decision=choice.decision,
            status=STATUS_FAILED,
            disposition=choice.disposition,
            target_milestone=choice.target_milestone,
            punt_reason_code=choice.punt_reason_code,
            summary=str(exc),
        )

    _upsert_application(
        project_root,
        event=event,
        choice=choice,
        status=STATUS_APPLIED,
        external_effect_summary=summary,
        applied_at=audit_storage._now_iso(),
        emitted_at=str(row.get("emitted_at") or "").strip() or None,
    )
    return RatificationFindingOutcome(
        finding_id=str(event.get("finding_id") or ""),
        issue_ref=issue_ref,
        decision=choice.decision,
        status=STATUS_APPLIED,
        disposition=choice.disposition,
        target_milestone=choice.target_milestone,
        punt_reason_code=choice.punt_reason_code,
        summary=summary,
    )


def ratify_triage_run(
    triage_run_id: str,
    config: object,
    *,
    project_root: Path | None = None,
    input_fn: Callable[[str], str] = input,
    emit: Callable[[str], None] = print,
) -> RatificationSummary:
    """Prompt for ratification, persist the operator decision, and apply it."""
    root = project_root or getattr(config, "project_root")
    try:
        conn = audit_storage.open_readonly(root)
    except audit_storage.SubstrateError as exc:
        raise TriageRatificationError(
            f"triage run {triage_run_id} was not found in the audit substrate; "
            "a proposal run invoked with --no-audit cannot be ratified later"
        ) from exc
    try:
        run = audit_read_model.load_triage_proposal_run(conn, triage_run_id)
        events = list(
            audit_read_model.iter_triage_proposal_events(
                conn,
                triage_run_id=triage_run_id,
            )
        )
        prior_rows = {
            str(row.get("finding_id") or ""): row
            for row in audit_read_model.iter_triage_application_records(
                conn, triage_run_id=triage_run_id
            )
        }
    finally:
        conn.close()

    if run is None and not events:
        raise TriageRatificationError(
            f"triage run {triage_run_id} was not found in the audit substrate; "
            "a proposal run invoked with --no-audit cannot be ratified later"
        )
    if run is not None and int(run.get("findings_count") or 0) > 0 and not events:
        raise TriageRatificationError(
            f"triage run {triage_run_id} has a recorded summary but no proposal events"
        )

    for event in events:
        finding_id = str(event.get("finding_id") or "")
        existing = prior_rows.get(finding_id)
        if existing is not None:
            continue
        legacy = _legacy_stale_reason(event)
        if legacy:
            stored = _upsert_application(
                root,
                event=event,
                choice=OperatorChoice(decision=DECISION_SKIP),
                status=STATUS_STALE,
                stale_reason=legacy,
                external_effect_summary="No mutation applied.",
                applied_at=audit_storage._now_iso(),
            )
            prior_rows[finding_id] = stored
            continue
        choice = _prompt_choice(event, input_fn=input_fn, emit=emit)
        if choice.decision == DECISION_SKIP:
            stored = _upsert_application(
                root,
                event=event,
                choice=choice,
                status=STATUS_SKIPPED,
                external_effect_summary="Operator skipped this finding.",
                applied_at=audit_storage._now_iso(),
            )
        else:
            stored = _upsert_application(
                root,
                event=event,
                choice=choice,
                status=STATUS_RATIFIED,
                external_effect_summary="Awaiting application.",
            )
        prior_rows[finding_id] = stored

    live_findings = _live_finding_map(config, root)
    open_milestones = fetch_open_milestones(root)
    outcomes: list[RatificationFindingOutcome] = []
    for event in events:
        finding_id = str(event.get("finding_id") or "")
        row = prior_rows.get(finding_id)
        if row is None:
            continue
        status = str(row.get("status") or "")
        if status == STATUS_RATIFIED or status == STATUS_FAILED:
            outcomes.append(
                _apply_choice(
                    root,
                    event=event,
                    row=row,
                    live_findings=live_findings,
                    open_milestones=open_milestones,
                )
            )
            continue
        outcomes.append(
            RatificationFindingOutcome(
                finding_id=finding_id,
                issue_ref=str(event.get("issue_ref") or event.get("finding_id") or "?"),
                decision=str(row.get("operator_decision") or ""),
                status=status or STATUS_SKIPPED,
                disposition=str(row.get("applied_disposition") or "").strip() or None,
                target_milestone=str(row.get("target_milestone") or "").strip() or None,
                punt_reason_code=str(row.get("punt_reason_code") or "").strip() or None,
                summary=str(row.get("external_effect_summary") or "").strip(),
                stale_reason=str(row.get("stale_reason") or "").strip(),
            )
        )

    return RatificationSummary(triage_run_id=triage_run_id, findings=tuple(outcomes))


__all__ = [
    "TriageRatificationError",
    "ratify_triage_run",
]
