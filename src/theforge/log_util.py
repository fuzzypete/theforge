"""Shared log-line emitter with HH:MM:SS.mmm timestamps.

All modules with a local ``_log`` or ``_log_verbose`` wrapper delegate here
so every line written to stderr carries a consistent timestamp prefix.

This module also owns the thread-local *worker slug* — a short tag (e.g. an
issue number under ``forge diagnose --parallel`` or a story slug under
``forge sprint --parallel``) that every emitted line is prefixed with, so
concurrent workers' interleaved output stays attributable to its source.
Owning it here (a stdlib-only leaf module) lets both the coordinator and the
lower-level runners tag their lines through the shared emitter without an
upward dependency; ``coordinator.log_tee`` re-exports the accessors for
backward compatibility.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime

_worker_ctx = threading.local()


def set_worker_slug(slug: str) -> None:
    """Set the current thread's worker slug (empty string clears it)."""
    _worker_ctx.slug = slug


def get_worker_slug() -> str:
    """Return the current thread's worker slug, or ``""`` if unset."""
    return getattr(_worker_ctx, "slug", "")


def _log_line(tag: str, msg: str) -> None:
    """Emit a single tagged log line to stderr with a millisecond timestamp.

    Format: ``<tag> HH:MM:SS.mmm [<slug>] <msg>`` — the ``[<slug>]`` segment is
    present only when the current thread has a worker slug set, so parallel
    workers' lines are attributable to their issue/story.
    """
    now = datetime.now()
    ts = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
    slug = get_worker_slug()
    prefix = f"[{slug}] " if slug else ""
    print(f"{tag} {ts} {prefix}{msg}", file=sys.stderr, flush=True)
