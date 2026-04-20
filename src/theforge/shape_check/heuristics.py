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

_TERMINAL_TRIAGE_LABELS: frozenset[str] = frozenset(
    {
        "triage-accepted",
        "triage-rejected",
        "triage-dup",
        "triage-closed",
        "triage-fix-now",
        "triage-fix-soon",
        "triage-punt",
    }
)

_TRACKING_PHRASES = (
    "tracking issue",
    "tracking only",
    "planning issue",
    "umbrella",
    "meta issue",
    "meta-issue",
    "parent issue",
)

_BUG_LABELS: frozenset[str] = frozenset({"bug"})

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


def is_bug_format_issue(body: str, labels: Iterable[str]) -> bool:
    """Return true for bug reports, which use observed/expected sections instead of AC."""
    if _lower_labels(labels) & _BUG_LABELS:
        return True
    return has_heading(body, r"what happened") and has_heading(body, r"what was expected")


def check_epic_or_tracking(title: str, body: str, labels: Iterable[str]) -> Reason | None:
    lset = _lower_labels(labels)
    title_l = title.strip().lower()
    body_l = body.lower()
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
    for phrase in _TRACKING_PHRASES:
        if phrase in body_l:
            return Reason(
                code="epic_or_tracking",
                severity=Severity.BLOCKING,
                detail=f"Body declares tracking intent ('{phrase}').",
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
    if lset & _TERMINAL_TRIAGE_LABELS:
        return None
    return Reason(
        code="untriaged_finding",
        severity=Severity.BLOCKING,
        detail=(
            "forge-finding has no terminal triage label "
            "(e.g. triage-accepted/rejected/dup/closed)."
        ),
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
