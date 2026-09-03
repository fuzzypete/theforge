"""Corpus reporting for semantic evaluation audits."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from theforge.eval.semantic_runner import normalize_issue_ref
from theforge.eval.semantic_storage import (
    SemanticEvaluationRecord,
    SemanticReviewStore,
)
from theforge.eval.semantic_types import STATUS_EVALUATION_FAILED, STATUS_FINDINGS

JUDGMENT_CONFIRMED = "confirmed"
JUDGMENT_REJECTED = "rejected"
JUDGMENT_VALUES = frozenset({JUDGMENT_CONFIRMED, JUDGMENT_REJECTED})


class SemanticCorpusError(ValueError):
    """Raised when a semantic corpus annotation file is malformed or ambiguous."""


@dataclass(frozen=True)
class SemanticFindingJudgment:
    finding_digest: str
    judgment: str
    defect_id: str | None = None


@dataclass(frozen=True)
class SemanticCorpusEntry:
    issue_ref: str | None
    input_digest: str | None
    frozen_baseline_defect_ids: tuple[str, ...]
    judgments: tuple[SemanticFindingJudgment, ...]


@dataclass(frozen=True)
class SemanticCorpus:
    name: str
    entries: tuple[SemanticCorpusEntry, ...]


@dataclass(frozen=True)
class SemanticCorpusReport:
    name: str
    total_documents: int
    documents_with_records: int
    absent_documents: int
    failed_documents: int
    baseline_defects_total: int
    known_defects_recovered: int
    confirmed_findings: int
    rejected_findings: int
    unjudged_findings: int
    confirmed_novel_defects: int
    precision: float | None
    rejection_rate: float | None
    cost_per_confirmed_finding: float | None
    cost_unknown_records_excluded: int
    repeated_identity_groups: int
    stable_repeated_identity_groups: int
    independent_repeat_groups: int
    stable_independent_repeat_groups: int
    cache_derived_repeat_groups: int
    stable_cache_derived_repeat_groups: int

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "total_documents": self.total_documents,
            "documents_with_records": self.documents_with_records,
            "absent_documents": self.absent_documents,
            "failed_documents": self.failed_documents,
            "baseline_defects_total": self.baseline_defects_total,
            "known_defects_recovered": self.known_defects_recovered,
            "confirmed_findings": self.confirmed_findings,
            "rejected_findings": self.rejected_findings,
            "unjudged_findings": self.unjudged_findings,
            "confirmed_novel_defects": self.confirmed_novel_defects,
            "precision": self.precision,
            "rejection_rate": self.rejection_rate,
            "cost_per_confirmed_finding": self.cost_per_confirmed_finding,
            "cost_unknown_records_excluded": self.cost_unknown_records_excluded,
            "repeated_identity_groups": self.repeated_identity_groups,
            "stable_repeated_identity_groups": self.stable_repeated_identity_groups,
            "independent_repeat_groups": self.independent_repeat_groups,
            "stable_independent_repeat_groups": self.stable_independent_repeat_groups,
            "cache_derived_repeat_groups": self.cache_derived_repeat_groups,
            "stable_cache_derived_repeat_groups": self.stable_cache_derived_repeat_groups,
        }


def load_semantic_corpus(path: Path) -> SemanticCorpus:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SemanticCorpusError("semantic corpus must be a YAML mapping")
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list):
        raise SemanticCorpusError("semantic corpus entries must be a list")
    entries: list[SemanticCorpusEntry] = []
    for item in entries_raw:
        if not isinstance(item, dict):
            raise SemanticCorpusError("each semantic corpus entry must be a mapping")
        issue_ref = item.get("issue_ref")
        input_digest = item.get("input_digest")
        if issue_ref in (None, "") and input_digest in (None, ""):
            raise SemanticCorpusError("each corpus entry must declare issue_ref or input_digest")
        baseline_ids_raw = item.get("frozen_baseline_defect_ids")
        if not isinstance(baseline_ids_raw, list):
            raise SemanticCorpusError("frozen_baseline_defect_ids must be a list")
        judgments_raw = item.get("judgments") or []
        if not isinstance(judgments_raw, list):
            raise SemanticCorpusError("judgments must be a list")
        judgments: list[SemanticFindingJudgment] = []
        for judgment in judgments_raw:
            if not isinstance(judgment, dict):
                raise SemanticCorpusError("each judgment must be a mapping")
            finding_digest = str(judgment.get("finding_digest") or "").strip()
            if not finding_digest:
                raise SemanticCorpusError("judgment.finding_digest must be non-empty")
            judgment_value = str(judgment.get("judgment") or "").strip().lower()
            if judgment_value not in JUDGMENT_VALUES:
                raise SemanticCorpusError(
                    f"judgment must be one of {sorted(JUDGMENT_VALUES)}, got {judgment_value!r}"
                )
            defect_id = judgment.get("defect_id")
            judgments.append(
                SemanticFindingJudgment(
                    finding_digest=finding_digest,
                    judgment=judgment_value,
                    defect_id=None if defect_id in (None, "") else str(defect_id),
                )
            )
        entries.append(
            SemanticCorpusEntry(
                issue_ref=None if issue_ref in (None, "") else normalize_issue_ref(str(issue_ref)),
                input_digest=None if input_digest in (None, "") else str(input_digest),
                frozen_baseline_defect_ids=tuple(
                    sorted({str(item) for item in baseline_ids_raw if str(item)})
                ),
                judgments=tuple(judgments),
            )
        )
    return SemanticCorpus(name=str(raw.get("name") or path.stem), entries=tuple(entries))


def _records_for_entry(
    *,
    entry: SemanticCorpusEntry,
    records_by_digest: dict[str, list[SemanticEvaluationRecord]],
    records_by_issue: dict[str, list[SemanticEvaluationRecord]],
) -> tuple[str | None, list[SemanticEvaluationRecord]]:
    if entry.input_digest:
        records = list(records_by_digest.get(entry.input_digest, ()))
        if entry.issue_ref is not None:
            records = [record for record in records if record.issue_ref == entry.issue_ref]
        return entry.input_digest, records

    issue_ref = entry.issue_ref
    assert issue_ref is not None
    records = list(records_by_issue.get(issue_ref, ()))
    digests = {record.input_digest for record in records}
    if len(digests) > 1:
        raise SemanticCorpusError(
            f"corpus entry {issue_ref} matches multiple input digests; specify input_digest"
        )
    resolved_digest = next(iter(digests), None)
    return resolved_digest, records


def _stability_groups(
    records: list[SemanticEvaluationRecord],
) -> list[list[SemanticEvaluationRecord]]:
    by_identity: dict[tuple[str, str, str], list[SemanticEvaluationRecord]] = {}
    for record in records:
        key = (
            record.input_digest,
            record.model_id,
            record.prompt_contract_version,
        )
        by_identity.setdefault(key, []).append(record)
    return [group for group in by_identity.values() if len(group) > 1]


def build_semantic_corpus_report(
    corpus: SemanticCorpus,
    *,
    store: SemanticReviewStore,
) -> SemanticCorpusReport:
    semantic_records = store.iter_records()
    records_by_digest: dict[str, list[SemanticEvaluationRecord]] = {}
    records_by_issue: dict[str, list[SemanticEvaluationRecord]] = {}
    for record in semantic_records:
        records_by_digest.setdefault(record.input_digest, []).append(record)
        records_by_issue.setdefault(record.issue_ref, []).append(record)

    documents_with_records = 0
    absent_documents = 0
    failed_documents = 0
    baseline_defects_total = 0
    recovered_known_defects: set[tuple[str, str]] = set()
    confirmed_findings = 0
    rejected_findings = 0
    confirmed_novel_defects: set[tuple[str, str]] = set()
    unjudged_finding_digests: set[tuple[str, str]] = set()
    live_cost_total = 0.0
    live_cost_known_records = 0
    cost_unknown_records_excluded = 0
    repeated_identity_groups = 0
    stable_repeated_identity_groups = 0
    independent_repeat_groups = 0
    stable_independent_repeat_groups = 0
    cache_derived_repeat_groups = 0
    stable_cache_derived_repeat_groups = 0

    for entry in corpus.entries:
        input_digest, entry_records = _records_for_entry(
            entry=entry,
            records_by_digest=records_by_digest,
            records_by_issue=records_by_issue,
        )
        baseline_defects_total += len(entry.frozen_baseline_defect_ids)

        stored_baseline = None if input_digest is None else store.frozen_baseline(input_digest)
        if (
            stored_baseline is not None
            and stored_baseline.defect_ids != entry.frozen_baseline_defect_ids
        ):
            raise SemanticCorpusError(
                f"frozen baseline mismatch for {entry.issue_ref or input_digest}"
            )
        if entry_records:
            documents_with_records += 1
            if input_digest is None:
                raise SemanticCorpusError(
                    "recorded corpus entry unexpectedly resolved without a digest"
                )
            if stored_baseline is None:
                raise SemanticCorpusError(
                    f"records exist for {entry.issue_ref or input_digest} "
                    "but no frozen baseline was stored"
                )
        else:
            absent_documents += 1

        if entry_records and all(
            record.status == STATUS_EVALUATION_FAILED for record in entry_records
        ):
            failed_documents += 1

        finding_index: dict[str, bool] = {}
        for record in entry_records:
            if not record.cache_hit:
                if record.cost_usd is None:
                    cost_unknown_records_excluded += 1
                else:
                    live_cost_total += record.cost_usd
                    live_cost_known_records += 1
            for finding in record.findings:
                finding_index[finding.finding_digest] = True

        judged_digests = {judgment.finding_digest for judgment in entry.judgments}
        if input_digest is not None:
            for record in entry_records:
                if record.status == STATUS_FINDINGS:
                    for finding in record.findings:
                        if finding.finding_digest not in judged_digests:
                            unjudged_finding_digests.add((input_digest, finding.finding_digest))

        for judgment in entry.judgments:
            if judgment.finding_digest not in finding_index:
                raise SemanticCorpusError(
                    f"judgment references finding_digest {judgment.finding_digest!r} "
                    f"that is absent from records for {entry.issue_ref or input_digest}"
                )
            if judgment.judgment == JUDGMENT_CONFIRMED:
                confirmed_findings += 1
                if (
                    judgment.defect_id is not None
                    and judgment.defect_id in entry.frozen_baseline_defect_ids
                    and input_digest is not None
                ):
                    recovered_known_defects.add((input_digest, judgment.defect_id))
                else:
                    if input_digest is not None:
                        confirmed_novel_defects.add(
                            (input_digest, judgment.defect_id or judgment.finding_digest)
                        )
            else:
                rejected_findings += 1

        for group in _stability_groups(entry_records):
            repeated_identity_groups += 1
            signatures = {record.outcome_signature() for record in group}
            if len(signatures) == 1:
                stable_repeated_identity_groups += 1
            live_group = [record for record in group if not record.cache_hit]
            if len(live_group) >= 2:
                independent_repeat_groups += 1
                if len({record.outcome_signature() for record in live_group}) == 1:
                    stable_independent_repeat_groups += 1
            else:
                cache_derived_repeat_groups += 1
                if len(signatures) == 1:
                    stable_cache_derived_repeat_groups += 1

    judged_total = confirmed_findings + rejected_findings
    precision = None if judged_total == 0 else confirmed_findings / judged_total
    rejection_rate = None if judged_total == 0 else rejected_findings / judged_total
    cost_per_confirmed = None
    if confirmed_findings and live_cost_known_records:
        cost_per_confirmed = live_cost_total / confirmed_findings

    return SemanticCorpusReport(
        name=corpus.name,
        total_documents=len(corpus.entries),
        documents_with_records=documents_with_records,
        absent_documents=absent_documents,
        failed_documents=failed_documents,
        baseline_defects_total=baseline_defects_total,
        known_defects_recovered=len(recovered_known_defects),
        confirmed_findings=confirmed_findings,
        rejected_findings=rejected_findings,
        unjudged_findings=len(unjudged_finding_digests),
        confirmed_novel_defects=len(confirmed_novel_defects),
        precision=precision,
        rejection_rate=rejection_rate,
        cost_per_confirmed_finding=cost_per_confirmed,
        cost_unknown_records_excluded=cost_unknown_records_excluded,
        repeated_identity_groups=repeated_identity_groups,
        stable_repeated_identity_groups=stable_repeated_identity_groups,
        independent_repeat_groups=independent_repeat_groups,
        stable_independent_repeat_groups=stable_independent_repeat_groups,
        cache_derived_repeat_groups=cache_derived_repeat_groups,
        stable_cache_derived_repeat_groups=stable_cache_derived_repeat_groups,
    )


def render_semantic_corpus_report(report: SemanticCorpusReport) -> str:
    precision = "n/a" if report.precision is None else f"{report.precision:.3f}"
    rejection_rate = "n/a" if report.rejection_rate is None else f"{report.rejection_rate:.3f}"
    cost_per_confirmed = (
        "n/a"
        if report.cost_per_confirmed_finding is None
        else f"${report.cost_per_confirmed_finding:.4f}"
    )
    return "\n".join(
        [
            f"semantic_corpus={report.name}",
            f"documents={report.total_documents} recorded={report.documents_with_records} "
            f"absent={report.absent_documents} failed={report.failed_documents}",
            f"known_defect_recovery={report.known_defects_recovered}/{report.baseline_defects_total}",
            f"precision={precision} rejection_rate={rejection_rate}",
            f"confirmed_findings={report.confirmed_findings} "
            f"rejected_findings={report.rejected_findings} "
            f"unjudged_findings={report.unjudged_findings}",
            f"novel_confirmed_defects={report.confirmed_novel_defects}",
            f"cost_per_confirmed_finding={cost_per_confirmed} "
            f"cost_unknown_records_excluded={report.cost_unknown_records_excluded}",
            f"repeat_stability="
            f"{report.stable_repeated_identity_groups}/{report.repeated_identity_groups} "
            f"independent="
            f"{report.stable_independent_repeat_groups}/{report.independent_repeat_groups} "
            f"cache_derived="
            f"{report.stable_cache_derived_repeat_groups}/"
            f"{report.cache_derived_repeat_groups}",
        ]
    )


def render_semantic_corpus_report_json(report: SemanticCorpusReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)
