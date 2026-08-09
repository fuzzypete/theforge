"""The enclosing worker window a story runs inside, and what it excludes.

A sprint story is dispatched with a *worker timeout*: the wall-clock ceiling the
scheduler will let its worker thread occupy before killing it. Everything the
story does happens inside that ceiling — every development invocation, every
review cycle, every operator gate — but until this module existed nothing inside
the story could see it. Phase-level allowances were derived from unrelated base
values, so a single development invocation could be granted ~92% of the whole
story ceiling and a gate could be told to wait for exactly as long as the story
itself was allowed to live (#2333).

Two things live here:

1. **The registry.** The sprint runner registers a :class:`WorkerBudget` per
   dispatched story. Code deep inside the coordinator reaches it through
   :func:`current_worker_budget`, keyed by the same thread-local worker slug the
   log emitter already uses — no signature threading, and a no-op outside a
   sprint (``forge run`` registers nothing, so every accessor returns ``None``).

2. **Operator-wait accounting.** Time the system spends waiting for a human
   decision is not time the worker is unresponsive. :func:`operator_wait` marks
   those intervals; the scheduler credits them back to the story deadline via
   :func:`operator_wait_credit`, so a wait the system itself chose to begin is
   never charged against the story as a fault.

Stdlib-only by design: this is a leaf module imported by the sprint runner, the
coordinator, and the pending-decision poller alike.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .log_util import get_worker_slug

__all__ = [
    "WorkerBudget",
    "clear_worker_budgets",
    "current_worker_budget",
    "get_worker_budget",
    "operator_wait",
    "operator_wait_credit",
    "register_worker_budget",
    "remaining_seconds",
    "unregister_worker_budget",
]


@dataclass
class WorkerBudget:
    """One dispatched story's wall-clock ceiling and its operator-wait ledger.

    ``group`` lets several slugs share one ceiling. A cost-aware batch group runs
    its members on a single worker thread under a single summed deadline, so a
    wait entered under one member's slug must credit the deadline all of them are
    measured against.
    """

    slug: str
    worker_timeout_seconds: float
    started_at: float
    group: str | None = None
    #: Completed operator waits, in seconds.
    operator_wait_seconds: float = 0.0
    #: Monotonic start of an operator wait currently in progress, if any.
    wait_started_at: float | None = None
    #: Human-readable label for the in-progress wait (gate phase), for messages.
    wait_label: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def credit(self, now: float | None = None) -> float:
        """Seconds of operator wait accrued so far, including an in-flight wait."""
        with self._lock:
            credit = self.operator_wait_seconds
            if self.wait_started_at is not None:
                # Read the clock only when a wait is actually open: the credit of
                # an idle story is exact arithmetic on what it already banked.
                _now = time.monotonic() if now is None else now
                credit += max(0.0, _now - self.wait_started_at)
        return credit

    def waiting(self) -> bool:
        """True while this story is blocked on an operator decision."""
        with self._lock:
            return self.wait_started_at is not None

    def remaining(self, now: float | None = None) -> float:
        """Working seconds left before the enclosing deadline, waits excluded."""
        _now = time.monotonic() if now is None else now
        elapsed = max(0.0, _now - self.started_at) - self.credit(_now)
        return self.worker_timeout_seconds - elapsed

    def begin_wait(self, label: str = "") -> None:
        with self._lock:
            if self.wait_started_at is None:
                self.wait_started_at = time.monotonic()
                self.wait_label = label

    def end_wait(self) -> float:
        """Close an in-flight wait, fold it into the ledger, return its length."""
        with self._lock:
            if self.wait_started_at is None:
                return 0.0
            waited = max(0.0, time.monotonic() - self.wait_started_at)
            self.operator_wait_seconds += waited
            self.wait_started_at = None
            self.wait_label = ""
        return waited


_registry: dict[str, WorkerBudget] = {}
_registry_lock = threading.Lock()


def register_worker_budget(
    slug: str,
    worker_timeout_seconds: float,
    *,
    group: str | None = None,
    started_at: float | None = None,
) -> WorkerBudget:
    """Register (and return) the enclosing budget for a dispatched story."""
    budget = WorkerBudget(
        slug=slug,
        worker_timeout_seconds=float(worker_timeout_seconds),
        started_at=time.monotonic() if started_at is None else started_at,
        group=group,
    )
    with _registry_lock:
        _registry[slug] = budget
    return budget


def unregister_worker_budget(slug: str) -> None:
    """Drop a story's budget once its worker is no longer running."""
    with _registry_lock:
        _registry.pop(slug, None)


def get_worker_budget(slug: str) -> WorkerBudget | None:
    with _registry_lock:
        return _registry.get(slug)


def current_worker_budget() -> WorkerBudget | None:
    """Return the budget for the story running on *this* thread, if any.

    Keyed by the thread-local worker slug the sprint runner already sets, so
    coordinator code reaches its enclosing ceiling without threading it through
    every signature. Returns ``None`` under ``forge run`` and in any thread that
    is not a registered story worker — every caller must treat that as "no
    enclosing budget" and leave its own allowance alone.
    """
    slug = get_worker_slug()
    if not slug:
        return None
    return get_worker_budget(slug)


def clear_worker_budgets() -> None:
    """Drop every registered budget (sprint teardown, and test isolation)."""
    with _registry_lock:
        _registry.clear()


def _group_peers(budget: WorkerBudget) -> list[WorkerBudget]:
    if budget.group is None:
        return [budget]
    with _registry_lock:
        return [b for b in _registry.values() if b.group == budget.group]


def operator_wait_credit(slug: str, now: float | None = None) -> float:
    """Seconds the scheduler must add to *slug*'s deadline for operator waits.

    Includes a wait still in progress, so a deadline check landing mid-wait sees
    the credit rather than expiring the story eighteen seconds before the gate it
    is sitting in would have answered.
    """
    budget = get_worker_budget(slug)
    if budget is None:
        return 0.0
    return sum(peer.credit(now) for peer in _group_peers(budget))


def remaining_seconds(slug: str, now: float | None = None) -> float | None:
    """Working seconds left for *slug*, or ``None`` when it has no budget."""
    budget = get_worker_budget(slug)
    if budget is None:
        return None
    return budget.remaining(now)


def waiting_on_operator(slug: str) -> tuple[bool, str, float]:
    """Return ``(waiting, label, waited_seconds)`` for *slug*'s current wait."""
    budget = get_worker_budget(slug)
    if budget is None:
        return False, "", 0.0
    for peer in _group_peers(budget):
        if peer.waiting():
            with peer._lock:
                started = peer.wait_started_at
                label = peer.wait_label
            waited = max(0.0, time.monotonic() - started) if started is not None else 0.0
            return True, label, waited
    return False, "", 0.0


@contextmanager
def operator_wait(label: str = "", budget: WorkerBudget | None = None) -> Iterator[float]:
    """Mark the enclosed block as time spent waiting for an operator decision.

    Yields the seconds already credited before the wait began. A no-op when there
    is no enclosing budget, which is the standalone-``forge run`` case.
    """
    _budget = budget if budget is not None else current_worker_budget()
    if _budget is None:
        yield 0.0
        return
    already = _budget.operator_wait_seconds
    _budget.begin_wait(label)
    try:
        yield already
    finally:
        _budget.end_wait()
