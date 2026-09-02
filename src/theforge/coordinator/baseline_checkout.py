"""A clean baseline checkout for read-only agent invocations.

Three phases need the same thing: a throwaway directory holding the base
branch's tree, with no worktree state, no uncommitted edits, and no ``.git`` —
so an agent asked to *judge* rather than to *change* reads what is actually on
the base branch and cannot commit, stage, or push from where it stands.

Preflight materializes one so its verdict is deterministic; the escalation
advisor materializes one so a fresh advisor never inherits the failed run's
working tree; the preflight decomposition assessment materializes one for both
reasons at once.

This lives in its own module rather than in whichever phase happened to write it
first. When ``preflight_flow`` owned it, every later read-only invocation had to
reach into the preflight phase to borrow it — an import edge from an advisory
step back into a phase module, which is both backwards and (once three of them
existed) circular.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path


def prepare_baseline_checkout(
    project_root: Path, base_branch: str
) -> tuple[Path, Callable[[], None]]:
    """Materialize a clean checkout of ``base_branch``; return it and its cleanup.

    ``git archive`` piped into ``tar`` rather than a clone or a worktree: it
    produces the tree *without* a ``.git`` directory, so the result is
    structurally incapable of carrying a commit back. A repository with no
    ``.git`` at all yields an empty directory rather than an error — a caller
    that wants to fail on that must check for itself.

    The returned callable removes the directory and is safe to call once, in a
    ``finally``.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="forge-preflight-"))
    git_dir = project_root / ".git"
    if not git_dir.exists():
        return temp_dir, lambda: shutil.rmtree(temp_dir, ignore_errors=True)

    try:
        archive = subprocess.run(
            ["git", "archive", base_branch],
            cwd=project_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["tar", "-xmf", "-"],
            cwd=temp_dir,
            input=archive.stdout,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to archive baseline branch {base_branch!r}") from exc

    def cleanup() -> None:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return temp_dir, cleanup
