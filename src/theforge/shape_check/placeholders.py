"""Placeholder-content detection. Stdlib only.

The shape gate tests *structure* as a proxy for *substance*: a section that
exists, is long enough, and contains a fence/list/table is accepted as a
concrete example. That proxy holds for hand-written bodies and breaks for
machine-written ones — a producer can emit text shaped like the missing
thing and satisfy a check nothing actually resolved.

This module is the shared contract that closes the gap. Producers mark
content they could not supply with :data:`PLACEHOLDER_MARKER`; the
heuristics strip marked lines before measuring substance, so a
placeholder-only section keeps its finding instead of erasing it. The
generic markers (``TBD``, ``FIXME``, ``XXX``, bare ``TODO``) are recognised
too, so a hand-copied stub is caught the same way.
"""

from __future__ import annotations

import re

from theforge.shape_check.parsing import FenceTracker

#: Marker a producer writes when it cannot supply the content a section
#: needs. Written by ``forge groom``; recognised by the heuristics below.
PLACEHOLDER_MARKER = "TODO(forge-groom)"

# Leading bullet / checkbox / blockquote / comment scaffolding that may
# precede the marker word on a placeholder line.
_PLACEHOLDER_LINE_RE = re.compile(
    r"^\s*(?:>\s*)*(?:(?:[-*+]|\d+[.)])\s+)?(?:\[[ xX]\]\s+)?(?:<!--\s*)?"
    r"(?:TODO|TBD|FIXME|XXX)\b",
    re.IGNORECASE,
)


# A placeholder's instructions may wrap onto following lines. Anything that
# opens new structure (blank line, bullet, heading, fence, table row) ends
# the wrap.
_NEW_BLOCK_RE = re.compile(r"^\s*(?:$|(?:[-*+]|\d+[.)])\s|#{1,6}\s|`{3,}|~{3,}|\|)")

# Only a line that is visibly mid-sentence pulls the next line in with it.
# A self-contained "TODO: ..." followed by real sample output must not
# swallow that output — the wrap rule has to be narrower than "next line".
_WRAPS_RE = re.compile(r"[,;:—–\-(\[]$")


def is_placeholder_line(line: str) -> bool:
    """True when ``line`` is an unfilled-template marker rather than content."""
    return _PLACEHOLDER_LINE_RE.match(line) is not None


def _wraps_onto_next_line(line: str) -> bool:
    return _WRAPS_RE.search(line.rstrip()) is not None


def _is_continuation(line: str) -> bool:
    """True when ``line`` continues the preceding placeholder's prose."""
    return _NEW_BLOCK_RE.match(line) is None


def has_placeholder_marker(text: str) -> bool:
    """True when ``text`` contains the producer-written placeholder marker."""
    return PLACEHOLDER_MARKER.lower() in text.lower()


def strip_placeholder_content(text: str) -> str:
    """Return ``text`` with placeholder lines removed.

    Fenced blocks left with no substantive content are dropped whole, so a
    fence wrapping nothing but a TODO does not read as structured content
    to :mod:`theforge.shape_check.heuristics`.
    """
    if not text:
        return text

    out: list[str] = []
    block: list[str] | None = None  # buffered fenced block, incl. delimiters
    block_has_content = False
    dropping = False  # inside a placeholder's wrapped prose
    tracker = FenceTracker()

    for line in text.splitlines():
        role = tracker.feed(line)
        if role == "open":
            block = [line]
            block_has_content = False
            dropping = False
            continue
        if role == "inside":
            if block is None:  # defensive: fence state without an opener
                out.append(line)
                continue
            if is_placeholder_line(line):
                dropping = _wraps_onto_next_line(line)
                continue
            if dropping and _is_continuation(line):
                dropping = _wraps_onto_next_line(line)
                continue
            dropping = False
            block.append(line)
            if line.strip():
                block_has_content = True
            continue
        if role == "close":
            dropping = False
            if block is not None:
                if block_has_content:
                    block.append(line)
                    out.extend(block)
                block = None
                block_has_content = False
            continue
        if is_placeholder_line(line):
            dropping = _wraps_onto_next_line(line)
            continue
        if dropping and _is_continuation(line):
            dropping = _wraps_onto_next_line(line)
            continue
        dropping = False
        out.append(line)

    # Unclosed fence at end of text: keep it only if it held real content.
    if block is not None and block_has_content:
        out.extend(block)

    return "\n".join(out)


def is_placeholder_only(text: str) -> bool:
    """True when nothing but placeholder scaffolding remains in ``text``."""
    if not text.strip():
        return False
    return not strip_placeholder_content(text).strip()
