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
   pgid + owner pid in a sidecar under ``.forge/runs/agents/``. A later ``forge``
   invocation (status/stop/sprint-startup) sweeps those sidecars and ``killpg``-s
   any group whose owner sprint is no longer alive.

Stdlib-only by design (convention 4) so it can be imported by both the runners
and the CLI without pulling in heavier dependencies.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
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
    except BaseException:
        # Covers TimeoutExpired and every other unwind identically: kill the
        # group, then wait for it *bounded*. An unbounded wait here blocks for
        # the child's full natural lifetime whenever the group kill did not land
        # (#1959), turning an enforced timeout into no timeout at all.
        group_killed = terminate_process_group(proc)
        raise
    finally:
        if pgid is not None:
            release_group_record(pgid, group_killed=group_killed)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


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


def register_agent_group(pgid: int, *, sandbox_dir: str | Path | None = None) -> None:
    """Record a spawned agent group's pgid so a later reaper can kill orphans.

    Writes ``.forge/runs/agents/{owner_pid}-{pgid}.json`` under the orchestrator
    runs dir, where ``owner_pid`` is the sprint process (``os.getpid()``). Per-pgid
    files avoid write contention from parallel review pools. No-op when the runs
    dir is unresolvable (e.g. ``FORGE_PROJECT_ROOT`` unset in tests).
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


def reap_orphan_agents(project_root: Path) -> int:
    """Kill agent groups whose owner sprint is dead; return the count reaped.

    Sweeps ``.forge/runs/agents/*.json``. For each sidecar whose ``owner_pid`` is
    no longer alive, ``killpg`` the recorded group, unlink the sidecar, and log the
    reaped sandbox path loudly. Groups whose owner is still alive are left intact.
    This is the guaranteed path for the abrupt-``SIGKILL`` case, where the
    synchronous group kill in `run_in_process_group` never got to run.
    """
    from theforge.detach import _is_pid_alive  # noqa: PLC0415

    agents_dir = project_root / ".forge" / "runs" / "agents"
    if not agents_dir.exists():
        return 0

    reaped = 0
    for sidecar in sorted(agents_dir.glob("*.json")):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Corrupt/unreadable sidecar — drop it so it doesn't linger forever.
            _unlink(sidecar)
            continue

        owner_pid = data.get("owner_pid")
        pgid = data.get("pgid")
        if not isinstance(owner_pid, int) or not isinstance(pgid, int):
            _unlink(sidecar)
            continue

        if _is_pid_alive(owner_pid):
            # Sprint still running — its own teardown owns this group.
            continue

        sandbox = data.get("sandbox_dir")
        _log(
            f"  reaping orphaned agent process group pgid={pgid} "
            f"(owner sprint pid={owner_pid} is dead, sandbox={sandbox})"
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
