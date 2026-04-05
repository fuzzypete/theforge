from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.cli.sprint import cmd_sprint
from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.sprint.query import fetch_issues_by_numbers


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


def _make_args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    config_path = tmp_path / "forge.yaml"
    if not config_path.exists():
        config_path.write_text("project:\n  root: .\n", encoding="utf-8")
    base = dict(
        manifest=None,
        config=None,
        fg=True,
        detach=False,
        resume=False,
        milestone=None,
        label=None,
        issues="403,405",
        budget="10",
        parallel=1,
        name=None,
        dry_run=True,
        auto_merge=False,
        interactive=False,
        verbose=False,
        no_notify=True,
        no_pull=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestFetchIssuesByNumbers:
    def test_fetches_and_sorts_requested_issues(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(
                    returncode=0, stdout=json.dumps({"number": 405, "title": "B"}), stderr=""
                ),
                MagicMock(
                    returncode=0, stdout=json.dumps({"number": 403, "title": "A"}), stderr=""
                ),
            ]
            issues = fetch_issues_by_numbers([405, 403], tmp_path)
        assert issues == [{"number": 403, "title": "A"}, {"number": 405, "title": "B"}]

    def test_raises_when_any_issue_is_missing(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(
                    returncode=0, stdout=json.dumps({"number": 403, "title": "A"}), stderr=""
                ),
                MagicMock(returncode=1, stdout="", stderr="Could not resolve to an issue"),
            ]
            try:
                fetch_issues_by_numbers([403, 405], tmp_path)
            except RuntimeError as exc:
                assert "405" in str(exc)
            else:
                raise AssertionError("Expected RuntimeError")


class TestCmdSprintIssuesValidation:
    def test_issues_and_milestone_are_mutually_exclusive(self, tmp_path: Path, capsys) -> None:
        args = _make_args(tmp_path, milestone="v1")
        rc = cmd_sprint(args)
        err = capsys.readouterr().err
        assert rc == 1
        assert "mutually exclusive" in err

    def test_issues_and_label_are_mutually_exclusive(self, tmp_path: Path, capsys) -> None:
        args = _make_args(tmp_path, label="bug")
        rc = cmd_sprint(args)
        err = capsys.readouterr().err
        assert rc == 1
        assert "mutually exclusive" in err

    def test_issues_and_manifest_are_mutually_exclusive(self, tmp_path: Path, capsys) -> None:
        args = _make_args(tmp_path, manifest="sprint.yaml")
        rc = cmd_sprint(args)
        err = capsys.readouterr().err
        assert rc == 1
        assert "mutually exclusive" in err

    def test_budget_required_with_issues(self, tmp_path: Path, capsys) -> None:
        args = _make_args(tmp_path, budget=None)
        rc = cmd_sprint(args)
        err = capsys.readouterr().err
        assert rc == 1
        assert "--budget <usd> is required" in err

    def test_invalid_issue_number_reports_cli_error(self, tmp_path: Path, capsys) -> None:
        args = _make_args(tmp_path, issues="403,abc")
        rc = cmd_sprint(args)
        err = capsys.readouterr().err
        assert rc == 1
        assert "--issues must be a comma-separated list of integer issue numbers" in err

    def test_query_mode_uses_requested_issue_numbers(self, tmp_path: Path) -> None:
        config = _make_forge_config(tmp_path)
        args = _make_args(tmp_path)
        with (
            patch("theforge.cli.sprint.load_config", return_value=config),
            patch("theforge.cli.sprint._find_config", return_value=tmp_path / "forge.yaml"),
            patch(
                "theforge.sprint.query.fetch_issues_by_numbers",
                return_value=[{"number": 403, "title": "A"}, {"number": 405, "title": "B"}],
            ) as mock_fetch,
            patch("theforge.sprint.query.build_resolved_sprint") as mock_build,
        ):
            mock_build.return_value = MagicMock(stories=[])
            rc = cmd_sprint(args)
        assert rc == 0
        mock_fetch.assert_called_once_with([403, 405], tmp_path)
