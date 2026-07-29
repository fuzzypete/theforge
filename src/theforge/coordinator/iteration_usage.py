"""Per-cycle dev iteration usage, shared by coordinator and sprint audit.

Stdlib-only imports, duck-typed on the state object, so both the live
coordinator state and a rehydrated sprint-level state can be read through the
same helper and cannot drift apart.

``used`` and ``max`` must be on the same scale. ``max`` (``adaptive_dev_max`` /
``config.retry.max_dev_iterations``) is a **per-cycle** budget limit, so ``used``
must be per-cycle too. Counting ``len(state.dev_iteration_telemetry)`` reported
a cumulative, never-reset, whole-story total against that per-cycle cap, so any
story spanning more than one review cycle read as a budget overrun that never
happened (#1985).

The per-cycle figure reported is the *peak* single-cycle usage: each telemetry
entry's ``iteration`` is ``budget.cycle_count`` at the time the dev call ran
(1-based, reset at every review-cycle boundary), so the largest one is the
closest any single cycle came to the cap. The live ``budget.cycle_count`` acts
as a floor for the in-flight cycle, whose dev call may have consumed budget
before any telemetry was recorded.
"""

from __future__ import annotations


def peak_dev_cycle_usage(state: object) -> int:
    """Return the highest dev-iteration count reached in any single review cycle."""
    peak = 0
    for item in list(getattr(state, "dev_iteration_telemetry", []) or []):
        iteration = getattr(item, "iteration", None)
        if isinstance(iteration, int) and iteration > peak:
            peak = iteration
    cycle_count = getattr(getattr(state, "budget", None), "cycle_count", None)
    if isinstance(cycle_count, int) and cycle_count > peak:
        peak = cycle_count
    return peak


def dev_usage(state: object, *, default_max: int | None = None) -> tuple[int, int | None]:
    """Return ``(peak_per_cycle_used, per_cycle_cap)`` for a story's dev budget.

    The cap prefers the adaptive value, falls back to the cap recorded on the
    first dev telemetry entry, then to ``default_max`` (the configured limit for
    callers that have a ``ForgeConfig``; ``None`` for those that do not).
    """
    used = peak_dev_cycle_usage(state)
    cap = getattr(state, "adaptive_dev_max", None)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        telemetry = list(getattr(state, "dev_iteration_telemetry", []) or [])
        cap = getattr(telemetry[0], "max_iterations", None) if telemetry else None
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        cap = default_max
    return used, cap
