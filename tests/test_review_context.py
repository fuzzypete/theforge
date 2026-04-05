from pathlib import Path
from unittest.mock import patch

from theforge.coordinator.review_context import _get_commit_diffs, _get_commit_log


def test_get_commit_log_uses_oneline(tmp_path: Path) -> None:
    with (
        patch("theforge.coordinator.review_context._has_uncommitted_changes", return_value=False),
        patch(
            "theforge.coordinator.review_context._cu._run_shell",
            return_value=(True, "abc123 feat: thing"),
        ) as run_shell,
    ):
        result = _get_commit_log(tmp_path, "main")

    assert result == "abc123 feat: thing"
    run_shell.assert_called_once_with("git log main..HEAD --oneline --reverse", tmp_path)


def test_get_commit_diffs_truncates_per_commit(tmp_path: Path) -> None:
    responses = [
        (True, "abc1234 first commit"),
        (True, "x" * 20),
    ]

    with patch("theforge.coordinator.review_context._cu._run_shell", side_effect=responses):
        result = _get_commit_diffs(tmp_path, "main", per_commit_limit=10, total_limit=100)

    assert "x" * 10 in result
    assert "[... diff truncated at 10 chars ...]" in result


def test_get_commit_diffs_truncates_total_output(tmp_path: Path) -> None:
    responses = [
        (True, "abc1234 first commit\ndef5678 second commit"),
        (True, "first diff"),
        (True, "second diff is long"),
    ]

    with patch("theforge.coordinator.review_context._cu._run_shell", side_effect=responses):
        result = _get_commit_diffs(tmp_path, "main", per_commit_limit=100, total_limit=20)

    assert "first diff" in result
    assert "[... remaining 1 commits omitted — total diff too large ...]" in result
    assert "second diff is long" not in result
