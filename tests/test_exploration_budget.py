"""Durable per-sprint exploration budget ledger (#325, clause-8 "bounded")."""

from __future__ import annotations

from theforge.coordinator import exploration_budget as eb


def test_no_sprint_boundary_disables_exploration(tmp_path):
    # No sprint name → no durable boundary → exploration inactive (None budget).
    assert eb.remaining_budget(tmp_path, None, 1) is None
    assert eb.consumed_count(tmp_path, None) == 0
    assert eb.record_exploration(tmp_path, None, {"x": 1}) is False


def test_cap_zero_disables_even_with_sprint(tmp_path):
    assert eb.remaining_budget(tmp_path, "s1", 0) is None


def test_remaining_decrements_as_slots_consumed(tmp_path):
    assert eb.remaining_budget(tmp_path, "s1", 2) == 2
    assert eb.record_exploration(tmp_path, "s1", {"story": "a"}) is True
    assert eb.consumed_count(tmp_path, "s1") == 1
    assert eb.remaining_budget(tmp_path, "s1", 2) == 1
    eb.record_exploration(tmp_path, "s1", {"story": "b"})
    assert eb.remaining_budget(tmp_path, "s1", 2) == 0
    # Never goes negative even if over-consumed.
    eb.record_exploration(tmp_path, "s1", {"story": "c"})
    assert eb.remaining_budget(tmp_path, "s1", 2) == 0


def test_ledger_is_per_sprint(tmp_path):
    eb.record_exploration(tmp_path, "s1", {"story": "a"})
    assert eb.consumed_count(tmp_path, "s2") == 0
    assert eb.remaining_budget(tmp_path, "s2", 1) == 1


def test_ledger_lands_under_gitignored_forge_dir(tmp_path):
    eb.record_exploration(tmp_path, "s1", {"story": "a"})
    assert eb.ledger_path(tmp_path, "s1").is_relative_to(tmp_path / ".forge")
