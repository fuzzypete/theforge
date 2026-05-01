"""Cooperative cancellation signal for sprint-driven worker timeouts.

Sprint workers run inside a ThreadPoolExecutor. Future.cancel() cannot stop a
running thread, so the sprint scheduler signals cancellation via a per-story
threading.Event. The coordinator checks the event at phase boundaries and
raises StoryCancelled, which entry points catch to bypass end-of-run
persistence (escalation history, model profile updates) — the scheduler's
synthetic timeout record is the only artifact for cancelled stories.
"""

from __future__ import annotations


class StoryCancelled(Exception):
    """Raised when a sprint-level stop_event signals worker cancellation."""
