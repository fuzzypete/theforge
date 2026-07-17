"""Tests for forge run precondition guards (issue #795).

Covers the pure ``check_run_preconditions`` helper over a real git repo and the
``cmd_run`` fail-fast / warn-but-proceed behaviour wired on top of it.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config

from theforge.cli.run import cmd_run
from theforge.cli.shared import check_run_preconditions
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(path: Path) -> None:
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")


def _track(root: Path, rel: str, content: str = "x\n") -> None:
    """Create and git-add a file at ``root/rel`` (no .gitignore in the way)."""
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(root, "add", "-f", rel)


def _stub_result() -> CoordinatorResult:
    return CoordinatorResult(
        success=True, phase=Phase.DONE, state=CoordinatorState(), message="ok"
    )


# ── check_run_preconditions: pure helper ───────────────────────────────────


def test_no_git_repo_returns_no_findings(tmp_path: Path) -> None:
    """A directory that is not a git repo yields no blockers or warnings."""
    blockers, warnings = check_run_preconditions(tmp_path)
    assert blockers == []
    assert warnings == []


def test_clean_repo_returns_no_findings(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _track(tmp_path, "src/app.py")
    _git(tmp_path, "commit", "-q", "-m", "init")

    blockers, warnings = check_run_preconditions(tmp_path)
    assert blockers == []
    assert warnings == []


def test_tracked_worktrees_dir_is_a_blocker(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _track(tmp_path, ".forge/worktrees/issue-1/foo.py")

    blockers, warnings = check_run_preconditions(tmp_path)
    assert warnings == []
    assert len(blockers) == 1
    assert ".forge/worktrees/ is tracked in git." in blockers[0]
    assert "git rm --cached -r .forge/worktrees/" in blockers[0]
    assert "then retry" in blockers[0]


def test_tracked_merge_lock_file_is_a_blocker(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _track(tmp_path, ".forge/merge.lock")

    blockers, _ = check_run_preconditions(tmp_path)
    assert len(blockers) == 1
    # File blocker: no -r flag.
    assert "git rm --cached .forge/merge.lock" in blockers[0]
    assert "-r" not in blockers[0]


def test_all_blocker_paths_detected(tmp_path: Path) -> None:
    """Every catastrophic path in the design doc trips a blocker."""
    _init_repo(tmp_path)
    _track(tmp_path, ".forge/worktrees/w/a.py")
    _track(tmp_path, ".forge/locks/run.lock")
    _track(tmp_path, ".forge/merge.lock")
    _track(tmp_path, ".forge/daemon.json")
    _track(tmp_path, ".forge/pending/p.json")
    _track(tmp_path, ".forge/runs/r.json")
    _track(tmp_path, ".forge/.env")
    _track(tmp_path, ".forge/secrets.yaml")
    _track(tmp_path, "handoff.yaml")

    blockers, warnings = check_run_preconditions(tmp_path)
    assert warnings == []
    assert len(blockers) == 9
    for display in (
        ".forge/worktrees/",
        ".forge/locks/",
        ".forge/merge.lock",
        ".forge/daemon.json",
        ".forge/pending/",
        ".forge/runs/",
        ".forge/.env",
        ".forge/secrets.yaml",
        "handoff.yaml",
    ):
        assert any(b.startswith(f"{display} is tracked in git.") for b in blockers), display


def test_noise_paths_are_warnings_not_blockers(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _track(tmp_path, ".forge/logs/run.log")
    _track(tmp_path, ".forge/audits/history.jsonl")
    _track(tmp_path, ".forge/audits/index.sqlite")
    _track(tmp_path, ".forge/assignment_history.yaml")

    blockers, warnings = check_run_preconditions(tmp_path)
    assert blockers == []
    assert len(warnings) == 4
    for display in (
        ".forge/logs/",
        ".forge/audits/history.jsonl",
        ".forge/audits/index.sqlite",
        ".forge/assignment_history.yaml",
    ):
        assert any(w.startswith(f"{display} is tracked in git.") for w in warnings), display
    # Warnings still name the resolving command.
    assert any("git rm --cached" in w for w in warnings)


def test_audit_runs_dir_is_not_flagged(tmp_path: Path) -> None:
    """The tracked audit dir (.forge/audits/runs/) is project memory, not a
    blocker — only the distinct execution-state .forge/runs/ dir is caught."""
    _init_repo(tmp_path)
    _track(tmp_path, ".forge/audits/runs/abc.json")

    blockers, warnings = check_run_preconditions(tmp_path)
    assert blockers == []
    assert warnings == []


# ── cmd_run: fail-fast / warn-but-proceed wiring ────────────────────────────


def _run_args(story: Path, forge_yaml: Path) -> argparse.Namespace:
    return argparse.Namespace(
        story=str(story),
        slug=None,
        config=str(forge_yaml),
        base_branch=None,
        plan=None,
        from_phase=None,
        until=None,
        reviewers=None,
        max_cycles=None,
        resume=False,
        dev_model=None,
        plan_model=None,
        dry_run=False,
        interactive=False,
        auto_merge=False,
        verbose=False,
        no_notify=True,
        fg=True,
        no_pull=False,
    )


def test_cmd_run_aborts_on_tracked_blocker(tmp_path: Path, capsys) -> None:
    _init_repo(tmp_path)
    story = tmp_path / "story.md"
    story.write_text("# Story\n", encoding="utf-8")
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    _track(tmp_path, ".forge/worktrees/w/a.py")

    config = _make_config(tmp_path)
    args = _run_args(story, forge_yaml)

    with (
        patch("theforge.cli.run.load_config", return_value=config),
        patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
    ):
        rc = cmd_run(args)

    assert rc == 1
    # Precondition check runs before any agent invocation / workspace mutation.
    mock_run.assert_not_called()
    err = capsys.readouterr().err
    assert "git rm --cached -r .forge/worktrees/" in err


def test_cmd_run_proceeds_on_warning_only(tmp_path: Path, capsys) -> None:
    _init_repo(tmp_path)
    story = tmp_path / "story.md"
    story.write_text("# Story\n", encoding="utf-8")
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    _track(tmp_path, ".forge/logs/run.log")

    config = _make_config(tmp_path)
    args = _run_args(story, forge_yaml)

    with (
        patch("theforge.cli.run.load_config", return_value=config),
        patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
        patch("theforge.cli.run._write_audit", return_value=tmp_path / "audit.json"),
    ):
        rc = cmd_run(args)

    assert rc == 0
    mock_run.assert_called_once()
    err = capsys.readouterr().err
    assert ".forge/logs/ is tracked in git." in err
