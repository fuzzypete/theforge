"""Tests for theforge.diagnose_types — pure-data types and markdown helpers.

Mirrors src/theforge/diagnose_types.py per the test_mirrors_source convention.
The flow-level integration tests live in test_diagnose_flow.py; this module
covers only the dataclasses and rendering/upsert helpers in isolation.
"""

from __future__ import annotations

import pytest

from theforge.diagnose_types import (
    DIAGNOSE_OUTPUT_DESTINATIONS,
    AbsentPremise,
    ClaimVerification,
    DiagnosePartialReason,
    DiagnosePhase,
    DiagnoseResult,
    DiagnoseState,
    DiagnosisArtifact,
    Hypothesis,
    PremiseAnchor,
    RelatedFinding,
    ScopeCoverageLocation,
    SupportProvenance,
    SymptomScopeCoverage,
    UncheckedPremise,
    render_already_resolved_markdown,
    render_artifact_markdown,
    upsert_diagnosis_section,
)


class TestDiagnosePhase:
    def test_phase_names_distinct_from_coordinator_phase(self):
        # The diagnose flow has its own state machine; its phase names must
        # not collide with coordinator.state.Phase semantics.
        from theforge.coordinator.state import Phase as CoordPhase

        coord_names = {p.name for p in CoordPhase}
        diag_names = {p.name for p in DiagnosePhase}
        # FETCH / INVESTIGATE / PARSE / LAND / TIMEOUT_PARTIAL / BUDGET_EXCEEDED
        # are diagnose-only — assert at least one is unique.
        assert "INVESTIGATE" in diag_names
        assert "INVESTIGATE" not in coord_names

    def test_required_phases_present(self):
        names = {p.name for p in DiagnosePhase}
        for required in (
            "INIT",
            "FETCH",
            "INVESTIGATE",
            "PARSE",
            "LAND",
            "DONE",
            "FAILED",
            "TIMEOUT_PARTIAL",
            "UNCLASSIFIED_PARTIAL",
            "VERIFY_PREMISE",
            "ALREADY_RESOLVED",
        ):
            assert required in names


class TestAlreadyResolvedRendering:
    def test_names_removing_commit_and_omits_diagnosis_heading(self):
        md = render_already_resolved_markdown(
            issue_number=1494,
            baseline_sha="deadbeefcafefeed",
            absent=(
                AbsentPremise(
                    file="src/mod.py",
                    pattern="def buggy_func",
                    removing_commit="817222bd1234",
                    removing_summary="drop the buggy path",
                ),
            ),
        )
        assert "already resolved" in md.lower()
        assert "817222bd1234"[:12] in md
        assert "def buggy_func" in md
        assert "src/mod.py" in md
        # Must NOT present as a fix-ready Diagnosis section: no confirmed-cause
        # scaffolding a shape gate would read as implementation-ready.
        assert "## Diagnosis" not in md
        assert "### Confirmed cause" not in md

    def test_renders_whole_file_removal(self):
        md = render_already_resolved_markdown(
            issue_number=1,
            baseline_sha="abc123",
            absent=(
                AbsentPremise(
                    file="src/gone.py", pattern="", removing_commit="ff00aa", removing_summary=""
                ),
            ),
        )
        assert "file removed" in md
        assert "src/gone.py" in md

    def test_renders_unable_to_check_premises_alongside_absent_ones(self):
        md = render_already_resolved_markdown(
            issue_number=1,
            baseline_sha="abc123",
            absent=(
                AbsentPremise(
                    file="src/gone.py", pattern="", removing_commit="ff00aa", removing_summary=""
                ),
            ),
            unable_to_check=(
                UncheckedPremise(
                    file="src/other.py",
                    pattern="def maybe_buggy",
                    reason="baseline artifact missing; premise not checked",
                ),
            ),
        )
        assert "Premises the coordinator could not verify" in md
        assert "src/other.py:def maybe_buggy" in md
        assert "baseline artifact missing" in md


class TestPremiseAnchorField:
    def test_artifact_carries_premise_anchors(self):
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="s",
            reproduction_or_evidence="r",
            hypotheses=(
                Hypothesis(
                    "h",
                    "confirmed",
                    "e",
                    claim_verification=ClaimVerification(
                        "source", "Checked against the target repository source."
                    ),
                ),
            ),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
            premise_anchors=(PremiseAnchor(file="a.py", pattern="def x"),),
        )
        assert artifact.premise_anchors[0].file == "a.py"
        # Optional metadata: does not affect completeness.
        assert artifact.is_complete()


class TestRelatedFindingsField:
    def _make(self, **overrides) -> DiagnosisArtifact:
        defaults = dict(
            issue_number=1672,
            observed_symptom="empty plan on connection close",
            reproduction_or_evidence="audit YAML shows empty plan",
            hypotheses=(
                Hypothesis(
                    "missing retry",
                    "confirmed",
                    "no retry wrapper",
                    claim_verification=ClaimVerification(
                        "source", "Checked against the target repository source."
                    ),
                ),
            ),
            confirmed_cause="PLAN runner does not retry on connection-closed",
            affected_code_path="runner_claude.py",
            fix_success_criterion="connection-closed retries and yields a plan",
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
        )
        defaults.update(overrides)
        return DiagnosisArtifact(**defaults)

    def test_artifact_carries_related_findings(self):
        artifact = self._make(
            related_findings=(
                RelatedFinding(summary="no process-group isolation", related="#1649"),
            ),
        )
        assert artifact.related_findings[0].related == "#1649"
        # Optional metadata: does not affect completeness.
        assert artifact.is_complete()

    def test_related_findings_default_empty(self):
        assert self._make().related_findings == ()

    def test_related_findings_do_not_count_as_substantive_content(self):
        # A run that produced only adjacent findings but no diagnosis of the
        # stated symptom has diagnosed nothing — related findings alone must not
        # clear the content floor (mirrors the notes exclusion).
        empty = self._make(
            observed_symptom="",
            reproduction_or_evidence="",
            hypotheses=(),
            confirmed_cause="",
            affected_code_path="",
            fix_success_criterion="",
            related_findings=(RelatedFinding(summary="adjacent bug", related="#1649"),),
        )
        assert not empty.has_substantive_content()

    def test_render_surfaces_related_findings_as_out_of_scope_section(self):
        artifact = self._make(
            related_findings=(
                RelatedFinding(summary="no process-group isolation", related="#1649"),
                RelatedFinding(summary="an unlinked adjacent defect", related=""),
            ),
        )
        md = render_artifact_markdown(artifact)
        assert "### Related findings (out of scope)" in md
        assert "out of scope for this fix" in md.lower()
        assert "no process-group isolation (related: #1649)" in md
        assert "- an unlinked adjacent defect" in md
        # The out-of-scope material must live in its own section, not inside the
        # confirmed-cause block that a dev implements.
        cause_idx = md.index("### Confirmed cause")
        related_idx = md.index("### Related findings")
        assert related_idx > cause_idx
        confirmed_block = md[cause_idx:related_idx]
        assert "process-group" not in confirmed_block

    def test_no_related_section_when_empty(self):
        md = render_artifact_markdown(self._make())
        assert "### Related findings" not in md


class TestAdvisoryRepairProposalField:
    def _make(self, **overrides) -> DiagnosisArtifact:
        defaults = dict(
            issue_number=2501,
            observed_symptom="renderer publishes notes into the diagnosis body",
            reproduction_or_evidence="issue body shows speculative fix prose under Notes",
            hypotheses=(
                Hypothesis(
                    "notes is the only free-text slot",
                    "confirmed",
                    "artifact schema has no typed field for repair advice",
                    claim_verification=ClaimVerification(
                        "source", "Checked against the target repository source."
                    ),
                ),
            ),
            confirmed_cause="DiagnosisArtifact has no advisory-typed repair field.",
            affected_code_path="src/theforge/diagnose_types.py",
            fix_success_criterion=(
                "Confirmed diagnosis renders without fix guesses reading as spec."
            ),
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
        )
        defaults.update(overrides)
        return DiagnosisArtifact(**defaults)

    def test_advisory_repair_proposal_default_empty(self):
        assert self._make().advisory_repair_proposal == ""

    def test_advisory_repair_proposal_does_not_count_as_substantive_content(self):
        artifact = self._make(
            observed_symptom="",
            reproduction_or_evidence="",
            hypotheses=(),
            confirmed_cause="",
            affected_code_path="",
            fix_success_criterion="",
            advisory_repair_proposal="Likely belongs in the landing renderer.",
        )
        assert not artifact.has_substantive_content()

    def test_render_marks_advisory_repair_proposal_explicitly(self):
        artifact = self._make(
            advisory_repair_proposal=(
                "Likely belongs in the tool-free single-shot API dispatch layer."
            ),
        )
        md = render_artifact_markdown(artifact)
        assert "### Advisory repair proposal" in md
        assert "Advisory only" in md
        assert "unverified repair idea" in md
        assert "tool-free single-shot API dispatch layer" in md

    def test_no_advisory_section_when_empty(self):
        assert "### Advisory repair proposal" not in render_artifact_markdown(self._make())


class TestDiagnoseOutputDestinations:
    def test_three_destinations_exposed(self):
        assert DIAGNOSE_OUTPUT_DESTINATIONS == frozenset({"comment", "body_section", "pr_to_body"})

    def test_destinations_are_immutable(self):
        # frozenset prevents accidental in-place mutation by callers.
        assert isinstance(DIAGNOSE_OUTPUT_DESTINATIONS, frozenset)


class TestHypothesis:
    def test_status_and_evidence_round_trip(self):
        h = Hypothesis(
            statement="x",
            status="confirmed",
            evidence="e",
            evidence_provenance=SupportProvenance("observed", "reproduced locally"),
            claim_verification=ClaimVerification("source", "checked in source"),
        )
        assert h.statement == "x"
        assert h.status == "confirmed"
        assert h.evidence == "e"
        assert h.claim_verification.verification_type == "source"
        assert h.evidence_provenance.source_type == "observed"

    def test_evidence_defaults_to_empty(self):
        h = Hypothesis(statement="x", status="inconclusive")
        assert h.evidence == ""
        assert h.claim_verification == ClaimVerification()
        assert h.evidence_provenance == SupportProvenance()

    def test_unrecognized_status_normalizes_to_inconclusive(self):
        h = Hypothesis(statement="x", status="maybe")
        assert h.status == "inconclusive"


class TestSupportProvenance:
    def test_unknown_default(self):
        assert SupportProvenance() == SupportProvenance("unknown", "")

    def test_unknown_normalizes_unrecognized_source_type(self):
        provenance = SupportProvenance(source_type="commit_message", detail="already asserted")
        assert provenance.source_type == "unknown"
        assert provenance.detail == "already asserted"


class TestClaimVerification:
    def test_unknown_default(self):
        assert ClaimVerification() == ClaimVerification("unknown", "")

    def test_unknown_normalizes_unrecognized_type(self):
        verification = ClaimVerification("filesystem", "checked somewhere")
        assert verification.verification_type == "unknown"
        assert verification.detail == "checked somewhere"

    def test_unrecognized_type_does_not_count_as_recorded_verification(self):
        verification = ClaimVerification("filesystem", "checked somewhere")
        assert not verification.has_recorded_verification_type()


class TestSymptomScopeCoverage:
    def test_non_categorical_record_is_complete_by_default(self):
        assert SymptomScopeCoverage().is_complete()
        assert SymptomScopeCoverage().satisfies_issue_requirement()

    def test_categorical_record_requires_valid_examined_locations(self):
        record = SymptomScopeCoverage(
            symptom_is_categorical=True,
            stated_scope="every sibling renderer",
            examined_locations=(
                ScopeCoverageLocation(
                    location="src/foo.py:render",
                    status="covered",
                    rationale="Same construct and same omission.",
                ),
            ),
        )
        assert record.is_complete()
        assert record.satisfies_issue_requirement(issue_requires_categorical_scope=True)

    def test_issue_level_categorical_requirement_rejects_default_record(self):
        assert not SymptomScopeCoverage().satisfies_issue_requirement(
            issue_requires_categorical_scope=True
        )

    def test_invalid_examined_location_status_breaks_completeness(self):
        record = SymptomScopeCoverage(
            symptom_is_categorical=True,
            stated_scope="every sibling renderer",
            examined_locations=(
                ScopeCoverageLocation(
                    location="src/foo.py:render",
                    status="maybe",
                    rationale="unclear",
                ),
            ),
        )
        assert not record.is_complete()


class TestDiagnosisArtifact:
    def _make(self, **overrides) -> DiagnosisArtifact:
        defaults = dict(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(
                Hypothesis(
                    "z",
                    "confirmed",
                    "e",
                    claim_verification=ClaimVerification(
                        "source", "Checked against the target repository source."
                    ),
                ),
            ),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
        )
        defaults.update(overrides)
        return DiagnosisArtifact(**defaults)

    def test_is_complete_true_for_full_artifact(self):
        assert self._make().is_complete()

    def test_is_complete_false_when_any_required_field_empty(self):
        assert not self._make(observed_symptom="").is_complete()
        assert not self._make(reproduction_or_evidence="   ").is_complete()
        assert not self._make(hypotheses=()).is_complete()
        assert not self._make(confirmed_cause="").is_complete()
        assert not self._make(affected_code_path="").is_complete()
        assert not self._make(fix_success_criterion="").is_complete()

    def test_is_complete_false_when_claim_verification_missing_for_substantive_claims(self):
        artifact = self._make(
            hypotheses=(Hypothesis("z", "confirmed", "e"),),
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
        )
        assert not artifact.is_complete()
        assert artifact.missing_required_fields() == ("hypotheses[0].claim_verification",)

    def test_is_complete_false_when_confirmed_cause_verification_missing(self):
        artifact = self._make(confirmed_cause_verification=ClaimVerification())
        assert not artifact.is_complete()
        assert artifact.missing_required_fields() == ("confirmed_cause_verification",)

    def test_missing_verification_metadata_is_not_lifecycle_blocking(self):
        # The strict schema signal stays; only its lifecycle weight changes. A
        # confirmed cause that renders verbatim is not a partial diagnosis just
        # because nobody recorded how it was checked (#2797).
        artifact = self._make(
            hypotheses=(Hypothesis("z", "confirmed", "e"),),
            confirmed_cause_verification=ClaimVerification(),
        )
        assert artifact.missing_required_fields() == (
            "hypotheses[0].claim_verification",
            "confirmed_cause_verification",
        )
        assert artifact.lifecycle_blocking_missing_fields() == ()
        assert artifact.nonblocking_missing_fields() == (
            "hypotheses[0].claim_verification",
            "confirmed_cause_verification",
        )

    def test_missing_diagnosis_content_stays_lifecycle_blocking(self):
        artifact = self._make(
            confirmed_cause="",
            confirmed_cause_verification=ClaimVerification(),
            affected_code_path="",
        )
        assert artifact.lifecycle_blocking_missing_fields() == (
            "confirmed_cause",
            "affected_code_path",
        )
        assert artifact.nonblocking_missing_fields() == ()

    def test_missing_categorical_scope_coverage_stays_audit_visible_only(self):
        artifact = self._make(
            symptom_scope_coverage=SymptomScopeCoverage(),
            confirmed_cause_verification=ClaimVerification(),
        )
        assert artifact.missing_required_fields(issue_requires_categorical_scope=True) == (
            "confirmed_cause_verification",
            "symptom_scope_coverage",
        )
        assert (
            artifact.lifecycle_blocking_missing_fields(issue_requires_categorical_scope=True) == ()
        )
        assert artifact.nonblocking_missing_fields(issue_requires_categorical_scope=True) == (
            "confirmed_cause_verification",
            "symptom_scope_coverage",
        )

    def test_empty_hypotheses_stays_audit_visible_only(self):
        artifact = self._make(
            hypotheses=(),
            confirmed_cause_verification=ClaimVerification(),
        )
        assert artifact.missing_required_fields() == (
            "hypotheses",
            "confirmed_cause_verification",
        )
        assert artifact.lifecycle_blocking_missing_fields() == ()
        assert artifact.nonblocking_missing_fields() == (
            "hypotheses",
            "confirmed_cause_verification",
        )

    def test_complete_artifact_has_no_lifecycle_blockers_or_metadata_gaps(self):
        artifact = self._make()
        assert artifact.lifecycle_blocking_missing_fields() == ()
        assert artifact.nonblocking_missing_fields() == ()

    def test_is_complete_false_when_claim_verification_type_is_unrecognized(self):
        artifact = self._make(
            hypotheses=(
                Hypothesis(
                    "z",
                    "confirmed",
                    "e",
                    claim_verification=ClaimVerification("filesystem", "checked somewhere"),
                ),
            ),
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
        )
        assert not artifact.is_complete()
        assert artifact.missing_required_fields() == ("hypotheses[0].claim_verification",)

    def test_is_complete_false_when_confirmed_cause_verification_type_is_unrecognized(self):
        artifact = self._make(
            confirmed_cause_verification=ClaimVerification("filesystem", "checked somewhere")
        )
        assert not artifact.is_complete()
        assert artifact.missing_required_fields() == ("confirmed_cause_verification",)

    def test_partial_flag_does_not_affect_completeness(self):
        # is_complete checks structural fields only; partial is a separate
        # signal carried alongside complete YAML to flag budget/timeout exits.
        assert self._make(partial=True).is_complete()

    def test_non_categorical_scope_coverage_stays_optional(self):
        artifact = self._make(
            symptom_scope_coverage=SymptomScopeCoverage(
                symptom_is_categorical=False,
                stated_scope="",
                examined_locations=(),
            )
        )
        assert artifact.is_complete()

    def test_categorical_scope_coverage_requires_examined_locations(self):
        missing_coverage = self._make(
            symptom_scope_coverage=SymptomScopeCoverage(
                symptom_is_categorical=True,
                stated_scope="every sibling renderer",
                examined_locations=(),
            )
        )
        assert not missing_coverage.is_complete()
        assert not missing_coverage.is_complete(issue_requires_categorical_scope=True)

        covered = self._make(
            symptom_scope_coverage=SymptomScopeCoverage(
                symptom_is_categorical=True,
                stated_scope="every sibling renderer",
                examined_locations=(
                    ScopeCoverageLocation(
                        location="src/foo.py:render_cli",
                        status="covered",
                        rationale="Same renderer construct and same omitted field.",
                    ),
                    ScopeCoverageLocation(
                        location="src/foo.py:render_web",
                        status="excluded",
                        rationale=(
                            "Sibling checked; different serializer already includes the field."
                        ),
                    ),
                ),
            )
        )
        assert covered.is_complete()
        assert covered.is_complete(issue_requires_categorical_scope=True)

    def test_issue_level_categorical_requirement_reports_missing_scope_coverage(self):
        artifact = self._make()
        assert artifact.is_complete()
        assert not artifact.is_complete(issue_requires_categorical_scope=True)
        assert artifact.missing_required_fields(issue_requires_categorical_scope=True) == (
            "symptom_scope_coverage",
        )

    def test_has_substantive_content_true_when_any_field_filled(self):
        base = dict(
            observed_symptom="",
            reproduction_or_evidence="",
            hypotheses=(),
            confirmed_cause="",
            affected_code_path="",
            fix_success_criterion="",
        )
        for field_name in base:
            if field_name == "hypotheses":
                filled = self._make(**{**base, field_name: (Hypothesis("real", "confirmed", ""),)})
            else:
                filled = self._make(**{**base, field_name: "content"})
            assert filled.has_substantive_content(), field_name

    def test_has_substantive_content_false_for_all_empty(self):
        empty = self._make(
            observed_symptom="",
            reproduction_or_evidence="   ",
            hypotheses=(),
            confirmed_cause="",
            affected_code_path="",
            fix_success_criterion="",
        )
        assert not empty.has_substantive_content()

    def test_has_substantive_content_false_for_blank_hypothesis_scaffold(self):
        # parse_diagnose_output turns `hypotheses: [{}]` into a Hypothesis with
        # blank statement/evidence and default status. A tuple of such blank
        # bullets is scaffolding, not investigative content — it must not clear
        # the content floor even though the hypotheses tuple is non-empty.
        scaffold = self._make(
            observed_symptom="",
            reproduction_or_evidence="",
            hypotheses=(Hypothesis(statement="", status="inconclusive", evidence=""),),
            confirmed_cause="",
            affected_code_path="",
            fix_success_criterion="",
        )
        assert not scaffold.has_substantive_content()
        with pytest.raises(ValueError):
            render_artifact_markdown(scaffold)

    def test_has_substantive_content_true_when_hypothesis_has_evidence_only(self):
        # A hypothesis with a blank statement but real evidence is still content.
        h_evidence = self._make(
            observed_symptom="",
            reproduction_or_evidence="",
            hypotheses=(Hypothesis(statement="", status="ruled_out", evidence="logs show X"),),
            confirmed_cause="",
            affected_code_path="",
            fix_success_criterion="",
        )
        assert h_evidence.has_substantive_content()


class TestDiagnoseState:
    def test_default_phase_is_init(self):
        s = DiagnoseState(issue_number=1)
        assert s.phase == DiagnosePhase.INIT
        assert s.phase_transitions == []

    def test_transition_appends_history_in_order(self):
        s = DiagnoseState(issue_number=1)
        s.transition(DiagnosePhase.FETCH, "t1")
        s.transition(DiagnosePhase.INVESTIGATE, "t2")
        s.transition(DiagnosePhase.DONE, "t3")
        assert s.phase == DiagnosePhase.DONE
        assert s.phase_transitions == [
            ("FETCH", "t1"),
            ("INVESTIGATE", "t2"),
            ("DONE", "t3"),
        ]


class TestDiagnoseResult:
    def test_carries_state_and_message(self):
        s = DiagnoseState(issue_number=42)
        r = DiagnoseResult(success=True, state=s, message="ok")
        assert r.success is True
        assert r.state is s
        assert r.message == "ok"


class TestRenderArtifactMarkdown:
    def test_renders_all_required_sections(self):
        artifact = DiagnosisArtifact(
            issue_number=42,
            observed_symptom="It crashes",
            reproduction_or_evidence="Run X then Y",
            hypotheses=(
                Hypothesis("A", "ruled_out", "no log"),
                Hypothesis("B", "confirmed", "stack trace points here"),
            ),
            confirmed_cause="B was right",
            affected_code_path="foo.py:10",
            fix_success_criterion="X no longer crashes",
        )
        md = render_artifact_markdown(artifact)
        for required in (
            "## Diagnosis",
            "### Observed symptom",
            "### Reproduction / evidence",
            "### Hypotheses tested",
            "### Confirmed cause",
            "### Affected code path",
            "### Fix-success criterion",
        ):
            assert required in md

    def test_includes_hypothesis_status_and_evidence(self):
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="s",
            reproduction_or_evidence="r",
            hypotheses=(
                Hypothesis(
                    "the hypothesis",
                    "confirmed",
                    "the evidence",
                    evidence_provenance=SupportProvenance(
                        "observed",
                        "reproduced in a failing test",
                    ),
                    claim_verification=ClaimVerification(
                        "source",
                        "checked in the inspected repository file",
                    ),
                ),
            ),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
        )
        md = render_artifact_markdown(artifact)
        assert "[confirmed]" in md
        assert "the hypothesis" in md
        assert "Evidence: the evidence" in md
        assert "Evidence provenance: observed" in md
        assert "reproduced in a failing test" in md
        assert "Claim verification:" not in md

    def test_prior_assertion_support_renders_as_restatement_not_corroboration(self):
        artifact = DiagnosisArtifact(
            issue_number=342,
            observed_symptom="diagnosis cites its own prior conclusion as independent",
            reproduction_or_evidence="operator quote captured in issue body",
            hypotheses=(
                Hypothesis(
                    "the schema lacks provenance",
                    "confirmed",
                    (
                        "independently confirmed by commit 858ec73a whose message states "
                        "the identical mechanism"
                    ),
                    evidence_provenance=SupportProvenance(
                        "prior_assertion",
                        "Commit 858ec73a already states the same mechanism.",
                    ),
                    claim_verification=ClaimVerification(
                        "attached_evidence",
                        "Only the attached commit message was available.",
                    ),
                ),
            ),
            confirmed_cause="Diagnosis support lacks observed-vs-restated provenance.",
            confirmed_cause_support=(
                "The same commit message already states the cause and the diagnosis "
                "described it as an independent fix."
            ),
            confirmed_cause_verification=ClaimVerification(
                "source_and_attached_evidence",
                "Confirmed in source and the attached report.",
            ),
            confirmed_cause_support_provenance=SupportProvenance(
                "prior_assertion",
                "Unmerged branch fix/mcp-live-surface-resources-dir already asserted the cause.",
            ),
            affected_code_path="src/theforge/diagnose_types.py",
            fix_success_criterion="Prior assertions render as restatements, not corroboration.",
        )
        md = render_artifact_markdown(artifact)
        assert "Claim verification: rests only on attached evidence." in md
        assert "Claim verification: verified against source and attached evidence." in md
        assert "Support provenance: prior_assertion" in md
        assert "restatement, not independent corroboration" in md
        assert "Evidence provenance: prior_assertion" in md
        assert "Independence note:" in md

    def test_confirmed_cause_independence_language_uses_support_provenance_caveat(self):
        artifact = DiagnosisArtifact(
            issue_number=342,
            observed_symptom="confirmed cause text itself claims independent confirmation",
            reproduction_or_evidence="operator quote captured in issue body",
            hypotheses=(
                Hypothesis(
                    "support provenance exists but the cause prose carried the claim",
                    "confirmed",
                    "Earlier diagnosis already stated the same mechanism",
                ),
            ),
            confirmed_cause=(
                "The renderer treated an earlier diagnosis as independently corroborating "
                "the same cause."
            ),
            confirmed_cause_support_provenance=SupportProvenance(
                "prior_assertion",
                "The cited earlier diagnosis already stated the cause.",
            ),
            affected_code_path="src/theforge/diagnose_types.py",
            fix_success_criterion="Confirmed-cause prose is caveated as a restatement.",
        )
        md = render_artifact_markdown(artifact)
        assert "Support: _(none recorded)_" in md
        assert "Support provenance: prior_assertion" in md
        assert "already stated this cause and is not independent corroboration" in md

    def test_duplicate_confirmed_cause_independence_notes_are_deduplicated(self):
        artifact = DiagnosisArtifact(
            issue_number=342,
            observed_symptom="both cause and support repeat the same independence claim",
            reproduction_or_evidence="operator quote captured in issue body",
            hypotheses=(
                Hypothesis(
                    "both fields restate the same prior assertion",
                    "confirmed",
                    "Earlier diagnosis already stated the same mechanism",
                ),
            ),
            confirmed_cause="The earlier diagnosis independently confirmed the same cause.",
            confirmed_cause_support=(
                "The earlier diagnosis independently confirmed the same cause."
            ),
            confirmed_cause_support_provenance=SupportProvenance(
                "prior_assertion",
                "The cited earlier diagnosis already stated the cause.",
            ),
            affected_code_path="src/theforge/diagnose_types.py",
            fix_success_criterion="The support block renders one caveat, not duplicates.",
        )
        md = render_artifact_markdown(artifact)
        assert md.count("Independence note:") == 1

    def test_independence_language_with_observed_provenance_gets_caveat(self):
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="s",
            reproduction_or_evidence="r",
            hypotheses=(
                Hypothesis(
                    "the hypothesis",
                    "confirmed",
                    "independently confirmed by the latest test run",
                    evidence_provenance=SupportProvenance(
                        "observed",
                        "test_red.py failed at HEAD",
                    ),
                    claim_verification=ClaimVerification("source", "checked at HEAD"),
                ),
            ),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
        )
        md = render_artifact_markdown(artifact)
        assert "Verify the cited material is a second source rather than a prior assertion." in md

    def test_unverifiable_hypothesis_renders_distinct_from_ruled_out(self):
        artifact = DiagnosisArtifact(
            issue_number=2672,
            observed_symptom="missing intake artifact looked like a negative result",
            reproduction_or_evidence="attached packet omitted intake candidate artifacts",
            hypotheses=(
                Hypothesis(
                    "the intake candidate was malformed at creation",
                    "unverifiable",
                    "intake candidate artifacts absent from bundle; not checked",
                    claim_verification=ClaimVerification(
                        "attached_evidence",
                        "No source copy of the artifact was available.",
                    ),
                ),
            ),
            confirmed_cause="",
            affected_code_path="src/theforge/diagnose_types.py",
            fix_success_criterion="missing artifacts render as unverifiable",
        )
        md = render_artifact_markdown(artifact)
        assert "[unverifiable]" in md
        assert "[ruled out]" not in md
        assert "attached evidence" in md

    def test_unchecked_premises_render_in_dedicated_section(self):
        artifact = DiagnosisArtifact(
            issue_number=2672,
            observed_symptom="premise check failed open silently",
            reproduction_or_evidence="baseline SHA unavailable",
            hypotheses=(Hypothesis("h", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
            unchecked_premises=(
                UncheckedPremise(
                    file="src/mod.py",
                    pattern="def buggy_func",
                    reason="baseline SHA unavailable; premise not checked",
                ),
            ),
        )
        md = render_artifact_markdown(artifact)
        assert "### Premise verification" in md
        assert "unable to check" in md
        assert "baseline SHA unavailable" in md

    def test_partial_artifact_renders_warning_block(self):
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "inconclusive", ""),),
            confirmed_cause="",
            affected_code_path="?",
            fix_success_criterion="?",
            partial=True,
        )
        md = render_artifact_markdown(artifact)
        assert "Partial diagnosis" in md
        assert "Operator review required" in md

    def test_unclassified_partial_artifact_does_not_claim_budget_or_timeout(self):
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "inconclusive", ""),),
            confirmed_cause="",
            affected_code_path="?",
            fix_success_criterion="?",
            partial=True,
            partial_reason=DiagnosePartialReason.UNCLASSIFIED,
        )
        md = render_artifact_markdown(artifact)
        assert "did not reach a confirmed cause" in md
        assert "budget or timeout" not in md

    def test_cause_found_partial_with_confirmed_cause_uses_generic_warning(self):
        artifact = DiagnosisArtifact(
            issue_number=2665,
            observed_symptom="A diagnose banner contradicts the artifact beneath it.",
            reproduction_or_evidence=(
                "Raw output shows a confirmed cause plus empty scope coverage."
            ),
            hypotheses=(
                Hypothesis(
                    "scope coverage was the only missing requirement",
                    "confirmed",
                    "missing_required_fields returns only symptom_scope_coverage",
                    claim_verification=ClaimVerification(
                        "source", "Checked against the target repository source."
                    ),
                ),
            ),
            confirmed_cause="The banner renderer keys only on partial_reason.",
            affected_code_path="src/theforge/diagnose_types.py",
            fix_success_criterion="The banner describes scope coverage, not cause failure.",
            partial=True,
            partial_reason=DiagnosePartialReason.UNCLASSIFIED,
            confirmed_cause_verification=ClaimVerification(
                "source", "Checked against the target repository source."
            ),
        )
        rendered = render_artifact_markdown(
            artifact,
            issue_requires_categorical_scope=True,
        )
        assert "confirmed a cause" in rendered
        assert "diagnosis is otherwise incomplete" in rendered
        assert "did not reach a confirmed cause" not in rendered

    def test_budget_partial_artifact_names_budget(self):
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "inconclusive", ""),),
            confirmed_cause="",
            affected_code_path="?",
            fix_success_criterion="?",
            partial=True,
            partial_reason=DiagnosePartialReason.BUDGET_EXCEEDED,
        )
        md = render_artifact_markdown(artifact)
        assert "exceeded its budget" in md

    def test_timeout_partial_artifact_names_timeout(self):
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "inconclusive", ""),),
            confirmed_cause="",
            affected_code_path="?",
            fix_success_criterion="?",
            partial=True,
            partial_reason=DiagnosePartialReason.TIMEOUT,
        )
        md = render_artifact_markdown(artifact)
        assert "timed out" in md

    def test_empty_required_field_renders_placeholder(self):
        # A partial artifact with *some* content renders explicit placeholders
        # for its blank fields, so it is never silently shown as a confident,
        # fully-populated section. At least one field must carry content — an
        # all-empty artifact is refused (see test below).
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="it crashes",
            reproduction_or_evidence="",
            hypotheses=(),
            confirmed_cause="",
            affected_code_path="",
            fix_success_criterion="",
            partial=True,
        )
        md = render_artifact_markdown(artifact)
        assert "_(empty)_" in md
        assert "_(none recorded)_" in md

    def test_all_empty_artifact_refuses_to_render(self):
        # Hardening: the renderer must never emit a Diagnosis section whose only
        # content is its own headings. An all-empty artifact (the shape a
        # killed/timed-out agent produces) is a failure to diagnose and must not
        # be landed into operator-visible state.
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="",
            reproduction_or_evidence="",
            hypotheses=(),
            confirmed_cause="",
            affected_code_path="",
            fix_success_criterion="",
            partial=True,
        )
        assert not artifact.has_substantive_content()
        with pytest.raises(ValueError):
            render_artifact_markdown(artifact)

    def test_notes_section_only_emitted_when_non_empty(self):
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
            notes="",
        )
        assert "### Notes" not in render_artifact_markdown(artifact)

        with_notes = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
            notes="caveat: only happens on Tuesdays",
        )
        rendered = render_artifact_markdown(with_notes)
        assert "### Notes" in rendered
        assert "Tuesdays" in rendered
        assert "### Advisory repair proposal" not in rendered

    def test_scope_coverage_section_only_emitted_for_categorical_symptoms(self):
        non_categorical = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
        )
        assert "### Stated symptom scope coverage" not in render_artifact_markdown(non_categorical)

        categorical = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
            symptom_scope_coverage=SymptomScopeCoverage(
                symptom_is_categorical=True,
                stated_scope="Every sibling renderer omitted the field",
                examined_locations=(
                    ScopeCoverageLocation(
                        location="src/foo.py:render_cli",
                        status="covered",
                        rationale="Same renderer helper omitted the field.",
                    ),
                    ScopeCoverageLocation(
                        location="src/foo.py:render_web",
                        status="excluded",
                        rationale=(
                            "Checked sibling path; different serializer already includes it."
                        ),
                    ),
                ),
            ),
        )
        rendered = render_artifact_markdown(categorical)
        assert "### Stated symptom scope coverage" in rendered
        assert "Every sibling renderer omitted the field" in rendered
        assert "[covered]" in rendered
        assert "[excluded]" in rendered


class TestUpsertDiagnosisSection:
    def test_appends_when_section_absent(self):
        body = "# Title\n\nBody text\n"
        new = upsert_diagnosis_section(body, "## Diagnosis\n\nDetails\n")
        assert new.startswith("# Title")
        assert "Body text" in new
        assert "## Diagnosis" in new
        assert "Details" in new

    def test_replaces_existing_section_preserving_later_sections(self):
        body = "# Title\n\nIntro\n\n## Diagnosis\n\nOld content\n\n## Other section\n\nKeep me\n"
        new = upsert_diagnosis_section(body, "## Diagnosis\n\nNew content\n")
        assert "Old content" not in new
        assert "New content" in new
        assert "Keep me" in new
        assert new.count("## Diagnosis") == 1

    def test_empty_body_returns_section_only(self):
        assert upsert_diagnosis_section("", "## Diagnosis\n\nx\n").rstrip() == "## Diagnosis\n\nx"

    def test_replacement_is_case_insensitive_for_heading_match(self):
        body = "## diagnosis\n\nlowercase old\n"
        new = upsert_diagnosis_section(body, "## Diagnosis\n\ncanonical new\n")
        assert "lowercase old" not in new
        assert "canonical new" in new

    def test_replaces_non_h2_diagnosis_heading_instead_of_appending(self):
        """A shape-authored ### Diagnosis placeholder must be reconciled in
        place, not left standing beside a newly appended artifact (#2263)."""
        body = (
            "# Title\n\nIntro\n\n"
            "### Diagnosis\n\nStatus: no diagnosis yet. Next step: run `forge diagnose`.\n"
        )
        new = upsert_diagnosis_section(body, "## Diagnosis\n\nLanded artifact content\n")
        assert "no diagnosis yet" not in new
        assert "Landed artifact content" in new
        assert new.lower().count("diagnosis") == new.lower().count("## diagnosis")

    def test_leaves_ordinary_prose_heading_mentioning_diagnosis_untouched(self):
        body = (
            "# Title\n\n"
            "## Further evidence — generated diagnosis text becomes scope-classification input\n\n"
            "narrative content\n\n"
            "## Diagnosis\n\nOld content\n"
        )
        new = upsert_diagnosis_section(body, "## Diagnosis\n\nNew content\n")
        assert (
            "## Further evidence — generated diagnosis text becomes "
            "scope-classification input" in new
        )
        assert "narrative content" in new
        assert "Old content" not in new
        assert "New content" in new

    def test_reconciles_preexisting_duplicate_canonical_sections(self):
        """A body that already carries two canonical Diagnosis sections (left
        by a prior append-instead-of-replace bug) collapses to one on the
        next landing, regardless of which duplicate came first (#2263)."""
        body = (
            "# Title\n\nIntro\n\n"
            "## Diagnosis\n\nFirst old content\n\n"
            "## Other section\n\nKeep me\n\n"
            "## Diagnosis\n\nSecond old content\n"
        )
        new = upsert_diagnosis_section(body, "## Diagnosis\n\nReconciled content\n")
        assert new.count("## Diagnosis") == 1
        assert "First old content" not in new
        assert "Second old content" not in new
        assert "Reconciled content" in new
        assert "Keep me" in new

    def test_reconciles_placeholder_after_artifact_in_reversed_order(self):
        """Order of the canonical sections in the body must not change the
        outcome: a single canonical Diagnosis section survives either way."""
        body = (
            "# Title\n\nIntro\n\n"
            "## Diagnosis\n\nLanded artifact\n\n"
            "### Diagnosis\n\nStatus: no diagnosis yet.\n"
        )
        new = upsert_diagnosis_section(body, "## Diagnosis\n\nReconciled content\n")
        assert new.lower().count("diagnosis") == new.lower().count("## diagnosis")
        assert new.count("## Diagnosis") == 1
        assert "Landed artifact" not in new
        assert "no diagnosis yet" not in new
        assert "Reconciled content" in new

    def test_preserves_operator_authored_root_cause_section(self):
        """Landing a ## Diagnosis artifact must not delete a distinct,
        operator-authored 'Root cause' section — that heading is a different
        section than the one being landed, not a duplicate of it (#2263
        review cycle 1)."""
        body = (
            "## Observed\n\nsecrets go missing\n\n"
            "## Root cause\n\n"
            "The operator's own investigation narrative: worktree creation "
            "races the .env copy step under high load.\n\n"
            "## Expected\n\nsecrets propagate\n"
        )
        new = upsert_diagnosis_section(body, "## Diagnosis\n\nLanded artifact content\n")
        assert "## Root cause" in new
        assert "races the .env copy step" in new
        assert "## Diagnosis" in new
        assert "Landed artifact content" in new
