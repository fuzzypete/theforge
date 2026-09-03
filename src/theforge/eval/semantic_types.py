"""Pure data types for audit-only semantic evaluation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

OUTCOME_FINDINGS = "FINDINGS"
OUTCOME_NO_FINDINGS = "NO_FINDINGS"
OUTCOME_EVALUATION_FAILED = "EVALUATION_FAILED"

STATUS_FINDINGS = "findings"
STATUS_NO_FINDINGS = "no_findings"
STATUS_EVALUATION_FAILED = "evaluation_failed"

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
SEVERITY_VALUES = (SEVERITY_LOW, SEVERITY_MEDIUM, SEVERITY_HIGH)

SemanticOutcome = Literal["FINDINGS", "NO_FINDINGS"]
SemanticStatus = Literal["findings", "no_findings", "evaluation_failed"]
SemanticSeverity = Literal["low", "medium", "high"]


def _finding_digest_payload(
    *,
    summary: str,
    rationale: str,
    evidence: str | None,
    severity: str | None,
) -> str:
    payload = {
        "evidence": evidence,
        "rationale": rationale,
        "severity": severity,
        "summary": summary,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class SemanticFinding:
    """One structured semantic-review defect finding."""

    summary: str
    rationale: str
    evidence: str | None = None
    severity: SemanticSeverity | None = None
    finding_digest: str = field(init=False)

    def __post_init__(self) -> None:
        digest = hashlib.sha256(
            _finding_digest_payload(
                summary=self.summary,
                rationale=self.rationale,
                evidence=self.evidence,
                severity=self.severity,
            ).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "finding_digest", digest)

    def to_dict(self) -> dict[str, str | None]:
        return {
            "finding_digest": self.finding_digest,
            "summary": self.summary,
            "rationale": self.rationale,
            "evidence": self.evidence,
            "severity": self.severity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SemanticFinding":
        finding = cls(
            summary=str(data.get("summary") or ""),
            rationale=str(data.get("rationale") or ""),
            evidence=(None if data.get("evidence") in (None, "") else str(data.get("evidence"))),
            severity=(None if data.get("severity") in (None, "") else str(data.get("severity"))),
        )
        stored = data.get("finding_digest")
        if isinstance(stored, str) and stored:
            object.__setattr__(finding, "finding_digest", stored)
        return finding


@dataclass(frozen=True)
class SemanticParsedOutcome:
    """Structured parser output from a successful semantic evaluation."""

    outcome: SemanticOutcome
    findings: tuple[SemanticFinding, ...] = ()


def status_for_outcome(outcome: SemanticOutcome) -> SemanticStatus:
    if outcome == OUTCOME_NO_FINDINGS:
        return STATUS_NO_FINDINGS
    return STATUS_FINDINGS
