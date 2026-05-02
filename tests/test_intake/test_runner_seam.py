"""Seam-level tests for the runner intake injection helpers.

These cover the boundary between dependency normalization and batch preflight:
the filter helper that drops intake-rejected slugs from a normalized plan and
the helper that decides when to run the intake pass at all. They protect the
phase-boundary contract called out in convention 8 — cross-phase state flow
must have a seam-level test that doesn't rely on the full sprint pipeline.
"""

from __future__ import annotations

from theforge.intake import IntakeFinding, IntakeSeverity
from theforge.sprint.query import NormalizedDependencyPlan
from theforge.sprint.runner import _filter_normalized_for_intake, _intake_agent_caller_stub
from theforge.task import TaskStory


def _t(slug: str, deps: list[str] | None = None) -> TaskStory:
    return TaskStory(name=slug, slug=slug, depends_on=deps or [])


def test_filter_drops_slug_and_propagates_blocked():
    a = _t("a")
    b = _t("b", deps=["a"])
    c = _t("c")
    plan = NormalizedDependencyPlan(tasks=[a, b, c], blocked={})
    new = _filter_normalized_for_intake(plan, {"a"})
    assert [t.slug for t in new.tasks] == ["b", "c"]
    assert new.blocked["b"] == ["a"]


def test_filter_no_dropped_returns_input():
    plan = NormalizedDependencyPlan(tasks=[_t("a")], blocked={})
    assert _filter_normalized_for_intake(plan, set()) is plan


def test_filter_preserves_existing_blocked_entries():
    a = _t("a")
    b = _t("b", deps=["a"])
    plan = NormalizedDependencyPlan(tasks=[a, b], blocked={"b": ["preexisting"]})
    new = _filter_normalized_for_intake(plan, {"a"})
    assert "preexisting" in new.blocked["b"]
    assert "a" in new.blocked["b"]


def test_intake_agent_caller_stub_returns_none():
    findings = [
        IntakeFinding(
            code="missing_acceptance_criteria",
            severity=IntakeSeverity.BLOCK,
            location="acceptance_criteria",
            problem="no AC",
        )
    ]
    assert _intake_agent_caller_stub("body", findings) is None
