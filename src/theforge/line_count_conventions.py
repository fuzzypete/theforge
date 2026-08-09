"""Module and test-file size checks. Reporting only — nothing here blocks.

:func:`check_line_counts` is the plain scan: every module over the configured
``max_module_lines`` with its distance from that limit, always
``blocking=False``. It is what the advisory report reads.

The ADR-0008 ratchet used to live here and did block. It was withdrawn: module
size is a codebase-scoped, temporal property, and a story-scoped gate could only
be satisfied by relocating lines to whichever module was cheapest — which bought
fragmentation rather than decomposition, and charged it to whichever story
happened to be blocked. Size is now measured and reported so it can feed
grooming; deciding a module needs splitting is funded work, not a mid-story toll.
"""

from __future__ import annotations

from pathlib import Path

from theforge.config.types import HardConventionsConfig
from theforge.convention_types import ConventionViolation

MODULE_LINES_RULE = "max_module_lines"
TEST_FILE_LINES_RULE = "max_test_file_lines"


def module_line_counts(config: HardConventionsConfig, project_root: Path) -> dict[str, int]:
    """Return ``{path relative to project_root: line count}`` for every module.

    Module scan roots: configured package_roots (which may live outside src/),
    else the legacy src/** scope. Dedup by resolved path so overlapping roots
    (e.g. "src" and "src/pipeline") don't double-report a file.
    """
    if config.package_roots:
        module_roots = [project_root / rel for rel in config.package_roots]
    else:
        module_roots = [project_root / "src"]

    counts: dict[str, int] = {}
    seen: set[Path] = set()
    for module_root in module_roots:
        if not module_root.exists():
            continue
        for py_file in sorted(module_root.rglob("*.py")):
            resolved = py_file.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            rel = str(py_file.relative_to(project_root))
            counts[rel] = len(py_file.read_text(encoding="utf-8", errors="replace").splitlines())
    return counts


def check_line_counts(
    config: HardConventionsConfig, project_root: Path
) -> list[ConventionViolation]:
    """Return the advisory scan: every module and test file over its limit."""
    violations: list[ConventionViolation] = []

    for rel, line_count in module_line_counts(config, project_root).items():
        if line_count > config.max_module_lines:
            violations.append(
                ConventionViolation(
                    rule=MODULE_LINES_RULE,
                    file=rel,
                    detail=f"{rel} has {line_count} lines (limit {config.max_module_lines})",
                    blocking=False,
                )
            )

    for py_file in sorted((project_root / "tests").rglob("*.py")):
        line_count = len(py_file.read_text(encoding="utf-8", errors="replace").splitlines())
        if line_count > config.max_test_file_lines:
            rel = str(py_file.relative_to(project_root))
            violations.append(
                ConventionViolation(
                    rule=TEST_FILE_LINES_RULE,
                    file=rel,
                    detail=f"{rel} has {line_count} lines (limit {config.max_test_file_lines})",
                    blocking=False,
                )
            )

    return violations
