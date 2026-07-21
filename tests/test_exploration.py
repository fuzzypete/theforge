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


def test_routing_key_normalizes_complexity_and_picks_primary_domain():
    key = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=["python", "api"])
    assert key.band == "large"
    assert key.domain == "api"  # lexicographically first
    assert key.as_str() == "dev:large:api"


def test_routing_key_no_domain():
    key = exp.RoutingKey.build(phase="dev", complexity="MEDIUM", domains=[])
    assert key.as_str() == "dev:medium:-"


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


def test_domain_scoped_aggregates_differ_by_domain():
    """A domain-keyed aggregate reads the story's domain slice, not the pooled band.

    Two stories at the same complexity band but different domains must NOT race
    against an identical pooled aggregate (the routing key records the domain, so
    the aggregation must honor it).
    """
    data: dict = {"models": {}}
    for _ in range(4):
        apply_run(data, RunOutcome("large", "m1", True, 1, 0.1, domains=["api"]))
    for _ in range(4):
        apply_run(data, RunOutcome("large", "m1", False, 1, 0.1, domains=["web"]))

    key_api = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=["api"])
    key_web = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=["web"])
    key_band = exp.RoutingKey.build(phase="dev", complexity="HIGH", domains=None)

    agg_api = exp.derive_key_aggregates(data, [_cand("m1")], key_api, min_sample_size=3)["m1"]
    agg_web = exp.derive_key_aggregates(data, [_cand("m1")], key_web, min_sample_size=3)["m1"]
    agg_band = exp.derive_key_aggregates(data, [_cand("m1")], key_band, min_sample_size=3)["m1"]

    assert agg_api.success_rate == 1.0  # strong in api
    assert agg_web.success_rate == 0.0  # weak in web
    # Band pools both domains → lands strictly between the two domain slices
    # (recency-weighted, so ~0.5 rather than exactly 0.5).
    assert agg_web.success_rate < agg_band.success_rate < agg_api.success_rate


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
    assert block["routing_key"] == "dev:large:-"
    assert block["pool"] == ["winner", "rival"]
    assert block["selected"] == "rival"
    assert block["winner"] == "winner"


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
    entry = cache["keys"]["dev:large:-"]
    assert entry["winner"] == "m1"
    assert entry["models"]["m1"]["runs"] == 4
    # Writing the cache is best-effort and lands under the gitignored path.
    out_path = tmp_path / ".forge" / "performance_table.yaml"
    exp.write_performance_cache(out_path, cache)
    assert out_path.exists()
    assert "never authoritative" in out_path.read_text()
