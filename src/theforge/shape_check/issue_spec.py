"""The declarative typed issue specification — one contract, stated as data.

ADR-0009 decides that a declarative specification *is* the contract: the
checker validates against it, producers render through it, and reference
documentation is generated from it. This module is that specification.

Nothing here executes a rule. It states, per issue type:

- the canonical type label,
- the section headings, their canonical spelling and their order,
- which sections are required, which are advisory, and which are forbidden,
- field-level constraints inside a section (the bug ``## Diagnosis`` bullets),
- the lifecycle states the type can occupy and which of them admit
  implementation.

Two distinct notions of "this heading is that section" live here on purpose:

``SectionSpec.recognition_pattern``
    The regex the *gate* probes a body with. Deliberately broad — a body
    written ``## Observed behavior`` is a bug body, and ADR-0003's
    single-recognition principle says one gate must not refuse what another
    admits.

``SectionSpec.aliases``
    The exact heading spellings a *renderer* may canonicalize. Narrow on
    purpose: recognizing a spelling on input must never license rewriting a
    heading whose extra words carry the author's meaning. A heading matched by
    the pattern but not by an alias is preserved verbatim (ADR-0009 clause 8).

The two are not independent. Every alias is a spelling the renderer will
rewrite, so the gate must be able to see it: :attr:`SectionSpec.heading_pattern`
is the recognition pattern widened, mechanically, to cover each declared alias,
and it is what every heading probe uses. Declaring an alias is therefore enough
— there is no second edit to keep the gate in step with the renderer.

Stdlib only, no imports from the rest of the package — pure-data types in a
low-dependency module (project convention 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

#: Repo-relative path of the generated per-type reference rendered from this spec.
ISSUE_SHAPE_REFERENCE_PATH = "docs/reference/issue-shape.md"

#: Repo-relative path of the generated bug-specific slice of that reference.
BUG_SHAPE_REFERENCE_PATH = "docs/reference/bug-shape.md"


def normalize_heading_text(text: str) -> str:
    """Strip a heading down to its bare wording for canonical comparison.

    Trailing label punctuation (``:``, ``.``, ``-``, em dash), surrounding
    whitespace and case are removed, so ``"Diagnosis"``, ``"Diagnosis:"`` and
    ``"Diagnosis —"`` normalize the same way. Interior whitespace collapses so
    a wrapped or double-spaced heading is not a different heading.
    """
    collapsed = re.sub(r"\s+", " ", text.strip())
    return re.sub(r"[\s:.\-—]+$", "", collapsed).strip().lower()


class Presence(str, Enum):
    """How a type's specification treats one section.

    ``REQUIRED`` — absence is a structural refusal.
    ``ADVISORY`` — absence is reported but decides nothing (ADR-0009 clause 4).
    ``OPTIONAL`` — modeled so it renders canonically; never demanded.
    ``FORBIDDEN`` — presence contradicts the declared type.
    """

    REQUIRED = "required"
    ADVISORY = "advisory"
    OPTIONAL = "optional"
    FORBIDDEN = "forbidden"


class ContradictionTrigger(str, Enum):
    """When one forbidden section actually contradicts the declared type.

    A property of the *section*, not of the type: some headings say what they
    are on their own, and some are ordinary English that only mean a bug report
    in company.

    ``ANY_SECTION`` — the heading is enough by itself. ``## Diagnosis`` in a
    feature body is a defect investigation however the rest of the body reads,
    so the gate refuses it on sight. This is the default.
    ``BUG_BODY_SHAPE`` — the section counts only when the body carries the whole
    *bug report shape*: a reproduction heading, or a symptom heading paired with
    an expectation heading. A lone ``## Expected`` under a feature issue is
    ordinary prose describing intended behavior, not a bug report.
    """

    ANY_SECTION = "any_section"
    BUG_BODY_SHAPE = "bug_body_shape"


@dataclass(frozen=True)
class FieldSpec:
    """One labelled field inside a section (e.g. a ``## Diagnosis`` bullet).

    Attributes:
        key: stable machine identifier.
        label: the bolded label an operator writes as the bullet lead-in
            (e.g. ``"Confirmed cause"``). The validator matches it
            case-insensitively; finding messages, remediation prompts and the
            generated skeleton all quote it verbatim.
        satisfies: human description of what content satisfies the field.
        example: a concrete example value, quoted verbatim into the finding
            message, the remediation prompt and the skeleton.
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
        producer/validator drift the spec exists to eliminate (#1629).
        """
        return self.label.lower()

    def bullet(self) -> str:
        """Render the field as a skeleton bullet: ``- **Label:** example``."""
        return f"- **{self.label}:** {self.example}"


@dataclass(frozen=True)
class SectionSpec:
    """One modeled section: how it is recognized, and how it is rendered."""

    key: str
    canonical_heading: str
    level: int
    aliases: tuple[str, ...]
    recognition_pattern: str
    summary: str
    fields: tuple[FieldSpec, ...] = ()

    @property
    def canonical_heading_line(self) -> str:
        """The exact Markdown line a renderer emits for this section."""
        return f"{'#' * self.level} {self.canonical_heading}"

    @property
    def normalized_aliases(self) -> tuple[str, ...]:
        """Every spelling a renderer may rewrite to the canonical heading."""
        return tuple(
            dict.fromkeys(
                [normalize_heading_text(self.canonical_heading)]
                + [normalize_heading_text(a) for a in self.aliases]
            )
        )

    @property
    def heading_pattern(self) -> str:
        """The regex every heading probe uses to find this section in a body.

        :attr:`recognition_pattern` widened, mechanically, to cover each
        declared alias. Recognition must be at least as wide as
        canonicalization: an alias is a spelling the *renderer* will rewrite to
        the canonical heading, so a gate that could not see that spelling would
        refuse — or admit — a section depending on which of the two read the
        body first. ``## Reproduction`` was exactly that hole: it parsed as the
        reproduction section and rendered as ``## Steps to reproduce``, while
        the pattern the gate probed with only matched the latter.

        Alias alternatives are anchored, with the same trailing label
        punctuation :func:`normalize_heading_text` tolerates, because an alias
        is an exact spelling. The hand-written ``recognition_pattern`` stays
        deliberately broad on top of that — ``## Observed behavior`` is a
        symptom heading — and is left exactly as declared.
        """
        alternatives = [self.recognition_pattern]
        broad = re.compile(self.recognition_pattern, re.IGNORECASE)
        for alias in self.normalized_aliases:
            if broad.search(alias):
                continue
            alternatives.append(rf"^{re.escape(alias)}[\s:.\-—]*$")
        return "|".join(alternatives)

    def matches_heading(self, heading_text: str) -> bool:
        """True when ``heading_text`` is an exact (normalized) spelling of this section."""
        return normalize_heading_text(heading_text) in self.normalized_aliases

    def field_tokens(self) -> tuple[str, ...]:
        return tuple(f.token for f in self.fields)


@dataclass(frozen=True)
class SectionRule:
    """A type's stance on one section.

    ``trigger`` applies only to :attr:`Presence.FORBIDDEN` and says when the
    section's presence is a contradiction rather than ordinary prose. It sits
    here, on the rule, because it is a fact about *that section under that
    type* — a feature issue's ``## Diagnosis`` contradicts on sight while its
    ``## Expected`` only does so as part of a whole bug report.
    """

    section_key: str
    presence: Presence
    trigger: ContradictionTrigger = ContradictionTrigger.ANY_SECTION


@dataclass(frozen=True)
class LifecycleState:
    """One state an issue of a given type can occupy.

    ``admits_implementation`` is the single admission fact this state carries;
    ``refusal_code`` is the reason code the gate emits when the document is in
    this state and the state does not admit implementation. States that admit
    implementation carry no refusal code.
    """

    key: str
    summary: str
    admits_implementation: bool
    refusal_code: str | None = None


@dataclass(frozen=True)
class TypeShapeContradiction:
    """How a type's forbidden sections are reported when they appear.

    *Which* sections are forbidden, and when each one counts, is not stated
    here — that is :attr:`IssueTypeSpec.section_rules` with
    :attr:`Presence.FORBIDDEN` and its :attr:`SectionRule.trigger`, and that is
    the only place it is stated. This record carries the wording of the refusal
    and nothing else.
    """

    slug: str
    rule_text: str
    remediation_hint: str


@dataclass(frozen=True)
class IssueTypeSpec:
    """The complete declarative shape of one issue type."""

    key: str
    label: str
    summary: str
    dispatchable: bool
    #: True when the label satisfies the gate's "declare exactly one type"
    #: requirement. ``operator-action`` is a deliberate non-dispatch marker
    #: that sits *beside* the type vocabulary rather than inside it.
    declares_type: bool
    section_rules: tuple[SectionRule, ...]
    lifecycle_states: tuple[LifecycleState, ...]
    contradiction: TypeShapeContradiction | None = None

    def sections(self) -> tuple[SectionSpec, ...]:
        """Modeled sections, in the canonical order this type renders them."""
        return tuple(SECTIONS[rule.section_key] for rule in self.section_rules)

    def rule_for(self, section_key: str) -> SectionRule | None:
        for rule in self.section_rules:
            if rule.section_key == section_key:
                return rule
        return None

    def presence_of(self, section_key: str) -> Presence:
        rule = self.rule_for(section_key)
        return rule.presence if rule is not None else Presence.OPTIONAL

    def section_keys_with(self, presence: Presence) -> tuple[str, ...]:
        return tuple(r.section_key for r in self.section_rules if r.presence is presence)

    def forbidden_keys_with_trigger(self, trigger: ContradictionTrigger) -> tuple[str, ...]:
        """Forbidden section keys whose presence counts under ``trigger``.

        In declaration order, so a refusal names the sections in the order the
        type renders them.
        """
        return tuple(
            rule.section_key
            for rule in self.section_rules
            if rule.presence is Presence.FORBIDDEN and rule.trigger is trigger
        )

    def requires(self, section_key: str) -> bool:
        return self.presence_of(section_key) is Presence.REQUIRED

    def forbids(self, section_key: str) -> bool:
        return self.presence_of(section_key) is Presence.FORBIDDEN

    def lifecycle_state(self, key: str) -> LifecycleState | None:
        for state in self.lifecycle_states:
            if state.key == key:
                return state
        return None


# --- Sections ---------------------------------------------------------------
#
# The bug-report shape is a symptom heading paired with an expectation heading.
# The corpus overwhelmingly writes "Observed" / "Expected" and `forge shape`
# emits them, so those are canonical here; "What happened" / "What was expected"
# stay recognized, and stay recognized in exactly one place, because intake asks
# "is this a bug body?" at more than one gate and a body admitted by one gate
# must not be refused by another (#2139, ADR-0003).

OBSERVED_SECTION = SectionSpec(
    key="observed",
    canonical_heading="Observed",
    level=2,
    aliases=("Observed", "What happened"),
    recognition_pattern=r"what happened|observed|actual behaviou?r",
    summary="what the system actually did, stated as a fact a reader can check",
)

EXPECTED_SECTION = SectionSpec(
    key="expected",
    canonical_heading="Expected",
    level=2,
    aliases=("Expected", "What was expected"),
    recognition_pattern=r"what was expected|expected",
    summary="the category-level rule that should have held instead",
)

REPRODUCTION_SECTION = SectionSpec(
    key="reproduction",
    canonical_heading="Steps to reproduce",
    level=2,
    aliases=("Steps to reproduce", "Reproduction"),
    recognition_pattern=r"steps to reproduce",
    summary="the shortest sequence that exhibits the observed behavior",
)

DIAGNOSIS_SECTION = SectionSpec(
    key="diagnosis",
    canonical_heading="Diagnosis",
    level=2,
    aliases=("Diagnosis", "Root cause"),
    # "root cause" is recognized alongside "diagnosis" so the gate agrees with
    # intake's diagnosis-state detection about what counts as a diagnosis-shaped
    # heading — a body whose analysis lives under "## Root cause" must not read
    # as diagnosed to one and missing to the other (#2263).
    recognition_pattern=r"diagnosis|root cause",
    summary=(
        "the investigated account of the defect: confirmed symptom, evidence, "
        "cause, code path, and behavioral fix-success criterion; unverified "
        "repair proposals stay advisory rather than reading as diagnosis"
    ),
    fields=(
        FieldSpec(
            key="observed_symptom",
            label="Observed symptom",
            satisfies='bolded bullet lead-in or heading inside "## Diagnosis"',
            example=(
                "sprint resume false-skips zero-delta APPROVE stories, reporting them "
                "merged when no commit landed."
            ),
        ),
        FieldSpec(
            key="evidence",
            label="Evidence",
            satisfies='bolded bullet lead-in or heading inside "## Diagnosis"',
            example="run id `1ff6b0bb7992`, story #1102 — resume log shows the false skip.",
        ),
        FieldSpec(
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
        FieldSpec(
            key="affected_code_path",
            label="Affected code path",
            satisfies="bolded bullet lead-in or heading naming the module/function",
            example="`sprint.runner._is_already_merged`.",
        ),
        FieldSpec(
            key="fix_success_criterion",
            label="Fix-success criterion",
            satisfies="bolded bullet lead-in or heading stating the observable pass condition",
            example="resume identifies a zero-delta APPROVE story as already merged.",
        ),
    ),
)

ACCEPTANCE_CRITERIA_SECTION = SectionSpec(
    key="acceptance_criteria",
    canonical_heading="Acceptance criteria",
    level=2,
    aliases=("Acceptance criteria", "Done criteria", "Checklist"),
    recognition_pattern=r"acceptance criteria|done criteria|checklist",
    summary="the observable outcomes a reviewer checks the change against",
)

EXAMPLE_SECTION = SectionSpec(
    key="example",
    canonical_heading="Example",
    level=2,
    aliases=("Example", "Examples"),
    recognition_pattern=(
        r"example(?:s)?|what it should look like|target(?: sketch| output| state)?"
    ),
    summary="a concrete sketch of the target: sample output, a table, or a fenced block",
)

SECTIONS: dict[str, SectionSpec] = {
    section.key: section
    for section in (
        OBSERVED_SECTION,
        EXPECTED_SECTION,
        REPRODUCTION_SECTION,
        DIAGNOSIS_SECTION,
        ACCEPTANCE_CRITERIA_SECTION,
        EXAMPLE_SECTION,
    )
}

# --- Lifecycle --------------------------------------------------------------

_IMPLEMENTATION_READY = LifecycleState(
    key="implementation_ready",
    summary="the document satisfies its type's grammar and may enter a sprint",
    admits_implementation=True,
)

_BUG_LIFECYCLE: tuple[LifecycleState, ...] = (
    LifecycleState(
        key="undiagnosed",
        summary="symptom-only: no Diagnosis section, so no cause a reviewer could check",
        admits_implementation=False,
        refusal_code="needs_diagnosis",
    ),
    LifecycleState(
        key="investigation_ready",
        summary=(
            "Diagnosis section complete but the confirmed cause is a non-assertion; "
            "the next job is cause discovery, not hypothesized-cause implementation"
        ),
        admits_implementation=False,
        refusal_code="diagnosis_cause_unknown",
    ),
    _IMPLEMENTATION_READY,
)

_FEATURE_LIFECYCLE: tuple[LifecycleState, ...] = (
    LifecycleState(
        key="ungroomed",
        summary="no acceptance criteria, so no observable statement of done",
        admits_implementation=False,
        refusal_code="missing_acceptance_criteria",
    ),
    _IMPLEMENTATION_READY,
)

_TRACKING_LIFECYCLE: tuple[LifecycleState, ...] = (
    LifecycleState(
        key="tracking_only",
        summary="an entry that groups runnable children; never dispatched itself",
        admits_implementation=False,
        refusal_code="epic_or_tracking",
    ),
)

_OPERATOR_LIFECYCLE: tuple[LifecycleState, ...] = (
    LifecycleState(
        key="awaiting_operator",
        summary="the deliverable is human action no dev agent can perform",
        admits_implementation=False,
        refusal_code="operator_action",
    ),
)


# --- Types ------------------------------------------------------------------

BUG_SPEC = IssueTypeSpec(
    key="bug",
    label="bug",
    summary="a defect report: what happened, what should have happened, and why",
    dispatchable=True,
    declares_type=True,
    section_rules=(
        SectionRule("observed", Presence.REQUIRED),
        SectionRule("expected", Presence.REQUIRED),
        SectionRule("reproduction", Presence.OPTIONAL),
        SectionRule("diagnosis", Presence.REQUIRED),
        SectionRule("acceptance_criteria", Presence.FORBIDDEN),
    ),
    lifecycle_states=_BUG_LIFECYCLE,
    contradiction=TypeShapeContradiction(
        slug="acceptance-criteria",
        rule_text="bugs use observed/expected plus diagnosis",
        remediation_hint="remove the feature-style checklist or relabel the issue",
    ),
)

ENHANCEMENT_SPEC = IssueTypeSpec(
    key="enhancement",
    label="enhancement",
    summary="new or changed behavior, stated as outcomes a reviewer can check",
    dispatchable=True,
    declares_type=True,
    section_rules=(
        SectionRule("acceptance_criteria", Presence.REQUIRED),
        SectionRule("example", Presence.ADVISORY),
        SectionRule("observed", Presence.FORBIDDEN, ContradictionTrigger.BUG_BODY_SHAPE),
        SectionRule("expected", Presence.FORBIDDEN, ContradictionTrigger.BUG_BODY_SHAPE),
        SectionRule("reproduction", Presence.FORBIDDEN),
        SectionRule("diagnosis", Presence.FORBIDDEN),
    ),
    lifecycle_states=_FEATURE_LIFECYCLE,
    contradiction=TypeShapeContradiction(
        slug="bug-report-shape",
        rule_text=(
            "enhancement issues use why/acceptance criteria/example, not bug-report sections"
        ),
        remediation_hint="relabel the issue as a bug or rewrite the body to the feature shape",
    ),
)

TASK_SPEC = IssueTypeSpec(
    key="task",
    label="task",
    summary="operator-scoped or documentation-shaped work with a checkable outcome",
    dispatchable=True,
    declares_type=True,
    section_rules=(
        SectionRule("acceptance_criteria", Presence.REQUIRED),
        SectionRule("example", Presence.ADVISORY),
        SectionRule("observed", Presence.FORBIDDEN, ContradictionTrigger.BUG_BODY_SHAPE),
        SectionRule("expected", Presence.FORBIDDEN, ContradictionTrigger.BUG_BODY_SHAPE),
        SectionRule("reproduction", Presence.FORBIDDEN),
        SectionRule("diagnosis", Presence.FORBIDDEN),
    ),
    lifecycle_states=_FEATURE_LIFECYCLE,
    contradiction=TypeShapeContradiction(
        slug="bug-report-shape",
        rule_text="task issues use why/acceptance criteria/example, not bug-report sections",
        remediation_hint="relabel the issue as a bug or rewrite the body to the task shape",
    ),
)

SPIKE_SPEC = IssueTypeSpec(
    key="spike",
    label="spike",
    summary=(
        "a chartered question: design work and a validating POC, which closes only on a "
        "recorded outcome"
    ),
    dispatchable=True,
    declares_type=True,
    section_rules=(
        SectionRule("acceptance_criteria", Presence.REQUIRED),
        SectionRule("example", Presence.ADVISORY),
        SectionRule("observed", Presence.FORBIDDEN, ContradictionTrigger.BUG_BODY_SHAPE),
        SectionRule("expected", Presence.FORBIDDEN, ContradictionTrigger.BUG_BODY_SHAPE),
        SectionRule("reproduction", Presence.FORBIDDEN),
        SectionRule("diagnosis", Presence.FORBIDDEN),
    ),
    lifecycle_states=_FEATURE_LIFECYCLE,
    contradiction=TypeShapeContradiction(
        slug="bug-report-shape",
        rule_text="spike issues use why/acceptance criteria/example, not bug-report sections",
        remediation_hint="relabel the issue as a bug or rewrite the body to the spike shape",
    ),
)

EPIC_SPEC = IssueTypeSpec(
    key="epic",
    label="epic",
    summary="a tracking entry grouping runnable children; never dispatched itself",
    dispatchable=False,
    declares_type=True,
    section_rules=(
        SectionRule("acceptance_criteria", Presence.REQUIRED),
        SectionRule("example", Presence.ADVISORY),
        SectionRule("observed", Presence.FORBIDDEN, ContradictionTrigger.BUG_BODY_SHAPE),
        SectionRule("expected", Presence.FORBIDDEN, ContradictionTrigger.BUG_BODY_SHAPE),
        SectionRule("reproduction", Presence.FORBIDDEN),
        SectionRule("diagnosis", Presence.FORBIDDEN),
    ),
    lifecycle_states=_TRACKING_LIFECYCLE,
    contradiction=TypeShapeContradiction(
        slug="bug-report-shape",
        rule_text="epic issues are tracking entries, not bug-report sections",
        remediation_hint="relabel the issue as a bug or file runnable child work instead",
    ),
)

OPERATOR_ACTION_SPEC = IssueTypeSpec(
    key="operator_action",
    label="operator-action",
    summary="a deliverable only a human operator can produce; deliberately non-dispatched",
    dispatchable=False,
    declares_type=False,
    section_rules=(SectionRule("acceptance_criteria", Presence.REQUIRED),),
    lifecycle_states=_OPERATOR_LIFECYCLE,
)

#: Every typed issue specification, in the order reference docs render them.
ISSUE_TYPES: tuple[IssueTypeSpec, ...] = (
    BUG_SPEC,
    ENHANCEMENT_SPEC,
    TASK_SPEC,
    SPIKE_SPEC,
    EPIC_SPEC,
    OPERATOR_ACTION_SPEC,
)

#: Type labels the checker recognizes as declaring a dispatchable issue type.
#: Exactly one of these must be present for a body to enter a sprint.
RECOGNIZED_TYPE_LABELS: frozenset[str] = frozenset(
    spec.label for spec in ISSUE_TYPES if spec.declares_type
)

#: ``operator-action`` is mutually exclusive with the runnable type labels.
OPERATOR_ACTION_LABEL = OPERATOR_ACTION_SPEC.label
OPERATOR_ACTION_CONFLICT_LABELS: frozenset[str] = RECOGNIZED_TYPE_LABELS

#: The default type used to resolve sections when no type label is declared.
#: Recognition is type-agnostic — a section key means the same thing whichever
#: type carries it — so an untyped body still parses into modeled sections.
_ALL_SECTION_KEYS: tuple[str, ...] = tuple(SECTIONS)


def spec_for_label(label: str) -> IssueTypeSpec | None:
    """Return the type specification declared by ``label``, or ``None``."""
    normalized = str(label).strip().lower()
    for spec in ISSUE_TYPES:
        if spec.label == normalized or spec.key == normalized:
            return spec
    return None


def spec_for_labels(labels) -> IssueTypeSpec | None:
    """Return the single dispatchable type spec declared by ``labels``.

    Returns ``None`` when no recognized type label is present or when more than
    one is — an ambiguous declaration is not a declaration.
    """
    matches = {
        spec.label
        for spec in (spec_for_label(label) for label in labels)
        if spec is not None and spec.label in RECOGNIZED_TYPE_LABELS
    }
    if len(matches) != 1:
        return None
    return spec_for_label(next(iter(matches)))


def section_for_heading(heading_text: str) -> SectionSpec | None:
    """Return the modeled section a heading spells exactly, or ``None``.

    Exact (normalized) alias match only — see the module docstring for why
    recognition and canonicalization are deliberately different widths.
    """
    for key in _ALL_SECTION_KEYS:
        section = SECTIONS[key]
        if section.matches_heading(heading_text):
            return section
    return None


def lifecycle_refusals() -> tuple[tuple[str, str], ...]:
    """Return ``(refusal_code, lifecycle_state_key)`` for every refusing state.

    The verdict layer derives its explicit lifecycle refusals from this rather
    than carrying a second copy (ADR-0009 clause 4: where a condition must
    block, the specification says so).
    """
    seen: dict[str, str] = {}
    for spec in ISSUE_TYPES:
        for state in spec.lifecycle_states:
            if state.admits_implementation or state.refusal_code is None:
                continue
            seen.setdefault(state.refusal_code, state.key)
    return tuple(seen.items())
