"""Tests for workspace subprocess environment construction."""

from __future__ import annotations

import os
from pathlib import Path

from theforge.workspace_env import build_workspace_env


def test_build_workspace_env_prepends_venv_and_sets_virtual_env(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    base_env = {"PATH": "/usr/bin:/bin", "PYTHONHOME": "/opt/python"}

    env = build_workspace_env(tmp_path, base_env)

    assert env["PATH"].split(os.pathsep)[0] == str(venv_bin)
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    assert "PYTHONHOME" not in env


def test_build_workspace_env_without_venv_preserves_base_env(tmp_path: Path) -> None:
    base_env = {"PATH": "/usr/bin", "PYTHONHOME": "/opt/python"}

    env = build_workspace_env(tmp_path, base_env, extra={"EXTRA": "1"})

    assert env == {"PATH": "/usr/bin", "PYTHONHOME": "/opt/python", "EXTRA": "1"}


def test_build_workspace_env_strips_pyenv_path_entries(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    base_env = {
        "PATH": os.pathsep.join(
            [
                "/Users/example/.pyenv/shims",
                "/usr/local/bin",
                "/Users/example/.pyenv/versions/3.12.12/bin",
                "/usr/bin",
            ]
        )
    }

    env = build_workspace_env(tmp_path, base_env)

    assert env["PATH"].split(os.pathsep) == [str(venv_bin), "/usr/local/bin", "/usr/bin"]


def test_build_workspace_env_drops_python_leak_variables(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    base_env = {
        "PATH": "/usr/bin",
        "PYTHONHOME": "/opt/python",
        "PYTHONPATH": "/tmp/imports",
        "__PYVENV_LAUNCHER__": "/usr/bin/python",
    }

    env = build_workspace_env(tmp_path, base_env)

    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert "__PYVENV_LAUNCHER__" not in env


def test_build_workspace_env_merges_extra_last(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)

    env = build_workspace_env(tmp_path, {"PATH": "/usr/bin"}, extra={"VIRTUAL_ENV": "override"})

    assert env["VIRTUAL_ENV"] == "override"
