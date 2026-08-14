"""One observation sees the whole tree, not one generation of it.

``DescendantTracker`` walked a single generation per ``observe()`` call, so a
depth-N tree was only fully recorded after N samples. Two consequences, both
real: a spawn killed before enough samples elapsed took its unrecorded
descendants with it — the containment #2309 exists to provide — and a sample
that saw no new process still reported growth, because reaching one generation
deeper into a tree that was already there looks identical to a new fork.

Real subprocesses, for the reason ``test_process_tree.py`` gives: the claim is
about what the kernel tells us about processes we did not write.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from theforge import process_tree

# A chain three processes long: root -> middle -> leaf. Rooting the tracker at
# the head makes the leaf TWO generations out, which is the whole point — a
# tree only one generation deep is recorded by either implementation and would
# prove nothing.
_LEAF = "import time; time.sleep(30)"
_MIDDLE = (
    "import subprocess,sys,time;"
    f"subprocess.Popen([sys.executable,'-c',{_LEAF!r}],"
    "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
    "time.sleep(30)"
)
_ROOT = (
    "import subprocess,sys,time;"
    f"subprocess.Popen([sys.executable,'-c',{_MIDDLE!r}],"
    "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
    "time.sleep(30)"
)


def _reap(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _wait_until(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _only_child_of(pid: int) -> int | None:
    kids = process_tree.children_of(pid)
    return kids[0] if kids else None


@pytest.fixture
def two_deep_tree():  # type: ignore[no-untyped-def]
    """A live ``root -> middle -> leaf`` chain; yields ``(root_pid, leaf_pid)``."""
    root = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _ROOT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Everything below the Popen is inside the finally, setup included: a chain
    # this fixture leaks is a live two-deep descendant of the pytest worker, and
    # that is precisely the state that breaks the tracker tests running after it.
    middle: int | None = None
    leaf: int | None = None
    try:
        assert _wait_until(lambda: _only_child_of(root.pid) is not None), "middle never appeared"
        middle = _only_child_of(root.pid)
        assert middle is not None
        assert _wait_until(lambda: _only_child_of(middle) is not None), (
            "the leaf must exist before the claim under test is meaningful"
        )
        leaf = _only_child_of(middle)
        assert leaf is not None
        yield root.pid, leaf
    finally:
        for pid in (leaf, middle, root.pid):
            if pid is not None:
                _reap(pid)
        root.wait(timeout=5)


class TestOneSampleReachesEveryGeneration:
    def test_first_observation_records_a_two_generation_descendant(
        self,
        two_deep_tree,  # type: ignore[no-untyped-def]
    ) -> None:
        """Depth must not cost samples: a kill can land before the second one."""
        root_pid, leaf = two_deep_tree
        tracker = process_tree.DescendantTracker(root_pid=root_pid)
        tracker.observe()
        assert leaf in tracker.recorded

    def test_a_quiet_second_sample_reports_no_growth(self, two_deep_tree) -> None:  # type: ignore[no-untyped-def]
        """Growth means a new process appeared, not that the walk got deeper."""
        root_pid, _leaf = two_deep_tree
        seen: list[dict[int, str]] = []
        tracker = process_tree.DescendantTracker(root_pid=root_pid, on_observed=seen.append)
        tracker.observe()
        assert seen, "the first sample found a tree and must report it"
        before = len(seen)
        tracker.observe()
        assert len(seen) == before

    def test_a_process_born_after_the_first_sample_still_reports_growth(self) -> None:
        """The quiet-sample rule must not silence a genuinely new descendant."""
        seen: list[dict[int, str]] = []
        tracker = process_tree.DescendantTracker(root_pid=os.getpid(), on_observed=seen.append)
        tracker.observe()
        before = len(seen)
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _wait_until(lambda: proc.pid in process_tree.children_of(os.getpid()))
            tracker.observe()
            assert len(seen) > before
            assert proc.pid in tracker.recorded
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_a_sibling_subtree_is_never_claimed(self, two_deep_tree) -> None:  # type: ignore[no-untyped-def]
        """Walking deeper must not widen: another spawn's tree is still not ours."""
        _root_pid, _leaf = two_deep_tree
        watched = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            tracker = process_tree.DescendantTracker(root_pid=watched.pid)
            tracker.observe()
            assert tracker.recorded == {}
        finally:
            _reap(watched.pid)
            watched.wait(timeout=5)
