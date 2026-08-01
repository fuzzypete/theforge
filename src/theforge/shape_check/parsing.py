"""Parsing utilities for issue bodies. Stdlib only."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,})(.*?)\s*$")
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


@dataclass(frozen=True)
class ContextualBullet:
    text: str
    in_example_section: bool


@dataclass(frozen=True)
class ContextualFencedBlock:
    content: str
    in_example_section: bool


def fenced_spans(body: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` character offsets of each fenced code region.

    A region runs from the start of its opening fence line through the end of
    its closing fence line. A fence closes on a line with at least as many
    backticks as the opener and no info string; an unclosed fence runs to the
    end of the document, as in CommonMark.
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    open_start: int | None = None
    open_ticks = 0
    for line in body.splitlines(keepends=True):
        m = _FENCE_RE.match(line)
        if m:
            ticks = len(m.group(1))
            if open_start is None:
                open_start = offset
                open_ticks = ticks
            elif ticks >= open_ticks and not m.group(2).strip():
                spans.append((open_start, offset + len(line)))
                open_start = None
        offset += len(line)
    if open_start is not None:
        spans.append((open_start, len(body)))
    return spans


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
    level = len(m.group(1))
    start = m.end()
    # find next unfenced heading of level <= current
    for nm in iter_headings(body):
        if nm.start() >= start and len(nm.group(1)) <= level:
            return start, nm.start()
    return start, len(body)


def extract_section(body: str, heading_pattern: str) -> str | None:
    """Return text from a matching heading up to the next heading of same-or-higher level."""
    span = section_span(body, heading_pattern)
    if span is None:
        return None
    return body[span[0] : span[1]]


def extract_ac_section(body: str) -> str | None:
    return extract_section(body, r"acceptance criteria|done criteria|checklist")


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
    in_fence = False
    for line in lines:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if stripped.startswith("```"):
            if current is not None:
                current.append(line)
            in_fence = not in_fence
            continue
        if in_fence:
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
    in_fence = False

    for line in section.splitlines():
        stripped = line.strip()
        heading_match = _HEADING_RE.match(line)
        if heading_match and not in_fence:
            level = len(heading_match.group(1))
            if example_level is not None and level <= example_level:
                example_level = None
            if is_example_heading(heading_match.group(2)):
                example_level = level
            continue

        if stripped.startswith("```"):
            in_fence = not in_fence
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
    """Return the contents of each triple-backtick fenced code block."""
    blocks: list[str] = []
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^\s*```(\w*)\s*$", line)
        if fence:
            i += 1
            buf: list[str] = []
            while i < len(lines) and not re.match(r"^\s*```\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            blocks.append("\n".join(buf))
        i += 1
    return blocks


def extract_contextual_fenced_code_blocks(body: str) -> list[ContextualFencedBlock]:
    """Return fenced blocks annotated with whether they appear under an example heading."""
    blocks: list[ContextualFencedBlock] = []
    example_level: int | None = None
    in_fence = False
    current: list[str] = []

    for line in body.splitlines():
        if not in_fence:
            heading_match = _HEADING_RE.match(line)
            if heading_match:
                level = len(heading_match.group(1))
                if example_level is not None and level <= example_level:
                    example_level = None
                if is_example_heading(heading_match.group(2)):
                    example_level = level
                continue

        if re.match(r"^\s*```", line):
            if in_fence:
                blocks.append(
                    ContextualFencedBlock(
                        content="\n".join(current),
                        in_example_section=example_level is not None,
                    )
                )
                current = []
                in_fence = False
            else:
                in_fence = True
            continue

        if in_fence:
            current.append(line)

    return blocks
