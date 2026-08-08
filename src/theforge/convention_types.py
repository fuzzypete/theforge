"""Pure-data types shared by the hard convention checks.

Kept in its own stdlib-only module so the individual check modules
(``conventions``, ``line_count_conventions``) can all depend on the violation
type without depending on each other.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ConventionViolation:
    rule: str  # "max_module_lines", "no_circular_imports", "test_mirrors_source"
    file: str  # path relative to project root
    detail: str  # human-readable description
    blocking: bool = True
