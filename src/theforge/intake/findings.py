"""Shared ``IntakeFinding`` type and ``ShapeResult`` mapping.

Both the shape gate and the sprint-time grooming check emit findings in this
common shape so the remediation orchestrator can treat them uniformly.

Stdlib only — pure-data types in low-dependency modules (project convention 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..shape_check.types import Reason, Severity, ShapeResult


class IntakeSeverity(str, Enum):
    """Severity of an intake finding.

    BLOCK: story is dropped from the sprint if unresolved after the auto-fix
    pass. FLAG: advisory; story still proceeds.
    """

    BLOCK = "block"
    FLAG = "flag"


class FixType(str, Enum):
    """How a finding can be remediated.

    ``mechanical`` findings can be patched without an LLM call (e.g. add a
    missing label, insert a missing section header). ``semantic`` findings
    require an agent rewrite of the affected section.
    """

    MECHANICAL = "mechanical"
    SEMANTIC = "semantic"


class RerunHint(str, Enum):
    """Optional coordinator hint for a specific post-rewrite rerun outcome."""

    UNREADABLE_REQUIRED_REGION = "unreadable_required_region"


# Codes emitted by the existing shape_check heuristics that can be patched
# without an LLM. Anything not in this set is treated as semantic.
_MECHANICAL_SHAPE_CODES: frozenset[str] = frozenset(
    {
        "missing_type",
    }
)
_UNREADABLE_REGION_MARKERS: frozenset[str] = frozenset(
    {
        "inside a blockquoted region",
        "inside a fenced code block",
        "inside a region it does not scan",
    }
)


@dataclass(frozen=True)
class IntakeFinding:
    """A single intake finding from shape or grooming.

    Attributes:
        code: machine-readable finding identifier (matches shape_check codes
            when sourced from a ShapeResult; grooming-only codes are
            namespaced with ``groom_`` prefix).
        severity: BLOCK (story dropped if unresolved) or FLAG (advisory).
        location: which field/section of the issue body the finding refers to
            (e.g. ``"acceptance_criteria"``, ``"body"``, ``"labels"``).
        problem: human-readable description.
        suggested_replacement: proposed replacement text for the affected
            section, or ``None`` for structural findings with no obvious patch.
        fix_type: ``mechanical`` (no LLM needed) or ``semantic`` (agent pass).
        rerun_hint: machine-readable coordinator hint for bounded follow-up
            handling after a rerun, or ``None`` when no special handling applies.
    """

    code: str
    severity: IntakeSeverity
    location: str
    problem: str
    suggested_replacement: str | None = None
    fix_type: FixType = FixType.SEMANTIC
    rerun_hint: RerunHint | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "location": self.location,
            "problem": self.problem,
            "suggested_replacement": self.suggested_replacement,
            "fix_type": self.fix_type.value,
            "rerun_hint": self.rerun_hint.value if self.rerun_hint is not None else None,
        }


def _classify_fix_type(code: str) -> FixType:
    if code in _MECHANICAL_SHAPE_CODES:
        return FixType.MECHANICAL
    return FixType.SEMANTIC


def _location_for_code(code: str) -> str:
    """Best-effort mapping from shape_check code to issue body location."""
    if code in {"missing_acceptance_criteria", "no_observable_done_state"}:
        return "acceptance_criteria"
    if code == "missing_example":
        return "examples"
    if code == "missing_type":
        return "labels"
    if code == "implementation_design_dump":
        return "body"
    if code == "too_many_behavioral_clusters":
        return "scope"
    if code in {"epic_or_tracking", "superseded", "untriaged_finding"}:
        return "issue"
    return "body"


def _rerun_hint_for_reason(reason: Reason) -> RerunHint | None:
    if reason.code != "needs_diagnosis":
        return None
    detail = reason.detail.lower()
    if any(marker in detail for marker in _UNREADABLE_REGION_MARKERS):
        return RerunHint.UNREADABLE_REQUIRED_REGION
    return None


def findings_from_shape_result(result: ShapeResult) -> list[IntakeFinding]:
    """Map a ``ShapeResult`` to a list of ``IntakeFinding``.

    The mapping is loss-less: every ``Reason`` in ``result.reasons`` produces
    exactly one ``IntakeFinding`` with the same ``code`` and a severity
    derived from the ``Reason.severity`` field.
    """
    out: list[IntakeFinding] = []
    for reason in result.reasons:
        out.append(_finding_from_reason(reason))
    return out


def _finding_from_reason(reason: Reason) -> IntakeFinding:
    severity = (
        IntakeSeverity.BLOCK if reason.severity is Severity.BLOCKING else IntakeSeverity.FLAG
    )
    return IntakeFinding(
        code=reason.code,
        severity=severity,
        location=_location_for_code(reason.code),
        problem=reason.detail,
        suggested_replacement=None,
        fix_type=_classify_fix_type(reason.code),
        rerun_hint=_rerun_hint_for_reason(reason),
    )
