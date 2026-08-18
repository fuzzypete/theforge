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
    DiagnosePhase,
    DiagnoseResult,
    DiagnoseState,
    DiagnosisArtifact,
    Hypothesis,
    PremiseAnchor,
    RelatedFinding,
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


class TestPremiseAnchorField:
    def test_artifact_carries_premise_anchors(self):
        artifact = DiagnosisArtifact(
            issue_number=1,
            observed_symptom="s",
            reproduction_or_evidence="r",
            hypotheses=(Hypothesis("h", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
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
            hypotheses=(Hypothesis("missing retry", "confirmed", "no retry wrapper"),),
            confirmed_cause="PLAN runner does not retry on connection-closed",
            affected_code_path="runner_claude.py",
            fix_success_criterion="connection-closed retries and yields a plan",
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


class TestDiagnoseOutputDestinations:
    def test_three_destinations_exposed(self):
        assert DIAGNOSE_OUTPUT_DESTINATIONS == frozenset({"comment", "body_section", "pr_to_body"})

    def test_destinations_are_immutable(self):
        # frozenset prevents accidental in-place mutation by callers.
        assert isinstance(DIAGNOSE_OUTPUT_DESTINATIONS, frozenset)


class TestHypothesis:
    def test_status_and_evidence_round_trip(self):
        h = Hypothesis(statement="x", status="confirmed", evidence="e")
        assert h.statement == "x"
        assert h.status == "confirmed"
        assert h.evidence == "e"

    def test_evidence_defaults_to_empty(self):
        h = Hypothesis(statement="x", status="inconclusive")
        assert h.evidence == ""


class TestDiagnosisArtifact:
    def _make(self, **overrides) -> DiagnosisArtifact:
        defaults = dict(
            issue_number=1,
            observed_symptom="x",
            reproduction_or_evidence="y",
            hypotheses=(Hypothesis("z", "confirmed", "e"),),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
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

    def test_partial_flag_does_not_affect_completeness(self):
        # is_complete checks structural fields only; partial is a separate
        # signal carried alongside complete YAML to flag budget/timeout exits.
        assert self._make(partial=True).is_complete()

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
            hypotheses=(Hypothesis("the hypothesis", "confirmed", "the evidence"),),
            confirmed_cause="c",
            affected_code_path="p",
            fix_success_criterion="f",
        )
        md = render_artifact_markdown(artifact)
        assert "[confirmed]" in md
        assert "the hypothesis" in md
        assert "Evidence: the evidence" in md

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
