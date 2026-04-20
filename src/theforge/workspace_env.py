"""Workspace subprocess environment helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def build_workspace_env(
    workspace_path: Path,
    base_env: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an environment that prefers the workspace virtualenv when present."""
    env = dict(base_env or os.environ)
    venv_path = workspace_path / ".venv"
    venv_bin = venv_path / "bin"

    if venv_bin.exists():
        path_entries = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
        filtered_entries = [
            entry
            for entry in path_entries
            if "/.pyenv/shims" not in entry and "/.pyenv/versions/" not in entry
        ]
        env["PATH"] = os.pathsep.join([str(venv_bin), *filtered_entries])
        env["VIRTUAL_ENV"] = str(venv_path)
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        env.pop("__PYVENV_LAUNCHER__", None)

    if extra:
        env.update(extra)
    return env
