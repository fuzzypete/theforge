"""Tests for workspace auto-pull behavior and git index hygiene.

Covers:
- Fresh workspace: pull succeeds before worktree creation
- Fresh workspace: pull fails (diverged) -> hard abort, workspace NOT created, error returned
- Fresh workspace: pull fails (offline/unreachable) -> hard abort, workspace NOT created
- Base branch ahead of origin (ahead-only) -> hard abort on fresh, reused, and
  reattach paths, even though `git pull --ff-only` succeeds in that state
- no_pull=True: no pull attempted on fresh path
- no_pull=True: no base-branch sync on resume path
- Daemon sprint_args dict includes no_pull; _execute_sprint passes it to run_sprint
- _deindex_forge_artifacts: runs git rm --cached --ignore-unmatch on all return paths
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.workspace import (
    _FORGE_ARTIFACTS,
    _create_workspace,
    _deindex_forge_artifacts,
    pull_base_branch,
)

# ── Fresh workspace pull tests ────────────────────────────────────────


class TestFreshWorkspacePull:
    """Pull runs before worktree creation on the fresh path.

    The implementation branches on whether the project root has base_branch
    checked out:
      - On base_branch: git pull --ff-only  (fetch refspec rejected when checked out)
      - Not on base_branch: git fetch origin base:base  (pull would advance current branch)
    """

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_pull_succeeds_before_create_on_base(self, mock_log, mock_shell, tmp_path):
        """When root is on base_branch, pull --ff-only runs before worktree creation."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        call_order = []

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, config.workspace.base_branch)  # checked out on base
            if "pull --ff-only" in cmd:
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
    def test_fetch_refspec_used_when_not_on_base(self, mock_log, mock_shell, tmp_path):
        """When root is NOT on base_branch, fetch origin base:base runs before creation."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        call_order = []

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "feat/other-branch")  # NOT on base
            if "fetch origin" in cmd and ":" in cmd:
                call_order.append("fetch")
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
        assert call_order.index("fetch") < call_order.index("create")

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_pull_diverged_aborts(self, mock_log, mock_shell, tmp_path):
        """When pull fails because local and origin have diverged, workspace creation aborts."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, config.workspace.base_branch)
            if "pull --ff-only" in cmd:
                return (False, "fatal: Not possible to fast-forward, aborting.")
            if "rev-list --count origin/" in cmd and ".." + config.workspace.base_branch in cmd:
                # local ahead count
                return (True, "2\n")
            if "rev-list --count " + config.workspace.base_branch + "..origin/" in cmd:
                # local behind count
                return (True, "2\n")
            if "mkdir" in cmd:
                (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, branch_name, err = _create_workspace(config, task, no_pull=False)

        assert workspace_path is None
        assert err is not None
        assert "diverged" in err
        assert "2 ahead" in err
        assert "2 behind" in err
        assert "git rebase" in err

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_pull_offline_aborts(self, mock_log, mock_shell, tmp_path):
        """When pull fails and the branch hasn't diverged, workspace creation aborts.

        This covers the offline/network-error case: git rev-list is a local operation
        that succeeds even when offline (as long as the origin tracking ref exists from
        a previous fetch). The 0-ahead/0-behind result correctly distinguishes this from
        a diverged branch and produces an error with the original pull failure message.
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        pull_err = "fatal: unable to access 'https://...': Could not resolve host"

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, config.workspace.base_branch)
            if "pull --ff-only" in cmd:
                return (False, pull_err)
            if "rev-list" in cmd:
                # Local operation: succeeds even offline; 0 ahead, 0 behind
                return (True, "0\n")
            if "mkdir" in cmd:
                (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, branch_name, err = _create_workspace(config, task, no_pull=False)

        assert workspace_path is None
        assert err is not None
        assert "abort" in err.lower()
        # The error should include the original pull failure message, not claim divergence
        assert "diverged" not in err
        assert "Could not resolve host" in err

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_pull_non_ff_recovers_with_fetch_reset_on_clean_base(
        self, mock_log, mock_shell, tmp_path
    ):
        """A clean checked-out base branch auto-recovers from a non-ff pull failure."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        issued: list[str] = []

        def shell_side_effect(cmd, cwd, **kwargs):
            issued.append(cmd)
            if "rev-parse HEAD:src/theforge" in cmd:
                return (True, "tree123\n")
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, config.workspace.base_branch)
            if "pull --ff-only" in cmd:
                return (False, "fatal: Not possible to fast-forward, aborting.")
            if "rev-list --count origin/" in cmd and ".." + config.workspace.base_branch in cmd:
                return (True, "0\n")
            if "rev-list --count " + config.workspace.base_branch + "..origin/" in cmd:
                return (True, "1\n")
            if cmd == "git fetch origin main":
                return (True, "From origin")
            if cmd == "git status --porcelain":
                return (True, "")
            if cmd == "git reset --hard origin/main":
                return (True, "HEAD is now at 9c2e778")
            if "mkdir" in cmd:
                (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, branch_name, err = _create_workspace(config, task, no_pull=False)

        assert err is None
        assert workspace_path is not None
        assert branch_name == "forge/test-task"
        assert "git fetch origin main" in issued
        assert "git reset --hard origin/main" in issued
        assert any(
            "recovering with fetch + reset" in str(call) for call in mock_log.call_args_list
        )

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_pull_non_ff_recovery_refuses_dirty_base(self, mock_log, mock_shell, tmp_path):
        """A dirty checked-out base branch aborts with an actionable recovery error."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse HEAD:src/theforge" in cmd:
                return (True, "tree123\n")
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, config.workspace.base_branch)
            if "pull --ff-only" in cmd:
                return (False, "fatal: Not possible to fast-forward, aborting.")
            if "rev-list --count origin/" in cmd and ".." + config.workspace.base_branch in cmd:
                return (True, "0\n")
            if "rev-list --count " + config.workspace.base_branch + "..origin/" in cmd:
                return (True, "1\n")
            if cmd == "git fetch origin main":
                return (True, "From origin")
            if cmd == "git status --porcelain":
                return (True, " M src/theforge/coordinator/workspace.py\n")
            if "mkdir" in cmd:
                (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, branch_name, err = _create_workspace(config, task, no_pull=False)

        assert workspace_path is None
        assert branch_name is None
        assert err is not None
        assert "local changes" in err
        assert "rebase onto origin/main" in err

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_no_pull_skips_pull(self, mock_log, mock_shell, tmp_path):
        """When no_pull=True, no fetch or pull command is issued."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        sync_called = []

        def shell_side_effect(cmd, cwd, **kwargs):
            if "pull --ff-only" in cmd or ("fetch origin" in cmd and ":" in cmd):
                sync_called.append(cmd)
            if "mkdir" in cmd:
                (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        _create_workspace(config, task, no_pull=True)

        assert sync_called == [], "no pull/fetch should be called when no_pull=True"


# ── Base branch ahead-only guard tests ───────────────────────────────


def _ahead_only_shell(ahead: str = "2", behind: str = "0", *, recorder=None):
    """Shell stub where the base branch is `ahead` commits ahead of origin.

    ``git pull --ff-only`` succeeds trivially in this state — that is precisely
    what made the old guard miss it — so the stub returns success for the sync.
    """

    def shell_side_effect(cmd, cwd, **kwargs):
        if recorder is not None:
            recorder.append(cmd)
        if "rev-list --count origin/" in cmd:
            return (True, f"{ahead}\n")
        if "rev-list --count" in cmd:
            return (True, f"{behind}\n")
        if "rev-parse --abbrev-ref HEAD" in cmd:
            return (True, "main")
        if "rev-parse HEAD:src/theforge" in cmd:
            return (True, "treesha")
        if "git log" in cmd and "--oneline" in cmd:
            # Non-empty: keeps an existing worktree out of the stale-removal
            # path and marks a collided branch as having commits to reattach.
            return (True, "abc1234 a commit\n")
        return (True, "")

    return shell_side_effect


class TestBaseBranchAheadOnlyGuard:
    """An ahead-only base branch must block worktree use, not just log."""

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_pull_base_branch_raises_when_ahead_only(self, mock_log, mock_shell, tmp_path):
        """Successful pull + ahead>0/behind==0 still aborts."""
        config = _make_config(tmp_path)
        mock_shell.side_effect = _ahead_only_shell(ahead="2", behind="0")

        try:
            pull_base_branch(config)
        except RuntimeError as exc:
            assert "2 local commit(s) not on origin/main" in str(exc)
        else:
            raise AssertionError("pull_base_branch must abort on an ahead-only base branch")

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_pull_base_branch_passes_when_in_sync(self, mock_log, mock_shell, tmp_path):
        """ahead == 0 is the normal case and must not raise."""
        config = _make_config(tmp_path)
        mock_shell.side_effect = _ahead_only_shell(ahead="0", behind="0")

        assert pull_base_branch(config) is True

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_fresh_workspace_aborts_before_worktree_add(self, mock_log, mock_shell, tmp_path):
        """The fresh path returns an error and never runs the create command."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        calls: list[str] = []
        mock_shell.side_effect = _ahead_only_shell(recorder=calls)

        path, branch, error = _create_workspace(config, task, no_pull=False)

        assert path is None and branch is None
        assert error is not None and "not on origin/main" in error
        assert not any(cmd.startswith("mkdir -p ") for cmd in calls)

    @patch("theforge.coordinator.workspace._rebase_reused_worktree")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_reused_worktree_aborts_before_rebase(
        self, mock_log, mock_shell, mock_rebase, tmp_path
    ):
        """The reused-worktree path blocks too — it used to only log behind-origin."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
        mock_shell.side_effect = _ahead_only_shell()

        path, branch, error = _create_workspace(config, task, no_pull=False)

        assert path is None and branch is None
        assert error is not None and "not on origin/main" in error
        mock_rebase.assert_not_called()

    @patch("theforge.coordinator.workspace._rebase_reused_worktree")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_reattach_path_aborts_before_worktree_add(
        self, mock_log, mock_shell, mock_rebase, tmp_path
    ):
        """Branch-collision reattach blocks before `git worktree add`."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        calls: list[str] = []
        base_shell = _ahead_only_shell(ahead="0", behind="0", recorder=calls)
        # First sync succeeds (in-sync) so creation is attempted; the create
        # command then collides with an existing branch that has commits, and
        # by the time the reattach path re-syncs the base branch is ahead-only.
        state = {"synced": 0}

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-list --count origin/" in cmd:
                calls.append(cmd)
                state["synced"] += 1
                return (True, "0\n" if state["synced"] == 1 else "2\n")
            if cmd.startswith("mkdir -p "):
                calls.append(cmd)
                return (False, "fatal: branch already exists")
            if cmd.startswith("git branch --list"):
                calls.append(cmd)
                return (True, "  forge/test-task\n")
            return base_shell(cmd, cwd, **kwargs)

        mock_shell.side_effect = shell_side_effect

        path, branch, error = _create_workspace(config, task, no_pull=False)

        assert path is None and branch is None
        assert error is not None and "not on origin/main" in error
        assert not any("worktree add" in cmd for cmd in calls)
        mock_rebase.assert_not_called()


class TestResumeNoPull:
    """On resume/reuse paths, no_pull=True suppresses the base-branch sync."""

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_no_pull_skips_base_sync_on_reuse(self, mock_log, mock_shell, tmp_path):
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
    def test_pull_false_runs_base_sync_on_reuse(self, mock_log, mock_shell, tmp_path):
        """When no_pull=False and worktree exists (non-stale), the sync delta is measured."""
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


class TestDaemonForce:
    """Daemon correctly threads --force from CLI submit through to run_sprint."""

    def test_daemon_sprint_args_includes_force(self, tmp_path):
        import argparse

        import theforge.cli.sprint as sprint_cli

        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.touch()
        (tmp_path / "forge.yaml").touch()

        args = argparse.Namespace(
            manifest=str(manifest_path),
            config=None,
            auto_merge=False,
            interactive=False,
            verbose=False,
            no_notify=False,
            resume=False,
            detach=True,
            fg=False,
            no_pull=False,
            force=True,
        )

        captured: dict = {}

        def capture_submit(root, mpath, sprint_args):
            captured.update(sprint_args)
            return {"ok": True, "queued": "sprint", "position": 1}

        with (
            patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.cli.sprint.load_config", return_value=MagicMock(project_root=tmp_path)
            ),
            patch("theforge.cli.sprint.parse_manifest_slugs", return_value=[]),
            patch("theforge.cli.sprint.release_story_locks"),
            patch("theforge.daemon.is_daemon_running", return_value=True),
            patch("theforge.daemon.submit_sprint", side_effect=capture_submit),
        ):
            sprint_cli.cmd_sprint(args)

        assert captured.get("force") is True

    def test_daemon_execute_sprint_passes_force(self, tmp_path):
        from theforge.daemon import DaemonServer

        mock_config = MagicMock()
        mock_config.project_root = tmp_path

        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.touch()

        args = {
            "auto_merge": False,
            "notify": True,
            "resume": False,
            "no_pull": False,
            "force": True,
        }

        with (
            patch("theforge.config.load_config", return_value=mock_config),
            patch("theforge.sprint.runner.parse_manifest_slugs", return_value=[]),
            patch("theforge.sprint.lock.acquire_story_locks", return_value=([], [])),
            patch("theforge.sprint.lock.release_story_locks"),
            patch("theforge.sprint.run_sprint") as mock_rs,
        ):
            mock_rs.return_value = MagicMock(specs_failed=0)

            daemon = object.__new__(DaemonServer)
            daemon.forge_root = tmp_path

            DaemonServer._execute_sprint(daemon, str(manifest_path), args, state_update_fn=None)

        mock_rs.assert_called_once()
        _, kwargs = mock_rs.call_args
        assert kwargs.get("force") is True

    def test_daemon_execute_sprint_defaults_force_false(self, tmp_path):
        """Args without 'force' key must default to False (no implicit bypass)."""
        from theforge.daemon import DaemonServer

        mock_config = MagicMock()
        mock_config.project_root = tmp_path
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.touch()

        args = {"auto_merge": False, "notify": True, "resume": False, "no_pull": False}

        with (
            patch("theforge.config.load_config", return_value=mock_config),
            patch("theforge.sprint.runner.parse_manifest_slugs", return_value=[]),
            patch("theforge.sprint.lock.acquire_story_locks", return_value=([], [])),
            patch("theforge.sprint.lock.release_story_locks"),
            patch("theforge.sprint.run_sprint") as mock_rs,
        ):
            mock_rs.return_value = MagicMock(specs_failed=0)

            daemon = object.__new__(DaemonServer)
            daemon.forge_root = tmp_path

            DaemonServer._execute_sprint(daemon, str(manifest_path), args, state_update_fn=None)

        _, kwargs = mock_rs.call_args
        assert kwargs.get("force") is False


# ── Deindex forge artifacts tests ─────────────────────────────────────


class TestDeindexForgeArtifacts:
    """_deindex_forge_artifacts runs git rm -f --cached --ignore-unmatch on both artifacts."""

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_issues_git_rm_force_cached_ignore_unmatch(self, mock_log, mock_shell, tmp_path):
        """The helper runs git rm -f --cached --ignore-unmatch for both forge artifacts."""
        mock_shell.return_value = (True, "")

        _deindex_forge_artifacts(tmp_path)

        assert mock_shell.call_count == 1
        cmd_issued = mock_shell.call_args[0][0]
        assert "git rm" in cmd_issued
        assert "-f" in cmd_issued
        assert "--cached" in cmd_issued
        assert "--ignore-unmatch" in cmd_issued
        for artifact in _FORGE_ARTIFACTS:
            assert artifact in cmd_issued

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_force_flag_present_for_diverged_index(self, mock_log, mock_shell, tmp_path):
        """git rm -f is used so removal succeeds when index entry diverges from HEAD/worktree.

        This is the agent-misbehavior state: handoff.yaml is tracked but its index
        content differs from both HEAD and the working tree.  Without -f git rm
        --cached refuses to remove it.  With -f it succeeds unconditionally.
        """
        mock_shell.return_value = (True, "rm '.forge/handoff.yaml'")

        _deindex_forge_artifacts(tmp_path)

        cmd_issued = mock_shell.call_args[0][0]
        # -f must appear before --cached (or anywhere) in the command
        assert "-f" in cmd_issued.split()

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_logs_warning_on_failure(self, mock_log, mock_shell, tmp_path):
        """When git rm fails, a warning is logged and no exception raised."""
        mock_shell.return_value = (False, "not a git repository")

        _deindex_forge_artifacts(tmp_path)  # must not raise

        log_calls = [str(c) for c in mock_log.call_args_list]
        assert any("git rm --cached failed" in c for c in log_calls)

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_deletes_local_artifacts_after_deindex(self, mock_log, mock_shell, tmp_path):
        """Local forge artifacts are deleted so reused worktrees cannot keep stale state."""
        mock_shell.return_value = (True, "")

        for artifact in _FORGE_ARTIFACTS:
            artifact_path = tmp_path / artifact
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text("stale\n", encoding="utf-8")

        _deindex_forge_artifacts(tmp_path, purge=True)

        for artifact in _FORGE_ARTIFACTS:
            assert not (tmp_path / artifact).exists()

    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_runs_in_workspace_path(self, mock_log, mock_shell, tmp_path):
        """git rm runs with the workspace path as cwd."""
        mock_shell.return_value = (True, "")

        workspace = tmp_path / "my-worktree"
        workspace.mkdir()
        _deindex_forge_artifacts(workspace)

        cwd_used = mock_shell.call_args[0][1]
        assert cwd_used == workspace


@patch("theforge.coordinator.workspace._deindex_forge_artifacts")
@patch("theforge.coordinator.workspace._cu._run_shell")
def test_merge_branch_deindexes_workspace_before_merge(mock_shell, mock_deindex, tmp_path):
    """_merge_branch scrubs tracked forge artifacts from the workspace before merge."""
    from theforge.coordinator.workspace import _merge_branch

    workspace = tmp_path / "wt"
    workspace.mkdir()

    def shell_side_effect(cmd, cwd, **kwargs):
        if "git branch --list" in cmd:
            return (True, "* main")
        if "git status --porcelain" in cmd:
            return (True, "")
        if "git log main..forge/issue-1 --oneline" in cmd:
            return (True, "abc123 feat: change")
        if "git checkout main" in cmd:
            return (True, "")
        if "git merge --ff-only forge/issue-1" in cmd:
            return (True, "")
        return (True, "")

    mock_shell.side_effect = shell_side_effect

    info = _merge_branch(
        project_root=tmp_path,
        base_branch="main",
        branch_name="forge/issue-1",
        slug="issue-1",
        workspace_path=workspace,
    )

    assert info["merged"] is True
    mock_deindex.assert_called_once_with(workspace, purge=True)


@patch("theforge.coordinator.workspace._cu._run_shell")
def test_merge_branch_removes_leftover_forge_shell_after_git_worktree_remove(mock_shell, tmp_path):
    """_merge_branch deletes a leftover worktree shell when only .forge/ content remains."""
    from theforge.coordinator.workspace import _merge_branch

    workspace = tmp_path / "wt"
    (workspace / ".forge" / "traces").mkdir(parents=True)
    (workspace / ".forge" / "traces" / "1-dev.txt").write_text("trace\n", encoding="utf-8")

    def shell_side_effect(cmd, cwd, **kwargs):
        if "git branch --list" in cmd:
            return (True, "* main")
        if "git status --porcelain" in cmd:
            return (True, "")
        if "git log main..forge/issue-1 --oneline" in cmd:
            return (True, "abc123 feat: change")
        if "git checkout main" in cmd:
            return (True, "")
        if "git merge --ff-only forge/issue-1" in cmd:
            return (True, "")
        if "git worktree remove --force .forge/worktrees/issue-1" in cmd:
            return (True, "")
        return (True, "")

    mock_shell.side_effect = shell_side_effect

    info = _merge_branch(
        project_root=tmp_path,
        base_branch="main",
        branch_name="forge/issue-1",
        slug="issue-1",
        workspace_path=workspace,
    )

    assert info["merged"] is True
    assert not workspace.exists()


class TestDeindexRunsOnAllReturnPaths:
    """_create_workspace calls _deindex_forge_artifacts on every successful return path."""

    @patch("theforge.coordinator.workspace._deindex_forge_artifacts")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_deindex_called_on_new_workspace(self, mock_log, mock_shell, mock_deindex, tmp_path):
        """Fresh workspace creation: deindex runs before returning."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, config.workspace.base_branch)
            if "pull --ff-only" in cmd:
                return (True, "")
            if "mkdir" in cmd:
                (tmp_path / task.slug).mkdir(parents=True, exist_ok=True)
                return (True, "")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, _, err = _create_workspace(config, task, no_pull=False)

        assert err is None
        mock_deindex.assert_called_once_with(workspace_path, purge=True)

    @patch("theforge.coordinator.workspace._deindex_forge_artifacts")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_deindex_called_on_reuse_existing_worktree(
        self, mock_log, mock_shell, mock_deindex, tmp_path
    ):
        """Reusing an existing non-stale worktree: deindex runs before returning."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        wt_path = tmp_path / task.slug
        wt_path.mkdir(parents=True, exist_ok=True)

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "forge/test-task")
            if "git log" in cmd and ".." in cmd:
                return (True, "abc1234 a commit\n")
            if "rev-list" in cmd:
                return (True, "0\n")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, _, err = _create_workspace(config, task, no_pull=True)

        assert err is None
        mock_deindex.assert_called_once_with(workspace_path, purge=True)

    @patch("theforge.coordinator.workspace._deindex_forge_artifacts")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_deindex_called_on_reattach(self, mock_log, mock_shell, mock_deindex, tmp_path):
        """Reattaching a branch that has commits: deindex runs before returning."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)

        wt_path = tmp_path / task.slug

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "main")
            if "pull --ff-only" in cmd:
                return (True, "")
            if cmd.startswith("mkdir"):
                # First mkdir (worktree creation) fails to trigger reattach path
                return (False, "already exists")
            if "branch --list" in cmd:
                return (True, "feat/test-task")
            if "worktree list" in cmd:
                return (True, "")  # no registered worktree
            if "git log" in cmd and ".." in cmd:
                return (True, "abc1234 a commit\n")  # has commits
            if "worktree add" in cmd:
                wt_path.mkdir(parents=True, exist_ok=True)
                return (True, "")
            if "rev-list" in cmd:
                return (True, "0\n")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, _, err = _create_workspace(config, task, no_pull=False)

        assert err is None
        mock_deindex.assert_called_once_with(workspace_path, purge=True)
