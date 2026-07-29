"""In-process registry of the live ``CoordinatorState`` for each running story.

The sprint scheduler and the coordinator engine run in the same process but on
different threads: the scheduler cannot see the ``CoordinatorState`` the engine
is mutating, because that object never leaves the worker's stack until the
engine returns a ``CoordinatorResult``. A story that times out never returns
one, so the audit the scheduler writes for it was built from a fresh, empty
state — ``run_id: null``, no ``dev_loop``, no ``gate_decisions`` — even though
the run log shows several dev iterations and gate retries actually happened
(#2013).

This module is the seam that makes that history reachable: the engine registers
its state under the story slug for the lifetime of the run, and the scheduler
snapshots it when it has to synthesize a terminal result. Registration is
lifecycle bookkeeping, not a process decision — the engine's control flow is
unchanged by it.
"""

from __future__ import annotations

import copy
import threading

from .state import CoordinatorState

_lock = threading.Lock()
_live_states: dict[str, CoordinatorState] = {}


def register_live_state(slug: str, state: CoordinatorState) -> None:
    """Publish *state* as the live coordinator state for *slug*."""
    if not slug:
        return
    with _lock:
        _live_states[slug] = state


def release_live_state(slug: str, state: CoordinatorState | None = None) -> None:
    """Drop *slug*'s registration.

    When *state* is given, only that exact object is dropped, so a worker
    unwinding late cannot delete the registration a re-dispatched run of the
    same slug has already installed.
    """
    if not slug:
        return
    with _lock:
        current = _live_states.get(slug)
        if current is None:
            return
        if state is not None and current is not state:
            return
        del _live_states[slug]


def snapshot_live_state(slug: str) -> CoordinatorState | None:
    """Return a detached copy of *slug*'s live state, or None if not registered.

    The owning worker thread is still running when the scheduler calls this (a
    timed-out worker is unresponsive, not stopped), so the registered object is
    being mutated concurrently. Copying keeps the audit writer off that shared
    object; a deep copy that trips over a member mid-mutation falls back to a
    shallow one rather than losing the telemetry altogether.
    """
    with _lock:
        state = _live_states.get(slug)
    if state is None:
        return None
    try:
        return copy.deepcopy(state)
    except Exception:  # noqa: BLE001 — telemetry salvage must never raise
        try:
            return copy.copy(state)
        except Exception:  # noqa: BLE001
            return state
