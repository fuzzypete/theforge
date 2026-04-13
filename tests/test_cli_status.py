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
