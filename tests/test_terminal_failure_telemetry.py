"""Seam test: terminal-failure rows must render last-known telemetry.

Drives the runner-side worker-timeout path end-to-end through the canonical
SprintStateWriter, the snapshot helper, the sprint-summary writer, and the
status_reader render path. Verifies that both the live state file and the
completed sprint-summary expose the lifecycle phase, model, cost, and elapsed
seconds for a story that died in DEV.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint import runner as runner_mod
from theforge.sprint.audit import _write_sprint_summary
from theforge.sprint.state_writer import SprintStateWriter
from theforge.sprint.status_reader import read_completed_status, read_live_status
from theforge.sprint.story_state import StoryOutcome


def _make_writer(tmp_path: Path) -> SprintStateWriter:
    writer = SprintStateWriter(
        run_id="run-failtel",
        project_root=tmp_path,
        sprint_name="failtel-sprint",
        sprint_id="sprint-failtel",
    )
    writer.init([{"slug": "issue-1326", "path": "Issue #1326", "outcome": "waiting"}])
    return writer


def test_snapshot_last_known_reads_canonical_state(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path)
    started_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=60)
    writer.update(
        "issue-1326",
        status="running",
        phase="DEV",
        cost_usd=1.23,
        current_model="claude-opus",
        started_at=started_at.isoformat(),
    )

    snapshot = runner_mod._snapshot_last_known("issue-1326", writer)

    assert snapshot["last_phase"] == "DEV"
    assert snapshot["last_model"] == "claude-opus"
    assert snapshot["last_cost"] == pytest.approx(1.23)
    assert snapshot["last_started_at"] is not None
    assert abs((snapshot["last_started_at"] - started_at).total_seconds()) < 1.0


def test_snapshot_last_known_handles_missing_state() -> None:
    snapshot = runner_mod._snapshot_last_known("issue-x", None)
    assert snapshot == {
        "last_phase": None,
        "last_model": None,
        "last_cost": None,
        "last_started_at": None,
    }


def test_terminal_failure_renders_last_known_telemetry(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path)
    started_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=120)
    writer.update(
        "issue-1326",
        status="running",
        phase="DEV",
        cost_usd=1.23,
        current_model="claude-opus",
        started_at=started_at.isoformat(),
    )

    # Snapshot at the moment the worker would have timed out.
    snapshot = runner_mod._snapshot_last_known("issue-1326", writer)
    assert snapshot["last_phase"] == "DEV"

    timed_out_at = datetime.datetime.now(datetime.timezone.utc)
    story_started_at = snapshot["last_started_at"] or started_at

    # Mirror the runner's terminal transition: preserve last_phase via
    # state_writer.update before _set_outcome flips phase to ESCALATE.
    writer.update(
        "issue-1326",
        status=StoryOutcome.FAILED,
        phase="ESCALATE",
        last_phase=snapshot["last_phase"],
        finished_at=timed_out_at.isoformat(),
    )

    # Synthetic CoordinatorState/Result like the runner builds at timeout.
    timeout_state = CoordinatorState(
        phase=Phase.ESCALATE,
        started_at=story_started_at.isoformat(),
        workspace_path=tmp_path / "workspace",
        log_dir=tmp_path / ".forge" / "logs" / "failtel-sprint" / "issue-1326",
        error="Story deadline exhausted (>60s of working time) during phase DEV",
        error_type="TimeoutError",
    )
    timeout_result = CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=timeout_state,
        message=(
            "Story deadline exhausted after 60s of working time — not a review or quality failure"
        ),
    )

    sprint_log_dir = tmp_path / ".forge" / "logs" / "failtel-sprint"
    sprint_log_dir.mkdir(parents=True, exist_ok=True)

    sprint_result = SimpleNamespace(
        results=[("issue:1326", timeout_result)],
        stopped_reason=None,
    )
    manifest = SimpleNamespace(
        name="failtel-sprint",
        budget_usd=50.0,
        max_parallel=1,
    )

    _write_sprint_summary(
        manifest=manifest,
        result=sprint_result,
        canonical_refs=["issue:1326"],
        started_at=started_at,
        finished_at=timed_out_at,
        duration=(timed_out_at - started_at).total_seconds(),
        sprint_log_dir=sprint_log_dir,
        story_times={"issue-1326": (story_started_at, timed_out_at)},
        batch_assignments={"issue-1326": 1},
        slug_map={"issue:1326": "issue-1326"},
        run_id="run-failtel",
        tasks_by_slug={"issue-1326": SimpleNamespace(depends_on=[])},
        sprint_id="sprint-failtel",
        project_root=tmp_path,
        story_state=writer.story_state,
        live_telemetry_snapshots={"issue-1326": snapshot},
    )

    summary_path = sprint_log_dir / "sprint-summary.yaml"
    assert summary_path.exists()
    completed_entries = read_completed_status(summary_path)
    assert len(completed_entries) == 1
    completed = completed_entries[0]
    assert completed.phase == "DEV", f"phase should preserve DEV, got {completed.phase!r}"
    assert completed.model == "claude-opus"
    assert completed.cost_usd == pytest.approx(1.23)
    assert completed.elapsed_seconds is not None
    assert completed.elapsed_seconds >= 60

    # Live state must agree on the lifecycle phase. The state file was last
    # written by writer.update above (status=FAILED, phase=ESCALATE,
    # last_phase=DEV). The renderer should prefer last_phase for failed rows.
    live_entries = read_live_status("run-failtel", tmp_path)
    assert live_entries is not None
    live = next(e for e in live_entries if e.slug == "issue-1326")
    assert live.status == "failed"
    assert live.phase == "DEV", f"live phase should preserve DEV, got {live.phase!r}"
