"""Lifecycle tests for process-group isolation and the orphan reaper.

Per the runner-lifecycle invariant these exercise *real* subprocesses via
``tests/fake_bin/`` fake CLIs, not ``Popen`` mocks — a mock cannot show that a
group kill reaches grandchildren (node/tool leaf) that a bare ``proc.kill()``
would leave alive.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import ClassVar

import pytest

from theforge import process_group
from theforge.config import ModelProfile
from theforge.runners.runner_claude import _run_claude
from theforge.runners.runner_codex import _run_codex
from theforge.runners.sandbox import workspace_effect_sandbox_command

_FAKE_BIN = Path(__file__).parent / "fake_bin"

# Sentinel for "compute the real fingerprint" vs. an explicit (possibly None) one.
_UNSET = object()


def _raise_oserror(*_args: object, **_kwargs: object) -> list[str]:
    raise OSError("simulated /proc read failure")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------------------
# Orphan reaper
# ---------------------------------------------------------------------------


class _SidecarWriter:
    def _write_sidecar(
        self,
        project_root: Path,
        owner_pid: int,
        pgid: int,
        sandbox: str | None = None,
        *,
        fingerprint: str | None = _UNSET,
        origin: str | None = None,
        members: dict[int, str] | None = None,
    ) -> Path:
        """Write a sidecar; by default with the *real* fingerprint of ``pgid``.

        ``fingerprint`` may be overridden to simulate the recycled-pgid case
        (a record whose recorded leader is not the one now holding the id), and
        ``members`` stands in for the survivor snapshot `retain_group_record`
        takes when a teardown leaves descendants behind.
        """
        agents_dir = project_root / ".forge" / "runs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        path = agents_dir / f"{owner_pid}-{pgid}.json"
        record: dict[str, object] = {
            "owner_pid": owner_pid,
            "pgid": pgid,
            "run_id": "run-test",
            "sandbox_dir": sandbox,
            "leader_fingerprint": (
                process_group._leader_fingerprint(pgid) if fingerprint is _UNSET else fingerprint
            ),
        }
        if origin is not None:
            record["origin"] = origin
        if members is not None:
            record["members"] = {str(pid): fp for pid, fp in members.items()}
        path.write_text(json.dumps(record), encoding="utf-8")
        return path


class TestReapOrphanAgents(_SidecarWriter):
    def test_dead_owner_group_is_killed_and_sidecar_unlinked(self, tmp_path: Path) -> None:
        """A sidecar whose owner sprint is dead → its group is killpg-ed and the file removed."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        # Owner pid that is guaranteed dead (never-assigned high pid).
        sidecar = self._write_sidecar(
            tmp_path, owner_pid=999_999, pgid=pgid, sandbox=str(tmp_path)
        )
        try:
            reaped = process_group.reap_orphan_agents(tmp_path)
            assert reaped == 1
            assert _wait_until(lambda: proc.poll() is not None), "group was not killed"
            assert not sidecar.exists(), "sidecar was not unlinked after reaping"
        finally:
            process_group.kill_agent_group(pgid)
            proc.wait(timeout=5)

    def test_live_owner_group_is_left_untouched(self, tmp_path: Path) -> None:
        """A sidecar whose owner sprint is still alive → the group is not touched."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        # Owner is this test process — alive.
        sidecar = self._write_sidecar(tmp_path, owner_pid=os.getpid(), pgid=pgid)
        try:
            reaped = process_group.reap_orphan_agents(tmp_path)
            assert reaped == 0
            assert sidecar.exists(), "live-owner sidecar must not be removed"
            assert proc.poll() is None, "live-owner group must not be killed"
        finally:
            process_group.kill_agent_group(pgid)
            sidecar.unlink(missing_ok=True)
            proc.wait(timeout=5)

    def test_missing_agents_dir_is_noop(self, tmp_path: Path) -> None:
        assert process_group.reap_orphan_agents(tmp_path) == 0

    def test_corrupt_sidecar_is_dropped(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / ".forge" / "runs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        bad = agents_dir / "garbage.json"
        bad.write_text("not json", encoding="utf-8")
        assert process_group.reap_orphan_agents(tmp_path) == 0
        assert not bad.exists()


# ---------------------------------------------------------------------------
# Identity verification before signalling (issue #2115)
# ---------------------------------------------------------------------------


class TestReapVerifiesGroupIdentity(_SidecarWriter):
    """A recorded pgid is a claim about the past, not a durable handle.

    The OS recycles process-group ids, and sidecars persist until some later
    sweep consumes them, so the group holding a recorded id at reap time may be
    an unrelated process. Every one of these asserts that a record which cannot
    be *shown* to still describe its group produces no signal at all.
    """

    def test_recycled_pgid_is_not_signalled(self, tmp_path: Path) -> None:
        """The bug: a live group whose leader is not the recorded one is spared."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        # Same pgid, but the recorded fingerprint is a real one belonging to a
        # different process — the shape a recycled pgid actually produces, not a
        # value the format check alone could reject.
        sidecar = self._write_sidecar(
            tmp_path,
            owner_pid=999_999,
            pgid=pgid,
            fingerprint=process_group._leader_fingerprint(os.getpid()),
        )
        try:
            assert process_group.reap_orphan_agents(tmp_path) == 0
            assert proc.poll() is None, (
                "an unrelated process group holding a recycled pgid was killed"
            )
            assert not sidecar.exists(), "the unverifiable record must be discarded"
        finally:
            process_group.kill_agent_group(pgid)
            proc.wait(timeout=5)

    def test_record_without_a_fingerprint_is_not_signalled(self, tmp_path: Path) -> None:
        """No evidence is not weak evidence — a legacy record kills nothing."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        sidecar = self._write_sidecar(tmp_path, owner_pid=999_999, pgid=pgid, fingerprint=None)
        try:
            assert process_group.reap_orphan_agents(tmp_path) == 0
            assert proc.poll() is None, "a group was killed on an unverifiable record"
            assert not sidecar.exists()
        finally:
            process_group.kill_agent_group(pgid)
            proc.wait(timeout=5)

    def test_record_for_a_vanished_group_is_dropped(self, tmp_path: Path) -> None:
        """Nothing holds the pgid any more: no signal, and the record goes away."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        pgid = proc.pid
        proc.wait(timeout=5)
        sidecar = self._write_sidecar(
            tmp_path, owner_pid=999_999, pgid=pgid, fingerprint="Thu Jan  1 00:00:00 1970"
        )
        assert process_group.reap_orphan_agents(tmp_path) == 0
        assert not sidecar.exists()

    def _leaderless_group(self, tmp_path: Path) -> tuple[int, int]:
        """Spawn a group, let its leader exit with a grandchild still in it.

        Returns ``(pgid, grandchild pid)``. This is both the shape the reaper
        exists for (#2013) and the shape a recycled pgid can take, which is why
        the two cases below must be told apart by evidence, not by the fact that
        the group is non-empty.
        """
        pidfile = tmp_path / "gc.pid"
        script = (
            "import subprocess,sys,pathlib;"
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))"
        )
        leader = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
        pgid = os.getpgid(leader.pid)
        leader.wait(timeout=10)
        assert _wait_until(pidfile.exists, timeout=5.0)
        return pgid, int(pidfile.read_text().strip())

    def test_surviving_descendants_are_still_reaped_after_the_leader_exits(
        self, tmp_path: Path
    ) -> None:
        """The #2013 case must survive the new check.

        The record names a member that is still alive with the start time it had
        when the group was known to be ours, which identifies the group without
        the leader.
        """
        pgid, gc_pid = self._leaderless_group(tmp_path)
        sidecar = self._write_sidecar(
            tmp_path, owner_pid=999_999, pgid=pgid, members=process_group.group_members(pgid)
        )
        try:
            assert process_group.reap_orphan_agents(tmp_path) == 1
            assert _wait_until(lambda: not _pid_alive(gc_pid)), (
                "a grandchild left by an exited leader was not reaped"
            )
            assert not sidecar.exists()
        finally:
            try:
                os.kill(gc_pid, signal.SIGKILL)
            except OSError:
                pass

    def test_recycled_pgid_whose_own_leader_exited_is_not_signalled(self, tmp_path: Path) -> None:
        """The other half of the recycled case: an unrelated *leaderless* group.

        A stale record naming a pgid that now belongs to someone else's group —
        one that has itself lost its leader and kept its descendants — must not
        be killed. "The group is non-empty" says nothing about whose group it is.
        """
        pgid, gc_pid = self._leaderless_group(tmp_path)
        # Recorded members from the registered group: pids that are not in this
        # group at all, which is what a record predating the id's reuse holds.
        sidecar = self._write_sidecar(
            tmp_path,
            owner_pid=999_999,
            pgid=pgid,
            fingerprint=process_group._leader_fingerprint(os.getpid()),
            members={999_998: "proc:1", 999_999: "sysctl:1.000000"},
        )
        try:
            assert process_group.reap_orphan_agents(tmp_path) == 0
            assert _pid_alive(gc_pid), (
                "a leaderless group holding a recycled pgid was killed on a stale record"
            )
            assert not sidecar.exists(), "the unverifiable record must still be discarded"
        finally:
            try:
                os.kill(gc_pid, signal.SIGKILL)
            except OSError:
                pass

    def test_leaderless_group_without_recorded_members_is_not_signalled(
        self, tmp_path: Path
    ) -> None:
        """No snapshot, no evidence: the group is left alone and the record dropped.

        This is the deliberate residual cost of the fix — a group orphaned before
        any teardown could snapshot it leaks rather than being killed on a guess.
        """
        pgid, gc_pid = self._leaderless_group(tmp_path)
        sidecar = self._write_sidecar(tmp_path, owner_pid=999_999, pgid=pgid)
        try:
            assert process_group.reap_orphan_agents(tmp_path) == 0
            assert _pid_alive(gc_pid), "a group was killed with no identity evidence at all"
            assert not sidecar.exists()
        finally:
            try:
                os.kill(gc_pid, signal.SIGKILL)
            except OSError:
                pass

    def test_teardown_that_leaves_survivors_records_them_and_the_reaper_uses_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end over the real seam: partial teardown → snapshot → reap.

        The group kill is refused and only the direct child dies — the sandbox
        case `release_group_record` keeps the sidecar for. The survivors it
        records are what lets the later sweep prove the leaderless group it finds
        is this one.

        The stand-in sandbox denies signalling the grandchild by pid as well as
        by group, which is what "teardown reached only the direct child" means. A
        host that permits the per-pid kill no longer reaches the reaper at all:
        the lease sweep ends the survivor at release (covered separately in
        test_process_teardown_lifecycle.py).
        """
        pidfile = tmp_path / "gc.pid"

        def _kill_only_the_direct_child(pid: int) -> bool:
            try:
                grandchild = int(pidfile.read_text().strip())
            except (OSError, ValueError):
                grandchild = -1
            if pid == grandchild:
                return False
            os.kill(pid, signal.SIGKILL)
            return True

        monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr(process_group, "_killpg_for", lambda _pid: False)
        monkeypatch.setattr(process_group, "_kill_pid", _kill_only_the_direct_child)
        # The kill is refused on purpose here, so every teardown pass waits out the
        # full observation window. That window is not what this test verifies, and
        # at the 2s default it put the test over the five-second per-test convention.
        monkeypatch.setattr(process_group, "KILL_GRACE_SECONDS", 0.3)

        script = (
            "import subprocess,sys,pathlib,time;"
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid));"
            "time.sleep(30)"
        )
        with pytest.raises(subprocess.TimeoutExpired):
            process_group.run_in_process_group(
                [sys.executable, "-c", script], timeout=1.5, capture_output=True, text=True
            )
        assert _wait_until(pidfile.exists, timeout=3.0)
        gc_pid = int(pidfile.read_text().strip())
        sidecars = list((tmp_path / ".forge" / "runs" / "agents").glob("*.json"))
        assert len(sidecars) == 1, "the surviving group must still be registered"
        record = json.loads(sidecars[0].read_text(encoding="utf-8"))
        assert str(gc_pid) in record.get("members", {}), (
            "the survivor was not snapshotted, so no later sweep could identify the group"
        )

        # The owner sprint is gone — the state `forge stop` leaves behind.
        record["owner_pid"] = 999_999
        sidecars[0].write_text(json.dumps(record), encoding="utf-8")
        try:
            assert process_group.reap_orphan_agents(tmp_path) == 1
            assert _wait_until(lambda: not _pid_alive(gc_pid)), (
                "the recorded survivor was not reaped"
            )
        finally:
            try:
                os.kill(gc_pid, signal.SIGKILL)
            except OSError:
                pass

    def test_test_origin_record_is_discarded_unsignalled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator's sweep never acts on a record the suite left behind."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        sidecar = self._write_sidecar(tmp_path, owner_pid=999_999, pgid=pgid, origin="test")
        # The sweeping process is a real forge invocation, not a test run.
        monkeypatch.setattr(process_group, "_running_under_pytest", lambda: False)
        try:
            assert process_group.reap_orphan_agents(tmp_path) == 0
            assert proc.poll() is None, "a test-origin record was acted on as real work"
            assert not sidecar.exists()
        finally:
            process_group.kill_agent_group(pgid)
            proc.wait(timeout=5)


class TestGroupMembers:
    """Membership enumeration is the identity a leaderless group still has."""

    def test_transient_unreadable_result_is_retried_before_reporting_no_members(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = iter(
            [
                ({}, False),
                ({1234: "sysctl:1.000001", 5678: "sysctl:1.000002"}, True),
            ]
        )

        monkeypatch.setattr(
            process_group,
            "group_members_checked",
            lambda _pgid: next(calls),
        )

        assert process_group.group_members(4321) == {
            1234: "sysctl:1.000001",
            5678: "sysctl:1.000002",
        }

    def test_unsupported_platform_skips_retry_delay_when_enumeration_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleeps: list[float] = []

        monkeypatch.setattr(process_group.sys, "platform", "win32")
        monkeypatch.setattr(
            process_group,
            "group_members_checked",
            lambda _pgid: ({}, False),
        )
        monkeypatch.setattr(process_group.time, "sleep", sleeps.append)

        assert process_group.group_members(4321) == {}
        assert sleeps == []

    def test_lists_every_live_process_in_the_group(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "gc.pid"
        script = (
            "import subprocess,sys,pathlib,time;"
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid));"
            "time.sleep(30)"
        )
        leader = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
        pgid = os.getpgid(leader.pid)
        try:
            assert _wait_until(pidfile.exists, timeout=5.0)
            gc_pid = int(pidfile.read_text().strip())
            members = process_group.group_members(pgid)
            assert set(members) >= {leader.pid, gc_pid}, members
            # The leader's entry must agree with the fingerprint recorded at
            # registration, or a record could never match its own group.
            assert members[leader.pid] == process_group._leader_fingerprint(pgid)
        finally:
            process_group.kill_agent_group(pgid)
            leader.wait(timeout=5)

    def test_unsafe_pgid_enumerates_nothing(self) -> None:
        for bogus in (0, 1, -1, None, "4321"):
            assert process_group.group_members(bogus) == {}  # type: ignore[arg-type]

    def test_a_zombie_is_not_a_member(self) -> None:
        """An exited process is gone to every reader, or teardown disagrees itself.

        Both platforms' group enumerations list a process that has exited but not
        been reaped, while ``process_tree`` — the reader every descendant check
        settles on — reports it gone. Left unreconciled, the same corpse counts
        as a survivor in one place and as nothing in another, and a kill that
        worked is recorded as a leak that outlived the run (#2309).
        """
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(proc.pid)
        try:
            assert proc.pid in process_group.group_members(pgid)
            os.kill(proc.pid, signal.SIGKILL)
            # Deliberately not waited: this process is now the group's only
            # member and has exited, which is precisely the state pid 1 sees for
            # every orphan it inherits.
            assert _wait_until(lambda: process_group.group_members(pgid) == {}), (
                "a killed-but-unreaped member still counts as a live group member"
            )
            members, enumerated = process_group.group_members_checked(pgid)
            assert (members, enumerated) == ({}, True)
        finally:
            proc.wait(timeout=5)

    def test_a_leader_that_died_unreaped_still_proves_the_pgid_is_not_recycled(self) -> None:
        """Membership and identity ask different questions of the same corpse.

        A process that has exited is not a *member* — nothing is running. But it
        still occupies its pid, so its start time remains proof the pgid has not
        been recycled, and that proof is what lets a sweep verify and then kill a
        group whose direct child died unreaped while its descendants ran on. An
        earlier attempt at this cleanup excluded zombies from the shared stat
        parser, which took the proof away and would have left those descendants
        running (#2309).
        """
        script = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "time.sleep(30)"
        )
        leader = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(leader.pid)
        try:
            assert _wait_until(lambda: len(process_group.group_members(pgid)) >= 2)
            recorded = process_group._leader_fingerprint(pgid)
            os.kill(leader.pid, signal.SIGKILL)
            assert _wait_until(lambda: leader.pid not in process_group.group_members(pgid)), (
                "the exited leader still counts as a running member"
            )
            assert process_group._leader_fingerprint(pgid) == recorded, (
                "the pgid lost its identity proof, so a sweep would decline the "
                "record and leave the group's live descendants running"
            )
            assert process_group.group_members(pgid), "its descendant is still running"
        finally:
            process_group.kill_agent_group(pgid)
            leader.wait(timeout=5)

    def test_an_unreadable_group_is_never_reported_as_an_empty_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read that failed learned nothing, and must not read as "no members".

        Membership and liveness are answered by one read for exactly this reason.
        An earlier version refined the platform enumeration with a second,
        independently fallible per-process reader while keeping the first read's
        confidence flag, so a live group whose members all failed that second
        read reported itself empty *and* fully enumerated — and release then
        dropped its reaper sidecar without ever signalling the survivors (#2309).
        """
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(proc.pid)
        try:
            assert proc.pid in process_group.group_members(pgid)

            # The descendant reader failing must not change membership at all —
            # it is no longer consulted for it.
            monkeypatch.setattr(process_group.process_tree, "process_info", lambda _pid: None)
            members, enumerated = process_group.group_members_checked(pgid)
            assert proc.pid in members and enumerated is True

            # The authoritative read failing must say so rather than say "empty".
            if sys.platform == "darwin":
                monkeypatch.setattr(process_group, "_sysctl_bytes", lambda _mib: None)
            else:
                monkeypatch.setattr(process_group.os, "listdir", _raise_oserror, raising=False)
            assert process_group.group_members_checked(pgid) == ({}, False)
        finally:
            process_group.kill_agent_group(pgid)
            proc.wait(timeout=5)

    def test_a_live_group_that_cannot_be_read_keeps_its_sidecar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The consequence that matters: release must not drop the only handle.

        Driven through ``release_group_record`` rather than the enumeration alone,
        because the defect was not that the read failed — reads fail — but that a
        failed read reached the release path disguised as a settled group.
        """
        monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(proc.pid)
        try:
            process_group.register_agent_group(pgid, sandbox_dir=str(tmp_path))
            monkeypatch.setattr(process_group, "group_members_checked", lambda _p: ({}, False))
            monkeypatch.setattr(process_group, "kill_agent_group", lambda _p: False)

            teardown = process_group.release_group_record(pgid, group_killed=True)

            assert teardown is not None, "a group that could not be read is not 'no teardown'"
            assert teardown.action == process_group.TEARDOWN_RETAINED_FOR_REAPER
            assert teardown.completed is False
            assert list((tmp_path / ".forge" / "runs" / "agents").glob("*.json")), (
                "the reaper's only handle on the survivors was dropped"
            )
            assert proc.poll() is None
        finally:
            # Signalled directly: ``kill_agent_group`` is stubbed for this test
            # and monkeypatch does not unwind until after this block.
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

    def test_group_has_running_members_retries_a_transient_unreadable_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = iter([({}, False), ({1234: "started"}, True)])

        monkeypatch.setattr(
            process_group,
            "group_members_checked",
            lambda _pgid: next(calls),
        )
        monkeypatch.setattr(process_group, "group_is_alive", lambda _pgid: True)

        assert process_group.group_has_running_members(4242) is True

    def test_group_has_running_members_treats_a_zombie_only_group_as_settled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(process_group, "group_members_checked", lambda _pgid: ({}, True))

        assert process_group.group_has_running_members(4242) is False

    def test_group_has_running_members_raises_when_a_live_group_stays_unreadable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(process_group, "group_members_checked", lambda _pgid: ({}, False))
        monkeypatch.setattr(process_group, "group_is_alive", lambda _pgid: True)

        with pytest.raises(OSError, match="could not be enumerated"):
            process_group.group_has_running_members(4242)

    def test_a_group_holding_only_a_zombie_settles_immediately(self) -> None:
        """The liveness wait must not sit out its grace period for a corpse.

        ``killpg(pgid, 0)`` succeeds while an unreaped member remains, so a
        teardown that worked would otherwise be waited out in full and then
        reported as survivors that outlived it.
        """
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pgid = os.getpgid(proc.pid)
        try:
            os.kill(proc.pid, signal.SIGKILL)
            assert _wait_until(lambda: process_group.group_members(pgid) == {})
            started = time.monotonic()
            assert process_group._await_empty_group(pgid) is True
            assert time.monotonic() - started < process_group.KILL_GRACE_SECONDS / 2
        finally:
            proc.wait(timeout=5)


class TestListOrphanAgents:
    """``forge status`` must be able to see orphans without touching them."""

    def test_lists_dead_owner_records_without_signalling_or_unlinking(
        self, tmp_path: Path
    ) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        agents_dir = tmp_path / ".forge" / "runs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        sidecar = agents_dir / f"999999-{pgid}.json"
        sidecar.write_text(
            json.dumps({"owner_pid": 999_999, "pgid": pgid, "sandbox_dir": str(tmp_path)}),
            encoding="utf-8",
        )
        live = agents_dir / f"{os.getpid()}-4242.json"
        live.write_text(json.dumps({"owner_pid": os.getpid(), "pgid": 4242}), encoding="utf-8")
        try:
            orphans = process_group.list_orphan_agents(tmp_path)
            assert [record["pgid"] for record in orphans] == [pgid]
            assert proc.poll() is None, "a read-only listing killed a process group"
            assert sidecar.exists(), "a read-only listing must not consume records"
            assert live.exists()
        finally:
            process_group.kill_agent_group(pgid)
            proc.wait(timeout=5)

    def test_missing_agents_dir_lists_nothing(self, tmp_path: Path) -> None:
        assert process_group.list_orphan_agents(tmp_path) == []


# ---------------------------------------------------------------------------
# Registry sidecars (register / unregister)
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_unregister_roundtrip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("FORGE_DETACHED_RUN_ID", "run-xyz")
        pgid = 4242
        process_group.register_agent_group(pgid, sandbox_dir=tmp_path)
        sidecar = tmp_path / ".forge" / "runs" / "agents" / f"{os.getpid()}-{pgid}.json"
        assert sidecar.exists()
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["pgid"] == pgid
        assert data["owner_pid"] == os.getpid()
        assert data["run_id"] == "run-xyz"

        process_group.unregister_agent_group(pgid)
        assert not sidecar.exists()

    def test_register_records_the_group_leader_fingerprint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The identity evidence the reaper needs is captured at registration (#2115)."""
        monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        try:
            process_group.register_agent_group(pgid, sandbox_dir=tmp_path)
            sidecar = tmp_path / ".forge" / "runs" / "agents" / f"{os.getpid()}-{pgid}.json"
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            assert data["leader_fingerprint"] == process_group._leader_fingerprint(pgid)
            assert data["leader_fingerprint"], "no start time captured for a live leader"
        finally:
            process_group.kill_agent_group(pgid)
            proc.wait(timeout=5)

    @pytest.mark.skipif(
        not (sys.platform.startswith("linux") or sys.platform == "darwin"),
        reason="kernel start-time interface is Linux/macOS only",
    )
    def test_fingerprinting_spawns_no_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registration must not exec anything to fingerprint a group (#2115).

        It runs for every agent and gate launch, and ``ps`` is both a spawn in a
        hot path and an exec a sandbox profile can deny — which would silently
        leave every record unverifiable.
        """

        def _no_spawn(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("fingerprinting spawned a process")

        monkeypatch.setattr(subprocess, "Popen", _no_spawn)
        assert process_group._leader_fingerprint(os.getpid())
        # Same for membership, which teardown takes on the way out of a run.
        assert process_group.group_members(os.getpgid(os.getpid()))

    def test_register_marks_records_written_by_a_test_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A suite-written record must be distinguishable from real work (#2115)."""
        monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
        process_group.register_agent_group(4242, sandbox_dir=tmp_path)
        sidecar = tmp_path / ".forge" / "runs" / "agents" / f"{os.getpid()}-4242.json"
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["origin"] == "test"

    def test_register_is_noop_without_project_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FORGE_PROJECT_ROOT", raising=False)
        # Must not raise and must not create anything.
        process_group.register_agent_group(1234, sandbox_dir=tmp_path)
        process_group.unregister_agent_group(1234)
        assert not (tmp_path / ".forge").exists()


# ---------------------------------------------------------------------------
# kill_agent_group guard (issue #1793)
# ---------------------------------------------------------------------------


class TestKillAgentGroupGuard:
    """``kill_agent_group`` must never hand a pgid <= 1 (or a non-int) to killpg.

    ``os.killpg(1, sig)`` is ``kill(-1, sig)`` — a broadcast SIGKILL to every
    process the user can signal — and ``os.killpg(0, sig)`` targets the caller's
    own group. A subprocess test double whose ``pid`` is unset coerces through
    ``__index__`` to 1, so ``os.getpgid(pid)`` returns 1 with no error; without
    the guard the watchdog then broadcast-kills the whole session (the Linux CI
    hang this bug was filed for).
    """

    @pytest.mark.parametrize("bad_pgid", [1, 0, -1, "1", None])
    def test_unsafe_pgid_never_reaches_killpg(
        self, bad_pgid: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[object, int]] = []
        monkeypatch.setattr(process_group.os, "killpg", lambda pg, sig: calls.append((pg, sig)))
        process_group.kill_agent_group(bad_pgid)  # type: ignore[arg-type]
        assert calls == [], f"killpg was invoked for unsafe pgid={bad_pgid!r}"

    def test_real_group_is_killed(self) -> None:
        """A genuine spawned group (pgid > 1) is still SIGKILL-ed."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        assert pgid > 1
        try:
            process_group.kill_agent_group(pgid)
            assert _wait_until(lambda: proc.poll() is not None), "real group not killed"
        finally:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# run_in_process_group
# ---------------------------------------------------------------------------


class TestRunInProcessGroup:
    def test_returns_completed_process(self, tmp_path: Path) -> None:
        result = process_group.run_in_process_group(
            [sys.executable, "-c", "print('hello')"],
            capture_output=True,
            text=True,
        )
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_timeout_kills_grandchild(self, tmp_path: Path) -> None:
        """On timeout the whole group dies — a grandchild spawned by the child too."""
        pidfile = tmp_path / "gc.pid"
        script = (
            "import subprocess,sys,time,pathlib;"
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid));"
            "time.sleep(30)"
        )
        with pytest.raises(subprocess.TimeoutExpired):
            process_group.run_in_process_group(
                [sys.executable, "-c", script],
                timeout=1.5,
                capture_output=True,
                text=True,
            )
        assert _wait_until(pidfile.exists, timeout=2.0)
        gc_pid = int(pidfile.read_text().strip())
        assert _wait_until(lambda: not _pid_alive(gc_pid)), (
            "grandchild survived group timeout kill"
        )

    def test_timeout_preserves_partial_stdout_as_text(self, tmp_path: Path) -> None:
        """Output written before the kill survives on the exception, decoded (#2019).

        The raised TimeoutExpired previously carried either nothing or bytes-joined
        garbage in text mode, so a killed agent's already-reported token usage was
        unrecoverable and the run could only be recorded cost-unknown.
        """
        script = "import sys,time;print('partial line');sys.stdout.flush();time.sleep(30)"
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            process_group.run_in_process_group(
                [sys.executable, "-c", script],
                timeout=1.5,
                capture_output=True,
                text=True,
            )
        assert isinstance(excinfo.value.stdout, str)
        assert "partial line" in excinfo.value.stdout

    def test_timeout_drain_is_bounded(self, tmp_path: Path) -> None:
        """A timeout must not become an unbounded wait on a surviving writer (#1959)."""
        script = "import sys,time;print('x');sys.stdout.flush();time.sleep(30)"
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            process_group.run_in_process_group(
                [sys.executable, "-c", script],
                timeout=1.0,
                capture_output=True,
                text=True,
            )
        # timeout + two bounded grace windows, with slack for a loaded CI box.
        assert time.monotonic() - start < 1.0 + 4 * process_group.KILL_GRACE_SECONDS + 5.0


# ---------------------------------------------------------------------------
# Runner-level group kill (real fake-CLI subprocess trees)
# ---------------------------------------------------------------------------


class _RunnerGroupKillBase:
    _FAKE_BIN: ClassVar[Path] = _FAKE_BIN

    def _ensure_exec(self, name: str) -> None:
        script = self._FAKE_BIN / name
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _patch_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module: str,
        mode_var: str,
        mode: str,
        pidfile: Path,
        fake_bin: Path | None = None,
    ) -> None:
        """Put the fake CLI on PATH and select its mode.

        ``fake_bin`` overrides the repo's ``tests/fake_bin`` for the sandbox-wrapped
        tests, which need a copy the applied profile can actually read (see
        ``TestClaudeGroupKillThroughSandbox``).
        """
        fake_bin = self._FAKE_BIN if fake_bin is None else fake_bin
        from theforge.workspace_env import build_workspace_env as _orig

        def _build(workspace_path, base_env=None, *, extra=None):  # type: ignore[no-untyped-def]
            env = _orig(workspace_path, base_env, extra=extra)
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            env[mode_var] = mode
            env["FAKE_GRANDCHILD_PID_FILE"] = str(pidfile)
            return env

        monkeypatch.setattr(f"theforge.runners.{module}.build_workspace_env", _build)


class TestCleanupCannotReachTheNextInvocation:
    """One invocation's teardown must not signal the invocation that followed it.

    The first acceptance criterion of #2832. That issue's occurrences were three
    DEV invocations that each started within a second of a gate failure and died
    ~11s later at ``exit=-9``, with a teardown line naming killed escapees right
    beside the death — a sequence that reads as the previous iteration's cleanup
    reaching into the next invocation.

    Investigation established it was not: the kill came from the invocation's own
    runner (see ``test_silent_stream_close_is_classified_as_never_ran``), and the
    teardown line is that same invocation reporting its own escapees *after* the
    fact. But nothing in the suite actually held the ownership predicate to that
    claim, so a later change to the lease sweep could make the misreading true
    without any test objecting. This does.
    """

    def _leased_sleeper(self, lease_env: dict[str, str]) -> subprocess.Popen:
        """A long-lived process in its own session, carrying *lease_env*.

        Its own session on purpose: that is what an escapee is, and it is the
        only case the lease sweep exists to reach — a process still inside the
        group would be caught by the group kill instead.
        """
        return subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=lease_env,
            start_new_session=True,
        )

    @pytest.mark.skipif(
        sys.platform not in ("darwin",) and not sys.platform.startswith("linux"),
        reason="lease_holders can only read another process's environment on macOS/Linux",
    )
    def test_lease_sweep_for_one_invocation_spares_a_later_one(self, tmp_path: Path) -> None:
        """A's escapee sweep kills A's leftovers and leaves B's process running.

        B is started *after* A's lease is opened, so it sits squarely inside the
        window ``_lease_candidates`` considers — that prune has a lower bound and
        no upper one, and this is exactly the shape that would slip through if
        candidacy were mistaken for ownership. What keeps them apart is the token:
        ``open_process_lease`` mints a fresh one per spawn, so B's environment
        carries B's token and never matches A's needle.
        """
        base = dict(os.environ)
        env_a, lease_a = process_group.open_process_lease(base)
        env_b, lease_b = process_group.open_process_lease(base)
        assert lease_a.token != lease_b.token, "a lease token must be unique per spawn"

        # A's leftover background work — the `make dev-check` the observed run's
        # first iteration backgrounded and returned while it was still running.
        proc_a = self._leased_sleeper(env_a)
        # The next invocation, started immediately afterwards.
        proc_b = self._leased_sleeper(env_b)
        try:
            assert _wait_until(lambda: process_group.lease_holders(lease_a) == [proc_a.pid]), (
                f"A's own escapee was not found by its lease "
                f"(holders={process_group.lease_holders(lease_a)}, want=[{proc_a.pid}])"
            )
            # The claim itself: B is never a candidate for A's cleanup.
            assert proc_b.pid not in process_group.lease_holders(lease_a)

            killed, all_gone = process_group.kill_escapees(lease=lease_a)

            assert killed == (proc_a.pid,)
            assert all_gone is True
            # Reaped rather than probed with signal 0: these sleepers are this
            # test's own children, and an unreaped zombie still answers
            # ``kill(pid, 0)``. The wait is what proves it actually died.
            assert proc_a.wait(timeout=5) == -signal.SIGKILL, "A's escapee survived A's own sweep"
            assert proc_b.poll() is None, (
                "the next invocation was signalled by the previous one's cleanup — "
                "the #2832 misreading made real"
            )
        finally:
            for proc in (proc_a, proc_b):
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except OSError:
                    pass

    def test_sequential_invocations_do_not_share_a_lease(self, tmp_path: Path) -> None:
        """Every spawn gets its own token, so no sweep can be inherited by the next.

        The cheap, platform-independent half of the claim above: the ownership
        predicate is the token, and a token is never reused. Kept separate so the
        invariant still has coverage on a host where reading another process's
        environment is unavailable and the sweep is a documented dead end.
        """
        base = dict(os.environ)
        tokens = set()
        for _ in range(5):
            env, lease = process_group.open_process_lease(base)
            assert env[process_group.LEASE_ENV_VAR] == lease.token
            tokens.add(lease.token)
        assert len(tokens) == 5


class TestClaudeGroupKill(_RunnerGroupKillBase):
    def test_timeout_kills_grandchild(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._ensure_exec("claude")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch, "runner_claude", "FAKE_CLAUDE_MODE", "grandchild_hang", pidfile
        )
        profile = ModelProfile(
            name="dev",
            cli="claude",
            model="claude-sonnet-4-5",
            budget_usd=2.0,
            timeout_seconds=2,
            allowed_tools=("Bash",),
            sandbox_mode="none",
        )
        result = _run_claude(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        assert result.success is False
        assert _wait_until(pidfile.exists, timeout=3.0), "fake claude never spawned the grandchild"
        gc_pid = int(pidfile.read_text().strip())
        assert _wait_until(lambda: not _pid_alive(gc_pid)), (
            "grandchild survived the runner's group kill — bare proc.kill() regression"
        )

    def test_exception_after_spawn_kills_group(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A SystemExit unwinding mid-stream must kill the group, not just drop the sidecar.

        Simulates detach.py's SIGTERM handler raising SystemExit while _run_claude
        is blocked reading the agent's stdout (forge stop's graceful path). Before
        the fix, the sole ``except FileNotFoundError`` let SystemExit fall straight
        into the ``finally`` that unregistered the sidecar without ever killing the
        group — the exact leak this bug was filed to close.
        """
        self._ensure_exec("claude")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch, "runner_claude", "FAKE_CLAUDE_MODE", "grandchild_stream", pidfile
        )

        # Raise SystemExit the first time the runner processes a stream line —
        # i.e. while it is inside the stdout loop with the agent tree alive.
        def _boom(*_args: object, **_kwargs: object) -> None:
            raise SystemExit(0)

        monkeypatch.setattr("theforge.runners.runner_claude._process_stream_event", _boom)

        profile = ModelProfile(
            name="dev",
            cli="claude",
            model="claude-sonnet-4-5",
            budget_usd=2.0,
            timeout_seconds=30,
            allowed_tools=("Bash",),
            sandbox_mode="none",
        )
        with pytest.raises(SystemExit):
            _run_claude(
                prompt="do the thing",
                profile=profile,
                working_dir=tmp_path,
                fallback_to_file=False,
            )
        assert _wait_until(pidfile.exists, timeout=3.0), "fake claude never spawned the grandchild"
        gc_pid = int(pidfile.read_text().strip())
        assert _wait_until(lambda: not _pid_alive(gc_pid)), (
            "grandchild survived a SystemExit unwind — the except-BaseException kill regressed"
        )


def _sandbox_reachability_skip_reason(fake_bin: Path, root: Path) -> str | None:
    """Why this host cannot exercise the wrapped path, or None when it can.

    Runs the fake CLI itself through the *same* wrapper call the runner makes,
    in an unknown mode so it exits immediately with a known marker. Nothing
    weaker would do: the question is not "does a profile compile" but "can the
    wrapped process actually start here", and that covers every way the wrapped
    path can be untestable — no ``sandbox-exec``/``bwrap`` at all (the wrapper
    hands the command back unchanged), a host that refuses to apply a profile
    from inside an existing sandbox, a backend whose mounts do not reach *root*
    (bwrap tmpfs-es ``/tmp``, which is where pytest's tmp_path lives on Linux),
    and an interpreter the profile's toolchain roots do not reach. Each is a
    property of the harness rather than of the code under test, so each skips.
    """
    probe = ["claude"]
    try:
        wrapped = workspace_effect_sandbox_command(probe, root, allow_credential_services=True)
    except Exception as exc:  # noqa: BLE001 - any wrapper refusal means "cannot test here"
        return f"sandbox wrapper unavailable ({exc})"
    if wrapped[0] == probe[0]:
        return "platform sandbox (sandbox-exec/bwrap) unavailable on this host"
    env = dict(os.environ)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["FAKE_CLAUDE_MODE"] = "__sandbox_probe__"
    try:
        result = subprocess.run(
            wrapped, capture_output=True, text=True, timeout=60, check=False, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"platform sandbox could not run a probe ({exc})"
    if "unknown FAKE_CLAUDE_MODE" not in result.stderr:
        return (
            "platform sandbox cannot start the fake CLI on this host "
            f"(exit {result.returncode}: {result.stderr.strip()[:200]})"
        )
    return None


class TestClaudeGroupKillThroughSandbox(_RunnerGroupKillBase):
    """The same two kills, through the wrapper a real dev invocation goes through.

    Since #1907 every Claude invocation whose profile does not say
    ``sandbox_mode: none`` runs as ``sandbox-exec``/``bwrap`` → ``claude`` →
    grandchild, so the wrapper owns the intermediate process and the group. The
    unwrapped tests above opt out of it deliberately (the wrapper is noise for
    what they assert); these leave the profile at its default so the shape under
    test is the shape production uses.
    """

    @pytest.fixture
    def wrapped_bin(self, tmp_path: Path) -> Path:
        """A copy of the fake claude CLI inside the sandboxed root, or skip.

        The applied profile grants reads on the worktree and the toolchain —
        ``tests/fake_bin`` is neither, so the repo copy is unreadable the moment
        the wrapper takes effect. Copying it under ``tmp_path`` (which *is* the
        allowed root) keeps the fake CLI reachable without widening the profile.
        """
        self._ensure_exec("claude")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        dest = bin_dir / "claude"
        shutil.copy2(self._FAKE_BIN / "claude", dest)
        dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        reason = _sandbox_reachability_skip_reason(bin_dir, tmp_path)
        if reason is not None:
            pytest.skip(reason)
        return bin_dir

    def _profile(self, timeout_seconds: int) -> ModelProfile:
        # No sandbox_mode argument: the default ("workspace-write") is what makes
        # _run_claude wrap the CLI. Setting it to "none" here would silently turn
        # this back into a copy of the unwrapped test.
        return ModelProfile(
            name="dev",
            cli="claude",
            model="claude-sonnet-4-5",
            budget_usd=2.0,
            timeout_seconds=timeout_seconds,
            allowed_tools=("Bash",),
        )

    def test_timeout_kills_grandchild_through_sandbox_wrapper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrapped_bin: Path
    ) -> None:
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch,
            "runner_claude",
            "FAKE_CLAUDE_MODE",
            "grandchild_hang",
            pidfile,
            fake_bin=wrapped_bin,
        )
        result = _run_claude(
            prompt="do the thing",
            # The test waits this out, so at 5s it could not fit the
            # five-second per-test convention however fast everything else ran. What
            # it proves is that the group kill crosses the sandbox wrapper, not
            # how long the agent was given: the unwrapped sibling above times
            # out at 2s and finishes in 2.0s. Three leaves that margin again for
            # sandbox-exec's own startup before the fake writes its pidfile.
            profile=self._profile(timeout_seconds=3),
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        assert result.success is False
        assert "SANDBOX_UNAVAILABLE" not in (result.output or ""), (
            "the runner refused to wrap — this test would prove nothing about the "
            f"wrapped path: {(result.output or '')[:300]}"
        )
        assert _wait_until(pidfile.exists, timeout=6.0), "fake claude never spawned the grandchild"
        gc_pid = int(pidfile.read_text().strip())
        assert _wait_until(lambda: not _pid_alive(gc_pid)), (
            "grandchild survived the timeout — group kill did not cross the sandbox wrapper"
        )

    def test_exception_after_spawn_kills_group_through_sandbox_wrapper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrapped_bin: Path
    ) -> None:
        """The SystemExit unwind (forge stop's graceful path), wrapper included.

        With the wrapper in place the process the runner holds is the sandbox, not
        the CLI, so an unwind that only killed its direct child would leave both
        the agent and its grandchild running.
        """
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch,
            "runner_claude",
            "FAKE_CLAUDE_MODE",
            "grandchild_stream",
            pidfile,
            fake_bin=wrapped_bin,
        )

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise SystemExit(0)

        monkeypatch.setattr("theforge.runners.runner_claude._process_stream_event", _boom)

        with pytest.raises(SystemExit):
            _run_claude(
                prompt="do the thing",
                profile=self._profile(timeout_seconds=30),
                working_dir=tmp_path,
                fallback_to_file=False,
            )
        assert _wait_until(pidfile.exists, timeout=6.0), "fake claude never spawned the grandchild"
        gc_pid = int(pidfile.read_text().strip())
        assert _wait_until(lambda: not _pid_alive(gc_pid)), (
            "grandchild survived a SystemExit unwind — the group kill did not cross "
            "the sandbox wrapper"
        )


class TestCodexGroupKill(_RunnerGroupKillBase):
    def test_timeout_kills_grandchild(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._ensure_exec("npx")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(monkeypatch, "runner_codex", "FAKE_CODEX_MODE", "grandchild_hang", pidfile)
        profile = ModelProfile(
            name="dev",
            cli="codex",
            model="gpt-5-codex",
            budget_usd=2.0,
            timeout_seconds=2,
            allowed_tools=("Bash",),
            sandbox_mode="none",
        )
        result = _run_codex(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
        )
        # Timeout is surfaced as a failure (npm exec never returned output).
        assert result.success is False
        assert _wait_until(pidfile.exists, timeout=3.0), "fake codex never spawned the grandchild"
        gc_pid = int(pidfile.read_text().strip())
        assert _wait_until(lambda: not _pid_alive(gc_pid)), (
            "grandchild survived past `npm exec` — group kill did not reach the leaf"
        )
