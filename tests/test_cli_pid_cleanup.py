"""Tests for PID file cleanup on exception in cmd_run, cmd_sprint, and _run_query_mode.

These tests verify that remove_pid is called unconditionally via try/finally,
even when run_task or run_sprint raises an uncaught exception.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.cli import cmd_run
from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase

# ── Shared helpers ────────────────────────────────────────────────────


def _api_profile(
    name: str, provider: str = "anthropic", model: str = "claude-opus-4-6"
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=("Read", "Grep"),
    )


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
        review_pool=[_api_profile("claude-reviewer")],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig(enabled=False),
        log=LogConfig(enabled=False),
    )


def _stub_result(phase: Phase = Phase.DONE, success: bool = True) -> CoordinatorResult:
    return CoordinatorResult(
        success=success,
        phase=phase,
        state=CoordinatorState(),
        message="ok",
    )


def _make_run_args(tmp_path: Path, *, fg: bool = True) -> argparse.Namespace:
    story = tmp_path / "story.md"
    story.write_text("# Story\nDo the thing.\n", encoding="utf-8")
    forge_yaml = tmp_path / "forge.yaml"
    if not forge_yaml.exists():
        forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    return argparse.Namespace(
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
        fg=fg,
        base_branch=None,
        no_pull=False,
    )


def _make_sprint_args(tmp_path: Path, *, fg: bool = True) -> argparse.Namespace:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text("stories: []\n", encoding="utf-8")
    forge_yaml = tmp_path / "forge.yaml"
    if not forge_yaml.exists():
        forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    return argparse.Namespace(
        manifest=str(manifest_path),
        config=str(forge_yaml),
        fg=fg,
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


# ── TestPidFileCleanupOnException ─────────────────────────────────────


class TestPidFileCleanupOnException:
    """PID file is removed even when run_task / run_sprint raises an exception."""

    def test_cmd_run_removes_pid_on_run_task_exception(self, tmp_path):
        """remove_pid is called when run_task raises an uncaught exception."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, fg=True)

        removed: list[str] = []

        def _fake_remove_pid(run_id: str, project_root: object) -> None:
            removed.append(run_id)

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", side_effect=RuntimeError("agent crash")),
            patch("theforge.detach.remove_pid", side_effect=_fake_remove_pid),
        ):
            with pytest.raises(RuntimeError, match="agent crash"):
                cmd_run(args)

        assert len(removed) == 1, "remove_pid must be called exactly once even on exception"

    def test_cmd_run_removes_pid_on_success(self, tmp_path):
        """remove_pid is called on normal completion (regression guard)."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, fg=True)

        removed: list[str] = []

        def _fake_remove_pid(run_id: str, project_root: object) -> None:
            removed.append(run_id)

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()),
            patch("theforge.cli.run._write_audit"),
            patch("theforge.detach.remove_pid", side_effect=_fake_remove_pid),
        ):
            rc = cmd_run(args)

        assert rc == 0
        assert len(removed) == 1

    def test_cmd_sprint_removes_pid_on_run_sprint_exception(self, tmp_path):
        """remove_pid is called when run_sprint raises an exception (manifest mode)."""
        from theforge.cli import cmd_sprint

        config = _make_forge_config(tmp_path)
        args = _make_sprint_args(tmp_path, fg=True)

        removed: list[str] = []

        def _fake_remove_pid(run_id: str, project_root: object) -> None:
            removed.append(run_id)

        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint.run_sprint", side_effect=RuntimeError("sprint crash")),
            patch("theforge.cli.sprint.release_story_locks"),
            patch(
                "theforge.cli.sprint._acquire_launch_locks",
                return_value=([], None),
            ),
            patch("theforge.detach.remove_pid", side_effect=_fake_remove_pid),
        ):
            rc = cmd_sprint(args)

        assert rc == 1
        assert len(removed) == 1, "remove_pid must be called even when run_sprint raises"

    def test_cmd_sprint_removes_pid_on_success(self, tmp_path):
        """remove_pid is called on normal sprint completion (regression guard)."""
        from theforge.cli import cmd_sprint
        from theforge.sprint import SprintResult

        config = _make_forge_config(tmp_path)
        args = _make_sprint_args(tmp_path, fg=True)

        removed: list[str] = []

        def _fake_remove_pid(run_id: str, project_root: object) -> None:
            removed.append(run_id)

        stub_result = SprintResult(
            name="test",
            specs_total=1,
            specs_succeeded=1,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=0.0,
            budget_usd=0.0,
        )

        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint.run_sprint", return_value=stub_result),
            patch("theforge.cli.sprint.release_story_locks"),
            patch(
                "theforge.cli.sprint._acquire_launch_locks",
                return_value=([], None),
            ),
            patch("theforge.detach.remove_pid", side_effect=_fake_remove_pid),
        ):
            rc = cmd_sprint(args)

        assert rc == 0
        assert len(removed) == 1

    def test_run_query_mode_removes_pid_on_run_sprint_exception(self, tmp_path):
        """remove_pid is called when run_sprint raises in _run_query_mode."""
        from theforge import detach as _detach_module
        from theforge.cli.sprint import _run_query_mode

        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(
            name=None,
            no_notify=True,
            fg=True,
            detach=False,
        )

        task = MagicMock()
        task.slug = "issue-42"
        task.depends_on = []
        resolved = MagicMock()
        resolved.stories = [(task, MagicMock(), "issue:42")]

        removed: list[str] = []

        def _fake_remove_pid(run_id: str, project_root: object) -> None:
            removed.append(run_id)

        with (
            patch(
                "theforge.sprint.query.fetch_issues_for_milestone",
                return_value=[{"number": 42, "title": "Story"}],
            ),
            patch("theforge.sprint.query.build_resolved_sprint", return_value=resolved),
            patch(
                "theforge.cli.sprint._acquire_launch_locks",
                return_value=([], None),
            ),
            patch("theforge.cli.sprint.release_story_locks"),
            patch(
                "theforge.cli.sprint.run_sprint",
                side_effect=RuntimeError("query sprint crash"),
            ),
            patch("theforge.detach.remove_pid", side_effect=_fake_remove_pid),
        ):
            rc = _run_query_mode(
                args=args,
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
                _detach=_detach_module,
                _generate_run_id=MagicMock(return_value="run-xyz"),
            )

        assert rc == 1
        assert len(removed) == 1, "remove_pid must be called even when run_sprint raises"

    def test_run_query_mode_removes_pid_on_success(self, tmp_path):
        """remove_pid is called on normal _run_query_mode completion (regression guard)."""
        from theforge import detach as _detach_module
        from theforge.cli.sprint import _run_query_mode
        from theforge.sprint import SprintResult

        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(
            name=None,
            no_notify=True,
            fg=True,
            detach=False,
        )

        task = MagicMock()
        task.slug = "issue-42"
        task.depends_on = []
        resolved = MagicMock()
        resolved.stories = [(task, MagicMock(), "issue:42")]

        stub_result = SprintResult(
            name="test",
            specs_total=1,
            specs_succeeded=1,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=0.0,
            budget_usd=0.0,
        )

        removed: list[str] = []

        def _fake_remove_pid(run_id: str, project_root: object) -> None:
            removed.append(run_id)

        with (
            patch(
                "theforge.sprint.query.fetch_issues_for_milestone",
                return_value=[{"number": 42, "title": "Story"}],
            ),
            patch("theforge.sprint.query.build_resolved_sprint", return_value=resolved),
            patch(
                "theforge.cli.sprint._acquire_launch_locks",
                return_value=([], None),
            ),
            patch("theforge.cli.sprint.release_story_locks"),
            patch("theforge.cli.sprint.run_sprint", return_value=stub_result),
            patch("theforge.detach.remove_pid", side_effect=_fake_remove_pid),
        ):
            rc = _run_query_mode(
                args=args,
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
                _detach=_detach_module,
                _generate_run_id=MagicMock(return_value="run-abc"),
            )

        assert rc == 0
        assert len(removed) == 1
