"""Adaptive gate-timeout resolver.

Computes an effective per-gate timeout from the static baseline and runtime
contention conditions (host CPU count, gate CPU demand, sprint --parallel N).
Pure function, stdlib-only — easy to unit-test in isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


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


def resolve_effective_gate_timeout(
    baseline: int,
    max_parallel: int,
    host_cores: int,
    gate_cpu_cores: int | None,
    mode: str,
) -> GateTimeoutResolution:
    """Resolve the effective gate timeout given contention conditions.

    Formula:
        gate_demand_cores = gate_cpu_cores or host_cores
        demand            = gate_demand_cores * max_parallel
        factor            = max(1.0, demand / host_cores)
        effective         = baseline if mode == "fixed" else ceil(baseline * factor)
        overcommit        = demand > host_cores * 1.5
    """
    safe_host = max(1, host_cores)
    safe_parallel = max(1, max_parallel)
    gate_demand_cores = gate_cpu_cores if gate_cpu_cores and gate_cpu_cores > 0 else safe_host
    demand = gate_demand_cores * safe_parallel
    factor = max(1.0, demand / safe_host)
    normalized_mode = mode if mode in ("adaptive", "fixed") else "adaptive"
    if normalized_mode == "fixed":
        effective = int(baseline)
    else:
        effective = int(math.ceil(baseline * factor))
    overcommit = demand > safe_host * 1.5
    reason = (
        f"baseline={baseline}s mode={normalized_mode} parallel={safe_parallel} "
        f"gate_cpu_cores={gate_demand_cores} host_cores={safe_host} "
        f"demand={demand} factor={factor:.2f} effective={effective}s"
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
    )
