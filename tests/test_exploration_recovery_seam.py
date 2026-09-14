"""Seam coverage for failed-challenger recovery at the coordinator boundary (#325).

Drives the engine's recovery hook directly: when a story's dev slot ran an
exploration challenger and it fails, the coordinator must swap to the current
winner, record the failure as an *exploration* failure in the routing_decision
block (not the story's final outcome), and fire at most once.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.config import (
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator.engine import _coordinator_loop, _maybe_recover_failed_challenger
from theforge.coordinator.review_phase import _ReviewOutcome
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.coordinator.validate_phase import _ValidateOutcome
from theforge.task import TaskStory


def _config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}", path_pattern="{slug}", branch_pattern="forge/{slug}"
        ),
        validation=ValidationConfig(gate_command="make gate"),
        dev_profile=ModelProfile(
            name="dev",
            cli="claude",
            provider=None,
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=600,
            allowed_tools=("Read",),
            phase="dev",
        ),
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


def _winner_profile() -> ModelProfile:
    return ModelProfile(
        name="dev",
        cli="claude",
        provider=None,
        model="opus",
        budget_usd=5.0,
        timeout_seconds=900,
        allowed_tools=("Read",),
        phase="dev",
    )


def _state_with_active_challenger() -> CoordinatorState:
    state = CoordinatorState()
    state.exploration_challenger = {
        "routing_key": "dev:large:-",
        "challenger": "haiku",
        "winner": "opus",
        "pool": ["opus", "haiku"],
    }
    state.exploration_winner_dev_profile = _winner_profile()
    state.routing_decision = {
        "dev": {"exploration": {"mode": "challenger", "selected": "haiku", "winner": "opus"}}
    }
    return state


def _state_ready_for_coordinator_loop(tmp_path: Path) -> CoordinatorState:
    state = _state_with_active_challenger()
    state.workspace_path = tmp_path
    state.branch_name = "feat/test"
    # Keep the test focused on the DEV → VALIDATE handoff rather than adaptive
    # iteration derivation.
    state.adaptive_dev_max = 2
    state.adaptive_review_max = 2
    state.adaptive_dev_timeout_seconds = 600
    state.adaptive_dev_cost_estimate_usd = 1.0
    return state


def _noop(_msg: str) -> None:
    pass


def test_recovery_swaps_to_winner_and_records_failure(tmp_path):
    state = _state_with_active_challenger()
    state.error = "challenger terminal error"
    state.error_type = "gate_failure"
    state.escalate_reason = "challenger escalation"
    state.dev_session_id = "challenger-session"
    config = _config(tmp_path)
    new_config = _maybe_recover_failed_challenger(state, config, _noop, None)

    assert new_config is not None
    # Retried through the winner's dev profile.
    assert new_config.dev_profile.model == "opus"
    assert state.exploration_recovered is True
    # The challenger failure is recorded in the audit view, not silently dropped.
    block = state.routing_decision["dev"]["exploration"]
    assert block["challenger_failed"] is True
    assert block["recovery"]["kind"] == "exploration_failure"
    assert block["recovery"]["challenger"] == "haiku"
    assert block["recovery"]["recovered_via"] == "winner"
    assert block["recovery"]["terminal_error"] == {
        "message": "challenger terminal error",
        "type": "gate_failure",
        "escalate_reason": "challenger escalation",
    }
    # The exploration record owns the challenger's failure; the story must
    # start the winner retry without a terminal error or resumable challenger
    # session attached to it.
    assert state.error is None
    assert state.error_type is None
    assert state.escalate_reason is None
    assert state.dev_session_id is None


def test_recovery_fires_at_most_once(tmp_path):
    state = _state_with_active_challenger()
    config = _config(tmp_path)
    assert _maybe_recover_failed_challenger(state, config, _noop, None) is not None
    # Second failure does not recover again (would loop otherwise).
    assert _maybe_recover_failed_challenger(state, config, _noop, None) is None


def test_no_recovery_when_no_challenger(tmp_path):
    state = CoordinatorState()  # winner-mode run, nothing to recover
    assert _maybe_recover_failed_challenger(state, _config(tmp_path), _noop, None) is None


def test_validate_escalation_recovers_challenger_through_winner(tmp_path: Path) -> None:
    """A terminal gate failure retries DEV once with the exploration winner."""
    state = _state_ready_for_coordinator_loop(tmp_path)
    config = _config(tmp_path)
    task = TaskStory(name="test", slug="test", story_path=tmp_path / "story.md")
    challenger_failure = CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message="iteration_exhaustion",
    )
    winner_failure = CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message="winner_attempt_failed",
    )
    dispatched_models: list[str] = []

    def _dev_attempt(_state, current_config, *_args, **_kwargs):
        dispatched_models.append(current_config.dev_profile.model)
        return None if len(dispatched_models) == 1 else winner_failure

    with (
        patch("theforge.coordinator.engine._run_dev_phase", side_effect=_dev_attempt),
        patch(
            "theforge.coordinator.engine._run_validate_phase",
            return_value=(_ValidateOutcome.ESCALATE, challenger_failure),
        ) as validate,
        patch("theforge.coordinator.engine._scrub_forge_history"),
        patch("theforge.coordinator.gate_green_salvage.salvage_gate_green_landing") as salvage,
    ):
        result = _coordinator_loop(state, config, task, "story", task_start=0.0)

    assert result is winner_failure
    assert dispatched_models == ["haiku", "opus"]
    validate.assert_called_once()
    salvage.assert_not_called()
    exploration = state.routing_decision["dev"]["exploration"]
    assert exploration["challenger_failed"] is True
    assert exploration["recovery"]["recovered_via"] == "winner"


def test_validate_recovery_winner_success_has_no_challenger_terminal_error(
    tmp_path: Path,
) -> None:
    """A winner success does not retain the challenger's terminal outcome."""
    state = _state_ready_for_coordinator_loop(tmp_path)
    state.dev_session_id = "challenger-session"
    config = _config(tmp_path)
    task = TaskStory(name="test", slug="test", story_path=tmp_path / "story.md")
    challenger_failure = CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message="challenger terminal gate error",
    )
    winner_success = CoordinatorResult(
        success=True,
        phase=Phase.DONE,
        state=state,
        message="winner approved",
    )
    dispatched_models: list[str] = []
    dispatched_sessions: list[str | None] = []

    def _dev_attempt(current_state, current_config, *_args, **_kwargs):
        dispatched_models.append(current_config.dev_profile.model)
        dispatched_sessions.append(current_state.dev_session_id)
        return None

    def _validate_attempt(current_state, *_args, **_kwargs):
        if len(dispatched_models) == 1:
            current_state.phase = Phase.ESCALATE
            current_state.error = challenger_failure.message
            current_state.error_type = "gate_failure"
            current_state.escalate_reason = "challenger escalation"
            return _ValidateOutcome.ESCALATE, challenger_failure
        return _ValidateOutcome.PASS, None

    with (
        patch("theforge.coordinator.engine._run_dev_phase", side_effect=_dev_attempt),
        patch("theforge.coordinator.engine._run_validate_phase", side_effect=_validate_attempt),
        patch(
            "theforge.coordinator.engine._run_review_phase",
            return_value=(
                _ReviewOutcome.DONE,
                winner_success,
                config,
            ),
        ),
        patch("theforge.coordinator.engine._scrub_forge_history"),
    ):
        result = _coordinator_loop(state, config, task, "story", task_start=0.0)

    assert result is winner_success
    assert dispatched_models == ["haiku", "opus"]
    assert dispatched_sessions == ["challenger-session", None]
    assert state.error is None
    assert state.error_type is None
    assert state.escalate_reason is None


def test_validate_escalation_salvages_after_challenger_recovery_is_spent(
    tmp_path: Path,
) -> None:
    """The next terminal VALIDATE escalation still reaches gate-green salvage."""
    state = _state_ready_for_coordinator_loop(tmp_path)
    config = _config(tmp_path)
    task = TaskStory(name="test", slug="test", story_path=tmp_path / "story.md")
    challenger_failure = CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message="challenger terminal gate error",
    )
    winner_failure = CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message="winner terminal gate error",
    )
    dispatched_models: list[str] = []

    def _dev_attempt(_state, current_config, *_args, **_kwargs):
        dispatched_models.append(current_config.dev_profile.model)
        return None

    def _validate_attempt(current_state, *_args, **_kwargs):
        if len(dispatched_models) == 1:
            current_state.phase = Phase.ESCALATE
            current_state.error = challenger_failure.message
            return _ValidateOutcome.ESCALATE, challenger_failure
        current_state.phase = Phase.ESCALATE
        current_state.error = winner_failure.message
        return _ValidateOutcome.ESCALATE, winner_failure

    with (
        patch("theforge.coordinator.engine._run_dev_phase", side_effect=_dev_attempt),
        patch("theforge.coordinator.engine._run_validate_phase", side_effect=_validate_attempt),
        patch("theforge.coordinator.engine._scrub_forge_history"),
        patch(
            "theforge.coordinator.gate_green_salvage.salvage_gate_green_landing",
            side_effect=lambda *_args, **_kwargs: winner_failure,
        ) as salvage,
    ):
        result = _coordinator_loop(state, config, task, "story", task_start=0.0)

    assert result is winner_failure
    assert dispatched_models == ["haiku", "opus"]
    salvage.assert_called_once()
    assert salvage.call_args.args[1].dev_profile.model == "opus"
