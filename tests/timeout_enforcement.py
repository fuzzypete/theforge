"""Enforcement support for CONVENTIONS.md's five-second per-test bound.

``pyproject.toml`` declares ``timeout = 5`` / ``timeout_method = "thread"``, so
pytest-timeout kills any test that runs past the bound. This module supplies the
two things that configuration alone cannot:

1. **Attribution under xdist.** The thread method ends a timed-out test with
   ``os._exit(1)``. In an xdist worker that kills the process before
   pytest-timeout's stack dump — written through the worker's terminal writer —
   reaches the controller, so the controller sees only "worker gw3 crashed" with
   no stack. Each process therefore wraps ``pytest_timeout.timeout_timer``: on
   the way to ``os._exit(1)`` the wrapper writes the culprit's nodeid and the
   same stack dump to a file in a controller-created directory, and the
   controller re-emits it during terminal summary.

   The wrapper deliberately runs on pytest-timeout's *existing* timer thread
   rather than arming a timer of its own. An earlier version armed
   ``faulthandler.dump_traceback_later`` around every test; that adds a thread
   start and join to each of ~10,000 tests in a suite full of forking
   process-group tests, which is the lock-inheritance hazard CONVENTIONS.md
   already warns about. This version adds no threads and does no per-test work,
   so a run that never times out pays nothing and nothing new can deadlock.
   Only the process that actually timed out ever writes a file, so a dump is
   never attributed to the wrong test.

2. **The bound each test gets.** One rule decides it, at collection:

   * A test that drives a real sprint — established from its own source by
     ``tests/orchestration_scope.py``, or declared with
     ``@pytest.mark.orchestration`` for the machinery that module cannot see —
     is bound at ``ORCHESTRATION_MAX_SECONDS``. The mark is attached here, so
     eligibility is never something a test has to be granted by hand.
   * Every other test is bound at the shared five seconds.

   A ``timeout`` mark of the test's own may only *shorten* whichever of those
   applies. Disabling it (``0`` or negative) or changing how it is enforced
   (``method=``, ``func_only=True``) would put the test back outside the budget
   the gate depends on, so those are rejected outright.

   There is deliberately no list of the tests that get the raised bound. The
   previous design kept one, and a list can only be complete up to the last
   red gate: every test it was missing was discovered by failing a release cut
   under contention it should have been allowed to absorb. Membership now
   follows from what the test does, so a new sprint-driving test is covered the
   moment it is written.

   The category raises the bound; it does not remove one. An orchestration test
   stays in the default gate and still fails with its name and a stack trace
   when it exceeds ``ORCHESTRATION_MAX_SECONDS``.

The signal method is not an option here: measured against this suite's
``-n auto --dist worksteal`` configuration it parks every worker at 0% CPU and
the run never finishes.

Loaded as a plugin (``-p timeout_enforcement``) or via re-exported hooks in
``tests/conftest.py``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from orchestration_scope import drives_a_sprint

#: Environment variable the controller uses to publish the dump directory to
#: xdist workers, which inherit the environment when execnet spawns them.
DUMP_DIR_ENV = "THEFORGE_TIMEOUT_DUMP_DIR"

#: First line of every dump file, naming the test the stack belongs to.
HEADER_PREFIX = "THEFORGE-TIMEOUT-DUMP nodeid="

_SECTION_TITLE = "per-test timeout stack dumps"

#: Marker naming the one category allowed a bound above the shared five seconds:
#: a test that orchestrates real processes, real repositories, or a full sprint
#: workflow, where the cost is the production machinery it must drive rather
#: than the test's own design.
#:
#: The marker is an *opt-in of last resort*. Membership in the category is
#: normally derived from the test's source (``orchestration_scope``), and a test
#: whose sprint call that module can already see must not also carry the marker
#: — otherwise the marker set slowly becomes the hand-maintained list this
#: design exists to remove. It is for equivalent machinery reached somewhere the
#: source of the test module does not show: a cross-module fixture, or a driver
#: that is not the sprint entrypoint.
ORCHESTRATION_MARKER = "orchestration"

#: The ceiling the category itself may not exceed. Generous against the
#: contention these tests actually see — a 1.06s test was measured reaching 5s
#: under a loaded gate, a 4.7x inflation — and still far below the point where a
#: genuinely hung test costs the run, which is the failure the bound exists to
#: catch. The category raises the bound; it does not remove one.
ORCHESTRATION_MAX_SECONDS = 30.0

#: The bound an orchestration test is given, attached at collection. One value
#: for the whole category, because per-test tuning is the hand-maintained list
#: under another name: a number chosen from one machine's measurement of one
#: test is exactly as reactive as a list of the tests that were seen failing.
#: The category's job is to absorb contention, and the ceiling already states
#: how much of it the repository is willing to absorb.
ORCHESTRATION_BOUND_SECONDS = ORCHESTRATION_MAX_SECONDS

#: Nodeids seen carrying the marker in this collection, and those classified
#: from their source. Populated by the collection hook so the marker set can be
#: checked against reality without paying for a second collection.
collected_orchestration_nodeids: set[str] = set()
collected_sprint_driving_nodeids: set[str] = set()

#: Set on any item whose timeout mark was rejected at collection time.
_VIOLATION_KEY: pytest.StashKey[str] = pytest.StashKey()


class _DumpState:
    """Per-process state for the timeout stack-dump channel."""

    def __init__(self) -> None:
        self.dir: Path | None = None
        self.owns_dir = False
        self.worker_id = "controller"


_state = _DumpState()


# ---------------------------------------------------------------------------
# Override validation
# ---------------------------------------------------------------------------


def _as_float(value: object) -> float | None:
    """Coerce *value* to float, or return None if it is not a plain number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def raised_bound_allowed(item: pytest.Item) -> bool:
    """True if *item* belongs to the orchestration category.

    Either the item's own source shows it driving a real sprint, or it carries
    the marker for equivalent machinery that source cannot show. Nothing else
    grants it, and in particular nothing about the item's *name* does: there is
    no list of nodeids to be on.
    """
    return item.get_closest_marker(ORCHESTRATION_MARKER) is not None or drives_a_sprint(item)


def validate_timeout_mark(
    mark: pytest.Mark, limit: float, *, ceiling: float | None = None
) -> str | None:
    """Return a rejection reason for *mark*, or None if it is allowed.

    A ``timeout`` mark may only shorten the shared bound, except for a test in
    the orchestration category, for which *ceiling* is raised. Anything that
    disables the bound or changes the enforcement mechanism is rejected either
    way.
    """
    ceiling = limit if ceiling is None else ceiling
    args = tuple(mark.args)
    kwargs = dict(mark.kwargs)

    if "method" in kwargs or "timeout_method" in kwargs or len(args) > 1:
        return (
            "a timeout mark may not change the enforcement method; "
            f"{limit:g}s is enforced with pytest-timeout's thread method for all tests"
        )
    if kwargs.get("func_only") is True:
        return (
            "a timeout mark may not set func_only=True; the bound has to cover "
            "setup and teardown as well as the test body"
        )

    raw = args[0] if args else kwargs.get("timeout")
    if raw is None:
        return None

    value = _as_float(raw)
    if value is None:
        return f"timeout mark value {raw!r} is not a number"
    if value <= 0:
        return f"timeout mark value {value:g} disables the per-test bound"
    if value > ceiling > limit:
        return (
            f"timeout mark value {value:g}s exceeds the {ceiling:g}s ceiling for the "
            f"{ORCHESTRATION_MARKER!r} category. The category raises the bound; it "
            "does not remove one."
        )
    if value > ceiling:
        return (
            f"timeout mark value {value:g}s exceeds the shared {limit:g}s bound; a mark "
            f"may only shorten it. A test that drives a real sprint is given the "
            f"{ORCHESTRATION_BOUND_SECONDS:g}s orchestration bound automatically, from its "
            f"own source — it does not ask for one. If this test drives equivalent real "
            f"processes, repositories or sprint machinery through something that source "
            f"cannot show, declare it with @pytest.mark.{ORCHESTRATION_MARKER} instead."
        )
    return None


def validate_item_timeout_marks(item: pytest.Item, limit: float) -> str | None:
    """Return the first rejection reason among *item*'s timeout marks."""
    ceiling = ORCHESTRATION_MAX_SECONDS if raised_bound_allowed(item) else limit
    for mark in item.iter_markers("timeout"):
        reason = validate_timeout_mark(mark, limit, ceiling=ceiling)
        if reason is not None:
            return reason
    return None


def configured_timeout(config: pytest.Config) -> float | None:
    """Return the shared per-test bound this run is configured with."""
    candidates = []
    try:
        candidates.append(config.getoption("timeout", None))
    except ValueError:
        pass
    try:
        candidates.append(config.getini("timeout"))
    except (KeyError, ValueError):
        pass
    for raw in candidates:
        value = _as_float(raw)
        if value is not None and value > 0:
            return value
    return None


# ---------------------------------------------------------------------------
# Stack-dump channel
# ---------------------------------------------------------------------------


class _FileTerminal:
    """Minimal terminal-writer stand-in so pytest-timeout's own ``dump_stacks``
    can render into a file, keeping the recovered dump identical in shape to
    the one it would have printed."""

    def __init__(self, fh) -> None:
        self._fh = fh

    def sep(self, ch: str, title: str = "", **kwargs) -> None:
        self._fh.write(f"{ch * 8} {title} {ch * 8}\n" if title else f"{ch * 24}\n")

    def write(self, text: str = "", **kwargs) -> None:
        self._fh.write(str(text))

    def line(self, text: str = "", **kwargs) -> None:
        self._fh.write(f"{text}\n")

    def flush(self) -> None:
        self._fh.flush()


def _write_dump(item, settings) -> None:
    """Persist the timing-out test's nodeid and thread stacks.

    Runs on pytest-timeout's timer thread, microseconds before it calls
    ``os._exit(1)``, so this is the last chance to record anything at all.
    """
    import pytest_timeout

    dump_dir = _state.dir
    if dump_dir is None:
        return
    # The pid keeps a restarted worker from clobbering the dump of the worker
    # it replaced — xdist reuses the same "gw3" id for the replacement.
    path = dump_dir / f"{_state.worker_id}-{os.getpid()}.txt"
    with open(path, "w") as fh:
        fh.write(f"{HEADER_PREFIX}{item.nodeid}\n")
        fh.write(f"Exceeded its {getattr(settings, 'timeout', '?')}s per-test bound.\n")
        pytest_timeout.dump_stacks(_FileTerminal(fh))
        fh.flush()
        os.fsync(fh.fileno())


def _install_dump_hook() -> None:
    """Wrap ``pytest_timeout.timeout_timer`` so the dump survives ``os._exit``.

    ``pytest_timeout_set_timer`` reads ``timeout_timer`` out of the module
    globals when it builds each ``threading.Timer``, so replacing the global
    here is enough — and costs nothing per test.
    """
    try:
        import pytest_timeout
    except ImportError:
        return
    original = pytest_timeout.timeout_timer
    if getattr(original, "_theforge_dump_wrapper", False):
        return

    def timeout_timer(item, settings):
        try:
            _write_dump(item, settings)
        except Exception:
            # Never let dump bookkeeping pre-empt pytest-timeout's own
            # reporting and exit — a lost stack beats a lost timeout.
            pass
        return original(item, settings)

    timeout_timer._theforge_dump_wrapper = True
    pytest_timeout.timeout_timer = timeout_timer


def collect_dumps(dump_dir: Path | None) -> list[tuple[str, str]]:
    """Return ``(nodeid, stack)`` pairs for every surviving dump file."""
    if dump_dir is None or not dump_dir.is_dir():
        return []
    found: list[tuple[str, str]] = []
    for path in sorted(dump_dir.iterdir()):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if not text.startswith(HEADER_PREFIX):
            continue
        header, _, body = text.partition("\n")
        if not body.strip():
            continue
        found.append((header[len(HEADER_PREFIX) :].strip(), body))
    return found


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Point this process at the dump directory; on the controller, create it.

    ``tryfirst`` matters: the controller has to publish ``DUMP_DIR_ENV`` before
    xdist spawns workers, which inherit it through the environment.

    A controller that is *given* a directory uses it and does not delete it.
    That is what makes the dump readable when the timeout kills the controller
    itself: in a serial run the process exits through ``os._exit(1)``, so
    neither terminal summary nor cleanup ever runs, and a self-made temporary
    directory would strand the one artifact worth having.
    """
    worker_input = getattr(config, "workerinput", None)
    if worker_input is None:
        provided = os.environ.get(DUMP_DIR_ENV)
        if provided:
            _state.dir = Path(provided)
            _state.owns_dir = False
        else:
            dump_dir = Path(tempfile.mkdtemp(prefix="theforge-timeout-dumps-"))
            os.environ[DUMP_DIR_ENV] = str(dump_dir)
            _state.dir = dump_dir
            _state.owns_dir = True
        _state.worker_id = "controller"
    else:
        raw = os.environ.get(DUMP_DIR_ENV)
        _state.dir = Path(raw) if raw else None
        _state.worker_id = str(worker_input.get("workerid", "worker"))

    _install_dump_hook()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Give each orchestration test its bound, and reject invalid overrides.

    Validation runs first and over author-declared marks only. The bound this
    hook attaches is the rule's own answer; validating it afterwards would mean
    a run failing tests on a mark the run itself wrote.

    A violation is reported from ``pytest_runtest_setup`` rather than raised
    here so it surfaces as an ordinary named failure — under xdist, collection
    happens in the workers, where raising would crash the worker instead.
    """
    # Recorded during collection so the marker set can be checked against the
    # classified set for free. Spawning a `--collect-only` to find out costs
    # seconds on a 10,000-test suite — the very thing this module exists to catch.
    collected_orchestration_nodeids.clear()
    collected_sprint_driving_nodeids.clear()
    for item in items:
        if item.get_closest_marker(ORCHESTRATION_MARKER) is not None:
            collected_orchestration_nodeids.add(item.nodeid)
        if drives_a_sprint(item):
            collected_sprint_driving_nodeids.add(item.nodeid)

    limit = configured_timeout(config)
    if limit is None:
        return

    raise_bound_for = []
    for item in items:
        reason = validate_item_timeout_marks(item, limit)
        if reason is not None:
            item.stash[_VIOLATION_KEY] = reason
            continue
        # An explicit mark is always a shortening by now, and a deliberate one:
        # leave it alone rather than widening it back out.
        if raised_bound_allowed(item) and not any(item.iter_markers("timeout")):
            raise_bound_for.append(item)

    for item in raise_bound_for:
        item.add_marker(pytest.mark.timeout(ORCHESTRATION_BOUND_SECONDS))


def pytest_runtest_setup(item: pytest.Item) -> None:
    reason = item.stash.get(_VIOLATION_KEY, None)
    if reason is not None:
        pytest.fail(f"invalid per-test timeout override: {reason}", pytrace=False)


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:
    """Re-emit stack dumps left behind by workers that died on a timeout."""
    if getattr(config, "workerinput", None) is not None:
        return
    dumps = collect_dumps(_state.dir)
    if not dumps:
        return
    terminalreporter.write_sep("=", _SECTION_TITLE, red=True)
    for nodeid, body in dumps:
        terminalreporter.write_line(
            f"TIMEOUT: {nodeid} exceeded its per-test bound; stack where it was stuck:"
        )
        for line in body.splitlines():
            terminalreporter.write_line(line)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Drop the dump directory once the terminal reporters have read it."""
    if _state.owns_dir and _state.dir is not None:
        shutil.rmtree(_state.dir, ignore_errors=True)
        os.environ.pop(DUMP_DIR_ENV, None)
    _state.dir = None
    _state.owns_dir = False
