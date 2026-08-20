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

from theforge.process_group import (
    group_has_running_members,
    kill_agent_group,
)

__all__ = [
    "InheritedAgentGroup",
    "LivenessResolution",
    "await_inherited_agents",
    "reclaim_inherited_agents",
    "resolve_inherited_agents",
    "resolve_liveness",
    "resolve_live_story_slugs",
    "unresolved_liveness",
]


@dataclass(frozen=True)
class InheritedAgentGroup:
    """One agent process group this process spawned before the re-exec."""

    slug: str
    pgid: int
    sidecar: Path


@dataclass(frozen=True)
class LivenessResolution:
    """What a liveness lookup established — including what it could not.

    ``live_slugs`` are *confirmed* live: a sidecar owned by this pid names the
    slug's worktree and its group answered "alive". ``unresolved_slugs`` are the
    slugs the lookup could not answer for, because a sidecar was unreadable or
    malformed, the agents directory could not be listed, or the liveness probe
    itself failed. The distinction is the whole point of this type: an empty
    ``live_slugs`` alone cannot tell a caller whether nothing is running or
    whether nothing could be *asked*, and treating the second as the first is how
    a sprint's own running story became a foreign worktree collision (#2079).

    Unresolved is scoped to slugs whose worktree exists on disk: a slug with no
    worktree has nothing that could be live, so a scan failure says nothing about
    it and it stays fully schedulable.
    """

    live_slugs: frozenset[str] = frozenset()
    unresolved_slugs: frozenset[str] = frozenset()
    failures: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        """True when every candidate slug's liveness was actually established."""
        return not self.unresolved_slugs

    @property
    def deferred_slugs(self) -> frozenset[str]:
        """Slugs that must not be reconciled as foreign state by this launch.

        Confirmed-live and unresolved alike: both may have an agent of this
        generation writing to their worktree right now.
        """
        return frozenset(self.live_slugs | self.unresolved_slugs)


def unresolved_liveness(slugs: Iterable[str], *, reason: str) -> LivenessResolution:
    """A resolution that established nothing — every slug stays unresolved.

    The fail-closed answer for a caller whose lookup could not run at all (a bad
    config, an import failure). Coarser than :func:`resolve_liveness`, which can
    scope the taint to slugs that actually have a worktree, because a failure
    this early means even that much is unknown.
    """
    return LivenessResolution(
        live_slugs=frozenset(),
        unresolved_slugs=frozenset(s for s in slugs if s),
        failures=(reason,),
    )


def _agent_sidecars(project_root: Path) -> tuple[list[Path], str | None]:
    """Sidecar paths, plus a failure description when they could not be listed."""
    agents_dir = Path(project_root) / ".forge" / "runs" / "agents"
    if not agents_dir.exists():
        return [], None
    try:
        return sorted(agents_dir.glob("*.json")), None
    except OSError as exc:
        return [], f"agents directory {agents_dir} could not be listed: {exc}"


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


def _scan_inherited_agents(
    worktree_to_slug: dict[Path, str],
    root: Path,
    *,
    owner_pid: int | None,
    only_live: bool,
    is_group_alive: Callable[[int], bool],
) -> tuple[list[InheritedAgentGroup], list[tuple[str | None, str]]]:
    """Scan the sidecar directory, returning groups and unresolved-liveness facts.

    Each failure is ``(slug_or_None, description)``. ``None`` means the failure
    could not be attributed to one slug — an unreadable sidecar names no worktree,
    so it may belong to any of them and taints them all.
    """
    failures: list[tuple[str | None, str]] = []
    sidecars, listing_failure = _agent_sidecars(root)
    if listing_failure is not None:
        failures.append((None, listing_failure))

    my_pid = os.getpid() if owner_pid is None else int(owner_pid)
    found: list[InheritedAgentGroup] = []
    for sidecar in sidecars:
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            failures.append((None, f"sidecar {sidecar.name} is unreadable: {exc}"))
            continue
        if not isinstance(data, dict):
            failures.append((None, f"sidecar {sidecar.name} is not a record"))
            continue
        if data.get("owner_pid") != my_pid:
            # Another process's group: not this sprint generation's work.
            continue
        pgid = data.get("pgid")
        sandbox_raw = data.get("sandbox_dir")
        if not isinstance(pgid, int) or not sandbox_raw:
            failures.append((None, f"sidecar {sidecar.name} names no pgid/sandbox"))
            continue
        try:
            sandbox = Path(str(sandbox_raw)).resolve()
        except OSError as exc:
            failures.append((None, f"sidecar {sidecar.name} sandbox is unresolvable: {exc}"))
            continue
        slug = _slug_for_sandbox(sandbox, worktree_to_slug)
        if slug is None:
            # Ours, but for a story outside this launch's slug set — knowable,
            # and irrelevant here.
            continue
        if only_live:
            try:
                alive = is_group_alive(pgid)
            except Exception as exc:  # noqa: BLE001 - a failed probe is unresolved, not "dead"
                failures.append((slug, f"liveness probe for pgid {pgid} failed: {exc}"))
                continue
            if not alive:
                continue
        found.append(InheritedAgentGroup(slug=slug, pgid=pgid, sidecar=sidecar))
    return found, failures


def resolve_inherited_agents(
    slugs: Iterable[str],
    *,
    project_root: Path,
    path_pattern: str,
    owner_pid: int | None = None,
    only_live: bool = True,
    is_group_alive: Callable[[int], bool] = group_has_running_members,
) -> list[InheritedAgentGroup]:
    """Agent groups spawned by this process for any of *slugs*.

    A group qualifies when its sidecar records ``owner_pid`` equal to this
    process's pid — the pid survives ``os.execv``, so this is exactly "spawned by
    me, before the re-exec" — and its ``sandbox_dir`` lies inside that slug's
    worktree. With ``only_live`` (the default) dead groups are filtered out;
    pass ``False`` to reach their sidecar records for cleanup.

    Returns only what it could positively establish. Callers that must not read
    "found nothing" as "nothing is running" use :func:`resolve_liveness`, which
    reports the same scan's failures instead of discarding them.
    """
    root = Path(project_root)
    worktree_to_slug = _worktree_index(slugs, root, path_pattern)
    if not worktree_to_slug:
        return []
    groups, _failures = _scan_inherited_agents(
        worktree_to_slug,
        root,
        owner_pid=owner_pid,
        only_live=only_live,
        is_group_alive=is_group_alive,
    )
    return groups


def _existing_worktree_slugs(worktree_to_slug: dict[Path, str]) -> set[str]:
    present: set[str] = set()
    for path, slug in worktree_to_slug.items():
        try:
            if path.exists():
                present.add(slug)
        except OSError:
            # Cannot even tell whether the worktree is there — assume it may be.
            present.add(slug)
    return present


def resolve_liveness(
    slugs: Iterable[str],
    *,
    project_root: Path,
    path_pattern: str,
    owner_pid: int | None = None,
    is_group_alive: Callable[[int], bool] = group_has_running_members,
) -> LivenessResolution:
    """Establish, per slug, whether this process still has an agent running for it.

    Fail-closed: any part of the lookup that could not be completed leaves the
    affected slugs *unresolved* rather than silently absent from ``live_slugs``.
    A launch guard must be able to tell "this story is definitely not running"
    from "nobody could say", because only the first justifies reconciling the
    story's worktree as foreign state.
    """
    wanted = [s for s in slugs if s]
    try:
        root = Path(project_root)
        worktree_to_slug = _worktree_index(wanted, root, path_pattern)
        groups, failures = _scan_inherited_agents(
            worktree_to_slug,
            root,
            owner_pid=owner_pid,
            only_live=True,
            is_group_alive=is_group_alive,
        )
    except Exception as exc:  # noqa: BLE001 - a broken scan resolves nothing
        return unresolved_liveness(wanted, reason=f"liveness scan failed: {exc}")

    live = {group.slug for group in groups}
    if not failures:
        return LivenessResolution(live_slugs=frozenset(live))

    if any(slug is None for slug, _msg in failures):
        # An unattributable failure could concern any worktree that exists.
        tainted = _existing_worktree_slugs(worktree_to_slug)
    else:
        tainted = {slug for slug, _msg in failures if slug}
    unresolved = (tainted & set(wanted)) - live
    return LivenessResolution(
        live_slugs=frozenset(live),
        unresolved_slugs=frozenset(unresolved),
        failures=tuple(msg for _slug, msg in failures),
    )


def resolve_live_story_slugs(
    slugs: Iterable[str],
    *,
    project_root: Path,
    path_pattern: str,
    owner_pid: int | None = None,
    is_group_alive: Callable[[int], bool] = group_has_running_members,
) -> set[str]:
    """The subset of *slugs* whose inherited agent group is confirmed running.

    Drops the unresolved set; use :func:`resolve_liveness` where "unknown" and
    "not running" must not be conflated.
    """
    return set(
        resolve_liveness(
            slugs,
            project_root=project_root,
            path_pattern=path_pattern,
            owner_pid=owner_pid,
            is_group_alive=is_group_alive,
        ).live_slugs
    )


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
    is_group_alive: Callable[[int], bool] = group_has_running_members,
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
            settled = resolve_inherited_agents(
                [slug],
                project_root=project_root,
                path_pattern=path_pattern,
                owner_pid=owner_pid,
                only_live=False,
                is_group_alive=is_group_alive,
            )
            if (waited or settled) and log is not None:
                log(f"IN-FLIGHT {slug}: inherited agent finished; resuming the story")
            _discard_records(settled)
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
    is_group_alive: Callable[[int], bool] = group_has_running_members,
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
