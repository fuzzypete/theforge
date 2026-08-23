"""Tests for ``theforge.triage_ratification`` pure data and render helpers.

Mirrors ``src/theforge/triage_ratification.py`` per the test mirror convention.
The flow-level application coverage lives in ``test_triage_ratification_flow.py``;
this module covers only the low-dependency vocabulary, dataclasses, and text
renderers in isolation.
"""

from __future__ import annotations

from theforge.triage_ratification import (
    DECISION_ACCEPT,
    DECISION_OVERRIDE,
    DECISION_SKIP,
    DECISIONS,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_RATIFIED,
    STATUS_SKIPPED,
    STATUS_STALE,
    TERMINAL_STATUSES,
    OperatorChoice,
    RatificationFindingOutcome,
    RatificationSummary,
    render_ratification_summary,
    render_reviewed_proposal,
)


class TestDecisionVocabulary:
    def test_exposes_expected_operator_decisions(self) -> None:
        assert DECISIONS == (
            DECISION_ACCEPT,
            DECISION_OVERRIDE,
            DECISION_SKIP,
        )

    def test_terminal_statuses_exclude_non_terminal_states(self) -> None:
        assert TERMINAL_STATUSES == (
            STATUS_APPLIED,
            STATUS_STALE,
            STATUS_SKIPPED,
        )
        assert STATUS_RATIFIED not in TERMINAL_STATUSES
        assert STATUS_FAILED not in TERMINAL_STATUSES


class TestOperatorChoice:
    def test_to_dict_preserves_optional_fields(self) -> None:
        choice = OperatorChoice(
            decision=DECISION_OVERRIDE,
            disposition="punt",
            target_milestone="Hygiene",
            punt_reason_code="verified-stale",
            evidence_refs=("symbol-absent", "commit-drift"),
            operator_note="review concurred after re-check",
        )

        assert choice.to_dict() == {
            "decision": "override",
            "disposition": "punt",
            "target_milestone": "Hygiene",
            "punt_reason_code": "verified-stale",
            "evidence_refs": ["symbol-absent", "commit-drift"],
            "operator_note": "review concurred after re-check",
        }


class TestRatificationSummary:
    def test_count_tallies_per_status(self) -> None:
        summary = RatificationSummary(
            triage_run_id="triage-123",
            findings=(
                RatificationFindingOutcome(
                    finding_id="f-1",
                    issue_ref="#1",
                    decision=DECISION_ACCEPT,
                    status=STATUS_APPLIED,
                ),
                RatificationFindingOutcome(
                    finding_id="f-2",
                    issue_ref="#2",
                    decision=DECISION_SKIP,
                    status=STATUS_SKIPPED,
                ),
                RatificationFindingOutcome(
                    finding_id="f-3",
                    issue_ref="#3",
                    decision=DECISION_ACCEPT,
                    status=STATUS_STALE,
                ),
            ),
        )

        assert summary.total == 3
        assert summary.count(STATUS_APPLIED) == 1
        assert summary.count(STATUS_SKIPPED) == 1
        assert summary.count(STATUS_STALE) == 1
        assert summary.count(STATUS_FAILED) == 0


class TestRenderReviewedProposal:
    def test_renders_snapshot_proposal_and_review_context(self) -> None:
        event = {
            "finding_id": "finding-1312",
            "issue_ref": "#1312",
            "proposal": {
                "disposition": "punt",
                "punt_reason_code": "verified-stale",
                "evidence_refs": ["symbol-absent"],
                "rationale": "The cited symbol no longer exists.",
            },
            "finding_snapshot": {
                "title": "remove dead audit symbol",
                "pool_state": "Hygiene",
                "verification_status": "stale_evidence",
            },
            "punt_review": {
                "verdict": "concur",
                "evidence_refs": ["symbol-absent"],
                "rationale": "Repository state still matches the staleness claim.",
            },
            "fallback_reason": "proposal parser fallback",
            "review_fallback_reason": "review parser fallback",
        }

        rendered = render_reviewed_proposal(event)

        assert "#1312  PROPOSE punt/verified-stale" in rendered
        assert "title: remove dead audit symbol" in rendered
        assert "snapshot: Hygiene, stale_evidence" in rendered
        assert "cites: symbol-absent" in rendered
        assert "reasoning (unverified): The cited symbol no longer exists." in rendered
        assert "punt review: concur" in rendered
        assert "review cites: symbol-absent" in rendered
        assert "reviewer reasoning (unverified): Repository state still matches" in rendered
        assert "fallback: proposal parser fallback" in rendered
        assert "review fallback: review parser fallback" in rendered

    def test_renders_milestone_target_for_fix_dispositions(self) -> None:
        rendered = render_reviewed_proposal(
            {
                "issue_ref": "#943",
                "proposal": {
                    "disposition": "fix_later",
                    "target_milestone": "Hygiene",
                },
            }
        )

        assert rendered == "#943  PROPOSE fix_later -> Hygiene"


class TestRenderRatificationSummary:
    def test_renders_status_counts_and_payload_details(self) -> None:
        summary = RatificationSummary(
            triage_run_id="triage-123",
            findings=(
                RatificationFindingOutcome(
                    finding_id="finding-1312",
                    issue_ref="#1312",
                    decision=DECISION_ACCEPT,
                    status=STATUS_APPLIED,
                    disposition="punt",
                    punt_reason_code="verified-stale",
                    summary="Closed with evidence-backed comment.",
                ),
                RatificationFindingOutcome(
                    finding_id="finding-943",
                    issue_ref="#943",
                    decision=DECISION_OVERRIDE,
                    status=STATUS_STALE,
                    disposition="fix_later",
                    target_milestone="Hygiene",
                    stale_reason="Live labels diverged from the reviewed snapshot.",
                ),
            ),
        )

        rendered = render_ratification_summary(summary)

        assert "TRIAGE RATIFICATION" in rendered
        assert "run triage-123; 2 finding(s)" in rendered
        assert "Applied: 1, stale: 1, skipped: 0, failed: 0" in rendered
        assert "#1312  APPLIED (accept punt/verified-stale)" in rendered
        assert "Closed with evidence-backed comment." in rendered
        assert "#943  STALE (override fix_later -> Hygiene)" in rendered
        assert "stale: Live labels diverged from the reviewed snapshot." in rendered
