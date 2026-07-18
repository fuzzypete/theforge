"""Symptom-verification test escalation for bug-fix review findings.

Pure Python, stdlib-only.  Consumed by ``review_phase`` to decide whether a
reviewer finding — that the seam-level integration test for the closing bug's
symptom path is *absent* — must be escalated from P2 to P1.

Why this exists (#1560)
-----------------------
The bug-fix lifecycle the system relies on is: bug filed → diagnosis → fix →
review → close → never recurs silently.  The last link is mechanical only if a
regression on the symptom path fails a test.  When a review panel notices that
the symptom-verification test is missing but files the finding as P2
(non-blocking), the fix ships, the test gap persists, and the next regression on
that path reaches operators undetected.  That is exactly what happened with
#1402 / #1407: the fix shipped APPROVE with a P2 flagging the missing seam-level
test, and six days later the symptom recurred on the same code path.

For bug-class stories, such a finding is load-bearing for shipping the fix, so
it is escalated to P1 and the merge blocks until the symptom-verification test
lands.

Scope discipline
----------------
This escalates *only* findings that assert an absent seam-level / integration /
end-to-end test for the failure mode the PR claims to fix.  It deliberately does
NOT fire on generic "test coverage could be higher" findings: the detector
requires BOTH an explicit missing-test assertion AND a seam/symptom-path signal,
and a bare coverage-gap remark carries neither seam signal, so it is left at P2.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .review import ReviewFinding

# ── Missing-test assertions ───────────────────────────────────────────────────
# Phrases that assert a test is *absent* — not merely that coverage could be
# richer.  A generic "coverage could be higher" remark matches none of these.
_MISSING_TEST_SIGNALS: tuple[str, ...] = (
    "no test",
    "no seam",
    "no integration test",
    "no end-to-end",
    "no end to end",
    "no e2e",
    "no regression test",
    "missing test",
    "missing seam",
    "missing integration",
    "missing a test",
    "missing an integration",
    "missing end-to-end",
    "does not test",
    "doesn't test",
    "not tested",
    "untested",
    "does not exercise",
    "doesn't exercise",
    "not exercised",
    "never exercised",
    "lacks a test",
    "lacks test",
    "lacks a seam",
    "lacks integration",
    "absent",
    "no coverage",
    "not covered end-to-end",
    "needs a seam",
    "needs an integration",
    "needs a regression test",
    "requires a seam",
    "requires an integration",
    "add a seam",
    "add an integration",
    "only exercises",
    "only tests",
    "only covers",
)

# ── Seam / symptom-path signals ───────────────────────────────────────────────
# The finding must point at the *symptom path* — a seam-level / integration /
# end-to-end test that exercises the failure mode across a boundary — rather than
# a generic unit-coverage gap.  Requiring one of these is the discriminator that
# keeps "test coverage could be higher" findings at P2 (AC3).
_SEAM_SYMPTOM_SIGNALS: tuple[str, ...] = (
    "seam-level",
    "seam level",
    "seam test",
    "integration test",
    "integration-level",
    "end-to-end",
    "end to end",
    "e2e",
    "symptom",
    "failure mode",
    "phase boundary",
    "state handoff",
    "cross-phase",
    "cross phase",
    "would catch",
    "would have caught",
    "recur",
    "boundary and state",
    "dependent dispatch",
)


def flags_missing_symptom_test(text: str) -> bool:
    """Return True when *text* asserts an absent seam-level symptom test.

    Fires only when the text carries BOTH a missing-test assertion AND a
    seam/symptom-path signal.  A generic coverage-gap remark carries no seam
    signal and therefore yields False — the finding stays at its reported
    severity.
    """
    t = text.lower()
    if not any(sig in t for sig in _MISSING_TEST_SIGNALS):
        return False
    return any(sig in t for sig in _SEAM_SYMPTOM_SIGNALS)


def _finding_text(finding: "ReviewFinding") -> str:
    """Concatenate a finding's prose fields for symptom-test detection.

    The reviewer may phrase the gap in any of observed / expected / evidence /
    suggestion (the #1407 finding cited the seam-level convention in its body and
    the missing driver in the same sentence), so all are considered together.
    """
    return " ".join(
        part
        for part in (
            finding.observed,
            finding.expected,
            finding.evidence,
            finding.suggestion or "",
        )
        if part
    )


def escalate_symptom_test_findings(
    findings: list["ReviewFinding"],
    *,
    is_bug_fix: bool,
) -> tuple[list["ReviewFinding"], list[dict]]:
    """Escalate P2 missing-symptom-test findings to P1 for bug-fix PRs.

    Returns ``(rewritten_findings, escalations)``.  ``rewritten_findings`` is a
    new list in original order; each P2 finding that (a) belongs to a bug-class
    story and (b) asserts an absent seam-level test for the symptom path is
    replaced with a P1 copy.  ``escalations`` is one audit dict per escalation so
    the rule's hit-rate becomes queryable in the audit substrate.

    When ``is_bug_fix`` is False the findings are returned unchanged and
    ``escalations`` is empty — the rule is specifically about bug-fix symptom
    verification (feature/enhancement PRs have different review obligations).
    """
    if not is_bug_fix:
        return list(findings), []

    rewritten: list["ReviewFinding"] = []
    escalations: list[dict] = []
    for finding in findings:
        if finding.severity == "P2" and flags_missing_symptom_test(_finding_text(finding)):
            rewritten.append(replace(finding, severity="P1"))
            escalations.append(
                {
                    "file": finding.file,
                    "line": finding.line,
                    "original_severity": "P2",
                    "effective_severity": "P1",
                    "reporter": finding.reporter,
                    "description": finding.description,
                }
            )
        else:
            rewritten.append(finding)
    return rewritten, escalations
