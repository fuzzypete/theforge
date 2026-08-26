"""Parsing utilities for issue bodies.

Stdlib plus the declarative issue specification, which is itself pure data:
the heading vocabularies these parsers probe with are stated once, in
:mod:`theforge.shape_check.issue_spec`, and read from there rather than
restated here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from theforge.shape_check.issue_spec import (
    ACCEPTANCE_CRITERIA_SECTION,
    EXPECTED_SECTION,
    OBSERVED_SECTION,
    REPRODUCTION_SECTION,
    normalize_heading_text,
)

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
# Indentation is not restricted to CommonMark's 0-3 spaces: fences nested under
# a bullet are indented to the item's content column, and the bullet-block
# helpers below have always treated those as fences.
_FENCE_RE = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})(?P<info>.*?)\s*$")
_BLOCKQUOTE_LINE_RE = re.compile(r"^\s{0,3}(?:>\s?)+(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")
_EXAMPLE_HEADING_RE = re.compile(
    r"^(?:"
    r"examples?"
    r"|target(?:\s+(?:output|state|sketch))?"
    r"|what\s+it\s+should\s+look\s+like"
    r"|schema(?:\s+(?:example|output|sketch))?"
    r"|sample\s+output"
    r"|expected\s+output"
    r")$",
    re.IGNORECASE,
)

ACCEPTANCE_CRITERIA_HEADING_PATTERN = ACCEPTANCE_CRITERIA_SECTION.heading_pattern


@dataclass(frozen=True)
class ContextualBullet:
    text: str
    in_example_section: bool


@dataclass(frozen=True)
class ContextualFencedBlock:
    content: str
    in_example_section: bool


class FenceTracker:
    """Line-by-line fenced-code state for a Markdown document.

    Both fence families are recognised (``` ``` ``` and ``~~~``). A fence closes
    only on a line using the *same* marker character, at least as long as the
    opener, with no info string — so a ``~~~`` line inside a backtick block is
    content, not a delimiter. An unclosed fence runs to the end of the document,
    as in CommonMark.

    Every fence-aware helper in this module shares this one implementation:
    the parsers must agree on what counts as literal content, or a body can be
    admitted by one check and refused by another for the same text.
    """

    __slots__ = ("_char", "_len")

    def __init__(self) -> None:
        self._char: str | None = None
        self._len = 0

    @property
    def in_fence(self) -> bool:
        """True when the *previous* fed line left the document inside a fence."""
        return self._char is not None

    def feed(self, line: str) -> str:
        """Consume ``line`` and return its role.

        One of ``"open"``, ``"close"`` (the delimiter lines themselves),
        ``"inside"`` (fenced content) or ``"outside"`` (ordinary document text).
        """
        m = _FENCE_RE.match(line)
        if self._char is None:
            if m:
                self._char = m.group("marker")[0]
                self._len = len(m.group("marker"))
                return "open"
            return "outside"
        if (
            m
            and m.group("marker")[0] == self._char
            and len(m.group("marker")) >= self._len
            and not m.group("info").strip()
        ):
            self._char = None
            self._len = 0
            return "close"
        return "inside"


def fenced_spans(body: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` character offsets of each fenced code region.

    A region runs from the start of its opening fence line through the end of
    its closing fence line. See :class:`FenceTracker` for the delimiter rules.
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    open_start = 0
    tracker = FenceTracker()
    for line in body.splitlines(keepends=True):
        role = tracker.feed(line)
        if role == "open":
            open_start = offset
        elif role == "close":
            spans.append((open_start, offset + len(line)))
        offset += len(line)
    if tracker.in_fence:
        spans.append((open_start, len(body)))
    return spans


def blockquote_blocks(body: str) -> list[str]:
    """Return contiguous blockquoted regions with quote markers stripped.

    The returned blocks preserve the quoted Markdown content but remove the
    leading ``>`` scaffolding so downstream readers can inspect whether a
    structurally meaningful section exists only inside a quoted region.
    """
    blocks: list[str] = []
    current: list[str] = []
    in_blockquote = False
    for line in body.splitlines():
        m = _BLOCKQUOTE_LINE_RE.match(line)
        if m is None:
            if in_blockquote and not line.strip():
                current.append("")
                continue
            if current:
                blocks.append("\n".join(current))
                current = []
            in_blockquote = False
            continue
        current.append(m.group(1))
        in_blockquote = True
    if current:
        blocks.append("\n".join(current))
    return blocks


def iter_headings(body: str) -> list[re.Match[str]]:
    """Return every heading match in ``body`` that is not inside a fenced block.

    Headings quoted inside fenced code are content the author is exhibiting,
    not structure of the document itself, so they are excluded.
    """
    spans = fenced_spans(body)
    return [
        m
        for m in _HEADING_RE.finditer(body)
        if not any(start <= m.start() < end for start, end in spans)
    ]


def find_heading(body: str, pattern: str) -> re.Match[str] | None:
    """Return a re.Match for the first heading whose text matches ``pattern``.

    Headings inside fenced code blocks are ignored. The match's ``start()`` is
    the start of the heading line.
    """
    regex = re.compile(pattern, re.IGNORECASE)
    for m in iter_headings(body):
        if regex.search(m.group(2)):
            return m
    return None


def has_heading(body: str, pattern: str) -> bool:
    return find_heading(body, pattern) is not None


def _span_from_heading_match(body: str, m: re.Match[str]) -> tuple[int, int]:
    """Return the ``(start, end)`` content span owned by heading match ``m``.

    ``start`` is the offset just past the end of the heading line; ``end`` is
    the offset of the next unfenced heading of same-or-higher level, or
    ``len(body)``.
    """
    level = len(m.group(1))
    start = m.end()
    for nm in iter_headings(body):
        if nm.start() >= start and len(nm.group(1)) <= level:
            return start, nm.start()
    return start, len(body)


def section_span(body: str, heading_pattern: str) -> tuple[int, int] | None:
    """Return ``(start, end)`` offsets of a matching heading's section content.

    ``start`` is the offset just past the end of the heading line; ``end`` is the
    offset of the next heading of same-or-higher level, or ``len(body)``.
    Headings inside fenced code blocks bound nothing — a fenced sample of a
    document's shape is content, so the section runs past it. Returns ``None``
    when no heading matches. Producers that need to insert text into an existing
    section use this rather than re-deriving heading arithmetic.
    """
    m = find_heading(body, heading_pattern)
    if not m:
        return None
    return _span_from_heading_match(body, m)


def extract_section(body: str, heading_pattern: str) -> str | None:
    """Return text from a matching heading up to the next heading of same-or-higher level."""
    span = section_span(body, heading_pattern)
    if span is None:
        return None
    return body[span[0] : span[1]]


def extract_ac_section(body: str) -> str | None:
    return extract_section(body, ACCEPTANCE_CRITERIA_HEADING_PATTERN)


#: Heading-text normalization lives with the specification, because the
#: renderer's notion of "this heading is that section" and the gate's must be
#: the same one. Re-exported under the private name this module has always used.
_normalize_heading_text = normalize_heading_text


def find_authoritative_heading(
    body: str,
    pattern: str,
    canonical_texts: tuple[str, ...] = (),
    score_fn: Callable[[str], int] | None = None,
) -> re.Match[str] | None:
    """Return the heading that authoritatively represents a named section.

    A body can carry more than one heading matching ``pattern`` — an earlier
    intake step's placeholder, ordinary prose that merely mentions the topic,
    or a genuine landed artifact. Unlike :func:`find_heading` (first match in
    document order), this prefers a *canonical* heading — one whose text,
    normalized, exactly equals one of ``canonical_texts`` — over a heading
    that only contains the word as part of a longer title.

    When more than one canonical heading exists, ``score_fn`` (given the
    matched section's text) ranks them; the highest-scoring section wins.
    Document position must not decide authority on its own — a stale
    placeholder can be reordered, appended around, or simply be longer than
    a genuine artifact, so raw position or length are not authority by
    themselves. Callers that care about a domain-specific notion of
    "complete" (e.g. a diagnosis with a confirmed cause) should pass a
    ``score_fn`` that measures that. Defaults to section content length when
    omitted. Ties fall to the later heading.

    Falls back to first-match-in-document-order (the same behavior as
    :func:`find_heading`) when ``canonical_texts`` is empty or no heading
    matches it.
    """
    regex = re.compile(pattern, re.IGNORECASE)
    matches = [m for m in iter_headings(body) if regex.search(m.group(2))]
    if not matches:
        return None
    if canonical_texts:
        canonical = [m for m in matches if _normalize_heading_text(m.group(2)) in canonical_texts]
        if canonical:
            scorer = score_fn if score_fn is not None else len
            best = canonical[0]
            best_score = -1
            for m in canonical:
                start, end = _span_from_heading_match(body, m)
                score = scorer(body[start:end])
                if score >= best_score:
                    best = m
                    best_score = score
            return best
    return matches[0]


def authoritative_section_span(
    body: str,
    pattern: str,
    canonical_texts: tuple[str, ...] = (),
    score_fn: Callable[[str], int] | None = None,
) -> tuple[int, int] | None:
    m = find_authoritative_heading(body, pattern, canonical_texts, score_fn)
    if not m:
        return None
    return _span_from_heading_match(body, m)


def extract_authoritative_section(
    body: str,
    pattern: str,
    canonical_texts: tuple[str, ...] = (),
    score_fn: Callable[[str], int] | None = None,
) -> str | None:
    """Like :func:`extract_section`, but selects the authoritative heading.

    See :func:`find_authoritative_heading` for the selection rule.
    """
    span = authoritative_section_span(body, pattern, canonical_texts, score_fn)
    if span is None:
        return None
    return body[span[0] : span[1]]


# The bug-report shape is a symptom heading paired with an expectation heading.
# Both the canonical spelling and the legacy one are recognised, in exactly one
# place — the specification — because intake decides "is this a bug body?" in
# more than one gate and a body admitted by one gate must not be refused by
# another (#2139, ADR-0003).
_BUG_SYMPTOM_HEADING = OBSERVED_SECTION.heading_pattern
_BUG_EXPECTATION_HEADING = EXPECTED_SECTION.heading_pattern
_BUG_REPRODUCTION_HEADING = REPRODUCTION_SECTION.heading_pattern

# Public aliases for other readers that need to recognize the same bug-body
# sections. The accepted heading spellings must stay in one place.
BUG_SYMPTOM_HEADING = _BUG_SYMPTOM_HEADING
BUG_EXPECTATION_HEADING = _BUG_EXPECTATION_HEADING
BUG_REPRODUCTION_HEADING = _BUG_REPRODUCTION_HEADING


def has_bug_body_headings(body: str) -> bool:
    """True when ``body`` carries the heading structure of a bug report.

    Either a symptom/expectation heading pair (in any of its accepted
    spellings), or a reproduction-steps heading, which only a bug report has.
    """
    if has_heading(body, _BUG_REPRODUCTION_HEADING):
        return True
    return has_heading(body, _BUG_SYMPTOM_HEADING) and has_heading(body, _BUG_EXPECTATION_HEADING)


def is_example_heading(title: str) -> bool:
    normalized = re.sub(r"\s+", " ", title.strip().rstrip(":")).lower()
    return _EXAMPLE_HEADING_RE.fullmatch(normalized) is not None


def _bullet_indent(line: str) -> int | None:
    m = _BULLET_RE.match(line)
    if not m:
        return None
    return len(line) - len(line.lstrip(" "))


def extract_bullets(section: str) -> list[str]:
    """All bullets (any nesting level), first-line text only."""
    bullets: list[str] = []
    for line in section.splitlines():
        m = _BULLET_RE.match(line)
        if m:
            bullets.append(m.group(1).strip())
    return bullets


def extract_top_level_bullet_blocks(section: str) -> list[str]:
    """Top-level bullets only. Each entry is the full text block of the bullet.

    A top-level bullet starts at the smallest bullet indent found in the section.
    Continuation lines (including fenced code) are included, but nested bullets
    (indent greater than the top-level indent) are excluded.
    """
    lines = section.splitlines()
    indents = [ind for ind in (_bullet_indent(line) for line in lines) if ind is not None]
    if not indents:
        return []
    top_indent = min(indents)

    blocks: list[list[str]] = []
    current: list[str] | None = None
    tracker = FenceTracker()
    for line in lines:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if tracker.feed(line) != "outside":
            # fence delimiters and their contents are literal continuation text
            if current is not None:
                current.append(line)
            continue
        m = _BULLET_RE.match(line)
        if m and indent == top_indent:
            if current is not None:
                blocks.append(current)
            current = [m.group(1)]
        elif m and indent > top_indent:
            # nested bullet — skip entirely
            continue
        else:
            if current is not None and line.strip():
                # continuation line (prose under the bullet)
                current.append(stripped)
    if current is not None:
        blocks.append(current)
    return ["\n".join(b) for b in blocks]


def extract_contextual_bullets(section: str) -> list[ContextualBullet]:
    """Return bullets annotated with whether they appear under an example heading."""
    bullets: list[ContextualBullet] = []
    example_level: int | None = None
    tracker = FenceTracker()

    for line in section.splitlines():
        role = tracker.feed(line)
        if role in ("open", "close"):
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match and role == "outside":
            level = len(heading_match.group(1))
            if example_level is not None and level <= example_level:
                example_level = None
            if is_example_heading(heading_match.group(2)):
                example_level = level
            continue

        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            bullets.append(
                ContextualBullet(
                    text=bullet_match.group(1).strip(),
                    in_example_section=example_level is not None,
                )
            )

    return bullets


def fenced_code_blocks(body: str) -> list[str]:
    """Return the contents of each fenced code block (backtick or tilde)."""
    blocks: list[str] = []
    current: list[str] = []
    tracker = FenceTracker()
    for line in body.splitlines():
        role = tracker.feed(line)
        if role == "open":
            current = []
        elif role == "close":
            blocks.append("\n".join(current))
        elif role == "inside":
            current.append(line)
    if tracker.in_fence:
        blocks.append("\n".join(current))
    return blocks


def extract_contextual_fenced_code_blocks(body: str) -> list[ContextualFencedBlock]:
    """Return fenced blocks annotated with whether they appear under an example heading."""
    blocks: list[ContextualFencedBlock] = []
    example_level: int | None = None
    current: list[str] = []
    tracker = FenceTracker()

    for line in body.splitlines():
        role = tracker.feed(line)
        if role == "outside":
            heading_match = _HEADING_RE.match(line)
            if heading_match:
                level = len(heading_match.group(1))
                if example_level is not None and level <= example_level:
                    example_level = None
                if is_example_heading(heading_match.group(2)):
                    example_level = level
            continue

        if role == "open":
            current = []
        elif role == "close":
            blocks.append(
                ContextualFencedBlock(
                    content="\n".join(current),
                    in_example_section=example_level is not None,
                )
            )
        else:
            current.append(line)

    return blocks
