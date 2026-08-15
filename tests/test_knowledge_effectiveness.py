"""Tests for the knowledge-loop effectiveness report (#1867).

Every input here is a synthetic audit record: the report is a pure function of
recorded telemetry, so the tests assert on cohort classification, metric
denominators, and the insufficient-data / no-improvement boundary without
touching a substrate or an agent.
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
    STATUS_NO_OBSERVED_IMPROVEMENT,
    STATUS_OBSERVED_IMPROVEMENT,
    build_report,
    classify_cohort,
    extract_signals,
)

# ── Record builders ───────────────────────────────────────────────────


def _manifests(cohort: str, *, phase: str = "dev") -> list[dict]:
    """Context manifests that put a run in the requested cohort."""
    if cohort == "unclassified":
        return [
            {
                "phase": phase,
                "prior_run_context": {
                    "enabled": False,
                    "included": [],
                    "dropped": [],
                    "note": "prior-run context disabled (knowledge.prior_run_context)",
                },
            }
        ]
    included = (
        [{"run_id": "prior-1", "reason": "file_overlap", "score": 14}] if cohort == "with" else []
    )
    return [
        {
            "phase": phase,
            "prior_run_context": {
                "enabled": True,
                "included": included,
                "dropped": [],
                "note": "note",
            },
        }
    ]


def _review_loop(*, restated: int, novel: int) -> list[dict]:
    return [
        {
            "iteration": 1,
            "verdict": "APPROVE",
            "novel_findings": novel,
            "restated_findings": restated,
        }
    ]


def _record(
    run_id: str,
    *,
    cohort: str = "with",
    success: bool = True,
    cost: float | None = 4.0,
    plan_regenerated: bool = False,
    restated: int = 0,
    novel: int = 2,
    dev_iterations: int = 1,
    review_cycles: int = 1,
    work_type: str = "feature",
    complexity: str = "medium",
    domains: tuple[str, ...] = ("backend",),
    started_at: str = "2026-08-01T10:00:00+00:00",
) -> dict:
    return {
        "run_id": run_id,
        "task": {"slug": run_id, "name": run_id},
        "timing": {"started_at": started_at},
        "outcome": {"success": success, "final_phase": "DONE"},
        "cost": {"total_usd": cost},
        "preflight": {
            "work_type": work_type,
            "complexity": complexity,
            "complexity_score": 5,
            "domains": list(domains),
        },
        "context_manifests": _manifests(cohort),
        "plan_review": {"decision": "approve", "regenerated": plan_regenerated},
        "iterations": {
            "dev_iterations_productive": dev_iterations,
            "review_cycles_total": review_cycles,
            "review_loop": _review_loop(restated=restated, novel=novel),
        },
    }


def _cohorts(with_kwargs: dict, without_kwargs: dict, *, count: int = 3) -> list[dict]:
    """``count`` runs per cohort, all sharing one comparability bucket."""
    records = []
    for i in range(count):
        records.append(_record(f"with-{i}", cohort="with", **with_kwargs))
        records.append(_record(f"without-{i}", cohort="without", **without_kwargs))
    return records


# ── Cohort classification ─────────────────────────────────────────────


class TestCohortClassification:
    def test_included_prior_summary_is_with_cohort(self) -> None:
        assert classify_cohort(_record("r", cohort="with")) == COHORT_WITH

    def test_enabled_but_nothing_included_is_control_cohort(self) -> None:
        """Enabled with an empty manifest is a genuine control, not unclassified."""
        assert classify_cohort(_record("r", cohort="without")) == COHORT_WITHOUT

    def test_disabled_manifest_is_unclassified(self) -> None:
        assert classify_cohort(_record("r", cohort="unclassified")) == COHORT_UNCLASSIFIED

    def test_missing_context_manifests_is_unclassified(self) -> None:
        record = _record("r")
        del record["context_manifests"]
        assert classify_cohort(record) == COHORT_UNCLASSIFIED

    def test_ineligible_phase_manifest_does_not_create_a_control(self) -> None:
        """Preflight never receives prior-run context, so its manifest proves nothing."""
        record = _record("r")
        record["context_manifests"] = _manifests("without", phase="preflight")
        assert classify_cohort(record) == COHORT_UNCLASSIFIED

    def test_unclassified_runs_never_enter_a_cohort_metric(self) -> None:
        records = [
            *_cohorts({}, {}),
            _record("legacy-1", cohort="unclassified", dev_iterations=99),
        ]
        report = build_report(records)

        assert report.cohort_counts[COHORT_UNCLASSIFIED] == 1
        with_cohort = report.cohort(COHORT_WITH)
        without_cohort = report.cohort(COHORT_WITHOUT)
        assert with_cohort is not None and without_cohort is not None
        assert with_cohort.run_count == 3
        assert without_cohort.run_count == 3
        assert without_cohort.metric(METRIC_DEV_ITERATIONS).value == 1.0


# ── Comparability bucketing ───────────────────────────────────────────


class TestBucketMatching:
    def test_only_buckets_holding_both_cohorts_are_matched(self) -> None:
        records = [
            *_cohorts({}, {}),
            # A with-prior run in a bucket with no control counterpart.
            _record("lonely", cohort="with", work_type="bug", complexity="small"),
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
        records = [_record(f"with-{i}", cohort="with", domains=("backend",)) for i in range(3)] + [
            _record(f"without-{i}", cohort="without", domains=("cli",)) for i in range(3)
        ]
        report = build_report(records)

        assert report.matched_buckets == ()
        assert report.status == STATUS_INSUFFICIENT_DATA


# ── Metric computation ────────────────────────────────────────────────


class TestMetricComputation:
    def test_plan_regeneration_rate_uses_recorded_flag(self) -> None:
        records = [
            _record("with-0", cohort="with", plan_regenerated=False),
            _record("with-1", cohort="with", plan_regenerated=False),
            _record("with-2", cohort="with", plan_regenerated=True),
            *(_record(f"without-{i}", cohort="without", plan_regenerated=True) for i in range(3)),
        ]
        report = build_report(records)
        comparison = report.comparison(METRIC_PLAN_REGENERATION)

        assert comparison.with_prior.value == 0.3333
        assert comparison.without_prior.value == 1.0
        assert comparison.improved is True

    def test_review_recurrence_rate_pools_findings(self) -> None:
        records = _cohorts(
            {"restated": 1, "novel": 3},
            {"restated": 2, "novel": 2},
        )
        report = build_report(records)
        comparison = report.comparison(METRIC_REVIEW_RECURRENCE)

        assert comparison.with_prior.value == 0.25
        assert comparison.without_prior.value == 0.5
        assert comparison.with_prior.sample_size == 3
        assert comparison.improved is True

    def test_repeated_findings_by_severity_is_the_fallback_signal(self) -> None:
        record = _record("r")
        record["iterations"]["review_loop"] = [
            {
                "iteration": 1,
                "finding_counts": {"P1": 2, "P2": 2},
                "repeated_findings_by_severity": {"P1": 1},
            }
        ]
        signals = extract_signals(record)

        assert (signals.restated_findings, signals.total_findings) == (1, 4)

    def test_iteration_averages_come_from_recorded_counts(self) -> None:
        records = _cohorts(
            {"dev_iterations": 1, "review_cycles": 1},
            {"dev_iterations": 3, "review_cycles": 2},
        )
        report = build_report(records)

        assert report.comparison(METRIC_DEV_ITERATIONS).with_prior.value == 1.0
        assert report.comparison(METRIC_DEV_ITERATIONS).without_prior.value == 3.0
        assert report.comparison(METRIC_REVIEW_CYCLES).improved is True

    def test_cost_per_completed_story_and_stories_per_dollar(self) -> None:
        records = _cohorts({"cost": 2.0}, {"cost": 8.0})
        report = build_report(records)

        assert report.comparison(METRIC_COST_PER_STORY).with_prior.value == 2.0
        assert report.comparison(METRIC_COST_PER_STORY).without_prior.value == 8.0
        assert report.comparison(METRIC_COST_PER_STORY).improved is True
        assert report.comparison(METRIC_STORIES_PER_DOLLAR).with_prior.value == 0.5
        assert report.comparison(METRIC_STORIES_PER_DOLLAR).improved is True

    def test_failed_runs_are_excluded_from_cost_denominators(self) -> None:
        records = _cohorts({"cost": 2.0}, {"cost": 8.0})
        records.append(_record("with-failed", cohort="with", success=False, cost=50.0))
        report = build_report(records)

        assert report.cohort(COHORT_WITH).run_count == 4
        assert report.comparison(METRIC_COST_PER_STORY).with_prior.sample_size == 3
        assert report.comparison(METRIC_COST_PER_STORY).with_prior.value == 2.0

    def test_unmeasured_cost_is_not_coerced_to_zero(self) -> None:
        """A successful run with null cost is a delivery of unknown spend.

        It stays in the cohort head-count and out of both cost denominators —
        counting it as $0.00 would understate cost per completed story.
        """
        records = _cohorts({"cost": 2.0}, {"cost": 8.0})
        records.append(_record("with-unmeasured", cohort="with", cost=None))
        report = build_report(records)

        with_cohort = report.cohort(COHORT_WITH)
        assert with_cohort.run_count == 4
        assert with_cohort.metric(METRIC_COST_PER_STORY).sample_size == 3
        assert with_cohort.metric(METRIC_COST_PER_STORY).value == 2.0
        assert with_cohort.metric(METRIC_STORIES_PER_DOLLAR).value == 0.5

    def test_zero_measured_spend_yields_no_stories_per_dollar(self) -> None:
        records = _cohorts({"cost": 0.0}, {"cost": 8.0})
        report = build_report(records)

        assert report.cohort(COHORT_WITH).metric(METRIC_STORIES_PER_DOLLAR).value is None
        assert report.comparison(METRIC_STORIES_PER_DOLLAR).comparable is False


# ── Missing telemetry ─────────────────────────────────────────────────


class TestMissingTelemetry:
    def test_missing_plan_review_block_makes_the_denominator_unavailable(self) -> None:
        """No plan_review block means "not recorded", never "zero regenerations"."""
        record = _record("r")
        del record["plan_review"]
        signals = extract_signals(record)
        assert signals.plan_regenerated is None

        records = [_record(f"with-{i}", cohort="with") for i in range(3)]
        for entry in records:
            del entry["plan_review"]
        records.extend(_record(f"without-{i}", cohort="without") for i in range(3))
        report = build_report(records)
        comparison = report.comparison(METRIC_PLAN_REGENERATION)

        assert comparison.with_prior.sample_size == 0
        assert comparison.with_prior.value is None
        assert comparison.with_prior.available is False
        assert comparison.comparable is False

    def test_record_without_manifests_or_plan_review_is_unclassified(self) -> None:
        record = _record("legacy")
        del record["plan_review"]
        del record["context_manifests"]
        signals = extract_signals(record)

        assert signals.cohort == COHORT_UNCLASSIFIED
        assert signals.plan_regenerated is None

    def test_missing_review_loop_leaves_recurrence_unavailable(self) -> None:
        record = _record("r")
        del record["iterations"]["review_loop"]
        signals = extract_signals(record)

        assert (signals.restated_findings, signals.total_findings) == (None, None)

    def test_empty_record_set_is_insufficient_data(self) -> None:
        report = build_report([])

        assert report.status == STATUS_INSUFFICIENT_DATA
        assert report.records_considered == 0
        assert report.stories_per_dollar_trend == ()


# ── Status semantics ──────────────────────────────────────────────────


class TestStatusSemantics:
    def test_thin_cohorts_are_insufficient_data_not_no_improvement(self) -> None:
        report = build_report(_cohorts({"cost": 2.0}, {"cost": 8.0}, count=2))

        assert report.status == STATUS_INSUFFICIENT_DATA
        assert "required before comparing" in report.status_reason

    def test_identical_cohorts_are_no_observed_improvement(self) -> None:
        report = build_report(_cohorts({}, {}))

        assert report.status == STATUS_NO_OBSERVED_IMPROVEMENT
        assert "none improved" in report.status_reason

    def test_worse_with_prior_is_no_observed_improvement(self) -> None:
        report = build_report(
            _cohorts({"dev_iterations": 4, "cost": 9.0}, {"dev_iterations": 1, "cost": 2.0})
        )

        assert report.status == STATUS_NO_OBSERVED_IMPROVEMENT

    def test_one_improved_metric_is_observed_improvement(self) -> None:
        report = build_report(_cohorts({"dev_iterations": 1}, {"dev_iterations": 3}))

        assert report.status == STATUS_OBSERVED_IMPROVEMENT
        assert METRIC_DEV_ITERATIONS in report.status_reason

    def test_matched_runs_present_but_telemetry_absent_is_insufficient_data(self) -> None:
        """Cohorts are big enough, but no metric has observations on both sides."""
        records = _cohorts({}, {})
        for record in records:
            del record["plan_review"]
            del record["iterations"]["review_loop"]
            record["iterations"]["dev_iterations_productive"] = None
            record["iterations"]["review_cycles_total"] = None
            record["cost"]["total_usd"] = None
        report = build_report(records)

        assert report.status == STATUS_INSUFFICIENT_DATA
        assert "telemetry is not recorded on both sides" in report.status_reason


# ── Window metadata and trend ─────────────────────────────────────────


class TestWindowAndTrend:
    def test_window_bounds_are_carried_into_the_report(self) -> None:
        report = build_report(
            _cohorts({}, {}), since="2026-08-01", until="2026-08-31", recent_run_count=25
        )

        assert (report.since, report.until, report.recent_run_count) == (
            "2026-08-01",
            "2026-08-31",
            25,
        )
        assert report.records_considered == 6

    def test_stories_per_dollar_trend_splits_the_window_chronologically(self) -> None:
        records = [
            _record(
                "early-0", cohort="without", cost=10.0, started_at="2026-08-01T00:00:00+00:00"
            ),
            _record(
                "early-1", cohort="without", cost=10.0, started_at="2026-08-02T00:00:00+00:00"
            ),
            _record("late-0", cohort="with", cost=2.0, started_at="2026-08-03T00:00:00+00:00"),
            _record("late-1", cohort="with", cost=2.0, started_at="2026-08-04T00:00:00+00:00"),
        ]
        earlier, later = build_report(records).stories_per_dollar_trend

        assert (earlier.label, earlier.stories_per_dollar) == ("earlier", 0.1)
        assert (later.label, later.stories_per_dollar) == ("later", 0.5)
        assert later.measured_cost_usd == 4.0
