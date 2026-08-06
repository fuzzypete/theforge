"""Per-story budget allocation derived from the complexity band (#2169).

The allocation is pure arithmetic over recorded costs: same substrate, same
score, same number, every time. These tests pin the derivation itself, the
substrate reader that feeds it, and the fallback when a band has no history.
"""

from __future__ import annotations

import json
from pathlib import Path

from theforge.coordinator import audit_substrate as sub
from theforge.coordinator import story_budget as sb

# Observed distribution for score 2 in the issue's table (n=24, median $0.46,
# p90 $1.08, max $1.35), compressed to the sample floor.
_SCORE_2_COSTS = [0.21, 0.30, 0.38, 0.46, 0.52, 0.61, 1.08, 1.35]
# Score 9: median $7.53, max $40.64.
_SCORE_9_COSTS = [1.9, 3.4, 6.1, 7.53, 9.2, 14.0, 19.74, 40.64]


def _record(
    *,
    run_id: str,
    score: int | None,
    cost: float | None,
    trust_status: str | None = None,
    review_cycle_costs: list[float | None] | None = None,
) -> dict:
    rec: dict = {
        "run_id": run_id,
        "task": {"slug": f"story-{run_id}", "name": run_id},
        "outcome": {"success": True, "final_phase": "DONE"},
        "timing": {"started_at": "2026-03-01T10:00:00+00:00", "duration_seconds": 60.0},
        "cost": {"total_usd": cost},
        "totals": {"cost_usd": cost, "duration_s": 60.0},
        "preflight": {"complexity": "medium", "complexity_score": score},
        "reviews": [],
    }
    if trust_status is not None:
        rec["trust_status"] = trust_status
    if review_cycle_costs is not None:
        rec["iterations"] = {
            "review_loop": [
                {"iteration": index + 1, "cost_usd": cycle_cost}
                for index, cycle_cost in enumerate(review_cycle_costs)
            ],
            "review_cycles_total": len(review_cycle_costs),
        }
    return rec


def _seed_substrate(project_root: Path, records: list[dict]) -> None:
    runs = sub.runs_dir(project_root)
    runs.mkdir(parents=True, exist_ok=True)
    for rec in records:
        (runs / f"{rec['run_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    conn = sub.create_or_open(project_root)
    try:
        for rec in records:
            sub.upsert_run_record(conn, rec, provenance="native")
        conn.commit()
    finally:
        conn.close()


class TestPercentile:
    def test_nearest_rank_median_and_p90(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        assert sb.percentile(values, 0.5) == 5.0
        assert sb.percentile(values, 0.9) == 9.0

    def test_unsorted_input_and_single_value(self) -> None:
        assert sb.percentile([9.0, 1.0, 5.0], 0.5) == 5.0
        assert sb.percentile([2.5], 0.9) == 2.5


class TestAllocationFromSamples:
    def test_story_within_its_band_is_governed_by_the_band(self) -> None:
        """A score-2 story is allocated from score-2 costs, not the $50 constant."""
        allocation = sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0)

        assert allocation.basis == sb.BASIS_SUBSTRATE_BAND
        assert allocation.sample_count == 8
        assert allocation.median_usd == 0.46
        assert allocation.p90_usd == 1.35  # nearest-rank over 8 samples
        assert allocation.max_usd == 1.35
        # 1.35 x 1.25 headroom — two orders of magnitude below the flat ceiling.
        assert allocation.allocation_usd == 1.69
        assert allocation.fallback_configured_usd == 50.0

    def test_ordinary_story_in_band_is_not_flagged(self) -> None:
        """$0.60 on a score-2 story is unremarkable and reads as such."""
        allocation = sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0)
        block = sb.evaluate_allocation(allocation, 0.60)

        assert block["status"] == sb.STATUS_WITHIN
        assert block["exceeded"] is False

    def test_story_exceeding_its_band_is_reported_with_its_expected_range(self) -> None:
        """The issue's example: a score-2 story passing $4 is anomalous.

        The flat $50 ceiling calls this unremarkable; the band says it is 3x the
        most expensive score-2 story ever recorded.
        """
        allocation = sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0)
        block = sb.evaluate_allocation(allocation, 4.00)

        assert block["status"] == sb.STATUS_EXCEEDED
        assert block["exceeded"] is True
        assert block["observed_usd"] == 4.0
        assert block["overrun_usd"] == round(4.0 - 1.69, 4)
        # The report names the band it is being judged against.
        assert block["complexity_score"] == 2
        assert block["median_usd"] == 0.46
        assert block["max_usd"] == 1.35
        assert block["basis"] == sb.BASIS_SUBSTRATE_BAND

    def test_expensive_story_in_a_high_band_is_not_flagged(self) -> None:
        """The mirror case: a score-9 story at $40 is ordinary for its band."""
        allocation = sb.allocation_from_samples(9, _SCORE_9_COSTS, configured_usd=50.0)
        block = sb.evaluate_allocation(allocation, 40.00)

        assert allocation.allocation_usd == round(40.64 * 1.25, 2)
        assert block["status"] == sb.STATUS_WITHIN

    def test_band_with_no_history_falls_back_and_states_the_basis(self) -> None:
        allocation = sb.allocation_from_samples(4, [], configured_usd=50.0)

        assert allocation.basis == sb.BASIS_CONFIGURED_FALLBACK
        assert allocation.allocation_usd == 50.0
        assert allocation.sample_count == 0
        assert "below the 8-run floor" in allocation.reason
        assert allocation.derived is False

    def test_band_below_the_sample_floor_falls_back(self) -> None:
        allocation = sb.allocation_from_samples(4, [1.0, 2.0, 3.0], configured_usd=12.5)

        assert allocation.basis == sb.BASIS_CONFIGURED_FALLBACK
        assert allocation.allocation_usd == 12.5
        assert allocation.sample_count == 3

    def test_missing_score_falls_back_and_says_so(self) -> None:
        allocation = sb.allocation_from_samples(None, _SCORE_2_COSTS, configured_usd=50.0)

        assert allocation.basis == sb.BASIS_CONFIGURED_FALLBACK
        assert allocation.complexity_score is None
        assert "no preflight complexity score" in allocation.reason

    def test_zero_and_negative_costs_are_not_spend_observations(self) -> None:
        allocation = sb.allocation_from_samples(
            2, [*_SCORE_2_COSTS, 0.0, 0.0, -1.0], configured_usd=50.0
        )
        assert allocation.sample_count == 8

    def test_cheap_band_never_allocates_below_the_floor(self) -> None:
        allocation = sb.allocation_from_samples(1, [0.01] * 8, configured_usd=50.0)
        assert allocation.allocation_usd == sb.MIN_ALLOCATION_USD

    def test_unmeasured_cost_is_neither_within_nor_exceeded(self) -> None:
        allocation = sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0)
        block = sb.evaluate_allocation(allocation, None)

        assert block["status"] == sb.STATUS_UNKNOWN
        assert block["exceeded"] is None


class TestSubstrateCostSamples:
    def test_samples_group_by_score_and_skip_unusable_rows(self, tmp_path: Path) -> None:
        _seed_substrate(
            tmp_path,
            [
                _record(run_id="r1", score=2, cost=0.5),
                _record(run_id="r2", score=2, cost=1.2),
                _record(run_id="r3", score=8, cost=9.0),
                _record(run_id="r4", score=None, cost=3.0),
                _record(run_id="r5", score=8, cost=None),
                _record(run_id="r6", score=8, cost=0.0),
            ],
        )
        conn = sub.require_substrate(tmp_path)
        try:
            samples = sub.derive_cost_samples_by_score(conn)
        finally:
            conn.close()

        assert sorted(samples[2]) == [0.5, 1.2]
        assert samples[8] == [9.0]

    def test_tainted_runs_do_not_teach_what_a_story_costs(self, tmp_path: Path) -> None:
        _seed_substrate(
            tmp_path,
            [
                _record(run_id="r1", score=5, cost=2.0),
                _record(run_id="r2", score=5, cost=99.0, trust_status="tainted"),
            ],
        )
        conn = sub.require_substrate(tmp_path)
        try:
            stats: dict = {}
            samples = sub.derive_cost_samples_by_score(conn, stats=stats)
        finally:
            conn.close()

        assert samples[5] == [2.0]
        assert stats["excluded_for_taint"] == 1


class TestDeriveStoryAllocation:
    def test_derives_from_the_substrate_for_the_story_score(self, tmp_path: Path) -> None:
        _seed_substrate(
            tmp_path,
            [_record(run_id=f"r{i}", score=2, cost=cost) for i, cost in enumerate(_SCORE_2_COSTS)],
        )

        allocation = sb.derive_story_allocation(tmp_path, complexity_score=2, configured_usd=50.0)

        assert allocation.basis == sb.BASIS_SUBSTRATE_BAND
        assert allocation.allocation_usd == 1.69
        assert allocation.sample_count == 8

    def test_other_bands_do_not_contaminate_the_allocation(self, tmp_path: Path) -> None:
        _seed_substrate(
            tmp_path,
            [_record(run_id=f"a{i}", score=2, cost=c) for i, c in enumerate(_SCORE_2_COSTS)]
            + [_record(run_id=f"b{i}", score=9, cost=c) for i, c in enumerate(_SCORE_9_COSTS)],
        )

        low = sb.derive_story_allocation(tmp_path, complexity_score=2, configured_usd=50.0)
        high = sb.derive_story_allocation(tmp_path, complexity_score=9, configured_usd=50.0)

        assert low.allocation_usd == 1.69
        assert high.allocation_usd == round(40.64 * 1.25, 2)

    def test_fresh_repo_with_no_substrate_falls_back(self, tmp_path: Path) -> None:
        allocation = sb.derive_story_allocation(tmp_path, complexity_score=6, configured_usd=50.0)

        assert allocation.basis == sb.BASIS_CONFIGURED_FALLBACK
        assert allocation.allocation_usd == 50.0
        assert allocation.reason.startswith("no audit substrate; ")


class TestReviewCyclePlanningPrice:
    def test_observed_review_cycle_price_uses_median_plus_explicit_headroom(self) -> None:
        planning = sb.review_cycle_planning_from_samples([3.10, 3.64, 4.20], 17.55)

        assert planning.basis == sb.BASIS_OBSERVED_REVIEW_CYCLE
        assert planning.sample_count == 3
        assert planning.median_usd == 3.64
        assert planning.p90_usd == 4.2
        assert planning.max_usd == 4.2
        assert planning.planned_cost_usd == round(3.64 * 1.25, 4)
        assert "median observed review-cycle spend $3.64 x 1.25" in planning.reason

    def test_insufficient_history_falls_back_to_the_reviewer_ceiling_sum(self) -> None:
        planning = sb.review_cycle_planning_from_samples([3.10, 3.64], 17.55)

        assert planning.basis == sb.BASIS_REVIEW_CEILING_FALLBACK
        assert planning.sample_count == 2
        assert planning.planned_cost_usd == 17.55
        assert "below the 3-cycle floor" in planning.reason

    def test_substrate_reader_collects_per_cycle_review_costs(self, tmp_path: Path) -> None:
        _seed_substrate(
            tmp_path,
            [
                _record(run_id="r1", score=5, cost=4.0, review_cycle_costs=[3.10, 3.64]),
                _record(run_id="r2", score=5, cost=5.0, review_cycle_costs=[4.20]),
                _record(
                    run_id="r3",
                    score=5,
                    cost=99.0,
                    trust_status="tainted",
                    review_cycle_costs=[9.99],
                ),
            ],
        )

        planning = sb.derive_review_cycle_planning_price(tmp_path, configured_ceiling_usd=17.55)

        assert planning.basis == sb.BASIS_OBSERVED_REVIEW_CYCLE
        assert planning.sample_count == 3
        assert planning.excluded_for_taint == 1
        assert planning.planned_cost_usd == round(3.64 * 1.25, 4)


class TestScaleRoleBudgets:
    def test_shares_scale_proportionally_to_the_allocation(self) -> None:
        current = {"dev": 6.0, "preflight": 1.0, "review_pool[0]": 3.0}
        scaled = sb.scale_role_budgets(current, 5.0)

        assert round(sum(scaled.values()), 4) == 5.0
        assert scaled["dev"] == 3.0
        assert scaled["review_pool[0]"] == 1.5

    def test_explicit_overrides_keep_their_configured_budget(self) -> None:
        current = {"dev": 6.0, "preflight": 1.0, "review_pool[0]": 3.0}
        scaled = sb.scale_role_budgets(current, 10.0, locked={"dev"})

        assert scaled["dev"] == 6.0
        assert round(sum(scaled.values()), 4) == 10.0

    def test_locked_roles_consuming_the_allocation_leave_a_floor_not_zero(self) -> None:
        """A zero budget is an instruction to run nothing — never the answer."""
        current = {"dev": 20.0, "review_pool[0]": 3.0}
        scaled = sb.scale_role_budgets(current, 10.0, locked={"dev"})

        assert scaled["dev"] == 20.0
        assert scaled["review_pool[0]"] > 0


class TestPhaseFundingShortfall:
    def _allocation(self) -> dict:
        return sb.allocation_from_samples(2, _SCORE_2_COSTS, configured_usd=50.0).as_dict()

    def test_funded_phase_returns_no_shortfall(self) -> None:
        assert (
            sb.phase_funding_shortfall(
                self._allocation(),
                0.20,
                phase="review",
                participants=["a", "b"],
                planned_usd=0.60,
            )
            is None
        )

    def test_unfundable_phase_names_the_participants_it_planned(self) -> None:
        shortfall = sb.phase_funding_shortfall(
            self._allocation(),
            1.50,
            phase="review",
            participants=["a", "b", "c"],
            planned_usd=0.60,
        )

        assert shortfall is not None
        assert shortfall["participants"] == ["a", "b", "c"]
        assert shortfall["planned_usd"] == 0.60
        assert shortfall["allocation_usd"] == 1.69
        assert shortfall["remaining_usd"] == round(1.69 - 1.50, 4)

        message = sb.format_shortfall(shortfall, story="issue-2169")
        assert "issue-2169" in message
        assert "a, b, c" in message
        assert "$1.69" in message
        assert "median $0.46" in message

    def test_unmeasured_spend_never_refuses_a_phase(self) -> None:
        assert (
            sb.phase_funding_shortfall(
                self._allocation(),
                None,
                phase="review",
                participants=["a"],
                planned_usd=99.0,
            )
            is None
        )


class TestReviewCycleReconciliation:
    """Permitted review cycles are reconciled against the allocation (#2238)."""

    def _allocation(self, usd: float = 48.02) -> dict:
        return {
            "allocation_usd": usd,
            "basis": sb.BASIS_SUBSTRATE_BAND,
            "complexity_score": 8,
            "median_usd": 7.53,
            "p90_usd": 19.74,
            "max_usd": 38.42,
            "sample_count": 12,
        }

    def test_issue_2204_arithmetic_reduces_five_permitted_cycles_to_one(self) -> None:
        """The seating numbers from run 88a7e2cc81eb admit one review cycle."""
        record = sb.reconcile_review_cycles(
            self._allocation(),
            dev_cost_estimate_usd=25.0428,
            review_cycle_cost_usd=17.55,
            review_cycle_planning=sb.review_cycle_planning_from_samples(
                [17.55], 17.55, min_samples=1
            ).as_dict(),
            requested_review_max=5,
            spent_so_far_usd=0.0,
        )

        assert record["action"] == sb.RECONCILE_REDUCED
        assert record["affordable_review_cycles"] == 1
        assert record["reconciled_review_max"] == 1
        assert record["allocation_usd"] == 48.02
        assert record["dev_cost_estimate_usd"] == 25.0428
        assert record["review_cycle_cost_usd"] == 17.55
        assert record["requested_review_max"] == 5
        assert record["remaining_after_dev_usd"] == round(48.02 - 25.0428, 4)
        assert record["shortfall_usd"] > 0

        message = sb.format_reconciliation(record)
        assert "5 → 1" in message
        assert "$48.02" in message
        assert "observed median $17.55 x 1.25 headroom" in message

    def test_spend_already_incurred_counts_against_the_remainder(self) -> None:
        """Preflight/plan spend is real money and is not affordable twice."""
        record = sb.reconcile_review_cycles(
            self._allocation(),
            dev_cost_estimate_usd=25.0428,
            review_cycle_cost_usd=17.55,
            requested_review_max=5,
            spent_so_far_usd=6.0,
        )

        assert record["action"] == sb.RECONCILE_UNFUNDABLE
        assert record["affordable_review_cycles"] == 0
        assert record["spent_so_far_usd"] == 6.0
        assert record["shortfall_usd"] == round(17.55 - (48.02 - 6.0 - 25.0428), 4)

    def test_an_allocation_that_funds_the_permission_is_left_alone(self) -> None:
        record = sb.reconcile_review_cycles(
            self._allocation(usd=120.0),
            dev_cost_estimate_usd=25.0,
            review_cycle_cost_usd=17.55,
            requested_review_max=5,
            spent_so_far_usd=0.0,
        )

        assert record["action"] == sb.RECONCILE_AFFORDABLE
        assert record["reconciled_review_max"] == 5
        assert record["affordable_review_cycles"] == 5

    def test_missing_inputs_are_explicit_no_ops_not_guesses(self) -> None:
        base = dict(
            dev_cost_estimate_usd=25.0,
            review_cycle_cost_usd=17.55,
            requested_review_max=5,
            spent_so_far_usd=0.0,
        )
        assert sb.reconcile_review_cycles(None, **base)["action"] == sb.RECONCILE_NO_ALLOCATION
        assert (
            sb.reconcile_review_cycles(
                self._allocation(), **{**base, "dev_cost_estimate_usd": 0.0}
            )["action"]
            == sb.RECONCILE_NO_DEV_ESTIMATE
        )
        assert (
            sb.reconcile_review_cycles(
                self._allocation(), **{**base, "review_cycle_cost_usd": 0.0}
            )["action"]
            == sb.RECONCILE_NO_REVIEW_COST
        )
        # The configured fallback is one pass through every role by
        # construction, so it funds exactly one review cycle for reasons that
        # say nothing about this story. Reconciling against it would clamp
        # verification to one cycle on every band without history.
        fallback = sb.reconcile_review_cycles(
            {**self._allocation(), "basis": sb.BASIS_CONFIGURED_FALLBACK}, **base
        )
        assert fallback["action"] == sb.RECONCILE_NO_BAND_HISTORY
        assert fallback["reconciled_review_max"] == 5
        # Cost-unknown spend is a lower bound; refusing on it would be a guess.
        unknown = sb.reconcile_review_cycles(
            self._allocation(usd=1.0), **{**base, "spent_so_far_usd": None}
        )
        assert unknown["action"] == sb.RECONCILE_COST_UNKNOWN
        assert unknown["reconciled_review_max"] == 5
        for record in (
            sb.reconcile_review_cycles(None, **base),
            fallback,
            unknown,
        ):
            assert record["reconciled_review_max"] == record["requested_review_max"]

    def test_unfundable_seating_reports_the_existing_shortfall_shape(self) -> None:
        allocation = self._allocation(usd=30.0)
        record = sb.reconcile_review_cycles(
            allocation,
            dev_cost_estimate_usd=25.0,
            review_cycle_cost_usd=17.55,
            requested_review_max=5,
            spent_so_far_usd=0.0,
        )
        shortfall = sb.seating_shortfall(allocation, record, participants=["a", "b", "c"])

        assert shortfall is not None
        # Same keys every existing consumer already reads.
        assert shortfall["phase"] == "review"
        assert shortfall["participants"] == ["a", "b", "c"]
        assert shortfall["planned_usd"] == 17.55
        assert shortfall["allocation_usd"] == 30.0
        assert shortfall["observed_usd"] == 25.0
        assert shortfall["projected"] is True
        assert shortfall["seating_reconciliation"]["action"] == sb.RECONCILE_UNFUNDABLE

        message = sb.format_shortfall(shortfall, story="issue-2204")
        assert "issue-2204" in message
        assert "projected" in message
        assert "Decided at seating" in message

    def test_no_shortfall_payload_when_the_seating_was_fundable(self) -> None:
        allocation = self._allocation(usd=120.0)
        record = sb.reconcile_review_cycles(
            allocation,
            dev_cost_estimate_usd=25.0,
            review_cycle_cost_usd=17.55,
            requested_review_max=5,
            spent_so_far_usd=0.0,
        )
        assert sb.seating_shortfall(allocation, record, participants=["a"]) is None
