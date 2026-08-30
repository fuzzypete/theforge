"""Which tests pay for real machinery, decided from the test source itself.

Some tests cost what the machinery they drive costs, not what their own logic
costs: a sprint through the real scheduler, a real subprocess, a real git
repository, a walk over the repository's own source tree. Under the gate's
``-n auto --dist worksteal`` contention that cost inflates several-fold — a
1.06s test was measured reaching 5s, and 1.1-1.2s source-scanning guards were
measured failing the shared bound on a machine at load 12 — so those tests need
a bound above the shared five seconds.

The question this module answers is *which* tests those are, and it answers it
from a durable property of the test rather than from a list of the tests someone
watched time out. A list is complete only up to the last red gate: each test it
is missing announces itself by failing a release cut, and a re-run turns that
cut green (#2825).

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
_SPRINT_CALLS = frozenset({"run_sprint", "run_sprint_ctx"})

#: Calling any of these starts a real process — the git invocations behind a
#: real repository fixture, a fake-binary runner lifecycle, a child pytest.
#: Qualified on the receiver (``subprocess.run``, not ``run``) so the ordinary
#: ``.run(...)`` of some object in a fully-mocked test is not swept in; the bare
#: names are the ``from subprocess import ...`` spelling of the same calls.
_PROCESS_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.check_output",
        "subprocess.check_call",
        "subprocess.call",
        "os.system",
        "Popen",
        "check_output",
        "check_call",
    }
)

#: Enumerating a directory. On its own this says nothing — most of them run over
#: a ``tmp_path`` holding three files — so it counts only when the thing being
#: enumerated is the repository's own tree (see ``_repo_rooted_names``). That is
#: the mechanical guards: each parses every module under ``tests/`` or ``src/``,
#: thousands of files, on every run.
_TREE_WALK_ATTRS = frozenset({"glob", "rglob", "iterdir", "walk"})

#: Every call that puts a test in the category, by the reason it does. The
#: tree walk is not here: it is a call *plus* what it is called on.
ORCHESTRATION_CALLS: dict[str, frozenset[str]] = {
    "sprint": _SPRINT_CALLS,
    "process": _PROCESS_CALLS,
}

#: Substrings that must appear in a module's text before any of the above can be
#: a call in it. A cheap way to skip parsing the modules that cannot qualify; a
#: mention is necessary for a call and never sufficient for one, so nothing is
#: classified by this.
_PREFILTER = (
    "run_sprint",
    "subprocess",
    "os.system",
    "Popen",
    "check_output",
    "check_call",
    # A repository tree walk needs a path derived from __file__ to walk.
    "__file__",
)


def _called_names(node: ast.AST) -> set[str]:
    """Names invoked anywhere inside *node*, from executable call nodes only.

    An attribute call contributes both spellings — ``f`` for ``mod.f(...)`` and
    ``mod.f`` — so a rule can require the receiver where the bare name would be
    ambiguous and ignore it where it would not. Nested definitions are walked
    with the body that encloses them, because a closure a test defines is part
    of what that test runs.
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
            if isinstance(func.value, ast.Name):
                names.add(f"{func.value.id}.{func.attr}")
    return names


def _repo_rooted_names(tree: ast.Module) -> set[str]:
    """Names bound to a path derived from ``__file__``, anywhere in the module.

    ``REPO_ROOT = Path(__file__).resolve().parents[1]`` and everything computed
    from it (``TESTS_ROOT = REPO_ROOT / "tests"``). Reached transitively, so the
    chain a module happens to spell out does not change the answer, and taken
    from function bodies as well as module level, because half the mechanical
    guards in this suite compute the root inside the test that walks it.
    """
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            mentioned = {child.id for child in ast.walk(node.value) if isinstance(child, ast.Name)}
            if "__file__" not in mentioned and not (mentioned & found):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in found:
                    found.add(target.id)
                    changed = True
    return found


def _walks_repo_tree(node: ast.AST, repo_rooted: set[str]) -> bool:
    """True if *node* enumerates a directory rooted in the repository itself."""
    if not repo_rooted:
        return False
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute) and func.attr in _TREE_WALK_ATTRS:
            # ``TESTS_ROOT.glob(...)`` and ``(REPO_ROOT / "src").rglob(...)``
            # alike: what matters is that the receiver came from __file__.
            receiver = func.value
        elif isinstance(func, ast.Name) and func.id == "walk" and child.args:
            receiver = child.args[0]
        elif isinstance(func, ast.Attribute) and func.attr == "walk" and child.args:
            receiver = child.args[0]
        else:
            continue
        if any(
            isinstance(inner, ast.Name) and inner.id in repo_rooted for inner in ast.walk(receiver)
        ):
            return True
    return False


def _parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = node.args
    return {arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)}


def _entrypoint_names(tree: ast.Module) -> set[str]:
    """Every classifying call, plus any local alias an import bound one to."""
    names = {name for calls in ORCHESTRATION_CALLS.values() for name in calls}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in names and alias.asname:
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


def _driving(tree: ast.Module) -> frozenset[str]:
    entrypoints = _entrypoint_names(tree)
    repo_rooted = _repo_rooted_names(tree)
    defs = _module_definitions(tree)
    calls = {name: _called_names(node) for name, node in defs.items()}
    requires = {name: _parameter_names(node) for name, node in defs.items()}

    found = {
        name
        for name, node in defs.items()
        if calls[name] & entrypoints or _walks_repo_tree(node, repo_rooted)
    }
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
def machinery_driving_functions(path: str) -> frozenset[str]:
    """Names of functions in the module at *path* whose cost is real machinery."""
    try:
        source = Path(path).read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    if not any(token in source for token in _PREFILTER):
        return frozenset()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    return _driving(tree)


def drives_real_machinery(item) -> bool:
    """True if the collected *item* is a test classified by the rule above."""
    path = getattr(item, "path", None)
    if path is None:
        return False
    name = getattr(item, "originalname", None) or str(getattr(item, "name", "")).partition("[")[0]
    if not name:
        return False
    return name in machinery_driving_functions(str(path))
