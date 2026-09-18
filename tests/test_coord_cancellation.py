"""Tests for cooperative cancellation via stop_event.

When the sprint scheduler's per-story timeout fires, it sets a per-worker
threading.Event. The coordinator must:
  - check the event at every phase boundary in _coordinator_loop
  - raise StoryCancelled instead of starting the next phase
  - bypass _record_run_memory() so no conflicting escalation record is written
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.cancellation import (
    BUDGET_CANCEL_ERROR_TYPE,
    PRESERVATION_FAILED_ERROR_TYPE,
    StopSignal,
    StoryCancelled,
)
from theforge.coordinator.commit_guard import CHECKPOINT_COMMIT_SUBJECT
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


# ── Cancellation must not strand a dev iteration's work (#3059) ───────


def _init_story_repo(path: Path) -> str:
    """A repo on the story branch with a seeded base commit; returns the base sha."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "Makefile").write_text("test:\n\techo base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "-b", "feat/test"], cwd=path, check=True)
    return base


def _commits_ahead(path: Path, base: str) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(out or "0")


def _porcelain(path: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


def _last_commit_subject(path: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_budget_cap_after_successful_dev_commits_the_iterations_work(tmp_path: Path) -> None:
    """The reported failure: the sprint budget cap halts the story in the window
    between a successful DEV return and VALIDATE. VALIDATE — which owns the
    post-gate sweep commit — never runs, so the coordinator must preserve the dev
    iteration's tracked edits AND new untracked files itself before the worktree
    is preserved for re-entry."""
    base = _init_story_repo(tmp_path)
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = _seeded_state(tmp_path)
    stop = StopSignal()

    def _dev_writes_then_budget_halt(*_args, **_kwargs):
        # Two tracked edits and two new untracked files — the shape of the hdp
        # story 242 iteration that was stranded.
        (tmp_path / "Makefile").write_text("test:\n\techo run_tests.py\n")
        (tmp_path / "run_tests.py").write_text("print('run')\n")
        (tmp_path / "test_run_tests.py").write_text("def test_x():\n    assert True\n")
        # The cost report for this successful iteration tips the sprint over cap.
        stop.stop("Sprint budget exhausted", error_type=BUDGET_CANCEL_ERROR_TYPE)
        return None  # successful dev: no escalation

    with (
        patch(
            "theforge.coordinator.engine._run_dev_phase",
            side_effect=_dev_writes_then_budget_halt,
        ) as mock_dev,
        patch("theforge.coordinator.engine._run_validate_phase") as mock_val,
        patch("theforge.coordinator.engine._run_review_phase") as mock_rev,
        patch("theforge.coordinator.engine._scrub_forge_history"),
    ):
        with pytest.raises(StoryCancelled):
            _coordinator_loop(state, config, task, "story", task_start=0.0, stop_event=stop)

    mock_dev.assert_called_once()
    mock_val.assert_not_called()
    mock_rev.assert_not_called()
    # The work is on the branch, and nothing is left as working-tree state.
    assert _commits_ahead(tmp_path, base) > 0
    assert _porcelain(tmp_path) == ""
    assert _last_commit_subject(tmp_path) == CHECKPOINT_COMMIT_SUBJECT
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "Makefile" in committed
    assert "run_tests.py" in committed
    assert "test_run_tests.py" in committed


def test_stop_before_any_dev_attempt_does_not_commit_foreign_dirty_state(tmp_path: Path) -> None:
    """A stop that lands before the first DEV attempt has no dev output to
    preserve, so whatever the workspace happens to be carrying must be left
    exactly as it is."""
    base = _init_story_repo(tmp_path)
    (tmp_path / "foreign.txt").write_text("not this story's work\n")
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = _seeded_state(tmp_path)
    stop = StopSignal()
    stop.stop("Sprint budget exhausted", error_type=BUDGET_CANCEL_ERROR_TYPE)

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
    assert _commits_ahead(tmp_path, base) == 0
    assert "foreign.txt" in _porcelain(tmp_path)


def test_failed_preservation_is_reported_not_silently_swallowed(tmp_path: Path) -> None:
    """When the worktree is confirmed dirty but the checkpoint commit does not
    happen (git or repository failure), the stranded work must be visible in the
    run log and the event stream — not indistinguishable from 'nothing to
    commit'."""
    _init_story_repo(tmp_path)
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = _seeded_state(tmp_path)
    stop = StopSignal()
    logger = MagicMock()
    logged: list[str] = []

    def _dev_writes_then_halt(*_args, **_kwargs):
        (tmp_path / "run_tests.py").write_text("print('run')\n")
        stop.stop("Sprint budget exhausted", error_type=BUDGET_CANCEL_ERROR_TYPE)
        return None

    with (
        patch("theforge.coordinator.engine._run_dev_phase", side_effect=_dev_writes_then_halt),
        patch("theforge.coordinator.engine._run_validate_phase"),
        patch("theforge.coordinator.engine._run_review_phase"),
        patch("theforge.coordinator.engine._scrub_forge_history"),
        patch("theforge.coordinator.commit_guard._checkpoint_commit", return_value=False),
        patch("theforge.coordinator.engine._log", side_effect=logged.append),
    ):
        with pytest.raises(StoryCancelled):
            _coordinator_loop(
                state, config, task, "story", task_start=0.0, stop_event=stop, logger=logger
            )

    assert any("FAILED to checkpoint-commit" in line for line in logged), logged
    assert any(str(tmp_path) in line for line in logged), logged
    events = [call.args[0] for call in logger._safe_emit.call_args_list if call.args]
    assert "dev_checkpoint_commit_failed" in events
    assert "dev_checkpoint_commit" not in events
    # Recorded on state, which is what makes the cancellation fail closed below.
    assert state.dev_output_preservation_failure is not None
    assert str(tmp_path) in state.dev_output_preservation_failure


def test_operator_stop_after_successful_dev_also_commits_the_work(tmp_path: Path) -> None:
    """The budget cap and an operator stop pull the same lever with different
    wording, so the preservation must not be specific to the budget reason."""
    base = _init_story_repo(tmp_path)
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = _seeded_state(tmp_path)
    stop = StopSignal()  # plain set() → the historical operator/timeout wording

    def _dev_writes_then_operator_stop(*_args, **_kwargs):
        (tmp_path / "run_tests.py").write_text("print('run')\n")
        stop.set()
        return None

    with (
        patch(
            "theforge.coordinator.engine._run_dev_phase",
            side_effect=_dev_writes_then_operator_stop,
        ),
        patch("theforge.coordinator.engine._run_validate_phase") as mock_val,
        patch("theforge.coordinator.engine._run_review_phase"),
        patch("theforge.coordinator.engine._scrub_forge_history"),
    ):
        with pytest.raises(StoryCancelled):
            _coordinator_loop(state, config, task, "story", task_start=0.0, stop_event=stop)

    mock_val.assert_not_called()
    assert _commits_ahead(tmp_path, base) > 0
    assert _porcelain(tmp_path) == ""
    assert state.dev_output_preservation_failure is None


def test_dev_output_left_on_another_branch_is_not_committed_there(tmp_path: Path) -> None:
    """A dev iteration that ends on some other attached branch must not have its
    output committed onto that branch: the commit would land on a ref the story
    never publishes while the story branch stayed empty. Preservation refuses, and
    the cancellation that follows fails closed."""
    base = _init_story_repo(tmp_path)
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = _seeded_state(tmp_path)  # story branch is feat/test
    stop = StopSignal()

    def _dev_wanders_off_the_story_branch(*_args, **_kwargs):
        subprocess.run(["git", "checkout", "-q", "-b", "elsewhere"], cwd=tmp_path, check=True)
        (tmp_path / "run_tests.py").write_text("print('run')\n")
        stop.stop("Sprint budget exhausted", error_type=BUDGET_CANCEL_ERROR_TYPE)
        return None

    with (
        patch(
            "theforge.coordinator.engine._run_dev_phase",
            side_effect=_dev_wanders_off_the_story_branch,
        ),
        patch("theforge.coordinator.engine._run_validate_phase"),
        patch("theforge.coordinator.engine._run_review_phase"),
        patch("theforge.coordinator.engine._scrub_forge_history"),
    ):
        with pytest.raises(StoryCancelled):
            _coordinator_loop(state, config, task, "story", task_start=0.0, stop_event=stop)

    # Nothing committed anywhere, and the refusal is recorded for the fail-closed
    # cancellation result.
    assert _commits_ahead(tmp_path, base) == 0
    assert "run_tests.py" in _porcelain(tmp_path)
    assert state.dev_output_preservation_failure is not None
    assert "not on the story branch" in state.dev_output_preservation_failure
    # Every branch is still where it was: no checkpoint on the foreign ref.
    for branch in ("feat/test", "elsewhere", "main"):
        subjects = subprocess.run(
            ["git", "log", "--format=%s", branch],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert CHECKPOINT_COMMIT_SUBJECT not in subjects, branch
        assert subjects.strip() == "seed", branch


def _run_task_over(tmp_path: Path, config, task, dev_side_effect, stop) -> object:
    """Drive the real run_task path so the StoryCancelled catch site builds the
    result, rather than asserting on the loop's exception alone."""
    with (
        patch(
            "theforge.coordinator.workspace._create_workspace",
            return_value=(tmp_path, "feat/test", None),
        ),
        patch(
            "theforge.coordinator.engine._create_workspace",
            return_value=(tmp_path, "feat/test", None),
        ),
        patch("theforge.coordinator.preflight_flow._run_preflight_phase") as mock_preflight,
        patch("theforge.coordinator.plan_flow._run_plan_phase", return_value=None),
        patch("theforge.coordinator.engine._run_dev_phase", side_effect=dev_side_effect),
        patch("theforge.coordinator.engine._run_validate_phase"),
        patch("theforge.coordinator.engine._run_review_phase"),
        patch("theforge.coordinator.engine._scrub_forge_history"),
        patch("theforge.coordinator.engine._record_run_memory") as mock_record,
        patch("theforge.coordinator.engine._fire_post_run_hook"),
    ):
        mock_preflight.return_value = (config, None, False)
        result = run_task(config, task, stop_event=stop)
    return result, mock_record


def test_unpreservable_dev_output_fails_the_story_closed_not_as_an_ordinary_stop(
    tmp_path: Path,
) -> None:
    """A cancellation whose dev work could not be preserved must not be recorded
    as an ordinary stop: the worktree is being kept with uncommitted dev output
    that a later phase will read as foreign content. That is a substrate failure,
    so the result is an infrastructure failure naming the stranded workspace."""
    _init_story_repo(tmp_path)
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    stop = StopSignal()

    def _dev_writes_then_halt(*_args, **_kwargs):
        (tmp_path / "run_tests.py").write_text("print('run')\n")
        stop.stop("Sprint budget exhausted", error_type=BUDGET_CANCEL_ERROR_TYPE)
        return None

    with patch("theforge.coordinator.commit_guard._checkpoint_commit", return_value=False):
        result, mock_record = _run_task_over(tmp_path, config, task, _dev_writes_then_halt, stop)

    assert result.success is False
    assert result.infrastructure_failure is True
    assert result.state.error_type == PRESERVATION_FAILED_ERROR_TYPE
    assert "could NOT be preserved" in result.message
    assert str(tmp_path) in result.message
    # The work really is still stranded — this is the state being reported.
    assert "run_tests.py" in _porcelain(tmp_path)
    # Still no second failure narrative for a cancelled story.
    mock_record.assert_not_called()


def test_preserved_dev_output_still_cancels_as_an_ordinary_stop(tmp_path: Path) -> None:
    """Contrast with the test above: when preservation succeeds, the story ends as
    the ordinary cancellation it is, carrying the stop's own reason."""
    base = _init_story_repo(tmp_path)
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    stop = StopSignal()

    def _dev_writes_then_halt(*_args, **_kwargs):
        (tmp_path / "run_tests.py").write_text("print('run')\n")
        stop.stop("Sprint budget exhausted", error_type=BUDGET_CANCEL_ERROR_TYPE)
        return None

    result, _mock_record = _run_task_over(tmp_path, config, task, _dev_writes_then_halt, stop)

    assert result.success is False
    assert result.infrastructure_failure is False
    assert result.state.error_type == BUDGET_CANCEL_ERROR_TYPE
    assert result.message == "Sprint budget exhausted"
    assert _commits_ahead(tmp_path, base) > 0
    assert _porcelain(tmp_path) == ""
