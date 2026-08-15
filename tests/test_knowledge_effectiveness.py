"""Tests for the knowledge-loop effectiveness metrics (#1867).

This mirror covers the metric half: cohort rollups, comparability matching,
metric denominators, and the insufficient-data / no-improvement boundary. Record
reading is covered by ``test_knowledge_effectiveness_signals.py``; every input
here is still a synthetic audit record, because the report is a pure function of
recorded telemetry and never touches a substrate or an agent.
"""

from __future__ import annotations

from tests.knowledge_effectiveness_test_helpers import cohorts, record
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
    STATUS_NO_OBSERVED_IMPROVEMENT,
    STATUS_OBSERVED_IMPROVEMENT,
    build_report,
)

# ── Comparability bucketing ───────────────────────────────────────────


class TestBucketMatching:
    def test_only_buckets_holding_both_cohorts_are_matched(self) -> None:
        records = [
            *cohorts({}, {}),
            # A with-prior run in a bucket with no control counterpart.
            record("lonely", cohort="with", work_type="bug", complexity="small"),
        ]
        report = build_report(records)

        assert report.cohort_counts[COHORT_WITH] == 4
        assert len(report.matched_buckets) == 1
        bucket = report.matched_buckets[0]
        assert (bucket.work_type, bucket.complexity, bucket.domains) == (
            "feature",
            "MEDIUM",
            ("backend",),
        )
        assert report.cohort(COHORT_WITH).run_count == 3
        assert report.cohort(COHORT_WITH, matched=False).run_count == 4

    def test_differing_domains_are_not_comparable(self) -> None:
        records = [record(f"with-{i}", cohort="with", domains=("backend",)) for i in range(3)] + [
            record(f"without-{i}", cohort="without", domains=("cli",)) for i in range(3)
        ]
        report = build_report(records)

        assert report.matched_buckets == ()
        assert report.status == STATUS_INSUFFICIENT_DATA

    def test_unclassified_runs_never_enter_a_cohort_metric(self) -> None:
        records = [
            *cohorts({}, {}),
            record("legacy-1", cohort="unclassified", dev_iterations=99),
        ]
        report = build_report(records)

        assert report.cohort_counts[COHORT_UNCLASSIFIED] == 1
        with_cohort = report.cohort(COHORT_WITH)
        without_cohort = report.cohort(COHORT_WITHOUT)
        assert with_cohort is not None and without_cohort is not None
        assert with_cohort.run_count == 3
        assert without_cohort.run_count == 3
        assert without_cohort.metric(METRIC_DEV_ITERATIONS).value == 1.0


# ── Metric computation ────────────────────────────────────────────────


class TestMetricComputation:
    def test_plan_regeneration_rate_uses_recorded_flag(self) -> None:
        records = [
            record("with-0", cohort="with", plan_regenerated=False),
            record("with-1", cohort="with", plan_regenerated=False),
            record("with-2", cohort="with", plan_regenerated=True),
            *(record(f"without-{i}", cohort="without", plan_regenerated=True) for i in range(3)),
        ]
        report = build_report(records)
        comparison = report.comparison(METRIC_PLAN_REGENERATION)

        assert comparison.with_prior.value == 0.3333
        assert comparison.without_prior.value == 1.0
        assert comparison.improved is True

    def test_review_recurrence_rate_pools_findings(self) -> None:
        records = cohorts(
            {"restated": 1, "novel": 3},
            {"restated": 2, "novel": 2},
        )
        report = build_report(records)
        comparison = report.comparison(METRIC_REVIEW_RECURRENCE)

        assert comparison.with_prior.value == 0.25
        assert comparison.without_prior.value == 0.5
        assert comparison.with_prior.sample_size == 3
        assert comparison.improved is True

    def test_runs_with_no_findings_leave_the_recurrence_denominator_empty(self) -> None:
        """Zero findings is no evidence about recurrence, not a 0% recurrence rate."""
        report = build_report(cohorts({"restated": 0, "novel": 0}, {"restated": 1, "novel": 1}))
        comparison = report.comparison(METRIC_REVIEW_RECURRENCE)

        assert comparison.with_prior.sample_size == 0
        assert comparison.with_prior.value is None
        assert comparison.comparable is False

    def test_iteration_averages_come_from_recorded_counts(self) -> None:
        records = cohorts(
            {"dev_iterations": 1, "review_cycles": 1},
            {"dev_iterations": 3, "review_cycles": 2},
        )
        report = build_report(records)

        assert report.comparison(METRIC_DEV_ITERATIONS).with_prior.value == 1.0
        assert report.comparison(METRIC_DEV_ITERATIONS).without_prior.value == 3.0
        assert report.comparison(METRIC_REVIEW_CYCLES).improved is True

    def test_cost_per_completed_story_and_stories_per_dollar(self) -> None:
        records = cohorts({"cost": 2.0}, {"cost": 8.0})
        report = build_report(records)

        assert report.comparison(METRIC_COST_PER_STORY).with_prior.value == 2.0
        assert report.comparison(METRIC_COST_PER_STORY).without_prior.value == 8.0
        assert report.comparison(METRIC_COST_PER_STORY).improved is True
        assert report.comparison(METRIC_STORIES_PER_DOLLAR).with_prior.value == 0.5
        assert report.comparison(METRIC_STORIES_PER_DOLLAR).improved is True

    def test_failed_runs_are_excluded_from_cost_denominators(self) -> None:
        records = cohorts({"cost": 2.0}, {"cost": 8.0})
        records.append(record("with-failed", cohort="with", success=False, cost=50.0))
        report = build_report(records)

        assert report.cohort(COHORT_WITH).run_count == 4
        assert report.comparison(METRIC_COST_PER_STORY).with_prior.sample_size == 3
        assert report.comparison(METRIC_COST_PER_STORY).with_prior.value == 2.0

    def test_unmeasured_cost_is_not_coerced_to_zero(self) -> None:
        """A successful run with null cost is a delivery of unknown spend.

        It stays in the cohort head-count and out of both cost denominators —
        counting it as $0.00 would understate cost per completed story.
        """
        records = cohorts({"cost": 2.0}, {"cost": 8.0})
        records.append(record("with-unmeasured", cohort="with", cost=None))
        report = build_report(records)

        with_cohort = report.cohort(COHORT_WITH)
        assert with_cohort.run_count == 4
        assert with_cohort.metric(METRIC_COST_PER_STORY).sample_size == 3
        assert with_cohort.metric(METRIC_COST_PER_STORY).value == 2.0
        assert with_cohort.metric(METRIC_STORIES_PER_DOLLAR).value == 0.5

    def test_zero_measured_spend_yields_no_stories_per_dollar(self) -> None:
        records = cohorts({"cost": 0.0}, {"cost": 8.0})
        report = build_report(records)

        assert report.cohort(COHORT_WITH).metric(METRIC_STORIES_PER_DOLLAR).value is None
        assert report.comparison(METRIC_STORIES_PER_DOLLAR).comparable is False

    def test_a_cohort_with_no_successful_runs_has_no_cost_metrics(self) -> None:
        records = cohorts({"success": False}, {})
        report = build_report(records)
        with_cohort = report.cohort(COHORT_WITH)

        assert with_cohort.run_count == 3
        assert with_cohort.metric(METRIC_COST_PER_STORY).value is None
        assert with_cohort.metric(METRIC_COST_PER_STORY).sample_size == 0
        assert with_cohort.metric(METRIC_STORIES_PER_DOLLAR).value is None


# ── Missing telemetry ─────────────────────────────────────────────────


class TestMissingTelemetry:
    def test_missing_plan_review_block_makes_the_denominator_unavailable(self) -> None:
        """No plan_review block means "not recorded", never "zero regenerations"."""
        records = [record(f"with-{i}", cohort="with") for i in range(3)]
        for entry in records:
            del entry["plan_review"]
        records.extend(record(f"without-{i}", cohort="without") for i in range(3))
        report = build_report(records)
        comparison = report.comparison(METRIC_PLAN_REGENERATION)

        assert comparison.with_prior.sample_size == 0
        assert comparison.with_prior.value is None
        assert comparison.with_prior.available is False
        assert comparison.comparable is False
        assert comparison.delta is None
        assert comparison.improved is None

    def test_empty_record_set_is_insufficient_data(self) -> None:
        report = build_report([])

        assert report.status == STATUS_INSUFFICIENT_DATA
        assert report.records_considered == 0
        assert report.stories_per_dollar_trend == ()


# ── Status semantics ──────────────────────────────────────────────────


class TestStatusSemantics:
    def test_thin_cohorts_are_insufficient_data_not_no_improvement(self) -> None:
        report = build_report(cohorts({"cost": 2.0}, {"cost": 8.0}, count=2))

        assert report.status == STATUS_INSUFFICIENT_DATA
        assert "required before comparing" in report.status_reason

    def test_identical_cohorts_are_no_observed_improvement(self) -> None:
        report = build_report(cohorts({}, {}))

        assert report.status == STATUS_NO_OBSERVED_IMPROVEMENT
        assert "none improved" in report.status_reason

    def test_worse_with_prior_is_no_observed_improvement(self) -> None:
        report = build_report(
            cohorts({"dev_iterations": 4, "cost": 9.0}, {"dev_iterations": 1, "cost": 2.0})
        )

        assert report.status == STATUS_NO_OBSERVED_IMPROVEMENT

    def test_one_improved_metric_is_observed_improvement(self) -> None:
        report = build_report(cohorts({"dev_iterations": 1}, {"dev_iterations": 3}))

        assert report.status == STATUS_OBSERVED_IMPROVEMENT
        assert METRIC_DEV_ITERATIONS in report.status_reason

    def test_better_stories_per_dollar_alone_is_observed_improvement(self) -> None:
        """The one higher-is-better metric must be read in its own direction."""
        report = build_report(cohorts({"cost": 2.0}, {"cost": 8.0}))

        assert report.status == STATUS_OBSERVED_IMPROVEMENT
        assert METRIC_STORIES_PER_DOLLAR in report.status_reason

    def test_matched_runs_present_but_telemetry_absent_is_insufficient_data(self) -> None:
        """Cohorts are big enough, but no metric has observations on both sides."""
        records = cohorts({}, {})
        for entry in records:
            del entry["plan_review"]
            del entry["iterations"]["review_loop"]
            entry["iterations"]["dev_iterations_productive"] = None
            entry["iterations"]["review_cycles_total"] = None
            entry["cost"]["total_usd"] = None
        report = build_report(records)

        assert report.status == STATUS_INSUFFICIENT_DATA
        assert "telemetry is not recorded on both sides" in report.status_reason

    def test_unbucketed_runs_cannot_carry_the_verdict(self) -> None:
        """Plenty of classified runs, none comparable — still insufficient data."""
        records = cohorts({}, {})
        for entry in records:
            del entry["preflight"]
        report = build_report(records)

        assert report.cohort_counts[COHORT_WITH] == 3
        assert report.matched_buckets == ()
        assert report.status == STATUS_INSUFFICIENT_DATA


# ── Window metadata and trend ─────────────────────────────────────────


class TestWindowAndTrend:
    def test_window_bounds_are_carried_into_the_report(self) -> None:
        report = build_report(
            cohorts({}, {}), since="2026-08-01", until="2026-08-31", recent_run_count=25
        )

        assert (report.since, report.until, report.recent_run_count) == (
            "2026-08-01",
            "2026-08-31",
            25,
        )
        assert report.records_considered == 6

    def test_stories_per_dollar_trend_splits_the_window_chronologically(self) -> None:
        records = [
            record("early-0", cohort="without", cost=10.0, started_at="2026-08-01T00:00:00+00:00"),
            record("early-1", cohort="without", cost=10.0, started_at="2026-08-02T00:00:00+00:00"),
            record("late-0", cohort="with", cost=2.0, started_at="2026-08-03T00:00:00+00:00"),
            record("late-1", cohort="with", cost=2.0, started_at="2026-08-04T00:00:00+00:00"),
        ]
        earlier, later = build_report(records).stories_per_dollar_trend

        assert (earlier.label, earlier.stories_per_dollar) == ("earlier", 0.1)
        assert (later.label, later.stories_per_dollar) == ("later", 0.5)
        assert later.measured_cost_usd == 4.0

    def test_trend_excludes_unclassified_runs_and_unmeasured_spend(self) -> None:
        records = [
            record("early", cohort="with", cost=4.0, started_at="2026-08-01T00:00:00+00:00"),
            record(
                "noise", cohort="unclassified", cost=99.0, started_at="2026-08-02T00:00:00+00:00"
            ),
            record("late", cohort="with", cost=None, started_at="2026-08-03T00:00:00+00:00"),
        ]
        earlier, later = build_report(records).stories_per_dollar_trend

        assert earlier.measured_cost_usd == 4.0
        assert later.completed_stories == 0
        assert later.stories_per_dollar is None
