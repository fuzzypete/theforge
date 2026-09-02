"""Presentation for the prior-run uptake indicator (#2684).

Split from :mod:`theforge.knowledge_uptake` for the same reason
:mod:`theforge.knowledge_effectiveness_render` is split from its core: the
comparison stays a pure function of recorded artifacts, and the terminal view
cannot quietly disagree with the stored record.

The renderer's editorial rules are load-bearing, not stylistic:

- The unmatched bucket is always spelled *not matched to an eligible injected
  claim*. It is never called novel, new, or missed-by-the-loop — this matcher
  cannot establish novelty, only the absence of a correspondence it could find.
- Every block carries the missed-uptake-indicator sentence. A reader who takes
  one number out of context should still not be able to read it as a verdict on
  whether knowledge helped.
- A run with no eligible claims, or no findings, prints *that* rather than a
  correspondence block full of zeroes. "Nothing to compare" and "compared,
  found nothing" are different facts and must not render alike.
"""

from __future__ import annotations

from typing import Any, Mapping

from theforge.knowledge_uptake import (
    INTERPRETATION_NOTE,
    OUTCOME_INDETERMINATE,
    OUTCOME_MATCHED,
    OUTCOME_NOT_MATCHED,
    STATUS_COMPARED,
    STATUS_NO_ELIGIBLE_CLAIMS,
    STATUS_NO_REVIEW_FINDINGS,
    STATUS_UNCOMPARABLE,
    VALIDATION_MEASURED,
)

_MATCHED_LABEL = "corresponding to an eligible claim"
_NOT_MATCHED_LABEL = "not matched to an eligible injected claim"
_INDETERMINATE_LABEL = "indeterminate"


def render_run_uptake(report: Mapping[str, Any] | None) -> str:
    """Render one run's prior-run uptake block."""
    if not isinstance(report, Mapping):
        return ""
    lines = ["Prior-run uptake"]
    status = report.get("status")

    if status == STATUS_UNCOMPARABLE:
        lines.append("  uncomparable — run predates claim-exposure capture")
        lines.append(f"  review findings         {_int(report.get('review_findings'))}")
        lines.append(
            "  these findings are not reported as corresponding to nothing: what the "
            "agents were shown was never recorded"
        )
        lines.append("")
        lines.append(f"  {_method_line(report)}")
        lines.append(f"  {INTERPRETATION_NOTE}")
        return "\n".join(lines) + "\n"

    rendered = _int(report.get("claims_rendered"))
    eligible = _int(report.get("claims_eligible"))
    lines.append(f"  claims rendered         {rendered}{_recipients(report)}")
    lines.append(f"  claims eligible         {eligible}{_exclusions(report)}")
    lines.append(f"  review findings         {_int(report.get('review_findings'))}")

    if status == STATUS_NO_ELIGIBLE_CLAIMS:
        lines.append(
            "  no correspondence computed: "
            + _reason(report, "no eligible claim reached the author")
        )
    elif status == STATUS_NO_REVIEW_FINDINGS:
        lines.append(
            "  no correspondence computed: " + _reason(report, "the review recorded no findings")
        )
    elif status == STATUS_COMPARED:
        lines.extend(_correspondence_lines(report))

    lines.append("")
    lines.append(f"  {_method_line(report)}")
    lines.append(f"  {INTERPRETATION_NOTE}")
    return "\n".join(lines) + "\n"


def _reason(report: Mapping[str, Any], default: str) -> str:
    """The record's own reason for having nothing to compare.

    Read from the report rather than restated here: there is more than one way
    to end up with no eligible claim — none ever reached the author, or they all
    arrived after the last finding was recorded — and a renderer that hard-codes
    one of them tells the reader the wrong thing about the other.
    """
    note = report.get("note")
    if not isinstance(note, str) or not note.strip():
        return default
    return note.removesuffix("; nothing to compare").strip()


def _correspondence_lines(report: Mapping[str, Any]) -> list[str]:
    counts = report.get("counts") or {}
    correspondences = report.get("correspondences") or []
    lines = [
        f"    {_MATCHED_LABEL}   {_int(counts.get(OUTCOME_MATCHED))}",
    ]
    for item in correspondences:
        if not isinstance(item, Mapping) or item.get("outcome") != OUTCOME_MATCHED:
            continue
        lines.append(f"      -> {_claim_citation(item)}")
    lines.append(f"    {_NOT_MATCHED_LABEL}   {_int(counts.get(OUTCOME_NOT_MATCHED))}")
    lines.append(f"    {_INDETERMINATE_LABEL}   {_int(counts.get(OUTCOME_INDETERMINATE))}")
    return lines


def _claim_citation(item: Mapping[str, Any]) -> str:
    parts = []
    index = item.get("claim_index")
    if index is not None:
        parts.append(f"claim {index}")
    ref = item.get("claim_ref")
    if ref:
        parts.append(f"ref {ref}")
    run_id = item.get("claim_run_id")
    if run_id:
        parts.append(f"run {run_id}")
    role = item.get("claim_agent_role")
    iteration = item.get("claim_phase_iteration")
    if role and iteration is not None:
        parts.append(f"{role} iteration {iteration}")
    return ", ".join(parts) if parts else "claim reference unavailable"


def _recipients(report: Mapping[str, Any]) -> str:
    breakdown = report.get("claims_rendered_by_recipient") or []
    parts = [
        f"{entry.get('count')} to {entry.get('agent_role') or 'unattributed'} "
        f"iter {entry.get('phase_iteration')}"
        for entry in breakdown
        if isinstance(entry, Mapping) and entry.get("count")
    ]
    return f"   ({', '.join(parts)})" if parts else ""


def _exclusions(report: Mapping[str, Any]) -> str:
    excluded = report.get("claims_excluded") or []
    parts = [
        f"{entry.get('count')} {entry.get('reason')}"
        for entry in excluded
        if isinstance(entry, Mapping) and entry.get("count")
    ]
    return f"   (excluded: {', '.join(parts)})" if parts else ""


def _method_line(report: Mapping[str, Any]) -> str:
    method = report.get("method") or {}
    name = method.get("name", "unknown")
    version = method.get("version", "unknown")
    validation = report.get("validation") or {}
    if validation.get("status") == VALIDATION_MEASURED:
        agreement = validation.get("agreement")
        n = validation.get("n")
        return f"method: {name} {version} — agreement with labelled set {agreement} (n={n})"
    reason = validation.get("reason") or "not_measured"
    return f"method: {name} {version} — UNVALIDATED (agreement not measured: {reason})"


def _int(value: Any) -> str:
    return "—" if value is None else str(value)
