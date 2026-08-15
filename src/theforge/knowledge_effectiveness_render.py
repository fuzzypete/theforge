"""Presentation for the knowledge-effectiveness report (#1867).

Split from :mod:`theforge.knowledge_effectiveness` so the computation stays a
pure function of audit records: the same report backs the terminal view, the
structured payload, and the tests, and none of them can quietly disagree.

The renderer's one editorial rule: never print a bare number for an
under-sampled metric. ``n=1`` rendered as "0.00" reads like a finding; rendered
as "insufficient" it reads like what it is.
"""

from __future__ import annotations

from theforge.knowledge_effectiveness import (
    COHORT_UNCLASSIFIED,
    COHORT_WITH,
    COHORT_WITHOUT,
    METRIC_COST_PER_STORY,
    METRIC_DEV_ITERATIONS,
    METRIC_PLAN_REGENERATION,
    METRIC_REVIEW_CYCLES,
    METRIC_REVIEW_RECURRENCE,
    METRIC_STORIES_PER_DOLLAR,
    STATUS_INSUFFICIENT_DATA,
    CohortMetrics,
    KnowledgeEffectivenessReport,
    Metric,
    MetricComparison,
)

_METRIC_LABELS = {
    METRIC_PLAN_REGENERATION: "plan regeneration rate",
    METRIC_REVIEW_RECURRENCE: "review restated-finding rate",
    METRIC_DEV_ITERATIONS: "avg dev iterations",
    METRIC_REVIEW_CYCLES: "avg review cycles",
    METRIC_COST_PER_STORY: "cost per completed story",
    METRIC_STORIES_PER_DOLLAR: "stories per dollar",
}

_MONEY_METRICS = frozenset({METRIC_COST_PER_STORY})
_RATE_METRICS = frozenset({METRIC_PLAN_REGENERATION, METRIC_REVIEW_RECURRENCE})


# ── Structured payload ───────────────────────────────────────────────────────


def report_payload(report: KnowledgeEffectivenessReport) -> dict:
    """JSON/YAML-serializable view of the report."""
    return {
        "window": {
            "since": report.since,
            "until": report.until,
            "recent_run_count": report.recent_run_count,
            "records_considered": report.records_considered,
        },
        "status": report.status,
        "status_reason": report.status_reason,
        "cohorts": {
            COHORT_WITH: report.cohort_counts.get(COHORT_WITH, 0),
            COHORT_WITHOUT: report.cohort_counts.get(COHORT_WITHOUT, 0),
            COHORT_UNCLASSIFIED: report.cohort_counts.get(COHORT_UNCLASSIFIED, 0),
        },
        "matched_buckets": [
            {
                "work_type": bucket.work_type,
                "complexity": bucket.complexity,
                "domains": list(bucket.domains),
                "with_prior_runs": bucket.with_prior_runs,
                "without_prior_runs": bucket.without_prior_runs,
            }
            for bucket in report.matched_buckets
        ],
        "matched_comparison": [_comparison_payload(item) for item in report.comparisons],
        "overall": {item.cohort: _cohort_payload(item) for item in report.overall},
        "matched": {item.cohort: _cohort_payload(item) for item in report.matched},
        "stories_per_dollar_trend": [
            {
                "label": point.label,
                "stories_per_dollar": point.stories_per_dollar,
                "completed_stories": point.completed_stories,
                "measured_cost_usd": point.measured_cost_usd,
            }
            for point in report.stories_per_dollar_trend
        ],
    }


def _cohort_payload(cohort: CohortMetrics) -> dict:
    return {
        "run_count": cohort.run_count,
        "metrics": {item.name: _metric_payload(item) for item in cohort.metrics},
    }


def _metric_payload(metric: Metric) -> dict:
    return {
        "value": metric.value,
        "sample_size": metric.sample_size,
        "available": metric.available,
        "comparable": metric.comparable,
        "lower_is_better": metric.lower_is_better,
    }


def _comparison_payload(comparison: MetricComparison) -> dict:
    return {
        "metric": comparison.name,
        "lower_is_better": comparison.lower_is_better,
        "comparable": comparison.comparable,
        "with_prior_summary": _metric_payload(comparison.with_prior),
        "without_prior_summary": _metric_payload(comparison.without_prior),
        "delta": comparison.delta,
        "improved": comparison.improved,
    }


# ── Terminal renderer ────────────────────────────────────────────────────────


def render_terminal(report: KnowledgeEffectivenessReport) -> str:
    """Operator-facing view: window, cohorts, matched comparison, verdict."""
    lines: list[str] = []
    _render_header(report, lines)
    _render_buckets(report, lines)
    _render_comparison(report, lines)
    _render_trend(report, lines)
    _render_verdict(report, lines)
    return "\n".join(lines) + "\n"


def _render_header(report: KnowledgeEffectivenessReport, lines: list[str]) -> None:
    lines.append("Knowledge-loop effectiveness")
    lines.append("=" * 60)
    window = _window_label(report)
    lines.append(f"Window:  {window}")
    lines.append(f"Records: {report.records_considered}")
    counts = report.cohort_counts
    lines.append(
        f"Cohorts: {counts.get(COHORT_WITH, 0)} with prior summaries, "
        f"{counts.get(COHORT_WITHOUT, 0)} without, "
        f"{counts.get(COHORT_UNCLASSIFIED, 0)} unclassified "
        "(prior-run context disabled or not recorded)"
    )
    lines.append("")


def _window_label(report: KnowledgeEffectivenessReport) -> str:
    if report.recent_run_count is not None:
        return f"most recent {report.recent_run_count} run(s)"
    if report.since or report.until:
        return f"{report.since or 'start'} → {report.until or 'now'}"
    return "all recorded runs"


def _render_buckets(report: KnowledgeEffectivenessReport, lines: list[str]) -> None:
    lines.append("Matched comparability buckets (work type / complexity / domains)")
    if not report.matched_buckets:
        lines.append("  none — no bucket holds runs from both cohorts")
        lines.append("")
        return
    for bucket in report.matched_buckets:
        domains = ",".join(bucket.domains) if bucket.domains else "—"
        lines.append(
            f"  {bucket.work_type}/{bucket.complexity}/{domains}: "
            f"{bucket.with_prior_runs} with, {bucket.without_prior_runs} without"
        )
    lines.append("")


def _render_comparison(report: KnowledgeEffectivenessReport, lines: list[str]) -> None:
    with_cohort = report.cohort(COHORT_WITH)
    without_cohort = report.cohort(COHORT_WITHOUT)
    with_n = with_cohort.run_count if with_cohort else 0
    without_n = without_cohort.run_count if without_cohort else 0
    lines.append(f"Matched cohorts: {with_n} with prior / {without_n} without prior")
    header = f"  {'metric':<30} {'with':>12} {'without':>12}  verdict"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for comparison in report.comparisons:
        label = _METRIC_LABELS.get(comparison.name, comparison.name)
        with_cell = _cell(comparison.name, comparison.with_prior)
        without_cell = _cell(comparison.name, comparison.without_prior)
        lines.append(f"  {label:<30} {with_cell:>12} {without_cell:>12}  {_verdict(comparison)}")
    lines.append("")


def _cell(metric_name: str, metric: Metric) -> str:
    if not metric.available:
        return "—"
    value = metric.value
    if value is None:  # pragma: no cover - available implies a value
        return "—"
    if metric_name in _MONEY_METRICS:
        rendered = f"${value:.2f}"
    elif metric_name in _RATE_METRICS:
        rendered = f"{value * 100:.0f}%"
    else:
        rendered = f"{value:.2f}"
    return f"{rendered} (n={metric.sample_size})"


def _verdict(comparison: MetricComparison) -> str:
    if not comparison.comparable:
        return "insufficient data"
    if comparison.improved:
        return f"improved ({comparison.delta:+})"
    if comparison.delta == 0:
        return "unchanged"
    return f"not improved ({comparison.delta:+})"


def _render_trend(report: KnowledgeEffectivenessReport, lines: list[str]) -> None:
    if not report.stories_per_dollar_trend:
        return
    lines.append("Stories per dollar over the window (classified runs, measured cost only)")
    for point in report.stories_per_dollar_trend:
        value = f"{point.stories_per_dollar:.2f}" if point.stories_per_dollar is not None else "—"
        lines.append(
            f"  {point.label:<8} {value:>8}  "
            f"({point.completed_stories} completed, ${point.measured_cost_usd:.2f} measured)"
        )
    lines.append("")


def _render_verdict(report: KnowledgeEffectivenessReport, lines: list[str]) -> None:
    marker = "?" if report.status == STATUS_INSUFFICIENT_DATA else "→"
    lines.append(f"{marker} {report.status}: {report.status_reason}")
