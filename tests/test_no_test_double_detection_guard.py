"""Mechanical guard: production code under ``src/`` must never branch on
test-double detection (issue #1737).

The gate module used to import ``unittest.mock`` and branch on
``isinstance(run_shell, Mock)`` to detect "am I under test right now?" and vary
its behavior accordingly — so tests validated a synthesized path that never
shipped, and never exercised the real exit-code/timeout semantics. That branch
was removed; this static guard fails the gate if it (or any equivalent
test-double detection) is reintroduced anywhere in production code, so the
pattern cannot silently drift back the way it did for 70+ days.

The scan is AST-based (not regex) so strings, comments, and docstrings that
merely mention ``Mock`` or ``unittest.mock`` do not trip it — only real imports
and real ``isinstance(..., Mock)`` collaborator checks do. Offending
``file:line`` locations are named in the assertion message so a recurrence is
immediately diagnosable.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "theforge"

# Names that, when used as the second argument to isinstance(), indicate the
# code is checking whether a collaborator is a unittest.mock test double.
_MOCK_TYPE_NAMES = {"Mock", "MagicMock", "NonCallableMock", "NonCallableMagicMock", "AsyncMock"}


def _iter_src_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _could_violate(text: str) -> bool:
    """True if *text* could possibly hold a violation, so it is worth parsing.

    This only decides whether to *look*; everything that passes is still checked
    through the AST, so the "strings and docstrings do not trip it" guarantee
    above is untouched. The conditions are exact rather than heuristic: an
    offending import always spells ``unittest``, and an offending isinstance
    check always spells both ``isinstance`` and one of the mock type names.

    Worth the four lines because parsing all of ``src/`` twice per call cost
    0.88s, and under the five-second per-test convention this guard was
    crossing it once every xdist worker had to re-pay the scan.
    """
    if "unittest" in text:
        return True
    return "isinstance" in text and any(name in text for name in _MOCK_TYPE_NAMES)


def _import_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (lineno, message) for any ``unittest.mock`` import."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "unittest.mock" or alias.name.startswith("unittest.mock."):
                    out.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "unittest.mock" or module.startswith("unittest.mock."):
                names = ", ".join(a.name for a in node.names)
                out.append((node.lineno, f"from {module} import {names}"))
            elif module == "unittest" and any(a.name == "mock" for a in node.names):
                out.append((node.lineno, "from unittest import mock"))
    return out


def _isinstance_mock_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return (lineno, message) for any ``isinstance(x, Mock)`` check."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "isinstance" or len(node.args) < 2:
            continue
        second = node.args[1]
        # isinstance(x, Mock)
        if isinstance(second, ast.Name) and second.id in _MOCK_TYPE_NAMES:
            out.append((node.lineno, f"isinstance(..., {second.id})"))
        # isinstance(x, mock.Mock) / unittest.mock.MagicMock
        elif isinstance(second, ast.Attribute) and second.attr in _MOCK_TYPE_NAMES:
            out.append((node.lineno, f"isinstance(..., {second.attr})"))
        # isinstance(x, (Mock, ...)) tuple form
        elif isinstance(second, ast.Tuple):
            for elt in second.elts:
                if isinstance(elt, ast.Name) and elt.id in _MOCK_TYPE_NAMES:
                    out.append((node.lineno, f"isinstance(..., {elt.id})"))
                elif isinstance(elt, ast.Attribute) and elt.attr in _MOCK_TYPE_NAMES:
                    out.append((node.lineno, f"isinstance(..., {elt.attr})"))
    return out


def test_no_unittest_mock_or_mock_isinstance_in_src() -> None:
    """No production module may import unittest.mock or branch on isinstance(_, Mock)."""
    offenders: list[str] = []
    for path in _iter_src_files():
        text = path.read_text(encoding="utf-8")
        if not _could_violate(text):
            continue
        tree = ast.parse(text, filename=str(path))
        rel = path.relative_to(REPO_ROOT)
        for lineno, what in _import_violations(tree) + _isinstance_mock_violations(tree):
            offenders.append(f"{rel}:{lineno}: {what}")

    assert not offenders, (
        "Production code under src/ must not detect test doubles "
        "(no unittest.mock import, no isinstance(_, Mock) on a collaborator). "
        "This shim varies shipped behavior under test and hides real semantics "
        "(see #1737). Offending locations:\n  " + "\n  ".join(offenders)
    )
