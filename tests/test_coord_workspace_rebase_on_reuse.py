"""End-to-end tests for issue #1466: rebase reused worktrees onto current base.

Sprint resume must rebase a reused worktree onto current base before any dev
cycle runs, and abort cleanly when conflicts surface — so the operator learns
about the conflict before paying for unmergeable work.

These tests use real git operations through `_create_workspace` (the path that
emits "↻ WORKSPACE  reusing existing worktree") rather than mocks, since the
defect lives in cross-process worktree state coordination.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.workspace import _create_workspace
from theforge.task import TaskStory


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo_with_origin(tmp_path: Path, slug: str) -> tuple[Path, Path, str]:
    """Set up a bare origin + working clone with a feature branch that has commits.

    Returns (project_root, workspace_path, branch_name).
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")

    project_root = tmp_path / "repo"
    project_root.mkdir()
    _git(project_root, "init", "--initial-branch=main")
    _git(project_root, "config", "user.email", "test@example.com")
    _git(project_root, "config", "user.name", "Test")
    _git(project_root, "remote", "add", "origin", str(origin))

    shared_file = project_root / "shared.txt"
    shared_file.write_text("base content\n", encoding="utf-8")
    _git(project_root, "add", "shared.txt")
    _git(project_root, "commit", "-m", "initial")
    _git(project_root, "push", "-u", "origin", "main")

    branch = f"feat/{slug}"
    workspace_path = project_root / slug
    _git(project_root, "worktree", "add", "-b", branch, str(workspace_path), "main")
    _git(workspace_path, "config", "user.email", "test@example.com")
    _git(workspace_path, "config", "user.name", "Test")
    (workspace_path / "shared.txt").write_text("feature edit\n", encoding="utf-8")
    _git(workspace_path, "add", "shared.txt")
    _git(workspace_path, "commit", "-m", "feature change")

    return project_root, workspace_path, branch


def _make_real_config(project_root: Path, slug: str) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=project_root,
        workspace=WorkspaceConfig(
            create_command="git worktree add {slug} {base_branch}",
            path_pattern="{slug}",
            branch_pattern=f"feat/{slug}",
            base_branch="main",
            setup_command=None,
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_real_task(slug: str, project_root: Path) -> TaskStory:
    spec = project_root / f"{slug}.md"
    spec.write_text("# task\n", encoding="utf-8")
    return TaskStory(name="task", story_path=spec, slug=slug)


def test_reused_worktree_rebases_onto_current_base_when_clean(tmp_path):
    """Resume on a worktree whose base advanced cleanly: rebase succeeds, returns workspace."""
    slug = "rebase-clean"
    project_root, workspace_path, branch = _init_repo_with_origin(tmp_path, slug)

    # Advance main with a non-conflicting change after the worktree was created.
    other = project_root / "other.txt"
    other.write_text("new file on main\n", encoding="utf-8")
    _git(project_root, "add", "other.txt")
    _git(project_root, "commit", "-m", "advance main")
    _git(project_root, "push", "origin", "main")

    config = _make_real_config(project_root, slug)
    task = _make_real_task(slug, project_root)

    path, branch_name, error = _create_workspace(config, task, no_pull=True)

    assert error is None, f"expected clean rebase, got error: {error}"
    assert path == workspace_path
    assert branch_name == branch
    # Worktree now contains other.txt from rebased base
    assert (workspace_path / "other.txt").exists()


def test_reused_worktree_aborts_with_diagnostic_when_rebase_conflicts(tmp_path):
    """Resume on a worktree whose base diverged with conflicting changes: abort, no dev cycle."""
    slug = "rebase-conflict"
    project_root, workspace_path, _branch = _init_repo_with_origin(tmp_path, slug)

    # Advance main with a CONFLICTING change to shared.txt.
    (project_root / "shared.txt").write_text("base advanced\n", encoding="utf-8")
    _git(project_root, "add", "shared.txt")
    _git(project_root, "commit", "-m", "conflicting main change")
    _git(project_root, "push", "origin", "main")

    config = _make_real_config(project_root, slug)
    task = _make_real_task(slug, project_root)

    path, branch_name, error = _create_workspace(config, task, no_pull=True)

    assert path is None
    assert branch_name is None
    assert error is not None
    assert "rebase" in error.lower()
    assert "main" in error
    # Worktree must not be left mid-rebase (the rebase function aborts on failure).
    assert not (workspace_path / ".git" / "rebase-merge").exists()
    assert not (workspace_path / ".git" / "rebase-apply").exists()
