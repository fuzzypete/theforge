"""Heuristic check for unsubstantiated fix claims in dev handoff summaries.

Detects sentences in the dev handoff summary that assert a prior finding has
been fixed but cite no test name or invariant backing the claim.  Returns a
structured result with flagged and accepted claims for injection into the
reviewer prompt and the audit trail.

Detection is entirely string-based — no LLM in the loop.

Scoping rules
-------------
- Only claims that appear to be *about* a prior P1 finding are evaluated.
  Generic fix-claim language with no keyword overlap to any known finding is
  silently ignored (not flagged, not accepted).
- Within a matched claim, the presence of a test-name reference (``test_*``,
  ``*Test``, ``*Tests``) or an invariant phrase (e.g. "by construction",
  "invariant") counts as backing evidence → accepted, not flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Fix-claim phrases
# ---------------------------------------------------------------------------
# Patterns that signal a confident fix assertion in prose.
_FIX_CLAIM_PATTERNS: list[re.Pattern[str]] = [
    # "ensures X never/always/no longer/cannot ..."
    re.compile(r"\bensures?\b.{0,60}\b(never|always|no longer|can(?:not|'t))\b", re.I | re.S),
    # "guarantees X"
    re.compile(r"\bguarantees?\b", re.I),
    # "fixes / fixed / resolves / resolved / addresses / addressed"
    re.compile(r"\b(?:fix(?:es|ed)?|resolv(?:es|ed)|address(?:es|ed))\b", re.I),
    # "no longer <verb>"
    re.compile(r"\bno longer\b", re.I),
    # "can never / cannot / can't ... happen / occur / be skipped / be missed"
    re.compile(
        r"\b(?:can(?:not|'t)|will never|won't)\b.{0,40}\b"
        r"(?:happen|occur|be skipped|be missed|recur|fail)\b",
        re.I | re.S,
    ),
    # "prevents X from Y"
    re.compile(r"\bprevents?\b", re.I),
    # "is now always / never / safe / correct / properly handled"
    re.compile(
        r"\bis now\b.{0,30}\b(?:always|never|safe|correct|properly|correctly|handled)\b",
        re.I | re.S,
    ),
]

# ---------------------------------------------------------------------------
# Backing-evidence patterns
# ---------------------------------------------------------------------------
# A claim is *backed* when it cites a concrete test or invariant argument.

# Test-name references: test_*, *Test, *Tests, tests/test_*
_TEST_REF_PATTERN = re.compile(
    r"\btest_\w+|\w+Tests?\b|tests[\\/]test_\w+",
    re.I,
)

# Explicit invariant / proof phrases
_INVARIANT_PHRASES: tuple[str, ...] = (
    "invariant",
    "by construction",
    "by design",
    "guaranteed by",
    "proven by",
    "always holds",
    "structurally impossible",
    "structurally enforced",
    "by invariant",
    "type system",
    "enforced by",
)

# ---------------------------------------------------------------------------
# Stop-words for token-overlap scoping
# ---------------------------------------------------------------------------
_STOP: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "it",
        "in",
        "of",
        "to",
        "and",
        "or",
        "not",
        "for",
        "with",
        "was",
        "has",
        "are",
        "be",
        "by",
        "this",
        "that",
        "which",
        "when",
        "now",
        "so",
        "if",
        "on",
        "at",
        "as",
        "from",
        "but",
        "its",
        "no",
        "via",
        "was",
        "were",
    }
)

_MIN_OVERLAP_TOKENS = 2


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixClaimResult:
    """Result for a single fix-claim sentence."""

    claim_text: str
    """The sentence that contains the fix claim."""

    related_finding: str
    """Prior P1 description that this claim appears to address (200-char cap)."""

    has_backing: bool
    """True when the claim cites a test name or invariant statement."""

    flag_message: str | None
    """Reviewer-facing warning string, or None when the claim is accepted."""


@dataclass(frozen=True)
class HandoffClaimCheckResult:
    """Aggregated result of scanning all fix claims in a handoff summary."""

    flagged: list[FixClaimResult] = field(default_factory=list)
    """Claims that lack backing evidence (reviewer should verify)."""

    accepted: list[FixClaimResult] = field(default_factory=list)
    """Claims that cite a test name or invariant (accepted without warning)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    """Return lowercase word tokens, minus stop-words."""
    raw = set(re.findall(r"\b[a-z_]\w*\b", text.casefold()))
    return raw - _STOP


def _overlaps_with_finding(sentence: str, finding_desc: str) -> bool:
    """Return True when *sentence* shares ≥ MIN_OVERLAP meaningful tokens with *finding_desc*."""
    return len(_tokens(sentence) & _tokens(finding_desc)) >= _MIN_OVERLAP_TOKENS


def _is_fix_claim(sentence: str) -> bool:
    """Return True when *sentence* contains at least one fix-claim phrase."""
    return any(p.search(sentence) for p in _FIX_CLAIM_PATTERNS)


def _has_backing_evidence(sentence: str) -> bool:
    """Return True when *sentence* cites a test name or invariant phrase."""
    if _TEST_REF_PATTERN.search(sentence):
        return True
    lower = sentence.casefold()
    return any(phrase in lower for phrase in _INVARIANT_PHRASES)


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences on common end-of-sentence punctuation or newlines."""
    # Split on sentence-ending punctuation followed by whitespace, or on newlines.
    raw = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [s.strip() for s in raw if s.strip()]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_handoff_fix_claims(
    summary: str,
    prior_p1_descriptions: list[str],
) -> HandoffClaimCheckResult:
    """Scan *summary* for fix claims about prior P1 findings.

    Only claims whose text overlaps meaningfully with a known prior P1 finding
    description are evaluated.  Claims that reference no known finding are
    silently skipped — the check is scoped, not global.

    A claim is *accepted* when it cites a test name (``test_*``, ``*Test``) or
    an explicit invariant phrase.  Otherwise it is *flagged*.

    Args:
        summary: Raw text of the dev handoff summary field.
        prior_p1_descriptions: P1 finding description strings from prior
            review cycles (already truncated to ≤200 chars by CycleHistory).

    Returns:
        HandoffClaimCheckResult with separate ``flagged`` and ``accepted`` lists.
    """
    if not summary or not prior_p1_descriptions:
        return HandoffClaimCheckResult()

    sentences = _split_sentences(summary)
    flagged: list[FixClaimResult] = []
    accepted: list[FixClaimResult] = []

    for sentence in sentences:
        if not _is_fix_claim(sentence):
            continue

        # Find the first prior P1 finding this sentence appears to address.
        related: str | None = None
        for desc in prior_p1_descriptions:
            if _overlaps_with_finding(sentence, desc):
                related = desc
                break

        if related is None:
            # Claim doesn't map to any known prior finding — skip.
            continue

        has_backing = _has_backing_evidence(sentence)

        if has_backing:
            accepted.append(
                FixClaimResult(
                    claim_text=sentence,
                    related_finding=related,
                    has_backing=True,
                    flag_message=None,
                )
            )
        else:
            flag_msg = (
                "Dev claimed this finding fixed but cited no test or invariant"
                " — verify independently.\n"
                f'  Claim: "{sentence}"\n'
                f'  Related finding: "{related}"'
            )
            flagged.append(
                FixClaimResult(
                    claim_text=sentence,
                    related_finding=related,
                    has_backing=False,
                    flag_message=flag_msg,
                )
            )

    return HandoffClaimCheckResult(flagged=flagged, accepted=accepted)
