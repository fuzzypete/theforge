from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from theforge.config import ModelProfile
from theforge.coordinator.github_integration import assign_pr_reviewers, post_findings_comment
from theforge.review import ReviewFinding, ReviewResult


def _review() -> ReviewResult:
    return ReviewResult(
        verdict="APPROVE",
        summary="Looks good",
        findings=[
            ReviewFinding(
                severity="P1",
                file="src/app.py",
                line=12,
                observed="Critical bug",
                suggestion="Guard the null case",
            ),
            ReviewFinding(
                severity="P2",
                file="src/lib.py",
                line=34,
                observed="Minor issue",
                suggestion="Rename variable",
            ),
        ],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _profiles() -> list[ModelProfile]:
    return [
        ModelProfile(
            name="reviewer-one",
            cli="claude",
            provider=None,
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=30,
            allowed_tools=(),
            github_handle="alice",
        ),
        ModelProfile(
            name="reviewer-two",
            cli="claude",
            provider=None,
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=30,
            allowed_tools=(),
            github_handle="bob",
        ),
    ]


@patch("theforge.coordinator.github_integration.subprocess.run")
def test_post_findings_comment_posts_markdown_body(mock_run):
    mock_run.return_value = type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    result = post_findings_comment(
        "https://github.com/x/y/pull/1", _review(), _profiles(), Path(".")
    )

    assert result == {"success": True, "error": None}
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["gh", "pr", "comment"]
    body = cmd[cmd.index("--body") + 1]
    assert "**Verdict:** APPROVE" in body
    assert "**P1** `src/app.py:12` — Critical bug" in body
    assert "Suggestion: Guard the null case" in body
    assert "**P2** `src/lib.py:34` — Minor issue" in body
    assert "- reviewer-one" in body
    assert "- reviewer-two" in body


@patch("theforge.coordinator.github_integration.subprocess.run")
def test_assign_pr_reviewers_calls_gh_edit(mock_run):
    mock_run.return_value = type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    result = assign_pr_reviewers("https://github.com/x/y/pull/1", _profiles(), Path("."))

    assert result == {"success": True, "error": None}
    assert mock_run.call_args[0][0] == [
        "gh",
        "pr",
        "edit",
        "https://github.com/x/y/pull/1",
        "--add-reviewer",
        "alice,bob",
    ]


def test_assign_pr_reviewers_noop_when_no_handles():
    profiles = [
        ModelProfile(
            name="reviewer",
            cli="claude",
            provider=None,
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=30,
            allowed_tools=(),
        )
    ]

    with patch("theforge.coordinator.github_integration.subprocess.run") as mock_run:
        result = assign_pr_reviewers("https://github.com/x/y/pull/1", profiles, Path("."))

    assert result == {"success": True, "error": None}
    mock_run.assert_not_called()


@patch("theforge.coordinator.github_integration.subprocess.run")
def test_post_findings_comment_returns_failure_on_nonzero_exit(mock_run):
    mock_run.return_value = type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()

    result = post_findings_comment(
        "https://github.com/x/y/pull/1", _review(), _profiles(), Path(".")
    )

    assert result == {"success": False, "error": "boom"}


@patch("theforge.coordinator.github_integration.subprocess.run")
def test_assign_pr_reviewers_returns_failure_on_nonzero_exit(mock_run):
    mock_run.return_value = type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()

    result = assign_pr_reviewers("https://github.com/x/y/pull/1", _profiles(), Path("."))

    assert result == {"success": False, "error": "boom"}
