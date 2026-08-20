"""Plan-advisory resolution measurement (#2112).

Plan review approves plans while holding unresolved P1-level findings and hands
them to dev as advisory context. This package measures the one quantity that
policy turns on: how often the change that shipped addressed such a finding.

The measurement is read-only over the audit substrate for the mechanical half
(which findings existed, what each run cost) and a checked-in, hand-authored
judgment corpus for the half no field records (was the finding addressed, and of
what class). ``analysis`` is pure aggregation; ``report`` loads the substrate and
renders.
"""

from __future__ import annotations

from .analysis import (
    ESCAPED,
    EVIDENCE_UNAVAILABLE,
    FINDING_CLASSES,
    RESOLVED,
    CorpusMismatchError,
    analyze,
    extract_plan_findings,
    finding_key,
)

__all__ = [
    "ESCAPED",
    "EVIDENCE_UNAVAILABLE",
    "FINDING_CLASSES",
    "RESOLVED",
    "CorpusMismatchError",
    "analyze",
    "extract_plan_findings",
    "finding_key",
]
