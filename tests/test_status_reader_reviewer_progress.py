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

    def test_nudge_chip_appended(self) -> None:
        # issue #1087: an active reviewer that received a time-nudge shows a
        # ⚠Ns imminent-timeout chip, distinct from iter/retry progress.
        progress = {
            "gemini": {"iter": 3, "tool_calls": 1, "retry": None, "done": False, "nudge": 116},
        }
        stage, _pool = _reviewer_progress_stage(progress, 1)
        assert stage == "gemini=iter3 ⚠116s"

    def test_nudge_chip_coexists_with_retry(self) -> None:
        progress = {
            "gemini": {"iter": 3, "tool_calls": 1, "retry": [1, 2], "done": False, "nudge": 90},
        }
        stage, _pool = _reviewer_progress_stage(progress, 1)
        assert stage == "gemini=iter3 ↻r1/2 ⚠90s"

    def test_nudge_chip_suppressed_when_done(self) -> None:
        # A finalized reviewer never shows the imminent-timeout chip even if a
        # stale nudge value lingers.
        progress = {
            "gemini": {"iter": 5, "tool_calls": 2, "retry": None, "done": True, "nudge": 116},
        }
        stage, _pool = _reviewer_progress_stage(progress, 1)
        assert stage == "gemini=done"

    def test_missing_nudge_field_is_tolerated(self) -> None:
        # Legacy entries (pre-#1087) have no nudge key — must not raise.
        progress = {"gpt": {"iter": 2, "tool_calls": 0, "retry": None, "done": False}}
        stage, _pool = _reviewer_progress_stage(progress, 1)
        assert stage == "gpt=iter2"


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
        # On REVIEW entry (before any reviewer event) STAGE must still render the
        # cycle from the seeded detail rather than go blank (issue #1488).
        assert entry.stage == "cycle=1/3"
        assert entry.detail == "running"
        assert entry.last_event_ts is None

    def test_pool_start_shows_pool_zero_of_n_before_any_event(self, tmp_path: Path) -> None:
        # Constructing the channel (i.e. a reviewer pool starting) must emit the
        # seeded state immediately, so the row shows "pool 0/N done" rather than
        # legacy "running" before any reviewer emits its first event.
        story: dict = {"slug": "s", "path": "s", "status": "running"}
        ReviewerProgressChannel(
            reviewer_names=["deepseek", "gemini", "gpt"],
            phase="REVIEW",
            iteration=1,
            cost_usd=0.0,
            complexity="medium",
            state_update_fn=_worker_style_state_update(story),
        )

        # The seeded detail is present with every reviewer at zero progress.
        detail = story["detail"]
        assert set(detail["reviewer_progress"]) == {"deepseek", "gemini", "gpt"}
        assert all(not e["done"] for e in detail["reviewer_progress"].values())
        assert detail["reviewer_pool_size"] == 3
        assert isinstance(detail["last_reviewer_event_ts"], float)

        _write_state(tmp_path, "run-x", story)
        entry = read_live_status("run-x", tmp_path)[0]
        assert entry.detail == "pool 0/3 done"
        # STAGE lists each reviewer (no iterations yet → ellipsis, no ↻ glyph).
        assert "deepseek=…" in entry.stage
        assert "↻" not in entry.stage
        # EVENT AGE has a source from the very first frame.
        assert entry.last_event_ts == detail["last_reviewer_event_ts"]

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

    def test_time_nudge_flag_persists_until_finalize(self, tmp_path: Path) -> None:
        # issue #1087: when the runner emits a time-nudge for an active reviewer,
        # the row must flag imminent timeout (⚠Ns) and keep flagging it across
        # subsequent iter events, then clear once the reviewer finalizes.
        story: dict = {"slug": "s", "path": "s", "status": "running"}
        channel = ReviewerProgressChannel(
            reviewer_names=["gemini"],
            phase="REVIEW",
            iteration=1,
            cost_usd=0.0,
            complexity="medium",
            state_update_fn=_worker_style_state_update(story),
        )

        # The runner surfaces the nudge (as run_agent's progress_cb would).
        channel.cb({"label": "gemini", "iter": 3, "tool_calls": 4})
        channel.cb({"label": "gemini", "nudge": 116})
        assert story["detail"]["reviewer_progress"]["gemini"]["nudge"] == 116
        _write_state(tmp_path, "run-x", story)
        stage_nudged = read_live_status("run-x", tmp_path)[0].stage
        assert "gemini=iter3 ⚠116s" in stage_nudged

        # A later iter event must NOT clear the imminent-timeout flag — the
        # reviewer is still within seconds of its deadline (unlike ↻ retry).
        channel.cb({"label": "gemini", "iter": 4, "tool_calls": 2})
        assert story["detail"]["reviewer_progress"]["gemini"]["nudge"] == 116
        _write_state(tmp_path, "run-x", story)
        stage_still = read_live_status("run-x", tmp_path)[0].stage
        assert "gemini=iter4 ⚠116s" in stage_still

        # Finalizing clears the flag.
        channel.cb({"label": "gemini", "done": True})
        assert story["detail"]["reviewer_progress"]["gemini"]["nudge"] is None
        _write_state(tmp_path, "run-x", story)
        entry = read_live_status("run-x", tmp_path)[0]
        assert "gemini=done" in entry.stage
        assert "⚠" not in entry.stage

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


# ── REVIEW detail contract across entry / in-flight / completion (issue #1488) ─


class TestReviewDetailContract:
    """The REVIEW STAGE/DETAIL columns must stay meaningful across the whole
    cycle: on entry (cycle rendered, no stale gate_status leaking), while
    reviewers run (pool progress), and after a cycle completes (cycle STILL
    rendered alongside the P1/P2 counts). This drives the real renderer with the
    detail dicts the three coordinator writers now emit.
    """

    def test_entry_detail_renders_cycle_and_clears_prior_phase_gate_status(
        self, tmp_path: Path
    ) -> None:
        # On REVIEW entry the writer replaces detail wholesale with the cycle
        # context (review_phase.py). Even if the prior phase left gate_status
        # behind, the fresh detail must not carry it, and STAGE must render.
        story = {
            "slug": "issue-1488",
            "path": "Issue #1488",
            "status": "running",
            "phase": "REVIEW",
            "detail": {"review_cycle": 1, "review_max_cycles": 3},
        }
        assert "gate_status" not in story["detail"]
        _write_state(tmp_path, "run-x", story)
        entry = read_live_status("run-x", tmp_path)[0]
        assert entry.stage == "cycle=1/3"
        # No leftover GATE/VALIDATE fragment surfaces in DETAIL.
        assert "PASS" not in entry.detail
        assert entry.detail == "running"

    def test_in_flight_reviewer_progress_renders_pool_stage(self, tmp_path: Path) -> None:
        # While reviewers emit events the reviewer_progress dict drives STAGE, but
        # the cycle number must remain visible (issue #1488) — the operator saw
        # only per-reviewer progress for the whole 14m in-flight window.
        story: dict = {"slug": "issue-1488", "path": "Issue #1488", "status": "running"}
        channel = ReviewerProgressChannel(
            reviewer_names=["deepseek", "gemini"],
            phase="REVIEW",
            iteration=2,
            cost_usd=0.0,
            complexity="medium",
            state_update_fn=_worker_style_state_update(story),
        )
        channel.cb({"label": "deepseek", "iter": 1, "tool_calls": 2})
        channel.cb({"label": "gemini", "done": True})
        # review_cycle rides along with the reviewer-progress detail.
        assert story["detail"]["review_cycle"] == 2
        _write_state(tmp_path, "run-x", story)
        entry = read_live_status("run-x", tmp_path)[0]
        # STAGE prefixes the cycle onto the per-reviewer progress.
        assert entry.stage.startswith("cycle=2 ")
        assert "deepseek=iter1" in entry.stage
        assert entry.detail == "pool 1/2 done"

    def test_in_flight_stage_includes_cycle_with_max_when_present(self, tmp_path: Path) -> None:
        # When both review_cycle and review_max_cycles are present alongside
        # reviewer_progress, STAGE renders cycle=N/M then the per-reviewer stage.
        story = {
            "slug": "issue-1488",
            "path": "Issue #1488",
            "status": "running",
            "phase": "REVIEW",
            "detail": {
                "review_cycle": 2,
                "review_max_cycles": 3,
                "reviewer_progress": {
                    "deepseek": {"iter": 4, "tool_calls": 2, "retry": None, "done": False},
                    "gemini": {"iter": 5, "tool_calls": 3, "retry": None, "done": True},
                },
                "reviewer_pool_size": 2,
            },
        }
        _write_state(tmp_path, "run-x", story)
        entry = read_live_status("run-x", tmp_path)[0]
        assert entry.stage == "cycle=2/3 deepseek=iter4, gemini=done"
        assert entry.detail == "pool 1/2 done"

    def test_completion_detail_keeps_cycle_alongside_counts(self, tmp_path: Path) -> None:
        # After a cycle completes the writer emits {review_cycle, review_max_cycles,
        # review_p1, review_p2}. STAGE must still render the cycle and DETAIL the
        # counts — neither goes blank (the original symptom).
        story = {
            "slug": "issue-1488",
            "path": "Issue #1488",
            "status": "running",
            "phase": "REVIEW",
            "detail": {
                "review_cycle": 2,
                "review_max_cycles": 3,
                "review_p1": 0,
                "review_p2": 0,
            },
        }
        _write_state(tmp_path, "run-x", story)
        entry = read_live_status("run-x", tmp_path)[0]
        assert entry.stage == "cycle=2/3"
        assert entry.detail == "0P1 0P2"
