"""Price-aware tie-break within an equal cost_rank / capability bucket (issue #1617).

Regression suite for the starvation bug: an enabled cheap-tier model with no run
history was never dispatched because every cheapest-tier selector broke the
sonnet/mini tie by ``models.enabled`` list order (and, for dev, by cold-start
last-place). These tests pin the fix — the genuinely cheaper model wins the tie
by its real per-MTok price — across every ranking helper the fix touches:
``_agents_by_tier``/``_rerank_by_profiles`` (assignment), ``derive_roles`` and
``_auto_assign_models`` (config), and ``_build_pool_entries`` /
``_apply_complexity_adaptation`` (coordinator preflight seam).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from theforge.assignment import (
    AssignmentConfig,
    _agents_by_tier,
    _pick_agent,
    _rerank_by_profiles,
    assign_models,
)
from theforge.config import (
    AGENT_REGISTRY,
    DEFAULT_VALIDATION,
    AgentDef,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.models import MODEL_REGISTRY, price_tiebreak_signal
from theforge.config.profiles import _agents_from_models, _auto_assign_models
from theforge.config.role_derivation import derive_roles
from theforge.config.types import PlanConfig
from theforge.coordinator.preflight import _apply_complexity_adaptation, _build_pool_entries
from theforge.model_profiles import get_observed_cost_tiebreak_signal

# The reported pool: cheap tier holds both sonnet and gpt-5.4-mini at
# cost_rank=1, capability=7. Sonnet is listed FIRST — the exact ordering that
# used to guarantee it won every cheap-tier tie.
_POOL_SONNET_FIRST = ["anthropic/sonnet/cli", "openai/gpt-5.4-mini/cli"]
_POOL_MINI_FIRST = ["openai/gpt-5.4-mini/cli", "anthropic/sonnet/cli"]


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")


@pytest.fixture
def _all_authed(monkeypatch):
    """Isolate ranking from transport auth: the pool models dispatch via the
    claude/codex CLIs, whose binaries aren't present in the test env. Force auth
    so the tests exercise the price tie-break, not binary discovery."""
    monkeypatch.setattr("theforge.assignment._has_auth", lambda *a, **k: True)


# ── price_tiebreak_signal ──────────────────────────────────────────────


def test_price_signal_uses_max_of_input_output():
    # Mirrors custom_model_cost_rank's price banding: max(input, output).
    assert price_tiebreak_signal(0.25, 2.00) == 2.00
    assert price_tiebreak_signal(3.00, 15.00) == 15.00


def test_price_signal_unpriced_sorts_last():
    signal = price_tiebreak_signal(None, None)
    assert signal == float("inf")
    # A priced model always ranks ahead of an unpriced one.
    assert price_tiebreak_signal(999.0, 999.0) < signal


def test_price_signal_partial_data_treats_missing_as_zero():
    assert price_tiebreak_signal(1.5, None) == 1.5
    assert price_tiebreak_signal(None, 4.0) == 4.0


# ── built-in registry now carries real prices ──────────────────────────


def test_builtin_registry_populates_cheap_tier_costs():
    sonnet = MODEL_REGISTRY["anthropic/sonnet/cli"]
    mini = MODEL_REGISTRY["openai/gpt-5.4-mini/cli"]
    assert (sonnet.input_cost_per_mtok, sonnet.output_cost_per_mtok) == (3.00, 15.00)
    assert (mini.input_cost_per_mtok, mini.output_cost_per_mtok) == (0.25, 2.00)
    # The cheaper model has the smaller tie-break signal.
    assert price_tiebreak_signal(
        mini.input_cost_per_mtok, mini.output_cost_per_mtok
    ) < price_tiebreak_signal(sonnet.input_cost_per_mtok, sonnet.output_cost_per_mtok)


# ── _agents_by_tier ────────────────────────────────────────────────────


@pytest.mark.parametrize("pool", [_POOL_SONNET_FIRST, _POOL_MINI_FIRST])
def test_agents_by_tier_orders_by_price_not_list_order(pool):
    agents = _agents_from_models(pool, budget_usd=50.0)
    ordered = _agents_by_tier(agents, "cheap")
    assert [a.name for a in ordered] == ["openai-gpt-5.4-mini-cli", "anthropic-sonnet-cli"]


def test_pick_agent_cheap_tier_picks_cheaper_model(_all_authed):
    agents = _agents_from_models(_POOL_SONNET_FIRST, budget_usd=50.0)
    picked = _pick_agent(agents, "cheap")
    assert picked is not None
    assert picked.model == "gpt-5.4-mini"


# ── assign_models: adaptive preflight (the reported dispatch path) ──────


@pytest.mark.parametrize("pool", [_POOL_SONNET_FIRST, _POOL_MINI_FIRST])
def test_assign_models_preflight_dispatches_cheaper_cheap_tier(pool, _all_authed):
    """Adaptive preflight (_pick_agent(agents, 'cheap')) must reach the cheaper model."""
    agents = _agents_from_models(pool, budget_usd=50.0)
    cfg = AssignmentConfig(
        enabled=True,
        min_reviewers=1,
        max_reviewers=1,
        prefer_cross_provider=False,
        max_cost_per_story_usd=None,
        escalation_memory=False,
    )
    decision = assign_models(agents, cfg, complexity="LOW")
    assert decision.preflight.model == "gpt-5.4-mini"
    # LOW dev also targets the cheap tier — the enabled model is now exercised.
    assert decision.dev.model == "gpt-5.4-mini"


# ── _rerank_by_profiles: cold-start ordering ───────────────────────────


def _two_cheap_agents() -> list[AgentDef]:
    return _agents_from_models(_POOL_SONNET_FIRST, budget_usd=50.0)


def _equal_reliability_profiles(rate: float = 0.8) -> dict:
    return {
        "models": {
            "anthropic/sonnet/cli": {
                "dev": {
                    "runs": 10,
                    "success_rate": rate,
                    "by_complexity": {"small": {"runs": 10, "success_rate": rate}},
                }
            },
            "openai/gpt-5.4-mini/cli": {
                "dev": {
                    "runs": 10,
                    "success_rate": rate,
                    "by_complexity": {"small": {"runs": 10, "success_rate": rate}},
                }
            },
        }
    }


def _fresh_stamp() -> str:
    """A timestamp inside the recency window *whenever the suite runs*.

    A literal date would pass today and silently start failing the day it aged
    past ``OBSERVED_COST_TIEBREAK_RECENCY_DAYS`` — the cohort would fall back to
    the seed and the assertions below would be testing the fallback while
    claiming to test observation.
    """
    return (datetime.now(UTC) - timedelta(days=1)).isoformat()


def _observed_costs(*, sonnet: list[float], mini: list[float], effort: str = "low") -> dict:
    def _obs(values: list[float]) -> list[dict[str, object]]:
        stamp = _fresh_stamp()
        return [
            {
                "cost_usd": value,
                "started_at": stamp,
                "cost_provenance": "provider_reported",
            }
            for value in values
        ]

    return {
        "anthropic/sonnet/cli": {
            f"DEV|LOW|{effort}": {
                "role": "DEV",
                "complexity": "LOW",
                "reasoning_effort": effort,
                "observations": _obs(sonnet),
            }
        },
        "openai/gpt-5.4-mini/cli": {
            f"DEV|LOW|{effort}": {
                "role": "DEV",
                "complexity": "LOW",
                "reasoning_effort": effort,
                "observations": _obs(mini),
            }
        },
    }


def test_rerank_cold_start_orders_unobserved_by_price():
    """With no history, the cheaper unobserved model is explored first."""
    agents = _two_cheap_agents()
    profiles = {"models": {}}  # truthy, but no per-model dev history
    ranked = _rerank_by_profiles(agents, profiles, role="dev", complexity="LOW")
    assert ranked[0].model == "gpt-5.4-mini"


def test_rerank_observed_still_beats_cheaper_unobserved():
    """Cold-start price ordering never overrides real evidence: an observed model
    outranks a cheaper model that has no track record yet."""
    agents = _two_cheap_agents()
    profiles = {
        "models": {
            "claude-sonnet": {
                "dev": {
                    "runs": 10,
                    "success_rate": 0.9,
                    "by_complexity": {"small": {"runs": 10, "success_rate": 0.9}},
                }
            }
        }
    }
    ranked = _rerank_by_profiles(agents, profiles, role="dev", complexity="LOW")
    assert ranked[0].model == "sonnet"


def test_assign_models_uses_observed_cost_for_final_dev_tiebreak(_all_authed):
    agents = _two_cheap_agents()
    decision = assign_models(
        agents,
        AssignmentConfig(
            enabled=True,
            min_reviewers=1,
            max_reviewers=1,
            prefer_cross_provider=False,
            max_cost_per_story_usd=None,
            escalation_memory=False,
        ),
        complexity="LOW",
        complexity_score=2,
        model_profiles=_equal_reliability_profiles(),
        observed_costs=_observed_costs(sonnet=[1.1, 1.2, 1.0], mini=[2.8, 2.9, 3.0]),
    )
    assert decision.dev.model == "sonnet"
    assert decision.routing_decision["dev"]["cost_tiebreak"]["source"] == "observed"
    assert decision.routing_decision["dev"]["cost_tiebreak"]["observations"] == 3
    assert decision.routing_decision["dev"]["cost_tiebreak"]["cohort"] == {
        "role": "DEV",
        "complexity": "LOW",
        "reasoning_effort": "low",
    }


def test_observed_cost_never_beats_stronger_reliability(_all_authed):
    agents = _two_cheap_agents()
    profiles = _equal_reliability_profiles()
    # Sonnet is the cheaper candidate on observed cost but the weaker one on
    # reliability evidence; cost must not buy it the lead.
    for key, rate in (("anthropic/sonnet/cli", 0.7), ("openai/gpt-5.4-mini/cli", 0.9)):
        dev = profiles["models"][key]["dev"]
        dev["success_rate"] = rate
        dev["by_complexity"]["small"]["success_rate"] = rate
    decision = assign_models(
        agents,
        AssignmentConfig(
            enabled=True,
            min_reviewers=1,
            max_reviewers=1,
            prefer_cross_provider=False,
            max_cost_per_story_usd=None,
            escalation_memory=False,
        ),
        complexity="LOW",
        complexity_score=2,
        model_profiles=profiles,
        observed_costs=_observed_costs(sonnet=[0.8, 0.9, 1.0], mini=[3.0, 3.1, 2.9]),
    )
    assert decision.dev.model == "gpt-5.4-mini"


def test_observed_cost_mismatched_effort_falls_back_to_declared_seed(_all_authed):
    agents = _two_cheap_agents()
    decision = assign_models(
        agents,
        AssignmentConfig(
            enabled=True,
            min_reviewers=1,
            max_reviewers=1,
            prefer_cross_provider=False,
            max_cost_per_story_usd=None,
            escalation_memory=False,
        ),
        complexity="LOW",
        complexity_score=2,
        model_profiles=_equal_reliability_profiles(),
        observed_costs=_observed_costs(
            sonnet=[1.1, 1.2, 1.0], mini=[2.8, 2.9, 3.0], effort="high"
        ),
    )
    assert decision.dev.model == "gpt-5.4-mini"
    assert decision.routing_decision["dev"]["cost_tiebreak"]["source"] == "seed"


def test_observed_cost_below_floor_and_stale_fall_back_to_seed():
    below_floor = _observed_costs(sonnet=[1.0, 1.1], mini=[2.0, 2.1])
    signal = get_observed_cost_tiebreak_signal(
        below_floor,
        "anthropic/sonnet/cli",
        role="dev",
        complexity="LOW",
        reasoning_effort="low",
        seed=15.0,
    )
    assert signal["source"] == "seed"
    assert signal["reason"] == "below_sample_floor"

    stale_costs = _observed_costs(sonnet=[1.0, 1.1, 1.2], mini=[2.0, 2.1, 2.2])
    stale_costs["anthropic/sonnet/cli"]["DEV|LOW|low"]["observations"] = [
        {
            "cost_usd": 1.0,
            "started_at": "2026-06-01T12:00:00+00:00",
            "cost_provenance": "provider_reported",
        },
        {
            "cost_usd": 1.1,
            "started_at": "2026-06-01T12:00:00+00:00",
            "cost_provenance": "provider_reported",
        },
        {
            "cost_usd": 1.2,
            "started_at": "2026-06-01T12:00:00+00:00",
            "cost_provenance": "provider_reported",
        },
    ]
    stale = get_observed_cost_tiebreak_signal(
        stale_costs,
        "anthropic/sonnet/cli",
        role="dev",
        complexity="LOW",
        reasoning_effort="low",
        seed=15.0,
        now=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
    )
    assert stale["source"] == "seed"
    assert stale["reason"] == "stale"


# ── derive_roles (load-time) ───────────────────────────────────────────


@pytest.mark.parametrize("pool", [_POOL_SONNET_FIRST, _POOL_MINI_FIRST])
def test_derive_roles_dev_picks_cheaper_cheap_tier(pool):
    assignment = derive_roles(pool, complexity="LOW")
    assert assignment.dev.ref.model == "gpt-5.4-mini"


# ── _auto_assign_models (load-time, static) ────────────────────────────


@pytest.mark.parametrize("pool", [_POOL_SONNET_FIRST, _POOL_MINI_FIRST])
def test_auto_assign_dev_picks_cheaper_cheap_tier(pool):
    dev_profile, _preflight, _pool, _synth = _auto_assign_models(pool, budget_usd=50.0)
    assert dev_profile.model == "gpt-5.4-mini"


# ── _build_pool_entries (coordinator preflight) ────────────────────────


@pytest.mark.parametrize("pool", [_POOL_SONNET_FIRST, _POOL_MINI_FIRST])
def test_build_pool_entries_orders_cheap_tier_by_price(pool):
    entries = _build_pool_entries(pool)
    cheap_keys = [key for rank, key, _ in entries if rank == 1]
    assert cheap_keys[0] == "openai/gpt-5.4-mini/cli"


# ── Seam: complexity-adaptation config propagation ─────────────────────


def _seam_config(tmp_path: Path, models: list[str]) -> ForgeConfig:
    """Simple-mode ForgeConfig whose dev/preflight default to sonnet but whose
    pool includes the cheaper cheap-tier model."""
    dev = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=6.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    mini = ModelProfile(
        name="openai-gpt-5.4-mini",
        cli=None,
        provider="openai",
        model="gpt-5.4-mini",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    plan = PlanConfig.of(cli="claude", model="sonnet", budget_usd=0.5, timeout=600)
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=dev,
        preflight_profile=dev,
        review_pool=[mini],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        models=models,
        plan=plan,
        plan_model_is_default=True,
        dev_profile_is_default=True,
        review_pool_is_default=True,
        model_registry=dict(AGENT_REGISTRY),
        model_registry_sources={key: "builtin" for key in AGENT_REGISTRY},
    )


@pytest.mark.parametrize("pool", [_POOL_SONNET_FIRST, _POOL_MINI_FIRST])
def test_complexity_adaptation_seam_routes_low_dev_to_cheaper_model(tmp_path, pool):
    """Across the coordinator config-propagation boundary, a LOW story reroutes the
    default dev model from sonnet to the cheaper enabled cheap-tier model."""
    config = _seam_config(tmp_path, pool)
    adapted = _apply_complexity_adaptation(config, "small")
    assert adapted.dev_profile.model == "gpt-5.4-mini"
