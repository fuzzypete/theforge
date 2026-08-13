"""RetryBudget: unified per-cycle and cumulative dev iteration counter.

Stdlib-only imports. No coordinator or runner dependencies.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field


@dataclass
class RetryBudgetConsumption:
    """Record of a single consume() call, for audit trail."""

    cycle: int  # state.review_cycle at time of consume()
    cycle_count: int  # cycle_count AFTER increment
    total_count: int  # total_count AFTER increment
    timestamp: str  # ISO 8601 UTC


@dataclass
class RetryBudget:
    """Owns per-cycle and cumulative dev iteration counts.

    Replaces _dev_calls_this_cycle (local var in engine.py) and dev_iteration
    (int field in CoordinatorState). All mutation goes through consume() or
    reset_cycle(); every consumption is appended to consumption_log.

    max_iterations  — per-cycle budget limit; set from
                      config.retry.max_dev_iterations before the loop starts
    cycle_count     — dev calls in the current review cycle; reset by reset_cycle()
    total_count     — monotonically increasing across all review cycles; never reset
    consumption_log — one entry per consume() call
    """

    max_iterations: int = 0
    cycle_count: int = 0
    total_count: int = 0
    consumption_log: list[RetryBudgetConsumption] = field(default_factory=list)

    def consume(self, *, review_cycle: int) -> None:
        """Increment both counters and record the consumption event."""
        self.cycle_count += 1
        self.total_count += 1
        self.consumption_log.append(
            RetryBudgetConsumption(
                cycle=review_cycle,
                cycle_count=self.cycle_count,
                total_count=self.total_count,
                timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
        )

    def release_unused(self) -> bool:
        """Undo the most recent consume() when that reserved slot was never used."""
        if not self.consumption_log or self.total_count <= 0:
            return False
        self.consumption_log.pop()
        self.total_count = max(0, self.total_count - 1)
        self.cycle_count = max(0, self.cycle_count - 1)
        return True

    def reset_cycle(self) -> None:
        """Zero the per-cycle counter. Call when a new review cycle starts."""
        self.cycle_count = 0

    def is_exhausted(self) -> bool:
        """Return True when cycle_count >= max_iterations."""
        return self.cycle_count >= self.max_iterations

    def remaining(self) -> int:
        """Return remaining dev calls allowed in this review cycle."""
        return max(0, self.max_iterations - self.cycle_count)
