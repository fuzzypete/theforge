"""Does the knowledge feed-forward loop earn its keep? (#1867)

Layer 4 of ``docs/plans/knowledge-capture.md``: the measurement that decides
whether Layers 1-3 compound or are merely maintained. Every input is recorded
telemetry, read by :mod:`theforge.knowledge_effectiveness_signals` — the audit
record's ``context_manifests``, ``plan_review.regenerated``,
``iterations.review_loop``, ``cost.total_usd`` and ``outcome.success``. No
sentence a model wrote is admitted as evidence, which is the whole point: a
knowledge layer that reports on itself in prose proves nothing.

Two ideas carry this half:

1. **Comparisons are bucketed before they are made.** "Stories that had prior
   summaries did better" is worthless if those stories were also smaller. Runs
   are bucketed by preflight work type, complexity band and domains, and only
   buckets holding *both* cohorts contribute to the matched comparison.

2. **Each metric carries its own denominator.** A record with no ``plan_review``
   block does not mean "zero regenerations", and a successful run with
   ``cost.total_usd`` null is a delivery of unknown spend — counted in its
   cohort, excluded from both cost denominators with that exclusion stated. A
   metric whose denominator is too small reports ``insufficient_data`` rather
   than a number nobody should act on, which is the distinction the whole
   report exists to make: "we cannot tell yet" is not "it did not help".
"""

from __future__ import annotations

from dataclasses import dataclass

from theforge.knowledge_effectiveness_signals import (
    COHORT_UNCLASSIFIED,
    COHORT_WITH,
    COHORT_WITHOUT,
    RunSignals,
    classify_cohort,
    extract_signals,
)

__all__ = [
    "COHORT_UNCLASSIFIED",
    "COHORT_WITH",
    "COHORT_WITHOUT",
    "MIN_COHORT_RUNS",
    "MIN_METRIC_SAMPLES",
    "METRIC_COST_PER_STORY",
    "METRIC_DEV_ITERATIONS",
    "METRIC_DIRECTION",
    "METRIC_NAMES",
    "METRIC_PLAN_REGENERATION",
    "PRIMARY_VERDICT_METRIC",
    "METRIC_REVIEW_CYCLES",
    "METRIC_REVIEW_RECURRENCE",
    "METRIC_STORIES_PER_DOLLAR",
    "STATUS_INSUFFICIENT_DATA",
    "STATUS_NO_OBSERVED_IMPROVEMENT",
    "STATUS_OBSERVED_IMPROVEMENT",
    "CohortMetrics",
    "KnowledgeEffectivenessReport",
    "MatchedBucket",
    "Metric",
    "MetricComparison",
    "RunSignals",
    "TrendPoint",
    "build_report",
    "classify_cohort",
    "compute_cohort_metrics",
    "extract_signals",
]

# ── Statuses ─────────────────────────────────────────────────────────────────

STATUS_INSUFFICIENT_DATA = "insufficient_data"
STATUS_NO_OBSERVED_IMPROVEMENT = "no_observed_improvement"
STATUS_OBSERVED_IMPROVEMENT = "observed_improvement"

#: Runs each cohort must contribute to the matched buckets before any comparison
#: is reported. Deliberately small — the plan's own bar is 20-30 runs, and a
#: report that stays silent until then teaches nothing about its own shape — but
#: never 1, where a single run would decide the verdict.
MIN_COHORT_RUNS = 3

#: Observations a single metric needs on *both* sides before it is compared.
#: A metric can be under-sampled while its cohort is not: plan_review telemetry
#: is missing from more records than cost telemetry is.
MIN_METRIC_SAMPLES = 3


# ── Metrics ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Metric:
    """A value and the denominator it was computed over.

    ``value is None`` means the metric could not be computed at all; a value
    with a small ``sample_size`` means it could, but should not be compared.
    """

    name: str
    value: float | None
    sample_size: int
    lower_is_better: bool

    @property
    def available(self) -> bool:
        return self.value is not None and self.sample_size > 0

    @property
    def comparable(self) -> bool:
        return self.available and self.sample_size >= MIN_METRIC_SAMPLES


@dataclass(frozen=True)
class CohortMetrics:
    """Rollup of one cohort over one population of runs."""

    cohort: str
    run_count: int
    metrics: tuple[Metric, ...]
    measured_cost_run_count: int = 0
    unmeasured_cost_run_count: int = 0

    def metric(self, name: str) -> Metric | None:
        for item in self.metrics:
            if item.name == name:
                return item
        return None


METRIC_PLAN_REGENERATION = "plan_regeneration_rate"
METRIC_REVIEW_RECURRENCE = "review_recurrence_rate"
METRIC_DEV_ITERATIONS = "avg_dev_iterations"
METRIC_REVIEW_CYCLES = "avg_review_cycles"
METRIC_COST_PER_STORY = "cost_per_completed_story"
METRIC_STORIES_PER_DOLLAR = "stories_per_dollar"
PRIMARY_VERDICT_METRIC = METRIC_COST_PER_STORY

#: Ordered for rendering; also the set the status verdict reads. Direction is
#: declared here so "improved" is never inferred from a metric's name.
METRIC_DIRECTION: dict[str, bool] = {
    METRIC_PLAN_REGENERATION: True,
    METRIC_REVIEW_RECURRENCE: True,
    METRIC_DEV_ITERATIONS: True,
    METRIC_REVIEW_CYCLES: True,
    METRIC_COST_PER_STORY: True,
    METRIC_STORIES_PER_DOLLAR: False,
}
METRIC_NAMES = tuple(METRIC_DIRECTION)


def _rate(numerator: float, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator > 0 else None


def _mean(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def compute_cohort_metrics(cohort: str, runs: list[RunSignals]) -> CohortMetrics:
    """Roll one cohort's runs up into the six reported metrics."""
    regen = [run.plan_regenerated for run in runs if run.plan_regenerated is not None]
    recurrence_runs = [
        run for run in runs if run.total_findings is not None and run.total_findings > 0
    ]
    restated_total = sum(run.restated_findings or 0 for run in recurrence_runs)
    findings_total = sum(run.total_findings or 0 for run in recurrence_runs)
    dev_iters = [run.dev_iterations for run in runs if run.dev_iterations is not None]
    review_cycles = [run.review_cycles for run in runs if run.review_cycles is not None]

    measured_cost_runs = [run for run in runs if run.cost_usd is not None]
    successful_measured_cost_runs = [run for run in measured_cost_runs if run.success]
    spend = sum(run.cost_usd or 0.0 for run in measured_cost_runs)
    completed_story_count = len(successful_measured_cost_runs)

    values: dict[str, tuple[float | None, int]] = {
        METRIC_PLAN_REGENERATION: (
            _rate(sum(1 for flag in regen if flag), len(regen)),
            len(regen),
        ),
        METRIC_REVIEW_RECURRENCE: (
            _rate(restated_total, findings_total),
            len(recurrence_runs),
        ),
        METRIC_DEV_ITERATIONS: (_mean(dev_iters), len(dev_iters)),
        METRIC_REVIEW_CYCLES: (_mean(review_cycles), len(review_cycles)),
        METRIC_COST_PER_STORY: (
            round(spend / completed_story_count, 4) if completed_story_count else None,
            completed_story_count,
        ),
        # Zero completed stories is not a measurable velocity, even if the
        # cohort spent real money getting there.
        METRIC_STORIES_PER_DOLLAR: (
            round(completed_story_count / spend, 4)
            if completed_story_count and spend > 0
            else None,
            completed_story_count,
        ),
    }
    return CohortMetrics(
        cohort=cohort,
        run_count=len(runs),
        metrics=tuple(
            Metric(
                name=name,
                value=values[name][0],
                sample_size=values[name][1],
                lower_is_better=METRIC_DIRECTION[name],
            )
            for name in METRIC_NAMES
        ),
        measured_cost_run_count=len(measured_cost_runs),
        unmeasured_cost_run_count=sum(1 for run in runs if run.cost_usd is None),
    )


# ── Comparison and report ────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetricComparison:
    """One metric held side by side across the two cohorts."""

    name: str
    lower_is_better: bool
    with_prior: Metric
    without_prior: Metric
    comparative_claim_supported: bool = True
    comparison_note: str | None = None

    @property
    def comparable(self) -> bool:
        return self.with_prior.comparable and self.without_prior.comparable

    @property
    def delta(self) -> float | None:
        if not self.comparative_claim_supported or not self.comparable:
            return None
        with_value = self.with_prior.value
        without_value = self.without_prior.value
        if (
            with_value is None or without_value is None
        ):  # pragma: no cover - comparable implies set
            return None
        return round(with_value - without_value, 4)

    @property
    def improved(self) -> bool | None:
        """True when the with-prior cohort is better on this metric."""
        delta = self.delta
        if delta is None:
            return None
        return delta < 0 if self.lower_is_better else delta > 0


@dataclass(frozen=True)
class MatchedBucket:
    """A comparability bucket holding runs from both cohorts."""

    work_type: str
    complexity: str
    domains: tuple[str, ...]
    with_prior_runs: int
    without_prior_runs: int


@dataclass(frozen=True)
class TrendPoint:
    """Stories-per-dollar for one half of the classified window."""

    label: str
    stories_per_dollar: float | None
    completed_stories: int
    measured_cost_usd: float
    measured_cost_run_count: int = 0
    unmeasured_cost_run_count: int = 0


@dataclass(frozen=True)
class KnowledgeEffectivenessReport:
    """The full report: window, cohorts, matched comparison, verdict."""

    since: str | None
    until: str | None
    recent_run_count: int | None
    records_considered: int
    cohort_counts: dict[str, int]
    matched_buckets: tuple[MatchedBucket, ...]
    overall: tuple[CohortMetrics, ...]
    matched: tuple[CohortMetrics, ...]
    comparisons: tuple[MetricComparison, ...]
    stories_per_dollar_trend: tuple[TrendPoint, ...]
    status: str
    status_reason: str

    def cohort(self, name: str, *, matched: bool = True) -> CohortMetrics | None:
        for item in self.matched if matched else self.overall:
            if item.cohort == name:
                return item
        return None

    def comparison(self, name: str) -> MetricComparison | None:
        for item in self.comparisons:
            if item.name == name:
                return item
        return None


def build_report(
    records: list[dict],
    *,
    since: str | None = None,
    until: str | None = None,
    recent_run_count: int | None = None,
) -> KnowledgeEffectivenessReport:
    """Build the effectiveness report from already-windowed audit records.

    ``since`` / ``until`` / ``recent_run_count`` are recorded verbatim so the
    rendered report states the window it measured; filtering itself belongs to
    the caller, which holds the substrate.
    """
    signals = [extract_signals(record) for record in records]
    cohort_counts = {
        COHORT_WITH: sum(1 for s in signals if s.cohort == COHORT_WITH),
        COHORT_WITHOUT: sum(1 for s in signals if s.cohort == COHORT_WITHOUT),
        COHORT_UNCLASSIFIED: sum(1 for s in signals if s.cohort == COHORT_UNCLASSIFIED),
    }

    matched_buckets, matched_runs = _match_buckets(signals)
    overall = tuple(
        compute_cohort_metrics(name, [s for s in signals if s.cohort == name])
        for name in (COHORT_WITH, COHORT_WITHOUT)
    )
    matched = tuple(
        compute_cohort_metrics(name, [s for s in matched_runs if s.cohort == name])
        for name in (COHORT_WITH, COHORT_WITHOUT)
    )
    with_metrics, without_metrics = matched
    comparison_note = (
        "descriptive only: without_prior_summary forms only when enabled prior-run "
        "selection included nothing, so this classifier cannot produce an "
        "independently assigned control cohort"
    )
    comparisons = _compare(
        with_metrics,
        without_metrics,
        comparative_claim_supported=False,
        comparison_note=comparison_note,
    )
    status, reason = _verdict(with_metrics, without_metrics, comparisons)

    return KnowledgeEffectivenessReport(
        since=since,
        until=until,
        recent_run_count=recent_run_count,
        records_considered=len(signals),
        cohort_counts=cohort_counts,
        matched_buckets=matched_buckets,
        overall=overall,
        matched=matched,
        comparisons=comparisons,
        stories_per_dollar_trend=_trend([s for s in signals if s.classified]),
        status=status,
        status_reason=reason,
    )


def _compare(
    with_metrics: CohortMetrics,
    without_metrics: CohortMetrics,
    *,
    comparative_claim_supported: bool,
    comparison_note: str | None,
) -> tuple[MetricComparison, ...]:
    """Hold every metric side by side. Both cohorts always carry all six."""
    comparisons: list[MetricComparison] = []
    for name in METRIC_NAMES:
        with_metric = with_metrics.metric(name)
        without_metric = without_metrics.metric(name)
        if with_metric is None or without_metric is None:  # pragma: no cover - always present
            continue
        comparisons.append(
            MetricComparison(
                name=name,
                lower_is_better=METRIC_DIRECTION[name],
                with_prior=with_metric,
                without_prior=without_metric,
                comparative_claim_supported=comparative_claim_supported,
                comparison_note=comparison_note,
            )
        )
    return tuple(comparisons)


def _match_buckets(
    signals: list[RunSignals],
) -> tuple[tuple[MatchedBucket, ...], list[RunSignals]]:
    """Keep only buckets that hold both cohorts — the comparable population."""
    grouped: dict[tuple[str, str, tuple[str, ...]], list[RunSignals]] = {}
    for signal in signals:
        if signal.bucket is None or not signal.classified:
            continue
        grouped.setdefault(signal.bucket, []).append(signal)

    buckets: list[MatchedBucket] = []
    matched_runs: list[RunSignals] = []
    for key in sorted(grouped):
        members = grouped[key]
        with_count = sum(1 for s in members if s.cohort == COHORT_WITH)
        without_count = len(members) - with_count
        if with_count == 0 or without_count == 0:
            continue
        work_type, complexity, domains = key
        buckets.append(
            MatchedBucket(
                work_type=work_type,
                complexity=complexity,
                domains=domains,
                with_prior_runs=with_count,
                without_prior_runs=without_count,
            )
        )
        matched_runs.extend(members)
    return tuple(buckets), matched_runs


def _trend(classified: list[RunSignals]) -> tuple[TrendPoint, ...]:
    """Stories-per-dollar for the earlier and later half of the window.

    The cohort comparison answers "did prior knowledge help this story"; this
    answers the plan's other question — "does the same budget buy more over
    time" — from the same measured-cost denominator.
    """
    ordered = sorted(classified, key=lambda s: (s.started_at or "", s.run_id))
    if len(ordered) < 2:
        return ()
    midpoint = len(ordered) // 2
    return (
        _trend_point("earlier", ordered[:midpoint]),
        _trend_point("later", ordered[midpoint:]),
    )


def _trend_point(label: str, runs: list[RunSignals]) -> TrendPoint:
    measured_cost_runs = [run for run in runs if run.cost_usd is not None]
    successful_measured_cost_runs = [run for run in measured_cost_runs if run.success]
    spend = sum(run.cost_usd or 0.0 for run in measured_cost_runs)
    return TrendPoint(
        label=label,
        stories_per_dollar=(
            round(len(successful_measured_cost_runs) / spend, 4)
            if successful_measured_cost_runs and spend > 0
            else None
        ),
        completed_stories=len(successful_measured_cost_runs),
        measured_cost_usd=round(spend, 4),
        measured_cost_run_count=len(measured_cost_runs),
        unmeasured_cost_run_count=sum(1 for run in runs if run.cost_usd is None),
    )


def _verdict(
    with_metrics: CohortMetrics,
    without_metrics: CohortMetrics,
    comparisons: tuple[MetricComparison, ...],
) -> tuple[str, str]:
    """Separate "we cannot tell yet" from "we can tell, and it did not help"."""
    if comparisons and not comparisons[0].comparative_claim_supported:
        note = comparisons[0].comparison_note or "descriptive only comparison"
        return (
            STATUS_INSUFFICIENT_DATA,
            f"{note}; a causal with-prior vs without-prior verdict is unreachable "
            "under the current cohort classifier",
        )
    if with_metrics.run_count < MIN_COHORT_RUNS or without_metrics.run_count < MIN_COHORT_RUNS:
        return (
            STATUS_INSUFFICIENT_DATA,
            f"matched cohorts hold {with_metrics.run_count} with-prior and "
            f"{without_metrics.run_count} without-prior run(s); "
            f"{MIN_COHORT_RUNS} of each are required before comparing",
        )

    comparable = [item for item in comparisons if item.comparable]
    if not comparable:
        return (
            STATUS_INSUFFICIENT_DATA,
            f"no metric has {MIN_METRIC_SAMPLES} observations in both cohorts; "
            "the runs are matched but their telemetry is not recorded on both sides",
        )

    # build_report's shipped cohorts currently gate above as descriptive-only.
    # This primary-metric branch remains for any future independently assigned
    # comparison that reuses the report model.
    primary = next((item for item in comparisons if item.name == PRIMARY_VERDICT_METRIC), None)
    if primary is None or not primary.comparable:
        return (
            STATUS_INSUFFICIENT_DATA,
            f"primary verdict metric {PRIMARY_VERDICT_METRIC} needs "
            f"{MIN_METRIC_SAMPLES} observations in both cohorts before comparing",
        )

    if primary.improved:
        return (
            STATUS_OBSERVED_IMPROVEMENT,
            f"primary verdict metric {PRIMARY_VERDICT_METRIC} improved with prior summaries",
        )
    return (
        STATUS_NO_OBSERVED_IMPROVEMENT,
        f"primary verdict metric {PRIMARY_VERDICT_METRIC} did not improve with prior summaries",
    )
