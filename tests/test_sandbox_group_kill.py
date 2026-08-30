"""Group-kill must survive the platform sandbox wrapper.

Every forge timeout and teardown path kills a *process group* (``killpg``), and
the agent that issues it runs wrapped in the host sandbox. If the sandbox refuses
that signal the kill no-ops silently: no exception, no log, just a subprocess
that runs to its natural end instead of to its timeout. That is invisible in a
transcript and shows up only as wall clock — a 30s suite costing 605s at idle CPU
(#1959). These tests pin the grant that makes the kill land, and — where the host
can actually apply a profile — prove it end to end with real processes.
"""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.runners.sandbox import workspace_effect_sandbox_command

# Spawns a child in its own session (so it leads its own process group — exactly
# how the runners spawn agent CLIs), killpg's that group, and reports which of
# the two signal scopes the sandbox permitted.
_PROBE = """
import os, signal, subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
)
time.sleep(0.3)
try:
    os.killpg(os.getpgid(child.pid), signal.SIGKILL)
    print("KILLPG_OK")
except OSError as exc:
    print("KILLPG_FAIL", exc)
    try:
        os.kill(child.pid, signal.SIGKILL)
    except OSError:
        pass
try:
    child.wait(timeout=5)
except subprocess.TimeoutExpired:
    print("CHILD_SURVIVED")
"""


def _macos_profile_for(root: Path) -> str:
    """The seatbelt profile text the wrapper would apply for *root*."""
    with (
        patch("theforge.runners.sandbox._SYSTEM", "Darwin"),
        patch("theforge.runners.sandbox._sandbox_available", return_value=True),
    ):
        wrapped = workspace_effect_sandbox_command(["true"], root)
    assert wrapped[:2] == ["sandbox-exec", "-p"]
    return wrapped[2]


def _run_sandboxed(cmd: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    """Run *cmd* under the real host sandbox, skipping when that is not possible.

    Skips rather than fails on a host that cannot apply a profile at all —
    notably when this suite is itself running inside a sandbox, where nested
    ``sandbox_apply`` is refused. That is a property of the harness, not of the
    code under test.
    """
    if platform.system() != "Darwin":
        pytest.skip("seatbelt signal scope is macOS-specific")
    if shutil.which("sandbox-exec") is None:
        pytest.skip("sandbox-exec not available on this host")
    wrapped = workspace_effect_sandbox_command(cmd, root)
    if wrapped[0] != "sandbox-exec":
        pytest.skip("host sandbox unavailable; wrapper returned the command unchanged")
    result = subprocess.run(wrapped, capture_output=True, text=True, timeout=120, check=False)
    if "sandbox_apply" in result.stderr:
        pytest.skip("cannot apply a sandbox profile from inside an existing sandbox")
    return result


class TestSignalScopeGrant:
    """Profile-text assertions — these run on every host, including Linux CI."""

    def test_profile_grants_signalling_descendants(self, tmp_path: Path) -> None:
        """``(target self)`` alone denies killpg on a child's group — the #1959 cause."""
        profile = _macos_profile_for(tmp_path)
        assert "(allow signal (target children))" in profile
        assert "(allow signal (target same-sandbox))" in profile

    def test_profile_does_not_grant_signalling_arbitrary_processes(self, tmp_path: Path) -> None:
        """Widening stays inward: an agent still cannot signal the coordinator.

        ``(target others)`` would let a sandboxed agent signal any process the
        user owns — the coordinator, a sibling story's agent, the operator's
        shell. The fix must not reach for it.
        """
        profile = _macos_profile_for(tmp_path)
        assert "(target others)" not in profile
        assert "(allow signal)" not in profile

    def test_signal_grant_does_not_widen_write_boundary(self, tmp_path: Path) -> None:
        """The signal rules are capability grants, not filesystem ones."""
        profile = _macos_profile_for(tmp_path)
        assert "(deny default)" in profile
        signal_block = "\n".join(
            line for line in profile.splitlines() if line.startswith("(allow signal")
        )
        assert "subpath" not in signal_block


class TestGroupKillUnderRealSandbox:
    """End-to-end with real processes under a real seatbelt profile."""

    def test_profile_compiles(self, tmp_path: Path) -> None:
        """An unsupported filter would make sandbox-exec reject every agent run."""
        result = _run_sandboxed(["/usr/bin/true"], tmp_path)
        assert result.returncode == 0, f"profile rejected by sandbox-exec: {result.stderr}"

    def test_killpg_reaches_a_child_process_group(self, tmp_path: Path) -> None:
        """The actual bug: killpg on a spawned group must not return EPERM.

        Runs the real interpreter (resolved through the venv symlink, since the
        profile grants the toolchain roots rather than an arbitrary venv) under a
        real profile scoped to a throwaway worktree root, and has it spawn and
        group-kill a child exactly the way the runners do.
        """
        interpreter = os.path.realpath(sys.executable)
        result = _run_sandboxed([interpreter, "-c", _PROBE], tmp_path)
        assert "KILLPG_OK" in result.stdout, (
            "group kill was refused inside the sandbox — a timeout in this state "
            f"silently no-ops and runs to natural completion.\n{result.stdout}\n{result.stderr}"
        )
        assert "CHILD_SURVIVED" not in result.stdout


def _spawn_sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )


def _require_signalling_own_children() -> None:
    """Skip when the *host* forbids signalling our own subprocesses.

    Some sandboxes (including a nested one wrapping the test runner itself) deny
    signal delivery outright. Nothing about the code under test can be observed
    in that state, so skip rather than report a failure that is really a property
    of the harness.
    """
    proc = _spawn_sleeper()
    try:
        os.kill(proc.pid, 0)
        os.killpg(os.getpgid(proc.pid), 0)
    except OSError as exc:
        pytest.skip(f"host forbids signalling own children ({exc})")
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except OSError:
            pass


class TestTerminateProcessGroup:
    """The bounded-teardown backstop, exercised with real subprocesses."""

    @pytest.fixture(autouse=True)
    def _guard(self) -> None:
        _require_signalling_own_children()

    def _spawn_sleeper(self) -> subprocess.Popen[bytes]:
        return _spawn_sleeper()

    def test_kills_the_group_and_returns_promptly(self) -> None:
        from theforge import process_group

        proc = self._spawn_sleeper()
        try:
            process_group.terminate_process_group(proc)
            assert proc.poll() is not None, "sleeper outlived terminate_process_group"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_bounded_when_the_group_kill_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A refused killpg must cost the grace period, not the child's lifetime.

        Simulates the sandbox denial directly: with the group kill neutered, the
        old code path blocked in an unbounded ``proc.wait()`` for the full 60s
        sleep. The escalation to the direct child has to bound that.
        """
        from theforge import process_group

        monkeypatch.setattr(process_group, "_killpg_for", lambda _pid: False)
        proc = self._spawn_sleeper()
        try:
            process_group.terminate_process_group(proc, grace_seconds=1.0)
            assert proc.poll() is not None, (
                "refused group kill did not escalate to the direct child; "
                "teardown would block for the child's natural lifetime"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


class TestSurvivorIsAbandonedNotAwaited:
    """Deliberately outside TestTerminateProcessGroup's signalling guard.

    Both kill seams are stubbed out, so nothing here needs signal delivery to
    actually work — which means this runs on every host, including one that
    denies signalling entirely. That matters: the skip that protects the tests
    above is what hid a defect in this very test from a local run.
    """

    def test_survivor_is_logged_not_waited_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When nothing lands, give up loudly instead of blocking forever.

        Patches the two kill *seams* rather than ``os.kill`` itself. Replacing
        ``os.kill`` would replace it for everything in the process — including
        this test's own cleanup below, which would then be refused and leave the
        teardown blocking on a sleeper it could not kill.
        """
        from theforge import process_group

        logged: list[str] = []
        monkeypatch.setattr(process_group, "_killpg_for", lambda _pid: False)
        monkeypatch.setattr(process_group, "_kill_pid", lambda _pid: False)
        monkeypatch.setattr(process_group, "_log", logged.append)

        proc = _spawn_sleeper()
        try:
            start = time.monotonic()
            process_group.terminate_process_group(proc, grace_seconds=0.2)
            elapsed = time.monotonic() - start

            assert proc.poll() is None, "sleeper should still be alive — every kill was refused"
            assert any("survived teardown" in msg for msg in logged), logged
            assert elapsed < 5, (
                f"teardown took {elapsed:.1f}s with both kills refused — it waited on "
                "signals that were never delivered (#1959)"
            )
        finally:
            # Tolerant: on a host that denies signalling, cleanup cannot succeed
            # and the sleeper exits on its own. Failing here would report the
            # host's restrictions as a defect in the code under test.
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass


class TestKillPidGuard:
    """The direct-pid escalation needs the same guard killpg has (#1793)."""

    def test_refuses_targets_that_are_not_a_spawned_child(self) -> None:
        """``os.kill(0, …)`` signals our own group; ``os.kill(1, …)`` targets init.

        Pure — no process is spawned, so this pins the guard on every host,
        including ones where signalling is denied outright.
        """
        from theforge import process_group

        for bogus in (0, 1, -1, None, "4321"):
            assert process_group._kill_pid(bogus) is False, f"{bogus!r} must not be signalled"


class TestGateTimeoutBoundsItsOwnCleanup:
    """The gate's shell timeout must cost the timeout, not the command's lifetime.

    Neuters the group kill outright rather than relying on the host to refuse it,
    so this pins the behaviour on every platform — including one where signalling
    works and the regression would otherwise be invisible.
    """

    def test_timeout_returns_promptly_when_the_kill_does_not_land(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from theforge.coordinator import util

        # Record the process instead of killing it: this is the "kill was
        # refused" state, and it gives the teardown below a handle to clean up.
        spared: list[subprocess.Popen[str]] = []
        monkeypatch.setattr(util, "_kill_process_group", spared.append)

        cmd = (
            "python3 -c \"import sys, time; sys.stdout.write(chr(10).join(['partial'])); "
            'sys.stdout.write(chr(10)); sys.stdout.flush(); time.sleep(120)"'
        )
        start = time.monotonic()
        try:
            ok, output = util._run_shell(cmd, tmp_path, timeout=0.2)
            elapsed = time.monotonic() - start

            assert ok is False
            assert output.startswith("TIMEOUT after 0.2s:")
            assert "partial" in output, "output written before the timeout must survive"
            assert elapsed < 30, (
                f"timeout cleanup took {elapsed:.1f}s against a 120s command — the "
                "drain is waiting out the command's natural lifetime (#1959)"
            )
        finally:
            for proc in spared:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except OSError:
                    pass


class TestSidecarSurvivesPartialTeardown:
    """A group that outlives teardown must stay reachable by the orphan reaper.

    When the group kill is refused but the direct child can be signalled, that
    child dies while its grandchildren keep running. Dropping the pgid sidecar at
    that moment erases the only record `reap_orphan_agents` could ever use to
    kill them, so the survivors become permanently untracked — holding their
    workspace-write sandbox grant with nothing left to reclaim it.

    Both kill seams are stubbed, so none of this needs working signal delivery
    and it runs on every host.
    """

    # Child spawns a grandchild, records its pid, then hangs — the npm→node→leaf
    # shape the whole group-kill mechanism exists for, in miniature.
    _SPAWNS_GRANDCHILD = (
        "import subprocess,sys,time,pathlib;"
        "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(gc.pid));"
        "time.sleep(20)"
    )

    def _sidecars(self, project_root: Path) -> list[Path]:
        return sorted((project_root / ".forge" / "runs" / "agents").glob("*.json"))

    def _run_until_timeout(
        self, project_root: Path, pidfile: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from theforge import process_group

        monkeypatch.setenv("FORGE_PROJECT_ROOT", str(project_root))
        with pytest.raises(subprocess.TimeoutExpired):
            process_group.run_in_process_group(
                [sys.executable, "-c", self._SPAWNS_GRANDCHILD, str(pidfile)],
                timeout=1.0,
                capture_output=True,
                text=True,
            )

    def test_sidecar_kept_when_teardown_reached_only_the_direct_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from theforge import process_group

        # The exact defect state: killpg refused, direct-pid kill accepted.
        monkeypatch.setattr(process_group, "_killpg_for", lambda _pid: False)
        monkeypatch.setattr(process_group, "_kill_pid", lambda _pid: True)
        # The kill is refused on purpose here, so every teardown pass waits out the
        # full observation window. That window is not what this test verifies, and
        # at the 2s default it put the test over the enforced five-second bound.
        monkeypatch.setattr(process_group, "KILL_GRACE_SECONDS", 0.3)

        pidfile = tmp_path / "gc.pid"
        try:
            self._run_until_timeout(tmp_path, pidfile, monkeypatch)
            assert self._sidecars(tmp_path), (
                "sidecar was dropped while the group may still be alive — the "
                "surviving tree is now invisible to reap_orphan_agents"
            )
        finally:
            _reap_tree(pidfile)

    def test_sidecar_released_when_the_group_kill_lands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half: a landed group kill must not leak sidecars.

        Without this, "keep the record when unsure" quietly degrades into "never
        clean up", and the reaper accumulates entries pointing at pgids the OS is
        free to recycle.
        """
        from theforge import process_group

        killed: list[int] = []

        def _kill_group_for_real(pid: int) -> bool:
            killed.append(pid)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                pass
            return True  # report the group as killed, as a granted killpg would

        monkeypatch.setattr(process_group, "_killpg_for", _kill_group_for_real)

        pidfile = tmp_path / "gc.pid"
        try:
            self._run_until_timeout(tmp_path, pidfile, monkeypatch)
            assert killed, "teardown never attempted the group kill"
            assert not self._sidecars(tmp_path), (
                "sidecar outlived a successful group kill — records would "
                "accumulate pointing at pgids the OS can recycle"
            )
        finally:
            _reap_tree(pidfile)

    def test_normal_completion_releases_the_sidecar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No teardown at all is the common case and must not leak either."""
        from theforge import process_group

        monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
        result = process_group.run_in_process_group(
            [sys.executable, "-c", "print('ok')"], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert not self._sidecars(tmp_path)


class TestGroupIsAliveGuard:
    def test_refuses_pgids_that_cannot_denote_a_real_group(self) -> None:
        """Same #1793 guard: a probe on pgid 0/1 would ask about the wrong group."""
        from theforge import process_group

        for bogus in (0, 1, -1, None, "4321"):
            assert process_group.group_is_alive(bogus) is False, f"{bogus!r} is not a group"


def _reap_tree(pidfile: Path) -> None:
    """Best-effort cleanup of a child/grandchild left alive by a stubbed kill.

    Tolerant by design: on a host that denies signalling nothing here can
    succeed, and the sleepers exit on their own well inside the suite's runtime.
    """
    try:
        gc_pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(gc_pid, signal.SIGKILL)
    except OSError:
        pass
