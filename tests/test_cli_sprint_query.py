"""Tests for sprint query-mode CLI dry-run dependency output."""

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
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.sources import GitHubIssueSource
from theforge.task import TaskStory


def _api_profile(
    name: str, provider: str = "anthropic", model: str = "claude-opus-4-6"
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        model=model,
        budget_usd=1.0,
        timeout_seconds=120,
        allowed_tools=("Read", "Grep"),
    )


def _make_forge_config(tmp_path: Path) -> ForgeConfig:
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
        review_pool=[_api_profile("claude-reviewer"), _api_profile("codex-reviewer", "openai")],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig(enabled=False),
        log=LogConfig(enabled=False),
    )


def _make_query_args(tmp_path: Path, *, parallel: int) -> argparse.Namespace:
    config_path = tmp_path / "forge.yaml"
    if not config_path.exists():
        config_path.write_text("project:\n  root: .\n", encoding="utf-8")
    return argparse.Namespace(
        manifest=None,
        config=None,
        fg=True,
        detach=False,
        resume=False,
        milestone="v0.5.0",
        label=None,
        budget="10",
        parallel=parallel,
        name=None,
        dry_run=True,
        auto_merge=False,
        interactive=False,
        verbose=False,
        no_notify=True,
        no_pull=False,
    )


class TestCmdSprintDryRunQuery:
    def test_query_dry_run_prints_dependency_batches(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)
        args = _make_query_args(tmp_path, parallel=2)

        resolved = ResolvedSprint(
            name="v0.5.0",
            budget_usd=10.0,
            stories=[
                (
                    TaskStory(name="A", slug="issue-1", github_issue=1),
                    GitHubIssueSource(),
                    "issue:1",
                ),
                (
                    TaskStory(
                        name="B",
                        slug="issue-2",
                        github_issue=2,
                        depends_on=["issue-1"],
                        inferred_dependencies=["issue-1"],
                    ),
                    GitHubIssueSource(),
                    "issue:2",
                ),
            ],
            max_parallel=2,
        )

        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.sprint.query.fetch_issues_for_milestone",
                return_value=[{"number": 1, "title": "A"}, {"number": 2, "title": "B"}],
            ),
            patch("theforge.sprint.query.build_resolved_sprint", return_value=resolved),
        ):
            rc = cmd_sprint(args)

        out = capsys.readouterr().out
        assert rc == 0
        assert "batch=0" in out
        assert "batch=1" in out
        assert "deps=[issue-1]" in out

    def test_query_mode_dry_run_marks_unresolved_external_blocker_as_blocked(
        self, tmp_path: Path, capsys
    ) -> None:
        config = _make_forge_config(tmp_path)
        args = _make_query_args(tmp_path, parallel=2)

        resolved = ResolvedSprint(
            name="v0.5.0",
            budget_usd=10.0,
            stories=[
                (
                    TaskStory(
                        name="B",
                        slug="issue-2",
                        github_issue=2,
                        depends_on=["issue-1"],
                        inferred_dependencies=["issue-1"],
                    ),
                    GitHubIssueSource(),
                    "issue:2",
                ),
            ],
            max_parallel=2,
        )

        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.sprint.query.fetch_issues_for_milestone",
                return_value=[{"number": 2, "title": "B"}],
            ),
            patch("theforge.sprint.query.build_resolved_sprint", return_value=resolved),
            patch("theforge.sprint.dag._is_branch_merged", return_value=False),
        ):
            rc = cmd_sprint(args)

        out = capsys.readouterr().out
        assert rc == 0
        assert "blocked=[issue-1]" in out
        assert "batch=" not in out
