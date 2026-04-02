"""Tests for plan regen trajectory filtering: consecutive_streak_dominant_theme
and build_filtered_regen_findings in plan_trajectory.py."""

from __future__ import annotations

from theforge.coordinator.plan_trajectory import (
    build_filtered_regen_findings,
    consecutive_streak_dominant_theme,
)
from theforge.coordinator.state import PlanFindingRecord
from theforge.plan_finding_classifier import MatchResult
from theforge.review import PlanReviewFinding

# ── Helpers ────────────────────────────────────────────────────────────────────


def _finding(description: str, severity: str = "P1", suggestion: str | None = None):
    return PlanReviewFinding(severity=severity, description=description, suggestion=suggestion)


def _record(
    description: str,
    severity: str = "P1",
    cycle_first_seen: int = 0,
    cycle_last_seen: int = 0,
    disposition: str = "unresolved",
) -> PlanFindingRecord:
    return PlanFindingRecord(
        description=description,
        severity=severity,
        cycle_first_seen=cycle_first_seen,
        cycle_last_seen=cycle_last_seen,
        disposition=disposition,
    )


def _match(current_index: int, prior_index: int | None) -> MatchResult:
    return MatchResult(
        current_index=current_index,
        prior_index=prior_index,
        shared_anchors=(),
        prose_similarity_used=False,
        abstain_reason=None if prior_index is not None else "no shared structural anchors",
    )


# ── consecutive_streak_dominant_theme ─────────────────────────────────────────


class TestConsecutiveStreakDominantTheme:
    def test_empty_registry_returns_empty(self):
        assert consecutive_streak_dominant_theme([], current_attempt=1) == ""

    def test_only_new_findings_no_streak(self):
        registry = [_record("load_config is broken", cycle_first_seen=1)]
        # first_seen == current_attempt → not recurring
        assert consecutive_streak_dominant_theme(registry, current_attempt=1) == ""

    def test_single_recurring_p1(self):
        registry = [_record("load_config is broken", cycle_first_seen=0, cycle_last_seen=1)]
        result = consecutive_streak_dominant_theme(registry, current_attempt=1)
        assert result == "load_config is broken"

    def test_fixed_finding_excluded(self):
        registry = [
            _record("load_config is broken", cycle_first_seen=0, disposition="fixed"),
        ]
        assert consecutive_streak_dominant_theme(registry, current_attempt=1) == ""

    def test_p2_finding_excluded(self):
        registry = [
            _record("load_config is broken", severity="P2", cycle_first_seen=0),
        ]
        assert consecutive_streak_dominant_theme(registry, current_attempt=1) == ""

    def test_longest_streak_wins(self):
        registry = [
            _record("short_streak issue", cycle_first_seen=1, cycle_last_seen=2),
            _record("long_streak issue", cycle_first_seen=0, cycle_last_seen=2),
        ]
        result = consecutive_streak_dominant_theme(registry, current_attempt=2)
        assert result == "long_streak issue"  # streak of 3 vs 2

    def test_tie_broken_alphabetically(self):
        registry = [
            _record("beta_issue fix needed", cycle_first_seen=0, cycle_last_seen=2),
            _record("alpha_issue fix needed", cycle_first_seen=0, cycle_last_seen=2),
        ]
        result = consecutive_streak_dominant_theme(registry, current_attempt=2)
        assert result == "alpha_issue fix needed"

    def test_p0_counts_as_blocking(self):
        registry = [_record("critical_path broken", severity="P0", cycle_first_seen=0)]
        result = consecutive_streak_dominant_theme(registry, current_attempt=1)
        assert result == "critical_path broken"


# ── build_filtered_regen_findings ─────────────────────────────────────────────


class TestBuildFilteredRegenFindings:
    # ── first attempt ──────────────────────────────────────────────────────────

    def test_attempt_zero_no_filtering(self):
        findings = [_finding("load_config broken", "P1"), _finding("bad file", "P2")]
        matches = [_match(0, None), _match(1, None)]
        registry: list[PlanFindingRecord] = []
        text, audit = build_filtered_regen_findings(findings, matches, 0, registry)
        assert audit["filtering_applied"] is False
        assert audit["reason"] == "first_attempt"
        assert "load_config broken" in text
        assert "bad file" in text

    # ── no recurring P1s ───────────────────────────────────────────────────────

    def test_no_recurring_p1s_all_new_unfiltered(self):
        findings = [_finding("load_config broken", "P1"), _finding("validate missing", "P2")]
        matches = [_match(0, None), _match(1, None)]
        registry: list[PlanFindingRecord] = []
        text, audit = build_filtered_regen_findings(findings, matches, 1, registry)
        assert audit["filtering_applied"] is False
        assert audit["reason"] == "no_recurring_p1s"
        assert "load_config broken" in text
        assert "validate missing" in text

    def test_only_recurring_p2s_no_filtering(self):
        """Recurring P2s without any recurring P1 → no filtering."""
        findings = [_finding("cosmetic_issue rename", "P2")]
        matches = [_match(0, 0)]  # recurring but P2
        registry = [_record("cosmetic_issue rename", severity="P2", cycle_first_seen=0)]
        text, audit = build_filtered_regen_findings(findings, matches, 1, registry)
        assert audit["filtering_applied"] is False
        assert "cosmetic_issue rename" in text

    # ── recurring P1s present → filtering active ───────────────────────────────

    def test_p2s_omitted_when_recurring_p1_exists(self):
        findings = [
            _finding("core_logic broken", "P1"),
            _finding("rename_test file", "P2"),
        ]
        # first is recurring (prior_index=0), second is new (prior_index=None)
        matches = [_match(0, 0), _match(1, None)]
        registry = [_record("core_logic broken", cycle_first_seen=0)]
        text, audit = build_filtered_regen_findings(findings, matches, 1, registry)
        assert audit["filtering_applied"] is True
        assert "core_logic broken" in text
        assert "rename_test file" not in text
        assert audit["p2_omitted_count"] == 1
        assert "core_logic broken" in audit["highlighted"]
        assert "rename_test file" in audit["filtered_out"]

    def test_recurring_p1s_listed_before_new_p1s(self):
        findings = [
            _finding("new_arch issue", "P1"),
            _finding("old_pattern missing", "P1"),
        ]
        # first is new, second is recurring
        matches = [_match(0, None), _match(1, 0)]
        registry = [_record("old_pattern missing", cycle_first_seen=0)]
        text, audit = build_filtered_regen_findings(findings, matches, 1, registry)
        assert audit["filtering_applied"] is True
        recurring_pos = text.index("old_pattern missing")
        new_pos = text.index("new_arch issue")
        assert recurring_pos < new_pos, "recurring P1 should appear before new P1"
        assert "Recurring findings" in text
        assert "New findings" in text

    def test_dominant_theme_called_out_in_backtrack(self):
        findings = [_finding("approval_path broken", "P1")]
        matches = [_match(0, 0)]
        registry = [_record("approval_path broken", cycle_first_seen=0, cycle_last_seen=1)]
        text, audit = build_filtered_regen_findings(findings, matches, 1, registry)
        assert audit["dominant_theme"] == "approval_path broken"
        assert "Dominant recurring theme" in text
        assert "approval_path broken" in text

    def test_no_dominant_theme_when_all_new_recurring(self):
        """Finding first seen at current_attempt: no dominant theme."""
        findings = [_finding("load_config broken", "P1")]
        matches = [_match(0, None)]  # new
        registry = [_record("load_config broken", cycle_first_seen=1, cycle_last_seen=1)]
        text, audit = build_filtered_regen_findings(findings, matches, 1, registry)
        # No recurring P1s → no filtering
        assert audit["filtering_applied"] is False

    def test_suggestion_included_for_recurring_p1(self):
        findings = [_finding("approval_path broken", "P1", suggestion="Use return_type=None")]
        matches = [_match(0, 0)]
        registry = [_record("approval_path broken", cycle_first_seen=0)]
        text, audit = build_filtered_regen_findings(findings, matches, 1, registry)
        assert "Use return_type=None" in text

    def test_audit_counts_accurate(self):
        findings = [
            _finding("arch_issue one", "P1"),  # recurring P1
            _finding("new_issue two", "P1"),  # new P1
            _finding("style_issue three", "P2"),  # recurring P2 → omitted
            _finding("style_issue four", "P2"),  # new P2 → omitted
        ]
        matches = [_match(0, 0), _match(1, None), _match(2, 1), _match(3, None)]
        registry = [
            _record("arch_issue one", cycle_first_seen=0),
            _record("style_issue three", severity="P2", cycle_first_seen=0),
        ]
        text, audit = build_filtered_regen_findings(findings, matches, 1, registry)
        assert audit["filtering_applied"] is True
        assert audit["recurring_p1_count"] == 1
        assert audit["new_p1_count"] == 1
        assert audit["p2_omitted_count"] == 2

    def test_p2_omit_notice_in_text(self):
        findings = [
            _finding("core_logic broken", "P1"),
            _finding("test_rename thing", "P2"),
        ]
        matches = [_match(0, 0), _match(1, None)]
        registry = [_record("core_logic broken", cycle_first_seen=0)]
        text, audit = build_filtered_regen_findings(findings, matches, 1, registry)
        assert "P2" in text
        assert "omitted" in text.lower()


# ── Regression: revived-from-fixed findings ────────────────────────────────────


class TestRevivedFromFixedFindings:
    """A P1 that was fixed in one attempt and reappears in a later attempt must NOT
    be treated as a genuinely recurring finding for filtering purposes.

    Without prior_dispositions, the registry update in plan_flow.py revives the
    old record by setting disposition="unresolved".  build_filtered_regen_findings
    must use prior_dispositions to detect this and classify the finding as 'new'.
    """

    def test_revived_p1_treated_as_new_not_recurring(self):
        """Finding A: first seen at attempt 0, fixed at attempt 1, reappears at attempt 2."""
        # The registry record was marked "fixed" before attempt 2
        findings = [_finding("auth_flow broken", "P1")]
        matches = [_match(0, 0)]  # matches prior record at index 0
        registry = [_record("auth_flow broken", cycle_first_seen=0, cycle_last_seen=2)]
        # Prior disposition at attempt 2: was "fixed" (fixed at attempt 1)
        prior_dispositions = {0: "fixed"}
        text, audit = build_filtered_regen_findings(
            findings, matches, 2, registry, prior_dispositions
        )
        # No genuinely recurring P1s → no filtering
        assert audit["filtering_applied"] is False
        assert audit["reason"] == "no_recurring_p1s"
        assert "auth_flow broken" in text

    def test_revived_p1_with_prior_dispositions_none_still_filters(self):
        """Without prior_dispositions, matched findings are treated as recurring."""
        findings = [_finding("auth_flow broken", "P1")]
        matches = [_match(0, 0)]
        registry = [_record("auth_flow broken", cycle_first_seen=0)]
        # prior_dispositions=None → treat all matched as recurring (no check)
        text, audit = build_filtered_regen_findings(findings, matches, 1, registry, None)
        assert audit["filtering_applied"] is True

    def test_consecutive_streak_excludes_revived_finding(self):
        """consecutive_streak_dominant_theme ignores findings that were 'fixed' before current."""
        registry = [
            _record("auth_flow broken", cycle_first_seen=0, cycle_last_seen=2),  # revived
            _record("new_real recurring", cycle_first_seen=1, cycle_last_seen=2),  # genuine
        ]
        prior_dispositions = {0: "fixed", 1: "unresolved"}
        result = consecutive_streak_dominant_theme(registry, 2, prior_dispositions)
        # auth_flow_broken was "fixed" before attempt 2, so excluded
        assert result == "new_real recurring"

    def test_consecutive_streak_without_prior_dispositions_includes_revived(self):
        """Without prior_dispositions, revived findings are still counted (backward compat)."""
        registry = [
            _record("auth_flow broken", cycle_first_seen=0, cycle_last_seen=2),
        ]
        # No prior_dispositions → revived finding included
        result = consecutive_streak_dominant_theme(registry, 2, None)
        assert result == "auth_flow broken"

    def test_p2_revived_finding_still_excluded_from_filtering(self):
        """A P2 revived finding matched to a prior P2 should not trigger filtering."""
        findings = [
            _finding("style_issue rename", "P2"),  # revived P2
        ]
        matches = [_match(0, 0)]
        registry = [_record("style_issue rename", severity="P2", cycle_first_seen=0)]
        prior_dispositions = {0: "fixed"}
        text, audit = build_filtered_regen_findings(
            findings, matches, 2, registry, prior_dispositions
        )
        # P2 revived, classified as new → no recurring P1s → no filtering
        assert audit["filtering_applied"] is False
        assert "style_issue rename" in text

    def test_mixed_revived_and_genuine_recurring_p1(self):
        """One revived P1 and one genuinely recurring P1: only the genuine one triggers filter."""
        findings = [
            _finding("revived_auth broken", "P1"),  # revived (was fixed)
            _finding("persistent_core issue", "P1"),  # genuinely recurring
        ]
        matches = [_match(0, 0), _match(1, 1)]
        registry = [
            _record("revived_auth broken", cycle_first_seen=0),
            _record("persistent_core issue", cycle_first_seen=0),
        ]
        prior_dispositions = {0: "fixed", 1: "unresolved"}
        text, audit = build_filtered_regen_findings(
            findings, matches, 2, registry, prior_dispositions
        )
        assert audit["filtering_applied"] is True
        assert audit["recurring_p1_count"] == 1
        # Only persistent_core in recurring; revived_auth classified as new
        assert "Recurring findings" in text
        assert "persistent_core issue" in text
        # revived_auth should appear under new findings
        assert "New findings" in text
        assert "revived_auth broken" in text
