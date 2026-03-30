"""Tests for workspace auto-pull behavior.

Covers:
- Fresh workspace: pull succeeds before worktree creation
- Fresh workspace: pull fails (non-ff / offline) -> warning logged, workspace still created
- Resume path (existing worktree): behind-origin check logs informational note
- Resume path: rev-list fails -> silently skipped
- no_pull=True: no pull attempted on fresh path
- no_pull=True: no behind-origin check on resume path
- Daemon sprint_args dict includes no_pull; _execute_sprint passes it to run_sprint
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.workspace import _check_behind_origin, _create_workspace

# ── Fresh workspace pull tests ────────────────────────────────────────


class TestFreshWorkspacePull:
    """Pull runs before worktree creation on the fresh path."""

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_pull_succeeds_before_create(self, mock_log, mock_shell, tmp_path):
        """When pull succeeds, creation runs and pull precedes it."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        call_order = []

        def shell_side_effect(cmd, cwd, **kwargs):
            if "fetch origin" in cmd:
                call_order.append("pull")
                return (True, "")
            if "mkdir" in cmd:
                call_order.append("create")
                (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, branch_name, err = _create_workspace(config, task, no_pull=False)

        assert err is None
        assert workspace_path is not None
        assert call_order.index("pull") < call_order.index("create")

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_pull_fails_workspace_still_created(self, mock_log, mock_shell, tmp_path):
        """When pull fails, a warning is logged but the workspace is still created."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "fetch origin" in cmd:
                return (False, "fatal: Not possible to fast-forward, aborting.")
            if "mkdir" in cmd:
                (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, branch_name, err = _create_workspace(config, task, no_pull=False)

        assert err is None
        assert workspace_path is not None

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any("pull failed" in c or "non-ff" in c for c in log_calls)

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_no_pull_skips_pull(self, mock_log, mock_shell, tmp_path):
        """When no_pull=True, no git fetch origin command is issued."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        pull_called = []

        def shell_side_effect(cmd, cwd, **kwargs):
            if "fetch origin" in cmd:
                pull_called.append(cmd)
            if "mkdir" in cmd:
                (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        _create_workspace(config, task, no_pull=True)

        assert pull_called == [], "pull should not be called when no_pull=True"


# ── Resume path behind-origin check tests ────────────────────────────


class TestBehindOriginCheck:
    """_check_behind_origin logs when base is behind; silently skips on failure."""

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_behind_origin_logs_info(self, mock_log, mock_shell, tmp_path):
        """When base_branch is N commits behind origin, log an info note."""
        config = _make_config(tmp_path)

        mock_shell.return_value = (True, "3\n")

        _check_behind_origin(config)

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any("3 commit" in c and "behind" in c for c in log_calls)

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_not_behind_no_log(self, mock_log, mock_shell, tmp_path):
        """When base_branch is 0 commits behind, no info note logged."""
        config = _make_config(tmp_path)

        mock_shell.return_value = (True, "0\n")

        _check_behind_origin(config)

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert not any("behind" in c for c in log_calls)

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_rev_list_fails_silent(self, mock_log, mock_shell, tmp_path):
        """When rev-list command fails, silently skip — no error raised."""
        config = _make_config(tmp_path)

        mock_shell.return_value = (False, "fatal: ambiguous argument")

        # Should not raise
        _check_behind_origin(config)

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert not any("behind" in c for c in log_calls)


class TestResumeNoPull:
    """On resume/reuse paths, no_pull=True suppresses the behind-origin check."""

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_no_pull_skips_behind_origin_check_on_reuse(self, mock_log, mock_shell, tmp_path):
        """When no_pull=True and worktree exists (non-stale), no rev-list is run."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        # Create existing worktree directory
        wt_path = tmp_path / task.slug
        wt_path.mkdir(parents=True, exist_ok=True)

        rev_list_calls = []

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-list" in cmd:
                rev_list_calls.append(cmd)
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/test-task")
            if "git log" in cmd and "..feat/" in cmd:
                return (True, "abc1234 a commit\n")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        _create_workspace(config, task, no_pull=True)

        assert rev_list_calls == [], "rev-list should not run when no_pull=True"

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_pull_false_runs_behind_origin_check_on_reuse(self, mock_log, mock_shell, tmp_path):
        """When no_pull=False and worktree exists (non-stale), rev-list is run."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        wt_path = tmp_path / task.slug
        wt_path.mkdir(parents=True, exist_ok=True)

        rev_list_calls = []

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-list" in cmd:
                rev_list_calls.append(cmd)
                return (True, "0\n")
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/test-task")
            if "git log" in cmd and "..feat/" in cmd:
                return (True, "abc1234 a commit\n")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        _create_workspace(config, task, no_pull=False)

        assert any("rev-list" in c for c in rev_list_calls), (
            "rev-list should run when no_pull=False"
        )


# ── Daemon threading tests ────────────────────────────────────────────


class TestDaemonNoPull:
    """Daemon correctly passes no_pull from sprint_args to run_sprint."""

    def test_daemon_sprint_args_includes_no_pull(self, tmp_path):
        """When --no-pull is passed to forge sprint --detach, sprint_args contains no_pull."""
        import argparse

        import theforge.cli.sprint as sprint_cli

        args = argparse.Namespace(
            manifest="sprint.yaml",
            config=None,
            auto_merge=False,
            interactive=False,
            verbose=False,
            no_notify=False,
            resume=False,
            detach=True,
            fg=False,
            no_pull=True,
        )

        captured_sprint_args: dict = {}

        def capture_submit(root, mpath, sprint_args):
            captured_sprint_args.update(sprint_args)
            return {"ok": True, "queued": "sprint", "position": 1}

        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.touch()
        (tmp_path / "forge.yaml").touch()
        args.manifest = str(manifest_path)

        with (
            patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.cli.sprint.load_config", return_value=MagicMock(project_root=tmp_path)
            ),
            patch("theforge.cli.sprint.parse_manifest_slugs", return_value=[]),
            patch("theforge.cli.sprint.acquire_story_locks", return_value=([], [])),
            patch("theforge.cli.sprint.release_story_locks"),
            # cmd_sprint does `from theforge import daemon as _daemon` locally;
            # patch the symbol on the theforge.daemon module itself
            patch("theforge.daemon.is_daemon_running", return_value=True),
            patch("theforge.daemon.submit_sprint", side_effect=capture_submit),
        ):
            sprint_cli.cmd_sprint(args)

        assert captured_sprint_args.get("no_pull") is True

    def test_daemon_execute_sprint_passes_no_pull(self, tmp_path):
        """DaemonServer._execute_sprint passes no_pull from sprint_args to run_sprint."""
        from theforge.daemon import DaemonServer

        mock_config = MagicMock()
        mock_config.project_root = tmp_path

        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.touch()

        args = {
            "auto_merge": False,
            "notify": True,
            "resume": False,
            "no_pull": True,
        }

        with (
            # _execute_sprint uses local imports — patch at their source modules
            patch("theforge.config.load_config", return_value=mock_config),
            patch("theforge.sprint.runner.parse_manifest_slugs", return_value=[]),
            patch("theforge.sprint.lock.acquire_story_locks", return_value=([], [])),
            patch("theforge.sprint.lock.release_story_locks"),
            # `from .sprint import run_sprint` binds to theforge.sprint.run_sprint
            patch("theforge.sprint.run_sprint") as mock_rs,
        ):
            mock_rs.return_value = MagicMock(specs_failed=0)

            daemon = object.__new__(DaemonServer)
            daemon.forge_root = tmp_path

            DaemonServer._execute_sprint(daemon, str(manifest_path), args, state_update_fn=None)

        mock_rs.assert_called_once()
        _, kwargs = mock_rs.call_args
        assert kwargs.get("no_pull") is True
