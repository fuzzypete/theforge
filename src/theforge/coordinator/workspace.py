"""Coordinator worktree lifecycle and merge machinery."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from theforge.artifacts import ESCALATED_MARKER_PATH
from theforge.config import ForgeConfig
from theforge.detach import find_run_id_for_pid as _find_run_id_for_pid
from theforge.task import TaskStory

from . import util as _cu
from .gate import _run_gate
from .git_lock import FETCH_LOCK

# Populated lazily on first call to _resolve_merge_conflicts.
run_agent = None

_MAX_AUTO_RESOLVE_FILES = 5
_CONFLICT_RESOLUTION_TIMEOUT = 120

# Matches setup_command templates that still contain the {forge_python} placeholder.
# Captures the install command (group 1).  Matched BEFORE substitution so we never
# have to parse shlex.quote() output — any valid interpreter path is accepted.
_VENV_GUARD_TEMPLATE_RE = re.compile(
    r"test\s+-d\s+\.venv\s*\|\|\s*\(\s*\{forge_python\}\s+-m\s+venv\s+\.venv\s*&&\s*(.+?)\s*\)",
    re.DOTALL,
)

# Legacy form: bare executable token, no {forge_python} placeholder.
# Only applied when the template form does not match.
_VENV_GUARD_RE = re.compile(
    r"test\s+-d\s+\.venv\s*\|\|\s*\(\s*(\S+)\s+-m\s+venv\s+\.venv\s*&&\s*(.+?)\s*\)",
    re.DOTALL,
)


def _claude_project_slug(path: Path) -> str:
    """Encode a filesystem path the same way Claude Code names ~/.claude/projects/<slug>/.

    Mirrors runner_claude._get_claude_session_id: replace separators in the resolved
    absolute path with hyphens. Keep this in sync with that encoding.
    """
    return str(path.resolve()).replace("/", "-")


def _propagate_claude_memory(project_root: Path, workspace_path: Path) -> None:
    """Symlink the worktree's Claude project memory dir to the main project's.

    Claude Code stores per-project memory under ~/.claude/projects/<encoded-cwd>/memory/.
    A worktree's cwd encodes to a different slug than the main project, so a dev
    subprocess spawned in the worktree starts with empty memory. Symlinking the
    worktree's memory dir back to the main project's makes them share state, so
    lessons accumulated by interactive sessions reach forge dev agents.

    Best-effort: failures are logged but never block workspace creation.
    """
    try:
        if workspace_path.resolve() == project_root.resolve():
            return
        claude_projects = Path.home() / ".claude" / "projects"
        if not claude_projects.is_dir():
            return
        main_memory = claude_projects / _claude_project_slug(project_root) / "memory"
        if not main_memory.is_dir():
            return
        worktree_proj_dir = claude_projects / _claude_project_slug(workspace_path)
        worktree_proj_dir.mkdir(parents=True, exist_ok=True)
        link = worktree_proj_dir / "memory"
        if link.is_symlink():
            try:
                if link.resolve() == main_memory.resolve():
                    return
            except OSError:
                pass
            link.unlink()
        elif link.exists():
            # Real directory already present — don't clobber unknown user data.
            return
        link.symlink_to(main_memory, target_is_directory=True)
    except OSError as exc:
        _cu._log(f"⚠ WORKSPACE  failed to propagate Claude memory: {exc}")


def _resolve_setup_command(cmd: str) -> str:
    """Replace {forge_python} with the shell-safe path to the running interpreter.

    Uses shlex.quote so that paths containing spaces or shell-sensitive characters
    do not break shell=True invocations.
    """
    return cmd.replace("{forge_python}", shlex.quote(sys.executable))


def _read_last_setup_command(workspace_path: Path) -> str | None:
    """Read the last setup_command run in this workspace, if any."""
    path = workspace_path / ".forge" / "last_setup_command"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write_last_setup_command(workspace_path: Path, cmd: str) -> None:
    """Persist the last setup_command run in this workspace."""
    forge_dir = workspace_path / ".forge"
    forge_dir.mkdir(parents=True, exist_ok=True)
    (forge_dir / "last_setup_command").write_text(cmd, encoding="utf-8")


def _run_setup_split(setup_command: str, workspace_path: Path) -> tuple[bool, str]:
    """Run workspace setup, splitting venv creation from install.

    Matches {forge_python} template before substitution to avoid parsing
    shlex.quote() output; falls back to legacy form; then runs verbatim.
    """
    last_setup_command = _read_last_setup_command(workspace_path)
    if last_setup_command is not None and last_setup_command != setup_command:
        _cu._log("⚠ WORKSPACE  setup_command changed — re-running install")

    m = _VENV_GUARD_TEMPLATE_RE.search(setup_command)
    if m:
        install_cmd = m.group(1).strip()
        python_exe = shlex.quote(sys.executable)
        ok, out = _cu._run_shell(f"test -d .venv || {python_exe} -m venv .venv", workspace_path)
        if not ok:
            return ok, out
        ok, out = _cu._run_shell(install_cmd, workspace_path)
        if ok:
            _write_last_setup_command(workspace_path, setup_command)
        return ok, out

    cmd = _resolve_setup_command(setup_command)
    m = _VENV_GUARD_RE.search(cmd)
    if not m:
        ok, out = _cu._run_shell(cmd, workspace_path)
        if ok:
            _write_last_setup_command(workspace_path, setup_command)
        return ok, out
    python_exe = m.group(1)
    install_cmd = m.group(2).strip()
    ok, out = _cu._run_shell(f"test -d .venv || {python_exe} -m venv .venv", workspace_path)
    if not ok:
        return ok, out
    ok, out = _cu._run_shell(install_cmd, workspace_path)
    if ok:
        _write_last_setup_command(workspace_path, setup_command)
    return ok, out


def _resolve_merge_conflicts(
    project_root: Path,
    branch_name: str,
    task_name: str,
    config: ForgeConfig,
    workspace_path: Path,
) -> bool:
    """Auto-resolve merge conflicts via dev agent. Returns True if gate passed.
    On failure, aborts the in-progress merge and returns False.
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
    Returns dict with keys: attempted, merged, base_branch, error.
    When config is provided, auto-resolves merge conflicts via dev agent.
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
        info["landing_path"] = "zero-delta"
        info["skipped"] = True
        info["skip_reason"] = "zero-delta branch"
        info["guard_evidence"] = {
            "branch": branch_name,
            "base_branch": base_branch,
            "ahead_commit_count": 0,
        }
        _cu._log(
            f"LANDING zero-delta short-circuit: branch {branch_name} has 0 commits ahead of "
            f"{base_branch} — nothing to merge"
        )
        return info

    _deindex_forge_artifacts(workspace_path, purge=True)

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
    info["landing_path"] = "fresh-merge"
    info["guard_evidence"] = {
        "branch": branch_name,
        "base_branch": base_branch,
    }
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
        _remove_leftover_worktree_dir(workspace_path)

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


def _is_forge_owned_worktree_leftover(path: Path) -> bool:
    """Return True when a leftover worktree path is empty or contains only .forge/ content."""
    try:
        for child in path.rglob("*"):
            rel = child.relative_to(path)
            if not rel.parts or rel.parts[0] != ".forge":
                return False
    except FileNotFoundError:
        return True
    return True


def _remove_leftover_worktree_dir(path: Path) -> None:
    """Remove a leftover worktree shell when only Forge-owned artifacts remain."""
    if not path.exists():
        return
    if not path.is_dir():
        _cu._log(f"  Warning: leftover worktree path is not a directory: {path}")
        return
    if not _is_forge_owned_worktree_leftover(path):
        _cu._log(f"  Warning: leftover worktree has unexpected contents, preserving: {path}")
        return
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        _cu._log(f"  Warning: failed to delete leftover worktree directory {path}: {exc}")
    else:
        _cu._log(f"  Removed leftover worktree directory: {path}")


def _is_stale_worktree(path: Path, base_branch: str, config: ForgeConfig) -> tuple[bool, str]:
    """Check whether an existing worktree is stale and should be removed."""
    stale_days: int = config.workspace.stale_worktree_days

    ok, branch_out = _cu._run_shell("git rev-parse --abbrev-ref HEAD", path)
    if not ok or not branch_out.strip() or branch_out.strip() == "HEAD":
        return True, "branch not found or detached HEAD — removing (corrupted state)"

    branch_name = branch_out.strip()

    if (path / ESCALATED_MARKER_PATH).exists():
        return False, "escalate marker present — preserving worktree"

    ok, status_out = _cu._run_shell("git status --porcelain", path)
    if not ok:
        return False, f"Cannot determine worktree status — git status failed: {status_out.strip()}"
    if status_out.strip():
        return False, "uncommitted changes present — preserving worktree"

    if stale_days == 0:
        return True, "stale_worktree_days=0 — always removing (CI/automated mode)"

    ok, log_out = _cu._run_shell(
        f"git log {base_branch}..{branch_name} --oneline",
        config.project_root,
    )
    if not ok:
        return False, f"Cannot determine branch state — git log failed: {log_out.strip()}"
    commits_ahead = [ln for ln in log_out.strip().splitlines() if ln.strip()]
    if not commits_ahead:
        return True, f"0 commits ahead of {base_branch} — removing (stale)"
    n = len(commits_ahead)
    return False, f"{n} commit{'s' if n != 1 else ''} ahead of {base_branch}"


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
        _remove_leftover_worktree_dir(path)

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


def sweep_orphan_worktrees(project_root: Path, config: ForgeConfig) -> None:
    """Remove orphaned or safely-removable worktrees under .forge/worktrees/."""
    worktrees_root = project_root / ".forge" / "worktrees"
    if not worktrees_root.exists():
        return

    ok, output = _cu._run_shell("git worktree list --porcelain", project_root)
    registered: dict[Path, str | None] = {}
    if ok:
        current_path: Path | None = None
        for line in output.splitlines():
            if line.startswith("worktree "):
                current_path = Path(line[len("worktree ") :]).resolve()
                registered[current_path] = None
            elif line.startswith("branch ") and current_path is not None:
                ref = line[len("branch ") :]
                if ref.startswith("refs/heads/"):
                    registered[current_path] = ref.removeprefix("refs/heads/")

    lock_dir = project_root / ".forge" / "locks"
    base_branch = config.workspace.base_branch
    remote_base = f"origin/{base_branch}"

    for candidate in worktrees_root.iterdir():
        if not candidate.is_dir():
            continue
        slug = candidate.name
        if (lock_dir / f"{slug}.lock").exists():
            continue
        if (candidate / ESCALATED_MARKER_PATH).exists():
            continue

        resolved_candidate = candidate.resolve()
        branch_name = registered.get(resolved_candidate)
        if branch_name is None and resolved_candidate not in registered:
            if _is_forge_owned_worktree_leftover(candidate):
                shutil.rmtree(candidate, ignore_errors=True)
            continue

        if branch_name is None:
            continue
        ok_status, status_out = _cu._run_shell("git status --porcelain", candidate)
        if not ok_status or status_out.strip():
            continue
        ok_gone, gone_out = _cu._run_shell(
            f"git rev-parse --verify --quiet refs/remotes/origin/{branch_name}", project_root
        )
        branch_gone = not ok_gone or not gone_out.strip()
        ok_merged, merged_out = _cu._run_shell(f"git branch --merged {remote_base}", project_root)
        merged = False
        if ok_merged:
            merged_branches = {
                line.strip().lstrip("* ").strip()
                for line in merged_out.splitlines()
                if line.strip()
            }
            merged = branch_name in merged_branches
        if not (merged or branch_gone):
            continue
        _remove_worktree(candidate, branch_name, project_root, "sweep: merged or branch gone")


def _check_behind_origin(config: ForgeConfig) -> None:
    """Log an informational note if base_branch is behind origin.
    Silently skips if rev-list fails. Purely informational — never blocks.
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


_FORGE_ARTIFACTS = (".forge/handoff.yaml", ".forge/trajectory.yaml", ".forge/last_setup_command")


def _deindex_forge_artifacts(workspace_path: Path, *, purge: bool = False) -> None:
    """Remove transient .forge artifacts from the git index (git rm -f --cached --ignore-unmatch).
    With purge=True, also deletes the files from disk to prevent stale state in reused worktrees.
    Omit purge on validate-phase callers that need the handoff on disk. Safe to run repeatedly.
    """
    files_arg = " ".join(_FORGE_ARTIFACTS)
    ok, out = _cu._run_shell(f"git rm -f --cached --ignore-unmatch {files_arg}", workspace_path)
    if not ok:
        _cu._log(f"⚠ WORKSPACE  git rm --cached failed: {out.strip()}")

    if purge:
        for artifact in _FORGE_ARTIFACTS:
            artifact_path = workspace_path / artifact
            try:
                artifact_path.unlink(missing_ok=True)
            except OSError as exc:
                _cu._log(f"⚠ WORKSPACE  failed to delete {artifact}: {exc}")


def _sync_base_branch(config: ForgeConfig, current_branch: str) -> tuple[bool, str]:
    """Sync the local base branch from origin using the safest fast path."""
    base_branch = config.workspace.base_branch
    if current_branch == base_branch:
        return _cu._run_shell(f"git pull --ff-only origin {base_branch}", config.project_root)
    return _cu._run_shell(f"git fetch origin {base_branch}:{base_branch}", config.project_root)


def _branch_sync_delta(config: ForgeConfig, base_branch: str) -> tuple[int | None, int | None]:
    """Return (ahead, behind) for local base vs origin/base, or (None, None) if unavailable."""
    try:
        ok_a, a = _cu._run_shell(
            f"git rev-list --count origin/{base_branch}..{base_branch}", config.project_root
        )
        ok_b, b = _cu._run_shell(
            f"git rev-list --count {base_branch}..origin/{base_branch}", config.project_root
        )
        if ok_a and ok_b:
            return int(a.strip()), int(b.strip())
    except Exception:
        pass
    return None, None


def _recover_base_branch_sync(
    config: ForgeConfig,
    *,
    current_branch: str,
    pull_out: str,
    ahead: int | None,
    behind: int | None,
) -> tuple[bool, str]:
    """Attempt a conservative recovery after a base-branch sync failure."""
    base_branch = config.workspace.base_branch
    if ahead not in (None, 0):
        return (
            False,
            f"WORKSPACE abort: base branch '{base_branch}' has local commits and cannot be "
            "auto-recovered safely. Resolve it manually before rerunning the sprint.",
        )

    ok_fetch, fetch_out = _cu._run_shell(f"git fetch origin {base_branch}", config.project_root)
    if not ok_fetch:
        return (
            False,
            f"WORKSPACE abort: pull failed for base branch '{base_branch}', and recovery fetch "
            f"also failed — {fetch_out.strip() or pull_out.strip()}",
        )

    if current_branch == base_branch:
        ok_status, status_out = _cu._run_shell("git status --porcelain", config.project_root)
        if not ok_status:
            return (
                False,
                f"WORKSPACE abort: pull failed for base branch '{base_branch}', and recovery "
                f"could not verify a clean worktree — {status_out.strip() or pull_out.strip()}",
            )
        if status_out.strip():
            return (
                False,
                f"WORKSPACE abort: pull failed for base branch '{base_branch}', but automatic "
                "reset recovery was skipped because the project root has local changes. "
                f"Clean the worktree or rebase onto origin/{base_branch} before rerunning.",
            )
        _cu._log(
            f"⚠ WORKSPACE  pull failed while {base_branch} was checked out — "
            f"recovering with fetch + reset to origin/{base_branch}"
        )
        return _cu._run_shell(f"git reset --hard origin/{base_branch}", config.project_root)

    _cu._log(
        f"⚠ WORKSPACE  ref update for {base_branch} failed — "
        f"recovering with fetch + branch -f origin/{base_branch}"
    )
    return _cu._run_shell(
        f"git branch -f {base_branch} origin/{base_branch}",
        config.project_root,
    )


def pull_base_branch(config: ForgeConfig) -> bool:
    """Pull the base branch once before parallel workspace creation.
    Uses --ff-only (checked out) or fetch origin base:base (not checked out).
    Re-execs if src/theforge source changes. Raises RuntimeError if base
    branch diverged from origin or origin is unreachable.
    """
    base_branch = config.workspace.base_branch

    ok_before, tree_before = _cu._run_shell("git rev-parse HEAD:src/theforge", config.project_root)
    if not ok_before:
        tree_before = ""

    _, current_branch = _cu._run_shell("git rev-parse --abbrev-ref HEAD", config.project_root)
    with FETCH_LOCK:
        current_branch = current_branch.strip()
        ok_pull, pull_out = _sync_base_branch(config, current_branch)
        if not ok_pull and "incorrect old value" in pull_out:
            _cu._log("⚠ WORKSPACE  fetch race detected — retrying fetch once")
            time.sleep(1)
            ok_pull, pull_out = _sync_base_branch(config, current_branch)
        ahead, behind = _branch_sync_delta(config, base_branch)
        if not ok_pull and ahead == 0 and behind and behind > 0:
            ok_pull, pull_out = _recover_base_branch_sync(
                config,
                current_branch=current_branch,
                pull_out=pull_out,
                ahead=ahead,
                behind=behind,
            )
    if ok_pull:
        _cu._log(f"✓ WORKSPACE  pulled latest {base_branch}")
    else:
        _cu._log(f"⚠ WORKSPACE  pull failed (non-ff / offline): {pull_out.strip()}")
        if ahead is not None and behind is not None and ahead > 0 and behind > 0:
            raise RuntimeError(
                f"WORKSPACE abort: base branch '{base_branch}' has diverged from origin"
                f" (local is {ahead} ahead, {behind} behind)."
                f" Run: git rebase origin/{base_branch}"
            )
        raise RuntimeError(
            f"WORKSPACE abort: pull failed for base branch '{base_branch}' — {pull_out.strip()}"
        )

    ok_after, tree_after = _cu._run_shell("git rev-parse HEAD:src/theforge", config.project_root)
    if not ok_after:
        tree_after = ""

    if tree_before and tree_after and tree_before.strip() != tree_after.strip():
        _cu._log("Source updated after pull — re-execing to load new code")
        os.chdir(str(config.project_root))
        if _prev := _find_run_id_for_pid(config.project_root, os.getpid()):
            os.environ["FORGE_PREV_RUN_ID"] = _prev
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except OSError:
            raise SystemExit(
                "Source files changed after git pull"
                " — re-run forge sprint to pick up the new code."
            )

    return True


def _create_workspace(
    config: ForgeConfig, task: TaskStory, *, no_pull: bool = False
) -> tuple[Path | None, str | None, str | None]:
    """Create an isolated workspace. Returns (path, branch, error)."""
    slug = task.slug
    cmd = config.workspace.create_command.format(
        slug=slug,
        base_branch=config.workspace.base_branch,
    )
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
            if config.workspace.setup_command:
                _cu._log(f"Running workspace setup: {config.workspace.setup_command}")
                ok_s, out_s = _run_setup_split(config.workspace.setup_command, workspace_path)
                if not ok_s:
                    return None, None, f"Workspace setup command failed: {out_s}"
            _deindex_forge_artifacts(workspace_path, purge=True)
            _propagate_claude_memory(config.project_root, workspace_path)
            return workspace_path, branch_name, None

    if not no_pull:
        try:
            pull_base_branch(config)
        except RuntimeError as exc:
            return None, None, str(exc)

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
                _deindex_forge_artifacts(existing_wt, purge=True)
                _propagate_claude_memory(config.project_root, existing_wt)
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
            _deindex_forge_artifacts(workspace_path, purge=True)
            _propagate_claude_memory(config.project_root, workspace_path)
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

    _deindex_forge_artifacts(workspace_path, purge=True)
    _propagate_claude_memory(config.project_root, workspace_path)
    return workspace_path, branch_name, None
