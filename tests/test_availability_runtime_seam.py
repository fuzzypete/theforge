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
    fallback = replace(base.dev_profile, name="preflight-fallback", model="haiku")
    config = replace(base, preflight_fallback_profile=fallback)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(exist_ok=True)
    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

    answers = {}
    for profile, state in (
        (config.preflight_profile, MODEL_AVAILABILITY_UNAVAILABLE),
        (fallback, MODEL_AVAILABILITY_AVAILABLE),
    ):
        key = profile_dispatch_key(profile, config)
        if key is not None:
            answers[key] = _answer(state)

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
