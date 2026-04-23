"""Tests for CLI run/sprint commands, fg flag, logs, stop, status, daemon, and version helpers."""

from __future__ import annotations

import argparse
import signal as _signal
import warnings
from pathlib import Path
from unittest.mock import MagicMock, call, patch

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
from theforge.runners import AgentResult

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


def _make_forge_config(
    tmp_path: Path,
    review_pool: list[ModelProfile] | None = None,
) -> ForgeConfig:
    if review_pool is None:
        review_pool = [_api_profile("claude-reviewer"), _api_profile("codex-reviewer", "openai")]
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
        review_pool=review_pool,
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig(enabled=False),
        log=LogConfig(enabled=False),
    )


def _make_pass_result(profile_name: str = "test") -> AgentResult:
    return AgentResult(
        success=True,
        output='{"verdict": "APPROVE", "summary": "ok", "findings": []}',
        session_id=None,
        cost_usd=0.003,
        exit_code=0,
        raw={},
        profile_name=profile_name,
        structured_data={"verdict": "APPROVE", "summary": "ok", "findings": []},
    )


def _make_run_args(
    tmp_path,
    *,
    plan: str | None = None,
    from_phase: str | None = None,
    until: str | None = None,
    reviewers: int | None = None,
    max_cycles: int | None = None,
    slug: str | None = None,
    resume: bool = False,
    dev_model: str | None = None,
    plan_model: str | None = None,
    dry_run: bool = False,
    fg: bool = True,
) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for cmd_run tests."""
    story = tmp_path / "story.md"
    story.write_text("# Story\nDo the thing.\n", encoding="utf-8")
    # Create a dummy forge.yaml so _find_config succeeds (load_config is mocked).
    forge_yaml = tmp_path / "forge.yaml"
    if not forge_yaml.exists():
        forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    return argparse.Namespace(
        story=str(story),
        slug=slug,
        config=str(forge_yaml),
        plan=plan,
        from_phase=from_phase,
        until=until,
        reviewers=reviewers,
        max_cycles=max_cycles,
        resume=resume,
        dev_model=dev_model,
        plan_model=plan_model,
        dry_run=dry_run,
        interactive=False,
        auto_merge=False,
        verbose=False,
        no_notify=True,
        fg=fg,
    )


def _stub_result(phase: Phase = Phase.DONE, success: bool = True) -> CoordinatorResult:
    return CoordinatorResult(
        success=success,
        phase=phase,
        state=CoordinatorState(),
        message="ok",
    )


def _make_sprint_args(
    tmp_path,
    *,
    fg: bool = True,
    detach: bool = False,
    resume: bool = False,
    manifest: str | None = None,
    milestone: str | None = None,
    label: str | None = None,
    budget: str | None = None,
    parallel: int | None = None,
) -> argparse.Namespace:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text("stories: []\n", encoding="utf-8")
    forge_yaml = tmp_path / "forge.yaml"
    if not forge_yaml.exists():
        forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    return argparse.Namespace(
        manifest=manifest if manifest is not None else str(manifest_path),
        config=str(forge_yaml),
        fg=fg,
        detach=detach,
        resume=resume,
        milestone=milestone,
        label=label,
        budget=budget,
        parallel=parallel,
        name=None,
        dry_run=False,
        auto_merge=False,
        interactive=False,
        verbose=False,
        no_notify=True,
        no_pull=False,
    )


# ── TestCmdRunUntilFlag ───────────────────────────────────────────────


class TestCmdRunUntilFlag:
    """--until flag parsing and wiring."""

    def test_until_plan_parsed_and_passed_to_run_task(self, tmp_path):
        """--until plan passes stop_phase=Phase.PLAN to run_task."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, until="plan")

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli.run._write_audit"),
        ):
            rc = cmd_run(args)

        assert rc == 0
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("stop_phase") == Phase.PLAN


class TestCmdRunPhaseValidation:
    def test_until_unknown_phase_returns_1(self, tmp_path):
        """--until with invalid phase name returns exit code 1."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, until="bogus-phase")

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task") as mock_run,
        ):
            rc = cmd_run(args)

        assert rc == 1
        mock_run.assert_not_called()

    def test_start_phase_none_when_no_from(self, tmp_path):
        """When neither --from nor --plan is given, start_phase=None."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path)

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli.run._write_audit"),
        ):
            rc = cmd_run(args)

        assert rc == 0
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("start_phase") is None


# ── TestCmdRunFromFlag ────────────────────────────────────────────────


class TestCmdRunFromFlag:
    """--from flag precondition validation."""

    def test_from_dev_no_worktree_returns_1(self, tmp_path):
        """--from dev when worktree does not exist → exit code 1."""
        config = _make_forge_config(tmp_path)
        slug = "story"
        args = _make_run_args(tmp_path, from_phase="dev", slug=slug)

        # Worktree does NOT exist
        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task") as mock_run,
        ):
            rc = cmd_run(args)

        assert rc == 1
        mock_run.assert_not_called()

    def test_from_dev_no_plan_md_returns_1(self, tmp_path):
        """--from dev with worktree but no .forge/plan.md → exit code 1."""
        config = _make_forge_config(tmp_path)
        slug = "story"
        args = _make_run_args(tmp_path, from_phase="dev", slug=slug)

        # Create worktree without .forge/plan.md
        wt = tmp_path / slug
        wt.mkdir()

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task") as mock_run,
        ):
            rc = cmd_run(args)

        assert rc == 1
        mock_run.assert_not_called()

    def test_from_dev_with_plan_md_succeeds(self, tmp_path):
        """--from dev with worktree + .forge/plan.md passes preconditions."""
        config = _make_forge_config(tmp_path)
        slug = "story"
        args = _make_run_args(tmp_path, from_phase="dev", slug=slug)

        # Create worktree with .forge/plan.md
        wt = tmp_path / slug
        (wt / ".forge").mkdir(parents=True)
        (wt / ".forge" / "plan.md").write_text("# Plan", encoding="utf-8")

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli.run._write_audit"),
        ):
            rc = cmd_run(args)

        assert rc == 0
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("start_phase") == Phase.DEV

    def test_from_dev_with_legacy_root_plan_succeeds(self, tmp_path):
        """--from dev accepts legacy forge_plan.md when .forge/plan.md is absent."""
        config = _make_forge_config(tmp_path)
        slug = "story"
        args = _make_run_args(tmp_path, from_phase="dev", slug=slug)

        wt = tmp_path / slug
        wt.mkdir()
        (wt / "forge_plan.md").write_text("# Legacy Plan", encoding="utf-8")

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli.run._write_audit"),
        ):
            rc = cmd_run(args)

        assert rc == 0
        assert mock_run.call_args.kwargs.get("start_phase") == Phase.DEV


# ── TestCmdRunConfigOverrides ─────────────────────────────────────────


class TestCmdRunConfigOverrides:
    """--reviewers and --max-cycles override flags."""

    def test_reviewers_trims_pool(self, tmp_path):
        """--reviewers 1 passes a review_pool of length 1 to run_task."""
        config = _make_forge_config(tmp_path)
        assert len(config.review_pool) == 2  # fixture has 2 reviewers
        args = _make_run_args(tmp_path, reviewers=1)

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli.run._write_audit"),
        ):
            cmd_run(args)

        passed_config = mock_run.call_args.args[0]
        assert len(passed_config.review_pool) == 1
        assert passed_config.review_pool[0] == config.review_pool[0]

    def test_max_cycles_override(self, tmp_path):
        """--max-cycles 1 passes config.retry.max_review_cycles==1 to run_task."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, max_cycles=1)

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli.run._write_audit"),
        ):
            cmd_run(args)

        passed_config = mock_run.call_args.args[0]
        assert passed_config.retry.max_review_cycles == 1

    def test_override_flags_not_persisted_to_yaml(self, tmp_path):
        """Config overrides do not write to forge.yaml."""
        config = _make_forge_config(tmp_path)
        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project: test\n", encoding="utf-8")
        yaml_before = forge_yaml.read_text(encoding="utf-8")

        args = _make_run_args(tmp_path, reviewers=1, max_cycles=1)

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()),
            patch("theforge.cli.run._write_audit"),
        ):
            cmd_run(args)

        assert forge_yaml.read_text(encoding="utf-8") == yaml_before

    def test_plan_flag_with_existing_worktree_implies_from_dev(self, tmp_path):
        """--plan with existing worktree sets start_phase=Phase.DEV."""
        config = _make_forge_config(tmp_path)
        slug = "story"

        # Create existing worktree
        wt = tmp_path / slug
        wt.mkdir()

        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# My Plan\n", encoding="utf-8")
        args = _make_run_args(tmp_path, plan=str(plan_file), slug=slug)

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli.run._write_audit"),
        ):
            cmd_run(args)

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("start_phase") == Phase.DEV

    def test_plan_flag_without_worktree_no_start_phase(self, tmp_path):
        """--plan on a fresh run (no worktree) does NOT set start_phase."""
        config = _make_forge_config(tmp_path)
        # No worktree directory created
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# My Plan\n", encoding="utf-8")
        args = _make_run_args(tmp_path, plan=str(plan_file))

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
            patch("theforge.cli.run._write_audit"),
        ):
            cmd_run(args)

        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs.get("start_phase") is None


# ── TestFgFlag ────────────────────────────────────────────────────────


class TestFgFlag:
    """--fg flag parsing for run and sprint."""

    def test_run_fg_true_skips_daemonization(self, tmp_path):
        """With --fg, daemonize_run should NOT be called."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, fg=True)

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()),
            patch("theforge.cli.run._write_audit"),
            patch("theforge.detach.daemonize_run") as mock_daemonize,
            patch("theforge.detach.remove_pid"),
        ):
            cmd_run(args)
            mock_daemonize.assert_not_called()

    def test_run_fg_false_calls_daemonization(self, tmp_path):
        """Without --fg, daemonize_run should be called."""
        config = _make_forge_config(tmp_path)
        args = _make_run_args(tmp_path, fg=False)

        with (
            patch("theforge.cli.run.load_config", return_value=config),
            patch("theforge.cli.run.run_task", return_value=_stub_result()),
            patch("theforge.cli.run._write_audit"),
            patch("theforge.detach.daemonize_run") as mock_daemonize,
            patch("theforge.detach.suppress_app_nap"),
            patch("theforge.detach.install_cleanup_handler"),
            patch("theforge.detach.remove_pid"),
        ):
            cmd_run(args)
            mock_daemonize.assert_called_once()

    def test_sprint_fg_true_skips_daemonization(self, tmp_path):
        """With --fg on sprint, daemonize_run should NOT be called."""
        from theforge.cli import cmd_sprint
        from theforge.sprint import SprintResult

        config = _make_forge_config(tmp_path)
        args = _make_sprint_args(tmp_path, fg=True)

        stub_result = SprintResult(
            name="test",
            specs_total=0,
            specs_succeeded=0,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=0.0,
            budget_usd=0.0,
        )

        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint.run_sprint", return_value=stub_result),
            patch("theforge.detach.daemonize_run") as mock_daemonize,
            patch("theforge.detach.remove_pid"),
        ):
            cmd_sprint(args)
            mock_daemonize.assert_not_called()


class TestCmdSprintQueryMode:
    def test_query_mode_defaults_to_sequential_when_parallel_omitted(self, tmp_path):
        from theforge.cli.sprint import _run_query_mode

        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(
            name=None,
            no_notify=True,
            fg=True,
            detach=False,
        )
        resolved = MagicMock()
        task = MagicMock()
        task.slug = "issue-42"
        resolved.stories = [(task, MagicMock(), "issue:42")]

        with (
            patch(
                "theforge.sprint.query.fetch_issues_for_milestone",
                return_value=[{"number": 42, "title": "Story"}],
            ),
            patch(
                "theforge.sprint.query.build_resolved_sprint", return_value=resolved
            ) as mock_build,
            patch("theforge.cli.sprint.release_story_locks"),
            patch(
                "theforge.cli.sprint.run_sprint",
                return_value=MagicMock(specs_failed=0),
            ),
        ):
            rc = _run_query_mode(
                args=args,
                config=config,
                config_path=tmp_path / "forge.yaml",
                milestone="v1.0",
                label=None,
                budget_str="5",
                dry_run=False,
                max_parallel=None,
                auto_merge=False,
                interactive=False,
                resume=False,
                no_pull=False,
                _daemon=MagicMock(),
                _detach=MagicMock(),
                _generate_run_id=MagicMock(return_value="run-123"),
            )

        assert rc == 0
        assert mock_build.call_args.kwargs["max_parallel"] == 1


# ── TestCmdStop ───────────────────────────────────────────────────────


class TestCmdStop:
    def test_sends_sigterm_to_pid(self, tmp_path):
        """forge stop <run-id> sends SIGTERM to the correct PID."""
        from theforge.cli import cmd_stop

        run_id = "abc123"
        target_pid = 54321
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.pid").write_text(f"{target_pid}\nmy-slug\n")

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id, no_wait=True, timeout=60)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status.os.kill") as mock_kill,
        ):
            result = cmd_stop(args)

        assert result == 0
        mock_kill.assert_called_once_with(target_pid, _signal.SIGTERM)

    def test_blocks_until_process_dies(self, tmp_path):
        """forge stop waits for the process to exit before returning 0."""
        from theforge.cli import cmd_stop

        run_id = "abc123"
        target_pid = 54321
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.pid").write_text(f"{target_pid}\nmy-slug\n")

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id, no_wait=False, timeout=60)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status.os.kill"),
            patch("theforge.cli.status.time.sleep") as mock_sleep,
            patch("theforge.detach._is_pid_alive", side_effect=[True, False]),
        ):
            result = cmd_stop(args)

        assert result == 0
        mock_sleep.assert_called_once_with(0.1)

    def test_timeout_escalates_to_sigkill_and_cleans_up(self, tmp_path):
        """forge stop escalates to SIGKILL after SIGTERM timeout and cleans up locks."""
        from theforge.cli import cmd_stop

        run_id = "abc123"
        target_pid = 54321
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.pid").write_text(f"{target_pid}\nmy-slug\n")
        lock_dir = tmp_path / ".forge" / "locks"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "my-slug.lock"
        lock_path.write_text(f"{target_pid}|fingerprint\n", encoding="utf-8")

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id, no_wait=False, timeout=60)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status.os.kill") as mock_kill,
            patch("theforge.cli.status.time.sleep"),
            patch(
                "theforge.cli.status.time.monotonic",
                side_effect=[0.0, 0.0, 61.0, 61.0, 61.1],
            ),
            patch(
                "theforge.detach._is_pid_alive",
                side_effect=[True, True, False, False],
            ),
        ):
            result = cmd_stop(args)

        assert result == 0
        expected_calls = [
            call(target_pid, _signal.SIGTERM),
            call(target_pid, _signal.SIGKILL),
        ]
        assert mock_kill.call_args_list[:2] == expected_calls
        assert not (runs_dir / f"{run_id}.pid").exists()
        ended_path = runs_dir / f"{run_id}.ended"
        assert ended_path.read_text(encoding="utf-8") == "stopped"
        assert not lock_path.exists()

    def test_timeout_after_sigkill_keeps_lock_when_process_still_alive(self, tmp_path, capsys):
        """forge stop must not remove locks if the process survives the SIGKILL wait."""
        from theforge.cli import cmd_stop

        run_id = "abc123"
        target_pid = 54321
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.pid").write_text(f"{target_pid}\nmy-slug\n")
        lock_dir = tmp_path / ".forge" / "locks"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "my-slug.lock"
        lock_path.write_text(f"{target_pid}|fingerprint\n", encoding="utf-8")

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id, no_wait=False, timeout=60)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status.os.kill") as mock_kill,
            patch("theforge.cli.status.time.sleep"),
            patch(
                "theforge.cli.status.time.monotonic",
                side_effect=[0.0, 0.0, 61.0, 61.0, 61.1, 66.2],
            ),
            patch("theforge.detach._is_pid_alive", return_value=True),
        ):
            result = cmd_stop(args)

        captured = capsys.readouterr()
        assert result == 1
        expected_calls = [
            call(target_pid, _signal.SIGTERM),
            call(target_pid, _signal.SIGKILL),
        ]
        assert mock_kill.call_args_list == expected_calls
        assert (runs_dir / f"{run_id}.pid").exists()
        assert not (runs_dir / f"{run_id}.ended").exists()
        assert lock_path.exists()
        assert "still alive" in captured.err

    def test_timeout_escalates_to_sigkill_and_cleans_up_sprint_locks(self, tmp_path):
        """forge stop cleans up sprint locks via the live state file."""
        from theforge.cli import cmd_stop

        run_id = "abc123"
        target_pid = 54321
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.pid").write_text(f"{target_pid}\nsprint-slug\n")
        (runs_dir / f"{run_id}.state").write_text(
            "stories:\n  - slug: story-a\n  - slug: story-b\n",
            encoding="utf-8",
        )
        lock_dir = tmp_path / ".forge" / "locks"
        lock_dir.mkdir(parents=True)
        story_a_lock = lock_dir / "story-a.lock"
        story_b_lock = lock_dir / "story-b.lock"
        lock_contents = f"{target_pid}|fingerprint\n"
        story_a_lock.write_text(lock_contents, encoding="utf-8")
        story_b_lock.write_text(lock_contents, encoding="utf-8")

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id, no_wait=False, timeout=60)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status.os.kill"),
            patch("theforge.cli.status.time.sleep"),
            patch(
                "theforge.cli.status.time.monotonic",
                side_effect=[0.0, 0.0, 61.0, 61.0, 61.1],
            ),
            patch(
                "theforge.detach._is_pid_alive",
                side_effect=[True, True, False, False, False],
            ),
        ):
            result = cmd_stop(args)

        assert result == 0
        assert not story_a_lock.exists()
        assert not story_b_lock.exists()

    def test_sigkill_failure_returns_nonzero_with_manual_instruction(self, tmp_path, capsys):
        """forge stop returns 1 with explicit escalation guidance when SIGKILL fails."""
        from theforge.cli import cmd_stop

        run_id = "abc123"
        target_pid = 54321
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.pid").write_text(f"{target_pid}\nmy-slug\n")

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id, no_wait=False, timeout=60)

        def fake_kill(pid, sig):
            if sig == _signal.SIGKILL:
                raise OSError("permission denied")

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status.os.kill", side_effect=fake_kill),
            patch("theforge.cli.status.time.sleep"),
            patch("theforge.cli.status.time.monotonic", side_effect=[0.0, 0.0, 61.0]),
            patch("theforge.detach._is_pid_alive", return_value=True),
        ):
            result = cmd_stop(args)

        captured = capsys.readouterr()
        assert result == 1
        assert "Kill it manually" in captured.err

    def test_no_wait_skips_polling(self, tmp_path):
        """forge stop --no-wait returns immediately without polling."""
        from theforge.cli import cmd_stop

        run_id = "abc123"
        target_pid = 54321
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.pid").write_text(f"{target_pid}\nmy-slug\n")

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id, no_wait=True, timeout=60)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status.os.kill"),
            patch("theforge.detach._is_pid_alive") as mock_is_alive,
        ):
            result = cmd_stop(args)

        assert result == 0
        mock_is_alive.assert_not_called()

    def test_returns_error_when_no_pid_file(self, tmp_path):
        """forge stop returns 1 when no PID file found."""
        from theforge.cli import cmd_stop

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id="nosuchrun", no_wait=False, timeout=60)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            result = cmd_stop(args)

        assert result == 1


# ── TestDaemonDeprecation ─────────────────────────────────────────────


class TestDaemonDeprecation:
    def test_daemon_emits_deprecation_warning(self, tmp_path):
        """forge daemon emits DeprecationWarning."""
        from theforge.cli import cmd_daemon

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(
            daemon_subcommand="status",
            config=str(forge_yaml),
            no_daemonize=False,
        )

        with (
            patch("theforge.cli.daemon._find_config", return_value=forge_yaml),
            patch("theforge.cli.daemon.load_config", return_value=config),
            patch("theforge.daemon.get_daemon_status", return_value={}),
            patch("theforge.cli.daemon._print_daemon_status"),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            cmd_daemon(args)

        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1
        assert "deprecated" in str(dep_warnings[0].message).lower()


# ── TestGetDevSuffix ──────────────────────────────────────────────────


class TestGetDevSuffix:
    """Unit tests for _get_dev_suffix() and _editable_source_path()."""

    def test_ahead_of_tag_returns_dev_suffix(self):
        from theforge.cli.init_commands import _get_dev_suffix

        with (
            patch(
                "theforge.cli.init_commands._editable_source_path",
                return_value="/src/theforge",
            ),
            patch(
                "theforge.cli.init_commands.subprocess.check_output",
                return_value="v0.2.1-5-g8704ff0",
            ),
        ):
            result = _get_dev_suffix()

        assert result == "-dev+g8704ff0"

    def test_at_exact_tag_returns_empty(self):
        from theforge.cli.init_commands import _get_dev_suffix

        with (
            patch(
                "theforge.cli.init_commands._editable_source_path",
                return_value="/src/theforge",
            ),
            patch(
                "theforge.cli.init_commands.subprocess.check_output",
                return_value="v0.2.1-0-gabcdef1",
            ),
        ):
            result = _get_dev_suffix()

        assert result == ""

    def test_no_editable_install_returns_empty(self):
        from theforge.cli.init_commands import _get_dev_suffix

        with patch(
            "theforge.cli.init_commands._editable_source_path",
            return_value=None,
        ):
            result = _get_dev_suffix()

        assert result == ""

    def test_git_unavailable_returns_empty(self):
        from theforge.cli.init_commands import _get_dev_suffix

        with (
            patch(
                "theforge.cli.init_commands._editable_source_path",
                return_value="/src/theforge",
            ),
            patch(
                "theforge.cli.init_commands.subprocess.check_output",
                side_effect=FileNotFoundError("git not found"),
            ),
        ):
            result = _get_dev_suffix()

        assert result == ""

    def test_package_not_found_returns_empty(self):
        import importlib.metadata

        from theforge.cli.init_commands import _get_dev_suffix

        with patch(
            "theforge.cli.init_commands.importlib.metadata.distribution",
            side_effect=importlib.metadata.PackageNotFoundError("theforge"),
        ):
            result = _get_dev_suffix()

        assert result == ""

    def test_git_runs_in_editable_source_dir_not_cwd(self, tmp_path):
        """git describe must use cwd=source_path, not the caller's directory."""
        from theforge.cli.init_commands import _get_dev_suffix

        source = "/opt/myproject/theforge"
        with (
            patch(
                "theforge.cli.init_commands._editable_source_path",
                return_value=source,
            ),
            patch(
                "theforge.cli.init_commands.subprocess.check_output",
                return_value="v0.2.1-3-gabc1234",
            ) as mock_co,
        ):
            result = _get_dev_suffix()

        assert result == "-dev+gabc1234"
        mock_co.assert_called_once()
        _, kwargs = mock_co.call_args
        assert kwargs.get("cwd") == source


# ── TestEditableSourcePath ────────────────────────────────────────────


class TestEditableSourcePath:
    """Unit tests for _editable_source_path()."""

    def test_file_url_returns_path(self):
        from theforge.cli.init_commands import _editable_source_path

        mock_dist = MagicMock()
        mock_dist.read_text.return_value = '{"url": "file:///home/user/src/theforge"}'
        with patch(
            "theforge.cli.init_commands.importlib.metadata.distribution",
            return_value=mock_dist,
        ):
            result = _editable_source_path()

        assert result == "/home/user/src/theforge"

    def test_no_direct_url_returns_none(self):
        from theforge.cli.init_commands import _editable_source_path

        mock_dist = MagicMock()
        mock_dist.read_text.return_value = None
        with patch(
            "theforge.cli.init_commands.importlib.metadata.distribution",
            return_value=mock_dist,
        ):
            result = _editable_source_path()

        assert result is None

    def test_package_not_found_returns_none(self):
        import importlib.metadata

        from theforge.cli.init_commands import _editable_source_path

        with patch(
            "theforge.cli.init_commands.importlib.metadata.distribution",
            side_effect=importlib.metadata.PackageNotFoundError("theforge"),
        ):
            result = _editable_source_path()

        assert result is None
