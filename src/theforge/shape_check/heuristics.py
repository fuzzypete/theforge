"""Deterministic structural checks — one function per reason code.

Each check returns ``Optional[Reason]`` given already-parsed inputs. The
structural rules themselves — recognized type labels, which sections a type
requires, which it forbids, the heading spellings each is recognized by, and
the lifecycle states it can occupy — are *not* stated here. They are read from
the declarative specification (:mod:`theforge.shape_check.issue_spec`), so a
rule changed there changes what this module enforces with no second edit
(ADR-0009 clause 1).

Stdlib plus that pure-data specification.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from theforge.shape_check.diagnosis_spec import (
    BUG_SHAPE_REFERENCE_PATH,
    REQUIRED_DIAGNOSIS_COMPONENTS,
    component_for_token,
    describe_missing,
    required_diagnosis_tokens,
)
from theforge.shape_check.issue_spec import (
    ACCEPTANCE_CRITERIA_SECTION,
    BUG_SPEC,
    DIAGNOSIS_SECTION,
    ENHANCEMENT_SPEC,
    EXAMPLE_SECTION,
    EXPECTED_SECTION,
    ISSUE_SHAPE_REFERENCE_PATH,
    OBSERVED_SECTION,
    RECOGNIZED_TYPE_LABELS,
    SECTIONS,
    ContradictionTrigger,
    IssueTypeSpec,
    Presence,
    spec_for_labels,
)
from theforge.shape_check.parsing import (
    ACCEPTANCE_CRITERIA_HEADING_PATTERN,
    FenceTracker,
    blockquote_blocks,
    extract_ac_section,
    extract_authoritative_section,
    extract_bullets,
    extract_section,
    extract_top_level_bullet_blocks,
    fenced_code_blocks,
    find_authoritative_heading,
    has_bug_body_headings,
    has_heading,
    iter_headings,
)
from theforge.shape_check.placeholders import is_placeholder_only, strip_placeholder_content
from theforge.shape_check.types import Reason, Severity

DEFAULT_CLUSTER_THRESHOLD = 4

SEED_VOCABULARY: frozenset[str] = frozenset(
    {
        "release",
        "triage",
        "label",
        "report",
        "schema",
        "config",
        "workflow",
        "phase",
        "prompt",
        "audit",
        "milestone",
        "hook",
        "finding",
    }
)

_NEEDS_TRIAGE_LABEL = "needs-triage"

_TRACKING_PHRASES = (
    "tracking issue",
    "tracking only",
    "planning issue",
    "umbrella",
    "meta issue",
    "meta-issue",
    "parent issue",
)

_TRACKING_SENTENCE_START_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tracking issue", re.compile(r"^tracking issue\b")),
    ("tracking only", re.compile(r"^tracking only\b")),
    ("planning issue", re.compile(r"^planning issue\b")),
    ("umbrella", re.compile(r"^umbrella(?:\s+issue)?(?:\s+for\b|[:.-]|\s*$)")),
    ("meta issue", re.compile(r"^meta issue\b")),
    ("meta-issue", re.compile(r"^meta-issue\b")),
    ("parent issue", re.compile(r"^parent issue\b")),
)

_TRACKING_SUBJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "tracking issue",
        re.compile(
            r"^(?:this|it)(?:\s+issue)?\s+"
            r"(?:is|serves as|acts as|remains)\s+(?:an?\s+)?tracking issue\b"
        ),
    ),
    (
        "tracking only",
        re.compile(
            r"^(?:this|it)(?:\s+issue)?\s+"
            r"(?:is|serves as|acts as|remains)\s+tracking only\b"
        ),
    ),
    (
        "planning issue",
        re.compile(
            r"^(?:this|it)(?:\s+issue)?\s+"
            r"(?:is|serves as|acts as|remains)\s+(?:an?\s+)?planning issue\b"
        ),
    ),
    (
        "umbrella",
        re.compile(
            r"^(?:this|it)(?:\s+issue)?\s+"
            r"(?:is|serves as|acts as|remains)\s+(?:an?\s+)?umbrella(?:\s+issue)?\b"
        ),
    ),
    (
        "meta issue",
        re.compile(
            r"^(?:this|it)(?:\s+issue)?\s+"
            r"(?:is|serves as|acts as|remains)\s+(?:an?\s+)?meta issue\b"
        ),
    ),
    (
        "meta-issue",
        re.compile(
            r"^(?:this|it)(?:\s+issue)?\s+"
            r"(?:is|serves as|acts as|remains)\s+(?:an?\s+)?meta-issue\b"
        ),
    ),
    (
        "parent issue",
        re.compile(
            r"^(?:this|it)(?:\s+issue)?\s+"
            r"(?:is|serves as|acts as|remains)\s+(?:an?\s+)?parent issue\b"
        ),
    ),
)

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<content>.+?)\s*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_BUG_LABELS: frozenset[str] = frozenset({"bug"})

# The recognized type vocabulary is the specification's, not this module's.
_RECOGNIZED_TYPE_LABELS: frozenset[str] = RECOGNIZED_TYPE_LABELS

_EXAMPLE_SECTION_PATTERNS = (EXAMPLE_SECTION.heading_pattern,)
_EXAMPLE_MIN_CONTENT_CHARS = 30

# "root cause" is recognized alongside "diagnosis" so the gate agrees with
# theforge.intake.shape_classify's diagnosis-state detection about what
# counts as a diagnosis-shaped heading — a body whose analysis lives under
# "## Root cause" must not be treated as diagnosed by the classifier while
# the gate simultaneously reports the Diagnosis section missing (#2263).
DIAGNOSIS_HEADING_PATTERN = DIAGNOSIS_SECTION.heading_pattern

# Headings whose text, normalized, exactly equals one of these are the
# canonical diagnosis section — as opposed to a heading that merely mentions
# the word in a longer, unrelated title. Used to pick the authoritative
# Diagnosis section when a body carries more than one heading matching
# DIAGNOSIS_HEADING_PATTERN (#2263). These are the specification's declared
# alias spellings, which is also exactly what the renderer canonicalizes.
DIAGNOSIS_CANONICAL_HEADING_TEXTS = DIAGNOSIS_SECTION.normalized_aliases
OBSERVED_HEADING_PATTERN = OBSERVED_SECTION.heading_pattern
OBSERVED_CANONICAL_HEADING_TEXTS = OBSERVED_SECTION.normalized_aliases
EXPECTED_HEADING_PATTERN = EXPECTED_SECTION.heading_pattern
EXPECTED_CANONICAL_HEADING_TEXTS = EXPECTED_SECTION.normalized_aliases


@dataclass(frozen=True)
class TypeShapeRule:
    """A type's forbidden-section rule, as the finding message states it.

    Kept as a named view over :attr:`IssueTypeSpec.contradiction` so callers
    that already import ``TypeShapeRule`` keep working; the content is the
    specification's.
    """

    contradicted_section_slug: str
    type_rule_text: str
    remediation_hint: str


def _type_shape_rule(spec: IssueTypeSpec) -> TypeShapeRule | None:
    contradiction = spec.contradiction
    if contradiction is None:
        return None
    return TypeShapeRule(
        contradicted_section_slug=contradiction.slug,
        type_rule_text=contradiction.rule_text,
        remediation_hint=contradiction.remediation_hint,
    )


def _lower_labels(labels: Iterable[str]) -> set[str]:
    return {str(label).strip().lower() for label in labels}


def _normalize_tracking_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _tracking_context_line(line: str, max_len: int = 140) -> str:
    compact = " ".join(line.strip().split())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 3].rstrip() + "..."


def _match_tracking_phrase(text: str, *, subject_only: bool = False) -> str | None:
    patterns = _TRACKING_SUBJECT_PATTERNS if subject_only else _TRACKING_SENTENCE_START_PATTERNS
    for phrase, pattern in patterns:
        if pattern.match(text):
            return phrase
    return None


def _find_tracking_body_declaration(body: str) -> tuple[str, str] | None:
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        heading_match = _HEADING_RE.match(raw_line)
        if heading_match:
            heading = _normalize_tracking_text(heading_match.group("content"))
            phrase = _match_tracking_phrase(heading)
            if phrase is not None:
                return phrase, _tracking_context_line(raw_line)
            continue

        for sentence in _SENTENCE_SPLIT_RE.split(line):
            normalized = _normalize_tracking_text(sentence)
            if not normalized:
                continue
            phrase = _match_tracking_phrase(normalized)
            if phrase is not None:
                return phrase, _tracking_context_line(raw_line)
            phrase = _match_tracking_phrase(normalized, subject_only=True)
            if phrase is not None:
                return phrase, _tracking_context_line(raw_line)
    return None


def _declared_type_spec(labels: Iterable[str]) -> IssueTypeSpec | None:
    """The single declared type specification, or ``None`` when ambiguous."""
    return spec_for_labels(labels)


def _declared_story_type(labels: Iterable[str]) -> str | None:
    spec = _declared_type_spec(labels)
    return spec.label if spec is not None else None


def _heading_text_for_pattern(body: str, pattern: str) -> str | None:
    regex = re.compile(pattern, re.IGNORECASE)
    for match in iter_headings(body):
        heading = match.group(2).strip()
        if regex.search(heading):
            return heading
    return None


def _forbidden_section_headings(body: str, section_keys: Iterable[str]) -> list[str]:
    """Headings in ``body`` that spell one of the given forbidden sections.

    The keys come from the type's specification, so a section added to a type's
    forbidden list is reported here without a second edit. The Diagnosis
    section resolves through the authoritative-heading rule (#2263) rather than
    first-match, so a stale lookalike is not what gets named.
    """
    headings: list[str] = []
    for key in section_keys:
        section = SECTIONS[key]
        if key == DIAGNOSIS_SECTION.key:
            match = find_authoritative_heading(
                body,
                section.heading_pattern,
                DIAGNOSIS_CANONICAL_HEADING_TEXTS,
                diagnosis_completeness_score,
            )
            heading = match.group(2).strip() if match is not None else None
        else:
            heading = _heading_text_for_pattern(body, section.heading_pattern)
        if heading is not None and heading not in headings:
            headings.append(heading)
    return headings


def _format_heading_list(headings: list[str]) -> str:
    if not headings:
        return ""
    quoted = [f"{heading!r}" for heading in headings]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} and {quoted[1]}"
    return ", ".join(quoted[:-1]) + f", and {quoted[-1]}"


# Derived from the single declarative spec (diagnosis_spec.py) — this module
# must not carry its own independent list of required components. Kept as a
# module-level name for backward-compatible imports; the spec is the source of
# truth (#1629).
REQUIRED_DIAGNOSIS_TOKENS: tuple[str, ...] = required_diagnosis_tokens()

# The exact `<fill in>` slot marker `forge shape`'s own scaffold
# (intake.shape_render._diagnosis_bullets) writes as *every* component's
# value in a freshly-appended Diagnosis section. Matched against a single
# extracted field value (whole-value match), not searched for anywhere in a
# section — a genuine diagnosis is free to quote this literal string as part
# of its prose (e.g. evidence describing the bug this marker itself causes)
# without being mistaken for scaffolding (#2263).
_FILL_IN_MARKER_RE = re.compile(r"^\s*<\s*fill\s+in\s*>\s*$", re.IGNORECASE)

# Non-assertion vocabulary admissible as the value of the "confirmed cause" field.
# A bug whose Diagnosis section has every other component but states the cause is
# unknown is investigation-ready (admissible) — not implementation-ready. The dev
# cycle's first job on such bugs is cause discovery, not hypothesized-cause
# implementation. The gate must distinguish these states rather than coercing
# operators into asserting causes they have not confirmed (which produced the
# silent-contract-swap failure mode of #1435/#1446/#1447).
_NON_ASSERTION_CAUSE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:cause\s+(?:is|remains)\s+)?unknown\b", re.IGNORECASE),
    re.compile(
        r"^\s*(?:still\s+|currently\s+)?not\s+yet\s+"
        r"(?:identified|known|determined|confirmed|isolated|established)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*not\s+(?:identified|known|determined|confirmed|established)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:still\s+|currently\s+)?pending\s+investigation\b", re.IGNORECASE),
    re.compile(r"^\s*under\s+investigation\b", re.IGNORECASE),
    re.compile(r"^\s*tbd\b", re.IGNORECASE),
    re.compile(r"^\s*to\s+be\s+(?:determined|identified|investigated|confirmed)\b", re.IGNORECASE),
    re.compile(r"^\s*undetermined\b", re.IGNORECASE),
    re.compile(r"^\s*unconfirmed\b", re.IGNORECASE),
    re.compile(r"^\s*unidentified\b", re.IGNORECASE),
    re.compile(r"^\s*investigation\s+(?:ongoing|in\s+progress)\b", re.IGNORECASE),
    re.compile(r"^\s*investigating\b", re.IGNORECASE),
    re.compile(r"^\s*\?+\s*$"),
    # Placeholder values a producer emits for an unfilled field. These are the
    # shape `render_artifact_markdown` used to emit for an empty
    # ``confirmed_cause`` (#2060) — a slot marker is never a cause claim.
    re.compile(
        r"^\s*_*\(?\s*(?:empty|none(?:\s+recorded)?|not\s+recorded|n/?a)\s*\)?_*\s*$",
        re.IGNORECASE,
    ),
    # Unfilled shape scaffolding is never a cause claim, however
    # label-complete it reads.
    _FILL_IN_MARKER_RE,
)

# Status labels operators can apply to a bug to communicate fix-readiness intent.
# These are advisory — the body's Diagnosis section is the authoritative signal.
RECOGNIZED_STATUS_LABELS: frozenset[str] = frozenset(
    {"status:triage", "status:investigating", "status:diagnosed"}
)
FIX_READY_STATUS_LABELS: frozenset[str] = frozenset({"status:diagnosed"})


def _value_below_label(lines: list[str]) -> str:
    """Return the value carried on the lines below a bare field label.

    Used for the heading and label-only forms, where the value lives on a
    following line rather than after a colon. The first non-blank line is the
    value — unless it opens a sibling field (a new heading or a bullet), in
    which case the field has no value at all.
    """
    for line in lines:
        if not line.strip():
            continue
        if re.match(r"^\s*(?:#|[-*+]\s)", line):
            return ""
        return line.strip()
    return ""


def _extract_label_value(section: str, label_pattern: str) -> str | None:
    """Return the prose value following a bolded/heading field label, if present.

    Operators write a field in several common shapes::

        - **<Label>.** the value is X
        - **<Label>:** unknown
        - <Label>: not yet identified
        - <Label> — pending investigation

    First-party producers — notably ``forge diagnose`` via
    :func:`theforge.diagnose_types.render_artifact_markdown` — write the same
    field as a heading with the value on a following line::

        ### <Label>

        unknown

    Both shapes must yield the same value: a value readable in one form and
    invisible in the other let a diagnose-landed artifact with no confirmed
    cause derive as implementation-ready (#2060).

    Returns the trimmed value, ``""`` when the label carries no value, or
    ``None`` when no labelled occurrence exists. ``label_pattern`` is matched
    case-insensitively as a regex (already ``\\s+``-joined for multi-word
    labels by callers).
    """
    lines = section.splitlines()
    for idx, line in enumerate(lines):
        # Strip leading whitespace, heading markers, bullet markers, and
        # opening bold markers.
        cleaned = re.sub(r"^\s*#+\s*", "", line)
        cleaned = re.sub(r"^\s*(?:[-*+]\s+)?", "", cleaned)
        cleaned = re.sub(r"^\*+\s*", "", cleaned)
        m = re.match(label_pattern + r"\b", cleaned, re.IGNORECASE)
        if m is None:
            continue
        rest = cleaned[m.end() :]
        # Strip any combination of label punctuation, closing bold markers, and
        # whitespace separating the label from its value.
        rest = re.sub(r"^[\s*:.\-—]+", "", rest)
        value = re.sub(r"\*+\s*$", "", rest).strip()
        if value:
            return value
        return _value_below_label(lines[idx + 1 :])
    return None


def _extract_confirmed_cause_value(section: str) -> str | None:
    """Return the value of the 'confirmed cause' field. See :func:`_extract_label_value`."""
    return _extract_label_value(section, r"confirmed\s+cause")


def _is_non_assertion_cause(value: str) -> bool:
    if not value:
        return False
    return any(pattern.match(value) for pattern in _NON_ASSERTION_CAUSE_PATTERNS)


def _is_unfilled_scaffold_section(section: str) -> bool:
    """True when every required component that appears in ``section`` carries
    no real value — either the label is missing outright or its value is
    exactly `forge shape`'s `<fill in>` marker — and at least one component
    explicitly carries that marker.

    Requiring positive evidence of the marker (not just an absence of bolded
    labels) means an ordinary section that simply doesn't use the bolded-bullet
    convention is never mistaken for scaffolding, and a genuine diagnosis that
    happens to quote the marker string as prose in one field is not penalized
    as long as some other field carries real content (#2263).
    """
    saw_marker = False
    for component in REQUIRED_DIAGNOSIS_COMPONENTS:
        label_pattern = r"\s+".join(re.escape(word) for word in component.label.split())
        value = _extract_label_value(section, label_pattern)
        if value is None:
            continue
        if _FILL_IN_MARKER_RE.match(value.strip()):
            saw_marker = True
            continue
        return False
    return saw_marker


def diagnosis_completeness_score(section: str) -> int:
    """Rank a candidate Diagnosis section by how much of the required shape
    it actually satisfies, so a complete, confirmed-cause artifact outranks
    a placeholder or a stale earlier draft regardless of which is longer or
    which the document happens to place first (#2263).

    Score is the count of required components present, plus a large bonus
    when the confirmed-cause field carries a specific claim rather than
    non-assertion vocabulary (``unknown``, ``TBD``, an empty slot, ...) —
    an asserted cause always outranks any token-count tie, matching the
    same "asserted beats non-asserted beats missing" ordering
    :func:`cause_assertion_state` reports.

    A section whose components are *all* unfilled `<fill in>` scaffold slots
    is heavily penalized regardless of how many required labels it lists:
    listing every label is exactly what an unfilled `forge shape` scaffold
    does by construction, so label count alone cannot tell it apart from a
    genuine artifact. Any section with real content in at least one field —
    even an inconclusive, cause-unknown diagnosis, even one that quotes the
    marker string itself as prose — must outrank pure scaffolding (#2263).
    """
    section_lower = section.lower()
    score = sum(1 for tok in REQUIRED_DIAGNOSIS_TOKENS if tok in section_lower)
    cause_value = _extract_confirmed_cause_value(section)
    if cause_value and not _is_non_assertion_cause(cause_value):
        score += len(REQUIRED_DIAGNOSIS_TOKENS) + 1
    if _is_unfilled_scaffold_section(section):
        score -= len(REQUIRED_DIAGNOSIS_TOKENS) + 1
    return score


def cause_assertion_state(body: str) -> str:
    """Classify the confirmed-cause field of the Diagnosis section.

    Returns one of:

    - ``"asserted"``: a confirmed-cause line exists and its value is a
      specific cause claim (i.e. not non-assertion vocabulary).
    - ``"non_asserted"``: the field exists but its value is non-assertion
      vocabulary (``unknown``, ``not yet identified``, ``pending
      investigation``, ``TBD``, etc.), a slot placeholder, or empty. Bug is
      investigation-ready.
    - ``"missing"``: no confirmed-cause line present (or no Diagnosis
      section at all).

    A field written but left empty is a non-assertion, not an assertion: the
    record states no cause was found, and must never read as one (#2060).
    """
    section = extract_authoritative_section(
        body,
        DIAGNOSIS_HEADING_PATTERN,
        DIAGNOSIS_CANONICAL_HEADING_TEXTS,
        diagnosis_completeness_score,
    )
    if section is None:
        return "missing"
    value = _extract_confirmed_cause_value(section)
    if value is None:
        return "missing"
    if not value or _is_non_assertion_cause(value):
        return "non_asserted"
    return "asserted"


def diagnosis_completeness(body: str) -> tuple[bool, list[str]]:
    """Return (is_complete, missing_tokens) for a bug's Diagnosis section.

    Returns ``(False, ["missing Diagnosis section"])`` when the section is
    absent. Returns ``(False, [...])`` when the section is present but lacks
    one or more required tokens. Returns ``(True, [])`` only when every
    required token appears within the section text (case-insensitive).

    The ``confirmed cause`` token requires presence of the field label; the
    field's *value* may be either a specific cause or a non-assertion phrase
    such as ``unknown`` or ``not yet identified``. Distinguishing those two
    states is the job of :func:`cause_assertion_state` — the gate's only
    responsibility here is verifying the operator filled the slot at all.
    """
    section = extract_authoritative_section(
        body,
        DIAGNOSIS_HEADING_PATTERN,
        DIAGNOSIS_CANONICAL_HEADING_TEXTS,
        diagnosis_completeness_score,
    )
    if section is None:
        return False, ["missing Diagnosis section"]
    section_lower = section.lower()
    missing = [tok for tok in REQUIRED_DIAGNOSIS_TOKENS if tok not in section_lower]
    if missing:
        return False, missing
    return True, []


def _format_diagnosis_labels(tokens: Iterable[str]) -> str:
    labels: list[str] = []
    for token in tokens:
        component = component_for_token(token)
        if component is None:
            labels.append(f'"{token}"')
            continue
        labels.append(f'"**{component.label}:**"')
    return ", ".join(labels)


def _diagnosis_unreadable_region_hint(body: str, missing: list[str]) -> str:
    """Explain when a missing diagnosis section/component exists only unreadably."""
    region_matches: list[tuple[str, tuple[str, ...]]] = []

    for region_name, region_bodies in (
        ("blockquoted region", blockquote_blocks(body)),
        ("fenced code block", fenced_code_blocks(body)),
    ):
        matched_tokens: set[str] = set()
        for region_body in region_bodies:
            if missing == ["missing Diagnosis section"]:
                if has_heading(region_body, DIAGNOSIS_HEADING_PATTERN):
                    region_matches.append((region_name, ()))
                    break
                continue
            section = extract_authoritative_section(
                region_body,
                DIAGNOSIS_HEADING_PATTERN,
                DIAGNOSIS_CANONICAL_HEADING_TEXTS,
                diagnosis_completeness_score,
            )
            if section is None:
                continue
            section_lower = section.lower()
            for token in missing:
                if token != "missing Diagnosis section" and token in section_lower:
                    matched_tokens.add(token)
        if matched_tokens:
            region_matches.append((region_name, tuple(sorted(matched_tokens))))

    if not region_matches:
        return ""

    if missing == ["missing Diagnosis section"]:
        regions = ", ".join(f"a {name}" for name, _tokens in region_matches)
        return (
            f" A `## Diagnosis` section already appears inside {regions}, which the gate "
            "does not scan; restate it as ordinary top-level Markdown outside quoted "
            "or fenced regions."
        )

    parts: list[str] = []
    for region_name, tokens in region_matches:
        if not tokens:
            continue
        parts.append(f"{_format_diagnosis_labels(tokens)} in a {region_name}")
    if not parts:
        return ""
    joined = "; ".join(parts)
    return (
        " The gate also found "
        f"{joined}, but only inside a region it does not scan; move those "
        "bullets into the top-level `## Diagnosis` section."
    )


def derive_fix_ready(
    story_type: str | None, body: str, labels: Iterable[str] | None = None
) -> tuple[bool | None, bool, list[str]]:
    """Compute the (fix_ready, investigation_ready, warnings) signals.

    Rules:
    - ``type=None`` → ``(None, False, [warning])`` (cannot determine).
    - ``type='bug'`` with a complete Diagnosis section whose confirmed-cause
      value is a specific claim → ``(True, False, [])`` (implementation-ready).
    - ``type='bug'`` with a complete Diagnosis section whose confirmed-cause
      value is non-assertion vocabulary (``unknown``, ``not yet identified``,
      ``pending investigation``, ``TBD``, etc.) → ``(True, True, [warning])``
      (investigation-ready). The bug passes the shape gate; the dev cycle's
      first job is cause discovery, not hypothesized-cause implementation.
    - ``type='bug'`` with a missing or incomplete Diagnosis section →
      ``(False, False, [warnings])``.
    - Any other recognized type (enhancement, task, spike, epic) →
      ``(True, False, [])``.

    Status labels (e.g. ``status:diagnosed``) are operator intent and do not
    override body content per AC: absence of the Diagnosis section flips a
    bug to not-fix-ready regardless of label.
    """
    if story_type is None:
        return None, False, ["fix-readiness undetermined: story type unknown"]
    if story_type == "bug":
        ok, missing = diagnosis_completeness(body)
        if ok:
            state = cause_assertion_state(body)
            if state == "non_asserted":
                return (
                    True,
                    True,
                    [
                        "bug is investigation-ready: Diagnosis section is complete but the "
                        "confirmed-cause field is a non-assertion (e.g. 'unknown', 'not yet "
                        "identified'). Dev cycle's first job is cause discovery."
                    ],
                )
            if state == "missing":
                # Completeness only saw the label token somewhere in the
                # section; no field value could be read. An unreadable cause is
                # not an asserted cause — refusing to promote it keeps the
                # three-state invariant closed against producer drift (#2060).
                return (
                    True,
                    True,
                    [
                        "bug is investigation-ready: the Diagnosis section's "
                        "confirmed-cause field carries no readable value. Dev cycle's "
                        "first job is cause discovery."
                    ],
                )
            return True, False, []
        if missing == ["missing Diagnosis section"]:
            return (
                False,
                False,
                [
                    "bug missing Diagnosis section — not fix-ready (run "
                    "`forge diagnose` or add diagnosis manually)"
                ],
            )
        return (
            False,
            False,
            [f"bug Diagnosis section missing required component: {tok}" for tok in missing],
        )
    return True, False, []


def is_bug_format_issue(body: str, labels: Iterable[str]) -> bool:
    """Return true for bug reports, which use observed/expected sections instead of AC.

    Heading detection is delegated to :func:`has_bug_body_headings` so this gate
    and the shape classifier cannot drift apart on what a bug body looks like.
    """
    if _lower_labels(labels) & _BUG_LABELS:
        return True
    return has_bug_body_headings(body)


def _governing_spec(body: str, labels: Iterable[str]) -> IssueTypeSpec:
    """The specification whose rules apply to this body.

    The declared type label decides it. When no single type is declared, the
    body's own shape does: a bug-report body is governed by the bug
    specification, anything else by the feature specification. An undeclared
    type is itself a blocking finding (``missing_type``); this fallback only
    keeps the *other* findings from being nonsense in the meantime.
    """
    spec = _declared_type_spec(labels)
    if spec is not None:
        return spec
    return BUG_SPEC if is_bug_format_issue(body, labels) else ENHANCEMENT_SPEC


def _presence_of(body: str, labels: Iterable[str], section_key: str) -> Presence:
    """What the governing specification says about one section."""
    return _governing_spec(body, labels).presence_of(section_key)


def check_missing_type(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    """Flag issues that lack a structured type label.

    Type is the deterministic input that drives downstream phase behavior
    (preflight, plan, dev, review). Without it, consumers fall back to
    inferring type from prose density, which is the misclassification risk
    this check exists to prevent.
    """
    matches = _lower_labels(labels) & _RECOGNIZED_TYPE_LABELS
    if not matches:
        return Reason(
            code="missing_type",
            severity=Severity.BLOCKING,
            detail=(
                "Issue has no recognized type label — expected one of: "
                f"{', '.join(sorted(_RECOGNIZED_TYPE_LABELS))}."
            ),
        )
    if len(matches) > 1:
        return Reason(
            code="missing_type",
            severity=Severity.BLOCKING,
            detail=(
                f"Issue has multiple type labels {sorted(matches)!r} — "
                "exactly one type label is required."
            ),
        )
    return None


# Phrases that explicitly mark a bug's confirmed cause as still open.
# Narrow on purpose: an unrelated body line containing "tbd" elsewhere
# must not flip a complete diagnosis into "cause unknown". The separator class
# admits newlines, so the heading form (label, blank line, value) matches too.
_UNRESOLVED_CAUSE_RE = re.compile(
    r"confirmed\s+cause[^\w\n]*[:.\-\*\s]*"
    r"(?:not\s+yet\s+identified|unknown|tbd|investigating|n/?a)\b",
    re.IGNORECASE,
)


def _diagnosis_cause_unknown(body: str) -> bool:
    """Return True when the Diagnosis section's confirmed cause is still open.

    This is the admissible "investigation-ready" state: the bug is not
    malformed (it has been investigated), but it is not implementation-runnable
    because no cause has been confirmed.

    A confirmed-cause field written but left without a value counts as open:
    the vocabulary above cannot see an absence, and a producer that emits the
    label with nothing under it is reporting no cause, not a cause (#2060).
    `forge shape`'s own unfilled `<fill in>` scaffold marker counts as open
    too, for the same reason — a slot nobody filled is not a cause claim
    (#2263). This function intentionally recognizes a narrower vocabulary
    than :func:`cause_assertion_state`'s full non-assertion list (e.g. it
    does not flag "pending investigation"): that broader set is admissible
    as RUNNABLE with an advisory reason elsewhere, and is not this
    function's distinct ``diagnosis_cause_unknown`` ShapeVerdict.
    """
    section = extract_authoritative_section(
        body,
        DIAGNOSIS_HEADING_PATTERN,
        DIAGNOSIS_CANONICAL_HEADING_TEXTS,
        diagnosis_completeness_score,
    )
    if section is None:
        return False
    if _UNRESOLVED_CAUSE_RE.search(section):
        return True
    value = _extract_confirmed_cause_value(section)
    if value is None:
        return False
    return not value or bool(_FILL_IN_MARKER_RE.match(value.strip()))


def check_bug_missing_diagnosis(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    """Three-state diagnosis classification for bug-typed issues.

    - No Diagnosis section → BLOCKING ``needs_diagnosis``: the body is
      symptom-only and would let an implementer hypothesize a cause that
      reviewers cannot verify against. This is the original failure mode.
    - Diagnosis section present and complete, but confirmed cause explicitly
      unresolved → ADVISORY ``diagnosis_cause_unknown``. The issue is
      admissible (well-formed) but the verdict-derivation layer lifts it to
      its own verdict so the sprint gate keeps it out of implementation runs.
    - Complete Diagnosis section → no Reason; bug is implementation-runnable.

    The three states are the bug specification's lifecycle states, and the
    codes emitted here are the ``refusal_code`` those states declare.
    """
    if _presence_of(body, labels, DIAGNOSIS_SECTION.key) is not Presence.REQUIRED:
        return None
    ok, missing = diagnosis_completeness(body)
    if ok:
        # All required Diagnosis labels are present, but the confirmed-cause
        # line may still be explicitly unresolved ("not yet identified",
        # etc.) — that's the admissible-but-not-runnable state.
        if _diagnosis_cause_unknown(body):
            return Reason(
                code="diagnosis_cause_unknown",
                severity=Severity.ADVISORY,
                detail=(
                    "Bug Diagnosis section is complete but no confirmed cause is "
                    "asserted — investigation-ready but not "
                    "implementation-runnable."
                ),
            )
        return None
    if missing == ["missing Diagnosis section"]:
        unreadable_hint = _diagnosis_unreadable_region_hint(body, missing)
        return Reason(
            code="needs_diagnosis",
            severity=Severity.BLOCKING,
            detail=(
                "Bug has no Diagnosis section — not fix-ready. Add a '## Diagnosis' "
                "section containing "
                # Derive the full required-component list from the single spec so
                # the no-section message quotes the same literal labels + examples
                # the validator checks (#1629), not a hand-maintained name list.
                f"{describe_missing(required_diagnosis_tokens())} "
                "before sprinting (or run `forge diagnose` when available). The "
                "confirmed-cause value may be a specific claim or a non-assertion "
                "phrase such as 'unknown', 'not yet identified', 'pending "
                "investigation', or 'TBD' — investigation-ready bugs are admissible; "
                f"only symptom-only bodies are refused.{unreadable_hint} "
                f"Full shape reference: {BUG_SHAPE_REFERENCE_PATH}"
            ),
        )
    if _diagnosis_cause_unknown(body):
        return Reason(
            code="diagnosis_cause_unknown",
            severity=Severity.ADVISORY,
            detail=(
                "Bug Diagnosis section is complete but no confirmed cause is asserted "
                "— investigation-ready but not implementation-runnable."
            ),
        )
    return Reason(
        code="needs_diagnosis",
        severity=Severity.BLOCKING,
        detail=(
            "Bug Diagnosis section is incomplete — missing "
            f"{describe_missing(missing)}.{_diagnosis_unreadable_region_hint(body, missing)} "
            f"Full shape reference: {BUG_SHAPE_REFERENCE_PATH}"
        ),
    )


def _section_content(section: str) -> str:
    """Return a section's content without its heading line."""
    _heading, _sep, rest = section.partition("\n")
    return rest


def _required_bug_section_reason(
    body: str,
    labels: Iterable[str],
    *,
    section_key: str,
    heading_pattern: str,
    canonical_texts: tuple[str, ...],
    detail_heading: str,
    code: str,
) -> Reason | None:
    if _presence_of(body, labels, section_key) is not Presence.REQUIRED:
        return None
    section = extract_authoritative_section(body, heading_pattern, canonical_texts)
    if section is None:
        condition = "absent"
    else:
        content = _section_content(section)
        if not content.strip():
            condition = "empty"
        elif is_placeholder_only(content):
            condition = "contains only placeholder scaffolding"
        else:
            return None
    return Reason(
        code=code,
        severity=Severity.BLOCKING,
        detail=(
            f"Bug {detail_heading!r} section is required but {condition}. "
            f"Full shape reference: {BUG_SHAPE_REFERENCE_PATH}"
        ),
    )


def check_bug_missing_observed(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    """Refuse bug bodies missing required observed section content.

    The test is purely mechanical: a required section fails only when it is
    absent, empty, or placeholder-only. Any non-placeholder prose satisfies
    the requirement, whatever its wording or length.
    """
    if not _lower_labels(labels) & _BUG_LABELS:
        return None
    return _required_bug_section_reason(
        body,
        labels,
        section_key=OBSERVED_SECTION.key,
        heading_pattern=OBSERVED_HEADING_PATTERN,
        canonical_texts=OBSERVED_CANONICAL_HEADING_TEXTS,
        detail_heading=OBSERVED_SECTION.canonical_heading,
        code="missing_observed",
    )


def check_bug_missing_expected(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    """Refuse bug bodies missing required expected section content."""
    if not _lower_labels(labels) & _BUG_LABELS:
        return None
    return _required_bug_section_reason(
        body,
        labels,
        section_key=EXPECTED_SECTION.key,
        heading_pattern=EXPECTED_HEADING_PATTERN,
        canonical_texts=EXPECTED_CANONICAL_HEADING_TEXTS,
        detail_heading=EXPECTED_SECTION.canonical_heading,
        code="missing_expected",
    )


def check_type_shape_contradiction(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    """Flag bodies whose top-level sections contradict their declared type.

    Both the forbidden sections and the wording of the refusal come from the
    declared type's specification: a bug may not carry the feature-style
    acceptance-criteria section, and the feature-shaped types may not use the
    bug report sections. Marking a section ``Presence.FORBIDDEN`` on a type is
    the whole edit — this function reads that, it does not restate it.

    Each forbidden rule carries its own trigger, because sections differ in how
    much they say alone. ``## Diagnosis`` on an enhancement is a defect
    investigation however the rest of the body reads, so it is refused on sight;
    ``## Expected`` is ordinary English about intended behavior, so it counts
    only when the body carries the whole bug-report shape around it.
    """
    spec = _declared_type_spec(labels)
    if spec is None:
        return None

    # The forbidden set — and when each member of it counts — is the section
    # rules themselves. The contradiction block carries only *how the refusal
    # reads*, never a second list of which sections are forbidden: that is the
    # drift this specification exists to remove.
    shape_keys = spec.forbidden_keys_with_trigger(ContradictionTrigger.BUG_BODY_SHAPE)
    carries_bug_shape = bool(shape_keys) and has_bug_body_headings(body)
    enforced_keys = [
        key
        for key in spec.section_keys_with(Presence.FORBIDDEN)
        if key not in shape_keys or carries_bug_shape
    ]
    # Section order is declaration order, so a refusal names the sections the
    # way the specification lists them.
    offending_headings = _forbidden_section_headings(body, enforced_keys)
    if not offending_headings:
        return None

    declared_type = spec.label
    rule = _type_shape_rule(spec)
    if rule is None:
        # A type that forbids sections without declaring how to say so is still
        # enforced; it just gets the specification's own words for it.
        return Reason(
            code="type_shape_contradiction",
            severity=Severity.BLOCKING,
            detail=(
                f"{declared_type} body carries "
                f"{_format_heading_list(offending_headings)}, which the {declared_type} "
                f"specification forbids (see {ISSUE_SHAPE_REFERENCE_PATH}). "
                "Remove the section or relabel the issue."
            ),
        )

    if len(offending_headings) == 1:
        article = "an" if rule.contradicted_section_slug[:1].lower() in "aeiou" else "a"
        return Reason(
            code="type_shape_contradiction",
            severity=Severity.BLOCKING,
            detail=(
                f"{declared_type} body carries {article} {rule.contradicted_section_slug} section"
                f" ({offending_headings[0]!r}); "
                f"{rule.type_rule_text} (see authoring guide). "
                f"{rule.remediation_hint.capitalize()}."
            ),
        )

    return Reason(
        code="type_shape_contradiction",
        severity=Severity.BLOCKING,
        detail=(
            f"{declared_type} body carries {rule.contradicted_section_slug} sections "
            f"{_format_heading_list(offending_headings)}; "
            f"{rule.type_rule_text} (see authoring guide). "
            f"{rule.remediation_hint.capitalize()}."
        ),
    )


def check_epic_or_tracking(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    lset = _lower_labels(labels)
    title_l = title.strip().lower()
    if title_l.startswith("epic:") or title_l.startswith("epic "):
        return Reason(
            code="epic_or_tracking",
            severity=Severity.BLOCKING,
            detail="Title is prefixed with 'Epic:' — treat as tracking-only.",
        )
    if "epic" in lset:
        return Reason(
            code="epic_or_tracking",
            severity=Severity.BLOCKING,
            detail="Label 'epic' present — treat as tracking-only.",
        )
    match = _find_tracking_body_declaration(body)
    if match is not None:
        phrase, context = match
        return Reason(
            code="epic_or_tracking",
            severity=Severity.BLOCKING,
            detail=f"Body declares tracking intent via '{phrase}' in line: {context!r}.",
        )
    return None


def check_missing_acceptance_criteria(
    title: str, body: str, labels: Iterable[str]
) -> Reason | None:
    """Refuse a body whose type requires acceptance criteria and has none.

    Which types require the section is the specification's call, not this
    function's — a bug's specification forbids it, so a bug is exempt here by
    derivation rather than by a private label check.
    """
    if _presence_of(body, labels, ACCEPTANCE_CRITERIA_SECTION.key) is not Presence.REQUIRED:
        return None
    placeholder_only = False
    if has_heading(body, ACCEPTANCE_CRITERIA_HEADING_PATTERN):
        section = extract_ac_section(body) or ""
        # Placeholder bullets are shaped like criteria but assert nothing;
        # measure substance on what remains once they are stripped.
        if extract_bullets(strip_placeholder_content(section)):
            return None
        placeholder_only = bool(extract_bullets(section))
    detail = (
        "Acceptance criteria section contains only placeholder/TODO text — "
        "no verifiable criteria supplied."
        if placeholder_only
        else "No acceptance criteria section with a bullet/checklist found."
    )
    return Reason(
        code="missing_acceptance_criteria",
        severity=Severity.BLOCKING,
        detail=detail,
    )


def _extract_example_section(body: str) -> str | None:
    for pattern in _EXAMPLE_SECTION_PATTERNS:
        section = extract_section(body, pattern)
        if section is not None:
            return section
    return None


def _has_structured_example_content(section: str) -> bool:
    if fenced_code_blocks(section):
        return True
    lines = [line.rstrip() for line in section.splitlines()]
    if any(re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line) for line in lines):
        return True

    table_like_lines = 0
    for line in lines:
        stripped = line.strip()
        if stripped.count("|") >= 2:
            table_like_lines += 1
            if table_like_lines >= 2:
                return True
        elif stripped:
            table_like_lines = 0
    return False


def check_missing_example(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    """Report a missing concrete example on types whose spec asks for one.

    ``Presence.ADVISORY`` in the specification is exactly this finding's
    severity: it informs a reader and decides nothing (ADR-0009 clause 4).
    """
    if _presence_of(body, labels, EXAMPLE_SECTION.key) is not Presence.ADVISORY:
        return None
    section = _extract_example_section(body)
    if section is None:
        return Reason(
            code="missing_example",
            severity=Severity.ADVISORY,
            detail="No recognizable example section found for this feature-format issue.",
        )

    # A section that only says an example is missing is not an example.
    # Strip placeholder scaffolding before measuring substance, so a producer
    # cannot resolve this finding with text merely shaped like the answer.
    substantive = strip_placeholder_content(section)
    content_chars = len(re.sub(r"\s+", "", substantive))
    if content_chars < _EXAMPLE_MIN_CONTENT_CHARS or not _has_structured_example_content(
        substantive
    ):
        detail = (
            "Example section contains only placeholder/TODO text — no concrete example supplied."
            if is_placeholder_only(section)
            else (
                "Example section is present but not substantive; include a concrete list, "
                "table, or fenced example."
            )
        )
        return Reason(
            code="missing_example",
            severity=Severity.ADVISORY,
            detail=detail,
        )
    return None


_SUPERSEDED_RE = re.compile(r"superseded\s+by\s+#\d+", re.IGNORECASE)


def check_superseded(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    if _SUPERSEDED_RE.search(title) or _SUPERSEDED_RE.search(body):
        return Reason(
            code="superseded",
            severity=Severity.BLOCKING,
            detail="Issue marks itself as superseded by another issue.",
        )
    return None


def check_untriaged_finding(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    lset = _lower_labels(labels)
    if "forge-finding" not in lset:
        return None
    if _NEEDS_TRIAGE_LABEL not in lset:
        return None
    return Reason(
        code="untriaged_finding",
        severity=Severity.BLOCKING,
        detail="finding still has needs-triage label; remove needs-triage after triage",
    )


_DESIGN_SIGNATURE_RES = (
    re.compile(r"^\s*(?:def|class)\s+\w+", re.MULTILINE),
    re.compile(r"^\s*prompt\s*:\s*[\"'|>]", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*(?:return|raise)\s+\w", re.MULTILINE),
)

_DESIGN_FENCE_LANGS = {"python", "py", "yaml", "yml"}


def check_implementation_design_dump(
    title: str, body: str, labels: Iterable[str]
) -> Reason | None:
    """Conservative — only fire when the AC section itself is loaded with code/config dumps."""
    ac = extract_ac_section(body)
    if not ac:
        return None
    # Count suspicious signals inside AC.
    signals = 0
    for block in fenced_code_blocks(ac):
        for pat in _DESIGN_SIGNATURE_RES:
            if pat.search(block):
                signals += 1
                break
    # Also catch explicit function/class defs not in fences
    for pat in _DESIGN_SIGNATURE_RES[:1]:
        signals += len(pat.findall(ac))
    # Detect fenced blocks tagged python/yaml even without signature
    for line in ac.splitlines():
        m = re.match(r"^\s*(?:`{3,}|~{3,})(\w+)\s*$", line)
        if m and m.group(1).lower() in _DESIGN_FENCE_LANGS:
            signals += 1
    if signals >= 3:
        return Reason(
            code="implementation_design_dump",
            severity=Severity.ADVISORY,
            detail=(
                f"AC section contains {signals} code/config signals — likely implementation dump."
            ),
        )
    return None


_PLAN_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+"
    r"(?P<title>"
    r"design"
    r"|implementation(?:\s+(?:plan|notes|details|strategy|approach))?"
    r"|technical\s+design"
    r"|proposed\s+(?:implementation|design|approach)"
    r")\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)

_FILE_LINE_RE = re.compile(
    r"(?<![\w/])"
    r"(?P<path>(?:[\w][\w./-]*/)?[\w][\w.-]*\.(?:py|pyi|js|jsx|ts|tsx|yaml|yml|json|sh|toml|rs|go|java|rb))"
    r":(?P<line>\d+)\b"
)

_FILE_PATH_RE = re.compile(
    r"(?<![\w/])"
    r"(?:[\w][\w.-]*/)+[\w][\w.-]*\.(?:py|pyi|js|jsx|ts|tsx|yaml|yml|json|sh|toml|rs|go|java|rb|md)"
    r"(?![\w./])"
)

_PLAN_EXCEPTION_RE = re.compile(
    r"^\s*(?:implementation\s+target|refactor\s+target)\s*:\s*\S",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_fenced_blocks(body: str) -> str:
    """Drop fenced code, so prose heuristics never read quoted samples as prose.

    Shares :class:`FenceTracker` with the structural parsers: one definition of
    what counts as literal content, both fence families included.
    """
    tracker = FenceTracker()
    return "\n".join(line for line in body.splitlines() if tracker.feed(line) == "outside")


_BUG_FIX_LOCATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bfix\s+belongs\s+in\b", re.IGNORECASE),
    re.compile(r"\bfix\s+should\s+(?:go|live|land|be)\s+(?:in|under)\b", re.IGNORECASE),
    re.compile(r"\bpatch\s+should\s+(?:go|live|land|be)\s+(?:in|under)\b", re.IGNORECASE),
)

_BUG_TEST_REQUIREMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:add|write|include|require)\b[^\n.]{0,80}\b"
        r"(?:(?:regression|unit|integration|contract|end-to-end|e2e)\s+)?tests?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\btests?\s+(?:should|must|need(?:s)?\s+to|are\s+required\s+to)\b",
        re.IGNORECASE,
    ),
)


def _matching_bug_phrase_line(
    body: str, patterns: tuple[re.Pattern[str], ...]
) -> tuple[str, str] | None:
    for raw_line in _strip_fenced_blocks(body).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for pattern in patterns:
            if pattern.search(line):
                return pattern.pattern, _tracking_context_line(raw_line)
    return None


def check_bug_fix_location_prescription(
    title: str, body: str, labels: Iterable[str]
) -> Reason | None:
    if not is_bug_format_issue(body, labels):
        return None
    match = _matching_bug_phrase_line(body, _BUG_FIX_LOCATION_PATTERNS)
    if match is None:
        return None
    _pattern, context = match
    return Reason(
        code="bug_fix_location_prescription",
        severity=Severity.ADVISORY,
        detail=(
            f"Bug body prescribes a fix location in line: {context!r}. "
            "Keep bug bodies on observed/expected/diagnosis and leave file placement to planning."
        ),
    )


def check_bug_test_requirement(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    if not is_bug_format_issue(body, labels):
        return None
    match = _matching_bug_phrase_line(body, _BUG_TEST_REQUIREMENT_PATTERNS)
    if match is None:
        return None
    _pattern, context = match
    return Reason(
        code="bug_test_requirement",
        severity=Severity.ADVISORY,
        detail=(
            f"Bug body contains test-requirement phrasing in line: {context!r}. "
            "Bug reports should describe expected behavior, not prescribe tests."
        ),
    )


_EXAMPLE_HEADING_RE = re.compile(
    r"^\s{0,3}(?P<hashes>#{1,6})\s+"
    r"(?:examples?|target(?:\s+(?:sketch|output|state))?|"
    r"what\s+it\s+should\s+look\s+like)"
    r"\s*:?\s*$",
    re.IGNORECASE,
)


def _strip_example_section(body: str) -> str:
    out: list[str] = []
    skip_level: int | None = None
    for line in body.splitlines():
        if skip_level is not None:
            heading_m = re.match(r"^\s{0,3}(#{1,6})\s+", line)
            if heading_m and len(heading_m.group(1)) <= skip_level:
                skip_level = None
                out.append(line)
            continue
        m = _EXAMPLE_HEADING_RE.match(line)
        if m:
            skip_level = len(m.group("hashes"))
            continue
        out.append(line)
    return "\n".join(out)


def check_implementation_plan_in_body(
    title: str, body: str, labels: Iterable[str]
) -> Reason | None:
    """Flag issue bodies that contain implementation-plan content (HOW, not WHAT).

    A story body is meant to describe observable behavior. When it instead
    pre-names file paths, function signatures, line numbers, or carries a
    ``## Design`` section, the body becomes a structural plan that dev agents
    can verify against existing code while missing the actually-undelivered
    behavior — the failure mode documented in superseded issue #1110.

    Bug-format issues are exempt because their Diagnosis section is required
    to name the affected code path — the exemption is derived from the type's
    specification requiring that section, not asserted separately. Stories that
    legitimately describe a refactor of one named module can opt out by adding a
    body line of the form ``Implementation target: <path>``.
    """
    if _presence_of(body, labels, DIAGNOSIS_SECTION.key) is Presence.REQUIRED:
        return None
    if _PLAN_EXCEPTION_RE.search(body):
        return None

    pruned = _strip_example_section(_strip_fenced_blocks(body))

    signals: list[str] = []

    heading_match = _PLAN_HEADING_RE.search(pruned)
    if heading_match is not None:
        heading_text = heading_match.group(0).strip()
        signals.append(f"plan-style heading {heading_text!r}")

    file_line_hits = [m.group(0) for m in _FILE_LINE_RE.finditer(pruned)]
    if file_line_hits:
        sample = sorted(set(file_line_hits))[:3]
        signals.append(f"{len(file_line_hits)} file:line reference(s) (e.g. {', '.join(sample)})")

    pruned_no_fileline = _FILE_LINE_RE.sub("", pruned)
    path_hits = _FILE_PATH_RE.findall(pruned_no_fileline)
    distinct_paths = sorted(set(path_hits))
    if distinct_paths:
        sample = distinct_paths[:3]
        signals.append(
            f"{len(path_hits)} file path reference(s) "
            f"({len(distinct_paths)} distinct, e.g. {', '.join(sample)})"
        )

    fires = heading_match is not None or bool(file_line_hits) or len(distinct_paths) >= 2
    if not fires:
        return None

    return Reason(
        code="implementation_plan_in_body",
        severity=Severity.BLOCKING,
        detail=(
            "Body contains implementation-plan content (HOW belongs in the plan "
            "phase, not the issue body): "
            + "; ".join(signals)
            + ". Rewrite acceptance criteria as observable behavior and remove "
            "any ## Design / ## Implementation section. To opt out for a "
            "legitimate single-file refactor, add a body line "
            "'Implementation target: <path>'."
        ),
    )


def check_no_observable_done_state(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    """Refuse a body that states no done condition at all.

    Structural only: the section must exist and must carry bullets. Whether a
    criterion is *meaningfully* observable is a semantic question, and the
    closed thirty-word verb vocabulary that used to answer it here has been
    retired as an admission input (ADR-0009 clause 5).

    It was not merely imprecise, it was unanswerable from the text: the list was
    closed and tense-sensitive (``writes`` counted, ``written`` did not), and
    bullets were truncated to their first line before matching, so a criterion
    whose verb landed on a wrapped continuation line read as having none.
    Admission depended on word choice, verb tense and line wrapping rather than
    on whether a reviewer could check the outcome.
    """
    if _presence_of(body, labels, ACCEPTANCE_CRITERIA_SECTION.key) is not Presence.REQUIRED:
        return None
    ac = extract_ac_section(body)
    if not ac:
        return Reason(
            code="no_observable_done_state",
            severity=Severity.BLOCKING,
            detail=(
                "No acceptance criteria section — "
                "no observable way for a reviewer to verify completion."
            ),
        )
    if not extract_bullets(ac):
        return Reason(
            code="no_observable_done_state",
            severity=Severity.BLOCKING,
            detail="AC section has no bullets — no observable done state.",
        )
    return None


# --- live-run evidence detection (#1735) ------------------------------------
#
# Some acceptance criteria are satisfiable only by the *recorded outcome of a
# live run* — its dollar cost, its wall-clock duration, its budget consumption,
# or whether the artifact an agent produced during that run was complete. The
# dev loop produces exactly one class of evidence: source changes, deterministic
# tests, and a gate exit code, and a reviewer only ever sees the diff. A
# criterion in the live-run class therefore cannot be satisfied by any diff the
# loop can produce; discovering that through iteration exhaustion is the
# expensive way to learn what the story text says for free (#1425, run
# 0f519b0b845b: dev spent its whole iteration budget on such a criterion).
#
# Detection requires TWO independent signals in the *same* criterion:
#   1. a live-run scope — the criterion is about an actual execution instance
#      ("a diagnose run", "running the pipeline", "a live agent run"), and
#   2. a recorded-outcome assertion — a property observable only by performing
#      that run (cost, duration, budget consumption, produced-artifact
#      completeness).
# The conjunction is deliberate. A criterion that merely *mentions* runs,
# budgets, agents, or artifacts (e.g. "the report lists each agent's cost",
# "forge run prints warnings", "the agent produces a diff") describes an
# inspectable source feature and must not fire (#1735 AC3).

_LIVE_RUN_SCOPE_RES: tuple[re.Pattern[str], ...] = (
    # "a diagnose run", "the run", "one sprint run" — run as an execution noun.
    re.compile(
        r"\b(?:a|an|the|each|one|any|every|another|this|first|second)\s+"
        r"(?:\w+[- ]){0,3}run\b",
        re.IGNORECASE,
    ),
    # "run on an issue", "run produces", "run completes" — run + outcome verb.
    re.compile(
        r"\brun\s+(?:on|against|of|over|produces?|produced|completes?|completed|"
        r"yields?|results?|takes?|costs?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\brunning\b", re.IGNORECASE),
    re.compile(r"\blive\s+(?:run|agent|invocation|execution|dispatch)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:agent|pipeline|dev|sprint|diagnose|diagnosis|end-to-end|e2e|real|actual)"
        r"[- ]run\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bactual\s+(?:invocation|execution)\b", re.IGNORECASE),
    re.compile(r"\bexecuting\b", re.IGNORECASE),
    re.compile(r"\bon\s+a\s+(?:representative|real|live|sample)\b", re.IGNORECASE),
)

_LIVE_RUN_OUTCOME_RES: tuple[re.Pattern[str], ...] = (
    # Budget consumption of an execution.
    re.compile(r"\bwithin\s+(?:its\s+|the\s+|a\s+)?budget\b", re.IGNORECASE),
    re.compile(
        r"\b(?:under|over|exceeds?|exhausts?|stays?\s+(?:with)?in|consumes?|spends?)\s+"
        r"(?:its\s+|the\s+|a\s+)?(?:\w+\s+){0,2}budget\b",
        re.IGNORECASE,
    ),
    re.compile(r"\biteration\s+budget\b", re.IGNORECASE),
    re.compile(r"\bhit_limit\b", re.IGNORECASE),
    # Completeness of an artifact produced by the run.
    re.compile(r"\bcomplete\s+artifact\b", re.IGNORECASE),
    re.compile(r"\bproduces?\s+(?:a\s+)?complete\b", re.IGNORECASE),
    re.compile(r"\bartifact\s+(?:is|was)\s+complete\b", re.IGNORECASE),
    # Dollar cost and wall-clock duration — only observable by executing.
    re.compile(r"\$\s*\d"),
    re.compile(r"\b\d+\s*(?:minutes?|mins?|seconds?|secs?|hours?)\b", re.IGNORECASE),
    re.compile(r"\bwall[-\s]?clock\b", re.IGNORECASE),
    re.compile(r"\b(?:run|agent|it)\s+(?:costs?|spent|spends?)\b", re.IGNORECASE),
)


def _truncate_criterion(text: str, max_len: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1].rstrip() + "…"


def _criterion_needs_live_evidence(bullet: str) -> bool:
    """True when a single AC bullet asserts the recorded outcome of a live run."""
    if not any(p.search(bullet) for p in _LIVE_RUN_SCOPE_RES):
        return False
    return any(p.search(bullet) for p in _LIVE_RUN_OUTCOME_RES)


def check_criterion_needs_live_evidence(
    title: str, body: str, labels: Iterable[str]
) -> Reason | None:
    """Flag acceptance criteria satisfiable only by a live run's recorded outcome.

    The dev loop produces source changes, deterministic tests, and a gate exit
    code; the reviewer sees only the resulting diff. A criterion whose
    satisfaction depends on the cost, duration, budget consumption, or
    produced-artifact completeness of an actual run cannot be met by any diff —
    it is knowable from the story text at intake, so refusing here spends no dev
    budget to learn what iteration exhaustion would otherwise teach expensively.

    Bug-format issues are exempt (they use observed/expected, not acceptance
    criteria, and their Diagnosis section legitimately references runs and
    evidence) — exempt by derivation from the type's specification, which does
    not ask a bug for acceptance criteria. The check fires only on the
    conjunction of a live-run scope and a recorded-outcome assertion within the
    *same* bullet, so a criterion that merely mentions runs, budgets, agents, or
    artifacts does not trip it.

    When some criteria are satisfiable and others are not, the detail names the
    flagged criteria and notes the satisfiable remainder, so a partly-dispatchable
    story stays distinguishable from a wholly-undispatchable one.
    """
    if _presence_of(body, labels, ACCEPTANCE_CRITERIA_SECTION.key) is not Presence.REQUIRED:
        return None
    ac = extract_ac_section(body)
    if not ac:
        return None
    # Use whole-bullet blocks (continuation prose included): a criterion often
    # states its live-run scope on the first line and its recorded-outcome
    # assertion on a wrapped continuation line, and both signals must be seen
    # together for the conjunction to fire.
    bullets = extract_top_level_bullet_blocks(ac)
    if not bullets:
        return None
    flagged = [b for b in bullets if _criterion_needs_live_evidence(b)]
    if not flagged:
        return None

    satisfiable = len(bullets) - len(flagged)
    flagged_render = "; ".join(f"'{_truncate_criterion(b)}'" for b in flagged)
    if satisfiable > 0:
        mix_note = (
            f" The other {satisfiable} criterion(s) are satisfiable by ordinary source "
            "and tests and remain dispatchable once the live-run criterion(s) are split "
            "out."
        )
    else:
        mix_note = (
            " Every acceptance criterion here depends on a live-run outcome; the issue "
            "is wholly undispatchable to the dev loop as written."
        )
    return Reason(
        code="criterion_needs_live_evidence",
        severity=Severity.BLOCKING,
        detail=(
            f"{len(flagged)} of {len(bullets)} acceptance criterion(s) depend on the "
            "recorded outcome of a live run (cost, duration, budget consumption, or the "
            "completeness of an agent-produced artifact) rather than on inspectable "
            "source or deterministic tests. The dev loop emits only source changes, "
            "deterministic tests, and a gate exit code, and a reviewer sees only the "
            f"diff — so no diff can satisfy: {flagged_render}." + mix_note
        ),
    )


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-_]+")


def check_too_many_behavioral_clusters(
    title: str,
    body: str,
    labels: Iterable[str],
    *,
    threshold: int = DEFAULT_CLUSTER_THRESHOLD,
    vocabulary: Iterable[str] | None = None,
) -> Reason | None:
    ac = extract_ac_section(body)
    if not ac:
        return None
    blocks = extract_top_level_bullet_blocks(ac)
    if not blocks:
        return None
    vocab = {w.lower() for w in (vocabulary if vocabulary is not None else SEED_VOCABULARY)}
    text = "\n".join(blocks).lower()
    words = set(_WORD_RE.findall(text))
    # normalize simple plurals
    normalized = {w[:-1] if w.endswith("s") and w[:-1] in vocab else w for w in words}
    hits = normalized & vocab
    if len(hits) > threshold:
        return Reason(
            code="too_many_behavioral_clusters",
            severity=Severity.BLOCKING,
            detail=(
                f"AC bullets reference {len(hits)} distinct subsystem nouns "
                f"({sorted(hits)}) — exceeds threshold {threshold}."
            ),
        )
    return None
