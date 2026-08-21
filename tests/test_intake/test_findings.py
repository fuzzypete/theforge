"""Tests for IntakeFinding shared type and ShapeResult mapping."""

from __future__ import annotations

from theforge.intake.findings import (
    FixType,
    IntakeFinding,
    IntakeSeverity,
    RerunHint,
    findings_from_shape_result,
)
from theforge.shape_check.types import Reason, Severity, Shape, ShapeResult, SuggestedAction


def _shape_result(reasons: list[Reason]) -> ShapeResult:
    return ShapeResult(
        shape=Shape.NEEDS_GROOMING if reasons else Shape.RUNNABLE,
        reasons=tuple(reasons),
        suggested_action=SuggestedAction.CLARIFY if reasons else SuggestedAction.PROCEED,
    )


def test_intake_finding_dataclass_fields():
    f = IntakeFinding(
        code="x",
        severity=IntakeSeverity.BLOCK,
        location="body",
        problem="problem",
    )
    d = f.as_dict()
    assert d["code"] == "x"
    assert d["severity"] == "block"
    assert d["fix_type"] == "semantic"
    assert d["suggested_replacement"] is None
    assert d["rerun_hint"] is None


def test_findings_from_shape_result_lossless_mapping():
    reasons = [
        Reason(code="missing_acceptance_criteria", severity=Severity.BLOCKING, detail="no AC"),
        Reason(code="missing_example", severity=Severity.ADVISORY, detail="no example"),
        Reason(code="missing_type", severity=Severity.BLOCKING, detail="needs type label"),
    ]
    result = _shape_result(reasons)
    findings = findings_from_shape_result(result)
    assert len(findings) == 3
    assert {f.code for f in findings} == {
        "missing_acceptance_criteria",
        "missing_example",
        "missing_type",
    }
    by_code = {f.code: f for f in findings}
    assert by_code["missing_acceptance_criteria"].severity is IntakeSeverity.BLOCK
    assert by_code["missing_example"].severity is IntakeSeverity.FLAG
    assert by_code["missing_type"].fix_type is FixType.MECHANICAL
    assert by_code["missing_acceptance_criteria"].fix_type is FixType.SEMANTIC
    # Detail string is preserved.
    assert by_code["missing_example"].problem == "no example"


def test_findings_from_empty_shape_result_yields_empty_list():
    assert findings_from_shape_result(_shape_result([])) == []


def test_unreadable_region_diagnosis_maps_retry_hint():
    result = _shape_result(
        [
            Reason(
                code="needs_diagnosis",
                severity=Severity.BLOCKING,
                detail=(
                    "Diagnosis covers Observed symptom, Evidence, Confirmed cause, "
                    "Affected code path, but only inside a blockquoted region; "
                    "move those bullets into top-level readable Markdown."
                ),
            )
        ]
    )
    findings = findings_from_shape_result(result)
    assert len(findings) == 1
    assert findings[0].rerun_hint is RerunHint.UNREADABLE_REQUIRED_REGION
