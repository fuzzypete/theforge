"""Seam tests: a `forge diagnose` artifact read by the shape gate (#2060).

`forge diagnose` renders its artifact with :func:`render_artifact_markdown` and
lands the result as the issue body; the shape gate then derives fix-readiness
from that body. The two sides wrote and read the confirmed-cause field in
different shapes — heading form out, bullet form in — so every diagnose-landed
artifact derived as implementation-ready regardless of whether a cause had been
confirmed, including the honest-refusal case the diagnose prompt asks for.

These tests pin the seam itself: the verdict a rendered artifact receives, and
the requirement that it not depend on which of the two admissible shapes the
field was written in.
"""

from __future__ import annotations

import textwrap

import pytest

from theforge.diagnose_types import (
    DiagnosisArtifact,
    Hypothesis,
    SupportProvenance,
    render_artifact_markdown,
)
from theforge.intake.groom_flow import BugDiagnosisState, classify_bug_diagnosis
from theforge.shape_check.diagnosis_spec import REQUIRED_DIAGNOSIS_COMPONENTS
from theforge.shape_check.heuristics import (
    cause_assertion_state,
    check_bug_missing_diagnosis,
    derive_fix_ready,
    diagnosis_completeness,
)


def _artifact(confirmed_cause: str) -> DiagnosisArtifact:
    return DiagnosisArtifact(
        issue_number=2060,
        observed_symptom="A diagnose artifact with no confirmed cause derives as fix-ready.",
        reproduction_or_evidence="Rendered at baseline `f2caf7d` and passed to derive_fix_ready.",
        hypotheses=(
            Hypothesis(
                statement="the extractor never reads the heading form",
                status="confirmed",
                evidence="`_extract_confirmed_cause_value` stripped bullet markers only",
            ),
        ),
        confirmed_cause=confirmed_cause,
        affected_code_path="src/theforge/shape_check/heuristics.py",
        fix_success_criterion="an empty-cause artifact derives as investigation-ready.",
    )


def _bullet_body(cause_value: str) -> str:
    """The same fields an operator would write, in bullet form."""
    return textwrap.dedent(
        f"""\
        ## Diagnosis

        - **Observed symptom:** a diagnose artifact derives as fix-ready.
        - **Evidence:** rendered at baseline `f2caf7d`.
        - **Confirmed cause:** {cause_value}
        - **Affected code path:** src/theforge/shape_check/heuristics.py
        - **Fix-success criterion:** derives as investigation-ready.
        """
    )


REAL_CAUSE = "the renderer writes the field in heading form while the extractor reads bullet form."
NON_ASSERTIONS = ["unknown", "not yet identified", "pending investigation", "TBD"]


class TestRenderedArtifactReadiness:
    def test_rendered_real_cause_is_implementation_ready(self) -> None:
        body = render_artifact_markdown(_artifact(REAL_CAUSE))
        assert cause_assertion_state(body) == "asserted"
        assert derive_fix_ready("bug", body) == (True, False, [])

    @pytest.mark.parametrize("cause_value", NON_ASSERTIONS)
    def test_rendered_non_assertion_is_investigation_ready(self, cause_value: str) -> None:
        body = render_artifact_markdown(_artifact(cause_value))
        assert cause_assertion_state(body) == "non_asserted"
        fix_ready, investigation_ready, warnings = derive_fix_ready("bug", body)
        assert (fix_ready, investigation_ready) == (True, True)
        assert warnings and "investigation-ready" in warnings[0]

    def test_rendered_empty_cause_is_investigation_ready(self) -> None:
        # The designed honest-refusal path: the diagnose prompt asks the agent
        # for `confirmed_cause: ""` when nothing was confirmed. That outcome
        # must not carry the implementation-ready verdict.
        body = render_artifact_markdown(_artifact(""))
        assert cause_assertion_state(body) == "non_asserted"
        fix_ready, investigation_ready, warnings = derive_fix_ready("bug", body)
        assert (fix_ready, investigation_ready) == (True, True)
        assert warnings and "investigation-ready" in warnings[0]

    def test_rendered_empty_cause_with_support_lines_stays_investigation_ready(self) -> None:
        body = render_artifact_markdown(
            DiagnosisArtifact(
                issue_number=2060,
                observed_symptom=(
                    "A diagnose artifact with no confirmed cause derives as fix-ready."
                ),
                reproduction_or_evidence=(
                    "Rendered at baseline `f2caf7d` and passed to derive_fix_ready."
                ),
                hypotheses=(
                    Hypothesis(
                        statement="the extractor never reads the heading form",
                        status="confirmed",
                        evidence="`_extract_confirmed_cause_value` stripped bullet markers only",
                        evidence_provenance=SupportProvenance(
                            "observed",
                            "Read directly from code.",
                        ),
                    ),
                ),
                confirmed_cause="",
                confirmed_cause_support=(
                    "An earlier diagnosis independently confirmed the same cause."
                ),
                confirmed_cause_support_provenance=SupportProvenance(
                    "prior_assertion",
                    "Earlier diagnosis already stated the same cause.",
                ),
                affected_code_path="src/theforge/shape_check/heuristics.py",
                fix_success_criterion="an empty-cause artifact derives as investigation-ready.",
            )
        )
        assert cause_assertion_state(body) == "non_asserted"
        fix_ready, investigation_ready, warnings = derive_fix_ready("bug", body)
        assert (fix_ready, investigation_ready) == (True, True)
        assert warnings and "investigation-ready" in warnings[0]

    def test_rendered_empty_cause_stays_complete(self) -> None:
        # Investigation-ready is an admissible shape, not a malformed body: the
        # section is still complete, so the bug is not refused outright.
        body = render_artifact_markdown(_artifact(""))
        assert diagnosis_completeness(body) == (True, [])

    def test_rendered_empty_cause_states_the_non_assertion_in_prose(self) -> None:
        # A human reading the landed body must see "no cause was confirmed",
        # not an unexplained slot placeholder.
        body = render_artifact_markdown(_artifact(""))
        assert "_(empty)_" not in body.split("### Confirmed cause")[1].split("###")[0]
        assert "did not confirm a cause" in body

    def test_rendered_empty_cause_gets_the_cause_unknown_reason(self) -> None:
        body = render_artifact_markdown(_artifact(""))
        reason = check_bug_missing_diagnosis("Bug: foo", body, ["bug"])
        assert reason is not None
        assert reason.code == "diagnosis_cause_unknown"

    def test_rendered_empty_cause_grooms_as_cause_unknown(self) -> None:
        # ADR-0001 / groom_flow: a bug whose diagnosis is complete but whose
        # cause is not asserted MUST NOT transition to `ready`.
        body = render_artifact_markdown(_artifact(""))
        assert classify_bug_diagnosis(body, ["bug"]) is BugDiagnosisState.CAUSE_UNKNOWN

    def test_rendered_real_cause_grooms_as_confirmed(self) -> None:
        body = render_artifact_markdown(_artifact(REAL_CAUSE))
        assert classify_bug_diagnosis(body, ["bug"]) is BugDiagnosisState.CONFIRMED_CAUSE

    def test_rendered_real_cause_with_support_provenance_caveat_stays_asserted(self) -> None:
        body = render_artifact_markdown(
            DiagnosisArtifact(
                issue_number=2060,
                observed_symptom="A diagnose artifact with confirmed cause stays asserted.",
                reproduction_or_evidence="Rendered with support provenance text below the cause.",
                hypotheses=(
                    Hypothesis(
                        statement="support provenance adds lines below the cause heading",
                        status="confirmed",
                        evidence="render_artifact_markdown now emits a support block",
                    ),
                ),
                confirmed_cause=(
                    "The earlier diagnosis independently confirmed the same cause in heading form."
                ),
                confirmed_cause_support_provenance=SupportProvenance(
                    "prior_assertion",
                    "The earlier diagnosis already stated the cause.",
                ),
                affected_code_path="src/theforge/diagnose_types.py",
                fix_success_criterion="support provenance does not change cause extraction.",
            )
        )
        assert cause_assertion_state(body) == "asserted"
        assert derive_fix_ready("bug", body) == (True, False, [])
        assert "Support provenance: prior_assertion" in body


class TestHeadingAndBulletFormAgree:
    @pytest.mark.parametrize("cause_value", [REAL_CAUSE, *NON_ASSERTIONS])
    def test_same_verdict_either_form(self, cause_value: str) -> None:
        rendered = render_artifact_markdown(_artifact(cause_value))
        operator = _bullet_body(cause_value)
        assert cause_assertion_state(rendered) == cause_assertion_state(operator)
        assert derive_fix_ready("bug", rendered) == derive_fix_ready("bug", operator)

    def test_label_only_bullet_does_not_absorb_the_next_field(self) -> None:
        # A bullet whose value is empty must not read the following field's
        # bullet as its value.
        body = _bullet_body("")
        assert cause_assertion_state(body) == "non_asserted"


class TestRendererLabelsMatchSpec:
    """The renderer restates the field labels the gate validates — pin them.

    `diagnosis_spec` is the single source of truth for the component labels
    (#1629). The renderer writes its own heading strings, so a spec-side label
    change with no renderer change is exactly the drift that produced #2060.
    """

    def test_every_required_component_label_is_rendered_as_a_heading(self) -> None:
        body = render_artifact_markdown(_artifact(REAL_CAUSE))
        for component in REQUIRED_DIAGNOSIS_COMPONENTS:
            token = component.token
            headings = [
                line.lstrip("#").strip().lower()
                for line in body.splitlines()
                if line.startswith("#")
            ]
            assert any(token in heading for heading in headings), (
                f"renderer emits no heading carrying the required label {component.label!r}"
            )
