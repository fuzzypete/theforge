"""Tests for ``sprint/budget_runtime.py``: the runtime half of the sprint cap.

The module owns one responsibility end to end — accumulate what a sprint has
spent, restore and disclose what it carried in, ask
:mod:`theforge.sprint.budget` at each enforcement moment, publish the live
budget status, and cancel in-flight work when the cap is met — and every test
here calls it directly: a spend state is constructed, an enforcement moment is
exercised, and the decision is read back with no sprint running, no worktree,
and no agent invoked (#2621).

Sprint-level behaviour (outcomes, audit fields, totals, operator-facing strings)
stays covered end to end in ``tests/test_sprint_budget_enforcement.py``. What is
asserted here is the concern itself, on its own.
"""

from __future__ import annotations

import ast
import dataclasses
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from theforge.coordinator.cancellation import BUDGET_CANCEL_ERROR_TYPE
from theforge.sprint.budget_runtime import (
    SprintBudgetRuntime,
    SprintCostLedger,
    SprintCostObservation,
    checkpoint_cost,
    optional_cost,
)
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.runner import SprintExecutionState, SprintRunContext
from theforge.sprint.story_state import StoryOutcome


def _run_context(**overrides) -> SprintRunContext:
    """A context for a sprint that is never dispatched."""
    resolved = ResolvedSprint(
        name="budget-unit",
        budget_usd=overrides.pop("budget_usd", 10.0),
        max_parallel=1,
        stories=[],
    )
    kwargs = {
        "config": SimpleNamespace(
            project_root=Path("/nonexistent"),
            notifications=SimpleNamespace(backend="none"),
        ),
        "resolved": resolved,
        "sprint_id": "sprint-abc",
        "run_id": "run-abc",
        "notify": False,
    }
    kwargs.update(overrides)
    return SprintRunContext(**kwargs)  # type: ignore[arg-type]


def _state(**overrides) -> SprintExecutionState:
    return SprintExecutionState(context=_run_context(**overrides))


class _FakeDag:
    def __init__(self, slugs: list[str] | None = None) -> None:
        self.skipped: list[str] = []
        self._remaining = [SimpleNamespace(slug=s) for s in (slugs or [])]

    def mark_skipped(self, slug: str) -> None:
        self.skipped.append(slug)

    def remaining(self) -> list[SimpleNamespace]:
        return list(self._remaining)


class _FakeStateWriter:
    def __init__(self) -> None:
        self.budget_status: tuple | None = None
        self.updates: list[tuple[str, dict]] = []

    def set_budget_status(self, status: str, *, overrun_usd: float, spend_usd: float) -> None:
        self.budget_status = (status, overrun_usd, spend_usd)

    def update(self, slug: str, **fields: object) -> None:
        self.updates.append((slug, dict(fields)))


class _FakeStopSignal:
    def __init__(self) -> None:
        self.stopped_with: tuple[str, str | None] | None = None

    def stop(self, reason: str, *, error_type: str | None = None) -> None:
        self.stopped_with = (reason, error_type)


# ── the ledger ───────────────────────────────────────────────────────────


class TestSprintCostLedgerOwnership:
    """Cost accumulation has exactly one owner."""

    def test_only_the_ledger_advances_the_total(self) -> None:
        ledger = SprintCostLedger()

        # Reading the figure out and reassigning the name is what the old
        # ``nonlocal accumulated_cost`` allowed. Here it cannot reach the total.
        accumulated_cost = ledger.accumulated
        accumulated_cost += 12.5

        assert ledger.accumulated == 0.0
        assert ledger.add(2.5) == 2.5
        assert ledger.accumulated == 2.5

    def test_the_total_is_not_settable(self) -> None:
        ledger = SprintCostLedger()
        ledger.add(4.0)

        with pytest.raises(AttributeError):
            ledger.accumulated = 100.0  # type: ignore[misc]
        with pytest.raises(AttributeError):
            ledger.spent = 100.0  # type: ignore[misc]

        assert ledger.accumulated == 4.0

    def test_add_returns_the_accumulated_figure(self) -> None:
        ledger = SprintCostLedger()

        assert ledger.add(1.25) == 1.25
        assert ledger.add(0.75) == 2.0
        assert ledger.snapshot().accumulated == 2.0

    def test_prior_spend_is_carried_but_kept_distinct(self) -> None:
        ledger = SprintCostLedger()
        ledger.set_prior(6.0)
        ledger.add(1.5)

        snapshot = ledger.snapshot()
        assert snapshot.prior == 6.0
        assert snapshot.accumulated == 1.5
        assert snapshot.spent == 7.5
        assert ledger.spent == 7.5

    def test_unmeasured_story_cost_lands_with_its_flag(self) -> None:
        """The total and the fact that it is a lower bound move together."""
        ledger = SprintCostLedger()

        ledger.record_story_cost("story-a", 3.0, measured=3.0)
        assert ledger.measured is True

        ledger.record_story_cost("story-b", 2.0, measured=None)
        snapshot = ledger.snapshot()
        assert snapshot.accumulated == 5.0
        assert snapshot.measured is False
        assert snapshot.unmeasured == ("story-b",)
        assert snapshot.current_generation_unmeasured == frozenset({"story-b"})

    def test_carried_unmeasured_is_not_this_generation(self) -> None:
        """An inherited unknown must not be absorbed by a local acceptance."""
        ledger = SprintCostLedger()
        ledger.note_carried_unmeasured("carried:story-a")
        ledger.flag_unmeasured_here("intake:story-b")

        snapshot = ledger.snapshot()
        assert snapshot.unmeasured == ("carried:story-a", "intake:story-b")
        assert snapshot.current_generation_unmeasured == frozenset({"intake:story-b"})

    def test_snapshot_does_not_track_later_writes(self) -> None:
        ledger = SprintCostLedger()
        ledger.record_story_cost("story-a", 1.0, measured=None)
        snapshot = ledger.snapshot()

        ledger.record_story_cost("story-b", 4.0, measured=None)

        assert snapshot.accumulated == 1.0
        assert snapshot.unmeasured == ("story-a",)
        assert ledger.snapshot().accumulated == 5.0

    def test_snapshot_is_read_only(self) -> None:
        with pytest.raises(FrozenInstanceError):
            SprintCostLedger().snapshot().accumulated = 99.0  # type: ignore[misc]

    def test_concurrent_workers_cannot_lose_spend(self) -> None:
        """Workers land in parallel; the ledger serialises what they add."""
        ledger = SprintCostLedger()
        barrier = threading.Barrier(8)

        def _land(index: int) -> None:
            barrier.wait()
            ledger.record_story_cost(f"story-{index}", 0.5, measured=None if index % 2 else 0.5)

        threads = [threading.Thread(target=_land, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        snapshot = ledger.snapshot()
        assert snapshot.accumulated == pytest.approx(4.0)
        assert len(snapshot.unmeasured) == 4
        assert snapshot.current_generation_unmeasured == frozenset(
            f"story-{i}" for i in range(8) if i % 2
        )


class TestCheckpointCost:
    """What a live state update is worth as a lower bound on spend."""

    def test_reported_cost_is_measured(self) -> None:
        assert checkpoint_cost({"cost_usd": 1.5}) == SprintCostObservation(1.5, measured=True)

    def test_detail_lower_bound_is_not_measured(self) -> None:
        observation = checkpoint_cost({"detail": {"cost_measured_lower_bound_usd": 2.0}})
        assert observation == SprintCostObservation(2.0, measured=False)

    def test_coordinator_total_without_measured_total_is_a_lower_bound(self) -> None:
        updates = {"coordinator_state": SimpleNamespace(total_cost=3.0, total_cost_measured=None)}
        assert checkpoint_cost(updates) == SprintCostObservation(3.0, measured=False)

    def test_nothing_to_report_returns_none(self) -> None:
        assert checkpoint_cost({}) is None

    def test_optional_cost_preserves_the_unmeasured_distinction(self) -> None:
        assert optional_cost(0.0) == 0.0
        assert optional_cost(None) is None
        assert optional_cost(True) is None


# ── the enforcement moments, without a sprint ────────────────────────────


class TestEnforcementMoments:
    """A spend state is constructed and the concern returns its decision."""

    def test_dispatch_decision_is_none_while_there_is_headroom(self) -> None:
        state = _state()
        state.cost.add(2.0)

        assert state.budget.decision_before_dispatch() is None

    def test_dispatch_decision_is_exhausted_once_the_cap_is_met(self) -> None:
        state = _state(budget_usd=5.0)
        state.cost.record_story_cost("story-a", 5.0, measured=5.0)

        decision = state.budget.decision_before_dispatch()

        assert decision is not None
        assert decision.kind == "exhausted"
        assert "budget exhausted" in decision.story_reason

    def test_dispatch_fails_closed_on_unmeasured_spend(self) -> None:
        """A cap cannot be certified against a number known to be a lower bound."""
        state = _state(budget_usd=100.0)
        state.cost.record_story_cost("story-a", 1.0, measured=None)

        decision = state.budget.decision_before_dispatch()

        assert decision is not None
        assert decision.kind == "unverifiable"

    def test_in_flight_spend_is_charged_to_the_decision(self) -> None:
        """A sprint that has paid for a running story has spent that money."""
        state = _state(budget_usd=5.0)
        state.cost.add(1.0)
        snapshot = state.cost.checkpoint_in_flight_cost("story-a", 4.5)

        assert snapshot.spent_including_in_flight == pytest.approx(5.5)
        decision = state.budget.decision_for(snapshot)
        assert decision is not None and decision.kind == "exhausted"

    def test_checkpoint_charges_spend_publishes_status_and_halts(self) -> None:
        state = _state(budget_usd=5.0)
        state.state_writer = _FakeStateWriter()
        signal = _FakeStopSignal()
        state.stop_events["story-a"] = signal  # type: ignore[assignment]
        gate = threading.Event()
        state.plan_gates["story-a"] = gate

        state.budget.checkpoint("story-a", SprintCostObservation(6.0, measured=True))

        assert state.cost.snapshot().in_flight == pytest.approx(6.0)
        assert state.state_writer.budget_status is not None
        status, overrun, spend = state.state_writer.budget_status
        assert status == "over"
        assert overrun == pytest.approx(1.0)
        assert spend == pytest.approx(6.0)
        assert state.stop.stopped
        assert state.stop.record is not None and state.stop.record.halt_slug == "story-a"
        assert "story-a" in state.budget_cancelled_slugs
        assert signal.stopped_with is not None
        assert signal.stopped_with[1] == BUDGET_CANCEL_ERROR_TYPE
        # The plan gate is released so a story parked between PLAN and DEV stops
        # in seconds rather than after another full review cycle.
        assert gate.is_set()

    def test_checkpoint_does_not_kill_work_over_an_unverifiable_answer(self) -> None:
        """Only ``exhausted`` cancels: the dispatch gate re-runs the other case."""
        state = _state(budget_usd=100.0)
        state.cost.record_story_cost("story-a", 1.0, measured=None)
        signal = _FakeStopSignal()
        state.stop_events["story-b"] = signal  # type: ignore[assignment]

        state.budget.checkpoint("story-b", SprintCostObservation(1.0, measured=True))

        assert not state.stop.stopped
        assert signal.stopped_with is None

    def test_no_cap_configured_means_no_enforcement(self) -> None:
        state = _state(budget_usd=0.0)
        state.state_writer = _FakeStateWriter()

        state.budget.checkpoint("story-a", SprintCostObservation(999.0, measured=True))

        assert not state.stop.stopped
        assert state.state_writer.budget_status is None
        assert state.cost.snapshot().in_flight == 0.0

    def test_halt_is_first_writer_wins(self) -> None:
        state = _state(budget_usd=5.0)
        state.stop.stop("CI red on story-z")

        state.budget.checkpoint("story-a", SprintCostObservation(50.0, measured=True))

        assert state.stop.reason == "CI red on story-z"


class TestBudgetSkips:
    """A refused story records the refusal on every surface the runner owns."""

    def _bound(self, state: SprintExecutionState) -> tuple[list, list]:
        outcomes: list = []
        entries: list = []
        state.budget.bind_story_hooks(
            set_outcome=lambda slug, outcome, **fields: outcomes.append((slug, outcome, fields)),
            record_story_entry=lambda slug, outcome, **fields: entries.append(
                (slug, outcome, fields)
            ),
        )
        return outcomes, entries

    def test_skip_story_marks_the_dag_outcome_entry_and_live_row(self) -> None:
        state = _state(budget_usd=1.0)
        state.dag = _FakeDag()  # type: ignore[assignment]
        state.state_writer = _FakeStateWriter()
        outcomes, entries = self._bound(state)
        state.cost.record_story_cost("story-a", 2.0, measured=2.0)
        decision = state.budget.decision_before_dispatch()
        assert decision is not None

        state.budget.skip_story("story-b", decision)

        assert state.dag.skipped == ["story-b"]
        assert outcomes == [("story-b", StoryOutcome.SKIPPED, {"reason": decision.story_reason})]
        assert entries == [("story-b", "SKIPPED", {"error": decision.story_reason})]
        assert state.state_writer.updates == [("story-b", {"status": "skipped"})]
        assert state.stop.reason == decision.stopped_reason

    def test_a_startup_refusal_refuses_every_remaining_story(self) -> None:
        state = _state(budget_usd=1.0)
        state.dag = _FakeDag(["story-a", "story-b", "story-c"])  # type: ignore[assignment]
        self._bound(state)
        state.cost.set_prior(2.0)
        decision = state.budget.decision_before_dispatch()
        assert decision is not None

        state.budget.skip_remaining_stories(decision)

        assert state.dag.skipped == ["story-a", "story-b", "story-c"]


class TestBudgetVerification:
    """What the cap was finally verified against."""

    def test_measured_run_verifies_against_its_own_total(self) -> None:
        state = _state()
        state.cost.set_prior(1.0)
        state.cost.add(2.0)

        verification = state.budget.verification(state.cost.snapshot())

        assert verification.unresolved_sources == ()
        assert verification.accepted == ()
        assert verification.accepted_ceiling_usd == 0.0
        assert verification.verification_spend_usd == pytest.approx(3.0)

    def test_unresolved_sources_are_reported_unaccepted(self) -> None:
        state = _state()
        state.cost.record_story_cost("story-a", 1.0, measured=None)

        verification = state.budget.verification(state.cost.snapshot())

        assert verification.unresolved_sources == ("story-a",)
        assert verification.accepted_ceiling_usd == 0.0


class TestRuntimeIsReachableFromTheState:
    """The concern is reached through the state, never rebuilt by its callers."""

    def test_every_state_carries_its_budget_runtime(self) -> None:
        state = SprintExecutionState.for_run(_run_context())

        assert isinstance(state.budget, SprintBudgetRuntime)
        assert state.budget.context is state.context
        assert state.budget.budget_usd == state.context.resolved.budget_usd

    def test_two_states_do_not_share_a_budget_runtime(self) -> None:
        first, second = _state(), _state()

        assert first.budget is not second.budget


# ── structural guards ────────────────────────────────────────────────────


def _budget_runtime_tree() -> ast.Module:
    from theforge.sprint import budget_runtime

    return ast.parse(Path(budget_runtime.__file__).read_text(encoding="utf-8"))


def test_the_module_does_not_import_the_sprint_runner() -> None:
    """Separation is the point of the move — a dependency back would undo it.

    Two stories changing unrelated parts of a sprint run should claim different
    files. An import of ``sprint.runner`` here — including one hidden inside a
    function or behind ``TYPE_CHECKING`` — puts this module back in the runner's
    dependency graph and makes the move a relocation (#2402, #2621).
    """
    offenders: list[str] = []
    for node in ast.walk(_budget_runtime_tree()):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "runner" or module.endswith(".runner"):
                offenders.append(f"line {node.lineno}: from {'.' * node.level}{module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("sprint.runner"):
                    offenders.append(f"line {node.lineno}: import {alias.name}")
    assert not offenders, "sprint/budget_runtime.py imports the sprint runner: " + "; ".join(
        offenders
    )


def test_the_runtime_takes_the_state_not_its_members() -> None:
    """Threading state members into the new home is the extraction not happening.

    ``tests/test_sprint_runner_structure.py`` holds this shape for functions
    left in ``runner.py``; a concern that moved out could otherwise satisfy it
    by taking fifteen members here instead, which is the same coupling under
    another name (#2399, #2621).

    ``SprintCostLedger`` is exempt: it *is* ``state.cost``, so a ``cost`` amount
    on its own methods is the thing it owns being written, not the state being
    threaded past it.
    """
    carried = frozenset(
        f.name for f in dataclasses.fields(SprintExecutionState) if f.name != "context"
    )
    assert carried, "the execution state carries nothing — nothing to guard"
    offenders: list[str] = []
    for owner in ast.walk(_budget_runtime_tree()):
        if isinstance(owner, ast.ClassDef) and owner.name == "SprintCostLedger":
            continue
        for node in ast.iter_child_nodes(owner):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            for param in params:
                if param.arg in carried:
                    offenders.append(f"{node.name}(..., {param.arg}=...)")
    assert not offenders, (
        "these budget-runtime functions take members of the execution state as "
        "parameters; take the state instead: " + "; ".join(offenders)
    )
