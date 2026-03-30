"""Coordinator worktree lifecycle and merge machinery."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.task import TaskStory

from . import util as _cu
from .gate import _run_gate

# Populated lazily on first call to _resolve_merge_conflicts.
run_agent = None

_MAX_AUTO_RESOLVE_FILES = 5
_CONFLICT_RESOLUTION_TIMEOUT = 120

# Matches: test -d .venv || (python -m venv .venv && <install>)
_VENV_GUARD_RE = re.compile(
    r"test\s+-d\s+\.venv\s*\|\|\s*\(\s*python\s+-m\s+venv\s+\.venv\s*&&\s*(.+?)\s*\)",
    re.DOTALL,
)


def _run_setup_split(setup_command: str, workspace_path: Path) -> tuple[bool, str]:
    """Run workspace setup, always running pip install even if .venv exists.

    Detects the `test -d .venv || (python -m venv .venv && <install>)` pattern
    and splits it: venv creation is guarded, install always runs.
    Falls back to running setup_command verbatim when the pattern is not found.
    """
    m = _VENV_GUARD_RE.search(setup_command)
    if not m:
        return _cu._run_shell(setup_command, workspace_path)
    install_cmd = m.group(1).strip()
    ok, out = _cu._run_shell("test -d .venv || python -m venv .venv", workspace_path)
    if not ok:
        return ok, out
    return _cu._run_shell(install_cmd, workspace_path)


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

    global run_agent
    if run_agent is None:
        import theforge.runners as _r  # noqa: PLC0415

        run_agent = _r.run_agent
    _resolve_start = time.monotonic()
    resolve_result = run_agent(
        prompt=prompt,
        profile=config.dev_profile,
        working_dir=project_root,
        session_id=None,
        secrets=config.secrets,
    )
    _resolve_elapsed = time.monotonic() - _resolve_start
    _cu._log(f"  ... resolution done ({int(_resolve_elapsed)}s)")

    if not resolve_result.success:
        _cu._log("  ⚠ Conflict resolution agent failed — aborting merge")
        _cu._run_shell("git merge --abort", project_root)
        return False

    _cu._log("  Running gate to verify resolution...")
    gate_decision, gate_error, _ = _run_gate(config, workspace_path)
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

    n_commits = len(commits_ahead)
    return (
        False,
        f"{n_commits} commit{'s' if n_commits != 1 else ''} ahead of {base_branch}",
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


def _find_worktree_for_branch(branch: str, project_root: Path) -> Path | None:
    """Return the registered worktree path for branch, or None if not found."""
    ok, output = _cu._run_shell("git worktree list --porcelain", project_root)
    if not ok:
        return None
    current_path: Path | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree ") :])
        elif line.startswith("branch ") and current_path is not None:
            if line[len("branch ") :] == f"refs/heads/{branch}":
                return current_path
    return None


def _check_behind_origin(config: ForgeConfig) -> None:
    """Log an informational note if base_branch is behind origin.

    Silently skips if the rev-list command fails (no remote, fetch needed, etc.).
    This check is purely informational — it never blocks execution.
    """
    base_branch = config.workspace.base_branch
    ok, out = _cu._run_shell(
        f"git rev-list --count {base_branch}..origin/{base_branch}",
        config.project_root,
    )
    if not ok:
        return
    try:
        count = int(out.strip())
    except ValueError:
        return
    if count > 0:
        _cu._log(f"ℹ WORKSPACE  {base_branch} is {count} commit(s) behind origin/{base_branch}")


def _create_workspace(
    config: ForgeConfig, task: TaskStory, *, no_pull: bool = False
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
            if not no_pull:
                _check_behind_origin(config)
            return workspace_path, branch_name, None

    if not no_pull:
        base_branch = config.workspace.base_branch
        # Use `git fetch origin <base>:<base>` instead of `git pull` so that the
        # local base_branch ref is advanced regardless of what the project root has
        # checked out.  `git pull` only fast-forwards the *current* branch, which
        # would leave local main stale whenever the root is on a feature branch.
        ok_pull, pull_out = _cu._run_shell(
            f"git fetch origin {base_branch}:{base_branch}", config.project_root
        )
        if ok_pull:
            _cu._log(f"✓ WORKSPACE  pulled latest {base_branch}")
        else:
            _cu._log(f"⚠ WORKSPACE  pull failed (non-ff / offline): {pull_out.strip()}")

    _cu._log(f"Creating workspace: {cmd}")
    ok, output = _cu._run_shell(cmd, config.project_root)
    if not ok:
        # Check if a branch collision caused the failure
        ok_branch, branch_out = _cu._run_shell(
            f"git branch --list {branch_name}", config.project_root
        )
        if not ok_branch or not branch_out.strip():
            return None, None, f"Failed to create workspace: {output}"

        # Branch exists — find registered worktree
        existing_wt = _find_worktree_for_branch(branch_name, config.project_root)

        if existing_wt is not None:
            if existing_wt.exists():
                _cu._log(f"↻ WORKSPACE  reusing existing worktree (registered): {existing_wt}")
                if not no_pull:
                    _check_behind_origin(config)
                if config.workspace.setup_command:
                    _cu._log(f"Running workspace setup: {config.workspace.setup_command}")
                    ok_s, out_s = _run_setup_split(config.workspace.setup_command, existing_wt)
                    if not ok_s:
                        return None, None, f"Workspace setup command failed: {out_s}"
                return existing_wt, branch_name, None
            else:
                _cu._log("⚠ WORKSPACE  linked worktree directory missing — pruning")
                _cu._run_shell("git worktree prune", config.project_root)

        # Check commits ahead of base
        ok_log, log_out = _cu._run_shell(
            f"git log {config.workspace.base_branch}..{branch_name} --oneline",
            config.project_root,
        )
        commits_ahead = [ln for ln in log_out.strip().splitlines() if ln.strip()] if ok_log else []

        if commits_ahead:
            _cu._log(f"↻ WORKSPACE  branch has commits, reattaching worktree: {branch_name}")
            if not no_pull:
                _check_behind_origin(config)
            ok_add, add_out = _cu._run_shell(
                f"git worktree add {workspace_path} {branch_name}", config.project_root
            )
            if not ok_add:
                return None, None, f"Failed to reattach worktree: {add_out}"
            if config.workspace.setup_command:
                _cu._log(f"Running workspace setup: {config.workspace.setup_command}")
                ok_s, out_s = _run_setup_split(config.workspace.setup_command, workspace_path)
                if not ok_s:
                    return None, None, f"Workspace setup command failed: {out_s}"
            return workspace_path, branch_name, None
        else:
            _cu._log(
                f"⚠ WORKSPACE  stale branch with 0 commits — deleting and recreating:"
                f" {branch_name}"
            )
            ok_del, del_out = _cu._run_shell(f"git branch -D {branch_name}", config.project_root)
            if not ok_del:
                return None, None, f"Failed to delete stale branch: {del_out}"
            ok2, output2 = _cu._run_shell(cmd, config.project_root)
            if not ok2:
                return (
                    None,
                    None,
                    f"Failed to create workspace (retry after branch delete): {output2}",
                )

    if not workspace_path.exists():
        return None, None, f"Workspace path does not exist after creation: {workspace_path}"

    if config.workspace.setup_command:
        _cu._log(f"Running workspace setup: {config.workspace.setup_command}")
        ok, output = _run_setup_split(config.workspace.setup_command, workspace_path)
        if not ok:
            return None, None, f"Workspace setup command failed: {output}"

    return workspace_path, branch_name, None
