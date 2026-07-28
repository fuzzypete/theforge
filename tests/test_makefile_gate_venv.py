"""The per-version gate venv must be refreshed when dependency metadata changes.

CI installs dependencies fresh for every run. A matrix leg that reused a venv
built before pyproject.toml changed would prove a dependency set CI never
installs — the same gate/CI disagreement the interpreter matrix exists to close
(#1945). These tests drive the real `gate-venv` target from the repository
Makefile with a stubbed interpreter and pip, so they assert the actual recipe
rather than its transcription.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# A version that cannot collide with a real interpreter on the host.
STUB_VERSION = "9.9"

_PYTHON_STUB = """\
#!/usr/bin/env python3
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] != ["-m", "venv"]:
    raise SystemExit(f"unexpected interpreter args: {args!r}")
venv = Path(args[2])
(venv / "bin").mkdir(parents=True, exist_ok=True)
pip = venv / "bin" / "pip"
pip.write_text('#!/bin/sh\\necho "$@" >> pip.log\\n')
pip.chmod(0o755)
"""


@pytest.fixture
def stub_repo(tmp_path: Path) -> Path:
    """A minimal checkout: the real Makefile, a pyproject, and a stub interpreter."""
    shutil.copy(REPO_ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "stub"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    interpreter = tmp_path / f"python{STUB_VERSION}"
    interpreter.write_text(_PYTHON_STUB, encoding="utf-8")
    interpreter.chmod(0o755)
    return tmp_path


def _run_gate_venv(repo: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{repo}:{os.environ['PATH']}"}
    return subprocess.run(
        ["make", "gate-venv", f"PY={STUB_VERSION}"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def _pip_invocations(repo: Path) -> list[str]:
    log = repo / "pip.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_first_run_creates_the_venv_and_installs_dev_extras(stub_repo: Path) -> None:
    result = _run_gate_venv(stub_repo)

    assert result.returncode == 0, result.stderr
    assert (stub_repo / f".venv-{STUB_VERSION}").is_dir()
    invocations = _pip_invocations(stub_repo)
    assert len(invocations) == 1
    # The same extras CI installs — not the local ".[all,dev]".
    assert "install -e .[dev]" in invocations[0]


def test_unchanged_dependency_metadata_reuses_the_venv(stub_repo: Path) -> None:
    assert _run_gate_venv(stub_repo).returncode == 0
    result = _run_gate_venv(stub_repo)

    assert result.returncode == 0, result.stderr
    assert len(_pip_invocations(stub_repo)) == 1


def test_changed_dependency_metadata_reinstalls_into_the_existing_venv(
    stub_repo: Path,
) -> None:
    # The P1 from cycle 1: keying the cached venv on directory existence alone
    # let a leg run against packages from an earlier commit's dependency set.
    assert _run_gate_venv(stub_repo).returncode == 0
    assert len(_pip_invocations(stub_repo)) == 1

    (stub_repo / "pyproject.toml").write_text(
        '[project]\nname = "stub"\nversion = "0.1.0"\ndependencies = ["attrs"]\n',
        encoding="utf-8",
    )
    result = _run_gate_venv(stub_repo)

    assert result.returncode == 0, result.stderr
    assert len(_pip_invocations(stub_repo)) == 2


def test_failed_install_is_not_stamped_as_good(stub_repo: Path) -> None:
    # A stamp written before the install succeeded would cache a broken venv and
    # make every later leg skip the repair.
    venv = stub_repo / f".venv-{STUB_VERSION}"
    (venv / "bin").mkdir(parents=True)
    failing_pip = venv / "bin" / "pip"
    failing_pip.write_text('#!/bin/sh\necho "$@" >> pip.log\nexit 1\n', encoding="utf-8")
    failing_pip.chmod(0o755)

    result = _run_gate_venv(stub_repo)

    assert result.returncode != 0
    assert not (venv / ".forge-gate-deps").exists()


def test_missing_py_argument_fails_closed(stub_repo: Path) -> None:
    env = {**os.environ, "PATH": f"{stub_repo}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["make", "gate-venv"], cwd=stub_repo, capture_output=True, text=True, env=env
    )

    assert result.returncode != 0
    assert "requires PY=" in result.stderr
    assert not _pip_invocations(stub_repo)


def test_missing_interpreter_fails_closed(stub_repo: Path) -> None:
    (stub_repo / f"python{STUB_VERSION}").unlink()
    env = {**os.environ, "PATH": f"{stub_repo}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["make", "gate-venv", f"PY={STUB_VERSION}"],
        cwd=stub_repo,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert f"python{STUB_VERSION} not found on PATH" in result.stderr
    assert not _pip_invocations(stub_repo)
