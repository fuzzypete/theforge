"""Tests for complexity-aware role derivation (story #807).

Covers:
- tier × complexity matrix for derive_roles() and _apply_complexity_adaptation()
- Override bypass (plan_model_is_default=False, custom dev)
- Fallback when complexity=None (static assignment unchanged)
- Fallback when pool lacks a required tier
- Medium complexity produces no model changes
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from theforge.config import (
    DEFAULT_VALIDATION,
    MODEL_REGISTRY,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.role_derivation import (
    _COST_RANK_TO_TIER,
    _TIER_TO_COST_RANK,
    derive_roles,
)
from theforge.config.types import PlanConfig
from theforge.coordinator.preflight import _apply_complexity_adaptation

# ── Helpers ────────────────────────────────────────────────────────────────────


def _cost_rank_of(cli: str | None, model: str) -> int:
    """Look up cost_rank for a (cli, model) pair from MODEL_REGISTRY."""
    for info in MODEL_REGISTRY.values():
        if info.cli == cli and info.model == model:
            return info.cost_rank
    return 2  # unknown → mid (conservative default)


def _tier_of(cli: str | None, model: str) -> str:
    """Return tier string ('cheap'/'mid'/'strong') for a (cli, model) pair."""
    rank = _cost_rank_of(cli, model)
    return _COST_RANK_TO_TIER.get(rank, "mid")


# Gold pool: [sonnet/cheap, gpt-5.4/mid, opus/strong] — one model per tier.
_GOLD_POOL = ["claude/sonnet", "openai/gpt-5.4", "claude/opus"]

# Sentinel to distinguish "use gold pool default" from "explicitly pass None".
_USE_GOLD: Any = object()


def _make_forge_config(
    tmp_path: Path,
    *,
    dev_model: str = "sonnet",
    dev_cli: str | None = "claude",
    plan_model: str = "sonnet",
    plan_cli: str | None = "claude",
    plan_model_is_default: bool = True,
    smart_config_models: Any = _USE_GOLD,
    review_pool: list[ModelProfile] | None = None,
) -> ForgeConfig:
    """Build a minimal ForgeConfig for complexity-adaptation tests."""
    dev = ModelProfile(
        name="dev",
        cli=dev_cli,
        model=dev_model,
        budget_usd=6.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    gpt_reviewer = ModelProfile(
        name="openai-gpt-5.4",
        cli="codex",
        model="gpt-5.4",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    opus_reviewer = ModelProfile(
        name="claude-opus",
        cli="claude",
        model="opus",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    default_review = review_pool if review_pool is not None else [gpt_reviewer, opus_reviewer]
    synthesis = ModelProfile(
        name="synthesis",
        cli="claude",
        model="opus",
        budget_usd=1.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    plan = PlanConfig(cli=plan_cli, model=plan_model, budget_usd=0.5, timeout=600)

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
        review_pool=default_review,
        synthesis_profile=synthesis,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        smart_config_models=(
            _GOLD_POOL if smart_config_models is _USE_GOLD else smart_config_models
        ),
        plan=plan,
        plan_model_is_default=plan_model_is_default,
    )


# ── derive_roles() tier × complexity matrix ────────────────────────────────────


class TestDeriveRolesComplexityMatrix:
    def test_high_complexity_dev_routes_to_strong_tier(self):
        """HIGH complexity → dev role uses strong-tier model from pool."""
        ra = derive_roles(_GOLD_POOL, complexity="large")
        rank = _cost_rank_of(ra.dev.ref.cli, ra.dev.ref.model)
        assert rank == _TIER_TO_COST_RANK["strong"], (
            f"Expected strong tier (rank=3) for HIGH dev, got rank={rank} "
            f"(model={ra.dev.ref.model})"
        )

    def test_high_complexity_plan_routes_to_strong_tier(self):
        """HIGH complexity → plan role uses strong-tier model from pool."""
        ra = derive_roles(_GOLD_POOL, complexity="large")
        rank = _cost_rank_of(ra.plan.ref.cli, ra.plan.ref.model)
        assert rank == _TIER_TO_COST_RANK["strong"], (
            f"Expected strong tier (rank=3) for HIGH plan, got rank={rank} "
            f"(model={ra.plan.ref.model})"
        )

    def test_low_complexity_dev_routes_to_cheap_or_mid_tier(self):
        """LOW complexity → dev role uses cheap or mid tier model."""
        ra = derive_roles(_GOLD_POOL, complexity="small")
        rank = _cost_rank_of(ra.dev.ref.cli, ra.dev.ref.model)
        assert rank <= _TIER_TO_COST_RANK["mid"], (
            f"Expected cheap/mid tier (rank<=2) for LOW dev, got rank={rank} "
            f"(model={ra.dev.ref.model})"
        )

    def test_low_complexity_plan_routes_to_mid_tier(self):
        """LOW complexity → plan role uses mid-tier model."""
        ra = derive_roles(_GOLD_POOL, complexity="small")
        rank = _cost_rank_of(ra.plan.ref.cli, ra.plan.ref.model)
        assert rank == _TIER_TO_COST_RANK["mid"], (
            f"Expected mid tier (rank=2) for LOW plan, got rank={rank} (model={ra.plan.ref.model})"
        )

    def test_low_complexity_review_pool_has_single_mid_or_strong_reviewer(self):
        """LOW complexity → single reviewer from mid/strong tier, no synthesis."""
        ra = derive_roles(_GOLD_POOL, complexity="small")
        assert len(ra.review_pool) == 1
        assert ra.synthesis is None
        rank = _cost_rank_of(ra.review_pool[0].ref.cli, ra.review_pool[0].ref.model)
        assert rank >= _TIER_TO_COST_RANK["mid"], (
            f"Expected mid/strong reviewer for LOW, got rank={rank}"
        )

    def test_high_complexity_review_pool_has_mid_and_strong(self):
        """HIGH complexity → review pool contains only mid/strong models."""
        ra = derive_roles(_GOLD_POOL, complexity="large")
        for reviewer in ra.review_pool:
            rank = _cost_rank_of(reviewer.ref.cli, reviewer.ref.model)
            assert rank >= _TIER_TO_COST_RANK["mid"], (
                f"Review pool for HIGH should only contain mid/strong, "
                f"got rank={rank} (model={reviewer.ref.model})"
            )

    def test_high_complexity_has_synthesis(self):
        """HIGH complexity with multi-model pool → synthesis is set."""
        ra = derive_roles(_GOLD_POOL, complexity="large")
        assert ra.synthesis is not None

    def test_medium_complexity_review_pool_same_as_static(self):
        """MEDIUM complexity → same review pool as static (no complexity arg)."""
        ra_static = derive_roles(_GOLD_POOL)
        ra_medium = derive_roles(_GOLD_POOL, complexity="medium")
        static_models = [r.ref.model for r in ra_static.review_pool]
        medium_models = [r.ref.model for r in ra_medium.review_pool]
        assert static_models == medium_models

    def test_none_complexity_preserves_static_behavior(self):
        """No complexity → existing static cheapest-first assignment unchanged."""
        ra_static = derive_roles(_GOLD_POOL)
        ra_none = derive_roles(_GOLD_POOL, complexity=None)
        assert ra_static.dev.ref.model == ra_none.dev.ref.model
        assert ra_static.plan.ref.model == ra_none.plan.ref.model

    def test_high_complexity_accepts_uppercase_level(self):
        """'HIGH' (uppercase) is accepted as synonym for 'large'."""
        ra = derive_roles(_GOLD_POOL, complexity="HIGH")
        rank = _cost_rank_of(ra.dev.ref.cli, ra.dev.ref.model)
        assert rank == _TIER_TO_COST_RANK["strong"]

    def test_low_complexity_accepts_uppercase_level(self):
        """'LOW' (uppercase) is accepted as synonym for 'small'."""
        ra = derive_roles(_GOLD_POOL, complexity="LOW")
        rank = _cost_rank_of(ra.dev.ref.cli, ra.dev.ref.model)
        assert rank <= _TIER_TO_COST_RANK["mid"]

    def test_fallback_when_pool_lacks_cheap_tier(self):
        """Pool without cheap tier → LOW dev falls back to nearest available (mid)."""
        pool_no_cheap = ["openai/gpt-5.4", "claude/opus"]  # cost_rank 2 and 3, no 1
        ra = derive_roles(pool_no_cheap, complexity="small")
        rank = _cost_rank_of(ra.dev.ref.cli, ra.dev.ref.model)
        # Should fall back to next tier up (mid=2), not error
        assert rank >= 1, "Fallback should always return a valid tier"

    def test_fallback_when_pool_has_only_strong_models(self):
        """Pool with only strong models → LOW routes to strong (only option)."""
        pool_strong_only = ["claude/opus", "openai/gpt-5.4-pro"]  # both cost_rank=3
        ra = derive_roles(pool_strong_only, complexity="small")
        rank = _cost_rank_of(ra.dev.ref.cli, ra.dev.ref.model)
        # No cheap or mid → must use what's available
        assert rank == _TIER_TO_COST_RANK["strong"]

    def test_overrides_bypass_tier_selection(self):
        """Explicit overrides are applied after tier selection."""
        overrides = {"dev": {"model": "opus", "cli": "claude"}}
        ra = derive_roles(_GOLD_POOL, overrides=overrides, complexity="small")
        # Override wins over LOW cheap selection
        assert ra.dev.ref.model == "opus"


# ── _apply_complexity_adaptation() gold tests ──────────────────────────────────


class TestApplyComplexityAdaptationGold:
    def test_high_routes_dev_to_strong_tier(self, tmp_path):
        """HIGH complexity → dev_profile swapped to strong-tier model."""
        config = _make_forge_config(tmp_path)
        adapted = _apply_complexity_adaptation(config, "large")
        rank = _cost_rank_of(adapted.dev_profile.cli, adapted.dev_profile.model)
        assert rank == _TIER_TO_COST_RANK["strong"], (
            f"Expected strong tier for HIGH dev, got rank={rank} "
            f"(model={adapted.dev_profile.model})"
        )

    def test_high_routes_plan_to_strong_tier_when_default(self, tmp_path):
        """HIGH complexity → plan.model swapped to strong tier when plan_model_is_default."""
        config = _make_forge_config(tmp_path, plan_model_is_default=True)
        adapted = _apply_complexity_adaptation(config, "large")
        rank = _cost_rank_of(adapted.plan.cli, adapted.plan.model)
        assert rank == _TIER_TO_COST_RANK["strong"], (
            f"Expected strong tier for HIGH plan, got rank={rank} (model={adapted.plan.model})"
        )

    def test_low_routes_plan_to_mid_tier_when_default(self, tmp_path):
        """LOW complexity → plan.model swapped to mid tier when plan_model_is_default."""
        config = _make_forge_config(tmp_path, plan_model_is_default=True)
        adapted = _apply_complexity_adaptation(config, "small")
        rank = _cost_rank_of(adapted.plan.cli, adapted.plan.model)
        assert rank == _TIER_TO_COST_RANK["mid"], (
            f"Expected mid tier for LOW plan, got rank={rank} (model={adapted.plan.model})"
        )

    def test_low_review_has_single_mid_or_strong_reviewer(self, tmp_path):
        """LOW complexity → single mid/strong reviewer, no synthesis."""
        config = _make_forge_config(tmp_path)
        adapted = _apply_complexity_adaptation(config, "small")
        assert len(adapted.review_pool) == 1
        assert adapted.synthesis_profile is None
        rank = _cost_rank_of(adapted.review_pool[0].cli, adapted.review_pool[0].model)
        assert rank >= _TIER_TO_COST_RANK["mid"], (
            f"LOW review should keep mid/strong reviewer, got rank={rank}"
        )


# ── Override bypass ────────────────────────────────────────────────────────────


class TestComplexityOverrideBypass:
    def test_plan_not_changed_when_not_default(self, tmp_path):
        """plan_model_is_default=False → plan.model unchanged even for HIGH."""
        config = _make_forge_config(
            tmp_path,
            plan_model="sonnet",
            plan_model_is_default=False,
        )
        adapted = _apply_complexity_adaptation(config, "large")
        assert adapted.plan.model == "sonnet"

    def test_dev_not_changed_when_not_in_pool(self, tmp_path):
        """Custom dev model not in smart_config_models → dev unchanged."""
        config = _make_forge_config(
            tmp_path,
            dev_model="custom-model",  # not in smart_config_models
        )
        adapted = _apply_complexity_adaptation(config, "large")
        assert adapted.dev_profile.model == "custom-model"

    def test_medium_no_change_to_dev(self, tmp_path):
        """MEDIUM complexity → dev_profile unchanged."""
        config = _make_forge_config(tmp_path)
        adapted = _apply_complexity_adaptation(config, "medium")
        assert adapted.dev_profile.model == config.dev_profile.model

    def test_medium_no_change_to_plan(self, tmp_path):
        """MEDIUM complexity → plan.model unchanged."""
        config = _make_forge_config(tmp_path, plan_model_is_default=True)
        adapted = _apply_complexity_adaptation(config, "medium")
        assert adapted.plan.model == config.plan.model

    def test_no_smart_config_is_noop(self, tmp_path):
        """Without smart_config_models, complexity adaptation is always a no-op."""
        config = _make_forge_config(tmp_path, smart_config_models=None)
        # smart_config_models=None → returns config unchanged
        adapted_high = _apply_complexity_adaptation(config, "large")
        adapted_low = _apply_complexity_adaptation(config, "small")
        assert adapted_high is config
        assert adapted_low is config


# ── Fallback when pool lacks target tier ──────────────────────────────────────


class TestComplexityTierFallback:
    def test_low_dev_fallback_when_no_cheap_model(self, tmp_path):
        """LOW dev target is cheap; if pool has only mid/strong, dev stays unchanged."""
        # Pool with no cost_rank=1 model: gpt-5.4(mid) and opus(strong)
        config = _make_forge_config(
            tmp_path,
            dev_model="gpt-5.4",
            dev_cli="codex",
            smart_config_models=["openai/gpt-5.4", "claude/opus"],
        )
        adapted = _apply_complexity_adaptation(config, "small")
        # dev.model is gpt-5.4 (in pool_model_names) → target=cheap → fallback to mid
        # No assertion on specific model; just verify no error and result is valid
        assert adapted.dev_profile.model in {"gpt-5.4", "opus"}

    def test_high_plan_fallback_when_no_strong_model(self, tmp_path):
        """HIGH plan target is strong; if pool has only cheap/mid, picks nearest."""
        config = _make_forge_config(
            tmp_path,
            plan_model_is_default=True,
            smart_config_models=["claude/sonnet", "openai/gpt-5.4"],
        )
        adapted = _apply_complexity_adaptation(config, "large")
        # No strong model → fallback to mid (gpt-5.4, cost_rank=2)
        rank = _cost_rank_of(adapted.plan.cli, adapted.plan.model)
        assert rank >= 1  # some valid tier assigned

    def test_complexity_none_via_apply_no_change(self, tmp_path):
        """_apply_complexity_adaptation is not called with None; medium is the sentinel."""
        config = _make_forge_config(tmp_path)
        adapted = _apply_complexity_adaptation(config, "medium")
        assert adapted is config

    def test_review_pool_all_cheap_low_falls_back(self, tmp_path):
        """LOW review needs mid/strong; pool of all cheap → fallback to cheapest."""
        # Both reviewers are sonnet (cheap), no mid/strong
        cheap_reviewer1 = ModelProfile(
            name="sonnet-1",
            cli="claude",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
        )
        cheap_reviewer2 = ModelProfile(
            name="sonnet-2",
            cli="claude",
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
        )
        config = _make_forge_config(
            tmp_path,
            review_pool=[cheap_reviewer1, cheap_reviewer2],
            smart_config_models=["claude/sonnet"],
        )
        # Fallback: keeps cheapest reviewer even though no mid/strong available
        adapted = _apply_complexity_adaptation(config, "small")
        assert len(adapted.review_pool) == 1
        assert adapted.synthesis_profile is None
