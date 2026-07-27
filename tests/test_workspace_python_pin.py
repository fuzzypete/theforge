"""Tests for the patient project's pinned gate interpreter.

The gate must run under the interpreter the project declares, never under
whichever Python TheForge itself happens to be installed under, and never under
whatever interpreter a leftover worktree virtualenv was built from.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from theforge.config import load_config
from theforge.coordinator.workspace import _invalidate_stale_venv, _run_setup_split
from theforge.workspace_env import (
    build_workspace_env,
    read_venv_base_executable,
    resolve_base_executable,
    venv_matches_interpreter,
)

_OTHER_PYTHON = "/opt/some-other-runtime/bin/python3.14"


def _base_executable() -> str:
    """The real interpreter behind this test run, as ``venv`` would record it."""
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


def _fake_venv(root: Path, *, executable: str | None = None, command: str | None = None) -> Path:
    """Create a .venv skeleton with the given pyvenv.cfg provenance keys."""
    venv = root / ".venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    lines = ["home = /irrelevant", "version = 3.12.12"]
    if executable is not None:
        lines.append(f"executable = {executable}")
    if command is not None:
        lines.append(f"command = {command}")
    (venv / "pyvenv.cfg").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return venv


def _write_config(data: dict, tmp_dir: Path) -> Path:
    config_path = tmp_dir / "forge.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


class TestReadVenvBaseExecutable:
    def test_reads_executable_key(self, tmp_path):
        venv = _fake_venv(tmp_path, executable="/usr/bin/python3.12")
        assert read_venv_base_executable(venv) == "/usr/bin/python3.12"

    def test_falls_back_to_command_first_token(self, tmp_path):
        venv = _fake_venv(tmp_path, command="/usr/bin/python3.12 -m venv /tmp/x")
        assert read_venv_base_executable(venv) == "/usr/bin/python3.12"

    def test_command_with_spaces_is_shell_split(self, tmp_path):
        venv = _fake_venv(tmp_path, command="'/opt/my python/bin/python3' -m venv /tmp/x")
        assert read_venv_base_executable(venv) == "/opt/my python/bin/python3"

    def test_missing_pyvenv_cfg_is_unknown(self, tmp_path):
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        assert read_venv_base_executable(tmp_path / ".venv") is None

    def test_cfg_without_provenance_keys_is_unknown(self, tmp_path):
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("home = /x\nversion = 3.12.12\n", encoding="utf-8")
        assert read_venv_base_executable(venv) is None


class TestVenvMatchesInterpreter:
    def test_matches_when_built_from_the_pin(self, tmp_path):
        venv = _fake_venv(tmp_path, executable=_base_executable())
        assert venv_matches_interpreter(venv, sys.executable) is True

    def test_mismatch_when_built_from_another_interpreter(self, tmp_path):
        venv = _fake_venv(tmp_path, executable=_OTHER_PYTHON)
        assert venv_matches_interpreter(venv, sys.executable) is False

    def test_unknown_provenance_fails_closed(self, tmp_path):
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        assert venv_matches_interpreter(tmp_path / ".venv", sys.executable) is False

    def test_unresolvable_pin_fails_closed(self, tmp_path):
        venv = _fake_venv(tmp_path, executable=_base_executable())
        assert venv_matches_interpreter(venv, "/nonexistent/bin/python3.99") is False

    def test_resolve_base_executable_of_missing_command_is_none(self):
        assert resolve_base_executable("/nonexistent/bin/python3.99") is None


class TestBuildWorkspaceEnvPinAware:
    def test_matching_venv_is_preferred(self, tmp_path):
        venv = _fake_venv(tmp_path, executable=_base_executable())

        env = build_workspace_env(tmp_path, {"PATH": "/usr/bin"}, expected_python=sys.executable)

        assert env["PATH"].split(os.pathsep)[0] == str(venv / "bin")
        assert env["VIRTUAL_ENV"] == str(venv)

    def test_stale_venv_is_not_put_on_path(self, tmp_path):
        _fake_venv(tmp_path, executable=_OTHER_PYTHON)

        env = build_workspace_env(tmp_path, {"PATH": "/usr/bin"}, expected_python=sys.executable)

        assert env["PATH"] == "/usr/bin"
        assert "VIRTUAL_ENV" not in env

    def test_venv_with_unknown_provenance_is_not_put_on_path(self, tmp_path):
        (tmp_path / ".venv" / "bin").mkdir(parents=True)

        env = build_workspace_env(tmp_path, {"PATH": "/usr/bin"}, expected_python=sys.executable)

        assert env["PATH"] == "/usr/bin"

    def test_without_a_pin_the_venv_is_preferred_as_before(self, tmp_path):
        venv = _fake_venv(tmp_path, executable=_OTHER_PYTHON)

        env = build_workspace_env(tmp_path, {"PATH": "/usr/bin"})

        assert env["PATH"].split(os.pathsep)[0] == str(venv / "bin")


class TestInvalidateStaleVenv:
    def test_removes_venv_built_from_another_interpreter(self, tmp_path):
        venv = _fake_venv(tmp_path, executable=_OTHER_PYTHON)

        assert _invalidate_stale_venv(tmp_path, sys.executable) is None
        assert not venv.exists()

    def test_keeps_venv_built_from_the_pin(self, tmp_path):
        venv = _fake_venv(tmp_path, executable=_base_executable())

        assert _invalidate_stale_venv(tmp_path, sys.executable) is None
        assert venv.exists()

    def test_no_pin_leaves_the_venv_alone(self, tmp_path):
        venv = _fake_venv(tmp_path, executable=_OTHER_PYTHON)

        assert _invalidate_stale_venv(tmp_path, None) is None
        assert venv.exists()

    def test_missing_venv_is_a_noop(self, tmp_path):
        assert _invalidate_stale_venv(tmp_path, sys.executable) is None

    def test_removal_failure_is_reported_not_swallowed(self, tmp_path):
        _fake_venv(tmp_path, executable=_OTHER_PYTHON)

        with patch(
            "theforge.coordinator.workspace.shutil.rmtree",
            side_effect=OSError("permission denied"),
        ):
            err = _invalidate_stale_venv(tmp_path, sys.executable)

        assert err is not None
        assert "permission denied" in err


class TestRunSetupSplitReprovisionsStaleVenv:
    def test_stale_venv_is_removed_before_the_guard_runs(self, tmp_path):
        venv = _fake_venv(tmp_path, executable=_OTHER_PYTHON)
        cmd = "test -d .venv || ({forge_python} -m venv .venv && .venv/bin/pip install -e .)"
        calls: list[str] = []

        def fake_shell(cmd_arg, cwd, **kw):
            calls.append(cmd_arg)
            return (True, "ok")

        with patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell):
            ok, _out = _run_setup_split(cmd, tmp_path, sys.executable)

        assert ok is True
        assert not venv.exists()  # guard will now actually create it under the pin
        assert len(calls) == 2

    def test_matching_venv_survives_setup(self, tmp_path):
        venv = _fake_venv(tmp_path, executable=_base_executable())
        cmd = "test -d .venv || ({forge_python} -m venv .venv && .venv/bin/pip install -e .)"

        with patch("theforge.coordinator.workspace._cu._run_shell", return_value=(True, "ok")):
            ok, _out = _run_setup_split(cmd, tmp_path, sys.executable)

        assert ok is True
        assert venv.exists()

    def test_unremovable_stale_venv_fails_setup_closed(self, tmp_path):
        _fake_venv(tmp_path, executable=_OTHER_PYTHON)
        cmd = "test -d .venv || ({forge_python} -m venv .venv && .venv/bin/pip install -e .)"
        calls: list[str] = []

        def fake_shell(cmd_arg, cwd, **kw):
            calls.append(cmd_arg)
            return (True, "ok")

        with (
            patch(
                "theforge.coordinator.workspace.shutil.rmtree",
                side_effect=OSError("permission denied"),
            ),
            patch("theforge.coordinator.workspace._cu._run_shell", side_effect=fake_shell),
        ):
            ok, out = _run_setup_split(cmd, tmp_path, sys.executable)

        assert ok is False
        assert "stale virtualenv" in out
        assert calls == []  # nothing ran under the wrong interpreter


class TestWorkspaceConfigPin:
    def test_pin_is_parsed(self, tmp_path):
        config_path = _write_config(
            {
                "workspace": {
                    "setup_command": "test -d .venv || {forge_python} -m venv .venv",
                    "python_interpreter": "python3.12",
                }
            },
            tmp_path,
        )

        config = load_config(config_path)

        assert config.workspace.python_interpreter == "python3.12"

    def test_forge_python_without_a_pin_is_rejected(self, tmp_path):
        config_path = _write_config(
            {"workspace": {"setup_command": "test -d .venv || {forge_python} -m venv .venv"}},
            tmp_path,
        )

        with pytest.raises(ValueError, match="python_interpreter"):
            load_config(config_path)

    def test_blank_pin_is_rejected_like_a_missing_one(self, tmp_path):
        config_path = _write_config(
            {
                "workspace": {
                    "setup_command": "{forge_python} -m venv .venv",
                    "python_interpreter": "   ",
                }
            },
            tmp_path,
        )

        with pytest.raises(ValueError, match="python_interpreter"):
            load_config(config_path)

    def test_setup_command_without_the_placeholder_needs_no_pin(self, tmp_path):
        config_path = _write_config({"workspace": {"setup_command": "pip install -e ."}}, tmp_path)

        config = load_config(config_path)

        assert config.workspace.python_interpreter is None

    def test_default_config_has_no_pin(self, tmp_path):
        config = load_config(_write_config({}, tmp_path))

        assert config.workspace.python_interpreter is None


class TestGateRunsUnderThePin:
    def _config(self, tmp_path, pin):
        workspace: dict[str, object] = {
            "setup_command": "test -d .venv || {forge_python} -m venv .venv"
        }
        if pin is not None:
            workspace["python_interpreter"] = pin
        return load_config(
            _write_config(
                {"workspace": workspace, "validation": {"gate_command": "make gate"}},
                tmp_path,
            )
        )

    def test_gate_command_is_run_with_the_pin(self, tmp_path):
        from theforge.coordinator.gate import run_gate_full

        config = self._config(tmp_path, "python3.12")
        seen: dict[str, object] = {}

        def fake_run(cmd, cwd, timeout=120, env=None, expected_python=None):
            seen["expected_python"] = expected_python
            return (True, "ok", 0, False)

        with patch("theforge.coordinator.gate._cu._run_shell_detailed", side_effect=fake_run):
            decision, error, _tail, _cmd, _exit = run_gate_full(config, tmp_path)

        assert error is None
        assert decision == "PASS"
        assert seen["expected_python"] == "python3.12"

    def test_gate_falls_back_to_no_pin_when_project_declares_none(self, tmp_path):
        from theforge.coordinator.gate import run_gate_full

        config = load_config(
            _write_config({"validation": {"gate_command": "make gate"}}, tmp_path)
        )
        seen: dict[str, object] = {"expected_python": "unset"}

        def fake_run(cmd, cwd, timeout=120, env=None, expected_python=None):
            seen["expected_python"] = expected_python
            return (True, "ok", 0, False)

        with patch("theforge.coordinator.gate._cu._run_shell_detailed", side_effect=fake_run):
            run_gate_full(config, tmp_path)

        assert seen["expected_python"] is None
