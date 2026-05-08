"""Regression test for issue #1435.

CLI reviewer profiles in the review pool must carry api_fallback after
preflight complexity adaptation, so that quota-exhausted CLI failures escalate
through the configured API fallback instead of triggering a Pending Escalate Gate.

Test strategy: exercise the full path the failing run took — preflight
_apply_complexity_adaptation → review-pool runner CLI branch → quota-shaped
failure → _maybe_run_api_fallback. Mock only the API client call itself.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.agent_types import AgentResult, ModelUsage
from theforge.config import (
    DEFAULT_VALIDATION,
    ApiFallbackConfig,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.types import PlanConfig
from theforge.coordinator.preflight import _apply_complexity_adaptation

_GOLD_POOL = ["claude/sonnet", "openai/gpt-5.4", "claude/opus"]


def _make_config_with_provider_fallbacks(
    tmp_path: Path,
    *,
    provider_fallbacks: dict[str, ApiFallbackConfig],
) -> ForgeConfig:
    """Build a ForgeConfig that mirrors the failing-run shape: review_pool with
    a Codex CLI reviewer and provider_fallbacks.openai configured at load time.
    """
    dev = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=6.0,
        timeout_seconds=300,
        allowed_tools=(),
    )
    # Intentionally do NOT preload api_fallback on these reviewers. The bug under
    # test (#1435) is that a CLI reviewer reaching the runner without api_fallback
    # silently skips the configured API fallback. The fix re-applies
    # provider_fallbacks inside _apply_complexity_adaptation, so a reviewer that
    # arrives at preflight without api_fallback must acquire it through that path.
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
    plan = PlanConfig(cli="claude", model="sonnet", budget_usd=0.5, timeout=600)

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
        review_pool=[gpt_reviewer, opus_reviewer],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        models=_GOLD_POOL,
        plan=plan,
        plan_model_is_default=True,
        dev_profile_is_default=True,
        review_pool_is_default=True,
        provider_fallbacks=provider_fallbacks,
    )


def test_low_complexity_review_pool_preserves_api_fallback(tmp_path):
    """LOW complexity → single mid/strong reviewer keeps api_fallback set."""
    fallbacks = {
        "openai": ApiFallbackConfig(provider="openai", model="gpt-5.4-mini"),
        "anthropic": ApiFallbackConfig(provider="anthropic", model="haiku"),
    }
    config = _make_config_with_provider_fallbacks(tmp_path, provider_fallbacks=fallbacks)

    adapted = _apply_complexity_adaptation(config, "small")

    assert len(adapted.review_pool) == 1
    reviewer = adapted.review_pool[0]
    assert reviewer.api_fallback is not None, (
        f"LOW reviewer {reviewer.name!r} ({reviewer.cli}/{reviewer.model}) lost api_fallback "
        f"after complexity adaptation; spec #1435 requires it to fire on quota exhaustion."
    )


def test_medium_complexity_review_pool_and_synthesis_carry_api_fallback(tmp_path):
    """MEDIUM complexity → every reviewer + auto-derived synthesis carries api_fallback."""
    fallbacks = {
        "openai": ApiFallbackConfig(provider="openai", model="gpt-5.4-mini"),
        "anthropic": ApiFallbackConfig(provider="anthropic", model="haiku"),
    }
    config = _make_config_with_provider_fallbacks(tmp_path, provider_fallbacks=fallbacks)

    adapted = _apply_complexity_adaptation(config, "medium")

    for reviewer in adapted.review_pool:
        assert reviewer.api_fallback is not None, (
            f"MEDIUM reviewer {reviewer.name!r} ({reviewer.cli}/{reviewer.model}) "
            f"lost api_fallback after complexity adaptation."
        )

    assert adapted.synthesis_profile is not None
    assert adapted.synthesis_profile.api_fallback is not None, (
        "MEDIUM synthesis profile must carry api_fallback so that quota-exhausted "
        "synthesis runs escalate via API fallback, not via pending escalate gate."
    )


def test_dev_reroute_across_providers_re_derives_api_fallback(tmp_path):
    """Cross-provider dev reroute must re-derive api_fallback for the new CLI's provider.

    Regression: dev starts on claude/sonnet (api_fallback for anthropic). HIGH complexity
    routes dev to openai/gpt-5.4-pro (codex CLI). Without re-applying provider_fallbacks,
    the rerouted dev profile would carry a stale anthropic api_fallback — wrong provider
    for a codex CLI quota failure.
    """
    fallbacks = {
        "openai": ApiFallbackConfig(provider="openai", model="gpt-5.4-mini"),
        "anthropic": ApiFallbackConfig(provider="anthropic", model="haiku"),
    }
    dev = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=6.0,
        timeout_seconds=300,
        allowed_tools=(),
        api_fallback=fallbacks["anthropic"],
    )
    plan = PlanConfig(cli="claude", model="sonnet", budget_usd=0.5, timeout=600)
    config = ForgeConfig(
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
        review_pool=[
            ModelProfile(
                name="claude-opus",
                cli="claude",
                model="opus",
                budget_usd=1.0,
                timeout_seconds=300,
                allowed_tools=(),
                api_fallback=fallbacks["anthropic"],
            ),
        ],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        models=["claude/sonnet", "openai/gpt-5.4", "openai/gpt-5.4-pro"],
        plan=plan,
        plan_model_is_default=True,
        dev_profile_is_default=True,
        review_pool_is_default=True,
        provider_fallbacks=fallbacks,
    )

    adapted = _apply_complexity_adaptation(config, "large")

    assert adapted.dev_profile.cli == "codex"
    assert adapted.dev_profile.api_fallback is not None
    assert adapted.dev_profile.api_fallback.provider == "openai", (
        f"Cross-provider dev reroute (claude→codex) must re-derive api_fallback for openai; "
        f"got {adapted.dev_profile.api_fallback.provider!r}."
    )


def test_rerouted_codex_reviewer_quota_failure_fires_api_fallback(tmp_path):
    """End-to-end: preflight-adapted review-pool reviewer hits 'usage limit' →
    runner invokes _maybe_run_api_fallback → returns API-attributed success."""
    fallback_cfg = ApiFallbackConfig(provider="openai", model="gpt-5.4-mini")
    config = _make_config_with_provider_fallbacks(
        tmp_path, provider_fallbacks={"openai": fallback_cfg}
    )

    # HIGH complexity routes dev → opus (strong); the self-review guard then keeps
    # gpt-5.4/codex in the review pool. MEDIUM would reroute dev to gpt-5.4 itself,
    # which the self-review guard would then drop — leaving no Codex reviewer to test.
    adapted = _apply_complexity_adaptation(config, "large")
    codex_reviewer = next(
        (p for p in adapted.review_pool if p.cli == "codex"),
        None,
    )
    assert codex_reviewer is not None, "Test setup: expected codex reviewer in pool"
    assert codex_reviewer.api_fallback is not None

    cli_quota_failure = AgentResult(
        success=False,
        output="ERROR: You've hit your usage limit. Upgrade to Pro",
        session_id=None,
        cost_usd=None,
        exit_code=1,
        raw={},
        profile_name=codex_reviewer.name,
    )
    api_success = AgentResult(
        success=True,
        output="REVIEW OK",
        session_id=None,
        cost_usd=0.01,
        exit_code=0,
        raw={},
        profile_name=codex_reviewer.name,
        model_usage=(
            ModelUsage(
                model="gpt-5.4-mini",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=0.01,
            ),
        ),
    )

    with (
        patch("theforge.runners.runner_codex._run_codex", return_value=cli_quota_failure),
        patch("theforge.runners.api.run_api_agent", return_value=api_success) as mock_api,
    ):
        from theforge.runners.cli import run_agent

        result = run_agent(
            prompt="review this",
            profile=codex_reviewer,
            working_dir=tmp_path,
            quiet=True,
        )

    mock_api.assert_called_once()
    assert result.success
    assert result.cli_quota_error_observed is True
    assert result.transport_fallback_fired is True
    assert result.transport_used == "api"
    assert result.model_used == "gpt-5.4-mini"
