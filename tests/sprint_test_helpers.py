"""Shared helper for driving ``run_sprint`` from tests.

``run_sprint`` takes a :class:`SprintRunContext` and nothing else (#2399), so
every test that starts a sprint builds one. Building it here keeps the call
sites reading as "run this sprint with these options" while still exercising
the real entrypoint: the context is constructed by the same
``SprintRunContext.for_sprint`` the CLI and the daemon use, including its
manifest resolution and liveness-set normalisation.

The runner is looked up on the module at call time rather than imported once,
so a test that patches ``theforge.sprint.runner.run_sprint`` still sees its
patch take effect.
"""

from __future__ import annotations

from typing import Any

from theforge.sprint import runner as _runner
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.runner import SprintRunContext


def stub_resolved(name: str = "sprint", budget_usd: float = 10.0) -> ResolvedSprint:
    """An empty resolved sprint, for tests that patch the runner out entirely.

    The CLI and the daemon resolve the manifest while building the context, so a
    test that mocks ``run_sprint`` still needs manifest resolution to produce
    something. Patch ``theforge.sprint.runner.resolve_from_manifest`` with this.
    """
    return ResolvedSprint(name=name, budget_usd=budget_usd, stories=[])


def make_run_context(config: Any, sprint: Any, **kwargs: Any) -> SprintRunContext:
    """Build the run context a sprint invocation with these options would use."""
    return SprintRunContext.for_sprint(config, sprint, **kwargs)


def run_sprint_ctx(config: Any, sprint: Any, **kwargs: Any) -> Any:
    """Run a sprint, building its context from loose options first."""
    return _runner.run_sprint(make_run_context(config, sprint, **kwargs))
