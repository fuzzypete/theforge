"""Parsing utilities for issue bodies. Stdlib only."""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")


def find_heading(body: str, pattern: str) -> re.Match[str] | None:
    """Return a re.Match for the first heading whose text matches ``pattern``.

    The match's ``start()`` is the start of the heading line.
    """
    regex = re.compile(pattern, re.IGNORECASE)
    for m in _HEADING_RE.finditer(body):
        if regex.search(m.group(2)):
            return m
    return None


def has_heading(body: str, pattern: str) -> bool:
    return find_heading(body, pattern) is not None


def extract_section(body: str, heading_pattern: str) -> str | None:
    """Return text from a matching heading up to the next heading of same-or-higher level."""
    m = find_heading(body, heading_pattern)
    if not m:
        return None
    level = len(m.group(1))
    start = m.end()
    remainder = body[start:]
    # find next heading of level <= current
    next_heading = None
    for nm in _HEADING_RE.finditer(remainder):
        if len(nm.group(1)) <= level:
            next_heading = nm
            break
    if next_heading:
        return remainder[: next_heading.start()]
    return remainder


def extract_ac_section(body: str) -> str | None:
    return extract_section(body, r"acceptance criteria|done criteria|checklist")


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
