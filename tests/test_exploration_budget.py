"""Durable, atomic per-sprint exploration budget ledger (#325, clause-8 "bounded").

The cap must hold exactly even when parallel sprint workers race to consume the
last slot — so the consume is an ``O_CREAT | O_EXCL`` per-index claim, not a
read-count-then-append (which has a TOCTOU race the review flagged).
"""

from __future__ import annotations

import concurrent.futures

from theforge.coordinator import exploration_budget as eb


def test_no_sprint_boundary_disables_exploration(tmp_path):
    # No sprint name → no durable boundary → exploration inactive (None budget).
    assert eb.remaining_budget(tmp_path, None, 1) is None
    assert eb.consumed_count(tmp_path, None) == 0
    assert eb.reserve_slot(tmp_path, None, 1, {"x": 1}) is False


def test_cap_zero_disables_even_with_sprint(tmp_path):
    assert eb.remaining_budget(tmp_path, "s1", 0) is None
    assert eb.reserve_slot(tmp_path, "s1", 0, {"x": 1}) is False


def test_reserve_consumes_and_remaining_decrements(tmp_path):
    assert eb.remaining_budget(tmp_path, "s1", 2) == 2
    assert eb.reserve_slot(tmp_path, "s1", 2, {"story": "a"}) is True
    assert eb.consumed_count(tmp_path, "s1") == 1
    assert eb.remaining_budget(tmp_path, "s1", 2) == 1
    assert eb.reserve_slot(tmp_path, "s1", 2, {"story": "b"}) is True
    assert eb.remaining_budget(tmp_path, "s1", 2) == 0
    # Cap reached → further reservations refused (bound holds).
    assert eb.reserve_slot(tmp_path, "s1", 2, {"story": "c"}) is False
    assert eb.consumed_count(tmp_path, "s1") == 2


def test_cap_one_allows_exactly_one(tmp_path):
    assert eb.reserve_slot(tmp_path, "s1", 1, {"story": "a"}) is True
    assert eb.reserve_slot(tmp_path, "s1", 1, {"story": "b"}) is False


def test_ledger_is_per_sprint(tmp_path):
    eb.reserve_slot(tmp_path, "s1", 1, {"story": "a"})
    assert eb.consumed_count(tmp_path, "s2") == 0
    assert eb.remaining_budget(tmp_path, "s2", 1) == 1


def test_slots_land_under_gitignored_forge_dir(tmp_path):
    eb.reserve_slot(tmp_path, "s1", 1, {"story": "a"})
    assert eb.slot_dir(tmp_path, "s1").is_relative_to(tmp_path / ".forge")


def test_concurrent_reservation_respects_cap_of_one(tmp_path):
    """Many workers race for a cap-1 sprint; the O_EXCL claim lets exactly one win."""
    cap = 1
    workers = 12

    def _try(i: int) -> bool:
        return eb.reserve_slot(tmp_path, "sprint", cap, {"worker": i})

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_try, range(workers)))

    assert sum(1 for r in results if r) == cap  # exactly one winner
    assert eb.consumed_count(tmp_path, "sprint") == cap


def test_concurrent_reservation_respects_cap_of_three(tmp_path):
    cap = 3
    workers = 20

    def _try(i: int) -> bool:
        return eb.reserve_slot(tmp_path, "sprint", cap, {"worker": i})

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_try, range(workers)))

    assert sum(1 for r in results if r) == cap
    assert eb.consumed_count(tmp_path, "sprint") == cap
