"""Work a story starts must not outlive the story (#2309).

The leak these cover is the *quiet* one: the agent's own invocation succeeds and
exits 0, so every kill path in the runner is skipped — while something it started
(``pytest -n auto`` in the observed incident) keeps running, past the sprint, past
the worktree, past the budget that authorised it. Nothing failed, so nothing was
recorded; the only trace was the CPU the next run had to share.

Real subprocesses via ``tests/fake_bin/`` per the runner-lifecycle invariant: a
mock cannot show that a grandchild is gone, which is the entire claim here.
"""

from __future__ import annotations

import json
import os
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
from theforge.runners.runner_gemini import _run_gemini

_FAKE_BIN = Path(__file__).parent / "fake_bin"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait_until(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _reap(pid: int) -> None:
    """Best-effort cleanup so a failing assertion cannot itself leak a process."""
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------


class TestCleanExitLeavesNothingRunning:
    def test_grandchild_of_a_cleanly_exiting_child_is_killed(self, tmp_path: Path) -> None:
        """A child that exits 0 with descendants still running does not leave them.

        The pre-fix release path took a clean exit as proof the group had gone
        with the child and dropped the sidecar; the descendants then belonged to
        nothing at all.
        """
        pidfile = tmp_path / "gc.pid"
        script = (
            "import subprocess,sys,pathlib;"
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid));"
            "print('done')"
        )
        teardowns: list[process_group.ProcessTeardown] = []
        result = process_group.run_in_process_group(
            [sys.executable, "-c", script],
            timeout=30,
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            teardown_out=teardowns,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "done"
        gc_pid = int(pidfile.read_text().strip())
        try:
            assert _wait_until(lambda: not _pid_alive(gc_pid)), (
                "a grandchild outlived a cleanly-exiting invocation (#2309)"
            )
        finally:
            _reap(gc_pid)

        assert len(teardowns) == 1, "the forced teardown was not reported to the caller"
        teardown = teardowns[0]
        assert teardown.action == process_group.TEARDOWN_KILLED_SURVIVORS
        assert teardown.completed is True
        assert teardown.member_count >= 1
        assert teardown.sandbox_dir == str(tmp_path)

    def test_a_group_that_ends_on_its_own_reports_no_teardown(self, tmp_path: Path) -> None:
        """No survivors → no record. The presence of one has to mean something."""
        teardowns: list[process_group.ProcessTeardown] = []
        result = process_group.run_in_process_group(
            [sys.executable, "-c", "print('hi')"],
            timeout=30,
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            teardown_out=teardowns,
        )
        assert result.returncode == 0
        assert teardowns == []

    def test_the_sidecar_is_dropped_once_the_survivors_are_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A completed forced teardown leaves no work for the orphan reaper."""
        monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
        pidfile = tmp_path / "gc.pid"
        script = (
            "import subprocess,sys,pathlib;"
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))"
        )
        process_group.run_in_process_group(
            [sys.executable, "-c", script], timeout=30, capture_output=True, text=True
        )
        gc_pid = int(pidfile.read_text().strip())
        try:
            assert _wait_until(lambda: not _pid_alive(gc_pid))
        finally:
            _reap(gc_pid)
        assert list((tmp_path / ".forge" / "runs" / "agents").glob("*.json")) == [], (
            "the group is gone, so nothing should remain for a later sweep to chase"
        )


class TestLeaseCatchesWhatTheGroupCannot:
    """A process group is escapable; the inherited lease token is not."""

    def test_a_child_that_leaves_the_group_is_still_killed(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "gc.pid"
        script = (
            "import subprocess,sys,pathlib;"
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "start_new_session=True,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))"
        )
        teardowns: list[process_group.ProcessTeardown] = []
        result = process_group.run_in_process_group(
            [sys.executable, "-c", script],
            timeout=30,
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            teardown_out=teardowns,
        )
        assert result.returncode == 0
        gc_pid = int(pidfile.read_text().strip())
        try:
            assert _wait_until(lambda: not _pid_alive(gc_pid)), (
                "a process that left the group by calling setsid outlived the invocation"
            )
        finally:
            _reap(gc_pid)
        assert len(teardowns) == 1
        assert teardowns[0].escaped_pids == (gc_pid,)
        assert teardowns[0].members == (), "it had left the group, so it is not a member"
        assert teardowns[0].completed is True

    def test_the_token_is_inherited_through_a_whole_chain(self, tmp_path: Path) -> None:
        """Depth is not a way out: the token rides every fork and exec.

        A single ``setsid`` is the easy case. What has to hold is that a
        descendant several execs deep — the shape of ``npm exec`` → node → leaf,
        or a shell that starts a runner that starts workers — still carries it.
        """
        pidfile = tmp_path / "gc.pid"
        inner = (
            "import subprocess,sys,pathlib;"
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "start_new_session=True,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))"
        )
        outer = f"import subprocess,sys;subprocess.run([sys.executable,'-c',{inner!r}],check=True)"
        teardowns: list[process_group.ProcessTeardown] = []
        process_group.run_in_process_group(
            [sys.executable, "-c", outer],
            timeout=30,
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            teardown_out=teardowns,
        )
        gc_pid = int(pidfile.read_text().strip())
        try:
            assert _wait_until(lambda: not _pid_alive(gc_pid)), (
                "a great-grandchild in its own session escaped teardown"
            )
        finally:
            _reap(gc_pid)
        assert teardowns and gc_pid in teardowns[0].escaped_pids

    def test_a_lease_never_matches_another_spawn(self, tmp_path: Path) -> None:
        """Tokens are per-spawn, so one invocation cannot reap another's work.

        The lease kills by pid on the strength of the token alone, so the token
        being unique is what keeps that from being indiscriminate.
        """
        env_a, lease_a = process_group.open_process_lease({"PATH": os.environ["PATH"]})
        _env_b, lease_b = process_group.open_process_lease(None)
        assert lease_a.token != lease_b.token

        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=env_a,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _wait_until(lambda: proc.pid in process_group.lease_holders(lease_a))
            assert proc.pid not in process_group.lease_holders(lease_b)
            killed, gone = process_group.close_process_lease(lease_b)
            assert killed == () and gone is True
            assert _pid_alive(proc.pid), "another spawn's lease killed this process"
            # Only the pids are asserted, not "all gone": this process is a direct
            # child of the test, so it lingers as a zombie until waited below,
            # which a signal-0 probe cannot distinguish from running. The runners
            # never see that — they wait their direct child before releasing.
            killed, _gone = process_group.close_process_lease(lease_a)
            assert killed == (proc.pid,)
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# The CLI runners
# ---------------------------------------------------------------------------


class _RunnerTeardownBase:
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
    ) -> None:
        fake_bin = self._FAKE_BIN
        from theforge.workspace_env import build_workspace_env as _orig

        def _build(workspace_path, base_env=None, *, extra=None):  # type: ignore[no-untyped-def]
            env = _orig(workspace_path, base_env, extra=extra)
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            env[mode_var] = mode
            env["FAKE_GRANDCHILD_PID_FILE"] = str(pidfile)
            return env

        monkeypatch.setattr(f"theforge.runners.{module}.build_workspace_env", _build)

    def _assert_reaped_and_recorded(
        self, result: object, pidfile: Path, *, escaped: bool = False
    ) -> None:
        from theforge.agent_types import AgentResult

        assert isinstance(result, AgentResult)
        assert result.success is True, "the invocation itself succeeded; only its leftovers died"
        assert _wait_until(pidfile.exists, timeout=5.0), "the fake CLI never spawned a grandchild"
        gc_pid = int(pidfile.read_text().strip())
        try:
            assert _wait_until(lambda: not _pid_alive(gc_pid)), (
                "a process the agent started outlived the invocation (#2309)"
            )
        finally:
            _reap(gc_pid)
        teardown = result.process_teardown
        assert teardown is not None, "the forced kill left no trace in the run's own result"
        assert teardown.action == process_group.TEARDOWN_KILLED_SURVIVORS
        assert teardown.completed is True
        assert teardown.pgid is not None and teardown.pgid > 1
        if escaped:
            # The record must say *which* container caught it: a process that
            # left the group is a different fact from one that stayed in it.
            assert gc_pid in teardown.escaped_pids
            assert gc_pid not in teardown.members


class TestClaudeCleanExitTeardown(_RunnerTeardownBase):
    def test_success_does_not_leave_a_grandchild_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._ensure_exec("claude")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch, "runner_claude", "FAKE_CLAUDE_MODE", "grandchild_success", pidfile
        )
        profile = ModelProfile(
            name="dev",
            cli="claude",
            model="claude-sonnet-4-5",
            budget_usd=2.0,
            timeout_seconds=30,
            allowed_tools=("Bash",),
            sandbox_mode="none",
        )
        result = _run_claude(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        assert result.output == "Task complete."
        self._assert_reaped_and_recorded(result, pidfile)

    def test_success_does_not_leave_an_escapee_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A child that calls setsid leaves the group — and is still ended.

        This is the hole a group-only container has by construction: the escapee
        is in no group teardown can name, so killpg cannot reach it however
        carefully release checks. Its inherited lease token can.
        """
        self._ensure_exec("claude")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch, "runner_claude", "FAKE_CLAUDE_MODE", "escapee_success", pidfile
        )
        profile = ModelProfile(
            name="dev",
            cli="claude",
            model="claude-sonnet-4-5",
            budget_usd=2.0,
            timeout_seconds=30,
            allowed_tools=("Bash",),
            sandbox_mode="none",
        )
        result = _run_claude(
            prompt="do the thing",
            profile=profile,
            working_dir=tmp_path,
            fallback_to_file=False,
        )
        assert result.output == "Task complete."
        self._assert_reaped_and_recorded(result, pidfile, escaped=True)


class TestCodexCleanExitTeardown(_RunnerTeardownBase):
    def test_success_does_not_leave_a_grandchild_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._ensure_exec("npx")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch, "runner_codex", "FAKE_CODEX_MODE", "grandchild_success", pidfile
        )
        profile = ModelProfile(
            name="dev",
            cli="codex",
            model="gpt-5-codex",
            budget_usd=2.0,
            timeout_seconds=30,
            allowed_tools=("Bash",),
            sandbox_mode="none",
        )
        result = _run_codex(prompt="do the thing", profile=profile, working_dir=tmp_path)
        self._assert_reaped_and_recorded(result, pidfile)

    def test_success_does_not_leave_an_escapee_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._ensure_exec("npx")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(monkeypatch, "runner_codex", "FAKE_CODEX_MODE", "escapee_success", pidfile)
        profile = ModelProfile(
            name="dev",
            cli="codex",
            model="gpt-5-codex",
            budget_usd=2.0,
            timeout_seconds=30,
            allowed_tools=("Bash",),
            sandbox_mode="none",
        )
        result = _run_codex(prompt="do the thing", profile=profile, working_dir=tmp_path)
        self._assert_reaped_and_recorded(result, pidfile, escaped=True)


class TestGeminiCleanExitTeardown(_RunnerTeardownBase):
    def test_success_does_not_leave_a_grandchild_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Gemini gets the same containment as codex — it used to get none.

        Before this change the Gemini runner spawned through a bare
        ``subprocess.run``: no process group, no registry entry, and therefore no
        teardown and nothing for the orphan reaper to find either.
        """
        self._ensure_exec("npx")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch, "runner_gemini", "FAKE_GEMINI_MODE", "grandchild_success", pidfile
        )
        profile = ModelProfile(
            name="dev",
            cli="gemini",
            model="gemini-2.5-flash",
            budget_usd=2.0,
            timeout_seconds=30,
            allowed_tools=("Bash",),
            sandbox_mode="none",
        )
        result = _run_gemini(prompt="do the thing", profile=profile, working_dir=tmp_path)
        assert result.output == "Task complete."
        self._assert_reaped_and_recorded(result, pidfile)

    def test_success_does_not_leave_an_escapee_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._ensure_exec("npx")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch, "runner_gemini", "FAKE_GEMINI_MODE", "escapee_success", pidfile
        )
        profile = ModelProfile(
            name="dev",
            cli="gemini",
            model="gemini-2.5-flash",
            budget_usd=2.0,
            timeout_seconds=30,
            allowed_tools=("Bash",),
            sandbox_mode="none",
        )
        result = _run_gemini(prompt="do the thing", profile=profile, working_dir=tmp_path)
        self._assert_reaped_and_recorded(result, pidfile, escaped=True)


# ---------------------------------------------------------------------------
# The gate shell
# ---------------------------------------------------------------------------


class TestGateShellCleanExitTeardown:
    def test_gate_command_that_exits_first_does_not_leave_workers_running(
        self, tmp_path: Path
    ) -> None:
        """``make gate`` returning is not evidence its test workers stopped.

        Same assumption, same leak, one layer over: the shell path registered a
        group and unregistered it on any clean completion.
        """
        from theforge.coordinator.util import _run_shell_detailed

        pidfile = tmp_path / "gc.pid"
        script = (
            f'{sys.executable} -c "import subprocess,sys,pathlib;'
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))\""
        )
        ok, _output, code, timed_out = _run_shell_detailed(script, tmp_path, timeout=30)
        assert ok is True
        assert code == 0
        assert timed_out is False
        gc_pid = int(pidfile.read_text().strip())
        try:
            assert _wait_until(lambda: not _pid_alive(gc_pid)), (
                "a gate command's descendant outlived the gate run (#2309)"
            )
        finally:
            _reap(gc_pid)

    def test_gate_command_descendant_that_leaves_the_group_is_killed_too(
        self, tmp_path: Path
    ) -> None:
        """The gate is where ``pytest -n auto`` actually runs, so it needs both."""
        from theforge.coordinator.util import _run_shell_detailed

        pidfile = tmp_path / "gc.pid"
        script = (
            f'{sys.executable} -c "import subprocess,sys,pathlib;'
            "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
            "start_new_session=True,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))\""
        )
        ok, _output, code, _timed_out = _run_shell_detailed(script, tmp_path, timeout=30)
        assert ok is True
        assert code == 0
        gc_pid = int(pidfile.read_text().strip())
        try:
            assert _wait_until(lambda: not _pid_alive(gc_pid)), (
                "a gate worker in its own session outlived the gate run (#2309)"
            )
        finally:
            _reap(gc_pid)


# ---------------------------------------------------------------------------
# The originating run's own record
# ---------------------------------------------------------------------------


def test_forced_teardown_appears_in_the_run_audit(tmp_path: Path) -> None:
    """The run that spawned the work says the work had to be killed.

    A leaked process produces no artifact, no cost line and no log the run keeps,
    so without this the only evidence is on a host somebody has to think to look
    at. ``None`` on an invocation that ended cleanly is equally load-bearing: it
    is the difference between "nothing survived" and "nobody checked".
    """
    import dataclasses

    from theforge.agent_types import AgentResult
    from theforge.config import (
        DEFAULT_DEV_PROFILE,
        DEFAULT_PREFLIGHT_PROFILE,
        DEFAULT_REVIEW_PROFILE,
        ForgeConfig,
        RetryPolicy,
        ValidationConfig,
        WorkspaceConfig,
        build_provenance,
    )
    from theforge.coordinator.audit import generate_audit_log
    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
    from theforge.task import TaskStory

    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# spec", encoding="utf-8")
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: test\n", encoding="utf-8")
    config = ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(gate_command="make gate"),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )
    config = dataclasses.replace(config, provenance=build_provenance(config, config_path))

    teardown = process_group.ProcessTeardown(
        pgid=90210,
        action=process_group.TEARDOWN_KILLED_SURVIVORS,
        member_count=41,
        members=(90210, 90211),
        escaped_pids=(90777,),
        sandbox_dir=str(tmp_path / "worktrees" / "issue-2284"),
        completed=True,
    )
    state = CoordinatorState()
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.run_id = "deadbeefcafe"
    state.dev_results.append(
        AgentResult(
            success=True,
            output="done",
            session_id="s",
            cost_usd=1.0,
            exit_code=0,
            raw={},
            profile_name="dev",
            process_teardown=teardown,
        )
    )
    state.dev_durations.append(1.0)
    # A second invocation whose group ended on its own, to pin the honest null.
    state.review_agent_results.append(
        AgentResult(
            success=True,
            output="APPROVE",
            session_id="s",
            cost_usd=1.0,
            exit_code=0,
            raw={},
            profile_name="reviewer",
        )
    )
    state.review_durations.append(1.0)

    record = generate_audit_log(
        config,
        TaskStory(name="Test", slug="test", story_path=spec_path),
        CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done"),
    )
    by_role = {entry["role"]: entry for entry in record["cost"]["agents"]}
    assert by_role["dev"]["process_teardown"] == {
        "pgid": 90210,
        "action": "killed_survivors",
        "member_count": 41,
        "members": [90210, 90211],
        "escaped_pids": [90777],
        "sandbox_dir": str(tmp_path / "worktrees" / "issue-2284"),
        "completed": True,
    }
    assert by_role["review"]["process_teardown"] is None


def test_pre_v24_records_read_back_as_did_not_say() -> None:
    """A v23 record never asked the question, so the migration must not answer it."""
    from theforge.coordinator.audit_substrate import _migrate_record

    migrated = _migrate_record(
        {"cost": {"agents": [{"role": "dev"}, {"role": "review"}]}}, from_version=23
    )
    assert [entry["process_teardown"] for entry in migrated["cost"]["agents"]] == [None, None]


def test_a_group_the_kill_cannot_reach_is_left_for_the_reaper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused kill keeps the sidecar and says so, rather than claiming success.

    The sandbox case: ``killpg`` is denied, the survivors stay up, and the only
    honest outcome is a retained record plus a teardown marked incomplete.
    """
    monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(process_group, "kill_agent_group", lambda _pgid: False)

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = os.getpgid(proc.pid)
    try:
        process_group.register_agent_group(pgid, sandbox_dir=str(tmp_path))
        teardown = process_group.release_group_record(
            pgid, group_killed=True, sandbox_dir=str(tmp_path)
        )
        assert teardown is not None
        assert teardown.action == process_group.TEARDOWN_RETAINED_FOR_REAPER
        assert teardown.completed is False
        assert proc.pid in teardown.members
        sidecars = list((tmp_path / ".forge" / "runs" / "agents").glob("*.json"))
        assert len(sidecars) == 1, "a group that survived must stay reachable by the reaper"
    finally:
        _reap(proc.pid)
        proc.wait(timeout=5)


def test_the_reaper_kills_an_escapee_a_sigkilled_sprint_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SIGKILL-ed-sprint path needs the lease too, for the same reason.

    A sprint killed outright runs no teardown at all, so the sidecar is the only
    handle a later sweep has. A pgid on it cannot describe a descendant that had
    already left the group — and a recycled pgid is why the sweep declines to act
    on one it cannot verify (#2115). The token is verifiable by construction: it
    was minted for one spawn and never reused.
    """
    monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
    leased_env, lease = process_group.open_process_lease(dict(os.environ))
    escapee = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=leased_env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # The record a spawn leaves behind, with its owner sprint already dead.
        process_group.register_agent_group(escapee.pid, sandbox_dir=str(tmp_path), lease=lease)
        sidecars = list((tmp_path / ".forge" / "runs" / "agents").glob("*.json"))
        assert len(sidecars) == 1
        record = json.loads(sidecars[0].read_text(encoding="utf-8"))
        assert record["lease"] == lease.token
        record["owner_pid"] = 999_999
        record["pgid"] = 2  # a pgid that names nothing — all the sweep has is the token
        sidecars[0].write_text(json.dumps(record), encoding="utf-8")

        process_group.reap_orphan_agents(tmp_path)

        assert _wait_until(lambda: not _pid_alive(escapee.pid) or escapee.poll() is not None), (
            "the reaper left an escapee running because the pgid could not name it"
        )
    finally:
        _reap(escapee.pid)
        escapee.wait(timeout=5)
