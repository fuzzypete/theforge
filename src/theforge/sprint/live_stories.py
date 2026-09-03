"""Work this sprint still owns across a mid-run re-exec.

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

A surviving agent group is not the only way a story can be this run's own work,
and treating it as the only way is what produced #2617. A story executing in pure
Python at the instant of the exec — VALIDATE's post-gate bookkeeping, a phase
transition, the commit after an approved review — has no subprocess to survive
it, so a liveness model built solely on pgids reported "nothing is running" for a
story that was. Ownership is therefore also *declared*, by the scheduler, in
:mod:`theforge.sprint.story_executions`, and folded in here: liveness answers "is
this slug this run's own in-flight work?", and either kind of evidence answers it.
Which kind it was still matters to a caller — waiting for a process group is only
meaningful when there is one — so :class:`LivenessResolution` reports it.

Stdlib-only apart from :mod:`theforge.process_group` and
:mod:`theforge.sprint.story_executions` (both stdlib-only), per convention 4.
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

    ``live_slugs`` are *confirmed* live: this run still owns the slug's
    execution, either because a sidecar owned by this pid names its worktree and
    that group answered "alive", or because the scheduler recorded the story as
    dispatched and has not yet settled it. ``unresolved_slugs`` are the slugs the
    lookup could not answer for, because a sidecar or ownership record was
    unreadable or malformed, a registry directory could not be listed, or the
    liveness probe itself failed. The distinction is the whole point of this
    type: an empty ``live_slugs`` alone cannot tell a caller whether nothing is
    running or whether nothing could be *asked*, and treating the second as the
    first is how a sprint's own running story became a foreign worktree collision
    (#2079).

    ``registered_slugs`` names the subset of ``live_slugs`` vouched for *only* by
    an ownership record — this run's in-flight work with no surviving agent
    process group. A caller that is about to wait for a process, or to tell an
    operator one is running, needs to know there is none (#2617).

    Unresolved is scoped to slugs whose worktree exists on disk: a slug with no
    worktree has nothing that could be live, so a scan failure says nothing about
    it and it stays fully schedulable.
    """

    live_slugs: frozenset[str] = frozenset()
    unresolved_slugs: frozenset[str] = frozenset()
    failures: tuple[str, ...] = ()
    registered_slugs: frozenset[str] = frozenset()

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


def _scan_story_executions(
    wanted: set[str],
    root: Path,
    *,
    owner_pid: int | None,
) -> tuple[set[str], list[tuple[str | None, str]]]:
    """Slugs this process declared it is executing, plus unresolved-ownership facts.

    Local import: :mod:`theforge.sprint.story_executions` is stdlib-only, and
    keeping the import here rather than at module scope means a caller that only
    wants agent-group liveness never pays for the registry at all.
    """
    from theforge.sprint.story_executions import scan_story_executions  # noqa: PLC0415

    scan = scan_story_executions(root, owner_pid=owner_pid)
    failures: list[tuple[str | None, str]] = [(None, msg) for msg in scan.failures]
    # A record whose identity could not be checked may be this run's own work and
    # may be a dead run's leftover. That is exactly the "nobody could say" the
    # unresolved bucket exists for — the slug is named, so the taint is precise.
    failures.extend(
        (
            record.slug,
            f"story execution record for {record.slug} carries no comparable process identity",
        )
        for record in scan.unverifiable
        if record.slug in wanted
    )
    return {record.slug for record in scan.owned if record.slug in wanted}, failures


def resolve_liveness(
    slugs: Iterable[str],
    *,
    project_root: Path,
    path_pattern: str,
    owner_pid: int | None = None,
    is_group_alive: Callable[[int], bool] = group_has_running_members,
    include_executions: bool = True,
) -> LivenessResolution:
    """Establish, per slug, whether this process still has that story in flight.

    Two independent kinds of evidence, because neither covers the other's blind
    spot. An **agent process group** this pid spawned and that is still alive
    proves a subprocess is writing to the worktree right now. An **execution
    record** the scheduler wrote before dispatch proves the story is this run's
    responsibility whether or not any subprocess exists — which is the only thing
    that can vouch for a story executing in-process at the instant of a re-exec
    (#2617).

    Fail-closed: any part of the lookup that could not be completed leaves the
    affected slugs *unresolved* rather than silently absent from ``live_slugs``.
    A launch guard must be able to tell "this story is definitely not running"
    from "nobody could say", because only the first justifies reconciling the
    story's worktree as foreign state.

    ``include_executions=False`` restricts the answer to agent process groups.
    Its one caller is :func:`await_inherited_agents`, which is asking a narrower
    question — "is a subprocess still writing here?" — and would otherwise wait
    out its whole timeout on a record that describes itself (#2617).
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
        registered: set[str] = set()
        if include_executions:
            registered, registry_failures = _scan_story_executions(
                set(wanted), root, owner_pid=owner_pid
            )
            failures = [*failures, *registry_failures]
    except Exception as exc:  # noqa: BLE001 - a broken scan resolves nothing
        return unresolved_liveness(wanted, reason=f"liveness scan failed: {exc}")

    live = {group.slug for group in groups} | registered
    registry_only = frozenset(registered - {group.slug for group in groups})
    if not failures:
        return LivenessResolution(live_slugs=frozenset(live), registered_slugs=registry_only)

    if any(slug is None for slug, _msg in failures):
        # An unattributable failure could concern any worktree that exists.
        tainted = _existing_worktree_slugs(worktree_to_slug)
        tainted |= {slug for slug, _msg in failures if slug}
    else:
        tainted = {slug for slug, _msg in failures if slug}
    unresolved = (tainted & set(wanted)) - live
    return LivenessResolution(
        live_slugs=frozenset(live),
        unresolved_slugs=frozenset(unresolved),
        failures=tuple(msg for _slug, msg in failures),
        registered_slugs=registry_only,
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


def _classify_groups(
    groups: Iterable[InheritedAgentGroup],
    *,
    is_group_alive: Callable[[int], bool],
) -> tuple[list[InheritedAgentGroup], list[InheritedAgentGroup], list[InheritedAgentGroup]]:
    """Split groups into live, settled, and unresolved liveness buckets."""
    live: list[InheritedAgentGroup] = []
    settled: list[InheritedAgentGroup] = []
    unresolved: list[InheritedAgentGroup] = []
    for group in groups:
        try:
            alive = is_group_alive(group.pgid)
        except Exception:  # noqa: BLE001 - unresolved is distinct from "settled"
            unresolved.append(group)
            continue
        if alive:
            live.append(group)
        else:
            settled.append(group)
    return live, settled, unresolved


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

    Deliberately asks only about *agent process groups*
    (``include_executions=False``). The scheduler's own ownership record for this
    slug is what put this call here in the first place; counting it as something
    to wait for would make the story wait out its entire quiesce timeout for
    itself, and a story with no surviving agent would reach triage that much later
    for no reason (#2617).
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    wait_log_emitted = False
    while True:
        resolution = resolve_liveness(
            [slug],
            project_root=project_root,
            path_pattern=path_pattern,
            owner_pid=owner_pid,
            is_group_alive=is_group_alive,
            include_executions=False,
        )
        if slug not in resolution.deferred_slugs:
            settled = resolve_inherited_agents(
                [slug],
                project_root=project_root,
                path_pattern=path_pattern,
                owner_pid=owner_pid,
                only_live=False,
                is_group_alive=is_group_alive,
            )
            if wait_log_emitted and log is not None:
                log(f"IN-FLIGHT {slug}: inherited agent finished; resuming the story")
            _discard_records(settled)
            return True
        if stop_event is not None and stop_event.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not wait_log_emitted and log is not None:
            if slug in resolution.live_slugs:
                groups = resolve_inherited_agents(
                    [slug],
                    project_root=project_root,
                    path_pattern=path_pattern,
                    owner_pid=owner_pid,
                    is_group_alive=is_group_alive,
                )
                if groups:
                    pgids = ", ".join(str(g.pgid) for g in groups)
                    log(
                        f"IN-FLIGHT {slug}: waiting up to {int(timeout)}s for the agent process "
                        f"group(s) {pgids} inherited across the re-exec to finish before resuming"
                    )
                else:
                    log(
                        f"IN-FLIGHT {slug}: waiting up to {int(timeout)}s because inherited "
                        "agent liveness is temporarily unobservable; retaining its sidecar "
                        "until it is confirmed finished or the wait times out"
                    )
            else:
                log(
                    f"IN-FLIGHT {slug}: waiting up to {int(timeout)}s because inherited "
                    "agent liveness is temporarily unobservable; retaining its sidecar "
                    "until it is confirmed finished or the wait times out"
                )
            wait_log_emitted = True
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
    groups = resolve_inherited_agents(
        [slug],
        project_root=project_root,
        path_pattern=path_pattern,
        owner_pid=owner_pid,
        only_live=False,
        is_group_alive=is_group_alive,
    )
    live, settled, unresolved = _classify_groups(groups, is_group_alive=is_group_alive)

    killed: list[int] = []
    discarded: list[InheritedAgentGroup] = list(settled)
    for group in [*live, *unresolved]:
        if kill_group(group.pgid):
            killed.append(group.pgid)
            discarded.append(group)

    _discard_records(discarded)
    return killed
