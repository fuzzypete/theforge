"""Tests for workspace subprocess environment construction."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.workspace_env import (
    WorkspacePython,
    build_workspace_env,
    maybe_resolve_workspace_python,
    resolve_workspace_python,
    workspace_venv_is_usable,
    workspace_venv_matches_python,
)


def _write_workspace_pin(workspace: Path, version: str = "3.12.12") -> None:
    (workspace / ".python-version").write_text(f"{version}\n", encoding="utf-8")


def _write_pyvenv_cfg(
    workspace: Path,
    *,
    version: str,
    executable: str,
    home: str | None = None,
) -> None:
    home_value = home or str(Path(executable).parent)
    cfg = workspace / ".venv" / "pyvenv.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "\n".join(
            [
                f"home = {home_value}",
                "include-system-site-packages = false",
                f"version = {version}",
                f"executable = {executable}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _resolved_python() -> WorkspacePython:
    return WorkspacePython(
        executable=Path("/tmp/toolchain/python3.12").resolve(),
        version="3.12.12",
        version_parts=(3, 12, 12),
    )


def test_resolve_workspace_python_uses_pinned_interpreter(tmp_path: Path) -> None:
    _write_workspace_pin(tmp_path)
    expected = "/tmp/toolchain/python3.12"

    with (
        patch("theforge.workspace_env.shutil.which") as mock_which,
        patch("theforge.workspace_env._read_python_version") as mock_version,
    ):
        mock_which.side_effect = lambda name: expected if name == "python3.12" else None
        mock_version.return_value = "3.12.12"
        resolved = resolve_workspace_python(tmp_path)

    assert resolved.version == "3.12.12"
    assert resolved.executable.name == "python3.12"
    assert resolved.executable == Path(expected).resolve()


def test_resolve_workspace_python_requires_repo_pin(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"missing \.python-version"):
        resolve_workspace_python(tmp_path)


def test_maybe_resolve_workspace_python_returns_none_without_pin(tmp_path: Path) -> None:
    assert maybe_resolve_workspace_python(tmp_path) is None


def test_maybe_resolve_workspace_python_raises_when_declared_pin_is_unresolved(
    tmp_path: Path,
) -> None:
    _write_workspace_pin(tmp_path, "3.99.1")

    with pytest.raises(ValueError, match=r"pins Python '3\.99\.1'"):
        maybe_resolve_workspace_python(tmp_path)


def test_resolve_workspace_python_rejects_non_pinned_value(tmp_path: Path) -> None:
    _write_workspace_pin(tmp_path, "3.12-dev")

    with pytest.raises(ValueError, match=r"must pin major\.minor or major\.minor\.patch"):
        resolve_workspace_python(tmp_path)


def test_build_workspace_env_prepends_venv_and_sets_virtual_env(tmp_path: Path) -> None:
    _write_workspace_pin(tmp_path)
    resolved = _resolved_python()
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_pyvenv_cfg(tmp_path, version=resolved.version, executable=str(resolved.executable))
    base_env = {"PATH": "/usr/bin:/bin", "PYTHONHOME": "/opt/python"}

    with patch("theforge.workspace_env.resolve_workspace_python", return_value=resolved):
        env = build_workspace_env(tmp_path, base_env)

    assert env["PATH"].split(os.pathsep)[0] == str(venv_bin)
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    assert "PYTHONHOME" not in env


def test_build_workspace_env_without_venv_preserves_base_env(tmp_path: Path) -> None:
    base_env = {"PATH": "/usr/bin", "PYTHONHOME": "/opt/python"}

    env = build_workspace_env(tmp_path, base_env, extra={"EXTRA": "1"})

    assert env == {"PATH": "/usr/bin", "PYTHONHOME": "/opt/python", "EXTRA": "1"}


def test_build_workspace_env_strips_pyenv_path_entries(tmp_path: Path) -> None:
    _write_workspace_pin(tmp_path)
    resolved = _resolved_python()
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_pyvenv_cfg(tmp_path, version=resolved.version, executable=str(resolved.executable))
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

    with patch("theforge.workspace_env.resolve_workspace_python", return_value=resolved):
        env = build_workspace_env(tmp_path, base_env)

    assert env["PATH"].split(os.pathsep) == [str(venv_bin), "/usr/local/bin", "/usr/bin"]


def test_build_workspace_env_drops_python_leak_variables(tmp_path: Path) -> None:
    _write_workspace_pin(tmp_path)
    resolved = _resolved_python()
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_pyvenv_cfg(tmp_path, version=resolved.version, executable=str(resolved.executable))
    base_env = {
        "PATH": "/usr/bin",
        "PYTHONHOME": "/opt/python",
        "PYTHONPATH": "/tmp/imports",
        "__PYVENV_LAUNCHER__": "/usr/bin/python",
    }

    with patch("theforge.workspace_env.resolve_workspace_python", return_value=resolved):
        env = build_workspace_env(tmp_path, base_env)

    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert "__PYVENV_LAUNCHER__" not in env


def test_build_workspace_env_merges_extra_last(tmp_path: Path) -> None:
    _write_workspace_pin(tmp_path)
    resolved = _resolved_python()
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_pyvenv_cfg(tmp_path, version=resolved.version, executable=str(resolved.executable))

    with patch("theforge.workspace_env.resolve_workspace_python", return_value=resolved):
        env = build_workspace_env(
            tmp_path,
            {"PATH": "/usr/bin"},
            extra={"VIRTUAL_ENV": "override"},
        )

    assert env["VIRTUAL_ENV"] == "override"


def test_workspace_venv_matches_python_rejects_stale_interpreter(tmp_path: Path) -> None:
    _write_workspace_pin(tmp_path)
    resolved = _resolved_python()
    stale_executable = "/tmp/toolchain/python3.11"
    _write_pyvenv_cfg(tmp_path, version="3.11.9", executable=stale_executable)

    with patch("theforge.workspace_env.resolve_workspace_python", return_value=resolved):
        assert workspace_venv_matches_python(tmp_path) is False


def test_workspace_venv_matches_python_accepts_matching_home_without_executable(
    tmp_path: Path,
) -> None:
    _write_workspace_pin(tmp_path)
    resolved = _resolved_python()
    cfg = tmp_path / ".venv" / "pyvenv.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "\n".join(
            [
                f"home = {resolved.executable.parent}",
                "include-system-site-packages = false",
                f"version = {resolved.version}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with patch("theforge.workspace_env.resolve_workspace_python", return_value=resolved):
        assert workspace_venv_matches_python(tmp_path) is True


def test_build_workspace_env_ignores_stale_venv_on_path(tmp_path: Path) -> None:
    _write_workspace_pin(tmp_path)
    resolved = _resolved_python()
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_pyvenv_cfg(
        tmp_path,
        version="3.11.9",
        executable="/Users/example/.pyenv/versions/3.11.9/bin/python3.11",
    )
    base_env = {"PATH": "/usr/local/bin:/usr/bin", "PYTHONHOME": "/opt/python"}

    with patch("theforge.workspace_env.resolve_workspace_python", return_value=resolved):
        env = build_workspace_env(tmp_path, base_env)

    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert "VIRTUAL_ENV" not in env
    assert env["PYTHONHOME"] == "/opt/python"


def test_build_workspace_env_ignores_existing_venv_when_pin_missing(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_pyvenv_cfg(
        tmp_path,
        version="3.11.9",
        executable="/Users/example/.pyenv/versions/3.11.9/bin/python3.11",
    )
    base_env = {"PATH": "/usr/local/bin:/usr/bin", "PYTHONHOME": "/opt/python"}

    env = build_workspace_env(tmp_path, base_env)

    assert env["PATH"].split(os.pathsep)[0] == str(venv_bin)
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    assert "PYTHONHOME" not in env


def test_workspace_venv_is_usable_without_pin_when_cfg_exists(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_pyvenv_cfg(
        tmp_path,
        version="3.11.9",
        executable="/Users/example/.pyenv/versions/3.11.9/bin/python3.11",
    )

    assert workspace_venv_is_usable(tmp_path) is True


def test_build_workspace_env_raises_for_unresolved_declared_pin(tmp_path: Path) -> None:
    _write_workspace_pin(tmp_path, "3.99.1")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_pyvenv_cfg(
        tmp_path,
        version="3.99.1",
        executable="/tmp/toolchain/python3.99",
    )

    with pytest.raises(ValueError, match=r"pins Python '3\.99\.1'"):
        build_workspace_env(tmp_path, {"PATH": "/usr/local/bin:/usr/bin"})
