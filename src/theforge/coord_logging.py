"""Structured JSON Lines logger for coordinator runs."""

from __future__ import annotations

import datetime
import json
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

    def emit(self, event: str, **fields: object) -> None:
        """Append one JSON event line to the log file. Never raises."""
        if not self._enabled:
            return
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
