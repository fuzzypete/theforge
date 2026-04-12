"""Tests for forge logs subcommand and _follow_log_with_redirect helper."""

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


def _api_profile(
    name: str, provider: str = "anthropic", model: str = "claude-opus-4-6"
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=("Read", "Grep"),
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
        review_pool=[
            _api_profile("claude-reviewer"),
            _api_profile("codex-reviewer", "openai"),
        ],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig(enabled=False),
        log=LogConfig(enabled=False),
    )


# ── TestCmdLogs ───────────────────────────────────────────────────────


class TestCmdLogs:
    def test_tails_log_file_for_known_run(self, tmp_path):
        """forge logs <run-id> tails the correct log file and exits cleanly."""
        from theforge.cli import cmd_logs

        run_id = "abc123"
        slug = "my-slug"
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.pid").write_text(f"12345\n{slug}\n")
        log_dir = tmp_path / ".forge" / "logs" / slug
        log_dir.mkdir(parents=True)
        log_file = log_dir / "run.log"
        log_file.write_text("hello\n[forge test sentinel EOF]\n")

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            result = cmd_logs(args)

        assert result == 0

    def test_follows_reexec_redirect(self, tmp_path, capsys):
        """forge logs follows a re-exec redirect to the successor log."""
        from theforge.cli import cmd_logs
        from theforge.cli.status import _SENTINEL_EOF

        old_run_id = "aabbccddee11"
        new_run_id = "ff99887766aa"
        slug = "my-slug"

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{old_run_id}.pid").write_text(f"12345\n{slug}\n")

        log_dir = tmp_path / ".forge" / "logs" / slug
        log_dir.mkdir(parents=True)

        new_log = log_dir / f"run-{new_run_id}.log"
        new_log.write_text(f"new run output\n{_SENTINEL_EOF}\n")

        old_log = log_dir / f"run-{old_run_id}.log"
        old_log.write_text(
            "old run output\n"
            "[forge] Run started in background\n"
            f"[forge] Run ID:  {new_run_id}\n"
            f"[forge] Log:     {new_log}\n"
            f"[forge] Logs:    forge logs {new_run_id}\n"
        )

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=old_run_id)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            result = cmd_logs(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "old run output" in captured.out
        assert "new run output" in captured.out
        assert f"Run re-exec'd — following new run {new_run_id}" in captured.err

    def test_no_redirect_stays_on_original_log(self, tmp_path, capsys):
        """_follow_log_with_redirect returns None when no re-exec trailer present."""
        from theforge.cli.status import _SENTINEL_EOF, _follow_log_with_redirect

        log_file = tmp_path / "run.log"
        log_file.write_text(f"line one\nline two\n{_SENTINEL_EOF}\n")

        result = _follow_log_with_redirect(log_file, "aabbccddee11")

        assert result is None
        captured = capsys.readouterr()
        assert "line one" in captured.out
        assert "line two" in captured.out

    def test_returns_error_when_no_pid_and_no_log(self, tmp_path):
        """forge logs with unknown run_id returns error."""
        from theforge.cli import cmd_logs

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id="deadbeef")

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            result = cmd_logs(args)

        assert result == 1
