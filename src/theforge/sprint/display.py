"""Sprint display helpers: story headers and worker status lines."""

from __future__ import annotations

import sys
from concurrent.futures import Future

from .dag import StoryDAG


def _story_header(idx: int, total: int, slug: str) -> str:
    """Format a story header line: [N/total] slug ─────... (fills to 60 chars)."""
    prefix = f"[{idx}/{total}] {slug} "
    dashes = "─" * max(0, 60 - len(prefix))
    return prefix + dashes


def _print_worker_status(
    active: "dict[str, Future[object]]",
    worker_phases: dict[str, str],
    dag: StoryDAG,
    total: int,
) -> None:
    """Print one status line per active worker, plus a summary of waiting stories."""
    if not active:
        return
    lines = []
    for slug in sorted(active):
        phase = worker_phases.get(slug, "RUNNING")
        lines.append(f"  [{slug}] {phase}")
    waiting = dag.remaining()
    waiting_active = [t for t in waiting if t.slug not in active]
    if waiting_active:
        names = ", ".join(t.slug for t in waiting_active[:5])
        suffix = f" (+{len(waiting_active) - 5} more)" if len(waiting_active) > 5 else ""
        lines.append(f"  waiting: {names}{suffix}")
    if lines:
        print("\n".join(lines), file=sys.stderr, flush=True)
