"""Tests for the text-only grooming check."""

from __future__ import annotations

from theforge.intake.findings import FixType, IntakeSeverity
from theforge.intake.groom import groom_check


def test_groom_passes_clean_body():
    body = (
        "## What\n\nUsers can export.\n\n"
        "## Acceptance criteria\n\n- Export download is available\n- File contains user records\n"
    )
    findings = groom_check("Title", body, ["enhancement"])
    assert findings == []


def test_groom_flags_how_shaped_ac():
    body = (
        "## Acceptance criteria\n\n"
        "- Refactor the export pipeline into a new ExportService class\n"
        "- Implementation: use the BatchEmitter module\n"
    )
    findings = groom_check("Title", body, ["enhancement"])
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "groom_how_shaped_ac"
    assert f.severity is IntakeSeverity.BLOCK
    assert f.location == "acceptance_criteria"
    assert f.fix_type is FixType.SEMANTIC


def test_groom_no_findings_on_empty_body():
    assert groom_check("Title", "", []) == []
