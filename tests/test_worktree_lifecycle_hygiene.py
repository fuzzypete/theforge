from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

from coord_test_helpers import _make_config, _make_task

from theforge.artifacts import ESCALATED_MARKER_PATH
from theforge.cli.shared import _write_audit
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.coordinator.workspace import sweep_orphan_worktrees
from theforge.sprint.audit import _write_story_audit
from theforge.sprint.lock import _is_escalated_worktree


def _make_result(
    *, phase: Phase, workspace_path: Path | None = None, log_dir: Path | None = None
) -> CoordinatorResult:
    state = CoordinatorState()
    state.phase = phase
    state.workspace_path = workspace_path
    state.log_dir = log_dir
    return CoordinatorResult(
        success=phase == Phase.DONE, phase=phase, state=state, message=phase.name
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_write_audit_skips_worktree_audit_for_non_escalate(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(parents=True)
    log_dir = tmp_path / "logs" / task.slug
    result = _make_result(phase=Phase.DONE, workspace_path=workspace, log_dir=log_dir)

    _write_audit(result, config, task)

    assert not (workspace / ".forge" / "audit.yaml").exists()
    assert not (workspace / ESCALATED_MARKER_PATH).exists()
    assert (log_dir / "audit.yaml").exists()


def test_escalate_writes_marker_and_detection_uses_it(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir(parents=True)
    log_dir = tmp_path / "logs" / task.slug

    escalate_result = _make_result(phase=Phase.ESCALATE, workspace_path=workspace, log_dir=log_dir)
    _write_audit(escalate_result, config, task)

    assert (workspace / ESCALATED_MARKER_PATH).exists()
    assert _is_escalated_worktree(workspace) is True

    clean_workspace = tmp_path / "clean"
    clean_workspace.mkdir()
    assert _is_escalated_worktree(clean_workspace) is False


def test_sweep_orphan_worktrees_removes_orphan_and_merged_but_preserves_escalated(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "fetch", "origin", "main:refs/remotes/origin/main")

    base_config = _make_config(repo)
    config = dataclasses.replace(
        base_config,
        workspace=dataclasses.replace(
            base_config.workspace,
            path_pattern=".forge/worktrees/{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
    )

    worktrees_root = repo / ".forge" / "worktrees"
    worktrees_root.mkdir(parents=True, exist_ok=True)

    orphan = worktrees_root / "orphan"
    (orphan / ".forge").mkdir(parents=True)
    (orphan / ".forge" / "audit.yaml").write_text("outcome: {}\n", encoding="utf-8")

    merged = worktrees_root / "merged"
    _git(repo, "worktree", "add", "-b", "forge/merged", str(merged), "main")
    (merged / "merged.txt").write_text("done\n", encoding="utf-8")
    _git(merged, "add", "merged.txt")
    _git(merged, "commit", "-m", "merged work")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--ff-only", "forge/merged")

    escalated = worktrees_root / "escalated"
    _git(repo, "worktree", "add", "-b", "forge/escalated", str(escalated), "main")
    task = _make_task(repo)
    task = dataclasses.replace(task, slug="escalated")
    esc_result = _make_result(
        phase=Phase.ESCALATE, workspace_path=escalated, log_dir=repo / "logs"
    )
    _write_story_audit(config, task, esc_result, sprint_id="sprint-1")

    sweep_orphan_worktrees(repo, config)

    assert not orphan.exists()
    assert not merged.exists()
    assert not any(
        "forge/merged" in line
        for line in subprocess.run(
            ["git", "branch", "--list"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.splitlines()
    )
    assert escalated.exists()
    assert (escalated / ESCALATED_MARKER_PATH).exists()
