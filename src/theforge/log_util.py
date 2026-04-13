"""Shared log-line emitter with HH:MM:SS.mmm timestamps.

All modules with a local ``_log`` or ``_log_verbose`` wrapper delegate here
so every line written to stderr carries a consistent timestamp prefix.
"""

from __future__ import annotations

import sys
from datetime import datetime


def _log_line(tag: str, msg: str) -> None:
    """Emit a single tagged log line to stderr with a millisecond timestamp.

    Format: ``<tag> HH:MM:SS.mmm <msg>``
    """
    now = datetime.now()
    ts = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
    print(f"{tag} {ts} {msg}", file=sys.stderr, flush=True)
