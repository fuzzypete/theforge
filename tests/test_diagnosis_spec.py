"""Tests for the single declarative Diagnosis spec and its consumers (#1629).

The bug-shape gate's required Diagnosis components live in one declarative spec
(``theforge.shape_check.diagnosis_spec``); the validator, the gate finding
message, the intake remediation prompt, and the published skeleton all derive
from it. These tests pin that every consumer stays derived from the spec so the
producer/validator drift of #1629 cannot recur.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from theforge.intake.agent_rewrite import build_agent_rewrite_prompt
from theforge.intake.findings import IntakeFinding, IntakeSeverity
from theforge.shape_check import Shape, check
from theforge.shape_check.diagnosis_spec import (
    BUG_SHAPE_REFERENCE_PATH,
    REQUIRED_DIAGNOSIS_COMPONENTS,
    render_bug_shape_reference,
    render_bug_skeleton_body,
    required_diagnosis_tokens,
)
from theforge.shape_check.heuristics import (
    REQUIRED_DIAGNOSIS_TOKENS,
    check_bug_missing_diagnosis,
)

# Repo root: tests/ -> project root.
_REPO_ROOT = Path(__file__).resolve().parent.parent


# A compliant, implementation-ready bug body (shape of #1609/#1598).
_COMPLIANT_BODY = textwrap.dedent(
    """\
    ## Diagnosis

    - **Observed symptom.** Sprint resume false-skips zero-delta APPROVE stories.
    - **Evidence.** Run id `1ff6b0bb7992`, story #1102.
    - **Ruled out.** Workspace creation actually succeeds (verified in logs).
    - **Confirmed cause.** `_is_already_merged` requires at least one commit ahead.
    - **Affected code path.** sprint.runner._is_already_merged.
    - **Fix-success criterion.** Resume identifies zero-delta APPROVE as merged.
    """
)

# The content is all present, but the confirmed-cause component is written under
# a different label ("Root cause") than the one the validator matches. This is
# exactly the #1629 failure mode: complete RCA, mismatched label.
_LABEL_MISMATCH_BODY = textwrap.dedent(
    """\
    ## Diagnosis

    - **Observed symptom.** Sprint resume false-skips zero-delta APPROVE stories.
    - **Evidence.** Run id `1ff6b0bb7992`, story #1102.
    - **Ruled out.** Workspace creation actually succeeds (verified in logs).
    - **Root cause.** `_is_already_merged` requires at least one commit ahead.
    - **Affected code path.** sprint.runner._is_already_merged.
    - **Fix-success criterion.** Resume identifies zero-delta APPROVE as merged.
    """
)


class TestSpecIsSingleSource:
    def test_validator_tokens_derive_from_spec(self) -> None:
        # The heuristics module must not carry an independent list (#1629 AC1).
        assert REQUIRED_DIAGNOSIS_TOKENS == required_diagnosis_tokens()
        assert REQUIRED_DIAGNOSIS_TOKENS == tuple(c.token for c in REQUIRED_DIAGNOSIS_COMPONENTS)

    def test_every_component_bullet_contains_its_own_token(self) -> None:
        # Guarantees a skeleton bullet always satisfies the validator match.
        for component in REQUIRED_DIAGNOSIS_COMPONENTS:
            assert component.token in component.bullet().lower()


class TestSkeletonPassesByConstruction:
    def test_skeleton_body_passes_shape_gate(self) -> None:
        # AC4: a filing that starts from the skeleton passes by construction.
        result = check("Bug: something", render_bug_skeleton_body(), ["bug"])
        assert result.shape is Shape.RUNNABLE
        assert result.reasons == ()

    def test_skeleton_has_all_labels(self) -> None:
        skeleton = render_bug_skeleton_body()
        for component in REQUIRED_DIAGNOSIS_COMPONENTS:
            assert f"**{component.label}:**" in skeleton


class TestPublishedReferenceMatchesSpec:
    def test_doc_on_disk_matches_generator(self) -> None:
        # AC4: the published reference is checked against the same spec, so it
        # cannot drift from what the validator enforces.
        doc_path = _REPO_ROOT / BUG_SHAPE_REFERENCE_PATH
        assert doc_path.exists(), f"{BUG_SHAPE_REFERENCE_PATH} must exist"
        on_disk = doc_path.read_text(encoding="utf-8")
        assert on_disk == render_bug_shape_reference(), (
            "docs/reference/bug-shape.md is stale — regenerate it from "
            "theforge.shape_check.diagnosis_spec.render_bug_shape_reference()"
        )


class TestRemediationPromptEmbedsSpec:
    def test_prompt_embeds_labels_and_examples_verbatim(self) -> None:
        # AC2: the remediation agent aims at the same target the validator checks.
        finding = IntakeFinding(
            code="needs_diagnosis",
            severity=IntakeSeverity.BLOCK,
            location="body",
            problem="Bug Diagnosis section is incomplete.",
        )
        prompt = build_agent_rewrite_prompt("## What\nprose", [finding])
        for component in REQUIRED_DIAGNOSIS_COMPONENTS:
            assert f"**{component.label}:**" in prompt
            assert component.example in prompt
        assert BUG_SHAPE_REFERENCE_PATH in prompt

    def test_prompt_omits_spec_when_no_diagnosis_finding(self) -> None:
        finding = IntakeFinding(
            code="missing_example",
            severity=IntakeSeverity.BLOCK,
            location="examples",
            problem="No example.",
        )
        prompt = build_agent_rewrite_prompt("## What\nprose", [finding])
        assert "Diagnosis section requirements:" not in prompt


class TestFindingMessageQuotesLiteralLabel:
    def test_compliant_body_passes(self) -> None:
        # AC6: existing well-formed bodies (e.g. #1609, #1598) pass unchanged.
        assert check_bug_missing_diagnosis("T", _COMPLIANT_BODY, ["bug"]) is None

    def test_label_mismatch_message_quotes_missing_literal_label(self) -> None:
        # AC6: the finding message quotes the exact literal label the producer
        # must hit, plus its example and the shape reference.
        reason = check_bug_missing_diagnosis("T", _LABEL_MISMATCH_BODY, ["bug"])
        assert reason is not None
        assert reason.code == "needs_diagnosis"
        assert '"**Confirmed cause:**"' in reason.detail
        # The example for the missing component is quoted so the target is
        # unambiguous — not merely the component name.
        confirmed = next(c for c in REQUIRED_DIAGNOSIS_COMPONENTS if c.key == "confirmed_cause")
        assert confirmed.example in reason.detail
        assert BUG_SHAPE_REFERENCE_PATH in reason.detail
        # Components that ARE present must not be reported as missing.
        assert "**Observed symptom:**" not in reason.detail

    def test_pipeline_flags_label_mismatch(self) -> None:
        result = check("Bug: foo", _LABEL_MISMATCH_BODY, ["bug"])
        assert result.shape is Shape.NEEDS_GROOMING
        assert any(r.code == "needs_diagnosis" for r in result.reasons)

    def test_no_section_message_quotes_every_literal_label_and_example(self) -> None:
        # AC3: the completely-absent-section branch must also quote the literal
        # labels + examples from the spec, not bare component names — the
        # producer must see the same target the validator checks (iter 1 P1).
        reason = check_bug_missing_diagnosis("T", "## What\nprose only", ["bug"])
        assert reason is not None
        assert reason.code == "needs_diagnosis"
        for component in REQUIRED_DIAGNOSIS_COMPONENTS:
            assert f'"**{component.label}:**"' in reason.detail
            assert component.example in reason.detail
        assert BUG_SHAPE_REFERENCE_PATH in reason.detail
