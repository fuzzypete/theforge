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
        """forge logs follows a re-exec redirect via sidecar file to the successor log."""
        import json

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
        old_log.write_text("old run output\n")

        # Write the sidecar redirect file (normally written by the new daemon's
        # grandchild after re-exec; here we write it directly for the test).
        (runs_dir / f"{old_run_id}.redirect").write_text(
            json.dumps({"new_run_id": new_run_id, "new_log": str(new_log)}),
            encoding="utf-8",
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

    def test_reexec_redirect_new_log_never_appears(self, tmp_path, capsys):
        """forge logs returns error gracefully when the new log never appears after re-exec."""
        import json
        from unittest.mock import patch

        from theforge.cli import cmd_logs

        old_run_id = "aabbccddee11"
        new_run_id = "ff99887766aa"
        slug = "my-slug"

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{old_run_id}.pid").write_text(f"12345\n{slug}\n")

        log_dir = tmp_path / ".forge" / "logs" / slug
        log_dir.mkdir(parents=True)

        # The new log path is referenced in the sidecar redirect file but never created.
        new_log = log_dir / f"run-{new_run_id}.log"

        old_log = log_dir / f"run-{old_run_id}.log"
        old_log.write_text("old run output\n")

        # Write sidecar redirect file pointing to a non-existent new log.
        (runs_dir / f"{old_run_id}.redirect").write_text(
            json.dumps({"new_run_id": new_run_id, "new_log": str(new_log)}),
            encoding="utf-8",
        )

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=old_run_id)

        # Patch time.sleep so the wait loop runs instantly.
        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
            patch("theforge.cli.status.time.sleep"),
        ):
            result = cmd_logs(args)

        assert result == 1
        captured = capsys.readouterr()
        assert "Timed out waiting for new log" in captured.err

    def test_same_log_reexec_no_duplicate_lines(self, tmp_path, capsys):
        """Re-exec that redirects to the same log file must not replay prior lines."""
        import json

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

        # Single log file used by both the old and new run (same path, new run ID).
        log_file = log_dir / f"run-{old_run_id}.log"
        log_file.write_text(f"before reexec\nafter reexec\n{_SENTINEL_EOF}\n")

        # Redirect sidecar points back to the SAME log file.
        (runs_dir / f"{old_run_id}.redirect").write_text(
            json.dumps({"new_run_id": new_run_id, "new_log": str(log_file)}),
            encoding="utf-8",
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
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        # Each log line must appear exactly once — no replay on re-exec.
        assert lines.count("before reexec") == 1, f"duplicated 'before reexec': {lines}"
        assert lines.count("after reexec") == 1, f"duplicated 'after reexec': {lines}"

    def test_spoof_log_lines_do_not_trigger_redirect(self, tmp_path, capsys):
        """Log lines that look like re-exec trailers must NOT trigger a redirect.

        LLM output printed to the log could contain ``[forge] Run ID:`` and
        ``[forge] Log:`` lines.  The new sidecar-file mechanism ensures these
        are ignored — a redirect only happens when forge itself writes the
        ``.redirect`` file.
        """
        from theforge.cli import cmd_logs
        from theforge.cli.status import _SENTINEL_EOF

        old_run_id = "aabbccddee11"
        spoof_run_id = "deadbeefcafe"
        slug = "my-slug"

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{old_run_id}.pid").write_text(f"12345\n{slug}\n")

        log_dir = tmp_path / ".forge" / "logs" / slug
        log_dir.mkdir(parents=True)

        spoof_log = log_dir / f"run-{spoof_run_id}.log"
        # Do NOT create the spoof log — if redirect happens it would fail anyway.

        old_log = log_dir / f"run-{old_run_id}.log"
        old_log.write_text(
            "legitimate output\n"
            f"[forge] Run ID:  {spoof_run_id}\n"
            f"[forge] Log:     {spoof_log}\n"
            f"{_SENTINEL_EOF}\n"
        )

        # No .redirect sidecar file is written — these are just spoofed log lines.

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=old_run_id)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            result = cmd_logs(args)

        # The follower must stop at the sentinel EOF without switching logs.
        assert result == 0
        captured = capsys.readouterr()
        assert "legitimate output" in captured.out
        assert "Run re-exec'd" not in captured.err

    def test_story_tail_reads_nested_sprint_log(self, tmp_path, capsys):
        """forge logs <run> --story <slug> tails that story's nested run log."""
        from theforge.cli import cmd_logs
        from theforge.cli.status import _SENTINEL_EOF

        run_id = "sprintrun01"
        sprint_name = "my-sprint"
        slug = "story-a"

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.state").write_text(
            f"sprint_name: {sprint_name}\n"
            "sprint_id: sid1\n"
            "stories:\n"
            f"  - slug: {slug}\n"
            "    status: running\n"
            "    phase: DEV\n",
            encoding="utf-8",
        )

        story_dir = tmp_path / ".forge" / "logs" / sprint_name / slug
        story_dir.mkdir(parents=True)
        (story_dir / f"run-{run_id}.log").write_text(
            f"story-a dev output\n{_SENTINEL_EOF}\n", encoding="utf-8"
        )

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id, story=slug)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            result = cmd_logs(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "story-a dev output" in captured.out
        assert slug in captured.err  # "Tailing .../story-a/run-...log"

    def test_story_tail_picks_most_recent_log(self, tmp_path, capsys):
        """Across re-exec generations, --story tails the newest run log."""
        import os

        from theforge.cli import cmd_logs
        from theforge.cli.status import _SENTINEL_EOF

        run_id = "sprintrun01"
        sprint_name = "my-sprint"
        slug = "story-a"

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.state").write_text(
            f"sprint_name: {sprint_name}\nstories:\n  - slug: {slug}\n",
            encoding="utf-8",
        )

        story_dir = tmp_path / ".forge" / "logs" / sprint_name / slug
        story_dir.mkdir(parents=True)
        old_log = story_dir / "run-oldgen.log"
        old_log.write_text(f"old generation\n{_SENTINEL_EOF}\n", encoding="utf-8")
        new_log = story_dir / "run-newgen.log"
        new_log.write_text(f"new generation\n{_SENTINEL_EOF}\n", encoding="utf-8")
        # Make new_log clearly newer than old_log.
        os.utime(old_log, (1_000_000, 1_000_000))
        os.utime(new_log, (2_000_000, 2_000_000))

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id, story=slug)

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            result = cmd_logs(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "new generation" in captured.out
        assert "old generation" not in captured.out

    def test_story_tail_missing_log_returns_error(self, tmp_path, capsys):
        """--story with a slug that has no run log yet returns an error hint."""
        from theforge.cli import cmd_logs

        run_id = "sprintrun01"
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.state").write_text(
            "sprint_name: my-sprint\nstories:\n  - slug: story-a\n",
            encoding="utf-8",
        )

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id=run_id, story="never-ran")

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            result = cmd_logs(args)

        assert result == 1
        captured = capsys.readouterr()
        assert "never-ran" in captured.err

    def test_story_list_mode_shows_slugs_and_phases(self, tmp_path, capsys):
        """forge logs <run> --story (no arg) lists stories with their phases."""
        from theforge.cli import cmd_logs

        run_id = "sprintrun01"
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.state").write_text(
            "sprint_name: my-sprint\n"
            "stories:\n"
            "  - slug: story-a\n"
            "    status: running\n"
            "    phase: DEV\n"
            "  - slug: story-b\n"
            "    status: running\n"
            "    phase: REVIEW\n",
            encoding="utf-8",
        )

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        # const="" — argparse yields "" when --story is passed with no value.
        args = argparse.Namespace(run_id=run_id, story="")

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            result = cmd_logs(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "story-a" in captured.out
        assert "DEV" in captured.out
        assert "story-b" in captured.out
        assert "REVIEW" in captured.out

    def test_story_list_mode_no_live_stories_errors(self, tmp_path, capsys):
        """--story listing returns error when there is no live sprint state."""
        from theforge.cli import cmd_logs

        forge_yaml = tmp_path / "forge.yaml"
        forge_yaml.write_text("project:\n  root: .\n")
        config = _make_forge_config(tmp_path)
        args = argparse.Namespace(run_id="nostate", story="")

        with (
            patch("theforge.cli.status._find_config", return_value=forge_yaml),
            patch("theforge.cli.status.load_config", return_value=config),
        ):
            result = cmd_logs(args)

        assert result == 1
        captured = capsys.readouterr()
        assert "No live sprint stories" in captured.err

    def test_reflexive_redirect_does_not_emit_reexec_notice(self, tmp_path, capsys):
        """A redirect file pointing to the current run id must not trigger a re-exec notice.

        This is the bug from issue #1447: when the redirect file's new_run_id equals
        the currently-followed run id, _follow_log_with_redirect must ignore it and
        continue tailing rather than returning a tuple that causes the outer loop to
        emit the notice repeatedly.
        """
        import json

        from theforge.cli.status import _SENTINEL_EOF, _follow_log_with_redirect

        run_id = "fea6776bee4a"
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)

        log_file = tmp_path / f"run-{run_id}.log"
        log_file.write_text(f"sprint output line\n{_SENTINEL_EOF}\n")

        # Redirect file points reflexively back to the same run id.
        (runs_dir / f"{run_id}.redirect").write_text(
            json.dumps({"new_run_id": run_id, "new_log": str(log_file)}),
            encoding="utf-8",
        )

        result = _follow_log_with_redirect(log_file, run_id, runs_dir=runs_dir)

        assert result is None, "reflexive redirect must not be treated as a re-exec"
        captured = capsys.readouterr()
        assert "sprint output line" in captured.out
