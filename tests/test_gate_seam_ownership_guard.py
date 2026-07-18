"""Mechanical guard: gate/shell command dispatch is owned by ONE seam (issue #1737).

The production gate path has a single primitive (``_run_shell_detailed``). Tests
that simulate gate/shell behavior must go through the one sanctioned seam in
``coord_test_helpers`` (``mock_gate`` / ``_gate_side_effect`` / ``_shell_with_gate``
/ ``_as_detailed`` / ``_handle_stale_check_cmd``). Those helpers own the command
dispatch (gate vs git-status vs stale-worktree checks) and the 2-tuple→4-tuple
exit-code/timeout derivation.

This guard enforces two things, both AST-based (not regex) so strings, comments,
and docstrings that merely mention the names/target do not trip it:

1. **No re-defining the seam primitives.** When a test module *re-defines* one of
   the reserved dispatch/derivation helpers inline instead of importing it, the
   dispatch and the ``0 if ok else 1`` / ``TIMEOUT`` derivation get duplicated —
   exactly how the original test-double drift went unnoticed for 70+ days, and how
   a botched migration left seven identical inline dispatch blocks in one test.

2. **No patching the primitive directly.** A test may only reach the production
   ``_run_shell_detailed`` primitive through the sanctioned seam
   (``mock_gate`` / ``patch_gate_shell``, both defined in ``coord_test_helpers``).
   Naming the primitive as a ``patch`` target inline (rather than going through
   ``patch_gate_shell``) lets a test simulate gate behavior without entering the
   seam, so the "one owner of gate dispatch" contract would only be as strong as
   reviewer vigilance. This makes it mechanical.

Offending ``file:line`` locations are named in the assertion message so a
recurrence is immediately diagnosable.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = REPO_ROOT / "tests"

# The single module that is *allowed* to define the seam primitives and to name
# the production patch target.
_SANCTIONED_MODULE = "coord_test_helpers.py"

# Reserved names: the gate/shell dispatch + exit-code/timeout derivation
# primitives. A test module must import these from ``coord_test_helpers`` rather
# than re-implement them inline.
_RESERVED_SEAM_NAMES = frozenset(
    {
        "_gate_side_effect",
        "_shell_with_gate",
        "_as_detailed",
        "_handle_stale_check_cmd",
    }
)

# The production shell primitive (and its ``gate._cu`` alias) that only the
# sanctioned seam may name as a ``patch`` target.
_PATCH_TARGETS = frozenset(
    {
        "theforge.coordinator.util._run_shell_detailed",
        "theforge.coordinator.gate._cu._run_shell_detailed",
    }
)


def _iter_test_files() -> list[Path]:
    return sorted(TESTS_ROOT.glob("test_*.py"))


def _seam_redefinitions(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (lineno, name) for any def re-defining a reserved seam primitive."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _RESERVED_SEAM_NAMES:
                out.append((node.lineno, node.name))
    return out


def _is_patch_call(node: ast.Call) -> bool:
    """True if ``node`` is a call to ``patch(...)`` / ``mock.patch(...)``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if isinstance(func, ast.Attribute):
        return func.attr == "patch"
    return False


def _direct_primitive_patches(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (lineno, target) for any ``patch("<gate shell primitive>")`` call."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_patch_call(node) and node.args):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value in _PATCH_TARGETS:
            out.append((node.lineno, first.value))
    return out


def test_no_inline_gate_dispatch_outside_sanctioned_seam() -> None:
    """Only coord_test_helpers may define the gate/shell dispatch primitives."""
    offenders: list[str] = []
    for path in _iter_test_files():
        if path.name == _SANCTIONED_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT)
        for lineno, name in _seam_redefinitions(tree):
            offenders.append(f"{rel}:{lineno}: def {name}(...)")

    assert not offenders, (
        "Gate/shell command dispatch must be owned by the single sanctioned seam "
        "in tests/coord_test_helpers.py (mock_gate / _gate_side_effect / "
        "_shell_with_gate / _as_detailed / _handle_stale_check_cmd). Import these "
        "helpers instead of re-defining them inline — duplicating the dispatch and "
        "the exit-code/timeout derivation is how test-double drift hid for 70+ days "
        "(see #1737). Offending definitions:\n  " + "\n  ".join(offenders)
    )


def test_no_direct_patch_of_gate_shell_primitive_outside_seam() -> None:
    """Only coord_test_helpers may patch the production shell primitive.

    Gate-touching tests must enter the seam via ``mock_gate`` (for
    PASS/FAIL/exit/timeout behavior) or ``patch_gate_shell`` (when wiring a
    sanctioned side_effect), never by naming the primitive as a ``patch`` target
    themselves. Otherwise gate behavior can be simulated without entering the seam.
    """
    offenders: list[str] = []
    for path in _iter_test_files():
        if path.name == _SANCTIONED_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT)
        for lineno, target in _direct_primitive_patches(tree):
            offenders.append(f'{rel}:{lineno}: patch("{target}")')

    assert not offenders, (
        "The production gate/shell primitive may only be patched through the "
        "sanctioned seam in tests/coord_test_helpers.py. Use "
        "``with patch_gate_shell() as mock_shell:`` / ``@patch_gate_shell()`` (or "
        "``mock_gate(...)``) instead of naming "
        "theforge.coordinator.util._run_shell_detailed as a patch target directly — "
        "otherwise gate behavior can be simulated without entering the seam "
        "(see #1737). Offending patches:\n  " + "\n  ".join(offenders)
    )
