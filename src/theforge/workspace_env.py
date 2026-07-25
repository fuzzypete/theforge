"""Workspace subprocess environment helpers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class WorkspacePython:
    """Resolved workspace interpreter and validated pinned version."""

    executable: Path
    version: str
    version_parts: tuple[int, ...]


_PINNED_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


def _parse_version(version: str) -> tuple[int, ...]:
    if not _PINNED_VERSION_RE.fullmatch(version):
        raise ValueError(
            f"Workspace .python-version must pin major.minor or major.minor.patch, got {version!r}"
        )
    return tuple(int(part) for part in version.split("."))


def _read_workspace_python_pin(workspace_path: Path) -> str:
    pin_path = workspace_path / ".python-version"
    try:
        raw = pin_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(
            "Workspace "
            f"{workspace_path} is missing .python-version; "
            "gate setup requires a pinned Python interpreter."
        ) from exc

    for line in raw.splitlines():
        pin = line.split("#", 1)[0].strip()
        if pin:
            return pin
    raise ValueError(f"Workspace {workspace_path} has an empty .python-version pin.")


def _candidate_python_names(pin: str) -> list[str]:
    version_parts = pin.split(".")
    candidates = [f"python{pin}"]
    if len(version_parts) == 3:
        candidates.append(f"python{'.'.join(version_parts[:2])}")
    candidates.extend(["python3", "python"])
    return list(dict.fromkeys(candidates))


def _read_python_version(executable: str) -> str:
    proc = subprocess.run(
        [
            executable,
            "-c",
            "import sys; "
            "print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}')",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise ValueError(f"Failed to inspect Python interpreter {executable!r}: {stderr}")
    return proc.stdout.strip()


def resolve_workspace_python(workspace_path: Path) -> WorkspacePython:
    """Resolve the interpreter pinned by workspace .python-version from PATH."""
    pin = _read_workspace_python_pin(workspace_path)
    pinned_parts = _parse_version(pin.removeprefix("python"))

    for candidate_name in _candidate_python_names(pin):
        candidate = candidate_name if "/" in candidate_name else shutil.which(candidate_name)
        if not candidate:
            continue
        actual_version = _read_python_version(candidate)
        actual_parts = tuple(int(part) for part in actual_version.split("."))
        if actual_parts[: len(pinned_parts)] != pinned_parts:
            continue
        return WorkspacePython(
            executable=Path(candidate).resolve(),
            version=actual_version,
            version_parts=actual_parts,
        )

    searched = ", ".join(_candidate_python_names(pin))
    raise ValueError(
        "Workspace "
        f"{workspace_path} pins Python {pin!r}, but no matching interpreter "
        f"was found on PATH ({searched})."
    )


def read_workspace_venv_config(workspace_path: Path) -> dict[str, str]:
    """Read workspace .venv/pyvenv.cfg into a normalized key/value mapping."""
    cfg_path = workspace_path / ".venv" / "pyvenv.cfg"
    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}

    parsed: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip().lower()] = value.strip()
    return parsed


def workspace_venv_matches_python(
    workspace_path: Path, resolved_python: WorkspacePython | None = None
) -> bool:
    """Return True when workspace .venv provenance matches the pinned interpreter."""
    resolved = resolved_python or resolve_workspace_python(workspace_path)
    cfg = read_workspace_venv_config(workspace_path)
    if not cfg:
        return False

    cfg_version = cfg.get("version")
    try:
        cfg_version_parts = _parse_version(cfg_version) if cfg_version else ()
    except ValueError:
        return False
    if cfg_version_parts != resolved.version_parts:
        return False

    cfg_executable = cfg.get("executable")
    if cfg_executable:
        return Path(cfg_executable).resolve() == resolved.executable

    cfg_home = cfg.get("home")
    if cfg_home:
        return Path(cfg_home).resolve() == resolved.executable.parent

    return False


def build_workspace_env(
    workspace_path: Path,
    base_env: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an environment that prefers a matching workspace virtualenv when present."""
    env = dict(base_env or os.environ)
    venv_path = workspace_path / ".venv"
    venv_bin = venv_path / "bin"

    if venv_bin.exists() and workspace_venv_matches_python(workspace_path):
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
