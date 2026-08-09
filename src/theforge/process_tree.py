"""Descendant tracking: who a spawn started, observed while that is knowable.

A process group is the cheap container, but it is escapable — anything that calls
``setsid`` leaves it — and once a descendant has left, nothing the OS still holds
points back to the spawn that started it. Asking "which processes belong to this
invocation?" *after* the invocation ends is a question that may no longer have an
answer:

* **Parentage is erased.** An orphan reparents to ``launchd``/``init`` the moment
  its parent exits, taking the only link to its ancestor with it.
* **The environment is not universally readable.** Stamping a token into the
  child's environment (`theforge.process_group.open_process_lease`) survives
  ``setsid``, but reading it back out of another process does not work
  everywhere: on macOS ``KERN_PROCARGS2`` is refused for SIP-protected platform
  binaries, so a descendant that is ``/bin/sleep`` or ``/bin/sh`` — the ordinary
  shape of a shell command — cannot be found that way at all. A descendant can
  also drop the token deliberately (``env -i``).

So this module does not interrogate afterwards; it **observes while the answer
still exists**. A `DescendantTracker` walks the child links of what it already
knows about, at short intervals, and remembers every descendant it sees together
with the start time that identifies it. What it recorded stays known after the
parent that proved the relationship is gone.

Sampling reads only what the kernel exposes for every same-uid process however
hardened — pid, ppid, pgid, start time — through ``/proc`` on Linux and
``libproc`` on macOS, and it asks only about pids it already cares about rather
than scanning the table, so a pass costs tens of microseconds.

Identity is a start time, never an id (the discipline #2115 established). A
recorded pid is handed to a kill only while it still carries the start time
recorded for it, so a pid recycled between observation and teardown is left
alone.

**The residual window, stated plainly.** A descendant that is created *and*
orphaned entirely between two samples is never observed as anyone's child, and
if its environment is also unreadable nothing else can identify it either.
Closing that completely takes a kernel container — a cgroup, a job object, a
subreaper — and macOS offers unentitled processes none of them (its coalitions
survive ``setsid`` and reparenting, but a process inherits its coalition from
whatever started *forge*, so killing by coalition would take the operator's shell
with it). The layers here are chosen so that each covers what the others cannot,
and the interval is short enough that the gap is sub-100ms.

Stdlib-only by design (convention 4), like `theforge.process_group`.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# How often the tracker walks its known set while an invocation runs. A pass is
# one cheap query per already-known pid, not a table scan, so this can be short:
# the interval *is* the width of the unobserved window described above.
SAMPLE_INTERVAL_SECONDS = 0.05


def is_real_pid(value: object) -> bool:
    """True only for a value that can denote a real process.

    The same guard, and for the same reason, as
    ``theforge.process_group.is_killable_pgid``: a test double's unset ``pid`` is
    a ``Mock``, not a number, and letting one into a set of processes to signal
    is how a bogus id reaches a kill (#1793). Values ``<= 1`` are never a spawned
    descendant — pid 1 is ``init``/``launchd``.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 1


@dataclass(frozen=True)
class ProcessInfo:
    """One live process, in the fields that are always readable."""

    pid: int
    ppid: int
    pgid: int
    fingerprint: str
    """Start time — the only identity a recyclable pid has."""


# ── Linux: /proc ─────────────────────────────────────────────────────


def _parse_stat(raw: str) -> tuple[int, int, str] | None:
    """``/proc/<pid>/stat`` → ``(ppid, pgrp, fingerprint)``.

    comm (field 2) is parenthesised and may itself contain spaces, so the split
    starts after the final ``)``: ``rest`` begins at field 3 (state), making ppid
    (field 4) index 1, pgrp (field 5) index 2, starttime (field 22) index 19.
    """
    _, _, rest = raw.rpartition(")")
    fields = rest.split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[1]), int(fields[2]), f"proc:{fields[19]}"
    except ValueError:
        return None


def _linux_info(pid: int) -> ProcessInfo | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parsed = _parse_stat(raw)
    return None if parsed is None else ProcessInfo(pid, parsed[0], parsed[1], parsed[2])


def _linux_children(pid: int) -> list[int]:
    """Direct children of *pid*, from ``/proc/<pid>/task/<tid>/children``.

    Threads each keep their own children list, so every task directory is read.
    Missing on a kernel built without ``CONFIG_PROC_CHILDREN``; the caller falls
    back to a full scan when this yields nothing for a live process.
    """
    children: list[int] = []
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return []
    for tid in tids:
        try:
            raw = Path(f"/proc/{pid}/task/{tid}/children").read_text(encoding="utf-8")
        except OSError:
            continue
        children.extend(int(part) for part in raw.split() if part.isdigit())
    return children


def _linux_scan() -> dict[int, ProcessInfo]:
    try:
        entries = os.listdir("/proc")
    except OSError:
        return {}
    table: dict[int, ProcessInfo] = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        info = _linux_info(int(entry))
        if info is not None:
            table[info.pid] = info
    return table


# ── macOS: libproc ───────────────────────────────────────────────────
#
# ``proc_pidinfo(pid, PROC_PIDTBSDINFO, …)`` fills a ``struct proc_bsdinfo``,
# public ABI, whose pbi_pid/pbi_ppid/pbi_pgid and closing start-time pair sit at
# fixed offsets. Chosen over the ``kinfo_proc`` its sibling module reads because
# it answers for *every* same-uid process, including the SIP-protected platform
# binaries whose argv/environ ``KERN_PROCARGS2`` refuses outright — which is the
# hole that made an environment token insufficient as containment.
#
# The offsets are checked against ground truth before use (`_layout_is_valid`)
# rather than trusted: this process knows its own pid, ppid and pgid, so a layout
# that cannot reproduce them is one we must not act on. A misread field here
# would mean choosing a kill target from the wrong bytes.
_PROC_PIDTBSDINFO = 3
_PROC_ALL_PIDS = 1
_PROC_PGRP_ONLY = 2
_PROC_PPID_ONLY = 6
_BSDINFO_SIZE = 136
_OFF_PID = 12
_OFF_PPID = 16
_OFF_PGID = 100
_OFF_START_SEC = 120
_OFF_START_USEC = 128

_state: dict[str, object] = {}


def _libproc() -> ctypes.CDLL | None:
    if "lib" not in _state:
        try:
            _state["lib"] = ctypes.CDLL(None, use_errno=True)
        except (OSError, AttributeError):
            _state["lib"] = None
    lib = _state["lib"]
    return lib if isinstance(lib, ctypes.CDLL) else None


def _darwin_info(pid: int) -> ProcessInfo | None:
    lib = _libproc()
    if lib is None or pid <= 0:
        return None
    buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
    try:
        written = lib.proc_pidinfo(pid, _PROC_PIDTBSDINFO, ctypes.c_uint64(0), buf, _BSDINFO_SIZE)
    except (OSError, AttributeError, ValueError):
        return None
    if written < _BSDINFO_SIZE:
        # Gone, or refused — either way nothing is known about it.
        return None
    raw = buf.raw
    try:
        seconds = struct.unpack_from("Q", raw, _OFF_START_SEC)[0]
        micros = struct.unpack_from("Q", raw, _OFF_START_USEC)[0]
        return ProcessInfo(
            pid=struct.unpack_from("I", raw, _OFF_PID)[0],
            ppid=struct.unpack_from("I", raw, _OFF_PPID)[0],
            pgid=struct.unpack_from("I", raw, _OFF_PGID)[0],
            fingerprint=f"bsdinfo:{seconds}.{micros:06d}",
        )
    except struct.error:
        return None


def _layout_is_valid() -> bool:
    """True once ``proc_bsdinfo``'s offsets reproduce facts we already know.

    Cached — an ABI cannot change under a running process. A failure disables
    macOS descendant discovery instead of letting misread bytes name a target.
    """
    cached = _state.get("layout_ok")
    if isinstance(cached, bool):
        return cached
    me = _darwin_info(os.getpid())
    ok = (
        me is not None
        and me.pid == os.getpid()
        and me.ppid == os.getppid()
        and me.pgid == os.getpgrp()
    )
    _state["layout_ok"] = ok
    return ok


def _darwin_listpids(kind: int, selector: int) -> list[int]:
    lib = _libproc()
    if lib is None:
        return []
    capacity = 1024 if kind != _PROC_ALL_PIDS else 8192
    for _ in range(3):
        buf = (ctypes.c_int * capacity)()
        try:
            written = lib.proc_listpids(kind, selector, buf, ctypes.sizeof(buf))
        except (OSError, AttributeError, ValueError):
            return []
        if written <= 0:
            return []
        count = written // ctypes.sizeof(ctypes.c_int)
        if count < capacity:
            return [pid for pid in buf[:count] if pid > 0]
        # Filled the buffer: the answer may have been truncated, and a truncated
        # answer silently loses descendants. Retry with room.
        capacity *= 4
    return []


def _darwin_scan() -> dict[int, ProcessInfo]:
    table: dict[int, ProcessInfo] = {}
    for pid in _darwin_listpids(_PROC_ALL_PIDS, 0):
        info = _darwin_info(pid)
        if info is not None and info.pid == pid:
            table[pid] = info
    return table


# ── Platform-neutral surface ─────────────────────────────────────────


def _supported() -> bool:
    if sys.platform.startswith("linux"):
        return True
    return sys.platform == "darwin" and _layout_is_valid()


def live_pids() -> list[int]:
    """Every live pid this user can see.

    Shared with `theforge.process_group`'s lease sweep so both containment
    mechanisms enumerate the same way: the retry-on-growth handling here is the
    difference between "the table was busy" and "there are no processes", and a
    sweep that cannot tell those apart reports a clean host under load.
    """
    if sys.platform.startswith("linux"):
        try:
            return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
        except OSError:
            return []
    if sys.platform == "darwin" and _layout_is_valid():
        return _darwin_listpids(_PROC_ALL_PIDS, 0)
    return []


def process_info(pid: int) -> ProcessInfo | None:
    """One process's ppid/pgid/start time, or None if it is gone or opaque."""
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        return _linux_info(pid)
    if sys.platform == "darwin" and _layout_is_valid():
        return _darwin_info(pid)
    return None


def children_of(pid: int) -> list[int]:
    """Direct children of *pid*, asked of the kernel rather than scanned for."""
    if pid <= 0:
        return []
    if sys.platform.startswith("linux"):
        children = _linux_children(pid)
        if children or _linux_info(pid) is None:
            return children
        # A live process with no children list means the kernel does not expose
        # one; fall back to the scan rather than concluding it is childless.
        return [info.pid for info in _linux_scan().values() if info.ppid == pid]
    if sys.platform == "darwin" and _layout_is_valid():
        return _darwin_listpids(_PROC_PPID_ONLY, pid)
    return []


def members_of_group(pgid: int) -> list[int]:
    """Every live process in *pgid*."""
    if pgid <= 1:
        return []
    if sys.platform.startswith("linux"):
        return [info.pid for info in _linux_scan().values() if info.pgid == pgid]
    if sys.platform == "darwin" and _layout_is_valid():
        return _darwin_listpids(_PROC_PGRP_ONLY, pgid)
    return []


class DescendantTracker:
    """Records what a spawn started, by watching while the links still exist.

    Start it as soon as the child is spawned and stop it at teardown. In between
    it repeatedly walks the child links of everything it already knows about, so
    a descendant that later reparents to ``init`` — destroying the only evidence
    of where it came from — is still known to belong to this invocation.

    `survivors` re-checks every recorded pid against its recorded start time, so
    what reaches a kill contains only processes that are still the ones observed.
    Nothing here signals anything; the caller decides what to do with the set.
    """

    def __init__(self, *, root_pid: int | None = None, pgid: int | None = None) -> None:
        self._roots = {pid for pid in (root_pid,) if is_real_pid(pid)}
        self._pgid = pgid if is_real_pid(pgid) else None
        self._seen: dict[int, str] = {}
        self._frontier: set[int] = set(self._roots)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = bool(self._roots or self._pgid) and _supported()

    @property
    def active(self) -> bool:
        """False where the platform offers no way to read process ancestry."""
        return self._active

    @property
    def recorded(self) -> dict[int, str]:
        with self._lock:
            return dict(self._seen)

    def observe(self) -> None:
        """Walk one generation out from everything currently known."""
        if not self._active:
            return
        with self._lock:
            frontier = set(self._frontier)
        candidates: list[int] = []
        for pid in frontier:
            candidates.extend(children_of(pid))
        if self._pgid is not None:
            # A group member is ours whether or not we ever saw it born — this
            # covers a descendant whose own parent chain broke before the first
            # sample but which never left the group.
            candidates.extend(members_of_group(self._pgid))

        me = os.getpid()
        with self._lock:
            fresh = [
                pid
                for pid in candidates
                if is_real_pid(pid)
                and pid != me
                and pid not in self._seen
                and pid not in self._roots
            ]
        for pid in fresh:
            info = process_info(pid)
            if info is None:
                continue
            with self._lock:
                # Union, never replace: the point is to remember a descendant
                # that has since been orphaned and would appear in no later
                # snapshot as anything's child.
                self._seen.setdefault(pid, info.fingerprint)
                self._frontier.add(pid)

    def start(self) -> None:
        if self._thread is not None or not self._active:
            return
        self.observe()
        self._thread = threading.Thread(
            target=self._run, name="forge-descendant-tracker", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(SAMPLE_INTERVAL_SECONDS):
            try:
                self.observe()
            except Exception:  # noqa: BLE001
                # Sampling is an observation, never a reason to fail a run — and
                # never a reason to stop watching either. One bad pass costs this
                # sample; returning here would silently blind the tracker for the
                # rest of the invocation, which is the failure it exists to catch.
                continue

    def stop(self) -> None:
        """Stop sampling, after one final pass."""
        if self._stop.is_set():
            return
        try:
            self.observe()
        except Exception:  # noqa: BLE001
            pass
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=SAMPLE_INTERVAL_SECONDS * 20)

    def survivors(self, *, exclude: set[int] | None = None) -> dict[int, str]:
        """Recorded descendants that are still alive *and* still themselves.

        The start-time re-check is what makes this safe to kill by pid: a pid
        recycled since it was recorded names a different process, and signalling
        it is precisely the mistake #2115 exists to prevent.
        """
        recorded = self.recorded
        if not recorded:
            return {}
        skip = {os.getpid()} | (exclude or set())
        alive: dict[int, str] = {}
        for pid, fingerprint in recorded.items():
            if pid in skip or not is_real_pid(pid):
                continue
            info = process_info(pid)
            if info is not None and info.fingerprint == fingerprint:
                alive[pid] = fingerprint
        return alive

    def __enter__(self) -> DescendantTracker:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def wait_until_gone(pids: dict[int, str], *, timeout: float) -> dict[int, str]:
    """Poll until every pid in *pids* is gone or recycled; return what remains."""
    deadline = time.monotonic() + timeout
    remaining = dict(pids)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        still: dict[int, str] = {}
        for pid, fingerprint in remaining.items():
            info = process_info(pid)
            if info is not None and info.fingerprint == fingerprint:
                still[pid] = fingerprint
        remaining = still
    return remaining
