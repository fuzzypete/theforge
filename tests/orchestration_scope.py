"""Which tests drive a real sprint, decided from the test source itself.

A test that starts a sprint pays for production machinery — process spawns, git
operations, the scheduler — and under the gate's own ``-n auto --dist worksteal``
contention that cost inflates several-fold. Those tests need a bound above the
shared five seconds. The question this module answers is *which* tests those
are, and it answers it from a durable property of the test — it calls the sprint
entrypoint — rather than from a list of the tests someone once watched time out.
A list built by observation is complete only up to the last red gate; each test
it is missing announces itself by failing a release cut.

Classification is structural, not textual. The module is parsed and only
executable ``Call`` nodes count, so a docstring, a comment, or a string naming
``run_sprint_ctx`` classifies nothing. Within a module the relation is
transitive: a test that calls a local helper that runs a sprint drives a sprint
too, and so does one that requests a module-local fixture that runs one
(pytest-timeout runs with ``func_only=False``, so fixture cost is charged to the
test). Import aliases are followed.

What this cannot see is machinery reached through another module — a fixture in
a conftest, or an equivalent entrypoint under a different name. That residue is
what ``@pytest.mark.orchestration`` is for, and it is deliberately small: see
``tests/timeout_enforcement.py``.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

#: Calling either of these means the test runs a real sprint: ``run_sprint`` is
#: the production entrypoint and ``run_sprint_ctx`` is the shared test helper
#: that builds its context and calls it (``tests/sprint_test_helpers.py``).
ORCHESTRATION_ENTRYPOINTS = frozenset({"run_sprint", "run_sprint_ctx"})


def _called_names(node: ast.AST) -> set[str]:
    """Names invoked anywhere inside *node*, from executable call nodes only.

    Both ``f(...)`` and ``mod.f(...)`` contribute ``f``: what matters is the
    callable reached, not how the module happened to spell its import. Nested
    definitions are walked with the body that encloses them, because a closure
    a test defines is part of what that test runs.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = node.args
    return {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}


def _entrypoint_names(tree: ast.Module) -> set[str]:
    """The entrypoints plus any local alias an import bound them to."""
    names = set(ORCHESTRATION_ENTRYPOINTS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in ORCHESTRATION_ENTRYPOINTS and alias.asname:
                    names.add(alias.asname)
    return names


def _module_definitions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Top-level and class-body functions, keyed by name.

    Nested functions are deliberately excluded as graph nodes: they are already
    covered by the body that defines them, and a locally shadowed name would
    otherwise displace the module-level definition of the same name.
    """
    defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defs.setdefault(member.name, member)
    return defs


def _orchestrating(tree: ast.Module) -> frozenset[str]:
    entrypoints = _entrypoint_names(tree)
    defs = _module_definitions(tree)
    calls = {name: _called_names(node) for name, node in defs.items()}
    requires = {name: _parameter_names(node) for name, node in defs.items()}

    found = {name for name, called in calls.items() if called & entrypoints}
    changed = True
    while changed:
        changed = False
        for name in defs:
            if name in found:
                continue
            if (calls[name] | requires[name]) & found:
                found.add(name)
                changed = True
    return frozenset(found)


@lru_cache(maxsize=None)
def orchestrating_functions(path: str) -> frozenset[str]:
    """Names of functions in the module at *path* that drive a real sprint.

    The textual pre-check is a cheap way to skip parsing the ~10,000-test
    suite's modules that cannot possibly qualify — a mention is necessary for a
    call, never sufficient for one, so nothing is classified by it.
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    if not any(name in source for name in ORCHESTRATION_ENTRYPOINTS):
        return frozenset()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    return _orchestrating(tree)


def drives_a_sprint(item) -> bool:
    """True if the collected *item* is a test classified as sprint-driving."""
    path = getattr(item, "path", None)
    if path is None:
        return False
    name = getattr(item, "originalname", None) or str(getattr(item, "name", "")).partition("[")[0]
    if not name:
        return False
    return name in orchestrating_functions(str(path))
