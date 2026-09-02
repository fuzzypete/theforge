"""Process-group isolation, pgid registry sidecars, and an orphan reaper.

Agent CLI subprocesses (``codex``, ``claude``, …) spawn their own child trees
(``npm exec`` → ``node`` → the provider binary). When a sprint process dies —
cleanly, on error, or by an external ``kill -9`` — those trees must die with it.
Two mechanisms cooperate:

1. **Group-isolated spawn** (`run_in_process_group`) — every agent subprocess is
   launched with ``start_new_session=True`` so it leads its own process group.
   On timeout or any teardown the whole group is ``killpg``-ed, so grandchildren
   (node, the provider leaf binary) die too — a bare ``proc.kill()`` reaches only
   the direct child. A *clean* exit is checked too rather than assumed: an agent
   that shelled out to a long-running command and returned first leaves that
   command running, and it is killed at release (`release_group_record`) rather
   than left to outlive the story that authorised it (#2309).
2. **Escapee kill** (`kill_escapees`) — a process group is escapable: a
   descendant that calls ``setsid``/``start_new_session`` leaves the pgid behind
   and becomes invisible to every group-based teardown, which is exactly what a
   test runner or a daemonising helper does. Two independent mechanisms find
   those, because neither covers the other's blind spot (#2309):

   * **Observation while the links exist** (`theforge.process_tree`) — a tracker
     samples the spawn's descendants during the invocation, reading only
     ppid/start-time. Those the kernel exposes for *every* same-uid process, so
     this holds for a SIP-protected platform binary and for a descendant that
     cleared its own environment. What it saw stays known after the parent that
     proved the relationship is gone.
   * **An inherited environment token** (`open_process_lease`) — stamped into
     the child's env, so it needs no prior observation and covers a descendant
     born and orphaned between two samples. It cannot read the environment of a
     SIP-protected binary, so it is a second signal rather than the containment.
3. **Orphan reaper** (`reap_orphan_agents`) — synchronous group-kill cannot run
   when the parent sprint is ``SIGKILL``-ed, so each spawned group records its
   pgid + owner pid in a sidecar under ``.forge/runs/agents/``. A later
   *mutating* ``forge`` invocation (stop / sprint-startup) sweeps those sidecars
   and ``killpg``-s any group whose owner sprint is no longer alive **and whose
   recorded identity still matches the group presently holding that pgid**
   (`reap_orphan_agents`). ``forge status`` only reports them (`list_orphan_agents`) —
   a command whose job is to describe state must not signal processes (#2115).

Identity is a start time, never an id. Pids and pgids are recycled, so a record
naming one describes a past moment; acting on it later takes evidence the moment
still holds. A sidecar therefore carries the leader's start time at registration,
and — when a teardown leaves survivors — a snapshot of the group's members taken
while the group was still known to be ours (`retain_group_record`). A sweep that
can match neither declines to signal and drops the record. A lease token needs no
such care: it is freshly generated per spawn and never reused, so a process
holding one *is* a descendant of that spawn, whenever the question is asked.

Stdlib-only by design (convention 4) so it can be imported by both the runners
and the CLI without pulling in heavier dependencies.
"""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from theforge import process_tree
from theforge.process_tree import DescendantTracker

# Env seams: register/unregister resolve the orchestrator runs dir from
# FORGE_PROJECT_ROOT (set at run bootstrap); run_id is the detached run id.
_PROJECT_ROOT_ENV = "FORGE_PROJECT_ROOT"
_RUN_ID_ENV = "FORGE_DETACHED_RUN_ID"

# How long a teardown waits for a SIGKILL-ed tree to actually be reaped before
# escalating, and again before giving up. Short on purpose, and it can afford to
# be: this is not a grace period for a graceful shutdown — SIGKILL is not
# catchable, so a process that has received one is already dead barring
# uninterruptible sleep. It is only the window to observe the exit. Two passes
# bound the whole teardown at 2x this, which keeps it inside the <5s budget the
# gate's timeout path is asserted against even when every kill is refused.
KILL_GRACE_SECONDS = 2.0


def _grace(seconds: float | None) -> float:
    """Resolve a grace argument against the current ``KILL_GRACE_SECONDS``.

    The callers below take ``None`` rather than defaulting to the constant
    directly, because a default expression is bound once at definition time: a
    test that shortens the constant would still get 2.0 everywhere the default
    applied, and the teardown paths that deliberately let a kill fail would keep
    paying the full window. Resolving here reads the value at call time, so the
    constant is one knob rather than four copies of it.
    """
    return KILL_GRACE_SECONDS if seconds is None else seconds


# Teardown actions recorded on a `ProcessTeardown`.
TEARDOWN_KILLED_SURVIVORS = "killed_survivors"
"""The invocation finished but left live descendants; they were killed here."""

TEARDOWN_RETAINED_FOR_REAPER = "retained_for_reaper"
"""Descendants survived the kill; the sidecar was kept so a later sweep can act."""


@dataclass(frozen=True)
class ProcessTeardown:
    """What it took to end the processes one invocation left running.

    Produced only when something did *not* end on its own — i.e. the invocation
    returned (cleanly or not) while processes it had started were still alive. A
    quiet, self-terminating tree produces no record at all, so the presence of
    one is itself the signal: this invocation left work behind, and something had
    to end it.

    ``members`` are survivors still in the spawned process group; ``escaped_pids``
    are survivors that had left it (``setsid``) and were found by lease token
    instead. The two are recorded separately because they are different failures:
    the first is work that outran its invocation, the second is work that also
    stepped outside the container the invocation put it in.

    Stdlib-only and pure data (convention 4) so both the runners and the audit
    writer can carry it without importing process machinery.
    """

    pgid: int | None
    action: str
    member_count: int
    members: tuple[int, ...]
    sandbox_dir: str | None
    completed: bool
    """True when every survivor was gone by the time teardown returned."""
    escaped_pids: tuple[int, ...] = ()

    def to_audit_dict(self) -> dict[str, Any]:
        """Audit-record projection: the facts an operator needs, JSON-shaped."""
        return {
            "pgid": self.pgid,
            "action": self.action,
            "member_count": self.member_count,
            "members": list(self.members),
            "escaped_pids": list(self.escaped_pids),
            "sandbox_dir": self.sandbox_dir,
            "completed": self.completed,
        }


# ── Process lease ────────────────────────────────────────────────────

LEASE_ENV_VAR = "FORGE_PROCESS_LEASE"
"""Environment variable carrying a spawn's lease token to all its descendants."""


@dataclass(frozen=True)
class ProcessLease:
    """A per-spawn token that every descendant inherits and cannot shed.

    ``started_at`` records when the spawn happened, in wall-clock seconds, for
    the record and for hosts that can date a process no other way.

    ``opened_since_boot`` records the same moment in the kernel's boot-relative
    frame, which is the one Linux reports process start times in. The age prune
    prefers it, because ordering a spawn against a process through wall time on
    Linux means composing the process's start from a cached boot epoch — and if
    that composition drifts, a live descendant reads as older than the spawn and
    is pruned for the life of the lease (#2689). None where the platform has no
    such frame (macOS reports a wall-clock start directly) or where the spawn
    moment is not known at all.
    """

    token: str
    started_at: float
    opened_since_boot: float | None = None


def open_process_lease(env: dict[str, str] | None) -> tuple[dict[str, str], ProcessLease]:
    """Return ``(env with the lease stamped in, lease)`` for a child about to spawn.

    ``None`` means "inherit the parent's environment", so it is materialised into
    a copy here — the stamp has to be *in* the child's env for its descendants to
    inherit it. The token is fresh per spawn and never reused, which is what makes
    a later match proof of descent rather than a guess.
    """
    lease = ProcessLease(
        token=os.urandom(12).hex(),
        started_at=time.time(),
        opened_since_boot=process_tree.boot_relative_now(),
    )
    stamped = dict(os.environ if env is None else env)
    stamped[LEASE_ENV_VAR] = lease.token
    return stamped, lease


def _pid_holds_lease_linux(pid: int, needle: bytes) -> bool:
    try:
        return needle in Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        # Gone, or another user's — either way not something we may act on.
        return False


# A descendant cannot predate the spawn that created it, so a process older than
# the lease is not a candidate however its environment reads. A second of slack
# absorbs clock granularity rather than a real ordering question — which is only
# a true statement of what it absorbs when both times come from the same clock,
# hence *since_boot* below.
_LEASE_AGE_SLACK_SECONDS = 1.0


def _lease_candidates(since: float, since_boot: float | None = None) -> list[int]:
    """Pids that could belong to a spawn opened at *since*.

    *since* is the spawn's wall-clock moment; *since_boot* is the same moment in
    the kernel's boot-relative frame, or None where it is unknown. When both it
    and a process's own boot-relative start are available the comparison is made
    there, in one clock, and the wall clock is not consulted at all.

    Reading a process's environment is the expensive part of the sweep — on macOS
    ``KERN_PROCARGS2`` copies a whole argv+environ blob per process, which across
    a full table costs tens of milliseconds. Doing that for every process on the
    host, on the release of every short shell command, was a real regression
    (#2309, cycle 3): a `git status` paid to search for a descendant it could not
    possibly have had.

    Start times are read instead — one small, fixed-size query per process
    through `theforge.process_tree`, the same reader the tracker and the identity
    checks already use — and the environment is read only for what survives. The
    prune is sound rather than a heuristic: a descendant cannot predate the spawn
    that created it.

    Two kinds of "we do not know" are handled differently, and the difference is
    what keeps the prune both cheap and safe:

    * **Described, but not datable** — keep it. Dropping a process we can see
      would silently narrow the sweep, and a sweep that quietly stops looking is
      the failure this layer exists to prevent. Keeping it costs one read.
    * **Not described at all** — skip it. The kernel refuses to describe another
      user's process, and it refuses that process's environment for the same
      reason, so such a process cannot be a lease match and reading it is a call
      guaranteed to fail. (Measured on the development host: of 161 processes
      whose ``proc_pidinfo`` was refused, exactly none had a readable
      environment, while 80 of 80 readable ones did.) A lease can only ever be
      held by *our own* descendant, which is precisely the set the platform will
      describe to us.

    There is deliberately no "consider everything" fallback. An earlier version
    reached for one whenever a bulk ``sysctl`` read failed, and on a host where
    that read is unavailable the prune silently degraded back into the full scan
    it was added to remove (#2309, cycle 4). Reading start times one process at a
    time through the same interface the tracker already relies on has no such
    cliff: it either describes a process or tells us it will not.

    There is a third way to be wrong, and it is the one that made this prune
    drop live descendants on Linux CI (#2689): comparing two times that came
    from *different* clocks. Linux reports a process's start as ticks since
    boot; turning that into a wall-clock instant needs the boot epoch from
    ``/proc/stat``, which `theforge.process_tree` caches for the life of the
    worker. Bridged to a freshly-read ``time.time()`` by a fixed second of
    slack, any disagreement larger than that second turns a descendant born
    after the spawn into one that reads as older — and, since neither reading
    changes, it stays pruned for as long as the lease lives, so the sweep never
    recovers. Ordering the two in the kernel's own frame removes the bridge.
    """
    cutoff = since - _LEASE_AGE_SLACK_SECONDS
    boot_cutoff = None if since_boot is None else since_boot - _LEASE_AGE_SLACK_SECONDS
    candidates: list[int] = []
    for pid in process_tree.live_pids():
        if pid <= 1:
            continue
        info = process_tree.process_info(pid)
        if info is None:
            continue
        if boot_cutoff is not None and info.started_since_boot is not None:
            started, against = info.started_since_boot, boot_cutoff
        else:
            started, against = info.started_at, cutoff
        if started is None or started >= against:
            candidates.append(pid)
    return candidates


def _lease_holders_linux(needle: bytes, candidates: list[int]) -> list[int]:
    return [pid for pid in candidates if _pid_holds_lease_linux(pid, needle)]


def _lease_holders_darwin(needle: bytes, candidates: list[int]) -> list[int]:
    """macOS: read each candidate's environ via ``KERN_PROCARGS2``."""
    holders: list[int] = []
    for pid in candidates:
        args = _sysctl_bytes((_CTL_KERN, _KERN_PROCARGS2, pid))
        if args and needle in args:
            holders.append(pid)
    return holders


def lease_holders(lease: ProcessLease) -> list[int]:
    """Live pids still carrying *lease*, excluding this process.

    Empty on a platform with no way to read another process's environment. That
    is a dead end rather than a fallback, and it is the safe direction: the group
    kill still runs, and nothing is signalled on a guess.
    """
    candidates = _lease_candidates(lease.started_at, lease.opened_since_boot)
    if not candidates:
        return []
    needle = f"{LEASE_ENV_VAR}={lease.token}".encode()
    if sys.platform.startswith("linux"):
        found = _lease_holders_linux(needle, candidates)
    elif sys.platform == "darwin":
        found = _lease_holders_darwin(needle, candidates)
    else:
        return []
    me = os.getpid()
    return sorted(pid for pid in found if pid > 1 and pid != me)


def kill_escapees(
    *,
    tracker: DescendantTracker | None = None,
    lease: ProcessLease | None = None,
    recorded: dict[int, str] | None = None,
) -> tuple[tuple[int, ...], bool]:
    """Kill descendants that left the process group; ``(killed pids, all gone)``.

    The group kill runs first and normally takes the whole tree with it. What
    reaches here is what left the group — a helper that called ``setsid``, a
    daemonised child that double-forked — and would otherwise outlive the story
    with nothing pointing back to it.

    Three independent ways of knowing, because none is sufficient alone:

    * **What was observed** (`tracker`) — descendants seen while their parent
      links still existed. Reads only ppid/start-time, which the kernel exposes
      for every same-uid process, so it holds for a SIP-protected platform binary
      and for a descendant that cleared its own environment.
    * **What still carries the token** (`lease`) — an inherited environment
      stamp, which needs no prior observation and so covers a descendant born and
      orphaned between two samples. It cannot see a process whose environment is
      unreadable, which is why it is not relied on alone (#2309, cycle 2).
    * **What a previous process observed** (`recorded`) — the same observation
      as the first, read back from a sidecar after the process that made it died.
      This is the only one available to `reap_orphan_agents`, and without it a
      stopped sprint leaves an unreadable escapee running (#2309, cycle 3).

    What counts as identity differs by signal, and saying so precisely matters
    because the cost of killing the wrong process is unbounded (#2115):

    * A **pid** is not identity — it is recycled. So both observation signals
      re-check each pid against the start time it was seen with, and drop it if
      the number now belongs to something else. ``tracker.survivors()`` does
      this for what it watched; the loop below does it for what a previous
      process wrote down.
    * A **lease token** *is* identity. It is minted once per spawn and never
      reused, so a process carrying it is that spawn's descendant whatever its
      pid, and there is nothing older to check it against. The start time read
      for such a pid here is not evidence — it is only what `wait_until_gone`
      needs in order to tell "it exited" from "the id was reused while we
      waited".
    """
    targets: dict[int, str] = {}
    if tracker is not None:
        targets.update(tracker.survivors())
    for pid, fingerprint in (recorded or {}).items():
        # Verified against the start time it was recorded with, exactly as the
        # tracker verifies its own: a recycled pid is a different process.
        info = process_tree.process_info(pid) if is_killable_pgid(pid) else None
        if info is not None and info.fingerprint == fingerprint:
            targets[pid] = fingerprint
    for pid in lease_holders(lease) if lease is not None else []:
        if pid not in targets:
            # The token match is the identity (see above); this read only gives
            # the post-kill wait something to compare against.
            info = process_tree.process_info(pid)
            if info is not None:
                targets[pid] = info.fingerprint
    if not targets:
        return (), True
    pids = tuple(sorted(targets))
    _log(
        f"  ⚠ {len(pids)} process(es) started by this invocation outlived it in their "
        f"own process group(s) (pids={list(pids)}); killing them directly"
    )
    for pid in pids:
        _kill_pid(pid)
    remaining = process_tree.wait_until_gone(targets, timeout=KILL_GRACE_SECONDS)
    if remaining:
        _log(f"  ⚠ pids={sorted(remaining)} survived the kill; they are a real leak, not a delay")
    return pids, not remaining


def _log(msg: str) -> None:
    # Local import keeps this module stdlib-only at import time.
    from theforge.log_util import _log_line  # noqa: PLC0415

    _log_line("[forge]", msg)


# ── Group-isolated spawn ─────────────────────────────────────────────


def run_in_process_group(
    cmd: list[str],
    *,
    timeout: float | None = None,
    input: str | None = None,
    capture_output: bool = False,
    text: bool = False,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    teardown_out: list[ProcessTeardown] | None = None,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` that isolates the child into its own process group.

    Returns a ``subprocess.CompletedProcess`` with the same shape ``subprocess.run``
    would produce. On ``subprocess.TimeoutExpired`` — or any other ``BaseException``
    raised while the child runs — the *entire* process group is ``SIGKILL``-ed
    before the exception is re-raised, so npm→node→leaf grandchildren cannot
    outlive the call. ``subprocess.run``'s own timeout kill signals only the
    direct child (``npm exec``), leaving node + the provider leaf alive.

    A descendant tracker runs for the life of the call and the child's
    environment is stamped with a lease token, so that teardown can still reach a
    descendant that left the process group by calling ``setsid`` — the one thing
    group isolation alone cannot contain (see `kill_escapees`).

    ``teardown_out`` is an out-parameter rather than part of the return value:
    a forced teardown is a fact about the *call*, and it has to reach the caller
    on the exception paths too, where there is no return value to carry it. Any
    `ProcessTeardown` produced at release is appended to it.
    """
    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None
    stdin = subprocess.PIPE if input is not None else None

    leased_env, lease = open_process_lease(env)
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        env=leased_env,
        cwd=cwd,
        start_new_session=True,
    )
    # Register the group so the reaper can kill it if the sprint is SIGKILL-ed
    # while communicate() blocks in its heartbeat thread (no synchronous cleanup
    # runs in that case). sandbox_dir defaults to cwd — the workspace grant.
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None:
        register_agent_group(pgid, sandbox_dir=cwd, lease=lease)
    tracker = descendant_tracker(root_pid=proc.pid, pgid=pgid)
    tracker.start()
    # Normal completion implies the group went with the child; only a teardown
    # that could not reach the group flips this.
    group_killed = True
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Kill the group first (bounded — see below), then salvage whatever the
        # child had already written so the caller can still account for work it
        # paid for. subprocess.run does this; not doing it discarded a timed-out
        # agent's partial output, including any token usage it had reported.
        group_killed = terminate_process_group(proc)
        _attach_partial_output(proc, exc)
        raise
    except BaseException:
        # Every other unwind: kill the group, then wait for it *bounded*. An
        # unbounded wait here blocks for the child's full natural lifetime
        # whenever the group kill did not land (#1959), turning an enforced
        # timeout into no timeout at all.
        group_killed = terminate_process_group(proc)
        raise
    finally:
        teardown = release_group_record(
            pgid,
            group_killed=group_killed,
            sandbox_dir=cwd,
            lease=lease,
            tracker=tracker,
        )
        if teardown is not None and teardown_out is not None:
            teardown_out.append(teardown)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _attach_partial_output(
    proc: subprocess.Popen[Any],
    exc: subprocess.TimeoutExpired,
    *,
    drain_seconds: float | None = None,
) -> None:
    """Populate ``exc.stdout``/``exc.stderr`` with the killed child's partial output.

    ``Popen.communicate`` does attach partial reads to the ``TimeoutExpired`` it
    raises, but joins them as bytes even in text mode, so the value is unusable
    for a ``text=True`` caller. Re-draining after the group kill gives properly
    decoded text, the same way ``subprocess.run`` does it.

    Bounded on purpose: if the group kill did not land, a survivor still holding
    the write end would block this drain for the child's full natural lifetime —
    exactly the failure #1959 exists to prevent. A drain that times out leaves the
    exception as-is; partial accounting is a best-effort bonus, never a new way to
    hang teardown.
    """
    try:
        out, err = proc.communicate(timeout=_grace(drain_seconds))
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return
    if out is not None:
        exc.stdout = out
    if err is not None:
        exc.stderr = err


def _group_is_empty(pgid: int) -> bool:
    """True when nothing in *pgid* is still running.

    A completed enumeration outranks the signal-0 probe here. ``killpg(pgid, 0)``
    succeeds while the group still holds a process that has exited and not been
    reaped, so a kill that worked would otherwise be waited out to its full grace
    and then recorded as survivors that outlived it — which is what forge running
    as pid 1 sees for every child it kills, since it inherits its own orphans and
    never reaps them. Where enumeration is unavailable the probe is all there is.
    """
    members, enumerated = group_members_checked(pgid)
    return not members if enumerated else not group_is_alive(pgid)


def _await_empty_group(pgid: int, *, grace_seconds: float | None = None) -> bool:
    """Poll until *pgid* holds no running process, bounded by *grace_seconds*."""
    deadline = time.monotonic() + _grace(grace_seconds)
    while True:
        if _group_is_empty(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _release_group(
    pgid: int, *, group_killed: bool, sandbox: str | None
) -> tuple[str | None, dict[int, str], bool]:
    """Settle the process group; returns ``(action or None, members, completed)``.

    Three outcomes, and they are not the same fact:

    * **The group is already empty** — nothing to do but drop the sidecar. No
      action, and the invocation reports no teardown at all.
    * **Teardown reached only the direct child** (``group_killed=False``) — the
      kill was refused, so re-issuing it buys nothing. Unregistering would erase
      the one record that lets `reap_orphan_agents` ever reach the survivors, so
      the sidecar is kept instead.
    * **The invocation ended on its own terms while descendants kept running**
      (``group_killed=True``, group still alive) — a dev agent that shelled out
      to ``pytest -n auto`` and returned before those workers did. Nothing killed
      that tree, because nothing had decided it needed killing: the leader exited
      cleanly, so teardown treated the group as gone. It was not, and the workers
      then outlived the sprint, the worktree and the budget that authorised them,
      competing for the host with every run measured afterwards (#2309). Work a
      story starts ends with the story, so the survivors are killed here.

    Killing on membership alone is safe despite pgid recycling: a pgid is only
    reusable once its group is empty, and a group with live members has not
    emptied. The members enumerated here are therefore this group's, not a
    successor's — the check `_identity_verdict` makes for a record read minutes
    later is not needed for one read microseconds after our own wait returned.
    """
    if not group_is_alive(pgid):
        unregister_agent_group(pgid)
        return None, {}, True

    members, enumerated = group_members_checked(pgid)
    if group_killed and not members and enumerated:
        # The signal-0 probe errs toward "alive"; a *completed* enumeration is the
        # sharper instrument and says there is nothing left to kill. A read that
        # failed says nothing at all, and must not be mistaken for that — an
        # empty answer from a busy sysctl would otherwise drop a live group's
        # record without ever killing it.
        unregister_agent_group(pgid)
        return None, {}, True

    if not group_killed:
        _log(
            f"  ⚠ keeping agent sidecar for pgid={pgid}: teardown reached only the "
            "direct child and the group is still alive"
        )
        # Capture the survivors now, while we still know this group is ours — it
        # is the only identity a later sweep can check once the leader is gone.
        retain_group_record(pgid)
        return TEARDOWN_RETAINED_FOR_REAPER, members, False

    _log(
        f"  ⚠ agent invocation finished but left {len(members) or 'live'} process(es) "
        f"running in pgid={pgid} (sandbox={sandbox}); killing them now so they cannot "
        "outlive the story that started them"
    )
    if kill_agent_group(pgid) and _await_empty_group(pgid):
        unregister_agent_group(pgid)
        return TEARDOWN_KILLED_SURVIVORS, members, True

    _log(
        f"  ⚠ pgid={pgid} still holds processes after the kill; keeping its sidecar "
        "so a later sweep can finish the job"
    )
    retain_group_record(pgid)
    return TEARDOWN_RETAINED_FOR_REAPER, members, False


def release_group_record(
    pgid: int | None,
    *,
    group_killed: bool,
    sandbox_dir: str | Path | None = None,
    lease: ProcessLease | None = None,
    tracker: DescendantTracker | None = None,
) -> ProcessTeardown | None:
    """End everything the invocation started, then drop the reaper's sidecar.

    Two containers, checked in order. The **process group** catches descendants
    that stayed in it (see `_release_group`). The **escapee kill** catches the
    ones that did not: anything that calls ``setsid``, as a test runner or a
    daemonising helper does, leaves the pgid and becomes invisible to every
    group-based teardown. Those are found by what the tracker observed while
    their parent links still existed, and by the inherited lease token — see
    `kill_escapees` for why it takes both. ``pgid`` may be ``None`` when the
    spawn's group could not be resolved; the escapee kill still runs, because
    that is the case with the least other cover.

    Returns a `ProcessTeardown` describing whatever had to be killed, and ``None``
    when nothing did — so a caller can record that a kill was needed without
    having to infer it from a log line.
    """
    sandbox = str(sandbox_dir) if sandbox_dir is not None else None
    action: str | None = None
    members: dict[int, str] = {}
    completed = True
    if pgid is not None:
        action, members, completed = _release_group(
            pgid, group_killed=group_killed, sandbox=sandbox
        )

    if tracker is not None:
        # Final sample first: a descendant spawned since the last one is still
        # observable right now, and will not be once we start killing.
        tracker.stop()
    escaped, escaped_gone = kill_escapees(tracker=tracker, lease=lease)
    if action == TEARDOWN_RETAINED_FOR_REAPER and pgid is not None and not group_is_alive(pgid):
        # The lease sweep reached by pid what the refused ``killpg`` could not,
        # and the group is empty after all. Retaining a record now would hand a
        # later sweep an empty group to chase, and reporting the teardown as
        # incomplete would be simply untrue.
        unregister_agent_group(pgid)
        action = TEARDOWN_KILLED_SURVIVORS
        completed = True
    if not escaped_gone:
        completed = False
    if action is None:
        if not escaped:
            return None
        # The group was clean and the survivors had left it entirely — the escape
        # the lease exists for. Nothing is retained: a pgid-keyed sidecar cannot
        # describe a process that is no longer in that group.
        action = TEARDOWN_KILLED_SURVIVORS

    return ProcessTeardown(
        pgid=pgid,
        action=action,
        member_count=len(members),
        members=tuple(sorted(members)),
        escaped_pids=escaped,
        sandbox_dir=sandbox,
        completed=completed,
    )


def _killpg_for(pid: int) -> bool:
    """Best-effort ``SIGKILL`` of the process group led by *pid*."""
    try:
        return kill_agent_group(os.getpgid(pid))
    except OSError:
        return False


def _kill_pid(pid: int) -> bool:
    """Best-effort ``SIGKILL`` of a single process; twin of `_killpg_for`.

    Returns True when the process is gone or the signal was delivered. The same
    guard applies as for a pgid, and for the same reason (#1793): a real spawned
    child always has pid > 1, while ``os.kill(0, …)`` signals the caller's own
    process group and ``os.kill(1, …)`` targets init.
    """
    if not is_killable_pgid(pid):
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        # Already gone — the state we wanted, not a failure.
        return True
    except OSError as exc:
        _log(f"  ⚠ direct kill of pid={pid} refused: {exc}")
        return False
    return True


def _wait_bounded(proc: subprocess.Popen[Any], timeout: float) -> bool:
    """True if *proc* exited within *timeout* seconds."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def group_is_alive(pgid: int) -> bool:
    """True while any process remains in *pgid*, using signal 0 as a probe.

    Errs toward "alive" when the answer cannot be obtained (e.g. a sandbox that
    refuses even a zero signal). The two mistakes are not symmetric: believing a
    dead group is alive costs one stale sidecar and a no-op reap, while believing
    a live group is dead drops the only record that can ever kill it.
    """
    if not is_killable_pgid(pgid):
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def terminate_process_group(
    proc: subprocess.Popen[Any], *, grace_seconds: float | None = None
) -> bool:
    """Kill the group led by *proc* and wait — bounded — for the tree to die.

    Returns True when the kill reached the whole *group*, False when it reached
    at most the direct child. That distinction is the caller's business, not a
    detail: a false return means grandchildren may still be running, so the
    group's reaper sidecar must be kept rather than dropped.

    Group kill is best-effort and can fail to land: the macOS seatbelt profile
    denies ``killpg`` unless it grants signalling descendants, and a group can
    also be partly gone already. Every caller that reaches here has already
    decided the tree must die, so the only question is how long we are willing to
    wait to observe it. Waiting without a bound is the failure mode this exists
    to prevent — it converts a denied kill into a block for the child's entire
    natural lifetime, which is how a nested gate spent ten minutes at idle CPU
    (#1959). So: kill the group, wait briefly, escalate to the direct child, wait
    briefly again, and log loudly if even that does not settle it — a survivor is
    a real leak and the log line is the only trace of it, so it must not be
    swallowed the way the refused kill itself was.
    """
    # Resolve before any wait: `_wait_bounded` passes this straight to
    # `Popen.wait`, where None means "block forever" — the exact unbounded wait
    # the docstring above says this function exists to prevent.
    grace = _grace(grace_seconds)
    # Only ever wait on a signal that was actually delivered. A refused kill
    # tells us up front that nothing will change, so waiting out the grace period
    # for it buys nothing but the latency this function exists to avoid.
    if _killpg_for(proc.pid):
        # SIGKILL reached the group and is uncatchable, so every member is dead
        # or dying. The bounded wait only observes our own child being reaped.
        if not _wait_bounded(proc, grace):
            _log(f"  ⚠ pid={proc.pid} not reaped after its group was killed")
        return True
    # The group kill did not land. Signalling the direct pid is a strictly weaker
    # guarantee — it cannot reach grandchildren — but it is the one thing a
    # sandbox that denies cross-group signalling still permits. Report it as what
    # it is so the caller keeps tracking whatever survived.
    if _kill_pid(proc.pid):
        _wait_bounded(proc, grace)
    else:
        _log(f"  ⚠ pid={proc.pid} survived teardown; abandoning it rather than blocking on it")
    return False


# ── pgid registry sidecars ───────────────────────────────────────────


def _agents_dir_from_env() -> Path | None:
    """Resolve ``.forge/runs/agents`` from ``FORGE_PROJECT_ROOT``, or None."""
    raw = os.environ.get(_PROJECT_ROOT_ENV)
    if not raw:
        return None
    return Path(raw) / ".forge" / "runs" / "agents"


def _sidecar_path(agents_dir: Path, owner_pid: int, pgid: int) -> Path:
    return agents_dir / f"{owner_pid}-{pgid}.json"


def _running_under_pytest() -> bool:
    """True when this process is a test run rather than a dispatched agent.

    Sidecars written by the suite are otherwise indistinguishable from ones
    describing real work, and they land in whatever ``FORGE_PROJECT_ROOT`` the
    test process inherited — in the dogfood setup, the *real* project (#2115).
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def _parse_proc_stat(raw: str) -> tuple[int, str, str] | None:
    """Linux ``/proc/<pid>/stat`` → (process-group id, start-time fingerprint).

    Returns the process state alongside, rather than deciding what to do about
    it, because its two callers need opposite things and both are right:

    * `group_members` asks "is anything still *running* here?" — a process that
      has exited but not been reaped is not, so it drops one whose state is
      ``Z``.
    * `_leader_fingerprint` asks "does this pgid still belong to the group I
      recorded?" — a corpse continues to occupy its pid, so its start time is
      still proof the id has not been recycled. That proof is load-bearing: it
      is what lets a sweep verify and then kill a group whose direct child died
      unreaped while its descendants ran on. Excluding zombies here outright
      took it away, and because a corpse answers a signal-0 probe the sweep then
      declined the record and left those descendants running — measured on
      Linux-as-pid-1 (#2309).
    """
    # comm (field 2) is parenthesised and may itself contain spaces, so split
    # after the final ')' rather than tokenising the whole line.
    _, _, rest = raw.rpartition(")")
    fields = rest.split()
    # rest starts at field 3 (state), so pgrp (field 5) is index 2 and starttime
    # (field 22) is index 19.
    if len(fields) < 20:
        return None
    try:
        pgrp = int(fields[2])
    except ValueError:
        return None
    return pgrp, f"proc:{fields[19]}", fields[0]


def _read_proc_stat(pid: int) -> tuple[int, str, str] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _parse_proc_stat(raw)


# ``sysctl`` MIB pieces and the two offsets this module reads out of the
# ``struct extern_proc`` that opens every ``kinfo_proc``: its leading union's
# other arm is ``p_starttime``, so the ``timeval`` sits at offset 0, and
# ``p_pid`` follows p_vmspace/p_sigacts/p_flag/p_stat at offset 40 on the 64-bit
# Darwin ABI. Microsecond resolution makes this a far sharper identity than
# ``ps``'s second-granularity ``lstart``.
_CTL_KERN = 1
_KERN_PROC = 14
_KERN_PROC_PID = 1
_KERN_PROC_PGRP = 2
# ``KERN_PROCARGS2`` returns a process's argv + environ blob, which is how the
# lease sweep reads another process's environment on a platform with no /proc.
_KERN_PROCARGS2 = 49
_KINFO_PID_OFFSET = 40
# ``p_stat`` follows p_flag in ``struct extern_proc``; ``SZOMB`` marks a process
# that has exited and not yet been reaped. Verified against a live member (SRUN)
# and the same member after a kill (SZOMB) on the development host.
_KINFO_P_STAT_OFFSET = 36
_SZOMB = 5


def _kinfo_is_zombie(raw: bytes, offset: int) -> bool:
    """True when this ``kinfo_proc`` record describes an unreaped corpse."""
    try:
        return struct.unpack_from("b", raw, offset + _KINFO_P_STAT_OFFSET)[0] == _SZOMB
    except struct.error:
        return False


def _sysctl_bytes(mib_values: tuple[int, ...]) -> bytes | None:
    """Raw ``sysctl`` result for *mib_values*, or None if it cannot be read."""
    import ctypes  # noqa: PLC0415

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * len(mib_values))(*mib_values)
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, len(mib_values), None, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value == 0:
            return b""
        # Slack, because the process table can grow between the sizing call and
        # the read; without it a busy moment returns ENOMEM instead of an answer.
        buf = ctypes.create_string_buffer(size.value + 8192)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctl(mib, len(mib_values), buf, ctypes.byref(size), None, 0) != 0:
            return None
        return buf.raw[: size.value]
    except (OSError, ValueError, AttributeError):
        return None


def _kinfo_start_time(raw: bytes, offset: int) -> str | None:
    try:
        seconds, micros = struct.unpack_from("qi", raw, offset)
    except struct.error:
        return None
    return f"sysctl:{seconds}.{micros:06d}"


def _kinfo_pid(raw: bytes, offset: int) -> int | None:
    try:
        return int(struct.unpack_from("i", raw, offset + _KINFO_PID_OFFSET)[0])
    except struct.error:
        return None


def _sysctl_record_size() -> int:
    """Size of one ``kinfo_proc``, taken from a query that returns exactly one."""
    raw = _sysctl_bytes((_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, os.getpid()))
    return 0 if raw is None else len(raw)


def _start_time_from_sysctl(pid: int) -> str | None:
    """macOS/BSD: ``kinfo_proc.kp_proc.p_starttime`` for a single pid."""
    raw = _sysctl_bytes((_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid))
    if not raw:
        return None
    return _kinfo_start_time(raw, 0)


def _leader_fingerprint(pgid: int) -> str | None:
    """Start-time fingerprint of the process leading *pgid*, or None.

    The pgid of a group spawned with ``start_new_session=True`` *is* its leader's
    pid, so the leader's start time is a property a recycled pgid cannot
    reproduce — the evidence the reaper needs before signalling anything (#2115).

    Deliberately still answers for a leader that has exited but not been reaped,
    unlike `group_members`, which reports such a process as gone. The two are
    asking different questions and the answers are both right: membership asks
    "is anything still running here?", while this asks "does the pgid still
    belong to the group I recorded?" — and a corpse continues to occupy its pid,
    so its start time remains proof that the id has *not* been recycled. That
    proof is load-bearing: it is what lets a sweep verify and then kill a group
    whose direct child died unreaped while its descendants ran on, which is the
    case the reaper exists for (#2309).

    Read from the kernel directly — ``/proc`` on Linux, ``sysctl`` on macOS — so
    that taking a fingerprint costs no process spawn: this sits in the path of
    every agent and gate launch, and ``ps`` is an exec that a sandbox profile can
    deny outright. ``ps`` remains only as the fallback for a platform with
    neither interface. Each value carries its source, so a record written from
    one source is never compared against another (a mismatch there would read as
    "recycled" and, correctly, kill nothing).
    """
    from theforge.detach import _is_pid_alive  # noqa: PLC0415
    from theforge.pid import _pid_start_time  # noqa: PLC0415

    if not is_killable_pgid(pgid) or not _is_pid_alive(pgid):
        return None
    if sys.platform.startswith("linux"):
        parsed = _read_proc_stat(pgid)
        return None if parsed is None else parsed[1]
    if sys.platform == "darwin":
        return _start_time_from_sysctl(pgid)
    started = _pid_start_time(pgid)
    return None if started is None else f"ps:{started}"


def group_members(pgid: int) -> dict[int, str]:
    """``{pid: start-time fingerprint}`` for every live process in *pgid*.

    Membership is the only identity a process group has once its leader is gone:
    the leader's pid — and with it the pgid — is reusable the moment the group
    empties, so "some group with this id exists" proves nothing about *which*
    group it is (#2115). A recorded member that is still alive with the same
    start time does prove it.

    Empty when the platform offers no way to enumerate a group without spawning
    ``ps``. Callers that need to tell "the group is empty" from "the answer could
    not be obtained" must use `group_members_checked` — the two are the same
    value here and only one of them is safe to act on.
    """
    members, enumerated = group_members_checked(pgid)
    if members or enumerated:
        return members
    # A single unreadable pass is not evidence the group is empty; on Darwin the
    # kernel can momentarily refuse ``KERN_PROC_PGRP`` under table churn. The
    # checked API still reports that uncertainty to safety-critical callers, but
    # this convenience helper retries a few times, with a short settling delay,
    # so transient read failures do not masquerade as missing members in
    # observational paths and tests. When the platform has no enumeration
    # interface at all there is nothing to settle, so skip the delay entirely.
    for _ in range(2):
        if sys.platform.startswith("linux") or sys.platform == "darwin":
            time.sleep(0.01)
        members, enumerated = group_members_checked(pgid)
        if members or enumerated:
            return members
    return members


def group_members_checked(pgid: int) -> tuple[dict[int, str], bool]:
    """``(members still running, enumeration completed)`` for *pgid*.

    An empty result means two very different things, and the flag is what keeps
    them apart. When the enumeration completed, the group is genuinely empty.
    When the read failed — a ``sysctl`` that returned nothing under load, a
    ``/proc`` that could not be listed, a platform with no interface at all —
    nothing was learned, and a caller must not mistake that for a settled group:
    doing so drops a live group's reaper sidecar without ever signalling it.

    Membership and liveness come out of the *same* read, deliberately. An earlier
    version established membership from the platform enumeration and then refined
    it with a second, independently fallible per-process reader while keeping the
    first read's confidence flag — so when that second reader failed for every
    member, a live group reported itself empty *and* fully enumerated, which is
    exactly the mistake the flag exists to prevent (#2309). One read yields both
    facts, so the flag always describes the data it is attached to.
    """
    if not is_killable_pgid(pgid):
        return {}, True
    if sys.platform.startswith("linux"):
        return _enumerate_group_linux(pgid)
    if sys.platform == "darwin":
        return _enumerate_group_darwin(pgid)
    return {}, False


def group_has_running_members(pgid: int) -> bool:
    """True when *pgid* still has non-zombie members; false when it is settled.

    Unlike :func:`group_is_alive`, this asks the kernel for current membership so
    an unreaped corpse does not count as live work. When the group cannot be
    enumerated but still answers a signal-0 probe, the result is unknown rather
    than "alive", so callers that need a definite answer can surface that
    uncertainty instead of waiting on a process they can no longer observe.
    """
    for _attempt in range(3):
        members, enumerated = group_members_checked(pgid)
        if enumerated:
            return bool(members)
    if not group_is_alive(pgid):
        return False
    raise OSError(f"process group {pgid} could not be enumerated")


def _enumerate_group_linux(pgid: int) -> tuple[dict[int, str], bool]:
    """Linux: one ``/proc/<pid>/stat`` read per process gives group and state."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return {}, False
    members: dict[int, str] = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        parsed = _read_proc_stat(int(entry))
        # A process that vanishes mid-scan is not an incomplete read: it is
        # simply no longer a member, which is the answer we wanted anyway.
        if parsed is not None and parsed[0] == pgid and parsed[2] != "Z":
            members[int(entry)] = parsed[1]
    return members, True


def _enumerate_group_darwin(pgid: int) -> tuple[dict[int, str], bool]:
    """macOS: one ``KERN_PROC_PGRP`` read gives every member and its state.

    ``p_stat`` sits inside the same ``kinfo_proc`` record as the pid and start
    time, so "who is in the group" and "which of them are still running" are
    answered by a single call that either succeeds or says it did not.
    """
    record_size = _sysctl_record_size()
    if record_size <= _KINFO_P_STAT_OFFSET:
        return {}, False
    raw = _sysctl_bytes((_CTL_KERN, _KERN_PROC, _KERN_PROC_PGRP, pgid))
    if raw is None:
        return {}, False
    members: dict[int, str] = {}
    for offset in range(0, len(raw) - record_size + 1, record_size):
        pid = _kinfo_pid(raw, offset)
        fingerprint = _kinfo_start_time(raw, offset)
        if pid is None or fingerprint is None:
            # A record the layout cannot explain means this answer is not the
            # whole truth. Reporting it as complete would let a partially
            # unreadable group read as an empty one.
            return members, False
        if _kinfo_is_zombie(raw, offset):
            continue
        members[pid] = fingerprint
    return members, True


def register_agent_group(
    pgid: int,
    *,
    sandbox_dir: str | Path | None = None,
    lease: ProcessLease | None = None,
) -> None:
    """Record a spawned agent group's pgid so a later reaper can kill orphans.

    Writes ``.forge/runs/agents/{owner_pid}-{pgid}.json`` under the orchestrator
    runs dir, where ``owner_pid`` is the sprint process (``os.getpid()``). Per-pgid
    files avoid write contention from parallel review pools. No-op when the runs
    dir is unresolvable (e.g. ``FORGE_PROJECT_ROOT`` unset in tests).

    Three fields exist purely so a later sweep can decide what it may act on:
    ``leader_fingerprint`` — the group leader's start time, the evidence that the
    group holding this pgid at reap time is the group registered here; ``origin``,
    which marks records left by a test run; and ``lease``, the spawn's token,
    which reaches descendants that left the group and which no recycled id can
    counterfeit.
    """
    agents_dir = _agents_dir_from_env()
    if agents_dir is None:
        return
    owner_pid = os.getpid()
    payload = {
        "owner_pid": owner_pid,
        "pgid": pgid,
        "run_id": os.environ.get(_RUN_ID_ENV),
        "sandbox_dir": str(sandbox_dir) if sandbox_dir is not None else None,
        "leader_fingerprint": _leader_fingerprint(pgid),
        "origin": "test" if _running_under_pytest() else "agent",
        "lease": lease.token if lease is not None else None,
    }
    try:
        agents_dir.mkdir(parents=True, exist_ok=True)
        _write_sidecar_json(_sidecar_path(agents_dir, owner_pid, pgid), payload)
    except OSError:
        pass


def _write_sidecar_json(path: Path, payload: dict[str, object]) -> None:
    """Atomically replace a sidecar JSON record in-place.

    Sidecar readers treat a malformed ``*.json`` as unresolved liveness. Writing
    via a same-directory temp file keeps that failure mode reserved for truly
    broken records rather than our own partial writes.
    """
    encoded = json.dumps(payload)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
        Path(tmp_name).replace(path)
    except Exception:
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def _update_sidecar(pgid: int, field: str, value: dict[str, str]) -> None:
    """Merge ``{field: value}`` into this process's sidecar for *pgid*.

    Read-modify-write of a file only this process writes (the path is keyed on
    our own pid), so no locking is needed. Silent on every failure: a record that
    cannot be updated is a weaker later sweep, never a reason to disturb the run
    that is producing the observation.
    """
    agents_dir = _agents_dir_from_env()
    if agents_dir is None:
        return
    path = _sidecar_path(agents_dir, os.getpid(), pgid)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get(field) == value:
        return
    data[field] = value
    try:
        _write_sidecar_json(path, data)
    except OSError:
        pass


def record_observed_descendants(pgid: int, observed: dict[int, str]) -> None:
    """Persist descendants observed during the run into the group's sidecar.

    The live teardown path can kill an escaped descendant because the tracker
    watched it being born (`theforge.process_tree`). A sprint that is killed
    outright runs no teardown at all, and the observation dies with the process
    that made it — leaving a later sweep with only the pgid, which cannot name a
    process that left the group, and the lease token, which cannot be read out of
    a SIP-protected binary or one that cleared its environment. Writing the
    observation down as it is made is what gives the reaper the same
    non-environment signal the live path has (#2309).

    Each entry keeps the start time it was seen with, so the sweep can tell the
    process it recorded from whatever later holds that pid (#2115).
    """
    if not is_killable_pgid(pgid) or not observed:
        return
    # JSON object keys are strings; the reaper converts back on read.
    _update_sidecar(
        pgid, "observed", {str(pid): fingerprint for pid, fingerprint in observed.items()}
    )


def descendant_tracker(*, root_pid: int, pgid: int | None) -> DescendantTracker:
    """A tracker whose observations are mirrored into the group's sidecar.

    Every spawn site takes its tracker from here so none of them can register a
    group and then watch it without leaving the durable record a stopped sprint
    depends on.
    """
    mirror = None
    if pgid is not None:

        def mirror(observed: dict[int, str], _pgid: int = pgid) -> None:  # noqa: F811
            record_observed_descendants(_pgid, observed)

    return DescendantTracker(root_pid=root_pid, pgid=pgid, on_observed=mirror)


def retain_group_record(pgid: int) -> None:
    """Snapshot the group's live membership into its sidecar, for a kept record.

    Called at the one moment a record is deliberately kept: teardown reached at
    most the direct child, so the leader is dying while its descendants run on.
    From then on the leader's start time is unverifiable — it will be gone — and
    the survivors are the only identity the group has left. Recording them is
    what lets a later sweep tell "the registered group, minus its leader" from
    "an unrelated group that happens to hold a recycled pgid and whose own
    leader has also exited" (#2115). Without it the sweep can only decline.
    """
    members = group_members(pgid)
    if not members:
        return
    _update_sidecar(
        pgid, "members", {str(pid): fingerprint for pid, fingerprint in members.items()}
    )


def unregister_agent_group(pgid: int) -> None:
    """Remove the sidecar for a group that terminated normally."""
    agents_dir = _agents_dir_from_env()
    if agents_dir is None:
        return
    try:
        _sidecar_path(agents_dir, os.getpid(), pgid).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def is_killable_pgid(pgid: object) -> bool:
    """True only for a pgid that can denote a real, spawned process group.

    Shared safety rule for every ``killpg`` call site (the agent runners and the
    coordinator's shell-timeout kill). A group launched via
    ``start_new_session=True`` is led by its child, so its pgid equals that
    child's pid and is always ``> 1``. Values ``<= 1`` are never such a group and
    are catastrophic as ``killpg`` targets: ``os.killpg(0)`` signals the
    *caller's own* group, and ``os.killpg(1)`` is ``kill(-1, …)`` — a broadcast to
    every process the user may signal. A non-int (e.g. a test double's unset
    ``pid`` coerced through ``__index__`` to ``1``) is likewise not a real group.
    Refusing these keeps a bogus pgid from taking down the process tree (#1793).
    """
    return isinstance(pgid, int) and pgid > 1


def kill_agent_group(pgid: int) -> bool:
    """Best-effort ``SIGKILL`` of an entire agent process group.

    Returns True when the group is gone or the signal was delivered, False when
    there was no group to kill or the kill was refused.

    A pgid ``<= 1`` (or a non-int) is treated as "no group to kill" rather than
    passed to ``killpg``: ``os.killpg(1, …)`` is ``kill(-1, …)``, a broadcast
    SIGKILL that would take down the whole process tree — including sibling test
    workers and the CI runner (issue #1793).
    """
    if not is_killable_pgid(pgid):
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        # Already gone — that is the state we wanted, not a failure.
        return True
    except OSError as exc:
        # Loud on purpose. A refused group kill produces no error anywhere else;
        # it surfaces only as a teardown that takes the child's full natural
        # lifetime, which is indistinguishable from "the work was slow" in a log
        # (#1959). Under macOS seatbelt this is EPERM when the profile does not
        # grant signalling descendants.
        _log(f"  ⚠ group kill of pgid={pgid} refused: {exc}")
        return False
    return True


# ── Orphan reaper ────────────────────────────────────────────────────


def _pid_fingerprint_map(raw: object) -> dict[int, str]:
    """A sidecar ``{pid: start time}`` map, with unusable entries dropped.

    JSON object keys are strings, so every read converts back — and an entry
    that cannot be converted is discarded rather than guessed at, because the
    value's only use is deciding whether to signal a process.
    """
    if not isinstance(raw, dict):
        return {}
    parsed: dict[int, str] = {}
    for pid, fingerprint in raw.items():
        try:
            key = int(pid)
        except (TypeError, ValueError):
            continue
        if isinstance(fingerprint, str) and fingerprint:
            parsed[key] = fingerprint
    return parsed


def _recorded_members(data: dict[str, Any]) -> dict[int, str]:
    """The ``members`` snapshot from a sidecar, as ``{pid: fingerprint}``."""
    return _pid_fingerprint_map(data.get("members"))


def _identity_verdict(pgid: object, data: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether *pgid* still denotes the registered group; (may_signal, why).

    Process-group ids are recycled by the OS, so a persisted pgid is a claim
    about a past moment, not a durable handle. Sending a group ``SIGKILL`` on
    that claim alone is how a routine sweep came to signal unrelated processes
    (#2115): the cost of not killing a stale group is one lingering process, and
    the cost of killing the wrong one is unbounded and lands outside this tool.
    So a signal requires a live process whose *start time* matches something this
    record captured while the group was known to be ours — one of:

    * **The leader** — its pid is the pgid, so a recycled id cannot reproduce the
      start time recorded at registration.
    * **A recorded member** — captured by `retain_group_record` when a teardown
      left survivors behind. This is the case the reaper exists for (npm→node→leaf
      grandchildren outliving a refused teardown, #2013), and it needs a real
      check of its own: "some group with this id is non-empty" is *not* evidence,
      because an unrelated group can hold a recycled id and have lost its own
      leader too.

    Everything else — no fingerprint, no recorded members, an unreadable start
    time, a vanished group — is unverifiable, and unverifiable means no signal.
    The residual cost is that a group orphaned with no snapshot (the sprint was
    ``SIGKILL``-ed before any teardown ran, and the leader has since exited)
    leaks rather than being killed on a guess.
    """
    from theforge.detach import _is_pid_alive  # noqa: PLC0415

    if not isinstance(pgid, int) or not is_killable_pgid(pgid):
        return False, "pgid cannot denote a real process group"
    fingerprint = data.get("leader_fingerprint")
    if _is_pid_alive(pgid):
        if not isinstance(fingerprint, str) or not fingerprint:
            return False, "record carries no leader fingerprint to check the holder against"
        current = _leader_fingerprint(pgid)
        if current is None:
            return False, "the current holder's start time could not be read"
        if current != fingerprint:
            return False, (
                f"pgid is now held by a different process (started {current!r}, "
                f"record was written for one started {fingerprint!r})"
            )
        return True, "the leader's start time still matches the record"

    recorded = _recorded_members(data)
    if not recorded:
        return False, (
            "the leader is gone and the record names no surviving member to identify the group by"
        )
    live = group_members(pgid)
    if not live:
        return False, "no live process group holds this pgid"
    matched = [pid for pid, fingerprint in recorded.items() if live.get(pid) == fingerprint]
    if not matched:
        return False, (
            "the group holding this pgid contains none of the recorded members — "
            "the id has been reused"
        )
    return True, f"the leader is gone but recorded member(s) {matched} are still in the group"


def _load_sidecar(sidecar: Path) -> dict[str, Any] | None:
    """Parsed sidecar with a usable owner_pid/pgid pair, or None if unusable."""
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("owner_pid"), int) or not isinstance(data.get("pgid"), int):
        return None
    return data


def list_orphan_agents(project_root: Path) -> list[dict[str, Any]]:
    """Sidecars whose owner sprint is dead, without touching anything.

    Read-only by contract: no signals, no unlinks. This is what an inspection
    command such as ``forge status`` may call; killing is reserved for the
    commands that already mutate run state (#2115).
    """
    from theforge.detach import _is_pid_alive  # noqa: PLC0415

    agents_dir = project_root / ".forge" / "runs" / "agents"
    if not agents_dir.exists():
        return []
    orphans: list[dict[str, Any]] = []
    for sidecar in sorted(agents_dir.glob("*.json")):
        data = _load_sidecar(sidecar)
        if data is None:
            continue
        if _is_pid_alive(data["owner_pid"]):
            continue
        orphans.append(data)
    return orphans


def reap_orphan_agents(project_root: Path) -> int:
    """Kill agent groups whose owner sprint is dead; return the count reaped.

    Sweeps ``.forge/runs/agents/*.json``. A sidecar is acted on only when its
    ``owner_pid`` is no longer alive *and* the group presently holding the
    recorded pgid can still be shown to be the registered one (see
    `_identity_verdict`); otherwise the record is discarded unsignalled, with the
    reason logged. Groups whose owner is still alive are left intact — the sprint's
    own teardown owns them. This is the guaranteed path for the
    abrupt-``SIGKILL`` case, where the synchronous group kill in
    `run_in_process_group` never got to run.

    A record's ``lease`` and its ``observed`` descendants are swept whatever the
    pgid verdict says, and neither needs a verdict of its own. The token was
    generated once for one spawn and never reused, so a live process carrying it
    is that spawn's descendant and nothing else; an observed pid is checked
    against the start time it was seen with, so a recycled id names a different
    process and is left alone. Between them they are the only handle on a
    descendant that left the group by calling ``setsid``, which the pgid by
    construction cannot describe — the token where the environment can be read,
    the observation where it cannot.

    Mutating by design, so only mutating commands (``forge stop``, sprint
    startup) may call it; ``forge status`` uses `list_orphan_agents` instead.
    """
    from theforge.detach import _is_pid_alive  # noqa: PLC0415

    agents_dir = project_root / ".forge" / "runs" / "agents"
    if not agents_dir.exists():
        return 0

    reaped = 0
    for sidecar in sorted(agents_dir.glob("*.json")):
        data = _load_sidecar(sidecar)
        if data is None:
            # Corrupt/unusable sidecar — drop it so it doesn't linger forever.
            _unlink(sidecar)
            continue

        owner_pid = data["owner_pid"]
        pgid = data["pgid"]

        if _is_pid_alive(owner_pid):
            # Sprint still running — its own teardown owns this group.
            continue

        sandbox = data.get("sandbox_dir")
        # A record the suite left behind describes a test subprocess that exited
        # long ago, not a dispatched agent. Only a test run may act on one; for an
        # operator's sweep it is noise pointing at a recyclable pgid (#2115).
        if data.get("origin") == "test" and not _running_under_pytest():
            _log(
                f"  discarding test-origin agent sidecar pgid={pgid} unsignalled "
                f"(owner pid={owner_pid} is dead, sandbox={sandbox})"
            )
            _unlink(sidecar)
            continue

        # No tracker exists here — the process that did the observing is the one
        # that died. What it wrote down before dying is the sweep's substitute
        # for watching: the pgid cannot name a descendant that left the group,
        # and the token cannot be read out of every process (#2309).
        escaped, _gone = kill_escapees(
            lease=_recorded_lease(data), recorded=_recorded_observed(data)
        )

        may_signal, reason = _identity_verdict(pgid, data)
        if not may_signal:
            _log(
                f"  discarding stale agent sidecar pgid={pgid} unsignalled: {reason} "
                f"(owner sprint pid={owner_pid} is dead, sandbox={sandbox})"
            )
            _unlink(sidecar)
            # An unverifiable pgid says nothing about the lease: if the token
            # found escapees, this record did reap something and must say so.
            reaped += 1 if escaped else 0
            continue

        _log(
            f"  reaping orphaned agent process group pgid={pgid} "
            f"(owner sprint pid={owner_pid} is dead, sandbox={sandbox}, "
            f"identity: {reason})"
        )
        kill_agent_group(pgid)
        _unlink(sidecar)
        reaped += 1

    return reaped


def _recorded_observed(data: dict[str, Any]) -> dict[int, str]:
    """The ``observed`` descendant snapshot from a sidecar, as ``{pid: start time}``.

    Written during the run by `record_observed_descendants`, so it survives the
    process that made it — which is the whole point: a sprint that is killed
    outright never runs a teardown, and this is what it leaves behind.
    """
    return _pid_fingerprint_map(data.get("observed"))


def _recorded_lease(data: dict[str, Any]) -> ProcessLease | None:
    """The ``lease`` token from a sidecar, or None for a record without one.

    ``started_at`` is 0.0 and ``opened_since_boot`` None rather than the spawn's
    real moment: the sweep must not skip a descendant on age when it no longer
    knows when the spawn was, and a reap is rare enough to afford the full scan.
    Both are needed to say that — a boot-relative moment left behind by a spawn
    from *this* boot would prune, and one from a previous boot would prune
    wrongly, so a record that cannot date its spawn carries neither.
    """
    token = data.get("lease")
    if not isinstance(token, str) or not token:
        return None
    return ProcessLease(token=token, started_at=0.0, opened_since_boot=None)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
