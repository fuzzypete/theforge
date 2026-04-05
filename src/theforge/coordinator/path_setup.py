"""Helpers for ensuring coordinator imports resolve from the active worktree."""

from __future__ import annotations

import sys
from pathlib import Path


def prepend_worktree_src(worktree_path: Path) -> None:
    """Prepend the worktree's src/ directory to sys.path when present.

    Parallel sprint workers run inside isolated git worktrees, but the Python
    process may have been launched from the project root. Prepending the active
    worktree's src/ directory ensures imports resolve against the worker's own
    checkout instead of a concurrently changing root checkout.
    """
    src_path = (worktree_path / "src").resolve()
    if not src_path.is_dir():
        return

    src_str = str(src_path)
    try:
        sys.path.remove(src_str)
    except ValueError:
        pass
    sys.path.insert(0, src_str)
