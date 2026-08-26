"""The typed issue document: the parse and render halves of the contract.

An issue body is Markdown, but the Markdown is a *rendering* of a structure,
never the structure itself (ADR-0009 clause 1). This module holds that
structure — :class:`IssueDocument` — plus the two functions that move between
it and Markdown:

- :func:`parse_issue_document` — every body, canonical or legacy, parses into a
  typed document. A heading whose spelling is a declared alias of a modeled
  section resolves to that section, so the canonical and the legacy spelling of
  one section produce *the same* document.
- :func:`render_issue_document` — emits only the canonical spelling of a
  modeled section, and writes back everything the specification does not model
  byte-for-byte: preamble prose, unknown sections, worked examples, asides.

Together these give the round trip the contract requires::

    render_issue_document(parse_issue_document(canonical_body)) == canonical_body

That property is what makes a producer safe to run on a conforming document.
#2053 is the observed failure of it: ``forge shape --apply`` rewrote a
gate-passing bug body into one the gate refused. A renderer that does not round
trip is a destructive edit with a schema attached.

Fence-aware heading scanning is shared with the gate's parsers
(:mod:`theforge.shape_check.parsing`) — the parse half of the contract and the
validating half must not disagree about what counts as a heading.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from theforge.shape_check.issue_spec import (
    SECTIONS,
    IssueTypeSpec,
    SectionSpec,
    section_for_heading,
    spec_for_labels,
)
from theforge.shape_check.parsing import iter_headings

#: Level used for the synthetic block holding text above the first heading.
PREAMBLE_LEVEL = 0


def _heading_line_span(body: str, match) -> tuple[int, int]:
    """Return the exact ``(start, end)`` offsets of a heading's own line.

    The shared heading regex is multiline and brackets its text with ``\\s``
    classes, so a match can begin on the blank line above the heading and end
    several blank lines below it. Reconstructing the byte-exact document
    requires the heading line itself and nothing else, so the span is recomputed
    from the position of the ``#`` run.
    """
    hash_start = match.start(1)
    line_start = body.rfind("\n", 0, hash_start) + 1
    newline = body.find("\n", hash_start)
    line_end = len(body) if newline == -1 else newline
    return line_start, line_end


@dataclass(frozen=True)
class DocumentSection:
    """One heading-delimited block of an issue body.

    ``key`` is the modeled section this block is, or ``None`` when the
    specification does not model it. For a modeled block, ``heading`` is
    already the canonical spelling — which is why a canonical body and its
    legacy-spelled equivalent parse to equal documents — and ``raw_heading_line``
    is ``None``, because a modeled heading is re-rendered from the
    specification rather than replayed.

    For an unmodeled block, ``heading`` is the heading's text as written and
    ``raw_heading_line`` is the whole heading line verbatim, so rendering
    reproduces it exactly, indentation and trailing hashes included.

    ``body`` is the raw text between the heading line and the next heading of
    same-or-higher level, verbatim, leading newline included.
    """

    key: str | None
    heading: str
    level: int
    body: str
    raw_heading_line: str | None = None

    @property
    def is_preamble(self) -> bool:
        return self.level == PREAMBLE_LEVEL

    @property
    def spec(self) -> SectionSpec | None:
        return SECTIONS.get(self.key) if self.key else None

    def render(self) -> str:
        """Render this block back to Markdown."""
        if self.is_preamble:
            return self.body
        if self.key is not None:
            spec = SECTIONS[self.key]
            return spec.canonical_heading_line + self.body
        if self.raw_heading_line is not None:
            return self.raw_heading_line + self.body
        return f"{'#' * self.level} {self.heading}" + self.body


@dataclass(frozen=True)
class IssueDocument:
    """A parsed issue body: its declared type and its blocks, in order."""

    sections: tuple[DocumentSection, ...] = ()
    type_key: str | None = None

    def section(self, key: str) -> DocumentSection | None:
        """Return the first block modeled as ``key``, or ``None``."""
        for block in self.sections:
            if block.key == key:
                return block
        return None

    def has_section(self, key: str) -> bool:
        return self.section(key) is not None

    def modeled_keys(self) -> tuple[str, ...]:
        return tuple(b.key for b in self.sections if b.key is not None)

    def unmodeled_headings(self) -> tuple[str, ...]:
        return tuple(b.heading for b in self.sections if b.key is None and not b.is_preamble)


def parse_issue_document(
    body: str,
    *,
    labels=None,
    type_spec: IssueTypeSpec | None = None,
) -> IssueDocument:
    """Parse ``body`` into a typed document.

    ``labels`` (or an explicit ``type_spec``) supplies the declared type; when
    neither resolves to exactly one recognized type label the document carries
    ``type_key=None``. Section recognition itself is type-agnostic: a heading
    spelling means the same section whichever type declares it, which is
    ADR-0003's single-recognition principle applied to the parse half.
    """
    body = body or ""
    if type_spec is None and labels is not None:
        type_spec = spec_for_labels(labels)
    type_key = type_spec.key if type_spec is not None else None

    headings = iter_headings(body)
    spans = [_heading_line_span(body, m) for m in headings]
    if not headings:
        blocks = (
            (DocumentSection(key=None, heading="", level=PREAMBLE_LEVEL, body=body),)
            if body
            else ()
        )
        return IssueDocument(sections=blocks, type_key=type_key)

    blocks: list[DocumentSection] = []
    preamble = body[: spans[0][0]]
    if preamble:
        blocks.append(DocumentSection(key=None, heading="", level=PREAMBLE_LEVEL, body=preamble))

    for index, match in enumerate(headings):
        level = len(match.group(1))
        heading_text = match.group(2).strip()
        start, heading_end = spans[index]
        raw_heading_line = body[start:heading_end]
        end = spans[index + 1][0] if index + 1 < len(spans) else len(body)
        section_body = body[heading_end:end]

        spec = section_for_heading(heading_text)
        # Level must match too. ``### Root cause`` nested under ``## Diagnosis``
        # is a subsection of that diagnosis, not a second one, and promoting it
        # to the specification's level would flatten structure the author built.
        # Unmodeled means preserved verbatim, and the gate's own heading probes
        # remain level-agnostic, so nothing stops recognizing it.
        if spec is not None and level == spec.level:
            blocks.append(
                DocumentSection(
                    key=spec.key,
                    heading=spec.canonical_heading,
                    level=spec.level,
                    body=section_body,
                )
            )
        else:
            blocks.append(
                DocumentSection(
                    key=None,
                    heading=heading_text,
                    level=level,
                    body=section_body,
                    raw_heading_line=raw_heading_line,
                )
            )

    return IssueDocument(sections=tuple(blocks), type_key=type_key)


def render_issue_document(document: IssueDocument) -> str:
    """Render a typed document back to Markdown.

    Modeled sections are emitted in their canonical spelling at the level the
    specification declares. Everything else — preamble prose, unknown sections,
    the operator's asides — is written back verbatim.
    """
    return "".join(block.render() for block in document.sections)


def _insertion_index(document: IssueDocument, spec_order: tuple[str, ...], key: str) -> int:
    """Where a newly modeled section belongs in ``document.sections``.

    Directly after the last modeled section that precedes ``key`` in the type's
    canonical order; failing that, before the first modeled section that follows
    it; failing that, at the end. Appending blindly would put a bug's
    ``## Observed`` after its ``## Diagnosis``, which is conforming but reads
    backwards.
    """
    if key not in spec_order:
        return len(document.sections)
    position = spec_order.index(key)
    predecessors = set(spec_order[:position])
    successors = set(spec_order[position + 1 :])

    after = 0
    for index, block in enumerate(document.sections):
        if block.key in predecessors:
            after = index + 1
            # Skip the predecessor's own nested subsections; they belong to it.
            level = block.level
            for offset in range(index + 1, len(document.sections)):
                if document.sections[offset].level <= level:
                    break
                after = offset + 1
    if after:
        return after
    for index, block in enumerate(document.sections):
        if block.key in successors:
            return index
    return len(document.sections)


def with_section(
    document: IssueDocument,
    key: str,
    body: str,
    *,
    type_spec: IssueTypeSpec | None = None,
) -> IssueDocument:
    """Return ``document`` with a modeled section added in canonical position.

    ``body`` is the section's content, without the heading line. Existing
    sections are never touched — a producer normalizes what does not conform
    and leaves what does alone (ADR-0009 clause 8).
    """
    if document.has_section(key):
        return document
    spec = SECTIONS[key]
    order = tuple(rule.section_key for rule in type_spec.section_rules) if type_spec else ()
    index = _insertion_index(document, order, key)
    blocks = list(document.sections)

    content = _render_section_body(body, has_following=index < len(blocks))
    blocks.insert(
        index,
        DocumentSection(key=key, heading=spec.canonical_heading, level=spec.level, body=content),
    )
    if index > 0:
        blocks[index - 1] = _blank_terminated(blocks[index - 1])
    return replace(document, sections=tuple(blocks))


def replace_section(document: IssueDocument, key: str, body: str) -> IssueDocument:
    """Return ``document`` with the first modeled ``key`` block's body replaced.

    This is the complement to :func:`with_section`: when a modeled section is
    present but incomplete, resuming authoring needs a way to replace its
    content instead of silently leaving the short section in place.
    """
    blocks = list(document.sections)
    for index, block in enumerate(blocks):
        if block.key != key:
            continue
        has_following = index + 1 < len(blocks)
        blocks[index] = replace(
            block,
            body=_render_section_body(body, has_following=has_following),
        )
        return replace(document, sections=tuple(blocks))
    return document


def _render_section_body(body: str, *, has_following: bool) -> str:
    """Render section content with the canonical blank-line padding."""
    content = "\n\n" + body.strip("\n") + "\n"
    if has_following:
        # A following block starts with its own heading line, so the section
        # body needs the blank line between them.
        content += "\n"
    return content


def _blank_terminated(block: DocumentSection) -> DocumentSection:
    """Return ``block`` with its content ending in exactly one blank line.

    The block a new section is inserted after must not run its last line
    straight into the inserted heading.
    """
    if block.body.endswith("\n\n"):
        return block
    return replace(block, body=block.body.rstrip("\n") + "\n\n")
