"""Append-only audit storage for semantic evaluation runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from theforge.agent_types import COST_UNKNOWN
from theforge.artifacts import ensure_parent_dir
from theforge.eval.semantic_types import (
    DECISION_ACCEPTED,
    DECISION_VALUES,
    OUTCOME_NO_FINDINGS,
    STATUS_EVALUATION_FAILED,
    STATUS_FINDINGS,
    STATUS_NO_FINDINGS,
    SemanticConcernDecisionValue,
    SemanticFinding,
    SemanticOutcome,
    SemanticStatus,
)

SEMANTIC_AUDIT_DIR = Path(".forge") / "audits" / "semantic-review"
SEMANTIC_RECORDS_PATH = SEMANTIC_AUDIT_DIR / "records.jsonl"
SEMANTIC_BASELINES_PATH = SEMANTIC_AUDIT_DIR / "baselines.jsonl"
SEMANTIC_RATIFICATIONS_PATH = SEMANTIC_AUDIT_DIR / "ratifications.jsonl"
COST_CACHE_HIT = "cache_zero"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class FrozenSemanticBaseline:
    """Human-frozen defect baseline for one document digest."""

    issue_ref: str
    input_digest: str
    canonical_type: str | None
    defect_ids: tuple[str, ...]
    frozen_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_ref": self.issue_ref,
            "input_digest": self.input_digest,
            "canonical_type": self.canonical_type,
            "defect_ids": list(self.defect_ids),
            "frozen_at": self.frozen_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "FrozenSemanticBaseline":
        defect_ids = data.get("defect_ids") or []
        if not isinstance(defect_ids, list):
            raise ValueError("baseline.defect_ids must be a list")
        return cls(
            issue_ref=str(data.get("issue_ref") or ""),
            input_digest=str(data.get("input_digest") or ""),
            canonical_type=(
                None if data.get("canonical_type") is None else str(data.get("canonical_type"))
            ),
            defect_ids=tuple(sorted({str(item) for item in defect_ids if str(item)})),
            frozen_at=str(data.get("frozen_at") or ""),
        )


@dataclass(frozen=True)
class SemanticConcernDecision:
    """One operator decision about one raised concern."""

    finding_digest: str
    decision: SemanticConcernDecisionValue

    def __post_init__(self) -> None:
        if self.decision not in DECISION_VALUES:
            raise ValueError(
                f"concern decision must be one of {DECISION_VALUES!r}, got {self.decision!r}"
            )

    def to_dict(self) -> dict[str, str]:
        return {"finding_digest": self.finding_digest, "decision": self.decision}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SemanticConcernDecision":
        return cls(
            finding_digest=str(data.get("finding_digest") or ""),
            decision=str(data.get("decision") or ""),
        )


@dataclass(frozen=True)
class SemanticRatificationRecord:
    """An operator's ratification of one evaluation, scoped to one revision.

    ``input_digest`` is the document revision the decision was made against.
    A ratification never speaks for any other revision: readiness derived from
    it is discarded the moment the document changes (#2785).
    """

    issue_ref: str
    input_digest: str
    model_id: str
    prompt_contract_version: str
    ratified_at: str
    decisions: tuple[SemanticConcernDecision, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_ref": self.issue_ref,
            "input_digest": self.input_digest,
            "model_id": self.model_id,
            "prompt_contract_version": self.prompt_contract_version,
            "ratified_at": self.ratified_at,
            "decisions": [decision.to_dict() for decision in self.decisions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SemanticRatificationRecord":
        decisions_raw = data.get("decisions") or []
        if not isinstance(decisions_raw, list):
            raise ValueError("ratification.decisions must be a list")
        return cls(
            issue_ref=str(data.get("issue_ref") or ""),
            input_digest=str(data.get("input_digest") or ""),
            model_id=str(data.get("model_id") or ""),
            prompt_contract_version=str(data.get("prompt_contract_version") or ""),
            ratified_at=str(data.get("ratified_at") or ""),
            decisions=tuple(
                SemanticConcernDecision.from_dict(item)
                for item in decisions_raw
                if isinstance(item, dict)
            ),
        )

    def decision_by_digest(self) -> dict[str, str]:
        return {decision.finding_digest: decision.decision for decision in self.decisions}

    def accepted_digests(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                decision.finding_digest
                for decision in self.decisions
                if decision.decision == DECISION_ACCEPTED
            )
        )


@dataclass(frozen=True)
class SemanticEvaluationRecord:
    """One append-only semantic-evaluation run record."""

    issue_ref: str
    canonical_type: str | None
    input_digest: str
    model_id: str
    prompt_contract_version: str
    status: SemanticStatus
    cache_hit: bool
    duration_seconds: float
    cost_usd: float | None
    cost_provenance: str = COST_UNKNOWN
    started_at: str = ""
    completed_at: str = ""
    configured_profile_name: str = ""
    configured_model_name: str = ""
    resolved_model_id: str | None = None
    outcome: SemanticOutcome | None = None
    findings: tuple[SemanticFinding, ...] = ()
    failure_detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "issue_ref": self.issue_ref,
            "canonical_type": self.canonical_type,
            "input_digest": self.input_digest,
            "model_id": self.model_id,
            "prompt_contract_version": self.prompt_contract_version,
            "status": self.status,
            "cache_hit": self.cache_hit,
            "duration_seconds": self.duration_seconds,
            "cost_usd": self.cost_usd,
            "cost_provenance": self.cost_provenance,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "configured_profile_name": self.configured_profile_name,
            "configured_model_name": self.configured_model_name,
            "resolved_model_id": self.resolved_model_id,
        }
        if self.status in (STATUS_FINDINGS, STATUS_NO_FINDINGS):
            data["outcome"] = self.outcome
            data["findings"] = [finding.to_dict() for finding in self.findings]
        elif self.failure_detail:
            data["failure_detail"] = self.failure_detail
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SemanticEvaluationRecord":
        findings_raw = data.get("findings") or []
        if not isinstance(findings_raw, list):
            raise ValueError("record.findings must be a list when present")
        findings = tuple(
            SemanticFinding.from_dict(item) for item in findings_raw if isinstance(item, dict)
        )
        return cls(
            issue_ref=str(data.get("issue_ref") or ""),
            canonical_type=(
                None if data.get("canonical_type") is None else str(data.get("canonical_type"))
            ),
            input_digest=str(data.get("input_digest") or ""),
            model_id=str(data.get("model_id") or ""),
            prompt_contract_version=str(data.get("prompt_contract_version") or ""),
            status=str(data.get("status") or STATUS_EVALUATION_FAILED),
            cache_hit=bool(data.get("cache_hit", False)),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            cost_usd=(None if data.get("cost_usd") is None else float(data.get("cost_usd"))),
            cost_provenance=str(data.get("cost_provenance") or COST_UNKNOWN),
            started_at=str(data.get("started_at") or ""),
            completed_at=str(data.get("completed_at") or ""),
            configured_profile_name=str(data.get("configured_profile_name") or ""),
            configured_model_name=str(data.get("configured_model_name") or ""),
            resolved_model_id=(
                None
                if data.get("resolved_model_id") in (None, "")
                else str(data.get("resolved_model_id"))
            ),
            outcome=(None if data.get("outcome") in (None, "") else str(data.get("outcome"))),
            findings=findings,
            failure_detail=(
                None
                if data.get("failure_detail") in (None, "")
                else str(data.get("failure_detail"))
            ),
        )

    def finding_digests(self) -> tuple[str, ...]:
        return tuple(finding.finding_digest for finding in self.findings)

    def outcome_signature(self) -> tuple[str, tuple[str, ...]]:
        if self.status == STATUS_EVALUATION_FAILED:
            return (STATUS_EVALUATION_FAILED, ())
        if self.outcome == OUTCOME_NO_FINDINGS:
            return (STATUS_NO_FINDINGS, ())
        return (STATUS_FINDINGS, tuple(sorted(self.finding_digests())))


class SemanticReviewStore:
    """Filesystem-backed append-only store for semantic-review audits."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.records_path = project_root / SEMANTIC_RECORDS_PATH
        self.baselines_path = project_root / SEMANTIC_BASELINES_PATH
        self.ratifications_path = project_root / SEMANTIC_RATIFICATIONS_PATH

    def append_record(self, record: SemanticEvaluationRecord) -> None:
        self._append_jsonl(self.records_path, record.to_dict())

    def append_ratification(self, ratification: SemanticRatificationRecord) -> None:
        self._append_jsonl(self.ratifications_path, ratification.to_dict())

    def iter_ratifications(self) -> list[SemanticRatificationRecord]:
        return [
            SemanticRatificationRecord.from_dict(item)
            for item in self._read_jsonl(self.ratifications_path)
            if isinstance(item, dict)
        ]

    def ratifications_for_digest(
        self, *, issue_ref: str, input_digest: str
    ) -> list[SemanticRatificationRecord]:
        return [
            ratification
            for ratification in self.iter_ratifications()
            if ratification.issue_ref == issue_ref and ratification.input_digest == input_digest
        ]

    def latest_ratification(
        self, *, issue_ref: str, input_digest: str
    ) -> SemanticRatificationRecord | None:
        matches = self.ratifications_for_digest(issue_ref=issue_ref, input_digest=input_digest)
        return matches[-1] if matches else None

    def append_baseline(self, baseline: FrozenSemanticBaseline) -> None:
        self._append_jsonl(self.baselines_path, baseline.to_dict())

    def iter_records(self) -> list[SemanticEvaluationRecord]:
        return [
            SemanticEvaluationRecord.from_dict(item)
            for item in self._read_jsonl(self.records_path)
            if isinstance(item, dict)
        ]

    def iter_baselines(self) -> list[FrozenSemanticBaseline]:
        return [
            FrozenSemanticBaseline.from_dict(item)
            for item in self._read_jsonl(self.baselines_path)
            if isinstance(item, dict)
        ]

    def latest_record_for_identity(
        self,
        *,
        input_digest: str,
        model_id: str,
        prompt_contract_version: str,
    ) -> SemanticEvaluationRecord | None:
        match = None
        for record in self.iter_records():
            if (
                record.input_digest == input_digest
                and record.model_id == model_id
                and record.prompt_contract_version == prompt_contract_version
                and record.status in (STATUS_FINDINGS, STATUS_NO_FINDINGS)
            ):
                match = record
        return match

    def latest_successful_record(
        self, *, issue_ref: str, input_digest: str
    ) -> SemanticEvaluationRecord | None:
        """Return the most recent successful evaluation of one exact revision.

        Scoped to ``input_digest`` so a run that completes after the document
        has moved on cannot speak for the revision that superseded it.
        """
        match = None
        for record in self.iter_records():
            if (
                record.issue_ref == issue_ref
                and record.input_digest == input_digest
                and record.status in (STATUS_FINDINGS, STATUS_NO_FINDINGS)
            ):
                match = record
        return match

    def records_for_digest(self, input_digest: str) -> list[SemanticEvaluationRecord]:
        return [record for record in self.iter_records() if record.input_digest == input_digest]

    def records_for_issue(self, issue_ref: str) -> list[SemanticEvaluationRecord]:
        return [record for record in self.iter_records() if record.issue_ref == issue_ref]

    def frozen_baseline(self, input_digest: str) -> FrozenSemanticBaseline | None:
        match = None
        for baseline in self.iter_baselines():
            if baseline.input_digest == input_digest:
                match = baseline
        return match

    def freeze_baseline(
        self,
        *,
        issue_ref: str,
        input_digest: str,
        canonical_type: str | None,
        defect_ids: tuple[str, ...],
    ) -> tuple[FrozenSemanticBaseline, bool]:
        normalized = tuple(sorted({item for item in defect_ids if item}))
        existing = self.frozen_baseline(input_digest)
        if existing is not None:
            if (
                existing.issue_ref != issue_ref
                or existing.canonical_type != canonical_type
                or existing.defect_ids != normalized
            ):
                raise ValueError(
                    f"baseline for {input_digest} is already frozen and cannot be changed"
                )
            return existing, False

        baseline = FrozenSemanticBaseline(
            issue_ref=issue_ref,
            input_digest=input_digest,
            canonical_type=canonical_type,
            defect_ids=normalized,
            frozen_at=utc_now_iso(),
        )
        self.append_baseline(baseline)
        return baseline, True

    def _append_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        ensure_parent_dir(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")

    def _read_jsonl(self, path: Path) -> list[object]:
        if not path.exists():
            return []
        rows: list[object] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows
