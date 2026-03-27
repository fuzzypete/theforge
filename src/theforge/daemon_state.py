"""Daemon state persistence: PID, socket, daemon.json, crash log.

Owns the file-level I/O for the forge daemon. Kept separate from the server
and process-lifecycle code in daemon.py so that stories about state schema
changes don't conflict with stories about daemon protocol or process management.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .config import ForgeConfig

# ── Constants ──────────────────────────────────────────────────────────

_PID_FILE = ".forge/daemon.pid"
_SOCK_FILE = ".forge/daemon.sock"
_STATE_FILE = ".forge/daemon.json"
_CRASH_LOG = ".forge/logs/crashes.jsonl"
_MAX_COMPLETED = 20  # keep last N completed sprint summaries


# ── PID helpers ────────────────────────────────────────────────────────


def _read_pid(pid_file: Path) -> int | None:
    """Read PID from pid_file, return None if missing or invalid."""
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def is_daemon_running(forge_root: Path) -> bool:
    """Return True if the daemon process is alive."""
    pid_file = forge_root / _PID_FILE
    pid = _read_pid(pid_file)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        # Stale PID file — clean it up
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it
        return True


# ── Atomic state file writers ──────────────────────────────────────────


def _write_daemon_json(forge_root: Path, state_dict: dict) -> None:
    """Atomically write daemon.json via tempfile + os.replace."""
    state_path = forge_root / _STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(state_dict, indent=2, default=str)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=state_path.parent,
        delete=False,
        suffix=".tmp",
    ) as tf:
        tf.write(data)
        tmp_path = tf.name
    try:
        os.replace(tmp_path, state_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _append_crash_log(forge_root: Path, crash: dict) -> None:
    """Append a JSON line to crashes.jsonl."""
    crash_path = forge_root / _CRASH_LOG
    crash_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(crash_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(crash, default=str) + "\n")
    except OSError:
        pass


def _daemon_ntfy_notify(config: "ForgeConfig", crash: dict) -> None:
    """Send ntfy notification on daemon crash. Fails silently."""
    if config.notifications.ntfy is None:
        return
    ntfy = config.notifications.ntfy
    slug = crash.get("spec", "unknown")
    phase = crash.get("phase", "UNKNOWN")
    cost = crash.get("cost_at_crash", 0.0)
    title = f"TheForge daemon CRASHED — {slug}"
    body = f"{phase}\nCost at crash: ${cost:.2f}"
    try:
        req = urllib.request.Request(
            ntfy.url,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": ntfy.priority,
                "Content-Type": "text/plain; charset=utf-8",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── State update callback ───────────────────────────────────────────────


def make_state_update_fn(
    forge_root: Path,
    current_state: dict,
    lock: threading.Lock,
) -> Callable[[dict], None]:
    """Return a thread-safe callback that merges updates into current_state.

    Also flushes daemon.json on each call.
    """

    def _update(updates: dict) -> None:
        with lock:
            current_state.update(updates)
            try:
                # Build daemon.json snapshot from current state
                snapshot = dict(current_state)
                _write_daemon_json(forge_root, snapshot)
            except Exception:
                pass

    return _update
