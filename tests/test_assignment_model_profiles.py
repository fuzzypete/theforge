"""assign_models honors model_profiles when ranking same-tier dev candidates."""

from __future__ import annotations

import pytest

from theforge.assignment import AssignmentConfig, assign_models
from theforge.config import AgentDef


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")


def _agents_two_mid() -> list[AgentDef]:
    # Both mid-tier, same tier. Without profiles, cheapest budget wins (budget=1).
    return [
        AgentDef(
            name="cheap-mid",
            provider="deepseek",
            model="ds",
            budget_usd=1.0,
            timeout_seconds=300,
            tier="mid",
        ),
        AgentDef(
            name="proven-mid",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=600,
            tier="mid",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=900,
            tier="strong",
        ),
    ]


def _cfg() -> AssignmentConfig:
    return AssignmentConfig(
        enabled=True,
        min_reviewers=1,
        max_reviewers=1,
        prefer_cross_provider=False,
        budget_per_story_usd=100.0,
        escalation_memory=False,
    )


def test_dev_selection_without_profiles_uses_cheapest():
    decision = assign_models(_agents_two_mid(), _cfg(), complexity="MEDIUM")
    assert decision.dev.name == "cheap-mid"


def test_dev_selection_prefers_high_success_rate_when_observed():
    profiles = {
        "models": {
            "proven-mid": {
                "dev": {
                    "runs": 10,
                    "success_rate": 0.9,
                    "by_complexity": {"medium": {"runs": 10, "success_rate": 0.9}},
                }
            },
            "cheap-mid": {
                "dev": {
                    "runs": 10,
                    "success_rate": 0.2,
                    "by_complexity": {"medium": {"runs": 10, "success_rate": 0.2}},
                }
            },
        }
    }
    decision = assign_models(
        _agents_two_mid(),
        _cfg(),
        complexity="MEDIUM",
        model_profiles=profiles,
    )
    assert decision.dev.name == "proven-mid"
    # Rationale annotated with the observed rate
    assert "0.90" in decision.rationale["dev"]


def test_dev_selection_ignores_profiles_below_min_runs():
    profiles = {
        "models": {
            "proven-mid": {
                "dev": {
                    "runs": 1,
                    "success_rate": 1.0,
                    "by_complexity": {"medium": {"runs": 1, "success_rate": 1.0}},
                }
            },
        }
    }
    decision = assign_models(
        _agents_two_mid(),
        _cfg(),
        complexity="MEDIUM",
        model_profiles=profiles,
    )
    # Insufficient data → fall back to budget-cheapest default
    assert decision.dev.name == "cheap-mid"
