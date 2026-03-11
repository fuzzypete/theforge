"""Tests for review output parsing."""

from theforge.review import (
    ReviewFinding,
    findings_to_markdown,
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
