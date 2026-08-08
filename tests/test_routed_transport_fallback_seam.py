"""Seam coverage for configured transport fallbacks on adaptively routed phases.

Issue #2298. A configured ``transport_fallback`` was attached only to profiles
built from *role declarations* at config load. Adaptive routing sources its
profiles from the model catalog, so the dev phase — the long, expensive,
iterating one — ran with ``api_fallback=None`` no matter how the operator had
configured the provider. Two paths that arrive at the same model must not
disagree about what happens when that model's provider stops answering.

The second half of the story: where a fallback is eligible and cannot be
applied, the run must say so, and must not keep spending iterations against a
provider that stated when its limit resets.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from theforge.agent_types import AgentResult  # noqa: E402
from theforge.assignment import (  # noqa: E402
    AssignmentConfig,
    apply_post_plan_checkpoint,
    assign_models,
)
from theforge.config import (  # noqa: E402
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    AgentDef,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    RetryPolicy,
    TransportFallbackConfig,
    WorkspaceConfig,
)
from theforge.coordinator.state import CoordinatorState, Phase  # noqa: E402
from theforge.task import TaskStory  # noqa: E402

_OPENAI_FALLBACK = TransportFallbackConfig(
    provider="openai",
    model="gpt-5.4",
    timeout_seconds=1800,
)
_FALLBACKS = {"openai": _OPENAI_FALLBACK}

# The verbatim provider refusal from the reported run.
_QUOTA_OUTPUT = (
    "You've hit your usage limit. Upgrade to Pro or try again at Aug 8th, 2026 7:59 AM."
)


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")


def _assign_cfg(**kwargs) -> AssignmentConfig:
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


def _catalog_agents(api_fallback: TransportFallbackConfig | None = None) -> list[AgentDef]:
    """A catalog pool whose entries carry no api_fallback of their own.

    This is the shape the bug reproduced against: the catalog agent named
    ``openai-gpt-5.4-cli`` resolves ``api_fallback`` to None even though the
    role-declared profile for the very same model has one.
    """
    return [
        AgentDef(
            name="openai-gpt-5.4-cli",
            provider=None,
            cli="codex",
            model="gpt-5.4",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
            api_fallback=api_fallback,
        ),
        AgentDef(
            name="openai-gpt-5.4-cheap",
            provider=None,
            cli="codex",
            model="gpt-5.4-mini",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="cheap",
            api_fallback=api_fallback,
        ),
        AgentDef(
            name="claude-opus-cli",
            provider=None,
            cli="claude",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
        ),
    ]


# ── Routed profiles carry the configured fallback (#2298) ─────────────


def test_routed_dev_profile_carries_configured_transport_fallback():
    """The reported symptom: dev is routed from the catalog, so it lost the fallback."""
    decision = assign_models(
        _catalog_agents(),
        _assign_cfg(),
        complexity="medium",
        transport_fallbacks=_FALLBACKS,
    )

    assert decision.dev.cli == "codex"
    assert decision.dev.api_fallback is not None
    assert decision.dev.api_fallback.provider == "openai"
    assert decision.dev.api_fallback.model == "gpt-5.4"


def test_routed_dev_profile_without_configured_fallback_stays_none():
    """No configuration means no fallback — the fix must not invent one."""
    decision = assign_models(_catalog_agents(), _assign_cfg(), complexity="medium")

    assert decision.dev.api_fallback is None


def test_explicit_catalog_api_fallback_wins_over_configured_entry():
    """A fallback the catalog entry states explicitly is preserved, not overwritten."""
    explicit = TransportFallbackConfig(provider="openai", model="o4-mini", timeout_seconds=600)
    decision = assign_models(
        _catalog_agents(api_fallback=explicit),
        _assign_cfg(),
        complexity="medium",
        transport_fallbacks=_FALLBACKS,
    )

    assert decision.dev.api_fallback is not None
    assert decision.dev.api_fallback.model == "o4-mini"


def test_routed_reviewer_and_planner_profiles_carry_configured_fallback():
    """Every routed role, not just dev — the fallback is a provider-level decision."""
    decision = assign_models(
        _catalog_agents(),
        _assign_cfg(min_reviewers=1, max_reviewers=1),
        complexity="medium",
        transport_fallbacks=_FALLBACKS,
    )

    routed = [decision.preflight, decision.planner, *decision.code_reviewers]
    for profile in routed:
        if profile.mode != "cli" or profile.provider_family != "openai":
            continue
        assert profile.api_fallback is not None, f"{profile.name} lost the configured fallback"


def test_post_plan_demoted_dev_profile_carries_configured_fallback():
    """The post-plan checkpoint rebuilds the dev profile — it must not drop the fallback."""
    decision = assign_models(
        _catalog_agents(),
        _assign_cfg(),
        complexity="medium",
        transport_fallbacks=_FALLBACKS,
    )
    updated = apply_post_plan_checkpoint(
        decision,
        _catalog_agents(),
        _assign_cfg(),
        "medium",
        plan_review_decision="APPROVE",
        plan_review_cycles=1,
        p1_count=0,
        p2_count=0,
        transport_fallbacks=_FALLBACKS,
    )

    assert updated.dev.api_fallback is not None
    assert updated.dev.api_fallback.model == "gpt-5.4"


def test_apply_transport_fallback_preserves_registry_identity():
    """The helper must not silently reset fields it does not name.

    Routed profiles carry registry provenance; rebuilding them field-by-field
    (as this helper used to) dropped it, which is the same defect that once lost
    ``phase``.
    """
    from theforge.config.profiles import _apply_transport_fallback

    profile = ModelProfile(
        name="openai-gpt-5.4-cli",
        cli="codex",
        model="gpt-5.4",
        budget_usd=5.0,
        timeout_seconds=900,
        allowed_tools=("Read",),
        phase="dev",
        registry_id="openai/gpt-5.4/cli",
        registry_source="forge.yaml",
        sandbox_capability_profile="wide",
    )

    out = _apply_transport_fallback(profile, _FALLBACKS)

    assert out.api_fallback is _OPENAI_FALLBACK
    assert out.registry_id == "openai/gpt-5.4/cli"
    assert out.registry_source == "forge.yaml"
    assert out.sandbox_capability_profile == "wide"
    assert out.phase == "dev"


# ── Eligible-but-unavailable fallback is recorded (#2298) ─────────────


def _codex_profile(**kwargs) -> ModelProfile:
    defaults = dict(
        name="openai-gpt-5.4-cli",
        cli="codex",
        model="gpt-5.4",
        budget_usd=5.0,
        timeout_seconds=900,
        allowed_tools=("Read",),
        phase="dev",
    )
    defaults.update(kwargs)
    return ModelProfile(**defaults)


def _quota_failure(profile: ModelProfile) -> AgentResult:
    return AgentResult(
        success=False,
        output=_QUOTA_OUTPUT,
        session_id=None,
        cost_usd=0.0,
        exit_code=1,
        raw={},
        profile_name=profile.name,
    )


def _run_codex_agent(profile: ModelProfile, tmp_path: Path) -> AgentResult:
    from theforge.runners.cli import run_agent

    with patch(
        "theforge.runners.runner_codex._run_codex",
        return_value=_quota_failure(profile),
    ):
        return run_agent(
            prompt="do work",
            profile=profile,
            working_dir=tmp_path,
            quiet=True,
        )


def test_quota_failure_without_fallback_records_why_it_was_not_applied(tmp_path: Path) -> None:
    """Recording only the reason describes an intention; the outcome must be stated too."""
    profile = _codex_profile()
    result = _run_codex_agent(profile, tmp_path)

    assert result.cli_quota_error_observed is True
    assert result.transport_fallback_fired is False
    assert result.transport_fallback_reason == "matched 'usage limit'"
    assert result.transport_fallback_not_applied_reason is not None
    assert "no transport fallback configured" in result.transport_fallback_not_applied_reason
    assert "openai" in result.transport_fallback_not_applied_reason


def test_quota_failure_records_the_reset_time_the_provider_stated(tmp_path: Path) -> None:
    result = _run_codex_agent(_codex_profile(), tmp_path)

    assert result.provider_quota_reset_at == "Aug 8th, 2026 7:59 AM"


def test_quota_failure_without_a_stated_reset_time_records_none(tmp_path: Path) -> None:
    """A limit that names no reset moment may clear on its own — do not claim certainty."""
    profile = _codex_profile()
    failure = replace(_quota_failure(profile), output="429 rate limit exceeded, try again later")
    from theforge.runners.cli import run_agent

    with patch("theforge.runners.runner_codex._run_codex", return_value=failure):
        result = run_agent(prompt="do work", profile=profile, working_dir=tmp_path, quiet=True)

    assert result.cli_quota_error_observed is True
    assert result.transport_fallback_not_applied_reason is not None
    assert result.provider_quota_reset_at is None


def test_applied_fallback_records_no_not_applied_reason(tmp_path: Path) -> None:
    """When the fallback does fire, there is no 'not applied' outcome to record."""
    profile = _codex_profile(api_fallback=_OPENAI_FALLBACK)
    api_success = AgentResult(
        success=True,
        output="done",
        session_id=None,
        cost_usd=0.02,
        exit_code=0,
        raw={},
        profile_name=profile.name,
    )
    from theforge.runners.cli import run_agent

    with (
        patch("theforge.runners.runner_codex._run_codex", return_value=_quota_failure(profile)),
        patch("theforge.runners.api.run_api_agent", return_value=api_success),
    ):
        result = run_agent(prompt="do work", profile=profile, working_dir=tmp_path, quiet=True)

    assert result.transport_fallback_fired is True
    assert result.transport_fallback_not_applied_reason is None
    assert result.provider_quota_reset_at is None


# ── The run stops rather than re-asking a provider that answered ──────


def _unrecoverable_quota_result() -> AgentResult:
    return AgentResult(
        success=False,
        output=_QUOTA_OUTPUT,
        session_id=None,
        cost_usd=0.0,
        exit_code=1,
        raw={},
        profile_name="dev",
        cli_quota_error_observed=True,
        transport_fallback_fired=False,
        transport_fallback_reason="matched 'usage limit'",
        transport_fallback_not_applied_reason=(
            "no transport fallback configured for provider 'openai'"
        ),
        provider_quota_reset_at="Aug 8th, 2026 7:59 AM",
        transport_used="cli",
    )


def test_unrecoverable_quota_failure_is_not_transient() -> None:
    from theforge.coordinator.dev_phase import _is_transient_dev_failure

    assert _is_transient_dev_failure(_unrecoverable_quota_result()) is False


def test_quota_failure_without_reset_time_stays_transient() -> None:
    """Where the failure carries no certainty, repeating it is still reasonable."""
    from theforge.coordinator.dev_phase import _is_transient_dev_failure

    result = replace(
        _unrecoverable_quota_result(),
        output="429 rate limit exceeded",
        provider_quota_reset_at=None,
    )

    assert _is_transient_dev_failure(result) is True


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
            base_branch="main",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def test_dev_phase_halts_instead_of_spending_remaining_iterations(tmp_path: Path) -> None:
    """The reported run spent two further iterations against a dead provider.

    With a stated reset time and no applicable fallback, the phase must end the
    story now — as an infrastructure abort, since no model judged anything —
    rather than return None and let the engine re-enter DEV.
    """
    from theforge.coordinator.dev_phase import _run_dev_phase

    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    spec = tmp_path / "specs" / "t.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# t\n", encoding="utf-8")
    state = CoordinatorState()
    state.adaptive_dev_max = 3
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)

    agent_result = _unrecoverable_quota_result()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.run_agent", return_value=agent_result)
        )
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock())
        )
        result = _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/x", notify=False, logger=None
        )

    # Not None → the engine loop ends the story instead of re-entering DEV with
    # two iterations still on the budget.
    assert result is not None
    assert result.success is False
    assert result.infrastructure_failure is True
    assert state.phase == Phase.ESCALATE
    # The audit says why, in the same terms a fired fallback would be reported.
    assert "Aug 8th, 2026 7:59 AM" in (state.error or "")
    assert "no transport fallback configured" in (state.error or "")
    assert state.infrastructure_failure is not None
    assert state.agent_invocation_failures
    _recorded = state.agent_invocation_failures[-1]
    assert _recorded["category"] == "transport"
    # Budget was never spent down to the limit.
    assert state.budget.remaining() > 0


def test_dev_phase_retries_when_the_provider_stated_no_reset_time(tmp_path: Path) -> None:
    """The halt is narrow: only a quota refusal certain to repeat ends the story."""
    from theforge.coordinator.dev_phase import _run_dev_phase

    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    spec = tmp_path / "specs" / "t.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# t\n", encoding="utf-8")
    state = CoordinatorState()
    state.adaptive_dev_max = 3
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)

    agent_result = replace(
        _unrecoverable_quota_result(),
        output="TIMEOUT: Agent exceeded 900s limit",
        failure_code="timeout",
        cli_quota_error_observed=False,
        transport_fallback_reason=None,
        transport_fallback_not_applied_reason=None,
        provider_quota_reset_at=None,
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.run_agent", return_value=agent_result)
        )
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock())
        )
        result = _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/x", notify=False, logger=None
        )

    assert result is None
    assert state.phase != Phase.ESCALATE
