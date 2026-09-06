"""Sprint state file writer — live per-story status for forge sprint-status.

Persists ``SprintStoryState`` (the canonical structure) to disk. The on-disk
format is a projection of ``SprintStoryState.as_dict()`` — there is no parallel
representation of story state owned by this module.
"""

from __future__ import annotations

import datetime
import threading
from pathlib import Path

import yaml

from .story_state import (
    GATE_STATUS_INCOMPLETE,
    GATE_STATUS_STOPPED,
    SprintStoryState,
    StoryOutcome,
)

# Terminal values for the state file's top-level ``sprint_phase``. Any other
# value means the sprint is still advancing; these three mean it is not, no
# matter what the owning process is doing (#2013).
SPRINT_PHASE_DONE = "done"
SPRINT_PHASE_FAILED = "failed"
SPRINT_PHASE_STOPPED = "stopped"
TERMINAL_SPRINT_PHASES = frozenset({SPRINT_PHASE_DONE, SPRINT_PHASE_FAILED, SPRINT_PHASE_STOPPED})


def is_terminal_sprint_phase(phase: object) -> bool:
    """True when ``phase`` is one of the terminal ``sprint_phase`` values."""
    return isinstance(phase, str) and phase.strip().lower() in TERMINAL_SPRINT_PHASES


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
        # How this run stands against its cap, and by how much it passed it.
        # Written by the runner's budget checkpoint so a live sprint's status
        # states the relationship between cost and budget rather than leaving an
        # operator to compare two independent numbers (#2547).
        self._budget_status: str | None = None
        self._budget_overrun_usd: float = 0.0
        self._budget_spend_usd: float | None = None
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
                    cost_usd=_story_cost(story),
                    bundle_candidate=bool(story.get("bundle_candidate", False)),
                    batch_group=story.get("batch_group"),
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

    def set_budget_status(
        self,
        status: str,
        *,
        overrun_usd: float = 0.0,
        spend_usd: float | None = None,
    ) -> None:
        """Record the run's standing against its cap and rewrite the state file.

        ``spend_usd`` is kept as a **high-water mark**: money this run has
        already spent stays recorded whatever happens to the process afterwards.
        A later publication reporting less spend is not a refund — it is a
        generation that lost part of its own accounting — so the recorded figure
        never decreases and is never cleared back to ``None`` (#2922). A rising
        spend also defeats the unchanged-status early return below; otherwise the
        recorded figure would lag reality for the whole time a run sits
        comfortably ``within`` its cap.
        """
        with self._lock:
            incoming = None if spend_usd is None else float(spend_usd)
            raised = incoming is not None and (
                self._budget_spend_usd is None or incoming > self._budget_spend_usd
            )
            unchanged = (
                self._budget_status == status
                and round(self._budget_overrun_usd, 4) == round(float(overrun_usd), 4)
                and not raised
            )
            if unchanged:
                return
            self._budget_status = status
            self._budget_overrun_usd = float(overrun_usd)
            if raised:
                self._budget_spend_usd = incoming
            self._write_locked()

    def recorded_spend_usd(self) -> float | None:
        """The highest spend this run has recorded, or ``None`` if it recorded none."""
        with self._lock:
            return self._budget_spend_usd

    def set_phase(self, phase: str) -> None:
        """Update sprint-level phase and rewrite the state file atomically."""
        with self._lock:
            self._sprint_phase = phase
            self._write_locked()

    def terminalize_stories(
        self,
        *,
        outcome: StoryOutcome | str = StoryOutcome.FAILED,
        phase: str | None = None,
        reason: str | None = None,
        gate_status: str = GATE_STATUS_INCOMPLETE,
    ) -> list[str]:
        """Move every still-nonterminal story to a terminal outcome; return their slugs.

        The sprint is over by the time this runs, so a story left at
        waiting/running/blocked is stranded, not live. Writing it terminal is
        what keeps the on-disk state from claiming work is in flight after the
        owning process has finished with it (#2013).
        """
        stranded: list[str] = []
        with self._lock:
            for entry in self.story_state.stories():
                if entry.outcome.is_terminal:
                    continue
                fields: dict = {
                    "detail_updates": {"gate_status": gate_status},
                    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
                if phase is not None:
                    fields["phase"] = phase
                if reason is not None:
                    fields["reason"] = reason
                self.story_state.transition(entry.slug, outcome=outcome, **fields)
                stranded.append(entry.slug)
            if stranded:
                self._write_locked()
        return stranded

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
        if self._budget_status is None and isinstance(data.get("budget_status"), str):
            self._budget_status = data["budget_status"]
            if isinstance(data.get("budget_overrun_usd"), (int, float)):
                self._budget_overrun_usd = float(data["budget_overrun_usd"])
            if isinstance(data.get("budget_spend_usd"), (int, float)):
                self._budget_spend_usd = float(data["budget_spend_usd"])

    def _write_locked(self) -> None:
        """Write the state file atomically. Caller must hold self._lock."""
        data: dict = {
            "sprint_name": self._sprint_name,
            "sprint_id": self._sprint_id,
            "sprint_phase": self._sprint_phase,
            "base_branch": self._base_branch,
            "budget_usd": self._budget_usd,
            "budget_status": self._budget_status,
            "budget_overrun_usd": round(self._budget_overrun_usd, 4),
            "budget_spend_usd": (
                None if self._budget_spend_usd is None else round(self._budget_spend_usd, 4)
            ),
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


def _story_cost(story: dict) -> float | None:
    """Read a story dict's ``cost_usd``, preserving an unmeasured ``None``.

    ``None`` means the transport could not measure that story's spend. Coercing
    it to 0.0 here would report unpriced work as free on every surface that
    reads the live state file (#1992).
    """
    raw = story.get("cost_usd", 0.0)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None if raw is None else 0.0


def _load_existing_state(state_path: Path) -> dict | None:
    """Read an existing state file when possible."""
    if not state_path.exists():
        return None
    try:
        with open(state_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _has_accumulated_live_state(data: dict | None) -> bool:
    """Return True once the bootstrap file has been superseded by live state.

    Bootstrap writes are only allowed while the on-disk file is still the seed
    view: unresolved sprint id and story rows that have not advanced beyond the
    initial waiting/skipped placeholders. Once the live writer has resolved the
    sprint id or moved any story forward, a later bootstrap invocation must stay
    inert so watch-mode data cannot regress.
    """
    if not isinstance(data, dict):
        return False
    if data.get("sprint_id") is not None:
        return True

    stories = data.get("stories")
    if not isinstance(stories, list):
        return False

    seed_outcomes = {"waiting", "skipped"}
    for story in stories:
        if not isinstance(story, dict):
            continue
        outcome = story.get("outcome", story.get("status"))
        if outcome not in seed_outcomes:
            return True
        _cost = story.get("cost_usd")
        if _cost is None and "cost_usd" in story:
            # Cost-unknown is recorded spend the seed file never has.
            return True
        if isinstance(_cost, (int, float)) and float(_cost) > 0.0:
            return True
    return False


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

    existing_state = _load_existing_state(state_path)
    if _has_accumulated_live_state(existing_state):
        return state_path

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
                "batch_group": None,
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
    from .shape_gate import skipped_issue_state_fields  # noqa: PLC0415

    for sk in skipped_issues or []:
        sk_dict = sk.as_dict() if hasattr(sk, "as_dict") else dict(sk)
        sk_num = sk_dict.get("issue_number")
        if sk_num is None:
            continue
        sk_slug = f"issue-{sk_num}"
        if sk_slug in seen_slugs:
            continue
        sk_reason, sk_detail = skipped_issue_state_fields(sk)
        stories.append(
            {
                "slug": sk_slug,
                "path": f"Issue #{sk_num}",
                "status": "skipped",
                "outcome": "skipped",
                "phase": None,
                "cost_usd": 0.0,
                "bundle_candidate": False,
                "batch_group": None,
                "blocked_by": [],
                "complexity": None,
                "complexity_score": None,
                "detail": sk_detail,
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
    _write_state_data(state_path, data)
    return state_path


def terminalize_state_file(
    run_id: str,
    project_root: Path,
    *,
    sprint_phase: str = SPRINT_PHASE_STOPPED,
    outcome: StoryOutcome | str = StoryOutcome.FAILED,
    phase: str | None = "STOPPED",
    reason: str | None = "stopped",
    gate_status: str = GATE_STATUS_STOPPED,
) -> list[str]:
    """Rewrite a run's live ``.state`` so the sprint and every story read terminal.

    Used by ``forge stop`` once the sprint process is gone: the process died
    holding whatever phase it was in, so nothing else will ever write the
    terminal transition, and the file is left claiming a running sprint with
    running stories (#2013). Already-terminal stories keep their recorded
    outcome and detail; only stranded ones are moved.

    Returns the slugs that were moved. No-op (empty list) when the state file is
    absent or unreadable — a stop must never fail over a missing state file.
    """
    state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
    data = _load_existing_state(state_path)
    if data is None:
        return []

    story_state = SprintStoryState.from_dict(data.get("stories") or [])
    stranded: list[str] = []
    for entry in story_state.stories():
        if entry.outcome.is_terminal:
            continue
        fields: dict = {
            "detail_updates": {"gate_status": gate_status},
            "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if phase is not None:
            fields["phase"] = phase
        if reason is not None:
            fields["reason"] = reason
        story_state.transition(entry.slug, outcome=outcome, **fields)
        stranded.append(entry.slug)

    data["sprint_phase"] = sprint_phase
    data["stories"] = story_state.as_dict()
    _write_state_data(state_path, data)
    return stranded


def _write_state_data(state_path: Path, data: dict) -> None:
    """Atomically replace a state file's contents. Best-effort; never raises."""
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


def update_state_phase(
    run_id: str,
    project_root: Path,
    sprint_phase: str,
    *,
    detail: str | None = None,
    started_at: str | None = None,
) -> None:
    """Update the ``sprint_phase`` field of an existing state file in place.

    No-op when the state file is absent (e.g. headless invocations without a
    bootstrap write). Called from the runner at phase boundaries that fire
    before SprintStateWriter is constructed.

    ``detail`` and ``started_at`` describe a phase the sprint is *inside* right
    now — the target of a long pre-story gate and when it began — so the status
    header can report elapsed time instead of an unchanging phase name (#2014).
    They are always rewritten, including to ``None``: leaving a finished gate's
    detail behind would report stale work as active.
    """
    state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
    if not state_path.exists():
        return
    data = _load_existing_state(state_path)
    if data is None:
        return
    if (
        data.get("sprint_phase") == sprint_phase
        and data.get("sprint_phase_detail") == detail
        and data.get("sprint_phase_started_at") == started_at
    ):
        return
    data["sprint_phase"] = sprint_phase
    data["sprint_phase_detail"] = detail
    data["sprint_phase_started_at"] = started_at
    _write_state_data(state_path, data)


def update_state_story(run_id: str, project_root: Path, slug: str, **fields: object) -> None:
    """Update one story row of an existing state file in place.

    The pre-story triage loop runs before ``SprintStateWriter`` exists, so the
    only live record of its progress is the bootstrap state file written at
    daemonize time. Without this, a story being gated for reuse reads
    ``waiting`` / no phase for the entire gate (#2014).

    ``detail_updates`` merges into the row's ``detail`` dict; every other field
    is assigned. Values of ``None`` are written as ``None`` so a completed phase
    can be cleared. No-op when the file, or a row for ``slug``, is absent — a
    progress write must never fail the sprint.
    """
    state_path = project_root / ".forge" / "runs" / f"{run_id}.state"
    if not state_path.exists():
        return
    data = _load_existing_state(state_path)
    if data is None:
        return
    stories = data.get("stories")
    if not isinstance(stories, list):
        return
    detail_updates = fields.pop("detail_updates", None)
    for story in stories:
        if not isinstance(story, dict) or story.get("slug") != slug:
            continue
        if "status" in fields:
            # The live file carries both the legacy status and the canonical
            # outcome; a reader picking either one must see the same thing.
            fields.setdefault("outcome", fields["status"])
        story.update(fields)
        if isinstance(detail_updates, dict):
            existing_detail = story.get("detail")
            merged = dict(existing_detail) if isinstance(existing_detail, dict) else {}
            for key, value in detail_updates.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            story["detail"] = merged
        _write_state_data(state_path, data)
        return
