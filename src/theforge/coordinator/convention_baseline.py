"""Resolve the git ref whose tree supplies the convention baseline.

Hard conventions block on what a story *added*, so *which* commit is treated as
the branch point decides what this story is answerable for. A base ref that lags
the commit the workspace was actually created from moves the branch point
backwards, and every violation another story introduced in between is then
charged to this one.

The resolution therefore prefers the remote-tracking ref when the local base
branch is strictly behind it — the common shape when work lands on the remote
and the local ref is never fast-forwarded.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 10


def resolve_convention_baseline_ref(workspace_path: Path, base_branch: str) -> str | None:
    """Return the merge-base of HEAD and the current base branch, or None."""
    return _merge_base(workspace_path, _current_base_ref(workspace_path, base_branch))


def _current_base_ref(workspace_path: Path, base_branch: str) -> str:
    """Return base_branch, or its remote-tracking ref when the local one is stale.

    A local ref that does not resolve at all is also deferred to the remote —
    there is nothing to be stale *relative to*, and an unresolvable ref would
    otherwise drop the baseline entirely.
    """
    remote_ref = f"origin/{base_branch}"
    if not _rev_parse(workspace_path, remote_ref):
        return base_branch
    if not _rev_parse(workspace_path, base_branch):
        log.info(
            "conventions: local base ref %r does not resolve; "
            "using %r for the convention baseline",
            base_branch,
            remote_ref,
        )
        return remote_ref
    if not _is_ancestor(workspace_path, base_branch, remote_ref):
        return base_branch
    # Local base is an ancestor of the remote-tracking ref: either identical
    # (the substitution is a no-op) or behind it, in which case the remote ref
    # is the branch point the workspace was really cut from.
    return remote_ref


def _rev_parse(workspace_path: Path, ref: str) -> str | None:
    proc = _git(workspace_path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.decode().strip() or None


def _is_ancestor(workspace_path: Path, maybe_ancestor: str, ref: str) -> bool:
    proc = _git(workspace_path, "merge-base", "--is-ancestor", maybe_ancestor, ref)
    return proc is not None and proc.returncode == 0


def _merge_base(workspace_path: Path, base_ref: str) -> str | None:
    proc = _git(workspace_path, "merge-base", "HEAD", base_ref)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.decode().strip() or None


def _git(workspace_path: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    """Run a read-only git command, returning None when it could not run."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=workspace_path,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.SubprocessError, OSError):
        return None
