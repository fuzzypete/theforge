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

#: Detail keys that describe the RUN, not the phase currently writing detail.
#:
#: A phase replaces the ``detail`` dict wholesale with what it knows, which is
#: correct for phase-scoped facts — DEV's iteration counters have no business
#: outliving DEV. But "this story's preflight produced no evidence, and every
#: value it was routed on is a conservative fallback" stays true for the rest of
#: the run. Letting DEV's write erase it is how a degraded story looked healthy
#: on the live row the moment it left PREFLIGHT (#2346). Keys listed here survive
#: a replacement; a later phase can still overwrite one deliberately by
#: including it in its own detail.
RUN_SCOPED_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        "preflight_degraded",
        "preflight_degraded_reason",
        "preflight_failure_action",
        "preflight_risk_signals",
        "complexity_source",
    }
)


class StoryOutcome(str, Enum):
    """Sprint story lifecycle outcome.

    Non-terminal: WAITING, RUNNING, BLOCKED.
    Terminal: DONE, ALREADY_DONE, FAILED, MERGE_FAILED, MERGE_ARMING_FAILED,
    ESCALATED, SKIPPED, PRESERVED, DROPPED, DECOMPOSED.

    MERGE_FAILED is the post-approval merge-step failure (dev + review succeeded
    but the integration step crashed/refused). It is distinct from FAILED (a
    generic non-success terminal state) and from DROPPED (story did not run).

    MERGE_ARMING_FAILED is the narrower case where ``gh pr merge --auto``
    failed at the auto-merge *arming* RPC (e.g. target branch lacks the
    protection rules ``enablePullRequestAutoMerge`` requires) — the PR itself
    is fine; only the arming step failed. Operator remediation differs from
    MERGE_FAILED (configure branch protection or merge manually) so it gets
    its own outcome.
    """

    WAITING = "waiting"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    ALREADY_DONE = "already_done"
    FAILED = "failed"
    MERGE_FAILED = "merge_failed"
    MERGE_ARMING_FAILED = "merge_arming_failed"
    ESCALATED = "escalated"
    SKIPPED = "skipped"
    PRESERVED = "preserved"
    DROPPED = "dropped"
    DROPPED_SHAPE = "dropped_shape"
    REMEDIATED = "remediated"
    DROPPED_AFTER_FIX = "dropped_after_fix"
    # operator-action: deliberately not run because the deliverable is human
    # action no dev agent can perform. Distinct from SKIPPED (generic skip) and
    # from FAILED-bucket outcomes — operator paid $0 and the system correctly
    # identified the issue as not its work.
    OPERATOR_ACTION = "operator_action"
    # returned-for-decomposition: the preflight complexity gate asked whether
    # the story should be planned as scoped, and the answer (an operator's, or
    # the configured no-decision action) was to split it (#2681). Terminal, and
    # deliberately outside the failed bucket: nothing about the story failed —
    # the system stopped before spending on a scope nobody had approved.
    DECOMPOSED = "decomposed"

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
        # report DROPPED as failed. DROPPED_SHAPE and DROPPED_AFTER_FIX
        # surface from the intake remediation gate; they share the same
        # operator semantics as DROPPED — the story did not run.
        return self in {
            StoryOutcome.FAILED,
            StoryOutcome.MERGE_FAILED,
            StoryOutcome.MERGE_ARMING_FAILED,
            StoryOutcome.ESCALATED,
            StoryOutcome.DROPPED,
            StoryOutcome.DROPPED_SHAPE,
            StoryOutcome.DROPPED_AFTER_FIX,
        }

    @property
    def is_skipped(self) -> bool:
        return self in {
            StoryOutcome.SKIPPED,
            StoryOutcome.PRESERVED,
            StoryOutcome.OPERATOR_ACTION,
            # Counted with the not-run stories, not the failed ones: the sprint
            # deliberately did not spend on it (#2681).
            StoryOutcome.DECOMPOSED,
        }


_TERMINAL_OUTCOMES = {
    StoryOutcome.DONE,
    StoryOutcome.ALREADY_DONE,
    StoryOutcome.FAILED,
    StoryOutcome.MERGE_FAILED,
    StoryOutcome.MERGE_ARMING_FAILED,
    StoryOutcome.ESCALATED,
    StoryOutcome.SKIPPED,
    StoryOutcome.PRESERVED,
    StoryOutcome.DROPPED,
    StoryOutcome.DROPPED_SHAPE,
    StoryOutcome.DROPPED_AFTER_FIX,
    StoryOutcome.OPERATOR_ACTION,
    StoryOutcome.DECOMPOSED,
}


# ``detail.gate_status`` values the sprint layer writes. RUNNING is set when a
# story enters VALIDATE; the rest are terminal readings recorded when the story
# stops without the gate reporting a decision.
GATE_STATUS_RUNNING = "running"
GATE_STATUS_INCOMPLETE = "incomplete"
GATE_STATUS_TIMEOUT = "timeout"
GATE_STATUS_STOPPED = "stopped"


_CANONICAL_TO_LEGACY_STATUS = {
    StoryOutcome.WAITING: "waiting",
    StoryOutcome.RUNNING: "running",
    StoryOutcome.BLOCKED: "blocked",
    StoryOutcome.DONE: "done",
    StoryOutcome.ALREADY_DONE: "done",
    StoryOutcome.FAILED: "failed",
    StoryOutcome.MERGE_FAILED: "failed",
    StoryOutcome.MERGE_ARMING_FAILED: "failed",
    StoryOutcome.ESCALATED: "failed",
    StoryOutcome.SKIPPED: "skipped",
    StoryOutcome.PRESERVED: "preserved",
    StoryOutcome.DROPPED: "failed",
    StoryOutcome.DROPPED_SHAPE: "failed",
    StoryOutcome.DROPPED_AFTER_FIX: "failed",
    StoryOutcome.REMEDIATED: "waiting",
    StoryOutcome.OPERATOR_ACTION: "operator-action",
    StoryOutcome.DECOMPOSED: "decomposed",
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
    "merge_failed": StoryOutcome.MERGE_FAILED,
    "merge_arming_failed": StoryOutcome.MERGE_ARMING_FAILED,
    "escalated": StoryOutcome.ESCALATED,
    "escalate": StoryOutcome.ESCALATED,  # phase-style alias seeded by runner
    "skipped": StoryOutcome.SKIPPED,
    "preserved": StoryOutcome.PRESERVED,
    "dropped": StoryOutcome.DROPPED,
    "dropped_shape": StoryOutcome.DROPPED_SHAPE,
    "remediated": StoryOutcome.REMEDIATED,
    "dropped_after_fix": StoryOutcome.DROPPED_AFTER_FIX,
    "operator_action": StoryOutcome.OPERATOR_ACTION,
    "operator-action": StoryOutcome.OPERATOR_ACTION,
    "decomposed": StoryOutcome.DECOMPOSED,
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


def landing_failure_outcome(merge_info: dict | None) -> StoryOutcome:
    """Classify a failed landing (landing_status == "failed") into an outcome.

    A merge-step failure is not one undifferentiated bucket. The merge_info flags
    distinguish who is responsible so the operator is pointed at the right fix:

    * ``inherited_dev_residue`` — integration refused git state a prior DEV
      iteration left behind (issue #1365). Integration is the victim, so this is
      a DEV-attributed ``ESCALATED``, never ``MERGE_FAILED``.
    * ``arming_failed`` — the PR is fine; only ``gh pr merge --auto`` arming
      failed → ``MERGE_ARMING_FAILED`` (configure branch protection).
    * otherwise → ``MERGE_FAILED`` (the generic post-approval merge failure).

    Centralised so every sprint landing site classifies identically.
    """
    m = merge_info or {}
    if m.get("inherited_dev_residue"):
        return StoryOutcome.ESCALATED
    if m.get("arming_failed"):
        return StoryOutcome.MERGE_ARMING_FAILED
    return StoryOutcome.MERGE_FAILED


@dataclass
class StoryStateEntry:
    """Per-story state. The canonical record for one story in one sprint."""

    slug: str
    path: str
    outcome: StoryOutcome = StoryOutcome.WAITING
    phase: str | None = None
    # None = at least one contributing phase's cost was unmeasured, so this
    # story's cost is unknown — never coerce it to 0.0 (#1992).
    cost_usd: float | None = 0.0
    bundle_candidate: bool = False
    # Cost-aware batch group this story was packed into (#727), or None when
    # it was dispatched on its own. Grouping metadata only: a batched story
    # keeps its own row, outcome, cost, and audit.
    batch_group: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    complexity: str | None = None
    complexity_score: int | None = None
    detail: dict = field(default_factory=dict)
    reason: str | None = None
    canonical_ref: str | None = None
    depends_on: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)
    # Every attempt's recorded failure cause, oldest first. Appended to, never
    # replaced: a retry's cause must not erase the cause of the attempt before
    # it, which is the only account of a story that ended abnormally (#2030).
    failure_history: list[dict] = field(default_factory=list)

    def record_failure_cause(self, cause: dict) -> None:
        """Retain one attempt's failure cause, keeping every earlier one.

        Idempotent per (run_id, kind, cause) so a path that records the same
        cause twice (e.g. an exception exit that also terminalizes the row) does
        not inflate the attempt count.
        """
        identity = (cause.get("run_id"), cause.get("kind"), cause.get("cause"))
        for existing in self.failure_history:
            if (existing.get("run_id"), existing.get("kind"), existing.get("cause")) == identity:
                return
        recorded = {k: v for k, v in cause.items() if k != "attempt"}
        recorded["attempt"] = len(self.failure_history) + 1
        self.failure_history.append(recorded)

    def as_dict(self) -> dict:
        # ``status`` is the legacy live-status field that ``read_live_status``
        # in status_reader.py understands (waiting/running/done/failed/
        # skipped/preserved/blocked). Canonical outcomes such as
        # ``already_done`` are mapped down to those legacy buckets so the
        # live .state file remains compatible. ``outcome`` keeps the full
        # canonical value for surfaces that want it.
        legacy_status = _CANONICAL_TO_LEGACY_STATUS.get(self.outcome, self.outcome.value)
        merged_detail = dict(self.detail)
        # Project the canonical outcome into ``final_outcome`` for terminal
        # stories. Preserve a more-specific final_outcome already in the detail
        # (e.g. ALREADY_DONE while outcome=DONE) only when its success/failure
        # bucket matches the current outcome — otherwise an in-run transition
        # (seeded DONE → ESCALATE) would leave a stale success outcome behind.
        if self.outcome.is_terminal:
            existing = merged_detail.get("final_outcome")
            existing_outcome = (
                _STATUS_TO_OUTCOME.get(existing.strip().lower())
                if isinstance(existing, str)
                else None
            )
            if not (
                existing_outcome is not None
                and existing_outcome.is_succeeded == self.outcome.is_succeeded
                and existing_outcome.is_failed == self.outcome.is_failed
                and existing_outcome.is_skipped == self.outcome.is_skipped
            ):
                merged_detail["final_outcome"] = self.outcome.name
            # review_verdict / review_p1 / review_p2 are per-run review artifacts.
            # When a terminal outcome is not a success, any prior cycle's review
            # summary must not bleed into the operator-facing detail.
            if not self.outcome.is_succeeded:
                for _stale in ("review_verdict", "review_p1", "review_p2"):
                    merged_detail.pop(_stale, None)
            # A terminal story cannot have a gate that is still running. The
            # gate_status detail is written while VALIDATE is in flight and is
            # never revisited when the story dies mid-gate (timeout, worker
            # exception, operator stop), which is how a `.state` file ends up
            # claiming `status: failed` and `gate_status: running` at once
            # (#2013). Enforced here rather than at each transition site so no
            # future exit path can serialize the contradiction.
            if merged_detail.get("gate_status") == GATE_STATUS_RUNNING:
                merged_detail["gate_status"] = GATE_STATUS_INCOMPLETE
        d: dict = {
            "slug": self.slug,
            "path": self.path,
            "status": legacy_status,
            "outcome": self.outcome.value,
            "phase": self.phase,
            "cost_usd": self.cost_usd,
            "bundle_candidate": self.bundle_candidate,
            "batch_group": self.batch_group,
            "blocked_by": list(self.blocked_by),
            "complexity": self.complexity,
            "complexity_score": self.complexity_score,
            "detail": deepcopy(merged_detail),
            "reason": self.reason,
            "canonical_ref": self.canonical_ref,
            "depends_on": list(self.depends_on),
        }
        if self.failure_history:
            d["failure_history"] = deepcopy(self.failure_history)
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
        cost_usd: float | None = 0.0,
        bundle_candidate: bool = False,
        batch_group: str | None = None,
        blocked_by: list[str] | None = None,
        complexity: str | None = None,
        complexity_score: int | None = None,
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
            if cost_usd is None:
                # Cost-unknown is a real state, not a missing value: it must not
                # be silently replaced by a previously recorded number.
                entry.cost_usd = None
            elif cost_usd:
                entry.cost_usd = cost_usd
            entry.bundle_candidate = bundle_candidate
            if batch_group is not None:
                entry.batch_group = batch_group
            if blocked_by is not None:
                entry.blocked_by = list(blocked_by)
            if complexity is not None:
                entry.complexity = complexity
            if complexity_score is not None:
                entry.complexity_score = complexity_score
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

        Landed immutability: a terminal outcome that has been marked
        ``landed=True`` (its PR was confirmed merged on the base branch) is
        immutable — no later transition may overwrite it with a non-DONE
        terminal outcome. This distinguishes a genuine "queued PR failed to
        land" correction (which is never marked landed) from a spurious
        redispatch-after-process-restart that would otherwise clobber a
        confirmed-merged DONE with FAILED.
        """
        with self._lock:
            entry = self._stories.get(slug)
            if entry is None:
                return None
            outcome_rejected = False
            if outcome is not None:
                new_outcome = coerce_outcome(outcome)
                if entry.outcome.is_terminal and not new_outcome.is_terminal:
                    # Reject: monotonicity invariant — once terminal, stay terminal.
                    new_outcome = entry.outcome
                    outcome_rejected = True
                elif (
                    entry.outcome.is_terminal
                    and entry.extras.get("landed")
                    and new_outcome is not StoryOutcome.DONE
                ):
                    # Reject: a confirmed-landed DONE cannot be overwritten by a
                    # non-DONE terminal (e.g. a bogus FAILED from a re-entry after
                    # an unrelated process restart).
                    new_outcome = entry.outcome
                    outcome_rejected = True
                entry.outcome = new_outcome
            if outcome_rejected:
                # The outcome change was rejected by a monotonicity/landed guard.
                # The accompanying fields (cost_usd, detail, etc.) describe the
                # rejected round's attempt and must not clobber the settled
                # entry's data — e.g. a round-2 failure's cost_usd=0.0 must not
                # overwrite round-1's recorded cost on a landed DONE.
                return entry
            for k, v in fields.items():
                if k == "phase" and isinstance(v, (str, type(None))):
                    entry.phase = v  # type: ignore[assignment]
                elif k == "cost_usd":
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        entry.cost_usd = float(v)
                    elif v is None:
                        entry.cost_usd = None
                elif k == "blocked_by" and isinstance(v, list):
                    entry.blocked_by = list(v)
                elif k == "batch_group":
                    entry.batch_group = v  # type: ignore[assignment]
                elif k == "complexity":
                    entry.complexity = v  # type: ignore[assignment]
                elif k == "complexity_score":
                    if isinstance(v, int):
                        entry.complexity_score = v
                    elif v is None:
                        entry.complexity_score = None
                elif k == "detail" and isinstance(v, dict):
                    # Wholesale replace, except for the run-scoped keys: a phase
                    # writing what IT knows must not silently retract a fact
                    # about the run that outlives it (see RUN_SCOPED_DETAIL_KEYS).
                    carried = {
                        key: value
                        for key, value in entry.detail.items()
                        if key in RUN_SCOPED_DETAIL_KEYS and key not in v
                    }
                    entry.detail = {**carried, **v}
                elif k == "detail_updates" and isinstance(v, dict):
                    # Merge, don't replace: a terminal exit path knows one or two
                    # detail keys (gate_status, say) and must not erase whatever
                    # the phases before it recorded.
                    merged = dict(entry.detail)
                    merged.update(v)
                    entry.detail = merged
                elif k == "failure_cause":
                    # Append, never replace: a second attempt at the same story
                    # records an additional cause, and the first attempt's cause
                    # stays readable (#2030). A caller with nothing to record
                    # passes None, which must not land in extras as a null.
                    if isinstance(v, dict) and v:
                        entry.record_failure_cause(v)
                elif k == "failure_history":
                    for cause in v if isinstance(v, list) else []:
                        if isinstance(cause, dict) and cause:
                            entry.record_failure_cause(cause)
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
                cost_usd=(
                    float(d["cost_usd"])
                    if isinstance(d.get("cost_usd"), (int, float))
                    and not isinstance(d.get("cost_usd"), bool)
                    else (0.0 if "cost_usd" not in d else None)
                ),
                bundle_candidate=bool(d.get("bundle_candidate", False)),
                batch_group=d.get("batch_group"),
                blocked_by=list(d.get("blocked_by") or []),
                complexity=d.get("complexity"),
                complexity_score=(
                    d.get("complexity_score")
                    if isinstance(d.get("complexity_score"), int)
                    else None
                ),
                detail=dict(d.get("detail") or {}),
                reason=d.get("reason"),
                canonical_ref=d.get("canonical_ref"),
                depends_on=list(d.get("depends_on") or []),
            )
            entry = state._stories[slug]
            for cause in d.get("failure_history") or []:
                if isinstance(cause, dict):
                    entry.record_failure_cause(cause)
            for k, v in d.items():
                if k in {
                    "failure_history",
                    "slug",
                    "path",
                    "outcome",
                    "status",
                    "phase",
                    "cost_usd",
                    "bundle_candidate",
                    "batch_group",
                    "blocked_by",
                    "complexity",
                    "complexity_score",
                    "detail",
                    "reason",
                    "canonical_ref",
                    "depends_on",
                }:
                    continue
                entry.extras[k] = v
        return state
