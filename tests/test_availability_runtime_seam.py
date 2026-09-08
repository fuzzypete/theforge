"""Runtime seams for the availability routing stop (#2950).

The pure router and the launch gate are covered elsewhere. These tests drive
the paths where money is actually committed:

- the live coordinator path, where the preflight agent is the story's first
  paid call and runs *before* routing could exclude anything;
- the cached-preflight path, where routing runs with no preflight spend at all;
- the sprint scheduler, which must read a routing stop as a non-failure that
  ends the run rather than as a story that failed.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from coord_test_helpers import (  # noqa: E402
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.assignment import ROUTING_STOPPED_ERROR_TYPE  # noqa: E402
from theforge.config.model_identity import (  # noqa: E402
    AVAILABILITY_FRESHNESS_CURRENT,
    MODEL_AVAILABILITY_AVAILABLE,
    MODEL_AVAILABILITY_UNAVAILABLE,
    ModelAvailability,
)
from theforge.coordinator.engine import run_from_review, run_task  # noqa: E402
from theforge.coordinator.preflight_cache import _story_content_hash  # noqa: E402
from theforge.coordinator.state import CoordinatorState  # noqa: E402
from theforge.model_availability import (  # noqa: E402
    StoryAvailability,
    profile_dispatch_key,
)

CHECKED_AT = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
_STORY = "# Test Spec\n\nImplement the thing."


@pytest.fixture(autouse=True)
def _mock_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def _answer(state: str) -> ModelAvailability:
    return ModelAvailability(
        state,
        "ChatGPT-account auth",
        CHECKED_AT,
        "not in account catalog" if state == MODEL_AVAILABILITY_UNAVAILABLE else None,
        AVAILABILITY_FRESHNESS_CURRENT,
    )


def _all_unavailable(config) -> StoryAvailability:
    """Every configured identity resolves unavailable."""
    answers = {}
    profiles = [config.preflight_profile, config.dev_profile, *config.review_pool]
    profiles += [a.to_model_profile(allowed_tools=()) for a in config.agents]
    for profile in profiles:
        key = profile_dispatch_key(profile, config)
        if key is not None:
            answers[key] = _answer(MODEL_AVAILABILITY_UNAVAILABLE)
    return StoryAvailability(answers)


def _cached_proceed_state() -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    state.preflight_reason = "ok"
    state.preflight_complexity = "medium"
    state.preflight_complexity_score = 5
    state.preflight_sufficiency = "implementation_ready"
    state.preflight_work_type = "feature"
    state.preflight_cache_snapshot = {
        "worktree_head": "OK",
        "evaluation_base_branch": "main",
        "evaluation_base_branch_head": "OK",
        "story_content_hash": _story_content_hash(_STORY),
    }
    return state


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_the_preflight_agent_is_never_invoked_when_it_cannot_be_invoked(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """The story's first paid call is gated, so the stop really does cost $0.00.

    Preflight runs ahead of routing, so an exclusion applied at assignment time
    would arrive after the invoice. This asserts the agent is not called at all.
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(exist_ok=True)
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

    with patch(
        "theforge.coordinator.preflight.resolve_story_availability",
        return_value=_all_unavailable(config),
    ):
        result = run_task(config, task)

    assert mock_preflight.call_count == 0, "an unavailable preflight model must not be dispatched"
    assert mock_dev.call_count == 0
    assert result.success is False
    assert result.infrastructure_failure is True
    assert result.state.error_type == ROUTING_STOPPED_ERROR_TYPE
    assert "no model available for phase preflight" in result.message
    assert "$0.00 spent" in result.message
    assert result.state.total_cost_measured == 0.0


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_preflight_reseats_onto_the_configured_fallback_rather_than_stopping(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """A configured fallback the account CAN invoke is used, not refused."""
    base = _make_config(tmp_path)
    # A preflight primary on its own dispatch identity, so marking it
    # unavailable says nothing about the models the later phases use — the
    # point under test is preflight's own reseat, not a story-wide stop.
    primary = replace(
        base.preflight_profile,
        name="preflight-primary",
        cli=None,
        provider="openai",
        model="gpt-5",
    )
    fallback = replace(base.dev_profile, name="preflight-fallback", model="haiku")
    config = replace(base, preflight_profile=primary, preflight_fallback_profile=fallback)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(exist_ok=True)
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

    answers = {}
    for profile, state in (
        (primary, MODEL_AVAILABILITY_UNAVAILABLE),
        (fallback, MODEL_AVAILABILITY_AVAILABLE),
        (config.dev_profile, MODEL_AVAILABILITY_AVAILABLE),
        *[(p, MODEL_AVAILABILITY_AVAILABLE) for p in config.review_pool],
    ):
        key = profile_dispatch_key(profile, config)
        if key is not None:
            answers.setdefault(key, _answer(state))

    mock_preflight.side_effect = RuntimeError("stop here — the profile choice is what matters")
    with patch(
        "theforge.coordinator.preflight.resolve_story_availability",
        return_value=StoryAvailability(answers),
    ):
        with pytest.raises(RuntimeError):
            run_task(config, task)

    assert mock_preflight.call_count == 1, "exactly one dispatch, on the fallback"
    dispatched = mock_preflight.call_args.kwargs["profile"]
    assert dispatched.model == "haiku", "the available fallback runs instead of a refusal"


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_the_cached_preflight_path_stops_with_no_spend_and_no_model_writes(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """A cached verdict skips preflight entirely, so its stop is genuinely free."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(exist_ok=True)
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_dev.return_value = _make_agent_result(success=True, output="implemented")

    with patch(
        "theforge.coordinator.preflight.resolve_story_availability",
        return_value=_all_unavailable(config),
    ):
        result = run_task(config, task, cached_preflight_state=_cached_proceed_state())

    assert mock_preflight.call_count == 0
    assert mock_dev.call_count == 0
    assert result.success is False
    assert result.infrastructure_failure is True
    assert result.state.error_type == ROUTING_STOPPED_ERROR_TYPE
    assert "$0.00 spent" in result.message
    forge_dir = tmp_path / ".forge"
    assert not (forge_dir / "model_profiles.yaml").exists()
    assert not (forge_dir / "model_capabilities.yaml").exists()


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_the_resume_path_stops_rather_than_seating_the_static_roster(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """A resumed story re-derives its routing, and reaches the same refusal.

    ``restore_routing_decision`` runs when the scheduler lost this story's
    preflight state. It re-applies routing, so it reaches the availability gate
    too — and must stop the same way rather than letting the refusal escape as a
    generic failure or seating the configured roster unchecked.
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(exist_ok=True)
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

    from theforge.assignment import NoAvailableModelError

    refusal = NoAvailableModelError(
        "code_review",
        {
            "identity:opus": {
                "reason": "model_unavailable",
                "label": "opus",
                "detail": {
                    "auth_mode": "ChatGPT-account auth",
                    "reason": "not in account catalog",
                },
            }
        },
    )
    with patch(
        "theforge.coordinator.preflight.restore_routing_decision", side_effect=refusal
    ) as restore:
        result = run_from_review(config, task, workspace)

    assert restore.call_count == 1, "the resume path is the one under test"
    assert result.success is False
    assert result.infrastructure_failure is True
    assert result.state.error_type == ROUTING_STOPPED_ERROR_TYPE
    assert "no model available for phase code_review" in result.message
    assert mock_pool.call_count == 0, "no reviewer is dispatched after the stop"


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_a_phase_emptied_by_availability_plus_capability_stops_before_preflight(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """Availability alone was not enough to see this phase was already dead.

    One reviewer is unavailable, the other is ruled out by the capability
    record. Neither rule empties code_review on its own, so the exhaustion was
    previously discovered at assignment time — after preflight had been charged
    for a story that could never reach review (#2950 review).
    """
    from theforge.config import AgentDef, AssignmentConfig
    from theforge.model_capabilities import CAPABILITY_TOOL_STRUCTURED, OUTCOME_ABSENT

    agents = [
        AgentDef(
            name="reachable",
            provider="anthropic",
            model="sonnet",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="mid",
        ),
        AgentDef(
            name="unreachable",
            provider="anthropic",
            model="opus",
            budget_usd=8.0,
            timeout_seconds=1200,
            tier="strong",
        ),
    ]
    config = replace(
        _make_config(tmp_path),
        agents=agents,
        assignment=AssignmentConfig(enabled=True, max_cost_per_story_usd=50.0),
        models=None,
        review_pool_is_default=True,
        plan_model_is_default=True,
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(exist_ok=True)
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

    # sonnet: reachable but demonstrated unable to produce structured output.
    capabilities = {
        "version": 1,
        "identities": {
            "anthropic/sonnet/api": {
                "provider": "anthropic",
                "model": "sonnet",
                "transport": "api",
                "capabilities": {
                    CAPABILITY_TOOL_STRUCTURED: {
                        "outcome": OUTCOME_ABSENT,
                        "established_at": "2026-09-01T00:00:00Z",
                        "subject_signature": "",
                        "detail": "returned prose",
                        "probe_role": "agent-code-review",
                    }
                },
            }
        },
    }
    # opus: capable, but the account cannot invoke it.
    answers = {}
    for profile in [a.to_model_profile(allowed_tools=()) for a in agents]:
        key = profile_dispatch_key(profile, config)
        if key is not None:
            answers[key] = _answer(
                MODEL_AVAILABILITY_UNAVAILABLE
                if profile.model == "opus"
                else MODEL_AVAILABILITY_AVAILABLE
            )
    preflight_key = profile_dispatch_key(config.preflight_profile, config)
    if preflight_key is not None:
        answers.setdefault(preflight_key, _answer(MODEL_AVAILABILITY_AVAILABLE))

    with patch(
        "theforge.coordinator.preflight.resolve_story_availability",
        return_value=StoryAvailability(answers),
    ):
        with patch("theforge.model_capabilities.load_capabilities", return_value=capabilities):
            result = run_task(config, task)

    assert mock_preflight.call_count == 0, (
        "the story was already unroutable; preflight must not be charged to find out"
    )
    assert result.state.error_type == ROUTING_STOPPED_ERROR_TYPE
    assert "$0.00 spent" in result.message
    assert result.state.total_cost_measured == 0.0
    # Both rules are named, not just the account answer.
    assert "not available to this account" in result.message
    assert "demonstrated absent" in result.message


def _pin_config(tmp_path, *, pinned_dev):
    """An adaptive config whose dev is pinned outside the agents pool."""
    from theforge.config import AgentDef, AssignmentConfig

    return replace(
        _make_config(tmp_path),
        agents=[
            AgentDef(
                name="pool-sonnet",
                provider="anthropic",
                model="sonnet",
                budget_usd=5.0,
                timeout_seconds=900,
                tier="mid",
            )
        ],
        assignment=AssignmentConfig(enabled=True, max_cost_per_story_usd=50.0),
        models=None,
        dev_profile=pinned_dev,
        review_pool_is_default=True,
        plan_model_is_default=True,
    )


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_an_unavailable_off_pool_dev_pin_stops_before_preflight_is_charged(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """The pin is checked where the spending decision is made, not after it.

    A dev pinned to an identity the agents pool does not contain was checked
    only at the routing boundary, which the live path reaches after preflight
    has run and been charged (#2950 review).
    """
    from theforge.config import ModelProfile

    pinned = ModelProfile(
        name="pinned-dev",
        provider="openai",
        model="gpt-5",
        budget_usd=6.0,
        timeout_seconds=900,
        allowed_tools=(),
    )
    config = _pin_config(tmp_path, pinned_dev=pinned)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(exist_ok=True)
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

    answers = {profile_dispatch_key(pinned, config): _answer(MODEL_AVAILABILITY_UNAVAILABLE)}
    for profile in [
        config.preflight_profile,
        *[a.to_model_profile(allowed_tools=()) for a in config.agents],
        *config.review_pool,
    ]:
        key = profile_dispatch_key(profile, config)
        if key is not None:
            answers.setdefault(key, _answer(MODEL_AVAILABILITY_AVAILABLE))

    with patch(
        "theforge.coordinator.preflight.resolve_story_availability",
        return_value=StoryAvailability(answers),
    ):
        result = run_task(config, task)

    assert mock_preflight.call_count == 0, "the pin was already known unroutable"
    assert result.state.error_type == ROUTING_STOPPED_ERROR_TYPE
    assert "phase dev" in result.message
    assert "gpt-5" in result.message
    assert "$0.00 spent" in result.message


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_an_unavailable_adaptive_pool_does_not_stop_a_fully_pinned_story(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """The pool is only a phase's candidate set when the phase draws from it.

    Judging every role against config.agents stopped a story whose configured
    preflight, dev and reviewers were all reachable, because the registry it
    never consults was not (#2950 review).
    """
    from theforge.config import ModelProfile

    pinned = ModelProfile(
        name="pinned-dev",
        provider="openai",
        model="gpt-5",
        budget_usd=6.0,
        timeout_seconds=900,
        allowed_tools=(),
    )
    config = replace(
        _pin_config(tmp_path, pinned_dev=pinned),
        review_pool_is_default=False,
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(exist_ok=True)
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

    # The whole adaptive registry is unreachable; every configured phase profile
    # is fine. review_pool is pinned in this config, so nothing draws on it.
    answers = {}
    for agent in config.agents:
        key = profile_dispatch_key(agent.to_model_profile(allowed_tools=()), config)
        if key is not None:
            answers[key] = _answer(MODEL_AVAILABILITY_UNAVAILABLE)
    for profile in [config.preflight_profile, pinned, *config.review_pool]:
        key = profile_dispatch_key(profile, config)
        if key is not None:
            answers[key] = _answer(MODEL_AVAILABILITY_AVAILABLE)

    mock_preflight.side_effect = RuntimeError("reached dispatch — the story was not stopped")
    with patch(
        "theforge.coordinator.preflight.resolve_story_availability",
        return_value=StoryAvailability(answers),
    ):
        with pytest.raises(RuntimeError):
            run_task(config, task)

    assert mock_preflight.call_count == 1, "every phase this story executes is reachable"


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_a_post_plan_refusal_surfaces_as_a_plan_phase_routing_stop(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """The engine converts the checkpoint's refusal, not just the earlier ones.

    The checkpoint runs deep inside plan-review handling; without the catch
    around _run_plan_phase its refusal escapes as a generic ValueError-shaped
    failure instead of the routing-stop contract.
    """
    from theforge.assignment import NoAvailableModelError
    from theforge.coordinator.state import Phase

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(exist_ok=True)
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

    refusal = NoAvailableModelError(
        "dev",
        {
            "identity:opus": {
                "reason": "model_unavailable",
                "label": "opus",
                "detail": {
                    "auth_mode": "ChatGPT-account auth",
                    "reason": "not in account catalog",
                },
            }
        },
    )
    with patch(
        "theforge.coordinator.plan_flow._run_plan_phase", side_effect=refusal
    ) as plan_phase:
        with patch(
            "theforge.coordinator.preflight.resolve_story_availability",
            return_value=StoryAvailability({}),
        ):
            mock_preflight.return_value = _make_agent_result(
                success=True,
                output=(
                    "verdict: PROCEED\nreason: ok\ncomplexity: medium\n"
                    "complexity_score: 5\nsufficiency: implementation_ready\n"
                ),
                cost_usd=0.05,
            )
            result = run_task(config, task)

    assert plan_phase.call_count == 1, "the refusal has to come from the plan phase"
    assert result.success is False
    assert result.infrastructure_failure is True
    assert result.phase == Phase.PLAN
    assert result.state.error_type == ROUTING_STOPPED_ERROR_TYPE
    assert "no model available for phase dev" in result.message
    # Preflight already ran, so the stop reports what the story actually cost.
    assert "$0.00 spent" not in result.message
