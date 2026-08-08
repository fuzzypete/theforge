"""Module and test-file size checks, including the module-size ratchet.

Two different questions are answered here and they must not be conflated:

* :func:`check_line_counts` is the plain scan — every module over the
  configured ``max_module_lines`` with its distance from that limit. It is
  advisory (``blocking=False``) and it is what the advisory report reads.
* :func:`module_growth_violations` is the ratchet (ADR-0008) — a module already
  over the limit in the baseline tree is frozen at the size it had there and may
  not grow, while a module within the limit stays governed by the limit itself.
  It blocks.

A module can be in both at once: 7,153 lines with a 7,153-line ceiling is
compliant with the ratchet and still eleven times the convention, and both facts
have to stay visible.
"""

from __future__ import annotations

import dataclasses
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


def module_line_ceiling(baseline_line_count: int | None, limit: int) -> int:
    """Return the size *this* module may not exceed (ADR-0008).

    A module already over ``limit`` in the baseline tree is frozen at the size it
    had there; anything else — within the limit, or absent from the baseline
    entirely — is governed by the configured limit. Derived from the tree on
    every check rather than recorded, so a ceiling cannot drift from actual size
    by omission and a newly added oversized module is governed from its first
    commit.
    """
    if baseline_line_count is not None and baseline_line_count > limit:
        return baseline_line_count
    return limit


def module_growth_violations(
    config: HardConventionsConfig,
    project_root: Path,
    baseline_module_lines: dict[str, int],
) -> list[ConventionViolation]:
    """Return blocking violations for modules that exceed their frozen ceiling.

    A module at or below its ceiling produces nothing, so an over-limit module
    that shrank or stayed the same size is never refused for being over the
    limit.
    """
    limit = config.max_module_lines
    violations: list[ConventionViolation] = []
    for rel, line_count in sorted(module_line_counts(config, project_root).items()):
        ceiling = module_line_ceiling(baseline_module_lines.get(rel), limit)
        if line_count <= ceiling:
            continue
        if ceiling > limit:
            reason = (
                f"it was already over the {limit}-line limit at the branch point, so it is "
                "frozen at that size and may not grow (it may shrink freely)"
            )
        else:
            reason = f"it must stay within the {limit}-line limit"
        violations.append(
            ConventionViolation(
                rule=MODULE_LINES_RULE,
                file=rel,
                detail=(
                    f"{rel} has {line_count} lines and may not exceed {ceiling} "
                    f"(exceeds it by {line_count - ceiling}) — {reason}"
                ),
                blocking=True,
            )
        )
    return violations


def fail_closed_module_violations(
    violations: list[ConventionViolation],
) -> list[ConventionViolation]:
    """Return *violations* with module-size findings promoted to blocking.

    Used when no baseline tree is available, so no ceiling can be derived: the
    configured limit is then the only ceiling there is, and an oversized module
    is refused rather than passed unblocked. Copies rather than mutates, so the
    advisory view of the same scan keeps reporting distance from the limit.
    """
    return [
        dataclasses.replace(v, blocking=True) if v.rule == MODULE_LINES_RULE else v
        for v in violations
    ]


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
