"""Tests for cooperative cancellation via stop_event.

When the sprint scheduler's per-story timeout fires, it sets a per-worker
threading.Event. The coordinator must:
  - check the event at every phase boundary in _coordinator_loop
  - raise StoryCancelled instead of starting the next phase
  - bypass _record_run_memory() so no conflicting escalation record is written
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.cancellation import StoryCancelled
from theforge.coordinator.engine import _coordinator_loop, run_task
from theforge.coordinator.state import CoordinatorState


def _seeded_state(tmp_path: Path) -> CoordinatorState:
    state = CoordinatorState()
    state.workspace_path = tmp_path
    state.branch_name = "feat/test"
    # Skip adaptive resolution by pre-populating
    state.adaptive_dev_max = 2
    state.adaptive_review_max = 2
    state.adaptive_dev_timeout_seconds = 600
    state.adaptive_dev_cost_estimate_usd = 1.0
    return state


def test_pre_set_stop_event_raises_story_cancelled_before_any_phase(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = _seeded_state(tmp_path)
    stop = threading.Event()
    stop.set()

    with (
        patch("theforge.coordinator.engine._run_dev_phase") as mock_dev,
        patch("theforge.coordinator.engine._run_validate_phase") as mock_val,
        patch("theforge.coordinator.engine._run_review_phase") as mock_rev,
    ):
        with pytest.raises(StoryCancelled):
            _coordinator_loop(state, config, task, "story", task_start=0.0, stop_event=stop)

    mock_dev.assert_not_called()
    mock_val.assert_not_called()
    mock_rev.assert_not_called()


def test_stop_event_set_after_dev_skips_validate_and_review(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = _seeded_state(tmp_path)
    stop = threading.Event()

    def _dev_then_set(*args, **kwargs):
        # Simulate timeout firing during DEV
        stop.set()
        return None  # no escalation

    with (
        patch("theforge.coordinator.engine._run_dev_phase", side_effect=_dev_then_set) as mock_dev,
        patch("theforge.coordinator.engine._run_validate_phase") as mock_val,
        patch("theforge.coordinator.engine._run_review_phase") as mock_rev,
        patch("theforge.coordinator.engine._scrub_forge_history"),
    ):
        with pytest.raises(StoryCancelled):
            _coordinator_loop(state, config, task, "story", task_start=0.0, stop_event=stop)

    mock_dev.assert_called_once()
    mock_val.assert_not_called()
    mock_rev.assert_not_called()


def test_run_task_bypasses_record_run_memory_on_cancellation(tmp_path: Path) -> None:
    """When _coordinator_loop raises StoryCancelled, run_task must NOT write
    the end-of-run escalation record. The sprint scheduler's synthetic timeout
    record is the only artifact for cancelled stories.
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "wt"
    workspace.mkdir()

    stop = threading.Event()

    def _raise_cancelled(*args, **kwargs):
        raise StoryCancelled()

    with (
        patch(
            "theforge.coordinator.workspace._create_workspace",
            return_value=(workspace, "feat/test", None),
        ),
        patch(
            "theforge.coordinator.engine._create_workspace",
            return_value=(workspace, "feat/test", None),
        ),
        patch("theforge.coordinator.engine._run_preflight_phase", create=True),
        patch("theforge.coordinator.preflight_flow._run_preflight_phase") as mock_preflight,
        patch("theforge.coordinator.plan_flow._run_plan_phase", return_value=None),
        patch("theforge.coordinator.engine._coordinator_loop", side_effect=_raise_cancelled),
        patch("theforge.coordinator.engine._record_run_memory") as mock_record,
        patch("theforge.coordinator.engine._fire_post_run_hook"),
    ):
        # preflight returns (config, None, False) → continue to plan/loop
        mock_preflight.return_value = (config, None, False)
        # set the event so even early checks would trigger
        stop.set()
        result = run_task(config, task, stop_event=stop)

    assert result.success is False
    assert result.message == "Story cancelled by sprint timeout"
    # The critical assertion: no escalation record was written
    mock_record.assert_not_called()
