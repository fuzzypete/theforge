"""Unit tests for symptom-verification test escalation (#1560).

Covers the pure detection + escalation logic:
- The #1407 finding (missing seam-level driver for the symptom path) is detected.
- Generic "test coverage could be higher" findings are NOT detected (AC3).
- Escalation only rewrites P2 findings, only for bug-fix PRs, and records an
  audit entry per escalation.
"""

from __future__ import annotations

from theforge.review import ReviewFinding
from theforge.symptom_test_classifier import (
    escalate_symptom_test_findings,
    flags_missing_symptom_test,
)


def _finding(severity: str = "P2", **kw) -> ReviewFinding:
    base = {
        "severity": severity,
        "file": "tests/test_sprint_parallel.py",
        "line": 1539,
        "observed": "",
        "suggestion": None,
        "expected": "",
        "evidence": "",
    }
    base.update(kw)
    return ReviewFinding(**base)


# ── flags_missing_symptom_test ────────────────────────────────────────────────


class TestDetector:
    def test_1407_finding_detected(self):
        """The exact #1407 P2 finding is recognised as a missing symptom test."""
        text = (
            "Project convention 8 requires seam-level integration tests for coordinator "
            "phase boundary and state handoff changes, but the new coverage only exercises "
            "_poll_queued_pr directly; no test drives run_sprint queued_prs through "
            "dependent dispatch."
        )
        assert flags_missing_symptom_test(text) is True

    def test_generic_coverage_finding_not_detected(self):
        """A generic coverage-gap remark carries no seam/symptom signal → not detected."""
        assert flags_missing_symptom_test("Test coverage could be higher.") is False
        assert flags_missing_symptom_test("Consider adding more unit tests here.") is False

    def test_missing_test_without_seam_signal_not_detected(self):
        """A missing-test assertion alone (no seam/symptom path) does not fire."""
        assert flags_missing_symptom_test("No test for the new helper function.") is False

    def test_seam_signal_without_missing_assertion_not_detected(self):
        """A seam mention where the test is present does not fire."""
        assert (
            flags_missing_symptom_test(
                "The seam-level integration test covers the phase boundary correctly."
            )
            is False
        )

    def test_end_to_end_symptom_gap_detected(self):
        assert (
            flags_missing_symptom_test(
                "The fix is not exercised end-to-end; no integration test reproduces the symptom."
            )
            is True
        )


# ── escalate_symptom_test_findings ────────────────────────────────────────────


class TestEscalation:
    def _symptom_finding(self, severity="P2") -> ReviewFinding:
        return _finding(
            severity=severity,
            observed=(
                "The new coverage only exercises _poll_queued_pr directly; no test drives "
                "run_sprint queued_prs through dependent dispatch."
            ),
            expected=(
                "Seam-level integration tests must cover the coordinator phase boundary "
                "and state handoff for the symptom path."
            ),
        )

    def test_p2_symptom_finding_escalated_for_bug(self):
        findings = [self._symptom_finding()]
        rewritten, escalations = escalate_symptom_test_findings(findings, is_bug_fix=True)
        assert rewritten[0].severity == "P1"
        assert len(escalations) == 1
        assert escalations[0]["original_severity"] == "P2"
        assert escalations[0]["effective_severity"] == "P1"
        assert escalations[0]["file"] == "tests/test_sprint_parallel.py"

    def test_not_escalated_for_non_bug(self):
        findings = [self._symptom_finding()]
        rewritten, escalations = escalate_symptom_test_findings(findings, is_bug_fix=False)
        assert rewritten[0].severity == "P2"
        assert escalations == []

    def test_generic_p2_not_escalated_for_bug(self):
        findings = [_finding(observed="Test coverage could be higher for this module.")]
        rewritten, escalations = escalate_symptom_test_findings(findings, is_bug_fix=True)
        assert rewritten[0].severity == "P2"
        assert escalations == []

    def test_existing_p1_untouched(self):
        """The rule only escalates P2s; an already-P1 finding is left as-is."""
        findings = [self._symptom_finding(severity="P1")]
        rewritten, escalations = escalate_symptom_test_findings(findings, is_bug_fix=True)
        assert rewritten[0].severity == "P1"
        assert escalations == []

    def test_order_preserved_and_mixed(self):
        findings = [
            _finding(observed="Generic coverage gap could be improved."),
            self._symptom_finding(),
        ]
        rewritten, escalations = escalate_symptom_test_findings(findings, is_bug_fix=True)
        assert [f.severity for f in rewritten] == ["P2", "P1"]
        assert len(escalations) == 1
