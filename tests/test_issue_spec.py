"""The typed issue contract: one specification, derived everywhere (#2725).

These tests pin the properties ADR-0009 decides, not the current wording of any
rule:

- the specification states, per type, the canonical label, the sections and
  their order, which are required and which forbidden, and the lifecycle states;
- the checker *derives* its structural rules from it — a rule changed in the
  specification changes what the gate enforces with no second edit;
- canonical and legacy heading spellings parse into the same typed document,
  and only the canonical spelling is ever rendered;
- a canonical body survives a round trip byte-for-byte;
- content the specification does not model is written back intact;
- the published reference cannot disagree with the specification.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from theforge.intake.shape_classify import Classification, Confidence, ShapeProposal
from theforge.intake.shape_render import restructure_body
from theforge.shape_check import check
from theforge.shape_check.diagnosis_spec import (
    BUG_SHAPE_REFERENCE_PATH,
    render_bug_shape_reference,
)
from theforge.shape_check.document import (
    parse_issue_document,
    render_issue_document,
    with_section,
)
from theforge.shape_check.issue_spec import (
    BUG_SPEC,
    ENHANCEMENT_SPEC,
    ISSUE_SHAPE_REFERENCE_PATH,
    ISSUE_TYPES,
    RECOGNIZED_TYPE_LABELS,
    SECTIONS,
    Presence,
    SectionRule,
    normalize_heading_text,
    spec_for_labels,
)
from theforge.shape_check.spec_reference import render_issue_shape_reference
from theforge.shape_check.types import ShapeVerdict

# tests/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent


# A canonical, gate-passing bug body: every heading is the canonical spelling,
# and the Diagnosis section carries every required field.
CANONICAL_BUG_BODY = (
    "## Observed\n"
    "\n"
    "`forge shape --apply` rewrote a gate-passing body into a failing one.\n"
    "\n"
    "## Expected\n"
    "\n"
    "A remediation offered toward a gate is a no-op on input that gate accepts.\n"
    "\n"
    "## Diagnosis\n"
    "\n"
    "- **Observed symptom:** the applied body no longer passes the gate.\n"
    "- **Evidence:** issue #2053 at baseline `f2caf7d`.\n"
    "- **Confirmed cause:** the renderer appended instead of round-tripping.\n"
    "- **Affected code path:** `intake.shape_render._restructure_bug`.\n"
    "- **Fix-success criterion:** the body is returned unchanged.\n"
)

LEGACY_BUG_BODY = CANONICAL_BUG_BODY.replace("## Observed\n", "## What happened\n").replace(
    "## Expected\n", "## What was expected\n"
)


class TestSpecificationIsData:
    """AC1: the specification states the contract, as data."""

    def test_every_type_declares_label_sections_order_and_lifecycle(self) -> None:
        assert ISSUE_TYPES
        for spec in ISSUE_TYPES:
            assert spec.label, f"{spec.key} has no canonical label"
            assert spec.section_rules, f"{spec.label} declares no sections"
            assert spec.lifecycle_states, f"{spec.label} declares no lifecycle states"
            # Order is the declaration order, and every rule names a real section.
            for rule in spec.section_rules:
                assert rule.section_key in SECTIONS
                assert isinstance(rule.presence, Presence)
            # Exactly one state may admit implementation, or none for the
            # deliberately non-dispatched types.
            admitting = [s for s in spec.lifecycle_states if s.admits_implementation]
            assert len(admitting) <= 1
            for state in spec.lifecycle_states:
                if not state.admits_implementation:
                    assert state.refusal_code, f"{spec.label}/{state.key} refuses with no code"

    def test_required_and_forbidden_are_stated_per_type(self) -> None:
        assert BUG_SPEC.requires("diagnosis")
        assert BUG_SPEC.forbids("acceptance_criteria")
        assert ENHANCEMENT_SPEC.requires("acceptance_criteria")
        assert ENHANCEMENT_SPEC.forbids("diagnosis")

    def test_recognized_labels_come_from_the_type_declarations(self) -> None:
        assert RECOGNIZED_TYPE_LABELS == frozenset(
            spec.label for spec in ISSUE_TYPES if spec.declares_type
        )
        assert spec_for_labels(["bug"]) is BUG_SPEC
        # Two type labels is not a declaration.
        assert spec_for_labels(["bug", "enhancement"]) is None

    def test_a_canonical_heading_is_its_own_alias(self) -> None:
        for section in SECTIONS.values():
            assert section.matches_heading(section.canonical_heading)
            assert section.matches_heading(section.canonical_heading + ":")
            assert normalize_heading_text(section.canonical_heading) in section.normalized_aliases


class TestCheckerDerivesFromSpecification:
    """AC2: the checker carries no second copy of the structural rules."""

    @staticmethod
    def _respecify(monkeypatch, replacement) -> None:
        """Swap one type's specification in, with no edit anywhere else.

        The whole point of AC2: the checker reads the specification at
        evaluation time, so replacing a type's data is the entire change.
        """
        monkeypatch.setattr(
            "theforge.shape_check.issue_spec.ISSUE_TYPES",
            tuple(replacement if s.key == replacement.key else s for s in ISSUE_TYPES),
            raising=True,
        )

    def test_changing_a_required_section_changes_what_the_gate_enforces(self, monkeypatch) -> None:
        # A bug's specification forbids acceptance criteria, so the gate does
        # not ask a bug for them.
        assert check("Rewrite bug", CANONICAL_BUG_BODY, ["bug"]).verdict is ShapeVerdict.RUNNABLE

        stricter = dataclasses.replace(
            BUG_SPEC,
            section_rules=tuple(
                SectionRule(rule.section_key, Presence.REQUIRED)
                if rule.section_key == "acceptance_criteria"
                else rule
                for rule in BUG_SPEC.section_rules
            ),
            contradiction=None,
        )
        self._respecify(monkeypatch, stricter)

        result = check("Rewrite bug", CANONICAL_BUG_BODY, ["bug"])
        assert result.verdict is ShapeVerdict.NEEDS_GROOMING_MISSING_AC
        assert any(r.code == "missing_acceptance_criteria" for r in result.reasons)

    def test_changing_a_forbidden_section_changes_what_the_gate_refuses(self, monkeypatch) -> None:
        body = CANONICAL_BUG_BODY + "\n## Steps to reproduce\n\n- run `forge shape --apply`\n"
        assert check("Rewrite bug", body, ["bug"]).verdict is ShapeVerdict.RUNNABLE

        forbidding = dataclasses.replace(
            BUG_SPEC,
            contradiction=dataclasses.replace(
                BUG_SPEC.contradiction,
                forbidden_section_keys=("reproduction",),
                slug="steps-to-reproduce",
            ),
        )
        self._respecify(monkeypatch, forbidding)

        result = check("Rewrite bug", body, ["bug"])
        assert result.verdict is ShapeVerdict.NEEDS_GROOMING_TYPE_SHAPE
        assert any(r.code == "type_shape_contradiction" for r in result.reasons)

    def test_observable_verb_vocabulary_is_no_longer_an_admission_input(self) -> None:
        # ADR-0009 clause 5: presence of criteria is structural, whether they
        # read as observable is semantic. Neither word choice, verb tense, nor
        # line wrapping decides admission any more.
        body = (
            "## What\n\nAdd a flag.\n\n"
            "## Example\n\n```text\n$ forge sprint --force\n```\n\n"
            "## Acceptance criteria\n\n"
            "- The behavior has been\n  written down somewhere sensible.\n"
        )
        result = check("Add a flag", body, ["enhancement"])
        assert result.verdict is ShapeVerdict.RUNNABLE
        assert not any(r.code == "no_observable_done_state" for r in result.reasons)

    def test_a_body_with_no_criteria_at_all_is_still_refused(self) -> None:
        result = check("Add a flag", "## What\n\nAdd a flag.\n", ["enhancement"])
        assert result.verdict is ShapeVerdict.NEEDS_GROOMING_MISSING_AC


class TestParseIsAliasBlind:
    """AC3: canonical and legacy spellings parse into the same document."""

    def test_legacy_and_canonical_bug_bodies_parse_identically(self) -> None:
        assert parse_issue_document(LEGACY_BUG_BODY, labels=["bug"]) == parse_issue_document(
            CANONICAL_BUG_BODY, labels=["bug"]
        )

    def test_rendering_emits_only_the_canonical_spelling(self) -> None:
        rendered = render_issue_document(parse_issue_document(LEGACY_BUG_BODY, labels=["bug"]))
        assert rendered == CANONICAL_BUG_BODY
        assert "What happened" not in rendered
        assert "What was expected" not in rendered

    def test_trailing_punctuation_is_the_same_heading(self) -> None:
        document = parse_issue_document("## Diagnosis:\n\nx\n")
        assert document.modeled_keys() == ("diagnosis",)
        assert render_issue_document(document) == "## Diagnosis\n\nx\n"


class TestRoundTrip:
    """AC4: a canonical body survives render(parse(body)) unchanged."""

    def test_canonical_bug_body_is_unchanged(self) -> None:
        assert (
            render_issue_document(parse_issue_document(CANONICAL_BUG_BODY, labels=["bug"]))
            == CANONICAL_BUG_BODY
        )

    def test_canonical_enhancement_body_is_unchanged(self) -> None:
        body = (
            "Some framing prose.\n"
            "\n"
            "## Acceptance criteria\n"
            "\n"
            "- the queue and the gate return one admission answer\n"
            "\n"
            "## Example\n"
            "\n"
            "```text\n"
            "## Diagnosis\n"
            "```\n"
        )
        assert render_issue_document(parse_issue_document(body, labels=["enhancement"])) == body

    def test_body_with_no_headings_survives(self) -> None:
        for body in ("", "just prose, no headings at all\n"):
            assert render_issue_document(parse_issue_document(body)) == body


class TestUnmodeledContentSurvives:
    """AC5: what the specification does not model is written back intact."""

    def test_unknown_sections_and_prose_are_preserved_verbatim(self) -> None:
        body = (
            "Framing prose the specification knows nothing about.\n"
            "\n"
            "## Why now\n"
            "\n"
            "Because the corpus says so.\n"
            "\n"
            "### A nested aside\n"
            "\n"
            "  indented, oddly spaced, and still ours\n"
            "\n"
            "## Diagnosis\n"
            "\n"
            "- **Confirmed cause:** unknown.\n"
        )
        document = parse_issue_document(body, labels=["bug"])
        assert document.unmodeled_headings() == ("Why now", "A nested aside")
        assert render_issue_document(document) == body

    def test_adding_a_section_leaves_everything_else_byte_identical(self) -> None:
        body = "## Why now\n\nBecause.\n\n## Diagnosis\n\n- **Confirmed cause:** unknown.\n"
        document = with_section(
            parse_issue_document(body, labels=["bug"]),
            "observed",
            "the gate refused a conforming body",
            type_spec=BUG_SPEC,
        )
        rendered = render_issue_document(document)
        assert "## Why now\n\nBecause.\n" in rendered
        assert "## Diagnosis\n\n- **Confirmed cause:** unknown.\n" in rendered
        assert "## Observed\n\nthe gate refused a conforming body\n" in rendered

    def test_a_heading_the_pattern_matches_but_no_alias_spells_is_not_rewritten(self) -> None:
        # "Observed behavior" is recognized by the gate as a symptom heading,
        # but its extra word is the author's, not ours to delete.
        body = "## Observed behavior\n\nit broke\n"
        assert render_issue_document(parse_issue_document(body, labels=["bug"])) == body


class TestProducerRendersThroughTheContract:
    """AC4/AC5 as the producer sees them (#2053)."""

    def _bug_proposal(self) -> ShapeProposal:
        return ShapeProposal(
            classification=Classification.BUG,
            confidence=Confidence.HIGH,
            proposed_labels=("bug",),
        )

    def test_restructure_is_a_no_op_on_a_gate_passing_canonical_body(self) -> None:
        assert check("Rewrite bug", CANONICAL_BUG_BODY, ["bug"]).admits_implementation_sprint
        assert restructure_body(self._bug_proposal(), CANONICAL_BUG_BODY) == CANONICAL_BUG_BODY

    def test_restructure_preserves_unmodeled_prose_while_repairing(self) -> None:
        body = "An aside worth keeping.\n\n## Notes\n\nand a section we do not model\n"
        repaired = restructure_body(self._bug_proposal(), body)
        assert repaired.startswith("An aside worth keeping.\n")
        assert "## Notes\n\nand a section we do not model\n" in repaired
        assert "## Diagnosis" in repaired

    def test_enhancement_repair_renders_the_canonical_heading(self) -> None:
        proposal = ShapeProposal(
            classification=Classification.ENHANCEMENT,
            confidence=Confidence.HIGH,
            proposed_labels=("enhancement",),
        )
        repaired = restructure_body(proposal, "rough idea\n")
        assert repaired.startswith("rough idea\n")
        assert "## Acceptance criteria" in repaired


class TestGeneratedReferenceCannotDrift:
    """AC6: CI fails when the docs and the specification disagree."""

    def test_issue_shape_reference_matches_the_specification(self) -> None:
        path = _REPO_ROOT / ISSUE_SHAPE_REFERENCE_PATH
        assert path.exists(), f"{ISSUE_SHAPE_REFERENCE_PATH} must exist"
        assert path.read_text(encoding="utf-8") == render_issue_shape_reference(), (
            f"{ISSUE_SHAPE_REFERENCE_PATH} is stale — regenerate it from "
            "theforge.shape_check.spec_reference.render_issue_shape_reference()"
        )

    def test_bug_shape_reference_matches_the_specification(self) -> None:
        path = _REPO_ROOT / BUG_SHAPE_REFERENCE_PATH
        assert path.read_text(encoding="utf-8") == render_bug_shape_reference()

    def test_every_type_and_section_appears_in_the_reference(self) -> None:
        rendered = render_issue_shape_reference()
        for spec in ISSUE_TYPES:
            assert f"## `{spec.label}`" in rendered
            for rule in spec.section_rules:
                assert SECTIONS[rule.section_key].canonical_heading_line in rendered
            for state in spec.lifecycle_states:
                assert f"`{state.key}`" in rendered
