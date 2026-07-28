"""Adaptive gate-timeout resolver.

Computes an effective per-gate timeout from the static baseline and runtime
contention conditions (host CPU count, gate CPU demand, sprint --parallel N).
Pure function, stdlib-only — easy to unit-test in isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

VALID_GATE_TIMEOUT_SCALES = frozenset({"adaptive", "fixed"})


@dataclass(frozen=True)
class GateTimeoutResolution:
    """Result of resolving the effective gate timeout for a sprint run."""

    effective_timeout: int
    baseline: int
    factor: float
    overcommit: bool
    mode: str
    host_cores: int
    gate_cpu_cores: int
    max_parallel: int
    reason: str
    running_stories: int = 0
    actual_parallel: int = 0


def resolve_effective_gate_timeout(
    baseline: int,
    max_parallel: int,
    host_cores: int,
    gate_cpu_cores: int | None,
    mode: str,
    running_stories: int = 0,
) -> GateTimeoutResolution:
    """Resolve the effective gate timeout given contention conditions.

    Formula:
        gate_demand_cores = gate_cpu_cores or host_cores
        actual_parallel   = max_parallel + running_stories
        demand            = gate_demand_cores * actual_parallel
        factor            = max(1.0, demand / host_cores)
        effective         = baseline if mode == "fixed" else ceil(baseline * factor)
        overcommit        = demand > host_cores * 1.5

    ``running_stories`` is the count of stories already executing when the
    timeout is resolved — non-zero only on a continuation (a mid-run re-exec
    inherits live agent process groups). Those agents contend with the gate just
    as this generation's own workers do, and a limit derived from configured
    ``--parallel`` alone under-counts them: exceeding a timeout that ignored half
    the load is a measurement of contention, not evidence that the tree is
    broken. At the default ``0`` the formula is exactly the fresh-start one.
    """
    safe_host = max(1, host_cores)
    safe_parallel = max(1, max_parallel)
    safe_running = max(0, int(running_stories or 0))
    actual_parallel = safe_parallel + safe_running
    gate_demand_cores = gate_cpu_cores if gate_cpu_cores and gate_cpu_cores > 0 else safe_host
    demand = gate_demand_cores * actual_parallel
    factor = max(1.0, demand / safe_host)
    normalized_mode = str(mode)
    if normalized_mode not in VALID_GATE_TIMEOUT_SCALES:
        raise ValueError(
            f"gate_timeout_scale must be 'adaptive' or 'fixed', got {normalized_mode!r}"
        )
    if normalized_mode == "fixed":
        effective = int(baseline)
    else:
        effective = int(math.ceil(baseline * factor))
    overcommit = demand > safe_host * 1.5
    # Field order is appended-to, never reordered: the reason line is an
    # operator-facing diagnostic that is grepped and asserted on by prefix.
    reason = (
        f"baseline={baseline}s mode={normalized_mode} parallel={safe_parallel} "
        f"gate_cpu_cores={gate_demand_cores} host_cores={safe_host} "
        f"demand={demand} factor={factor:.2f} effective={effective}s "
        f"running_stories={safe_running} actual_parallel={actual_parallel}"
    )
    return GateTimeoutResolution(
        effective_timeout=effective,
        baseline=int(baseline),
        factor=factor,
        overcommit=overcommit,
        mode=normalized_mode,
        host_cores=safe_host,
        gate_cpu_cores=gate_demand_cores,
        max_parallel=safe_parallel,
        reason=reason,
        running_stories=safe_running,
        actual_parallel=actual_parallel,
    )
