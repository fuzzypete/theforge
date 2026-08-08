"""A story held at a gate that waits on a person must not read as a stall.

Issue #2313: a story parked at the escalate gate emits no events while the
coordinator polls its pending checkpoint, so the live view aged it out as
``· stalled`` — the same signal as a hang. The information needed to say
otherwise already exists on disk: ``.forge/pending/<run>.yaml`` carries the
phase, the options and the deadline. These tests pin the seam that projects
that record into live status and renders it as a deliberate wait.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import yaml

from theforge import pending as pending_mod
from theforge.cli import status_watch
from theforge.sprint.state_writer import SprintStateWriter
from theforge.sprint.status_reader import (
    OPERATOR_DECISION_STAGE,
    StoryStatusEntry,
    read_live_status,
)

RUN_ID = "run-esc"
STORY = "issue-2206"
PENDING_RUN_ID = "b05817e579cd"
OPTIONS = ["accept", "redirect", "defer"]


def _live_state(tmp_path: Path, *, slugs: tuple[str, ...] = (STORY,)) -> SprintStateWriter:
    """A live sprint state file with each slug running in REVIEW."""
    (tmp_path / ".forge" / "runs").mkdir(parents=True, exist_ok=True)
    writer = SprintStateWriter(RUN_ID, tmp_path, "sprint")
    writer.init(
        [{"slug": slug, "path": slug, "status": "running", "phase": "REVIEW"} for slug in slugs]
    )
    return writer


def _write_pending(
    tmp_path: Path,
    *,
    story: str = STORY,
    run_id: str = PENDING_RUN_ID,
    timeout_seconds: int = 900,
    phase: str = "ESCALATE",
) -> Path:
    return pending_mod.write_pending(
        run_id=run_id,
        story=story,
        phase=phase,
        reason="ESCALATION — advisory report unavailable; select an action.",
        options=OPTIONS,
        timeout_seconds=timeout_seconds,
        project_root=tmp_path,
    )


def _patch_pending_file(path: Path, **fields: object) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(yaml.safe_dump(data, default_flow_style=False), encoding="utf-8")


def _frame(tmp_path: Path, entries: list[StoryStatusEntry], *, age_seconds: float) -> str:
    state: dict = {"costs": {}, "interval": 2.0}
    with (
        patch("theforge.cli.sprint_status.display_sprint_status", return_value=0),
        patch("theforge.sprint.status_reader.read_live_status", return_value=entries),
        patch.object(status_watch, "_last_audit_mtime", return_value=1000.0),
    ):
        text, _ok, _err = status_watch.render_frame(
            RUN_ID,
            tmp_path,
            state,
            frame_idx=0,
            color=False,
            now_fn=lambda: 1000.0 + age_seconds,
        )
    return text


class TestPendingDecisionInLiveStatus:
    """`.forge/pending/*.yaml` → read_live_status stage/detail."""

    def test_reports_an_operator_decision_stage_with_remaining_time(self, tmp_path: Path) -> None:
        _live_state(tmp_path)
        _write_pending(tmp_path)

        entry = next(e for e in read_live_status(RUN_ID, tmp_path) or [] if e.slug == STORY)

        # Still running: the wait is inside the story, not a separate outcome.
        assert entry.status == "running"
        assert entry.stage == OPERATOR_DECISION_STAGE
        # Enough to act on without opening a log: which checkpoint, which gate,
        # how long is left, and what may be chosen.
        assert PENDING_RUN_ID in entry.detail
        assert "ESCALATE" in entry.detail
        assert "remaining" in entry.detail
        assert "accept" in entry.detail

    def test_expired_window_still_reads_as_awaiting_a_decision(self, tmp_path: Path) -> None:
        # The escalate gate preserves a lapsed checkpoint rather than
        # auto-rejecting, so an elapsed window still needs the operator.
        _live_state(tmp_path)
        _write_pending(tmp_path, timeout_seconds=-60)

        entry = next(e for e in read_live_status(RUN_ID, tmp_path) or [] if e.slug == STORY)

        assert entry.stage == OPERATOR_DECISION_STAGE
        assert "awaiting decision" in entry.detail

    def test_decided_checkpoint_is_not_shown_as_pending(self, tmp_path: Path) -> None:
        _live_state(tmp_path)
        path = _write_pending(tmp_path)
        _patch_pending_file(path, decision="accept")

        entry = next(e for e in read_live_status(RUN_ID, tmp_path) or [] if e.slug == STORY)

        assert entry.stage != OPERATOR_DECISION_STAGE

    def test_checkpoint_owned_by_a_dead_process_is_not_shown_as_pending(
        self, tmp_path: Path
    ) -> None:
        _live_state(tmp_path)
        path = _write_pending(tmp_path)
        with patch("theforge.pid._is_pid_alive", return_value=False):
            entry = next(e for e in read_live_status(RUN_ID, tmp_path) or [] if e.slug == STORY)
        assert entry.stage != OPERATOR_DECISION_STAGE

        # Same file, live owner — proves the filter is the PID and nothing else.
        _patch_pending_file(path, pid=os.getpid())
        entry = next(e for e in read_live_status(RUN_ID, tmp_path) or [] if e.slug == STORY)
        assert entry.stage == OPERATOR_DECISION_STAGE

    def test_a_checkpoint_never_leaks_onto_another_story(self, tmp_path: Path) -> None:
        _live_state(tmp_path, slugs=(STORY, "issue-999"))
        _write_pending(tmp_path)

        entries = {e.slug: e for e in read_live_status(RUN_ID, tmp_path) or []}

        assert entries[STORY].stage == OPERATOR_DECISION_STAGE
        assert entries["issue-999"].stage != OPERATOR_DECISION_STAGE

    def test_escalate_checkpoint_wins_over_another_gate_for_the_same_story(
        self, tmp_path: Path
    ) -> None:
        _live_state(tmp_path)
        _write_pending(tmp_path, run_id="older-plan", phase="PLAN_REVIEW")
        _write_pending(tmp_path)

        entry = next(e for e in read_live_status(RUN_ID, tmp_path) or [] if e.slug == STORY)

        assert entry.stage == OPERATOR_DECISION_STAGE
        assert PENDING_RUN_ID in entry.detail


class TestWatchRendersPendingDecisions:
    """read_live_status → status_watch.render_frame."""

    def test_stale_story_awaiting_a_decision_is_not_rendered_as_stalled(
        self, tmp_path: Path
    ) -> None:
        _live_state(tmp_path)
        _write_pending(tmp_path)
        entries = [e for e in read_live_status(RUN_ID, tmp_path) or [] if e.slug == STORY]

        # 46 minutes past the last event — the age from the reported incident.
        text = _frame(tmp_path, entries, age_seconds=46 * 60)

        assert "stalled" not in text
        assert "decision" in text
        # The operator learns what is pending and how long is left without
        # opening a log, plus how to resolve it.
        assert "Awaiting operator decision" in text
        assert "remaining" in text
        assert PENDING_RUN_ID in text
        assert "forge decide" in text
        # The event age stays visible; only the warning framing is dropped.
        assert "46m00s" in text

    def test_ordinary_stale_running_story_still_reports_stalled(self, tmp_path: Path) -> None:
        entry = StoryStatusEntry(
            slug="story-c",
            path="story-c",
            status="running",
            phase="DEV",
            cost_usd=0.0,
        )

        text = _frame(tmp_path, [entry], age_seconds=46 * 60)

        assert "stalled" in text
        assert "Hint:" in text
        assert "Awaiting operator decision" not in text
