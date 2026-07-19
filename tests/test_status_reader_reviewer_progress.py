"""Reviewer-progress rendering + coordinator→state→renderer seam (issue #1086).

The REVIEW / PLAN_REVIEW watch view surfaces per-reviewer iteration, retry, and
pool-progress by writing a structured ``reviewer_progress`` contract into the
live ``.state`` story ``detail`` dict. These tests cover:

- ``_reviewer_progress_stage`` formatting (done / iter / retry / empty).
- The seam: a ``ReviewerProgressChannel`` fed by a fake ``run_agent_pool``
  progress stream writes ``reviewer_progress`` + ``last_reviewer_event_ts`` into
  live state, and ``read_live_status`` renders per-reviewer STAGE + pool DETAIL.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from theforge.coordinator.reviewer_progress import ReviewerProgressChannel
from theforge.sprint.status_reader import (
    _reviewer_progress_stage,
    read_live_status,
)

# ── Unit: _reviewer_progress_stage ────────────────────────────────────────────


class TestReviewerProgressStage:
    def test_done_and_iter_mix(self) -> None:
        progress = {
            "deepseek": {"iter": 5, "tool_calls": 3, "retry": None, "done": True},
            "gemini": {"iter": 3, "tool_calls": 1, "retry": None, "done": False},
        }
        stage, pool_detail = _reviewer_progress_stage(progress, 2)
        assert stage == "deepseek=done, gemini=iter3"
        assert pool_detail == "pool 1/2 done"

    def test_retry_glyph_appended(self) -> None:
        progress = {
            "gemini": {"iter": 3, "tool_calls": 1, "retry": [1, 2], "done": False},
        }
        stage, pool_detail = _reviewer_progress_stage(progress, 1)
        assert stage == "gemini=iter3 ↻r1/2"
        assert pool_detail == "pool 0/1 done"

    def test_zero_iter_shows_ellipsis(self) -> None:
        progress = {"gpt": {"iter": 0, "tool_calls": 0, "retry": None, "done": False}}
        stage, _pool = _reviewer_progress_stage(progress, 1)
        assert stage == "gpt=…"

    def test_pool_size_falls_back_to_len(self) -> None:
        progress = {
            "a": {"iter": 1, "tool_calls": 0, "retry": None, "done": True},
            "b": {"iter": 2, "tool_calls": 0, "retry": None, "done": True},
        }
        _stage, pool_detail = _reviewer_progress_stage(progress, None)
        assert pool_detail == "pool 2/2 done"

    def test_all_done(self) -> None:
        progress = {
            "a": {"iter": 4, "tool_calls": 0, "retry": None, "done": True},
            "b": {"iter": 6, "tool_calls": 0, "retry": None, "done": True},
        }
        stage, pool_detail = _reviewer_progress_stage(progress, 2)
        assert stage == "a=done, b=done"
        assert pool_detail == "pool 2/2 done"


# ── Seam: channel → live .state → read_live_status ────────────────────────────


def _worker_style_state_update(story: dict):
    """Return a state_update_fn that mirrors the worker wrapper's detail semantics.

    ``SprintStoryState`` transitions replace ``detail`` wholesale, so each emit
    carries the complete review-scoped detail. This fake reproduces that: it sets
    ``phase`` and replaces ``detail`` on every call.
    """

    def _update(updates: dict) -> None:
        if "phase" in updates:
            story["phase"] = updates["phase"]
        incoming = updates.get("detail")
        if isinstance(incoming, dict):
            story["detail"] = dict(incoming)

    return _update


def _write_state(project_root: Path, run_id: str, story: dict) -> None:
    runs_dir = project_root / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.state").write_text(
        yaml.safe_dump({"sprint_name": "s", "stories": [story]}),
        encoding="utf-8",
    )


class TestReviewerProgressSeam:
    def test_review_pool_progress_flows_to_renderer(self, tmp_path: Path) -> None:
        story: dict = {"slug": "issue-1075", "path": "Issue #1075", "status": "running"}
        channel = ReviewerProgressChannel(
            reviewer_names=["deepseek", "gemini", "gpt"],
            phase="REVIEW",
            iteration=1,
            cost_usd=0.11,
            complexity="medium",
            state_update_fn=_worker_style_state_update(story),
        )

        # Fake run_agent_pool: fans out a sequence of per-reviewer iter/done
        # events through the channel, exactly as the real pool would.
        def fake_run_agent_pool(progress_cb) -> None:
            progress_cb({"label": "deepseek", "iter": 1, "tool_calls": 2})
            progress_cb({"label": "gemini", "iter": 1, "tool_calls": 1})
            progress_cb({"label": "gemini", "iter": 3, "tool_calls": 4})
            progress_cb({"label": "deepseek", "done": True})

        fake_run_agent_pool(channel.cb)
        # A transient retry on gemini surfaces the glyph live.
        channel.set_retry("gemini", 1, 2)

        # The live-state detail carries the structured contract.
        detail = story["detail"]
        assert set(detail["reviewer_progress"]) == {"deepseek", "gemini", "gpt"}
        assert detail["reviewer_progress"]["deepseek"]["done"] is True
        assert detail["reviewer_progress"]["gemini"]["iter"] == 3
        assert detail["reviewer_progress"]["gemini"]["retry"] == [1, 2]
        assert detail["reviewer_pool_size"] == 3
        assert isinstance(detail["last_reviewer_event_ts"], float)

        # Feed the state through the real renderer.
        _write_state(tmp_path, "run-x", story)
        entries = read_live_status("run-x", tmp_path)
        assert entries is not None
        entry = entries[0]
        assert "deepseek=done" in entry.stage
        assert "gemini=iter3 ↻r1/2" in entry.stage
        assert entry.detail == "pool 1/3 done"
        assert entry.last_event_ts == detail["last_reviewer_event_ts"]

    def test_plan_review_pool_progress_flows_to_renderer(self, tmp_path: Path) -> None:
        story: dict = {"slug": "issue-1075", "path": "Issue #1075", "status": "running"}
        channel = ReviewerProgressChannel(
            reviewer_names=["deepseek", "gemini"],
            phase="PLAN_REVIEW",
            iteration=0,
            cost_usd=0.17,
            complexity="large",
            state_update_fn=_worker_style_state_update(story),
        )
        channel.cb({"label": "deepseek", "done": True})
        channel.cb({"label": "gemini", "iter": 3, "tool_calls": 2})
        channel.set_retry("gemini", 1, 2)

        _write_state(tmp_path, "run-x", story)
        entries = read_live_status("run-x", tmp_path)
        assert entries is not None
        entry = entries[0]
        assert entry.phase == "PLAN_REVIEW"
        assert "deepseek=done" in entry.stage
        assert "gemini=iter3 ↻r1/2" in entry.stage
        assert entry.detail == "pool 1/2 done"

    def test_no_progress_data_falls_back(self, tmp_path: Path) -> None:
        # Before any reviewer event, the REVIEW branch must keep its legacy
        # cycle/verdict rendering rather than emit an empty per-reviewer stage.
        story = {
            "slug": "issue-1075",
            "path": "Issue #1075",
            "status": "running",
            "phase": "REVIEW",
            "detail": {"review_cycle": 1, "review_max_cycles": 3},
        }
        _write_state(tmp_path, "run-x", story)
        entries = read_live_status("run-x", tmp_path)
        assert entries is not None
        entry = entries[0]
        assert "iter" not in entry.stage  # not per-reviewer
        assert entry.detail == "running"
        assert entry.last_event_ts is None

    def test_retry_glyph_clears_on_next_progress_event(self, tmp_path: Path) -> None:
        # AC #3: the ↻rN/M marker must show "while it is retrying", not stick for
        # the rest of the review. A fresh iter/done event for the reviewer clears
        # the retry marker; the pool-done count then reflects the resolution.
        story: dict = {"slug": "s", "path": "s", "status": "running"}
        channel = ReviewerProgressChannel(
            reviewer_names=["gemini"],
            phase="REVIEW",
            iteration=1,
            cost_usd=0.0,
            complexity="medium",
            state_update_fn=_worker_style_state_update(story),
        )

        # A transient retry is recorded → glyph is outstanding.
        channel.set_retry("gemini", 1, 2)
        _write_state(tmp_path, "run-x", story)
        stage_mid = read_live_status("run-x", tmp_path)[0].stage
        assert "↻r1/2" in stage_mid

        # The retried attempt starts producing events → marker clears.
        channel.cb({"label": "gemini", "iter": 4, "tool_calls": 2})
        assert story["detail"]["reviewer_progress"]["gemini"]["retry"] is None
        _write_state(tmp_path, "run-x", story)
        stage_after = read_live_status("run-x", tmp_path)[0].stage
        assert "↻" not in stage_after
        assert "gemini=iter4" in stage_after

        # A subsequent successful-retry done event clears it too and counts done.
        channel.set_retry("gemini", 2, 2)
        channel.cb({"label": "gemini", "done": True})
        assert story["detail"]["reviewer_progress"]["gemini"]["retry"] is None
        _write_state(tmp_path, "run-x", story)
        entry = read_live_status("run-x", tmp_path)[0]
        assert "gemini=done" in entry.stage
        assert "↻" not in entry.stage
        assert entry.detail == "pool 1/1 done"

    def test_raising_state_update_fn_never_breaks_channel(self, tmp_path: Path) -> None:
        def _boom(_updates: dict) -> None:
            raise RuntimeError("state write failed")

        channel = ReviewerProgressChannel(
            reviewer_names=["a"],
            phase="REVIEW",
            iteration=1,
            cost_usd=0.0,
            complexity="small",
            state_update_fn=_boom,
        )
        # Must not propagate — reviewers can never be broken by a bad state sink.
        channel.cb({"label": "a", "iter": 1, "tool_calls": 1})
        channel.set_retry("a", 1, 2)
