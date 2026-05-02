"""Mechanical (no-LLM) patches for intake findings.

A finding is mechanical if it can be remediated by a deterministic edit:
adding a label, inserting a missing section header, etc. Anything else
is semantic and must be handed to the agent pass.

Stdlib only.
"""

from __future__ import annotations

from .findings import FixType, IntakeFinding


def _patch_missing_type(labels: list[str]) -> list[str]:
    # We can't know the right type without inspecting content, so the safe
    # mechanical fix is to add the catch-all ``task`` label so shape gate's
    # missing_type check no longer trips. Refining the type is a semantic
    # concern handled by the agent pass when ``auto_fix: true``.
    if any(label.lower() in {"bug", "enhancement", "task", "epic"} for label in labels):
        return labels
    return labels + ["task"]


def apply_mechanical_fixes(
    body: str,
    labels: list[str],
    findings: list[IntakeFinding],
) -> tuple[str, list[str], list[IntakeFinding], list[IntakeFinding]]:
    """Apply mechanical patches to ``body``/``labels``.

    Returns a 4-tuple:

    - new body (may equal input)
    - new labels (may equal input)
    - findings consumed by mechanical patches
    - findings left over (semantic, or mechanical with no patch available)
    """
    new_body = body
    new_labels = list(labels)
    consumed: list[IntakeFinding] = []
    remaining: list[IntakeFinding] = []

    for finding in findings:
        if finding.fix_type is not FixType.MECHANICAL:
            remaining.append(finding)
            continue
        if finding.code == "missing_type":
            new_labels = _patch_missing_type(new_labels)
            consumed.append(finding)
            continue
        # Mechanical code with no registered patch — keep it pending.
        remaining.append(finding)

    return new_body, new_labels, consumed, remaining
