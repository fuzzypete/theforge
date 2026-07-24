"""Unit tests for mechanical plan-reviewer value signals (#1443).

Covers the deterministic uniqueness computation over same-pool structured
findings, the fold, and the sample-floor / recency / taint-gated signal reader.
No LLM, no provider SDKs — pure aggregation.
"""

from __future__ import annotations

from theforge.model_profiles import RunOutcome, apply_run
from theforge.review import PlanReviewFinding
from theforge.reviewer_value import (
    PlanReviewerValueSample,
    compute_plan_reviewer_uniqueness,
    fold_plan_reviewer_value,
    get_reviewer_value_signal,
)


def _f(severity: str, description: str) -> PlanReviewFinding:
    return PlanReviewFinding(severity=severity, description=description, suggestion=None)


# ── Deterministic uniqueness ────────────────────────────────────────────────


def test_uniqueness_shared_anchor_is_not_unique_for_either_reviewer():
    # Both reviewers name the same snake_case symbol (retry_backoff) → same issue,
    # so neither reviewer's finding is unique to it.
    a = [_f("P1", "The retry_backoff logic in engine.py is wrong")]
    b = [_f("P1", "retry_backoff must grow exponentially")]
    res = compute_plan_reviewer_uniqueness([("a", a), ("b", b)])
    assert res == {"a": (0, 1), "b": (0, 1)}


def test_uniqueness_novel_finding_counts_as_unique():
    a = [
        _f("P1", "The retry_backoff logic is wrong"),
        _f("P1", "cache_layer is never invalidated"),
    ]
    b = [_f("P1", "retry_backoff must grow exponentially")]
    res = compute_plan_reviewer_uniqueness([("a", a), ("b", b)])
    # a: retry_backoff shared (not unique) + cache_layer novel (unique) → (1, 2)
    # b: retry_backoff shared → (0, 1)
    assert res == {"a": (1, 2), "b": (0, 1)}


def test_uniqueness_p2_findings_are_ignored():
    a = [_f("P2", "style nit in parse_config"), _f("P1", "missing_guard here")]
    res = compute_plan_reviewer_uniqueness([("a", a)])
    assert res == {"a": (1, 1)}  # only the P1 counts


def test_uniqueness_p0_is_treated_as_blocking():
    a = [_f("P0", "architecture_break in module_x")]
    res = compute_plan_reviewer_uniqueness([("a", a)])
    assert res == {"a": (1, 1)}


def test_uniqueness_no_anchor_finding_is_unique_by_default():
    # Prose with no extractable structural anchor cannot be corroborated → novel.
    a = [_f("P1", "This is wrong and bad")]
    b = [_f("P1", "Also broken somehow")]
    res = compute_plan_reviewer_uniqueness([("a", a), ("b", b)])
    assert res == {"a": (1, 1), "b": (1, 1)}


def test_uniqueness_reviewer_with_no_p1s_present_in_result():
    res = compute_plan_reviewer_uniqueness([("a", []), ("b", [_f("P1", "bad frobnicate_all")])])
    assert res == {"a": (0, 0), "b": (1, 1)}


def test_uniqueness_is_deterministic_across_input_order():
    a = [_f("P1", "retry_backoff broken"), _f("P1", "novel_thing_x wrong")]
    b = [_f("P1", "retry_backoff needs work")]
    r1 = compute_plan_reviewer_uniqueness([("a", a), ("b", b)])
    r2 = compute_plan_reviewer_uniqueness([("b", b), ("a", a)])
    assert r1 == r2


# ── Fold + signal reader ────────────────────────────────────────────────────


def _fold_n(
    entry: dict, n: int, *, unique: int, total: int, latency: float, tainted: bool = False
):
    for _ in range(n):
        fold_plan_reviewer_value(
            entry,
            PlanReviewerValueSample("rev", "medium", unique, total, latency, "m", "p"),
            tainted=tainted,
        )


def test_signal_above_floor_recency_weighted_rate():
    entry: dict = {}
    _fold_n(entry, 6, unique=1, total=2, latency=120.0)
    profiles = {"models": {"rev": entry}}
    sig = get_reviewer_value_signal(
        profiles, "rev", "medium", min_runs=5, actual_model="m", provider="p"
    )
    assert sig["runs"] == 6
    assert sig["uniqueness_rate"]["floor"] == "pass"
    assert sig["uniqueness_rate"]["rate"] == 0.5
    # latency-per-P1 = 120 / 2 = 60s
    assert sig["latency_per_p1"]["rate"] == 60.0


def test_signal_below_sample_floor_is_none():
    entry: dict = {}
    _fold_n(entry, 3, unique=1, total=2, latency=100.0)
    profiles = {"models": {"rev": entry}}
    sig = get_reviewer_value_signal(
        profiles, "rev", "medium", min_runs=5, actual_model="m", provider="p"
    )
    assert sig["runs"] == 3
    assert sig["uniqueness_rate"]["floor"] == "fail"
    assert sig["uniqueness_rate"]["rate"] is None
    assert sig["latency_per_p1"]["rate"] is None
    # raw is still exposed for audit drift even below the floor.
    assert sig["uniqueness_rate"]["raw"] == 0.5


def test_signal_excludes_tainted_records():
    entry: dict = {}
    _fold_n(entry, 5, unique=2, total=2, latency=60.0)  # admissible, uniqueness 1.0
    _fold_n(entry, 3, unique=0, total=2, latency=900.0, tainted=True)  # tainted, ignored
    profiles = {"models": {"rev": entry}}
    sig = get_reviewer_value_signal(
        profiles, "rev", "medium", min_runs=5, actual_model="m", provider="p"
    )
    assert sig["runs"] == 5
    assert sig["tainted_runs"] == 3
    # Tainted redundant/slow runs must not drag the rate down.
    assert sig["uniqueness_rate"]["rate"] == 1.0
    assert sig["latency_per_p1"]["rate"] == 30.0  # 60 / 2, tainted 900s excluded


def test_no_p1_sample_is_not_folded():
    entry: dict = {}
    fold_plan_reviewer_value(entry, PlanReviewerValueSample("rev", "medium", 0, 0, 50.0, "m", "p"))
    assert entry == {}


def test_fold_via_run_outcome_end_to_end():
    data = {"models": {}}
    for _ in range(5):
        data = apply_run(
            data,
            RunOutcome(
                complexity="large",
                dev_model="d",
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=0.0,
                plan_reviewer_values=[
                    PlanReviewerValueSample("revX", "large", 2, 3, 180.0, "mX", "pX")
                ],
            ),
        )
    sig = get_reviewer_value_signal(
        data, "revX", "large", min_runs=5, actual_model="mX", provider="pX"
    )
    assert sig["runs"] == 5
    assert sig["uniqueness_rate"]["rate"] == round(2 / 3, 4)
    assert sig["latency_per_p1"]["rate"] == 60.0  # 180 / 3


def test_signal_band_specific():
    data = {"models": {}}
    for _ in range(5):
        data = apply_run(
            data,
            RunOutcome(
                complexity="small",
                dev_model="d",
                dev_success=True,
                dev_iterations=1,
                dev_cost_usd=0.0,
                plan_reviewer_values=[
                    PlanReviewerValueSample("revB", "small", 0, 2, 200.0, "mB", "pB")
                ],
            ),
        )
    # The 'small' band has data; the 'large' band has none → cold start (floor fail).
    small = get_reviewer_value_signal(
        data, "revB", "small", min_runs=5, actual_model="mB", provider="pB"
    )
    large = get_reviewer_value_signal(
        data, "revB", "large", min_runs=5, actual_model="mB", provider="pB"
    )
    assert small["uniqueness_rate"]["floor"] == "pass"
    assert large["uniqueness_rate"]["floor"] == "fail"
    assert large["uniqueness_rate"]["rate"] is None
