"""`forge review` story-source resolution.

Review-only mode existed for file-backed stories, but a story sourced from a
GitHub issue is never written to disk — ``GitHubIssueSource`` builds a
``TaskStory`` with ``story_path=None`` and the body in ``story_text``. That left
no invocation at all for re-reviewing an issue-backed worktree, and reaching for
the worktree path instead (the obvious guess, since it is the thing being
reviewed) crashed inside ``read_text()`` with ``IsADirectoryError``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.cli.review import cmd_review
from theforge.sprint.sources import IssueClosedError
from theforge.task import TaskStory


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        story=None,
        issue=None,
        slug=None,
        config=None,
        worktree=None,
        auto_merge=False,
        verbose=False,
        no_notify=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _fake_result(success: bool = True) -> MagicMock:
    result = MagicMock()
    result.success = success
    result.message = "APPROVE"
    result.state.total_cost = 1.25
    return result


class TestStorySourceSelection:
    def test_no_story_source_is_refused(self, capsys):
        assert cmd_review(_args()) == 1
        assert "exactly one story source" in capsys.readouterr().err

    def test_both_story_sources_is_refused(self, capsys):
        """Two sources cannot be silently reconciled — they may disagree."""
        assert cmd_review(_args(story="story.md", issue=2204)) == 1
        assert "exactly one story source" in capsys.readouterr().err

    def test_a_directory_names_the_flags_that_would_have_worked(self, tmp_path, capsys):
        """The worktree is what an operator reaches for, so say what to use instead."""
        worktree = tmp_path / "issue-2204"
        worktree.mkdir()
        assert cmd_review(_args(story=str(worktree))) == 1
        err = capsys.readouterr().err
        assert "is a directory, not a story file" in err
        assert "--issue" in err
        assert "--worktree" in err

    def test_a_missing_story_file_still_reports_plainly(self, tmp_path, capsys):
        assert cmd_review(_args(story=str(tmp_path / "nope.md"))) == 1
        assert "Story file not found" in capsys.readouterr().err


class TestIssueBackedReview:
    """--issue builds the task through the same source the sprint path uses."""

    def _patches(self, tmp_path, task, fetch_error=None):
        config = MagicMock()
        config.project_root = tmp_path
        config.workspace.path_pattern = ".forge/worktrees/{slug}"
        config.review_pool = [MagicMock(model="m", name="r")]
        source = MagicMock()
        if fetch_error is not None:
            source.fetch.side_effect = fetch_error
        else:
            source.fetch.return_value = task
        return config, source

    def test_issue_number_resolves_the_story_and_derives_the_worktree(self, tmp_path):
        task = TaskStory(name="T", story_path=None, slug="issue-2204", github_issue=2204)
        config, source = self._patches(tmp_path, task)
        with (
            patch("theforge.cli.review._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.review.load_config", return_value=config),
            patch("theforge.cli.review.GitHubIssueSource", return_value=source),
            patch("theforge.cli.review.run_from_review", return_value=_fake_result()) as run,
            patch("theforge.cli.review._write_audit", return_value=tmp_path / "a.yaml"),
            patch.object(Path, "exists", return_value=True),
        ):
            assert cmd_review(_args(issue=2204)) == 0
        source.fetch.assert_called_once_with("2204", tmp_path)
        passed_task, workspace = run.call_args.args[1], run.call_args.args[2]
        assert passed_task.github_issue == 2204
        # The slug the issue source derives is what points at the existing worktree.
        assert workspace == tmp_path / ".forge/worktrees/issue-2204"

    def test_an_explicit_slug_overrides_the_derived_one(self, tmp_path):
        task = TaskStory(name="T", story_path=None, slug="issue-2204", github_issue=2204)
        config, source = self._patches(tmp_path, task)
        with (
            patch("theforge.cli.review._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.review.load_config", return_value=config),
            patch("theforge.cli.review.GitHubIssueSource", return_value=source),
            patch("theforge.cli.review.run_from_review", return_value=_fake_result()) as run,
            patch("theforge.cli.review._write_audit", return_value=tmp_path / "a.yaml"),
            patch.object(Path, "exists", return_value=True),
        ):
            assert cmd_review(_args(issue=2204, slug="custom")) == 0
        assert run.call_args.args[1].slug == "custom"
        assert run.call_args.args[2] == tmp_path / ".forge/worktrees/custom"

    def test_a_closed_issue_is_reported_not_raised(self, tmp_path, capsys):
        closed = IssueClosedError("#2204 closed")
        config, source = self._patches(tmp_path, None, fetch_error=closed)
        with (
            patch("theforge.cli.review._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.review.load_config", return_value=config),
            patch("theforge.cli.review.GitHubIssueSource", return_value=source),
            patch.object(Path, "exists", return_value=True),
        ):
            assert cmd_review(_args(issue=2204)) == 1
        assert "#2204 closed" in capsys.readouterr().err

    def test_an_unreadable_issue_is_reported_not_raised(self, tmp_path, capsys):
        config, source = self._patches(tmp_path, None, fetch_error=RuntimeError("gh: not found"))
        with (
            patch("theforge.cli.review._find_config", return_value=tmp_path / "forge.yaml"),
            patch("theforge.cli.review.load_config", return_value=config),
            patch("theforge.cli.review.GitHubIssueSource", return_value=source),
            patch.object(Path, "exists", return_value=True),
        ):
            assert cmd_review(_args(issue=2204)) == 1
        assert "could not read issue #2204" in capsys.readouterr().err
