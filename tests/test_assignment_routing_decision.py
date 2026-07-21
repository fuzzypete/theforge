"""Routing explainability block (#1391) — focused assignment-level tests.

Covers the reason vocabulary, selected-candidate reason, profile-signal shape,
and the no-extra-profile-lookup contract for the routing_decision block that
assign_models attaches to its AssignmentDecision.
"""

from __future__ import annotations

import pytest

from theforge.assignment import (
    EXCLUSION_REASONS,
    REASON_ANTI_SELF_REVIEW,
    REASON_AUTH_MISSING,
    REASON_NONE,
    REASON_TRANSPORT_UNAVAILABLE,
    AssignmentConfig,
    _build_routing_decision,
    assign_models,
)
from theforge.config import AgentDef


@pytest.fixture()
def _keys_except_deepseek(monkeypatch):
    """Anthropic/OpenAI authed; DeepSeek key absent → auth_missing exclusion."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def _cfg(**kwargs) -> AssignmentConfig:
    defaults = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=3,
        prefer_cross_provider=True,
        max_cost_per_story_usd=100.0,
        escalation_memory=True,
    )
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


def _agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=3.0,
            timeout_seconds=600,
            tier="mid",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="gpt",
            provider="openai",
            model="gpt-5.4",
            budget_usd=8.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="deepseek",
            provider="deepseek",
            model="deepseek-reasoner",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="strong",
        ),
    ]


def _all_reasons(block: dict) -> list[str]:
    reasons: list[str] = []
    for role_block in block.values():
        if not isinstance(role_block, dict):
            continue
        for entry in role_block.get("candidate_pool", []):
            reasons.append(entry["reason"])
    return reasons


def test_exclusion_reasons_are_canonical(_keys_except_deepseek):
    decision = assign_models(_agents(), _cfg(), complexity="HIGH", complexity_score=9)
    block = decision.routing_decision
    assert block, "routing_decision block must be populated"
    for reason in _all_reasons(block):
        assert reason in EXCLUSION_REASONS, f"non-canonical reason: {reason!r}"


def test_selected_candidates_use_reason_none(_keys_except_deepseek):
    decision = assign_models(_agents(), _cfg(), complexity="HIGH", complexity_score=9)
    block = decision.routing_decision
    # Every included candidate carries the canonical "none" reason.
    for role_block in block.values():
        if not isinstance(role_block, dict):
            continue
        for entry in role_block.get("candidate_pool", []):
            if entry["included"]:
                assert entry["reason"] == REASON_NONE


def test_anti_self_review_and_auth_missing_reasons_are_attributed(_keys_except_deepseek):
    """The required reconstructable case: one anti_self_review, one auth_missing."""
    decision = assign_models(_agents(), _cfg(), complexity="HIGH", complexity_score=9)
    pool = {e["name"]: e for e in decision.routing_decision["code_review"]["candidate_pool"]}

    dev_model = decision.dev.model
    # The reviewer sharing the dev's model is excluded for anti-self-review.
    self_named = [
        n for n, e in pool.items() if not e["included"] and e["reason"] == REASON_ANTI_SELF_REVIEW
    ]
    assert self_named, "expected an anti_self_review exclusion in code_review"
    for name in self_named:
        agent = next(a for a in _agents() if a.name == name)
        assert agent.model == dev_model

    # DeepSeek has no key → auth_missing (a key-absence, not a transport gap).
    assert pool["deepseek"]["included"] is False
    assert pool["deepseek"]["reason"] == REASON_AUTH_MISSING


def test_profile_signal_entries_have_raw_weighted_runs_floor(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    profiles = {
        "models": {
            "opus": {
                "dev": {
                    "runs": 12,
                    "success_rate": 0.75,
                    "by_complexity": {"large": {"runs": 12, "success_rate": 0.75}},
                }
            },
            "gpt": {
                "dev": {
                    "runs": 1,  # below the min_runs floor
                    "success_rate": 1.0,
                    "by_complexity": {"large": {"runs": 1, "success_rate": 1.0}},
                }
            },
        }
    }
    decision = assign_models(
        _agents(),
        _cfg(),
        complexity="HIGH",
        complexity_score=9,
        model_profiles=profiles,
    )
    dev_pool = {e["name"]: e for e in decision.routing_decision["dev"]["candidate_pool"]}
    opus_sig = dev_pool["opus"]["signals"]["success_rate"]
    assert set(opus_sig) == {"raw", "weighted", "runs", "floor", "rate"}
    assert opus_sig["raw"] == 0.75
    assert opus_sig["weighted"] == 0.75  # v1: weighting not yet applied
    assert opus_sig["runs"] == 12
    assert opus_sig["floor"] == "pass"

    gpt_sig = dev_pool["gpt"]["signals"]["success_rate"]
    assert gpt_sig["runs"] == 1
    assert gpt_sig["floor"] == "fail"  # cleared vs failed the sample floor is visible


def test_block_building_does_not_re_read_profiles(monkeypatch):
    """_build_routing_decision consumes pre-collected signals; it never looks up
    profiles again — the block adds no profile re-reads (AC: bounded overhead)."""
    import theforge.model_profiles as mp

    calls = {"n": 0}
    real = mp.get_dev_signal

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(mp, "get_dev_signal", _counting)

    agents = _agents()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    profiles = {
        "models": {
            "opus": {
                "dev": {
                    "runs": 5,
                    "success_rate": 0.6,
                    "by_complexity": {"large": {"runs": 5, "success_rate": 0.6}},
                }
            },
        }
    }
    decision = assign_models(
        agents, _cfg(), complexity="HIGH", complexity_score=9, model_profiles=profiles
    )
    calls_during_assign = calls["n"]

    # Re-building the block from the already-collected signals must not trigger
    # any further profile lookups.
    dev_signals = {
        e["name"]: e["signals"]["success_rate"]
        for e in decision.routing_decision["dev"]["candidate_pool"]
        if "signals" in e
    }
    calls["n"] = 0
    rebuilt = _build_routing_decision(
        decision,
        agents,
        origin="preflight",
        score=9,
        dev_base_tier="strong",
        dev_effective_tier="strong",
        preflight_tier="cheap",
        planner_tier="strong",
        dev_signals=dev_signals,
        promotion_block={
            "fired": False,
            "matching_records": 0,
            "escalations": 0,
            "outcome": "no_promotion",
        },
        planner_model=decision.planner.model,
        dev_model=decision.dev.model,
        explicit_roles=set(),
        secrets=None,
    )
    assert calls["n"] == 0, "block building must not perform profile lookups"
    assert calls_during_assign > 0  # sanity: routing did consult profiles
    # And the rebuilt block surfaces the pre-collected signal object verbatim.
    opus_entry = next(e for e in rebuilt["dev"]["candidate_pool"] if e["name"] == "opus")
    assert opus_entry["signals"]["success_rate"] is dev_signals["opus"]


def _health_agents() -> list[AgentDef]:
    """Strong pool where dev picks opus (cheapest strong) and deepseek is a
    reviewer candidate we can mark unhealthy without it becoming the dev pick."""
    return [
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=2.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="gpt",
            provider="openai",
            model="gpt-5.4",
            budget_usd=8.0,
            timeout_seconds=900,
            tier="strong",
        ),
        AgentDef(
            name="deepseek",
            provider="deepseek",
            model="deepseek-reasoner",
            budget_usd=9.0,
            timeout_seconds=600,
            tier="strong",
        ),
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=3.0,
            timeout_seconds=600,
            tier="mid",
        ),
    ]


def test_health_deprioritized_reviewer_is_excluded_not_falsely_included(monkeypatch):
    """A reviewer candidate dropped by provider-health demotion is recorded
    excluded (transport_unavailable / health_deprioritized) and absent from
    final.models — never included=true with no seat (P1-B / ADR-0006 clause 5)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    decision = assign_models(
        _health_agents(),
        _cfg(min_reviewers=2, max_reviewers=2),
        complexity="HIGH",
        complexity_score=9,
        unhealthy_models={"deepseek"},
    )
    assert decision.dev.name == "opus"  # deepseek's high budget keeps it off dev
    cr = decision.routing_decision["code_review"]
    pool = {e["name"]: e for e in cr["candidate_pool"]}

    # deepseek: unhealthy with a healthy alternative → deprioritized, not seated.
    assert pool["deepseek"]["included"] is False
    assert pool["deepseek"]["reason"] == REASON_TRANSPORT_UNAVAILABLE
    assert pool["deepseek"]["detail"] == "health_deprioritized"
    assert "deepseek-reasoner" not in cr["final"]["models"]

    # The demotion path outcome is recorded per reviewer role, with attribution.
    demotion = cr["demotion_check"]
    assert demotion["mechanism"] == "provider_health"
    assert demotion["fired"] is True
    assert "deepseek" in demotion["deprioritized"]

    # Every included reviewer candidate still carries a canonical reason only.
    for entry in cr["candidate_pool"]:
        assert entry["reason"] in EXCLUSION_REASONS


def test_health_demotion_records_checked_but_not_fired(monkeypatch):
    """With no unhealthy models, the reviewer demotion_check still records a
    checked-but-didn't-fire outcome (AC clause 5), not silence."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    decision = assign_models(
        _health_agents(),
        _cfg(min_reviewers=2, max_reviewers=2),
        complexity="HIGH",
        complexity_score=9,
    )
    demotion = decision.routing_decision["code_review"]["demotion_check"]
    assert demotion["fired"] is False
    assert demotion["deprioritized"] == []
    assert demotion["reason"]  # a concrete reason, not empty
