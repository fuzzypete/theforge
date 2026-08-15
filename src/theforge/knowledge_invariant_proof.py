"""Did selective invariant context reduce churn? (#1875)

The measurement half of the invariant-index spike, and deliberately a sibling of
:mod:`theforge.knowledge_effectiveness` rather than a section inside it: the two
measure different treatments over the same audit records, and a spike's proof
machinery should be removable without touching the shipped knowledge loop.

The proof reuses that module's cohort metrics wholesale, restricted to the four
**churn** metrics the story names — plan regeneration, review cycles, dev
iterations, restated-finding rate. Cost and stories-per-dollar are deliberately
absent: the spike's success claim is churn reduction, and token or spend
movement is secondary telemetry that must not be able to carry the verdict.

The default answer is ``insufficient_data``. A spike that reports a number off
two runs has proved nothing, and saying so is the point.
"""

from __future__ import annotations

from dataclasses import dataclass

from theforge.knowledge_effectiveness import (
    METRIC_DEV_ITERATIONS,
    METRIC_DIRECTION,
    METRIC_PLAN_REGENERATION,
    METRIC_REVIEW_CYCLES,
    METRIC_REVIEW_RECURRENCE,
    MIN_COHORT_RUNS,
    STATUS_INSUFFICIENT_DATA,
    STATUS_NO_OBSERVED_IMPROVEMENT,
    STATUS_OBSERVED_IMPROVEMENT,
    CohortMetrics,
    Metric,
    compute_cohort_metrics,
)
from theforge.knowledge_effectiveness_signals import (
    COHORT_UNCLASSIFIED,
    INVARIANT_COHORT_WITH,
    INVARIANT_COHORT_WITHOUT,
    RunSignals,
    classify_invariant_cohort,
    extract_signals,
)

__all__ = [
    "CHURN_METRICS",
    "INVARIANT_COHORT_WITH",
    "INVARIANT_COHORT_WITHOUT",
    "InvariantChurnComparison",
    "InvariantContextProof",
    "InvariantSelectionCounts",
    "build_invariant_proof",
    "build_invariant_proof_from_records",
    "classify_invariant_cohort",
]

#: The churn signals this proof reports. Token and cost movement are excluded on
#: purpose — see the module docstring.
CHURN_METRICS = (
    METRIC_PLAN_REGENERATION,
    METRIC_REVIEW_RECURRENCE,
    METRIC_DEV_ITERATIONS,
    METRIC_REVIEW_CYCLES,
)


@dataclass(frozen=True)
class InvariantChurnComparison:
    """One churn metric held side by side across the invariant cohorts."""

    name: str
    lower_is_better: bool
    with_invariants: Metric
    without_invariants: Metric

    @property
    def comparable(self) -> bool:
        return self.with_invariants.comparable and self.without_invariants.comparable

    @property
    def delta(self) -> float | None:
        if not self.comparable:
            return None
        with_value = self.with_invariants.value
        without_value = self.without_invariants.value
        # comparable implies both are set
        if with_value is None or without_value is None:  # pragma: no cover
            return None
        return round(with_value - without_value, 4)

    @property
    def improved(self) -> bool | None:
        delta = self.delta
        if delta is None:
            return None
        return delta < 0 if self.lower_is_better else delta > 0


@dataclass(frozen=True)
class InvariantSelectionCounts:
    """What the selector actually did, summed over the classified runs.

    This is scope-decision telemetry, not a success measure: a high
    ``uncertain`` share means the conservative fallback carried the proof, which
    is exactly the thing the adoption decision needs to know.
    """

    runs_with_telemetry: int
    included: int
    uncertain: int
    dropped: int

    @property
    def uncertain_share(self) -> float | None:
        return round(self.uncertain / self.included, 4) if self.included else None


@dataclass(frozen=True)
class InvariantContextProof:
    """The spike's proof section: cohorts, churn comparison, and a verdict."""

    cohort_counts: dict[str, int]
    with_metrics: CohortMetrics
    without_metrics: CohortMetrics
    comparisons: tuple[InvariantChurnComparison, ...]
    selection_counts: InvariantSelectionCounts
    status: str
    status_reason: str

    def comparison(self, name: str) -> InvariantChurnComparison | None:
        for item in self.comparisons:
            if item.name == name:
                return item
        return None


def build_invariant_proof(signals: list[RunSignals]) -> InvariantContextProof:
    """Build the invariant-context proof from already-extracted run signals."""
    with_runs = [s for s in signals if s.invariant_cohort == INVARIANT_COHORT_WITH]
    without_runs = [s for s in signals if s.invariant_cohort == INVARIANT_COHORT_WITHOUT]
    cohort_counts = {
        INVARIANT_COHORT_WITH: len(with_runs),
        INVARIANT_COHORT_WITHOUT: len(without_runs),
        COHORT_UNCLASSIFIED: sum(1 for s in signals if not s.invariant_classified),
    }

    with_metrics = compute_cohort_metrics(INVARIANT_COHORT_WITH, with_runs)
    without_metrics = compute_cohort_metrics(INVARIANT_COHORT_WITHOUT, without_runs)
    comparisons = tuple(
        InvariantChurnComparison(
            name=name,
            lower_is_better=METRIC_DIRECTION[name],
            with_invariants=with_metrics.metric(name),
            without_invariants=without_metrics.metric(name),
        )
        for name in CHURN_METRICS
        if with_metrics.metric(name) is not None and without_metrics.metric(name) is not None
    )
    status, reason = _verdict(with_metrics, without_metrics, comparisons)
    return InvariantContextProof(
        cohort_counts=cohort_counts,
        with_metrics=with_metrics,
        without_metrics=without_metrics,
        comparisons=comparisons,
        selection_counts=_selection_counts(signals),
        status=status,
        status_reason=reason,
    )


def build_invariant_proof_from_records(records: list[dict]) -> InvariantContextProof:
    """Convenience for callers holding audit records rather than signals."""
    return build_invariant_proof([extract_signals(record) for record in records])


def _selection_counts(signals: list[RunSignals]) -> InvariantSelectionCounts:
    observed = [s for s in signals if s.invariant_included is not None]
    return InvariantSelectionCounts(
        runs_with_telemetry=len(observed),
        included=sum(s.invariant_included or 0 for s in observed),
        uncertain=sum(s.invariant_uncertain or 0 for s in observed),
        dropped=sum(s.invariant_dropped or 0 for s in observed),
    )


def _verdict(
    with_metrics: CohortMetrics,
    without_metrics: CohortMetrics,
    comparisons: tuple[InvariantChurnComparison, ...],
) -> tuple[str, str]:
    """Report "we cannot tell yet" — the honest default for a spike this young."""
    if with_metrics.run_count < MIN_COHORT_RUNS or without_metrics.run_count < MIN_COHORT_RUNS:
        return (
            STATUS_INSUFFICIENT_DATA,
            f"invariant cohorts hold {with_metrics.run_count} with-invariant and "
            f"{without_metrics.run_count} without-invariant run(s); "
            f"{MIN_COHORT_RUNS} of each are required before comparing churn",
        )
    comparable = [item for item in comparisons if item.comparable]
    if not comparable:
        return (
            STATUS_INSUFFICIENT_DATA,
            "no churn metric has enough observations in both invariant cohorts",
        )
    improved = [item.name for item in comparable if item.improved]
    if improved:
        return (
            STATUS_OBSERVED_IMPROVEMENT,
            f"{len(improved)} of {len(comparable)} comparable churn metric(s) improved "
            f"with invariant context: {', '.join(improved)}",
        )
    return (
        STATUS_NO_OBSERVED_IMPROVEMENT,
        f"{len(comparable)} comparable churn metric(s) and none improved with invariant context",
    )
