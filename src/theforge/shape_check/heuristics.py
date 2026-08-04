"""Deterministic heuristic checks — one function per reason code.

Each check returns ``Optional[Reason]`` given already-parsed inputs.
Stdlib only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from theforge.shape_check.diagnosis_spec import (
    BUG_SHAPE_REFERENCE_PATH,
    describe_missing,
    required_diagnosis_tokens,
)
from theforge.shape_check.parsing import (
    FenceTracker,
    extract_ac_section,
    extract_bullets,
    extract_section,
    extract_top_level_bullet_blocks,
    fenced_code_blocks,
    has_bug_body_headings,
    has_heading,
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
_RECOGNIZED_TYPE_LABELS: frozenset[str] = frozenset({"bug", "enhancement", "epic", "task"})
_EXAMPLE_SECTION_PATTERNS = (
    r"example(?:s)?",
    r"what it should look like",
    r"target(?: sketch| output| state)?",
)
_EXAMPLE_MIN_CONTENT_CHARS = 30

_OBSERVABLE_VERBS = (
    "returns",
    "return",
    "emits",
    "emit",
    "writes",
    "write",
    "fails",
    "fail",
    "passes",
    "pass",
    "warns",
    "warn",
    "exits",
    "exit",
    "logs",
    "log",
    "creates",
    "create",
    "raises",
    "raise",
    "produces",
    "produce",
    "reports",
    "report",
    "blocks",
    "block",
    "accepts",
    "accept",
    "rejects",
    "reject",
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


DIAGNOSIS_HEADING_PATTERN = r"diagnosis"

# Derived from the single declarative spec (diagnosis_spec.py) — this module
# must not carry its own independent list of required components. Kept as a
# module-level name for backward-compatible imports; the spec is the source of
# truth (#1629).
REQUIRED_DIAGNOSIS_TOKENS: tuple[str, ...] = required_diagnosis_tokens()

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


def _extract_confirmed_cause_value(section: str) -> str | None:
    """Return the prose value following the 'confirmed cause' label, if present.

    Operators write the field in several common shapes::

        - **Confirmed cause.** the cause is X
        - **Confirmed cause:** unknown
        - Confirmed cause: not yet identified
        - Confirmed cause — pending investigation

    First-party producers — notably ``forge diagnose`` via
    :func:`theforge.diagnose_types.render_artifact_markdown` — write the same
    field as a heading with the value on a following line::

        ### Confirmed cause

        unknown

    Both shapes must yield the same value: a value readable in one form and
    invisible in the other let a diagnose-landed artifact with no confirmed
    cause derive as implementation-ready (#2060).

    Returns the trimmed value, ``""`` when the label carries no value, or
    ``None`` when no labelled occurrence exists. The match is case-insensitive
    on the label only.
    """
    lines = section.splitlines()
    for idx, line in enumerate(lines):
        # Strip leading whitespace, heading markers, bullet markers, and
        # opening bold markers.
        cleaned = re.sub(r"^\s*#+\s*", "", line)
        cleaned = re.sub(r"^\s*(?:[-*+]\s+)?", "", cleaned)
        cleaned = re.sub(r"^\*+\s*", "", cleaned)
        m = re.match(r"confirmed\s+cause\b", cleaned, re.IGNORECASE)
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


def _is_non_assertion_cause(value: str) -> bool:
    if not value:
        return False
    return any(pattern.match(value) for pattern in _NON_ASSERTION_CAUSE_PATTERNS)


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
    section = extract_section(body, DIAGNOSIS_HEADING_PATTERN)
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
    section = extract_section(body, DIAGNOSIS_HEADING_PATTERN)
    if section is None:
        return False, ["missing Diagnosis section"]
    section_lower = section.lower()
    missing = [tok for tok in REQUIRED_DIAGNOSIS_TOKENS if tok not in section_lower]
    if missing:
        return False, missing
    return True, []


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
    - Any other recognized type (enhancement, task, epic) →
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
    """
    section = extract_section(body, DIAGNOSIS_HEADING_PATTERN)
    if section is None:
        return False
    if _UNRESOLVED_CAUSE_RE.search(section):
        return True
    value = _extract_confirmed_cause_value(section)
    return value is not None and not value


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
    """
    if not is_bug_format_issue(body, labels):
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
                "only symptom-only bodies are refused. "
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
            f"{describe_missing(missing)}. "
            f"Full shape reference: {BUG_SHAPE_REFERENCE_PATH}"
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
    if is_bug_format_issue(body, labels):
        return None
    placeholder_only = False
    if has_heading(body, r"acceptance criteria|done criteria|checklist"):
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
    if is_bug_format_issue(body, labels):
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
    to name the affected code path. Stories that legitimately describe a
    refactor of one named module can opt out by adding a body line of the form
    ``Implementation target: <path>``.
    """
    if is_bug_format_issue(body, labels):
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
    if is_bug_format_issue(body, labels):
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
    bullets = extract_bullets(ac)
    if not bullets:
        return Reason(
            code="no_observable_done_state",
            severity=Severity.BLOCKING,
            detail="AC section has no bullets — no observable done state.",
        )
    verb_re = re.compile(r"\b(" + "|".join(_OBSERVABLE_VERBS) + r")\b", re.IGNORECASE)
    if any(verb_re.search(b) for b in bullets):
        return None
    return Reason(
        code="no_observable_done_state",
        severity=Severity.ADVISORY,
        detail="No AC bullet contains a verifiable behavioral verb (returns/emits/writes/...).",
    )


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
    evidence). The check fires only on the conjunction of a live-run scope and a
    recorded-outcome assertion within the *same* bullet, so a criterion that
    merely mentions runs, budgets, agents, or artifacts does not trip it.

    When some criteria are satisfiable and others are not, the detail names the
    flagged criteria and notes the satisfiable remainder, so a partly-dispatchable
    story stays distinguishable from a wholly-undispatchable one.
    """
    if is_bug_format_issue(body, labels):
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
