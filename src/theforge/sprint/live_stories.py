"""Agent process groups this sprint inherited across a mid-run re-exec.

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
reconciled — its worktree must not be swept, its outcome is not yet knowable, and
it must not be re-dispatched *while the inherited agent runs*. It is also not
abandoned: the new process image waits for that group to finish
(:func:`await_inherited_agents`), reclaims it if it overruns
(:func:`reclaim_inherited_agents`), and then resumes the story through the normal
triage path. Nothing else will ever pick it up — the sidecar's owner is this pid,
so the moment this process exits, a later invocation's orphan reaper sees a dead
owner and kills the group.

Stdlib-only apart from :mod:`theforge.process_group` (itself stdlib-only), per
convention 4.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from theforge.process_group import group_is_alive, kill_agent_group

__all__ = [
    "InheritedAgentGroup",
    "await_inherited_agents",
    "reclaim_inherited_agents",
    "resolve_inherited_agents",
    "resolve_live_story_slugs",
]


@dataclass(frozen=True)
class InheritedAgentGroup:
    """One agent process group this process spawned before the re-exec."""

    slug: str
    pgid: int
    sidecar: Path


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


def _worktree_index(slugs: Iterable[str], root: Path, path_pattern: str) -> dict[Path, str]:
    index: dict[Path, str] = {}
    for slug in slugs:
        if not slug:
            continue
        try:
            index[(root / path_pattern.format(slug=slug)).resolve()] = slug
        except (KeyError, IndexError, OSError, ValueError):
            continue
    return index


def resolve_inherited_agents(
    slugs: Iterable[str],
    *,
    project_root: Path,
    path_pattern: str,
    owner_pid: int | None = None,
    only_live: bool = True,
    is_group_alive: Callable[[int], bool] = group_is_alive,
) -> list[InheritedAgentGroup]:
    """Agent groups spawned by this process for any of *slugs*.

    A group qualifies when its sidecar records ``owner_pid`` equal to this
    process's pid — the pid survives ``os.execv``, so this is exactly "spawned by
    me, before the re-exec" — and its ``sandbox_dir`` lies inside that slug's
    worktree. With ``only_live`` (the default) dead groups are filtered out;
    pass ``False`` to reach their sidecar records for cleanup.

    Best-effort by construction: an unreadable or malformed sidecar is ignored
    rather than raised on. The cost of a miss is today's behaviour (the story is
    treated as foreign), so this must never fail a launch.
    """
    root = Path(project_root)
    worktree_to_slug = _worktree_index(slugs, root, path_pattern)
    if not worktree_to_slug:
        return []

    my_pid = os.getpid() if owner_pid is None else int(owner_pid)
    found: list[InheritedAgentGroup] = []
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
        if slug is None:
            continue
        if only_live and not is_group_alive(pgid):
            continue
        found.append(InheritedAgentGroup(slug=slug, pgid=pgid, sidecar=sidecar))
    return found


def resolve_live_story_slugs(
    slugs: Iterable[str],
    *,
    project_root: Path,
    path_pattern: str,
    owner_pid: int | None = None,
    is_group_alive: Callable[[int], bool] = group_is_alive,
) -> set[str]:
    """The subset of *slugs* whose inherited agent group is still running."""
    return {
        group.slug
        for group in resolve_inherited_agents(
            slugs,
            project_root=project_root,
            path_pattern=path_pattern,
            owner_pid=owner_pid,
            is_group_alive=is_group_alive,
        )
    }


def _discard_records(groups: Iterable[InheritedAgentGroup]) -> None:
    """Drop sidecars for groups that are finished with.

    The image that registered them is gone, so nothing else will ever unregister
    them; left behind, they are indistinguishable from a live record and invite a
    later reaper to act on a recycled pgid.
    """
    for group in groups:
        try:
            group.sidecar.unlink()
        except OSError:
            pass


def await_inherited_agents(
    slug: str,
    *,
    project_root: Path,
    path_pattern: str,
    timeout: float,
    poll_interval: float = 2.0,
    owner_pid: int | None = None,
    is_group_alive: Callable[[int], bool] = group_is_alive,
    stop_event=None,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Block until no inherited agent group for *slug* is alive.

    Returns True when the story's inherited work has finished (or was never
    running), False when *timeout* elapsed or *stop_event* was set with a group
    still alive — in which case the caller must decide what to do with the
    survivor rather than dispatch a second agent into the same worktree.

    Their sidecar records are dropped once the groups are gone, so the pgids
    cannot be revisited by a later reaper.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    waited = False
    while True:
        groups = resolve_inherited_agents(
            [slug],
            project_root=project_root,
            path_pattern=path_pattern,
            owner_pid=owner_pid,
            is_group_alive=is_group_alive,
        )
        if not groups:
            if waited and log is not None:
                log(f"IN-FLIGHT {slug}: inherited agent finished; resuming the story")
            _discard_records(
                resolve_inherited_agents(
                    [slug],
                    project_root=project_root,
                    path_pattern=path_pattern,
                    owner_pid=owner_pid,
                    only_live=False,
                    is_group_alive=is_group_alive,
                )
            )
            return True
        if stop_event is not None and stop_event.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not waited and log is not None:
            pgids = ", ".join(str(g.pgid) for g in groups)
            log(
                f"IN-FLIGHT {slug}: waiting up to {int(timeout)}s for the agent process "
                f"group(s) {pgids} inherited across the re-exec to finish before resuming"
            )
        waited = True
        time.sleep(min(max(0.01, poll_interval), remaining))


def reclaim_inherited_agents(
    slug: str,
    *,
    project_root: Path,
    path_pattern: str,
    owner_pid: int | None = None,
    is_group_alive: Callable[[int], bool] = group_is_alive,
    kill_group: Callable[[int], bool] = kill_agent_group,
) -> list[int]:
    """Kill any still-live inherited groups for *slug*; return the pgids killed.

    Used when the wait for an inherited agent overruns: the story cannot be
    resumed while another agent is writing to its worktree, and leaving the group
    running would hand it to the next invocation's orphan reaper anyway.
    """
    live = resolve_inherited_agents(
        [slug],
        project_root=project_root,
        path_pattern=path_pattern,
        owner_pid=owner_pid,
        is_group_alive=is_group_alive,
    )
    for group in live:
        kill_group(group.pgid)
    _discard_records(
        resolve_inherited_agents(
            [slug],
            project_root=project_root,
            path_pattern=path_pattern,
            owner_pid=owner_pid,
            only_live=False,
            is_group_alive=is_group_alive,
        )
    )
    return [group.pgid for group in live]
