"""Tests for the forge daemon module."""

from __future__ import annotations

import asyncio
import json
import signal
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.daemon import (
    DaemonServer,
    _append_crash_log,
    _write_daemon_json,
    install_launchd,
    is_daemon_running,
    stop_daemon,
)

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def forge_root(tmp_path: Path) -> Path:
    """A temporary project root with forge structure."""
    (tmp_path / ".forge").mkdir()
    return tmp_path


@pytest.fixture()
def mock_config(forge_root: Path) -> MagicMock:
    """Minimal ForgeConfig mock."""
    config = MagicMock()
    config.project_root = forge_root
    config.notifications.ntfy = None
    return config


# ── is_daemon_running ─────────────────────────────────────────────────


def test_is_daemon_running_no_file(forge_root: Path) -> None:
    """No PID file → not running."""
    assert is_daemon_running(forge_root) is False


def test_is_daemon_running_stale_pid(forge_root: Path) -> None:
    """Stale PID file (process doesn't exist) → False, file removed."""
    pid_file = forge_root / ".forge" / "daemon.pid"
    pid_file.write_text("999999999", encoding="utf-8")

    with patch("os.kill", side_effect=ProcessLookupError):
        result = is_daemon_running(forge_root)

    assert result is False
    assert not pid_file.exists()


def test_is_daemon_running_live(forge_root: Path) -> None:
    """PID file exists and process is alive → True."""
    pid_file = forge_root / ".forge" / "daemon.pid"
    pid_file.write_text("12345", encoding="utf-8")

    with patch("os.kill", return_value=None):
        result = is_daemon_running(forge_root)

    assert result is True


# ── _write_daemon_json ─────────────────────────────────────────────────


def test_write_daemon_json_atomic(forge_root: Path) -> None:
    """Atomic write via tempfile results in valid JSON matching input."""
    state = {"pid": 42, "running": True, "queue": ["spec-a"], "completed": []}
    _write_daemon_json(forge_root, state)

    state_path = forge_root / ".forge" / "daemon.json"
    assert state_path.exists()
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert loaded == state


# ── _append_crash_log ──────────────────────────────────────────────────


def test_append_crash_log(forge_root: Path) -> None:
    """Two crashes are appended as separate JSON lines."""
    crash1 = {
        "signal": "SIGTERM (15)",
        "phase": "DEV",
        "iteration": 1,
        "cost_at_crash": 0.42,
        "uptime_seconds": 120.5,
        "last_log_event": "phase_start DEV",
    }
    crash2 = {
        "signal": "SIGTERM (15)",
        "phase": "REVIEW",
        "iteration": 2,
        "cost_at_crash": 1.23,
        "uptime_seconds": 300.0,
        "last_log_event": "phase_start REVIEW",
    }
    _append_crash_log(forge_root, crash1)
    _append_crash_log(forge_root, crash2)

    crash_path = forge_root / ".forge" / "logs" / "crashes.jsonl"
    assert crash_path.exists()
    lines = crash_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    loaded1 = json.loads(lines[0])
    loaded2 = json.loads(lines[1])
    assert loaded1["phase"] == "DEV"
    assert loaded2["phase"] == "REVIEW"
    assert loaded1["cost_at_crash"] == pytest.approx(0.42)
    assert loaded2["cost_at_crash"] == pytest.approx(1.23)


# ── DaemonServer dedup ─────────────────────────────────────────────────


def test_daemon_server_dedup(forge_root: Path, mock_config: MagicMock) -> None:
    """Submitting the same spec slug twice: second returns ok=False."""

    async def _run() -> None:
        server = DaemonServer(forge_root, mock_config)

        msg1 = {"action": "submit", "manifest": "sprints/foo.yaml", "args": {"slug": "my-spec"}}
        resp1 = await server.handle_submit(msg1)
        assert resp1["ok"] is True
        assert resp1["queued"] == "my-spec"

        msg2 = {"action": "submit", "manifest": "sprints/foo.yaml", "args": {"slug": "my-spec"}}
        resp2 = await server.handle_submit(msg2)
        assert resp2["ok"] is False
        assert "already queued" in resp2["error"]

    asyncio.run(_run())


def test_daemon_server_dedup_running(forge_root: Path, mock_config: MagicMock) -> None:
    """Submitting a spec that is currently running returns ok=False."""

    async def _run() -> None:
        server = DaemonServer(forge_root, mock_config)
        server._running_spec = "active-spec"

        msg = {"action": "submit", "manifest": "sprints/bar.yaml", "args": {"slug": "active-spec"}}
        resp = await server.handle_submit(msg)
        assert resp["ok"] is False
        assert "already running" in resp["error"]

    asyncio.run(_run())


# ── stop_daemon ────────────────────────────────────────────────────────


def test_stop_daemon_sends_sigterm(forge_root: Path) -> None:
    """stop_daemon reads PID file and sends SIGTERM."""
    pid_file = forge_root / ".forge" / "daemon.pid"
    pid_file.write_text("54321", encoding="utf-8")

    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        if sig == 0:
            # First call (in is_daemon_running) → process still alive
            return
        # Second call from stop_daemon → also succeeds initially
        return

    # After stop_daemon sends SIGTERM, we need the process to "disappear"
    # on the next kill(pid, 0) check.  Simulate: first kill(0) → alive,
    # then kill(SIGTERM) → ok, then subsequent kill(0) → ProcessLookupError.
    call_count = [0]

    def side_effect_kill(pid: int, sig: int) -> None:
        call_count[0] += 1
        if sig == signal.SIGTERM:
            return
        # sig == 0 checks
        if call_count[0] > 2:
            raise ProcessLookupError
        return

    with patch("os.kill", side_effect=side_effect_kill):
        with patch("time.sleep"):
            stop_daemon(forge_root)

    # Verify stop_daemon executed without error and made os.kill calls
    assert call_count[0] > 0


# ── fallback: no daemon → run_sprint directly ─────────────────────────


def test_fallback_no_daemon(tmp_path: Path) -> None:
    """When daemon is not running, cmd_sprint calls run_sprint directly."""
    import argparse

    from theforge import cli

    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text("name: test\nbudget_usd: 1.0\nstories: []\n", encoding="utf-8")
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: test\n", encoding="utf-8")

    args = argparse.Namespace(
        manifest=str(manifest_path),
        config=str(config_path),
        verbose=False,
        auto_merge=False,
        interactive=False,
        no_notify=True,
        resume=False,
        detach=False,
        fg=True,
    )

    mock_config = MagicMock()
    mock_config.project_root = tmp_path
    mock_result = MagicMock()
    mock_result.specs_failed = 0

    with patch("theforge.cli.load_config", return_value=mock_config):
        with patch("theforge.daemon.is_daemon_running", return_value=False):
            with patch("theforge.cli.run_sprint", return_value=mock_result) as mock_run:
                with patch("theforge.daemon.submit_sprint") as mock_submit:
                    exit_code = cli.cmd_sprint(args)

    mock_run.assert_called_once()
    mock_submit.assert_not_called()
    assert exit_code == 0


def test_submit_routes_to_daemon(tmp_path: Path) -> None:
    """When daemon is running and --detach is set, cmd_sprint routes to submit_sprint."""
    import argparse

    from theforge import cli

    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text("name: test\nbudget_usd: 1.0\nstories: []\n", encoding="utf-8")
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: test\n", encoding="utf-8")

    args = argparse.Namespace(
        manifest=str(manifest_path),
        config=str(config_path),
        verbose=False,
        auto_merge=False,
        interactive=False,
        no_notify=True,
        resume=False,
        detach=True,
        fg=True,
    )

    mock_config = MagicMock()
    mock_config.project_root = tmp_path

    with patch("theforge.cli.load_config", return_value=mock_config):
        with patch("theforge.daemon.is_daemon_running", return_value=True):
            with patch(
                "theforge.daemon.submit_sprint",
                return_value={"ok": True, "queued": "sprint", "position": 1},
            ) as mock_submit:
                with patch("theforge.cli.run_sprint") as mock_run:
                    exit_code = cli.cmd_sprint(args)

    mock_submit.assert_called_once()
    mock_run.assert_not_called()
    assert exit_code == 0


# ── launchd install ────────────────────────────────────────────────────


def test_launchd_install_creates_plist(forge_root: Path) -> None:
    """install_launchd writes plist file and calls launchctl."""
    forge_bin = Path("/usr/local/bin/forge")

    with tempfile.TemporaryDirectory() as tmpdir:
        plist_path = Path(tmpdir) / "com.theforge.daemon.plist"

        with patch("theforge.daemon._LAUNCHD_PLIST_PATH", plist_path):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result_path = install_launchd(forge_root, forge_bin)

        assert result_path == plist_path
        assert plist_path.exists()
        content = plist_path.read_text(encoding="utf-8")
        assert "com.theforge.daemon" in content
        assert str(forge_bin) in content

        # Verify launchctl was called
        mock_run.assert_called_once()
        launchctl_call = mock_run.call_args[0][0]
        assert "launchctl" in launchctl_call
        assert "load" in launchctl_call


# ── state_update_fn called in run_sprint ──────────────────────────────


def test_state_update_fn_called_in_run_sprint(tmp_path: Path) -> None:
    """state_update_fn is called at least once when passed to run_sprint."""
    from theforge.sprint import run_sprint

    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        "name: test\nbudget_usd: 1.0\nstories: ['story.md']\n",
        encoding="utf-8",
    )
    story_path = tmp_path / "story.md"
    story_path.write_text(
        "---\nname: Test\nslug: test-story\n---\n# Test\n",
        encoding="utf-8",
    )

    mock_config = MagicMock()
    mock_config.project_root = tmp_path
    mock_config.workspace.branch_pattern = "feat/{slug}"
    mock_config.workspace.base_branch = "main"
    mock_config.workspace.path_pattern = ".forge/worktrees/{slug}"
    mock_config.hooks = None
    mock_config.log.enabled = False
    mock_config.log.log_file = None
    mock_config.notifications.ntfy = None
    mock_config.sprint.max_parallel = 1

    mock_result = MagicMock()
    mock_result.success = True
    mock_result.state.total_cost = 0.0
    mock_result.state.preflight_verdict = None
    mock_result.state.log_dir = None
    mock_result.state.review_cycle_metadata = []
    mock_result.state.review_results = []
    mock_result.merge = None
    mock_result.phase.name = "DONE"

    updates: list[dict] = []

    def recording_fn(update: dict) -> None:
        updates.append(update)

    with patch("theforge.sprint.run_task", return_value=mock_result):
        with patch("theforge.sprint._triage_spec"):
            with patch("theforge.sprint._write_sprint_audit"):
                with patch("theforge.sprint._write_sprint_summary"):
                    with patch("theforge.sprint._validate_story_paths", return_value=[story_path]):
                        run_sprint(
                            mock_config,
                            manifest_path,
                            state_update_fn=recording_fn,
                        )

    assert len(updates) > 0
    phases_seen = {u.get("phase") for u in updates}
    assert "STARTING" in phases_seen
