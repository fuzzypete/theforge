"""Spike POC (#1848): reviewer finding-fate rates from structured audit records.

**This is a spike artifact, not a shipped component.** Nothing in the
coordinator imports it, it emits no routing decision, and it is invoked only by
hand::

    python -m theforge.finding_fate_proxy --project-root /path/to/repo

The question it exists to answer is recorded in
``docs/plans/1848-reviewer-finding-fate-spike.md``: does the coordinator already
write enough structured evidence to compute a routing-safe reviewer reliability
proxy from the downstream fate of P1 findings?

Read-only by construction: it opens the audit substrate through
``coordinator.audit_storage.open_readonly()``, iterates records through
``coordinator.audit_read_model.iter_records()``, and applies ADR-0006 clause-2
gates with the existing recency-weighting helpers from
``model_profiles_read_model``. It never writes, rebuilds, or migrates the
substrate.

Layout: :mod:`.analysis` is the pure derivation and aggregation logic,
:mod:`.report` is substrate loading, rendering and the CLI.
"""

from __future__ import annotations

from .analysis import (
    CONTRADICTED_MISSING_EVENT,
    DISMISSED_MISSING_EVENT,
    analyze_records,
    derive_finding_fate,
)
from .report import load_report, main, render

__all__ = [
    "CONTRADICTED_MISSING_EVENT",
    "DISMISSED_MISSING_EVENT",
    "analyze_records",
    "derive_finding_fate",
    "load_report",
    "main",
    "render",
]
