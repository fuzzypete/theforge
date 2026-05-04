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
        "ac_verification": [
            {
                "criterion": "It does the thing",
                "status": "VERIFIED",
                "evidence": "src/foo.py:10-20 + tests/test_foo.py::test_thing",
            },
        ],
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
                "observed": "Off by one",
                "suggestion": "Use range(n+1)",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
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
            {
                "severity": "P1",
                "file": "x.py",
                "observed": "Bug",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
            }
        ]
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
                "observed": "Prior P1 from Cycle 3 is fixed: null check was added",
                "suggestion": "Move this acknowledgement to summary or omit it",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
            }
        ]
        errors = validate_review_yaml(data)
        assert any("APPROVE" in e and "P1" in e for e in errors)

    def test_request_changes_without_p1_is_error(self):
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {
                "severity": "P2",
                "file": "x.py",
                "observed": "Style nit",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
            }
        ]
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
        data["findings"] = [
            {
                "severity": "P1",
                "file": "",
                "observed": "Bug",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
            }
        ]
        errors = validate_review_yaml(data)
        assert any("file" in e for e in errors)

    def test_finding_invalid_severity(self):
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {
                "severity": "P0",
                "file": "x.py",
                "observed": "Critical",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
            }
        ]
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
                "observed": "Bug here",
                "suggestion": "Fix it",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
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
                "observed": "Architecture violates separation of concerns",
                "suggestion": "Restructure the module boundaries",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
            }
        ]
        errors = validate_review_yaml(data)
        assert errors == []

    def test_approve_without_ac_verification_is_error(self):
        """APPROVE with empty ac_verification table fails cross-validation."""
        data = _valid_review()
        data["ac_verification"] = []
        errors = validate_review_yaml(data)
        assert any("APPROVE" in e and "ac_verification" in e for e in errors)

    def test_approve_with_partial_ac_is_error(self):
        """APPROVE with any PARTIAL entry fails — silent contract swap guard."""
        data = _valid_review()
        data["ac_verification"] = [
            {
                "criterion": "Detection patterns cover Codex 'usage limit' signature",
                "status": "PARTIAL",
                "evidence": "Generic CLI fallback exists but no test for 'usage limit' string",
            },
        ]
        errors = validate_review_yaml(data)
        assert any("APPROVE" in e and "PARTIAL" in e for e in errors)

    def test_approve_with_not_verified_ac_is_error(self):
        """APPROVE with any NOT_VERIFIED entry fails."""
        data = _valid_review()
        data["ac_verification"] = [
            {
                "criterion": "Per-AC verification table exists in audit",
                "status": "NOT_VERIFIED",
                "evidence": "Could not locate audit-render code path",
            },
        ]
        errors = validate_review_yaml(data)
        assert any("APPROVE" in e and "NOT_VERIFIED" in e for e in errors)

    def test_request_changes_with_partial_ac_is_valid(self):
        """REQUEST_CHANGES with PARTIAL entries is fine — that's the whole point."""
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {
                "severity": "P1",
                "file": "src/foo.py",
                "line": 5,
                "observed": "Missing coverage for the production-observed symptom",
                "suggestion": "Add a test for the specific failure signature",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
            }
        ]
        data["ac_verification"] = [
            {
                "criterion": "Detection covers actual production symptom",
                "status": "PARTIAL",
                "evidence": "Implementation exists but production-observed signature untested",
            },
        ]
        errors = validate_review_yaml(data)
        assert errors == []

    def test_ac_verification_invalid_status_is_error(self):
        data = _valid_review()
        data["ac_verification"] = [
            {"criterion": "Foo", "status": "MAYBE", "evidence": "..."},
        ]
        errors = validate_review_yaml(data)
        assert any("ac_verification" in e and "status" in e for e in errors)

    def test_ac_verification_missing_evidence_is_error(self):
        data = _valid_review()
        data["ac_verification"] = [
            {"criterion": "Foo", "status": "VERIFIED", "evidence": ""},
        ]
        errors = validate_review_yaml(data)
        assert any("ac_verification" in e and "evidence" in e for e in errors)

    def test_ac_verification_must_be_list(self):
        data = _valid_review()
        data["ac_verification"] = "all good"
        errors = validate_review_yaml(data)
        assert any("ac_verification" in e and "list" in e for e in errors)

    def test_p2_with_file_and_null_line_is_valid(self):
        """P2 findings are not subject to the line-enforcement rule."""
        data = _valid_review()
        data["verdict"] = "REQUEST_CHANGES"
        data["findings"] = [
            {
                "severity": "P1",
                "file": "src/foo.py",
                "line": 10,
                "observed": "Blocking bug",
                "suggestion": "Fix it",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
            },
            {
                "severity": "P2",
                "file": "src/foo.py",
                "line": None,
                "observed": "Style nit",
                "suggestion": "Clean it up",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
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
        data["findings"] = [
            {
                "severity": "P1",
                "file": "foo.py",
                "line": 10,
                "observed": "bug",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
            }
        ]
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
            {
                "severity": "P2",
                "file": "foo.py",
                "line": 10,
                "observed": "style",
                "expected": "project-contract category rule (test fixture)",
                "evidence": "(test fixture evidence)",
            }
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
                    "observed": "bug",
                    "suggestion": "fix",
                    "expected": "project-contract category rule (test fixture)",
                    "evidence": "(test fixture evidence)",
                }
            ],
        }
        repair_review_yaml(data)
        errors = validate_review_yaml(data)
        assert any("APPROVE" in error and "P1" in error for error in errors)
        assert data["verdict"] == "APPROVE"
