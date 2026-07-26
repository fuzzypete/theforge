"""Sprint-time grooming check — text-only semantic quality findings.

This check runs at the runner injection point (post-``normalize_dependency_plan``,
pre-``run_batch_preflight``) on the full normalized task list and produces
``IntakeFinding`` records for findings the shape gate doesn't catch — most
importantly HOW-shaped acceptance criteria. File-level collision detection
is NOT in scope for this check; that remains post-preflight via
``compute_synthetic_edges``.

Stdlib only.
"""

from __future__ import annotations

import re

from ..shape_check.parsing import (
    extract_ac_section,
    extract_contextual_bullets,
    extract_contextual_fenced_code_blocks,
)
from .findings import FixType, IntakeFinding, IntakeSeverity

# HOW-detection is deliberately context-aware. A flat substring list over raw
# AC text misfires whenever a flagged word functions as a domain verb, field
# name, or command rather than an implementation prescription — e.g. "Import
# output includes...", "Export saves...", "Rename target file to...". Those are
# legitimate WHAT-language and must not be dropped. So detection is split into
# two rules:
#
#   1. Strong tokens: phrases that describe *manipulating code* and have no
#      plausible domain reading (e.g. "refactor", "implement using"). These
#      flag on their own.
#   2. Verb + code-construct proximity: an implementation verb ("extract",
#      "rename", "split", ...) is HOW-shaped only when it operates on a code
#      construct ("class", "method", "module", ...). The same verb applied to a
#      domain object ("Rename target file", "Extract records from CSV") is not.
#
# Note: the ingest verb "import" is intentionally NOT in the verb list — it is
# the single most common domain-verb false positive ("Import output includes
# ..."). Genuine import-machinery prescriptions still trip a strong token
# (e.g. "implement using the existing import machinery").
_STRONG_HOW_TOKENS: tuple[str, ...] = (
    "refactor",
    "implement using",
    "implemented using",
    "implementation:",
    "subclass",
)

def _bounded_token_pattern(tok: str) -> str:
    """Anchor a token to whole-word boundaries only where the token itself ends
    in a word character. A trailing \\b after punctuation (e.g. "implementation:")
    is a no-op that fails to match, since \\b requires a word/non-word transition
    and punctuation already provides that separation from following text."""
    left = r"\b" if tok[0].isalnum() or tok[0] == "_" else ""
    right = r"\b" if tok[-1].isalnum() or tok[-1] == "_" else ""
    return left + re.escape(tok) + right


_STRONG_HOW_RE = re.compile(
    "|".join(_bounded_token_pattern(tok) for tok in _STRONG_HOW_TOKENS),
    re.IGNORECASE,
)

# An implementation verb operating on a code construct within the same clause.
# The verb list excludes "import" (see above); the construct list matches the
# code nouns that distinguish "restructure code" from "describe behavior".
_IMPL_VERB_CONSTRUCT_RE = re.compile(
    r"\b(?:add|create|extract|implement|introduce|modify|move|refactor|"
    r"rename|split|update|use)\b"
    r".{0,60}?\b(?:class|method|function|module|subclass)\b",
    re.IGNORECASE,
)


def _looks_how_shaped(text: str) -> bool:
    """True when a bullet reads as implementation-prescriptive (HOW), not WHAT."""
    return bool(_STRONG_HOW_RE.search(text) or _IMPL_VERB_CONSTRUCT_RE.search(text))


_EXAMPLE_FILE_DIRECTIVE_RE = re.compile(
    r"\b(?:add|create|edit|implement|modify|remove|rename|update)\b"
    r".{0,100}\b(?:src/|tests/|docs/|[\w./-]+\.(?:py|pyi|js|jsx|ts|tsx|yaml|yml|json|sh|toml|rs|go|java|rb|md))",
    re.IGNORECASE,
)
_EXAMPLE_SIGNATURE_RE = re.compile(
    r"^\s*(?:def|class)\s+\w+\b|^\s*[A-Za-z_]\w*\s*\([^)]*\)\s*(?:->\s*[^:]+)?\s*:\s*$",
    re.MULTILINE,
)
_EXAMPLE_IMPERATIVE_RE = re.compile(
    r"\b(?:add|create|extract|implement|modify|refactor|rename|update)\b"
    r".{0,60}\b(?:method|class|function|module|subclass)\b",
    re.IGNORECASE,
)


def _example_block_directive_preview(block: str) -> str | None:
    """Return a preview line when an example block contains explicit implementation directives."""
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _EXAMPLE_FILE_DIRECTIVE_RE.search(stripped):
            return stripped
        if _EXAMPLE_IMPERATIVE_RE.search(stripped):
            return stripped
    signature_match = _EXAMPLE_SIGNATURE_RE.search(block)
    if signature_match:
        return signature_match.group(0).strip()
    return None


def _ac_lines_look_how_shaped(section: str) -> list[str]:
    """Return AC bullet lines that look HOW-shaped (implementation prescriptive)."""
    return [
        bullet.text
        for bullet in extract_contextual_bullets(section)
        if not bullet.in_example_section and _looks_how_shaped(bullet.text)
    ]


def _example_section_directives(body: str) -> list[str]:
    """Return explicit implementation directives found inside example-shaped fenced blocks."""
    findings: list[str] = []
    for block in extract_contextual_fenced_code_blocks(body):
        if not block.in_example_section:
            continue
        preview = _example_block_directive_preview(block.content)
        if preview is not None:
            findings.append(preview)
    return findings


def groom_check(title: str, body: str, labels: list[str]) -> list[IntakeFinding]:
    """Return a list of grooming findings for the given issue text.

    Pure function — no I/O, no LLM. Designed to be cheap enough to run on
    every story at sprint intake.
    """
    findings: list[IntakeFinding] = []

    if not body or not body.strip():
        return findings

    section = extract_ac_section(body)
    if section:
        how_lines = _ac_lines_look_how_shaped(section)
        how_lines.extend(_example_section_directives(body))
        if how_lines:
            preview = "; ".join(line.strip()[:80] for line in how_lines[:3])
            findings.append(
                IntakeFinding(
                    code="groom_how_shaped_ac",
                    severity=IntakeSeverity.BLOCK,
                    location="acceptance_criteria",
                    problem=(
                        "Acceptance criteria contain implementation-prescriptive "
                        f"(HOW-shaped) language: {preview}"
                    ),
                    suggested_replacement=None,
                    fix_type=FixType.SEMANTIC,
                )
            )

    return findings
