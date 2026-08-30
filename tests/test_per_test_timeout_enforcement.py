"""The five-second per-test bound is configured, inherited, and attributable.

CONVENTIONS.md's rule used to be prose. These tests hold the line that it is
configuration: declared once in ``pyproject.toml`` so every invocation inherits
it, backed by a declared dev dependency, only shortenable per test, and — the
part that made the rule worth enforcing — attributable to a named test with a
stack trace even when the timeout kills an xdist worker outright.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from timeout_enforcement import (
    collect_dumps,
    configured_timeout,
    validate_timeout_mark,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_BOUND = 5.0


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


# ---------------------------------------------------------------------------
# The bound is declared once, in configuration
# ---------------------------------------------------------------------------


def test_pyproject_declares_the_five_second_bound():
    ini = _pyproject()["tool"]["pytest"]["ini_options"]
    assert ini["timeout"] == 5


def test_pyproject_declares_the_thread_enforcement_method():
    """Signal-method enforcement deadlocks every worker under worksteal."""
    ini = _pyproject()["tool"]["pytest"]["ini_options"]
    assert ini["timeout_method"] == "thread"


def test_runner_is_a_declared_dev_dependency():
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    assert any(spec.split("[")[0].split(">")[0].split("=")[0] == "pytest-timeout" for spec in dev)


def test_repository_commands_pass_no_timeout_flag():
    """Every invocation inherits the bound from config, including unanticipated ones."""
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "--timeout" not in makefile
    assert "--timeout" not in _pyproject()["tool"]["pytest"]["ini_options"]["addopts"]


def test_the_running_suite_actually_has_the_bound_in_force(pytestconfig):
    assert configured_timeout(pytestconfig) == SHARED_BOUND


# ---------------------------------------------------------------------------
# An override may shorten the bound, never widen or reshape it
# ---------------------------------------------------------------------------


def _mark(*args, **kwargs) -> pytest.Mark:
    return pytest.mark.timeout(*args, **kwargs).mark


@pytest.mark.parametrize(
    "mark",
    [
        pytest.param(_mark(1), id="shorter-positional"),
        pytest.param(_mark(timeout=0.25), id="shorter-keyword"),
        pytest.param(_mark(5), id="equal-to-shared-bound"),
        pytest.param(_mark(func_only=False), id="explicit-default-func_only"),
    ],
)
def test_shortening_the_bound_is_allowed(mark):
    assert validate_timeout_mark(mark, SHARED_BOUND) is None


@pytest.mark.parametrize(
    ("mark", "expected"),
    [
        pytest.param(_mark(6), "exceeds the shared", id="wider-positional"),
        pytest.param(_mark(timeout=6), "exceeds the shared", id="wider-keyword"),
        pytest.param(_mark(0), "disables", id="zero-disables"),
        pytest.param(_mark(-1), "disables", id="negative-disables"),
        pytest.param(_mark(1, method="signal"), "enforcement method", id="method-kwarg"),
        pytest.param(_mark(1, "signal"), "enforcement method", id="method-positional"),
        pytest.param(_mark(1, timeout_method="signal"), "enforcement method", id="ini-name"),
        pytest.param(_mark(1, func_only=True), "func_only", id="func-only"),
        pytest.param(_mark("soon"), "is not a number", id="non-numeric"),
        pytest.param(_mark(object()), "is not a number", id="non-numeric-object"),
        pytest.param(_mark(True), "is not a number", id="bool-is-not-a-duration"),
    ],
)
def test_widening_or_reshaping_the_bound_is_rejected(mark, expected):
    reason = validate_timeout_mark(mark, SHARED_BOUND)
    assert reason is not None
    assert expected in reason


def test_a_bare_timeout_mark_changes_nothing_and_is_accepted():
    assert validate_timeout_mark(_mark(), SHARED_BOUND) is None


def test_collect_dumps_ignores_empty_and_foreign_files(tmp_path):
    (tmp_path / "gw0.txt").write_text("THEFORGE-TIMEOUT-DUMP nodeid=t.py::a\n")
    (tmp_path / "gw1.txt").write_text("THEFORGE-TIMEOUT-DUMP nodeid=t.py::b\nFile 'x', line 1\n")
    (tmp_path / "other.txt").write_text("unrelated\n")
    assert collect_dumps(tmp_path) == [("t.py::b", "File 'x', line 1\n")]
    assert collect_dumps(tmp_path / "missing") == []


# ---------------------------------------------------------------------------
# A blocked test is named, with the stack where it was stuck
# ---------------------------------------------------------------------------

_CHILD_PYPROJECT = """\
[tool.pytest.ini_options]
timeout = 5
timeout_method = "thread"
addopts = "-n 2 --dist worksteal --max-worker-restart=0 -p no:cacheprovider"
"""

_CHILD_TESTS = '''\
"""One test that finishes, then one that blocks forever."""

import threading

import pytest


def test_finishes_promptly():
    assert True


@pytest.mark.timeout(0.25)
def test_blocks_forever():
    threading.Event().wait()
'''


def test_a_blocked_test_is_named_with_its_stack_under_xdist(tmp_path):
    """The failure the story is about: a test blocks, the worker dies, and the
    run must still say which test it was and where it was stuck.

    The child mirrors the real execution path (xdist + worksteal + the thread
    method) because that is where the attribution was lost: the thread method
    ends the test with ``os._exit(1)``, so the worker never gets its stack back
    to the controller on its own.

    It uses its own 0.25s bound, so the demonstration costs a quarter second
    rather than adding the shared five seconds to every run. Plugin autoload is
    off and the child runs two workers to keep its startup ~0.6s, which is what
    keeps this test inside the shared bound while the rest of the suite is
    saturating the machine.
    """
    (tmp_path / "pyproject.toml").write_text(_CHILD_PYPROJECT)
    (tmp_path / "test_blocking.py").write_text(_CHILD_TESTS)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist",
            "-p",
            "pytest_timeout",
            "-p",
            "timeout_enforcement",
            "-q",
            "--no-header",
        ],
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PYTHONPATH": str(REPO_ROOT / "tests"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        capture_output=True,
        text=True,
        # Bounded per CONVENTIONS.md; well under this test's own 5s budget.
        timeout=4,
    )
    output = proc.stdout + proc.stderr

    assert proc.returncode != 0, output
    # Named...
    assert "test_blocking.py::test_blocks_forever" in output, output
    # ...and located at the wait it was stuck on.
    assert "in test_blocks_forever" in output, output
    assert "threading.py" in output, output
    # The test that completed contributes no stack: its worker's dump slot is
    # cleared when the test ends, so only the culprit is reported.
    dump_section = output.split("per-test timeout stack dumps")[-1]
    assert "test_finishes_promptly" not in dump_section, output
    assert "1 passed" in output, output


def test_the_blocking_demonstration_source_is_what_the_child_runs():
    """Guard against the child fixture drifting into a no-op."""
    assert "@pytest.mark.timeout(0.25)" in _CHILD_TESTS
    assert "--timeout" not in _CHILD_PYPROJECT


def test_conftest_loads_the_same_support_the_child_project_loads():
    """The hooks must be live in this run, not merely importable.

    If tests/conftest.py stopped re-exporting them, the xdist demonstration
    above would still pass (it loads the plugin explicitly) while the real suite
    silently lost its attribution.
    """
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text()
    assert "from timeout_enforcement import" in conftest
    for hook in ("pytest_configure", "pytest_runtest_setup", "pytest_terminal_summary"):
        assert hook in conftest


def test_this_run_has_a_dump_channel_open():
    """The controller published a dump directory that workers inherited."""
    import timeout_enforcement

    assert timeout_enforcement._state.dir is not None
    assert os.environ.get(timeout_enforcement.DUMP_DIR_ENV)


def test_the_dump_hook_is_installed_and_costs_no_extra_thread():
    """Attribution rides pytest-timeout's own timer thread.

    An earlier design armed a faulthandler timer around every test; in a suite
    this size, full of forking process-group tests, that thread churn is the
    lock-inheritance hazard CONVENTIONS.md warns about. Wrapping
    ``timeout_timer`` adds no threads and no per-test work.
    """
    import pytest_timeout

    assert getattr(pytest_timeout.timeout_timer, "_theforge_dump_wrapper", False)

    source = (REPO_ROOT / "tests" / "timeout_enforcement.py").read_text()
    assert "import faulthandler" not in source
    assert "faulthandler.dump_traceback_later(" not in source
    # No per-test hook at all: nothing runs around a test that does not time out.
    assert "def pytest_runtest_protocol" not in source
