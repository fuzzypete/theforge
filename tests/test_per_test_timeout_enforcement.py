"""The concurrent gate's per-test bound is configured, inherited, and attributable.

CONVENTIONS.md's rule used to be prose. These tests hold the line that it is
configuration: declared once in ``pyproject.toml`` so every invocation inherits
it, backed by a declared dev dependency, only shortenable per test, and — the
part that made the rule worth enforcing — attributable to a named test with a
stack trace even when the timeout kills an xdist worker outright.

The last section holds the other half of the contract, and it is now a negative
one: there is exactly one bound, it is shared by every test, and no mechanism
exists to grant a larger one. The category that used to do that measured the
machine rather than the test — under ``-n auto --dist worksteal`` even its
raised 30s bound was reached by a 3.44s test — so it was removed rather than
widened (#2833).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import timeout_enforcement as te
from timeout_enforcement import (
    collect_dumps,
    configured_timeout,
    validate_timeout_mark,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The one bound every test in the default suite runs under.
SHARED_BOUND = 60.0


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


# ---------------------------------------------------------------------------
# The bound is declared once, in configuration
# ---------------------------------------------------------------------------


def test_pyproject_declares_the_shared_bound():
    """Sixty seconds, above anything this suite's own contention produces.

    A smaller value needs evidence that inflation cannot reach it: measured
    inflation under ``-n auto --dist worksteal`` ran to roughly 9x, enough for a
    3.44s test to cross a 30s bound (#2833).
    """
    ini = _pyproject()["tool"]["pytest"]["ini_options"]
    assert ini["timeout"] == 60


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
        pytest.param(_mark(60), id="equal-to-shared-bound"),
        pytest.param(_mark(func_only=False), id="explicit-default-func_only"),
    ],
)
def test_shortening_the_bound_is_allowed(mark):
    assert validate_timeout_mark(mark, SHARED_BOUND) is None


@pytest.mark.parametrize(
    ("mark", "expected"),
    [
        pytest.param(_mark(61), "exceeds the shared", id="wider-positional"),
        pytest.param(_mark(timeout=61), "exceeds the shared", id="wider-keyword"),
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


class _RecordingReporter:
    """Captures what the controller would print at terminal summary."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_sep(self, ch, title="", **kwargs):
        self.lines.append(f"{ch * 8} {title} {ch * 8}")

    def write_line(self, text="", **kwargs):
        self.lines.append(str(text))


def test_the_culprit_and_its_stack_survive_the_worker_exit(tmp_path, monkeypatch):
    """The dump channel, proven without spawning anything.

    This is the deterministic anchor for attribution: it exercises the exact
    code that runs on pytest-timeout's timer thread just before ``os._exit(1)``
    kills an xdist worker, then the controller-side re-emit. The xdist test
    below proves the same path end to end, but it pays real process startup and
    so is the more fragile of the two; this one cannot flake.
    """
    import timeout_enforcement as te

    monkeypatch.setattr(te._state, "dir", tmp_path)
    monkeypatch.setattr(te._state, "worker_id", "gw7")

    class _Item:
        nodeid = "tests/test_thing.py::test_blocks"

    class _Settings:
        timeout = 5

    te._write_dump(_Item(), _Settings())

    # The worker recorded who was running and where it was.
    dumps = collect_dumps(tmp_path)
    assert [nodeid for nodeid, _ in dumps] == ["tests/test_thing.py::test_blocks"]
    body = dumps[0][1]
    assert "test_the_culprit_and_its_stack_survive_the_worker_exit" in body, body
    assert "5s per-test bound" in body

    # The controller re-emits it, naming the culprit.
    reporter = _RecordingReporter()

    class _Config:
        pass

    te.pytest_terminal_summary(reporter, 1, _Config())
    printed = "\n".join(reporter.lines)
    assert "per-test timeout stack dumps" in printed
    assert "tests/test_thing.py::test_blocks" in printed
    assert "test_the_culprit_and_its_stack_survive_the_worker_exit" in printed


def test_a_worker_that_never_timed_out_leaves_no_dump(tmp_path, monkeypatch):
    """Only the process that actually timed out writes anything at all.

    This is what keeps a re-emitted stack attributable: there is no per-test
    arming, so a worker that finished its tests normally contributes nothing
    the controller could misattribute.
    """
    import timeout_enforcement as te

    monkeypatch.setattr(te._state, "dir", tmp_path)
    assert collect_dumps(tmp_path) == []
    reporter = _RecordingReporter()

    class _Config:
        pass

    te.pytest_terminal_summary(reporter, 0, _Config())
    assert reporter.lines == []


_CHILD_PYPROJECT = """\
[tool.pytest.ini_options]
timeout = 5
timeout_method = "thread"
addopts = "-p no:cacheprovider"
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


def test_a_real_timeout_names_the_blocked_test_and_where_it_was_stuck(tmp_path):
    """The failure the story is about, driven by a genuine pytest timeout.

    A child pytest run blocks past its own bound, so pytest-timeout's thread
    handler fires for real and ends the process with ``os._exit(1)``. That exit
    is exactly what used to lose the evidence. The dump this test reads back is
    written on the way out, and it has to name the culprit and show the frame it
    was parked on.

    The child uses its own 0.25s bound so the demonstration costs a quarter
    second rather than the shared bound on every run, and it runs in a single
    process: measured under a saturated machine an xdist child spiked to 13.3s
    of pure startup contention, and paying real process startup twice buys this
    test nothing it does not already prove. The controller-side re-emit that
    turns this file into run output is covered by
    :func:`test_the_culprit_and_its_stack_survive_the_worker_exit`.
    """
    (tmp_path / "pyproject.toml").write_text(_CHILD_PYPROJECT)
    (tmp_path / "test_blocking.py").write_text(_CHILD_TESTS)
    dumps = tmp_path / "dumps"
    dumps.mkdir()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
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
            # Hand the child a directory we own, so the dump outlives the
            # os._exit that stops it cleaning up after itself.
            "THEFORGE_TIMEOUT_DUMP_DIR": str(dumps),
        },
        capture_output=True,
        text=True,
        # Bounded per CONVENTIONS.md; well under the five-second convention this
        # test is written to, let alone the shared gate bound.
        timeout=4,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output

    recovered = collect_dumps(dumps)
    assert [nodeid for nodeid, _ in recovered] == ["test_blocking.py::test_blocks_forever"], (
        f"{recovered!r}\n{output}"
    )
    body = recovered[0][1]
    # Located at the wait it was stuck on, not merely named.
    assert "test_blocks_forever" in body, body
    assert "threading.py" in body, body
    assert "0.25s per-test bound" in body, body
    # The test that completed contributes nothing to misattribute.
    assert "test_finishes_promptly" not in body, body


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


# ---------------------------------------------------------------------------
# One bound, shared by every test, with no way to be granted a larger one
# ---------------------------------------------------------------------------


class _FakeItem:
    """Enough of a collected item for the collection hook to act on it."""

    def __init__(self, name: str, *marks: pytest.Mark) -> None:
        self.name = name
        self.originalname = name
        self.path = Path(__file__)
        self.nodeid = f"test_module.py::{name}"
        self.own_markers = list(marks)
        self.stash = pytest.Stash()

    def iter_markers(self, name=None):
        for mark in self.own_markers:
            if name is None or mark.name == name:
                yield mark

    def get_closest_marker(self, name, default=None):
        return next(self.iter_markers(name), default)

    def add_marker(self, marker, append=False):
        mark = marker.mark if hasattr(marker, "mark") else marker
        self.own_markers.append(mark) if append else self.own_markers.insert(0, mark)


class _FakeConfig:
    """A config carrying the shared bound and nothing else."""

    def getoption(self, name, default=None):
        return default

    def getini(self, name):
        if name == "timeout":
            return SHARED_BOUND
        raise KeyError(name)


def _effective_bound(item: _FakeItem) -> float | None:
    """The bound pytest-timeout would read off *item* after collection."""
    mark = item.get_closest_marker("timeout")
    if mark is None:
        return None
    raw = mark.args[0] if mark.args else mark.kwargs.get("timeout")
    return None if raw is None else float(raw)


def _collect(*items: _FakeItem) -> None:
    te.pytest_collection_modifyitems(_FakeConfig(), list(items))


def test_collection_attaches_no_bound_to_anything():
    """The hook grants nothing. Every test inherits the ini bound as-is.

    This is the property the story is about. The previous design classified
    tests and attached a raised bound to a category of them; under the gate's
    own parallelism that classification was answering the wrong question, since
    what a tight bound measured was load rather than the test (#2833).
    """
    items = [_FakeItem("test_one"), _FakeItem("test_two")]
    _collect(*items)
    assert [_effective_bound(item) for item in items] == [None, None]


def test_a_deliberate_shortening_survives_collection():
    """A test that asked for less keeps it; nothing widens it back out."""
    item = _FakeItem("test_short", _mark(1))
    _collect(item)
    assert _effective_bound(item) == 1.0
    assert item.stash.get(te._VIOLATION_KEY, None) is None


def test_a_raised_mark_is_rejected_at_collection_for_every_test():
    item = _FakeItem("test_greedy", _mark(SHARED_BOUND + 1))
    _collect(item)
    assert "exceeds the shared" in item.stash[te._VIOLATION_KEY]


def test_the_rejection_names_no_category_to_reach_for():
    """The message must not point at a mechanism that no longer exists.

    A rejection that names an escape hatch is how the category came back last
    time; there is nothing to name now, and the message has to say so.
    """
    reason = validate_timeout_mark(_mark(SHARED_BOUND + 1), SHARED_BOUND)
    assert reason is not None
    assert "no category" in reason
    for gone in ("orchestration", "ORCHESTRATION_BOUND_TESTS"):
        assert gone not in reason


def test_no_mechanism_survives_in_the_enforcement_module():
    """The category, its marker, and its ceiling are removed, not widened."""
    source = (REPO_ROOT / "tests" / "timeout_enforcement.py").read_text()
    for gone in (
        "ORCHESTRATION_MARKER",
        "ORCHESTRATION_MAX_SECONDS",
        "ORCHESTRATION_BOUND_SECONDS",
        "ORCHESTRATION_BOUND_TESTS",
        "raised_bound_allowed",
        "orchestration_scope",
        "add_marker(",
    ):
        assert gone not in source, gone
    for gone in ("ORCHESTRATION_MARKER", "ORCHESTRATION_MAX_SECONDS", "raised_bound_allowed"):
        assert not hasattr(te, gone), gone


def test_the_module_that_derived_category_membership_is_gone():
    """Source-derived membership is not a thing this suite has any more."""
    assert not (REPO_ROOT / "tests" / "orchestration_scope.py").exists()


def test_no_marker_grants_a_bound_and_none_is_registered():
    """Nothing left to mark a test with, and nothing left to mark."""
    markers = _pyproject()["tool"]["pytest"]["ini_options"]["markers"]
    assert not [entry for entry in markers if entry.split(":")[0] == "orchestration"]

    marked = [
        path.relative_to(REPO_ROOT)
        for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
        if path.resolve() != Path(__file__).resolve()
        and "pytest.mark.orchestration" in path.read_text()
    ]
    assert not marked, f"the removed marker is still applied in: {marked}"


def test_no_test_in_the_suite_carries_a_bound_above_the_shared_one():
    """A mechanical guard on the acceptance criterion itself.

    Collection rejects a raised mark, but it rejects it by failing that test
    during a run. This reads the sources instead, so a widened bound is a named
    failure here rather than a surprise in whichever story next touches it.
    """
    over: list[str] = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("@pytest.mark.timeout("):
                continue
            raw = stripped[len("@pytest.mark.timeout(") :].rstrip(")").removeprefix("timeout=")
            try:
                value = float(raw)
            except ValueError:
                continue
            if value > SHARED_BOUND:
                over.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not over, f"marks above the shared {SHARED_BOUND:g}s bound: {over}"


def test_conventions_states_the_bound_the_suite_enforces():
    """The documented contract and the configured one are the same number."""
    conventions = (REPO_ROOT / "CONVENTIONS.md").read_text()
    assert "timeout = 60" in conventions
    assert "`timeout = 5`" not in conventions
    # The five-second rule survives, as the convention for an isolated test.
    assert "Every test must complete in under\n  5 seconds run on its own" in conventions
    # And the removed mechanism is not still documented as live.
    assert "@pytest.mark.orchestration`" not in conventions


def test_the_tight_judgement_still_lives_in_the_serial_diagnostic():
    """Widening the concurrent bound must not touch the post-stall pass.

    That pass runs one test at a time, which is the only place a tight
    wall-clock threshold measures the test rather than the machine.
    """
    from theforge.config.types import ValidationConfig

    defaults = ValidationConfig(gate_command="make gate")
    assert defaults.gate_diagnostic_per_test_timeout == 10
    assert defaults.gate_diagnostic_budget == 60
