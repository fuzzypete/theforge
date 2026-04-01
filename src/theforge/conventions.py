"""Hard convention checks — mechanically enforced code structure rules."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from theforge.config.types import HardConventionsConfig


@dataclass
class ConventionViolation:
    rule: str  # "max_module_lines", "no_circular_imports", "test_mirrors_source"
    file: str  # path relative to project root
    detail: str  # human-readable description
    blocking: bool = True


def check_hard_conventions(
    config: HardConventionsConfig, project_root: Path
) -> list[ConventionViolation]:
    """Run all enabled hard convention checks and return violations."""
    violations: list[ConventionViolation] = []
    violations.extend(_check_line_counts(config, project_root))
    if config.no_circular_imports:
        violations.extend(_check_circular_imports(project_root))
    if config.test_mirrors_source:
        violations.extend(_check_test_mirrors(project_root))
    return violations


# ── Line count check ──────────────────────────────────────────────────


def _check_line_counts(
    config: HardConventionsConfig, project_root: Path
) -> list[ConventionViolation]:
    violations: list[ConventionViolation] = []

    src_root = project_root / "src"
    tests_root = project_root / "tests"

    for py_file in sorted(src_root.rglob("*.py")):
        line_count = len(py_file.read_text(encoding="utf-8", errors="replace").splitlines())
        if line_count > config.max_module_lines:
            rel = str(py_file.relative_to(project_root))
            violations.append(
                ConventionViolation(
                    rule="max_module_lines",
                    file=rel,
                    detail=f"{rel} has {line_count} lines (limit {config.max_module_lines})",
                )
            )

    for py_file in sorted(tests_root.rglob("*.py")):
        line_count = len(py_file.read_text(encoding="utf-8", errors="replace").splitlines())
        if line_count > config.max_test_file_lines:
            rel = str(py_file.relative_to(project_root))
            violations.append(
                ConventionViolation(
                    rule="max_test_file_lines",
                    file=rel,
                    detail=f"{rel} has {line_count} lines (limit {config.max_test_file_lines})",
                )
            )

    return violations


# ── Circular import check ─────────────────────────────────────────────


def _module_name(py_file: Path, src_root: Path) -> str:
    """Convert a file path to a dotted module name relative to src."""
    rel = py_file.relative_to(src_root)
    parts = list(rel.with_suffix("").parts)
    return ".".join(parts)


def _collect_imports(py_file: Path, src_root: Path) -> list[str]:
    """Return list of internal module names imported by py_file.

    Produces both the base module (e.g. "theforge") and dotted sub-imports
    (e.g. "theforge.b") so that `from theforge import b` resolves to
    "theforge.b" via _best_match when "theforge.b" is a known module.
    """
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []

    current = _module_name(py_file, src_root)
    imports: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import — resolve to absolute
                parts = current.split(".")
                base_parts = parts[: -(node.level)]
                base = ".".join(base_parts)
                if node.module:
                    resolved = f"{base}.{node.module}" if base else node.module
                else:
                    resolved = base
                imports.append(resolved)
                for alias in node.names:
                    imports.append(f"{resolved}.{alias.name}")
            elif node.module:
                imports.append(node.module)
                # Also add module.name variants (handles `from pkg import submod`)
                for alias in node.names:
                    imports.append(f"{node.module}.{alias.name}")

    return imports


def _check_circular_imports(project_root: Path) -> list[ConventionViolation]:
    src_root = project_root / "src"
    if not src_root.exists():
        return []

    # Build adjacency: module → set of imported modules
    adjacency: dict[str, list[str]] = {}
    file_map: dict[str, Path] = {}

    for py_file in sorted(src_root.rglob("*.py")):
        mod = _module_name(py_file, src_root)
        imports = _collect_imports(py_file, src_root)
        adjacency[mod] = imports
        file_map[mod] = py_file

    # DFS-based cycle detection
    visited: set[str] = set()
    in_stack: set[str] = set()
    cycles: list[list[str]] = []

    def dfs(node: str, path: list[str]) -> None:
        visited.add(node)
        in_stack.add(node)
        path.append(node)
        for neighbor in adjacency.get(node, []):
            # Normalize: strip sub-imports to known modules
            if neighbor not in adjacency:
                # Try prefix match: "a.b.c" → "a.b" or "a"
                neighbor = _best_match(neighbor, adjacency)
                if neighbor is None:
                    continue
            if neighbor not in visited:
                dfs(neighbor, path)
            elif neighbor in in_stack:
                # Found a cycle — extract the cycle portion
                cycle_start = path.index(neighbor)
                cycle = path[cycle_start:] + [neighbor]
                # Deduplicate: use canonical key = frozenset
                key = frozenset(cycle)
                if not any(frozenset(c) == key for c in cycles):
                    cycles.append(cycle)
        path.pop()
        in_stack.discard(node)

    for mod in list(adjacency):
        if mod not in visited:
            dfs(mod, [])

    violations: list[ConventionViolation] = []
    for cycle in cycles:
        cycle_str = " → ".join(cycle)
        # Use first file in cycle for the file field
        first_mod = cycle[0]
        rel = (
            str(file_map[first_mod].relative_to(project_root))
            if first_mod in file_map
            else first_mod
        )
        violations.append(
            ConventionViolation(
                rule="no_circular_imports",
                file=rel,
                detail=f"Circular import detected: {cycle_str}",
            )
        )

    return violations


def _best_match(name: str, adjacency: dict[str, list[str]]) -> str | None:
    """Return the longest prefix of name that exists in adjacency."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in adjacency:
            return candidate
    return None


# ── Test mirror check ─────────────────────────────────────────────────


def _check_test_mirrors(project_root: Path) -> list[ConventionViolation]:
    src_pkg = project_root / "src" / "theforge"
    tests_root = project_root / "tests"
    if not src_pkg.exists() or not tests_root.exists():
        return []

    violations: list[ConventionViolation] = []

    for item in sorted(src_pkg.iterdir()):
        if item.name.startswith("_") or item.name == "__pycache__":
            continue

        if item.is_file() and item.suffix == ".py":
            # src/theforge/foo.py → tests/test_foo.py
            expected = tests_root / f"test_{item.stem}.py"
            if not expected.exists():
                rel = str(item.relative_to(project_root))
                expected_rel = expected.relative_to(project_root)
                violations.append(
                    ConventionViolation(
                        rule="test_mirrors_source",
                        file=rel,
                        detail=f"No test mirror found for {rel} (expected {expected_rel})",
                    )
                )
        elif item.is_dir():
            # src/theforge/foo/ → tests/test_foo_*.py OR tests/test_foo/
            pkg_name = item.name
            mirror_dir = tests_root / f"test_{pkg_name}"
            mirror_glob = list(tests_root.glob(f"test_{pkg_name}_*.py"))
            if not mirror_dir.exists() and not mirror_glob:
                rel = str(item.relative_to(project_root))
                violations.append(
                    ConventionViolation(
                        rule="test_mirrors_source",
                        file=rel,
                        detail=(
                            f"No test mirror found for package {rel} "
                            f"(expected tests/test_{pkg_name}_*.py or tests/test_{pkg_name}/)"
                        ),
                    )
                )

    return violations
