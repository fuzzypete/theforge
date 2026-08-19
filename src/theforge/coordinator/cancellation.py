"""Cooperative cancellation signal for sprint-driven worker stops.

Sprint workers run inside a ThreadPoolExecutor. Future.cancel() cannot stop a
running thread, so the sprint scheduler signals cancellation via a per-story
threading.Event. The coordinator checks the event at phase boundaries and
raises StoryCancelled, which entry points catch to bypass end-of-run
persistence (escalation history, model profile updates) — the scheduler's
synthetic record is the only artifact for cancelled stories.

The sprint has more than one reason to pull that lever — a worker deadline, an
auth circuit breaker, an exhausted sprint budget — and they are not the same
fact about the story. :class:`StopSignal` carries the reason alongside the
signal so the cancelled result says which one it was, instead of every stop
reading downstream as the timeout the first caller happened to be.
"""

from __future__ import annotations

import threading

#: What a cancellation says happened when nobody said otherwise. Preserved as
#: the default because the worker deadline was the original and only caller.
DEFAULT_CANCEL_REASON = "Story cancelled by sprint timeout"
DEFAULT_CANCEL_ERROR_TYPE = "StoryCancelled"

#: Error type stamped on a story the sprint killed because its cap was reached.
#: Distinct from the timeout type so no consumer reads a budget halt as an
#: unresponsive worker, and neither reads as a judgment about the work (#2547).
BUDGET_CANCEL_ERROR_TYPE = "SprintBudgetExhausted"


class StoryCancelled(Exception):
    """Raised when a sprint-level stop_event signals worker cancellation."""


class StopSignal(threading.Event):
    """A cancellation event that also records why the sprint set it.

    A plain ``set()`` keeps the historical meaning (worker deadline), so callers
    that predate the distinction are unaffected. ``stop()`` names a different
    cause, and the coordinator stamps that cause on the cancelled result.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reason: str = DEFAULT_CANCEL_REASON
        self.error_type: str = DEFAULT_CANCEL_ERROR_TYPE

    def stop(self, reason: str, *, error_type: str = DEFAULT_CANCEL_ERROR_TYPE) -> None:
        """Signal cancellation with an explicit cause."""
        self.reason = reason
        self.error_type = error_type
        self.set()


def cancel_cause(stop_event: "threading.Event | None") -> tuple[str, str]:
    """The (reason, error_type) a cancellation through *stop_event* carries.

    Reads defensively rather than by type: the coordinator accepts any
    ``threading.Event``, and a caller that passes a bare one still gets the
    historical timeout wording.
    """
    reason = getattr(stop_event, "reason", None) or DEFAULT_CANCEL_REASON
    error_type = getattr(stop_event, "error_type", None) or DEFAULT_CANCEL_ERROR_TYPE
    return str(reason), str(error_type)
