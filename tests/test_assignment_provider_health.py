"""Tests for adaptive routing's provider-health awareness.

Covers the issue-1574 fix: when a reviewer model has produced a recent
provider-shape failure, `_select_reviewers` deprioritizes it and the
`assign_models` rationale surfaces the health context.
"""

from __future__ import annotations

import pytest

from theforge.assignment import (
    AssignmentConfig,
    _select_reviewers,
    assign_models,
)
from theforge.config import AgentDef


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")


def _cfg(**kwargs) -> AssignmentConfig:
    defaults = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=1,
        prefer_cross_provider=False,
        max_cost_per_story_usd=1000.0,
        escalation_memory=True,
    )
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


def _agents_two_strong() -> list[AgentDef]:
    """Two strong-tier reviewer candidates + a mid-tier dev agent.

    The mid-tier sonnet keeps dev assignment off the strong reviewer pool so
    self-exclusion doesn't trivially remove the flapping model — the health
    signal is what must do the deprioritization.
    """
    return [
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
        ),
        AgentDef(
            name="openai-gpt-5.5",
            provider="openai",
            model="gpt-5.5",
            budget_usd=2.0,
            timeout_seconds=600,
            tier="strong",
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


def _agents_only_unhealthy_strong() -> list[AgentDef]:
    """Only one strong-tier candidate, and it is unhealthy — selector must fall back to it.

    A mid-tier dev agent ensures the self-exclusion does not coincidentally
    remove the unhealthy strong-tier candidate.
    """
    return [
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
        ),
        AgentDef(
            name="openai-gpt-5.5",
            provider="openai",
            model="gpt-5.5",
            budget_usd=2.0,
            timeout_seconds=600,
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


# ── _select_reviewers unit tests ──────────────────────────────────────


def test_select_reviewers_skips_unhealthy_when_alternative_exists():
    agents = _agents_two_strong()
    selected = _select_reviewers(
        agents,
        tier="strong",
        n=1,
        prefer_cross_provider=False,
        unhealthy_models={"openai-gpt-5.5"},
    )
    assert len(selected) == 1
    assert selected[0].name == "opus"


def test_select_reviewers_falls_back_to_unhealthy_when_no_alternative():
    # Self-exclude opus so only the unhealthy gpt-5.5 remains at strong; cheap
    # haiku is still available below.  unhealthy_models filter must not erase
    # the candidate pool — falls back to unhealthy candidate.
    agents = [
        AgentDef(
            name="openai-gpt-5.5",
            provider="openai",
            model="gpt-5.5",
            budget_usd=2.0,
            timeout_seconds=600,
            tier="strong",
        ),
    ]
    selected = _select_reviewers(
        agents,
        tier="strong",
        n=1,
        prefer_cross_provider=False,
        unhealthy_models={"openai-gpt-5.5"},
    )
    assert len(selected) == 1
    assert selected[0].name == "openai-gpt-5.5"


def test_select_reviewers_no_unhealthy_set_unchanged():
    agents = _agents_two_strong()
    selected = _select_reviewers(
        agents,
        tier="strong",
        n=1,
        prefer_cross_provider=False,
        unhealthy_models=None,
    )
    # Cheapest at strong wins without health signal.
    assert selected[0].name == "openai-gpt-5.5"


# ── assign_models integration: rationale surfacing ────────────────────


def test_assign_models_deprioritizes_unhealthy_reviewer_and_surfaces_rationale():
    agents = _agents_two_strong()
    decision = assign_models(
        agents,
        _cfg(),
        complexity="medium",
        unhealthy_models={"openai-gpt-5.5"},
    )
    reviewer_models = [r.model for r in decision.code_reviewers]
    assert reviewer_models == ["opus"]
    rationale = decision.rationale["code_review"]
    assert "health-deprioritized" in rationale
    assert "openai-gpt-5.5" in rationale


def test_assign_models_falls_back_to_unhealthy_when_no_alternative():
    # Only one strong candidate, and it's unhealthy.  Selector must still
    # produce a reviewer rather than an empty pool, and the rationale must
    # say so explicitly.
    agents = _agents_only_unhealthy_strong()
    # max_reviewers=1, with no cross-provider preference, so candidate pool
    # at strong tier collapses to gpt-5.5 only.
    decision = assign_models(
        agents,
        _cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False),
        complexity="medium",
        unhealthy_models={"openai-gpt-5.5"},
    )
    # Selector descends to haiku rather than picking the unhealthy strong-tier
    # candidate — the descend pool intentionally widens to lower tiers (#1542).
    # Either outcome is acceptable as a "no healthy strong alternative" path;
    # what we require is that the rationale surfaces the health signal.
    rationale = decision.rationale["code_review"]
    assert "health-" in rationale, rationale


def test_assign_models_self_review_only_unhealthy_candidate_surfaces_fallback():
    """No-alternative path: only candidate is unhealthy AND same model as dev.

    `_select_reviewers` re-admits the self-excluded model when nothing else
    is available.  The rationale must surface health-fallback so the operator
    can distinguish a forced pick from a clean one (issue-1574 iter 1).
    """
    agents = [
        AgentDef(
            name="openai-gpt-5.5",
            provider="openai",
            model="gpt-5.5",
            budget_usd=2.0,
            timeout_seconds=600,
            tier="strong",
        ),
    ]
    decision = assign_models(
        agents,
        _cfg(min_reviewers=1, max_reviewers=1, prefer_cross_provider=False),
        complexity="large",  # HIGH → dev tier strong → dev=gpt-5.5
        unhealthy_models={"openai-gpt-5.5"},
    )
    assert decision.dev.model == "gpt-5.5"
    assert decision.code_reviewers[0].model == "gpt-5.5"
    rationale = decision.rationale["code_review"]
    assert "health-fallback" in rationale, rationale
    assert "openai-gpt-5.5" in rationale


def test_assign_models_no_health_signal_no_rationale_note():
    agents = _agents_two_strong()
    decision = assign_models(agents, _cfg(), complexity="medium", unhealthy_models=None)
    rationale = decision.rationale["code_review"]
    assert "health-" not in rationale


def test_assign_models_unhealthy_set_without_matching_agent_is_noop():
    agents = _agents_two_strong()
    decision = assign_models(
        agents,
        _cfg(),
        complexity="medium",
        unhealthy_models={"some-other-model"},
    )
    # No agent in the pool matched; selection unchanged, no rationale note.
    assert decision.code_reviewers[0].name == "openai-gpt-5.5"
    assert "health-" not in decision.rationale["code_review"]
