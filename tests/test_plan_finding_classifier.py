"""Tests for plan_finding_classifier: anchor extraction, matching, provenance."""

from __future__ import annotations

from theforge.plan_finding_classifier import (
    Anchor,
    MatchResult,
    extract_anchors,
    format_provenance,
    match_plan_findings,
    strip_reviewer_prefix,
)
from theforge.review import PlanReviewFinding

# ── Helpers ────────────────────────────────────────────────────────────────────


def _finding(description: str, severity: str = "P1") -> PlanReviewFinding:
    return PlanReviewFinding(severity=severity, description=description, suggestion=None)


def _anchor(value: str, kind: str) -> Anchor:
    return Anchor(value=value, kind=kind)  # type: ignore[arg-type]


# ── strip_reviewer_prefix ──────────────────────────────────────────────────────


class TestStripReviewerPrefix:
    def test_strips_simple_prefix(self):
        assert strip_reviewer_prefix("[reviewer-a] load_config is missing") == (
            "load_config is missing"
        )

    def test_strips_underscore_name(self):
        assert strip_reviewer_prefix("[codex_plan_reviewer] Something wrong") == (
            "Something wrong"
        )

    def test_strips_hyphen_name(self):
        result = strip_reviewer_prefix("[codex-plan-reviewer] The load_config function")
        assert result == "The load_config function"

    def test_no_prefix_unchanged(self):
        assert strip_reviewer_prefix("load_config is missing validation") == (
            "load_config is missing validation"
        )

    def test_empty_string(self):
        assert strip_reviewer_prefix("") == ""

    def test_prefix_only(self):
        assert strip_reviewer_prefix("[reviewer] ") == ""


# ── extract_anchors ────────────────────────────────────────────────────────────


class TestExtractAnchorsFilePaths:
    def test_simple_filename(self):
        anchors = extract_anchors("The coordinator.py module is too large")
        assert _anchor("coordinator.py", "file_path") in anchors

    def test_path_with_slashes(self):
        anchors = extract_anchors("See src/theforge/config.py for details")
        assert _anchor("src/theforge/config.py", "file_path") in anchors

    def test_no_extension_not_a_file_path(self):
        anchors = extract_anchors("The coordinator module needs work")
        values = {a.value for a in anchors}
        assert "coordinator" not in values

    def test_long_extension_not_a_file_path(self):
        # extensions >5 chars are not file paths
        anchors = extract_anchors("Check state.plan_attempt_metadata for this")
        values = {a.value for a in anchors if a.kind == "file_path"}
        assert not any("plan_attempt_metadata" in v for v in values)


class TestExtractAnchorsSnakeCase:
    def test_two_segment_snake(self):
        anchors = extract_anchors("load_config is missing validation")
        assert _anchor("load_config", "snake_ident") in anchors

    def test_three_segment_snake(self):
        anchors = extract_anchors("The run_agent_pool call fails")
        assert _anchor("run_agent_pool", "snake_ident") in anchors

    def test_single_segment_excluded(self):
        anchors = extract_anchors("config auth schema validation")
        kinds = {a.kind for a in anchors}
        assert "snake_ident" not in kinds

    def test_snake_inside_dotted_path_still_extracted(self):
        # state.load_config → load_config is also a snake_ident
        anchors = extract_anchors("state.load_config is broken")
        values_by_kind = {(a.kind, a.value) for a in anchors}
        assert ("snake_ident", "load_config") in values_by_kind

    def test_snake_not_extracted_from_filename(self):
        # load_config inside load_config.py should not produce a snake_ident anchor
        anchors = extract_anchors("load_config.py is missing")
        values_by_kind = {(a.kind, a.value) for a in anchors}
        assert ("file_path", "load_config.py") in values_by_kind
        assert ("snake_ident", "load_config") not in values_by_kind


class TestExtractAnchorsCamelCase:
    def test_two_hump_camel(self):
        anchors = extract_anchors("The loadConfig function is broken")
        assert _anchor("loadConfig", "camel_ident") in anchors

    def test_three_hump_camel(self):
        anchors = extract_anchors("checkAgentAuth should validate tokens")
        assert _anchor("checkAgentAuth", "camel_ident") in anchors

    def test_single_segment_excluded(self):
        anchors = extract_anchors("auth config")
        kinds = {a.kind for a in anchors}
        assert "camel_ident" not in kinds


class TestExtractAnchorsDottedPaths:
    def test_two_segment_dotted(self):
        anchors = extract_anchors("state.plan_attempt_metadata is populated")
        assert _anchor("state.plan_attempt_metadata", "dotted_path") in anchors

    def test_three_segment_dotted(self):
        anchors = extract_anchors("config.profiles.dev is missing")
        assert _anchor("config.profiles.dev", "dotted_path") in anchors

    def test_file_path_not_classified_as_dotted(self):
        anchors = extract_anchors("coordinator.py needs refactoring")
        kinds_for_coordinator = {a.kind for a in anchors if "coordinator" in a.value}
        assert "dotted_path" not in kinds_for_coordinator
        assert "file_path" in kinds_for_coordinator


class TestExtractAnchorsExclusions:
    def test_abbreviation_eg_excluded(self):
        anchors = extract_anchors("Use e.g. a config file")
        values = {a.value for a in anchors}
        assert "e.g" not in values

    def test_abbreviation_ie_excluded(self):
        anchors = extract_anchors("i.e. the function is wrong")
        values = {a.value for a in anchors}
        assert "i.e" not in values

    def test_abbreviation_etc_excluded(self):
        anchors = extract_anchors("handles configs, modules, etc.")
        values = {a.value for a in anchors}
        assert "etc" not in values

    def test_version_number_excluded(self):
        anchors = extract_anchors("Requires Python 3.12.1 or later")
        values = {a.value for a in anchors}
        assert "3.12.1" not in values
        assert "3.12" not in values

    def test_plan_step_excluded(self):
        anchors = extract_anchors("Step 3 load_config is not validated")
        # Step 3 should not be an anchor, but load_config should
        values = {a.value for a in anchors}
        assert "Step 3" not in values
        assert "load_config" in values

    def test_phase_label_excluded(self):
        anchors = extract_anchors("Phase 2 should include validate_input")
        values = {a.value for a in anchors}
        assert "Phase 2" not in values
        assert "validate_input" in values

    def test_single_char_segment_in_dotted_excluded(self):
        # e.g. "x.y" — second segment is single char → excluded
        anchors = extract_anchors("value x.y should match")
        values = {a.value for a in anchors}
        assert "x.y" not in values


class TestExtractAnchorsReviewerPrefix:
    def test_reviewer_prefix_not_extracted_as_anchor(self):
        """Reviewer name like codex-plan-reviewer must not become an anchor."""
        anchors = extract_anchors(
            strip_reviewer_prefix("[codex-plan-reviewer] The load_config function is broken")
        )
        values = {a.value for a in anchors}
        assert "codex-plan-reviewer" not in values
        assert "codex" not in values
        assert "load_config" in values

    def test_only_real_anchors_extracted_after_strip(self):
        anchors = extract_anchors(
            strip_reviewer_prefix("[reviewer-a] load_config.py missing validation")
        )
        values = {a.value for a in anchors}
        assert "load_config.py" in values
        assert "reviewer-a" not in values


# ── match_plan_findings ────────────────────────────────────────────────────────


class TestMatchPlanFindings:
    def test_file_plus_ident_match(self):
        """File path + shared identifier → match."""
        current = [_finding("coordinator.py load_config is not validated")]
        prior = [_finding("coordinator.py load_config missing null check")]
        results = match_plan_findings(current, prior)
        assert results[0].prior_index == 0
        assert results[0].abstain_reason is None

    def test_file_only_unmatched(self):
        """Shared file path but no other anchor → unmatched."""
        current = [_finding("coordinator.py is too large")]
        prior = [_finding("coordinator.py needs docstrings")]
        results = match_plan_findings(current, prior)
        assert results[0].prior_index is None
        assert "file-path-only" in (results[0].abstain_reason or "")

    def test_no_shared_anchor_unmatched(self):
        """Semantically similar but no shared structural anchor → unmatched."""
        current = [_finding("The configuration loader is missing validation")]
        prior = [_finding("Input checking is absent from the auth module")]
        results = match_plan_findings(current, prior)
        assert results[0].prior_index is None
        assert results[0].abstain_reason is not None

    def test_plan_section_reference_only_unmatched(self):
        """Shared plan-section label only → unmatched."""
        current = [_finding("Step 2 is incomplete")]
        prior = [_finding("Step 2 lacks implementation")]
        results = match_plan_findings(current, prior)
        assert results[0].prior_index is None

    def test_shared_non_file_anchor_match(self):
        """Shared snake_ident (no file path involved) → match."""
        current = [_finding("load_config is missing validation")]
        prior = [_finding("load_config does not check for None")]
        results = match_plan_findings(current, prior)
        assert results[0].prior_index == 0
        assert results[0].abstain_reason is None
        anchor_values = {a.value for a in results[0].shared_anchors}
        assert "load_config" in anchor_values

    def test_shared_anchor_different_prose_match(self):
        """Shared anchor despite different prose → match, anchor in provenance."""
        current = [_finding("load_config fails on empty input")]
        prior = [_finding("load_config crashes with an empty config")]
        results = match_plan_findings(current, prior)
        assert results[0].prior_index == 0
        anchor_values = {a.value for a in results[0].shared_anchors}
        assert "load_config" in anchor_values

    def test_no_extractable_anchors_unmatched_no_error(self):
        """Finding with no structural anchors → unmatched, no exception."""
        current = [_finding("Something is wrong here")]
        prior = [_finding("Something else is also wrong")]
        results = match_plan_findings(current, prior)
        assert results[0].prior_index is None
        assert results[0].abstain_reason is not None

    def test_empty_prior_all_unmatched(self):
        """No prior findings → all current are unmatched."""
        current = [_finding("load_config is broken"), _finding("validate_input fails")]
        results = match_plan_findings(current, [])
        assert all(r.prior_index is None for r in results)
        assert len(results) == 2

    def test_determinism(self):
        """Same inputs twice → identical output."""
        current = [
            _finding("load_config is missing validation"),
            _finding("coordinator.py is too large"),
        ]
        prior = [
            _finding("load_config does not check for None"),
            _finding("coordinator.py needs refactoring"),
        ]
        results_a = match_plan_findings(current, prior)
        results_b = match_plan_findings(current, prior)
        assert results_a == results_b

    def test_tie_break_uses_prose_similarity(self):
        """Multiple candidates with same anchor → Jaccard tie-break used."""
        current = [_finding("load_config is missing validation for empty inputs")]
        prior = [
            _finding("load_config should validate empty inputs carefully"),
            _finding("load_config needs a docstring update"),
        ]
        results = match_plan_findings(current, prior)
        # Both prior findings share load_config → tie-break needed
        assert results[0].prior_index is not None
        assert results[0].prose_similarity_used is True

    def test_cross_reviewer_prefix_match(self):
        """Findings from different reviewers match on shared anchor after prefix strip."""
        current = [_finding("[reviewer-a] load_config is missing validation")]
        prior = [_finding("[reviewer-b] load_config needs input checks")]
        results = match_plan_findings(current, prior)
        assert results[0].prior_index == 0
        anchor_values = {a.value for a in results[0].shared_anchors}
        assert "load_config" in anchor_values

    def test_multiple_findings_independent(self):
        """Each current finding is matched independently."""
        current = [
            _finding("load_config is broken"),
            _finding("validate_input is missing"),
        ]
        prior = [
            _finding("load_config has a bug"),
            _finding("validate_input is absent"),
        ]
        results = match_plan_findings(current, prior)
        assert len(results) == 2
        assert results[0].prior_index == 0
        assert results[1].prior_index == 1

    def test_dotted_path_match(self):
        """Two-segment dotted path is sufficient for a non-file match."""
        current = [_finding("state.plan_attempt_metadata is not updated")]
        prior = [_finding("state.plan_attempt_metadata missing after regen")]
        results = match_plan_findings(current, prior)
        assert results[0].prior_index == 0
        anchor_values = {a.value for a in results[0].shared_anchors}
        assert "state.plan_attempt_metadata" in anchor_values

    def test_camel_ident_match(self):
        """Shared camelCase identifier → match."""
        current = [_finding("loadConfig does not validate the schema")]
        prior = [_finding("loadConfig skips schema validation")]
        results = match_plan_findings(current, prior)
        assert results[0].prior_index == 0


# ── format_provenance ──────────────────────────────────────────────────────────


class TestFormatProvenance:
    def test_matched_finding_in_provenance(self):
        results = [
            MatchResult(
                current_index=0,
                prior_index=1,
                shared_anchors=(Anchor(value="load_config", kind="snake_ident"),),
                prose_similarity_used=False,
                abstain_reason=None,
            )
        ]
        prov = format_provenance(results)
        assert "finding[0]" in prov
        assert "prior[1]" in prov
        assert "snake_ident:load_config" in prov
        assert "prose" not in prov

    def test_unmatched_finding_in_provenance(self):
        results = [
            MatchResult(
                current_index=2,
                prior_index=None,
                shared_anchors=(),
                prose_similarity_used=False,
                abstain_reason="no shared structural anchors",
            )
        ]
        prov = format_provenance(results)
        assert "finding[2]" in prov
        assert "UNMATCHED" in prov
        assert "no shared structural anchors" in prov

    def test_prose_similarity_flag_in_provenance(self):
        results = [
            MatchResult(
                current_index=0,
                prior_index=0,
                shared_anchors=(Anchor(value="load_config", kind="snake_ident"),),
                prose_similarity_used=True,
                abstain_reason=None,
            )
        ]
        prov = format_provenance(results)
        assert "prose similarity" in prov

    def test_multiple_results(self):
        results = [
            MatchResult(
                current_index=0,
                prior_index=0,
                shared_anchors=(Anchor(value="load_config", kind="snake_ident"),),
                prose_similarity_used=False,
                abstain_reason=None,
            ),
            MatchResult(
                current_index=1,
                prior_index=None,
                shared_anchors=(),
                prose_similarity_used=False,
                abstain_reason="no extractable structural anchors in current finding",
            ),
        ]
        prov = format_provenance(results)
        assert "finding[0]" in prov
        assert "finding[1]" in prov
        assert "UNMATCHED" in prov

    def test_empty_results(self):
        assert format_provenance([]) == ""


# ── Registry integration ───────────────────────────────────────────────────────


class TestRegistryIntegration:
    """Verify that unmatched findings are preserved as independent new items."""

    def test_unmatched_findings_preserved(self):
        """Unmatched current findings are added to registry with disposition 'new'."""
        from theforge.coordinator.state import CoordinatorState, PlanFindingRecord

        state = CoordinatorState()
        current = [_finding("load_config is missing validation")]
        prior_as_findings: list[PlanReviewFinding] = []  # no prior

        results = match_plan_findings(current, prior_as_findings)
        assert results[0].prior_index is None

        # Simulate what plan_flow does: append unmatched as new
        for mr in results:
            if mr.prior_index is None:
                cf = current[mr.current_index]
                state.plan_finding_registry.append(
                    PlanFindingRecord(
                        description=strip_reviewer_prefix(cf.description),
                        severity=cf.severity,
                        cycle_first_seen=0,
                        cycle_last_seen=0,
                        disposition="new",
                    )
                )

        assert len(state.plan_finding_registry) == 1
        assert state.plan_finding_registry[0].disposition == "new"
        assert "load_config" in state.plan_finding_registry[0].description

    def test_matched_finding_updates_registry(self):
        """Matched findings update cycle_last_seen and stay 'unresolved'."""
        from theforge.coordinator.state import CoordinatorState, PlanFindingRecord

        state = CoordinatorState()
        # Pre-populate registry with a finding from cycle 0
        state.plan_finding_registry.append(
            PlanFindingRecord(
                description="load_config is missing validation",
                severity="P1",
                cycle_first_seen=0,
                cycle_last_seen=0,
                disposition="new",
            )
        )

        current = [_finding("load_config still missing validation")]
        prior_as_findings = [
            PlanReviewFinding(severity=r.severity, description=r.description, suggestion=None)
            for r in state.plan_finding_registry
        ]
        prior_registry_snapshot = list(state.plan_finding_registry)

        results = match_plan_findings(current, prior_as_findings)
        assert results[0].prior_index == 0

        # Update registry (simulating plan_flow logic)
        matched_indices: set[int] = set()
        for mr in results:
            if mr.prior_index is not None:
                prior_registry_snapshot[mr.prior_index].cycle_last_seen = 1
                prior_registry_snapshot[mr.prior_index].disposition = "unresolved"
                matched_indices.add(mr.prior_index)

        assert state.plan_finding_registry[0].cycle_last_seen == 1
        assert state.plan_finding_registry[0].disposition == "unresolved"
