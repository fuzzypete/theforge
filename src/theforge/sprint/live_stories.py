"""Identify the stories this sprint process still has in flight.

A mid-run re-exec (``os.execv`` after ``workspace.pull_base_branch`` observes new
source) replaces the process *image* but keeps the process *identity*: the pid is
unchanged, and the agent process groups the pre-exec image spawned are still
running. Those groups are recorded in ``.forge/runs/agents/{owner_pid}-{pgid}.json``
by :func:`theforge.process_group.register_agent_group`, together with the
``sandbox_dir`` the agent was launched in — which for a story agent is its
worktree.

That pairing is what lets a re-exec'd process recognise its own work: a sidecar
whose ``owner_pid`` is *this* pid and whose group is still alive names a story
that is still executing right now. Such a story is not foreign state to be
reconciled — it must not be re-dispatched, its worktree must not be swept, and
its presence is the evidence that startup-only checks (the baseline gate) no
longer hold their precondition.

Stdlib-only apart from :mod:`theforge.process_group` (itself stdlib-only), per
convention 4.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from theforge.process_group import group_is_alive

__all__ = ["resolve_live_story_slugs"]


def _agent_sidecars(project_root: Path) -> list[Path]:
    agents_dir = Path(project_root) / ".forge" / "runs" / "agents"
    if not agents_dir.exists():
        return []
    try:
        return sorted(agents_dir.glob("*.json"))
    except OSError:
        return []


def _slug_for_sandbox(sandbox: Path, worktree_to_slug: dict[Path, str]) -> str | None:
    """Map an agent sandbox dir onto the slug whose worktree contains it."""
    for candidate in (sandbox, *sandbox.parents):
        slug = worktree_to_slug.get(candidate)
        if slug is not None:
            return slug
    return None


def resolve_live_story_slugs(
    slugs: Iterable[str],
    *,
    project_root: Path,
    path_pattern: str,
    owner_pid: int | None = None,
    is_group_alive=group_is_alive,
) -> set[str]:
    """Return the subset of *slugs* still being executed by this process.

    A slug qualifies when an agent-group sidecar exists that (a) records
    ``owner_pid`` equal to this process's pid — the pid survives ``os.execv``, so
    this is exactly "spawned by me, before the re-exec" — and (b) names a process
    group that is still alive, launched inside that slug's worktree.

    Best-effort by construction: an unreadable or malformed sidecar is ignored
    rather than raised on. The cost of a miss is today's behaviour (the story is
    treated as foreign), so this must never fail a launch.
    """
    slug_list = [s for s in slugs if s]
    if not slug_list:
        return set()

    root = Path(project_root)
    worktree_to_slug: dict[Path, str] = {}
    for slug in slug_list:
        try:
            worktree = (root / path_pattern.format(slug=slug)).resolve()
        except (KeyError, IndexError, OSError, ValueError):
            continue
        worktree_to_slug[worktree] = slug

    if not worktree_to_slug:
        return set()

    my_pid = os.getpid() if owner_pid is None else int(owner_pid)
    live: set[str] = set()
    for sidecar in _agent_sidecars(root):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("owner_pid") != my_pid:
            # Another process's group: not this sprint generation's work.
            continue
        pgid = data.get("pgid")
        sandbox_raw = data.get("sandbox_dir")
        if not isinstance(pgid, int) or not sandbox_raw:
            continue
        try:
            sandbox = Path(str(sandbox_raw)).resolve()
        except OSError:
            continue
        slug = _slug_for_sandbox(sandbox, worktree_to_slug)
        if slug is None or slug in live:
            continue
        if is_group_alive(pgid):
            live.add(slug)
    return live
