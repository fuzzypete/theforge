"""Seam coverage for failed-challenger recovery at the coordinator boundary (#325).

Drives the engine's recovery hook directly: when a story's dev slot ran an
exploration challenger and it fails, the coordinator must swap to the current
winner, record the failure as an *exploration* failure in the routing_decision
block (not the story's final outcome), and fire at most once.
"""

from __future__ import annotations

from pathlib import Path

from theforge.config import (
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    ModelProfile,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator.engine import _maybe_recover_failed_challenger
from theforge.coordinator.state import CoordinatorState


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


def _noop(_msg: str) -> None:
    pass


def test_recovery_swaps_to_winner_and_records_failure(tmp_path):
    state = _state_with_active_challenger()
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


def test_recovery_fires_at_most_once(tmp_path):
    state = _state_with_active_challenger()
    config = _config(tmp_path)
    assert _maybe_recover_failed_challenger(state, config, _noop, None) is not None
    # Second failure does not recover again (would loop otherwise).
    assert _maybe_recover_failed_challenger(state, config, _noop, None) is None


def test_no_recovery_when_no_challenger(tmp_path):
    state = CoordinatorState()  # winner-mode run, nothing to recover
    assert _maybe_recover_failed_challenger(state, _config(tmp_path), _noop, None) is None
