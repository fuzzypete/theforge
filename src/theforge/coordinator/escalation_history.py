"""Escalation history derived from the SQLite audit substrate.

Adaptive routing's promotion logic needs the recent run-by-run outcome
sequence per (complexity, dev_model) to decide whether a model has earned
its current tier. Pre-substrate, this lived in
``.forge/assignment_history.yaml``. Post-migration, the canonical record
is the audit substrate — escalation records are a projection of audit
records, not a separate file.
"""

from __future__ import annotations

from pathlib import Path

from theforge.assignment import EscalationRecord
from theforge.coordinator import audit_substrate


def load_escalation_history_from_substrate(project_root: Path) -> list[EscalationRecord]:
    """Return escalation records derived from substrate audit rows.

    Behavior:
      - Truly fresh repo (no substrate file *and* no legacy/per-run audit
        inputs) → return ``[]``. Adaptive routing has no history to consume
        and proceeds with its tier-only defaults.
      - Substrate present and valid → project escalation rows from the
        audit table.
      - Substrate missing or corrupt while audit inputs exist → propagate
        ``SubstrateMissingError`` / ``SubstrateCorruptError``. The spec
        requires a clear operator-facing error, not silent no-history
        routing, when historical data is supposed to be readable.

    The "audit inputs exist" branch is the load-bearing one: if the
    operator already has runs/*.json or a legacy history.jsonl on disk,
    routing without that data is a wrong answer, not a degraded-but-OK
    answer. Surfacing the substrate exception lets the caller (preflight)
    abort the run rather than make a model-routing decision against a
    silently-empty history.
    """
    sub_path = audit_substrate.substrate_path(project_root)
    if not sub_path.exists() and not audit_substrate.has_audit_inputs(project_root):
        return []
    conn = audit_substrate.require_substrate(project_root)
    try:
        out: list[EscalationRecord] = []
        for entry in audit_substrate.derive_assignment_history(conn):
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
