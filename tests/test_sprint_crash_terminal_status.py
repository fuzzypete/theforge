"""Terminal disposition of a sprint that dies on an unhandled exception (issue-1979).

A run's reported disposition must be derived from how it actually ended. These
tests cover the write side (``.ended`` records ``failed`` plus the terminating
cause) and the read side (per-story state written before the crash is reported
as interrupted, not running).
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from sprint_test_helpers import stub_resolved

from theforge import detach
from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


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
        review_pool=[
            ModelProfile(
                name="claude-reviewer",
                provider="anthropic",
                model="claude-opus-4-6",
                budget_usd=1.0,
                timeout_seconds=120,
                allowed_tools=("Read", "Grep"),
            )
        ],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig.of(enabled=False),
        log=LogConfig(enabled=False),
    )


def _sprint_result():
    from theforge.sprint import SprintResult

    return SprintResult(
        name="test",
        specs_total=1,
        specs_succeeded=1,
        specs_failed=0,
        specs_skipped=0,
        total_cost_usd=0.0,
        budget_usd=0.0,
    )


def _query_mode_args() -> argparse.Namespace:
    return argparse.Namespace(name=None, no_notify=True, fg=True, detach=False)


def _run_query_mode(tmp_path: Path, config: ForgeConfig, run_id: str, run_sprint_mock):
    from theforge.cli.sprint import _run_query_mode as _impl

    task = MagicMock()
    task.slug = "issue-42"
    task.depends_on = []
    resolved = MagicMock()
    resolved.stories = [(task, MagicMock(), "issue:42")]

    with (
        patch(
            "theforge.sprint.query.fetch_issues_for_milestone",
            return_value=[{"number": 42, "title": "Story"}],
        ),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=resolved),
        patch("theforge.cli.sprint._acquire_launch_locks", return_value=([], None, {})),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", **run_sprint_mock),
        patch("theforge.detach.remove_pid"),
    ):
        return _impl(
            args=_query_mode_args(),
            config=config,
            config_path=tmp_path / "forge.yaml",
            milestone="v1.0",
            label=None,
            budget_str="5",
            dry_run=False,
            max_parallel=1,
            auto_merge=False,
            interactive=False,
            resume=False,
            no_pull=False,
            _daemon=MagicMock(is_daemon_running=MagicMock(return_value=False)),
            _detach=detach,
            _generate_run_id=MagicMock(return_value=run_id),
        )


def _make_state_file(tmp_path: Path, run_id: str, sprint_name: str, stories: list[dict]) -> Path:
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    state_path = runs_dir / f"{run_id}.state"
    with open(state_path, "w", encoding="utf-8") as f:
        yaml.dump({"sprint_name": sprint_name, "stories": stories}, f)
    return state_path


def _run_sprint_status(tmp_path: Path, run_id: str) -> tuple[int, str]:
    from theforge.cli.sprint_status import cmd_sprint_status

    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    fake_config = MagicMock()
    fake_config.project_root = tmp_path

    args = argparse.Namespace(run_id=run_id)
    buf = io.StringIO()
    with (
        patch("theforge.cli.shared._find_config", return_value=forge_yaml),
        patch("theforge.config.load_config", return_value=fake_config),
        # No GitHub access in tests — titles stay unresolved.
        patch(
            "theforge.sprint.query.fetch_issues_by_numbers",
            side_effect=RuntimeError("not found in this repository"),
        ),
        patch("sys.stdout", buf),
    ):
        code = cmd_sprint_status(args)
    return code, buf.getvalue()


# ── detach: terminal marker carries the cause ────────────────────────────────


def test_write_run_ended_persists_cause(tmp_path: Path) -> None:
    detach.write_run_ended("run1", tmp_path, "failed", cause="Broken baseline: gate FAIL")

    assert detach.read_run_ended("run1", tmp_path) == "failed"
    assert detach.read_run_ended_record("run1", tmp_path) == (
        "failed",
        "Broken baseline: gate FAIL",
    )


def test_write_run_ended_flattens_multiline_cause(tmp_path: Path) -> None:
    detach.write_run_ended("run2", tmp_path, "failed", cause="line one\nline two")

    outcome, cause = detach.read_run_ended_record("run2", tmp_path)
    assert outcome == "failed"
    assert cause == "line one line two"


def test_read_run_ended_record_legacy_single_line(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "legacy.ended").write_text("completed", encoding="utf-8")

    assert detach.read_run_ended_record("legacy", tmp_path) == ("completed", None)
    assert detach.read_run_ended_record("missing", tmp_path) is None


def test_read_run_status_reports_failed_phase(tmp_path: Path) -> None:
    detach.write_run_ended("run3", tmp_path, "failed", cause="RuntimeError: boom")

    status = detach.read_run_status("run3", "sprint-slug", tmp_path)
    assert status["phase"] == "FAILED"


# ── write side: sprint CLI records how the run actually ended ────────────────


def test_query_mode_records_failed_outcome_with_cause(tmp_path: Path) -> None:
    config = _make_forge_config(tmp_path)

    rc = _run_query_mode(
        tmp_path,
        config,
        "crash-run",
        {"side_effect": RuntimeError("Broken baseline: configured gate failed")},
    )

    assert rc == 1
    outcome, cause = detach.read_run_ended_record("crash-run", tmp_path)
    assert outcome == "failed"
    assert "Broken baseline" in cause
    assert "RuntimeError" in cause


def test_query_mode_records_completed_on_success(tmp_path: Path) -> None:
    config = _make_forge_config(tmp_path)

    rc = _run_query_mode(tmp_path, config, "ok-run", {"return_value": _sprint_result()})

    assert rc == 0
    assert detach.read_run_ended_record("ok-run", tmp_path) == ("completed", None)


def test_manifest_mode_records_failed_outcome_with_cause(tmp_path: Path) -> None:
    from theforge.cli import cmd_sprint

    config = _make_forge_config(tmp_path)
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text("stories: []\n", encoding="utf-8")
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    args = argparse.Namespace(
        manifest=str(manifest_path),
        config=str(forge_yaml),
        fg=True,
        detach=False,
        resume=False,
        milestone=None,
        label=None,
        issues=None,
        budget=None,
        parallel=None,
        name=None,
        dry_run=False,
        auto_merge=False,
        interactive=False,
        verbose=False,
        no_notify=True,
        no_pull=False,
        base_branch=None,
    )

    captured: dict = {}

    def _capture_write(run_id, project_root, outcome="stopped", *, force=False, cause=None):
        captured["run_id"] = run_id
        captured["outcome"] = outcome
        captured["cause"] = cause

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint.run_sprint", side_effect=RuntimeError("sprint crash")),
        patch("theforge.sprint.runner.resolve_from_manifest", return_value=stub_resolved()),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint._acquire_launch_locks", return_value=([], None, {})),
        patch("theforge.detach.remove_pid"),
        patch("theforge.detach.write_run_ended", side_effect=_capture_write),
    ):
        rc = cmd_sprint(args)

    assert rc == 1
    assert captured["outcome"] == "failed"
    assert "sprint crash" in captured["cause"]


def test_cmd_run_records_failed_outcome_on_exception(tmp_path: Path) -> None:
    from theforge.cli import cmd_run

    config = _make_forge_config(tmp_path)
    story = tmp_path / "story.md"
    story.write_text("# Story\nDo the thing.\n", encoding="utf-8")
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    args = argparse.Namespace(
        story=str(story),
        slug=None,
        config=str(forge_yaml),
        plan=None,
        from_phase=None,
        until=None,
        reviewers=None,
        max_cycles=None,
        resume=False,
        dev_model=None,
        plan_model=None,
        dry_run=False,
        interactive=False,
        auto_merge=False,
        verbose=False,
        no_notify=True,
        fg=True,
        base_branch=None,
        no_pull=False,
    )

    captured: dict = {}

    def _capture_write(run_id, project_root, outcome="stopped", *, force=False, cause=None):
        captured["outcome"] = outcome
        captured["cause"] = cause

    with (
        patch("theforge.cli.run.load_config", return_value=config),
        patch("theforge.cli.run.run_task", side_effect=RuntimeError("agent crash")),
        patch("theforge.detach.remove_pid"),
        patch("theforge.detach.write_run_ended", side_effect=_capture_write),
    ):
        with pytest.raises(RuntimeError, match="agent crash"):
            cmd_run(args)

    assert captured["outcome"] == "failed"
    assert "agent crash" in captured["cause"]


def test_backstop_records_failure_when_exception_escapes_cmd_sprint() -> None:
    """An exception escaping cmd_sprint must not leave the backstop on "completed"."""
    import theforge.cli.sprint as sprint_cli

    original = dict(sprint_cli._BACKSTOP)
    try:
        sprint_cli._BACKSTOP.update({"outcome": "completed", "cause": None})
        with patch.object(sprint_cli, "_cmd_sprint", side_effect=RuntimeError("early crash")):
            with pytest.raises(RuntimeError, match="early crash"):
                sprint_cli.cmd_sprint(argparse.Namespace())
        assert sprint_cli._BACKSTOP["outcome"] == "failed"
        assert "early crash" in (sprint_cli._BACKSTOP["cause"] or "")
    finally:
        sprint_cli._BACKSTOP.update(original)


def test_backstop_writes_recorded_outcome(tmp_path: Path) -> None:
    import theforge.cli.sprint as sprint_cli

    original = dict(sprint_cli._BACKSTOP)
    try:
        sprint_cli._BACKSTOP.update({"outcome": "failed", "cause": "RuntimeError: boom"})
        sprint_cli._backstop_run_ended("backstop-run", tmp_path)
    finally:
        sprint_cli._BACKSTOP.update(original)

    assert detach.read_run_ended_record("backstop-run", tmp_path) == (
        "failed",
        "RuntimeError: boom",
    )


# ── read side: in-flight stories are reported as interrupted ─────────────────


def test_mark_interrupted_entries_rewrites_running_stories() -> None:
    from theforge.sprint.status_reader import StoryStatusEntry, mark_interrupted_entries

    entries = [
        StoryStatusEntry(
            slug="issue-1972",
            path="Issue #1972",
            status="running",
            phase="DEV",
            cost_usd=1.0,
            detail="running",
        ),
        StoryStatusEntry(
            slug="issue-1946",
            path="Issue #1946",
            status="done",
            phase="DONE",
            cost_usd=0.5,
            detail="merged",
        ),
    ]

    result = mark_interrupted_entries(entries)

    assert result[0].status == "interrupted"
    assert result[0].phase == "DEV", "last known phase is preserved as history"
    assert "interrupted" in result[0].detail
    assert "last: running" in result[0].detail
    # Non-in-flight entries are untouched.
    assert result[1] == entries[1]
    # Input list is not mutated.
    assert entries[0].status == "running"


def test_crashed_sprint_status_names_cause_and_interrupts_stories(tmp_path: Path) -> None:
    """Regression for run f13f739e19c3: [completed] with stories still running."""
    _make_state_file(
        tmp_path,
        "f13f739e19c3",
        "issues-1972,1946,1945,1934",
        [
            {
                "slug": "issue-1945",
                "path": "Issue #1945",
                "status": "running",
                "phase": "WORKSPACE",
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": [],
            },
            {
                "slug": "issue-1972",
                "path": "Issue #1972",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 3.75,
                "bundle_candidate": False,
                "blocked_by": [],
            },
        ],
    )
    detach.write_run_ended(
        "f13f739e19c3",
        tmp_path,
        "failed",
        cause="RuntimeError: Broken baseline: configured gate failed on sprint merge base",
    )

    code, output = _run_sprint_status(tmp_path, "f13f739e19c3")

    assert code == 0
    assert "[failed]" in output
    assert "[completed]" not in output
    assert "Broken baseline" in output
    assert "interrupted" in output
    # No story may be described as running by a process that no longer exists.
    assert " running " not in output.replace("last: running", "")


def test_stopped_sprint_stories_are_interrupted_not_running(tmp_path: Path) -> None:
    _make_state_file(
        tmp_path,
        "stopped-1979",
        "stopped-sprint",
        [
            {
                "slug": "issue-1",
                "path": "Issue #1",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.05,
                "bundle_candidate": False,
                "blocked_by": [],
            }
        ],
    )
    detach.write_run_ended("stopped-1979", tmp_path, "stopped")

    code, output = _run_sprint_status(tmp_path, "stopped-1979")

    assert code == 0
    assert "[stopped]" in output
    assert "interrupted" in output


def test_live_sprint_still_reports_running(tmp_path: Path) -> None:
    """Guard: reconciliation must not touch a run whose process is alive."""
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "live-1979.pid").write_text("99999\nlive-sprint\n", encoding="utf-8")
    _make_state_file(
        tmp_path,
        "live-1979",
        "live-sprint",
        [
            {
                "slug": "issue-1",
                "path": "Issue #1",
                "status": "running",
                "phase": "DEV",
                "cost_usd": 0.05,
                "bundle_candidate": False,
                "blocked_by": [],
            }
        ],
    )

    code, output = _run_sprint_status(tmp_path, "live-1979")

    assert code == 0
    assert "[live]" in output
    assert "running" in output
    assert "interrupted" not in output
