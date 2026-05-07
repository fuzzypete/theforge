"""Escalation history derived from the SQLite audit substrate.

Adaptive routing's promotion logic needs the recent run-by-run outcome
sequence per (complexity, dev_model) to decide whether a model has earned
its current tier. Pre-substrate, this lived in
``.forge/assignment_history.yaml``. Post-migration, the canonical record
is the audit substrate — escalation records are a projection of audit
records, not a separate file.
"""

from __future__ import annotations

import logging
from pathlib import Path

from theforge.assignment import EscalationRecord
from theforge.coordinator import audit_substrate

log = logging.getLogger(__name__)


def load_escalation_history_from_substrate(project_root: Path) -> list[EscalationRecord]:
    """Return escalation records derived from substrate audit rows.

    Returns an empty list when the substrate is missing or unreadable —
    adaptive routing degrades to its no-history defaults rather than
    failing the run. (Operator surface: ``forge check-config`` reports a
    missing/corrupt substrate; runtime simply behaves as a fresh repo.)
    """
    sub_path = audit_substrate.substrate_path(project_root)
    if not sub_path.exists():
        if not audit_substrate.has_audit_inputs(project_root):
            return []
        log.warning(
            "[adaptive] audit substrate missing at %s; routing without history. "
            "Run `forge audits rebuild [--include-legacy-history]` to backfill.",
            sub_path,
        )
        return []
    try:
        conn = audit_substrate.require_substrate(project_root)
    except (audit_substrate.SubstrateMissingError, audit_substrate.SubstrateCorruptError) as exc:
        log.warning("[adaptive] could not open audit substrate (%s); routing without history", exc)
        return []
    try:
        out: list[EscalationRecord] = []
        for entry in audit_substrate.iter_escalation_records(conn):
            out.append(
                EscalationRecord(
                    story=str(entry.get("story") or ""),
                    complexity=str(entry.get("complexity") or ""),
                    dev_model=str(entry.get("dev_model") or ""),
                    outcome=str(entry.get("outcome") or ""),
                    reason=str(entry.get("reason") or ""),
                    timestamp=str(entry.get("timestamp") or ""),
                    complexity_score=entry.get("complexity_score"),
                )
            )
        return out
    finally:
        conn.close()
