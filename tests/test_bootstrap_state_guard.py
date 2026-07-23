"""Test that write_bootstrap_state doesn't overwrite active sprint state."""

from __future__ import annotations

import yaml
from pathlib import Path

from theforge.sprint.state_writer import write_bootstrap_state
from theforge.sprint.story_state import SprintStoryState


def test_write_bootstrap_state_does_not_overwrite_existing_sprint_id(tmp_path: Path) -> None:
    """Bootstrap write should be skipped if state file already has a sprint_id."""
    # First, create a state file with a sprint_id (simulating an active sprint)
    state_dir = tmp_path / ".forge" / "runs"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "test-run.state"

    # Write initial state with sprint_id set
    initial_data = {
        "sprint_name": "test-sprint",
        "sprint_id": "sprint-123",  # This is set, not None
        "sprint_phase": "underway",
        "stories": [
            {
                "slug": "issue-1",
                "status": "waiting",
                "outcome": "waiting",
            }
        ],
    }
    with open(state_path, "w", encoding="utf-8") as f:
        yaml.dump(initial_data, f)

    # Now try to write bootstrap state - this should be a no-op
    result_path = write_bootstrap_state(
        run_id="test-run",
        project_root=tmp_path,
        sprint_name="different-sprint",  # Different name
        sprint_phase="shape-gate",       # Different phase
        issues=[{"number": 2}],          # Different issue
    )

    # Should return the same path without overwriting
    assert result_path == state_path

    # File should still contain the original data
    with open(state_path, encoding="utf-8") as f:
        current_data = yaml.safe_load(f)

    assert current_data["sprint_id"] == "sprint-123"
    assert current_data["sprint_name"] == "test-sprint"
    assert current_data["sprint_phase"] == "underway"
    assert len(current_data["stories"]) == 1
    assert current_data["stories"][0]["slug"] == "issue-1"


def test_write_bootstrap_state_does_not_overwrite_non_waiting_stories(tmp_path: Path) -> None:
    """Bootstrap write should be skipped if state file has non-waiting stories."""
    # First, create a state file with a story that's not waiting
    state_dir = tmp_path / ".forge" / "runs"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "test-run.state"

    # Write initial state with a running story
    initial_data = {
        "sprint_name": "test-sprint",
        "sprint_id": None,  # Still None, but we have a running story
        "sprint_phase": "underway",
        "stories": [
            {
                "slug": "issue-1",
                "status": "running",  # Not waiting!
                "outcome": "running",
            }
        ],
    }
    with open(state_path, "w", encoding="utf-8") as f:
        yaml.dump(initial_data, f)

    # Now try to write bootstrap state - this should be a no-op
    result_path = write_bootstrap_state(
        run_id="test-run",
        project_root=tmp_path,
        sprint_name="different-sprint",
        sprint_phase="shape-gate",
        issues=[{"number": 2}],
    )

    # Should return the same path without overwriting
    assert result_path == state_path

    # File should still contain the original data
    with open(state_path, encoding="utf-8") as f:
        current_data = yaml.safe_load(f)

    assert current_data["sprint_id"] is None
    assert current_data["sprint_name"] == "test-sprint"
    assert current_data["sprint_phase"] == "underway"
    assert len(current_data["stories"]) == 1
    assert current_data["stories"][0]["status"] == "running"  # Still running!


def test_write_bootstrap_state_allows_initial_write_when_no_state_file(tmp_path: Path) -> None:
    """Bootstrap write should proceed when no state file exists."""
    state_dir = tmp_path / ".forge" / "runs"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "test-run.state"

    # Confirm file doesn't exist yet
    assert not state_path.exists()

    # Write bootstrap state - this should succeed
    result_path = write_bootstrap_state(
        run_id="test-run",
        project_root=tmp_path,
        sprint_name="test-sprint",
        sprint_phase="shape-gate",
        issues=[{"number": 1, "title": "Test Issue"}],
    )

    # Should return the state path
    assert result_path == state_path
    assert state_path.exists()

    # File should contain bootstrap data
    with open(state_path, encoding="utf-8") as f:
        current_data = yaml.safe_load(f)

    assert current_data["sprint_name"] == "test-sprint"
    assert current_data["sprint_phase"] == "shape-gate"
    assert current_data["sprint_id"] is None  # Bootstrap sets this to None
    assert len(current_data["stories"]) == 1
    assert current_data["stories"][0]["slug"] == "issue-1"
    assert current_data["stories"][0]["status"] == "waiting"


def test_write_bootstrap_state_allows_overwrite_when_all_waiting_and_no_sprint_id(tmp_path: Path) -> None:
    """Bootstrap write should proceed when state file exists but has no sprint_id and all stories waiting."""
    # Create a state file that looks like a bootstrap state (no sprint_id, all waiting)
    state_dir = tmp_path / ".forge" / "runs"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "test-run.state"

    # Write a bootstrap-like state
    bootstrap_data = {
        "sprint_name": "old-sprint",
        "sprint_id": None,  # Still None
        "sprint_phase": "shape-gate",
        "stories": [
            {
                "slug": "issue-1",
                "status": "waiting",  # Still waiting
                "outcome": "waiting",
            }
        ],
    }
    with open(state_path, "w", encoding="utf-8") as f:
        yaml.dump(bootstrap_data, f)

    # Now try to write bootstrap state - this should proceed (overwrite)
    result_path = write_bootstrap_state(
        run_id="test-run",
        project_root=tmp_path,
        sprint_name="new-sprint",
        sprint_phase="intake-remediation",
        issues=[{"number": 2, "title": "New Issue"}],
    )

    # Should return the same path (overwrote the file)
    assert result_path == state_path
    assert state_path.exists()

    # File should now contain the new bootstrap data
    with open(state_path, encoding="utf-8") as f:
        current_data = yaml.safe_load(f)

    assert current_data["sprint_name"] == "new-sprint"
    assert current_data["sprint_phase"] == "intake-remediation"
    assert current_data["sprint_id"] is None
    assert len(current_data["stories"]) == 1
    assert current_data["stories"][0]["slug"] == "issue-2"
    assert current_data["stories"][0]["status"] == "waiting"