"""Tests for forge status routing: sprint detection, run resolution, flags."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)


def _make_forge_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=300,
            allowed_tools=("Read",),
        ),
        preflight_profile=ModelProfile(
            name="preflight",
            cli="claude",
            model="sonnet",
            budget_usd=0.5,
            timeout_seconds=120,
            allowed_tools=("Read",),
        ),
        review_pool=[],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig(enabled=False),
        log=LogConfig(enabled=False),
    )


# ── TestCmdStatusRouting ──────────────────────────────────────────────────────


class TestCmdStatusRouting:
    def test_shows_no_active_runs(self, tmp_path: Path, capsys: object) -> None:
        """When no active runs and no recent runs, prints appropriate message."""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=False, last=False)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._find_active_run_id", return_value=None),
            patch("theforge.cli.status._find_most_recent_run", return_value=None),
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            result = cmd_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "No active or recent runs found" in captured.out

    def test_shows_single_run_status_for_active_run(self, tmp_path: Path, capsys: object) -> None:
        """Active non-sprint run displays single-run status table."""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=False, last=False)

        mock_status = {"phase": "DEV", "cost_usd": 1.23, "elapsed_seconds": 300, "log_path": None}

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._find_active_run_id", return_value="abc123ef"),
            patch("theforge.cli.status._is_sprint_run", return_value=False),
            patch("theforge.detach.read_run_status", return_value=mock_status),
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            result = cmd_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "abc123ef" in captured.out
        assert "DEV" in captured.out

    def test_shows_sprint_status_for_active_sprint(self, tmp_path: Path, capsys: object) -> None:
        """Active sprint run delegates to display_sprint_status."""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=False, last=False)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._find_active_run_id", return_value="sprint-run-1"),
            patch("theforge.cli.status._is_sprint_run", return_value=True),
            patch("theforge.cli.sprint_status.display_sprint_status", return_value=0) as mock_dss,
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            result = cmd_status(args)

        assert result == 0
        mock_dss.assert_called_once_with("sprint-run-1", config.project_root)

    def test_falls_back_to_historical_sprint_when_no_active(
        self, tmp_path: Path, capsys: object
    ) -> None:
        """When no active run, falls back to most recent historical run (sprint)."""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=False, last=False)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._find_active_run_id", return_value=None),
            patch("theforge.cli.status._find_most_recent_run", return_value=("hist-run-7", True)),
            patch("theforge.cli.status._is_sprint_run", return_value=True),
            patch("theforge.cli.sprint_status.display_sprint_status", return_value=0) as mock_dss,
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            result = cmd_status(args)

        assert result == 0
        mock_dss.assert_called_once_with("hist-run-7", config.project_root)

    def test_explicit_run_id_resolves_and_shows_status(
        self, tmp_path: Path, capsys: object
    ) -> None:
        """forge status <run-id> resolves specific run and shows its status."""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id="explicit-run", recent=False, last=False)

        mock_status = {
            "phase": "REVIEW",
            "cost_usd": 0.50,
            "elapsed_seconds": 120,
            "log_path": None,
        }

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._resolve_run_id", return_value=True),
            patch("theforge.cli.status._is_sprint_run", return_value=False),
            patch("theforge.detach.read_run_status", return_value=mock_status),
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            result = cmd_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "explicit-run" in captured.out

    def test_explicit_run_id_not_found_returns_error(self, tmp_path: Path, capsys: object) -> None:
        """forge status <unknown-run-id> returns exit code 1."""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id="no-such-run", recent=False, last=False)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._resolve_run_id", return_value=False),
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            result = cmd_status(args)

        assert result == 1

    def test_last_flag_shows_most_recent_completed(self, tmp_path: Path, capsys: object) -> None:
        """forge status --last shows the most recent completed run."""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=False, last=True)

        mock_status = {
            "phase": "DONE",
            "cost_usd": 2.50,
            "elapsed_seconds": 600,
            "log_path": None,
        }

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch(
                "theforge.cli.status._find_most_recent_run",
                return_value=("last-run-99", False),
            ),
            patch("theforge.cli.status._is_sprint_run", return_value=False),
            patch("theforge.detach.read_run_status", return_value=mock_status),
            patch("theforge.pending.cleanup_stale"),
            patch("theforge.pending.list_pending", return_value=[]),
        ):
            result = cmd_status(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "last-run-99" in captured.out

    def test_recent_flag_shows_compact_list(self, tmp_path: Path, capsys: object) -> None:
        """forge status --recent shows compact run list."""
        from theforge.cli import cmd_status

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=None, recent=True, last=False)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status._show_recent_runs", return_value=0) as mock_recent,
        ):
            result = cmd_status(args)

        assert result == 0
        mock_recent.assert_called_once_with(config.project_root)


# ── Regression: forge stop writes .ended; forge status shows STOPPED ─────────


class TestStopWritesTerminalMarker:
    def test_stopped_run_shows_stopped_not_running(self, tmp_path: Path) -> None:
        """Regression: after forge stop, forge status shows STOPPED not RUNNING.

        Simulates SIGTERM stop: writes .ended 'stopped', removes PID file.
        Then verifies read_run_status returns STOPPED, not RUNNING.
        """
        from theforge import detach

        # Set up a log file so read_run_status has something to work with.
        log_dir = tmp_path / ".forge" / "logs" / "issues-828"
        log_dir.mkdir(parents=True)
        (log_dir / "run-6f59d22b.log").write_text("✓ PREFLIGHT PROCEED\n")

        # Simulate what forge stop does: write .ended, no PID file present.
        detach.write_run_ended("6f59d22b", tmp_path, "stopped", force=True)

        st = detach.read_run_status("6f59d22b", "issues-828", tmp_path)
        assert st["phase"] == "STOPPED", f"Expected STOPPED, got {st['phase']!r}"

    def test_orphaned_run_shows_orphaned_not_running(self, tmp_path: Path) -> None:
        """Regression: a run that died silently shows ORPHANED, not RUNNING.

        Simulates a silent crash: log file exists, no PID file, no .ended.
        forge status should detect this as ORPHANED.
        """
        from theforge import detach

        # Set up a log file — process wrote some output but no terminal marker.
        log_dir = tmp_path / ".forge" / "logs" / "issues-828"
        log_dir.mkdir(parents=True)
        (log_dir / "run-deadbeef.log").write_text("✓ PREFLIGHT PROCEED\n")

        # No PID file (stale PID cleaned up by list_active_runs).
        # No .ended file (process crashed without writing one).

        st = detach.read_run_status("deadbeef", "issues-828", tmp_path)
        assert st["phase"] == "ORPHANED", f"Expected ORPHANED, got {st['phase']!r}"


# ── Race window: run starts between scans ────────────────────────────────────


class TestShowRecentRunsRaceWindow:
    """_show_recent_runs must not label a live run as orphaned.

    Regression for: a run that starts between list_active_runs and the log
    file scan would have a .pid file but no .ended file and should be shown
    as 'active', not 'orphaned'.
    """

    def test_run_with_pid_file_but_no_ended_shows_active(
        self, tmp_path: Path, capsys: object
    ) -> None:
        from theforge.cli.status import _show_recent_runs

        # Create a log file so the run appears in the historical scan.
        log_dir = tmp_path / ".forge" / "logs" / "issues-test"
        log_dir.mkdir(parents=True)
        (log_dir / "run-abcd1234.log").write_text("starting\n")

        # Simulate a PID file present (run started after initial active scan).
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "abcd1234.pid").write_text("99999\nissues-test\n")

        # No .ended file — process is alive but appeared after list_active_runs.
        with patch("theforge.detach.list_active_runs", return_value=[]):
            _show_recent_runs(tmp_path)

        out = capsys.readouterr().out
        assert "active" in out, f"Expected 'active' in output, got:\n{out}"
        assert "orphaned" not in out, f"Unexpected 'orphaned' in output:\n{out}"

    def test_run_without_pid_file_and_no_ended_shows_orphaned(
        self, tmp_path: Path, capsys: object
    ) -> None:
        from theforge.cli.status import _show_recent_runs

        # Create a log file so the run appears in the historical scan.
        log_dir = tmp_path / ".forge" / "logs" / "issues-test"
        log_dir.mkdir(parents=True)
        (log_dir / "run-dead1234.log").write_text("starting\n")

        # No PID file, no .ended file — truly orphaned.
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)

        with patch("theforge.detach.list_active_runs", return_value=[]):
            _show_recent_runs(tmp_path)

        out = capsys.readouterr().out
        assert "orphaned" in out, f"Expected 'orphaned' in output, got:\n{out}"


# ── Parser-level coverage ─────────────────────────────────────────────────────


class TestIsSprintRun:
    def test_detects_sprint_via_state_file(self, tmp_path: Path) -> None:
        from theforge.cli.status import _is_sprint_run

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "abc123.state").write_text("sprint_name: x\nstories: []\n")
        assert _is_sprint_run("abc123", tmp_path) is True

    def test_returns_false_for_non_sprint_run(self, tmp_path: Path) -> None:
        from theforge.cli.status import _is_sprint_run

        (tmp_path / ".forge" / "runs").mkdir(parents=True)
        with patch("theforge.sprint.status_reader.find_sprint_summary", return_value=None):
            assert _is_sprint_run("abc123", tmp_path) is False

    def test_detects_sprint_via_redirect_during_reexec_startup_window(
        self, tmp_path: Path
    ) -> None:
        """During the window after .pid is written but before .state exists, a
        .redirect from a sprint predecessor run identifies the new run as a sprint."""
        import json

        from theforge.cli.status import _is_sprint_run

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "oldrun.redirect").write_text(
            json.dumps({"new_run_id": "newrun", "new_log": "/tmp/x.log"})
        )
        # Predecessor has a .state file — it was a sprint
        (runs_dir / "oldrun.state").write_text("sprint_name: x\nstories: []\n")
        with patch("theforge.sprint.status_reader.find_sprint_summary", return_value=None):
            assert _is_sprint_run("newrun", tmp_path) is True

    def test_redirect_from_single_run_reexec_is_not_misclassified_as_sprint(
        self, tmp_path: Path
    ) -> None:
        """A forge run re-exec produces a .redirect but no predecessor .state;
        _is_sprint_run must return False so status falls back to the single-run view."""
        import json

        from theforge.cli.status import _is_sprint_run

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "oldrun.redirect").write_text(
            json.dumps({"new_run_id": "newrun", "new_log": "/tmp/x.log"})
        )
        # No oldrun.state — predecessor was not a sprint
        with patch("theforge.sprint.status_reader.find_sprint_summary", return_value=None):
            assert _is_sprint_run("newrun", tmp_path) is False

    def test_redirect_for_different_run_does_not_match(self, tmp_path: Path) -> None:
        import json

        from theforge.cli.status import _is_sprint_run

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "oldrun.redirect").write_text(
            json.dumps({"new_run_id": "otherid", "new_log": "/tmp/x.log"})
        )
        (runs_dir / "oldrun.state").write_text("sprint_name: x\nstories: []\n")
        with patch("theforge.sprint.status_reader.find_sprint_summary", return_value=None):
            assert _is_sprint_run("newrun", tmp_path) is False


def test_sprint_status_absent_from_cli_parser() -> None:
    """forge sprint-status must not appear in the built CLI parser."""
    from theforge.cli.main import build_parser

    parser = build_parser()
    try:
        parser.parse_args(["sprint-status", "some-run-id"])
        raise AssertionError("Expected SystemExit for removed sprint-status command")
    except SystemExit:
        pass  # argparse exits on unrecognised subcommand — expected
