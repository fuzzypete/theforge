"""Unit tests for assertion-based gate-contradiction classification.

A PASS gate contradicts exactly one class of claim: that tests/build/lint are
currently FAILING. It has no bearing on a claim that coverage is inadequate,
that acceptance evidence was never produced, or that a criterion remains
undemonstrated. ``asserts_gate_verifiable_failure`` must distinguish what a
finding *asserts* from what it is *about*, and fail closed.
"""

from __future__ import annotations

from theforge.coordinator.gate_contradiction import asserts_gate_verifiable_failure


class TestAssertsGateVerifiableFailure:
    """Only positive failure assertions are gate-contradictable; everything else blocks."""

    def test_positive_test_failure_assertion(self):
        assert asserts_gate_verifiable_failure("Test failure: 435 tests fail with ReferenceError")

    def test_positive_build_error_assertion(self):
        assert asserts_gate_verifiable_failure("Build error in compilation step prevents deploy")

    def test_positive_lint_failure_assertion(self):
        assert asserts_gate_verifiable_failure("Lint fail: unused import in module")

    def test_positive_does_not_compile(self):
        assert asserts_gate_verifiable_failure("The module does not compile after this change")

    def test_observed_inadequate_coverage_is_not_contradictable(self):
        """The exact shape that was wrongly suppressed: mentions tests, asserts inadequacy."""
        desc = (
            "The changed test suite only exercises mocked diagnose output and prompt "
            "string contents, so no sparse-body diagnose run on a representative "
            "landing-failure bug is shown to complete within budget."
        )
        assert not asserts_gate_verifiable_failure(desc)

    def test_missing_acceptance_evidence_is_not_contradictable(self):
        desc = "The third acceptance criterion is never demonstrated by any test."
        assert not asserts_gate_verifiable_failure(desc)

    def test_inadequate_coverage_wording_is_not_contradictable(self):
        desc = "Test coverage is inadequate; the new branch is not exercised."
        assert not asserts_gate_verifiable_failure(desc)

    def test_gate_self_indictment_is_not_contradictable(self):
        """A finding indicting the gate must never be dismissed on the gate's authority."""
        desc = (
            "Gate success detection substring-matches '0 failed' and would report PASS "
            "while ten or more tests fail."
        )
        assert not asserts_gate_verifiable_failure(desc)

    def test_test_double_detection_indictment_is_not_contradictable(self):
        desc = "The validation gate branches its behavior on test-double detection."
        assert not asserts_gate_verifiable_failure(desc)

    def test_subject_only_mention_is_not_contradictable(self):
        """Bare subject matter ('test suite') with no failure predicate keeps blocking."""
        assert not asserts_gate_verifiable_failure("The test suite covers the handler path.")

    def test_design_finding_unrelated_to_tests(self):
        assert not asserts_gate_verifiable_failure("Missing validation in the data pipeline.")

    def test_fails_closed_on_empty_description(self):
        assert not asserts_gate_verifiable_failure("")
