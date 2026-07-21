"""Seam-level coverage for challenger-sampling exploration (#325).

Exercises ``assign_models`` end-to-end so the exploration decision is verified
where it actually runs — through the real routing pass and the routing_decision
block, not just the pure decision core. Covers: a challenger firing with a
recorded pool and selected model; the per-sprint cap forcing winner mode;
challenger-tier budget envelopes; cold start via the static tier table; and
cache non-authority (decisions derive from audit records even when a
contradictory performance_table.yaml sits on disk).
"""

from __future__ import annotations

import random

import pytest

from theforge.assignment import assign_models
from theforge.config import AgentDef, AssignmentConfig
from theforge.config.types import ExplorationConfig
from theforge.model_profiles import RunOutcome, apply_run


@pytest.fixture()
def _anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def _agents(rival_tier: str = "strong") -> list[AgentDef]:
    return [
        AgentDef(
            name="winner",
            provider="anthropic",
            model="opus",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="rival",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=600,
            tier=rival_tier,
        ),
    ]


def _cfg(
    cap: int = 1, explore_every_n: int = 5, max_cost: float | None = 100.0
) -> AssignmentConfig:
    return AssignmentConfig(
        enabled=True,
        max_cost_per_story_usd=max_cost,
        exploration=ExplorationConfig(
            explore_every_n=explore_every_n, min_sample_size=3, per_sprint_cap=cap
        ),
    )


def _profiles(winner_runs: int = 4) -> dict:
    """Winner has an admissible clean record; rival has none (pure cold rival)."""
    data: dict = {"models": {}}
    for _ in range(winner_runs):
        apply_run(
            data,
            RunOutcome(
                "large",
                "winner",
                True,
                1,
                0.5,
                dev_actual_model="opus",
                dev_provider="anthropic",
            ),
        )
    return data


def _dev_exploration(decision) -> dict:
    return decision.routing_decision["dev"]["exploration"]


# ── Challenger fires with recorded pool + selection ────────────────────


def test_challenger_fires_on_cadence_and_overrides_dev(_anthropic_key):
    # 4 admissible winner runs → this is the 5th run for the key → cadence at N=5.
    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_profiles(4),
        sprint_exploration_budget=1,
        explore_rng=random.Random(0),
    )
    block = _dev_exploration(decision)
    assert block["mode"] == "challenger"
    assert block["reason"] == "challenger_cadence_hit"
    assert block["selected"] == "rival"
    assert block["winner"] == "winner"
    assert set(block["pool"]) == {"winner", "rival"}
    # The challenger actually replaces the winner as the dev model that runs.
    assert decision.dev.name == "rival"


def test_challenger_block_is_labeled_even_in_winner_mode(_anthropic_key):
    # 6 admissible runs → 7th run, not a cadence multiple of 5 → winner mode.
    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_profiles(6),
        sprint_exploration_budget=1,
        explore_rng=random.Random(0),
    )
    block = _dev_exploration(decision)
    assert block["mode"] == "winner"
    # Off-policy routing must never be unlabeled: key + pool recorded regardless.
    assert block["routing_key"] == "dev:large"
    assert set(block["pool"]) == {"winner", "rival"}
    assert decision.dev.name == "winner"


# ── Per-sprint cap ─────────────────────────────────────────────────────


def test_sprint_cap_zero_budget_forces_winner(_anthropic_key):
    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_profiles(4),
        sprint_exploration_budget=0,  # cap reached
        explore_rng=random.Random(0),
    )
    block = _dev_exploration(decision)
    assert block["mode"] == "winner"
    assert block["reason"] == "sprint_cap_reached"
    assert decision.dev.name == "winner"


def test_exploration_inactive_when_no_sprint_budget(_anthropic_key):
    # None budget (single-run / no sprint boundary) → on-policy winner, no challenger.
    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_profiles(4),
        sprint_exploration_budget=None,
    )
    block = _dev_exploration(decision)
    assert block["mode"] == "winner"
    assert block["reason"] == "on_policy_winner"
    assert decision.dev.name == "winner"


# ── Challenger-tier budget envelope ────────────────────────────────────


def test_challenger_spends_from_its_own_tier_envelope(_anthropic_key):
    # Rival is a CHEAPER tier (downward exploration, #170). When it becomes the
    # dev challenger the recorded dev budget is the challenger's, not the
    # incumbent strong winner's.
    decision = assign_models(
        _agents(rival_tier="cheap"),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_profiles(4),
        sprint_exploration_budget=1,
        explore_rng=random.Random(0),
    )
    assert _dev_exploration(decision)["mode"] == "challenger"
    assert decision.dev.name == "rival"
    assert decision.dev.budget_usd == 1.0  # challenger's envelope, not winner's 5.0


# ── Cold start via static tier table ───────────────────────────────────


def test_cold_start_marks_exploring_and_uses_static_tier(_anthropic_key):
    # No admissible history → no winner → cold-start exploring; the selection is
    # left to the deterministic static-tier pick (selected is None in the block).
    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        model_profiles={"models": {}},
        sprint_exploration_budget=1,
        explore_rng=random.Random(0),
    )
    block = _dev_exploration(decision)
    assert block["mode"] == "challenger"
    assert block["reason"] == "cold_start_exploring"
    assert block["selected"] is None
    # The static tier table still produced a concrete dev model.
    assert decision.dev.name in {"winner", "rival"}


# ── Cache non-authority ────────────────────────────────────────────────


def test_routing_ignores_stale_performance_cache_on_disk(_anthropic_key, tmp_path):
    """A contradictory performance_table.yaml must not influence routing."""
    stale = tmp_path / ".forge" / "performance_table.yaml"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("keys:\n  dev:large:-:\n    winner: rival\n", encoding="utf-8")
    # Winner has the only admissible record; the router derives its decision from
    # the audit-backed model_profiles, never the on-disk cache.
    decision = assign_models(
        _agents(),
        _cfg(explore_every_n=99),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_profiles(4),
        sprint_exploration_budget=1,
        explore_rng=random.Random(0),
    )
    # explore_every_n=99 → not a cadence run → winner mode; winner is the
    # audit-derived incumbent, NOT the cache's bogus "rival".
    assert decision.dev.name == "winner"
    assert _dev_exploration(decision)["winner"] == "winner"


# ── Empirical winner routed in winner mode (promotion/dethrone) ─────────


def _tiered_agents() -> list[AgentDef]:
    # A mid-tier agent (the deterministic MEDIUM pick) and a strong-tier agent
    # that is floor-compliant (strong >= mid) and empirically better.
    return [
        AgentDef(
            name="mid_pick",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="mid",
        ),
        AgentDef(
            name="strong_champ",
            provider="anthropic",
            model="opus",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="strong",
        ),
    ]


def test_empirical_winner_routed_over_static_incumbent_in_winner_mode(_anthropic_key):
    """A promoted winner off the static-tier pick is actually routed (winner mode)."""
    data: dict = {"models": {}}
    # MEDIUM band. The deterministic router picks the mid agent (effective tier
    # mid), but the audit-derived empirical winner is the floor-compliant strong
    # agent with the better record — it must be the routed dev model.
    for _ in range(4):
        apply_run(data, RunOutcome("medium", "mid_pick", True, 1, 0.5))  # mid tier, some wins
    for _ in range(3):
        apply_run(data, RunOutcome("medium", "mid_pick", False, 1, 0.5))  # mixed record
    for _ in range(5):
        apply_run(data, RunOutcome("medium", "strong_champ", True, 1, 0.5))  # strong, rate 1.0

    decision = assign_models(
        _tiered_agents(),
        _cfg(explore_every_n=99),  # never a cadence run → winner mode
        complexity="MEDIUM",
        complexity_score=5,
        model_profiles=data,
        sprint_exploration_budget=1,
        explore_rng=random.Random(0),
    )
    block = _dev_exploration(decision)
    assert block["mode"] == "winner"
    # The empirical winner (strong_champ) — not the deterministic mid pick —
    # is recorded AND routed.
    assert block["winner"] == "strong_champ"
    assert decision.dev.name == "strong_champ"


def test_winner_mode_promotion_is_budget_downgradable_to_floor(_anthropic_key):
    """A winner-mode empirical promotion is NORMAL routing: a tight per-story cost
    target can downgrade it to a floor-compliant cheaper dev (no protected
    challenger envelope). Contrast with challenger mode, whose budget floor IS the
    challenger tier."""
    data: dict = {"models": {}}
    for _ in range(4):
        apply_run(data, RunOutcome("medium", "mid_pick", True, 1, 0.5))
    for _ in range(3):
        apply_run(data, RunOutcome("medium", "mid_pick", False, 1, 0.5))
    for _ in range(5):
        apply_run(data, RunOutcome("medium", "strong_champ", True, 1, 0.5))

    # Tight cost target: the strong empirical winner cannot fit, but a mid-tier
    # floor-compliant dev (mid_pick) can. The enforcer must be free to downgrade.
    decision = assign_models(
        _tiered_agents(),
        _cfg(explore_every_n=99, max_cost=6.0),  # winner mode, tight cap
        complexity="MEDIUM",
        complexity_score=5,
        model_profiles=data,
        sprint_exploration_budget=1,
        explore_rng=random.Random(0),
    )
    assert _dev_exploration(decision)["mode"] == "winner"
    # Promotion is recorded, but the dev is downgraded back to the mid floor
    # (NOT protected at strong) so the per-story target is met.
    assert decision.dev.name == "mid_pick"
    assert decision.dev.model == "haiku"
    assert decision.budget_audit["downgraded"] is True
    assert decision.budget_audit["within_target"] is True


# ── Domain-scoped routing (routing key honors domain) ──────────────────


def _domain_agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="api_specialist",
            provider="anthropic",
            model="opus",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="web_specialist",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="strong",
        ),
    ]


def _domain_profiles() -> dict:
    data: dict = {"models": {}}
    # Each specialist is strong in its OWN (domain, large) cross slice and weak in
    # the other's, at the same band. The per-(domain, band) aggregate therefore
    # picks a different winner for the api key than the web key — the two slots do
    # not collapse despite sharing the "large" band.
    for _ in range(4):
        apply_run(data, RunOutcome("large", "api_specialist", True, 1, 0.5, domains=["api"]))
        apply_run(data, RunOutcome("large", "web_specialist", True, 1, 0.5, domains=["web"]))
    for _ in range(4):
        apply_run(data, RunOutcome("large", "api_specialist", False, 1, 0.5, domains=["web"]))
        apply_run(data, RunOutcome("large", "web_specialist", False, 1, 0.5, domains=["api"]))
    return data


def test_domain_routing_selects_domain_specialist(_anthropic_key):
    """Same complexity band, different domains → different empirical winner routed."""
    api_decision = assign_models(
        _domain_agents(),
        _cfg(explore_every_n=99),  # winner mode; empirical winner routed
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_domain_profiles(),
        domains=["api"],
        sprint_exploration_budget=1,
        explore_rng=random.Random(0),
    )
    web_decision = assign_models(
        _domain_agents(),
        _cfg(explore_every_n=99),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=_domain_profiles(),
        domains=["web"],
        sprint_exploration_budget=1,
        explore_rng=random.Random(0),
    )
    # The routing key is (phase, domain, band): same band, different domain →
    # DISTINCT keys and distinct cadence slots (the persistent finding's guard).
    api_block = _dev_exploration(api_decision)
    web_block = _dev_exploration(web_decision)
    assert api_block["routing_key"] == "dev:large:api"
    assert web_block["routing_key"] == "dev:large:web"
    assert api_block["domains"] == ["api"]
    assert web_block["domains"] == ["web"]
    # The (domain, band) cross-slice aggregate picks the domain specialist.
    assert api_decision.dev.name == "api_specialist"
    assert web_decision.dev.name == "web_specialist"
