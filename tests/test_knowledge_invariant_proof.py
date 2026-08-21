"""Invariant-context churn proof over audit records (#1875)."""

from __future__ import annotations

from knowledge_effectiveness_test_helpers import record

from theforge.knowledge_effectiveness import (
    METRIC_COST_PER_STORY,
    METRIC_DEV_ITERATIONS,
    METRIC_PLAN_REGENERATION,
    METRIC_REVIEW_CYCLES,
    METRIC_REVIEW_RECURRENCE,
    METRIC_STORIES_PER_DOLLAR,
    STATUS_INSUFFICIENT_DATA,
    STATUS_NO_OBSERVED_IMPROVEMENT,
    STATUS_OBSERVED_IMPROVEMENT,
)
from theforge.knowledge_effectiveness_render import invariant_proof_payload, render_terminal
from theforge.knowledge_effectiveness_signals import (
    COHORT_UNCLASSIFIED,
    INVARIANT_COHORT_WITH,
    INVARIANT_COHORT_WITHOUT,
    classify_invariant_cohort,
    extract_signals,
)
from theforge.knowledge_invariant_proof import (
    CHURN_METRICS,
    build_invariant_proof_from_records,
)


def _manifest(
    *,
    enabled: bool,
    included: int = 0,
    uncertain: int = 0,
    dropped: int = 0,
    phase: str = "dev",
) -> dict:
    return {
        "phase": phase,
        "prior_run_context": {
            "enabled": False,
            "included": [],
            "dropped": [],
            "index_state": None,
        },
        "invariant_context": {
            "enabled": enabled,
            "phase": phase,
            "selection_mode": "selective",
            "included": [{"id": f"inv-{i}"} for i in range(included)],
            "uncertain": [{"id": f"inv-{i}"} for i in range(uncertain)],
            "dropped": [{"id": f"drop-{i}"} for i in range(dropped)],
            "note": "note",
        },
    }


def _record(run_id: str, *, manifests: list[dict] | None, **kwargs) -> dict:
    rec = record(run_id, **kwargs)
    if manifests is None:
        rec.pop("context_manifests")
    else:
        rec["context_manifests"] = manifests
    return rec


# ── Cohort classification ────────────────────────────────────────────────────


def test_included_invariants_make_a_run_treatment():
    rec = _record("a", manifests=[_manifest(enabled=True, included=2)])

    assert classify_invariant_cohort(rec) == INVARIANT_COHORT_WITH


def test_enabled_with_nothing_included_is_a_genuine_control():
    rec = _record("a", manifests=[_manifest(enabled=True, included=0)])

    assert classify_invariant_cohort(rec) == INVARIANT_COHORT_WITHOUT


def test_disabled_or_absent_is_unclassified():
    assert (
        classify_invariant_cohort(_record("a", manifests=[_manifest(enabled=False)]))
        == COHORT_UNCLASSIFIED
    )
    assert classify_invariant_cohort(_record("a", manifests=None)) == COHORT_UNCLASSIFIED


def test_preflight_manifests_never_classify():
    rec = _record("a", manifests=[_manifest(enabled=True, included=2, phase="preflight")])

    assert classify_invariant_cohort(rec) == COHORT_UNCLASSIFIED


def test_absent_invariant_telemetry_is_unavailable_not_zero():
    signals = extract_signals(_record("a", manifests=[_manifest(enabled=False)]))

    assert signals.invariant_included is None
    assert signals.invariant_uncertain is None
    assert signals.invariant_dropped is None


# ── Churn proof ──────────────────────────────────────────────────────────────


def _cohort_records(*, count: int = 3, with_kwargs: dict, without_kwargs: dict) -> list[dict]:
    records: list[dict] = []
    for i in range(count):
        records.append(
            _record(
                f"with-{i}",
                manifests=[_manifest(enabled=True, included=2, uncertain=1, dropped=1)],
                **with_kwargs,
            )
        )
        records.append(
            _record(
                f"without-{i}",
                manifests=[_manifest(enabled=True, included=0)],
                **without_kwargs,
            )
        )
    return records


def test_proof_reports_insufficient_data_below_the_cohort_floor():
    proof = build_invariant_proof_from_records(
        [_record("a", manifests=[_manifest(enabled=True, included=1)])]
    )

    assert proof.status == STATUS_INSUFFICIENT_DATA
    assert "1 with-invariant" in proof.status_reason


def test_proof_reports_churn_improvement_when_both_cohorts_are_populated():
    records = _cohort_records(
        with_kwargs={"plan_regenerated": False, "review_cycles": 1, "dev_iterations": 1},
        without_kwargs={"plan_regenerated": True, "review_cycles": 3, "dev_iterations": 2},
    )

    proof = build_invariant_proof_from_records(records)

    assert proof.status == STATUS_OBSERVED_IMPROVEMENT
    assert proof.cohort_counts[INVARIANT_COHORT_WITH] == 3
    assert proof.cohort_counts[INVARIANT_COHORT_WITHOUT] == 3
    regen = proof.comparison(METRIC_PLAN_REGENERATION)
    assert regen.improved is True
    assert regen.delta == -1.0


def test_proof_reports_no_observed_improvement_honestly():
    records = _cohort_records(
        with_kwargs={"plan_regenerated": True, "review_cycles": 3, "dev_iterations": 2},
        without_kwargs={"plan_regenerated": True, "review_cycles": 3, "dev_iterations": 2},
    )

    proof = build_invariant_proof_from_records(records)

    assert proof.status == STATUS_NO_OBSERVED_IMPROVEMENT


def test_restated_finding_rate_is_a_reported_churn_signal():
    records = _cohort_records(
        with_kwargs={"restated": 0, "novel": 4},
        without_kwargs={"restated": 3, "novel": 1},
    )

    proof = build_invariant_proof_from_records(records)

    recurrence = proof.comparison(METRIC_REVIEW_RECURRENCE)
    assert recurrence.comparable is True
    assert recurrence.improved is True


def test_cost_metrics_are_not_part_of_the_churn_claim():
    assert set(CHURN_METRICS) == {
        METRIC_PLAN_REGENERATION,
        METRIC_REVIEW_RECURRENCE,
        METRIC_DEV_ITERATIONS,
        METRIC_REVIEW_CYCLES,
    }
    proof = build_invariant_proof_from_records(_cohort_records(with_kwargs={}, without_kwargs={}))
    assert proof.comparison(METRIC_COST_PER_STORY) is None
    assert proof.comparison(METRIC_STORIES_PER_DOLLAR) is None


def test_selection_counts_surface_how_often_the_fallback_carried_the_proof():
    proof = build_invariant_proof_from_records(_cohort_records(with_kwargs={}, without_kwargs={}))

    counts = proof.selection_counts
    assert counts.runs_with_telemetry == 6
    assert counts.included == 6
    assert counts.uncertain == 3
    assert counts.dropped == 3
    assert counts.uncertain_share == 0.5


def test_unavailable_telemetry_never_counts_as_zero_selection():
    proof = build_invariant_proof_from_records([_record("a", manifests=None)])

    assert proof.selection_counts.runs_with_telemetry == 0
    assert proof.selection_counts.uncertain_share is None


# ── Rendering ────────────────────────────────────────────────────────────────


def test_proof_payload_is_serializable_and_names_its_cohorts():
    proof = build_invariant_proof_from_records(_cohort_records(with_kwargs={}, without_kwargs={}))

    payload = invariant_proof_payload(proof)

    assert payload["cohorts"][INVARIANT_COHORT_WITH] == 3
    assert payload["selection"]["uncertain"] == 3
    assert {item["metric"] for item in payload["churn_comparison"]} == set(CHURN_METRICS)
    assert INVARIANT_COHORT_WITH in payload["churn_comparison"][0]


def test_terminal_render_appends_the_proof_section_only_when_asked():
    from theforge.knowledge_effectiveness import build_report

    records = _cohort_records(with_kwargs={}, without_kwargs={})
    report = build_report(records)
    proof = build_invariant_proof_from_records(records)

    assert "Invariant-context proof" not in render_terminal(report)
    rendered = render_terminal(report, proof)
    assert "Invariant-context proof" in rendered
    assert "uncertain → broad source" in rendered
