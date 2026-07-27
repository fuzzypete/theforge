"""Workspace subprocess environment helpers.

Stdlib-only by design: this module is imported by runners, the coordinator, and
gate diagnostics, so it must not pull in configuration or provider machinery.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Mapping

log = logging.getLogger("theforge.workspace_env")

_INTERPRETER_PROBE_TIMEOUT_SECONDS = 15

# ``sys._base_executable`` resolves through nested virtualenvs to the real
# interpreter; ``sys.executable`` inside a venv points at the venv's own shim.
# ``venv`` records the former in pyvenv.cfg, so the pin must be probed the same
# way or every comparison against a venv-hosted interpreter would spuriously
# mismatch.
_BASE_EXECUTABLE_PROBE = (
    "import sys; print(getattr(sys, '_base_executable', None) or sys.executable)"
)


def read_venv_base_executable(venv_path: Path) -> str | None:
    """Return the base interpreter a virtualenv was created from, if recorded.

    Reads ``pyvenv.cfg``'s ``executable`` key, falling back to the first token of
    ``command`` for virtualenvs written without it. Returns None when the file is
    absent or records neither — callers must treat that as "cannot prove a
    match", not as a match.
    """
    try:
        text = (venv_path / "pyvenv.cfg").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    executable: str | None = None
    command_exe: str | None = None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        name = key.strip().lower()
        val = value.strip()
        if not val:
            continue
        if name == "executable":
            executable = val
        elif name == "command":
            try:
                tokens = shlex.split(val)
            except ValueError:
                tokens = val.split()
            if tokens:
                command_exe = tokens[0]
    return executable or command_exe


@lru_cache(maxsize=32)
def resolve_base_executable(python_command: str) -> str | None:
    """Resolve an interpreter command to the real path of its base interpreter.

    ``python_command`` may be a bare name resolved through PATH (``python3.12``),
    a launcher shim, or an absolute path — including one inside a virtualenv.
    The interpreter is probed once per process so the result is comparable with
    what ``venv`` writes into pyvenv.cfg. Returns None when the command does not
    resolve to an existing file.
    """
    candidate = shutil.which(python_command) or python_command
    if not os.path.exists(candidate):
        return None
    try:
        probe = subprocess.run(
            [candidate, "-c", _BASE_EXECUTABLE_PROBE],
            capture_output=True,
            text=True,
            timeout=_INTERPRETER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return os.path.realpath(candidate)
    resolved = probe.stdout.strip()
    if probe.returncode == 0 and resolved:
        return os.path.realpath(resolved)
    return os.path.realpath(candidate)


def venv_matches_interpreter(venv_path: Path, expected_python: str) -> bool:
    """Whether ``venv_path`` was provably created from ``expected_python``.

    Fails closed: anything that cannot be proven — no pyvenv.cfg, no recorded
    interpreter, an unresolvable pin — is a mismatch. A virtualenv whose
    provenance is unknown must not be allowed to decide which Python a gate runs
    under.
    """
    recorded = read_venv_base_executable(venv_path)
    if not recorded:
        return False
    expected = resolve_base_executable(expected_python)
    if not expected:
        return False
    return os.path.realpath(recorded) == expected


def build_workspace_env(
    workspace_path: Path,
    base_env: Mapping[str, str] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
    expected_python: str | None = None,
) -> dict[str, str]:
    """Build an environment that prefers the workspace virtualenv when present.

    ``expected_python`` is the patient project's pinned interpreter. When given,
    an existing ``.venv`` is only put on PATH if it was built from that
    interpreter; otherwise it is left unpreferred so a virtualenv provisioned by
    a differently-built orchestrator cannot silently change the runtime. Callers
    that have no pin available omit it and keep the historical behavior.
    """
    env = dict(base_env or os.environ)
    venv_path = workspace_path / ".venv"
    venv_bin = venv_path / "bin"

    prefer_venv = venv_bin.exists()
    if prefer_venv and expected_python is not None:
        if not venv_matches_interpreter(venv_path, expected_python):
            log.warning(
                "workspace virtualenv at %s was not built from the pinned interpreter %r "
                "(recorded: %s) — not preferring it on PATH",
                venv_path,
                expected_python,
                read_venv_base_executable(venv_path) or "unknown",
            )
            prefer_venv = False

    if prefer_venv:
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
