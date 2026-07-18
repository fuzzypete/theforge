"""Mechanical guard: gate/shell command dispatch is owned by ONE seam (issue #1737).

The production gate path has a single primitive (``_run_shell_detailed``). Tests
that simulate gate/shell behavior must go through the one sanctioned seam in
``coord_test_helpers`` (``mock_gate`` / ``_gate_side_effect`` / ``_shell_with_gate``
/ ``_as_detailed`` / ``_handle_stale_check_cmd``). Those helpers own the command
dispatch (gate vs git-status vs stale-worktree checks) and the 2-tuple→4-tuple
exit-code/timeout derivation.

When a test module *re-defines* one of those primitives inline instead of
importing it, the dispatch and the ``0 if ok else 1`` / ``TIMEOUT`` derivation
get duplicated — which is exactly how the original test-double drift went
unnoticed for 70+ days, and how a botched migration left seven identical inline
dispatch blocks in one test. This guard fails the gate if any test module other
than ``coord_test_helpers`` defines a function with one of those reserved seam
names, so the derivation lives in exactly one place and cannot silently fork.

The scan is AST-based (not regex) so strings, comments, and docstrings that
merely mention the names do not trip it — only real ``def``/``async def``
definitions do. Offending ``file:line`` locations are named in the assertion
message so a recurrence is immediately diagnosable.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = REPO_ROOT / "tests"

# The single module that is *allowed* to define the seam primitives.
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
