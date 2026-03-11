"""Tests for review YAML schema validation."""

from theforge.schemas import validate_review_yaml


def _valid_review() -> dict:
    """Return a minimal valid review YAML structure."""
    return {
        "verdict": "APPROVE",
        "summary": "Clean implementation, all checks pass.",
        "findings": [],
        "spec_compliance": {"matches_spec": True, "mismatches": []},
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
        data["findings"] = [
            {"severity": "P1", "file": "x.py", "description": "Bug"}
        ]
        errors = validate_review_yaml(data)
        assert any("APPROVE" in e and "P1" in e for e in errors)

    def test_request_changes_without_p1_is_error(self):
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {"severity": "P2", "file": "x.py", "description": "Style nit"}
        ]
        errors = validate_review_yaml(data)
        assert any("REQUEST_CHANGES" in e and "P1" in e for e in errors)

    def test_missing_summary(self):
        data = _valid_review()
        del data["summary"]
        errors = validate_review_yaml(data)
        assert any("summary" in e for e in errors)

    def test_missing_spec_compliance(self):
        data = _valid_review()
        del data["spec_compliance"]
        errors = validate_review_yaml(data)
        assert any("spec_compliance" in e for e in errors)

    def test_missing_test_coverage(self):
        data = _valid_review()
        del data["test_coverage"]
        errors = validate_review_yaml(data)
        assert any("test_coverage" in e for e in errors)

    def test_finding_missing_file(self):
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {"severity": "P1", "file": "", "description": "Bug"}
        ]
        errors = validate_review_yaml(data)
        assert any("file" in e for e in errors)

    def test_finding_invalid_severity(self):
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {"severity": "P0", "file": "x.py", "description": "Critical"}
        ]
        errors = validate_review_yaml(data)
        assert any("severity" in e for e in errors)

    def test_non_dict_root(self):
        errors = validate_review_yaml("just a string")
        assert len(errors) == 1
        assert "mapping" in errors[0]
