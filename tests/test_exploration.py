"""Unit coverage for the challenger-sampling exploration subsystem (#325).

These tests exercise the pure decision core in ``theforge.exploration`` directly:
audit-derived aggregation (taint-excluded, recency-weighted), sample-floor
gating, winner promotion AND dethroning (clause-5 inverse), stochastic
challenger selection with a recorded pool, the per-sprint budget cap, cold
start, failed-challenger recovery, and the derived (non-authoritative)
performance cache.
"""

from __future__ import annotations

import random

from theforge import exploration as exp
from theforge.config.types import RecencyConfig
from theforge.model_profiles import RunOutcome, apply_run


def _cand(name: str) -> exp.Candidate:
    return exp.Candidate(id=name, model=None, provider=None, cli=None, tier="mid")


def _profiles_with(runs: dict[str, list[tuple[bool, float, int]]]) -> dict:
    """Build a model_profiles dict from {model: [(success, cost, iters), ...]}."""
    data: dict = {"models": {}}
    for model, outcomes in runs.items():
        for success, cost, iters in outcomes:
            apply_run(data, RunOutcome("large", model, success, iters, cost))
    return data


# ── Routing key ────────────────────────────────────────────────────────


def test_routing_key_identity_is_phase_domain_and_band():
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=["python", "api"])
    assert key.band == "large"
    # The slot identity is (phase, domain, band) per the spec contract. Domains
    # are normalized (sorted, deduped) and folded into the key string.
    assert key.as_str() == "dev:large:api+python"
    assert key.domains == ("api", "python")


def test_routing_key_no_domain():
    key = exp.RoutingKey.build(phase="dev", complexity="MEDIUM", domains=[])
    assert key.as_str() == "dev:medium"
    assert key.domains == ()


def test_routing_keys_differing_only_in_band_are_distinct():
    api_small = exp.RoutingKey.build(phase="dev", complexity="LOW", domains=["api"])
    api_large = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=["api"])
    # Same domain, different band → distinct slots (the cycle-2 defect guard).
    assert api_small.as_str() == "dev:small:api"
    assert api_large.as_str() == "dev:large:api"


def test_routing_keys_differing_only_in_domain_are_distinct():
    api = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=["api"])
    web = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=["web"])
    # Same band, different domain → distinct slots (the cycle-1/cycle-3 guard).
    assert api.as_str() == "dev:large:api"
    assert web.as_str() == "dev:large:web"


# ── Aggregation: taint exclusion + recency ─────────────────────────────


def test_aggregates_exclude_tainted_runs():
    data: dict = {"models": {}}
    for _ in range(5):
        apply_run(data, RunOutcome("large", "m1", True, 1, 0.10))
    # Two tainted runs must contribute no weight to the admissible aggregate.
    for _ in range(2):
        apply_run(data, RunOutcome("large", "m1", False, 1, 0.10, dev_tainted=True))
    aggs = exp.derive_key_aggregates(
        data,
        [_cand("m1")],
        exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None),
        min_sample_size=3,
    )
    agg = aggs["m1"]
    assert agg.runs == 5  # tainted runs excluded from admissible count
    assert agg.tainted_runs == 2
    assert agg.success_rate == 1.0  # only the clean successes counted


def test_aggregate_is_scoped_to_domain_and_band_cross_slice():
    """The per-key aggregate is the true (domain, band) cross slice — both axes.

    Same band, different domain → different runs/rate (no domain collapse, the
    cycle-1/cycle-3 defect); different band, same domain → different runs/rate
    (no band collapse, the cycle-2 defect). Both must hold simultaneously.
    """
    data: dict = {"models": {}}
    # (api, large): 4 successes; (web, large): 4 failures; (api, small): 4 fails.
    for _ in range(4):
        apply_run(data, RunOutcome("large", "m1", True, 1, 0.1, domains=["api"]))
    for _ in range(4):
        apply_run(data, RunOutcome("large", "m1", False, 1, 0.1, domains=["web"]))
    for _ in range(4):
        apply_run(data, RunOutcome("small", "m1", False, 1, 0.1, domains=["api"]))

    def _agg(complexity, domains):
        key = exp.RoutingKey.build(phase="dev", complexity=complexity, domains=domains)
        return exp.derive_key_aggregates(data, [_cand("m1")], key, min_sample_size=3)["m1"]

    api_large = _agg("HIGH", ["api"])
    web_large = _agg("HIGH", ["web"])
    api_small = _agg("LOW", ["api"])

    # Domain axis honored (same band large): api strong, web weak.
    assert api_large.success_rate == 1.0
    assert web_large.success_rate == 0.0
    # Band axis honored (same domain api): large strong, small weak.
    assert api_small.success_rate == 0.0
    # Each cross slice counts only its own (domain, band) runs → independent cadence.
    assert api_large.runs == 4
    assert web_large.runs == 4
    assert api_small.runs == 4


def test_domain_key_honors_configurable_recency_mode():
    """Domain-bearing keys use the SHARED configurable recency mechanism (#1392).

    An operator setting assignment.recency.mode must apply to (domain, band)
    routing slots too, not a hardcoded window: with mode="off" the ranked rate is
    the lifetime raw average; with exponential decay recent outcomes dominate.
    """
    data: dict = {"models": {}}
    # api-large: 10 old failures then 10 recent successes → raw 0.5.
    for _ in range(10):
        apply_run(data, RunOutcome("large", "m1", False, 1, 0.1, domains=["api"]))
    for _ in range(10):
        apply_run(data, RunOutcome("large", "m1", True, 1, 0.1, domains=["api"]))
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=["api"])

    off = exp.derive_key_aggregates(
        data, [_cand("m1")], key, min_sample_size=3, recency=RecencyConfig(mode="off")
    )["m1"]
    expo = exp.derive_key_aggregates(
        data,
        [_cand("m1")],
        key,
        min_sample_size=3,
        recency=RecencyConfig(mode="exponential", half_life_runs=5),
    )["m1"]

    assert off.success_rate == 0.5  # recency disabled → lifetime raw average
    assert expo.success_rate > 0.5  # exponential decay weights recent successes up


def test_cadence_counter_is_independent_per_domain_at_same_band():
    """Same-band api and web stories drive SEPARATE cadence counters.

    The cadence "every Nth run per routing key" reads the per-(domain, band)
    cross-slice run count, so accumulating api-large runs never advances the
    web-large cadence. Here api-large is at its Nth run (challenger fires) while
    web-large is not (winner mode) — despite sharing the "large" band.
    """
    data: dict = {"models": {}}
    for _ in range(4):  # api-large: 4 admissible runs → this is the 5th → cadence hit
        apply_run(data, RunOutcome("large", "winner", True, 1, 0.1, domains=["api"]))
    for _ in range(5):  # web-large: 5 admissible runs → this is the 6th → NOT a hit
        apply_run(data, RunOutcome("large", "winner", True, 1, 0.1, domains=["web"]))
    cands = [_cand("winner"), _cand("rival")]

    def _decide(domains):
        key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=domains)
        aggs = exp.derive_key_aggregates(data, cands, key, min_sample_size=3)
        return exp.decide_exploration(
            key=key,
            candidates=cands,
            aggregates=aggs,
            winner=exp.select_winner(aggs, 3),
            explore_every_n=5,
            min_sample_size=3,
            sprint_budget_remaining=1,
            rng=random.Random(0),
        )

    api_out = _decide(["api"])
    web_out = _decide(["web"])
    assert api_out.routing_key == "dev:large:api"
    assert web_out.routing_key == "dev:large:web"
    # Independent cadence: api at its Nth run fires; web (different count) does not.
    assert api_out.mode == exp.MODE_CHALLENGER
    assert api_out.reason == exp.REASON_CADENCE
    assert web_out.mode == exp.MODE_WINNER
    assert web_out.reason == exp.REASON_CADENCE_MISS


def test_aggregate_below_floor_has_no_rate():
    data = _profiles_with({"m1": [(True, 0.1, 1), (True, 0.1, 1)]})  # 2 < floor 3
    aggs = exp.derive_key_aggregates(
        data,
        [_cand("m1")],
        exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None),
        min_sample_size=3,
    )
    assert aggs["m1"].success_rate is None
    assert not aggs["m1"].meets_floor(3)


# ── Winner selection + sample floor + promotion/dethrone ───────────────


def test_select_winner_none_on_cold_start():
    data = _profiles_with({"m1": [(True, 0.1, 1)]})  # below floor
    aggs = exp.derive_key_aggregates(
        data,
        [_cand("m1")],
        exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None),
        min_sample_size=3,
    )
    assert exp.select_winner(aggs, 3) is None


def test_select_winner_prefers_higher_success_then_cost():
    data = _profiles_with(
        {
            "cheap_bad": [(False, 0.1, 1)] * 3 + [(True, 0.1, 1)] * 1,  # 0.25
            "pricey_good": [(True, 2.0, 1)] * 4,  # 1.0
        }
    )
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    aggs = exp.derive_key_aggregates(
        data, [_cand("cheap_bad"), _cand("pricey_good")], key, min_sample_size=3
    )
    assert exp.select_winner(aggs, 3) == "pricey_good"


def test_winner_promotion_then_dethrone_with_recency():
    """A later challenger race with fresh evidence dethrones the winner (clause 5)."""
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    # Round 1: incumbent is clean, challenger has a weak record → incumbent wins.
    data = _profiles_with(
        {
            "incumbent": [(True, 0.1, 1)] * 5,
            "challenger": [(False, 0.1, 1)] * 3 + [(True, 0.1, 1)],  # 0.25
        }
    )
    aggs = exp.derive_key_aggregates(
        data, [_cand("incumbent"), _cand("challenger")], key, min_sample_size=3
    )
    assert exp.select_winner(aggs, 3) == "incumbent"

    # Challenger races repeatedly and now wins recently while incumbent regresses.
    for _ in range(8):
        apply_run(data, RunOutcome("large", "challenger", True, 1, 0.1))
    for _ in range(8):
        apply_run(data, RunOutcome("large", "incumbent", False, 1, 0.1))
    aggs2 = exp.derive_key_aggregates(
        data, [_cand("incumbent"), _cand("challenger")], key, min_sample_size=3
    )
    # No winner is permanent — the recency-weighted view promotes the challenger.
    assert exp.select_winner(aggs2, 3) == "challenger"


# ── Cost-to-trusted-completion objective + reliability floor (#2392) ───


def test_completion_cost_divides_attempt_cost_by_success_rate():
    """The objective is total cost to TRUSTED completion, not per-attempt cost."""
    agg = exp.ModelAggregate(
        model_id="m",
        runs=4,
        success_rate=0.5,
        avg_cost_usd=1.0,
        avg_iterations=1.0,
        avg_duration_s=1.0,
        cost_measured_runs=4,
    )
    # Half the attempts fail, so reaching completion costs two attempts' worth.
    assert agg.completion_cost() == 2.0
    assert exp.COMPLETION_COST_FORMULA == "avg_cost_usd / success_rate"


def test_select_winner_prefers_cheaper_completion_over_higher_success():
    """The contract this story flips (#2392).

    ``pricey_good`` succeeds MORE often, but ``cheap_ok`` clears the reliability
    floor and reaches trusted completion for a fraction of the price. Under the
    old success-rate-first ranking, cost was consulted only on an exact rate tie
    — which recency-weighted fractional rates never produce — so the expensive
    incumbent could never be displaced. It now loses on completion cost.
    """
    data = _profiles_with(
        {
            "cheap_ok": [(True, 0.1, 1)] * 4 + [(False, 0.1, 1)],  # 0.8 @ $0.10
            "pricey_good": [(True, 2.0, 1)] * 5,  # 1.0 @ $2.00
        }
    )
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    aggs = exp.derive_key_aggregates(
        data, [_cand("cheap_ok"), _cand("pricey_good")], key, min_sample_size=3
    )
    sel = exp.select_winner_evidence(aggs, 3, reliability_floor=0.7)
    assert sel.winner == "cheap_ok"
    assert sel.reason == exp.SELECTION_COST_QUALIFIED
    # ~$0.125 to completion vs $2.00 — an order of magnitude, previously unreachable.
    assert sel.completion_costs["cheap_ok"] < sel.completion_costs["pricey_good"]


def test_reliability_floor_excludes_cheap_unreliable_candidate():
    """Cheap is not enough: below the floor a candidate is excluded outright.

    Cost-first ranking without a floor would crown whatever fails cheapest, so
    the floor is what makes misjudging a model degrade gracefully in BOTH
    directions.
    """
    data = _profiles_with(
        {
            "cheap_flaky": [(True, 0.01, 1)] + [(False, 0.01, 1)] * 4,  # 0.2 @ $0.01
            "solid": [(True, 1.0, 1)] * 5,  # 1.0 @ $1.00
        }
    )
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    aggs = exp.derive_key_aggregates(
        data, [_cand("cheap_flaky"), _cand("solid")], key, min_sample_size=3
    )
    sel = exp.select_winner_evidence(aggs, 3, reliability_floor=0.7)
    assert sel.winner == "solid"
    assert "cheap_flaky" in sel.below_reliability_floor
    # Excluded from exploitation — but still a candidate the explorer may race.
    assert "cheap_flaky" not in sel.qualified


def test_no_winner_when_nothing_clears_the_reliability_floor():
    data = _profiles_with({"flaky": [(True, 0.1, 1)] + [(False, 0.1, 1)] * 4})
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    aggs = exp.derive_key_aggregates(data, [_cand("flaky")], key, min_sample_size=3)
    sel = exp.select_winner_evidence(aggs, 3, reliability_floor=0.7)
    assert sel.winner is None
    assert sel.reason == exp.SELECTION_BELOW_FLOOR


def test_unmeasured_cost_is_surfaced_not_ranked_as_most_expensive():
    """An unmeasured candidate is reported as unmeasured, never ranked worst.

    The old key sorted a missing ``avg_cost_usd`` as ``float('inf')`` — silently
    the most expensive model possible — on a tie-break it could never reach. Now
    it is excluded from cost-ranked exploitation and NAMED, so bounded
    exploration can go measure it.
    """
    data: dict = {"models": {}}
    for _ in range(5):
        apply_run(data, RunOutcome("large", "unmeasured", True, 1, None))
        apply_run(data, RunOutcome("large", "measured", True, 1, 3.0))
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    aggs = exp.derive_key_aggregates(
        data, [_cand("unmeasured"), _cand("measured")], key, min_sample_size=3
    )
    assert aggs["unmeasured"].cost_measured is False
    assert aggs["unmeasured"].completion_cost() is None
    sel = exp.select_winner_evidence(aggs, 3, reliability_floor=0.7)
    assert sel.winner == "measured"
    assert sel.unmeasured_cost == ("unmeasured",)


def test_no_winner_when_every_reliable_candidate_is_unmeasured():
    """No completion-cost evidence anywhere → no exploitation, declared tier stands."""
    data: dict = {"models": {}}
    for _ in range(5):
        apply_run(data, RunOutcome("large", "m1", True, 1, None))
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    aggs = exp.derive_key_aggregates(data, [_cand("m1")], key, min_sample_size=3)
    sel = exp.select_winner_evidence(aggs, 3, reliability_floor=0.7)
    assert sel.winner is None
    assert sel.reason == exp.SELECTION_UNMEASURED
    assert sel.unmeasured_cost == ("m1",)


# ── Challenger selection: stochastic, recorded pool ────────────────────


def test_choose_challenger_excludes_winner_and_is_reproducible():
    pool = ["a", "b", "c"]
    rng = random.Random(1234)
    pick = exp.choose_challenger(pool, "a", rng)
    assert pick in {"b", "c"}
    # Same seed → same draw (reconstructable).
    assert exp.choose_challenger(pool, "a", random.Random(1234)) == pick


def test_choose_challenger_none_when_only_winner_eligible():
    assert exp.choose_challenger(["a"], "a", random.Random(0)) is None


def _agg(model_id: str, runs: int) -> exp.ModelAggregate:
    return exp.ModelAggregate(
        model_id=model_id,
        runs=runs,
        success_rate=1.0,
        avg_cost_usd=1.0,
        avg_iterations=1.0,
        avg_duration_s=1.0,
        cost_measured_runs=runs,
    )


def test_challenger_rotation_draws_the_least_sampled_alternative():
    """Least-sampled-first: the alternative with the thinnest record races next."""
    aggs = {m: _agg(m, r) for m, r in (("w", 40), ("a", 9), ("b", 2), ("c", 7))}
    pick = exp.choose_challenger(["w", "a", "b", "c"], "w", random.Random(0), aggregates=aggs)
    assert pick == "b"


def test_challenger_rotation_gives_every_alternative_a_race_within_pool_size():
    """The evidence-accumulation rate is bounded BELOW, not just above (#2392).

    A uniform draw lets a specific alternative go arbitrarily long without a
    race, so the incumbent's volume made its own displacement impossible. Under
    rotation, racing a candidate raises its run count, so within ``len(pool)-1``
    cadence hits every eligible alternative has been raced at least once.
    """
    pool = ["w", "a", "b", "c"]
    runs = {"w": 40, "a": 0, "b": 0, "c": 0}
    raced: list[str] = []
    for _ in range(len(pool) - 1):
        aggs = {m: _agg(m, r) for m, r in runs.items()}
        pick = exp.choose_challenger(pool, "w", random.Random(0), aggregates=aggs)
        assert pick is not None
        raced.append(pick)
        runs[pick] += 1  # the race itself is the evidence it accumulates
    assert sorted(raced) == ["a", "b", "c"]


def test_challenger_rotation_random_mode_restores_uniform_draw():
    aggs = {m: _agg(m, r) for m, r in (("w", 40), ("a", 9), ("b", 2))}
    picks = {
        exp.choose_challenger(
            ["w", "a", "b"],
            "w",
            random.Random(seed),
            aggregates=aggs,
            rotation=exp.ROTATION_RANDOM,
        )
        for seed in range(20)
    }
    # Uniform mode ignores sample counts, so the well-sampled "a" is still drawn.
    assert picks == {"a", "b"}


def test_decide_exploration_rotates_to_the_least_sampled_candidate():
    """The rotation reaches decide_exploration, not just the helper."""
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    # 4 admissible runs for the key → this is the 5th → cadence hit at N=5.
    data = _profiles_with(
        {
            "winner": [(True, 0.1, 1)] * 2,
            "well_sampled": [(True, 0.1, 1)] * 2,
            "starved": [],
        }
    )
    cands = [_cand("winner"), _cand("well_sampled"), _cand("starved")]
    aggs = exp.derive_key_aggregates(data, cands, key, min_sample_size=3)
    out = exp.decide_exploration(
        key=key,
        candidates=cands,
        aggregates=aggs,
        winner="winner",
        explore_every_n=5,
        min_sample_size=3,
        sprint_budget_remaining=1,
        rng=random.Random(0),
    )
    assert out.mode == exp.MODE_CHALLENGER
    assert out.selected == "starved"  # zero runs — the thinnest record races


# ── decide_exploration: cadence, cold start, sprint cap ────────────────


def _steady_aggs(runs_each: int) -> tuple[exp.RoutingKey, list[exp.Candidate], dict]:
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    data = _profiles_with(
        {"winner": [(True, 0.1, 1)] * runs_each, "rival": [(True, 0.2, 1)] * runs_each}
    )
    cands = [_cand("winner"), _cand("rival")]
    aggs = exp.derive_key_aggregates(data, cands, key, min_sample_size=3)
    return key, cands, aggs


def test_decide_cold_start_marks_exploring_without_overriding_selection():
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    cands = [_cand("m1"), _cand("m2")]
    aggs = exp.derive_key_aggregates(
        _profiles_with({"m1": [(True, 0.1, 1)]}), cands, key, min_sample_size=3
    )
    out = exp.decide_exploration(
        key=key,
        candidates=cands,
        aggregates=aggs,
        winner=None,
        explore_every_n=5,
        min_sample_size=3,
        sprint_budget_remaining=1,
        rng=random.Random(0),
    )
    assert out.mode == exp.MODE_CHALLENGER
    assert out.reason == exp.REASON_COLD_START
    assert out.selected is None  # cold start defers selection to static-tier routing
    assert out.consumes_slot is True
    assert set(out.pool) == {"m1", "m2"}


def test_decide_fires_challenger_on_cadence_run():
    # 4 admissible runs total → this run is the 5th → cadence hit at N=5.
    key, cands, aggs = _steady_aggs(2)  # 2 + 2 = 4 runs
    out = exp.decide_exploration(
        key=key,
        candidates=cands,
        aggregates=aggs,
        winner="winner",
        explore_every_n=5,
        min_sample_size=3,
        sprint_budget_remaining=1,
        rng=random.Random(7),
    )
    assert out.mode == exp.MODE_CHALLENGER
    assert out.reason == exp.REASON_CADENCE
    assert out.selected == "rival"  # only eligible non-winner
    assert out.winner == "winner"
    assert out.pool == ["winner", "rival"]  # full draw space recorded


def test_decide_winner_when_not_cadence_run():
    key, cands, aggs = _steady_aggs(3)  # 6 runs → 7th run, 7 % 5 != 0
    out = exp.decide_exploration(
        key=key,
        candidates=cands,
        aggregates=aggs,
        winner="winner",
        explore_every_n=5,
        min_sample_size=3,
        sprint_budget_remaining=1,
        rng=random.Random(7),
    )
    assert out.mode == exp.MODE_WINNER
    assert out.reason == exp.REASON_CADENCE_MISS
    assert out.selected == "winner"


def test_decide_sprint_cap_forces_winner_but_stays_labeled():
    key, cands, aggs = _steady_aggs(2)  # cadence would fire (5th run)
    out = exp.decide_exploration(
        key=key,
        candidates=cands,
        aggregates=aggs,
        winner="winner",
        explore_every_n=5,
        min_sample_size=3,
        sprint_budget_remaining=0,
        rng=random.Random(7),
    )
    assert out.mode == exp.MODE_WINNER
    assert out.reason == exp.REASON_SPRINT_CAP
    # Even a capped run is labeled with the key + candidate pool (no silent off-policy).
    assert out.routing_key == key.as_str()
    assert out.pool == ["winner", "rival"]


def test_decide_disabled_when_cadence_below_one():
    key, cands, aggs = _steady_aggs(2)
    out = exp.decide_exploration(
        key=key,
        candidates=cands,
        aggregates=aggs,
        winner="winner",
        explore_every_n=0,
        min_sample_size=3,
        sprint_budget_remaining=5,
        rng=random.Random(0),
    )
    assert out.mode == exp.MODE_WINNER
    assert out.reason == exp.REASON_DISABLED


def test_block_is_reconstructable():
    key, cands, aggs = _steady_aggs(2)
    out = exp.decide_exploration(
        key=key,
        candidates=cands,
        aggregates=aggs,
        winner="winner",
        explore_every_n=5,
        min_sample_size=3,
        sprint_budget_remaining=1,
        rng=random.Random(7),
    )
    block = out.to_block()
    assert block["mode"] == "challenger"
    assert block["routing_key"] == "dev:large"
    assert block["pool"] == ["winner", "rival"]
    assert block["selected"] == "rival"
    assert block["winner"] == "winner"
    assert block["domains"] == []  # no domains on this key


# ── Failed-challenger recovery ─────────────────────────────────────────


def test_recover_from_failed_challenger_retries_through_winner():
    out = exp.ExplorationOutcome(
        mode=exp.MODE_CHALLENGER,
        routing_key="dev:large:-",
        pool=["winner", "rival"],
        selected="rival",
        winner="winner",
        reason=exp.REASON_CADENCE,
    )
    recovery = exp.recover_from_failed_challenger(out)
    assert recovery is not None
    assert recovery.retry_selection == "winner"
    assert recovery.failure_record["kind"] == "exploration_failure"
    assert recovery.failure_record["challenger"] == "rival"
    assert recovery.failure_record["recovered_via"] == "winner"


def test_recover_none_for_winner_mode():
    out = exp.ExplorationOutcome(
        mode=exp.MODE_WINNER,
        routing_key="dev:large:-",
        pool=["winner"],
        selected="winner",
        winner="winner",
        reason=exp.REASON_ON_POLICY,
    )
    assert exp.recover_from_failed_challenger(out) is None


def test_recover_cold_start_falls_back_to_normal_tier():
    out = exp.ExplorationOutcome(
        mode=exp.MODE_CHALLENGER,
        routing_key="dev:large:-",
        pool=["m1", "m2"],
        selected=None,
        winner=None,
        reason=exp.REASON_COLD_START,
    )
    recovery = exp.recover_from_failed_challenger(out)
    assert recovery.retry_selection is None
    assert recovery.failure_record["recovered_via"] == "normal_tier"


# ── Derived performance cache (non-authoritative) ──────────────────────


def test_build_performance_cache_is_derived_from_audit(tmp_path):
    data = _profiles_with({"m1": [(True, 0.1, 1)] * 4, "m2": [(False, 0.1, 1)] * 4})
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)
    cache = exp.build_performance_cache(
        data, [key], {"dev": [_cand("m1"), _cand("m2")]}, min_sample_size=3
    )
    entry = cache["keys"]["dev:large"]
    assert entry["winner"] == "m1"
    assert entry["models"]["m1"]["runs"] == 4
    # Writing the cache is best-effort and lands under the gitignored path.
    out_path = tmp_path / ".forge" / "performance_table.yaml"
    exp.write_performance_cache(out_path, cache)
    assert out_path.exists()
    assert "never authoritative" in out_path.read_text()
