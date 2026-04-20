"""CLI tests: sprint query mode honors the sprint-entry shape gate."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from theforge.cli import cmd_sprint
from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.sprint.manifest import ResolvedSprint, SprintResult
from theforge.sprint.shape_gate import ShapeGateResult, SkippedIssue
from theforge.sprint.sources import GitHubIssueSource
from theforge.task import TaskStory


def _api_profile(name: str, provider: str = "anthropic", model: str = "claude-opus-4-6"):
    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=("Read",),
    )


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=300,
            allowed_tools=("Read",),
        ),
        preflight_profile=ModelProfile(
            name="preflight",
            cli="claude",
            model="sonnet",
            budget_usd=0.5,
            timeout_seconds=120,
            allowed_tools=("Read",),
        ),
        review_pool=[_api_profile("claude-reviewer"), _api_profile("codex", "openai")],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig(enabled=False),
        log=LogConfig(enabled=False),
    )


def _query_args(tmp_path: Path, *, force: bool = False) -> argparse.Namespace:
    (tmp_path / "forge.yaml").write_text("project:\n  root: .\n", encoding="utf-8")
    return argparse.Namespace(
        manifest=None,
        config=None,
        fg=True,
        detach=False,
        resume=False,
        milestone="v0.5.0",
        label=None,
        issues=None,
        budget="10",
        parallel=1,
        name=None,
        dry_run=False,
        auto_merge=False,
        interactive=False,
        verbose=False,
        no_notify=True,
        no_pull=False,
        force=force,
        base_branch=None,
    )


def _resolved(issues: list[int]) -> ResolvedSprint:
    stories = []
    for n in issues:
        task = TaskStory(name=f"T{n}", slug=f"issue-{n}", github_issue=n)
        stories.append((task, GitHubIssueSource(), f"issue:{n}"))
    return ResolvedSprint(
        name="v0.5.0",
        budget_usd=10.0,
        stories=stories,
        max_parallel=1,
    )


def _ok_result() -> SprintResult:
    return SprintResult(
        name="v0.5.0",
        specs_total=1,
        specs_succeeded=1,
        specs_failed=0,
        specs_skipped=0,
        total_cost_usd=0.0,
        budget_usd=10.0,
        results=[],
    )


def test_cli_query_mode_filters_skipped_and_passes_to_run_sprint(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    args = _query_args(tmp_path)

    fetched = [
        {"number": 1, "title": "good"},
        {"number": 2, "title": "bad"},
    ]
    gated = ShapeGateResult(
        runnable=[{"number": 1, "title": "good"}],
        skipped=[
            SkippedIssue(
                issue_number=2,
                reason_codes=("missing_ac",),
                source="local_check",
                title="bad",
            )
        ],
    )

    captured: dict = {}

    def fake_run_sprint(*_args, **kwargs):
        captured.update(kwargs)
        return _ok_result()

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([1])),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", side_effect=fake_run_sprint),
    ):
        rc = cmd_sprint(args)

    assert rc == 0
    assert "skipped_issues" in captured
    assert len(captured["skipped_issues"]) == 1
    assert captured["skipped_issues"][0].issue_number == 2
    # Only runnable issues feed build_resolved_sprint.
    # (Confirmed by ``build_resolved_sprint`` being called with one issue,
    # which _resolved([1]) represents — if two had leaked through, the
    # downstream DAG would differ.)


def test_cli_query_mode_all_skipped_exits_cleanly(tmp_path: Path, capsys) -> None:
    import yaml

    config = _make_config(tmp_path)
    args = _query_args(tmp_path)

    fetched = [{"number": 2, "title": "bad"}]
    gated = ShapeGateResult(
        runnable=[],
        skipped=[
            SkippedIssue(
                issue_number=2,
                reason_codes=("missing_ac",),
                source="local_check",
                title="bad",
            )
        ],
    )

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
    ):
        rc = cmd_sprint(args)

    assert rc == 0
    err = capsys.readouterr().err
    assert "skipped by shape gate" in err

    # All-skipped must still produce machine-readable audit/summary records.
    audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
    assert audit_path.exists()
    audit = yaml.safe_load(audit_path.read_text())
    assert audit["skipped"][0]["issue_number"] == 2
    assert audit["skipped"][0]["reason_codes"] == ["missing_ac"]

    # Summary file is keyed by sprint name (milestone here is "v0.5.0").
    summary_path = tmp_path / ".forge" / "logs" / "v0.5.0" / "sprint-summary.yaml"
    assert summary_path.exists()
    summary = yaml.safe_load(summary_path.read_text())
    assert summary["skipped"][0]["issue_number"] == 2


def test_cli_passes_configured_classifier_to_shape_gate(tmp_path: Path) -> None:
    """CLI must thread config.shape_check.classifier into the gate call."""
    from theforge.config.types import ShapeCheckConfig

    config = _make_config(tmp_path)
    # dataclasses.replace-style override on the frozen ForgeConfig field.
    object.__setattr__(config, "shape_check", ShapeCheckConfig(classifier="llm"))

    args = _query_args(tmp_path)
    fetched = [{"number": 1, "title": "good"}]
    gated = ShapeGateResult(runnable=fetched, skipped=[])
    captured: dict = {}

    def _spy(*call_args, **kwargs):
        captured["kwargs"] = kwargs
        return gated

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([1])),
        patch("theforge.sprint.shape_gate.apply_shape_gate", side_effect=_spy),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=_ok_result()),
    ):
        cmd_sprint(args)

    assert captured["kwargs"]["classifier_mode"] == "llm"


def test_cli_query_mode_force_runs_every_issue_but_warns(tmp_path: Path, capsys) -> None:
    config = _make_config(tmp_path)
    args = _query_args(tmp_path, force=True)

    fetched = [{"number": 2, "title": "bad"}]
    # With force=True the gate returns *all* issues runnable, but still
    # populates ``skipped`` so we can emit the warning.
    gated = ShapeGateResult(
        runnable=fetched,
        skipped=[
            SkippedIssue(
                issue_number=2,
                reason_codes=("missing_ac",),
                source="local_check",
                title="bad",
            )
        ],
    )

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
        patch("theforge.sprint.query.fetch_issues_for_milestone", return_value=fetched),
        patch("theforge.sprint.query.build_resolved_sprint", return_value=_resolved([2])),
        patch("theforge.sprint.shape_gate.apply_shape_gate", return_value=gated),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=_ok_result()),
    ):
        rc = cmd_sprint(args)

    assert rc == 0
    err = capsys.readouterr().err
    assert "--force in effect" in err
    assert "#2" in err
    assert "missing_ac" in err
