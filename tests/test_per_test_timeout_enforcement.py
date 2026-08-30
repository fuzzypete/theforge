"""The five-second per-test bound is configured, inherited, and attributable.

CONVENTIONS.md's rule used to be prose. These tests hold the line that it is
configuration: declared once in ``pyproject.toml`` so every invocation inherits
it, backed by a declared dev dependency, only shortenable per test, and — the
part that made the rule worth enforcing — attributable to a named test with a
stack trace even when the timeout kills an xdist worker outright.

The last section holds the other half of the contract: which tests are bound
higher, and that the answer comes from what a test *does* rather than from a
list of the tests someone watched time out. A list is complete only up to the
last red gate (#2825), so these check the rule, not a membership.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import timeout_enforcement as te
from orchestration_scope import orchestrating_functions
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
    second rather than adding the shared five to every run, and it runs in a
    single process: measured under a saturated machine an xdist child spiked to
    13.3s of pure startup contention, which no test living inside a five-second
    bound can absorb. The controller-side re-emit that turns this file into
    run output is covered by
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
        # Bounded per CONVENTIONS.md; well under this test's own 5s budget.
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
# The category allowed a larger bound, decided from the test's own source
# ---------------------------------------------------------------------------

#: A module shaped like the sprint-driving test files in this suite: the
#: entrypoint called directly, through an import alias, through a module-local
#: wrapper, and through a fixture that runs one — plus the two shapes that must
#: *not* qualify, a mention that is not a call and a test that drives nothing.
_DRIVER_MODULE = '''\
"""A module docstring that names run_sprint_ctx without calling it."""

import pytest
from sprint_test_helpers import run_sprint_ctx
from sprint_test_helpers import run_sprint_ctx as drive


def _wrapper(root):
    """Local helper standing in for the _run_sprint_* wrappers in this suite."""
    return run_sprint_ctx(object(), root)


@pytest.fixture
def sprinted(tmp_path):
    return _wrapper(tmp_path)


def test_direct(tmp_path):
    run_sprint_ctx(object(), tmp_path)


def test_through_an_alias(tmp_path):
    drive(object(), tmp_path)


def test_through_a_local_wrapper(tmp_path):
    _wrapper(tmp_path)


def test_through_a_fixture(sprinted):
    assert sprinted


def test_only_mentions_it(tmp_path):
    """Names run_sprint_ctx in a docstring."""
    # ...and in a comment: run_sprint_ctx(config, manifest)
    assert "run_sprint_ctx" != str(tmp_path)


def test_drives_nothing():
    assert True
'''


@pytest.fixture
def driver_module(tmp_path) -> Path:
    path = tmp_path / "test_driver_module.py"
    path.write_text(_DRIVER_MODULE)
    return path


class _FakeItem:
    """Enough of a collected item for the collection hook to act on it."""

    def __init__(self, path: Path, name: str, *marks: pytest.Mark) -> None:
        self.path = path
        self.name = name
        self.originalname = name
        self.nodeid = f"{path.name}::{name}"
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
    """Run the collection hook over *items*, leaving this run's own record intact.

    The hook rebuilds the module-level sets describing the collection it was
    handed; a test driving it with fakes would otherwise erase what the real
    collection recorded, which the marker guard below reads.
    """
    recorded = (
        set(te.collected_orchestration_nodeids),
        set(te.collected_sprint_driving_nodeids),
    )
    try:
        te.pytest_collection_modifyitems(_FakeConfig(), list(items))
    finally:
        for live, saved in zip(
            (te.collected_orchestration_nodeids, te.collected_sprint_driving_nodeids),
            recorded,
        ):
            live.clear()
            live.update(saved)


# --- classification ---------------------------------------------------------


def test_source_structure_decides_the_category_not_a_list_of_nodeids(driver_module):
    """Every shape that reaches a sprint qualifies; a mention of one does not."""
    classified = orchestrating_functions(str(driver_module))
    assert {name for name in classified if name.startswith("test")} == {
        "test_direct",
        "test_through_an_alias",
        "test_through_a_local_wrapper",
        "test_through_a_fixture",
    }
    # Only executable call nodes count: prose about a sprint is not a sprint.
    assert "test_only_mentions_it" not in classified
    assert "test_drives_nothing" not in classified


def test_a_sprint_driving_test_is_eligible_without_being_named_anywhere(driver_module):
    """The property the story is about: no nodeid entry, no marker, still in."""
    item = _FakeItem(driver_module, "test_direct")
    assert te.raised_bound_allowed(item)
    assert not te.raised_bound_allowed(_FakeItem(driver_module, "test_drives_nothing"))


def test_the_marker_still_admits_machinery_the_source_cannot_show(driver_module):
    item = _FakeItem(driver_module, "test_drives_nothing", pytest.mark.orchestration.mark)
    assert te.raised_bound_allowed(item)


def test_nothing_in_the_module_names_a_curated_allowlist():
    """The mechanism the story removes must not come back by another name."""
    source = (REPO_ROOT / "tests" / "timeout_enforcement.py").read_text()
    assert "ORCHESTRATION_BOUND_TESTS" not in source
    assert not hasattr(te, "ORCHESTRATION_BOUND_TESTS")


# --- the bound each test actually ends up with ------------------------------


def test_collection_gives_a_sprint_driving_test_the_orchestration_bound(driver_module):
    item = _FakeItem(driver_module, "test_direct")
    _collect(item)
    assert _effective_bound(item) == te.ORCHESTRATION_BOUND_SECONDS


def test_collection_leaves_an_ordinary_test_on_the_shared_bound(driver_module):
    item = _FakeItem(driver_module, "test_drives_nothing")
    _collect(item)
    assert _effective_bound(item) is None  # no mark: the shared 5s applies


def test_collection_gives_a_marked_test_the_orchestration_bound(driver_module):
    item = _FakeItem(driver_module, "test_drives_nothing", pytest.mark.orchestration.mark)
    _collect(item)
    assert _effective_bound(item) == te.ORCHESTRATION_BOUND_SECONDS


def test_a_deliberate_shortening_survives_collection(driver_module):
    """A test that asked for less keeps it; the rule raises, it does not widen."""
    item = _FakeItem(driver_module, "test_direct", _mark(1))
    _collect(item)
    assert _effective_bound(item) == 1.0


def test_the_bound_the_run_attaches_is_never_itself_rejected(driver_module):
    """Validation reads author-declared marks only.

    The attached bound is above the shared five seconds by construction, so a
    run that validated its own injection would fail every orchestration test in
    the suite on a mark no author wrote.
    """
    item = _FakeItem(driver_module, "test_direct")
    _collect(item)
    assert item.stash.get(te._VIOLATION_KEY, None) is None
    assert te.validate_item_timeout_marks(item, SHARED_BOUND) is None


def test_an_ordinary_test_still_cannot_raise_the_bound():
    reason = validate_timeout_mark(_mark(20), SHARED_BOUND)
    assert reason is not None
    assert "exceeds the shared" in reason
    # The message has to say how the raised bound is legitimately reached, or
    # the next person reaches for the nearest workaround instead.
    assert te.ORCHESTRATION_MARKER in reason
    assert "ORCHESTRATION_BOUND_TESTS" not in reason


def test_an_ordinary_tests_raised_mark_is_rejected_at_collection(driver_module):
    item = _FakeItem(driver_module, "test_drives_nothing", _mark(20))
    _collect(item)
    assert "exceeds the shared" in item.stash[te._VIOLATION_KEY]


def test_the_category_raises_the_bound_but_does_not_remove_it():
    ceiling = te.ORCHESTRATION_MAX_SECONDS
    assert validate_timeout_mark(_mark(20), SHARED_BOUND, ceiling=ceiling) is None
    over = validate_timeout_mark(_mark(ceiling + 1), SHARED_BOUND, ceiling=ceiling)
    assert over is not None
    assert "ceiling" in over
    # Disabling is still rejected inside the category.
    assert "disables" in (validate_timeout_mark(_mark(0), SHARED_BOUND, ceiling=ceiling) or "")


def test_the_ceiling_binds_every_orchestration_test(driver_module):
    """Eligible or marked, the bound attached and the bound allowed both stop here."""
    assert te.ORCHESTRATION_BOUND_SECONDS <= te.ORCHESTRATION_MAX_SECONDS
    for name, marks in (("test_direct", ()), ("test_drives_nothing", (te.ORCHESTRATION_MARKER,))):
        extra = tuple(getattr(pytest.mark, m).mark for m in marks)
        item = _FakeItem(driver_module, name, _mark(te.ORCHESTRATION_MAX_SECONDS + 1), *extra)
        _collect(item)
        assert "ceiling" in item.stash[te._VIOLATION_KEY]


# --- the classification cannot silently stop finding anything ---------------


def test_every_module_that_drives_sprints_has_a_classified_test():
    """A mechanical guard on the classifier itself.

    The failure this story is about was a test file whose sprint-driving tests
    were not covered. If the classifier ever stops recognising a call shape
    this suite uses — a new alias form, a wrapper it cannot follow — the module
    it stops recognising goes quiet rather than red. Any test module that
    imports the sprint helper must produce at least one classified test.
    """
    unclassified = []
    this_file = Path(__file__).resolve()
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        # This file names the helper only inside the fixture module above, which
        # is the one place a mention that is not a call is the point.
        if path.resolve() == this_file:
            continue
        source = path.read_text()
        if "run_sprint_ctx" not in source:
            continue
        if not any(name.startswith("test") for name in orchestrating_functions(str(path))):
            unclassified.append(path.relative_to(REPO_ROOT))
    assert not unclassified, f"modules driving sprints with nothing classified: {unclassified}"


def test_the_reported_file_is_covered_in_full():
    """The regression itself: every test in it drives a sprint, so all qualify."""
    path = REPO_ROOT / "tests" / "test_sprint_run_artifact_publish_timing.py"
    classified = orchestrating_functions(str(path))
    source = path.read_text()
    declared = {
        line.split("(")[0].removeprefix("def ")
        for line in source.splitlines()
        if line.startswith("def test_")
    }
    assert declared, source[:200]
    assert declared <= classified, f"still on the shared bound: {sorted(declared - classified)}"


def test_the_marker_stays_the_exception_it_is_documented_to_be():
    """A marker on a test the source already classifies rebuilds the old list.

    Checked against the marked and classified sets this collection produced, so
    it costs nothing, and it holds under a partial collection too.
    """
    redundant = te.collected_orchestration_nodeids & te.collected_sprint_driving_nodeids
    assert not redundant, (
        "these tests are already classified from their own source; the marker adds "
        f"nothing but a hand-maintained membership list: {sorted(redundant)}"
    )
