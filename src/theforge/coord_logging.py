"""Structured JSON Lines logger for coordinator runs."""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path


class StructuredLogger:
    """Append-only JSON Lines logger for persistent run events.

    Writes one JSON object per line to <project_root>/.forge/logs/forge.log
    (or a configured path). All writes are best-effort — failures are
    silently swallowed and never crash the run.
    """

    def __init__(
        self,
        run_id: str,
        project: str,
        task: str,
        log_file: str,
        enabled: bool,
        project_root: "Path | None" = None,
    ) -> None:
        self._run_id = run_id
        self._project = project
        self._task = task
        self._enabled = enabled
        if enabled:
            resolved = log_file.replace("{project}", project)
            raw_path = Path(resolved)
            if raw_path.is_absolute() or resolved.startswith("~"):
                # Absolute path or home-relative (legacy config): use as-is
                self._log_path = raw_path.expanduser()
            elif project_root is not None:
                # Relative path: resolve against project root
                self._log_path = project_root / raw_path
            else:
                # No project_root provided: fall back to expanduser
                self._log_path = raw_path.expanduser()
            # Best-effort backward-compat: if the old home-dir path exists and the
            # new project-local path does not, create a symlink from old → new.
            _old_path = Path(f"~/.forge/logs/{project}/forge.log").expanduser()
            if (
                project_root is not None
                and _old_path.exists()
                and not self._log_path.exists()
                and self._log_path != _old_path
            ):
                try:
                    self._log_path.parent.mkdir(parents=True, exist_ok=True)
                    import shutil

                    shutil.move(str(_old_path), str(self._log_path))
                    _old_path.parent.mkdir(parents=True, exist_ok=True)
                    _old_path.symlink_to(self._log_path)
                except Exception:
                    pass  # best-effort; never crash
        else:
            self._log_path = Path("/dev/null")
        self._last_event: str = ""
        # Protects _last_event writes in emit() when called from multiple threads.
        # The last_event property does NOT acquire this lock because it may be read
        # from a signal handler, and acquiring a lock inside a signal handler can
        # deadlock if the handler fires while another thread already holds it.
        # CPython's GIL guarantees that the string read in last_event is atomic.
        self._last_event_lock: threading.Lock = threading.Lock()

    @property
    def last_event(self) -> str:
        """The most recently emitted event name, or '' if none yet.

        Safe to call from a signal handler: reads without acquiring the lock
        (CPython's GIL makes the string read atomic; acquiring a lock from a
        signal handler risks deadlock).
        """
        return self._last_event

    def emit(self, event: str, **fields: object) -> None:
        """Append one JSON event line to the log file. Never raises."""
        if not self._enabled:
            return
        with self._last_event_lock:
            self._last_event = event
        try:
            entry = {
                "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "project": self._project,
                "run_id": self._run_id,
                "task": self._task,
                "event": event,
                **fields,
            }
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _safe_emit(self, event: str, **fields: object) -> None:
        """Call emit(), silently swallowing any exception (including mocked errors)."""
        try:
            self.emit(event, **fields)
        except Exception:
            pass
