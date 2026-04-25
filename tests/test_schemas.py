"""Tests for review YAML schema validation."""

from theforge.schemas import repair_review_yaml, validate_review_yaml


def _valid_review() -> dict:
    """Return a minimal valid review YAML structure."""
    return {
        "verdict": "APPROVE",
        "summary": "Clean implementation, all checks pass.",
        "findings": [],
        "story_compliance": {"matches_spec": True, "mismatches": []},
        "test_coverage": {"adequate": True, "gaps": []},
    }


class TestValidateReviewYaml:
    def test_valid_approve(self):
        errors = validate_review_yaml(_valid_review())
        assert errors == []

    def test_valid_request_changes(self):
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {
                "severity": "P1",
                "file": "src/foo.py",
                "line": 42,
                "description": "Off by one",
                "suggestion": "Use range(n+1)",
            }
        ]
        errors = validate_review_yaml(data)
        assert errors == []

    def test_invalid_verdict(self):
        data = _valid_review()
        data["verdict"] = "LGTM"
        errors = validate_review_yaml(data)
        assert any("verdict" in e for e in errors)

    def test_approve_with_p1_is_error(self):
        data = _valid_review()
        data["verdict"] = "APPROVE"
        data["findings"] = [{"severity": "P1", "file": "x.py", "description": "Bug"}]
        errors = validate_review_yaml(data)
        assert any("APPROVE" in e and "P1" in e for e in errors)

    def test_approve_with_closure_style_p1_is_error(self):
        """Closure-style acknowledgements in findings[] still trip APPROVE+P1 rejection."""
        data = _valid_review()
        data["verdict"] = "APPROVE"
        data["summary"] = "Prior cycle issue appears resolved."
        data["findings"] = [
            {
                "severity": "P1",
                "file": "src/foo.py",
                "line": 42,
                "description": "Prior P1 from Cycle 3 is fixed: null check was added",
                "suggestion": "Move this acknowledgement to summary or omit it",
            }
        ]
        errors = validate_review_yaml(data)
        assert any("APPROVE" in e and "P1" in e for e in errors)

    def test_request_changes_without_p1_is_error(self):
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [{"severity": "P2", "file": "x.py", "description": "Style nit"}]
        errors = validate_review_yaml(data)
        assert any("REQUEST_CHANGES" in e and "P1" in e for e in errors)

    def test_missing_summary(self):
        data = _valid_review()
        del data["summary"]
        errors = validate_review_yaml(data)
        assert any("summary" in e for e in errors)

    def test_missing_story_compliance(self):
        data = _valid_review()
        del data["story_compliance"]
        errors = validate_review_yaml(data)
        assert any("story_compliance" in e for e in errors)

    def test_missing_test_coverage(self):
        data = _valid_review()
        del data["test_coverage"]
        errors = validate_review_yaml(data)
        assert any("test_coverage" in e for e in errors)

    def test_finding_missing_file(self):
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [{"severity": "P1", "file": "", "description": "Bug"}]
        errors = validate_review_yaml(data)
        assert any("file" in e for e in errors)

    def test_finding_invalid_severity(self):
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [{"severity": "P0", "file": "x.py", "description": "Critical"}]
        errors = validate_review_yaml(data)
        assert any("severity" in e for e in errors)

    def test_non_dict_root(self):
        errors = validate_review_yaml("just a string")
        assert len(errors) == 1
        assert "mapping" in errors[0]

    def test_p1_with_file_and_null_line_is_error(self):
        """P1 finding with file set and line: null must be a validation error."""
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {
                "severity": "P1",
                "file": "src/foo.py",
                "line": None,
                "description": "Bug here",
                "suggestion": "Fix it",
            }
        ]
        errors = validate_review_yaml(data)
        assert any("line" in e for e in errors)

    def test_p1_with_null_file_and_null_line_is_valid(self):
        """P1 finding with file: null and line: null is valid (architectural finding)."""
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {
                "severity": "P1",
                "file": None,
                "line": None,
                "description": "Architecture violates separation of concerns",
                "suggestion": "Restructure the module boundaries",
            }
        ]
        errors = validate_review_yaml(data)
        assert errors == []

    def test_p2_with_file_and_null_line_is_valid(self):
        """P2 findings are not subject to the line-enforcement rule."""
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {
                "severity": "P1",
                "file": "src/foo.py",
                "line": 10,
                "description": "Blocking bug",
                "suggestion": "Fix it",
            },
            {
                "severity": "P2",
                "file": "src/foo.py",
                "line": None,
                "description": "Style nit",
                "suggestion": "Clean it up",
            },
        ]
        errors = validate_review_yaml(data)
        assert errors == []


# ── repair_review_yaml tests ────────────────────────────────────────


class TestRepairReviewYaml:
    def test_fills_missing_story_compliance(self):
        data = _valid_review()
        del data["story_compliance"]
        repair_review_yaml(data)
        assert data["story_compliance"] == {"matches_spec": True, "mismatches": []}

    def test_fills_missing_test_coverage(self):
        data = _valid_review()
        del data["test_coverage"]
        repair_review_yaml(data)
        assert data["test_coverage"] == {"adequate": True, "gaps": []}

    def test_does_not_flip_approve_with_p1(self):
        data = _valid_review()
        data["findings"] = [{"severity": "P1", "file": "foo.py", "line": 10, "description": "bug"}]
        repair_review_yaml(data)
        assert data["verdict"] == "APPROVE"

    def test_does_not_flip_request_changes_without_p1(self):
        """REQUEST_CHANGES with zero P1s is a schema error, not repaired.

        Flipping a rejection to approval would silently relax the integrity
        boundary. This should stay as REQUEST_CHANGES and fail validation.
        """
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {"severity": "P2", "file": "foo.py", "line": 10, "description": "style"}
        ]
        repair_review_yaml(data)
        assert data["verdict"] == "REQUEST_CHANGES"  # NOT flipped

    def test_findings_string_becomes_empty_list(self):
        data = _valid_review()
        data["findings"] = "No issues found"
        repair_review_yaml(data)
        assert data["findings"] == []

    def test_preserves_valid_data(self):
        data = _valid_review()
        original = dict(data)
        repair_review_yaml(data)
        assert data["verdict"] == original["verdict"]
        assert data["story_compliance"] == original["story_compliance"]
        assert data["test_coverage"] == original["test_coverage"]

    def test_contradictory_data_still_fails_validation(self):
        """DeepSeek-style output: APPROVE + P1 + missing sections must retry."""
        data = {
            "verdict": "APPROVE",
            "summary": "Looks good",
            "findings": [
                {
                    "severity": "P1",
                    "file": "x.py",
                    "line": 5,
                    "description": "bug",
                    "suggestion": "fix",
                }
            ],
        }
        repair_review_yaml(data)
        errors = validate_review_yaml(data)
        assert any("APPROVE" in error and "P1" in error for error in errors)
        assert data["verdict"] == "APPROVE"
