"""Tests for mechanical fix logic."""

from __future__ import annotations

from theforge.intake.findings import FixType, IntakeFinding, IntakeSeverity
from theforge.intake.mechanical import apply_mechanical_fixes


def _f(code: str, fix_type: FixType = FixType.SEMANTIC) -> IntakeFinding:
    return IntakeFinding(
        code=code,
        severity=IntakeSeverity.BLOCK,
        location="labels" if code == "missing_type" else "body",
        problem=f"problem-{code}",
        fix_type=fix_type,
    )


def test_mechanical_patches_missing_type_adds_task_label():
    body = "body"
    labels: list[str] = []
    findings = [_f("missing_type", FixType.MECHANICAL)]
    new_body, new_labels, consumed, remaining = apply_mechanical_fixes(body, labels, findings)
    assert new_body == body
    assert "task" in new_labels
    assert len(consumed) == 1
    assert remaining == []


def test_mechanical_skips_existing_type_label():
    findings = [_f("missing_type", FixType.MECHANICAL)]
    _, new_labels, consumed, _ = apply_mechanical_fixes("b", ["bug"], findings)
    assert new_labels == ["bug"]
    assert len(consumed) == 1


def test_semantic_findings_not_consumed():
    findings = [_f("missing_acceptance_criteria", FixType.SEMANTIC)]
    _, _, consumed, remaining = apply_mechanical_fixes("b", [], findings)
    assert consumed == []
    assert remaining == findings
