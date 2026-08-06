"""A held collision gate reads as a deliberate wait, not as a stall (issue #2235).

The scheduler already knew which story blocked the gate and on which files —
that reason only ever reached the run log, so the status view aged the gated
story out as ``stalled`` with a "has not moved in a while" hint. These tests
cover the whole seam: the scheduler publishing the hold into live state, the
status reader turning it into a ``collision gate`` stage, and watch mode
rendering it as gated rather than stalled.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.cli import status_watch
from theforge.sprint.runner import (
    CLAIM_IN_DEV,
    CLAIM_PRESERVED,
    GATE_HOLD_DETAIL_KEYS,
    _make_gate_hold_publisher,
    _release_plan_gates,
)
from theforge.sprint.state_writer import SprintStateWriter
from theforge.sprint.status_reader import COLLISION_GATE_STAGE, read_live_status


def _record_holds() -> tuple[list[tuple[str, dict | None]], object]:
    calls: list[tuple[str, dict | None]] = []

    def _fn(slug: str, payload: dict | None) -> None:
        calls.append((slug, payload))

    return calls, _fn


class TestSchedulerPublishesGateHold:
    def test_newly_detected_overlap_publishes_blockers_and_files(self) -> None:
        calls, fn = _record_holds()
        plan_done = {"story-b": "/tmp/ws-b"}
        file_footprints: dict[str, set[str]] = {"story-a": {"src/shared.py", "src/only-a.py"}}
        gate_b = threading.Event()
        plan_gates = {"story-b": gate_b}
        active: dict[str, object] = {"story-a": MagicMock(), "story-b": MagicMock()}

        with patch(
            "theforge.sprint.runner._extract_plan_footprint",
            return_value={"src/shared.py"},
        ):
            _release_plan_gates(
                plan_done,
                file_footprints,
                plan_gates,
                active,
                threading.Lock(),
                {"story-a": CLAIM_IN_DEV},
                gate_hold_fn=fn,
            )

        assert not gate_b.is_set()
        # The detection pass and the deferred re-check both report the same hold
        # in one call; the publisher collapses repeats, so only the payload matters.
        assert {slug for slug, _ in calls} == {"story-b"}
        assert all(
            payload
            == {
                "collision_gate_blockers": ["story-a"],
                # Only the shared path — story-a's other file is not a blocker.
                "collision_gate_files": ["src/shared.py"],
                "collision_gate_claims": {"story-a": CLAIM_IN_DEV},
            }
            for _slug, payload in calls
        )

    def test_still_deferred_gate_republishes_its_hold(self) -> None:
        calls, fn = _record_holds()
        file_footprints = {
            "story-a": {"src/shared.py"},
            "story-b": {"src/shared.py"},
        }
        plan_gates = {"story-b": threading.Event()}

        _release_plan_gates(
            {},
            file_footprints,
            plan_gates,
            {"story-a": MagicMock(), "story-b": MagicMock()},
            threading.Lock(),
            {"story-a": CLAIM_IN_DEV},
            gate_hold_fn=fn,
        )

        assert calls[0][0] == "story-b"
        assert calls[0][1] is not None
        assert calls[0][1]["collision_gate_blockers"] == ["story-a"]

    def test_opened_gate_clears_the_hold(self) -> None:
        calls, fn = _record_holds()
        gate_b = threading.Event()
        plan_gates = {"story-b": gate_b}

        # story-a has finished and holds no claim, so nothing blocks story-b.
        _release_plan_gates(
            {},
            {"story-a": {"src/shared.py"}, "story-b": {"src/shared.py"}},
            plan_gates,
            {"story-b": MagicMock()},
            threading.Lock(),
            {},
            gate_hold_fn=fn,
        )

        assert gate_b.is_set()
        assert calls == [("story-b", None)]

    def test_stand_down_clears_the_hold(self) -> None:
        calls, fn = _record_holds()
        plan_gates = {"story-b": threading.Event()}

        stood_down = _release_plan_gates(
            {},
            {"story-a": {"src/shared.py"}, "story-b": {"src/shared.py"}},
            plan_gates,
            {"story-b": MagicMock()},
            threading.Lock(),
            {"story-a": CLAIM_PRESERVED},
            gate_hold_fn=fn,
        )

        assert stood_down == ["story-b"]
        assert calls == [("story-b", None)]


class TestGateHoldPublisher:
    def _writer(self, tmp_path: Path) -> SprintStateWriter:
        (tmp_path / ".forge" / "runs").mkdir(parents=True)
        writer = SprintStateWriter("run-x", tmp_path, "sprint")
        writer.init([{"slug": "story-b", "path": "story-b", "status": "running"}])
        return writer

    def test_hold_merges_into_detail_without_erasing_co_resident_keys(
        self, tmp_path: Path
    ) -> None:
        writer = self._writer(tmp_path)
        writer.update("story-b", detail_updates={"last_reviewer_event_ts": 1234.0})

        publish = _make_gate_hold_publisher(writer)
        publish("story-b", {"collision_gate_blockers": ["story-a"]})

        detail = writer.story_state.get("story-b").detail
        assert detail["last_reviewer_event_ts"] == 1234.0
        assert detail["collision_gate_blockers"] == ["story-a"]

    def test_repeated_identical_hold_writes_once(self, tmp_path: Path) -> None:
        writer = self._writer(tmp_path)
        publish = _make_gate_hold_publisher(writer)
        payload = {"collision_gate_blockers": ["story-a"]}

        with patch.object(writer, "update", wraps=writer.update) as spy:
            publish("story-b", payload)
            publish("story-b", dict(payload))
            publish("story-b", dict(payload))

        assert spy.call_count == 1

    def test_clear_only_writes_for_a_story_that_was_held(self, tmp_path: Path) -> None:
        writer = self._writer(tmp_path)
        publish = _make_gate_hold_publisher(writer)

        with patch.object(writer, "update", wraps=writer.update) as spy:
            publish("story-b", None)
            assert spy.call_count == 0

            publish("story-b", {"collision_gate_blockers": ["story-a"]})
            publish("story-b", None)

        assert spy.call_count == 2
        detail = writer.story_state.get("story-b").detail
        assert all(detail.get(key) is None for key in GATE_HOLD_DETAIL_KEYS)


class TestLiveStatusSeam:
    """Scheduler → .state file → read_live_status → watch frame, end to end."""

    def _held_state(self, tmp_path: Path) -> SprintStateWriter:
        (tmp_path / ".forge" / "runs").mkdir(parents=True)
        writer = SprintStateWriter("run-x", tmp_path, "sprint")
        writer.init(
            [
                {"slug": "story-a", "path": "story-a", "status": "running", "phase": "DEV"},
                {
                    "slug": "story-b",
                    "path": "story-b",
                    "status": "running",
                    "phase": "PLAN_DONE",
                },
            ]
        )
        gate_b = threading.Event()
        _release_plan_gates(
            {},
            {
                "story-a": {"src/theforge/model_profiles.py"},
                "story-b": {"src/theforge/model_profiles.py"},
            },
            {"story-b": gate_b},
            {"story-a": MagicMock(), "story-b": MagicMock()},
            threading.Lock(),
            {"story-a": CLAIM_IN_DEV},
            gate_hold_fn=_make_gate_hold_publisher(writer),
        )
        assert not gate_b.is_set()
        return writer

    def test_read_live_status_reports_a_collision_gate_stage(self, tmp_path: Path) -> None:
        self._held_state(tmp_path)

        entries = read_live_status("run-x", tmp_path) or []
        held = next(e for e in entries if e.slug == "story-b")

        # Still running — the wait is inside the story, not a separate outcome.
        assert held.status == "running"
        assert held.stage == COLLISION_GATE_STAGE
        assert "story-a" in held.detail
        assert "model_profiles.py" in held.detail

        # An ungated story is untouched.
        other = next(e for e in entries if e.slug == "story-a")
        assert other.stage != COLLISION_GATE_STAGE

    def test_watch_renders_a_stale_gated_story_as_gated_not_stalled(self, tmp_path: Path) -> None:
        self._held_state(tmp_path)
        entries = read_live_status("run-x", tmp_path) or []
        held = [e for e in entries if e.slug == "story-b"]

        state: dict = {"costs": {}, "interval": 2.0}
        with (
            patch("theforge.cli.sprint_status.display_sprint_status", return_value=0),
            patch("theforge.sprint.status_reader.read_live_status", return_value=held),
            patch.object(status_watch, "_last_audit_mtime", return_value=1000.0),
        ):
            text, _ok, _err = status_watch.render_frame(
                "run-x",
                tmp_path,
                state,
                frame_idx=0,
                color=False,
                # 22 minutes past the last event — far beyond the stall threshold.
                now_fn=lambda: 1000.0 + 22 * 60,
            )

        assert "gated" in text
        assert "stalled" not in text
        # The event age stays visible: the operator still sees how long the wait
        # has run, just without the warning that it needs investigating.
        assert "22m00s" in text

    def test_ordinary_stale_running_story_still_reports_stalled(self, tmp_path: Path) -> None:
        from theforge.sprint.status_reader import StoryStatusEntry

        entry = StoryStatusEntry(
            slug="story-c",
            path="story-c",
            status="running",
            phase="DEV",
            cost_usd=0.0,
        )
        state: dict = {"costs": {}, "interval": 2.0}
        with (
            patch("theforge.cli.sprint_status.display_sprint_status", return_value=0),
            patch("theforge.sprint.status_reader.read_live_status", return_value=[entry]),
            patch.object(status_watch, "_last_audit_mtime", return_value=1000.0),
        ):
            text, _ok, _err = status_watch.render_frame(
                "run-x",
                tmp_path,
                state,
                frame_idx=0,
                color=False,
                now_fn=lambda: 1000.0 + 22 * 60,
            )

        assert "stalled" in text
        assert "Hint:" in text
