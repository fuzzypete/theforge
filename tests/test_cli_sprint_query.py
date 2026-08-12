"""Tests for sprint query-mode CLI dry-run dependency output."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import yaml

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
from theforge.sprint.audit import (
    persist_accumulated_story_state,
    persist_accepted_unmeasured_spend,
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
        plan_agent_review=PlanAgentReviewConfig.of(enabled=False),
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


def _set_existing_sprint_id(
    tmp_path: Path,
    sprint_name: str = "v0.5.0",
    sprint_id: str = "sprint-123",
) -> str:
    sprint_dir = tmp_path / ".forge" / "logs" / sprint_name
    sprint_dir.mkdir(parents=True, exist_ok=True)
    (sprint_dir / ".sprint_id").write_text(sprint_id, encoding="utf-8")
    return sprint_id


def _write_prior_sprint_audit(tmp_path: Path, sprint_id: str, total_cost_usd: float) -> None:
    audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        yaml.safe_dump(
            {
                "sprint": {
                    "sprint_id": sprint_id,
                    "total_cost_usd": total_cost_usd,
                    "cost_complete": True,
                }
            }
        ),
        encoding="utf-8",
    )


def _write_incomplete_prior_sprint_audit(
    tmp_path: Path,
    sprint_id: str,
    *,
    total_cost_measured_usd: float,
    unmeasured_spend_sources: list[str],
) -> None:
    audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        yaml.safe_dump(
            {
                "sprint": {
                    "sprint_id": sprint_id,
                    "total_cost_usd": None,
                    "total_cost_measured_usd": total_cost_measured_usd,
                    "cost_complete": False,
                    "unmeasured_spend_sources": unmeasured_spend_sources,
                }
            }
        ),
        encoding="utf-8",
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

    def test_query_mode_dry_run_reports_downstream_blockage(self, tmp_path: Path, capsys) -> None:
        config = _make_forge_config(tmp_path)
        args = _make_query_args(tmp_path, parallel=2)

        resolved = ResolvedSprint(
            name="v0.5.0",
            budget_usd=10.0,
            stories=[
                (
                    TaskStory(
                        name="A",
                        slug="issue-1",
                        github_issue=1,
                        depends_on=["issue-9"],
                        inferred_dependencies=["issue-9"],
                    ),
                    GitHubIssueSource(),
                    "issue:1",
                ),
                (
                    TaskStory(
                        name="C",
                        slug="issue-3",
                        github_issue=3,
                        depends_on=["issue-1"],
                        inferred_dependencies=["issue-1"],
                    ),
                    GitHubIssueSource(),
                    "issue:3",
                ),
            ],
            max_parallel=2,
        )

        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.sprint.query.fetch_issues_for_milestone",
                return_value=[{"number": 1, "title": "A"}, {"number": 3, "title": "C"}],
            ),
            patch("theforge.sprint.query.build_resolved_sprint", return_value=resolved),
            patch("theforge.sprint.dag._is_branch_merged", return_value=False),
        ):
            rc = cmd_sprint(args)

        out = capsys.readouterr().out
        assert rc == 0
        assert "issue-1" in out
        assert "blocked=[issue-9]" in out
        assert "issue-3" in out
        assert "blocked=[issue-1]" in out

    def test_query_mode_dry_run_prints_budget_carry_and_headroom(
        self, tmp_path: Path, capsys
    ) -> None:
        config = _make_forge_config(tmp_path)
        args = _make_query_args(tmp_path, parallel=2)
        args.resume = True
        sprint_id = _set_existing_sprint_id(tmp_path)
        _write_prior_sprint_audit(tmp_path, sprint_id, 64.56)

        resolved = ResolvedSprint(
            name="v0.5.0",
            budget_usd=150.0,
            stories=[
                (
                    TaskStory(name="A", slug="issue-1", github_issue=1),
                    GitHubIssueSource(),
                    "issue:1",
                )
            ],
            max_parallel=2,
        )

        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.sprint.query.fetch_issues_for_milestone",
                return_value=[{"number": 1, "title": "A"}],
            ),
            patch("theforge.sprint.query.build_resolved_sprint", return_value=resolved),
        ):
            rc = cmd_sprint(args)

        out = capsys.readouterr().out
        assert rc == 0
        assert "budget=$150.00 carried=$64.56 usable_headroom=$85.44" in out

    def test_query_mode_dry_run_ignores_existing_spend_without_resume(
        self, tmp_path: Path, capsys
    ) -> None:
        config = _make_forge_config(tmp_path)
        args = _make_query_args(tmp_path, parallel=2)
        sprint_id = _set_existing_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "v0.5.0",
            tmp_path,
            [
                {
                    "canonical_ref": "issue:1",
                    "slug": "issue-1",
                    "path": "issue-1.md",
                    "outcome": "DONE",
                    "cost_usd": 64.56,
                    "story_run_id": "run-prev",
                }
            ],
        )

        resolved = ResolvedSprint(
            name="v0.5.0",
            budget_usd=50.0,
            stories=[
                (
                    TaskStory(name="A", slug="issue-1", github_issue=1),
                    GitHubIssueSource(),
                    "issue:1",
                )
            ],
            max_parallel=2,
        )

        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.sprint.query.fetch_issues_for_milestone",
                return_value=[{"number": 1, "title": "A"}],
            ),
            patch("theforge.sprint.query.build_resolved_sprint", return_value=resolved),
        ):
            rc = cmd_sprint(args)

        out = capsys.readouterr().out
        assert rc == 0
        assert "budget=$50.00 carried=$0.00 usable_headroom=$50.00" in out
        assert "cannot dispatch under the supplied ceiling" not in out

    def test_query_mode_dry_run_reports_accepted_unmeasured_ceiling(
        self, tmp_path: Path, capsys
    ) -> None:
        config = _make_forge_config(tmp_path)
        args = _make_query_args(tmp_path, parallel=2)
        args.resume = True
        sprint_id = _set_existing_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "v0.5.0",
            tmp_path,
            [
                {
                    "canonical_ref": "issue:2",
                    "slug": "issue-2",
                    "path": "issue-2.md",
                    "outcome": "DONE",
                    "cost_usd": 6.0,
                    "story_run_id": "run-prev",
                },
                {
                    "canonical_ref": "issue:1",
                    "slug": "issue-1",
                    "path": "issue-1.md",
                    "outcome": "FAILED",
                    "cost_usd": None,
                    "story_run_id": "run-prev",
                },
            ],
        )
        _write_incomplete_prior_sprint_audit(
            tmp_path,
            sprint_id,
            total_cost_measured_usd=6.0,
            unmeasured_spend_sources=["carried:issue-1"],
        )
        assert (
            persist_accepted_unmeasured_spend(
                sprint_id,
                "v0.5.0",
                tmp_path,
                [
                    {
                        "source": "issue-1",
                        "accepted_ceiling_usd": 4.5,
                        "accepted_at": "2026-08-08T00:00:00+00:00",
                    }
                ],
            )
            is True
        )

        resolved = ResolvedSprint(
            name="v0.5.0",
            budget_usd=20.0,
            stories=[
                (
                    TaskStory(name="A", slug="issue-1", github_issue=1),
                    GitHubIssueSource(),
                    "issue:1",
                )
            ],
            max_parallel=2,
        )

        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.sprint.query.fetch_issues_for_milestone",
                return_value=[{"number": 1, "title": "A"}],
            ),
            patch("theforge.sprint.query.build_resolved_sprint", return_value=resolved),
        ):
            rc = cmd_sprint(args)

        out = capsys.readouterr().out
        assert rc == 0
        assert "accepted_unmeasured_ceiling=$4.50" in out
        assert "usable_headroom=$9.50" in out
        assert "lower bound" not in out

    def test_query_mode_dry_run_marks_headroom_as_lower_bound_for_incomplete_prior_cost(
        self, tmp_path: Path, capsys
    ) -> None:
        config = _make_forge_config(tmp_path)
        args = _make_query_args(tmp_path, parallel=2)
        args.resume = True
        sprint_id = _set_existing_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "v0.5.0",
            tmp_path,
            [
                {
                    "canonical_ref": "issue:2",
                    "slug": "issue-2",
                    "path": "issue-2.md",
                    "outcome": "DONE",
                    "cost_usd": 6.0,
                    "story_run_id": "run-prev",
                },
                {
                    "canonical_ref": "issue:1",
                    "slug": "issue-1",
                    "path": "issue-1.md",
                    "outcome": "FAILED",
                    "cost_usd": None,
                    "story_run_id": "run-prev",
                },
            ],
        )
        _write_incomplete_prior_sprint_audit(
            tmp_path,
            sprint_id,
            total_cost_measured_usd=6.0,
            unmeasured_spend_sources=["carried:issue-1"],
        )

        resolved = ResolvedSprint(
            name="v0.5.0",
            budget_usd=20.0,
            stories=[
                (
                    TaskStory(name="A", slug="issue-1", github_issue=1),
                    GitHubIssueSource(),
                    "issue:1",
                )
            ],
            max_parallel=2,
        )

        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.sprint.query.fetch_issues_for_milestone",
                return_value=[{"number": 1, "title": "A"}],
            ),
            patch("theforge.sprint.query.build_resolved_sprint", return_value=resolved),
        ):
            rc = cmd_sprint(args)

        out = capsys.readouterr().out
        assert rc == 0
        assert "usable_headroom=$14.00" in out
        assert "(lower bound; carried unmeasured spend remains)" in out
        assert "cannot dispatch under the supplied ceiling" in out
