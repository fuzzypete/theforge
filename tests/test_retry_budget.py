"""Tests for RetryBudget: unified per-cycle dev iteration counter."""

from __future__ import annotations

from theforge.coordinator.retry_budget import RetryBudget, RetryBudgetConsumption


class TestRetryBudgetConsume:
    def test_consume_increments_cycle_count(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        assert budget.cycle_count == 1

    def test_consume_increments_total_count(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        assert budget.total_count == 2

    def test_consume_appends_to_log(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=1)
        assert len(budget.consumption_log) == 1
        entry = budget.consumption_log[0]
        assert isinstance(entry, RetryBudgetConsumption)
        assert entry.cycle == 1
        assert entry.cycle_count == 1
        assert entry.total_count == 1

    def test_consume_log_carries_current_counts(self) -> None:
        budget = RetryBudget(max_iterations=5)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=1)
        assert budget.consumption_log[2].cycle == 1
        assert budget.consumption_log[2].cycle_count == 3
        assert budget.consumption_log[2].total_count == 3

    def test_consume_log_timestamp_is_string(self) -> None:
        budget = RetryBudget(max_iterations=2)
        budget.consume(review_cycle=0)
        assert isinstance(budget.consumption_log[0].timestamp, str)
        assert "T" in budget.consumption_log[0].timestamp  # ISO 8601 format

    def test_multiple_consumes_stack_log(self) -> None:
        budget = RetryBudget(max_iterations=5)
        for _ in range(3):
            budget.consume(review_cycle=0)
        assert len(budget.consumption_log) == 3


class TestRetryBudgetResetCycle:
    def test_reset_cycle_zeroes_cycle_count(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        budget.reset_cycle()
        assert budget.cycle_count == 0

    def test_reset_cycle_does_not_change_total_count(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        budget.reset_cycle()
        assert budget.total_count == 2

    def test_reset_cycle_does_not_clear_log(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        budget.reset_cycle()
        assert len(budget.consumption_log) == 1

    def test_consume_after_reset_increments_from_zero(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        budget.reset_cycle()
        budget.consume(review_cycle=1)
        assert budget.cycle_count == 1
        assert budget.total_count == 3

    def test_log_reflects_counts_after_reset_and_consume(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        budget.reset_cycle()
        budget.consume(review_cycle=1)
        entry = budget.consumption_log[-1]
        assert entry.cycle == 1
        assert entry.cycle_count == 1  # reset then +1
        assert entry.total_count == 2  # cumulative


class TestRetryBudgetReleaseUnused:
    def test_release_unused_decrements_counters_and_log(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)

        assert budget.release_unused() is True
        assert budget.cycle_count == 1
        assert budget.total_count == 1
        assert len(budget.consumption_log) == 1

    def test_release_unused_updates_remaining(self) -> None:
        budget = RetryBudget(max_iterations=2)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)

        budget.release_unused()

        assert budget.remaining() == 1

    def test_release_unused_on_empty_budget_is_noop(self) -> None:
        budget = RetryBudget(max_iterations=2)

        assert budget.release_unused() is False
        assert budget.cycle_count == 0
        assert budget.total_count == 0
        assert budget.consumption_log == []


class TestRetryBudgetIsExhausted:
    def test_not_exhausted_when_below_max(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        assert budget.is_exhausted() is False

    def test_exhausted_when_at_max(self) -> None:
        budget = RetryBudget(max_iterations=2)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        assert budget.is_exhausted() is True

    def test_exhausted_when_above_max(self) -> None:
        budget = RetryBudget(max_iterations=1)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        assert budget.is_exhausted() is True

    def test_not_exhausted_at_zero_count(self) -> None:
        budget = RetryBudget(max_iterations=3)
        assert budget.is_exhausted() is False

    def test_not_exhausted_after_reset(self) -> None:
        budget = RetryBudget(max_iterations=2)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        budget.reset_cycle()
        assert budget.is_exhausted() is False


class TestRetryBudgetRemaining:
    def test_remaining_equals_max_minus_cycle_count(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        assert budget.remaining() == 2

    def test_remaining_is_zero_when_exhausted(self) -> None:
        budget = RetryBudget(max_iterations=2)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        assert budget.remaining() == 0

    def test_remaining_never_negative(self) -> None:
        budget = RetryBudget(max_iterations=1)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)  # over the limit
        assert budget.remaining() == 0

    def test_remaining_resets_after_reset_cycle(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        budget.reset_cycle()
        assert budget.remaining() == 3


class TestRetryBudgetMultiCycle:
    def test_total_count_accumulates_across_cycles(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        budget.consume(review_cycle=0)
        budget.reset_cycle()
        budget.consume(review_cycle=1)
        assert budget.total_count == 3
        assert budget.cycle_count == 1

    def test_log_records_all_events_across_cycles(self) -> None:
        budget = RetryBudget(max_iterations=3)
        budget.consume(review_cycle=0)
        budget.reset_cycle()
        budget.consume(review_cycle=1)
        assert len(budget.consumption_log) == 2
        assert budget.consumption_log[0].cycle == 0
        assert budget.consumption_log[1].cycle == 1
