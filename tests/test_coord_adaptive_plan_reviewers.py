"""Seam tests: adaptive plan_reviewers are applied to config.plan_agent_review.

Issue: assign_models() populates plan_reviewers but _apply_preflight_config never
wrote them into config.plan_agent_review — so plan_flow's `if enabled` gate always
saw False and silently skipped plan review regardless of what the agents pool said.

These tests verify the config-propagation seam (preflight → plan_flow) and the
end-to-end integration (plan agent review actually runs when adaptive assignment
provides plan_reviewers).
"""

from __future__ import annotations

from dataclasses import replace as _dc_replace
from pathlib import Path

import pytest

from theforge.assignment import AssignmentConfig
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    AgentDef,
    ForgeConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.preflight import _apply_preflight_config
from theforge.coordinator.state import CoordinatorState

# ── Test fixtures ─────────────────────────────────────────────────────


def _make_agents() -> list[AgentDef]:
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


def _make_assignment_config(**kwargs) -> AssignmentConfig:
    defaults = dict(
        enabled=True,
        min_reviewers=1,
        max_reviewers=2,
        prefer_cross_provider=False,
        budget_per_story_usd=100.0,
        escalation_memory=True,
    )
    defaults.update(kwargs)
    return AssignmentConfig(**defaults)


def _make_config_with_assignment(tmp_path: Path) -> ForgeConfig:
    """Config with assignment.enabled and agents pool — no models list."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        agents=_make_agents(),
        assignment=_make_assignment_config(),
        plan=PlanConfig(enabled=True),
    )


def _reviewer_profile(name: str = "plan-reviewer", model: str = "opus") -> ModelProfile:
    return ModelProfile(
        name=name,
        cli="claude",
        model=model,
        budget_usd=5.0,
        timeout_seconds=300,
        allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
    )


# ── Unit tests for _apply_preflight_config seam ───────────────────────


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    """Ensure _has_auth() passes for all test agents."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


class TestPlanReviewersAppliedToConfig:
    """plan_reviewers from AssignmentDecision must propagate to config.plan_agent_review."""

    def test_plan_agent_review_enabled_when_plan_reviewers_present(self, tmp_path):
        """When adaptive decision has plan_reviewers, plan_agent_review.enabled becomes True."""
        config = _make_config_with_assignment(tmp_path)
        assert config.plan_agent_review.enabled is False

        state = CoordinatorState()
        state.preflight_complexity = "medium"

        updated = _apply_preflight_config(config, state)

        assert updated.plan_agent_review.enabled is True

    def test_plan_agent_review_pool_populated_from_decision(self, tmp_path):
        """plan_agent_review.pool is set from adaptive decision plan_reviewers."""
        config = _make_config_with_assignment(tmp_path)
        state = CoordinatorState()
        state.preflight_complexity = "medium"

        updated = _apply_preflight_config(config, state)

        assert updated.plan_agent_review.enabled is True
        assert len(updated.plan_agent_review.pool) >= 1
        # profiles property returns pool when pool is non-empty
        assert updated.plan_agent_review.profiles == updated.plan_agent_review.pool

    def test_plan_agent_review_not_overridden_when_explicit(self, tmp_path):
        """Explicit plan_agent_review config is preserved (not overwritten by adaptive)."""
        explicit_profile = _reviewer_profile("explicit-reviewer", "sonnet")
        explicit_par = PlanAgentReviewConfig(
            enabled=True,
            pool=[explicit_profile],
        )
        config = _dc_replace(
            _make_config_with_assignment(tmp_path),
            plan_agent_review=explicit_par,
        )
        state = CoordinatorState()
        state.preflight_complexity = "medium"
        # Mark plan_agent_review as explicitly configured
        state._explicit_roles = {"plan_agent_review"}

        updated = _apply_preflight_config(config, state)

        # Explicit config must be preserved unchanged
        assert updated.plan_agent_review.enabled is True
        assert updated.plan_agent_review.pool == [explicit_profile]

    def test_adaptive_decision_stored_on_state(self, tmp_path):
        """After _apply_preflight_config, state._adaptive_decision.plan_reviewers is set."""
        config = _make_config_with_assignment(tmp_path)
        state = CoordinatorState()
        state.preflight_complexity = "medium"

        _apply_preflight_config(config, state)

        assert state._adaptive_decision is not None
        assert len(state._adaptive_decision.plan_reviewers) >= 1

    def test_plan_agent_review_disabled_when_no_assignment(self, tmp_path):
        """When assignment is disabled, plan_agent_review.enabled stays False."""
        config = _dc_replace(
            _make_config_with_assignment(tmp_path),
            assignment=_dc_replace(_make_assignment_config(), enabled=False),
        )
        state = CoordinatorState()
        state.preflight_complexity = "medium"

        updated = _apply_preflight_config(config, state)

        assert updated.plan_agent_review.enabled is False

    def test_plan_reviewers_models_match_decision(self, tmp_path):
        """Pool models in config match the models from the assignment decision."""
        config = _make_config_with_assignment(tmp_path)
        state = CoordinatorState()
        state.preflight_complexity = "large"  # high complexity → more reviewers

        updated = _apply_preflight_config(config, state)

        decision = state._adaptive_decision
        assert decision is not None
        pool_models = [p.model for p in updated.plan_agent_review.pool]
        decision_models = [p.model for p in decision.plan_reviewers]
        assert pool_models == decision_models
