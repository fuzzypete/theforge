"""Lifecycle tests for process-group isolation and the orphan reaper.

Per the runner-lifecycle invariant these exercise *real* subprocesses via
``tests/fake_bin/`` fake CLIs, not ``Popen`` mocks — a mock cannot show that a
group kill reaches grandchildren (node/tool leaf) that a bare ``proc.kill()``
would leave alive.
"""

from __future__ import annotations

import json
import os
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

_FAKE_BIN = Path(__file__).parent / "fake_bin"


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


class TestReapOrphanAgents:
    def _write_sidecar(
        self, project_root: Path, owner_pid: int, pgid: int, sandbox: str | None = None
    ) -> Path:
        agents_dir = project_root / ".forge" / "runs" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        path = agents_dir / f"{owner_pid}-{pgid}.json"
        path.write_text(
            json.dumps(
                {
                    "owner_pid": owner_pid,
                    "pgid": pgid,
                    "run_id": "run-test",
                    "sandbox_dir": sandbox,
                }
            ),
            encoding="utf-8",
        )
        return path

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

    def test_register_is_noop_without_project_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FORGE_PROJECT_ROOT", raising=False)
        # Must not raise and must not create anything.
        process_group.register_agent_group(1234, sandbox_dir=tmp_path)
        process_group.unregister_agent_group(1234)
        assert not (tmp_path / ".forge").exists()


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
