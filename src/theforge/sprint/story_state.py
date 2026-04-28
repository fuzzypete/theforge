"""Canonical sprint story state — single source of truth.

Every operator-facing surface (forge status, sprint banner, sprint-summary.yaml,
sprint-end notifications) projects from a single ``SprintStoryState`` instance.
No surface maintains parallel counters or its own subset of stories. Adding a
new self-reporting surface MUST read from this structure.

Key invariants:

* Every story has exactly one entry, keyed by slug.
* Outcome transitions are monotonic toward terminal — once a story reaches a
  terminal outcome it cannot move back to a non-terminal one.
* All projection methods (``counts()``, ``as_dict()``, ``stories()``) operate
  on the same in-memory map; counts and listings cannot drift.

Stdlib-only imports (per project convention 4: pure-data types in
low-dependency modules).
"""

from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum


class StoryOutcome(str, Enum):
    """Sprint story lifecycle outcome.

    Non-terminal: WAITING, RUNNING, BLOCKED.
    Terminal: DONE, ALREADY_DONE, FAILED, ESCALATED, SKIPPED, PRESERVED, DROPPED.
    """

    WAITING = "waiting"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    ALREADY_DONE = "already_done"
    FAILED = "failed"
    ESCALATED = "escalated"
    SKIPPED = "skipped"
    PRESERVED = "preserved"
    DROPPED = "dropped"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_OUTCOMES

    @property
    def is_succeeded(self) -> bool:
        return self in {StoryOutcome.DONE, StoryOutcome.ALREADY_DONE}

    @property
    def is_failed(self) -> bool:
        # DROPPED (launch-guard collisions, etc.) historically counts as a
        # failure in sprint-audit and forge sprint-status. Keeping it in the
        # failed bucket here preserves cross-surface agreement: canonical
        # counts, summary, notifications, banner, and completed status all
        # report DROPPED as failed.
        return self in {StoryOutcome.FAILED, StoryOutcome.ESCALATED, StoryOutcome.DROPPED}

    @property
    def is_skipped(self) -> bool:
        return self in {StoryOutcome.SKIPPED, StoryOutcome.PRESERVED}


_TERMINAL_OUTCOMES = {
    StoryOutcome.DONE,
    StoryOutcome.ALREADY_DONE,
    StoryOutcome.FAILED,
    StoryOutcome.ESCALATED,
    StoryOutcome.SKIPPED,
    StoryOutcome.PRESERVED,
    StoryOutcome.DROPPED,
}


_CANONICAL_TO_LEGACY_STATUS = {
    StoryOutcome.WAITING: "waiting",
    StoryOutcome.RUNNING: "running",
    StoryOutcome.BLOCKED: "blocked",
    StoryOutcome.DONE: "done",
    StoryOutcome.ALREADY_DONE: "done",
    StoryOutcome.FAILED: "failed",
    StoryOutcome.ESCALATED: "failed",
    StoryOutcome.SKIPPED: "skipped",
    StoryOutcome.PRESERVED: "preserved",
    StoryOutcome.DROPPED: "failed",
}


# Mapping from legacy live-status string ("running", "done", "failed",
# "skipped", "blocked", "preserved", "waiting") to canonical outcome.
_STATUS_TO_OUTCOME: dict[str, StoryOutcome] = {
    "waiting": StoryOutcome.WAITING,
    "running": StoryOutcome.RUNNING,
    "blocked": StoryOutcome.BLOCKED,
    "done": StoryOutcome.DONE,
    "already_done": StoryOutcome.ALREADY_DONE,
    "failed": StoryOutcome.FAILED,
    "escalated": StoryOutcome.ESCALATED,
    "skipped": StoryOutcome.SKIPPED,
    "preserved": StoryOutcome.PRESERVED,
    "dropped": StoryOutcome.DROPPED,
}


def coerce_outcome(value: object) -> StoryOutcome:
    """Coerce a string/enum/None to a StoryOutcome (defaults to WAITING)."""
    if isinstance(value, StoryOutcome):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _STATUS_TO_OUTCOME:
            return _STATUS_TO_OUTCOME[s]
    return StoryOutcome.WAITING


@dataclass
class StoryStateEntry:
    """Per-story state. The canonical record for one story in one sprint."""

    slug: str
    path: str
    outcome: StoryOutcome = StoryOutcome.WAITING
    phase: str | None = None
    cost_usd: float = 0.0
    bundle_candidate: bool = False
    blocked_by: list[str] = field(default_factory=list)
    complexity: str | None = None
    detail: dict = field(default_factory=dict)
    reason: str | None = None
    canonical_ref: str | None = None
    depends_on: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        # ``status`` is the legacy live-status field that ``read_live_status``
        # in status_reader.py understands (waiting/running/done/failed/
        # skipped/preserved/blocked). Canonical outcomes such as
        # ``already_done`` are mapped down to those legacy buckets so the
        # live .state file remains compatible. ``outcome`` keeps the full
        # canonical value for surfaces that want it.
        legacy_status = _CANONICAL_TO_LEGACY_STATUS.get(self.outcome, self.outcome.value)
        merged_detail = dict(self.detail)
        # Surface ALREADY_DONE (and other canonical-only outcomes) as
        # ``final_outcome`` in the detail so the live status renders the
        # full canonical outcome the operator needs.
        if self.outcome.is_terminal and "final_outcome" not in merged_detail:
            merged_detail["final_outcome"] = self.outcome.name
        d: dict = {
            "slug": self.slug,
            "path": self.path,
            "status": legacy_status,
            "outcome": self.outcome.value,
            "phase": self.phase,
            "cost_usd": self.cost_usd,
            "bundle_candidate": self.bundle_candidate,
            "blocked_by": list(self.blocked_by),
            "complexity": self.complexity,
            "detail": deepcopy(merged_detail),
            "reason": self.reason,
            "canonical_ref": self.canonical_ref,
            "depends_on": list(self.depends_on),
        }
        if self.extras:
            for k, v in self.extras.items():
                if k not in d:
                    d[k] = deepcopy(v)
        return d


class SprintStoryState:
    """Thread-safe canonical container for all stories in a sprint.

    The runner constructs one ``SprintStoryState`` per sprint. Every other
    surface (state writer, banner, summary, notifications) reads counts and
    entries from this instance — no surface keeps its own counters.
    """

    def __init__(self) -> None:
        self._stories: dict[str, StoryStateEntry] = {}
        self._lock = threading.Lock()

    # ── registration / transition ──

    def register(
        self,
        slug: str,
        path: str,
        *,
        outcome: StoryOutcome | str = StoryOutcome.WAITING,
        phase: str | None = None,
        cost_usd: float = 0.0,
        bundle_candidate: bool = False,
        blocked_by: list[str] | None = None,
        complexity: str | None = None,
        detail: dict | None = None,
        reason: str | None = None,
        canonical_ref: str | None = None,
        depends_on: list[str] | None = None,
    ) -> StoryStateEntry:
        """Register a new story. Idempotent: re-registering the same slug
        updates fields without duplicating the entry."""
        with self._lock:
            entry = self._stories.get(slug)
            if entry is None:
                entry = StoryStateEntry(slug=slug, path=path, outcome=coerce_outcome(outcome))
                self._stories[slug] = entry
            else:
                # Idempotent re-registration — only allow outcome to advance
                # toward terminal (preserves monotonicity invariant).
                new_outcome = coerce_outcome(outcome)
                if not entry.outcome.is_terminal or new_outcome == entry.outcome:
                    entry.outcome = new_outcome
            entry.path = path
            if phase is not None:
                entry.phase = phase
            entry.cost_usd = cost_usd if cost_usd else entry.cost_usd
            entry.bundle_candidate = bundle_candidate
            if blocked_by is not None:
                entry.blocked_by = list(blocked_by)
            if complexity is not None:
                entry.complexity = complexity
            if detail is not None:
                entry.detail = dict(detail)
            if reason is not None:
                entry.reason = reason
            if canonical_ref is not None:
                entry.canonical_ref = canonical_ref
            if depends_on is not None:
                entry.depends_on = list(depends_on)
            return entry

    def transition(
        self,
        slug: str,
        outcome: StoryOutcome | str | None = None,
        **fields: object,
    ) -> StoryStateEntry | None:
        """Transition a story's outcome and/or update mutable fields.

        Monotonicity: once a story is at a terminal outcome, further
        ``transition`` calls cannot move it back to a non-terminal outcome.
        Terminal-to-terminal corrections (e.g. optimistic DONE -> FAILED
        when a queued PR fails to land) are permitted because the canonical
        structure must reflect the final reality, but the story never leaves
        the terminal set.
        """
        with self._lock:
            entry = self._stories.get(slug)
            if entry is None:
                return None
            if outcome is not None:
                new_outcome = coerce_outcome(outcome)
                if entry.outcome.is_terminal and not new_outcome.is_terminal:
                    # Reject: monotonicity invariant — once terminal, stay terminal.
                    new_outcome = entry.outcome
                entry.outcome = new_outcome
            for k, v in fields.items():
                if k == "phase" and isinstance(v, (str, type(None))):
                    entry.phase = v  # type: ignore[assignment]
                elif k == "cost_usd" and isinstance(v, (int, float)):
                    entry.cost_usd = float(v)
                elif k == "blocked_by" and isinstance(v, list):
                    entry.blocked_by = list(v)
                elif k == "complexity":
                    entry.complexity = v  # type: ignore[assignment]
                elif k == "detail" and isinstance(v, dict):
                    entry.detail = dict(v)
                elif k == "reason":
                    entry.reason = v  # type: ignore[assignment]
                elif k == "depends_on" and isinstance(v, list):
                    entry.depends_on = list(v)
                else:
                    entry.extras[k] = v
            return entry

    # ── queries / projections ──

    def has(self, slug: str) -> bool:
        with self._lock:
            return slug in self._stories

    def get(self, slug: str) -> StoryStateEntry | None:
        with self._lock:
            return self._stories.get(slug)

    def stories(self) -> list[StoryStateEntry]:
        with self._lock:
            return list(self._stories.values())

    def counts(self) -> dict[str, int]:
        """Return canonical counts. Banner, summary, and notifications all
        project from this method — by construction they cannot disagree."""
        with self._lock:
            total = len(self._stories)
            succeeded = 0
            failed = 0
            skipped = 0
            for entry in self._stories.values():
                if entry.outcome.is_succeeded:
                    succeeded += 1
                elif entry.outcome.is_failed:
                    failed += 1
                elif entry.outcome.is_skipped:
                    skipped += 1
            return {
                "total": total,
                "succeeded": succeeded,
                "failed": failed,
                "skipped": skipped,
            }

    def as_dict(self) -> list[dict]:
        """Serialize to a list of plain dicts (for YAML persistence)."""
        with self._lock:
            return [e.as_dict() for e in self._stories.values()]

    @classmethod
    def from_dict(cls, raw: list[dict] | None) -> SprintStoryState:
        """Reconstruct from a previously-serialized list of dicts."""
        state = cls()
        for d in raw or []:
            slug = d.get("slug")
            if not slug:
                continue
            state.register(
                slug,
                d.get("path", slug),
                outcome=d.get("outcome") or d.get("status") or "waiting",
                phase=d.get("phase"),
                cost_usd=float(d.get("cost_usd", 0.0)),
                bundle_candidate=bool(d.get("bundle_candidate", False)),
                blocked_by=list(d.get("blocked_by") or []),
                complexity=d.get("complexity"),
                detail=dict(d.get("detail") or {}),
                reason=d.get("reason"),
                canonical_ref=d.get("canonical_ref"),
                depends_on=list(d.get("depends_on") or []),
            )
            entry = state._stories[slug]
            for k, v in d.items():
                if k in {
                    "slug",
                    "path",
                    "outcome",
                    "status",
                    "phase",
                    "cost_usd",
                    "bundle_candidate",
                    "blocked_by",
                    "complexity",
                    "detail",
                    "reason",
                    "canonical_ref",
                    "depends_on",
                }:
                    continue
                entry.extras[k] = v
        return state
