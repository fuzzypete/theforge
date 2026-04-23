"""Shared PID helpers."""

from __future__ import annotations

import os
import subprocess


def _is_pid_alive(pid: int) -> bool:
    """Return True when *pid* refers to a running process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _pid_start_time(pid: int) -> str | None:
    """Return a stable process start-time fingerprint for *pid*, if available.

    Uses ``ps`` elapsed-start metadata rather than command names so callers can
    distinguish a recycled PID from the original process that created a lock.
    Returns ``None`` when the process does not exist or the start time cannot be
    determined.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    start_time = result.stdout.strip()
    return start_time or None


def _current_process_fingerprint(pid: int) -> str | None:
    """Return the current ownership fingerprint for *pid*, if the process exists."""
    if not _is_pid_alive(pid):
        return None
    return _pid_start_time(pid)


def _pid_matches_fingerprint(pid: int, fingerprint: str | None) -> bool:
    """Return True when *pid* still belongs to the recorded process instance."""
    if fingerprint is None:
        return _is_pid_alive(pid)
    current = _current_process_fingerprint(pid)
    return current is not None and current == fingerprint
