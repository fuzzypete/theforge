"""Seam-level integration test for the detach Popen re-exec model.

Spans CLI (cmd_sprint / cmd_run) → detach (daemonize_run /
is_detached_child / setup_detached_child) boundary. Covers the cross-phase
state flow introduced by replacing os.fork()-based daemonization with a
subprocess.Popen re-exec on macOS fork-safety grounds (issue #1374).
"""

from __future__ import annotations

import argparse
import sys
from unittest.mock import MagicMock, patch

from theforge import detach

# ── daemonize_run uses Popen, not fork ─────────────────────────────────


def test_daemonize_run_uses_popen_with_detached_env(tmp_path):
    """daemonize_run must spawn via subprocess.Popen with FORGE_DETACHED=1
    and never invoke os.fork — that was the macOS crash trigger.
    """
    with (
        patch("theforge.detach.subprocess.Popen") as mock_popen,
        patch("theforge.detach.sys.exit") as mock_exit,
        patch(
            "theforge.detach.os.fork",
            side_effect=AssertionError("os.fork must not be called by daemonize_run"),
        ),
    ):
        mock_popen.return_value = MagicMock(pid=12345)
        detach.daemonize_run("run-seam", "slug-seam", tmp_path)

    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    cmd = args[0]
    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "theforge.cli"]
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["FORGE_DETACHED"] == "1"
    assert kwargs["env"]["FORGE_DETACHED_RUN_ID"] == "run-seam"
    mock_exit.assert_called_once_with(0)

    # Parent wrote PID file with child's pid so `forge status` works
    # immediately after launch.
    pid_file = tmp_path / ".forge" / "runs" / "run-seam.pid"
    assert pid_file.exists()
    assert pid_file.read_text().splitlines()[0] == "12345"


# ── cli/run.py: detached child skips daemonize and runs the workload ──


def test_cmd_run_detached_child_skips_daemonize(tmp_path, monkeypatch):
    """When FORGE_DETACHED=1 is set, cmd_run must NOT call daemonize_run;
    it should call setup_detached_child and proceed into run_task.
    """
    from theforge.cli import run as run_cli

    # Minimal config + story
    story = tmp_path / "story.md"
    story.write_text("# story\n")
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: test\n")

    args = argparse.Namespace(
        story=str(story),
        slug="seam-slug",
        config=str(config_path),
        base_branch=None,
        dry_run=False,
        interactive=False,
        auto_merge=False,
        dev_model=None,
        plan_model=None,
        verbose=False,
        no_notify=True,
        plan=None,
        resume=False,
        until=None,
        from_phase=None,
        reviewers=None,
        max_cycles=None,
        fg=False,
        no_pull=False,
    )

    monkeypatch.setenv("FORGE_DETACHED", "1")
    monkeypatch.setenv("FORGE_DETACHED_RUN_ID", "child-run-id")

    fake_config = MagicMock()
    fake_config.project_root = tmp_path
    fake_config.project = "test"
    fake_config.dev_profile.model = "x"
    fake_config.review_pool = [MagicMock(name="r", model="m")]
    fake_config.synthesis_profile = None
    fake_config.retry.max_review_cycles = 1
    fake_config.retry.max_dev_iterations = 1
    fake_config.workspace.path_pattern = ".forge/worktrees/{slug}"

    fake_result = MagicMock()
    fake_result.success = True
    fake_result.message = "ok"
    fake_result.state.total_cost = 0.0

    with (
        patch("theforge.cli.run.load_config", return_value=fake_config),
        patch("theforge.cli.run.apply_base_branch_override", return_value=fake_config),
        patch("theforge.cli.run._build_task", return_value=MagicMock(slug="seam-slug")),
        patch("theforge.cli.run._find_config", return_value=config_path),
        patch("theforge.cli.run.run_task", return_value=fake_result) as mock_run_task,
        patch("theforge.cli.run._write_audit", return_value=tmp_path / "audit.yaml"),
        patch("theforge.detach.daemonize_run") as mock_daemonize,
        patch("theforge.detach.setup_detached_child") as mock_setup,
        patch("theforge.detach.install_cleanup_handler"),
        patch("theforge.detach.write_run_ended"),
        patch("theforge.detach.remove_pid"),
    ):
        rc = run_cli.cmd_run(args)

    assert rc == 0
    mock_daemonize.assert_not_called()
    mock_setup.assert_called_once()
    setup_args = mock_setup.call_args.args
    assert setup_args[0] == "child-run-id"
    assert setup_args[1] == "seam-slug"
    mock_run_task.assert_called_once()


# ── FORGE_DETACHED is orthogonal to FORGE_PREV_RUN_ID ──────────────────


def test_forge_detached_is_distinct_from_forge_prev_run_id(monkeypatch):
    """Re-exec sentinel must not collide with the coordinator's
    FORGE_PREV_RUN_ID re-exec sentinel — they signal different things.
    """
    monkeypatch.setenv("FORGE_DETACHED", "1")
    monkeypatch.delenv("FORGE_PREV_RUN_ID", raising=False)
    assert detach.is_detached_child() is True

    # In cli/sprint.py _is_reexec() must remain False for a plain detached
    # child — only true when coordinator did an os.execv after a git pull.
    from theforge.cli import sprint as sprint_cli

    assert sprint_cli._is_reexec() is False

    monkeypatch.setenv("FORGE_PREV_RUN_ID", "prev-run")
    assert sprint_cli._is_reexec() is True
    # Both can coexist (coordinator-reexec inside an already-detached child).
    assert detach.is_detached_child() is True


# ── Multiprocessing spawn is the start method process-wide ─────────────


def test_multiprocessing_start_method_is_spawn():
    """theforge package import sets multiprocessing to 'spawn' as defense in
    depth against macOS fork-safety crashes from any future Pool/Process use.
    """
    import multiprocessing

    # Force re-import to make sure our package init has applied.
    import theforge  # noqa: F401

    assert multiprocessing.get_start_method(allow_none=False) == "spawn"
