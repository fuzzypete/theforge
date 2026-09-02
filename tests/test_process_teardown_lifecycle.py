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

import dataclasses
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

from theforge import process_group, process_tree
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


def _audit_config(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A minimal-but-real ForgeConfig, loaded from a file like a real run's."""
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

    (tmp_path / "spec.md").write_text("# spec", encoding="utf-8")
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
    return dataclasses.replace(config, provenance=build_provenance(config, config_path))


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
            killed, gone = process_group.kill_escapees(lease=lease_b)
            assert killed == () and gone is True
            assert _pid_alive(proc.pid), "another spawn's lease killed this process"
            # Only the pids are asserted, not "all gone": this process is a direct
            # child of the test, so it lingers as a zombie until waited below,
            # which a signal-0 probe cannot distinguish from running. The runners
            # never see that — they wait their direct child before releasing.
            killed, _gone = process_group.kill_escapees(lease=lease_a)
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

    def test_success_does_not_leave_an_unreadable_escapee_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The shape no environment token can find (#2309, cycle 2).

        A SIP-protected platform binary with an empty environment in its own
        session: macOS refuses to hand out its environ, and there would be no
        token in it anyway. This passes only because teardown observed it while
        it still had a visible parent.
        """
        self._ensure_exec("claude")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch,
            "runner_claude",
            "FAKE_CLAUDE_MODE",
            "unreadable_escapee_success",
            pidfile,
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

    def test_success_does_not_leave_an_unreadable_escapee_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._ensure_exec("npx")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch,
            "runner_codex",
            "FAKE_CODEX_MODE",
            "unreadable_escapee_success",
            pidfile,
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

    def test_success_does_not_leave_an_unreadable_escapee_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._ensure_exec("npx")
        pidfile = tmp_path / "gc.pid"
        self._patch_env(
            monkeypatch,
            "runner_gemini",
            "FAKE_GEMINI_MODE",
            "unreadable_escapee_success",
            pidfile,
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
        self._assert_reaped_and_recorded(result, pidfile, escaped=True)


# ---------------------------------------------------------------------------
# The gate shell
# ---------------------------------------------------------------------------


def test_a_leaking_gate_is_recorded_in_the_run_audit(tmp_path: Path) -> None:
    """The run says the gate leaked, not just the console the run scrolled past.

    The gate is where ``pytest -n auto`` actually runs, so it is the likeliest
    source of a leak — and a killed worker leaves no artifact, no cost line and
    no failure, so unless the teardown fact is carried from the shell into the
    record, the run that caused it reads exactly like one that did not.
    """
    from theforge.coordinator import gate as gate_mod
    from theforge.coordinator.audit import generate_audit_log
    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
    from theforge.coordinator.validate_phase import _record_gate_run
    from theforge.task import TaskStory

    config = _audit_config(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pidfile = tmp_path / "gc.pid"
    leaky_gate = (
        f'{sys.executable} -c "import subprocess,sys,pathlib;'
        "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))\""
    )
    config = dataclasses.replace(
        config, validation=dataclasses.replace(config.validation, gate_command=leaky_gate)
    )

    state = CoordinatorState()
    state.started_at = "2026-01-01T00:00:00+00:00"
    state.run_id = "deadbeefcafe"
    teardowns: list[process_group.ProcessTeardown] = []
    decision, _err, _tail, _cmd, _code = gate_mod.run_gate_full(
        config, workspace, process_teardowns=teardowns
    )
    gc_pid = int(pidfile.read_text().strip())
    try:
        assert _wait_until(lambda: not _pid_alive(gc_pid))
    finally:
        _reap(gc_pid)
    assert decision == "PASS", "the gate itself passed; only its leftovers were killed"
    assert teardowns, "the gate's forced teardown never reached its caller"

    _record_gate_run(state, workspace, decision="PASS")
    for teardown in teardowns:
        state.gate_process_teardowns.append(
            {"gate_run": state.gate_runs, **teardown.to_audit_dict()}
        )
    state.validate_durations.append(1.0)
    record = generate_audit_log(
        config,
        TaskStory(name="Test", slug="test", story_path=tmp_path / "spec.md"),
        CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done"),
    )
    recorded = record["iterations"]["gate_process_teardowns"]
    assert len(recorded) == 1
    assert recorded[0]["gate_run"] == 1
    assert recorded[0]["action"] == process_group.TEARDOWN_KILLED_SURVIVORS
    assert recorded[0]["completed"] is True
    assert gc_pid in recorded[0]["members"] + recorded[0]["escaped_pids"]


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
    from theforge.agent_types import AgentResult
    from theforge.coordinator.audit import generate_audit_log
    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
    from theforge.task import TaskStory

    config = _audit_config(tmp_path)
    spec_path = tmp_path / "spec.md"

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


def test_a_v24_teardown_is_read_back_as_the_gate_that_produced_it() -> None:
    """v24 recorded a teardown without saying which shell leaked it.

    ``"gate"`` is the correct backfill rather than a null, and this asserts the
    output rather than trusting the docstring: v24 only ever collected from the
    gate-family shells, because the pre-validate path that made the distinction
    necessary did not record at all. So an older entry is not "unknown" — the
    field names what the record already implied.
    """
    from theforge.coordinator.audit_substrate import _migrate_record

    migrated = _migrate_record(
        {
            "iterations": {
                "gate_process_teardowns": [
                    {"gate_run": 1, "action": "killed_survivors"},
                    {"gate_run": 2, "action": "retained_for_reaper"},
                ]
            }
        },
        from_version=24,
    )
    entries = migrated["iterations"]["gate_process_teardowns"]
    assert [entry["source"] for entry in entries] == ["gate", "gate"]
    # The rest of each entry is carried through untouched.
    assert [entry["gate_run"] for entry in entries] == [1, 2]
    assert [entry["action"] for entry in entries] == [
        "killed_survivors",
        "retained_for_reaper",
    ]


def test_a_teardown_that_already_names_its_source_is_left_alone() -> None:
    """The migration fills a gap; it never overwrites what a writer stated."""
    from theforge.coordinator.audit_substrate import _migrate_record

    migrated = _migrate_record(
        {
            "iterations": {
                "gate_process_teardowns": [
                    {"gate_run": 1, "source": "pre_validate"},
                    {"gate_run": 1, "source": "gate_diagnostic"},
                ]
            }
        },
        from_version=24,
    )
    assert [e["source"] for e in migrated["iterations"]["gate_process_teardowns"]] == [
        "pre_validate",
        "gate_diagnostic",
    ]


def test_a_record_with_no_teardowns_survives_the_migration() -> None:
    """The shapes a real record can take, none of which the migration may break."""
    from theforge.coordinator.audit_substrate import _migrate_record

    for record in (
        {"iterations": {"gate_process_teardowns": []}},
        {"iterations": {}},
        {"iterations": "not-a-dict"},
        {},
    ):
        assert _migrate_record(dict(record), from_version=24) is not None


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


def test_the_reaper_kills_an_unreadable_escapee_a_stopped_sprint_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep's hardest case: no group, no readable token, no live observer.

    A SIP-protected platform binary with an empty environment, in its own
    session, whose owner sprint has since died. The pgid cannot name it (it left
    the group), the lease cannot find it (its environment is unreadable and
    carries no token anyway), and the tracker that watched it is gone with the
    sprint. What remains is what that tracker wrote down while it was alive
    (#2309) — without which the sweep reports zero reaped and walks away from a
    running process.
    """
    monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
    sleeper = shutil.which("sleep") or "/bin/sleep"
    escapee = subprocess.Popen(  # noqa: S603
        [sleeper, "30"],
        env={},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert (
            process_group.lease_holders(process_group.open_process_lease(dict(os.environ))[1])
            == []
        ), "sanity: this process carries no token any sweep could read"

        # The record a live run leaves behind: registered at spawn, then updated
        # as the tracker observes. Both halves are the production writers.
        pgid = 999_998  # a group that no longer exists — all the sweep has is the note
        process_group.register_agent_group(pgid, sandbox_dir=str(tmp_path))
        process_group.record_observed_descendants(
            pgid, {escapee.pid: process_tree.process_info(escapee.pid).fingerprint}
        )

        sidecars = list((tmp_path / ".forge" / "runs" / "agents").glob("*.json"))
        assert len(sidecars) == 1
        record = json.loads(sidecars[0].read_text(encoding="utf-8"))
        assert record["observed"] == {
            str(escapee.pid): process_tree.process_info(escapee.pid).fingerprint
        }, "the observation must be durable, or the sweep inherits nothing"

        # The owner sprint is gone — the state `forge stop` leaves behind.
        record["owner_pid"] = 999_999
        sidecars[0].write_text(json.dumps(record), encoding="utf-8")

        reaped = process_group.reap_orphan_agents(tmp_path)

        # Waited rather than probed: this escapee is a direct child of the test,
        # so after SIGKILL it lingers as a zombie that a signal-0 probe cannot
        # tell from a running process. The real runners never see that — they
        # wait their direct child before releasing.
        try:
            returncode = escapee.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail("the sweep left an unreadable escapee running (#2309)")
        assert returncode == -signal.SIGKILL
        assert reaped == 1, "a sweep that killed something must not report zero"
    finally:
        _reap(escapee.pid)
        escapee.wait(timeout=5)


def test_a_recycled_observed_pid_is_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A written-down pid is only a target while it is still the process seen.

    The observation outlives the process that made it, so by the time a sweep
    reads it the id may belong to something else entirely — which is exactly the
    mistake #2115 exists to prevent.
    """
    monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
    bystander = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        pgid = 999_998
        process_group.register_agent_group(pgid, sandbox_dir=str(tmp_path))
        # Same pid, a start time that is not this process's — what a recycled id
        # looks like to a sweep reading a note from an earlier run.
        process_group.record_observed_descendants(pgid, {bystander.pid: "bsdinfo:1.000000"})
        sidecars = list((tmp_path / ".forge" / "runs" / "agents").glob("*.json"))
        record = json.loads(sidecars[0].read_text(encoding="utf-8"))
        record["owner_pid"] = 999_999
        sidecars[0].write_text(json.dumps(record), encoding="utf-8")

        assert process_group.reap_orphan_agents(tmp_path) == 0
        assert bystander.poll() is None, "an unrelated process was killed on a stale note"
    finally:
        _reap(bystander.pid)
        bystander.wait(timeout=5)


def _validate_repo(tmp_path: Path) -> Path:
    """A repo with a commit ahead of base, which VALIDATE requires to proceed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)  # noqa: S603
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)  # noqa: S603
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)  # noqa: S603
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=repo, check=True)  # noqa: S603
    (repo / "g.txt").write_text("y\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)  # noqa: S603
    subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=repo, check=True)  # noqa: S603
    return repo


def _leaky_command(pidfile: Path) -> str:
    """A command that starts a long-lived background process and returns."""
    return (
        f'{sys.executable} -c "import subprocess,sys,pathlib;'
        "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))\""
    )


def test_a_gate_leak_is_tagged_with_the_gate_that_ran(tmp_path: Path) -> None:
    """The first gate's leak is gate_run 1, matching every other gate telemetry.

    Driven through the real validate phase rather than by calling the recorder
    directly, because the defect this pins was purely an ordering one: the
    teardown was appended before the run counter incremented, so the first leak
    of a run was filed under a gate number that had not run yet (#2309).
    """
    from theforge.coordinator.state import CoordinatorState
    from theforge.coordinator.validate_phase import _run_validate_phase
    from theforge.task import TaskStory

    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)  # noqa: S603
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)  # noqa: S603
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)  # noqa: S603
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=repo, check=True)  # noqa: S603
    (repo / "g.txt").write_text("y\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)  # noqa: S603
    subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=repo, check=True)  # noqa: S603

    pidfile = tmp_path / "gc.pid"
    leaky_gate = (
        f'{sys.executable} -c "import subprocess,sys,pathlib;'
        "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))\""
    )
    config = _audit_config(tmp_path)
    config = dataclasses.replace(
        config,
        validation=dataclasses.replace(config.validation, gate_command=leaky_gate),
        workspace=dataclasses.replace(config.workspace, base_branch="main"),
    )
    state = CoordinatorState()
    task = TaskStory(name="t", slug="t", story_path=tmp_path / "spec.md")

    _run_validate_phase(state, config, task, repo, notify=False, logger=None)

    gc_pid = int(pidfile.read_text().strip())
    try:
        assert _wait_until(lambda: not _pid_alive(gc_pid)), (
            "the gate's descendant outlived the gate run (#2309)"
        )
    finally:
        _reap(gc_pid)

    assert state.gate_runs == 1
    assert len(state.gate_process_teardowns) == 1
    recorded = state.gate_process_teardowns[0]
    assert recorded["gate_run"] == state.gate_runs, (
        "a leak filed under gate_run 0 points at a gate that never ran"
    )
    assert recorded["action"] == process_group.TEARDOWN_KILLED_SURVIVORS


class TestEnumerationFailureIsNotAnEmptyGroup:
    """An unreadable membership must not be mistaken for a settled one."""

    def test_a_failed_read_reports_that_it_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            members, enumerated = process_group.group_members_checked(proc.pid)
            assert proc.pid in members and enumerated is True

            # The read fails the way a loaded host makes it fail: sysctl returns
            # nothing, /proc cannot be listed.
            if sys.platform == "darwin":
                monkeypatch.setattr(process_group, "_sysctl_bytes", lambda _mib: None)
            else:
                monkeypatch.setattr(process_group.os, "listdir", _raise_oserror, raising=False)
            members, enumerated = process_group.group_members_checked(proc.pid)
            assert members == {}
            assert enumerated is False, (
                "a read that failed learned nothing; saying so is what keeps an "
                "empty answer from reading as an empty group"
            )
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_release_does_not_drop_a_live_group_on_a_failed_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sidecar is the only handle on survivors; a bad read must not lose it."""
        monkeypatch.setenv("FORGE_PROJECT_ROOT", str(tmp_path))
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            pgid = os.getpgid(proc.pid)
            process_group.register_agent_group(pgid, sandbox_dir=str(tmp_path))
            # Alive to the probe, unreadable to the enumeration.
            monkeypatch.setattr(process_group, "group_members_checked", lambda _pgid: ({}, False))
            monkeypatch.setattr(process_group, "kill_agent_group", lambda _pgid: False)

            teardown = process_group.release_group_record(pgid, group_killed=True)

            assert teardown is not None, "a live group that could not be read is not 'no teardown'"
            assert teardown.action == process_group.TEARDOWN_RETAINED_FOR_REAPER
            assert list((tmp_path / ".forge" / "runs" / "agents").glob("*.json")), (
                "the record was dropped without a kill on an answer that was never obtained"
            )
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)


def _raise_oserror(*_args: object, **_kwargs: object) -> list[str]:
    raise OSError("simulated /proc read failure")


def test_a_leaking_dev_verification_command_records_its_teardown(tmp_path: Path) -> None:
    """A declared verification command is a build or a test run — the leak shape.

    It runs outside the dev sandbox through the same shell as the gate, so it can
    leave workers behind in exactly the way #2309 describes. The kill already
    happened at release; what this pins is that the run's own record says so,
    rather than the fact living only in a log line.
    """
    from theforge.config.types import DevVerificationCommand
    from theforge.coordinator.dev_verification import DevVerificationBroker

    pidfile = tmp_path / "gc.pid"
    leaky = (
        f'{sys.executable} -c "import subprocess,sys,pathlib;'
        "gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path(r'{pidfile}').write_text(str(gc.pid))\""
    )
    broker = DevVerificationBroker(
        workspace_path=tmp_path,
        commands=(
            DevVerificationCommand(
                name="verify-leaky", command=leaky, timeout=60, output_tail_chars=200
            ),
        ),
        iteration=1,
        max_requests=5,
    )
    request = broker.request_dir / "r1.json.tmp"
    request.write_text(json.dumps({"command": "verify-leaky"}), encoding="utf-8")
    request.rename(broker.request_dir / "r1.json")

    broker.poll_once()

    gc_pid = int(pidfile.read_text().strip())
    try:
        assert _wait_until(lambda: not _pid_alive(gc_pid)), (
            "a verification command's descendant outlived the command (#2309)"
        )
    finally:
        _reap(gc_pid)

    payloads = broker.records()
    assert len(payloads) == 1
    teardown = payloads[0].get("process_teardown")
    assert teardown is not None, "the forced kill left no trace in the run's own record"
    assert teardown["action"] == process_group.TEARDOWN_KILLED_SURVIVORS
    assert teardown["completed"] is True


def test_a_verification_command_that_leaves_nothing_records_nothing(tmp_path: Path) -> None:
    """Absence has to mean something: a clean command carries no teardown key."""
    from theforge.config.types import DevVerificationCommand
    from theforge.coordinator.dev_verification import DevVerificationBroker

    broker = DevVerificationBroker(
        workspace_path=tmp_path,
        commands=(
            DevVerificationCommand(
                name="verify-clean",
                command=f'{sys.executable} -c "print(1)"',
                timeout=60,
                output_tail_chars=200,
            ),
        ),
        iteration=1,
        max_requests=5,
    )
    request = broker.request_dir / "r1.json.tmp"
    request.write_text(json.dumps({"command": "verify-clean"}), encoding="utf-8")
    request.rename(broker.request_dir / "r1.json")

    broker.poll_once()

    assert "process_teardown" not in broker.records()[0]


def test_a_leaking_pre_validate_command_is_recorded_and_named(tmp_path: Path) -> None:
    """A pre-validate leak reaches the record, and says it was not the gate.

    ``pre_validate_command`` is a project command configured to run after the
    gate passes, so it spawns whatever it likes — the same leak shape as the gate
    itself. It runs through ``_run_shell``, which used to drop the collector, so
    the kill happened and the run said nothing (#2309). Four commands run in this
    phase; the record names which one, or an operator opens the wrong trace.
    """
    from theforge.coordinator.state import CoordinatorState
    from theforge.coordinator.validate_phase import (
        VALIDATE_SHELL_PRE_VALIDATE,
        _run_validate_phase,
    )
    from theforge.task import TaskStory

    repo = _validate_repo(tmp_path)
    pidfile = tmp_path / "pv.pid"
    config = _audit_config(tmp_path)
    config = dataclasses.replace(
        config,
        validation=dataclasses.replace(
            config.validation,
            gate_command=f'{sys.executable} -c "print(1)"',
            pre_validate_command=_leaky_command(pidfile),
        ),
        workspace=dataclasses.replace(config.workspace, base_branch="main"),
    )
    state = CoordinatorState()
    task = TaskStory(name="t", slug="t", story_path=tmp_path / "spec.md")

    _run_validate_phase(state, config, task, repo, notify=False, logger=None)

    leaked = int(pidfile.read_text().strip())
    try:
        assert _wait_until(lambda: not _pid_alive(leaked)), (
            "the pre-validate command's descendant outlived the run (#2309)"
        )
    finally:
        _reap(leaked)

    recorded = [
        entry
        for entry in state.gate_process_teardowns
        if entry["source"] == VALIDATE_SHELL_PRE_VALIDATE
    ]
    assert len(recorded) == 1, (
        f"the pre-validate teardown never reached the run's record: {state.gate_process_teardowns}"
    )
    assert recorded[0]["action"] == process_group.TEARDOWN_KILLED_SURVIVORS
    assert recorded[0]["completed"] is True
    # The gate itself ran and left nothing, so it contributes no entry — the
    # record must not attribute this leak to it.
    assert recorded[0]["gate_run"] == state.gate_runs == 1


def test_a_clean_pre_validate_command_records_nothing(tmp_path: Path) -> None:
    """Absence keeps meaning something on this path too."""
    from theforge.coordinator.state import CoordinatorState
    from theforge.coordinator.validate_phase import _run_validate_phase
    from theforge.task import TaskStory

    repo = _validate_repo(tmp_path)
    config = _audit_config(tmp_path)
    config = dataclasses.replace(
        config,
        validation=dataclasses.replace(
            config.validation,
            gate_command=f'{sys.executable} -c "print(1)"',
            pre_validate_command=f'{sys.executable} -c "print(2)"',
        ),
        workspace=dataclasses.replace(config.workspace, base_branch="main"),
    )
    state = CoordinatorState()
    task = TaskStory(name="t", slug="t", story_path=tmp_path / "spec.md")

    _run_validate_phase(state, config, task, repo, notify=False, logger=None)

    assert state.gate_process_teardowns == []


def _environ_is_readable(pid: int) -> bool:
    """True when this process may read *pid*'s environment at all."""
    if sys.platform == "darwin":
        return bool(
            process_group._sysctl_bytes(
                (process_group._CTL_KERN, process_group._KERN_PROCARGS2, pid)
            )
        )
    try:
        Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return True


class TestTheLeaseSweepCostsWhatItShould:
    """The sweep must stay cheap without becoming blind (#2309, cycle 3)."""

    def test_every_pruned_process_was_one_the_sweep_could_never_match(self) -> None:
        """The prune's correctness condition, checked rather than assumed.

        A process may be dropped from the candidate set for exactly two reasons:
        it started before the spawn did (so it cannot be that spawn's
        descendant), or the platform will not describe it to us at all (so its
        environment is not ours to read and no match was ever possible). Anything
        else dropped would be a leak the sweep stopped looking for.

        "Older than the spawn" is asserted in whichever clock the prune actually
        used — boot-relative where the platform offers one, wall-clock otherwise.
        Restating it in the other clock would make this test itself a victim of
        the drift #2689 was about, passing or failing on which clock was read.
        """
        _env, lease = process_group.open_process_lease(None)
        # The table is snapshotted *before* the candidate scan, so every pid
        # checked below already existed when the scan ran. Reading it afterwards
        # would sweep up processes born in between and blame the prune for not
        # having seen the future.
        live = [pid for pid in process_tree.live_pids() if pid > 1]
        candidates = set(
            process_group._lease_candidates(lease.started_at, lease.opened_since_boot)
        )
        assert len(live) > 10, "sanity: this host should have a real process table"

        for pid in live:
            if pid in candidates:
                continue
            info = process_tree.process_info(pid)
            if info is None:
                # Exited between the snapshot and the scan, or never describable.
                # The latter is the case the prune relies on, so it is confirmed
                # rather than assumed: skipping is only safe because the refusal
                # to describe and the refusal to read coincide.
                assert not _environ_is_readable(pid), (
                    f"pid {pid} was skipped but its environment can be read — "
                    "the sweep dropped a process it could have matched"
                )
                continue
            if lease.opened_since_boot is not None and info.started_since_boot is not None:
                started, spawned = info.started_since_boot, lease.opened_since_boot
            else:
                started, spawned = info.started_at, lease.started_at
            assert started is not None and started < spawned, (
                f"pid {pid} was skipped without being older than the spawn"
            )

    def test_a_short_command_does_not_scan_the_whole_host(self) -> None:
        """Cost scales with how long the spawn ran, not with the process table.

        Reading every process's environment on the release of every short shell
        command made a `git status` pay to search for a descendant it could not
        possibly have had. The bound asserted here is deliberately loose: the
        exact count is a property of the host, but "most of the table" is a
        property of the bug.
        """
        _env, lease = process_group.open_process_lease(None)
        candidates = process_group._lease_candidates(lease.started_at, lease.opened_since_boot)
        live = [pid for pid in process_tree.live_pids() if pid > 1]
        assert len(live) > 10, "sanity: this host should have a real process table"
        assert len(candidates) < len(live) / 2, (
            f"the sweep still considers {len(candidates)} of {len(live)} processes; "
            "the start-time prune is not taking effect on this host"
        )

    def test_the_prune_never_drops_a_real_descendant(self) -> None:
        """Cheap must not mean blind: what the sweep exists to find is still found."""
        env, lease = process_group.open_process_lease(dict(os.environ))
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _wait_until(lambda: proc.pid in process_group.lease_holders(lease)), (
                "the prune dropped a descendant the sweep was built to catch"
            )
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)

    def test_a_reaper_record_with_no_spawn_time_prunes_nothing_on_age(self) -> None:
        """A sweep that cannot date the spawn must not prune on age.

        ``reap_orphan_agents`` reads a token from a sidecar written by a process
        that is gone, so it has no start moment to compare against and passes
        0.0. Every process it could read is then a candidate — the age filter
        contributes nothing, which is the only safe reading of "I do not know
        when this spawn was".
        """
        readable = {
            pid
            for pid in process_tree.live_pids()
            if pid > 1 and process_tree.process_info(pid) is not None
        }
        candidates = set(process_group._lease_candidates(0.0))
        # A process that exited between the two reads is legitimately absent.
        missed = {pid for pid in readable - candidates if process_tree.process_info(pid)}
        assert not missed, (
            f"an undated sweep dropped a live process it could have read: {sorted(missed)[:5]}"
        )

    def test_an_undated_record_carries_no_boot_relative_moment_either(self) -> None:
        """The undated sweep must stay undated in *both* clocks.

        A sidecar's lease survives the process that opened it, so a boot-relative
        moment recorded alongside it would either prune (defeating the "keep
        everything" reading above) or, after a reboot, prune against a frame that
        no longer exists. The reap therefore carries neither clock.
        """
        lease = process_group._recorded_lease({"lease": "sometoken"})

        assert lease is not None
        assert lease.started_at == 0.0
        assert lease.opened_since_boot is None

    def test_the_prune_orders_by_the_kernels_clock_not_a_drifting_wall_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wall clock that disagrees with the kernel changes neither verdict.

        This is #2689 stated as a unit. On Linux a process's wall-clock start is
        *composed* from a cached boot epoch, so it can drift arbitrarily far from
        a freshly-read ``time.time()`` while the kernel's own boot-relative
        numbers stay exact. Here the two clocks are made to contradict each other
        outright — the live descendant looks 100s older than the spawn in wall
        time, the pre-spawn process looks 100s newer — and the prune must follow
        the kernel in both directions: keep what started after the spawn, drop
        what started before it.
        """
        table = {
            # Started after the spawn, but the wall clock says a minute before it.
            101: process_tree.ProcessInfo(
                101, 1, 101, "proc:1", started_at=4900.0, started_since_boot=1002.0
            ),
            # Started before the spawn — after this worker did, which is exactly
            # the case a "when did *I* start" baseline would have kept — while
            # the wall clock says it is newer than the spawn.
            102: process_tree.ProcessInfo(
                102, 1, 102, "proc:2", started_at=5100.0, started_since_boot=900.0
            ),
            # Describable but not datable in either clock: kept, as ever.
            103: process_tree.ProcessInfo(103, 1, 103, "proc:3"),
        }
        monkeypatch.setattr(process_tree, "live_pids", lambda: list(table))
        monkeypatch.setattr(process_tree, "process_info", table.get)

        candidates = set(process_group._lease_candidates(5000.0, 1000.0))

        assert 101 in candidates, (
            "a descendant born after the spawn was pruned on a stale wall clock"
        )
        assert 102 not in candidates, "a process older than the spawn survived the prune"
        assert 103 in candidates, "an undatable process must be kept, not dropped"

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="the composed-start path is Linux-only"
    )
    def test_a_stale_boot_epoch_cannot_hide_a_live_descendant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The observed CI failure, forced rather than waited for (#2689).

        ``process_tree`` caches ``/proc/stat``'s ``btime`` for the life of the
        worker and never invalidates it, so on a long ``-n auto`` gate run the
        cached boot epoch can fall out of the frame of a ``time.time()`` read
        taken much later. Seeding the cache an hour stale reproduces that on
        demand: the wall-clock comparison drops a descendant that is plainly
        alive and does not recover, because neither reading changes on a retry.
        The assertion is that the prune no longer consults that comparison.
        """
        real_btime = process_tree._boot_time_epoch()
        assert real_btime is not None, "sanity: /proc/stat should report btime on Linux"
        monkeypatch.setitem(process_tree._state, "btime", real_btime - 3600.0)

        env, lease = process_group.open_process_lease(dict(os.environ))
        assert lease.opened_since_boot is not None, "Linux must date a spawn against boot"
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Synchronise on the child being describable, not on elapsed time.
            assert _wait_until(lambda: process_tree.process_info(proc.pid) is not None)
            info = process_tree.process_info(proc.pid)
            assert info is not None and info.started_at is not None
            # The teeth: under the stale cache the wall clock genuinely misorders
            # this child, and the old comparison genuinely dropped it.
            assert info.started_at < lease.started_at - process_group._LEASE_AGE_SLACK_SECONDS
            assert proc.pid not in process_group._lease_candidates(lease.started_at)

            assert proc.pid in process_group._lease_candidates(
                lease.started_at, lease.opened_since_boot
            ), "the prune dropped a live descendant because a cached clock had drifted"
        finally:
            _reap(proc.pid)
            proc.wait(timeout=5)


def test_a_leaking_workspace_setup_command_is_recorded_and_named(tmp_path: Path) -> None:
    """Workspace setup is a project command too, so its leak reaches the record.

    ``setup_command`` runs whatever the project configured — ``uv sync``, an npm
    install, a build — in the fresh worktree, before any gate. Its workers were
    being killed at release with nothing in the run saying so, which is the same
    hole already closed for the gate, the dev verification commands and
    pre-validate (#2309). ``gate_run`` is 0 here because no gate has run yet;
    ``source`` is what keeps that from reading as a gate leak.
    """
    from theforge.coordinator.state import CoordinatorState
    from theforge.coordinator.validate_phase import (
        SHELL_WORKSPACE_SETUP,
        _record_gate_teardowns,
    )
    from theforge.coordinator.workspace import _run_setup_split

    pidfile = tmp_path / "setup.pid"
    teardowns: list[process_group.ProcessTeardown] = []
    ok, _out = _run_setup_split(_leaky_command(pidfile), tmp_path, None, teardown_out=teardowns)

    leaked = int(pidfile.read_text().strip())
    try:
        assert _wait_until(lambda: not _pid_alive(leaked)), (
            "a setup command's descendant outlived the run (#2309)"
        )
    finally:
        _reap(leaked)

    assert ok is True, "the setup command itself succeeded; only its leftovers died"
    assert teardowns, "the setup command's teardown never reached its caller"

    state = CoordinatorState()
    _record_gate_teardowns(state, teardowns, source=SHELL_WORKSPACE_SETUP)
    assert len(state.gate_process_teardowns) == 1
    recorded = state.gate_process_teardowns[0]
    assert recorded["source"] == SHELL_WORKSPACE_SETUP
    assert recorded["gate_run"] == 0, "no gate had run when setup executed"
    assert recorded["action"] == process_group.TEARDOWN_KILLED_SURVIVORS


def test_a_clean_workspace_setup_command_records_nothing(tmp_path: Path) -> None:
    """Absence keeps meaning something on this path too."""
    from theforge.coordinator.workspace import _run_setup_split

    teardowns: list[process_group.ProcessTeardown] = []
    ok, _out = _run_setup_split(
        f'{sys.executable} -c "print(1)"', tmp_path, None, teardown_out=teardowns
    )
    assert ok is True
    assert teardowns == []


def test_a_reused_worktree_setup_leak_reaches_the_run_record(tmp_path: Path) -> None:
    """Workspace creation has more than one setup call, and each must record.

    When ``git worktree add`` fails on a branch collision, creation recovers by
    reusing the registered worktree — and runs the project's setup command there.
    That call was the one branch of four not handed the collector, so a leak it
    caused was killed with nothing in the run saying so (#2309).

    The setup command is stood in for rather than really run, because reaching
    this branch requires driving ``git`` through mocked shells; what the stand-in
    does is fill the collector it was given, which is exactly the wiring the
    defect broke — the caller's list and the callee's must be the same object.
    """
    import dataclasses as _dc
    from unittest.mock import patch

    from theforge.coordinator.state import CoordinatorState
    from theforge.coordinator.validate_phase import (
        SHELL_WORKSPACE_SETUP,
        _record_gate_teardowns,
    )
    from theforge.coordinator.workspace import _create_workspace
    from theforge.task import TaskStory

    config = _audit_config(tmp_path)
    config = _dc.replace(
        config,
        workspace=_dc.replace(
            config.workspace,
            setup_command="pip install -e .",
            path_pattern=str(tmp_path / "{slug}"),
        ),
    )
    task = TaskStory(name="t", slug="test-task", story_path=tmp_path / "spec.md")
    # Registered against the colliding branch but at a different path, so the
    # workspace path itself does not exist and creation has to recover through
    # the collision branch rather than the ordinary reuse one.
    existing = tmp_path / "registered-elsewhere"
    existing.mkdir()

    leaked = process_group.ProcessTeardown(
        pgid=4242,
        action=process_group.TEARDOWN_KILLED_SURVIVORS,
        member_count=1,
        members=(4242,),
        sandbox_dir=str(existing),
        completed=True,
    )

    def _shell(cmd, cwd, **kwargs):  # type: ignore[no-untyped-def]
        if "worktree add" in cmd or cmd.startswith("mkdir"):
            return (False, "fatal: a branch named 'forge/test-task' already exists")
        if "branch --list" in cmd:
            return (True, "  forge/test-task")
        if "worktree list --porcelain" in cmd:
            return (True, f"worktree {existing}\nbranch refs/heads/forge/test-task\n")
        return (True, "")

    def _setup(  # type: ignore[no-untyped-def]
        _cmd, _path, _interp=None, *, timeout=120, teardown_out=None
    ):
        # The real thing appends to whatever collector it was handed; if the
        # caller never passed one, this is where the record is lost.
        assert timeout == config.workspace.setup_timeout
        assert teardown_out is not None, (
            "the reuse branch ran a setup command without a teardown collector"
        )
        teardown_out.append(leaked)
        return (True, "")

    collected: list[process_group.ProcessTeardown] = []
    with (
        patch("theforge.coordinator.workspace._cu._run_shell", side_effect=_shell),
        patch("theforge.coordinator.workspace._run_setup_split", side_effect=_setup),
        patch("theforge.coordinator.workspace._sync_run_forge_yaml"),
        patch("theforge.coordinator.workspace._rebase_reused_worktree", return_value=None),
        patch("theforge.coordinator.workspace._deindex_forge_artifacts"),
        patch("theforge.coordinator.workspace._propagate_claude_memory"),
        patch("theforge.coordinator.workspace.record_worktree_provenance"),
        patch("theforge.coordinator.workspace._judge_worktree_provenance", return_value=None),
    ):
        path, _branch, err = _create_workspace(config, task, no_pull=True, teardown_out=collected)

    assert err is None and path == existing, "the reuse branch was not the one exercised"
    assert collected == [leaked], "the teardown never reached the caller's collector"

    # And the engine files it exactly as it does for the other setup branches.
    state = CoordinatorState()
    _record_gate_teardowns(state, collected, source=SHELL_WORKSPACE_SETUP)
    assert len(state.gate_process_teardowns) == 1
    assert state.gate_process_teardowns[0]["source"] == SHELL_WORKSPACE_SETUP
    assert state.gate_process_teardowns[0]["action"] == process_group.TEARDOWN_KILLED_SURVIVORS
