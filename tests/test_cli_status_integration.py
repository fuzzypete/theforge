"""Integration tests for forge status using real on-disk run layouts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import yaml

from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.sprint.audit import persist_accumulated_story_state


def _make_forge_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=300,
            allowed_tools=("Read",),
        ),
        preflight_profile=ModelProfile(
            name="preflight",
            cli="claude",
            model="sonnet",
            budget_usd=0.5,
            timeout_seconds=120,
            allowed_tools=("Read",),
        ),
        review_pool=[],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig(enabled=False),
        log=LogConfig(enabled=False),
    )


def _write_forge_yaml(tmp_path: Path) -> Path:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    return forge_yaml


def _write_pid_file(project_root: Path, run_id: str, slug: str, pid: int | None = None) -> None:
    runs_dir = project_root / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.joinpath(f"{run_id}.pid").write_text(
        f"{pid or os.getpid()}\n{slug}\n",
        encoding="utf-8",
    )


def _write_state_file(
    project_root: Path,
    run_id: str,
    sprint_name: str,
    stories: list[dict],
) -> Path:
    runs_dir = project_root / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    state_path = runs_dir / f"{run_id}.state"
    state_path.write_text(
        yaml.dump({"sprint_name": sprint_name, "stories": stories}, sort_keys=False),
        encoding="utf-8",
    )
    return state_path


def _write_summary_file(
    project_root: Path,
    sprint_name: str,
    run_id: str,
    stories: list[dict],
) -> Path:
    log_dir = project_root / ".forge" / "logs" / sprint_name
    log_dir.mkdir(parents=True, exist_ok=True)
    summary_path = log_dir / "sprint-summary.yaml"
    summary_path.write_text(
        yaml.dump(
            {
                "sprint": {
                    "name": sprint_name,
                    "run_id": run_id,
                    "budget_usd": 10.0,
                    "max_parallel": 2,
                    "total_cost_usd": sum(float(story.get("cost_usd", 0.0)) for story in stories),
                    "started_at": "2026-01-01T00:00:00Z",
                    "finished_at": "2026-01-01T00:30:00Z",
                    "duration_seconds": 1800.0,
                    "specs_total": len(stories),
                    "specs_succeeded": 0,
                    "specs_failed": 0,
                    "specs_skipped": 0,
                    "stopped_reason": None,
                    "ci_break_slug": None,
                },
                "stories": stories,
                "iteration_usage_distribution": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return summary_path


def _write_redirect(
    project_root: Path,
    predecessor_run_id: str,
    new_run_id: str,
    *,
    new_log: str = "/tmp/run.log",
) -> None:
    runs_dir = project_root / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.joinpath(f"{predecessor_run_id}.redirect").write_text(
        json.dumps({"new_run_id": new_run_id, "new_log": new_log}),
        encoding="utf-8",
    )


def _write_single_run_log(project_root: Path, slug: str, run_id: str, content: str) -> None:
    log_dir = project_root / ".forge" / "logs" / slug
    log_dir.mkdir(parents=True, exist_ok=True)
    log_dir.joinpath(f"run-{run_id}.log").write_text(content, encoding="utf-8")


def _run_cmd_status(
    tmp_path: Path,
    capsys: object,
    *,
    run_id: str | None = None,
    recent: bool = False,
    last: bool = False,
    watch: int | None = None,
    no_color: bool = False,
) -> tuple[int, str]:
    from theforge.cli import cmd_status

    forge_yaml = _write_forge_yaml(tmp_path)
    config = _make_forge_config(tmp_path)
    args = argparse.Namespace(
        run_id=run_id,
        recent=recent,
        last=last,
        watch=watch,
        no_color=no_color,
    )

    with (
        patch("theforge.cli.status._find_config", return_value=forge_yaml),
        patch("theforge.cli.status.load_config", return_value=config),
    ):
        rc = cmd_status(args)

    captured = capsys.readouterr()
    return rc, captured.out


def _live_story(issue: int, *, status: str, phase: str, cost_usd: float) -> dict:
    return {
        "slug": f"issue-{issue}",
        "path": f"Issue #{issue}",
        "status": status,
        "phase": phase,
        "cost_usd": cost_usd,
        "bundle_candidate": False,
        "blocked_by": [],
        "detail": {},
    }


def test_status_shows_live_mid_run_sprint_with_story_rows(tmp_path: Path, capsys: object) -> None:
    _write_pid_file(tmp_path, "live-run", "my-live-sprint")
    _write_state_file(
        tmp_path,
        "live-run",
        "my-live-sprint",
        [
            {
                "slug": "issue-20",
                "path": "Issue #20",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.05,
                "bundle_candidate": False,
                "blocked_by": [],
                "complexity": "medium",
                "detail": {"dev_iteration": 2, "dev_max_iterations": 3},
            },
            {
                "slug": "issue-21",
                "path": "Issue #21",
                "status": "waiting",
                "phase": "PREFLIGHT",
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": [],
                "detail": {
                    "preflight_verdict": "PROCEED",
                    "preflight_sufficiency": "needs_planning",
                },
            },
        ],
    )

    rc, out = _run_cmd_status(tmp_path, capsys)

    assert rc == 0
    assert "Sprint: my-live-sprint  run: live-run  [live]" in out
    assert "Issue #20" in out
    assert "Issue #21" in out
    assert "iter=2/3" in out


def test_status_shows_sprint_view_during_sprint_reexec_startup_window(
    tmp_path: Path, capsys: object
) -> None:
    _write_pid_file(tmp_path, "run-new", "reexec-sprint")
    _write_state_file(
        tmp_path,
        "run-old",
        "reexec-sprint",
        [
            {
                "slug": "issue-965",
                "path": "Issue #965",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 1.25,
                "bundle_candidate": False,
                "blocked_by": [],
                "detail": {"dev_iteration": 1, "dev_max_iterations": 3},
            }
        ],
    )
    _write_redirect(tmp_path, "run-old", "run-new")

    rc, out = _run_cmd_status(tmp_path, capsys)

    assert rc == 0
    assert "Sprint: reexec-sprint  run: run-new  [live]" in out
    assert "Issue #965" in out
    assert "RUN ID" not in out


def test_status_shows_single_run_view_during_single_run_reexec_startup_window(
    tmp_path: Path, capsys: object
) -> None:
    _write_pid_file(tmp_path, "run-new", "single-story")
    _write_redirect(tmp_path, "run-old", "run-new")

    rc, out = _run_cmd_status(tmp_path, capsys)

    assert rc == 0
    assert "Sprint:" not in out
    assert "RUN ID" in out
    assert "single-story" in out
    assert "run-new" in out


def test_status_shows_completed_sprint_from_summary_with_all_rows(
    tmp_path: Path, capsys: object
) -> None:
    _write_summary_file(
        tmp_path,
        "completed-sprint",
        "run-123",
        [
            {"slug": "issue-1", "path": "Issue #1", "outcome": "DONE", "cost_usd": 0.42},
            {"slug": "issue-2", "path": "Issue #2", "outcome": "ESCALATE", "cost_usd": 0.10},
            {"slug": "issue-3", "path": "Issue #3", "outcome": "SKIPPED", "cost_usd": 0.0},
        ],
    )

    rc, out = _run_cmd_status(tmp_path, capsys)

    assert rc == 0
    assert "Sprint: completed-sprint  run: run-123  [completed]" in out
    assert "Issue #1" in out
    assert "Issue #2" in out
    assert "Issue #3" in out


def test_status_preserves_prior_story_run_ids_in_completed_sprint_view(
    tmp_path: Path, capsys: object
) -> None:
    _write_summary_file(
        tmp_path,
        "rollover-sprint",
        "run-new",
        [
            {
                "slug": "issue-959",
                "path": "Issue #959",
                "outcome": "DONE",
                "cost_usd": 10.31,
                "story_run_id": "run-old",
            },
            {
                "slug": "issue-960",
                "path": "Issue #960",
                "outcome": "DONE",
                "cost_usd": 1.00,
                "story_run_id": "run-new",
            },
        ],
    )

    rc, out = _run_cmd_status(tmp_path, capsys, run_id="run-old")

    assert rc == 0
    assert "Sprint: rollover-sprint  run: run-old  [completed]" in out
    assert "Issue #959" in out
    assert "Issue #960" in out


def test_status_preserves_already_done_story_across_later_sprint_reruns(
    tmp_path: Path, capsys: object
) -> None:
    state_path = _write_state_file(
        tmp_path,
        "run-reexec",
        "issues-959,960",
        [
            {
                "slug": "issue-960",
                "path": "Issue #960",
                "status": "running",
                "phase": "PLAN",
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": [],
                "detail": {"plan_attempt": 1, "plan_max_attempts": 3},
            }
        ],
    )
    state_data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    state_data["sprint_id"] = "sprint-abc"
    state_path.write_text(yaml.dump(state_data, sort_keys=False), encoding="utf-8")
    persist_accumulated_story_state(
        "sprint-abc",
        "issues-959,960",
        tmp_path,
        [
            {
                "canonical_ref": "issue:959",
                "slug": "issue-959",
                "path": "Issue #959",
                "outcome": "ALREADY_DONE",
                "cost_usd": 10.31,
                "depends_on": [],
            }
        ],
    )
    _write_pid_file(tmp_path, "run-reexec", "issues-959,960")

    rc, out = _run_cmd_status(tmp_path, capsys)

    assert rc == 0
    assert "Issue #959" in out
    assert "ALREADY_DONE" in out
    assert "Issue #960" in out


def test_status_shows_stopped_after_forge_stop(tmp_path: Path, capsys: object) -> None:
    from theforge import detach

    _write_single_run_log(tmp_path, "issues-828", "6f59d22b", "[forge] ▸ DEV work\n")
    detach.write_run_ended("6f59d22b", tmp_path, "stopped", force=True)

    rc, out = _run_cmd_status(tmp_path, capsys, run_id="6f59d22b")

    assert rc == 0
    assert "STOPPED" in out
    assert "RUNNING" not in out


def test_status_shows_all_live_sprints_when_multiple_are_active(
    tmp_path: Path, capsys: object
) -> None:
    _write_pid_file(tmp_path, "run-a", "issues-1186,1192")
    _write_pid_file(tmp_path, "run-b", "issues-1186")
    _write_state_file(
        tmp_path,
        "run-a",
        "issues-1186,1192",
        [
            _live_story(1186, status="done", phase="DONE", cost_usd=0.11),
            _live_story(1192, status="running", phase="DEV", cost_usd=0.42),
        ],
    )
    _write_state_file(
        tmp_path,
        "run-b",
        "issues-1186",
        [_live_story(1186, status="running", phase="PLAN", cost_usd=0.19)],
    )

    rc, out = _run_cmd_status(tmp_path, capsys)

    assert rc == 0
    assert "Sprint: issues-1186,1192  run: run-a  [live]" in out
    assert "Sprint: issues-1186  run: run-b  [live]" in out
    assert "Issue #1192" in out
    assert "Issue #1186" in out


def test_watch_loop_renders_all_live_sprints_in_one_frame(tmp_path: Path, capsys: object) -> None:
    from theforge.cli import status_watch

    _write_pid_file(tmp_path, "run-a", "issues-1186,1192")
    _write_pid_file(tmp_path, "run-b", "issues-1186")
    _write_state_file(
        tmp_path,
        "run-a",
        "issues-1186,1192",
        [_live_story(1192, status="running", phase="DEV", cost_usd=0.42)],
    )
    _write_state_file(
        tmp_path,
        "run-b",
        "issues-1186",
        [_live_story(1186, status="running", phase="PLAN", cost_usd=0.19)],
    )

    with patch.object(status_watch, "is_tty", return_value=False):
        rc = status_watch.run_watch_loop(
            ["run-a", "run-b"],
            tmp_path,
            interval=0.01,
            color=False,
            follow_active_runs=True,
            sleep_fn=lambda _s: None,
            max_frames=1,
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "run=run-a" in out
    assert "run=run-b" in out
    assert "Issue #1192" in out
    assert "Issue #1186" in out


def test_watch_loop_surfaces_cost_delta_after_live_state_cost_update(
    tmp_path: Path, capsys: object
) -> None:
    from theforge.cli import status_watch

    _write_pid_file(tmp_path, "run-a", "issues-1240")
    _write_state_file(
        tmp_path,
        "run-a",
        "issues-1240",
        [_live_story(1240, status="running", phase="PLAN", cost_usd=0.38)],
    )

    def bump_cost(_seconds: float) -> None:
        _write_state_file(
            tmp_path,
            "run-a",
            "issues-1240",
            [_live_story(1240, status="running", phase="PLAN", cost_usd=0.42)],
        )

    with patch.object(status_watch, "is_tty", return_value=False):
        rc = status_watch.run_watch_loop(
            "run-a",
            tmp_path,
            interval=0.01,
            color=False,
            sleep_fn=bump_cost,
            max_frames=2,
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "+$0.04" in out


def test_watch_loop_drops_finished_sprint_on_next_frame(tmp_path: Path, capsys: object) -> None:
    from theforge.cli import status_watch

    _write_pid_file(tmp_path, "run-a", "issues-1186,1192")
    _write_pid_file(tmp_path, "run-b", "issues-1186")
    _write_state_file(
        tmp_path,
        "run-a",
        "issues-1186,1192",
        [_live_story(1192, status="running", phase="DEV", cost_usd=0.42)],
    )
    _write_state_file(
        tmp_path,
        "run-b",
        "issues-1186",
        [_live_story(1186, status="running", phase="PLAN", cost_usd=0.19)],
    )

    def remove_run_a(_seconds: float) -> None:
        (tmp_path / ".forge" / "runs" / "run-a.pid").unlink()

    with patch.object(status_watch, "is_tty", return_value=False):
        rc = status_watch.run_watch_loop(
            ["run-a", "run-b"],
            tmp_path,
            interval=0.01,
            color=False,
            follow_active_runs=True,
            sleep_fn=remove_run_a,
            max_frames=2,
        )

    out = capsys.readouterr().out
    assert rc == 0
    assert "run=run-a" in out
    assert out.count("run=run-b") == 2
