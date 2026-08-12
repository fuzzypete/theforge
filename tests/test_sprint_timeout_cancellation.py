"""Seam test: sprint timeout sets the per-story stop_event so worker threads
stop running, and no conflicting escalation record is written for the slug.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

from sprint_test_helpers import run_sprint_ctx

from tests.test_sprint_resume import _make_config, _make_manifest, _make_spec_file


def test_sprint_timeout_sets_stop_event_for_worker(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    spec = _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest = _make_manifest(tmp_path, [spec.name])

    captured: dict[str, threading.Event | None] = {"stop_event": None}

    class _NeverDoneFuture:
        def cancel(self):
            return True

        def result(self):
            raise AssertionError("should not be called")

    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, *args, **kwargs):
            # _run_single_story signature ends with stop_event (positional).
            if args:
                captured["stop_event"] = args[-1]
            return _NeverDoneFuture()

    with (
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
        patch("theforge.sprint.runner.wait", return_value=(set(), set())),
        patch("theforge.sprint.runner.time.monotonic", side_effect=[0.0, 4000.0, 4000.0]),
    ):
        run_sprint_ctx(config, manifest)

    # The scheduler must have passed a stop_event into the worker AND set it
    # when the timeout fired. Without this, the worker keeps running after
    # the synthetic FAILED audit is written, eventually writing its own
    # escalation record — exactly the bug this story fixes.
    assert captured["stop_event"] is not None, "scheduler did not pass stop_event to worker"
    assert isinstance(captured["stop_event"], threading.Event)
    assert captured["stop_event"].is_set(), "timeout handler did not signal stop_event"

    # Sanity: only the synthetic timeout audit exists; no second
    # ESCALATE record was appended (the worker never ran past _run_single_story
    # in this fake harness, so escalation history must be empty).
    esc_path = tmp_path / ".forge" / "assignment_history.yaml"
    assert not esc_path.exists() or "feature-a" not in esc_path.read_text(encoding="utf-8")
