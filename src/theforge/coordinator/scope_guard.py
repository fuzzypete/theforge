"""Committed-diff scope guard: catch out-of-scope environment config before it lands.

A dev iteration owns tree mutation, so anything the dev agent commits into its
feature branch flows through to REVIEW and integration unchecked. The failure
this guards against (theforge #1615, hdp #96): an agent whose worktree env broke
mid-run diagnoses it correctly and commits a repo-root ``poetry.toml``
(``virtualenvs.in-project = true``) into its feature commit to unblock the gate.
That is *environment scar tissue*, not a deliverable — but nothing downstream
distinguishes the two, so an unrelated repo-root config file ships in the PR.

This is the same class as the earlier ``handoff.yaml`` leak (agents committing
forge's own runtime artifacts), fixed narrowly for one filename. This module
generalizes it to a scope/hygiene check over the DEV-produced *committed* diff:
inspect ``git diff {base}...HEAD --name-only`` against a denylist of repo-root
environment/tooling config plus forge's own runtime artifacts, and flag any match
at the DEV→REVIEW boundary.

The guard fails **closed by escalating**, not by silently stripping. Auto-
rewriting committed history over a heuristic denylist risks dropping a
legitimately in-scope file (a story about poetry packaging may deliberately edit
``poetry.toml``); the violation must be operator-visible so a human decides.

Anchored to repo root: env-config patterns match only repo-root basenames (or
prefixes for editor dirs), never nested occurrences that could be legitimate
deliverables — a ``config/.npmrc`` shipped by an npm-tooling story is not scar
tissue, but a repo-root ``.npmrc`` an agent wrote to survive its own broken
install is.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

# Repo-root environment/tooling config an agent may add to unblock its own broken
# worktree. Matched against the repo-root basename only (the path must have no
# ``/``). ``*.local`` is a glob (matched with fnmatch); the rest are exact names.
_ENV_CONFIG_ROOT_NAMES: frozenset[str] = frozenset(
    {
        "poetry.toml",
        ".npmrc",
        ".python-version",
        ".nvmrc",
        ".tool-versions",
    }
)
_ENV_CONFIG_ROOT_GLOBS: tuple[str, ...] = ("*.local",)

# Editor / IDE state directories. Matched as a repo-root path prefix so anything
# beneath them (``.vscode/settings.json``) is flagged, but a nested occurrence
# deeper in the tree is not.
_ENV_CONFIG_ROOT_DIR_PREFIXES: tuple[str, ...] = (".vscode/", ".idea/")

# Forge's own runtime artifacts. Generalizes the earlier filename-specific
# handoff.yaml fix (workspace.py:_FORGE_ARTIFACTS) into this scope class: an
# agent must never land forge's bookkeeping as part of its deliverable.
_FORGE_ARTIFACT_PATHS: frozenset[str] = frozenset(
    {
        ".forge/handoff.yaml",
        ".forge/trajectory.yaml",
        ".forge/last_setup_command",
    }
)


def _is_out_of_scope_config(path: str) -> bool:
    """True if ``path`` (repo-relative) is out-of-scope environment/tooling config.

    Env-config names/globs are anchored to the repo root (no ``/`` in the path),
    so nested occurrences that could be legitimate deliverables are never flagged.
    Editor dirs and forge artifacts are matched by their repo-root prefix / exact
    path.
    """
    if path in _FORGE_ARTIFACT_PATHS:
        return True
    for prefix in _ENV_CONFIG_ROOT_DIR_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    if "/" in path:
        # Env-config names and globs are repo-root only.
        return False
    if path in _ENV_CONFIG_ROOT_NAMES:
        return True
    return any(fnmatch.fnmatch(path, glob) for glob in _ENV_CONFIG_ROOT_GLOBS)


def committed_diff_paths(workspace_path: Path, base_branch: str) -> list[str] | None:
    """Return the paths changed by HEAD relative to ``base_branch``.

    Runs ``git diff {base}...HEAD --name-only`` (three-dot: changes on HEAD since
    the merge-base, i.e. the dev iteration's own committed work). Tries
    ``origin/{base}`` first, then the local ref.

    Returns the changed-path list on the first ref that diffs cleanly — an empty
    list means the diff was computed and is genuinely empty. Returns ``None``
    when *neither* ref can be diffed (both git invocations error), so the caller
    can distinguish an inspection failure from a real empty diff and fail closed
    rather than waving the branch through unchecked (see #1615 review). This is a
    deliberate departure from commit_guard's fail-open posture: a scope guard
    that cannot see the diff must not certify it clean.
    """
    for ref in (f"origin/{base_branch}", base_branch):
        try:
            proc = subprocess.run(
                ["git", "diff", f"{ref}...HEAD", "--name-only"],
                cwd=str(workspace_path),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        return [line for line in proc.stdout.splitlines() if line.strip()]
    return None


def _has_committed_head(workspace_path: Path) -> bool:
    """True if ``workspace_path`` is a git repo whose HEAD resolves to a commit.

    Distinguishes "there is committed content to inspect but the base ref is
    unreachable" (a real unverifiable state worth escalating) from "no repo /
    no commits yet" (nothing the scope guard protects). Returns False on any git
    error or when HEAD is unborn.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def find_out_of_scope_config(paths: list[str]) -> list[str]:
    """Return the sorted subset of ``paths`` that are out-of-scope config."""
    return sorted({p for p in paths if _is_out_of_scope_config(p)})


def check_committed_scope(
    workspace_path: Path,
    base_branch: str,
) -> tuple[bool, str | None, dict]:
    """Verify the committed diff carries no out-of-scope environment config.

    Returns ``(ok, diagnostic, audit)``. When ``ok`` is False the diagnostic
    names every offending path in operator-grade language (same voice as
    ``check_phase_no_mutation``); auto-stripping is deliberately avoided so the
    operator, not a heuristic, decides whether the file belongs. ``audit`` always
    carries ``diff_paths`` (everything the diff touched) and ``offending`` (the
    flagged subset) for the trail.
    """
    diff_paths = committed_diff_paths(workspace_path, base_branch)
    if diff_paths is None:
        # Neither the remote nor local base ref could be diffed. Two cases:
        #   1. The workspace has a resolvable committed HEAD — there IS content
        #      to inspect but the declared base is unreachable. The guard cannot
        #      see the diff, so it must not certify it clean: fail closed with an
        #      operator-visible diagnostic (invariant: prefer failing closed over
        #      silently continuing with an unverifiable tree). This is the #1615
        #      review P1 fix.
        #   2. No repo / no committed HEAD (unborn HEAD, or not a git worktree at
        #      all) — there is no committed diff for an agent to have leaked
        #      anything into, so there is nothing for the scope guard to protect.
        #      Fail open rather than manufacture a spurious escalation.
        if _has_committed_head(workspace_path):
            audit = {"diff_paths": [], "offending": [], "diff_error": True}
            diagnostic = (
                f"Diff-scope guard could not compute the committed diff against "
                f"base branch '{base_branch}' (neither origin/{base_branch} nor "
                f"{base_branch} could be diffed) even though HEAD carries commits. "
                "Refusing to certify the branch clean of out-of-scope environment "
                "config when the committed diff is unverifiable. Ensure the base "
                "branch is fetched/reachable and retry."
            )
            return False, diagnostic, audit
        return True, None, {"diff_paths": [], "offending": [], "diff_error": False}
    offending = find_out_of_scope_config(diff_paths)
    audit: dict = {"diff_paths": sorted(diff_paths), "offending": offending, "diff_error": False}
    if not offending:
        return True, None, audit
    rendered = ", ".join(offending)
    diagnostic = (
        f"Diff-scope violation: the committed diff carries out-of-scope "
        f"environment/tooling config that the dev phase is not authorized to "
        f"land: {rendered}. These are environment scar tissue (config an agent "
        "commonly adds to unblock its own broken worktree) or forge's own "
        "runtime artifacts, not story deliverables. Remove them from the commit "
        "or, if a file is genuinely in scope for this story, justify it."
    )
    return False, diagnostic, audit
