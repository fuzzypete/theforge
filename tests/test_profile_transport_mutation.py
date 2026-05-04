"""Regression tests for model-swap transport consistency."""

from __future__ import annotations

import time
from pathlib import Path

from coord_test_helpers import _make_agent_result, _make_task

from theforge.config import (
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    MODEL_REGISTRY,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
    apply_model_info,
)
from theforge.coordinator import plan_flow, review_phase
from theforge.coordinator.preflight import _apply_complexity_adaptation
from theforge.coordinator.review_phase import _ReviewOutcome
from theforge.coordinator.state import CoordinatorState
from theforge.review import ReviewFinding, ReviewResult

PLAN_AGENT_APPROVE = """\
```yaml
verdict: APPROVE
findings: []
```
"""

PLAN_AGENT_REJECT_P0 = """\
```yaml
verdict: REJECT
findings:
  - severity: P0
    description: "Plan misses a required behavior"
    suggestion: "Cover the missing behavior"
```
"""


class _Logger:
    def _safe_emit(self, *_args, **_kwargs) -> None:
        return None


def _workspace(tmp_path: Path) -> WorkspaceConfig:
    return WorkspaceConfig(
        create_command="mkdir -p {slug}",
        path_pattern="{slug}",
        branch_pattern="forge/{slug}",
    )


def _profile_from(model_key: str, *, name: str = "dev") -> ModelProfile:
    info = MODEL_REGISTRY[model_key]
    return apply_model_info(
        ModelProfile(
            name=name,
            cli="claude",
            provider=None,
            model="sonnet",
            budget_usd=10.0,
            timeout_seconds=300,
            allowed_tools=(),
        ),
        info,
    )


def _config(
    tmp_path: Path,
    *,
    dev_profile: ModelProfile,
    models: list[str],
    plan: PlanConfig | None = None,
    retry: RetryPolicy | None = None,
    plan_agent_review: PlanAgentReviewConfig | None = None,
) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=_workspace(tmp_path),
        validation=DEFAULT_VALIDATION,
        dev_profile=dev_profile,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=retry or RetryPolicy(max_dev_iterations=2, max_review_cycles=3),
        log=LogConfig(enabled=False),
        models=models,
        plan=plan or PlanConfig(enabled=True),
        plan_agent_review=plan_agent_review or PlanAgentReviewConfig(enabled=False),
        review_pool_is_default=True,
        plan_model_is_default=True,
        dev_profile_is_default=True,
    )


def _assert_api_profile(profile: ModelProfile, *, provider: str, model: str) -> None:
    assert profile.cli is None
    assert profile.provider == provider
    assert profile.model == model
    assert profile.transport is not None
    assert profile.transport.kind == "api"


def _assert_cli_profile(profile: ModelProfile, *, cli: str, model: str) -> None:
    assert profile.cli == cli
    assert profile.provider is None
    assert profile.model == model
    assert profile.transport is not None
    assert profile.transport.kind == "cli"


def _p1_review() -> ReviewResult:
    finding = ReviewFinding(
        severity="P1",
        file="src/example.py",
        line=1,
        description="Persistent bug",
        suggestion="Fix it",
    )
    return ReviewResult(
        verdict="REQUEST_CHANGES",
        summary="needs changes",
        findings=[finding],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def test_preflight_complexity_adaptation_cli_to_api_dev_profile(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        dev_profile=_profile_from("claude/sonnet"),
        models=["claude/sonnet", "deepseek/deepseek-reasoner"],
    )

    updated = _apply_complexity_adaptation(config, "medium")

    _assert_api_profile(updated.dev_profile, provider="deepseek", model="deepseek-reasoner")


def test_preflight_complexity_adaptation_api_to_cli_dev_profile(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        dev_profile=_profile_from("deepseek/deepseek-chat"),
        models=["deepseek/deepseek-chat", "claude/opus"],
    )

    updated = _apply_complexity_adaptation(config, "large")

    _assert_cli_profile(updated.dev_profile, cli="claude", model="opus")


def test_preflight_complexity_adaptation_plan_uses_target_transport_fields(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        dev_profile=_profile_from("claude/sonnet"),
        models=["claude/sonnet", "openai-api/gpt-5.4-pro"],
        plan=PlanConfig(enabled=True, cli="claude", provider=None, model="sonnet"),
    )

    updated = _apply_complexity_adaptation(config, "medium")

    assert updated.plan.cli is None
    assert updated.plan.provider == "openai"
    assert updated.plan.model == "gpt-5.4-pro"


def test_review_phase_dev_escalation_cli_to_api_updates_transport(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(
        tmp_path,
        dev_profile=_profile_from("claude/sonnet"),
        models=["claude/sonnet", "deepseek/deepseek-reasoner"],
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=3,
            auto_model_escalation=True,
        ),
    )
    state = CoordinatorState()
    state.review_results.append(_p1_review())

    def fake_review_pool(*_args, **_kwargs):
        current = _p1_review()
        return [], [], current, [current], [("review", current)]

    monkeypatch.setattr(review_phase, "_run_review_pool", fake_review_pool)
    monkeypatch.setattr(
        "theforge.finding_classifier.update_finding_registry",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "theforge.review_finding_classifier.classify_families",
        lambda **_kwargs: ([], []),
    )

    outcome, result, updated = review_phase._run_review_phase(
        state,
        config,
        _make_task(tmp_path),
        "story",
        tmp_path,
        "forge/test",
        time.monotonic(),
        interactive=False,
        auto_merge=False,
        notify=False,
        logger=None,
    )

    assert outcome is _ReviewOutcome.RETRY_DEV
    assert result is None
    _assert_api_profile(updated.dev_profile, provider="deepseek", model="deepseek-reasoner")


def test_plan_review_escalation_cli_to_api_updates_regen_profile(
    tmp_path: Path, monkeypatch
) -> None:
    captured_profiles: list[ModelProfile] = []
    plan_reviewer_a = _profile_from("claude/sonnet", name="plan-review-a")
    plan_reviewer_b = _profile_from("claude/sonnet", name="plan-review-b")
    config = _config(
        tmp_path,
        dev_profile=_profile_from("claude/sonnet"),
        models=["claude/sonnet", "deepseek/deepseek-reasoner"],
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=3, plan_escalation_threshold=1),
        plan_agent_review=PlanAgentReviewConfig(
            enabled=True,
            pool=[plan_reviewer_a, plan_reviewer_b],
            min_reviewers=2,
        ),
    )
    state = CoordinatorState()
    state.log_dir = tmp_path / ".forge" / "logs"
    state.plan_output = "steps: []"
    state._explicit_roles = set()

    monkeypatch.setattr(
        plan_flow,
        "run_agent_pool",
        lambda **_kwargs: (
            [
                _make_agent_result(
                    success=True, output=PLAN_AGENT_REJECT_P0, profile_name="plan-review-a"
                ),
                _make_agent_result(
                    success=True, output=PLAN_AGENT_REJECT_P0, profile_name="plan-review-b"
                ),
            ]
            if not state.plan_escalated
            else [
                _make_agent_result(
                    success=True, output=PLAN_AGENT_APPROVE, profile_name="plan-review-a"
                ),
                _make_agent_result(
                    success=True, output=PLAN_AGENT_APPROVE, profile_name="plan-review-b"
                ),
            ]
        ),
    )

    def fake_run_agent(**kwargs):
        captured_profiles.append(kwargs["profile"])
        return _make_agent_result(success=True, output="steps: []", profile_name="plan")

    monkeypatch.setattr(plan_flow, "run_agent", fake_run_agent)
    monkeypatch.setattr(plan_flow, "save_sessions", lambda *_args, **_kwargs: None)

    result = plan_flow._run_plan_agent_review(
        state=state,
        config=config,
        task=_make_task(tmp_path),
        story_content="story",
        workspace_path=tmp_path,
        plan_profile=_profile_from("claude/sonnet", name="plan"),
        plan_text="steps: []",
        preflight_result=None,
        notify=False,
        logger=_Logger(),
    )

    assert result is None
    assert captured_profiles
    _assert_api_profile(captured_profiles[0], provider="deepseek", model="deepseek-reasoner")
