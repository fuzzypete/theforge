from __future__ import annotations

import sys
from dataclasses import replace
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.workspace import _FORGE_ARTIFACTS, _create_workspace, _run_setup_split


def _write_workspace_pin(workspace_path) -> None:
    (workspace_path / ".python-version").write_text("3.12.12\n", encoding="utf-8")


class TestRunSetupSplitVenvBehavior:
    def test_template_guard_always_runs_install_even_when_venv_exists(self, tmp_path):
        _write_workspace_pin(tmp_path)
        cmd = "test -d .venv || ({forge_python} -m venv .venv && pip install -e '.[all]')"
        calls = []

        def fake_shell(cmd_arg, cwd, **kwargs):
            calls.append(cmd_arg)
            return (True, "ok")

        with patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell):
            ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        assert len(calls) == 2
        assert "-m venv .venv" in calls[0]
        assert calls[1] == "pip install -e '.[all]'"

    def test_legacy_guard_always_runs_install_even_when_venv_exists(self, tmp_path):
        _write_workspace_pin(tmp_path)
        cmd = "test -d .venv || (python -m venv .venv && pip install -e '.[all]')"
        calls = []

        def fake_shell(cmd_arg, cwd, **kwargs):
            calls.append(cmd_arg)
            return (True, "ok")

        with patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell):
            ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        assert len(calls) == 2
        assert "python3.12" in calls[0]
        assert calls[1] == "pip install -e '.[all]'"

    def test_legacy_guard_without_pin_keeps_original_python_token(self, tmp_path):
        cmd = "test -d .venv || (python -m venv .venv && pip install -e '.[all]')"
        calls = []

        def fake_shell(cmd_arg, cwd, **kwargs):
            calls.append(cmd_arg)
            return (True, "ok")

        with patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell):
            ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        assert len(calls) == 2
        assert calls[0] == "test -d .venv || python -m venv .venv"
        assert calls[1] == "pip install -e '.[all]'"

    def test_template_guard_reprovisions_stale_venv(self, tmp_path):
        _write_workspace_pin(tmp_path)
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (tmp_path / ".venv" / "pyvenv.cfg").write_text(
            "\n".join(
                [
                    "home = /Users/example/.pyenv/versions/3.11.9/bin",
                    "include-system-site-packages = false",
                    "version = 3.11.9",
                    "executable = /Users/example/.pyenv/versions/3.11.9/bin/python3.11",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        cmd = "test -d .venv || ({forge_python} -m venv .venv && pip install -e '.[all]')"
        calls = []

        def fake_shell(cmd_arg, cwd, **kwargs):
            calls.append(cmd_arg)
            return (True, "ok")

        with patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell):
            ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        assert not (tmp_path / ".venv" / "pyvenv.cfg").exists()
        assert len(calls) == 2
        assert "python3.12" in calls[0]

    def test_template_guard_without_pin_falls_back_to_orchestrator_python(self, tmp_path):
        cmd = "test -d .venv || ({forge_python} -m venv .venv && pip install -e '.[all]')"
        calls = []

        def fake_shell(cmd_arg, cwd, **kwargs):
            calls.append(cmd_arg)
            return (True, "ok")

        with patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell):
            ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        assert len(calls) == 2
        assert calls[0] == f"test -d .venv || {sys.executable} -m venv .venv"
        assert calls[1] == "pip install -e '.[all]'"


class TestRunSetupSplitCommandTracking:
    @patch("theforge.coordinator.workspace._cu._log")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_logs_and_records_changed_setup_command(self, mock_shell, mock_log, tmp_path):
        _write_workspace_pin(tmp_path)
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        (forge_dir / "last_setup_command").write_text("pip install -e .", encoding="utf-8")
        mock_shell.return_value = (True, "ok")

        cmd = "pip install -e '.[all]'"
        ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        assert any("setup_command changed" in str(call) for call in mock_log.call_args_list)
        assert (forge_dir / "last_setup_command").read_text(encoding="utf-8") == cmd

    @patch("theforge.coordinator.workspace._cu._log")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_no_log_when_setup_command_unchanged(self, mock_shell, mock_log, tmp_path):
        _write_workspace_pin(tmp_path)
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        cmd = "pip install -e '.[all]'"
        (forge_dir / "last_setup_command").write_text(cmd, encoding="utf-8")
        mock_shell.return_value = (True, "ok")

        ok, out = _run_setup_split(cmd, tmp_path)

        assert ok is True
        assert not any("setup_command changed" in str(call) for call in mock_log.call_args_list)
        assert (forge_dir / "last_setup_command").read_text(encoding="utf-8") == cmd


class TestCreateWorkspaceReuseRunsSetup:
    @patch("theforge.coordinator.workspace._deindex_forge_artifacts")
    @patch("theforge.coordinator.workspace._run_setup_split")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    @patch("theforge.coordinator.workspace._cu._log")
    def test_non_stale_reuse_path_runs_setup(
        self, mock_log, mock_shell, mock_setup, mock_deindex, tmp_path
    ):
        config = _make_config(tmp_path)
        config = replace(
            config,
            workspace=replace(config.workspace, setup_command="pip install -e '.[all]'"),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / task.slug
        workspace.mkdir(parents=True, exist_ok=True)
        mock_setup.return_value = (True, "ok")

        def shell_side_effect(cmd, cwd, **kwargs):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "forge/test-task")
            if "git log" in cmd and ".." in cmd:
                return (True, "abc123 a commit\n")
            if "rev-list" in cmd:
                return (True, "0\n")
            return (True, "")

        mock_shell.side_effect = shell_side_effect

        workspace_path, branch_name, err = _create_workspace(config, task, no_pull=False)

        assert err is None
        assert workspace_path == workspace
        mock_setup.assert_called_once_with(config.workspace.setup_command, workspace)
        mock_deindex.assert_called_once_with(workspace, purge=True)


def test_last_setup_command_is_deindexed_artifact():
    assert ".forge/last_setup_command" in _FORGE_ARTIFACTS
