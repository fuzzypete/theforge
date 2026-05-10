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
        sprint_phase: str | None = None,
        base_branch: str | None = None,
        budget_usd: float | None = None,
        max_parallel: int | None = None,
    ) -> None:
        self._run_id = run_id
        self._sprint_name = sprint_name
        self._sprint_id = sprint_id
        self._state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
        self._lock = threading.Lock()
        # The canonical structure. Surfaces (banner, summary, notifications)
        # all project from this same instance.
        self.story_state: SprintStoryState = story_state or SprintStoryState()
        # Sprint-level metadata surfaced by forge status / forge sprint-status
        # before per-story state is rich. Lets the watcher render an informative
        # header during shape-gate / intake-remediation / preflight phases.
        self._sprint_phase = sprint_phase
        self._base_branch = base_branch
        self._budget_usd = budget_usd
        self._max_parallel = max_parallel
        # Preserve any pre-existing top-level metadata (e.g. bootstrap state
        # written before the writer was constructed) so init() does not erase
        # base_branch / budget / parallel set by cli/sprint.py at daemonize.
        self._inherit_existing_metadata()

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

    def set_phase(self, phase: str) -> None:
        """Update sprint-level phase and rewrite the state file atomically."""
        with self._lock:
            self._sprint_phase = phase
            self._write_locked()

    def _inherit_existing_metadata(self) -> None:
        """Adopt sprint_phase/base_branch/budget/parallel from an existing file.

        cli/sprint.py writes a bootstrap state file before run_sprint creates
        this writer — without inheriting those fields, the first init() would
        wipe them out and operators would lose the watch-mode header context.
        """
        if not self._state_path.exists():
            return
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            return
        if not isinstance(data, dict):
            return
        if self._sprint_phase is None and isinstance(data.get("sprint_phase"), str):
            self._sprint_phase = data["sprint_phase"]
        if self._base_branch is None and isinstance(data.get("base_branch"), str):
            self._base_branch = data["base_branch"]
        if self._budget_usd is None and isinstance(data.get("budget_usd"), (int, float)):
            self._budget_usd = float(data["budget_usd"])
        if self._max_parallel is None and isinstance(data.get("max_parallel"), int):
            self._max_parallel = data["max_parallel"]

    def _write_locked(self) -> None:
        """Write the state file atomically. Caller must hold self._lock."""
        data: dict = {
            "sprint_name": self._sprint_name,
            "sprint_id": self._sprint_id,
            "sprint_phase": self._sprint_phase,
            "base_branch": self._base_branch,
            "budget_usd": self._budget_usd,
            "max_parallel": self._max_parallel,
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


def write_bootstrap_state(
    run_id: str,
    project_root: Path,
    *,
    sprint_name: str,
    sprint_phase: str,
    base_branch: str | None = None,
    budget_usd: float | None = None,
    max_parallel: int | None = None,
    issues: list[dict] | None = None,
    skipped_issues: list | None = None,
) -> Path:
    """Write a minimal `.state` file before SprintStateWriter exists.

    The state file is the watcher's data source. Without an early write,
    `forge status --watch` polls during shape-gate / intake-remediation /
    preflight and finds nothing — operators see only the watch overlay
    headers with no per-issue rows or sprint phase.

    Each issue dict should provide ``number`` (int) and optional ``title``.
    Stories are seeded with ``status=waiting`` and no phase; the runner
    upgrades them through SprintStateWriter once preflight produces real
    per-story data.
    """
    state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    stories: list[dict] = []
    seen_slugs: set[str] = set()
    for issue in issues or []:
        number = issue.get("number")
        slug = issue.get("slug") or (f"issue-{number}" if number is not None else None)
        if not slug or slug in seen_slugs:
            continue
        path = issue.get("path") or (f"Issue #{number}" if number is not None else slug)
        canonical_ref = issue.get("canonical_ref")
        if canonical_ref is None and isinstance(number, int):
            canonical_ref = f"issue:{number}"
        stories.append(
            {
                "slug": slug,
                "path": path,
                "status": "waiting",
                "outcome": "waiting",
                "phase": None,
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": [],
                "complexity": None,
                "complexity_score": None,
                "detail": {},
                "reason": None,
                "canonical_ref": canonical_ref,
                "depends_on": [],
            }
        )
        seen_slugs.add(slug)

    # Shape-gate skips already known at daemonize time MUST surface in the
    # bootstrap state — otherwise operators watching the sprint cannot see
    # which issues were rejected by the gate until SprintStateWriter.init()
    # registers them several minutes later, after preflight. They are
    # rendered as terminal `skipped` rows with the gate reason in `reason`.
    for sk in skipped_issues or []:
        sk_dict = sk.as_dict() if hasattr(sk, "as_dict") else dict(sk)
        sk_num = sk_dict.get("issue_number")
        if sk_num is None:
            continue
        sk_slug = f"issue-{sk_num}"
        if sk_slug in seen_slugs:
            continue
        sk_codes = sk_dict.get("reason_codes") or []
        sk_reason = (
            ", ".join(sk_codes)
            if sk_codes
            else (sk_dict.get("detail") or sk_dict.get("source") or "shape-gate")
        )
        stories.append(
            {
                "slug": sk_slug,
                "path": f"Issue #{sk_num}",
                "status": "skipped",
                "outcome": "skipped",
                "phase": None,
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "blocked_by": [],
                "complexity": None,
                "complexity_score": None,
                "detail": {
                    "shape_gate_source": sk_dict.get("source"),
                    "shape_gate_codes": list(sk_codes),
                    "final_outcome": "SKIPPED",
                },
                "reason": sk_reason,
                "canonical_ref": f"issue:{sk_num}",
                "depends_on": [],
            }
        )
        seen_slugs.add(sk_slug)

    data: dict = {
        "sprint_name": sprint_name,
        "sprint_id": None,
        "sprint_phase": sprint_phase,
        "base_branch": base_branch,
        "budget_usd": budget_usd,
        "max_parallel": max_parallel,
        "stories": stories,
    }
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        tmp_path.replace(state_path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return state_path


def update_state_phase(run_id: str, project_root: Path, sprint_phase: str) -> None:
    """Update the ``sprint_phase`` field of an existing state file in place.

    No-op when the state file is absent (e.g. headless invocations without a
    bootstrap write). Called from the runner at phase boundaries that fire
    before SprintStateWriter is constructed.
    """
    state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
    if not state_path.exists():
        return
    try:
        with open(state_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(data, dict):
        return
    if data.get("sprint_phase") == sprint_phase:
        return
    data["sprint_phase"] = sprint_phase
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        tmp_path.replace(state_path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass
