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
from theforge.process_group import ProcessTeardown
from theforge.task import TaskStory
from theforge.workspace_env import read_venv_base_executable, venv_matches_interpreter

from . import util as _cu
from .branch_landing import BranchLanding, resolve_branch_landing
from .config_snapshot import sync_forge_yaml_into_worktree
from .gate import _run_gate
from .git_lock import FETCH_LOCK
from .run_setup import _rebase_onto_main
from .worktree_drift import DRIFT_HEADER, classify_rebase_conflict
from .worktree_provenance import (
    WorktreeProvenance,
    clear_worktree_provenance,
    evaluate_worktree_provenance,
    provenance_log_lines,
    record_worktree_provenance,
)

# Populated lazily on first call to _resolve_merge_conflicts.
run_agent = None

_MAX_AUTO_RESOLVE_FILES = 5
_CONFLICT_RESOLUTION_TIMEOUT = 120

# Cap on the porcelain excerpt quoted back to the operator in a dirty-root refusal.
_DIRTY_ROOT_SUMMARY_LIMIT = 200

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


def _resolve_setup_command(cmd: str, python_interpreter: str | None = None) -> str:
    """Replace {forge_python} with the shell-safe patient-project interpreter.

    The value comes from ``workspace.python_interpreter``, never from
    ``sys.executable``: the interpreter the orchestrator happens to be installed
    under must not decide which Python the patient project's gate runs on.
    Config load rejects a ``{forge_python}`` template without the pin, so a
    missing value here is a programming error and raises rather than silently
    falling back.

    Uses shlex.quote so that paths containing spaces or shell-sensitive characters
    do not break shell=True invocations.
    """
    if "{forge_python}" not in cmd:
        return cmd
    if not python_interpreter:
        raise ValueError(
            "setup_command uses {forge_python} but no workspace.python_interpreter was "
            "supplied — refusing to substitute the orchestrator's own interpreter"
        )
    return cmd.replace("{forge_python}", shlex.quote(python_interpreter))


def _invalidate_stale_venv(workspace_path: Path, python_interpreter: str | None) -> str | None:
    """Remove a worktree virtualenv not built from the project-pinned interpreter.

    Setup commands guard venv creation with ``test -d .venv ||``, and gate
    commands routinely invoke ``.venv/bin/...`` directly, so an existing
    virtualenv left by a differently-built orchestrator would decide the gate's
    runtime no matter what the pin says. Removing it lets the guard rebuild it
    under the pin, which also reruns dependency installation.

    Returns an error message when a stale virtualenv could not be removed —
    continuing would run the gate under the wrong interpreter — or None.
    """
    if not python_interpreter:
        return None
    venv_path = workspace_path / ".venv"
    if not venv_path.is_dir():
        return None
    if venv_matches_interpreter(venv_path, python_interpreter):
        return None

    recorded = read_venv_base_executable(venv_path) or "unknown"
    _cu._log(
        f"⚠ WORKSPACE  existing .venv was built from {recorded}, not the project-pinned "
        f"{python_interpreter} — removing so it is reprovisioned under the pin"
    )
    try:
        shutil.rmtree(venv_path)
    except OSError as exc:
        return (
            f"Could not remove stale virtualenv {venv_path} built from {recorded} "
            f"(pinned interpreter: {python_interpreter}): {exc}"
        )
    return None


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


def _setup_teardown_kwargs(teardown_out: "list[ProcessTeardown] | None") -> dict:
    """``teardown_out=...`` only when a caller asked for it; see `_create_workspace`."""
    return {} if teardown_out is None else {"teardown_out": teardown_out}


def _run_setup_split(
    setup_command: str,
    workspace_path: Path,
    python_interpreter: str | None = None,
    *,
    timeout: int = 120,
    teardown_out: list[ProcessTeardown] | None = None,
) -> tuple[bool, str]:
    """Run workspace setup, splitting venv creation from install.

    Matches {forge_python} template before substitution to avoid parsing
    shlex.quote() output; falls back to legacy form; then runs verbatim.

    ``python_interpreter`` is the patient project's pinned interpreter. A
    virtualenv already present in the worktree is validated against it before
    anything runs, so a stale one cannot survive the ``test -d .venv`` guard.

    ``teardown_out`` collects any forced process teardown. A setup command is a
    project command like the gate is — it runs whatever the project configured —
    so a worker it leaves behind is killed at release either way, and this is
    what lets the run's record say so rather than only the log (#2309).
    """
    last_setup_command = _read_last_setup_command(workspace_path)
    if last_setup_command is not None and last_setup_command != setup_command:
        _cu._log("⚠ WORKSPACE  setup_command changed — re-running install")

    stale_err = _invalidate_stale_venv(workspace_path, python_interpreter)
    if stale_err is not None:
        return False, stale_err

    m = _VENV_GUARD_TEMPLATE_RE.search(setup_command)
    if m:
        install_cmd = m.group(1).strip()
        if not python_interpreter:
            raise ValueError(
                "setup_command uses {forge_python} but no workspace.python_interpreter was "
                "supplied — refusing to substitute the orchestrator's own interpreter"
            )
        python_exe = shlex.quote(python_interpreter)
        ok, out = _cu._run_shell(
            f"test -d .venv || {python_exe} -m venv .venv",
            workspace_path,
            timeout=timeout,
            expected_python=python_interpreter,
            teardown_out=teardown_out,
        )
        if not ok:
            return ok, out
        ok, out = _cu._run_shell(
            install_cmd,
            workspace_path,
            timeout=timeout,
            expected_python=python_interpreter,
            teardown_out=teardown_out,
        )
        if ok:
            _write_last_setup_command(workspace_path, setup_command)
        return ok, out

    cmd = _resolve_setup_command(setup_command, python_interpreter)
    m = _VENV_GUARD_RE.search(cmd)
    if not m:
        ok, out = _cu._run_shell(
            cmd,
            workspace_path,
            timeout=timeout,
            expected_python=python_interpreter,
            teardown_out=teardown_out,
        )
        if ok:
            _write_last_setup_command(workspace_path, setup_command)
        return ok, out
    python_exe = m.group(1)
    install_cmd = m.group(2).strip()
    ok, out = _cu._run_shell(
        f"test -d .venv || {python_exe} -m venv .venv",
        workspace_path,
        timeout=timeout,
        expected_python=python_interpreter,
        teardown_out=teardown_out,
    )
    if not ok:
        return ok, out
    ok, out = _cu._run_shell(
        install_cmd,
        workspace_path,
        timeout=timeout,
        expected_python=python_interpreter,
        teardown_out=teardown_out,
    )
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
    # This gate decides whether an agent-authored conflict resolution may land,
    # so the run log has to say which profile produced that decision and what
    # authority it carried — the resolution leaves no other artifact stating
    # what was actually verified (#2358).
    gate_selection: list = []
    gate_decision, gate_error, _ = _run_gate(config, workspace_path, selection_out=gate_selection)
    _profile_note = f" [{gate_selection[0].describe()}]" if gate_selection else ""
    _cu._log(f"  Resolution gate: {gate_decision or gate_error}{_profile_note}")
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


def project_root_dirty_status(project_root: Path) -> str:
    """Porcelain status of the project-root checkout; ``""`` when clean.

    A failing ``git status`` yields ``""``: a root we cannot inspect is not
    evidence of dirt, and the landing check has always failed open there.

    Single source of truth for the landing precondition so the entry-time
    refusal and ``_merge_branch``'s refusal can never drift apart.
    """
    ok, out = _cu._run_shell("git status --porcelain", project_root)
    if not ok:
        return ""
    return out.strip()


def story_lands_in_project_root(
    config: ForgeConfig,
    *,
    auto_merge: bool = False,
    lands_in_project_root: bool | None = None,
) -> bool:
    """Whether this story's approval merges into the project-root checkout.

    The default derivation mirrors ``completion._finalize_approve``: the
    ``--auto-merge`` flag forces ``"merge"`` regardless of configuration, and no
    other ``on_approve`` value (``pr``, ``merge-pr``, ``none``) ever touches that
    checkout.

    ``lands_in_project_root`` is the caller's own answer and wins when supplied.
    The sprint scheduler has one: in parallel mode it rewrites a dependency
    parent's landing to a local merge after the story returns
    (``runner._attempt_integration``'s ``pending_integration`` conversion), so
    the story's own ``auto_merge`` and ``on_approve`` understate the obligation.
    """
    if lands_in_project_root is not None:
        return lands_in_project_root
    return auto_merge or config.workspace.on_approve == "merge"


def landing_precondition_error(
    config: ForgeConfig,
    *,
    auto_merge: bool = False,
    lands_in_project_root: bool | None = None,
) -> str | None:
    """Refusal message when the project root cannot accept a landing, else ``None``.

    A dirty project root makes ``_merge_branch`` refuse *after* dev and review
    have been paid for (#2048). The condition is knowable before any agent is
    dispatched, so callers evaluate it at run entry — and re-evaluate it at each
    story's entry, which is the first point a root dirtied mid-sprint can be
    observed while the next story's spend is still avoidable.

    Whether the story lands locally at all comes from
    ``story_lands_in_project_root``; under workflows that never touch the
    project-root checkout its cleanliness is not forge's business and this
    returns ``None``.

    The dirty condition mirrors ``_merge_branch`` exactly — untracked files
    included — so anything refused here is something that would have been
    refused at landing anyway.
    """
    if not story_lands_in_project_root(
        config, auto_merge=auto_merge, lands_in_project_root=lands_in_project_root
    ):
        return None
    if not (config.project_root / ".git").exists():
        return None
    dirty = project_root_dirty_status(config.project_root)
    if not dirty:
        return None
    files = ", ".join(line.strip() for line in dirty.splitlines())[:_DIRTY_ROOT_SUMMARY_LIMIT]
    return (
        "LANDING PRECONDITION: uncommitted changes in project root "
        f"{config.project_root}: {files}. An approved story cannot merge into a "
        "dirty checkout — commit, stash, or revert these before running, so the "
        "refusal does not arrive after dev and review have already been paid for."
    )


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

    dirty = project_root_dirty_status(project_root)
    if dirty:
        info["error"] = f"Uncommitted changes in project root: {dirty[:_DIRTY_ROOT_SUMMARY_LIMIT]}"
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


def _count_unpublished_commits(branch_name: str, project_root: Path) -> int | None:
    """Count commits on ``branch_name`` that no origin remote-tracking ref contains.

    A non-zero count means the branch holds work that exists only in this clone —
    deleting it would destroy the only copy. Returns ``None`` when the count cannot
    be determined; callers must treat that as "do not remove".
    """
    ok, out = _cu._run_shell(
        f"git rev-list --count {branch_name} --not --remotes=origin", project_root
    )
    if not ok:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _local_commit_note(unpublished: int | None) -> str:
    """Describe what the cheap origin probe found, for a preservation message."""
    if unpublished is None:
        return "cannot determine whether its commits exist on origin"
    return f"{unpublished} local commit{'s' if unpublished != 1 else ''} not present on origin"


def _preserve_reason(landing: BranchLanding, unpublished: int | None) -> str:
    """Say why a worktree survived, in terms an operator can act on.

    An undecidable landing names the evidence that was absent, because that is
    what separates a branch holding real unlanded work from one nothing could
    speak for — the same directory, two very different follow-ups (#2795).
    """
    if landing.undecidable:
        return f"landing undecidable — {landing.describe_absent()}"
    return f"no merge evidence; {_local_commit_note(unpublished)}"


def _is_registered_worktree_path(path: Path, project_root: Path) -> bool | None:
    """Return True when git registers ``path`` as a linked worktree.

    Returns ``None`` when ``git worktree list`` cannot be read, so callers can
    fail open rather than delete a directory on an unreadable answer. An empty
    listing counts as unreadable: a real repository always reports at least its
    own main worktree, so a listing with no entries is a failed probe, not a
    statement that ``path`` is unregistered.
    """
    ok, output = _cu._run_shell("git worktree list --porcelain", project_root)
    if not ok:
        return None
    try:
        target = path.resolve()
    except OSError:
        target = path
    saw_entry = False
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        saw_entry = True
        candidate = Path(line[len("worktree ") :])
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved == target:
            return True
    return False if saw_entry else None


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


def sweep_orphan_worktrees(
    project_root: Path,
    config: ForgeConfig,
    *,
    protected_slugs: set[str] | None = None,
) -> None:
    """Remove orphaned or safely-removable worktrees under .forge/worktrees/.

    ``protected_slugs`` names worktrees the caller knows are live work of the
    current run — after a mid-run re-exec, the surviving agent process groups
    (see :mod:`theforge.sprint.live_stories`). Liveness is not derivable from the
    git state this sweep inspects: an agent that has not committed yet leaves a
    clean tree 0 commits ahead of base, which is indistinguishable here from an
    abandoned worktree. The caller knows; the sweep does not. So it is told.
    """
    worktrees_root = project_root / ".forge" / "worktrees"
    if not worktrees_root.exists():
        return
    protected = set(protected_slugs or ())

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
        if slug in protected:
            _cu._log(
                f"⚠ WORKSPACE  preserving worktree {slug} — live agent from this "
                "sprint is still running in it"
            )
            continue
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
        # A clean tree is not evidence that the work is safe: an agent that commits
        # as it goes leaves a clean tree with local-only history. Absence of commits
        # missing from origin establishes the work exists elsewhere, and it also
        # separates a never-pushed branch from one whose remote ref was deleted
        # after merging — both of which lack refs/remotes/origin/<branch>.
        #
        # These local checks run first because they are cheap and they clear the
        # ordinary merged-and-published worktree without any network call.
        unpublished = _count_unpublished_commits(branch_name, project_root)
        if unpublished == 0:
            ok_gone, gone_out = _cu._run_shell(
                f"git rev-parse --verify --quiet refs/remotes/origin/{branch_name}", project_root
            )
            branch_gone = not ok_gone or not gone_out.strip()
            ok_merged, merged_out = _cu._run_shell(
                f"git branch --merged {remote_base}", project_root
            )
            merged = False
            if ok_merged:
                merged_branches = {
                    line.strip().lstrip("+*").strip()
                    for line in merged_out.splitlines()
                    if line.strip()
                }
                merged = branch_name in merged_branches
            if merged or branch_gone:
                info = (
                    f"sweep: merged into {remote_base}; no commits missing from origin"
                    if merged
                    else "sweep: remote branch gone; no commits missing from origin"
                )
                _remove_worktree(candidate, branch_name, project_root, info)
                continue

        # Everything below here the commit-presence and topology checks would have
        # preserved. That is exactly where they are too weak to decide: a squash
        # landing rewrites the branch's commits, so its own SHAs never reach origin
        # and it is never an ancestor of the base branch, whatever it landed
        # (#2795). Ask the shared resolver, which reads the same evidence resume
        # triage does, and let a landed answer clear both gates above.
        landing = resolve_branch_landing(branch_name, base_branch, project_root, slug=slug)
        if landing.landed:
            detail = landing.describe_source(base_branch)
            _cu._log(f"✓ WORKSPACE  {branch_name} landed via {detail} — reclaimable")
            _remove_worktree(candidate, branch_name, project_root, f"sweep: landed via {detail}")
            continue

        _cu._log(
            f"⚠ WORKSPACE  preserving worktree {branch_name} — "
            f"{_preserve_reason(landing, unpublished)}"
        )


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


def _base_branch_lands_locally(config: ForgeConfig, *, auto_merge: bool = False) -> bool:
    """Whether a run merges landed stories into the project-root base checkout.

    Mirrors the effective-``on_approve`` resolution in ``completion.py``: the
    ``--auto-merge`` CLI flag forces ``"merge"`` regardless of configuration,
    so the config value alone cannot answer this — that runtime override is why
    keying the publication rule off ``auto_push`` misclassified workflows.

    This is the *default* derivation for a single run. Callers that know more —
    notably the sprint runner, which also merges dependency-parent stories
    locally whether or not ``--auto-merge`` was passed — compute the answer
    themselves and pass it as ``lands_locally``.
    """
    return auto_merge or config.workspace.on_approve == "merge"


def _base_branch_tracks_origin(config: ForgeConfig, *, lands_locally: bool) -> bool:
    """Whether the project-root base branch is expected to match origin.

    Two independent questions decide this, and conflating them is what made the
    earlier ``auto_push``-only rule wrong in both directions:

    1. Does anything land on the local base branch at all? Only the ``"merge"``
       landing path (``_merge_branch``) does. Under ``on_approve: pr`` and
       ``merge-pr`` the forge never merges into the local checkout, so any
       commit the base branch carries beyond origin is unowned drift — and
       those are precisely the workflows that publish story branches, where a
       contaminated base makes GitHub attribute the extra content to whichever
       story is running.
    2. If stories do land locally, are those merges published? ``_merge_branch``
       pushes only when ``workspace.auto_push`` is set. Without it, the branch
       running ahead of origin is the configured steady state, and treating it
       as drift would abort every multi-story sprint after the first landing.

    So the base branch is expected to track origin unless the run lands stories
    locally *and* has opted out of pushing them. ``lands_locally`` answers (1)
    and is supplied by the caller — see ``_base_branch_lands_locally`` for the
    default derivation and why the sprint runner overrides it.
    """
    if not lands_locally:
        return True
    return config.workspace.auto_push


def _assert_base_branch_published(
    config: ForgeConfig, base_branch: str, *, lands_locally: bool
) -> None:
    """Fail closed when the local base branch carries commits absent from origin.

    A worktree cut from an ahead-only base branch inherits those commits, and
    GitHub diffs the resulting PR against origin/<base> — so the unpublished
    content is attributed to whichever story happens to be running. Since
    commit-based review is the evaluation mechanism, that is a correctness
    failure, not a cosmetic one. ``git pull --ff-only`` succeeds trivially in
    this state, so the delta has to be checked explicitly.

    Only applies when the base branch is supposed to track origin — see
    ``_base_branch_tracks_origin``. Under a local-merge workflow with pushing
    disabled, an ahead-only base branch is the configured steady state, and
    aborting on it would break every multi-story sprint.
    """
    if not _base_branch_tracks_origin(config, lands_locally=lands_locally):
        return
    ahead, _behind = _branch_sync_delta(config, base_branch)
    if ahead is None or ahead <= 0:
        return
    raise RuntimeError(
        f"WORKSPACE abort: base branch '{base_branch}' has {ahead} local commit(s) not on "
        f"origin/{base_branch}. Worktrees cut from it would attribute that content to the "
        f"running story. Run: git push origin {base_branch}"
    )


def _current_checked_out_branch(project_root: Path) -> str:
    """Return the currently checked-out branch for ``project_root``.

    Raises ``RuntimeError`` when the branch cannot be determined. Callers that
    mutate the project-root checkout use this as a fail-closed guard rather than
    acting on whichever ref HEAD happens to resolve to.
    """
    ok_branch, branch_out = _cu._run_shell("git rev-parse --abbrev-ref HEAD", project_root)
    current_branch = branch_out.strip()
    if ok_branch and current_branch:
        return current_branch
    detail = current_branch or branch_out.strip() or "unknown branch state"
    raise RuntimeError(
        f"WORKSPACE abort: could not determine the checked-out branch in {project_root}: {detail}"
    )


def assert_base_branch_checked_out(config: ForgeConfig, *, operation: str) -> str:
    """Fail closed when the live project-root checkout is not ``base_branch``."""
    base_branch = config.workspace.base_branch
    current_branch = _current_checked_out_branch(config.project_root)
    if current_branch != base_branch:
        raise RuntimeError(
            f"WORKSPACE abort: {operation} requires the project root to have base branch "
            f"'{base_branch}' checked out, but '{current_branch}' is checked out. "
            f"Check out {base_branch} and rerun."
        )
    return current_branch


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


def pull_base_branch(config: ForgeConfig, *, lands_locally: bool | None = None) -> bool:
    """Pull the base branch once before parallel workspace creation.
    Uses --ff-only (checked out) or fetch origin base:base (not checked out).
    Re-execs if src/theforge source changes. Raises RuntimeError if base
    branch diverged from origin, origin is unreachable, or the base branch
    carries unpublished commits in a workflow where it must track origin.

    ``lands_locally`` says whether this run merges stories into the local base
    checkout, which decides whether an ahead-only branch is drift or the
    configured steady state. ``None`` derives it from config alone — correct
    for direct/single-story callers, but the sprint runner passes its own
    DAG-aware value.
    """
    base_branch = config.workspace.base_branch
    if lands_locally is None:
        lands_locally = _base_branch_lands_locally(config)

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
        # A successful ff-only pull says nothing about unpublished local commits:
        # ahead-only is exactly the state that pull reports as clean.
        _assert_base_branch_published(config, base_branch, lands_locally=lands_locally)
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


def _sync_base_before_worktree_use(
    config: ForgeConfig, *, lands_locally: bool | None = None
) -> str | None:
    """Blocking base-branch sync shared by fresh, reused, and reattach paths.

    Returns an error message instead of raising so ``_create_workspace`` can
    surface it through its (path, branch, error) contract. Replaces the old
    informational ``_check_behind_origin`` call on the reuse paths, which never
    blocked and so let worktrees be advanced from an unpublished base branch.
    """
    try:
        pull_base_branch(config, lands_locally=lands_locally)
    except RuntimeError as exc:
        return str(exc)
    return None


def _rebase_reused_worktree(workspace_path: Path, base_branch: str) -> str | None:
    """Rebase a reused worktree onto current base. Returns error message on failure.

    A failure here is usually not "git broke" but "the work in this worktree
    predates commits that changed the same files" — the recurring cost of
    preserving an escalated story's worktree across an intervening merge
    (#1993). Classify that condition explicitly; only fall back to relaying the
    tool failure when drift cannot be established.
    """
    rebase_ok, rebase_err = _rebase_onto_main(str(workspace_path), base_branch, None)
    if rebase_ok:
        _cu._log(f"  rebased reused worktree onto origin/{base_branch}")
        return None
    classified = classify_rebase_conflict(workspace_path, base_branch, rebase_err)
    if classified is not None:
        _cu._log(f"⚠ WORKSPACE  {DRIFT_HEADER} on {base_branch}")
        for line in classified.splitlines()[1:]:
            _cu._log(f"  {line}" if line else "")
        return classified
    return (
        f"pre-dev rebase onto {base_branch} failed — conflicts must be resolved manually: "
        f"{rebase_err}"
    )


def _sync_run_forge_yaml(config: ForgeConfig, workspace_path: Path) -> None:
    """Give a freshly created or reused worktree the run's operative forge.yaml.

    Called on every path out of ``_create_workspace``, before the workspace
    setup command runs and therefore before any agent or gate reads the
    worktree. A fresh worktree otherwise carries whatever forge.yaml its base
    branch happened to hold, so a story dispatched after a config change landed
    mid-sprint ran against that change while the sprint record still reported
    the pinned snapshot (#1980). Under a sprint the source is that snapshot;
    a standalone run reads the project root, as before.
    """
    sync_forge_yaml_into_worktree(config.project_root, workspace_path, label="WORKSPACE")


def _judge_worktree_provenance(
    config: ForgeConfig,
    task: TaskStory,
    story_content: str | None,
    *,
    adopted: bool,
) -> WorktreeProvenance:
    """Decide whether the story text that produced this tree still governs, and say so.

    Adopting an existing worktree carries an earlier attempt's output into this
    run, and a working tree carries no provenance an agent can read — so the
    same question the resume record answers about phase records is asked here
    about artifacts, and the answer goes into the run log (#2288). The tree is
    never discarded on the strength of it: see :mod:`worktree_provenance`.
    """
    provenance = evaluate_worktree_provenance(
        config.project_root, task.slug, story_content, adopted=adopted
    )
    for line in provenance_log_lines(provenance):
        _cu._log(line)
    return provenance


def _create_workspace(
    config: ForgeConfig,
    task: TaskStory,
    *,
    no_pull: bool = False,
    lands_locally: bool | None = None,
    story_content: str | None = None,
    teardown_out: list[ProcessTeardown] | None = None,
) -> tuple[Path | None, str | None, str | None]:
    """Create an isolated workspace. Returns (path, branch, error).

    ``teardown_out`` collects any process teardown the configured setup command
    forced, so the run can record it (#2309). It is forwarded only when a caller
    actually supplies one: an out-parameter nobody asked for is a no-op, and not
    sending it leaves the setup call identical for every caller that does not
    want teardowns back.

    ``lands_locally`` is forwarded to the base-branch publication guard: when
    this run merges stories into the local base checkout, the branch is
    expected to run ahead of origin and the guard stands down.

    ``story_content`` is the story text this run executes. Every path out of
    here records it as the provenance of the workspace's contents, and every
    path that *adopts* contents an earlier attempt produced first compares it
    against what is recorded.
    """
    slug = task.slug
    cmd = config.workspace.create_command.format(
        slug=slug,
        base_branch=config.workspace.base_branch,
    )
    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=slug)
    branch_name = config.workspace.branch_pattern.format(slug=slug)

    if workspace_path.exists():
        _cu._log(f"⚠ WORKSPACE  existing worktree found: {workspace_path}")
        # A directory under the managed worktrees root that git does not register
        # as a worktree is residue, not a checkout. Reusing it runs the setup
        # command against a directory with no project in it, and the failure
        # surfaces as a confusing "not a Python project" error (#2111). Note that
        # git commands run inside such a directory resolve against the *parent*
        # repository, so the staleness probe below cannot tell the difference.
        registered = _is_registered_worktree_path(workspace_path, config.project_root)
        if registered is False:
            _cu._log(
                "⚠ WORKSPACE  path is not a registered git worktree — "
                f"treating as stale residue: {workspace_path}"
            )
            _remove_leftover_worktree_dir(workspace_path)
            if workspace_path.exists():
                return (
                    None,
                    None,
                    "Workspace path exists but git does not register it as a worktree "
                    f"and it holds non-Forge contents: {workspace_path}",
                )

    if workspace_path.exists():
        is_stale, info_line = _is_stale_worktree(
            workspace_path, config.workspace.base_branch, config
        )
        _cu._log(f"  {info_line}")
        if is_stale:
            _remove_worktree(workspace_path, branch_name, config.project_root, info_line)
            # The contents whose provenance was recorded no longer exist.
            clear_worktree_provenance(config.project_root, task.slug)
        else:
            _cu._log(f"↻ WORKSPACE  reusing existing worktree: {workspace_path}")
            provenance = _judge_worktree_provenance(config, task, story_content, adopted=True)
            if not no_pull:
                sync_err = _sync_base_before_worktree_use(config, lands_locally=lands_locally)
                if sync_err is not None:
                    return None, None, sync_err
            rebase_err = _rebase_reused_worktree(workspace_path, config.workspace.base_branch)
            if rebase_err is not None:
                return None, None, rebase_err
            _sync_run_forge_yaml(config, workspace_path)
            if config.workspace.setup_command:
                _cu._log(
                    "Running workspace setup "
                    f"(timeout {config.workspace.setup_timeout}s): "
                    f"{config.workspace.setup_command}"
                )
                ok_s, out_s = _run_setup_split(
                    config.workspace.setup_command,
                    workspace_path,
                    config.workspace.python_interpreter,
                    timeout=config.workspace.setup_timeout,
                    **_setup_teardown_kwargs(teardown_out),
                )
                if not ok_s:
                    return None, None, f"Workspace setup command failed: {out_s}"
            _deindex_forge_artifacts(workspace_path, purge=True)
            _propagate_claude_memory(config.project_root, workspace_path)
            record_worktree_provenance(config.project_root, task.slug, provenance)
            return workspace_path, branch_name, None

    if not no_pull:
        sync_err = _sync_base_before_worktree_use(config, lands_locally=lands_locally)
        if sync_err is not None:
            return None, None, sync_err

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
                provenance = _judge_worktree_provenance(config, task, story_content, adopted=True)
                if not no_pull:
                    sync_err = _sync_base_before_worktree_use(config, lands_locally=lands_locally)
                    if sync_err is not None:
                        return None, None, sync_err
                rebase_err = _rebase_reused_worktree(existing_wt, config.workspace.base_branch)
                if rebase_err is not None:
                    return None, None, rebase_err
                _sync_run_forge_yaml(config, existing_wt)
                if config.workspace.setup_command:
                    _cu._log(
                        "Running workspace setup "
                        f"(timeout {config.workspace.setup_timeout}s): "
                        f"{config.workspace.setup_command}"
                    )
                    ok_s, out_s = _run_setup_split(
                        config.workspace.setup_command,
                        existing_wt,
                        config.workspace.python_interpreter,
                        timeout=config.workspace.setup_timeout,
                        **_setup_teardown_kwargs(teardown_out),
                    )
                    if not ok_s:
                        return None, None, f"Workspace setup command failed: {out_s}"
                _deindex_forge_artifacts(existing_wt, purge=True)
                _propagate_claude_memory(config.project_root, existing_wt)
                record_worktree_provenance(config.project_root, task.slug, provenance)
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
            provenance = _judge_worktree_provenance(config, task, story_content, adopted=True)
            if not no_pull:
                sync_err = _sync_base_before_worktree_use(config, lands_locally=lands_locally)
                if sync_err is not None:
                    return None, None, sync_err
            ok_add, add_out = _cu._run_shell(
                f"git worktree add {workspace_path} {branch_name}", config.project_root
            )
            if not ok_add:
                return None, None, f"Failed to reattach worktree: {add_out}"
            rebase_err = _rebase_reused_worktree(workspace_path, config.workspace.base_branch)
            if rebase_err is not None:
                return None, None, rebase_err
            _sync_run_forge_yaml(config, workspace_path)
            if config.workspace.setup_command:
                _cu._log(
                    "Running workspace setup "
                    f"(timeout {config.workspace.setup_timeout}s): "
                    f"{config.workspace.setup_command}"
                )
                ok_s, out_s = _run_setup_split(
                    config.workspace.setup_command,
                    workspace_path,
                    config.workspace.python_interpreter,
                    timeout=config.workspace.setup_timeout,
                    **_setup_teardown_kwargs(teardown_out),
                )
                if not ok_s:
                    return None, None, f"Workspace setup command failed: {out_s}"
            _deindex_forge_artifacts(workspace_path, purge=True)
            _propagate_claude_memory(config.project_root, workspace_path)
            record_worktree_provenance(config.project_root, task.slug, provenance)
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

    _sync_run_forge_yaml(config, workspace_path)
    if config.workspace.setup_command:
        _cu._log(
            "Running workspace setup "
            f"(timeout {config.workspace.setup_timeout}s): "
            f"{config.workspace.setup_command}"
        )
        ok, output = _run_setup_split(
            config.workspace.setup_command,
            workspace_path,
            config.workspace.python_interpreter,
            timeout=config.workspace.setup_timeout,
            **_setup_teardown_kwargs(teardown_out),
        )
        if not ok:
            return None, None, f"Workspace setup command failed: {output}"

    _deindex_forge_artifacts(workspace_path, purge=True)
    _propagate_claude_memory(config.project_root, workspace_path)
    record_worktree_provenance(
        config.project_root,
        task.slug,
        _judge_worktree_provenance(config, task, story_content, adopted=False),
    )
    return workspace_path, branch_name, None
