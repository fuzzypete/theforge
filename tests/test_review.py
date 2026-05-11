"""Tests for review output parsing."""

from theforge.review import (
    ReviewFinding,
    ReviewResult,
    _best_individual_result,
    _coerce_line,
    _dedup_findings,
    _sanitize_yaml_text,
    _try_parse_review,
    append_convention_retry_findings,
    convention_violations_to_review_findings,
    findings_to_markdown,
    merge_review_results,
    parse_plan_review_output,
    parse_review_json,
    parse_review_output,
)

VALID_APPROVE_YAML = """\
```yaml
verdict: APPROVE
summary: "Clean implementation, matches spec."
findings: []
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
ac_verification:
  - criterion: "It batches inputs"
    status: VERIFIED
    evidence: "src/batch.py:10-30 + tests/test_batch.py::test_batches"
```
"""

VALID_REQUEST_CHANGES_YAML = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Off-by-one in batch processor"
findings:
  - severity: P1
    file: src/batch.py
    line: 42
    description: "Off-by-one: range(n) should be range(n+1)"
    suggestion: "Change range(n) to range(n+1)"
  - severity: P2
    file: src/batch.py
    line: 10
    description: "Unused import os"
    suggestion: "Remove import os"
story_compliance:
  matches_spec: false
  mismatches:
    - "Batch size not configurable as spec requires"
test_coverage:
  adequate: false
  gaps:
    - "No test for empty batch input"
```
"""


class TestParseReviewOutput:
    def test_approve(self):
        result = parse_review_output(VALID_APPROVE_YAML)
        assert result.verdict == "APPROVE"
        assert result.findings == []
        assert result.story_matches is True
        assert result.test_adequate is True
        assert result.parse_errors == []

    def test_request_changes(self):
        result = parse_review_output(VALID_REQUEST_CHANGES_YAML)
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) == 2
        assert result.findings[0].severity == "P1"
        assert result.findings[0].file == "src/batch.py"
        assert result.findings[0].line == 42
        assert result.findings[1].severity == "P2"
        assert result.story_matches is False
        assert len(result.story_mismatches) == 1
        assert result.test_adequate is False
        assert len(result.test_gaps) == 1

    def test_bare_yaml_no_fences(self):
        bare = (
            "verdict: APPROVE\n"
            "summary: ok\n"
            "findings: []\n"
            "story_compliance:\n"
            "  matches_spec: true\n"
            "test_coverage:\n"
            "  adequate: true\n"
        )
        result = parse_review_output(bare)
        assert result.verdict == "APPROVE"

    def test_garbage_input(self):
        result = parse_review_output("this is not yaml: [[[")
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.parse_errors) > 0

    def test_yaml_syntax_error_tagged_as_yaml_syntax(self):
        """Operator-facing: a parser-layer rejection must be tagged YAML_SYNTAX,
        not lumped with schema/contract failures."""
        from theforge.schemas import YAML_SYNTAX

        result = parse_review_output("this is not yaml: [[[")
        assert result.parse_errors
        stages = {e.stage for e in result.parse_errors}
        assert stages == {YAML_SYNTAX}

    def test_non_mapping_root_tagged_as_structure(self):
        from theforge.schemas import STRUCTURE

        # A bare scalar parses as YAML but isn't a mapping.
        result = parse_review_output("just a string")
        assert result.parse_errors
        assert result.parse_errors[0].stage == STRUCTURE

    def test_schema_field_errors_tagged_as_schema_validation(self):
        """Type/required-field rejections come from the schema layer, not parser."""
        from theforge.schemas import SCHEMA_VALIDATION

        # Valid YAML, valid mapping, but verdict is wrong type.
        result = parse_review_output(
            "```yaml\n"
            "verdict: MAYBE\n"
            "summary: ''\n"
            "findings: []\n"
            "story_compliance:\n"
            "  matches_spec: true\n"
            "test_coverage:\n"
            "  adequate: true\n"
            "```\n"
        )
        assert result.parse_errors
        assert all(e.stage == SCHEMA_VALIDATION for e in result.parse_errors)

    def test_contract_cross_validation_tagged_distinctly(self):
        """APPROVE + P1 and APPROVE + empty ac_verification are contract
        cross-checks — operators remediate at the prompt/contract layer, not
        the parser. Stage must distinguish them from YAML_SYNTAX."""
        from theforge.schemas import CONTRACT_CROSS_VALIDATION, YAML_SYNTAX

        # APPROVE with a P1 finding is a contract contradiction.
        result = parse_review_output(
            "```yaml\n"
            "verdict: APPROVE\n"
            "summary: 'looks good'\n"
            "findings:\n"
            "  - severity: P1\n"
            "    file: src/x.py\n"
            "    line: 1\n"
            "    description: 'blocker'\n"
            "    suggestion: 'fix it'\n"
            "story_compliance:\n"
            "  matches_spec: true\n"
            "test_coverage:\n"
            "  adequate: true\n"
            "ac_verification:\n"
            "  - criterion: 'AC1'\n"
            "    status: VERIFIED\n"
            "    evidence: 'ev'\n"
            "```\n"
        )
        stages = {e.stage for e in result.parse_errors}
        assert CONTRACT_CROSS_VALIDATION in stages
        assert YAML_SYNTAX not in stages

    def test_parse_error_str_carries_stage_tag(self):
        """Rendered form embeds the stage so log/audit consumers that emit
        the string (not the dataclass) still surface the classification."""
        from theforge.schemas import YAML_SYNTAX

        result = parse_review_output("not yaml: [[[")
        assert any(str(e).startswith(f"[{YAML_SYNTAX}]") for e in result.parse_errors)

    def test_yaml_with_extra_prose(self):
        text = (
            "Here is my review:\n\n"
            "```yaml\n"
            "verdict: APPROVE\n"
            "summary: all good\n"
            "findings: []\n"
            "story_compliance:\n"
            "  matches_spec: true\n"
            "test_coverage:\n"
            "  adequate: true\n"
            "```\n\n"
            "Let me know if you have questions.\n"
        )
        result = parse_review_output(text)
        assert result.verdict == "APPROVE"

    def test_parse_review_json_sanitizes_summary_and_findings(self):
        result = parse_review_json(
            {
                "verdict": "APPROVE",
                "summary": "Safe\x00 summary\x07",
                "findings": [
                    {
                        "severity": "P2",
                        "file": "src/batch.py",
                        "line": 5,
                        "description": "Bad\x00 finding\x1f text",
                    }
                ],
                "story_compliance": {"matches_spec": True, "mismatches": []},
                "test_coverage": {"adequate": True, "gaps": []},
            }
        )

        assert result.summary == "Safe summary"
        assert result.findings[0].description == "Bad finding text"
        assert result.sanitization_audit == {
            "summary": {"sanitized_chars": 2},
            "findings[0].description": {"sanitized_chars": 2},
        }


class TestSanitizeYamlText:
    """Unit tests for _sanitize_yaml_text — apostrophe-in-single-quoted-scalar fix."""

    def test_issue_1511_trace_line_fixed(self):
        # Exact pattern from story #1511: LLM emitted criterion with unescaped apostrophe.
        line = (
            "  criterion: 'For features, stories, and docs: "
            "ensures the body matches the type's required headings.'"
        )
        result = _sanitize_yaml_text(line)
        import yaml

        parsed = yaml.safe_load(result)
        expected = (
            "For features, stories, and docs: "
            "ensures the body matches the type's required headings."
        )
        assert parsed["criterion"] == expected

    def test_no_apostrophe_scalar_unchanged(self):
        line = "  status: 'VERIFIED'\n"
        assert _sanitize_yaml_text(line) == line

    def test_doubled_apostrophe_already_valid_unchanged(self):
        # 'it''s fine' is valid YAML single-quoted; must not be altered.
        line = "  text: 'it''s fine'\n"
        result = _sanitize_yaml_text(line)
        import yaml

        parsed = yaml.safe_load(result)
        assert parsed["text"] == "it's fine"

    def test_contraction_in_single_quoted_fixed(self):
        line = "  summary: 'The system doesn't handle this case.'"
        result = _sanitize_yaml_text(line)
        import yaml

        parsed = yaml.safe_load(result)
        assert parsed["summary"] == "The system doesn't handle this case."

    def test_existing_backslash_quote_fix_still_works(self):
        text = 'description: \\"bad escape\\"'
        result = _sanitize_yaml_text(text)
        assert '\\"' not in result

    def test_backtick_fix_still_works(self):
        text = "description: use `foo.bar` here\n"
        result = _sanitize_yaml_text(text)
        assert "`" not in result

    def test_non_scalar_line_not_affected(self):
        # A line without a key: 'value' pattern should pass through unchanged.
        line = "- item without quotes\n"
        assert _sanitize_yaml_text(line) == line


class TestParseReviewOutputApostrophe:
    """End-to-end regression for issue #1511: apostrophe in single-quoted scalar."""

    # Fixture reproduces the LLM emission failure from sprint 77a72be5ff30:
    # unescaped apostrophe inside single-quoted YAML scalar ('type's name').
    _ISSUE_1511_APOSTROPHE_YAML = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Review with possessive"
findings:
  - severity: P1
    file: src/theforge/schemas.py
    line: 10
    description: "Missing required field"
    suggestion: "Add the field"
story_compliance:
  matches_spec: false
  mismatches:
    - "Story body does not match spec"
test_coverage:
  adequate: false
  gaps:
    - "No regression test"
ac_verification:
  - criterion: 'Ensures the story body matches the type's required headings.'
    status: NOT_VERIFIED
    evidence: 'No evidence found'
```
"""

    def test_apostrophe_in_criterion_parses_successfully(self):
        result = parse_review_output(self._ISSUE_1511_APOSTROPHE_YAML)
        assert result.parse_errors == [], f"Unexpected parse errors: {result.parse_errors}"
        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) == 1
        assert result.findings[0].severity == "P1"
        assert len(result.ac_verification) == 1
        assert "type's required headings" in result.ac_verification[0].criterion

    def test_apostrophe_parse_preserves_content(self):
        result = parse_review_output(self._ISSUE_1511_APOSTROPHE_YAML)
        assert result.ac_verification[0].criterion == (
            "Ensures the story body matches the type's required headings."
        )


class TestFindingsToMarkdown:
    def test_empty(self):
        assert findings_to_markdown([]) == "No findings."

    def test_with_findings(self):
        findings = [
            ReviewFinding(
                severity="P1",
                file="src/foo.py",
                line=42,
                description="Bug here",
                suggestion="Fix it",
            ),
            ReviewFinding(
                severity="P2",
                file="src/bar.py",
                line=None,
                description="Style issue",
                suggestion=None,
            ),
        ]
        md = findings_to_markdown(findings)
        assert "[P1]" in md
        assert "`src/foo.py`" in md
        assert "(line 42)" in md
        assert "**Fix:** Fix it" in md
        assert "[P2]" in md
        assert "`src/bar.py`" in md


class TestConventionViolationsToReviewFindings:
    def test_blocking_violations_are_converted_to_p1_findings(self):
        findings = convention_violations_to_review_findings(
            [
                {
                    "rule": "no_scratch_files",
                    "file": "test_resolution_commentary.py",
                    "detail": "Unexpected root-level scratch file",
                    "blocking": True,
                },
                {
                    "rule": "max_module_lines",
                    "file": "src/large.py",
                    "detail": "Module exceeds limit",
                    "blocking": False,
                },
            ]
        )

        assert len(findings) == 1
        assert findings[0].severity == "P1"
        assert findings[0].file == "test_resolution_commentary.py"
        assert "no_scratch_files" in findings[0].description
        assert "Resolve the [no_scratch_files]" in findings[0].suggestion

    def test_append_convention_retry_findings_preserves_existing_feedback(self):
        output = append_convention_retry_findings(
            "## Review Summary\nPrior review feedback",
            [
                {
                    "rule": "no_scratch_files",
                    "file": "test_resolution_commentary.py",
                    "detail": "Unexpected root-level scratch file",
                    "blocking": True,
                }
            ],
        )

        assert output is not None
        assert "## Review Summary" in output
        assert "## Blocking Convention Violations" in output
        assert "`test_resolution_commentary.py`" in output


class TestParsePlanReviewOutput:
    def test_parse_plan_review_approve(self):
        yaml_text = """\
```yaml
verdict: APPROVE
findings: []
```
"""
        result = parse_plan_review_output(yaml_text)
        assert result.verdict == "APPROVE"
        assert result.findings == []
        assert result.parse_errors == []

    def test_parse_plan_review_reject_with_findings(self):
        yaml_text = """\
```yaml
verdict: REJECT
findings:
  - severity: P1
    description: "Plan references nonexistent function"
    suggestion: "Use load_config() instead"
```
"""
        result = parse_plan_review_output(yaml_text)
        assert result.verdict == "REJECT"
        assert len(result.findings) == 1
        assert result.findings[0].severity == "P1"
        assert result.findings[0].description == "Plan references nonexistent function"
        assert result.findings[0].suggestion == "Use load_config() instead"
        assert result.parse_errors == []

    def test_parse_plan_review_reject_no_findings_error(self):
        yaml_text = """\
```yaml
verdict: REJECT
findings: []
```
"""
        result = parse_plan_review_output(yaml_text)
        assert result.verdict == "REJECT"
        assert len(result.parse_errors) == 1

    def test_approve_with_p1_findings_preserved(self):
        """APPROVE with P1 findings: severity preserved; merge computes verdict from findings."""
        yaml_text = """\
```yaml
verdict: APPROVE
findings:
  - severity: P1
    description: "Hallucinated API"
    suggestion: "Fix it"
```
"""
        result = parse_plan_review_output(yaml_text)
        assert result.verdict == "APPROVE"
        assert not result.parse_errors
        assert result.findings[0].severity == "P1"

    def test_approve_with_p0_findings_preserved(self):
        """APPROVE with P0 findings: severity preserved; merge computes verdict from findings."""
        yaml_text = """\
```yaml
verdict: APPROVE
findings:
  - severity: P0
    description: "Architecturally broken"
    suggestion: "Rethink"
```
"""
        result = parse_plan_review_output(yaml_text)
        assert result.verdict == "APPROVE"
        assert not result.parse_errors
        assert result.findings[0].severity == "P0"

    def test_approve_with_malformed_findings_demoted_to_reject(self):
        yaml_text = """\
```yaml
verdict: APPROVE
findings:
  - severity: P1
    description: ""
```
"""
        result = parse_plan_review_output(yaml_text)
        assert result.verdict == "REJECT"
        assert any("non-empty" in e for e in result.parse_errors)

    def test_approve_with_non_list_findings_demoted_to_reject(self):
        yaml_text = """\
```yaml
verdict: APPROVE
findings: "not a list"
```
"""
        result = parse_plan_review_output(yaml_text)
        assert result.verdict == "REJECT"
        assert any("must be a list" in e for e in result.parse_errors)


# ── Tests: _try_parse_review ──────────────────────────────────────────


VALID_APPROVE_OUTPUT = """\
```yaml
verdict: APPROVE
summary: "All good"
findings: []
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
ac_verification:
  - criterion: "It works"
    status: VERIFIED
    evidence: "src/foo.py:1 + tests/test_foo.py::test_works"
```
"""

INVALID_OUTPUT = "not yaml at all {{{{ garbage"


class TestTryParseReview:
    """Tests for _try_parse_review helper."""

    def test_valid_output_returns_result(self):
        result = _try_parse_review(VALID_APPROVE_OUTPUT)
        assert result is not None
        assert result.verdict == "APPROVE"
        assert not result.parse_errors

    def test_invalid_yaml_returns_none(self):
        result = _try_parse_review("not valid yaml {{{")
        assert result is None

    def test_schema_error_repaired_successfully(self):
        # Valid YAML but missing optional fields — repair layer fills them.
        # ac_verification must be supplied because APPROVE+missing/empty is a
        # cross-validation failure (silent-contract-swap guard).
        bad = """\
```yaml
verdict: APPROVE
summary: "ok"
findings: []
ac_verification:
  - criterion: "It works"
    status: VERIFIED
    evidence: "src/foo.py:1 + tests/test_foo.py::test_works"
```
"""
        result = _try_parse_review(bad)
        assert result is not None
        assert result.verdict == "APPROVE"
        assert result.story_matches is True  # inferred from verdict
        assert result.test_adequate is True  # default

    def test_approve_without_ac_verification_returns_none(self):
        """APPROVE missing ac_verification triggers cross-validation failure."""
        bad = """\
```yaml
verdict: APPROVE
summary: "ok"
findings: []
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""
        result = _try_parse_review(bad)
        assert result is None  # parse_errors present → None

    def test_structured_data_path(self):
        data = {
            "verdict": "APPROVE",
            "summary": "ok",
            "findings": [],
            "story_compliance": {"matches_spec": True, "mismatches": []},
            "test_coverage": {"adequate": True, "gaps": []},
            "ac_verification": [
                {
                    "criterion": "It works",
                    "status": "VERIFIED",
                    "evidence": "src/foo.py:1 + tests/test_foo.py::test_works",
                },
            ],
        }
        result = _try_parse_review("", structured_data=data)
        assert result is not None
        assert result.verdict == "APPROVE"

    def test_structured_data_with_missing_fields_repaired(self):
        # Missing optional fields — repair layer fills them. ac_verification
        # must be supplied because APPROVE+missing fails cross-validation.
        data = {
            "verdict": "APPROVE",
            "summary": "ok",
            "findings": [],
            "ac_verification": [
                {
                    "criterion": "It works",
                    "status": "VERIFIED",
                    "evidence": "src/foo.py:1 + tests/test_foo.py::test_works",
                },
            ],
        }
        result = _try_parse_review("", structured_data=data)
        assert result is not None
        assert result.verdict == "APPROVE"

    def test_contradictory_approve_with_p1_returns_none(self):
        data = {
            "verdict": "APPROVE",
            "summary": "Looks good",
            "findings": [
                {
                    "severity": "P1",
                    "file": "src/foo.py",
                    "line": 12,
                    "description": "Prior P1 from cycle 1 is fixed",
                    "suggestion": "No action needed",
                }
            ],
            "story_compliance": {"matches_spec": True, "mismatches": []},
            "test_coverage": {"adequate": True, "gaps": []},
        }
        result = _try_parse_review("", structured_data=data)
        assert result is None


# ── Tests: _best_individual_result ───────────────────────────────────


def _make_review_result(
    verdict: str,
    findings: list | None = None,
    parse_errors: list | None = None,
) -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary=f"summary for {verdict}",
        findings=findings or [],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=parse_errors or [],
        raw_yaml={},
    )


def _p1_finding() -> ReviewFinding:
    return ReviewFinding(severity="P1", file="foo.py", line=1, description="Bug", suggestion="Fix")


def _p2_finding() -> ReviewFinding:
    return ReviewFinding(
        severity="P2", file="foo.py", line=2, description="Style", suggestion="Clean up"
    )


class TestBestIndividualResult:
    """Tests for _best_individual_result helper."""

    def test_empty_list_returns_none(self):
        assert _best_individual_result([]) is None

    def test_single_approve_returns_it(self):
        r = _make_review_result("APPROVE")
        assert _best_individual_result([r]) is r

    def test_p1_result_returned_when_any_has_p1(self):
        approve = _make_review_result("APPROVE")
        with_p1 = _make_review_result("REQUEST_CHANGES", findings=[_p1_finding()])
        result = _best_individual_result([approve, with_p1])
        assert result is with_p1

    def test_first_p1_wins_over_later(self):
        p1_first = _make_review_result("REQUEST_CHANGES", findings=[_p1_finding()])
        p1_second = _make_review_result("REQUEST_CHANGES", findings=[_p1_finding()])
        result = _best_individual_result([p1_first, p1_second])
        assert result is p1_first

    def test_all_approve_returns_first(self):
        a1 = _make_review_result("APPROVE")
        a2 = _make_review_result("APPROVE")
        result = _best_individual_result([a1, a2])
        assert result is a1

    def test_p2_only_no_p1_returns_first_approve_if_present(self):
        with_p2 = _make_review_result("REQUEST_CHANGES", findings=[_p2_finding()])
        approve = _make_review_result("APPROVE")
        result = _best_individual_result([with_p2, approve])
        assert result is approve

    def test_single_p1_result(self):
        with_p1 = _make_review_result("REQUEST_CHANGES", findings=[_p1_finding()])
        assert _best_individual_result([with_p1]) is with_p1


# ── Tests: _dedup_findings ───────────────────────────────────────────


def _rf(severity: str, file: str, line: int | None, description: str) -> ReviewFinding:
    return ReviewFinding(
        severity=severity, file=file, line=line, description=description, suggestion=None
    )


class TestDedupFindings:
    """Tests for _dedup_findings helper."""

    def test_no_duplicates_unchanged(self):
        f1 = _rf("P1", "foo.py", 1, "Bug A")
        f2 = _rf("P2", "bar.py", 2, "Bug B")
        result = _dedup_findings([("r1", f1), ("r2", f2)])
        assert len(result) == 2

    def test_exact_duplicate_collapsed(self):
        f = _rf("P1", "foo.py", 1, "Bug A")
        result = _dedup_findings([("r1", f), ("r2", f)])
        assert len(result) == 1

    def test_case_insensitive_description_dedup(self):
        f1 = _rf("P1", "foo.py", 1, "Bug A")
        f2 = _rf("P1", "foo.py", 1, "bug a")
        result = _dedup_findings([("r1", f1), ("r2", f2)])
        assert len(result) == 1

    def test_whitespace_normalized_description_dedup(self):
        f1 = _rf("P1", "foo.py", 1, "  Bug A  ")
        f2 = _rf("P1", "foo.py", 1, "Bug A")
        result = _dedup_findings([("r1", f1), ("r2", f2)])
        assert len(result) == 1

    def test_different_line_not_deduped(self):
        f1 = _rf("P1", "foo.py", 1, "Bug A")
        f2 = _rf("P1", "foo.py", 2, "Bug A")
        result = _dedup_findings([("r1", f1), ("r2", f2)])
        assert len(result) == 2

    def test_different_file_not_deduped(self):
        f1 = _rf("P1", "foo.py", 1, "Bug A")
        f2 = _rf("P1", "bar.py", 1, "Bug A")
        result = _dedup_findings([("r1", f1), ("r2", f2)])
        assert len(result) == 2

    def test_different_description_not_deduped(self):
        f1 = _rf("P1", "foo.py", 1, "Bug A")
        f2 = _rf("P1", "foo.py", 1, "Bug B")
        result = _dedup_findings([("r1", f1), ("r2", f2)])
        assert len(result) == 2

    def test_first_occurrence_wins(self):
        f1 = _rf("P1", "foo.py", 1, "Bug A")
        f2 = _rf("P2", "foo.py", 1, "bug a")  # different severity, same location/desc
        result = _dedup_findings([("r1", f1), ("r2", f2)])
        assert len(result) == 1
        assert result[0].severity == "P1"  # first wins

    def test_reviewers_attributed_on_duplicate(self):
        f = _rf("P1", "foo.py", 1, "Bug A")
        result = _dedup_findings([("reviewer-a", f), ("reviewer-b", f)])
        assert len(result) == 1
        assert set(result[0].reviewers) == {"reviewer-a", "reviewer-b"}

    def test_single_reviewer_attribution(self):
        f = _rf("P1", "foo.py", 1, "Bug A")
        result = _dedup_findings([("reviewer-a", f)])
        assert result[0].reviewers == ("reviewer-a",)

    def test_triple_duplicate_all_attributed(self):
        f = _rf("P1", "foo.py", 1, "Bug A")
        result = _dedup_findings([("r1", f), ("r2", f), ("r3", f)])
        assert len(result) == 1
        assert set(result[0].reviewers) == {"r1", "r2", "r3"}

    def test_null_line_deduplication(self):
        f1 = _rf("P1", "foo.py", None, "Bug A")
        f2 = _rf("P1", "foo.py", None, "bug a")
        result = _dedup_findings([("r1", f1), ("r2", f2)])
        assert len(result) == 1
        assert set(result[0].reviewers) == {"r1", "r2"}


# ── Tests: merge_review_results deduplication ────────────────────────


class TestMergeReviewResultsDedup:
    """Tests for deduplication in merge_review_results."""

    def test_duplicate_p1_across_reviewers_counted_once(self):
        finding = _rf("P1", "foo.py", 1, "Off-by-one error")
        r1 = _make_review_result("REQUEST_CHANGES", findings=[finding])
        r2 = _make_review_result("REQUEST_CHANGES", findings=[finding])
        r3 = _make_review_result("REQUEST_CHANGES", findings=[finding])
        merged = merge_review_results([r1, r2, r3], ["a", "b", "c"])
        p1_count = sum(1 for f in merged.findings if f.severity == "P1")
        assert p1_count == 1  # unique issue, not reviewer_count * issues

    def test_unique_findings_all_preserved(self):
        f1 = _rf("P1", "foo.py", 1, "Bug A")
        f2 = _rf("P1", "bar.py", 2, "Bug B")
        r1 = _make_review_result("REQUEST_CHANGES", findings=[f1])
        r2 = _make_review_result("REQUEST_CHANGES", findings=[f2])
        merged = merge_review_results([r1, r2], ["a", "b"])
        assert len(merged.findings) == 2

    def test_duplicate_finding_carries_reviewer_attribution(self):
        finding = _rf("P1", "foo.py", 1, "Same issue")
        r1 = _make_review_result("REQUEST_CHANGES", findings=[finding])
        r2 = _make_review_result("REQUEST_CHANGES", findings=[finding])
        merged = merge_review_results([r1, r2], ["reviewer-a", "reviewer-b"])
        assert len(merged.findings) == 1
        assert set(merged.findings[0].reviewers) == {"reviewer-a", "reviewer-b"}

    def test_all_parse_errors_propagated(self):
        from theforge.schemas import YAML_SYNTAX, ParseError

        r1 = _make_review_result(
            "REQUEST_CHANGES",
            parse_errors=[ParseError(stage=YAML_SYNTAX, message="bad yaml")],
        )
        r2 = _make_review_result(
            "REQUEST_CHANGES",
            parse_errors=[ParseError(stage=YAML_SYNTAX, message="bad yaml")],
        )
        merged = merge_review_results([r1, r2], ["a", "b"])
        assert merged.parse_errors  # propagated for retry loop

    def test_mixed_valid_and_parse_error(self):
        finding = _rf("P1", "foo.py", 1, "Bug")
        valid = _make_review_result("REQUEST_CHANGES", findings=[finding])
        from theforge.schemas import YAML_SYNTAX, ParseError

        invalid = _make_review_result(
            "REQUEST_CHANGES",
            parse_errors=[ParseError(stage=YAML_SYNTAX, message="bad")],
        )
        merged = merge_review_results([valid, invalid], ["a", "b"])
        assert not merged.parse_errors
        assert len(merged.findings) == 1


# ── Tests: _coerce_line ──────────────────────────────────────────────


class TestCoerceLine:
    def test_none_returns_none(self):
        assert _coerce_line(None) is None

    def test_int_passthrough(self):
        assert _coerce_line(42) == 42

    def test_string_int_coerced(self):
        assert _coerce_line("42") == 42

    def test_string_zero(self):
        assert _coerce_line("0") == 0

    def test_float_truncated(self):
        assert _coerce_line(3.9) == 3

    def test_non_numeric_string_returns_none(self):
        assert _coerce_line("N/A") is None

    def test_empty_string_returns_none(self):
        assert _coerce_line("") is None

    def test_object_returns_none(self):
        assert _coerce_line([1, 2]) is None


# ── Tests: parse_review_output with string line values ───────────────


REVIEW_WITH_STRING_LINE = """\
```yaml
verdict: REQUEST_CHANGES
summary: "String line number emitted by reviewer"
findings:
  - severity: P1
    file: src/foo.py
    line: "42"
    description: "Bug with string line"
    suggestion: "Fix it"
story_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
```
"""


class TestParseReviewOutputStringLine:
    def test_string_line_coerced_to_int(self):
        result = parse_review_output(REVIEW_WITH_STRING_LINE)
        assert len(result.findings) == 1
        assert result.findings[0].line == 42
        assert isinstance(result.findings[0].line, int)
