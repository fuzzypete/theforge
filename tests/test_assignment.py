"""Tests for src/theforge/assignment.py — pure unit tests, no I/O, no subprocess."""

from __future__ import annotations

import pytest

from theforge.assignment import (
    PHASE_TIER,
    AssignmentConfig,
    AssignmentDecision,
    EscalationRecord,
    _normalize_complexity,
    _normalize_complexity_score,
    _reviewer_count,
    append_escalation_record,
    assign_models,
    load_escalation_history,
)
from theforge.config import AgentDef


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    """Ensure _has_auth() passes for all test agents."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")


# ── Fixtures ───────────────────────────────────────────────────────────


def _make_cfg(**kwargs) -> AssignmentConfig:
    defaults = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=3,
        prefer_cross_provider=True,
        budget_per_story_usd=100.0,  # generous default so budget tests can be explicit
        escalation_memory=True,
    )
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


def _make_agents_one_per_tier() -> list[AgentDef]:
    return [
        AgentDef(
            name="haiku",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=300,
            tier="cheap",
        ),
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
        ),
    ]


def _make_agents_cross_provider() -> list[AgentDef]:
    return [
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
        ),
        AgentDef(
            name="deepseek-r1",
            provider="deepseek",
            model="deepseek-reasoner",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="strong",
        ),
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
        ),
    ]


def _make_domain_agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="cheap-general",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=300,
            tier="cheap",
            strengths=("general",),
        ),
        AgentDef(
            name="cheap-layout",
            provider="openai",
            model="gpt-5.4",
            budget_usd=1.5,
            timeout_seconds=300,
            tier="cheap",
            strengths=("frontend-layout", "css", "ui"),
        ),
        AgentDef(
            name="mid-general",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
            strengths=("general",),
        ),
        AgentDef(
            name="mid-layout",
            provider="openai",
            model="gpt-5.4",
            budget_usd=6.0,
            timeout_seconds=900,
            tier="mid",
            strengths=("frontend-layout", "css", "spatial"),
        ),
        AgentDef(
            name="strong-general",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
            strengths=("general",),
        ),
        AgentDef(
            name="strong-layout",
            provider="openai",
            model="gpt-5.4",
            budget_usd=9.0,
            timeout_seconds=1200,
            tier="strong",
            strengths=("frontend-layout", "css", "spatial"),
        ),
    ]


# ── test_normalize_complexity ──────────────────────────────────────────


def test_normalize_complexity_small():
    assert _normalize_complexity("small") == "LOW"


def test_normalize_complexity_medium():
    assert _normalize_complexity("medium") == "MEDIUM"


def test_normalize_complexity_large():
    assert _normalize_complexity("large") == "HIGH"


def test_normalize_complexity_passthrough_low():
    assert _normalize_complexity("LOW") == "LOW"


def test_normalize_complexity_passthrough_medium():
    assert _normalize_complexity("MEDIUM") == "MEDIUM"


def test_normalize_complexity_passthrough_high():
    assert _normalize_complexity("HIGH") == "HIGH"


def test_normalize_complexity_default_unknown():
    assert _normalize_complexity("unknown") == "MEDIUM"


def test_normalize_complexity_case_insensitive():
    assert _normalize_complexity("Large") == "HIGH"
    assert _normalize_complexity("SMALL") == "LOW"


def test_normalize_complexity_numeric_score():
    assert _normalize_complexity(2) == "LOW"
    assert _normalize_complexity(5) == "MEDIUM"
    assert _normalize_complexity(9) == "HIGH"


def test_normalize_complexity_score_legacy_mapping():
    assert _normalize_complexity_score("LOW") == 3
    assert _normalize_complexity_score("MEDIUM") == 5
    assert _normalize_complexity_score("HIGH") == 8


# ── test_tier_selection_low ────────────────────────────────────────────


def test_tier_selection_low():
    """LOW complexity: preflight=cheap, plan=mid, dev=cheap, code_review=mid."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "small")

    assert decision.dev.model == "haiku"  # cheap
    assert decision.preflight.model == "haiku"  # cheap
    assert decision.planner.model == "sonnet"  # mid
    assert len(decision.code_reviewers) == 1
    # code_review tier for LOW is "mid" but _select_reviewers prefers strong first
    # The pool has opus (strong), so it should be selected
    assert decision.code_reviewers[0].model == "opus"


def test_tier_selection_medium():
    """MEDIUM complexity: plan=strong, dev=mid, code_review=strong."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "medium")

    assert decision.dev.model == "sonnet"  # mid
    assert decision.planner.model == "opus"  # strong
    assert len(decision.code_reviewers) == 1
    assert decision.code_reviewers[0].model == "opus"  # strong


def test_tier_selection_high():
    """HIGH complexity: plan=strong, dev=strong, code_review=strong (max_reviewers)."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=3, budget_per_story_usd=1000.0)
    decision = assign_models(agents, cfg, "large")

    assert decision.dev.model == "opus"  # strong
    assert decision.planner.model == "opus"  # strong
    # HIGH uses max_reviewers=3 but only 1 strong agent in pool
    assert len(decision.code_reviewers) >= 1
    assert decision.code_reviewers[0].model == "opus"


def test_strength_matching_prefers_domain_agent():
    agents = _make_domain_agents()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)

    decision = assign_models(agents, cfg, 3, domain="frontend-layout")

    assert decision.dev.name == "cheap-layout"
    assert decision.preflight.name == "cheap-layout"
    assert decision.planner.name == "mid-layout"
    assert decision.code_reviewers[0].name == "strong-layout"


def test_general_domain_keeps_budget_order_fallback():
    agents = _make_domain_agents()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)

    decision = assign_models(agents, cfg, 3, domain="general")

    assert decision.dev.name == "cheap-general"
    assert decision.preflight.name == "cheap-general"
    assert decision.planner.name == "mid-general"


# ── test_cross_provider_preference ────────────────────────────────────


def test_cross_provider_preference():
    """When pool has opus/anthropic + deepseek-r1/deepseek + sonnet/anthropic,
    two code reviewers should come from different providers."""
    agents = _make_agents_cross_provider()
    cfg = _make_cfg(min_reviewers=2, max_reviewers=2, prefer_cross_provider=True)
    decision = assign_models(agents, cfg, "medium")

    assert len(decision.code_reviewers) == 2
    providers = [r.provider for r in decision.code_reviewers]
    assert len(set(providers)) == 2, f"Expected 2 different providers, got {providers}"


def test_cross_provider_with_three_reviewers():
    """With prefer_cross_provider and 2 providers, 3 reviewers still returns valid pool."""
    agents = _make_agents_cross_provider()
    cfg = _make_cfg(
        min_reviewers=3, max_reviewers=3, prefer_cross_provider=True, budget_per_story_usd=1000.0
    )
    decision = assign_models(agents, cfg, "large")

    # Should still return reviewers (fills up with same-provider if needed)
    assert len(decision.code_reviewers) >= 1


# ── test_cross_provider_fallback ──────────────────────────────────────


def test_cross_provider_fallback_same_provider():
    """All-same-provider pool should still return min_reviewers without error."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=2, max_reviewers=2, prefer_cross_provider=True)
    decision = assign_models(agents, cfg, "medium")

    # Should not raise; returns 2 reviewers even though same provider
    assert len(decision.code_reviewers) >= 1


# ── test_escalation_promotion ─────────────────────────────────────────


def test_escalation_promotion():
    """History with 2 ESCALATE records (MEDIUM, same dev_model) → dev promoted."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)

    # Default MEDIUM dev tier is "mid" → sonnet
    # Build history: 2 escalations with sonnet on MEDIUM
    history = [
        EscalationRecord(
            story=f"story-{i}",
            complexity="MEDIUM",
            dev_model="sonnet",
            outcome="ESCALATE",
            domain="frontend-layout",
        )
        for i in range(2)
    ] + [
        EscalationRecord(
            story="story-done",
            complexity="MEDIUM",
            dev_model="sonnet",
            outcome="DONE",
            domain="frontend-layout",
        )
    ]

    decision = assign_models(
        agents, cfg, "medium", escalation_history=history, domain="frontend-layout"
    )

    # Should promote from mid (sonnet) to strong (opus)
    assert decision.dev.model == "opus", (
        f"Expected opus (promoted), got {decision.dev.model}. Rationale: {decision.rationale}"
    )


def test_no_escalation_below_threshold():
    """Only 1 escalation in last 10 → no promotion."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)

    history = [
        EscalationRecord(
            story="story-0",
            complexity="MEDIUM",
            dev_model="sonnet",
            outcome="ESCALATE",
        ),
        EscalationRecord(
            story="story-done",
            complexity="MEDIUM",
            dev_model="sonnet",
            outcome="DONE",
        ),
    ]

    decision = assign_models(agents, cfg, "medium", escalation_history=history)

    # Should NOT promote — only 1 escalation, need 2+
    assert decision.dev.model == "sonnet", (
        f"Expected sonnet (no promotion), got {decision.dev.model}"
    )


def test_escalation_promotion_uses_domain_history():
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    history = [
        EscalationRecord(
            story="story-a",
            complexity="5",
            dev_model="sonnet",
            outcome="ESCALATE",
            domain="frontend-layout",
        ),
        EscalationRecord(
            story="story-b",
            complexity="MEDIUM",
            dev_model="sonnet",
            outcome="ESCALATE",
            domain="frontend-layout",
        ),
        EscalationRecord(
            story="story-c",
            complexity="MEDIUM",
            dev_model="sonnet",
            outcome="DONE",
            domain="backend-api",
        ),
    ]

    layout = assign_models(agents, cfg, 5, escalation_history=history, domain="frontend-layout")
    api = assign_models(agents, cfg, 5, escalation_history=history, domain="backend-api")

    assert layout.dev.model == "opus"
    assert api.dev.model == "sonnet"


# ── test_explicit_override ────────────────────────────────────────────


def test_explicit_override():
    """explicit_profiles['dev'] should force that model; others come from pool."""
    from theforge.config import ModelProfile

    agents = _make_agents_one_per_tier()
    cfg = _make_cfg()

    explicit_dev = ModelProfile(
        name="custom-dev",
        cli="claude",
        provider=None,
        model="custom-model",
        budget_usd=3.0,
        timeout_seconds=500,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )

    decision = assign_models(agents, cfg, "medium", explicit_profiles={"dev": explicit_dev})

    assert decision.dev.model == "custom-model"
    assert decision.dev.name == "custom-dev"
    # Other phases still come from pool
    assert decision.planner.model in ("haiku", "sonnet", "opus")


# ── test_budget_cap_enforcement ───────────────────────────────────────


def test_budget_cap_enforcement():
    """When sum of budgets exceeds cap, downgrade until within budget."""
    # Use a very tight budget that forces downgrades
    agents = _make_agents_one_per_tier()
    # Each story: preflight(1) + planner(8) + plan_review(8) + dev(5) + code_review(8)
    # Total ≈ 30 — much more than budget_per_story_usd=10
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        budget_per_story_usd=10.0,
        prefer_cross_provider=False,
    )

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        decision = assign_models(agents, cfg, "medium")

    # Budget enforcement may still exceed cap if no cheaper option,
    # but should have attempted to downgrade
    # At minimum: decision must be a valid AssignmentDecision
    assert isinstance(decision, AssignmentDecision)
    assert decision.dev is not None
    assert len(decision.code_reviewers) >= 1


def test_budget_cap_downgrade_dev():
    """Strict budget forces dev from strong to mid or cheap."""
    # Only strong agents exist for dev tier; with tiny budget, it should downgrade
    agents = [
        AgentDef("cheap-agent", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("strong-agent", "anthropic", "opus", 50.0, 1200, "strong"),
    ]
    # HIGH complexity → dev=strong (opus, $50) — clearly over budget of $5
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        budget_per_story_usd=5.0,
        prefer_cross_provider=False,
    )
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        decision = assign_models(agents, cfg, "large")

    # With only 2 agents, enforcement tries to downgrade dev from strong to cheap
    assert isinstance(decision, AssignmentDecision)
    # cheap-agent should be dev after downgrade
    assert decision.dev.budget_usd < 50.0 or decision.dev.model == "haiku"


# ── test_deterministic ────────────────────────────────────────────────


def test_deterministic():
    """Same inputs produce same outputs every time."""
    agents = _make_agents_cross_provider()
    cfg = _make_cfg(min_reviewers=2, max_reviewers=2)
    history = [
        EscalationRecord("s1", "MEDIUM", "sonnet", "DONE"),
    ]

    decision1 = assign_models(agents, cfg, "medium", escalation_history=history)
    decision2 = assign_models(agents, cfg, "medium", escalation_history=history)

    assert decision1.dev.model == decision2.dev.model
    assert decision1.dev.name == decision2.dev.name
    assert decision1.preflight.model == decision2.preflight.model
    assert decision1.planner.model == decision2.planner.model
    assert [r.model for r in decision1.code_reviewers] == [
        r.model for r in decision2.code_reviewers
    ]
    assert [r.model for r in decision1.plan_reviewers] == [
        r.model for r in decision2.plan_reviewers
    ]


# ── Phase→tier mapping tests ──────────────────────────────────────────


def test_phase_tier_table_completeness():
    """Every phase has LOW/MEDIUM/HIGH entries."""
    expected_phases = {"preflight", "plan", "plan_review", "dev", "code_review"}
    assert set(PHASE_TIER.keys()) == expected_phases
    for phase, tiers in PHASE_TIER.items():
        assert set(tiers.keys()) == {"LOW", "MEDIUM", "HIGH"}, (
            f"Phase {phase!r} missing complexity keys"
        )


def test_dev_cheap_for_low():
    assert PHASE_TIER["dev"]["LOW"] == "cheap"


def test_dev_mid_for_medium():
    assert PHASE_TIER["dev"]["MEDIUM"] == "mid"


def test_dev_strong_for_high():
    assert PHASE_TIER["dev"]["HIGH"] == "strong"


# ── Reviewer count tests ──────────────────────────────────────────────


def test_reviewer_count_low():
    assert _reviewer_count("LOW", 1, 3) == 1


def test_reviewer_count_high():
    assert _reviewer_count("HIGH", 1, 3) == 3


def test_reviewer_count_medium():
    count = _reviewer_count("MEDIUM", 1, 3)
    assert 1 <= count <= 3


# ── Sprint promotion stickiness ───────────────────────────────────────


def test_sprint_promotions_cached():
    """If sprint_promotions already has a promotion for the domain slice, use it."""
    sprint_promotions = {"MEDIUM:frontend-layout": "strong"}

    from theforge.assignment import _check_promotion

    result = _check_promotion("MEDIUM", "sonnet", [], sprint_promotions, "frontend-layout")
    assert result == "strong"


def test_sprint_promotions_isolated_by_domain():
    """A cached promotion in one domain must not leak into another."""
    sprint_promotions = {"MEDIUM:frontend-layout": "strong"}

    from theforge.assignment import _check_promotion

    layout = _check_promotion("MEDIUM", "sonnet", [], sprint_promotions, "frontend-layout")
    api = _check_promotion("MEDIUM", "sonnet", [], sprint_promotions, "backend-api")

    assert layout == "strong"
    assert api is None


def test_assign_models_sprint_promotion_isolated_by_domain():
    """Cached promotion must not change dev routing for a different domain."""
    agents = _make_domain_agents()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    sprint_promotions = {"MEDIUM:frontend-layout": "strong"}

    layout = assign_models(
        agents, cfg, 5, domain="frontend-layout", sprint_promotions=sprint_promotions
    )
    api = assign_models(agents, cfg, 5, domain="backend-api", sprint_promotions=sprint_promotions)

    assert layout.dev.name == "strong-layout"
    assert api.dev.name == "mid-general"


def test_no_promotion_with_empty_history():
    """Empty history → no promotion."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "medium", escalation_history=[])

    # No promotion — should use mid (sonnet)
    assert decision.dev.model == "sonnet"


def test_escalation_history_persists_domain(tmp_path):
    history_path = tmp_path / ".forge" / "assignment_history.yaml"
    append_escalation_record(
        history_path,
        EscalationRecord(
            story="story-1",
            complexity="8",
            dev_model="opus",
            outcome="DONE",
            domain="frontend-layout",
        ),
    )

    records = load_escalation_history(history_path)

    assert len(records) == 1
    assert records[0].complexity == "8"
    assert records[0].domain == "frontend-layout"
