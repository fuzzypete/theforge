"""Passive per-reviewer progress channel for the live watch view.

The reviewer pool runs several agents concurrently; each agent-loop iteration
and completion is a structured event (not a parsed log line). This module
aggregates those events, under a lock, into the live ``.state`` story ``detail``
dict via the coordinator's ``state_update_fn`` so ``forge status --watch`` can
render per-reviewer iteration / retry / pool-progress instead of a bare ``—``.

The channel is a *pure event emitter*: it never influences control flow and a
raising ``state_update_fn`` can never break a reviewer (every emit is wrapped).
This respects the runners-don't-decide invariant — the runner emits, the
coordinator aggregates, the CLI renders.

Detail contract written into the story ``detail`` dict (replaced wholesale on
each emit, since ``SprintStoryState`` transitions overwrite ``detail``):

    reviewer_progress: {name: {"iter": int, "tool_calls": int,
                               "retry": [n, m] | None, "done": bool}}
    reviewer_pool_size: int
    last_reviewer_event_ts: float   # epoch seconds of the freshest event
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class ReviewerProgressChannel:
    """Thread-safe aggregator of per-reviewer progress events.

    Seed with every pool profile name so the first frame shows the whole pool.
    Pass :attr:`cb` as ``progress_cb=`` to ``run_agent_pool``; call
    :meth:`set_retry` from the transient-retry loop to surface ``↻ rN/M`` live.
    """

    def __init__(
        self,
        *,
        reviewer_names: list[str],
        phase: str,
        iteration: int,
        cost_usd: float,
        complexity: object,
        state_update_fn: "Callable[[dict], None] | None",
    ) -> None:
        self._phase = phase
        self._iteration = iteration
        self._cost_usd = cost_usd
        self._complexity = complexity
        self._state_update_fn = state_update_fn
        self._lock = threading.Lock()
        self._progress: dict[str, dict] = {
            name: {"iter": 0, "tool_calls": 0, "retry": None, "done": False}
            for name in reviewer_names
        }

    # ── event ingress ────────────────────────────────────────────────
    def cb(self, event: dict) -> None:
        """Ingest one reviewer event. Best-effort: never raises into the pool."""
        try:
            label = event.get("label")
            with self._lock:
                entry = self._progress.get(label)
                if entry is None:
                    return
                if event.get("done"):
                    entry["done"] = True
                if "iter" in event:
                    entry["iter"] = event["iter"]
                if "tool_calls" in event:
                    entry["tool_calls"] = event["tool_calls"]
                self._emit_locked()
        except Exception:
            pass

    def set_retry(self, name: str, retry_n: int, retry_max: int) -> None:
        """Mark a reviewer as retrying (surfaces ``↻ rN/M``) and re-emit."""
        try:
            with self._lock:
                entry = self._progress.get(name)
                if entry is None:
                    return
                entry["retry"] = [retry_n, retry_max]
                self._emit_locked()
        except Exception:
            pass

    # ── state egress ─────────────────────────────────────────────────
    def _emit_locked(self) -> None:
        """Write the full review-scoped detail into live state. Caller holds lock."""
        if self._state_update_fn is None:
            return
        detail = {
            "reviewer_progress": {k: dict(v) for k, v in self._progress.items()},
            "reviewer_pool_size": len(self._progress),
            "last_reviewer_event_ts": time.time(),
        }
        try:
            self._state_update_fn(
                {
                    "phase": self._phase,
                    "iteration": self._iteration,
                    "cost_usd": self._cost_usd,
                    "complexity": self._complexity,
                    "detail": detail,
                }
            )
        except Exception:
            pass
