"""Sprint state file writer — live per-story status for forge sprint-status.

Persists ``SprintStoryState`` (the canonical structure) to disk. The on-disk
format is a projection of ``SprintStoryState.as_dict()`` — there is no parallel
representation of story state owned by this module.
"""

from __future__ import annotations

import threading
from pathlib import Path

import yaml

from .story_state import SprintStoryState, StoryOutcome


class SprintStateWriter:
    """Thread-safe writer for the sprint state file at .forge/runs/<run-id>.state.

    The state file tracks per-story status for live sprint monitoring. It is
    created when a sprint starts and removed when the sprint completes normally
    (sprint-summary.yaml takes over for completed sprints).

    If the sprint process dies unexpectedly, the state file persists and
    ``forge sprint-status`` will show a "sprint ended unexpectedly" banner
    when no matching PID file exists.

    The writer's internal representation is a ``SprintStoryState`` instance —
    there is no parallel dict of stories. ``init`` registers; ``update``
    transitions. The on-disk file is the projection of that one structure.
    """

    def __init__(
        self,
        run_id: str,
        project_root: Path,
        sprint_name: str,
        *,
        sprint_id: str | None = None,
        story_state: SprintStoryState | None = None,
    ) -> None:
        self._run_id = run_id
        self._sprint_name = sprint_name
        self._sprint_id = sprint_id
        self._state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
        self._lock = threading.Lock()
        # The canonical structure. Surfaces (banner, summary, notifications)
        # all project from this same instance.
        self.story_state: SprintStoryState = story_state or SprintStoryState()

    def init(self, stories: list[dict]) -> None:
        """Register all stories in the canonical structure and write state file.

        Each ``story`` dict provides initial fields; the ``status`` value (or
        ``outcome``) maps to a ``StoryOutcome``. Subsequent updates flow through
        ``update`` (which calls ``SprintStoryState.transition``).
        """
        with self._lock:
            for story in stories:
                slug = story.get("slug")
                if not slug:
                    continue
                self.story_state.register(
                    slug,
                    story.get("path", slug),
                    outcome=story.get("status") or story.get("outcome") or "waiting",
                    phase=story.get("phase"),
                    cost_usd=float(story.get("cost_usd", 0.0) or 0.0),
                    bundle_candidate=bool(story.get("bundle_candidate", False)),
                    blocked_by=list(story.get("blocked_by") or []),
                    complexity=story.get("complexity"),
                    detail=dict(story.get("detail") or {}),
                    reason=story.get("reason"),
                    canonical_ref=story.get("canonical_ref"),
                    depends_on=list(story.get("depends_on") or []),
                )
            self._write_locked()

    def register(
        self,
        slug: str,
        path: str,
        *,
        outcome: StoryOutcome | str = StoryOutcome.WAITING,
        **kwargs: object,
    ) -> None:
        """Register an additional story after init (e.g., late-arriving entries)."""
        with self._lock:
            self.story_state.register(slug, path, outcome=outcome, **kwargs)  # type: ignore[arg-type]
            self._write_locked()

    def update(self, slug: str, **kwargs: object) -> None:
        """Update fields on a story and rewrite the state file atomically.

        ``status`` (legacy) or ``outcome`` (canonical) advances the story's
        ``StoryOutcome``. Monotonicity is enforced by the canonical structure.
        """
        with self._lock:
            outcome = kwargs.pop("status", None) or kwargs.pop("outcome", None)
            if not self.story_state.has(slug) and outcome is not None:
                # Late registration when the slug was not in init() — keep all
                # stories visible to surfaces by registering before transitioning.
                self.story_state.register(
                    slug,
                    str(kwargs.get("path", slug)),
                    outcome=outcome,
                )
            self.story_state.transition(slug, outcome=outcome, **kwargs)
            self._write_locked()

    def remove(self) -> None:
        """Remove the state file. Called when the sprint completes normally."""
        try:
            self._state_path.unlink()
        except FileNotFoundError:
            pass

    def _write_locked(self) -> None:
        """Write the state file atomically. Caller must hold self._lock."""
        data: dict = {
            "sprint_name": self._sprint_name,
            "sprint_id": self._sprint_id,
            "stories": self.story_state.as_dict(),
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
