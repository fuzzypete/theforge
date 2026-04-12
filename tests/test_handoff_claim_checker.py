"""Tests for the heuristic handoff fix-claim checker."""

from theforge.handoff_claim_checker import (
    HandoffClaimCheckResult,
    check_handoff_fix_claims,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


_P1_GAP = "gap in lagging component is silently skipped when delta is zero"
_P1_RETRY = "retry loop does not reset the counter between attempts"


# ---------------------------------------------------------------------------
# Basic detection
# ---------------------------------------------------------------------------


class TestFixClaimDetection:
    def test_no_prior_findings_returns_empty(self):
        result = check_handoff_fix_claims(
            "Fixed the lagging component gap issue.", prior_p1_descriptions=[]
        )
        assert result == HandoffClaimCheckResult()

    def test_empty_summary_returns_empty(self):
        result = check_handoff_fix_claims("", prior_p1_descriptions=[_P1_GAP])
        assert result == HandoffClaimCheckResult()

    def test_claim_unrelated_to_any_finding_is_skipped(self):
        # "fixed the config loader" has no token overlap with _P1_GAP
        result = check_handoff_fix_claims(
            "Fixed the config loader path resolution.",
            prior_p1_descriptions=[_P1_GAP],
        )
        assert result.flagged == []
        assert result.accepted == []

    def test_claim_about_prior_p1_without_backing_is_flagged(self):
        summary = "Ensures the lagging component gap is never silently skipped when delta is zero."
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP])
        assert len(result.flagged) == 1
        assert result.accepted == []
        flag = result.flagged[0]
        assert flag.related_finding == _P1_GAP
        assert flag.has_backing is False
        assert flag.flag_message is not None
        assert "no test or invariant" in flag.flag_message

    def test_claim_with_test_name_is_accepted(self):
        summary = "Fixes the lagging component gap — covered by test_gap_skipped_delta_zero."
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP])
        assert result.flagged == []
        assert len(result.accepted) == 1
        assert result.accepted[0].has_backing is True

    def test_claim_with_Test_suffix_is_accepted(self):
        summary = "Resolves the gap in lagging component — verified in GapSkipTest."
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP])
        assert result.flagged == []
        assert len(result.accepted) == 1

    def test_claim_with_invariant_phrase_is_accepted(self):
        summary = (
            "Ensures the lagging component gap is never silently skipped — "
            "by construction the delta guard runs before the skip path."
        )
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP])
        assert result.flagged == []
        assert len(result.accepted) == 1

    def test_claim_with_by_design_is_accepted(self):
        summary = "Fixes gap in lagging component by design of the new delta guard."
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP])
        assert result.flagged == []
        assert len(result.accepted) == 1


# ---------------------------------------------------------------------------
# Multi-finding / multi-sentence
# ---------------------------------------------------------------------------


class TestMultipleFindings:
    def test_multiple_prior_p1s_each_can_be_flagged(self):
        summary = (
            "Ensures the lagging component gap is no longer silently skipped. "
            "Fixes the retry loop counter so it no longer persists between attempts."
        )
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP, _P1_RETRY])
        assert len(result.flagged) == 2
        assert result.accepted == []

    def test_mixed_backed_and_unbacked_claims(self):
        summary = (
            "Fixes the lagging component gap — covered by test_gap_delta_zero. "
            "Resolves the retry counter issue so it no longer persists between attempts."
        )
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP, _P1_RETRY])
        assert len(result.accepted) == 1
        assert len(result.flagged) == 1
        assert result.accepted[0].related_finding == _P1_GAP
        assert result.flagged[0].related_finding == _P1_RETRY

    def test_claim_matched_to_first_overlapping_finding(self):
        # If a sentence overlaps with both, it should be matched to the first.
        summary = "Ensures gap in lagging component retry loop is fixed."
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP, _P1_RETRY])
        assert len(result.flagged) == 1
        assert result.flagged[0].related_finding == _P1_GAP


# ---------------------------------------------------------------------------
# Phrase / pattern coverage
# ---------------------------------------------------------------------------


class TestFixClaimPhrases:
    """Ensure all major fix-claim phrases are detected."""

    def _flagged_for(self, sentence: str) -> bool:
        result = check_handoff_fix_claims(sentence, prior_p1_descriptions=[_P1_GAP])
        return len(result.flagged) > 0 or len(result.accepted) > 0

    def test_no_longer_phrase(self):
        assert self._flagged_for("The lagging component gap is no longer silently skipped.")

    def test_ensures_never_phrase(self):
        assert self._flagged_for(
            "This ensures the lagging component gap can never be silently skipped."
        )

    def test_guarantees_phrase(self):
        assert self._flagged_for("The new guard guarantees the lagging component gap is handled.")

    def test_fixes_phrase(self):
        assert self._flagged_for("This commit fixes the lagging component gap delta issue.")

    def test_resolves_phrase(self):
        assert self._flagged_for("Resolves the gap in lagging component skipped by delta check.")

    def test_prevents_phrase(self):
        assert self._flagged_for(
            "The new guard prevents the lagging component gap from being skipped."
        )


# ---------------------------------------------------------------------------
# Backing evidence patterns
# ---------------------------------------------------------------------------


class TestBackingEvidence:
    def test_test_underscore_prefix_accepted(self):
        summary = "Fixes lagging component gap — see test_gap_skipped."
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP])
        assert result.accepted and result.accepted[0].has_backing is True

    def test_Tests_suffix_accepted(self):
        summary = "Fixes lagging component gap — see GapSkipTests."
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP])
        assert result.accepted and result.accepted[0].has_backing is True

    def test_invariant_keyword_accepted(self):
        summary = "Fixes lagging component gap; this is an invariant of the scheduler."
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP])
        assert result.accepted and result.accepted[0].has_backing is True

    def test_by_construction_accepted(self):
        summary = "Fixes lagging component gap by construction."
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP])
        assert result.accepted and result.accepted[0].has_backing is True

    def test_no_backing_is_flagged(self):
        summary = "Fixes lagging component gap."
        result = check_handoff_fix_claims(summary, prior_p1_descriptions=[_P1_GAP])
        assert result.flagged and result.flagged[0].has_backing is False


# ---------------------------------------------------------------------------
# Flag message format
# ---------------------------------------------------------------------------


class TestFlagMessage:
    def test_flag_message_contains_claim_and_finding(self):
        claim = "Ensures gap in lagging component is never silently skipped."
        result = check_handoff_fix_claims(claim, prior_p1_descriptions=[_P1_GAP])
        assert result.flagged
        msg = result.flagged[0].flag_message
        assert msg is not None
        assert "no test or invariant" in msg
        assert "verify independently" in msg
        # Related finding should be included
        assert "lagging component" in msg or _P1_GAP[:30] in msg

    def test_accepted_claim_has_no_flag_message(self):
        claim = "Fixes lagging gap — test_gap_delta_zero covers this."
        result = check_handoff_fix_calls(claim, prior_p1_descriptions=[_P1_GAP])
        assert result.accepted
        assert result.accepted[0].flag_message is None


# ---------------------------------------------------------------------------
# Typo fix helper for the test above
# ---------------------------------------------------------------------------

# (rename the mistyped function call in the last test above)
check_handoff_fix_calls = check_handoff_fix_claims
