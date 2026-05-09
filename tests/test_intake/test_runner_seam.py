"""Seam-level tests for the runner intake injection helpers.

These cover the boundary between dependency normalization and batch preflight:
the filter helper that drops intake-rejected slugs from a normalized plan and
the helper that decides when to run the intake pass at all. They protect the
phase-boundary contract called out in convention 8 — cross-phase state flow
must have a seam-level test that doesn't rely on the full sprint pipeline.
"""

from __future__ import annotations

from unittest.mock import patch

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.types import IntakeConfig
from theforge.sprint.query import NormalizedDependencyPlan
from theforge.sprint.runner import (
    _build_intake_agent_caller,
    _filter_normalized_for_intake,
    _run_intake_remediation_pass,
)
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


def test_build_intake_agent_caller_returns_none_when_profile_unavailable(tmp_path):
    config = ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )
    with patch("theforge.sprint.runner.check_agent_auth", return_value=(False, "auth missing")):
        caller, detail = _build_intake_agent_caller(config=config, log=lambda *_: None)

    assert caller is None
    assert "auth missing" in detail


def _config_with_intake(tmp_path, *, grooming: bool, auto_fix: bool) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        intake=IntakeConfig(grooming=grooming, auto_fix=auto_fix, auto_fix_mode="edit"),
    )


def test_run_intake_remediation_pass_force_short_circuits(tmp_path):
    config = _config_with_intake(tmp_path, grooming=True, auto_fix=True)
    tasks = [_t("alpha"), _t("beta")]
    tasks[0] = TaskStory(name="alpha", slug="alpha", github_issue=101)
    tasks[1] = TaskStory(name="beta", slug="beta", github_issue=102)

    log_lines: list[str] = []

    def fake_run(*_a, force=False, **_kw):
        # Real run_intake_remediation honors `force`; assert the pass forwards it.
        assert force is True
        return {"alpha": "fake", "beta": "fake"}

    with (
        patch("theforge.sprint.runner.run_intake_remediation", side_effect=fake_run) as patched,
        patch("theforge.sprint.runner._build_intake_agent_caller") as build_agent,
    ):
        outcomes = _run_intake_remediation_pass(
            config=config, tasks=tasks, log=log_lines.append, force=True
        )

    # Agent caller must NOT be wired when force is set — bypass means $0 of LLM spend.
    build_agent.assert_not_called()
    assert patched.call_count == 1
    _, kwargs = patched.call_args
    assert kwargs["force"] is True
    assert kwargs["agent_caller"] is None
    assert outcomes == {"alpha": "fake", "beta": "fake"}
    assert any("bypassed by --force" in line for line in log_lines)


def test_run_intake_remediation_pass_force_runs_even_when_disabled(tmp_path):
    """force=True must enter the pass even when grooming + auto_fix are off,
    so the bypass logging fires and the empty-passed outcomes are recorded.
    """
    config = _config_with_intake(tmp_path, grooming=False, auto_fix=False)
    task = TaskStory(name="x", slug="x", github_issue=9)
    log_lines: list[str] = []

    def fake_run(_tasks, _root, **kwargs):
        assert kwargs.get("force") is True
        return {"x": "ok"}

    with patch("theforge.sprint.runner.run_intake_remediation", side_effect=fake_run):
        outcomes = _run_intake_remediation_pass(
            config=config, tasks=[task], log=log_lines.append, force=True
        )

    assert outcomes == {"x": "ok"}
    assert any("bypassed by --force" in line for line in log_lines)
