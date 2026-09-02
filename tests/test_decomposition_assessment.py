"""The decomposition assessment's schema boundary (#2686).

The assessment is an artifact an operator acts on while deciding whether to
spend on a story as scoped. What it must never be is *partially* valid: a split
that silently drops an acceptance criterion, or points at a slice that does not
exist, reads exactly like a good one. These tests pin the rules that stop such
an artifact from reaching the pause, and pin "no assessment" as a first-class
result rather than an error.
"""

from __future__ import annotations

from theforge.decomposition_assessment import (
    MIN_SLICES,
    NONE_ATOMIC,
    NONE_INVALID_OUTPUT,
    NONE_NO_BLOCK,
    AssessmentPacket,
    parse_decomposition_assessment,
    render_assessment_lines,
)
from theforge.task.decomposition_assessment_prompts import (
    build_decomposition_assessment_prompt,
)

CRITERIA = ["the gate emits an assessment", "slices name a scope", "unsettled is stated"]


def _block(body: str) -> str:
    return f"prose before\n<decomposition_assessment>\n{body}\n</decomposition_assessment>\nafter"


VALID = """
atomic: false
slices:
  - id: 1
    title: "Parser and data contract"
    scope: "The pure-data types and the strict parser. Excludes every call site."
    depends_on: []
    covers_criteria: [1, 2]
  - id: 2
    title: "Wire it at the gate"
    scope: "Only the call site and its failure handling."
    depends_on: [1]
    covers_criteria: [3]
unsettled:
  - "Whether criterion 3 belongs with slice 2 or its own slice."
"""


class TestValidAssessments:
    def test_a_valid_split_parses_into_slices_edges_and_coverage(self):
        result = parse_decomposition_assessment(_block(VALID), CRITERIA)

        assert result.produced is True
        assert result.none_produced_reason is None
        first, second = result.assessment.slices
        assert first.slice_id == 1
        assert first.title == "Parser and data contract"
        assert "Excludes every call site" in first.scope
        assert first.depends_on == ()
        assert first.covers_criteria == (1, 2)
        assert second.depends_on == (1,)
        assert second.covers_criteria == (3,)

    def test_unsettled_decisions_survive_into_the_artifact(self):
        result = parse_decomposition_assessment(_block(VALID), CRITERIA)

        assert result.assessment.unsettled == (
            "Whether criterion 3 belongs with slice 2 or its own slice.",
        )

    def test_an_assessment_that_settled_everything_carries_no_unsettled_entries(self):
        body = VALID.split("unsettled:")[0]

        result = parse_decomposition_assessment(_block(body), CRITERIA)

        assert result.produced is True
        assert result.assessment.unsettled == ()

    def test_the_artifact_is_json_safe_for_the_pending_record_and_audit(self):
        import json  # noqa: PLC0415

        result = parse_decomposition_assessment(_block(VALID), CRITERIA)

        payload = result.assessment.to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["slices"][1]["depends_on"] == [1]

    def test_rendering_puts_scope_edges_and_coverage_on_one_line_per_slice(self):
        result = parse_decomposition_assessment(_block(VALID), CRITERIA)

        rendered = "\n".join(render_assessment_lines(result.assessment))

        assert "2 candidate slices" in rendered
        assert "1. Parser and data contract — scope:" in rendered
        assert "depends_on: 1" in rendered
        assert "covers AC 1, 2" in rendered
        assert "Unsettled:" in rendered

    def test_a_missing_acceptance_criteria_section_does_not_fail_coverage(self):
        """A story whose criteria could not be extracted still gets an assessment."""
        body = VALID.replace("covers_criteria: [1, 2]", "covers_criteria: []").replace(
            "covers_criteria: [3]", "covers_criteria: []"
        )

        result = parse_decomposition_assessment(_block(body), [])

        assert result.produced is True


class TestCoverageIsEnforced:
    def test_a_split_that_drops_a_criterion_is_refused(self):
        result = parse_decomposition_assessment(
            _block(VALID), [*CRITERIA, "the cost is recorded per run"]
        )

        assert result.produced is False
        assert result.none_produced_reason == NONE_INVALID_OUTPUT
        assert any("covered by no slice" in e for e in result.validation_errors)

    def test_covering_a_criterion_that_does_not_exist_is_refused(self):
        body = VALID.replace("covers_criteria: [3]", "covers_criteria: [3, 9]")

        result = parse_decomposition_assessment(_block(body), CRITERIA)

        assert result.produced is False
        assert any("does not exist" in e for e in result.validation_errors)

    def test_one_criterion_may_be_covered_by_more_than_one_slice(self):
        """Overlap is a design choice the operator can read, not a validation error."""
        body = VALID.replace("covers_criteria: [3]", "covers_criteria: [2, 3]")

        result = parse_decomposition_assessment(_block(body), CRITERIA)

        assert result.produced is True


class TestDependencyEdgesAreValidated:
    def test_an_edge_to_an_undeclared_slice_is_refused(self):
        body = VALID.replace("depends_on: [1]", "depends_on: [7]")

        result = parse_decomposition_assessment(_block(body), CRITERIA)

        assert result.produced is False
        assert any("undeclared slice 7" in e for e in result.validation_errors)

    def test_a_slice_that_depends_on_itself_is_refused(self):
        body = VALID.replace("depends_on: [1]", "depends_on: [2]")

        result = parse_decomposition_assessment(_block(body), CRITERIA)

        assert result.produced is False
        assert any("depends on itself" in e for e in result.validation_errors)

    def test_duplicate_slice_ids_are_refused(self):
        body = VALID.replace("  - id: 2", "  - id: 1").replace("depends_on: [1]", "depends_on: []")

        result = parse_decomposition_assessment(_block(body), CRITERIA)

        assert result.produced is False
        assert any("unique" in e for e in result.validation_errors)


class TestNoAssessmentIsAResult:
    def test_an_atomic_story_produces_no_assessment_and_says_why(self):
        result = parse_decomposition_assessment(
            _block("atomic: true\natomic_reason: One indivisible schema change."),
            CRITERIA,
        )

        assert result.produced is False
        assert result.none_produced_reason.startswith(NONE_ATOMIC)
        assert "One indivisible schema change." in result.none_produced_reason
        # An atomic result is not a validation failure — nothing was malformed.
        assert result.validation_errors == ()

    def test_an_atomic_story_without_a_reason_still_records_the_result(self):
        result = parse_decomposition_assessment(_block("atomic: true"), CRITERIA)

        assert result.produced is False
        assert result.none_produced_reason == NONE_ATOMIC

    def test_output_with_no_block_produces_no_assessment(self):
        result = parse_decomposition_assessment("I think this should be four stories.", CRITERIA)

        assert result.produced is False
        assert result.none_produced_reason == NONE_NO_BLOCK

    def test_a_block_only_inside_a_code_fence_does_not_count(self):
        """An agent quoting the schema in prose has not produced an artifact."""
        text = "```\n<decomposition_assessment>\natomic: true\n</decomposition_assessment>\n```"

        result = parse_decomposition_assessment(text, CRITERIA)

        assert result.produced is False
        assert result.none_produced_reason == NONE_NO_BLOCK

    def test_malformed_yaml_produces_no_assessment(self):
        result = parse_decomposition_assessment(_block("slices: [oops: ["), CRITERIA)

        assert result.produced is False
        assert result.none_produced_reason == NONE_INVALID_OUTPUT

    def test_a_non_mapping_block_produces_no_assessment(self):
        result = parse_decomposition_assessment(_block("- just\n- a list"), CRITERIA)

        assert result.produced is False
        assert result.none_produced_reason == NONE_INVALID_OUTPUT

    def test_a_single_slice_is_not_a_split(self):
        body = """
atomic: false
slices:
  - id: 1
    title: "The whole story"
    scope: "Everything."
    covers_criteria: [1, 2, 3]
"""

        result = parse_decomposition_assessment(_block(body), CRITERIA)

        assert result.produced is False
        assert any(f"at least {MIN_SLICES} slices" in e for e in result.validation_errors)

    def test_a_slice_without_a_scope_boundary_is_refused(self):
        body = VALID.replace('    scope: "Only the call site and its failure handling."\n', "")

        result = parse_decomposition_assessment(_block(body), CRITERIA)

        assert result.produced is False
        assert any("missing scope boundary" in e for e in result.validation_errors)

    def test_a_slice_without_a_title_is_refused(self):
        body = VALID.replace('    title: "Wire it at the gate"\n', "")

        result = parse_decomposition_assessment(_block(body), CRITERIA)

        assert result.produced is False
        assert any("missing title" in e for e in result.validation_errors)


class TestPromptCarriesTheEvidenceWithoutTheAdvisorVocabulary:
    def _packet(self) -> AssessmentPacket:
        return AssessmentPacket(
            story_name="the-story",
            issue_ref="#2686",
            story_body="## What\nSomething large.",
            acceptance_criteria=CRITERIA,
            complexity_score=9,
            implementation_complexity_score=9,
            validation_complexity_score=3,
            scope_exceeded=True,
            score_provenance_note="degraded preflight (timeout_no_verdict)",
            likely_files=["src/theforge/coordinator/preflight_complexity_gate.py"],
            warnings=["the spec leaves the cost bound open"],
            criteria_checked=[
                {
                    "criterion": "the gate emits an assessment",
                    "files_checked": ["src/theforge/coordinator/preflight_complexity_gate.py"],
                    "evidence": "The gate renders only the two commands today.",
                }
            ],
        )

    def test_the_prompt_carries_the_preflight_evidence_the_split_is_derived_from(self):
        prompt = build_decomposition_assessment_prompt(self._packet())

        assert "#2686" in prompt
        assert "Something large." in prompt
        assert "1. the gate emits an assessment" in prompt
        assert "projected complexity score: 9 (implementation 9, validation 3)" in prompt
        assert "scope_exceeded" in prompt
        assert "degraded preflight (timeout_no_verdict)" in prompt
        assert "preflight_complexity_gate.py" in prompt
        assert "the spec leaves the cost bound open" in prompt
        assert "The gate renders only the two commands today." in prompt

    def test_the_prompt_asks_for_an_artifact_not_a_recommendation(self):
        prompt = build_decomposition_assessment_prompt(self._packet())

        # The escalation advisor's action vocabulary must not leak in: this step
        # describes a shape, it does not choose what the operator should do.
        for action in ("land_core_defer_edges", "defer_or_abandon", "redirect", "elevate"):
            assert action not in prompt
        assert "recommendation" not in prompt.lower()
        assert "<decomposition_assessment>" in prompt
        assert "must not modify anything" in prompt

    def test_the_prompt_demands_coverage_of_every_criterion(self):
        prompt = build_decomposition_assessment_prompt(self._packet())

        assert f"Every one of the {len(CRITERIA)} acceptance criteria" in prompt

    def test_a_story_without_extracted_criteria_relaxes_the_coverage_rule(self):
        from dataclasses import replace  # noqa: PLC0415

        prompt = build_decomposition_assessment_prompt(
            replace(self._packet(), acceptance_criteria=[])
        )

        assert "No acceptance criteria were extracted" in prompt


class TestTheAssessmentAddsNoImportCycle:
    """The assessment's helpers come from concept-owning modules, not from phases.

    The first cut of this feature imported its shared helpers from wherever they
    happened to live — the clean-checkout helper from ``preflight_flow``, the
    launch classifier and AC extractor from ``escalation_advisor_flow``, the
    credential allow-list from ``triage_proposal_flow`` — and closed three import
    cycles doing it, because ``preflight_flow`` imports the gate that imports
    this module. Each helper now lives in the module that owns the *concept*
    (``baseline_checkout``, ``agent_failure``, ``task.story``, ``config.auth``),
    which is both acyclic and where a fourth caller would look first.
    """

    #: Modules this story added or rewired. A cycle through any of them is the
    #: regression; the repo's other, older cycles are not this test's subject.
    TOUCHED = (
        "theforge.coordinator.preflight_decomposition_flow",
        "theforge.coordinator.preflight_complexity_gate",
        "theforge.coordinator.escalation_advisor_flow",
        "theforge.coordinator.preflight_flow",
        "theforge.coordinator.baseline_checkout",
    )

    def _cycles(self) -> list[str]:
        from pathlib import Path  # noqa: PLC0415

        from theforge.conventions import _check_circular_imports  # noqa: PLC0415

        repo_root = Path(__file__).resolve().parent.parent
        return [v.detail for v in _check_circular_imports(repo_root, ("src/theforge",))]

    def test_no_cycle_runs_through_the_assessment_or_the_gate(self):
        offending = [
            detail for detail in self._cycles() if any(module in detail for module in self.TOUCHED)
        ]

        assert offending == [], "import cycle through the decomposition assessment: " + "; ".join(
            offending
        )

    def test_the_flow_imports_its_helpers_from_the_concept_owners(self):
        """Pinned by name: an alias re-export would satisfy the cycle check alone."""
        import ast  # noqa: PLC0415
        import inspect  # noqa: PLC0415

        from theforge.coordinator import preflight_decomposition_flow as flow  # noqa: PLC0415

        tree = ast.parse(inspect.getsource(flow))
        sources = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        # Relative imports parse with module="agent_failure" etc.
        assert "agent_failure" in sources
        assert "baseline_checkout" in sources
        assert "theforge.task.story" in sources
        assert "theforge.config.auth" in sources
        # And specifically NOT from the phase/flow modules it would cycle with.
        for forbidden in ("preflight_flow", "escalation_advisor_flow", "triage_proposal_flow"):
            assert forbidden not in sources
