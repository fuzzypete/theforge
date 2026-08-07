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


def _comparable_dev_estimate_basis(score: int = 8, *, sample_count: int = 12) -> dict:
    """A dev estimate on the allocation's own population: score-scoped, all models.

    ``score`` must match the allocation under test — a basis drawn at another
    score is exactly what seating refuses to subtract (#2284).
    """
    return {
        "source": sb.DEV_ESTIMATE_SOURCE_SCORE,
        "band": "large",
        "complexity_score": score,
        "statistic": "avg_cost_usd",
        "scope": sb.DEV_ESTIMATE_SCOPE_DEV_PHASE,
        "sample_count": sample_count,
        "headroom_basis": sb.DEV_ESTIMATE_HEADROOM_BASIS_ALLOCATION,
        "headroom_multiplier": sb.BAND_HEADROOM,
        "allocation_comparable": True,
    }

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
    review_pools: list[list[str] | None] | None = None,
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
    if review_pools is not None:
        rec["reviews"] = [
            {"cycle": index + 1, "pool_models": pool}
            for index, pool in enumerate(review_pools)
            if pool is not None
        ]
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

    def test_sparse_history_still_prices_from_observation_not_the_ceiling(self) -> None:
        """One or two observations are sparse, not absent — the ceiling is for absent."""
        planning = sb.review_cycle_planning_from_samples([3.10, 3.64], 17.55)

        assert planning.basis == sb.BASIS_OBSERVED_SPARSE
        assert planning.derived is True
        assert planning.sample_count == 2
        # Leans on the observed maximum, not the median: a two-sample median
        # understates the spread, and under-reserving refuses a granted cycle
        # at dispatch.
        assert planning.planned_cost_usd == round(3.64 * 1.25, 4)
        assert "below the 3-cycle floor" in planning.reason

    def test_a_single_observation_is_enough_to_price_from(self) -> None:
        planning = sb.review_cycle_planning_from_samples([4.00], 17.55)

        assert planning.basis == sb.BASIS_OBSERVED_SPARSE
        assert planning.sample_count == 1
        assert planning.planned_cost_usd == 5.0

    def test_sparse_price_never_exceeds_the_ceiling_sum(self) -> None:
        """The ceiling still bounds: reserving above what the panel may spend is waste."""
        planning = sb.review_cycle_planning_from_samples([20.00], 17.55)

        assert planning.basis == sb.BASIS_OBSERVED_SPARSE
        assert planning.planned_cost_usd == 17.55

    def test_no_observation_at_all_falls_back_to_the_reviewer_ceiling_sum(self) -> None:
        """The only circumstance a permission describes a cost: nothing observed."""
        planning = sb.review_cycle_planning_from_samples([], 17.55)

        assert planning.basis == sb.BASIS_REVIEW_CEILING_FALLBACK
        assert planning.derived is False
        assert planning.sample_count == 0
        assert planning.planned_cost_usd == 17.55
        assert "unobserved installation" in planning.reason

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


_TRIO = ["anthropic-haiku-cli", "google-gemini-3.5-flash-api", "openai-gpt-5.5-cli"]
_PAIR = ["google-gemini-3.5-flash-api", "openai-gpt-5.5-cli"]


class TestReviewCyclePricingLadder:
    """The panel that will run governs before review-at-large does."""

    def test_composition_key_is_order_independent(self) -> None:
        assert sb.composition_key(_TRIO) == sb.composition_key(list(reversed(_TRIO)))
        assert sb.composition_key([]) is None
        assert sb.composition_key(None) is None

    def test_seated_panel_history_governs_over_the_broader_population(
        self, tmp_path: Path
    ) -> None:
        """A 3-reviewer panel is not priced from a 2-reviewer population."""
        _seed_substrate(
            tmp_path,
            [
                _record(
                    run_id="trio",
                    score=5,
                    cost=9.0,
                    review_cycle_costs=[4.00, 4.00, 4.00],
                    review_pools=[_TRIO, _TRIO, _TRIO],
                ),
                _record(
                    run_id="pair",
                    score=5,
                    cost=4.0,
                    review_cycle_costs=[1.00, 1.00, 1.00, 1.00],
                    review_pools=[_PAIR, _PAIR, _PAIR, _PAIR],
                ),
            ],
        )

        planning = sb.derive_review_cycle_planning_price(
            tmp_path, configured_ceiling_usd=17.55, composition=_TRIO
        )

        assert planning.basis == sb.BASIS_OBSERVED_COMPOSITION
        assert planning.sample_count == 3
        assert planning.composition == tuple(sorted(_TRIO))
        assert planning.planned_cost_usd == round(4.00 * 1.25, 4)
        # Pooling all seven cycles would have priced this panel at the blended
        # median of $1.00 — under half what it actually costs.
        assert planning.planned_cost_usd > 1.00 * 1.25

    def test_panel_below_the_floor_falls_to_review_at_large_not_the_ceiling(
        self, tmp_path: Path
    ) -> None:
        _seed_substrate(
            tmp_path,
            [
                _record(
                    run_id="trio",
                    score=5,
                    cost=9.0,
                    review_cycle_costs=[4.00],
                    review_pools=[_TRIO],
                ),
                _record(
                    run_id="pair",
                    score=5,
                    cost=4.0,
                    review_cycle_costs=[1.00, 2.00, 3.00],
                    review_pools=[_PAIR, _PAIR, _PAIR],
                ),
            ],
        )

        planning = sb.derive_review_cycle_planning_price(
            tmp_path, configured_ceiling_usd=17.55, composition=_TRIO
        )

        assert planning.basis == sb.BASIS_OBSERVED_REVIEW_CYCLE
        assert planning.sample_count == 4
        assert planning.planned_cost_usd < 17.55

    def test_an_unidentifiable_panel_still_counts_toward_review_at_large(
        self, tmp_path: Path
    ) -> None:
        """A cycle with no recorded panel is evidence about review, not about a panel."""
        _seed_substrate(
            tmp_path,
            [
                _record(
                    run_id="unlabelled",
                    score=5,
                    cost=9.0,
                    review_cycle_costs=[2.00, 2.00, 2.00],
                    review_pools=None,
                ),
            ],
        )

        panel = sb.derive_review_cycle_planning_price(
            tmp_path, configured_ceiling_usd=17.55, composition=_TRIO
        )
        assert panel.basis == sb.BASIS_OBSERVED_REVIEW_CYCLE
        assert panel.sample_count == 3

    def test_dense_panel_history_above_the_current_ceiling_is_capped(self, tmp_path: Path) -> None:
        """History from a pricier era cannot reserve spend this panel cannot make."""
        _seed_substrate(
            tmp_path,
            [
                _record(
                    run_id="trio",
                    score=5,
                    cost=30.0,
                    review_cycle_costs=[9.00, 9.00, 9.00],
                    review_pools=[_TRIO, _TRIO, _TRIO],
                ),
            ],
        )

        planning = sb.derive_review_cycle_planning_price(
            tmp_path, configured_ceiling_usd=3.00, composition=_TRIO
        )

        assert planning.basis == sb.BASIS_OBSERVED_COMPOSITION
        assert planning.planned_cost_usd == 3.00

    def test_dense_population_history_above_the_current_ceiling_is_capped(
        self, tmp_path: Path
    ) -> None:
        _seed_substrate(
            tmp_path,
            [
                _record(
                    run_id="pair",
                    score=5,
                    cost=30.0,
                    review_cycle_costs=[9.00, 9.00, 9.00],
                    review_pools=[_PAIR, _PAIR, _PAIR],
                ),
            ],
        )

        planning = sb.derive_review_cycle_planning_price(
            tmp_path, configured_ceiling_usd=3.00, composition=_TRIO
        )

        assert planning.basis == sb.BASIS_OBSERVED_REVIEW_CYCLE
        assert planning.planned_cost_usd == 3.00

    def test_no_history_whatsoever_reaches_the_ceiling(self, tmp_path: Path) -> None:
        _seed_substrate(
            tmp_path,
            [_record(run_id="r1", score=5, cost=4.0, review_cycle_costs=[])],
        )

        planning = sb.derive_review_cycle_planning_price(
            tmp_path, configured_ceiling_usd=17.55, composition=_TRIO
        )

        assert planning.basis == sb.BASIS_REVIEW_CEILING_FALLBACK
        assert planning.planned_cost_usd == 17.55


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
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(),
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
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(),
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
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(),
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
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(),
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
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(),
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
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(),
            review_cycle_cost_usd=17.55,
            requested_review_max=5,
            spent_so_far_usd=0.0,
        )
        assert sb.seating_shortfall(allocation, record, participants=["a"]) is None

    def _issue_2252_allocation(self) -> dict:
        # Run 076fa19d5fc3: 26 score-4 samples, max $8.14, allocated at 1.25x.
        return {
            "allocation_usd": 10.18,
            "basis": sb.BASIS_SUBSTRATE_BAND,
            "complexity_score": 4,
            "median_usd": 1.69,
            "p90_usd": 4.87,
            "max_usd": 8.14,
            "sample_count": 26,
        }

    def test_issue_2252_score_scoped_estimate_keeps_review_fundable(self) -> None:
        """The score-4 dev average ($2.32) x 1.25 leaves both cycles funded.

        The run that failed subtracted $9.89 — a medium-band, single-model
        average x 2.0 — from this same $10.18 allocation and refused a $2.42
        cycle with $0.29 left, before any phase ran (#2284).
        """
        record = sb.reconcile_review_cycles(
            self._issue_2252_allocation(),
            dev_cost_estimate_usd=2.9,
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(4, sample_count=26),
            review_cycle_cost_usd=2.42,
            requested_review_max=2,
            spent_so_far_usd=0.0,
        )

        assert record["action"] == sb.RECONCILE_AFFORDABLE
        assert record["remaining_after_dev_usd"] == 7.28
        assert record["affordable_review_cycles"] == 3
        assert record["reserved_review_cycles"] == 2
        assert record["reserved_review_usd"] == 4.84
        assert sb.seating_shortfall(self._issue_2252_allocation(), record, participants=["a"]) is (
            None
        )

    def test_band_and_model_estimate_is_a_no_op_not_a_seating_shortfall(self) -> None:
        """The exact figures from run 076fa19d5fc3, on their real basis.

        $9.89 is a medium-band, single-model average. Subtracting it from a
        score-4 allocation is the defect; refusing to subtract it leaves review
        permitted and DEV free to run.
        """
        allocation = self._issue_2252_allocation()
        record = sb.reconcile_review_cycles(
            allocation,
            dev_cost_estimate_usd=9.89,
            dev_cost_estimate_basis={
                "source": sb.DEV_ESTIMATE_SOURCE_BAND,
                "band": "medium",
                "complexity_score": 4,
                "statistic": "avg_cost_usd",
                "scope": sb.DEV_ESTIMATE_SCOPE_DEV_PHASE,
                "sample_count": 194,
                "headroom_basis": "band_headroom",
                "headroom_multiplier": 2.0,
                "allocation_comparable": False,
                "reason": "profile_history",
            },
            review_cycle_cost_usd=2.42,
            requested_review_max=2,
            spent_so_far_usd=0.0,
        )

        assert record["action"] == sb.RECONCILE_NONCOMPARABLE_DEV_ESTIMATE
        assert record["reconciled_review_max"] == 2
        assert record["reserved_review_cycles"] == 0
        assert record["reserved_review_usd"] == 0.0
        assert record["remaining_after_dev_usd"] is None
        assert record["dev_cost_estimate_comparable"] is False
        assert record["dev_cost_estimate_sample_count"] == 194
        assert sb.seating_shortfall(allocation, record, participants=["a"]) is None

        message = sb.format_reconciliation(record)
        assert "not on a common population" in message
        assert "medium-band, single-model average" in message
        # A refusal citing dollars invites raising a budget that was never the
        # constraint, so the message must not read as a shortfall.
        assert "short $" not in message
        assert "cannot fund" not in message

    def test_a_configured_fallback_estimate_is_also_not_comparable(self) -> None:
        record = sb.reconcile_review_cycles(
            self._issue_2252_allocation(),
            dev_cost_estimate_usd=9.89,
            dev_cost_estimate_basis={
                "source": sb.DEV_ESTIMATE_SOURCE_CONFIGURED,
                "statistic": "configured_budget_usd",
                "headroom_basis": None,
                "headroom_multiplier": None,
                "allocation_comparable": False,
                "reason": "insufficient_profile_history",
            },
            review_cycle_cost_usd=2.42,
            requested_review_max=2,
            spent_so_far_usd=0.0,
        )

        assert record["action"] == sb.RECONCILE_NONCOMPARABLE_DEV_ESTIMATE
        assert "configured fallback: insufficient_profile_history" in sb.format_reconciliation(
            record
        )

    def test_an_absent_basis_is_not_subtracted(self) -> None:
        record = sb.reconcile_review_cycles(
            self._issue_2252_allocation(),
            dev_cost_estimate_usd=9.89,
            dev_cost_estimate_basis=None,
            review_cycle_cost_usd=2.42,
            requested_review_max=2,
            spent_so_far_usd=0.0,
        )

        assert record["action"] == sb.RECONCILE_NONCOMPARABLE_DEV_ESTIMATE
        assert record["reconciled_review_max"] == 2
        assert "no recorded basis" in sb.format_reconciliation(record)

    def test_a_score_scoped_estimate_from_another_score_is_not_subtracted(self) -> None:
        """Same shape, different population — a carried-over estimate."""
        record = sb.reconcile_review_cycles(
            self._issue_2252_allocation(),
            dev_cost_estimate_usd=2.9,
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(9, sample_count=26),
            review_cycle_cost_usd=2.42,
            requested_review_max=2,
            spent_so_far_usd=0.0,
        )

        assert record["action"] == sb.RECONCILE_NONCOMPARABLE_DEV_ESTIMATE
        assert record["complexity_score"] == 4
        assert record["dev_cost_estimate_complexity_score"] == 9


class TestReviewFundingReservation:
    """A seated review cycle is money held, not money hoped for (#2258)."""

    def _allocation(self, usd: float = 4.12) -> dict:
        # The band from run 9b3fa1bf44a4 / story issue-2252.
        return {
            "allocation_usd": usd,
            "basis": sb.BASIS_SUBSTRATE_BAND,
            "complexity_score": 3,
            "median_usd": 0.94,
            "p90_usd": 2.38,
            "max_usd": 3.30,
            "sample_count": 20,
        }

    def _reconcile(self, *, usd: float = 4.12, review_max: int = 1) -> dict:
        return sb.reconcile_review_cycles(
            self._allocation(usd),
            dev_cost_estimate_usd=2.38,
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(3, sample_count=20),
            review_cycle_cost_usd=1.01,
            requested_review_max=review_max,
            spent_so_far_usd=0.0,
        )

    def test_an_affordable_seating_reserves_the_cycles_it_granted(self) -> None:
        record = self._reconcile()

        assert record["action"] == sb.RECONCILE_AFFORDABLE
        assert record["reserved_review_cycles"] == 1
        assert record["reserved_review_usd"] == 1.01

    def test_a_reduced_seating_reserves_only_what_it_left_permitted(self) -> None:
        record = self._reconcile(review_max=5)

        assert record["action"] == sb.RECONCILE_REDUCED
        assert record["reconciled_review_max"] == record["reserved_review_cycles"]
        assert record["reserved_review_usd"] == round(record["reconciled_review_max"] * 1.01, 4)

    def test_undecided_and_unfundable_seatings_reserve_nothing(self) -> None:
        base = dict(
            dev_cost_estimate_usd=2.38,
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(3, sample_count=20),
            review_cycle_cost_usd=1.01,
            requested_review_max=1,
            spent_so_far_usd=0.0,
        )
        unfundable = sb.reconcile_review_cycles(self._allocation(usd=3.0), **base)
        assert unfundable["action"] == sb.RECONCILE_UNFUNDABLE
        for record in (
            unfundable,
            sb.reconcile_review_cycles(None, **base),
            sb.reconcile_review_cycles(
                {**self._allocation(), "basis": sb.BASIS_CONFIGURED_FALLBACK}, **base
            ),
            sb.reconcile_review_cycles(self._allocation(), **{**base, "spent_so_far_usd": None}),
            sb.reconcile_review_cycles(self._allocation(), **{**base, "dev_cost_estimate_usd": 0}),
        ):
            assert record["reserved_review_cycles"] == 0
            assert record["reserved_review_usd"] == 0.0

    # ── The reserved balance funds review ────────────────────────────────────

    def _reservation(self, reserved_usd: float = 1.01, cycles: int = 1) -> dict:
        return {
            "allocation_usd": 4.12,
            "reserved_review_usd": reserved_usd,
            "reserved_review_cycles": cycles,
            "review_cycle_cost_usd": 1.01,
            "action": sb.RECONCILE_AFFORDABLE,
        }

    def test_the_issue_2252_dev_overrun_no_longer_defunds_the_panel(self) -> None:
        """Dev spent $4.00 of a $4.12 allocation; the reserved cycle still runs."""
        assert (
            sb.reserved_review_shortfall(
                self._reservation(),
                self._allocation(),
                observed_usd=4.00,
                review_observed_usd=0.0,
                participants=["openai-gpt-5.5-cli"],
                planned_usd=1.01,
            )
            is None
        )
        # Without the reservation this is the refusal the issue reports.
        assert (
            sb.phase_funding_shortfall(
                self._allocation(),
                4.00,
                phase="review",
                participants=["openai-gpt-5.5-cli"],
                planned_usd=1.01,
            )
            is not None
        )

    def test_a_cycle_beyond_the_reservation_may_still_draw_the_general_pool(self) -> None:
        """The reservation is a floor under verification, not a ceiling on it."""
        assert (
            sb.reserved_review_shortfall(
                self._reservation(),
                self._allocation(),
                observed_usd=1.51,  # $0.50 dev + $1.01 of review already run
                review_observed_usd=1.01,  # the reserved cycle is spent
                participants=["a"],
                planned_usd=1.01,
            )
            is None
        )

    def test_a_panel_neither_pool_funds_is_refused_with_the_reserved_figures(self) -> None:
        shortfall = sb.reserved_review_shortfall(
            self._reservation(),
            self._allocation(),
            observed_usd=4.05,
            review_observed_usd=1.01,
            participants=["a"],
            planned_usd=1.01,
        )

        assert shortfall is not None
        assert shortfall["phase"] == "review"
        assert shortfall["planned_usd"] == 1.01
        assert shortfall["reserved_review_usd"] == 1.01
        assert shortfall["reserved_review_remaining_usd"] == 0.0

    def test_no_reservation_falls_through_to_the_whole_allocation_check(self) -> None:
        for reservation in (None, {}, {"reserved_review_usd": 0.0}):
            shortfall = sb.reserved_review_shortfall(
                reservation,
                self._allocation(),
                observed_usd=4.00,
                review_observed_usd=0.0,
                participants=["a"],
                planned_usd=1.01,
            )
            assert shortfall is not None
            assert "reserved_review_usd" not in shortfall

    def test_unmeasured_spend_never_refuses_a_review_panel(self) -> None:
        for observed, review_observed in ((None, 0.0), (4.0, None), (None, None)):
            assert (
                sb.reserved_review_shortfall(
                    self._reservation(),
                    self._allocation(),
                    observed_usd=observed,
                    review_observed_usd=review_observed,
                    participants=["a"],
                    planned_usd=1.01,
                )
                is None
            )

    # ── The rest of the allocation is what dev may spend ─────────────────────

    def test_dev_is_funded_while_non_review_money_remains(self) -> None:
        assert (
            sb.nonreview_funding_exhausted(
                self._reservation(),
                self._allocation(),
                observed_usd=2.50,
                review_observed_usd=0.0,
                participants=["dev"],
            )
            is None
        )

    def test_dev_is_refused_once_only_the_reserved_money_is_left(self) -> None:
        shortfall = sb.nonreview_funding_exhausted(
            self._reservation(),
            self._allocation(),
            observed_usd=4.00,
            review_observed_usd=0.0,
            participants=["dev"],
        )

        assert shortfall is not None
        assert shortfall["phase"] == "dev"
        assert shortfall["nonreview_exhausted"] is True
        assert shortfall["nonreview_allocation_usd"] == 3.11
        assert shortfall["observed_usd"] == 4.00
        assert shortfall["reserved_review_usd"] == 1.01

        message = sb.format_shortfall(shortfall, story="issue-2252")
        assert "issue-2252" in message
        assert "$3.11" in message
        assert "reserved for the 1 review cycle(s)" in message

    def test_a_non_review_pool_spent_to_the_cent_refuses_the_next_attempt(self) -> None:
        """The boundary binds where the operator sees it: $3.11 of $3.11 is gone.

        ``4.12 - 1.01`` is ``3.1100000000000003`` in binary floating point, so an
        exactly-exhausted pool used to read as three ten-thousandths of a cent
        remaining — and the retry it funded would have spent the reserved cycle.
        """
        shortfall = sb.nonreview_funding_exhausted(
            self._reservation(),
            self._allocation(),
            observed_usd=3.11,
            review_observed_usd=0.0,
            participants=["dev"],
        )

        assert shortfall is not None
        assert shortfall["remaining_usd"] == 0.0
        assert shortfall["nonreview_allocation_usd"] == 3.11
        assert shortfall["observed_usd"] == 3.11
        # One cent short of the ceiling is still a funded attempt.
        assert (
            sb.nonreview_funding_exhausted(
                self._reservation(),
                self._allocation(),
                observed_usd=3.10,
                review_observed_usd=0.0,
                participants=["dev"],
            )
            is None
        )

    def test_a_panel_that_exactly_fits_its_reservation_is_funded(self) -> None:
        """The same rounding, in the direction that must not refuse work.

        ``3.03 - 2.02`` is ``1.0099999999999998``: a third reserved cycle that
        fits its reservation to the cent must not be refused for float noise,
        even when the general pool has nothing left to fall back on.
        """
        assert (
            sb.reserved_review_shortfall(
                {**self._reservation(), "reserved_review_usd": 3.03, "reserved_review_cycles": 3},
                {**self._allocation(usd=3.03)},
                observed_usd=2.02,
                review_observed_usd=2.02,
                participants=["a"],
                planned_usd=1.01,
            )
            is None
        )

    def test_a_remainder_that_exactly_covers_a_cycle_reserves_it(self) -> None:
        """Seating counts cycles in whole cents, not by float division."""
        record = sb.reconcile_review_cycles(
            self._allocation(usd=3.39),
            dev_cost_estimate_usd=2.38,
            dev_cost_estimate_basis=_comparable_dev_estimate_basis(3, sample_count=20),
            review_cycle_cost_usd=1.01,
            requested_review_max=1,
            spent_so_far_usd=0.0,
        )

        assert record["remaining_after_dev_usd"] == 1.01
        assert record["action"] == sb.RECONCILE_AFFORDABLE
        assert record["affordable_review_cycles"] == 1
        assert record["reserved_review_usd"] == 1.01

    def test_review_spend_does_not_count_against_the_dev_pool(self) -> None:
        """A cycle drawing its own reservation must not refuse the next dev attempt."""
        assert (
            sb.nonreview_funding_exhausted(
                self._reservation(),
                self._allocation(),
                observed_usd=3.01,  # $2.00 dev + $1.01 review
                review_observed_usd=1.01,
                participants=["dev"],
            )
            is None
        )

    def test_dev_is_never_refused_without_a_reservation_or_a_measured_total(self) -> None:
        for reservation, observed, review_observed in (
            (None, 4.0, 0.0),
            ({"reserved_review_usd": 0.0}, 4.0, 0.0),
            (self._reservation(), None, 0.0),
            (self._reservation(), 4.0, None),
        ):
            assert (
                sb.nonreview_funding_exhausted(
                    reservation,
                    self._allocation(),
                    observed_usd=observed,
                    review_observed_usd=review_observed,
                    participants=["dev"],
                )
                is None
            )
