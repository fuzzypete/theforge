"""Unit tests for config schema types, derive_roles(), and bridge conversion.

Tests cover:
- ModelRef construction and cli/provider mutual exclusion validation
- Phase wrapper composition (no transport field duplication)
- derive_roles() happy path: correct role → model mapping
- derive_roles() with overrides: override merges correctly
- Bridge conversion round-trip: RoleAssignment → ModelProfile fields match
- Clean insertion-point property: derive_roles can be replaced by a stub
"""

from __future__ import annotations

import pytest

from theforge.config.bridge import role_assignment_to_profiles
from theforge.config.role_derivation import derive_roles
from theforge.config.schema import (
    DevRoleConfig,
    ModelRef,
    PlanRoleConfig,
    PreflightRoleConfig,
    ReviewRoleConfig,
    RoleAssignment,
)
from theforge.config.types import ApiFallbackConfig, ModelProfile

# ---------------------------------------------------------------------------
# ModelRef construction and validation
# ---------------------------------------------------------------------------


class TestModelRef:
    def test_cli_only(self):
        ref = ModelRef(model="sonnet", cli="claude", budget_usd=2.0, timeout_seconds=900)
        assert ref.cli == "claude"
        assert ref.provider is None
        assert ref.model == "sonnet"

    def test_provider_only(self):
        ref = ModelRef(
            model="claude-3-7-sonnet", provider="anthropic", budget_usd=1.0, timeout_seconds=300
        )
        assert ref.provider == "anthropic"
        assert ref.cli is None

    def test_neither_cli_nor_provider_is_allowed(self):
        # ModelRef does not require one to be set — that's validated at profile parse time.
        ref = ModelRef(model="sonnet", budget_usd=1.0, timeout_seconds=300)
        assert ref.cli is None
        assert ref.provider is None

    def test_both_cli_and_provider_raises(self):
        with pytest.raises(ValueError, match="cannot have both"):
            ModelRef(
                model="sonnet",
                cli="claude",
                provider="anthropic",
                budget_usd=1.0,
                timeout_seconds=300,
            )

    def test_optional_fields_default(self):
        ref = ModelRef(model="sonnet", cli="claude", budget_usd=1.0, timeout_seconds=300)
        assert ref.fallback_models == ()
        assert ref.timeout_medium_seconds is None
        assert ref.timeout_large_seconds is None
        assert ref.reasoning_effort is None
        assert ref.thinking_budget is None
        assert ref.base_url is None
        assert ref.max_iterations is None
        assert ref.max_tool_output_bytes == 51200
        assert ref.api_fallback is None

    def test_all_optional_fields(self):
        fallback = ApiFallbackConfig(provider="anthropic", model="claude-3-5-haiku")
        ref = ModelRef(
            model="sonnet",
            cli="claude",
            budget_usd=2.0,
            timeout_seconds=900,
            fallback_models=("haiku",),
            timeout_medium_seconds=600,
            timeout_large_seconds=1200,
            reasoning_effort="high",
            thinking_budget=8000,
            base_url="http://localhost:11434",
            max_iterations=5,
            max_tool_output_bytes=102400,
            api_fallback=fallback,
        )
        assert ref.fallback_models == ("haiku",)
        assert ref.timeout_medium_seconds == 600
        assert ref.timeout_large_seconds == 1200
        assert ref.reasoning_effort == "high"
        assert ref.thinking_budget == 8000
        assert ref.base_url == "http://localhost:11434"
        assert ref.max_iterations == 5
        assert ref.max_tool_output_bytes == 102400
        assert ref.api_fallback is fallback

    def test_is_frozen(self):
        ref = ModelRef(model="sonnet", cli="claude", budget_usd=1.0, timeout_seconds=300)
        with pytest.raises((AttributeError, TypeError)):
            ref.model = "opus"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Phase wrapper composition — no transport field duplication
# ---------------------------------------------------------------------------


class TestPhaseWrappers:
    """Phase wrappers embed ModelRef; they have no transport fields of their own."""

    def test_dev_role_config_fields(self):
        ref = ModelRef(model="sonnet", cli="claude", budget_usd=2.0, timeout_seconds=900)
        dev = DevRoleConfig(ref=ref)
        # Transport lives in ref, not duplicated on DevRoleConfig
        assert not hasattr(dev, "cli")
        assert not hasattr(dev, "provider")
        assert not hasattr(dev, "model")
        assert not hasattr(dev, "budget_usd")
        assert not hasattr(dev, "timeout_seconds")
        # Phase-specific fields
        assert dev.allowed_tools == ("Read", "Edit", "Write", "Bash", "Glob", "Grep")
        assert dev.sandbox_mode == "workspace-write"

    def test_preflight_role_config_fields(self):
        ref = ModelRef(model="sonnet", cli="claude", budget_usd=1.0, timeout_seconds=300)
        pf = PreflightRoleConfig(ref=ref)
        assert not hasattr(pf, "cli")
        assert not hasattr(pf, "budget_usd")
        assert pf.allowed_tools == ("Read", "Bash", "Glob", "Grep")

    def test_plan_role_config_fields(self):
        ref = ModelRef(model="sonnet", cli="claude", budget_usd=0.5, timeout_seconds=600)
        plan = PlanRoleConfig(ref=ref)
        assert not hasattr(plan, "cli")
        assert plan.validate_spec is True

    def test_review_role_config_fields(self):
        ref = ModelRef(model="opus", cli="claude", budget_usd=1.0, timeout_seconds=300)
        rev = ReviewRoleConfig(ref=ref, review_role="correctness")
        assert not hasattr(rev, "cli")
        assert rev.review_role == "correctness"
        assert rev.allowed_tools == ("Read", "Bash", "Glob", "Grep")

    def test_wrapper_is_frozen(self):
        ref = ModelRef(model="sonnet", cli="claude", budget_usd=1.0, timeout_seconds=300)
        dev = DevRoleConfig(ref=ref)
        with pytest.raises((AttributeError, TypeError)):
            dev.sandbox_mode = "none"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# derive_roles() happy path
# ---------------------------------------------------------------------------


class TestDeriveRolesHappyPath:
    def test_single_model(self):
        """Single model: dev uses it, preflight uses it, pool = [dev]."""
        ra = derive_roles(["claude/sonnet"])
        assert ra.dev.ref.model == "sonnet"
        assert ra.dev.ref.cli == "claude"
        assert ra.preflight.ref.model == "sonnet"
        assert len(ra.review_pool) == 1
        assert ra.review_pool[0].ref.model == "sonnet"
        assert ra.synthesis is None  # pool <= 1, no synthesis

    def test_two_models_assigns_cheapest_to_dev(self):
        """Dev gets cheapest dev-capable model."""
        ra = derive_roles(["claude/sonnet", "claude/opus"])
        # sonnet is cost_rank=1, opus is cost_rank=3
        assert ra.dev.ref.model == "sonnet"

    def test_strongest_model_becomes_synthesis(self):
        """Synthesis is the highest-capability reviewer when pool > 1."""
        ra = derive_roles(["claude/sonnet", "claude/opus", "openai/gpt-5.4"])
        assert ra.synthesis is not None
        # claude/opus and openai/gpt-5.4 both have capability=10, gpt-5.4-pro is higher
        # synthesis picks max capability among reviewers
        assert ra.synthesis.ref.model in {"opus", "gpt-5.4"}

    def test_preflight_gets_fast_tier(self):
        """Preflight gets cheapest 'fast' tier model when available."""
        # claude/sonnet is tier="fast", claude/opus is tier="strong"
        ra = derive_roles(["claude/sonnet", "claude/opus"])
        assert ra.preflight.ref.model == "sonnet"  # fast tier

    def test_review_pool_excludes_dev(self):
        """Review pool excludes the dev model when multiple models are available."""
        ra = derive_roles(["claude/sonnet", "claude/opus"])
        pool_models = [rc.ref.model for rc in ra.review_pool]
        assert "sonnet" not in pool_models  # dev model excluded
        assert "opus" in pool_models

    def test_budget_distribution(self):
        """Budget is distributed per the documented percentages."""
        ra = derive_roles(["claude/sonnet", "claude/opus"], budget_usd=10.0)
        # dev: 60% = $6.00
        assert abs(ra.dev.ref.budget_usd - 6.0) < 0.001
        # preflight: max(2%, $1) = max($0.20, $1.00) = $1.00
        assert abs(ra.preflight.ref.budget_usd - 1.0) < 0.001

    def test_plan_defaults(self):
        """Plan role gets dev model and plan-specific budget/timeout."""
        ra = derive_roles(["claude/sonnet"])
        assert ra.plan.ref.model == ra.dev.ref.model
        assert ra.plan.ref.budget_usd == 0.50
        assert ra.plan.ref.timeout_seconds == 600
        assert ra.plan.validate_spec is True

    def test_dev_allowed_tools_default(self):
        ra = derive_roles(["claude/sonnet"])
        assert "Edit" in ra.dev.allowed_tools
        assert "Write" in ra.dev.allowed_tools

    def test_preflight_allowed_tools_default(self):
        ra = derive_roles(["claude/sonnet"])
        assert "Read" in ra.preflight.allowed_tools
        assert "Edit" not in ra.preflight.allowed_tools

    def test_empty_models_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            derive_roles([])

    def test_unknown_model_handled_gracefully(self):
        """Unknown models get sensible defaults from _resolve_model_info."""
        ra = derive_roles(["custom/my-local-model"])
        assert ra.dev.ref.model == "my-local-model"
        assert ra.synthesis is None  # only one model


# ---------------------------------------------------------------------------
# derive_roles() with overrides
# ---------------------------------------------------------------------------


class TestDeriveRolesOverrides:
    def test_dev_budget_override(self):
        ra = derive_roles(
            ["claude/sonnet"],
            overrides={"dev": {"budget_usd": 5.0}},
        )
        assert ra.dev.ref.budget_usd == 5.0

    def test_dev_timeout_override(self):
        ra = derive_roles(
            ["claude/sonnet"],
            overrides={"dev": {"timeout_seconds": 1800}},
        )
        assert ra.dev.ref.timeout_seconds == 1800

    def test_dev_sandbox_mode_override(self):
        ra = derive_roles(
            ["claude/sonnet"],
            overrides={"dev": {"sandbox_mode": "read-only"}},
        )
        assert ra.dev.sandbox_mode == "read-only"

    def test_preflight_model_override(self):
        """Override preflight to use a specific model."""
        ra = derive_roles(
            ["claude/sonnet", "claude/opus"],
            overrides={"preflight": {"model": "opus"}},
        )
        assert ra.preflight.ref.model == "opus"

    def test_plan_validate_spec_override(self):
        ra = derive_roles(
            ["claude/sonnet"],
            overrides={"plan": {"validate_spec": False}},
        )
        assert ra.plan.validate_spec is False

    def test_synthesis_budget_override(self):
        # Need 3+ models so pool > 1 and synthesis is generated
        ra = derive_roles(
            ["claude/sonnet", "claude/opus", "openai/gpt-5.4"],
            overrides={"synthesis": {"budget_usd": 3.0}},
        )
        assert ra.synthesis is not None
        assert ra.synthesis.ref.budget_usd == 3.0

    def test_review_pool_per_reviewer_override(self):
        """Pool overrides are applied by index."""
        ra = derive_roles(
            ["claude/sonnet", "claude/opus"],
            overrides={"review_pool": [{"budget_usd": 2.5}]},
        )
        assert ra.review_pool[0].ref.budget_usd == 2.5

    def test_unrecognized_role_override_ignored(self):
        """Overrides for unknown role keys are silently ignored."""
        # Should not raise even if unknown key is present
        ra = derive_roles(
            ["claude/sonnet"],
            overrides={"nonexistent_role": {"budget_usd": 99.0}},
        )
        assert ra.dev.ref.model == "sonnet"

    def test_override_does_not_mutate_base(self):
        """Applying an override returns a new RoleAssignment without mutating shared state."""
        ra1 = derive_roles(["claude/sonnet"])
        ra2 = derive_roles(["claude/sonnet"], overrides={"dev": {"budget_usd": 99.0}})
        # ra1 is unchanged
        assert ra1.dev.ref.budget_usd != 99.0
        assert ra2.dev.ref.budget_usd == 99.0

    def test_ref_fields_not_overridden_keep_derived_values(self):
        """Only specified override fields change; others retain derived values."""
        ra = derive_roles(
            ["claude/sonnet"],
            overrides={"dev": {"budget_usd": 5.0}},
            budget_usd=10.0,
        )
        # timeout_seconds was NOT overridden → keeps derived default
        assert ra.dev.ref.timeout_seconds > 0
        assert ra.dev.ref.model == "sonnet"


# ---------------------------------------------------------------------------
# Bridge conversion round-trip
# ---------------------------------------------------------------------------


class TestRoleAssignmentToProfiles:
    def _make_simple_assignment(self) -> RoleAssignment:
        return derive_roles(["claude/sonnet", "claude/opus"], budget_usd=10.0)

    def test_returns_expected_keys(self):
        ra = self._make_simple_assignment()
        result = role_assignment_to_profiles(ra)
        assert set(result.keys()) == {
            "dev_profile",
            "preflight_profile",
            "plan_profile",
            "review_pool",
            "synthesis_profile",
        }

    def test_dev_profile_is_model_profile(self):
        ra = self._make_simple_assignment()
        result = role_assignment_to_profiles(ra)
        assert isinstance(result["dev_profile"], ModelProfile)

    def test_dev_profile_fields_match_ref(self):
        ra = self._make_simple_assignment()
        result = role_assignment_to_profiles(ra)
        dev: ModelProfile = result["dev_profile"]  # type: ignore[assignment]
        assert dev.name == "dev"
        assert dev.model == ra.dev.ref.model
        assert dev.cli == ra.dev.ref.cli
        assert dev.budget_usd == ra.dev.ref.budget_usd
        assert dev.timeout_seconds == ra.dev.ref.timeout_seconds
        assert dev.phase == "dev"
        assert dev.allowed_tools == ra.dev.allowed_tools
        assert dev.sandbox_mode == ra.dev.sandbox_mode

    def test_preflight_profile_phase(self):
        ra = self._make_simple_assignment()
        result = role_assignment_to_profiles(ra)
        pf: ModelProfile = result["preflight_profile"]  # type: ignore[assignment]
        assert pf.phase == "preflight"

    def test_plan_profile_phase(self):
        ra = self._make_simple_assignment()
        result = role_assignment_to_profiles(ra)
        plan: ModelProfile = result["plan_profile"]  # type: ignore[assignment]
        assert plan.phase == "plan"

    def test_review_pool_is_list_of_model_profiles(self):
        ra = self._make_simple_assignment()
        result = role_assignment_to_profiles(ra)
        pool = result["review_pool"]
        assert isinstance(pool, list)
        assert len(pool) == len(ra.review_pool)
        for p in pool:
            assert isinstance(p, ModelProfile)
            assert p.phase == "review"

    def test_synthesis_profile_present_when_pool_gt_1(self):
        # Need 3+ models so review_pool > 1 and synthesis is generated
        ra = derive_roles(["claude/sonnet", "claude/opus", "openai/gpt-5.4"], budget_usd=10.0)
        result = role_assignment_to_profiles(ra)
        assert result["synthesis_profile"] is not None
        synth: ModelProfile = result["synthesis_profile"]  # type: ignore[assignment]
        assert isinstance(synth, ModelProfile)

    def test_synthesis_profile_none_when_single_model(self):
        ra = derive_roles(["claude/sonnet"])
        result = role_assignment_to_profiles(ra)
        assert result["synthesis_profile"] is None

    def test_all_optional_ref_fields_propagate(self):
        """Optional ModelRef fields (reasoning_effort, base_url, etc.) flow to ModelProfile."""
        ref = ModelRef(
            model="sonnet",
            cli="claude",
            budget_usd=2.0,
            timeout_seconds=900,
            timeout_medium_seconds=600,
            timeout_large_seconds=1200,
            reasoning_effort="high",
            thinking_budget=8000,
            base_url="http://localhost:11434",
            max_iterations=5,
            max_tool_output_bytes=102400,
        )
        dev = DevRoleConfig(ref=ref)
        pf_ref = ModelRef(model="sonnet", cli="claude", budget_usd=1.0, timeout_seconds=300)
        pf = PreflightRoleConfig(ref=pf_ref)
        plan_ref = ModelRef(model="sonnet", cli="claude", budget_usd=0.5, timeout_seconds=600)
        plan = PlanRoleConfig(ref=plan_ref)
        rev_ref = ModelRef(model="opus", cli="claude", budget_usd=1.0, timeout_seconds=300)
        rev = ReviewRoleConfig(ref=rev_ref)
        ra = RoleAssignment(
            dev=dev,
            preflight=pf,
            plan=plan,
            review_pool=(rev,),
            synthesis=None,
        )
        result = role_assignment_to_profiles(ra)
        dev_profile: ModelProfile = result["dev_profile"]  # type: ignore[assignment]
        assert dev_profile.timeout_medium_seconds == 600
        assert dev_profile.timeout_large_seconds == 1200
        assert dev_profile.reasoning_effort == "high"
        assert dev_profile.thinking_budget == 8000
        assert dev_profile.base_url == "http://localhost:11434"
        assert dev_profile.max_iterations == 5
        assert dev_profile.max_tool_output_bytes == 102400


# ---------------------------------------------------------------------------
# Clean insertion-point property
# ---------------------------------------------------------------------------


class TestInsertionPoint:
    """derive_roles() can be replaced by any function that returns a RoleAssignment.

    This verifies the type contract: as long as the stub returns a valid
    RoleAssignment, the bridge works without knowing how the roles were derived.
    """

    def test_stub_derive_roles_works_with_bridge(self):
        """A stub that bypasses the real assignment logic still works with bridge."""
        ref = ModelRef(model="custom-model", cli="codex", budget_usd=1.0, timeout_seconds=300)
        stub_ra = RoleAssignment(
            dev=DevRoleConfig(ref=ref),
            preflight=PreflightRoleConfig(ref=ref),
            plan=PlanRoleConfig(ref=ref),
            review_pool=(ReviewRoleConfig(ref=ref),),
            synthesis=None,
        )
        # Bridge must accept any valid RoleAssignment, regardless of how it was built
        result = role_assignment_to_profiles(stub_ra)
        dev: ModelProfile = result["dev_profile"]  # type: ignore[assignment]
        assert dev.model == "custom-model"
        assert dev.cli == "codex"

    def test_role_assignment_accepts_any_valid_review_pool(self):
        """review_pool can have any number of ReviewRoleConfigs."""
        ref1 = ModelRef(model="sonnet", cli="claude", budget_usd=1.0, timeout_seconds=300)
        ref2 = ModelRef(model="opus", cli="claude", budget_usd=1.0, timeout_seconds=300)
        ref3 = ModelRef(model="gpt-5.4", cli="codex", budget_usd=1.0, timeout_seconds=300)
        dev_ref = ModelRef(model="sonnet", cli="claude", budget_usd=6.0, timeout_seconds=900)
        plan_ref = ModelRef(model="sonnet", cli="claude", budget_usd=0.5, timeout_seconds=600)
        ra = RoleAssignment(
            dev=DevRoleConfig(ref=dev_ref),
            preflight=PreflightRoleConfig(ref=ref1),
            plan=PlanRoleConfig(ref=plan_ref),
            review_pool=(
                ReviewRoleConfig(ref=ref1),
                ReviewRoleConfig(ref=ref2),
                ReviewRoleConfig(ref=ref3),
            ),
        )
        result = role_assignment_to_profiles(ra)
        assert len(result["review_pool"]) == 3  # type: ignore[arg-type]
