"""Operator-ratified application data and rendering for ``forge triage``.

This module stays low-dependency on purpose: it defines the decision/status
vocabulary the operator-driven ratification flow records in the audit trail,
plus the text renderers the CLI prints before and after application.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

DECISION_ACCEPT = "accept"
DECISION_OVERRIDE = "override"
DECISION_SKIP = "skip"

DECISIONS: tuple[str, ...] = (
    DECISION_ACCEPT,
    DECISION_OVERRIDE,
    DECISION_SKIP,
)

STATUS_RATIFIED = "ratified"
STATUS_APPLIED = "applied"
STATUS_STALE = "stale"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

TERMINAL_STATUSES: tuple[str, ...] = (
    STATUS_APPLIED,
    STATUS_STALE,
    STATUS_SKIPPED,
)


@dataclass(frozen=True)
class OperatorChoice:
    """One per-finding operator decision before any tracker mutation."""

    decision: str
    disposition: str | None = None
    target_milestone: str | None = None
    punt_reason_code: str | None = None
    evidence_refs: tuple[str, ...] = ()
    operator_note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "disposition": self.disposition,
            "target_milestone": self.target_milestone,
            "punt_reason_code": self.punt_reason_code,
            "evidence_refs": list(self.evidence_refs),
            "operator_note": self.operator_note,
        }


@dataclass(frozen=True)
class RatificationFindingOutcome:
    """The stored/application outcome for one finding in a ratification run."""

    finding_id: str
    issue_ref: str
    decision: str
    status: str
    disposition: str | None = None
    target_milestone: str | None = None
    punt_reason_code: str | None = None
    summary: str = ""
    stale_reason: str = ""


@dataclass(frozen=True)
class RatificationSummary:
    """Operator-visible summary for a ratification/application pass."""

    triage_run_id: str
    findings: tuple[RatificationFindingOutcome, ...] = ()

    @property
    def total(self) -> int:
        return len(self.findings)

    def count(self, status: str) -> int:
        return sum(1 for finding in self.findings if finding.status == status)


def _proposal_target_display(proposal: Mapping[str, object]) -> str:
    disposition = str(proposal.get("disposition") or "")
    if disposition == "punt":
        reason = str(proposal.get("punt_reason_code") or "").strip()
        return f"/{reason}" if reason else ""
    target = str(proposal.get("target_milestone") or "").strip()
    return f" -> {target}" if target else ""


def render_reviewed_proposal(event: Mapping[str, object]) -> str:
    """Render one stored proposal plus its review context for operator ratification."""
    proposal = event.get("proposal")
    proposal_map = proposal if isinstance(proposal, Mapping) else {}
    snapshot = event.get("finding_snapshot")
    snapshot_map = snapshot if isinstance(snapshot, Mapping) else {}
    review = event.get("punt_review")
    review_map = review if isinstance(review, Mapping) else {}

    issue_ref = str(event.get("issue_ref") or event.get("finding_id") or "?")
    disposition = str(proposal_map.get("disposition") or event.get("disposition") or "?")
    header = f"{issue_ref}  PROPOSE {disposition}{_proposal_target_display(proposal_map)}"
    lines = [header]

    title = str(snapshot_map.get("title") or "").strip()
    if title:
        lines.append(f"       title: {title}")
    pool_state = str(snapshot_map.get("pool_state") or "").strip()
    verification_status = str(snapshot_map.get("verification_status") or "").strip()
    if pool_state or verification_status:
        rendered = ", ".join(value for value in (pool_state, verification_status) if value)
        lines.append(f"       snapshot: {rendered}")
    evidence_refs = proposal_map.get("evidence_refs") or event.get("evidence_refs") or []
    if isinstance(evidence_refs, list) and evidence_refs:
        lines.append(f"       cites: {', '.join(str(ref) for ref in evidence_refs)}")
    rationale = str(proposal_map.get("rationale") or "").strip()
    if rationale:
        lines.append(f"       reasoning (unverified): {rationale}")
    verdict = str(review_map.get("verdict") or "").strip()
    if verdict:
        lines.append(f"       punt review: {verdict}")
        review_refs = review_map.get("evidence_refs") or []
        if isinstance(review_refs, list) and review_refs:
            lines.append(f"       review cites: {', '.join(str(ref) for ref in review_refs)}")
        review_rationale = str(review_map.get("rationale") or "").strip()
        if review_rationale:
            lines.append(f"       reviewer reasoning (unverified): {review_rationale}")
    fallback_reason = str(event.get("fallback_reason") or "").strip()
    if fallback_reason:
        lines.append(f"       fallback: {fallback_reason}")
    review_fallback = str(event.get("review_fallback_reason") or "").strip()
    if review_fallback:
        lines.append(f"       review fallback: {review_fallback}")
    return "\n".join(lines)


def render_ratification_summary(summary: RatificationSummary) -> str:
    """Render the operator/application outcome for a ratified proposal run."""
    lines = [
        f"TRIAGE RATIFICATION — run {summary.triage_run_id}; {summary.total} finding(s)",
        (
            "Applied: "
            f"{summary.count(STATUS_APPLIED)}, stale: {summary.count(STATUS_STALE)}, "
            f"skipped: {summary.count(STATUS_SKIPPED)}, failed: {summary.count(STATUS_FAILED)}"
        ),
        "",
    ]
    for finding in summary.findings:
        payload = ""
        if finding.disposition == "punt" and finding.punt_reason_code:
            payload = f"/{finding.punt_reason_code}"
        elif finding.target_milestone:
            payload = f" -> {finding.target_milestone}"
        lines.append(
            f"{finding.issue_ref}  {finding.status.upper()} "
            f"({finding.decision}"
            f"{' ' + finding.disposition + payload if finding.disposition else ''})"
        )
        if finding.summary:
            lines.append(f"       {finding.summary}")
        if finding.stale_reason:
            lines.append(f"       stale: {finding.stale_reason}")
        lines.append("")
    return "\n".join(lines).rstrip()
