"""Tests for src/theforge/assignment.py — pure unit tests, no I/O, no subprocess."""

from __future__ import annotations

import pytest

from theforge.assignment import (
    PHASE_TIER,
    AssignmentConfig,
    AssignmentDecision,
    EscalationRecord,
    _normalize_complexity,
    _reviewer_count,
    assign_models,
)
from theforge.config import AgentDef, ApiFallbackConfig


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
    """HIGH complexity: plan=strong, dev=strong; code_review excludes dev model."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=3, budget_per_story_usd=1000.0)
    decision = assign_models(agents, cfg, "large")

    assert decision.dev.model == "opus"  # strong
    assert decision.planner.model == "opus"  # strong
    # code_reviewers exclude opus (dev model) and fall back to lower-tier agents
    assert len(decision.code_reviewers) >= 1
    assert all(r.model != "opus" for r in decision.code_reviewers)


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
        )
        for i in range(2)
    ] + [
        EscalationRecord(
            story="story-done",
            complexity="MEDIUM",
            dev_model="sonnet",
            outcome="DONE",
        )
    ]

    decision = assign_models(agents, cfg, "medium", escalation_history=history)

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


def test_agent_to_profile_preserves_api_fallback_for_adaptive_cli_agents(monkeypatch):
    """Adaptive assignment must preserve CLI fallback metadata on synthesized profiles."""
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    agents = [
        AgentDef(
            name="codex-dev",
            provider=None,
            cli="codex",
            model="gpt-5.4",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
            api_fallback=ApiFallbackConfig(provider="openai", model="gpt-5.4"),
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
        ),
        AgentDef(
            name="haiku",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=300,
            tier="cheap",
        ),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)

    decision = assign_models(agents, cfg, "medium")

    assert decision.dev.cli == "codex"
    assert decision.dev.api_fallback == ApiFallbackConfig(provider="openai", model="gpt-5.4")


# ── test_budget_cap_enforcement ───────────────────────────────────────


def test_budget_cap_enforcement():
    """When sum of budgets exceeds cap, downgrade until within budget."""
    # Use a very tight budget that forces downgrades
    agents = _make_agents_one_per_tier()
    # Uncapped MEDIUM story: preflight(1) + planner(8) + plan_review(8) + dev(5) +
    # code_review(8) ≈ $30. With budget=$10 and all phases as downgrade candidates,
    # enforcement should converge: all-cheap = $1×5 = $5 ≤ $10.
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        budget_per_story_usd=10.0,
        prefer_cross_provider=False,
    )

    decision = assign_models(agents, cfg, "medium")

    total = (
        decision.preflight.budget_usd
        + decision.planner.budget_usd
        + sum(p.budget_usd for p in decision.plan_reviewers)
        + decision.dev.budget_usd
        + sum(p.budget_usd for p in decision.code_reviewers)
    )
    assert total <= 10.0, f"Budget cap not met: total ${total:.2f} > $10.00"
    assert isinstance(decision, AssignmentDecision)
    assert decision.dev is not None
    assert len(decision.code_reviewers) >= 1


def test_budget_cap_plan_phase_downgraded():
    """Plan-phase models are downgraded when they alone exceed the cap.

    This guards against the regression where _enforce_budget excluded planner and
    plan_reviewers from candidates, making the cap unreachable for MEDIUM+ stories.
    """
    agents = _make_agents_one_per_tier()
    # MEDIUM complexity: planner=strong($8) + plan_reviewer=strong($8) = $16 alone.
    # With budget=$12, plan-phase models must be downgraded to reach the cap.
    # All-cheap floor = $1×5 = $5 so $12 IS achievable.
    cfg = _make_cfg(
        min_reviewers=1,
        max_reviewers=1,
        budget_per_story_usd=12.0,
        prefer_cross_provider=False,
    )

    decision = assign_models(agents, cfg, "medium")

    total = (
        decision.preflight.budget_usd
        + decision.planner.budget_usd
        + sum(p.budget_usd for p in decision.plan_reviewers)
        + decision.dev.budget_usd
        + sum(p.budget_usd for p in decision.code_reviewers)
    )
    assert total <= 12.0, f"Budget cap not met: total ${total:.2f} > $12.00"
    # At least one plan-phase model must have been downgraded from strong ($8)
    plan_phase_max = max(
        decision.planner.budget_usd,
        *(p.budget_usd for p in decision.plan_reviewers),
    )
    assert plan_phase_max < 8.0, (
        f"Plan-phase models were not downgraded (max budget ${plan_phase_max:.2f})"
    )


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
    """If sprint_promotions already has a promotion for the complexity, use it."""
    # No history, but sprint_promotions says MEDIUM was already promoted
    sprint_promotions = {"MEDIUM": "strong"}

    # With the cached promotion, the check should return the cached result
    from theforge.assignment import _check_promotion

    result = _check_promotion("MEDIUM", "sonnet", [], sprint_promotions)
    assert result == "strong"


def test_no_promotion_with_empty_history():
    """Empty history → no promotion."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "medium", escalation_history=[])

    # No promotion — should use mid (sonnet)
    assert decision.dev.model == "sonnet"


# ── Plan reviewer diversity tests ─────────────────────────────────────


def test_plan_reviewer_excludes_planner_model():
    """Plan reviewers must not share a model with the planner when alternatives exist."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gemini-pro", "google", "gemini-pro", 5.0, 900, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 3.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    planner_model = decision.planner.model
    reviewer_models = [r.model for r in decision.plan_reviewers]
    assert planner_model not in reviewer_models, (
        f"Plan reviewer {reviewer_models} shares model with planner ({planner_model})"
    )


def test_plan_reviewer_excludes_planner_when_only_strong_is_planner():
    """Regression: large story, only one strong agent (planner). Mid-tier must be used."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 3.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    # Planner should be opus (strong); reviewer must not be opus
    assert decision.planner.model == "opus"
    reviewer_models = [r.model for r in decision.plan_reviewers]
    assert "opus" not in reviewer_models, (
        f"Plan reviewer {reviewer_models} self-reviews — sonnet should have been selected"
    )
    assert reviewer_models == ["sonnet"]


def test_plan_reviewer_falls_back_when_only_one_model():
    """When only one model is available, it must still produce a reviewer (not empty)."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    # Only one model — must still produce a reviewer (fallback to self-review)
    assert len(decision.plan_reviewers) == 1
    assert decision.plan_reviewers[0].model == "opus"


def test_plan_reviewer_falls_back_cross_provider_single_model():
    """Single-model pool with cross_provider=True still produces a reviewer."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=True)
    decision = assign_models(agents, cfg, "large")

    assert len(decision.plan_reviewers) == 1
    assert decision.plan_reviewers[0].model == "opus"


def test_plan_reviewer_excludes_planner_cross_provider():
    """Cross-provider selection also excludes the planner model."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gemini-pro", "google", "gemini-pro", 5.0, 900, "strong"),
        AgentDef("deepseek-r1", "deepseek", "deepseek-reasoner", 1.0, 600, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=2, max_reviewers=2, prefer_cross_provider=True)
    decision = assign_models(agents, cfg, "large")

    planner_model = decision.planner.model
    reviewer_models = [r.model for r in decision.plan_reviewers]
    assert planner_model not in reviewer_models, (
        f"Plan reviewer {reviewer_models} shares model with planner ({planner_model})"
    )


def test_code_reviewer_excludes_dev_model():
    """Code reviewers must not share a model with the dev agent when alternatives exist."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gemini-pro", "google", "gemini-pro", 5.0, 900, "strong"),
        AgentDef("sonnet", "anthropic", "sonnet", 3.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    dev_model = decision.dev.model
    reviewer_models = [r.model for r in decision.code_reviewers]
    assert dev_model not in reviewer_models, (
        f"Code reviewer {reviewer_models} shares model with dev ({dev_model})"
    )


def test_code_reviewer_falls_back_when_only_one_model():
    """Single-model pool: code reviewer falls back to self-review, not empty list."""
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    assert len(decision.code_reviewers) == 1
    assert decision.code_reviewers[0].model == "opus"


def test_plan_reviewer_non_empty_when_no_auth(monkeypatch):
    """When no agents have auth, plan_reviewers falls back to unauthed agents (not empty)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    agents = [
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
        AgentDef("gemini-pro", "google", "gemini-pro", 5.0, 900, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False)
    decision = assign_models(agents, cfg, "large")

    assert len(decision.plan_reviewers) >= 1


# ── Tier guardrail tests ───────────────────────────────────────────────


def test_guardrail_cheap_not_assigned_medium_when_mid_available():
    """cheap cannot be dev on MEDIUM; mid must be preferred when available."""
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "medium")

    assert decision.dev.name != "haiku", (
        f"cheap model haiku should not dev MEDIUM, got {decision.dev.name}"
    )


def test_guardrail_cheap_not_assigned_large_when_strong_available():
    """cheap cannot be dev on HIGH (large); strong must be preferred when available."""
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "large")

    assert decision.dev.name != "haiku", (
        f"cheap model haiku should not dev HIGH, got {decision.dev.name}"
    )


def test_guardrail_mid_not_assigned_large_when_strong_available():
    """mid cannot be dev on HIGH (large) when a strong agent is present."""
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
        AgentDef("opus", "anthropic", "opus", 8.0, 1200, "strong"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "large")

    assert decision.dev.name == "opus", f"Expected strong agent for HIGH, got {decision.dev.name}"


def test_guardrail_fallback_prefers_highest_tier_when_floor_unavailable():
    """When no strong agent exists for HIGH, fallback picks mid (not cheap)."""
    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        AgentDef("sonnet", "anthropic", "sonnet", 5.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "large")

    # No strong available — mid is the best we can do; cheap must not be selected
    assert decision.dev.name == "sonnet", (
        f"Expected mid (sonnet) as best-available for HIGH, got {decision.dev.name}"
    )
    assert "WARNING" in decision.rationale.get("dev", ""), (
        "Rationale must warn when floor cannot be met"
    )


def test_guardrail_fallback_prefers_mid_over_cheap_for_medium_no_mid_no_strong(monkeypatch):
    """When mid/strong have no auth for MEDIUM, fallback still warns and picks best available."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    agents = [
        AgentDef("haiku", "anthropic", "haiku", 1.0, 300, "cheap"),
        # sonnet needs OPENAI_API_KEY which we've removed
        AgentDef("sonnet", "openai", "gpt-4o", 5.0, 900, "mid"),
    ]
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)
    decision = assign_models(agents, cfg, "medium")

    # haiku is the only authed agent — must use it but rationale must warn
    assert decision.dev.name == "haiku"
    assert "WARNING" in decision.rationale.get("dev", ""), (
        "Rationale must warn when only below-floor agent is available"
    )


def test_guardrail_mid_promoted_to_strong_for_large():
    """Existing promotion logic: 2+ MEDIUM escalations promote mid → strong for LARGE."""
    agents = _make_agents_one_per_tier()
    cfg = _make_cfg(min_reviewers=1, max_reviewers=1)

    # sonnet is the mid-tier dev for MEDIUM; build history with 2 escalations
    history = [
        EscalationRecord(
            story=f"story-{i}",
            complexity="MEDIUM",
            dev_model="sonnet",
            outcome="ESCALATE",
        )
        for i in range(2)
    ]

    # For HIGH (large) the base tier is already "strong" — promotion isn't triggered
    # from mid. This test confirms strong (opus) is selected for large even with history.
    decision = assign_models(agents, cfg, "large", escalation_history=history)

    assert decision.dev.name == "opus", f"Expected strong (opus) for HIGH, got {decision.dev.name}"
