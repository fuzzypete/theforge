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


def test_groom_ignores_yaml_example_block_under_example_heading_in_ac():
    body = (
        "## Acceptance criteria\n\n"
        "- Sprint RCA file is emitted.\n"
        "- The file contains the expected fields.\n\n"
        "### Example\n\n"
        "```yaml\n"
        "primary_failure_class: flaky-tests\n"
        "contributing_factors:\n"
        "  - missing test class coverage\n"
        "```\n"
    )

    assert groom_check("Title", body, ["enhancement"]) == []


def test_groom_ignores_schema_heading_example_blocks():
    body = (
        "## Acceptance criteria\n\n"
        "- The CLI writes the structured artifact.\n\n"
        "### Schema\n\n"
        "```yaml\n"
        "function_signature: str\n"
        "module_layout: nested\n"
        "```\n"
    )

    assert groom_check("Title", body, ["enhancement"]) == []


def test_groom_flags_directives_inside_example_block():
    body = (
        "## Acceptance criteria\n\n"
        "- Export output is available.\n\n"
        "## Target output\n\n"
        "```python\n"
        "def implement_export_pipeline(records):\n"
        "    return records\n"
        "```\n"
    )

    findings = groom_check("Title", body, ["enhancement"])
    assert len(findings) == 1
    assert findings[0].code == "groom_how_shaped_ac"


def test_groom_flags_how_shaped_bullet_inside_non_example_fenced_block():
    body = (
        "## Acceptance criteria\n\n"
        "- Export output is available.\n\n"
        "### Notes\n\n"
        "```markdown\n"
        "- Refactor the export pipeline into an ExportService class\n"
        "```\n"
    )

    findings = groom_check("Title", body, ["enhancement"])
    assert len(findings) == 1
    assert findings[0].code == "groom_how_shaped_ac"


def test_groom_flags_file_directive_inside_example_block():
    body = (
        "## Acceptance criteria\n\n"
        "- Export output is available.\n\n"
        "## Example\n\n"
        "```python\n"
        "# modify src/theforge/export.py\n"
        "```\n"
    )

    findings = groom_check("Title", body, ["enhancement"])
    assert len(findings) == 1
    assert findings[0].code == "groom_how_shaped_ac"


def test_groom_flags_imperative_inside_example_block():
    body = (
        "## Acceptance criteria\n\n"
        "- Export output is available.\n\n"
        "## Example\n\n"
        "```text\n"
        "add method render to class ExportPresenter\n"
        "```\n"
    )

    findings = groom_check("Title", body, ["enhancement"])
    assert len(findings) == 1
    assert findings[0].code == "groom_how_shaped_ac"
