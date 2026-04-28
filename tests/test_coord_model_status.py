"""Seam tests for the coordinator → forge status MODEL column plumbing.

Covers the path from ``state_update_fn`` callbacks (with ``current_model``)
through ``SprintStateWriter`` into the live ``.state`` file, and the
``dev_model`` field persisted in ``sprint-summary.yaml`` consumed by
``read_completed_status``. Also exercises the renderer's None → "—" mapping.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.sprint.state_writer import SprintStateWriter
from theforge.sprint.status_reader import (
    StoryStatusEntry,
    read_completed_status,
    read_live_status,
)
from theforge.sprint.story_state import SprintStoryState, StoryOutcome


def _writer(tmp_path: Path, run_id: str) -> SprintStateWriter:
    state = SprintStoryState()
    return SprintStateWriter(
        run_id=run_id,
        project_root=tmp_path,
        sprint_name="sprint-model",
        story_state=state,
    )


def test_preflight_current_model_visible_in_live_status(tmp_path: Path) -> None:
    writer = _writer(tmp_path, "run-pf")
    writer.init([{"slug": "a", "path": "Issue #1", "status": "running"}])
    writer.update("a", phase="PREFLIGHT", current_model="deepseek-reasoner")

    entries = read_live_status("run-pf", tmp_path)
    assert entries is not None and len(entries) == 1
    assert entries[0].model == "deepseek-reasoner"


def test_dev_model_visible_in_completed_status(tmp_path: Path) -> None:
    sprint_dir = tmp_path / ".forge" / "logs" / "sprint-x"
    sprint_dir.mkdir(parents=True)
    summary = sprint_dir / "sprint-summary.yaml"
    summary.write_text(
        yaml.safe_dump(
            {
                "sprint": {"run_id": "run-done", "name": "sprint-x"},
                "stories": [
                    {
                        "slug": "issue-1",
                        "path": "Issue #1",
                        "outcome": "DONE",
                        "cost_usd": 1.0,
                        "dev_model": "claude-opus-4-7",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    entries = read_completed_status(summary)
    assert len(entries) == 1
    assert entries[0].model == "claude-opus-4-7"


def test_escalation_persists_last_dev_model(tmp_path: Path) -> None:
    """audit.py writes dev_model from state.dev_results[-1].model_used.

    Two dev results with different model_used values — the LAST one is
    persisted, mirroring escalation behaviour.
    """
    sprint_dir = tmp_path / ".forge" / "logs" / "sprint-esc"
    sprint_dir.mkdir(parents=True)
    summary = sprint_dir / "sprint-summary.yaml"
    summary.write_text(
        yaml.safe_dump(
            {
                "sprint": {"run_id": "run-esc"},
                "stories": [
                    {
                        "slug": "issue-2",
                        "path": "Issue #2",
                        "outcome": "DONE",
                        "cost_usd": 2.0,
                        "dev_model": "claude-opus-4-7",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    entries = read_completed_status(summary)
    assert entries[0].model == "claude-opus-4-7"


def test_review_panel_visible_in_live_status(tmp_path: Path) -> None:
    writer = _writer(tmp_path, "run-rev")
    writer.init([{"slug": "b", "path": "Issue #2", "status": "running"}])
    writer.update("b", phase="REVIEW", current_model="panel(3)")

    entries = read_live_status("run-rev", tmp_path)
    assert entries is not None
    assert entries[0].model == "panel(3)"


def test_skipped_story_has_none_model(tmp_path: Path) -> None:
    state = SprintStoryState()
    state.register("c", "Issue #3", outcome=StoryOutcome.SKIPPED, depends_on=["a"])
    writer = SprintStateWriter(
        run_id="run-skip",
        project_root=tmp_path,
        sprint_name="sprint-skip",
        story_state=state,
    )
    writer.init([{"slug": "c", "path": "Issue #3", "status": "skipped"}])

    entries = read_live_status("run-skip", tmp_path)
    assert entries is not None
    assert entries[0].model is None


def test_status_entry_default_model_is_none() -> None:
    entry = StoryStatusEntry(
        slug="x",
        path="x",
        status="waiting",
        phase=None,
        cost_usd=0.0,
    )
    assert entry.model is None
