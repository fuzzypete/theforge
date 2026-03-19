"""Tests for review output parsing."""

from theforge.review import (
    ReviewFinding,
    ReviewResult,
    _best_individual_result,
    _try_parse_review,
    findings_to_markdown,
    parse_plan_review_output,
    parse_review_output,
)

VALID_APPROVE_YAML = """\
```yaml
verdict: APPROVE
summary: "Clean implementation, matches spec."
findings: []
spec_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
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
spec_compliance:
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
        assert result.spec_matches is True
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
        assert result.spec_matches is False
        assert len(result.spec_mismatches) == 1
        assert result.test_adequate is False
        assert len(result.test_gaps) == 1

    def test_bare_yaml_no_fences(self):
        bare = (
            "verdict: APPROVE\n"
            "summary: ok\n"
            "findings: []\n"
            "spec_compliance:\n"
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

    def test_yaml_with_extra_prose(self):
        text = (
            "Here is my review:\n\n"
            "```yaml\n"
            "verdict: APPROVE\n"
            "summary: all good\n"
            "findings: []\n"
            "spec_compliance:\n"
            "  matches_spec: true\n"
            "test_coverage:\n"
            "  adequate: true\n"
            "```\n\n"
            "Let me know if you have questions.\n"
        )
        result = parse_review_output(text)
        assert result.verdict == "APPROVE"


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

    def test_approve_with_p1_findings_allowed(self):
        """P1 findings are advisory in plan review — APPROVE is valid."""
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
        assert len(result.findings) == 1

    def test_approve_with_p0_findings_demoted_to_reject(self):
        """P0 findings are blocking — APPROVE with P0 is demoted to REJECT."""
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
        assert result.verdict == "REJECT"
        assert len(result.parse_errors) >= 1
        assert any("cannot approve" in e.lower() for e in result.parse_errors)

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
spec_compliance:
  matches_spec: true
  mismatches: []
test_coverage:
  adequate: true
  gaps: []
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

    def test_schema_error_returns_none(self):
        # Valid YAML but missing required fields
        bad = """\
```yaml
verdict: APPROVE
summary: "ok"
findings: []
```
"""
        result = _try_parse_review(bad)
        assert result is None

    def test_structured_data_path(self):
        data = {
            "verdict": "APPROVE",
            "summary": "ok",
            "findings": [],
            "spec_compliance": {"matches_spec": True, "mismatches": []},
            "test_coverage": {"adequate": True, "gaps": []},
        }
        result = _try_parse_review("", structured_data=data)
        assert result is not None
        assert result.verdict == "APPROVE"

    def test_structured_data_with_schema_error_returns_none(self):
        # Missing required fields
        data = {"verdict": "APPROVE", "summary": "ok", "findings": []}
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
        spec_matches=True,
        spec_mismatches=[],
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
