"""EVENT AGE ticks from reviewer tool-call events during review (issue #1086).

Before this change EVENT AGE only advanced when ``audit.yaml`` was touched, so a
REVIEW / PLAN_REVIEW cycle showed a frozen ``—`` for its whole duration. The
renderer now takes the freshest of the audit mtime and the per-entry
``last_event_ts`` (bumped on every reviewer iteration), so review advances live.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.cli import status_watch


def _entry(slug: str, *, status: str = "running", last_event_ts: float | None = None):
    from theforge.sprint.status_reader import StoryStatusEntry

    return StoryStatusEntry(
        slug=slug,
        path=slug,
        status=status,
        phase="REVIEW",
        cost_usd=0.0,
        last_event_ts=last_event_ts,
    )


def _render(tmp_path: Path, entries: list, *, now: float, audit_mtime: float | None) -> str:
    state: dict = {"costs": {}, "interval": 2.0}
    with (
        patch("theforge.cli.sprint_status.display_sprint_status", return_value=0),
        patch("theforge.sprint.status_reader.read_live_status", return_value=entries),
        patch.object(status_watch, "_last_audit_mtime", return_value=audit_mtime),
    ):
        text, _ok, _err = status_watch.render_frame(
            "run-x",
            tmp_path,
            state,
            frame_idx=0,
            color=False,
            now_fn=lambda: now,
        )
    return text


class TestReviewerEventAge:
    def test_event_age_ticks_from_reviewer_event_without_audit(self, tmp_path: Path) -> None:
        # audit.yaml absent/stale (mtime None) but a reviewer event landed 5s ago.
        entries = [_entry("story-a", last_event_ts=995.0)]
        text = _render(tmp_path, entries, now=1000.0, audit_mtime=None)
        assert "5s" in text
        # Not the frozen em-dash the old code showed for the whole review.
        assert "  —  " not in text or "5s" in text

    def test_freshest_source_wins(self, tmp_path: Path) -> None:
        # Reviewer event (2s ago) is fresher than audit mtime (40s ago).
        entries = [_entry("story-a", last_event_ts=998.0)]
        text = _render(tmp_path, entries, now=1000.0, audit_mtime=960.0)
        assert "2s" in text
        assert "40s" not in text

    def test_audit_mtime_used_when_no_reviewer_event(self, tmp_path: Path) -> None:
        # DEV / other phases have no reviewer event — audit mtime still drives age.
        entries = [_entry("story-a", last_event_ts=None)]
        text = _render(tmp_path, entries, now=1000.0, audit_mtime=970.0)
        assert "30s" in text

    def test_dash_when_neither_source(self, tmp_path: Path) -> None:
        entries = [_entry("story-a", last_event_ts=None)]
        text = _render(tmp_path, entries, now=1000.0, audit_mtime=None)
        assert "—" in text
