"""The receipt distribution over a window of runs (#2866).

Aggregation and presentation for :mod:`theforge.knowledge_receipts`. The rules
here are editorial and load-bearing, not stylistic:

- **Corroborated and uncorroborated use claims are never summed.** They are
  different populations — one names a consequence the record contains, the other
  names one it does not — and a total would assert a fact neither supports.
- **Nothing is labelled verified, confirmed, or effective use.** Resolving a
  pointer establishes existence, not causation.
- **Absence is never rendered as "unused".** A phase that received nothing had
  nothing to debrief; a phase that received claims and returned no debrief is
  undebriefed; a claim no debrief named is unaddressed. Three different silences,
  three different lines.
- **Every rendering carries the disclaimer, and the report draws no conclusion.**
  The ratio at the bottom is arithmetic. What it means is the operator's call.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from theforge.knowledge_receipts import (
    INTERPRETATION_NOTE,
    NON_USE_DISPOSITIONS,
    OUTCOME_CORROBORATED_USE,
    OUTCOME_UNCORROBORATED_USE,
    STATUS_UNCOMPARABLE,
)

_COUNT_KEYS = (
    "phases_with_injected_knowledge",
    "phases_debriefed",
    "phases_undebriefed",
    "phases_nothing_to_debrief",
    "claims_injected",
    OUTCOME_CORROBORATED_USE,
    OUTCOME_UNCORROBORATED_USE,
    *NON_USE_DISPOSITIONS,
    "unaddressed_claims",
    "unmatched_citations",
    "unrecognised_dispositions",
)

_LABELS = {
    OUTCOME_CORROBORATED_USE: "corroborated use claims",
    OUTCOME_UNCORROBORATED_USE: "uncorroborated use claims",
    "confirmed_approach": "confirmed existing approach",
    "already_known": "already known",
    "irrelevant": "irrelevant",
    "stale_or_wrong": "stale or wrong",
    "unaddressed_claims": "unaddressed",
    "unmatched_citations": "unmatched citations",
    "unrecognised_dispositions": "unrecognised dispositions",
}

_DISCLAIMER = "No effectiveness or ROI conclusion follows."


def build_receipt_distribution(records: Sequence[Mapping[str, Any]] | None) -> dict:
    """Sum every run's receipt block in the window into one distribution.

    Runs that predate the instrument are counted separately as *uncomparable*
    rather than folded in as zeroes: a run nobody asked contributes no evidence
    either way, and letting it dilute the denominator would answer the question
    with the software's release date.
    """
    totals = dict.fromkeys(_COUNT_KEYS, 0)
    runs_counted = 0
    runs_uncomparable = 0
    runs_without_block = 0

    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        block = record.get("knowledge_receipts")
        if not isinstance(block, Mapping):
            runs_without_block += 1
            continue
        if block.get("status") == STATUS_UNCOMPARABLE:
            runs_uncomparable += 1
            continue
        counts = block.get("counts")
        if not isinstance(counts, Mapping):
            runs_uncomparable += 1
            continue
        runs_counted += 1
        for key in _COUNT_KEYS:
            value = counts.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] += value

    return {
        "runs_counted": runs_counted,
        "runs_uncomparable": runs_uncomparable,
        "runs_without_receipt_block": runs_without_block,
        "counts": totals,
        "interpretation": INTERPRETATION_NOTE,
        "disclaimer": _DISCLAIMER,
    }


def report_payload(distribution: Mapping[str, Any]) -> dict:
    """The structured view. Same numbers as the terminal view, same source."""
    return dict(distribution)


def render_terminal(distribution: Mapping[str, Any]) -> str:
    """Render the distribution as a table plus the disclaimer, and nothing else."""
    counts = distribution.get("counts") or {}
    lines = ["", "Prior-run knowledge receipts", ""]

    if not distribution.get("runs_counted"):
        lines.append(
            f"  no run in this window carries a receipt block "
            f"({_int(distribution.get('runs_uncomparable'))} predate the instrument, "
            f"{_int(distribution.get('runs_without_receipt_block'))} record none)"
        )
        lines.append("")
        lines.append(f"  {INTERPRETATION_NOTE}")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        f"  {'phases with injected knowledge':<32}"
        f"{_int(counts.get('phases_with_injected_knowledge')):>5}"
    )
    lines.append(
        f"    {'debriefed':<30}{_int(counts.get('phases_debriefed')):>5}"
        f"     undebriefed  {_int(counts.get('phases_undebriefed'))}"
    )
    lines.append(
        f"    {'nothing to debrief':<30}{_int(counts.get('phases_nothing_to_debrief')):>5}"
    )
    lines.append(f"  {'claims injected':<32}{_int(counts.get('claims_injected')):>5}")
    for key in (
        OUTCOME_CORROBORATED_USE,
        OUTCOME_UNCORROBORATED_USE,
        *NON_USE_DISPOSITIONS,
        "unaddressed_claims",
        "unmatched_citations",
        "unrecognised_dispositions",
    ):
        lines.append(f"    {_LABELS[key]:<30}{_int(counts.get(key)):>5}")

    lines.append("")
    lines.append(
        f"  Corroborated uptake claims: {_int(counts.get(OUTCOME_CORROBORATED_USE))} of "
        f"{_int(counts.get('claims_injected'))} exposed claims. {_DISCLAIMER}"
    )
    lines.append(f"  {INTERPRETATION_NOTE}")
    if distribution.get("runs_uncomparable") or distribution.get("runs_without_receipt_block"):
        lines.append(
            f"  ({_int(distribution.get('runs_uncomparable'))} run(s) predate the instrument "
            f"and {_int(distribution.get('runs_without_receipt_block'))} carry no receipt "
            "block; both are excluded rather than counted as zero)"
        )
    lines.append("")
    return "\n".join(lines)


def _int(value: Any) -> str:
    return "—" if value is None else str(value)
