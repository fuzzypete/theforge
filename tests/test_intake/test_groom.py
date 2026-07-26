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


def test_groom_does_not_flag_import_as_domain_verb():
    """Regression for #1428: 'Import output includes...' uses 'import' as the
    issue's own domain verb (data ingestion), not an implementation directive."""
    body = (
        "## Acceptance criteria\n\n"
        "- Import output includes an operator-visible summary: imported, "
        "skipped-as-existing, updated/repaired, and failed records\n"
    )
    assert groom_check("Title", body, ["bug"]) == []


def test_groom_does_not_flag_domain_verbs_as_field_or_command_names():
    """Flagged tokens used as domain verbs / field names must not trip."""
    body = (
        "## Acceptance criteria\n\n"
        "- Export saves the generated report to the configured location\n"
        "- Rename target file to its canonical slug before writing\n"
        "- Extract records from the uploaded CSV and validate each row\n"
    )
    assert groom_check("Title", body, ["enhancement"]) == []


def test_groom_still_flags_verb_operating_on_code_construct():
    """Genuine implementation-prescriptive language must still be flagged even
    without a standalone strong token."""
    body = "## Acceptance criteria\n\n- Refactor the FooService class to extract a helper method\n"
    findings = groom_check("Title", body, ["enhancement"])
    assert len(findings) == 1
    assert findings[0].code == "groom_how_shaped_ac"


def test_groom_still_flags_implement_using_domain_import_machinery():
    """'implement using' is a strong token; it flags even when the object is
    the issue's own domain vocabulary ('import machinery')."""
    body = (
        "## Acceptance criteria\n\n- Implement using the existing import machinery in loader.py\n"
    )
    findings = groom_check("Title", body, ["enhancement"])
    assert len(findings) == 1
    assert findings[0].code == "groom_how_shaped_ac"


def test_groom_does_not_flag_renamed_entity_outcome_bullet():
    """Regression for hdp #141: outcome language about a renamed domain entity
    is not an instruction to rename a code construct."""
    body = (
        "## Acceptance criteria\n\n"
        "- A renamed or closely related exercise still shows its prior "
        "performance history on the card\n"
    )
    assert groom_check("Title", body, ["bug"]) == []


def test_groom_does_not_flag_inflected_strong_tokens_in_outcome_prose():
    """Participle/plural forms of strong tokens ('refactored', 'subclasses')
    must not trip the substring match when used as outcome/domain language."""
    body = (
        "## Acceptance criteria\n\n"
        "- The refactored module still passes existing tests\n"
        "- Sport subclasses like tennis show their own leaderboards\n"
    )
    assert groom_check("Title", body, ["enhancement"]) == []


def test_groom_still_flags_bare_strong_tokens():
    """Whole-word strong tokens must still flag on their own."""
    body = "## Acceptance criteria\n\n- Refactor the pipeline for clarity\n"
    findings = groom_check("Title", body, ["enhancement"])
    assert len(findings) == 1
    assert findings[0].code == "groom_how_shaped_ac"


def test_groom_still_flags_punctuation_ended_strong_token():
    """Regression: the 'implementation:' strong token must still flag even
    with no nearby code construct — word-boundary anchoring for other tokens
    must not accidentally suppress this punctuation-ended one."""
    body = "## Acceptance criteria\n\n- Implementation: use the BatchEmitter module\n"
    findings = groom_check("Title", body, ["enhancement"])
    assert len(findings) == 1
    assert findings[0].code == "groom_how_shaped_ac"


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
