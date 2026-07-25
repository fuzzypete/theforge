"""Declarative spec for the bug Diagnosis section the shape gate requires.

Single source of truth for the components a well-formed ``## Diagnosis`` section
must contain. Four consumers derive from this list rather than restating it:

- the shape-gate validator
  (:func:`theforge.shape_check.heuristics.diagnosis_completeness`),
- the shape-gate finding messages
  (:func:`theforge.shape_check.heuristics.check_bug_missing_diagnosis`),
- the intake remediation prompt
  (:func:`theforge.intake.agent_rewrite.build_agent_rewrite_prompt`), and
- the fileable skeleton (:func:`render_bug_shape_reference`, published as
  ``docs/reference/bug-shape.md``).

Deriving producers and validator from one spec is the fix for the
producer/validator drift documented in #1629 — five fully-diagnosed bugs were
refused because a component's *label* did not match the validator's private
token list, while the requirement was never published anywhere a producer could
read. It is the same shape as #1546's review-parse oscillation trap, applied to
the intake gate: a rewrite agent now aims at exactly the labels the validator
checks, and a filing that starts from the skeleton passes by construction.

Stdlib only — pure-data types in a low-dependency module (project convention 4).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: Repo-relative path of the published shape reference derived from this spec.
BUG_SHAPE_REFERENCE_PATH = "docs/reference/bug-shape.md"

#: The heading a bug's diagnosis lives under.
DIAGNOSIS_HEADING = "## Diagnosis"


@dataclass(frozen=True)
class DiagnosisComponent:
    """One required component of a bug's Diagnosis section.

    Attributes:
        key: stable machine identifier.
        label: the bolded label an operator writes as the bullet lead-in
            (e.g. ``"Confirmed cause"``). The validator matches it
            case-insensitively; the finding message, remediation prompt, and
            skeleton all quote it verbatim. The lowercased label is also the
            substring the validator scans for — see :attr:`token`.
        satisfies: human description of what content satisfies the component.
        example: a concrete example value, quoted verbatim into the finding
            message, the remediation prompt, and the skeleton.
    """

    key: str
    label: str
    satisfies: str
    example: str

    @property
    def token(self) -> str:
        """The lowercased substring the validator scans the section for.

        Kept identical to ``label.lower()`` so a skeleton bullet built from
        :meth:`bullet` always contains the token the validator matches — the
        drift the spec exists to eliminate.
        """
        return self.label.lower()

    def bullet(self) -> str:
        """Render the component as a skeleton bullet: ``- **Label:** example``."""
        return f"- **{self.label}:** {self.example}"


REQUIRED_DIAGNOSIS_COMPONENTS: tuple[DiagnosisComponent, ...] = (
    DiagnosisComponent(
        key="observed_symptom",
        label="Observed symptom",
        satisfies='bolded bullet lead-in or heading inside "## Diagnosis"',
        example=(
            "sprint resume false-skips zero-delta APPROVE stories, reporting them "
            "merged when no commit landed."
        ),
    ),
    DiagnosisComponent(
        key="evidence",
        label="Evidence",
        satisfies='bolded bullet lead-in or heading inside "## Diagnosis"',
        example="run id `1ff6b0bb7992`, story #1102 — resume log shows the false skip.",
    ),
    DiagnosisComponent(
        key="confirmed_cause",
        label="Confirmed cause",
        satisfies=(
            "bolded bullet lead-in or heading; its value may be a specific claim "
            'or an honest non-assertion ("unknown", "not yet identified")'
        ),
        example=(
            "`_is_already_merged` requires at least one commit ahead, so a "
            "zero-delta APPROVE is misclassified as unmerged."
        ),
    ),
    DiagnosisComponent(
        key="affected_code_path",
        label="Affected code path",
        satisfies="bolded bullet lead-in or heading naming the module/function",
        example="`sprint.runner._is_already_merged`.",
    ),
    DiagnosisComponent(
        key="fix_success_criterion",
        label="Fix-success criterion",
        satisfies="bolded bullet lead-in or heading stating the observable pass condition",
        example="resume identifies a zero-delta APPROVE story as already merged.",
    ),
)


def required_diagnosis_tokens() -> tuple[str, ...]:
    """The substring tokens the validator scans a Diagnosis section for.

    Derived from :data:`REQUIRED_DIAGNOSIS_COMPONENTS` — the validator must not
    carry its own independent list.
    """
    return tuple(component.token for component in REQUIRED_DIAGNOSIS_COMPONENTS)


def component_for_token(token: str) -> DiagnosisComponent | None:
    """Return the component whose token matches, or ``None``."""
    for component in REQUIRED_DIAGNOSIS_COMPONENTS:
        if component.token == token:
            return component
    return None


def describe_missing(tokens: Iterable[str]) -> str:
    """Render the operator-facing description of missing components.

    Each missing component is named by its exact literal label and paired with
    its example, so a finding message quotes the target a producer must hit
    rather than a bare component name (#1629 AC3).
    """
    parts: list[str] = []
    for token in tokens:
        component = component_for_token(token)
        if component is None:
            # Forward-compat: an unrecognized token still surfaces literally.
            parts.append(f'"{token}"')
            continue
        parts.append(
            f'a "**{component.label}:**" bullet under {DIAGNOSIS_HEADING} '
            f'(example: "{component.bullet()}")'
        )
    return "; ".join(parts)


def render_diagnosis_requirements_block() -> str:
    """Render the spec as prompt instructions embedding labels + examples.

    Consumed verbatim by the intake remediation prompt so a rewrite agent aims
    at exactly the labels the validator checks (#1629 AC2).
    """
    lines = [
        f"The bug body must contain a {DIAGNOSIS_HEADING} section with all of the",
        "components below, each written as a bolded bullet lead-in. Use these exact",
        "labels — the shape gate matches them literally:",
        "",
    ]
    for component in REQUIRED_DIAGNOSIS_COMPONENTS:
        lines.append(f"- **{component.label}:** {component.satisfies}")
        lines.append(f"  Example — {component.bullet()}")
    lines.extend(
        [
            "",
            f"Full shape reference: {BUG_SHAPE_REFERENCE_PATH}",
        ]
    )
    return "\n".join(lines)


def render_bug_skeleton_body() -> str:
    """Render a fileable ``## Diagnosis`` skeleton from the spec.

    A bug body that starts from this skeleton passes the shape gate by
    construction: every component's bullet contains its own token, and the
    confirmed-cause example is a specific claim (implementation-ready).
    """
    lines = [DIAGNOSIS_HEADING, ""]
    for component in REQUIRED_DIAGNOSIS_COMPONENTS:
        lines.append(component.bullet())
    lines.append("")
    return "\n".join(lines)


def render_bug_shape_reference() -> str:
    """Render the full ``docs/reference/bug-shape.md`` document from the spec.

    The on-disk doc is checked against this output by a test, so the published
    reference can never drift from what the validator enforces (#1629 AC4).
    """
    lines = [
        "# Bug shape reference",
        "",
        "> Generated from `theforge.shape_check.diagnosis_spec`. Do not edit by hand —",
        "> run the shape-reference generator (or the test that regenerates it) instead.",
        "",
        "A bug-typed issue is *fix-ready* when its body carries a complete",
        f"`{DIAGNOSIS_HEADING}` section. The shape gate refuses symptom-only bug bodies;",
        "an honest non-assertion confirmed cause (`unknown`, `not yet identified`,",
        "`pending investigation`, `TBD`) is admissible and marks the bug",
        "investigation-ready rather than implementation-ready.",
        "",
        "## Required components",
        "",
        "Each component is written as a bolded bullet lead-in under the",
        f"`{DIAGNOSIS_HEADING}` heading. The gate matches the **label** literally"
        " (case-insensitive).",
        "",
    ]
    for component in REQUIRED_DIAGNOSIS_COMPONENTS:
        lines.append(f"### {component.label}")
        lines.append("")
        lines.append(f"- Satisfies: {component.satisfies}")
        lines.append(f"- Example: {component.bullet()}")
        lines.append("")
    lines.extend(
        [
            "## Fileable skeleton",
            "",
            "Copy this into a bug issue body and replace each example value. A body",
            "that starts from this skeleton passes the shape gate by construction.",
            "",
            "```markdown",
            render_bug_skeleton_body().rstrip("\n"),
            "```",
            "",
        ]
    )
    return "\n".join(lines)
