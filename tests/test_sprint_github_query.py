"""Tests for sprint/query.py — fetch functions, build_resolved_sprint, and CLI integration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.query import (
    _gh_api_paginate_issues,
    assign_dependency_batches,
    assign_dependency_batches_with_satisfied,
    build_resolved_sprint,
    fetch_issues_for_label,
    fetch_issues_for_milestone,
)
from theforge.sprint.sources import IssueClosedError
from theforge.task import TaskStory

# ── _gh_api_paginate_issues ───────────────────────────────────────────────────


class TestGhApiPaginateIssues:
    def test_returns_parsed_issues(self, tmp_path: Path) -> None:
        stdout = json.dumps({"number": 1, "title": "First"}) + "\n"
        stdout += json.dumps({"number": 2, "title": "Second"}) + "\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
            issues = _gh_api_paginate_issues("repos/{owner}/{repo}/issues?state=open", tmp_path)
        assert issues == [{"number": 1, "title": "First"}, {"number": 2, "title": "Second"}]

    def test_raises_on_gh_failure(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
            with pytest.raises(RuntimeError, match="gh api"):
                _gh_api_paginate_issues("repos/{owner}/{repo}/issues?state=open", tmp_path)

    def test_raises_on_malformed_json(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not-json\n", stderr="")
            with pytest.raises(RuntimeError, match="malformed JSON"):
                _gh_api_paginate_issues("repos/{owner}/{repo}/issues?state=open", tmp_path)

    def test_empty_output_returns_empty_list(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            issues = _gh_api_paginate_issues("repos/{owner}/{repo}/issues?state=open", tmp_path)
        assert issues == []

    def test_uses_paginate_flag(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _gh_api_paginate_issues("some/endpoint", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "--paginate" in cmd
        assert "gh" in cmd

    def test_jq_filter_excludes_pull_requests(self, tmp_path: Path) -> None:
        """The jq filter passed to gh api must contain a pull_request null check."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _gh_api_paginate_issues("some/endpoint", tmp_path)
        cmd = mock_run.call_args[0][0]
        # Find the --jq argument value
        jq_idx = cmd.index("--jq")
        jq_expr = cmd[jq_idx + 1]
        assert "pull_request" in jq_expr, "jq filter must exclude pull requests"


# ── fetch_issues_for_milestone ────────────────────────────────────────────────


class TestFetchIssuesForMilestone:
    def _make_milestone_result(self, number: int) -> str:
        return str(number)

    def _make_issue_result(self, issues: list[dict]) -> str:
        return "\n".join(json.dumps(i) for i in issues) + "\n"

    def test_returns_sorted_issues(self, tmp_path: Path) -> None:
        issues = [{"number": 5, "title": "E"}, {"number": 2, "title": "B"}]
        with patch("subprocess.run") as mock_run:
            # First call: milestone lookup; second call: issue list
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="3\n", stderr=""),
                MagicMock(
                    returncode=0,
                    stdout=self._make_issue_result(issues),
                    stderr="",
                ),
            ]
            result = fetch_issues_for_milestone("M3: Something", tmp_path)
        assert result == [{"number": 2, "title": "B"}, {"number": 5, "title": "E"}]

    def test_raises_when_milestone_not_found(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with pytest.raises(RuntimeError, match="not found"):
                fetch_issues_for_milestone("Nonexistent", tmp_path)

    def test_raises_on_gh_failure(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth error")
            with pytest.raises(RuntimeError):
                fetch_issues_for_milestone("M1", tmp_path)

    def test_empty_milestone_returns_empty_list(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="7\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            result = fetch_issues_for_milestone("Empty Milestone", tmp_path)
        assert result == []

    def test_numeric_milestone_prefers_exact_title_match(self, tmp_path: Path) -> None:
        """A digit-only milestone name should still resolve by exact title first."""
        issues = [{"number": 3, "title": "Third"}]
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="9\n", stderr=""),
                MagicMock(returncode=0, stdout=self._make_issue_result(issues), stderr=""),
            ]
            result = fetch_issues_for_milestone("42", tmp_path)

        assert mock_run.call_count == 2
        lookup_cmd = mock_run.call_args_list[0][0][0]
        issues_cmd = mock_run.call_args_list[1][0][0]
        assert "milestones" in lookup_cmd[-1]
        assert "milestone=9" in issues_cmd[-1]
        assert result == [{"number": 3, "title": "Third"}]

    def test_numeric_milestone_falls_back_to_id_when_title_missing(self, tmp_path: Path) -> None:
        """A digit-only milestone still works as an ID when no matching title exists."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            fetch_issues_for_milestone("7", tmp_path)

        issues_cmd = mock_run.call_args_list[1][0][0]
        endpoint = issues_cmd[-1]
        assert "milestone=7" in endpoint

    def test_numeric_milestone_lookup_failure_does_not_fall_back(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth error")
            with pytest.raises(RuntimeError, match="auth error"):
                fetch_issues_for_milestone("7", tmp_path)

        assert mock_run.call_count == 1


# ── fetch_issues_for_label ────────────────────────────────────────────────────


class TestFetchIssuesForLabel:
    def _make_issue_result(self, issues: list[dict]) -> str:
        return "\n".join(json.dumps(i) for i in issues) + "\n"

    def test_returns_sorted_issues(self, tmp_path: Path) -> None:
        issues = [{"number": 10, "title": "Ten"}, {"number": 3, "title": "Three"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=self._make_issue_result(issues),
                stderr="",
            )
            result = fetch_issues_for_label("sprint-ready", tmp_path)
        assert result == [{"number": 3, "title": "Three"}, {"number": 10, "title": "Ten"}]

    def test_raises_on_gh_failure(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="bad creds")
            with pytest.raises(RuntimeError):
                fetch_issues_for_label("sprint-ready", tmp_path)

    def test_label_is_url_encoded_in_endpoint(self, tmp_path: Path) -> None:
        """Labels with spaces or special chars are percent-encoded in the API endpoint."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            fetch_issues_for_label("my label", tmp_path)
        cmd = mock_run.call_args[0][0]
        endpoint = cmd[-1]
        assert "my%20label" in endpoint

    def test_empty_label_returns_empty_list(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = fetch_issues_for_label("no-issues-label", tmp_path)
        assert result == []


# ── build_resolved_sprint ─────────────────────────────────────────────────────


class TestBuildResolvedSprint:
    def _open_issue_json(self, number: int, title: str) -> str:
        return json.dumps({"title": title, "body": f"Body of #{number}", "state": "OPEN"})

    def test_builds_resolved_sprint_from_issues(self, tmp_path: Path) -> None:
        issues = [{"number": 1, "title": "First"}, {"number": 2, "title": "Second"}]
        side_effects = [
            MagicMock(returncode=0, stdout=self._open_issue_json(1, "First"), stderr=""),
            MagicMock(returncode=0, stdout="[]", stderr=""),
            MagicMock(returncode=0, stdout=self._open_issue_json(2, "Second"), stderr=""),
            MagicMock(returncode=0, stdout="[]", stderr=""),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            resolved = build_resolved_sprint(
                issues=issues,
                name="Test Sprint",
                budget_usd=10.0,
                max_parallel=None,
                project_root=tmp_path,
            )

        assert isinstance(resolved, ResolvedSprint)
        assert resolved.name == "Test Sprint"
        assert resolved.budget_usd == 10.0
        assert len(resolved.stories) == 2
        task0, _src0, ref0 = resolved.stories[0]
        assert task0.github_issue == 1
        assert ref0 == "issue:1"
        assert task0.inferred_dependencies == []

    def test_builds_resolved_sprint_with_inferred_blockers(self, tmp_path: Path) -> None:
        issues = [{"number": 2, "title": "Second"}]
        side_effects = [
            MagicMock(returncode=0, stdout=self._open_issue_json(2, "Second"), stderr=""),
            MagicMock(
                returncode=0,
                stdout=json.dumps([{"event": "blocked_by", "blocking_issue": {"number": 1}}]),
                stderr="",
            ),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            resolved = build_resolved_sprint(
                issues=issues,
                name="Test Sprint",
                budget_usd=10.0,
                max_parallel=2,
                project_root=tmp_path,
            )

        task, _src, _ref = resolved.stories[0]
        assert task.depends_on == ["issue-1"]
        assert task.inferred_dependencies == ["issue-1"]

    def test_skips_closed_issues_with_warning(self, tmp_path: Path, capsys) -> None:
        issues = [{"number": 1, "title": "Open"}, {"number": 2, "title": "Closed"}]
        side_effects = [
            MagicMock(returncode=0, stdout=self._open_issue_json(1, "Open"), stderr=""),
            MagicMock(returncode=0, stdout="[]", stderr=""),
            MagicMock(
                returncode=0,
                stdout=json.dumps({"title": "Closed", "body": "", "state": "CLOSED"}),
                stderr="",
            ),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            resolved = build_resolved_sprint(
                issues=issues,
                name="Sprint",
                budget_usd=5.0,
                max_parallel=None,
                project_root=tmp_path,
            )

        # Only the open issue should be in the sprint
        assert len(resolved.stories) == 1
        task, _src, ref = resolved.stories[0]
        assert task.github_issue == 1
        # Warning should be logged
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "#2" in captured.err

    def test_propagates_non_closure_fetch_errors(self, tmp_path: Path) -> None:
        """A transient fetch failure (auth, network) must raise, not silently skip."""
        issues = [{"number": 1, "title": "Issue"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth error")
            with pytest.raises(RuntimeError, match="gh issue view"):
                build_resolved_sprint(
                    issues=issues,
                    name="Sprint",
                    budget_usd=5.0,
                    max_parallel=None,
                    project_root=tmp_path,
                )

    def test_empty_issues_list_returns_sprint_with_no_stories(self, tmp_path: Path) -> None:
        resolved = build_resolved_sprint(
            issues=[],
            name="Empty Sprint",
            budget_usd=5.0,
            max_parallel=None,
            project_root=tmp_path,
        )
        assert resolved.stories == []
        assert resolved.name == "Empty Sprint"


class TestAssignDependencyBatches:
    def test_dependency_batches_reflect_blockers(self) -> None:
        tasks = [
            TaskStory(name="A", slug="issue-1"),
            TaskStory(name="B", slug="issue-2", depends_on=["issue-1"]),
            TaskStory(name="C", slug="issue-3"),
        ]

        batch_plan = assign_dependency_batches(tasks, max_parallel=2)

        assert batch_plan.assignments["issue-1"] == 0
        assert batch_plan.assignments["issue-3"] == 0
        assert batch_plan.assignments["issue-2"] == 1
        assert batch_plan.blocked == {}

    def test_assign_dependency_batches_blocks_unresolved_external_dependency(self) -> None:
        tasks = [
            TaskStory(name="B", slug="issue-2", depends_on=["issue-1"]),
            TaskStory(name="C", slug="issue-3"),
        ]

        batch_plan = assign_dependency_batches(tasks, max_parallel=2)

        assert batch_plan.assignments == {"issue-3": 0}
        assert batch_plan.blocked == {"issue-2": ["issue-1"]}

    def test_external_blocker_is_treated_as_satisfied_when_explicitly_provided(self) -> None:
        tasks = [
            TaskStory(name="B", slug="issue-2", depends_on=["issue-1"]),
            TaskStory(name="C", slug="issue-3"),
        ]

        batch_plan = assign_dependency_batches_with_satisfied(
            tasks,
            max_parallel=2,
            satisfied={"issue-1"},
        )

        assert batch_plan.assignments["issue-2"] == 0
        assert batch_plan.assignments["issue-3"] == 0
        assert batch_plan.blocked == {}

    def test_blockage_propagates_to_downstream_in_sprint_dependencies(self) -> None:
        tasks = [
            TaskStory(name="A", slug="issue-1", depends_on=["issue-9"]),
            TaskStory(name="B", slug="issue-2", depends_on=["issue-1"]),
            TaskStory(name="C", slug="issue-3"),
        ]

        batch_plan = assign_dependency_batches(tasks, max_parallel=2)

        assert batch_plan.assignments == {"issue-3": 0}
        assert batch_plan.blocked == {
            "issue-1": ["issue-9"],
            "issue-2": ["issue-1"],
        }

    def test_independent_tasks_share_frontier_when_max_parallel_is_smaller(self) -> None:
        tasks = [
            TaskStory(name="A", slug="issue-1"),
            TaskStory(name="B", slug="issue-2"),
            TaskStory(name="C", slug="issue-3"),
        ]

        batch_plan = assign_dependency_batches(tasks, max_parallel=2)

        assert batch_plan.assignments == {"issue-1": 0, "issue-2": 0, "issue-3": 0}
        assert batch_plan.blocked == {}


# ── run_sprint accepts ResolvedSprint ─────────────────────────────────────────


class TestRunSprintAcceptsResolvedSprint:
    """run_sprint() must work when given a ResolvedSprint directly (not a Path)."""

    def _make_config(self, tmp_path: Path):
        from theforge.config import (
            DEFAULT_DEV_PROFILE,
            DEFAULT_PREFLIGHT_PROFILE,
            DEFAULT_REVIEW_PROFILE,
            DEFAULT_VALIDATION,
            ForgeConfig,
            RetryPolicy,
            WorkspaceConfig,
        )

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
        )

    def _make_coordinator_result(self, success: bool = True, cost: float = 1.0):
        from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase

        state = CoordinatorState()
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = cost
        state.preflight_result = mock_preflight
        return CoordinatorResult(
            success=success,
            phase=Phase.DONE if success else Phase.ESCALATE,
            state=state,
            message="ok" if success else "escalated",
        )

    def test_accepts_resolved_sprint_and_runs(self, tmp_path: Path) -> None:
        from theforge.sprint.manifest import ResolvedSprint
        from theforge.sprint.runner import run_sprint
        from theforge.sprint.sources import FileSource

        story_file = tmp_path / "story.md"
        story_file.write_text("---\nname: My Story\nslug: my-story\n---\n# Content\n")

        source = FileSource()
        task = source.fetch(str(story_file.relative_to(tmp_path)), tmp_path)
        resolved = ResolvedSprint(
            name="Test Sprint",
            budget_usd=10.0,
            stories=[(task, source, "my-story.md")],
            max_parallel=1,
        )
        config = self._make_config(tmp_path)

        with patch(
            "theforge.sprint.runner.run_task", return_value=self._make_coordinator_result()
        ):
            with patch("theforge.sprint.runner._write_sprint_audit"):
                with patch("theforge.sprint.runner._write_sprint_summary"):
                    result = run_sprint(config, resolved)

        assert result.specs_total == 1
        assert result.specs_succeeded == 1
        assert result.name == "Test Sprint"

    def test_no_path_assumption_in_run_sprint(self, tmp_path: Path) -> None:
        """run_sprint with a ResolvedSprint must not touch the filesystem for manifest."""
        from theforge.sprint.manifest import ResolvedSprint
        from theforge.sprint.runner import run_sprint
        from theforge.sprint.sources import GitHubIssueSource
        from theforge.task import TaskStory

        task = TaskStory(name="Issue Story", slug="issue-42", github_issue=42)
        source = GitHubIssueSource()
        resolved = ResolvedSprint(
            name="GitHub Sprint",
            budget_usd=5.0,
            stories=[(task, source, "issue:42")],
            max_parallel=1,
        )
        config = self._make_config(tmp_path)

        with patch(
            "theforge.sprint.runner.run_task", return_value=self._make_coordinator_result()
        ):
            with patch("theforge.sprint.runner._write_sprint_audit"):
                with patch("theforge.sprint.runner._write_sprint_summary"):
                    result = run_sprint(config, resolved)

        assert result.specs_total == 1
        assert result.name == "GitHub Sprint"

    def test_unresolved_external_blocker_is_skipped_at_runtime(self, tmp_path: Path) -> None:
        from theforge.sprint.manifest import ResolvedSprint
        from theforge.sprint.runner import run_sprint
        from theforge.sprint.sources import GitHubIssueSource

        blocked = TaskStory(name="Blocked", slug="issue-2", github_issue=2, depends_on=["issue-1"])
        ready = TaskStory(name="Ready", slug="issue-3", github_issue=3)
        resolved = ResolvedSprint(
            name="GitHub Sprint",
            budget_usd=5.0,
            stories=[
                (blocked, GitHubIssueSource(), "issue:2"),
                (ready, GitHubIssueSource(), "issue:3"),
            ],
            max_parallel=1,
        )
        config = self._make_config(tmp_path)

        with (
            patch("theforge.sprint.dag._is_branch_merged", return_value=False),
            patch(
                "theforge.sprint.runner.run_task",
                return_value=self._make_coordinator_result(),
            ) as mock_run_task,
        ):
            result = run_sprint(config, resolved)

        assert result.specs_total == 2
        assert result.specs_succeeded == 1
        assert result.specs_skipped == 1
        assert mock_run_task.call_count == 1
        assert mock_run_task.call_args[0][1].slug == "issue-3"


# ── IssueClosedError is only what's skipped ──────────────────────────────────


class TestIssueClosedErrorDistinction:
    def test_issue_closed_error_is_runtime_error_subclass(self) -> None:
        err = IssueClosedError("issue #1 is already closed")
        assert isinstance(err, RuntimeError)

    def test_fetch_raises_issue_closed_error_for_closed_issue(self, tmp_path: Path) -> None:
        from theforge.sprint.sources import GitHubIssueSource

        closed_data = json.dumps({"title": "Done", "body": "", "state": "CLOSED"})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=closed_data, stderr="")
            with pytest.raises(IssueClosedError, match="already closed"):
                GitHubIssueSource().fetch("42", tmp_path)

    def test_fetch_raises_plain_runtime_error_for_auth_failure(self, tmp_path: Path) -> None:
        from theforge.sprint.sources import GitHubIssueSource

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="bad credentials")
            with pytest.raises(RuntimeError) as exc_info:
                GitHubIssueSource().fetch("42", tmp_path)
            # Must NOT be IssueClosedError
            assert type(exc_info.value) is not IssueClosedError


# ── closed_dependency_slugs — blocked deps are satisfied, not dropped ─────────


class TestClosedDependencySlugs:
    """Closed issues dropped from the manifest must be tracked as satisfied deps."""

    def _open_issue_json(self, number: int, title: str) -> str:
        return json.dumps({"title": title, "body": "", "state": "OPEN"})

    def test_closed_issue_in_sprint_populates_closed_dependency_slugs(
        self, tmp_path: Path
    ) -> None:
        """When a sprint issue is already closed, its slug lands in closed_dependency_slugs."""
        issues = [{"number": 265, "title": "Closed"}, {"number": 750, "title": "Open"}]
        side_effects = [
            MagicMock(
                returncode=0,
                stdout=json.dumps({"title": "Closed", "body": "", "state": "CLOSED"}),
                stderr="",
            ),
            MagicMock(
                returncode=0,
                stdout=self._open_issue_json(750, "Open"),
                stderr="",
            ),
            MagicMock(returncode=0, stdout="[]", stderr=""),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            resolved = build_resolved_sprint(
                issues=issues,
                name="Sprint",
                budget_usd=5.0,
                max_parallel=None,
                project_root=tmp_path,
            )

        assert "issue-265" in resolved.closed_dependency_slugs
        assert len(resolved.stories) == 1

    def test_no_closed_issues_leaves_closed_dependency_slugs_empty(self, tmp_path: Path) -> None:
        issues = [{"number": 1, "title": "Open"}]
        side_effects = [
            MagicMock(returncode=0, stdout=self._open_issue_json(1, "Open"), stderr=""),
            MagicMock(returncode=0, stdout="[]", stderr=""),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            resolved = build_resolved_sprint(
                issues=issues,
                name="Sprint",
                budget_usd=5.0,
                max_parallel=None,
                project_root=tmp_path,
            )

        assert resolved.closed_dependency_slugs == set()

    def test_downstream_dep_not_blocked_when_dep_issue_closed(self, tmp_path: Path) -> None:
        """Downstream story depending on a closed sprint issue must not be blocked.

        Regression: before the fix, build_resolved_sprint dropped the closed
        issue without recording it as satisfied, so normalize_dependency_plan
        left it as an unresolved issue-backed dep and the downstream story
        was permanently skipped.
        """
        from theforge.sprint.dag import resolve_satisfied_dependencies
        from theforge.sprint.query import normalize_dependency_plan

        # Issue 265 is closed and dropped; issue 750 depends on it.
        issues = [{"number": 265, "title": "Done"}, {"number": 750, "title": "Blocked?"}]
        dep_body = "depends_on: issue-265\n\nSome story text."
        side_effects = [
            # fetch #265 → closed
            MagicMock(
                returncode=0,
                stdout=json.dumps({"title": "Done", "body": "", "state": "CLOSED"}),
                stderr="",
            ),
            # fetch #750 → open
            MagicMock(
                returncode=0,
                stdout=json.dumps({"title": "Blocked?", "body": dep_body, "state": "OPEN"}),
                stderr="",
            ),
            # blockers query for #750
            MagicMock(returncode=0, stdout="[]", stderr=""),
        ]
        with patch("subprocess.run", side_effect=side_effects):
            resolved = build_resolved_sprint(
                issues=issues,
                name="Sprint",
                budget_usd=5.0,
                max_parallel=None,
                project_root=tmp_path,
            )

        assert "issue-265" in resolved.closed_dependency_slugs

        tasks = [task for task, _src, _ref in resolved.stories]
        # resolve_satisfied_dependencies must not need a live gh call because
        # issue-265 is already in pre_satisfied from closed_dependency_slugs.
        satisfied = resolve_satisfied_dependencies(
            tasks,
            project_root=tmp_path,
            base_branch="main",
            branch_pattern="feat/{slug}",
            pre_satisfied=resolved.closed_dependency_slugs,
        )
        assert "issue-265" in satisfied

        from theforge.sprint.dag import build_dag

        normalized = normalize_dependency_plan(tasks, satisfied=satisfied)
        assert normalized.blocked == {}

        # issue-265 stays in depends_on (satisfied external dep tracked for ordering),
        # but the DAG treats it as completed so issue-750 is immediately ready.
        dag = build_dag(normalized.tasks, satisfied=satisfied)
        ready_slugs = [t.slug for t in dag.ready()]
        assert "issue-750" in ready_slugs
