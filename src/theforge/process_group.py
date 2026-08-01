"""Process-group isolation, pgid registry sidecars, and an orphan reaper.

Agent CLI subprocesses (``codex``, ``claude``, …) spawn their own child trees
(``npm exec`` → ``node`` → the provider binary). When a sprint process dies —
cleanly, on error, or by an external ``kill -9`` — those trees must die with it.
Two mechanisms cooperate:

1. **Group-isolated spawn** (`run_in_process_group`) — every agent subprocess is
   launched with ``start_new_session=True`` so it leads its own process group.
   On timeout or any teardown the whole group is ``killpg``-ed, so grandchildren
   (node, the provider leaf binary) die too — a bare ``proc.kill()`` reaches only
   the direct child.
2. **Orphan reaper** (`reap_orphan_agents`) — synchronous group-kill cannot run
   when the parent sprint is ``SIGKILL``-ed, so each spawned group records its
   pgid + owner pid in a sidecar under ``.forge/runs/agents/``. A later
   *mutating* ``forge`` invocation (stop / sprint-startup) sweeps those sidecars
   and ``killpg``-s any group whose owner sprint is no longer alive **and whose
   recorded identity still matches the group presently holding that pgid**
   (`reap_orphan_agents`). ``forge status`` only reports them (`list_orphan_agents`) —
   a command whose job is to describe state must not signal processes (#2115).

Stdlib-only by design (convention 4) so it can be imported by both the runners
and the CLI without pulling in heavier dependencies.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

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
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` that isolates the child into its own process group.

    Returns a ``subprocess.CompletedProcess`` with the same shape ``subprocess.run``
    would produce. On ``subprocess.TimeoutExpired`` — or any other ``BaseException``
    raised while the child runs — the *entire* process group is ``SIGKILL``-ed
    before the exception is re-raised, so npm→node→leaf grandchildren cannot
    outlive the call. ``subprocess.run``'s own timeout kill signals only the
    direct child (``npm exec``), leaving node + the provider leaf alive.
    """
    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None
    stdin = subprocess.PIPE if input is not None else None

    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        env=env,
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
        register_agent_group(pgid, sandbox_dir=cwd)
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
        if pgid is not None:
            release_group_record(pgid, group_killed=group_killed)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _attach_partial_output(
    proc: subprocess.Popen[Any],
    exc: subprocess.TimeoutExpired,
    *,
    drain_seconds: float = KILL_GRACE_SECONDS,
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
        out, err = proc.communicate(timeout=drain_seconds)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return
    if out is not None:
        exc.stdout = out
    if err is not None:
        exc.stderr = err


def release_group_record(pgid: int, *, group_killed: bool) -> None:
    """Drop the reaper's sidecar, but only once the group is really gone.

    When teardown could reach only the direct child, that child exits while its
    grandchildren keep running — and unregistering here would erase the one
    record that lets `reap_orphan_agents` ever kill them. The sidecar is how a
    surviving group stays reachable, so it outlives a partial teardown.
    """
    if not group_killed and group_is_alive(pgid):
        _log(
            f"  ⚠ keeping agent sidecar for pgid={pgid}: teardown reached only the "
            "direct child and the group is still alive"
        )
        return
    unregister_agent_group(pgid)


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
    proc: subprocess.Popen[Any], *, grace_seconds: float = KILL_GRACE_SECONDS
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
    # Only ever wait on a signal that was actually delivered. A refused kill
    # tells us up front that nothing will change, so waiting out the grace period
    # for it buys nothing but the latency this function exists to avoid.
    if _killpg_for(proc.pid):
        # SIGKILL reached the group and is uncatchable, so every member is dead
        # or dying. The bounded wait only observes our own child being reaped.
        if not _wait_bounded(proc, grace_seconds):
            _log(f"  ⚠ pid={proc.pid} not reaped after its group was killed")
        return True
    # The group kill did not land. Signalling the direct pid is a strictly weaker
    # guarantee — it cannot reach grandchildren — but it is the one thing a
    # sandbox that denies cross-group signalling still permits. Report it as what
    # it is so the caller keeps tracking whatever survived.
    if _kill_pid(proc.pid):
        _wait_bounded(proc, grace_seconds)
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


def _start_time_from_proc(pid: int) -> str | None:
    """Linux: field 22 of ``/proc/<pid>/stat`` — start time in jiffies since boot."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # comm (field 2) is parenthesised and may itself contain spaces, so split
    # after the final ')' rather than tokenising the whole line.
    _, _, rest = raw.rpartition(")")
    fields = rest.split()
    # rest starts at field 3 (state), so starttime (field 22) is index 19.
    if len(fields) < 20:
        return None
    return f"proc:{fields[19]}"


def _start_time_from_sysctl(pid: int) -> str | None:
    """macOS/BSD: ``kinfo_proc.kp_proc.p_starttime`` via ``sysctl``.

    ``extern_proc`` opens with a union whose other arm is ``p_starttime``, so the
    ``timeval`` sits at offset 0 of the returned record. Microsecond resolution
    makes it a far sharper identity than ``ps``'s second-granularity ``lstart``.
    """
    import ctypes  # noqa: PLC0415

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # CTL_KERN, KERN_PROC, KERN_PROC_PID, <pid>
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)
        buf = ctypes.create_string_buffer(4096)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value < ctypes.sizeof(ctypes.c_long) + ctypes.sizeof(ctypes.c_int):
            return None
        seconds = ctypes.cast(buf, ctypes.POINTER(ctypes.c_long))[0]
        micros = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int))[2]
    except (OSError, ValueError, AttributeError):
        return None
    return f"sysctl:{seconds}.{micros:06d}"


def _leader_fingerprint(pgid: int) -> str | None:
    """Start-time fingerprint of the process leading *pgid*, or None.

    The pgid of a group spawned with ``start_new_session=True`` *is* its leader's
    pid, so the leader's start time is a property a recycled pgid cannot
    reproduce — the evidence the reaper needs before signalling anything (#2115).

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
        return _start_time_from_proc(pgid)
    if sys.platform == "darwin":
        return _start_time_from_sysctl(pgid)
    started = _pid_start_time(pgid)
    return None if started is None else f"ps:{started}"


def register_agent_group(pgid: int, *, sandbox_dir: str | Path | None = None) -> None:
    """Record a spawned agent group's pgid so a later reaper can kill orphans.

    Writes ``.forge/runs/agents/{owner_pid}-{pgid}.json`` under the orchestrator
    runs dir, where ``owner_pid`` is the sprint process (``os.getpid()``). Per-pgid
    files avoid write contention from parallel review pools. No-op when the runs
    dir is unresolvable (e.g. ``FORGE_PROJECT_ROOT`` unset in tests).

    Two fields exist purely so a later sweep can decide whether the record is
    still safe to act on: ``leader_fingerprint`` — the group leader's start time,
    the evidence that the group holding this pgid at reap time is the group
    registered here — and ``origin``, which marks records left by a test run.
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
    }
    try:
        agents_dir.mkdir(parents=True, exist_ok=True)
        _sidecar_path(agents_dir, owner_pid, pgid).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    except OSError:
        pass


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


def _group_exists(pgid: int) -> bool:
    """True only when *pgid* is provably a live group we may signal.

    The strict twin of `group_is_alive`, which errs toward "alive" so a record is
    never dropped while it might still be the only handle on a survivor. Here the
    question is the opposite one — may we *signal* this group — so an
    unanswerable probe (EPERM: a group we do not own, which after pgid reuse is
    exactly the group we must not touch) counts as "no".
    """
    if not is_killable_pgid(pgid):
        return False
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def _identity_verdict(pgid: object, fingerprint: object) -> tuple[bool, str]:
    """Decide whether *pgid* still denotes the registered group; (may_signal, why).

    Process-group ids are recycled by the OS, so a persisted pgid is a claim
    about a past moment, not a durable handle. Sending a group ``SIGKILL`` on
    that claim alone is how a routine sweep came to signal unrelated processes
    (#2115): the cost of not killing a stale group is one lingering process, and
    the cost of killing the wrong one is unbounded and lands outside this tool.
    So a signal requires positive evidence, in one of two forms:

    * **The leader is alive** — then its start time must equal the one recorded
      at registration. A recycled pgid cannot reproduce that. No recorded
      fingerprint, or no readable current one, means no evidence: discard.
    * **The leader has exited but the group is non-empty** — the case the reaper
      exists for (npm→node→leaf grandchildren outliving a refused teardown,
      #2013). This is safe without a fingerprint because both Linux and XNU
      refuse to hand out a pid that is still in use as a process-group id, so a
      group that has never been empty since registration cannot have been
      re-created under the same id by anyone else.

    Anything else — an empty group, a group we may not signal — is discarded.
    """
    from theforge.detach import _is_pid_alive  # noqa: PLC0415

    if not isinstance(pgid, int) or not is_killable_pgid(pgid):
        return False, "pgid cannot denote a real process group"
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
    if _group_exists(pgid):
        return True, "the leader exited but the group is non-empty, so the pgid cannot be reused"
    return False, "no live process group holds this pgid"


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

        may_signal, reason = _identity_verdict(pgid, data.get("leader_fingerprint"))
        if not may_signal:
            _log(
                f"  discarding stale agent sidecar pgid={pgid} unsignalled: {reason} "
                f"(owner sprint pid={owner_pid} is dead, sandbox={sandbox})"
            )
            _unlink(sidecar)
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


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
