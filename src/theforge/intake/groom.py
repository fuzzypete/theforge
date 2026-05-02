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

from ..shape_check.parsing import extract_ac_section, extract_bullets
from .findings import FixType, IntakeFinding, IntakeSeverity

# Tokens that strongly suggest a how/implementation-shaped AC line. These are
# intentionally conservative — false positives would cause the grooming check
# to drop runnable stories.
_HOW_TOKENS: tuple[str, ...] = (
    "refactor",
    "extract",
    "rename",
    "implement using",
    "implemented using",
    "implementation:",
    "use the ",
    "using the ",
    "import ",
    "function ",
    "class ",
    "method ",
    "module ",
    "subclass",
)

_HOW_RE = re.compile("|".join(re.escape(tok) for tok in _HOW_TOKENS), re.IGNORECASE)


def _ac_lines_look_how_shaped(section: str) -> list[str]:
    """Return AC bullet lines that look HOW-shaped (implementation prescriptive)."""
    bullets = extract_bullets(section)
    return [line for line in bullets if _HOW_RE.search(line)]


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
