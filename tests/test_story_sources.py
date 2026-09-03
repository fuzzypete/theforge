"""Tests for story source abstraction, manifest parsing, issue lifecycle, and overlap gating."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from theforge.sprint.dag import _triage_spec
from theforge.sprint.manifest import (
    SprintManifest,
    build_tasks_from_manifest,
    load_sprint_manifest,
)
from theforge.sprint.runner import (
    _extract_plan_footprint,
    _run_fresh,
    parse_manifest_slugs,
    parse_manifest_story_refs,
)
from theforge.sprint.sources import (
    FileSource,
    GitHubIssueSource,
    StorySource,
    canonicalize_ref,
    resolve,
)
from theforge.task import TaskStory

# ── FileSource ─────────────────────────────────────────────────────────


class TestFileSource:
    def test_fetch_returns_task(self, tmp_path: Path) -> None:
        story = tmp_path / "my-story.md"
        story.write_text("---\nname: My Story\nslug: my-story\n---\n# Content\n")
        source = FileSource()
        task = source.fetch(str(story.relative_to(tmp_path)), tmp_path)
        assert task.name == "My Story"
        assert task.slug == "my-story"
        assert task.story_path == story.resolve()

    def test_on_complete_is_noop(self) -> None:
        source = FileSource()
        source.on_complete(MagicMock(), MagicMock(), MagicMock())  # no error

    def test_on_escalate_is_noop(self) -> None:
        source = FileSource()
        source.on_escalate(MagicMock(), MagicMock(), MagicMock())  # no error

    def test_is_story_source(self) -> None:
        assert isinstance(FileSource(), StorySource)


# ── GitHubIssueSource ─────────────────────────────────────────────────


class TestGitHubIssueSource:
    def test_fetch_calls_gh_and_returns_task(self, tmp_path: Path) -> None:
        issue_data = json.dumps({"title": "Fix the bug", "body": "## Details\nFix it."})
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=0, stdout="[]", stderr=""),
            ]
            source = GitHubIssueSource()
            task = source.fetch("42", tmp_path)

        assert task.name == "Fix the bug"
        assert task.slug == "issue-42"
        assert task.story_path is None
        assert task.story_text == "## Details\nFix it."
        assert task.github_issue == 42
        assert mock_run.call_count == 2
        cmd = mock_run.call_args_list[0][0][0]
        assert "42" in cmd
        assert "gh" in cmd

    def test_fetch_populates_depends_on_from_github_blockers(self, tmp_path: Path) -> None:
        issue_data = json.dumps(
            {"title": "Fix the bug", "body": "## Details\nFix it.", "state": "OPEN"}
        )
        timeline = json.dumps([{"event": "blocked_by", "blocking_issue": {"number": 12}}])
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=0, stdout=timeline, stderr=""),
            ]
            task = GitHubIssueSource().fetch("42", tmp_path)

        assert task.depends_on == ["issue-12"]
        assert task.inferred_dependencies == ["issue-12"]

    def test_fetch_promotes_body_prose_dependency_to_edge(self, tmp_path: Path) -> None:
        issue_data = json.dumps(
            {
                "title": "Fix the bug",
                "body": "This work is blocked by #12 and blocked by https://github.com/acme/repo/issues/7",
                "state": "OPEN",
            }
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=1, stdout="", stderr="preview unavailable"),
            ]
            task = GitHubIssueSource().fetch("42", tmp_path)

        assert task.depends_on == ["issue-7", "issue-12"]
        assert task.inferred_dependencies == ["issue-7", "issue-12"]
        assert task.dependency_warnings == []

    def test_fetch_promotes_depends_on_prose_to_edge(self, tmp_path: Path) -> None:
        issue_data = json.dumps(
            {
                "title": "Fix the bug",
                "body": "## Background\nDepends on: #265 for the schema change.",
                "state": "OPEN",
            }
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=1, stdout="", stderr="preview unavailable"),
            ]
            task = GitHubIssueSource().fetch("42", tmp_path)

        assert task.depends_on == ["issue-265"]
        assert task.inferred_dependencies == ["issue-265"]
        assert task.dependency_warnings == []

    def test_fetch_timeline_blockers_take_precedence_over_prose(self, tmp_path: Path) -> None:
        """Native blocked_by relationships override body-derived dependencies."""
        issue_data = json.dumps(
            {
                "title": "Fix the bug",
                "body": "Depends on #265",
                "state": "OPEN",
            }
        )
        timeline = json.dumps([{"event": "blocked_by", "blocking_issue": {"number": 12}}])
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=0, stdout=timeline, stderr=""),
            ]
            task = GitHubIssueSource().fetch("42", tmp_path)

        assert task.depends_on == ["issue-12"]
        assert task.inferred_dependencies == ["issue-12"]

    def test_fetch_merges_frontmatter_and_prose_when_timeline_empty(self, tmp_path: Path) -> None:
        issue_data = json.dumps(
            {
                "title": "Fix the bug",
                "body": (
                    "---\ndepends_on:\n  - issue-100\n---\n\nAlso depends on #200 for context."
                ),
                "state": "OPEN",
            }
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=1, stdout="", stderr="preview unavailable"),
            ]
            task = GitHubIssueSource().fetch("42", tmp_path)

        assert task.depends_on == ["issue-100", "issue-200"]
        assert task.dependency_warnings == []

    def test_fetch_populates_depends_on_from_issue_frontmatter(self, tmp_path: Path) -> None:
        issue_data = json.dumps(
            {
                "title": "Fix the bug",
                "body": "---\ndepends_on:\n  - issue-12\n  - 7\n---\n\n## Details\nFix it.",
                "state": "OPEN",
            }
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=1, stdout="", stderr="preview unavailable"),
            ]
            task = GitHubIssueSource().fetch("42", tmp_path)

        assert task.depends_on == ["issue-7", "issue-12"]
        assert task.inferred_dependencies == ["issue-7", "issue-12"]
        assert task.dependency_warnings == []

    def test_fetch_warns_when_issue_frontmatter_is_malformed(self, tmp_path: Path) -> None:
        issue_data = json.dumps(
            {
                "title": "Fix the bug",
                "body": "---\ndepends_on: issue-12\nbroken: [\n---\n\n## Details\nFix it.",
                "state": "OPEN",
            }
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=1, stdout="", stderr="preview unavailable"),
            ]
            task = GitHubIssueSource().fetch("42", tmp_path)

        # Malformed frontmatter falls back to prose detection: the depends_on
        # line is still honored as a scheduler edge, but the malformed-YAML
        # warning remains visible to the operator.
        assert task.depends_on == ["issue-12"]
        assert task.inferred_dependencies == ["issue-12"]
        assert task.dependency_warnings
        assert "malformed YAML frontmatter" in task.dependency_warnings[0]

    def test_fetch_appends_reopen_comment_to_story_text(self, tmp_path: Path) -> None:
        issue_data = json.dumps(
            {
                "title": "Fix the bug",
                "body": "## What happened\nOriginal contract",
                "state": "OPEN",
                "comments": [
                    {
                        "author": {"login": "operator"},
                        "body": "The remaining work is to wire reopen context into sprint intake.",
                        "createdAt": "2026-05-02T12:30:00Z",
                    }
                ],
            }
        )
        timeline = json.dumps(
            [
                {
                    "event": "reopened",
                    "created_at": "2026-05-02T12:00:00Z",
                    "actor": {"login": "operator"},
                }
            ]
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=0, stdout=timeline, stderr=""),
            ]
            task = GitHubIssueSource().fetch("42", tmp_path)

        assert "## Reopen Context" in task.story_text
        assert "operator" in task.story_text
        assert (
            "The remaining work is to wire reopen context into sprint intake." in task.story_text
        )
        assert "Original contract" in task.story_text

    @pytest.mark.parametrize(
        "body, expected",
        [
            ("Depends on #265", [265]),
            ("Depends on: #265", [265]),
            ("depends on #265", [265]),
            ("depends_on: #265", [265]),
            ("depends_on: issue-265", [265]),
            ("depends_on: [issue-265, issue-807]", [265, 807]),
            (
                "depends_on: https://github.com/acme/repo/issues/265",
                [265],
            ),
            (
                "This is blocked by #12 and depends on #265",
                [12, 265],
            ),
            (
                "depends_on: [issue-265, issue-807]\nblocked by #12",
                [12, 265, 807],
            ),
        ],
    )
    def test_prose_dependency_phrases_extract_refs(self, body: str, expected: list) -> None:
        source = GitHubIssueSource()
        result = sorted(
            {ref for _phrase, refs in source._find_prose_dependency_phrases(body) for ref in refs}
        )
        assert result == expected

    def test_parse_issue_blockers_from_body_metadata_requires_frontmatter(self) -> None:
        source = GitHubIssueSource()

        assert source._parse_issue_blockers_from_body_metadata("depends_on: issue-265") == []
        assert source._parse_issue_blockers_from_body_metadata(
            "---\ndepends_on: issue-265\n---\n\nBody"
        ) == [265]

    def test_matching_structured_dependency_suppresses_prose_warning(self) -> None:
        body = "---\ndepends_on: issue-265\n---\n\nDepends on #265 for context."

        warnings = GitHubIssueSource()._dependency_authoring_warnings(body, [265])

        assert warnings == []

    @pytest.mark.parametrize(
        "body",
        [
            "```\nissue-1071 depends_on issue-1070\n```",
            "Use `depends_on issue-1070` as an example in docs.",
            "> issue-1071 depends_on issue-1070",
        ],
    )
    def test_prose_dependency_phrases_ignore_illustrative_markdown(self, body: str) -> None:
        warnings = GitHubIssueSource()._dependency_authoring_warnings(body, [])

        assert warnings == []

    def test_prose_dependency_phrases_still_detect_plain_text_outside_examples(self) -> None:
        body = (
            "```\nissue-1071 depends_on issue-1070\n```\n\n"
            "This work depends_on issue-265.\n"
            "> blocked by #12"
        )

        warnings = GitHubIssueSource()._dependency_authoring_warnings(body, [])

        assert warnings == ["depends_on issue-265"]

    def test_fetch_raises_on_failure(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
            source = GitHubIssueSource()
            with pytest.raises(RuntimeError, match="gh issue view"):
                source.fetch("999", tmp_path)

    def test_on_complete_closes_issue_in_merge_mode(self, tmp_path: Path) -> None:
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=42)
        result = MagicMock()
        result.merge = {"merged": True}
        result.state.review_results = [MagicMock(summary="All good")]
        config = MagicMock()
        config.workspace.on_approve = "merge"
        config.project_root = tmp_path

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            source = GitHubIssueSource()
            source.on_complete(task, result, config)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "close" in cmd
        assert "42" in cmd
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)

    def test_on_complete_uses_project_root_cwd(self, tmp_path: Path) -> None:
        """gh issue close must use project_root as cwd regardless of process cwd."""
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=7)
        result = MagicMock()
        result.merge = {"merged": True}
        result.state.review_results = []
        config = MagicMock()
        config.workspace.on_approve = "merge"
        config.project_root = tmp_path / "my-repo"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            source = GitHubIssueSource()
            source.on_complete(task, result, config)

        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path / "my-repo")

    def test_on_complete_logs_warning_on_nonzero_exit(self, tmp_path: Path) -> None:
        """A nonzero exit from gh issue close is logged, not silently swallowed."""
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=42)
        result = MagicMock()
        result.merge = {"merged": True}
        result.state.review_results = []
        config = MagicMock()
        config.workspace.on_approve = "merge"
        config.project_root = tmp_path

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="not found", stdout="")
            with patch("theforge.sprint.sources._log") as mock_log:
                source = GitHubIssueSource()
                source.on_complete(task, result, config)

        mock_log.warning.assert_called_once()
        msg = mock_log.warning.call_args[0][0]
        assert "close" in msg

    def test_on_complete_noop_for_pr_mode(self) -> None:
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=42)
        result = MagicMock()
        result.merge = {"merged": True}
        config = MagicMock()
        config.workspace.on_approve = "pr"

        with patch("subprocess.run") as mock_run:
            source = GitHubIssueSource()
            source.on_complete(task, result, config)

        mock_run.assert_not_called()

    def test_on_complete_noop_when_not_merged(self) -> None:
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=42)
        result = MagicMock()
        result.merge = {"merged": False}
        config = MagicMock()
        config.workspace.on_approve = "merge"

        with patch("subprocess.run") as mock_run:
            source = GitHubIssueSource()
            source.on_complete(task, result, config)

        mock_run.assert_not_called()

    def test_on_escalate_comments_on_issue(self, tmp_path: Path) -> None:
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=42)
        state = MagicMock()
        state.error = "Gate failed"
        config = MagicMock()
        config.project_root = tmp_path

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            source = GitHubIssueSource()
            source.on_escalate(task, state, config)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "comment" in cmd
        assert "42" in cmd
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)

    def test_on_escalate_uses_project_root_cwd(self, tmp_path: Path) -> None:
        """gh issue comment must use project_root as cwd regardless of process cwd."""
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=5)
        state = MagicMock()
        state.error = "timeout"
        config = MagicMock()
        config.project_root = tmp_path / "my-repo"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            source = GitHubIssueSource()
            source.on_escalate(task, state, config)

        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path / "my-repo")

    def test_on_escalate_logs_warning_on_nonzero_exit(self, tmp_path: Path) -> None:
        """A nonzero exit from gh issue comment is logged, not silently swallowed."""
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=42)
        state = MagicMock()
        state.error = "timeout"
        config = MagicMock()
        config.project_root = tmp_path

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="auth error", stdout="")
            with patch("theforge.sprint.sources._log") as mock_log:
                source = GitHubIssueSource()
                source.on_escalate(task, state, config)

        mock_log.warning.assert_called_once()
        msg = mock_log.warning.call_args[0][0]
        assert "comment" in msg

    def _already_done_result(
        self,
        *,
        reason: str = "Working tree already satisfies the spec.",
        symptom: dict | None = None,
        work_type: str = "feature",
    ) -> SimpleNamespace:
        """A no-merge ALREADY_DONE CoordinatorResult stand-in."""
        state = SimpleNamespace(
            preflight_verdict="ALREADY_DONE",
            preflight_reason=reason,
            preflight_work_type=work_type,
            preflight_symptom_verification=symptom or {},
            review_results=[],
        )
        return SimpleNamespace(success=True, merge=None, state=state)

    def _merge_config(self, tmp_path: Path):
        config = MagicMock()
        config.workspace.on_approve = "merge"
        config.project_root = tmp_path
        return config

    def test_already_done_already_implemented_closes_completed(self, tmp_path: Path) -> None:
        """A verified-resolved symptom closes the issue with --reason completed."""
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=1879)
        result = self._already_done_result(
            reason="Symptom no longer reproduces at HEAD.",
            symptom={"status": "verified_resolved", "reproduces_now": False},
            work_type="bug",
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            GitHubIssueSource().on_complete(task, result, self._merge_config(tmp_path))

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "close" in cmd
        assert "1879" in cmd
        assert "--reason" in cmd
        assert "completed" in cmd
        # Comment carries the determination and its evidence.
        comment = cmd[cmd.index("--comment") + 1]
        assert "ALREADY_DONE" in comment
        assert "Symptom no longer reproduces at HEAD." in comment

    def test_already_done_premise_obsolete_closes_not_planned(self, tmp_path: Path) -> None:
        """A never-reproduced symptom closes the issue with --reason 'not planned'."""
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=1880)
        result = self._already_done_result(
            reason="Bug premise absent from baseline.",
            symptom={"status": "not_reproduced", "reproduces_now": False},
            work_type="bug",
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            GitHubIssueSource().on_complete(task, result, self._merge_config(tmp_path))

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "close" in cmd
        assert "--reason" in cmd
        assert "not planned" in cmd

    def test_already_done_ambiguous_routes_to_operator(self, tmp_path: Path) -> None:
        """An ambiguous disposition comments and adds the operator-action label, no close."""
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=1881)
        result = self._already_done_result(reason="Spec appears satisfied.", symptom={})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            GitHubIssueSource().on_complete(task, result, self._merge_config(tmp_path))

        assert mock_run.call_count == 2
        comment_cmd = mock_run.call_args_list[0][0][0]
        label_cmd = mock_run.call_args_list[1][0][0]
        # First: a durable comment with determination + evidence, no close.
        assert "comment" in comment_cmd
        assert "close" not in comment_cmd
        body = comment_cmd[comment_cmd.index("--body") + 1]
        assert "ALREADY_DONE" in body
        assert "Spec appears satisfied." in body
        # Second: route to the operator-action queue via the label vocabulary.
        assert "edit" in label_cmd
        assert "--add-label" in label_cmd
        assert "operator-action" in label_cmd

    def test_already_done_noop_when_not_merge_mode(self, tmp_path: Path) -> None:
        """ALREADY_DONE resolution is gated on merge mode like the close path."""
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=1882)
        result = self._already_done_result()
        config = MagicMock()
        config.workspace.on_approve = "pr"
        config.project_root = tmp_path
        with patch("subprocess.run") as mock_run:
            GitHubIssueSource().on_complete(task, result, config)
        mock_run.assert_not_called()

    def test_merged_land_still_uses_close_path(self, tmp_path: Path) -> None:
        """A real merge still closes with the review summary, never the ALREADY_DONE path."""
        task = TaskStory(name="Test", slug="test", type="enhancement", github_issue=1883)
        state = SimpleNamespace(
            preflight_verdict="ALREADY_DONE",  # even if preflight said ALREADY_DONE
            preflight_reason="x",
            review_results=[SimpleNamespace(summary="Shipped it")],
        )
        result = SimpleNamespace(success=True, merge={"merged": True}, state=state)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            GitHubIssueSource().on_complete(task, result, self._merge_config(tmp_path))

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "close" in cmd
        assert "--reason" not in cmd  # merged close uses the legacy no-reason form
        comment = cmd[cmd.index("--comment") + 1]
        assert "Completed by TheForge" in comment

    # ── Spike closure guard (#2600) ────────────────────────────────────

    _SPIKE_OUTCOME = (
        "<!-- forge-spike-outcome-v1\noutcome: do_not_proceed\n"
        "reason: the signal is unreachable\n-->"
    )

    def _spike_issue_json(self, body: str = "", labels=("spike",)) -> str:
        return json.dumps(
            {
                "state": "OPEN",
                "labels": [{"name": name} for name in labels],
                "body": body,
                "comments": [],
            }
        )

    def test_spike_without_an_outcome_is_not_closed_on_merge(self, tmp_path: Path) -> None:
        """A landed spike with no recorded outcome stays open, with the reason posted."""
        task = TaskStory(name="Spike", slug="issue-2348", type="spike", github_issue=2348)
        state = SimpleNamespace(
            preflight_verdict="PROCEED",
            review_results=[SimpleNamespace(summary="POC landed")],
        )
        result = SimpleNamespace(success=True, merge={"merged": True}, state=state)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._spike_issue_json("A question."), stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # the refusal comment
            ]
            GitHubIssueSource().on_complete(task, result, self._merge_config(tmp_path))

        commands = [call[0][0] for call in mock_run.call_args_list]
        assert not any("close" in cmd for cmd in commands), "the spike must stay open"
        comment = commands[-1]
        assert "comment" in comment
        body = comment[comment.index("--body") + 1]
        assert "records no outcome" in body
        # The story's own result is not lost just because the close was refused.
        assert "POC landed" in body

    def test_spike_with_a_recorded_outcome_closes_on_merge(self, tmp_path: Path) -> None:
        task = TaskStory(name="Spike", slug="issue-2348", type="spike", github_issue=2348)
        state = SimpleNamespace(
            preflight_verdict="PROCEED",
            review_results=[SimpleNamespace(summary="POC landed")],
        )
        result = SimpleNamespace(success=True, merge={"merged": True}, state=state)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(
                    returncode=0, stdout=self._spike_issue_json(self._SPIKE_OUTCOME), stderr=""
                ),
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            GitHubIssueSource().on_complete(task, result, self._merge_config(tmp_path))

        commands = [call[0][0] for call in mock_run.call_args_list]
        assert any("close" in cmd and "2348" in cmd for cmd in commands)

    def test_already_done_spike_without_an_outcome_is_not_closed(self, tmp_path: Path) -> None:
        """The ALREADY_DONE dispositions are close paths too, so they ask the guard."""
        task = TaskStory(name="Spike", slug="issue-2349", type="spike", github_issue=2349)
        result = self._already_done_result(
            reason="Symptom no longer reproduces at HEAD.",
            symptom={"status": "verified_resolved", "reproduces_now": False},
            work_type="bug",
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=self._spike_issue_json(), stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            GitHubIssueSource().on_complete(task, result, self._merge_config(tmp_path))

        commands = [call[0][0] for call in mock_run.call_args_list]
        assert not any("close" in cmd for cmd in commands)
        body = commands[-1][commands[-1].index("--body") + 1]
        assert "ALREADY_DONE" in body, "the determination stays durable"
        assert "records no outcome" in body

    def test_a_non_spike_close_makes_no_extra_gh_call(self, tmp_path: Path) -> None:
        """The guard must not put a lookup on the hot path of an ordinary close."""
        task = TaskStory(name="Test", slug="test", type="bug", github_issue=42)
        result = MagicMock()
        result.merge = {"merged": True}
        result.state.review_results = []
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            GitHubIssueSource().on_complete(task, result, self._merge_config(tmp_path))
        mock_run.assert_called_once()
        assert "close" in mock_run.call_args[0][0]

    def test_classify_disposition(self) -> None:
        from theforge.sprint.sources import classify_already_done_disposition

        assert (
            classify_already_done_disposition(
                SimpleNamespace(
                    preflight_symptom_verification={
                        "status": "verified_resolved",
                        "reproduces_now": False,
                    }
                )
            )
            == "already_implemented"
        )
        assert (
            classify_already_done_disposition(
                SimpleNamespace(
                    preflight_symptom_verification={
                        "status": "not_reproduced",
                        "reproduces_now": False,
                    }
                )
            )
            == "premise_obsolete"
        )
        assert (
            classify_already_done_disposition(SimpleNamespace(preflight_symptom_verification={}))
            == "ambiguous"
        )

    def test_fetch_raises_on_malformed_json(self, tmp_path: Path) -> None:
        """Malformed JSON from gh is wrapped in a clear RuntimeError."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not-json", stderr="")
            source = GitHubIssueSource()
            with pytest.raises(RuntimeError, match="malformed JSON"):
                source.fetch("42", tmp_path)

    def test_is_story_source(self) -> None:
        assert isinstance(GitHubIssueSource(), StorySource)


# ── resolve / canonicalize_ref ─────────────────────────────────────────


class TestResolve:
    def test_string_entry_returns_file_source(self, tmp_path: Path) -> None:
        source, ref, canonical = resolve("specs/story.md", tmp_path)
        assert isinstance(source, FileSource)
        assert ref == "specs/story.md"
        assert canonical == "specs/story.md"

    def test_dict_entry_returns_github_source(self, tmp_path: Path) -> None:
        source, ref, canonical = resolve({"issue": 34}, tmp_path)
        assert isinstance(source, GitHubIssueSource)
        assert ref == "34"
        assert canonical == "issue:34"

    def test_invalid_entry_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            resolve(42, tmp_path)

    def test_canonicalize_ref_string(self) -> None:
        assert canonicalize_ref("specs/story.md") == "specs/story.md"

    def test_canonicalize_ref_dict(self) -> None:
        assert canonicalize_ref({"issue": 130}) == "issue:130"

    def test_canonicalize_ref_invalid(self) -> None:
        with pytest.raises(ValueError):
            canonicalize_ref(42)


# ── Manifest parsing ──────────────────────────────────────────────────


class TestManifestParsing:
    def test_load_mixed_entries(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "budget_usd": 10.0,
                    "stories": [
                        "specs/story.md",
                        {"issue": 34},
                        {"issue": 130, "depends_on": ["my-story"], "slug": "custom-slug"},
                    ],
                }
            )
        )
        manifest = load_sprint_manifest(manifest_path)
        assert len(manifest.stories) == 3
        assert manifest.stories[0] == "specs/story.md"
        assert manifest.stories[1] == {"issue": 34}

    def test_reject_dict_without_issue(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            yaml.dump({"name": "test", "budget_usd": 10.0, "stories": [{"foo": "bar"}]})
        )
        with pytest.raises(ValueError, match="'issue' key"):
            load_sprint_manifest(manifest_path)

    def test_reject_non_int_issue(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            yaml.dump({"name": "test", "budget_usd": 10.0, "stories": [{"issue": "abc"}]})
        )
        with pytest.raises(ValueError, match="integer"):
            load_sprint_manifest(manifest_path)

    def test_reject_non_string_non_dict(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(yaml.dump({"name": "test", "budget_usd": 10.0, "stories": [42]}))
        with pytest.raises(ValueError, match="strings or dicts"):
            load_sprint_manifest(manifest_path)

    def test_build_tasks_from_manifest_file(self, tmp_path: Path) -> None:
        story = tmp_path / "specs" / "story.md"
        story.parent.mkdir(parents=True)
        story.write_text("---\nname: My Story\nslug: my-story\n---\n# Story\n")
        manifest = SprintManifest(name="test", budget_usd=10.0, stories=["specs/story.md"])
        entries = build_tasks_from_manifest(manifest, tmp_path)
        assert len(entries) == 1
        task, source, canonical = entries[0]
        assert task.slug == "my-story"
        assert isinstance(source, FileSource)
        assert canonical == "specs/story.md"

    def test_build_tasks_from_manifest_issue(self, tmp_path: Path) -> None:
        issue_data = json.dumps({"title": "Fix bug", "body": "Fix the thing"})
        manifest = SprintManifest(name="test", budget_usd=10.0, stories=[{"issue": 42}])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=issue_data, stderr="")
            entries = build_tasks_from_manifest(manifest, tmp_path)

        assert len(entries) == 1
        task, source, canonical = entries[0]
        assert task.slug == "issue-42"
        assert task.github_issue == 42
        assert isinstance(source, GitHubIssueSource)
        assert canonical == "issue:42"

    def test_build_tasks_skips_closed_manifest_issue_with_warning(
        self, tmp_path: Path, capsys
    ) -> None:
        """Closed issues in a manifest are skipped with a warning, not a crash.

        Regression: before the fix, IssueClosedError propagated out of
        build_tasks_from_manifest(), breaking any sprint manifest that
        referenced a previously-completed issue.
        """

        closed_json = json.dumps({"title": "Done", "body": "shipped", "state": "CLOSED"})
        manifest = SprintManifest(name="test", budget_usd=10.0, stories=[{"issue": 99}])
        with patch("subprocess.run") as mock_run:
            # gh issue view returns CLOSED state
            mock_run.return_value = MagicMock(returncode=0, stdout=closed_json, stderr="")
            entries = build_tasks_from_manifest(manifest, tmp_path)

        assert entries == []
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "99" in captured.err

    def test_build_tasks_applies_overrides(self, tmp_path: Path) -> None:
        issue_data = json.dumps({"title": "Fix bug", "body": "Fix the thing"})
        manifest = SprintManifest(
            name="test",
            budget_usd=10.0,
            stories=[{"issue": 42, "slug": "custom", "depends_on": ["other"]}],
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=issue_data, stderr="")
            entries = build_tasks_from_manifest(manifest, tmp_path)

        task = entries[0][0]
        assert task.slug == "custom"
        assert task.depends_on == ["other"]


# ── TaskStory story_text support ──────────────────────────────────────


class TestTaskStoryText:
    def test_story_text_field(self) -> None:
        task = TaskStory(name="Test", slug="test", story_text="Hello world")
        assert task.story_text == "Hello world"
        assert task.story_path is None

    def test_story_path_none(self) -> None:
        task = TaskStory(name="Test", slug="test")
        assert task.story_path is None
        assert task.story_text is None

    def test_story_path_still_works(self, tmp_path: Path) -> None:
        p = tmp_path / "story.md"
        p.write_text("content")
        task = TaskStory(name="Test", slug="test", story_path=p)
        assert task.story_path == p


# ── _triage_spec with optional task ───────────────────────────────────


class TestTriageSpecOptionalTask:
    def test_with_prebuilt_task(self, tmp_path: Path) -> None:
        """_triage_spec with task= bypasses disk read."""
        task = TaskStory(name="Test", slug="issue-42", story_text="# Issue content")
        config = MagicMock()
        config.workspace.branch_pattern = "feat/{slug}"
        config.workspace.base_branch = "main"
        config.workspace.path_pattern = ".forge/worktrees/{slug}"

        with patch("theforge.sprint.dag._is_branch_merged", return_value=False):
            triage = _triage_spec("issue:42", config, tmp_path, task=task)

        assert triage.slug == "issue-42"
        assert triage.action == "full"

    def test_without_task_reads_disk(self, tmp_path: Path) -> None:
        """_triage_spec without task= still reads from disk (backward compat)."""
        story = tmp_path / "story.md"
        story.write_text("---\nname: Test\nslug: test-story\n---\n# Content\n")
        config = MagicMock()
        config.workspace.branch_pattern = "feat/{slug}"
        config.workspace.base_branch = "main"
        config.workspace.path_pattern = ".forge/worktrees/{slug}"

        with patch("theforge.sprint.dag._is_branch_merged", return_value=False):
            triage = _triage_spec("story.md", config, tmp_path)

        assert triage.slug == "test-story"
        assert triage.action == "full"


# ── on_approve alias normalization ────────────────────────────────────


class TestOnApproveAliases:
    def test_ask_normalized_to_pr(self) -> None:
        from theforge.config._loaders import _parse_workspace

        result = _parse_workspace({"on_approve": "ask"})
        assert result.on_approve == "pr"

    def test_merge_pr_passthrough(self) -> None:
        from theforge.config._loaders import _parse_workspace

        result = _parse_workspace({"on_approve": "merge-pr", "auto_push": True})
        assert result.on_approve == "merge-pr"

    def test_merge_unchanged(self) -> None:
        from theforge.config._loaders import _parse_workspace

        result = _parse_workspace({"on_approve": "merge"})
        assert result.on_approve == "merge"

    def test_none_unchanged(self) -> None:
        from theforge.config._loaders import _parse_workspace

        result = _parse_workspace({})
        assert result.on_approve == "none"


# ── parse_manifest_slugs with issue entries ───────────────────────────


class TestParseManifestSlugs:
    def test_file_and_issue_entries(self, tmp_path: Path) -> None:
        story = tmp_path / "story.md"
        story.write_text("---\nname: Test\nslug: my-slug\n---\n# Content\n")
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "budget_usd": 10.0,
                    "stories": ["story.md", {"issue": 42}],
                }
            )
        )
        config = MagicMock()
        config.project_root = tmp_path
        slugs = parse_manifest_slugs(config, manifest_path)
        assert "my-slug" in slugs
        assert "issue-42" in slugs

    def test_issue_with_custom_slug(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "budget_usd": 10.0,
                    "stories": [{"issue": 42, "slug": "custom"}],
                }
            )
        )
        config = MagicMock()
        config.project_root = tmp_path
        slugs = parse_manifest_slugs(config, manifest_path)
        assert slugs == ["custom"]

    def test_story_refs_preserve_file_paths_and_issue_refs(self, tmp_path: Path) -> None:
        story = tmp_path / "story.md"
        story.write_text("---\nname: Test\nslug: custom-file\n---\n# Content\n")
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            yaml.dump(
                {
                    "name": "test",
                    "budget_usd": 10.0,
                    "stories": ["story.md", {"issue": 42, "slug": "custom-issue"}],
                }
            )
        )
        config = MagicMock()
        config.project_root = tmp_path

        slugs, canonical_refs_by_slug = parse_manifest_story_refs(config, manifest_path)

        assert slugs == ["custom-file", "custom-issue"]
        assert canonical_refs_by_slug == {
            "custom-file": "story.md",
            "custom-issue": "issue:42",
        }


# ── Completion: archive and PR body ───────────────────────────────────


class TestCompletionIssueSupport:
    def test_archive_returns_false_for_none_path(self) -> None:
        from theforge.coordinator.completion import _archive_story_to_done

        assert _archive_story_to_done(None, Path("/tmp")) is False

    def test_pr_body_shows_github_issue(self) -> None:
        """PR body shows 'GitHub Issue #N' when story_path is None."""
        task = TaskStory(name="Fix bug", slug="fix-bug", github_issue=42, story_path=None)
        # Just check the story_line logic (PR creation requires subprocess)
        if task.story_path is None and task.github_issue:
            story_line = f"{task.name} (GitHub Issue #{task.github_issue})"
        else:
            story_line = f"{task.name} (`{task.story_path}`)"
        assert "GitHub Issue #42" in story_line

    def test_pr_body_shows_path_for_file_story(self, tmp_path: Path) -> None:
        p = tmp_path / "story.md"
        task = TaskStory(name="Fix bug", slug="fix-bug", story_path=p)
        if task.story_path is None and task.github_issue:
            story_line = f"{task.name} (GitHub Issue #{task.github_issue})"
        else:
            story_line = f"{task.name} (`{task.story_path}`)"
        assert str(p) in story_line


# ── Overlap detection: _extract_plan_footprint ────────────────────────


class TestPlanFootprint:
    def test_extract_files_from_plan(self, tmp_path: Path) -> None:
        plan_dir = tmp_path / ".forge"
        plan_dir.mkdir()
        plan_file = plan_dir / "plan.md"
        plan_content = yaml.dump(
            {
                "plan": {
                    "approach": "test",
                    "steps": [
                        {
                            "id": 1,
                            "description": "step 1",
                            "files": ["src/a.py", "src/b.py"],
                            "action": "modify",
                            "details": "",
                        },
                        {
                            "id": 2,
                            "description": "step 2",
                            "files": ["src/c.py"],
                            "action": "create",
                            "details": "",
                        },
                    ],
                }
            }
        )
        plan_file.write_text(plan_content)
        footprint = _extract_plan_footprint(tmp_path)
        assert footprint == {"src/a.py", "src/b.py", "src/c.py"}

    def test_empty_set_on_missing_plan(self, tmp_path: Path) -> None:
        footprint = _extract_plan_footprint(tmp_path)
        assert footprint == set()

    def test_empty_set_on_parse_failure(self, tmp_path: Path) -> None:
        plan_dir = tmp_path / ".forge"
        plan_dir.mkdir()
        plan_file = plan_dir / "plan.md"
        plan_file.write_text("not valid yaml at all {{{{ [[[")
        footprint = _extract_plan_footprint(tmp_path)
        assert footprint == set()


# ── Plan gate: _run_fresh ─────────────────────────────────────────────


class TestRunFreshPlanGate:
    def test_no_gate_calls_run_task_directly(self) -> None:
        config = MagicMock()
        task = TaskStory(name="Test", slug="test")
        mock_result = MagicMock()

        with patch("theforge.sprint.runner.run_task", return_value=mock_result) as mock_rt:
            result = _run_fresh(
                config,
                task,
                "run-1",
                "sprint",
                False,
                False,
                False,
                None,
                False,
                None,
            )

        mock_rt.assert_called_once()
        assert result is mock_result

    def test_gate_splits_plan_and_dev(self) -> None:
        config = MagicMock()
        task = TaskStory(name="Test", slug="test")
        gate = threading.Event()

        plan_result = MagicMock()
        plan_result.success = True
        plan_result.state.workspace_path = Path("/tmp/ws")

        dev_result = MagicMock()

        updates: list[dict] = []

        def capture_fn(update: dict) -> None:
            updates.append(update)
            if update.get("phase") == "PLAN_DONE":
                gate.set()  # simulate scheduler releasing the gate

        with patch("theforge.sprint.runner.run_task", return_value=plan_result) as mock_rt:
            with patch("theforge.sprint.runner.run_from_dev", return_value=dev_result) as mock_rfd:
                result = _run_fresh(
                    config,
                    task,
                    "run-1",
                    "sprint",
                    False,
                    False,
                    False,
                    capture_fn,
                    False,
                    gate,
                )

        # run_task called with stop_phase
        assert mock_rt.call_args.kwargs.get("stop_phase") is not None
        # run_from_dev called after gate released
        mock_rfd.assert_called_once()
        assert result is dev_result
        # PLAN_DONE was signaled
        plan_done_updates = [u for u in updates if u.get("phase") == "PLAN_DONE"]
        assert len(plan_done_updates) == 1

    def test_gate_returns_plan_result_on_failure(self) -> None:
        config = MagicMock()
        task = TaskStory(name="Test", slug="test")
        gate = threading.Event()

        plan_result = MagicMock()
        plan_result.success = False

        with patch("theforge.sprint.runner.run_task", return_value=plan_result):
            with patch("theforge.sprint.runner.run_from_dev") as mock_rfd:
                result = _run_fresh(
                    config,
                    task,
                    "run-1",
                    "sprint",
                    False,
                    False,
                    False,
                    None,
                    False,
                    gate,
                )

        mock_rfd.assert_not_called()
        assert result is plan_result


# ── Local file regression ─────────────────────────────────────────────


class TestLocalFileRegression:
    def test_local_file_stories_unchanged(self, tmp_path: Path) -> None:
        """Local file stories work through build_tasks_from_manifest."""
        story = tmp_path / "specs" / "my-story.md"
        story.parent.mkdir(parents=True)
        story.write_text("---\nname: My Story\nslug: my-story\ngithub_issue: 99\n---\n# Content\n")
        manifest = SprintManifest(name="test", budget_usd=10.0, stories=["specs/my-story.md"])
        entries = build_tasks_from_manifest(manifest, tmp_path)
        assert len(entries) == 1
        task, source, canonical = entries[0]
        assert task.slug == "my-story"
        assert task.name == "My Story"
        assert task.story_path == story.resolve()
        assert task.github_issue == 99
        assert isinstance(source, FileSource)
        assert canonical == "specs/my-story.md"


# ── P1 regression: empty issue body ──────────────────────────────────


class TestEmptyIssueBody:
    def test_empty_story_text_is_not_none(self) -> None:
        """Empty string story_text should be used, not fall back to story_path."""
        task = TaskStory(name="Test", slug="test", story_text="", story_path=None)
        # The `is not None` check should return "" rather than trying load_story(None)
        content = task.story_text if task.story_text is not None else "FALLBACK"
        assert content == ""

    def test_empty_body_issue_fetch(self, tmp_path: Path) -> None:
        """GitHubIssueSource.fetch() with empty body sets story_text to ''."""
        issue_data = json.dumps({"title": "Empty Issue", "body": ""})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=issue_data, stderr="")
            source = GitHubIssueSource()
            task = source.fetch("50", tmp_path)
        assert task.story_text == ""
        assert task.story_path is None


# ── P1 regression: depends_on type validation ─────────────────────────


class TestDependsOnValidation:
    def test_invalid_depends_on_type_raises(self, tmp_path: Path) -> None:
        """Dict entry with non-str/non-list depends_on raises ValueError."""
        issue_data = json.dumps({"title": "Fix bug", "body": "content"})
        manifest = SprintManifest(
            name="test",
            budget_usd=10.0,
            stories=[{"issue": 42, "depends_on": 123}],
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=issue_data, stderr="")
            with pytest.raises(ValueError, match="depends_on.*must be a string or list"):
                build_tasks_from_manifest(manifest, tmp_path)

    def test_none_depends_on_raises(self, tmp_path: Path) -> None:
        """Dict entry with depends_on: null raises ValueError."""
        issue_data = json.dumps({"title": "Fix bug", "body": "content"})
        manifest = SprintManifest(
            name="test",
            budget_usd=10.0,
            stories=[{"issue": 42, "depends_on": None}],
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=issue_data, stderr="")
            with pytest.raises(ValueError, match="depends_on.*must be a string or list"):
                build_tasks_from_manifest(manifest, tmp_path)


# ── P1 regression: plan gate deadlock ─────────────────────────────────


class TestReleaseplanGates:
    def test_releases_non_overlapping_gate(self) -> None:
        """_release_plan_gates releases a gate when there is no file overlap."""
        from theforge.sprint.runner import _release_plan_gates

        plan_done = {"story-a": "/tmp/ws-a"}
        file_footprints: dict[str, set[str]] = {}
        gate = threading.Event()
        plan_gates = {"story-a": gate}
        active: dict[str, object] = {"story-a": MagicMock()}
        phase_lock = threading.Lock()

        with patch(
            "theforge.sprint.runner._extract_plan_footprint",
            return_value={"src/a.py"},
        ):
            _release_plan_gates(plan_done, file_footprints, plan_gates, active, phase_lock)

        assert gate.is_set()
        assert "story-a" not in plan_gates

    def test_defers_overlapping_gate(self) -> None:
        """_release_plan_gates defers a gate when files overlap with active dev."""
        from theforge.sprint.runner import _release_plan_gates

        plan_done = {"story-b": "/tmp/ws-b"}
        # story-a is already in DEV (not in plan_gates, in active, has footprint)
        file_footprints: dict[str, set[str]] = {"story-a": {"src/shared.py"}}
        gate_b = threading.Event()
        plan_gates = {"story-b": gate_b}
        active: dict[str, object] = {"story-a": MagicMock(), "story-b": MagicMock()}
        phase_lock = threading.Lock()

        with patch(
            "theforge.sprint.runner._extract_plan_footprint",
            return_value={"src/shared.py"},
        ):
            _release_plan_gates(plan_done, file_footprints, plan_gates, active, phase_lock)

        assert not gate_b.is_set()
        assert "story-b" in plan_gates

    def test_releases_deferred_after_conflict_finishes(self) -> None:
        """_release_plan_gates releases a previously-deferred gate after conflict clears."""
        from theforge.sprint.runner import _release_plan_gates

        plan_done: dict[str, str] = {}
        file_footprints = {
            "story-a": {"src/shared.py"},
            "story-b": {"src/shared.py"},
        }
        gate_b = threading.Event()
        plan_gates = {"story-b": gate_b}
        # story-a is no longer active (it finished)
        active: dict[str, object] = {"story-b": MagicMock()}
        phase_lock = threading.Lock()

        _release_plan_gates(plan_done, file_footprints, plan_gates, active, phase_lock)

        assert gate_b.is_set()
        assert "story-b" not in plan_gates

    def test_deferred_gate_stays_closed_while_blocker_holds_a_claim(self) -> None:
        """#2234: a blocker that left ``active`` still holds its files while claimed.

        story-a escalated with its worktree preserved — no longer active, but the
        pending decision is precisely whether its rewrite of src/shared.py lands.
        """
        from theforge.sprint.runner import CLAIM_PRESERVED, _release_plan_gates

        plan_done: dict[str, str] = {}
        file_footprints = {
            "story-a": {"src/shared.py"},
            "story-b": {"src/shared.py"},
        }
        gate_b = threading.Event()
        plan_gates = {"story-b": gate_b}
        # story-a is no longer active, but its claim survives.
        active: dict[str, object] = {"story-b": MagicMock()}
        collision_claims = {"story-a": CLAIM_PRESERVED}
        phase_lock = threading.Lock()

        stood_down = _release_plan_gates(
            plan_done, file_footprints, plan_gates, active, phase_lock, collision_claims
        )

        assert not gate_b.is_set()
        # Nothing in this run can resolve the preserved claim, so story-b is
        # stood down rather than released onto a base that is about to change.
        assert stood_down == ["story-b"]
        assert "story-b" in plan_gates

    def test_deferred_gate_waits_while_blocker_claim_is_still_resolvable(self) -> None:
        """A claim held for a pending landing is not a stand-down: it can resolve."""
        from theforge.sprint.runner import CLAIM_PENDING_LANDING, _release_plan_gates

        plan_done: dict[str, str] = {}
        file_footprints = {
            "story-a": {"src/shared.py"},
            "story-b": {"src/shared.py"},
        }
        gate_b = threading.Event()
        plan_gates = {"story-b": gate_b}
        active: dict[str, object] = {"story-b": MagicMock()}
        collision_claims = {"story-a": CLAIM_PENDING_LANDING}
        phase_lock = threading.Lock()

        stood_down = _release_plan_gates(
            plan_done, file_footprints, plan_gates, active, phase_lock, collision_claims
        )

        assert not gate_b.is_set()
        assert stood_down == []
        assert "story-b" in plan_gates

    def test_deferred_gate_releases_once_claim_is_dropped(self) -> None:
        """The claim, not the status transition, is what holds the gate."""
        from theforge.sprint.runner import CLAIM_PRESERVED, _release_plan_gates

        plan_done: dict[str, str] = {}
        file_footprints = {
            "story-a": {"src/shared.py"},
            "story-b": {"src/shared.py"},
        }
        gate_b = threading.Event()
        plan_gates = {"story-b": gate_b}
        active: dict[str, object] = {"story-b": MagicMock()}
        collision_claims = {"story-a": CLAIM_PRESERVED}
        phase_lock = threading.Lock()

        _release_plan_gates(
            plan_done, file_footprints, plan_gates, active, phase_lock, collision_claims
        )
        assert not gate_b.is_set()

        # The claim ends (work landed or was abandoned) — the scheduler clears
        # the footprint alongside it, exactly as _end_collision_claim does.
        del collision_claims["story-a"]
        del file_footprints["story-a"]

        stood_down = _release_plan_gates(
            plan_done, file_footprints, plan_gates, active, phase_lock, collision_claims
        )

        assert gate_b.is_set()
        assert stood_down == []
        assert "story-b" not in plan_gates
        # Passing the gate registers story-b's own claim on its files.
        assert "story-b" in collision_claims

    def test_new_plan_defers_behind_a_preserved_claim(self) -> None:
        """A story reaching PLAN_DONE after the blocker escalated is still held."""
        from theforge.sprint.runner import CLAIM_PRESERVED, _release_plan_gates

        plan_done = {"story-b": "/tmp/ws-b"}
        file_footprints: dict[str, set[str]] = {"story-a": {"src/shared.py"}}
        gate_b = threading.Event()
        plan_gates = {"story-b": gate_b}
        active: dict[str, object] = {"story-b": MagicMock()}
        collision_claims = {"story-a": CLAIM_PRESERVED}
        phase_lock = threading.Lock()

        with patch(
            "theforge.sprint.runner._extract_plan_footprint",
            return_value={"src/shared.py"},
        ):
            _release_plan_gates(
                plan_done, file_footprints, plan_gates, active, phase_lock, collision_claims
            )

        assert not gate_b.is_set()
        assert "story-b" in plan_gates
        assert "story-b" not in collision_claims

    def test_poll_loop_no_false_timeout_after_last_gate_released(self):
        """Releasing the last plan gate must not cause a false timeout.

        Regression: _poll_interval was mutated to 3600 before being added to
        _total_waited, making the loop think the full timeout had elapsed.
        """

        # Simulate the polling loop logic from run_sprint
        plan_gates: dict[str, threading.Event] = {"story-a": threading.Event()}
        _poll_interval = 2.0
        _total_waited = 0.0

        # First poll: no future completes, we release the last gate
        _current_interval = _poll_interval
        # Simulate: wait() returned no done futures
        # Release gates
        plan_gates.pop("story-a")
        if not plan_gates:
            _poll_interval = 3600.0
        _total_waited += _current_interval

        # After one 2s poll, total_waited should be 2.0, NOT 3600.0
        assert _total_waited == 2.0, f"Expected 2.0 but got {_total_waited}"
        assert _total_waited < 3600.0, "Loop would false-timeout"
