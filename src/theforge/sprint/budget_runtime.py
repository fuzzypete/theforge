"""Runtime budget enforcement for a sprint (#2621).

A sprint's cap is two halves. The *decision* half — given a total, some
unmeasured spend, and a ceiling, may this sprint keep spending — already lives
in :mod:`theforge.sprint.budget`, and the representation and acceptance of
unmeasured spend lives in :mod:`theforge.sprint.unmeasured`. Both are pure and
testable without a live sprint.

This module owns the other half: the machinery that feeds those two their
numbers and acts on the answer. That is one responsibility with a boundary it
holds end to end — the ledger that accumulates measured spend
(:class:`SprintCostLedger`), the restoration and disclosure of carried spend at
startup, the two enforcement moments (before a story is dispatched and while one
is running), publication of the live budget status ``forge status`` reads, and
the cancellation of in-flight work when the cap is met, plan gates included.

It moved here out of ``sprint/runner.py`` under ADR-0008, following the shape
#2402 established for :mod:`theforge.sprint.audit_publish`. The point of the
move is independent changeability, so the one rule this module keeps is that
**it does not import the runner**: a change to how a sprint enforces its cap
claims this file, and a change to how it schedules a story claims the runner.
A dependency back on the runner would give the two a shared claim again and make
the move a relocation.

That rule is why :class:`SprintBudgetRuntime` annotates the sprint's execution
state as ``Any`` rather than importing
:class:`~theforge.sprint.runner.SprintExecutionState` for the annotation alone.
It takes the state object itself — never a list of the members it carries, which
is the parameter threading #2399 ended — and reads ``state.context`` for the
frozen run context.

Two things the runtime needs are genuinely the runner's, not budget's: recording
a story's canonical outcome and writing this generation's accumulated story
entry. They are injected as callables through :meth:`bind_story_hooks` rather
than imported, for the same reason.
"""

from __future__ import annotations

import datetime
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from ..coordinator.cancellation import BUDGET_CANCEL_ERROR_TYPE
from ..log_util import _log_line
from . import unmeasured as unmeasured_spend_policy
from .audit import _load_accepted_unmeasured_spend, persist_accepted_unmeasured_spend
from .budget import (
    BudgetBlock,
    budget_overrun_usd,
    budget_status,
    budget_verification_spend,
    evaluate_budget,
)
from .carry import load_sprint_carry_budget_snapshot, prior_unmeasured_spend_sources
from .story_state import StoryOutcome
from .unmeasured import AcceptedUnmeasuredSpend, UnmeasuredSource

# Which run an unmeasured source's per-story audit should be read as of. A story
# has ONE per-story audit and running it again overwrites that file, so the
# carried occurrence and a new one produced here are two different records read
# from the same path at two different times.
_OCCURRENCE_CARRIED = "carried"
_OCCURRENCE_CURRENT = "current"


def _log(msg: str) -> None:
    # Worker-slug prefixing (parallel attribution) is applied centrally by
    # ``_log_line``; do not prepend it here or it would double-tag. The prefix
    # stays "[sprint]" after the move: this is still the sprint speaking, and an
    # operator reading a run log should not have to learn a new tag because a
    # function changed files.
    _log_line("[sprint]", msg)


def optional_cost(raw: object) -> float | None:
    """Coerce a persisted ``cost_usd`` to float, preserving an unmeasured ``None``.

    ``None`` records "the transport could not measure this spend", which is a
    different fact from ``0.0``. Numeric arithmetic (budget checks, carry-forward
    sums) may treat unknown as a zero-valued lower bound, but anything that
    *reports* a story's cost must keep the distinction (#1992).
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None


@dataclass(frozen=True)
class SprintCostObservation:
    """A story's latest known spend and whether it was fully measured.

    ``amount`` is always a lower bound on what the story spent so far. When
    ``measured`` is False, that lower bound came from a coordinator aggregate
    whose true total is unknown because some contributing phase reported no
    cost. The runner still charges the lower bound to the sprint cap, but it
    must never certify the sprint total as complete afterwards (#1992, #2547).
    """

    amount: float
    measured: bool = True


def checkpoint_cost(updates: Mapping[str, object]) -> SprintCostObservation | None:
    """Best measured lower bound available in a live state update.

    ``cost_usd`` is the preferred signal: it is the fully measured running
    total when the transport reported one. When a phase went unmeasured, the
    coordinator can still carry a lower bound on ``coordinator_state.total_cost``
    (or, for older callers, ``detail.cost_measured_lower_bound_usd``). Returning
    ``None`` means "no new lower bound arrived"; callers may still re-check the
    budget against the last provisional spend they already hold.
    """
    _reported = optional_cost(updates.get("cost_usd"))
    if _reported is not None:
        return SprintCostObservation(amount=_reported, measured=True)

    _detail = updates.get("detail")
    if isinstance(_detail, Mapping):
        _lower_bound = optional_cost(_detail.get("cost_measured_lower_bound_usd"))
        if _lower_bound is not None:
            return SprintCostObservation(amount=_lower_bound, measured=False)

    _coordinator_state = updates.get("coordinator_state")
    if _coordinator_state is None:
        return None
    _measured = optional_cost(getattr(_coordinator_state, "total_cost_measured", None))
    if _measured is not None:
        return SprintCostObservation(amount=_measured, measured=True)
    _lower_bound = optional_cost(getattr(_coordinator_state, "total_cost", None))
    if _lower_bound is None:
        return None
    return SprintCostObservation(amount=_lower_bound, measured=False)


@dataclass(frozen=True)
class SprintCostSnapshot:
    """One consistent read of the sprint ledger, taken under its lock.

    Every consumer that needs more than one of these figures at once needs them
    to agree — a budget check that reads the total, then re-reads the unmeasured
    list after a worker has landed, is evaluating a cap against two different
    moments. Taking the whole read in one go is what makes that impossible.
    """

    accumulated: float
    prior: float
    unmeasured: tuple[str, ...]
    current_generation_unmeasured: frozenset[str]
    # What the stories still running have measurably spent so far. Not part of
    # ``accumulated``: it is provisional, replaced by the story's terminal figure
    # the moment the story lands. Carried in the same read so an in-flight budget
    # check cannot see a total from one moment and in-flight spend from another.
    in_flight: float = 0.0

    @property
    def spent(self) -> float:
        """This generation's spend plus what it inherited on resume."""
        return self.accumulated + self.prior

    @property
    def spent_including_in_flight(self) -> float:
        """Everything spent so far, counting stories that have not landed yet.

        The figure a mid-story cap check has to use: a sprint that has paid for
        work still in progress has spent that money whether or not the story it
        belongs to has returned (#2547).
        """
        return self.spent + self.in_flight

    @property
    def measured(self) -> bool:
        """False while any spend in the total could not be measured (#1992)."""
        return not self.unmeasured


class SprintCostLedger:
    """The single owner of what a sprint has spent.

    Before this existed the figure lived in ``run_sprint``'s frame and closures
    advanced it through ``nonlocal accumulated_cost``, so nothing could answer
    "what has this sprint spent, and what last changed that" without reading
    every writer. The ledger is the answer to both: it is the only thing that
    writes the total, and every advance goes through a named method on it.

    Writes are serialised because workers land concurrently, and the pairing of
    a cost with the unmeasured flag that qualifies it (``record_story_cost``)
    has to be one step or a dispatch check can read a total it believes is
    measured when it is not.
    """

    def __init__(self, *, accumulated: float = 0.0, prior: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._accumulated = float(accumulated)
        self._prior = float(prior)
        self._unmeasured: list[str] = []
        self._current_generation: set[str] = set()
        # slug -> what that still-running story has measurably spent so far.
        # Provisional spend the ledger owns for the same reason it owns the
        # total: a sprint that tracked in-flight cost anywhere else would have
        # two writers for one question again (#2547).
        self._in_flight: dict[str, SprintCostObservation] = {}
        # slug -> measured spend advanced by a pass that is not the story's own
        # coordinator run (intake remediation). Kept here because the ledger is
        # the only thing that knows the total is composed of more than stories,
        # and the audit's cost cross-check has to be able to say which part of
        # the total no story row was ever going to carry (#2847).
        self._non_story: dict[str, float] = {}

    # -- reads ----------------------------------------------------------
    @property
    def accumulated(self) -> float:
        """Spend this generation has measured or recovered."""
        with self._lock:
            return self._accumulated

    @property
    def prior(self) -> float:
        """Spend carried in from earlier generations of the same sprint."""
        with self._lock:
            return self._prior

    @property
    def spent(self) -> float:
        """Everything this sprint is accountable for, this run plus carried."""
        return self.snapshot().spent

    @property
    def unmeasured_sources(self) -> tuple[str, ...]:
        """Sources of spend the sprint could not measure, in discovery order."""
        with self._lock:
            return tuple(self._unmeasured)

    @property
    def current_generation_unmeasured(self) -> frozenset[str]:
        """The subset of unmeasured sources THIS generation produced (#2310)."""
        with self._lock:
            return frozenset(self._current_generation)

    @property
    def measured(self) -> bool:
        """True only when every dollar in the total was actually measured."""
        with self._lock:
            return not self._unmeasured

    def snapshot(self) -> SprintCostSnapshot:
        """Read the whole ledger at one moment."""
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> SprintCostSnapshot:
        """Build the read. Caller must hold ``self._lock``."""
        return SprintCostSnapshot(
            accumulated=self._accumulated,
            prior=self._prior,
            unmeasured=tuple(self._unmeasured),
            current_generation_unmeasured=frozenset(self._current_generation),
            in_flight=sum(observation.amount for observation in self._in_flight.values()),
        )

    # -- writes ---------------------------------------------------------
    def add(self, amount: float) -> float:
        """Advance the total by ``amount`` and return the new figure."""
        with self._lock:
            self._accumulated += amount
            return self._accumulated

    def note_non_story(self, slug: str, amount: float) -> None:
        """Record measured spend advanced for ``slug`` outside its own story run.

        Does not advance the total — :meth:`add` does that. This only remembers
        *what* the advance was for, so the audit can distinguish spend a story
        row will carry from spend that belongs to no story of this sprint.
        """
        if amount <= 0.0:
            return
        with self._lock:
            self._non_story[slug] = self._non_story.get(slug, 0.0) + float(amount)

    def non_story_spend(self, story_slugs: "set[str] | frozenset[str]") -> float:
        """Of the non-story spend recorded, the part no story row will carry.

        A slug in ``story_slugs`` has a row of its own, and the runner attributes
        the same amount to it through ``story_cost_adjustments`` — so that part
        of the total *is* explained and must not be double-counted. What is left
        is spend on issues this sprint never scheduled: real, measured, and
        accountable only at the sprint level.
        """
        with self._lock:
            return round(
                sum(amount for slug, amount in self._non_story.items() if slug not in story_slugs),
                4,
            )

    def set_prior(self, amount: float) -> None:
        """Record spend inherited from an earlier generation (resume triage)."""
        with self._lock:
            self._prior = float(amount)

    def flag_unmeasured_here(self, source: str) -> None:
        """Record a source whose unmeasured spend occurred in THIS run.

        Spend that happened here is a new unknown nobody has bounded, so it must
        never be absorbed by an operator acceptance made for an earlier
        occurrence of the same story (#2310).
        """
        with self._lock:
            self._note_unmeasured_locked(source, current_generation=True)

    def note_carried_unmeasured(self, source: str) -> None:
        """Record unmeasured spend inherited from an earlier generation."""
        with self._lock:
            self._note_unmeasured_locked(source, current_generation=False)

    def _note_unmeasured_locked(self, source: str, *, current_generation: bool) -> None:
        """Record one unmeasured-spend source. Caller must hold ``self._lock``."""
        if source not in self._unmeasured:
            self._unmeasured.append(source)
        if current_generation:
            self._current_generation.add(source)

    def record_in_flight_cost(
        self, slug: str, cost: float, *, measured: bool = True
    ) -> SprintCostSnapshot:
        """Record what a *running* story has measurably spent so far."""
        return self.checkpoint_in_flight_cost(slug, cost, measured=measured)

    def checkpoint_in_flight_cost(
        self, slug: str, cost: float | None, *, measured: bool = True
    ) -> SprintCostSnapshot:
        """Return the ledger state at a phase boundary for one running story.

        When *cost* is numeric, last-write-wins per slug because the coordinator
        reports a running total rather than an increment. When *cost* is
        ``None``, no new lower bound was available, so the story keeps the last
        measured figure already on the ledger. In both cases the returned
        snapshot is taken under the same lock as any update, so the cap check
        evaluates one consistent moment instead of a total and an in-flight
        figure that drifted apart between two reads.
        """
        with self._lock:
            if cost is not None:
                self._in_flight[slug] = SprintCostObservation(
                    amount=max(0.0, float(cost)),
                    measured=bool(measured),
                )
            return self._snapshot_locked()

    def has_in_flight_cost(self, slug: str) -> bool:
        """Whether ``slug`` currently owns a provisional in-flight ledger entry."""
        with self._lock:
            return slug in self._in_flight

    def drop_in_flight_cost(self, slug: str) -> None:
        """Forget a story's provisional spend without recording a total.

        For exits that intentionally abandon the story's spend rather than
        promoting it into the sprint total: the provisional figure must not
        linger in the in-flight sum for the rest of the sprint.
        """
        with self._lock:
            self._in_flight.pop(slug, None)

    def recover_in_flight_cost(
        self,
        slug: str,
        *,
        fallback_cost: float | None = None,
        fallback_measured: bool = True,
    ) -> SprintCostSnapshot:
        """Fold a raised worker's last measured spend into the sprint total.

        A worker exception never produces a terminal ``CoordinatorResult`` to
        replace its provisional in-flight figure. Promoting that last measured
        figure into ``accumulated`` preserves money the sprint definitely spent
        instead of silently making it disappear from later cap checks and the
        terminal total. When the recovered figure is only a lower bound, the
        sprint total must stay marked unmeasured. When the in-flight ledger
        entry is already gone, the caller's last live-state cost snapshot is the
        next-best lower bound.
        """
        with self._lock:
            _recovered = self._in_flight.pop(slug, None)
            if _recovered is None and fallback_cost is not None:
                _recovered = SprintCostObservation(
                    amount=max(0.0, float(fallback_cost)),
                    measured=bool(fallback_measured),
                )
            if _recovered is not None:
                self._accumulated += _recovered.amount
                if not _recovered.measured:
                    self._note_unmeasured_locked(slug, current_generation=True)
            return self._snapshot_locked()

    def record_story_cost(self, slug: str, cost: float, *, measured: float | None) -> float:
        """Fold a finished story's spend into the total in one step.

        The budget can only ever be enforced against measured spend, so the
        shortfall is recorded alongside the figure rather than after it — the
        dispatch check must never see the advanced total without also seeing
        that it is a lower bound (#1992).

        The story's provisional in-flight figure is dropped inside the same lock
        that adds its terminal one, so no reader can observe the story's spend
        counted twice — nor, in the other order, missing entirely (#2547).
        """
        with self._lock:
            self._in_flight.pop(slug, None)
            if measured is None:
                self._note_unmeasured_locked(slug, current_generation=True)
            self._accumulated += cost
            return self._accumulated


def copy_worker_signals(
    signals: "dict[str, threading.Event]",
) -> list[tuple[str, threading.Event]]:
    """Copy a slug -> Event map safely from a thread that does not own it.

    The scheduler owns ``stop_events`` and ``plan_gates``, adding and removing
    entries as stories dispatch and land. Until the budget checkpoint (#2547) it
    was their only reader, so neither needed a lock; a worker thread copying one
    can now race a scheduler mutation, which CPython reports rather than
    corrupts. Retrying the copy is the whole fix — the loser of the race reads a
    moment later, and a story that landed in between no longer needs signalling.
    """
    for _ in range(3):
        try:
            return list(signals.items())
        except RuntimeError:  # dict changed size during iteration
            continue
    return []


@dataclass(frozen=True)
class BudgetVerification:
    """What the cap was finally verified against, and what stayed unresolved.

    Reported on ``SprintResult`` and in the terminal audit. An acceptance
    resolves the BUDGET question, never the measurement one: the sprint total
    stays a lower bound while any source is unmeasured, accepted or not (#2310).
    """

    unresolved_sources: tuple[str, ...]
    accepted: tuple[AcceptedUnmeasuredSpend, ...]
    accepted_ceiling_usd: float
    verification_spend_usd: float


class SprintBudgetRuntime:
    """The runtime half of a sprint's budget, as a thing with a name.

    Constructed with the sprint's execution state and nothing else, so a test
    can build the state a sprint would have, exercise an enforcement moment, and
    read back the decision without a sprint running.

    What it owns beyond the ledger is the sprint's answer to "which unmeasured
    spend has been resolved": the operator acceptances read from and written to
    the sprint's persisted state, and the occurrence each inherited source
    belongs to. Both are consulted by every enforcement moment, so they live
    with the moments rather than in the caller's frame.
    """

    def __init__(self, state: Any) -> None:
        self._state = state
        self._accepted: dict[str, AcceptedUnmeasuredSpend] = {}
        # Keyed by (occurrence, source), never by source alone — see
        # ``_OCCURRENCE_CARRIED`` above and :meth:`describe_source`.
        self._source_cache: dict[tuple[str, str], UnmeasuredSource] = {}
        self._carried_occurrence_ids: dict[str, str | None] = {}
        self._set_outcome: Callable[..., None] | None = None
        self._record_story_entry: Callable[..., None] | None = None

    # -- collaborators the runner owns ------------------------------------
    def bind_story_hooks(
        self,
        *,
        set_outcome: Callable[..., None],
        record_story_entry: Callable[..., None],
    ) -> None:
        """Supply the two runner-owned side effects a budget skip performs.

        Recording a story's canonical outcome and writing this generation's
        accumulated entry are the runner's concerns, not budget's; a budget skip
        merely has to cause them. Injecting them keeps the direction of the
        dependency one-way, which is the whole point of the move.
        """
        self._set_outcome = set_outcome
        self._record_story_entry = record_story_entry

    # -- reads -------------------------------------------------------------
    @property
    def context(self) -> Any:
        """The frozen run context, read off the state (never a copy)."""
        return self._state.context

    @property
    def budget_usd(self) -> float:
        return self.context.resolved.budget_usd

    @property
    def accepted_unmeasured(self) -> dict[str, AcceptedUnmeasuredSpend]:
        """Operator resolutions in force for this run, by normalized source."""
        return dict(self._accepted)

    @property
    def carried_occurrence_ids(self) -> dict[str, str | None]:
        """Which run each inherited unmeasured source belongs to."""
        return dict(self._carried_occurrence_ids)

    def describe_source(self, raw: str, *, occurrence: str | None = None) -> UnmeasuredSource:
        """Resolve one raw source id to its origin and derivable ceiling.

        ``occurrence`` defaults to whichever bucket the source is in: one this
        run produced reads the audit as it stands now, an inherited one reads it
        as it stood before any story here could rewrite it.

        The cache is keyed by (occurrence, source), never by source alone. A
        story has ONE per-story audit, and running it again overwrites that file
        — so the carried occurrence and a new one produced here are two
        different records read from the same path at two different times. Keyed
        by source alone, the carried read wins and the refusal on the NEW unknown
        would name the old run's id and ceiling: an operator would go looking at
        a call they had already accepted, and the amount they were being asked
        about would be the wrong one (#2310 review).
        """
        _occurrence = occurrence or (
            _OCCURRENCE_CURRENT
            if raw in self._state.cost.current_generation_unmeasured
            else _OCCURRENCE_CARRIED
        )
        key = (_occurrence, unmeasured_spend_policy.normalize_source_id(raw))
        cached = self._source_cache.get(key)
        if cached is None:
            _slug = unmeasured_spend_policy.source_slug(raw)
            _story_audit = (
                unmeasured_spend_policy.read_story_audit(
                    self.context.config.project_root, self.context.resolved.name, _slug
                )
                if _slug
                else None
            )
            cached = unmeasured_spend_policy.build_source(raw, _story_audit)
            self._source_cache[key] = cached
        return cached

    # -- startup -----------------------------------------------------------
    def load_operator_acceptances(self) -> None:
        """Apply the operator resolutions of unmeasured spend in force (#2310).

        A fail-closed guard that no action can satisfy is not a safety property;
        it is an absorbing state whose only exit is to stop using the pipeline
        for the story — the very outcome the guard exists to prevent. These
        records are the deliberate exit: each names one source, the origin of the
        call that went unpriced, and the ceiling charged in its place. Nothing
        here relabels unmeasured spend as measured — ``cost_complete`` stays
        False for it, and the accepted ceiling is charged to budget verification
        only.
        """
        _ctx = self.context
        if _ctx.sprint_id is not None:
            for _persisted in _load_accepted_unmeasured_spend(
                _ctx.sprint_id, _ctx.config.project_root
            ):
                _restored = AcceptedUnmeasuredSpend.from_dict(_persisted)
                if _restored is not None:
                    self._accepted[_restored.source] = _restored
        _newly_accepted = False
        if _ctx.accept_unmeasured_spend:
            # The set an operator could possibly be talking about: what this run
            # has already flagged, plus what the prior generation recorded.
            # Accepting a name outside it would record a resolution of nothing.
            _known_sources = {
                unmeasured_spend_policy.normalize_source_id(s)
                for s in self._state.cost.unmeasured_sources
            }
            _known_sources.update(
                unmeasured_spend_policy.normalize_source_id(s)
                for s in prior_unmeasured_spend_sources(_ctx.config.project_root, _ctx.sprint_id)
            )
            _accepted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for _raw_accept in _ctx.accept_unmeasured_spend:
                _normalized = unmeasured_spend_policy.normalize_source_id(_raw_accept)
                if not _normalized:
                    continue
                if _normalized not in _known_sources:
                    _log(
                        f"REFUSED --accept-unmeasured-spend {_raw_accept}: this sprint has no "
                        f"unmeasured source by that name "
                        f"(known: {', '.join(sorted(_known_sources)) or 'none'})"
                    )
                    continue
                # An operator is always accepting an occurrence that has already
                # been recorded, so this reads the audit as it stands before
                # dispatch — never a record some story in this run is about to
                # overwrite.
                _source = self.describe_source(_raw_accept, occurrence=_OCCURRENCE_CARRIED)
                _accepted = unmeasured_spend_policy.accept(
                    _source,
                    accepted_at=_accepted_at,
                    reason=_ctx.accept_unmeasured_reason,
                )
                if _accepted is None:
                    _log(
                        f"REFUSED --accept-unmeasured-spend {_raw_accept}: "
                        f"{_source.ceiling_reason}. "
                        "The guard stays closed — accepting an unbounded unknown would defeat "
                        "the measurement it stands in for."
                    )
                    continue
                self._accepted[_accepted.source] = _accepted
                _newly_accepted = True
                _log(
                    f"Accepted unmeasured spend {_accepted.source}: charging "
                    f"${_accepted.accepted_ceiling_usd:.2f} to budget verification "
                    f"({_source.describe()})"
                )
        if _newly_accepted:
            # Written before any dispatch: the resolution is what makes this run
            # legal, so it must be on disk even if the run dies mid-sprint.
            if not persist_accepted_unmeasured_spend(
                _ctx.sprint_id,
                _ctx.resolved.name,
                _ctx.config.project_root,
                [r.as_dict() for r in self._accepted.values()],
            ):
                # Reporting the acceptance as recorded when it is not would leave
                # an operator to rediscover the same refusal with no idea they
                # had already answered it. It still applies to this run — nothing
                # was lost yet — but say plainly that it will not survive.
                _log(
                    "WARNING: could not persist the unmeasured-spend acceptance to "
                    f"{_ctx.config.project_root / '.forge' / 'sprints' / (_ctx.sprint_id or '?')}"
                    "/state.yaml — it applies to THIS run only and must be passed "
                    "again on the next one."
                )

    def pin_carried_occurrences(self) -> None:
        """Fix which run each inherited unmeasured source belongs to.

        Called BEFORE any story can rewrite its per-story audit. That file is the
        only durable record of a source's occurrence identity, and a story
        re-running here overwrites it — so read once, up front, or a source
        carried from run A would later be measured against run B's record and an
        acceptance the operator legitimately made would be silently discarded
        (#2310 review).
        """
        _carried_current = self._state.cost.current_generation_unmeasured
        for _carried_raw in self._state.cost.unmeasured_sources:
            if _carried_raw in _carried_current:
                continue
            _carried_origin = self.describe_source(
                _carried_raw, occurrence=_OCCURRENCE_CARRIED
            ).origin
            self._carried_occurrence_ids[_carried_raw] = _carried_origin.get("run_id")

    def evaluate_startup_headroom(self) -> BudgetBlock | None:
        """Disclose the carried spend this run starts from, and refuse if it left no room.

        The headroom line is the disclosure half: an operator has to be able to
        see, before anything dispatches, how much of the ceiling this run
        actually has — and whether that figure is itself a lower bound. The
        returned block is the refusal half, and is global to the selected run:
        nothing has dispatched yet, so it applies to every selected story.
        """
        _ctx = self.context
        if self.budget_usd <= 0.0:
            return None
        _carry_snapshot = load_sprint_carry_budget_snapshot(
            project_root=_ctx.config.project_root,
            sprint_name=_ctx.resolved.name,
            sprint_id=_ctx.sprint_id,
            resume=_ctx.resume,
            reexec=_ctx.reexec,
            accepted_unmeasured=dict(self._accepted),
        )
        _headroom = _carry_snapshot.remaining_headroom_usd(self.budget_usd)
        _budget_line = f"Budget ${self.budget_usd:.2f}"
        _budget_line += f" · carried ${_carry_snapshot.carried_cost_usd:.2f}"
        if _carry_snapshot.accepted_unmeasured_ceiling_usd > 0.0:
            _budget_line += (
                " · accepted unmeasured ceiling "
                f"${_carry_snapshot.accepted_unmeasured_ceiling_usd:.2f}"
            )
        _budget_line += f" · usable headroom ${max(_headroom, 0.0):.2f}"
        if _carry_snapshot.headroom_is_lower_bound:
            _budget_line += " lower bound"
        _log(_budget_line)
        _startup_budget_details = (
            {
                raw: self.describe_source(raw, occurrence=_OCCURRENCE_CARRIED).describe()
                for raw in _carry_snapshot.unresolved_unmeasured_sources
            }
            if _carry_snapshot.unresolved_unmeasured_sources
            else None
        )
        _startup_budget_sources = {
            raw: self.describe_source(raw, occurrence=_OCCURRENCE_CARRIED)
            for raw in _carry_snapshot.unresolved_unmeasured_sources
        }
        _decision = evaluate_budget(
            accumulated_cost=0.0,
            prior_cost=_carry_snapshot.carried_cost_usd,
            budget_usd=self.budget_usd,
            unmeasured_spend=_carry_snapshot.unresolved_unmeasured_sources,
            accepted_unmeasured_ceiling_usd=_carry_snapshot.accepted_unmeasured_ceiling_usd,
            source_details=_startup_budget_details,
            acceptable_unmeasured_spend_sources=[
                source.source for source in _startup_budget_sources.values() if source.acceptable
            ],
        )
        if _decision is not None:
            _log(f"Selected run cannot dispatch under the supplied ceiling: {_decision.detail}")
        return _decision

    # -- enforcement -------------------------------------------------------
    def decision_for(self, snapshot: SprintCostSnapshot) -> BudgetBlock | None:
        """Evaluate the cap against one ledger read, in-flight spend included.

        The sprint's single cap decision, asked from both enforcement moments:
        before a story is dispatched and while one is running. Both charge the
        spend of stories that have not landed yet, because the sprint has
        already paid for it (#2547).
        """
        _unresolved, _applied = unmeasured_spend_policy.partition(
            list(snapshot.unmeasured),
            self._accepted,
            current_generation=set(snapshot.current_generation_unmeasured),
            occurrence_ids=self._carried_occurrence_ids,
        )
        # Origin/ceiling lookup reads per-story audits, so it runs off the
        # snapshot — it is reporting, not accounting.
        _details = (
            {raw: self.describe_source(raw).describe() for raw in _unresolved}
            if _unresolved
            else None
        )
        _sources = {raw: self.describe_source(raw) for raw in _unresolved}
        return evaluate_budget(
            accumulated_cost=snapshot.accumulated + snapshot.in_flight,
            prior_cost=snapshot.prior,
            budget_usd=self.budget_usd,
            unmeasured_spend=_unresolved,
            accepted_unmeasured_ceiling_usd=unmeasured_spend_policy.accepted_ceiling_total(
                _applied
            ),
            source_details=_details,
            acceptable_unmeasured_spend_sources=[
                source.source for source in _sources.values() if source.acceptable
            ],
        )

    def decision_before_dispatch(self) -> BudgetBlock | None:
        """The cap decision the scheduler asks before launching a story."""
        return self.decision_for(self._state.cost.snapshot())

    def checkpoint(self, slug: str, measured_cost: SprintCostObservation | None) -> None:
        """Charge a running story's spend to the cap, and halt if it is met.

        Called from the worker thread at every coordinator phase boundary that
        reports a cost. Only ``exhausted`` acts here: an unverifiable answer
        means some *other* spend was unmeasured, and killing paid-for work over
        that would destroy a story to protect a comparison the dispatch gate
        re-runs — and fails closed on — a moment later.
        """
        if self.budget_usd <= 0.0:
            return
        _snapshot = self._state.cost.checkpoint_in_flight_cost(
            slug,
            None if measured_cost is None else measured_cost.amount,
            measured=True if measured_cost is None else measured_cost.measured,
        )
        self.publish_live_status(_snapshot.spent_including_in_flight)
        if self._state.stop.stopped:
            return
        _decision = self.decision_for(_snapshot)
        if _decision is None or _decision.kind != "exhausted":
            return
        self.halt(slug, _decision)

    def publish_live_status(self, spend_usd: float) -> None:
        """Record how the live run stands against its cap.

        ``forge status`` reads this rather than comparing two numbers it happens
        to print next to each other — and the runner is the only party that can
        supply it, because the live story rows do not carry spend inherited from
        an earlier generation or spent outside any story (#2547).
        """
        if self._state.state_writer is None:
            return
        _status = budget_status(budget_usd=self.budget_usd, spend_usd=spend_usd)
        self._state.state_writer.set_budget_status(
            _status,
            overrun_usd=budget_overrun_usd(budget_usd=self.budget_usd, spend_usd=spend_usd),
            spend_usd=spend_usd,
        )

    def halt(self, slug: str, decision: BudgetBlock) -> None:
        """Stop every running story because the sprint's cap has been reached.

        The auth circuit breaker's shape (#1952), for the other reason a sprint
        has to stop work it already started: cancel in-flight workers at their
        next phase boundary and release any plan gate they are parked on, so the
        sprint stops in seconds rather than after another full review cycle.

        Which slugs WE cancelled is remembered, because their results return
        through the generic cancellation path and would otherwise be recorded as
        story failures — the sprint ran out of money, which is not a verdict on
        anyone's work.
        """
        _state = self._state
        _ctx = self.context
        if not _state.stop.stop_if_unset(decision.stopped_reason, halt_slug=slug):
            return
        _log(f"HALT sprint: {decision.stopped_reason}")
        _cancel_reason = f"sprint budget exhausted while running ({decision.detail})"
        for _pending_slug, _pending_evt in copy_worker_signals(_state.stop_events):
            _state.budget_cancelled_slugs.add(_pending_slug)
            _stop_fn = getattr(_pending_evt, "stop", None)
            if callable(_stop_fn):
                _stop_fn(_cancel_reason, error_type=BUDGET_CANCEL_ERROR_TYPE)
            else:  # pragma: no cover - defensive: a bare Event still stops work
                _pending_evt.set()
        for _gate_slug, _pending_gate in copy_worker_signals(_state.plan_gates):
            _log(f"Releasing plan gate for {_gate_slug} (budget halt)")
            _pending_gate.set()
        if _ctx.notify and _ctx.config.notifications.backend not in ("ntfy", "none"):
            from ..notify_backends import send_notifications

            send_notifications(
                _ctx.config,
                decision.notification_title(_ctx.resolved.name),
                f"{decision.detail} — running stories cancelled, remaining stories skipped",
            )

    def skip_story(self, slug: str, decision: BudgetBlock) -> None:
        """Refuse one story for the cap, recording the reason it was refused."""
        _state = self._state
        _ctx = self.context
        _state.dag.mark_skipped(slug)
        _budget_reason = decision.story_reason
        if self._set_outcome is not None:
            self._set_outcome(slug, StoryOutcome.SKIPPED, reason=_budget_reason)
        if _state.stop.stop_if_unset(decision.stopped_reason):
            if _ctx.notify and _ctx.config.notifications.backend not in ("ntfy", "none"):
                from ..notify_backends import send_notifications

                send_notifications(
                    _ctx.config,
                    decision.notification_title(_ctx.resolved.name),
                    f"{decision.detail} — remaining stories skipped",
                )
        _log(f"SKIPPED {slug} ({_budget_reason})")
        if self._record_story_entry is not None:
            self._record_story_entry(slug, "SKIPPED", error=_budget_reason)
        if _state.state_writer is not None:
            _state.state_writer.update(slug, status="skipped")

    def skip_remaining_stories(self, decision: BudgetBlock) -> None:
        """Refuse every story still scheduled, for one global decision.

        A startup headroom refusal is global to the selected run: nothing has
        dispatched yet, so every remaining selected story is refused for the same
        reason rather than letting downstream dependents later degrade into
        "dependency failed" during the deadlock sweep.
        """
        for _remaining_task in list(self._state.dag.remaining()):
            self.skip_story(_remaining_task.slug, decision)

    # -- terminal ----------------------------------------------------------
    def verification(self, snapshot: SprintCostSnapshot) -> BudgetVerification:
        """What the cap was verified against once the sprint is done.

        An acceptance resolves the BUDGET question, never the measurement one:
        the sprint total is still reported as a lower bound while any source is
        unmeasured. What acceptance changes is which figure the cap was verified
        against (#2310).
        """
        _unresolved, _accepted = unmeasured_spend_policy.partition(
            list(snapshot.unmeasured),
            self._accepted,
            current_generation=set(snapshot.current_generation_unmeasured),
            occurrence_ids=self._carried_occurrence_ids,
        )
        _ceiling = unmeasured_spend_policy.accepted_ceiling_total(_accepted)
        return BudgetVerification(
            unresolved_sources=tuple(_unresolved),
            accepted=tuple(_accepted),
            accepted_ceiling_usd=_ceiling,
            verification_spend_usd=budget_verification_spend(
                accumulated_cost=snapshot.accumulated,
                prior_cost=snapshot.prior,
                accepted_unmeasured_ceiling_usd=_ceiling,
            ),
        )
