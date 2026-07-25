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
                               "retry": [n, m] | None, "done": bool,
                               "nudge": int | None}}
    reviewer_pool_size: int
    last_reviewer_event_ts: float   # epoch seconds of the freshest event
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .util import live_complexity_fields


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
        complexity_score: "int | None" = None,
    ) -> None:
        self._phase = phase
        self._iteration = iteration
        self._cost_usd = cost_usd
        self._complexity = complexity
        self._complexity_score = complexity_score
        self._state_update_fn = state_update_fn
        self._lock = threading.Lock()
        self._progress: dict[str, dict] = {
            name: {
                "iter": 0,
                "tool_calls": 0,
                "retry": None,
                "done": False,
                "nudge": None,
            }
            for name in reviewer_names
        }
        # Emit the seeded pool state immediately so the watch view renders
        # "pool 0/N done" the moment a reviewer pool starts — before any
        # reviewer has emitted its first tool-call event — instead of falling
        # back to legacy "running". Best-effort (never raises into the caller).
        with self._lock:
            self._emit_locked()

    # ── event ingress ────────────────────────────────────────────────
    def cb(self, event: dict) -> None:
        """Ingest one reviewer event. Best-effort: never raises into the pool."""
        try:
            label = event.get("label")
            with self._lock:
                entry = self._progress.get(label)
                if entry is None:
                    return
                # A fresh iteration/completion event means the reviewer is
                # making progress again, so an outstanding transient-retry
                # marker is stale — clear it. set_retry() re-arms it if the
                # next attempt also fails, so ↻rN/M only shows while a retry
                # is actually outstanding (AC #3: "while it is retrying").
                if event.get("done"):
                    entry["done"] = True
                    entry["retry"] = None
                    # The reviewer finalized: the imminent-timeout warning is no
                    # longer relevant, so clear the nudge marker (AC: persists
                    # until the reviewer finalizes or times out).
                    entry["nudge"] = None
                if "iter" in event:
                    entry["iter"] = event["iter"]
                    entry["retry"] = None
                if "tool_calls" in event:
                    entry["tool_calls"] = event["tool_calls"]
                if "nudge" in event:
                    # A time-nudge was sent: the reviewer is within seconds of its
                    # wall-clock deadline. Unlike ``retry``, this marker is NOT
                    # cleared by subsequent iter events — the reviewer stays near
                    # its deadline — it only clears on finalize (done above).
                    entry["nudge"] = event["nudge"]
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
            # Carry the cycle number so it survives this writer's wholesale detail
            # replace. self._iteration is seeded at state.review_cycle + 1, i.e.
            # the current cycle. Keeps STAGE cycle context present while reviewers
            # are actively emitting events.
            "review_cycle": self._iteration,
        }
        try:
            self._state_update_fn(
                {
                    "phase": self._phase,
                    "iteration": self._iteration,
                    "cost_usd": self._cost_usd,
                    # Carry the numeric score alongside the band so a progress
                    # frame cannot overwrite a numeric live display with band-only
                    # state. When no score is known the key is omitted, preserving
                    # any score prior phases established (never clearing it).
                    **live_complexity_fields(self._complexity, self._complexity_score),
                    "detail": detail,
                }
            )
        except Exception:
            pass
