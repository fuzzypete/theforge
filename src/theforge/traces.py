"""Best-effort trace artifact writer for post-mortem debugging.

Writes raw agent inputs/outputs to .forge/traces/ under the worktree.
All failures are caught and logged as warnings — trace writes never crash
or block the pipeline.
"""

from __future__ import annotations

from pathlib import Path

from . import coord_util as _cu


def write_trace(path: Path, content: str) -> None:
    """Write *content* to *path*, creating parent directories as needed.

    Best-effort: any exception is caught and logged as a warning.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _cu._log(f"Warning: trace write failed for {path}: {exc}")
