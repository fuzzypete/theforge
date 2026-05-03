"""Hard convention checks — mechanically enforced code structure rules."""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tempfile
import tokenize
from dataclasses import dataclass
from pathlib import Path

from theforge.config.types import HardConventionsConfig
from theforge.root_file_conventions import (
    DEFAULT_ALLOWED_ROOT_FILES,
    resolve_root_file_allowances,
)


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
    if config.no_scratch_files:
        violations.extend(
            _check_no_scratch_files(
                project_root,
                stack=config.stack,
                allowed_root_files=config.allowed_root_files,
            )
        )
    violations.extend(_check_stack_neutrality(project_root))
    return violations


def new_hard_convention_violations_since_ref(
    config: HardConventionsConfig, project_root: Path, git_ref: str
) -> tuple[list[ConventionViolation], list[ConventionViolation]]:
    """Return (current, net-new) hard convention violations since git_ref."""
    current = check_hard_conventions(config, project_root)
    baseline = _check_hard_conventions_at_git_ref(config, project_root, git_ref)
    baseline_keys = {_violation_key(v) for v in baseline}
    net_new = [v for v in current if _violation_key(v) not in baseline_keys]
    return current, net_new


def _check_hard_conventions_at_git_ref(
    config: HardConventionsConfig, project_root: Path, git_ref: str
) -> list[ConventionViolation]:
    """Run hard convention checks against the repository tree at git_ref."""
    with tempfile.TemporaryDirectory(prefix="theforge-conventions-") as tmp:
        tmp_path = Path(tmp)
        proc = subprocess.run(
            ["git", "archive", git_ref],
            cwd=project_root,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                "Failed to archive "
                f"{git_ref!r} for convention baseline: {stderr or 'unknown error'}"
            )
        extract = subprocess.run(
            ["tar", "-xf", "-"],
            cwd=tmp_path,
            input=proc.stdout,
            capture_output=True,
            timeout=30,
        )
        if extract.returncode != 0:
            stderr = extract.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                "Failed to extract convention baseline for "
                f"{git_ref!r}: {stderr or 'unknown error'}"
            )
        return check_hard_conventions(config, tmp_path)


def _violation_key(violation: ConventionViolation) -> tuple[str, str]:
    """Stable identity for comparing convention violations across snapshots."""
    return (violation.rule, violation.file)


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
                    blocking=False,
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
                    blocking=False,
                )
            )

    return violations


# ── Circular import check ─────────────────────────────────────────────


def _module_name(py_file: Path, src_root: Path) -> str:
    """Convert a file path to a dotted module name relative to src.

    __init__.py is normalized to its package name:
    theforge/pkg/__init__.py → theforge.pkg
    """
    rel = py_file.relative_to(src_root)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _collect_imports(py_file: Path, src_root: Path) -> list[str]:
    """Return list of internal module names imported by py_file.

    Produces both the base module (e.g. "theforge") and dotted sub-imports
    (e.g. "theforge.b") so that `from theforge import b` resolves to
    "theforge.b" via _best_match when "theforge.b" is a known module.

    Relative imports are resolved using the *unnormalized* module path so that
    `from . import a` in sub/__init__.py correctly resolves to theforge.sub.a
    rather than theforge.a (which would be wrong after __init__ is stripped).
    """
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []

    # Unnormalized path keeps __init__ so relative imports anchor correctly.
    rel = py_file.relative_to(src_root)
    current_for_relative = ".".join(rel.with_suffix("").parts)

    imports: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import — resolve to absolute using unnormalized path
                parts = current_for_relative.split(".")
                base_parts = parts[: -(node.level)]
                base = ".".join(base_parts)
                if node.module:
                    # `from .sub import a` → add theforge.sub and theforge.sub.a
                    resolved = f"{base}.{node.module}" if base else node.module
                    imports.append(resolved)
                    for alias in node.names:
                        imports.append(f"{resolved}.{alias.name}")
                else:
                    # `from . import a` — no intermediate module; only add theforge.sub.a
                    # Do NOT add base itself (would create a self-reference for __init__.py)
                    for alias in node.names:
                        imports.append(f"{base}.{alias.name}" if base else alias.name)
            elif node.module:
                imports.append(node.module)
                # Also add module.name variants (handles `from pkg import submod`)
                for alias in node.names:
                    imports.append(f"{node.module}.{alias.name}")

    return imports


def _check_circular_imports(project_root: Path) -> list[ConventionViolation]:
    # Spec scopes this check to src/theforge only
    theforge_src = project_root / "src" / "theforge"
    src_root = project_root / "src"
    if not theforge_src.exists():
        return []

    # Build adjacency: module → set of imported modules
    adjacency: dict[str, list[str]] = {}
    file_map: dict[str, Path] = {}

    for py_file in sorted(theforge_src.rglob("*.py")):
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


# ── Scratch file check ────────────────────────────────────────────────

_ALLOWED_ROOT_FILES: frozenset[str] = frozenset(DEFAULT_ALLOWED_ROOT_FILES)


def _check_no_scratch_files(
    project_root: Path,
    *,
    stack: tuple[str, ...] | list[str] = (),
    allowed_root_files: tuple[str, ...] = (),
) -> list[ConventionViolation]:
    """Check that no unrecognised files exist directly in the project root.

    Source files should live in appropriate project directories rather than the
    repository root. Files that don't match the known-good whitelist of root
    files are almost always scratch/exploration files that were never deleted
    before committing (e.g. bin_script, fake_forge, test_exec.py from issue
    #646).

    Hidden files (dotfiles) are skipped — they are conventionally tooling
    configuration and pose no risk of accidental source-code pollution.
    """
    violations: list[ConventionViolation] = []
    allowed_files = resolve_root_file_allowances(
        stack=stack,
        allowed_root_files=allowed_root_files,
    ).effective
    for f in sorted(project_root.iterdir()):
        if not f.is_file():
            continue
        if f.name.startswith("."):
            continue  # dotfiles are tooling config — always allowed
        if f.name in allowed_files:
            continue
        rel = str(f.relative_to(project_root))
        violations.append(
            ConventionViolation(
                rule="no_scratch_files",
                file=rel,
                detail=(
                    f"{rel} is not an allowed root-level file — "
                    "source files must live in appropriate project directories, "
                    "not the repository root"
                ),
            )
        )
    return violations


# ── Test mirror check ─────────────────────────────────────────────────


def _check_test_mirrors(project_root: Path) -> list[ConventionViolation]:
    src_pkg = project_root / "src" / "theforge"
    tests_root = project_root / "tests"
    if not src_pkg.exists() or not tests_root.exists():
        return []

    violations: list[ConventionViolation] = []

    for item in sorted(src_pkg.iterdir()):
        if item.name == "__init__.py" or item.name == "__pycache__":
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


@dataclass(frozen=True)
class _StackNeutralityRule:
    pattern: re.Pattern[str]
    violation_rule: str
    convention_rule: str
    description: str


_STACK_NEUTRALITY_SCOPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "core orchestrator modules must be stack-neutral",
        (
            "src/theforge/task",
            "src/theforge/coordinator",
            "src/theforge/sprint",
        ),
    ),
    (
        "shared schemas may not encode stack-specific concepts",
        (
            "src/theforge/schemas.py",
            "src/theforge/config/types.py",
            "src/theforge/config/schema.py",
            "src/theforge/config/models.py",
        ),
    ),
    (
        "stack-specific assumptions belong in forge.yaml or repo-local conventions, "
        "not TheForge core",
        (
            "src/theforge/config/load.py",
            "src/theforge/config/defaults.py",
            "src/theforge/config/bridge.py",
            "src/theforge/config/role_derivation.py",
        ),
    ),
)

_STACK_NEUTRALITY_RULES: tuple[_StackNeutralityRule, ...] = (
    _StackNeutralityRule(
        pattern=re.compile(r"\b(?:pytest_|npm_|cargo_|maven_|gradle_)[A-Za-z0-9_]*\b"),
        violation_rule="stack_neutrality_shared_model_prefix",
        convention_rule="shared schemas may not encode stack-specific concepts",
        description=(
            "shared models must not use stack-specific prefixes like pytest_/npm_/cargo_/"
            "maven_/gradle_"
        ),
    ),
    _StackNeutralityRule(
        pattern=re.compile(r"\b(?:make fmt|pytest|npm test|cargo test|go test)\b"),
        violation_rule="stack_neutrality_literal_tool_command",
        convention_rule=(
            "prompt templates must reference configured commands, not literal tool invocations"
        ),
        description=(
            "core prompt logic must reference configured commands instead of literal "
            "tool invocations"
        ),
    ),
    _StackNeutralityRule(
        pattern=re.compile(r"(?<![A-Za-z0-9_])(?:src/|tests/|docs/)[^\s'\"]*"),
        violation_rule="stack_neutrality_hardcoded_layout",
        convention_rule=(
            "generated scaffolding must use generic names (e.g. test_target) or omit the concept"
        ),
        description=(
            "reusable prompt logic must not hardcode src/, tests/, or docs/ layout assumptions"
        ),
    ),
    _StackNeutralityRule(
        pattern=re.compile(
            r"(?:"
            r"(?:split|strip|replace|sub|match|search|findall|startswith|endswith)\b[^\n#]*"
            r"(?:pytest|xdist|npm|cargo|maven|gradle|go\s+test)"
            r"|"
            r"(?:pytest|npm|cargo|maven|gradle|go\s+test)[^\n#]*"
            r"(?:split|strip|replace|sub|match|search|findall|startswith|endswith)"
            r")",
            re.IGNORECASE,
        ),
        violation_rule="stack_neutrality_language_specific_story_parsing",
        convention_rule="core orchestrator modules must be stack-neutral",
        description="story parsing must remain stack-neutral rather than language-specific",
    ),
)

_STACK_NEUTRALITY_EXEMPT_MARKERS: tuple[str, ...] = (
    "python-specific example",
    "python specific example",
)


def _check_stack_neutrality(project_root: Path) -> list[ConventionViolation]:
    violations: list[ConventionViolation] = []
    for _convention_rule, scoped_paths in _STACK_NEUTRALITY_SCOPES:
        for scoped_path in scoped_paths:
            path = project_root / scoped_path
            if not path.exists():
                continue
            if path.is_file():
                violations.extend(_scan_stack_neutrality_file(path, project_root=project_root))
                continue
            for py_file in sorted(path.rglob("*.py")):
                violations.extend(_scan_stack_neutrality_file(py_file, project_root=project_root))
    return violations


def _scan_stack_neutrality_file(
    path: Path,
    *,
    project_root: Path,
) -> list[ConventionViolation]:
    rel = str(path.relative_to(project_root))
    text = path.read_text(encoding="utf-8", errors="replace")
    if _is_stack_neutrality_exempt(rel, text):
        return []

    violations: list[ConventionViolation] = []
    for line_number, line in _stack_neutrality_scan_lines(text):
        for rule in _STACK_NEUTRALITY_RULES:
            match = rule.pattern.search(line)
            if match is None:
                continue
            violations.append(
                ConventionViolation(
                    rule=rule.violation_rule,
                    file=rel,
                    detail=(
                        f"line {line_number}: matched {match.group(0)!r}; "
                        f"smell: {rule.description}; violates convention rule: "
                        f"{rule.convention_rule}"
                    ),
                )
            )
    return violations


def _stack_neutrality_scan_lines(text: str) -> list[tuple[int, str]]:
    """Return code-only lines for stack-neutrality scanning.

    Docstrings and shell/git plumbing lines are excluded entirely. Comments are
    stripped from otherwise-scannable lines so inline code still participates in
    the check.
    """
    lines = text.splitlines()
    ignored_lines = _docstring_line_numbers(text) | _shell_command_line_numbers(text)
    comment_spans = _comment_spans_by_line(text)
    scanned_lines: list[tuple[int, str]] = []
    for line_number, line in enumerate(lines, start=1):
        if line_number in ignored_lines:
            continue
        stripped_line = _strip_comment_spans(line, comment_spans.get(line_number, ()))
        if not stripped_line.strip():
            continue
        scanned_lines.append((line_number, stripped_line))
    return scanned_lines


def _docstring_line_numbers(text: str) -> set[int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()

    ignored: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first_stmt = body[0]
        if not isinstance(first_stmt, ast.Expr):
            continue
        value = first_stmt.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        start = getattr(first_stmt, "lineno", None)
        end = getattr(first_stmt, "end_lineno", start)
        if start is None:
            continue
        ignored.update(range(start, (end or start) + 1))
    return ignored


def _comment_spans_by_line(text: str) -> dict[int, list[tuple[int, int]]]:
    spans: dict[int, list[tuple[int, int]]] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT:
                continue
            line_number = token.start[0]
            spans.setdefault(line_number, []).append((token.start[1], token.end[1]))
    except tokenize.TokenError:
        return spans
    return spans


def _strip_comment_spans(
    line: str,
    spans: list[tuple[int, int]] | tuple[tuple[int, int], ...],
) -> str:
    if not spans:
        return line
    kept: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        kept.append(line[cursor:start])
        cursor = end
    kept.append(line[cursor:])
    return "".join(kept)


def _shell_command_line_numbers(text: str) -> set[int]:
    ignored: set[int] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ignored

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            is_run_shell = func.attr == "_run_shell"
        elif isinstance(func, ast.Name):
            is_run_shell = func.id == "_run_shell"
        else:
            is_run_shell = False
        if not is_run_shell or not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant):
            is_shell_text = isinstance(first_arg.value, str)
        elif isinstance(first_arg, ast.JoinedStr):
            is_shell_text = True
        else:
            is_shell_text = False
        if not is_shell_text:
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", start)
        if start is None:
            continue
        ignored.update(range(start, (end or start) + 1))
    return ignored


def _is_stack_neutrality_exempt(rel_path: str, text: str) -> bool:
    if rel_path == "forge.yaml":
        return True
    if "example" in rel_path.lower() and any(
        marker in text.lower() for marker in _STACK_NEUTRALITY_EXEMPT_MARKERS
    ):
        return True
    return False
