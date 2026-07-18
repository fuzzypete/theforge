"""Seam coverage for shape-gate skip emission → substrate → postmortem block.

Issue #1453: the gate's skip partition must land in the substrate with taxonomy
categories (including remediation outcomes), and the summary/RCA block must
project this run's events with stuck-issue flags. This exercises the full
emission → query → block seam the sprint entry path relies on.
"""

from __future__ import annotations

from pathlib import Path

from theforge.coordinator.audit_substrate import (
    create_or_open,
    iter_shape_skip_events,
    record_shape_skip_event,
)
from theforge.sprint.shape_report import  # noqa: F401  (re-export check below)
    build_shape_gate_skip_block as _direct_import_guard  # type: ignore  # noqa: E999
