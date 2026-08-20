"""Pure derivation and aggregation for the #1848 finding-fate spike."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from theforge.coordinator.trust_status import filter_tainted_records, is_tainted
from theforge.model_profiles_read_model import _recency_params, _weighted_rate
from theforge.reviewer_value import DEFAULT_VALUE_MIN_RUNS

ADDRESSED = "addressed"
SURVIVED = "survived"

DISMISSED_MISSING_EVENT = (
    "structured per-finding developer resolution/dismissal event keyed by "
    "finding_id and review cycle"
)
CONTRADICTED_MISSING_EVENT = (
    "structured later-reviewer contradiction relation keyed by finding_id, "
    "review cycle, and contradicting reviewer"
)

_SURVIVED_DISPOSITIONS = frozenset({"unresolved", "regression", "ac_blocking"})
_ADJACENT_CONTRADICTION_MARKERS = frozenset({"gate_contradicted", "downgraded"})


@dataclass
class _RunReviewerStats:
    attributable_findings: int = 0
    derivable_findings: int = 0
    addressed_findings: int = 0
    survived_findings: int = 0
    excluded_missing_fate: int = 0
    gate_contradicted_markers: int = 0
    downgraded_markers: int = 0


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def derive_finding_fate(finding: Mapping[str, object]) -> str | None:
    """Return the mechanically derivable fate for one P1 finding, if any.

    The spike refuses to approximate from prose or adjacent same-cycle labels:

    - ``fixed`` is the only structured addressed fate.
    - multi-cycle still-blocking records are the only structured survived fate.
    - ``gate_contradicted`` / ``downgraded`` are *not* the required later-reviewer
      contradiction event, so they remain adjacent observations rather than a
      contradicted fate.
    - ``net_new`` / ``diff_ungrounded`` / same-cycle blocking end states carry no
      downstream fate by themselves, so they stay excluded rather than guessed.
    """
    disposition = str(finding.get("disposition") or "")
    if disposition == "fixed":
        return ADDRESSED

    first_seen = _as_int(finding.get("cycle_first_seen"))
    last_seen = _as_int(finding.get("cycle_last_seen"))
    if (
        disposition in _SURVIVED_DISPOSITIONS
        and first_seen is not None
        and last_seen is not None
        and last_seen > first_seen
    ):
        return SURVIVED
    return None


def _rate_signal(values: list[float], *, min_runs: int, recency: Any | None) -> dict[str, Any]:
    mode, half_life_runs, window = _recency_params(recency)
    raw = round(sum(values) / len(values), 4) if values else None
    weighted = (
        _weighted_rate(
            values,
            fallback=raw,
            mode=mode,
            half_life_runs=half_life_runs,
            window=window,
            min_samples=min_runs,
        )
        if values
        else None
    )
    return {
        "raw": raw,
        "weighted": weighted,
        "rate": weighted if len(values) >= min_runs else None,
        "runs": len(values),
        "floor": "pass" if len(values) >= min_runs and values else "fail",
    }


def analyze_records(
    records: Iterable[dict],
    *,
    min_runs: int = DEFAULT_VALUE_MIN_RUNS,
    recency: Any | None = None,
) -> dict[str, Any]:
    """Compute the spike report from raw audit records.

    The admissibility population is *review-run observations*, not findings:
    each admissible run contributes at most one addressed-share and one
    survived-share observation per reviewer, so findings from the same run share
    one age in the recency ring and cannot satisfy the ADR-0006 floor as if they
    were independent runs.
    """
    ordered = list(records)
    admissible, tainted_records = filter_tainted_records(ordered)

    summaries: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "findings": 0,
            "derivable_findings": 0,
            "addressed_findings": 0,
            "survived_findings": 0,
            "runs_with_attributable_findings": 0,
            "tainted_runs_excluded": 0,
            "tainted_findings_excluded": 0,
            "excluded_missing_fate": 0,
            "gate_contradicted_markers": 0,
            "downgraded_markers": 0,
            "_addressed_recent": [],
            "_survived_recent": [],
        }
    )
    corpus = {
        "records_scanned": len(ordered),
        "records_with_code_review_findings": 0,
        "records_with_plan_review_findings": 0,
        "admissible_records": len(admissible),
        "tainted_records_excluded": tainted_records,
        "excluded_missing_attribution": 0,
        "excluded_missing_fate": 0,
        "gate_contradicted_markers": 0,
        "downgraded_markers": 0,
    }

    for record in ordered:
        plan_registry = record.get("plan_finding_registry")
        if isinstance(plan_registry, list) and plan_registry:
            corpus["records_with_plan_review_findings"] += 1
        finding_registry = record.get("finding_registry")
        if not (isinstance(finding_registry, list) and finding_registry):
            continue
        corpus["records_with_code_review_findings"] += 1
        if not is_tainted(record.get("trust_status")):
            continue
        reviewers_seen: set[str] = set()
        for finding in finding_registry:
            if not isinstance(finding, dict) or finding.get("severity") != "P1":
                continue
            reporter = finding.get("reporter")
            if not isinstance(reporter, str) or not reporter.strip():
                continue
            reviewer = reporter.strip()
            reviewers_seen.add(reviewer)
            summaries[reviewer]["tainted_findings_excluded"] += 1
        for reviewer in reviewers_seen:
            summaries[reviewer]["tainted_runs_excluded"] += 1

    for record in admissible:
        finding_registry = record.get("finding_registry")
        if not (isinstance(finding_registry, list) and finding_registry):
            continue
        run_stats: dict[str, _RunReviewerStats] = defaultdict(_RunReviewerStats)
        for finding in finding_registry:
            if not isinstance(finding, dict) or finding.get("severity") != "P1":
                continue
            reporter = finding.get("reporter")
            if not isinstance(reporter, str) or not reporter.strip():
                corpus["excluded_missing_attribution"] += 1
                continue
            reviewer = reporter.strip()
            stats = run_stats[reviewer]
            stats.attributable_findings += 1
            disposition = str(finding.get("disposition") or "")
            if disposition == "gate_contradicted":
                stats.gate_contradicted_markers += 1
                corpus["gate_contradicted_markers"] += 1
            if disposition == "downgraded":
                stats.downgraded_markers += 1
                corpus["downgraded_markers"] += 1
            fate = derive_finding_fate(finding)
            if fate is None:
                stats.excluded_missing_fate += 1
                corpus["excluded_missing_fate"] += 1
                continue
            stats.derivable_findings += 1
            if fate == ADDRESSED:
                stats.addressed_findings += 1
            elif fate == SURVIVED:
                stats.survived_findings += 1

        for reviewer, stats in run_stats.items():
            summary = summaries[reviewer]
            summary["findings"] += stats.attributable_findings
            summary["derivable_findings"] += stats.derivable_findings
            summary["addressed_findings"] += stats.addressed_findings
            summary["survived_findings"] += stats.survived_findings
            summary["excluded_missing_fate"] += stats.excluded_missing_fate
            summary["gate_contradicted_markers"] += stats.gate_contradicted_markers
            summary["downgraded_markers"] += stats.downgraded_markers
            summary["runs_with_attributable_findings"] += 1
            if stats.derivable_findings <= 0:
                continue
            summary["_addressed_recent"].append(
                round(stats.addressed_findings / stats.derivable_findings, 4)
            )
            summary["_survived_recent"].append(
                round(stats.survived_findings / stats.derivable_findings, 4)
            )

    reviewers: list[dict[str, Any]] = []
    for reviewer, summary in summaries.items():
        addressed = _rate_signal(
            summary.pop("_addressed_recent"),
            min_runs=min_runs,
            recency=recency,
        )
        survived = _rate_signal(
            summary.pop("_survived_recent"),
            min_runs=min_runs,
            recency=recency,
        )
        reviewers.append(
            {
                "reviewer": reviewer,
                **summary,
                "runs": addressed["runs"],
                "floor": addressed["floor"],
                "addressed_rate": addressed,
                "survived_rate": survived,
            }
        )
    reviewers.sort(
        key=lambda row: (
            -int(row["derivable_findings"]),
            -int(row["runs"]),
            -int(row["findings"]),
            str(row["reviewer"]),
        )
    )

    mode, half_life_runs, window = _recency_params(recency)
    return {
        "corpus": corpus,
        "fate_determination": {
            ADDRESSED: {"status": "derivable", "derivation": "final disposition == fixed"},
            "dismissed": {
                "status": "underivable",
                "missing_event": DISMISSED_MISSING_EVENT,
            },
            "contradicted": {
                "status": "underivable",
                "missing_event": CONTRADICTED_MISSING_EVENT,
            },
            SURVIVED: {
                "status": "derivable",
                "derivation": (
                    "final disposition still blocking/unresolved and "
                    "cycle_last_seen > cycle_first_seen"
                ),
            },
        },
        "weighting": {
            "mode": mode,
            "half_life_runs": half_life_runs,
            "window": window,
            "min_runs": min_runs,
        },
        "reviewers": reviewers,
    }
