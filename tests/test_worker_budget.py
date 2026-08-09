"""Unit tests for the enclosing worker budget registry (#2333).

The module answers two questions for code running deep inside a story: "what is
the ceiling I am running under?" and "how much of the elapsed clock was the
system waiting on a human?". Everything here pins those two answers, including
the cases where the answer must be *nothing* — outside a sprint there is no
enclosing budget, and a caller that treats a missing budget as zero would clamp
every standalone ``forge run`` allowance to nothing.
"""

from __future__ import annotations

import threading
import time

import pytest

from theforge import worker_budget
from theforge.log_util import get_worker_slug, set_worker_slug


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Registry and thread-local slug are process-global; never leak either."""
    worker_budget.clear_worker_budgets()
    set_worker_slug("")
    yield
    worker_budget.clear_worker_budgets()
    set_worker_slug("")


# ── Registry lifecycle ───────────────────────────────────────────────────────


def test_register_returns_and_stores_the_budget() -> None:
    budget = worker_budget.register_worker_budget("story-a", 3600, started_at=100.0)

    assert budget.slug == "story-a"
    assert budget.worker_timeout_seconds == 3600.0
    assert budget.started_at == 100.0
    assert worker_budget.get_worker_budget("story-a") is budget


def test_unregister_and_clear_drop_the_budget() -> None:
    worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)
    worker_budget.register_worker_budget("story-b", 3600, started_at=0.0)

    worker_budget.unregister_worker_budget("story-a")
    assert worker_budget.get_worker_budget("story-a") is None
    assert worker_budget.get_worker_budget("story-b") is not None

    # Unregistering an unknown slug is not an error: the scheduler pops budgets
    # on several terminal paths and must not care which one got there first.
    worker_budget.unregister_worker_budget("story-a")

    worker_budget.clear_worker_budgets()
    assert worker_budget.get_worker_budget("story-b") is None


def test_registering_the_same_slug_twice_replaces_the_window() -> None:
    """A re-dispatched story runs under a fresh ceiling, not its predecessor's."""
    first = worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)
    second = worker_budget.register_worker_budget("story-a", 5400, started_at=500.0)

    assert worker_budget.get_worker_budget("story-a") is second
    assert second is not first
    assert second.worker_timeout_seconds == 5400.0


# ── Thread-local lookup ──────────────────────────────────────────────────────


def test_current_budget_is_none_without_a_worker_slug() -> None:
    """Standalone ``forge run``: no slug, no budget, nothing to clamp against."""
    worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)

    assert get_worker_slug() == ""
    assert worker_budget.current_worker_budget() is None


def test_current_budget_is_none_for_a_slug_with_no_registration() -> None:
    set_worker_slug("story-a")
    assert worker_budget.current_worker_budget() is None


def test_current_budget_follows_the_thread_local_slug() -> None:
    budget = worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)
    set_worker_slug("story-a")

    assert worker_budget.current_worker_budget() is budget


def test_each_worker_thread_sees_only_its_own_budget() -> None:
    """Parallel stories share the registry but not the slug that keys it."""
    a = worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)
    b = worker_budget.register_worker_budget("story-b", 1800, started_at=0.0)
    seen: dict[str, worker_budget.WorkerBudget | None] = {}

    def _worker(slug: str) -> None:
        set_worker_slug(slug)
        seen[slug] = worker_budget.current_worker_budget()
        set_worker_slug("")

    threads = [threading.Thread(target=_worker, args=(slug,)) for slug in ("story-a", "story-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen["story-a"] is a
    assert seen["story-b"] is b
    # The test thread never set a slug of its own and still sees nothing.
    assert worker_budget.current_worker_budget() is None


# ── Remaining working time ───────────────────────────────────────────────────


def test_remaining_counts_down_with_elapsed_working_time() -> None:
    budget = worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)

    assert budget.remaining(now=0.0) == 3600.0
    assert budget.remaining(now=1000.0) == 2600.0
    assert budget.remaining(now=4000.0) == -400.0  # overrun is reported, not floored


def test_remaining_excludes_banked_operator_wait_time() -> None:
    """The whole point: a wait the system chose to begin is not spent budget."""
    budget = worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)
    budget.operator_wait_seconds = 1200.0

    # 2000s of wall clock elapsed, 1200s of it waiting on an operator.
    assert budget.remaining(now=2000.0) == 2800.0


def test_remaining_seconds_returns_none_for_an_unregistered_slug() -> None:
    """None, not 0.0 — a caller must be able to tell "no budget" from "no time"."""
    assert worker_budget.remaining_seconds("nobody") is None
    worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)
    assert worker_budget.remaining_seconds("story-a", now=600.0) == 3000.0


# ── Operator-wait ledger ─────────────────────────────────────────────────────


def test_credit_covers_an_in_flight_wait_not_only_completed_ones() -> None:
    """A deadline check landing mid-wait must see the wait it landed in."""
    budget = worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)
    assert budget.credit(now=500.0) == 0.0

    budget.wait_started_at = 100.0
    assert budget.credit(now=500.0) == 400.0

    budget.operator_wait_seconds = 50.0
    assert budget.credit(now=500.0) == 450.0


def test_operator_wait_banks_the_interval_and_clears_the_marker() -> None:
    budget = worker_budget.register_worker_budget("story-a", 3600, started_at=time.monotonic())
    set_worker_slug("story-a")

    with worker_budget.operator_wait("ESCALATE") as already_credited:
        assert already_credited == 0.0
        assert budget.waiting() is True
        assert budget.wait_label == "ESCALATE"
        time.sleep(0.05)

    assert budget.waiting() is False
    assert budget.wait_label == ""
    assert budget.operator_wait_seconds >= 0.05


def test_operator_wait_closes_the_wait_when_the_block_raises() -> None:
    """A gate that blows up must not leave the story permanently "waiting"."""
    budget = worker_budget.register_worker_budget("story-a", 3600, started_at=time.monotonic())
    set_worker_slug("story-a")

    with pytest.raises(RuntimeError):
        with worker_budget.operator_wait("HUMAN_REVIEW"):
            raise RuntimeError("gate exploded")

    assert budget.waiting() is False
    assert budget.operator_wait_seconds >= 0.0


def test_operator_wait_is_a_no_op_without_an_enclosing_budget() -> None:
    """``forge run`` polls the same pending file with no scheduler behind it."""
    with worker_budget.operator_wait("PLAN_REVIEW") as already_credited:
        assert already_credited == 0.0

    assert worker_budget.operator_wait_credit("story-a") == 0.0


def test_operator_wait_accepts_an_explicit_budget() -> None:
    """Callers off the slugged thread can still mark a wait against a story."""
    budget = worker_budget.register_worker_budget("story-a", 3600, started_at=time.monotonic())

    assert worker_budget.current_worker_budget() is None
    with worker_budget.operator_wait("ESCALATE", budget=budget):
        assert budget.waiting() is True
    assert budget.waiting() is False


def test_a_nested_wait_does_not_restart_the_outer_interval() -> None:
    budget = worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)
    budget.begin_wait("ESCALATE")
    started = budget.wait_started_at

    budget.begin_wait("HUMAN_REVIEW")
    assert budget.wait_started_at == started
    assert budget.wait_label == "ESCALATE"

    assert budget.end_wait() >= 0.0
    # Closing a wait that is not open banks nothing rather than double-counting.
    assert budget.end_wait() == 0.0


def test_operator_wait_credit_is_zero_for_an_unregistered_slug() -> None:
    assert worker_budget.operator_wait_credit("nobody") == 0.0


# ── Batch groups share one window ────────────────────────────────────────────


def test_group_members_pool_their_operator_wait_credit() -> None:
    """One thread serves a batch, so a member's wait must credit them all."""
    leader = worker_budget.register_worker_budget(
        "story-a", 5400, group="batch:g1", started_at=0.0
    )
    member = worker_budget.register_worker_budget(
        "story-b", 5400, group="batch:g1", started_at=0.0
    )
    outsider = worker_budget.register_worker_budget("story-c", 3600, started_at=0.0)

    leader.operator_wait_seconds = 300.0
    member.operator_wait_seconds = 120.0
    outsider.operator_wait_seconds = 999.0

    assert worker_budget.operator_wait_credit("story-a") == 420.0
    assert worker_budget.operator_wait_credit("story-b") == 420.0
    # An ungrouped story credits only itself.
    assert worker_budget.operator_wait_credit("story-c") == 999.0


def test_a_members_remaining_time_credits_the_wait_a_peer_banked() -> None:
    """The pooled credit has to reach ``remaining``, not only the deadline check.

    A member reaching a gate after a peer already waited computes its remaining
    time against elapsed clock that *includes* the peer's wait. Crediting only
    its own ledger would have it pay for a wait it did not take — and be offered
    nothing — on a story whose deadline had not moved.
    """
    leader = worker_budget.register_worker_budget(
        "story-a", 5400, group="batch:g1", started_at=0.0
    )
    member = worker_budget.register_worker_budget(
        "story-b", 5400, group="batch:g1", started_at=0.0
    )
    leader.operator_wait_seconds = 1800.0  # the leader sat at a gate for 30m

    # 2000s of wall clock gone, 1800s of it the leader's gate wait. Both members
    # are measured against the same window, so both see the same working time.
    assert member.remaining(now=2000.0) == 5200.0
    assert leader.remaining(now=2000.0) == 5200.0
    assert worker_budget.remaining_seconds("story-b", now=2000.0) == 5200.0


def test_an_ungrouped_story_pools_only_its_own_credit() -> None:
    solo = worker_budget.register_worker_budget("story-a", 3600, started_at=0.0)
    worker_budget.register_worker_budget(
        "story-b", 3600, started_at=0.0
    ).operator_wait_seconds = 900.0
    solo.operator_wait_seconds = 100.0

    assert solo.pooled_credit(now=1000.0) == 100.0
    assert solo.remaining(now=1000.0) == 2700.0


def test_pooled_credit_still_counts_a_member_dropped_from_the_registry() -> None:
    """A member unregistered at completion keeps accounting for its own ledger."""
    leader = worker_budget.register_worker_budget(
        "story-a", 5400, group="batch:g1", started_at=0.0
    )
    member = worker_budget.register_worker_budget(
        "story-b", 5400, group="batch:g1", started_at=0.0
    )
    leader.operator_wait_seconds = 300.0
    member.operator_wait_seconds = 120.0

    worker_budget.unregister_worker_budget("story-b")

    assert leader.pooled_credit(now=1000.0) == 300.0
    # The dropped member is not in the registry but still knows its own peers.
    assert member.pooled_credit(now=1000.0) == 420.0


def test_waiting_on_operator_reports_the_peer_actually_blocked() -> None:
    worker_budget.register_worker_budget("story-a", 5400, group="batch:g1", started_at=0.0)
    member = worker_budget.register_worker_budget(
        "story-b", 5400, group="batch:g1", started_at=0.0
    )

    assert worker_budget.waiting_on_operator("story-a") == (False, "", 0.0)

    member.begin_wait("ESCALATE")
    waiting, label, waited = worker_budget.waiting_on_operator("story-a")
    assert waiting is True
    assert label == "ESCALATE"
    assert waited >= 0.0

    member.end_wait()
    assert worker_budget.waiting_on_operator("story-a")[0] is False


def test_waiting_on_operator_is_false_for_an_unregistered_slug() -> None:
    assert worker_budget.waiting_on_operator("nobody") == (False, "", 0.0)
