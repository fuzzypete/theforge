"""Sprint state file writer — live per-story status for forge sprint-status."""

from __future__ import annotations

import threading
from pathlib import Path

import yaml


class SprintStateWriter:
    """Thread-safe writer for the sprint state file at .forge/runs/<run-id>.state.

    The state file tracks per-story status for live sprint monitoring.  It is
    created when a sprint starts (after preflight) and removed when the sprint
    completes normally (sprint-summary.yaml takes over for completed sprints).

    If the sprint process dies unexpectedly, the state file persists and
    ``forge sprint-status`` will show a "sprint ended unexpectedly" banner
    when no matching PID file exists.

    File format (YAML)::

        sprint_name: my-sprint
        stories:
          - slug: issue-123
            path: "Issue #123"
            status: running     # waiting | running | done | failed | skipped | blocked
            phase: DEV          # last known coordinator phase, or null
            cost_usd: 0.0
            bundle_candidate: false
            blocked_by: []      # slugs this story is waiting on
    """

    def __init__(self, run_id: str, project_root: Path, sprint_name: str) -> None:
        self._run_id = run_id
        self._sprint_name = sprint_name
        self._state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
        self._lock = threading.Lock()
        self._stories: dict[str, dict] = {}

    def init(self, stories: list[dict]) -> None:
        """Initialize with all stories and write the initial state file.

        Call once after preflight completes and the DAG is built.
        Each story dict must have: slug, path, status, phase, cost_usd,
        bundle_candidate, blocked_by.
        """
        with self._lock:
            for story in stories:
                self._stories[story["slug"]] = dict(story)
            self._write_locked()

    def update(self, slug: str, **kwargs: object) -> None:
        """Update fields on a story and rewrite the state file atomically."""
        with self._lock:
            if slug in self._stories:
                self._stories[slug].update(kwargs)
                self._write_locked()

    def remove(self) -> None:
        """Remove the state file.  Called when the sprint completes normally."""
        try:
            self._state_path.unlink()
        except FileNotFoundError:
            pass

    def _write_locked(self) -> None:
        """Write the state file atomically.  Caller must hold self._lock."""
        data: dict = {
            "sprint_name": self._sprint_name,
            "stories": list(self._stories.values()),
        }
        tmp_path = self._state_path.with_name(self._state_path.name + ".tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            tmp_path.replace(self._state_path)
        except Exception:
            try:
                tmp_path.unlink()
            except Exception:
                pass
