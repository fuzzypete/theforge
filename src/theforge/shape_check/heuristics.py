"""Deterministic heuristic checks — one function per reason code.

Each check returns ``Optional[Reason]`` given already-parsed inputs.
Stdlib only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from theforge.shape_check.parsing import (
    extract_ac_section,
    extract_bullets,
    extract_section,
    extract_top_level_bullet_blocks,
    fenced_code_blocks,
    has_heading,
)
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
REQUIRED_DIAGNOSIS_TOKENS: tuple[str, ...] = (
    "observed symptom",
    "evidence",
    "ruled out",
    "confirmed cause",
    "affected code path",
    "fix-success criterion",
)

# Status labels operators can apply to a bug to communicate fix-readiness intent.
# These are advisory — the body's Diagnosis section is the authoritative signal.
RECOGNIZED_STATUS_LABELS: frozenset[str] = frozenset(
    {"status:triage", "status:investigating", "status:diagnosed"}
)
FIX_READY_STATUS_LABELS: frozenset[str] = frozenset({"status:diagnosed"})


def diagnosis_completeness(body: str) -> tuple[bool, list[str]]:
    """Return (is_complete, missing_tokens) for a bug's Diagnosis section.

    Returns ``(False, ["missing Diagnosis section"])`` when the section is
    absent. Returns ``(False, [...])`` when the section is present but lacks
    one or more required tokens. Returns ``(True, [])`` only when every
    required token appears within the section text (case-insensitive).
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
) -> tuple[bool | None, list[str]]:
    """Compute the binary fix-readiness signal from structured type and body.

    Rules:
    - ``type=None`` → ``(None, [warning])`` (cannot determine).
    - ``type='bug'`` → ``True`` iff the body contains a complete Diagnosis section
      with every required component; otherwise ``False`` with explanatory warnings.
    - Any other recognized type (enhancement, task, epic) → always fix-ready
      since features/tasks are described by acceptance criteria, not diagnosis.

    Status labels (e.g. ``status:diagnosed``) are operator intent and do not
    override body content per AC: absence of the Diagnosis section flips a
    bug to not-fix-ready regardless of label.
    """
    if story_type is None:
        return None, ["fix-readiness undetermined: story type unknown"]
    if story_type == "bug":
        ok, missing = diagnosis_completeness(body)
        if ok:
            return True, []
        if missing == ["missing Diagnosis section"]:
            return False, [
                "bug missing Diagnosis section — not fix-ready (run "
                "`forge diagnose` or add diagnosis manually)"
            ]
        return False, [
            f"bug Diagnosis section missing required component: {tok}" for tok in missing
        ]
    return True, []


def is_bug_format_issue(body: str, labels: Iterable[str]) -> bool:
    """Return true for bug reports, which use observed/expected sections instead of AC."""
    if _lower_labels(labels) & _BUG_LABELS:
        return True
    return has_heading(body, r"what happened") and has_heading(body, r"what was expected")


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


def check_bug_missing_diagnosis(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    """Block bug-typed issues that lack a complete Diagnosis section.

    A symptom-only bug entering the sprint flow is the failure mode this
    check exists to prevent: implementers hypothesize a cause, the PR
    fixes the hypothesis, reviewers verify the implementation matches the
    plan, the bug closes — and the original symptom persists because the
    cause was elsewhere. Without a diagnosis, there is no contract for
    reviewers to verify against.
    """
    if not is_bug_format_issue(body, labels):
        return None
    ok, missing = diagnosis_completeness(body)
    if ok:
        return None
    if missing == ["missing Diagnosis section"]:
        return Reason(
            code="bug_missing_diagnosis",
            severity=Severity.BLOCKING,
            detail=(
                "Bug has no Diagnosis section — not fix-ready. Add a '## Diagnosis' "
                "section containing observed symptom, evidence, ruled-out hypotheses, "
                "confirmed cause, affected code path, and fix-success criterion before "
                "sprinting (or run `forge diagnose` when available)."
            ),
        )
    return Reason(
        code="bug_missing_diagnosis",
        severity=Severity.BLOCKING,
        detail=(
            "Bug Diagnosis section is incomplete — missing required component(s): "
            f"{', '.join(missing)}."
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
    if has_heading(body, r"acceptance criteria|done criteria|checklist"):
        section = extract_ac_section(body) or ""
        if extract_bullets(section):
            return None
    return Reason(
        code="missing_acceptance_criteria",
        severity=Severity.BLOCKING,
        detail="No acceptance criteria section with a bullet/checklist found.",
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

    content_chars = len(re.sub(r"\s+", "", section))
    if content_chars < _EXAMPLE_MIN_CONTENT_CHARS or not _has_structured_example_content(section):
        return Reason(
            code="missing_example",
            severity=Severity.ADVISORY,
            detail=(
                "Example section is present but not substantive; include a concrete list, "
                "table, or fenced example."
            ),
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
        m = re.match(r"^\s*```(\w+)\s*$", line)
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
