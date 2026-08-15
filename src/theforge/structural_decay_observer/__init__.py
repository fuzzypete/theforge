"""Spike POC (#2348): rank modules by controlled excess spend, not by size.

**This is a spike artifact, not a shipped component.** Nothing in the coordinator
imports it, it emits no issues, it blocks nothing, and it is invoked only by hand::

    python -m theforge.structural_decay_observer [--since 2026-06-01] [--top 15]

The question it exists to answer is in
``docs/plans/2348-structural-decay-observer-spike.md``: can the audit substrate
rank structural decay *better than ``wc -l``*? ADR-0008 withdrew the module-size
ratchet because a story-scoped gate can only enforce a proxy, and the proxy is
what gets optimised. Re-deriving the same ranking from richer data would recreate
the withdrawn metric, so this POC is written to be able to fail that comparison
visibly — see :func:`~theforge.structural_decay_observer.ranking.compare_to_line_counts`.

The one methodological commitment worth stating up front: **participation is not
causation**. A $40 run touching ten files does not attribute $40 to each of them.
Every cost here is *attributed* (run cost split across the files the run changed)
and then *controlled* (compared against runs of like complexity, dev model, panel
size and intended size that did **not** touch the path). The ranked quantity is
the residual — excess spend — and each entry names the weakest thing holding it
up, because a candidate that cannot say what is shaky about it cannot be argued
with.

Read-only by construction: it opens the substrate through
:func:`~theforge.coordinator.audit_storage.open_readonly` and issues SELECTs.
It never rebuilds, migrates, or writes. If the index is absent it says so and
tells the operator to run ``forge audits rebuild`` rather than doing it for them.

Layout: :mod:`.ranking` is the pure math (no I/O, testable against seeded rows),
:mod:`.report` is substrate loading, rendering and the CLI.
"""

from __future__ import annotations

from .ranking import (
    MIN_COHORT_RUNS,
    MIN_JOINABLE_RUNS,
    MIN_RUN_COVERAGE,
    MIN_TOUCHING_RUNS,
    Candidate,
    Comparison,
    Control,
    RunFacts,
    build_runs,
    compare_to_line_counts,
    rank_candidates,
    resolve_controls,
    threshold_status,
)
from .report import load_report, main, render

__all__ = [
    "MIN_COHORT_RUNS",
    "MIN_JOINABLE_RUNS",
    "MIN_RUN_COVERAGE",
    "MIN_TOUCHING_RUNS",
    "Candidate",
    "Comparison",
    "Control",
    "RunFacts",
    "build_runs",
    "compare_to_line_counts",
    "load_report",
    "main",
    "rank_candidates",
    "render",
    "resolve_controls",
    "threshold_status",
]
