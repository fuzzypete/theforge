"""Tests for reading an audit record into effectiveness signals (#1867).

This mirror covers the schema-reading half: which block a fact comes from, how a
cohort is decided from the context manifests, how a comparability bucket is
derived, and — the rule the whole report rests on — that absent telemetry
becomes ``None`` rather than a zero.
"""

from __future__ import annotations

from tests.knowledge_effectiveness_test_helpers import manifests, record
from theforge.knowledge_effectiveness_signals import (
    COHORT_UNCLASSIFIED,
    COHORT_WITH,
    COHORT_WITHOUT,
    classify_cohort,
    extract_signals,
)

# ── Cohort classification ─────────────────────────────────────────────


class TestCohortClassification:
    def test_included_prior_summary_is_with_cohort(self) -> None:
        assert classify_cohort(record("r", cohort="with")) == COHORT_WITH

    def test_enabled_but_nothing_included_is_control_cohort(self) -> None:
        """Enabled with an empty manifest is a genuine control, not unclassified."""
        assert classify_cohort(record("r", cohort="without")) == COHORT_WITHOUT

    def test_disabled_manifest_is_unclassified(self) -> None:
        assert classify_cohort(record("r", cohort="unclassified")) == COHORT_UNCLASSIFIED

    def test_missing_context_manifests_is_unclassified(self) -> None:
        entry = record("r")
        del entry["context_manifests"]
        assert classify_cohort(entry) == COHORT_UNCLASSIFIED

    def test_malformed_context_manifests_is_unclassified(self) -> None:
        entry = record("r")
        entry["context_manifests"] = "not-a-list"
        assert classify_cohort(entry) == COHORT_UNCLASSIFIED

    def test_ineligible_phase_manifest_does_not_create_a_control(self) -> None:
        """Preflight is signal-only, so its manifest still proves nothing for cohorts."""
        entry = record("r")
        entry["context_manifests"] = manifests("with", phase="preflight")
        assert classify_cohort(entry) == COHORT_UNCLASSIFIED

    def test_one_eligible_phase_with_an_inclusion_settles_the_cohort(self) -> None:
        """Plan saw a summary, review saw none — the run was still advised."""
        entry = record("r")
        entry["context_manifests"] = [
            *manifests("with", phase="plan"),
            *manifests("without", phase="review"),
        ]
        assert classify_cohort(entry) == COHORT_WITH


# ── Comparability bucket ──────────────────────────────────────────────


class TestBucketDerivation:
    def test_bucket_is_work_type_complexity_band_and_sorted_domains(self) -> None:
        signals = extract_signals(
            record("r", work_type="feature", complexity="large", domains=("cli", "backend"))
        )

        assert signals.bucket == ("feature", "HIGH", ("backend", "cli"))

    def test_complexity_score_is_the_fallback_band(self) -> None:
        entry = record("r")
        del entry["preflight"]["complexity"]
        entry["preflight"]["complexity_score"] = 9

        assert extract_signals(entry).bucket == ("feature", "HIGH", ("backend",))

    def test_missing_preflight_leaves_the_run_unbucketed(self) -> None:
        entry = record("r")
        del entry["preflight"]

        assert extract_signals(entry).bucket is None

    def test_preflight_without_work_type_or_complexity_is_unbucketed(self) -> None:
        entry = record("r")
        entry["preflight"] = {"domains": ["backend"]}

        assert extract_signals(entry).bucket is None

    def test_non_list_domains_degrade_to_empty(self) -> None:
        entry = record("r")
        entry["preflight"]["domains"] = "backend"

        assert extract_signals(entry).bucket == ("feature", "MEDIUM", ())


# ── Recurrence extraction ─────────────────────────────────────────────


class TestRecurrenceExtraction:
    def test_restated_and_novel_findings_are_the_primary_signal(self) -> None:
        signals = extract_signals(record("r", restated=2, novel=6))

        assert (signals.restated_findings, signals.total_findings) == (2, 8)

    def test_repeated_findings_by_severity_is_the_fallback_signal(self) -> None:
        entry = record("r")
        entry["iterations"]["review_loop"] = [
            {
                "iteration": 1,
                "finding_counts": {"P1": 2, "P2": 2},
                "repeated_findings_by_severity": {"P1": 1},
            }
        ]
        signals = extract_signals(entry)

        assert (signals.restated_findings, signals.total_findings) == (1, 4)

    def test_iterations_accumulate_across_the_review_loop(self) -> None:
        entry = record("r")
        entry["iterations"]["review_loop"] = [
            {"iteration": 1, "novel_findings": 3, "restated_findings": 0},
            {"iteration": 2, "novel_findings": 1, "restated_findings": 2},
        ]
        signals = extract_signals(entry)

        assert (signals.restated_findings, signals.total_findings) == (2, 6)

    def test_missing_review_loop_leaves_recurrence_unavailable(self) -> None:
        entry = record("r")
        del entry["iterations"]["review_loop"]
        signals = extract_signals(entry)

        assert (signals.restated_findings, signals.total_findings) == (None, None)

    def test_review_loop_without_any_recurrence_fields_is_unavailable(self) -> None:
        """An iteration that recorded neither signal is not a zero-recurrence run."""
        entry = record("r")
        entry["iterations"]["review_loop"] = [{"iteration": 1, "verdict": "APPROVE"}]
        signals = extract_signals(entry)

        assert (signals.restated_findings, signals.total_findings) == (None, None)


# ── Absent telemetry stays absent ─────────────────────────────────────


class TestAbsentTelemetry:
    def test_missing_plan_review_block_is_not_zero_regenerations(self) -> None:
        entry = record("r")
        del entry["plan_review"]

        assert extract_signals(entry).plan_regenerated is None

    def test_recorded_plan_review_flag_is_read_verbatim(self) -> None:
        assert extract_signals(record("r", plan_regenerated=True)).plan_regenerated is True
        assert extract_signals(record("r", plan_regenerated=False)).plan_regenerated is False

    def test_record_without_manifests_or_plan_review_is_unclassified(self) -> None:
        entry = record("legacy")
        del entry["plan_review"]
        del entry["context_manifests"]
        signals = extract_signals(entry)

        assert signals.cohort == COHORT_UNCLASSIFIED
        assert signals.plan_regenerated is None
        assert signals.classified is False

    def test_null_cost_is_not_coerced_to_zero(self) -> None:
        assert extract_signals(record("r", cost=None)).cost_usd is None
        assert extract_signals(record("r", cost=0.0)).cost_usd == 0.0

    def test_missing_iteration_counts_stay_none(self) -> None:
        entry = record("r")
        entry["iterations"]["dev_iterations_productive"] = None
        del entry["iterations"]["review_cycles_total"]
        signals = extract_signals(entry)

        assert signals.dev_iterations is None
        assert signals.review_cycles is None

    def test_booleans_are_never_read_as_counts(self) -> None:
        """``True`` is an ``int`` in Python; it is not an iteration count."""
        entry = record("r")
        entry["iterations"]["dev_iterations_productive"] = True

        assert extract_signals(entry).dev_iterations is None

    def test_identity_and_timing_survive_extraction(self) -> None:
        signals = extract_signals(record("run-7", started_at="2026-08-09T12:00:00+00:00"))

        assert (signals.run_id, signals.slug) == ("run-7", "run-7")
        assert signals.started_at == "2026-08-09T12:00:00+00:00"
        assert signals.success is True

    def test_an_empty_record_yields_all_unavailable_signals(self) -> None:
        signals = extract_signals({})

        assert signals.cohort == COHORT_UNCLASSIFIED
        assert signals.bucket is None
        assert signals.success is False
        assert signals.plan_regenerated is None
        assert signals.cost_usd is None
        assert (signals.dev_iterations, signals.review_cycles) == (None, None)
