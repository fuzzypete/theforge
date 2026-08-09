"""Descendant observation: the containment a process cannot opt out of (#2309).

Real subprocesses throughout, because the claim is about what the kernel will
tell us about processes we did not write. A mock of ``/proc`` or ``libproc``
would only assert that the parser matches the fake — the part that was never in
doubt. What was in doubt is whether the platform answers at all for a hardened
binary, and only a real one settles that.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from theforge import process_tree


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


def _sleeper(*, own_session: bool = False):  # type: ignore[no-untyped-def]
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=own_session,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class TestIsRealPid:
    """The guard that keeps a non-number out of a set of kill targets (#1793)."""

    def test_accepts_a_plausible_pid(self) -> None:
        assert process_tree.is_real_pid(4321) is True

    @pytest.mark.parametrize("value", [0, 1, -1])
    def test_rejects_ids_that_cannot_denote_a_descendant(self, value: int) -> None:
        # 1 is init/launchd; 0 and -1 address groups, never a spawned child.
        assert process_tree.is_real_pid(value) is False

    @pytest.mark.parametrize("value", ["4321", None, 4321.0, True])
    def test_rejects_anything_that_is_not_an_int(self, value: object) -> None:
        """``True`` is an int in Python and would sail through a bare ``> 1``."""
        assert process_tree.is_real_pid(value) is False

    def test_rejects_a_mock(self) -> None:
        """A test double's unset ``pid`` must never reach a kill (#1793)."""
        from unittest.mock import MagicMock

        assert process_tree.is_real_pid(MagicMock()) is False


class TestParseStat:
    """Linux ``/proc/<pid>/stat`` parsing, including the comm-with-spaces trap."""

    def test_reads_ppid_pgrp_and_starttime(self) -> None:
        fields = ["7", "(python3)", "S", "3", "5"] + [str(n) for n in range(6, 25)]
        # starttime is field 22 — index 19 of the tail that begins at field 3.
        assert process_tree._parse_stat(" ".join(fields)) == (3, 5, "proc:22")

    def test_a_command_name_with_spaces_and_parens_does_not_shift_fields(self) -> None:
        """comm is process-chosen text; splitting the whole line misreads it."""
        fields = ["7", "(od d) (name)", "S", "3", "5"] + [str(n) for n in range(6, 25)]
        assert process_tree._parse_stat(" ".join(fields)) == (3, 5, "proc:22")

    def test_a_truncated_line_yields_nothing_rather_than_a_guess(self) -> None:
        assert process_tree._parse_stat("7 (python3) S 3 5 6") is None

    def test_a_non_numeric_field_yields_nothing(self) -> None:
        fields = ["7", "(python3)", "S", "x", "5"] + [str(n) for n in range(6, 25)]
        assert process_tree._parse_stat(" ".join(fields)) is None


class TestProcessInfo:
    def test_reports_our_own_identity(self) -> None:
        info = process_tree.process_info(os.getpid())
        assert info is not None
        assert (info.pid, info.ppid, info.pgid) == (os.getpid(), os.getppid(), os.getpgrp())
        assert info.fingerprint

    def test_a_child_names_us_as_its_parent(self) -> None:
        proc = _sleeper()
        try:
            info = process_tree.process_info(proc.pid)
            assert info is not None and info.ppid == os.getpid()
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_a_process_in_its_own_session_leads_its_own_group(self) -> None:
        proc = _sleeper(own_session=True)
        try:
            info = process_tree.process_info(proc.pid)
            assert info is not None and info.pgid == proc.pid
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_a_dead_pid_is_unknown_rather_than_fabricated(self) -> None:
        proc = _sleeper()
        pid = proc.pid
        _reap(pid)
        proc.wait(timeout=5)
        assert _wait_until(lambda: process_tree.process_info(pid) is None)

    @pytest.mark.parametrize("value", [0, -5])
    def test_refuses_an_id_that_cannot_denote_a_process(self, value: int) -> None:
        assert process_tree.process_info(value) is None


class TestChildrenAndGroups:
    def test_children_of_lists_a_spawned_child(self) -> None:
        proc = _sleeper()
        try:
            assert _wait_until(lambda: proc.pid in process_tree.children_of(os.getpid()))
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_children_of_a_childless_process_is_empty(self) -> None:
        proc = _sleeper()
        try:
            assert process_tree.children_of(proc.pid) == []
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_members_of_group_lists_a_group_leader(self) -> None:
        proc = _sleeper(own_session=True)
        try:
            assert proc.pid in process_tree.members_of_group(proc.pid)
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_an_unsafe_pgid_enumerates_nothing(self) -> None:
        """``killpg(1, …)`` is a broadcast; nothing may treat 1 as a group (#1793)."""
        assert process_tree.members_of_group(1) == []
        assert process_tree.members_of_group(0) == []

    def test_live_pids_includes_us(self) -> None:
        assert os.getpid() in process_tree.live_pids()


class TestTransitiveWalk:
    """Depth is not a way out: the walk follows child links as far as they go."""

    def test_records_a_grandchild_that_left_the_group(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "gc.pid"
        script = (
            "import subprocess,sys,pathlib,time;"
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "start_new_session=True,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid));"
            "time.sleep(2)"
        )
        child = subprocess.Popen([sys.executable, "-c", script])  # noqa: S603
        tracker = process_tree.DescendantTracker(root_pid=child.pid)
        tracker.start()
        assert _wait_until(pidfile.exists)
        gc_pid = int(pidfile.read_text().strip())
        try:
            assert _wait_until(lambda: gc_pid in tracker.recorded), (
                "setsid changes a process's group and session, never its parent"
            )
            assert child.pid not in tracker.recorded, "the root is watched, not recorded"
        finally:
            tracker.stop()
            _reap(gc_pid)
            _reap(child.pid)
            child.wait(timeout=5)

    def test_claims_a_group_member_it_never_saw_born(self) -> None:
        """A pgid root claims members whose own parent link broke before we looked."""
        proc = _sleeper(own_session=True)
        try:
            tracker = process_tree.DescendantTracker(root_pid=None, pgid=proc.pid)
            tracker.observe()
            assert proc.pid in tracker.recorded
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_a_sibling_is_never_recorded(self) -> None:
        """Watching one spawn must not claim another's work — they are siblings."""
        watched = _sleeper()
        sibling = _sleeper()
        try:
            tracker = process_tree.DescendantTracker(root_pid=watched.pid)
            tracker.observe()
            assert sibling.pid not in tracker.recorded
        finally:
            for proc in (watched, sibling):
                _reap(proc.pid)
                proc.wait(timeout=5)


class TestDescendantTracker:
    def test_records_a_descendant_that_outlives_its_parent(self, tmp_path: Path) -> None:
        """The whole point: what it saw stays known after the link is destroyed."""
        pidfile = tmp_path / "gc.pid"
        script = (
            "import subprocess,sys,pathlib,time;"
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "start_new_session=True,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid));"
            "time.sleep(0.4)"
        )
        child = subprocess.Popen([sys.executable, "-c", script])  # noqa: S603
        tracker = process_tree.DescendantTracker(root_pid=child.pid)
        tracker.start()
        child.wait(timeout=30)
        tracker.stop()

        gc_pid = int(pidfile.read_text().strip())
        try:
            orphaned = process_tree.process_info(gc_pid)
            assert orphaned is not None and orphaned.ppid == 1, (
                "the grandchild should be orphaned by now — otherwise this proves nothing"
            )
            assert gc_pid in tracker.recorded
            assert gc_pid in tracker.survivors()
        finally:
            _reap(gc_pid)

    def test_survivors_drops_a_pid_that_is_gone(self) -> None:
        proc = _sleeper()
        tracker = process_tree.DescendantTracker(root_pid=os.getpid())
        tracker.observe()
        assert proc.pid in tracker.recorded
        _reap(proc.pid)
        proc.wait(timeout=5)
        assert _wait_until(lambda: proc.pid not in tracker.survivors())

    def test_survivors_refuses_a_recycled_pid(self) -> None:
        """A recorded pid is only a target while it is still the process seen.

        The discipline #2115 established, applied to observation: an id alone is
        not identity, so a mismatched start time means hands off.
        """
        proc = _sleeper()
        try:
            tracker = process_tree.DescendantTracker(root_pid=os.getpid())
            tracker.observe()
            assert proc.pid in tracker.survivors()
            tracker._seen[proc.pid] = "t:not-the-one-we-saw"
            assert proc.pid not in tracker.survivors()
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_never_records_the_tracking_process_itself(self) -> None:
        tracker = process_tree.DescendantTracker(root_pid=os.getpid())
        tracker.observe()
        assert os.getpid() not in tracker.recorded
        assert os.getpid() not in tracker.survivors()

    def test_excluded_pids_are_left_alone(self) -> None:
        proc = _sleeper()
        try:
            tracker = process_tree.DescendantTracker(root_pid=os.getpid())
            tracker.observe()
            assert proc.pid not in tracker.survivors(exclude={proc.pid})
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_a_tracker_with_nothing_to_watch_is_inert(self) -> None:
        tracker = process_tree.DescendantTracker()
        assert tracker.active is False
        tracker.start()
        tracker.observe()
        assert tracker.recorded == {}
        assert tracker.survivors() == {}
        tracker.stop()

    def test_a_mock_pid_never_becomes_a_root(self) -> None:
        from unittest.mock import MagicMock

        tracker = process_tree.DescendantTracker(root_pid=MagicMock(), pgid=MagicMock())
        assert tracker.active is False

    def test_stop_takes_a_final_sample(self) -> None:
        """A descendant born after the last tick is still observable at teardown."""
        tracker = process_tree.DescendantTracker(root_pid=os.getpid())
        tracker.start()
        proc = _sleeper()
        try:
            tracker.stop()
            assert proc.pid in tracker.recorded
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_is_a_context_manager(self) -> None:
        with process_tree.DescendantTracker(root_pid=os.getpid()) as tracker:
            assert tracker.active is True
        assert tracker._stop.is_set()


class TestObservationCallback:
    """The hook that makes an observation outlive the process that made it."""

    def test_fires_with_the_full_set_when_it_grows(self) -> None:
        seen: list[dict[int, str]] = []
        proc = _sleeper()
        try:
            tracker = process_tree.DescendantTracker(root_pid=os.getpid(), on_observed=seen.append)
            tracker.observe()
            assert seen, "a new descendant must be reported so it can be persisted"
            assert proc.pid in seen[-1]
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_does_not_fire_when_nothing_new_was_seen(self) -> None:
        """The sidecar write is cheap but not free; a quiet sample writes nothing."""
        seen: list[dict[int, str]] = []
        proc = _sleeper()
        try:
            tracker = process_tree.DescendantTracker(root_pid=os.getpid(), on_observed=seen.append)
            tracker.observe()
            before = len(seen)
            tracker.observe()
            assert len(seen) == before
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)


class TestWaitUntilGone:
    def test_returns_empty_once_everything_has_exited(self) -> None:
        proc = _sleeper()
        info = process_tree.process_info(proc.pid)
        assert info is not None
        _reap(proc.pid)
        proc.wait(timeout=5)
        assert process_tree.wait_until_gone({proc.pid: info.fingerprint}, timeout=5.0) == {}

    def test_returns_what_is_still_running(self) -> None:
        proc = _sleeper()
        try:
            info = process_tree.process_info(proc.pid)
            assert info is not None
            remaining = process_tree.wait_until_gone({proc.pid: info.fingerprint}, timeout=0.2)
            assert remaining == {proc.pid: info.fingerprint}
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_a_recycled_pid_counts_as_gone(self) -> None:
        """We waited for a specific process, not for whoever holds the number."""
        proc = _sleeper()
        try:
            assert process_tree.wait_until_gone({proc.pid: "t:someone-else"}, timeout=0.2) == {}
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)


class TestStartTimeAsAnEpoch:
    """``started_at`` orders a process against a wall clock, so the sweep can prune.

    ``fingerprint`` only ever compares against itself, so it may be any stable
    token. ``started_at`` is compared against a lease's own ``time.time()``, so
    it has to be a real epoch — and the Linux derivation (boot time plus clock
    ticks) is arithmetic worth pinning on any host, since the platform that runs
    it is not the platform this was written on.
    """

    def test_our_own_start_time_is_a_plausible_epoch(self) -> None:
        info = process_tree.process_info(os.getpid())
        assert info is not None and info.started_at is not None
        age = time.time() - info.started_at
        assert 0 <= age < 3600, f"our own process reads as {age:.0f}s old"

    def test_a_child_started_after_us(self) -> None:
        """The ordering the prune depends on: a descendant postdates its ancestor."""
        ours = process_tree.process_info(os.getpid())
        proc = _sleeper()
        try:
            child = process_tree.process_info(proc.pid)
            assert ours is not None and ours.started_at is not None
            assert child is not None and child.started_at is not None
            assert child.started_at >= ours.started_at
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_linux_ticks_are_converted_with_boot_time_and_clock_rate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """proc(5): ``starttime`` is clock ticks since boot, so add boot and divide."""
        monkeypatch.setitem(process_tree._state, "btime", 1_000_000.0)
        monkeypatch.setitem(process_tree._state, "hz", 100.0)
        assert process_tree._linux_started_at_epoch(250) == 1_000_002.5

    def test_an_unknown_boot_time_leaves_the_start_time_unstated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unorderable is the safe answer — the sweep keeps what it cannot date."""
        monkeypatch.setitem(process_tree._state, "btime", None)
        assert process_tree._linux_started_at_epoch(250) is None

    def test_an_unusable_clock_rate_leaves_the_start_time_unstated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(process_tree._state, "btime", 1_000_000.0)
        monkeypatch.setitem(process_tree._state, "hz", 0.0)
        assert process_tree._linux_started_at_epoch(250) is None


class TestAZombieIsGone:
    """An exited process is not a survivor, on either platform.

    macOS refuses to describe one at all; Linux keeps its ``/proc`` entry until
    the parent reaps it. Both must read as gone, or a killed escapee is a clean
    teardown on one platform and a surviving leak on the other — which is exactly
    what forge running as pid 1 in a container would hit, since it inherits its
    own orphans and never reaps them.
    """

    def test_a_linux_zombie_is_not_described(self) -> None:
        fields = ["7", "(python3)", "Z", "3", "5"] + [str(n) for n in range(6, 25)]
        assert process_tree._parse_stat(" ".join(fields)) is None

    def test_a_running_process_still_is(self) -> None:
        fields = ["7", "(python3)", "R", "3", "5"] + [str(n) for n in range(6, 25)]
        assert process_tree._parse_stat(" ".join(fields)) == (3, 5, "proc:22")

    def test_an_unreaped_child_reads_as_gone(self) -> None:
        """The real thing: a killed child we have not waited for."""
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(0.05)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _wait_until(lambda: process_tree.process_info(proc.pid) is None), (
                "an exited child still reads as a live process, so a completed "
                "teardown would be recorded as a leak that survived"
            )
        finally:
            proc.wait(timeout=5)
