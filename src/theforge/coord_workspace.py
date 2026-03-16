"""Coordinator worktree lifecycle and merge machinery."""

from __future__ import annotations

import datetime
import subprocess
import time
from pathlib import Path

from . import coord_util as _cu
from .config import ForgeConfig
from .coord_gate import _run_gate
from .runner import run_agent
from .task import TaskSpec

_MAX_AUTO_RESOLVE_FILES = 5
_CONFLICT_RESOLUTION_TIMEOUT = 120


def _resolve_merge_conflicts(
    project_root: Path,
    branch_name: str,
    task_name: str,
    config: ForgeConfig,
    workspace_path: Path,
) -> bool:
    """Attempt to auto-resolve merge conflicts using the dev agent.

    Returns True if conflicts were resolved and the gate passed, False otherwise.
    On failure, aborts the in-progress merge.
    """
    ok, conflict_out = _cu._run_shell("git diff --name-only --diff-filter=U", project_root)
    if not ok or not conflict_out.strip():
        return False

    conflicted_files = [f.strip() for f in conflict_out.strip().splitlines() if f.strip()]
    if not conflicted_files:
        return False

    if len(conflicted_files) > _MAX_AUTO_RESOLVE_FILES:
        _cu._log(
            f"  ⚠ Too many conflicted files ({len(conflicted_files)}) — skipping auto-resolution"
        )
        return False

    _cu._log(f"  Merge conflict in {len(conflicted_files)} file(s): {', '.join(conflicted_files)}")
    _cu._log("  Attempting auto-resolution...")

    file_sections: list[str] = []
    for rel_path in conflicted_files:
        full_path = project_root / rel_path
        try:
            content = full_path.read_text(encoding="utf-8")
            file_sections.append(f"### {rel_path}\n\n```\n{content}\n```")
        except OSError:
            file_sections.append(f"### {rel_path}\n\n(could not read file)")

    conflicted_files_with_content = "\n\n".join(file_sections)
    base_branch = config.workspace.base_branch

    prompt = (
        f"You are resolving a git merge conflict. The branch `{branch_name}` is being\n"
        f"merged into `{base_branch}`.\n\n"
        f"Branch purpose: {task_name}\n\n"
        f"The following files have conflicts:\n\n"
        f"{conflicted_files_with_content}\n\n"
        f"Resolve each conflict by editing the files to remove all conflict markers\n"
        f"(<<<<<<, =======, >>>>>>>). Preserve the intent of both sides. When both\n"
        f"sides add code in the same location, keep both additions.\n\n"
        f"After resolving, run the project's test suite to verify nothing is broken."
    )

    _resolve_start = time.monotonic()
    resolve_result = run_agent(
        prompt=prompt,
        profile=config.dev_profile,
        working_dir=project_root,
        session_id=None,
    )
    _resolve_elapsed = time.monotonic() - _resolve_start
    _cu._log(f"  ... resolution done ({int(_resolve_elapsed)}s)")

    if not resolve_result.success:
        _cu._log("  ⚠ Conflict resolution agent failed — aborting merge")
        _cu._run_shell("git merge --abort", project_root)
        return False

    _cu._log("  Running gate to verify resolution...")
    gate_decision, gate_error = _run_gate(config, workspace_path)
    if gate_error or gate_decision != "PASS":
        _cu._log("  ⚠ Conflict resolution broke tests — aborting merge")
        _cu._run_shell("git merge --abort", project_root)
        return False

    files_arg = " ".join(f'"{f}"' for f in conflicted_files)
    ok_add, _ = _cu._run_shell(f"git add {files_arg}", project_root)
    if not ok_add:
        _cu._log("  ⚠ Failed to stage resolved files — aborting merge")
        _cu._run_shell("git merge --abort", project_root)
        return False

    ok_commit, _ = _cu._run_shell("git commit --no-edit", project_root)
    if not ok_commit:
        _cu._log("  ⚠ Failed to commit resolution — aborting merge")
        _cu._run_shell("git merge --abort", project_root)
        return False

    _cu._log("  ✓ Conflict resolved and merged")
    return True


def _merge_branch(
    project_root: Path,
    base_branch: str,
    branch_name: str,
    slug: str,
    workspace_path: Path,
    *,
    auto_push: bool = False,
    config: ForgeConfig | None = None,
    task_name: str = "",
) -> dict:
    """Merge branch_name into base_branch in project_root.

    Returns a merge info dict with keys: attempted, merged, base_branch, error.
    When config is provided, auto-resolve merge conflicts using the dev agent.
    """
    info: dict = {
        "attempted": True,
        "merged": False,
        "base_branch": base_branch,
        "error": None,
    }

    ok, out = _cu._run_shell(f"git branch --list {base_branch}", project_root)
    if not ok or not out.strip():
        info["error"] = f"Base branch {base_branch!r} not found in project root"
        _cu._log(f"Auto-merge skipped: {info['error']}")
        return info

    ok, dirty = _cu._run_shell("git status --porcelain", project_root)
    if ok and dirty.strip():
        info["error"] = f"Uncommitted changes in project root: {dirty.strip()[:200]}"
        _cu._log(f"Auto-merge skipped: {info['error']}")
        return info

    ok, log_out = _cu._run_shell(f"git log {base_branch}..{branch_name} --oneline", project_root)
    if not ok or not log_out.strip():
        info["error"] = f"Branch {branch_name!r} has no commits ahead of {base_branch!r}"
        _cu._log(f"Auto-merge skipped: {info['error']}")
        return info

    ok, out = _cu._run_shell(f"git checkout {base_branch}", project_root)
    if not ok:
        info["error"] = f"Failed to checkout {base_branch!r}: {out}"
        _cu._log(f"Auto-merge failed: {info['error']}")
        return info

    ok, out = _cu._run_shell(f"git merge --ff-only {branch_name}", project_root)
    if not ok:
        _cu._log(f"Fast-forward merge failed, falling back to regular merge: {out}")
        ok, out = _cu._run_shell(f"git merge --no-edit {branch_name}", project_root)

    if not ok:
        if config is not None:
            resolved = _resolve_merge_conflicts(
                project_root,
                branch_name,
                task_name,
                config,
                workspace_path,
            )
            if not resolved:
                info["error"] = f"Merge failed: {out}"
                _cu._log(f"Auto-merge failed: {info['error']}")
                return info
        else:
            info["error"] = f"Merge failed: {out}"
            _cu._log(f"Auto-merge failed: {info['error']}")
            return info

    info["merged"] = True
    _cu._log(f"Auto-merge succeeded: {branch_name} → {base_branch}")

    if auto_push:
        try:
            subprocess.run(
                ["git", "push", "origin", base_branch],
                cwd=str(project_root),
                timeout=30,
                capture_output=True,
                check=True,
            )
            _cu._log(f"  Pushed {base_branch} to origin")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            _cu._log(f"  ⚠ Push failed: {e} (merge succeeded locally)")

    worktree_rel = f".forge/worktrees/{slug}"
    ok_rm, rm_out = _cu._run_shell(f"git worktree remove --force {worktree_rel}", project_root)
    if not ok_rm:
        _cu._log(f"Warning: worktree cleanup failed: {rm_out}")
    else:
        _cu._log(f"Worktree removed: {worktree_rel}")

    return info


# ── Workspace ────────────────────────────────────────────────────────


def _fmt_age(seconds: int) -> str:
    """Format an age in seconds as a human-readable string (e.g. '3 days', '12 minutes')."""
    if seconds < 3600:
        m = max(0, seconds // 60)
        return f"{m} minute{'s' if m != 1 else ''}"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''}"
    d = seconds // 86400
    return f"{d} day{'s' if d != 1 else ''}"


def _is_stale_worktree(path: Path, base_branch: str, config: ForgeConfig) -> tuple[bool, str]:
    """Check whether an existing worktree is stale and should be removed."""
    stale_days: int = config.workspace.stale_worktree_days

    ok, branch_out = _cu._run_shell("git rev-parse --abbrev-ref HEAD", path)
    if not ok or not branch_out.strip() or branch_out.strip() == "HEAD":
        return True, "branch not found or detached HEAD — removing (corrupted state)"

    branch_name = branch_out.strip()

    if stale_days == 0:
        return True, "stale_worktree_days=0 — always removing (CI/automated mode)"

    ok, log_out = _cu._run_shell(
        f"git log {base_branch}..{branch_name} --oneline",
        config.project_root,
    )
    commits_ahead = [ln for ln in log_out.strip().splitlines() if ln.strip()] if ok else []

    if not commits_ahead:
        return True, f"0 commits ahead of {base_branch} — removing (stale)"

    ok, ts_out = _cu._run_shell(
        f"git log -1 --format=%ct {branch_name}",
        config.project_root,
    )
    if not ok or not ts_out.strip():
        return True, "could not determine last commit timestamp — removing (stale)"

    try:
        last_commit_ts = int(ts_out.strip())
    except ValueError:
        return True, "could not parse commit timestamp — removing (stale)"

    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    age_seconds = max(0, now_ts - last_commit_ts)
    age_days_float = age_seconds / 86400
    n_commits = len(commits_ahead)
    age_str = _fmt_age(age_seconds)

    if age_days_float > stale_days:
        return (
            True,
            f"{n_commits} commit{'s' if n_commits != 1 else ''} ahead of {base_branch}, "
            f"last commit {age_str} ago — removing (stale)",
        )

    return (
        False,
        f"{n_commits} commit{'s' if n_commits != 1 else ''} ahead of {base_branch}, "
        f"last commit {age_str} ago",
    )


def _remove_worktree(path: Path, branch: str, project_root: Path, info_line: str = "") -> None:
    """Remove a stale worktree and its branch. Logs warnings but does not raise."""
    _cu._log(f"⚠ WORKSPACE  stale worktree detected — removing {branch}")
    if info_line:
        _cu._log(f"  {info_line}")

    ok, out = _cu._run_shell(f"git worktree remove --force {path}", project_root)
    if not ok:
        _cu._log(f"  Warning: git worktree remove failed: {out}")
    else:
        _cu._log(f"  Removed stale worktree: {path}")

    ok2, out2 = _cu._run_shell(f"git branch -D {branch}", project_root)
    if not ok2:
        _cu._log(f"  Warning: git branch -D failed: {out2}")
    else:
        _cu._log(f"  Deleted branch {branch}")


def _create_workspace(
    config: ForgeConfig, task: TaskSpec
) -> tuple[Path | None, str | None, str | None]:
    """Create an isolated workspace. Returns (path, branch, error)."""
    slug = task.slug
    cmd = config.workspace.create_command.format(slug=slug)
    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=slug)
    branch_name = config.workspace.branch_pattern.format(slug=slug)

    if workspace_path.exists():
        _cu._log(f"⚠ WORKSPACE  existing worktree found: {workspace_path}")
        is_stale, info_line = _is_stale_worktree(
            workspace_path, config.workspace.base_branch, config
        )
        _cu._log(f"  {info_line}")
        if is_stale:
            _remove_worktree(workspace_path, branch_name, config.project_root, info_line)
        else:
            _cu._log(f"↻ WORKSPACE  reusing existing worktree: {workspace_path}")
            return workspace_path, branch_name, None

    _cu._log(f"Creating workspace: {cmd}")
    ok, output = _cu._run_shell(cmd, config.project_root)
    if not ok:
        return None, None, f"Failed to create workspace: {output}"

    if not workspace_path.exists():
        return None, None, f"Workspace path does not exist after creation: {workspace_path}"

    if config.workspace.setup_command:
        _cu._log(f"Running workspace setup: {config.workspace.setup_command}")
        ok, output = _cu._run_shell(config.workspace.setup_command, workspace_path)
        if not ok:
            return None, None, f"Workspace setup command failed: {output}"

    return workspace_path, branch_name, None
