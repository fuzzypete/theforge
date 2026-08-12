"""Structural guards on ``sprint/runner.py``'s execution state (#2399).

ADR-0008 sequences every further extraction from ``run_sprint`` behind one
property: the sprint's execution state has to be reachable *as an object*, so
moving a function out of ``run_sprint`` is a move rather than a re-decision
about which fifteen frame locals to thread into its new home.

That property is not visible in a behavioural test — a sprint runs identically
whether its state lives in a frame or in an object — so it is asserted here,
against the parsed module. A later change that re-captures execution state
through the frame, or that hands a relocated function the *members* of the
state instead of the state, fails these tests instead of being noticed by
inspection.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import symtable
from pathlib import Path

import pytest

from theforge.sprint import runner as runner_mod
from theforge.sprint.runner import SprintExecutionState, SprintRunContext, run_sprint

# The five closures ADR-0008 names.
ADR_0008_CLOSURES = (
    "_attempt_integration",
    "_persist_current_story_result",
    "_record_dropped_story_audit",
    "_resolve_queued_pr",
    "_service_plan_gates",
)

# The name ``run_sprint`` binds its execution state to. Capturing *this* is the
# whole point; capturing anything it carries is what may not happen.
STATE_VAR = "_sprint_state"

# ``context`` is excluded deliberately: the frozen SprintRunContext is the other
# sanctioned carrier (a sprint consults it and never writes it), so reading it
# through the frame is not the condition this issue removes.
CARRIED_BY_STATE = frozenset(
    f.name for f in dataclasses.fields(SprintExecutionState) if f.name != "context"
)


@pytest.fixture(scope="module")
def runner_source() -> str:
    return Path(inspect.getfile(runner_mod)).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def runner_tree(runner_source: str) -> ast.Module:
    return ast.parse(runner_source)


def _run_sprint_node(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_sprint":
            return node
    raise AssertionError("run_sprint is not a module-scope function in runner.py")


def _scope_for(table: symtable.SymbolTable, name: str, lineno: int) -> symtable.SymbolTable:
    for child in table.get_children():
        if child.get_name() == name and child.get_lineno() == lineno:
            return child
        found = _scope_for(child, name, lineno)
        if found is not None:
            return found
    return None  # type: ignore[return-value]


def _nested_function_scopes(
    source: str, tree: ast.Module
) -> list[tuple[str, symtable.SymbolTable]]:
    """Every function scope nested inside ``run_sprint``, with a readable name."""
    table = symtable.symtable(source, "runner.py", "exec")
    node = _run_sprint_node(tree)
    root = _scope_for(table, "run_sprint", node.lineno)
    assert root is not None

    found: list[tuple[str, symtable.SymbolTable]] = []

    def walk(scope: symtable.SymbolTable, path: str) -> None:
        for child in scope.get_children():
            label = f"{path}.{child.get_name()}:{child.get_lineno()}"
            if child.get_type() == "function":
                found.append((label, child))
            walk(child, label)

    walk(root, "run_sprint")
    assert found, "run_sprint has no nested functions — this guard would pass vacuously"
    return found


def test_the_five_adr_0008_closures_are_module_scope() -> None:
    """Each is defined at module scope — not nested inside run_sprint."""
    for name in ADR_0008_CLOSURES:
        fn = getattr(runner_mod, name, None)
        assert fn is not None, f"{name} is not a module-scope name in sprint.runner"
        assert inspect.isfunction(fn), f"{name} is not a module-scope function"


def test_no_nested_function_closes_over_execution_state(
    runner_source: str, runner_tree: ast.Module
) -> None:
    """Functions may stay nested; capturing state through the frame may not.

    Anything still nested is nested by preference and can be moved later without
    re-deciding anything, which is what "every subsequent extraction is
    comparatively cheap" has to mean to be checkable.
    """
    assert CARRIED_BY_STATE, "the execution state carries nothing — nothing to guard"
    offenders: list[str] = []
    for label, scope in _nested_function_scopes(runner_source, runner_tree):
        captured = sorted(
            sym.get_name()
            for sym in scope.get_symbols()
            if sym.is_free() and sym.get_name() in CARRIED_BY_STATE
        )
        if captured:
            offenders.append(f"{label} captures {captured}")
    assert not offenders, (
        "these nested functions close over values carried by SprintExecutionState; "
        f"read them through `{STATE_VAR}` instead: " + "; ".join(offenders)
    )


def test_run_sprint_does_not_alias_execution_state_members(runner_tree: ast.Module) -> None:
    """No frame local is bound to a member of the state.

    An alias is frame capture wearing a different name: ``_stop = state.stop``
    followed by a closure over ``_stop`` re-creates exactly the coupling the
    previous test forbids, while passing it.
    """
    node = _run_sprint_node(runner_tree)
    offenders: list[str] = []
    for stmt in ast.walk(node):
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            targets = [stmt.target]
        else:
            continue
        value = stmt.value
        if not isinstance(value, ast.Attribute):
            continue
        if not (isinstance(value.value, ast.Name) and value.value.id == STATE_VAR):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                offenders.append(f"line {stmt.lineno}: {target.id} = {STATE_VAR}.{value.attr}")
    assert not offenders, "execution state aliased back into run_sprint's frame: " + "; ".join(
        offenders
    )


def _state_taking_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    out = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        args = node.args.posonlyargs + node.args.args
        if not args:
            continue
        annotation = args[0].annotation
        name = getattr(annotation, "id", None) or getattr(annotation, "value", None)
        if isinstance(annotation, ast.Constant):
            name = annotation.value
        if name == "SprintExecutionState":
            out.append(node)
    return out


def test_relocated_helpers_take_the_state_not_its_members(runner_tree: ast.Module) -> None:
    """A relocated function takes the state plus its own per-call inputs.

    Passing members of the state individually is the parameter threading this
    change exists to end, so it does not satisfy the extraction either.
    """
    functions = _state_taking_functions(runner_tree)
    assert functions, "no module-scope function takes a SprintExecutionState"
    offenders: list[str] = []
    for node in functions:
        params = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for param in params[1:]:
            if param.arg in CARRIED_BY_STATE:
                offenders.append(f"{node.name}(..., {param.arg}=...)")
    assert not offenders, (
        "these relocated helpers take members of the execution state as parameters; "
        "take the state instead: " + "; ".join(offenders)
    )


def test_the_five_closures_take_the_state(runner_tree: ast.Module) -> None:
    """Each of the five reads the sprint's state through the state object."""
    relocated = {node.name for node in _state_taking_functions(runner_tree)}
    missing = [name for name in ADR_0008_CLOSURES if name not in relocated]
    assert not missing, f"not declared as taking SprintExecutionState: {missing}"


def test_run_sprint_takes_the_run_context() -> None:
    """The entrypoint takes the context, not seventeen of its fields."""
    params = list(inspect.signature(run_sprint).parameters.values())
    assert len(params) <= 3, f"run_sprint takes {len(params)} parameters: {params}"
    assert params[0].annotation in (SprintRunContext, "SprintRunContext")


def test_execution_state_is_constructible_from_a_context_alone() -> None:
    """The state a sprint would have can be built — and asserted on — directly."""
    context = SprintRunContext(
        config=object(),  # type: ignore[arg-type]
        resolved=runner_mod.ResolvedSprint(name="s", budget_usd=1.0, stories=[]),
    )
    state = SprintExecutionState.for_run(context)
    assert state.context is context
    assert state.dag is None
    assert state.cost.snapshot().spent == 0.0
    assert not state.stop.stopped
