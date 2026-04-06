"""Shared PID helpers."""

from __future__ import annotations

import os


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
