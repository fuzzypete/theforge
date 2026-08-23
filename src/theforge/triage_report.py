"""The backlog-report contract ``forge triage`` consumes.

The proposal stage does not compute staleness evidence — the report slice of
epic #1033 does, and this module is the one boundary between the two. It
defines the artifact shape (findings, their stable IDs, and the deterministic
evidence entries each carries) and parses it into the low-dependency types the
taxonomy module validates against.

Keeping the contract here rather than inside the flow means the report slice can
be wired up by changing what writes this artifact, without touching the taxonomy,
the validator, or the audit writers. Until it lands, ``forge triage`` reads the
artifact from ``--report`` and fails with an operator-legible error if it is
missing or malformed — it never silently proposes against an empty backlog.

Artifact shape (JSON or YAML, both accepted)::

    current_milestone: v0.12.0          # optional; fix_now is unavailable without it
    named_milestones: [v0.13.0]         # optional; fix_later targets, plus Hygiene
    findings:
      - finding_id: "1312:audit-count"  # stable identity; falls back to issue_ref
        issue_ref: "#1312"
        body: "audit count is off by one when …"
        evidence:
          - id: symbol-absent
            kind: staleness
            summary: "cited symbol absent from current tree"
            checkable: true
            detail: "rg 'audit_count' at HEAD returns no match"

Stdlib + yaml only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from theforge.triage_proposal import PacketEvidence


class BacklogReportError(ValueError):
    """The backlog report is missing, unreadable, or does not match the contract.

    Raised rather than degraded: a triage run against half a report would
    propose dispositions from evidence nobody checked, which is the one outcome
    this stage exists to prevent.
    """


@dataclass(frozen=True)
class BacklogFinding:
    """One finding from the backlog report, before its packet is assembled."""

    finding_id: str
    issue_ref: str
    body: str
    issue_number: int | None = None
    title: str = ""
    labels: tuple[str, ...] = ()
    display_labels: str = ""
    opened_at: str = ""
    age_days: int | None = None
    pool_state: str = ""
    verification_status: str = ""
    evidence: tuple[PacketEvidence, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "finding_id": self.finding_id,
            "issue_ref": self.issue_ref,
            "body": self.body,
            "evidence": [item.to_dict() for item in self.evidence],
        }
        if self.issue_number is not None:
            payload["issue_number"] = self.issue_number
        if self.title:
            payload["title"] = self.title
        if self.labels:
            payload["labels"] = list(self.labels)
        if self.display_labels:
            payload["display_labels"] = self.display_labels
        if self.opened_at:
            payload["opened_at"] = self.opened_at
        if self.age_days is not None:
            payload["age_days"] = self.age_days
        if self.pool_state:
            payload["pool_state"] = self.pool_state
        if self.verification_status:
            payload["verification_status"] = self.verification_status
        return payload

    def snapshot_dict(self) -> dict[str, object]:
        """Return the stable state used for stale-ratification detection."""
        payload: dict[str, object] = {
            "issue_ref": self.issue_ref,
            "body": self.body,
            "evidence": [item.to_dict() for item in self.evidence],
        }
        if self.issue_number is not None:
            payload["issue_number"] = self.issue_number
        if self.title:
            payload["title"] = self.title
        if self.labels:
            payload["labels"] = list(self.labels)
        if self.pool_state:
            payload["pool_state"] = self.pool_state
        if self.verification_status:
            payload["verification_status"] = self.verification_status
        return payload


@dataclass(frozen=True)
class BacklogReport:
    """A parsed backlog report: the findings plus the milestone vocabulary."""

    findings: tuple[BacklogFinding, ...] = ()
    current_milestone: str | None = None
    named_milestones: tuple[str, ...] = ()
    source_path: str = ""


def _load_raw(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BacklogReportError(
            f"backlog report not found: {path}. Generate it with the backlog report "
            f"command, or pass --report path/to/report.json"
        ) from exc
    except OSError as exc:
        raise BacklogReportError(f"backlog report could not be read: {path}: {exc}") from exc

    if not text.strip():
        raise BacklogReportError(f"backlog report is empty: {path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BacklogReportError(
            f"backlog report is not valid JSON or YAML: {path}: {exc}"
        ) from exc


def _parse_evidence(raw: object, finding_id: str) -> tuple[PacketEvidence, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise BacklogReportError(f"finding {finding_id!r}: 'evidence' must be a list")
    entries: list[PacketEvidence] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BacklogReportError(
                f"finding {finding_id!r}: evidence[{index}] must be a mapping"
            )
        evidence_id = str(item.get("id") or item.get("evidence_id") or "").strip()
        if not evidence_id:
            raise BacklogReportError(
                f"finding {finding_id!r}: evidence[{index}] has no 'id' — a proposal "
                f"cannot cite evidence that has no identity"
            )
        if evidence_id in seen:
            raise BacklogReportError(
                f"finding {finding_id!r}: duplicate evidence id {evidence_id!r}"
            )
        seen.add(evidence_id)
        entries.append(
            PacketEvidence(
                evidence_id=evidence_id,
                kind=str(item.get("kind") or "report").strip(),
                summary=str(item.get("summary") or "").strip(),
                # Default False: an entry that does not say it was checked is
                # not treated as a checkable fact, so a report that omits the
                # flag routes to needs_verification rather than to a discard.
                checkable=bool(item.get("checkable", False)),
                detail=str(item.get("detail") or "").strip(),
            )
        )
    return tuple(entries)


def _parse_finding(raw: object, index: int) -> BacklogFinding:
    if not isinstance(raw, dict):
        raise BacklogReportError(f"findings[{index}] must be a mapping")
    issue_ref = str(raw.get("issue_ref") or raw.get("issue") or "").strip()
    finding_id = str(raw.get("finding_id") or raw.get("id") or issue_ref).strip()
    if not finding_id:
        raise BacklogReportError(
            f"findings[{index}] has neither 'finding_id' nor 'issue_ref' — a proposal "
            f"needs a stable identity to record against"
        )
    body = str(raw.get("body") or raw.get("text") or "").strip()
    raw_issue_number = raw.get("issue_number")
    issue_number: int | None
    if raw_issue_number in (None, ""):
        issue_number = None
    else:
        try:
            issue_number = int(raw_issue_number)
        except (TypeError, ValueError) as exc:
            raise BacklogReportError(
                f"findings[{index}] issue_number must be an integer when present"
            ) from exc
    raw_labels = raw.get("labels") or []
    if isinstance(raw_labels, str):
        raw_labels = [raw_labels]
    if not isinstance(raw_labels, list):
        raise BacklogReportError(f"findings[{index}] labels must be a list when present")
    raw_age_days = raw.get("age_days")
    age_days: int | None
    if raw_age_days in (None, ""):
        age_days = None
    else:
        try:
            age_days = int(raw_age_days)
        except (TypeError, ValueError) as exc:
            raise BacklogReportError(
                f"findings[{index}] age_days must be an integer when present"
            ) from exc
    return BacklogFinding(
        finding_id=finding_id,
        issue_ref=issue_ref or finding_id,
        body=body,
        issue_number=issue_number,
        title=str(raw.get("title") or "").strip(),
        labels=tuple(str(label).strip() for label in raw_labels if str(label).strip()),
        display_labels=str(raw.get("display_labels") or "").strip(),
        opened_at=str(raw.get("opened_at") or "").strip(),
        age_days=age_days,
        pool_state=str(raw.get("pool_state") or "").strip(),
        verification_status=str(raw.get("verification_status") or "").strip(),
        evidence=_parse_evidence(raw.get("evidence"), finding_id),
    )


def parse_backlog_report(data: object, *, source_path: str = "") -> BacklogReport:
    """Parse an already-loaded report payload into a :class:`BacklogReport`."""
    if not isinstance(data, dict):
        raise BacklogReportError(f"backlog report must be a mapping, got {type(data).__name__}")
    raw_findings = data.get("findings")
    if raw_findings is None:
        raw_findings = []
    if not isinstance(raw_findings, list):
        raise BacklogReportError("backlog report 'findings' must be a list")

    findings = tuple(_parse_finding(item, i) for i, item in enumerate(raw_findings))
    seen: set[str] = set()
    for finding in findings:
        if finding.finding_id in seen:
            raise BacklogReportError(f"duplicate finding_id {finding.finding_id!r}")
        seen.add(finding.finding_id)

    raw_named = data.get("named_milestones") or []
    if isinstance(raw_named, str):
        raw_named = [raw_named]
    if not isinstance(raw_named, list):
        raise BacklogReportError("backlog report 'named_milestones' must be a list")

    current = data.get("current_milestone")
    return BacklogReport(
        findings=findings,
        current_milestone=str(current).strip() if current else None,
        named_milestones=tuple(str(m).strip() for m in raw_named if str(m).strip()),
        source_path=source_path,
    )


def load_backlog_report(path: Path | str) -> BacklogReport:
    """Load and validate the backlog report artifact at ``path``."""
    resolved = Path(path)
    return parse_backlog_report(_load_raw(resolved), source_path=str(resolved))
