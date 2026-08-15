"""Pure verdict model for persisted knowledge-summary admissibility.

This module is deliberately stdlib-only. It owns the machine-readable verdict
statuses/reasons and the pure evaluation over a persisted summary plus
deterministic facts already materialized into the knowledge index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

STATUS_ADMISSIBLE = "admissible"
STATUS_ADMISSIBLE_WITH_REDUCED_RANK = "admissible_with_reduced_rank"
STATUS_INADMISSIBLE = "inadmissible"

RANK_FULL = "full"
RANK_REDUCED = "reduced"
RANK_EXCLUDED = "excluded"

SOURCE_STATE_UNCHANGED = "unchanged"
SOURCE_STATE_CHANGED = "changed"
SOURCE_STATE_MOVED = "moved"
SOURCE_STATE_DELETED = "deleted"
SOURCE_STATE_RELEVANCE_INDETERMINATE = "relevance_indeterminate"
SOURCE_STATE_SOUNDNESS_INDETERMINATE = "soundness_indeterminate"

RESOLUTION_RESOLVED = "resolved"
RESOLUTION_UNRESOLVED = "unresolved"
RESOLUTION_INDETERMINATE = "indeterminate"

REASON_SOURCES_CHANGED = "sources_changed"
REASON_SOURCES_MOVED = "sources_moved"
REASON_RELEVANCE_INDETERMINATE = "relevance_indeterminate"
REASON_CITED_SOURCE_DELETED = "cited_source_deleted"
REASON_SOURCE_RUN_TAINTED = "source_run_tainted"
REASON_PROVENANCE_UNRESOLVED = "provenance_unresolved"
REASON_SOUNDNESS_INDETERMINATE = "soundness_indeterminate"
REASON_NO_VERDICT = "no_verdict"

_INADMISSIBLE_REASONS = frozenset(
    {
        REASON_CITED_SOURCE_DELETED,
        REASON_SOURCE_RUN_TAINTED,
        REASON_PROVENANCE_UNRESOLVED,
        REASON_SOUNDNESS_INDETERMINATE,
        REASON_NO_VERDICT,
    }
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


@dataclass(frozen=True)
class KnowledgeSourceFact:
    cited_path: str
    state: str
    current_path: str | None = None
    commits_since_summary: int | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "cited_path": self.cited_path,
            "state": self.state,
        }
        if self.current_path is not None:
            data["current_path"] = self.current_path
        if self.commits_since_summary is not None:
            data["commits_since_summary"] = self.commits_since_summary
        return data

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "KnowledgeSourceFact | None":
        cited_path = _text(raw.get("cited_path"))
        state = _text(raw.get("state"))
        if not cited_path or not state:
            return None

        commits = raw.get("commits_since_summary")
        commits_since_summary = commits if isinstance(commits, int) and commits >= 0 else None
        current_path = _text(raw.get("current_path")) or None
        return cls(
            cited_path=cited_path,
            state=state,
            current_path=current_path,
            commits_since_summary=commits_since_summary,
        )


@dataclass(frozen=True)
class KnowledgeSummaryFacts:
    source_run_tainted: bool
    source_run_resolution: str
    provenance_resolution: str
    cited_sources: tuple[KnowledgeSourceFact, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "source_run": {
                "tainted": self.source_run_tainted,
                "resolution": self.source_run_resolution,
            },
            "provenance": {"resolution": self.provenance_resolution},
            "cited_sources": [source.to_dict() for source in self.cited_sources],
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "KnowledgeSummaryFacts":
        source_run_raw = raw.get("source_run")
        provenance_raw = raw.get("provenance")
        sources_raw = raw.get("cited_sources")

        source_run = source_run_raw if isinstance(source_run_raw, Mapping) else {}
        provenance = provenance_raw if isinstance(provenance_raw, Mapping) else {}
        cited_sources: list[KnowledgeSourceFact] = []
        if isinstance(sources_raw, list):
            for item in sources_raw:
                if isinstance(item, Mapping):
                    source = KnowledgeSourceFact.from_mapping(item)
                    if source is not None:
                        cited_sources.append(source)

        return cls(
            source_run_tainted=bool(source_run.get("tainted")),
            source_run_resolution=_text(source_run.get("resolution")) or RESOLUTION_INDETERMINATE,
            provenance_resolution=_text(provenance.get("resolution")) or RESOLUTION_INDETERMINATE,
            cited_sources=tuple(cited_sources),
        )


@dataclass(frozen=True)
class KnowledgeSummaryVerdict:
    status: str
    rank: str
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "status": self.status,
            "rank": self.rank,
        }
        if self.reasons:
            data["reasons"] = list(self.reasons)
        return data


def missing_verdict() -> KnowledgeSummaryVerdict:
    return KnowledgeSummaryVerdict(
        status=STATUS_INADMISSIBLE,
        rank=RANK_EXCLUDED,
        reasons=(REASON_NO_VERDICT,),
    )


def interpret_persisted_verdict(raw: Any) -> KnowledgeSummaryVerdict:
    """Return a safe verdict for persisted data, failing closed on absence/shape."""
    if not isinstance(raw, Mapping):
        return missing_verdict()

    status = _text(raw.get("status"))
    rank = _text(raw.get("rank"))
    reasons_raw = raw.get("reasons")
    reasons: list[str] = []
    if isinstance(reasons_raw, list):
        for item in reasons_raw:
            reason = _text(item)
            if reason:
                reasons.append(reason)

    if not status or not rank:
        return missing_verdict()

    return KnowledgeSummaryVerdict(status=status, rank=rank, reasons=tuple(reasons))


def _summary_cited_paths(summary: Mapping[str, Any]) -> frozenset[str]:
    """Return the file-path citations named directly by the persisted summary."""
    cited: set[str] = set()
    learned = summary.get("what_was_learned")
    if not isinstance(learned, list):
        return frozenset()

    for claim in learned:
        if not isinstance(claim, Mapping):
            continue
        evidence = claim.get("evidence")
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            if _text(item.get("type")).lower() != "file":
                continue
            path = _text(item.get("path") or item.get("reference"))
            if path:
                cited.add(path)
    return frozenset(cited)


def evaluate_summary_admissibility(
    summary: Mapping[str, Any],
    facts: KnowledgeSummaryFacts,
) -> KnowledgeSummaryVerdict:
    """Evaluate one persisted summary from its own content plus deterministic facts."""

    reasons: list[str] = []

    summary_paths = _summary_cited_paths(summary)
    fact_paths = {source.cited_path for source in facts.cited_sources}
    if not summary_paths.issubset(fact_paths):
        reasons.append(REASON_SOUNDNESS_INDETERMINATE)

    if facts.source_run_resolution == RESOLUTION_INDETERMINATE:
        reasons.append(REASON_SOUNDNESS_INDETERMINATE)
    if facts.source_run_tainted:
        reasons.append(REASON_SOURCE_RUN_TAINTED)

    if facts.provenance_resolution == RESOLUTION_INDETERMINATE:
        reasons.append(REASON_SOUNDNESS_INDETERMINATE)
    elif facts.provenance_resolution == RESOLUTION_UNRESOLVED:
        reasons.append(REASON_PROVENANCE_UNRESOLVED)

    for source in facts.cited_sources:
        if source.state == SOURCE_STATE_CHANGED:
            reasons.append(REASON_SOURCES_CHANGED)
        elif source.state == SOURCE_STATE_MOVED:
            reasons.append(REASON_SOURCES_MOVED)
        elif source.state == SOURCE_STATE_DELETED:
            reasons.append(REASON_CITED_SOURCE_DELETED)
        elif source.state == SOURCE_STATE_RELEVANCE_INDETERMINATE:
            reasons.append(REASON_RELEVANCE_INDETERMINATE)
        elif source.state == SOURCE_STATE_SOUNDNESS_INDETERMINATE:
            reasons.append(REASON_SOUNDNESS_INDETERMINATE)

    unique_reasons = tuple(dict.fromkeys(reasons))
    if any(reason in _INADMISSIBLE_REASONS for reason in unique_reasons):
        return KnowledgeSummaryVerdict(
            status=STATUS_INADMISSIBLE,
            rank=RANK_EXCLUDED,
            reasons=unique_reasons,
        )
    if unique_reasons:
        return KnowledgeSummaryVerdict(
            status=STATUS_ADMISSIBLE_WITH_REDUCED_RANK,
            rank=RANK_REDUCED,
            reasons=unique_reasons,
        )
    return KnowledgeSummaryVerdict(status=STATUS_ADMISSIBLE, rank=RANK_FULL)
