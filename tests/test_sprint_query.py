"""Tests for sprint GitHub query helpers (query.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.sprint.query import (
    _get_milestone_number,
    _gh_api_paginate_issues,
    fetch_issues_for_label,
    fetch_issues_for_milestone,
)


class TestGhApiPaginateIssues:
    def test_returns_parsed_issues(self, tmp_path: Path) -> None:
        ndjson = "\n".join(
            [
                json.dumps({"number": 1, "title": "First"}),
                json.dumps({"number": 2, "title": "Second"}),
            ]
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=ndjson, stderr="")
            issues = _gh_api_paginate_issues("repos/{owner}/{repo}/issues?state=open", tmp_path)
        assert issues == [{"number": 1, "title": "First"}, {"number": 2, "title": "Second"}]

    def test_empty_result_returns_empty_list(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            issues = _gh_api_paginate_issues("repos/{owner}/{repo}/issues?state=open", tmp_path)
        assert issues == []

    def test_raises_on_nonzero_exit(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth failure")
            with pytest.raises(RuntimeError, match="auth failure"):
                _gh_api_paginate_issues("repos/{owner}/{repo}/issues", tmp_path)

    def test_raises_on_malformed_json(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not-json\n", stderr="")
            with pytest.raises(RuntimeError, match="malformed JSON"):
                _gh_api_paginate_issues("repos/{owner}/{repo}/issues", tmp_path)

    def test_uses_paginate_flag(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _gh_api_paginate_issues("repos/{owner}/{repo}/issues", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "--paginate" in cmd

    def test_uses_project_root_as_cwd(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _gh_api_paginate_issues("repos/{owner}/{repo}/issues", tmp_path)
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)


class TestGetMilestoneNumber:
    def test_returns_number_for_matching_title(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="7\n", stderr="")
            number = _get_milestone_number("v1.0", tmp_path)
        assert number == "7"

    def test_raises_when_not_found(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with pytest.raises(RuntimeError, match="not found"):
                _get_milestone_number("nonexistent", tmp_path)

    def test_raises_on_gh_failure(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="bad credentials")
            with pytest.raises(RuntimeError, match="bad credentials"):
                _get_milestone_number("v1.0", tmp_path)

    def test_uses_paginate_flag(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="3\n", stderr="")
            _get_milestone_number("v1.0", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "--paginate" in cmd


class TestFetchIssuesForMilestone:
    def test_fetches_and_sorts_issues(self, tmp_path: Path) -> None:
        ndjson = "\n".join(
            [
                json.dumps({"number": 5, "title": "E"}),
                json.dumps({"number": 2, "title": "B"}),
            ]
        )
        with patch("subprocess.run") as mock_run:
            # First call: milestone lookup → returns number "3"
            # Second call: issues list → returns NDJSON
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="3\n", stderr=""),
                MagicMock(returncode=0, stdout=ndjson, stderr=""),
            ]
            issues = fetch_issues_for_milestone("v1.0", tmp_path)
        assert issues == [{"number": 2, "title": "B"}, {"number": 5, "title": "E"}]

    def test_endpoint_includes_milestone_number(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="42\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            fetch_issues_for_milestone("M1: Sprint", tmp_path)
        second_call_args = mock_run.call_args_list[1][0][0]
        assert "milestone=42" in " ".join(second_call_args)

    def test_raises_when_milestone_not_found(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with pytest.raises(RuntimeError, match="not found"):
                fetch_issues_for_milestone("missing", tmp_path)

    def test_no_hardcoded_limit(self, tmp_path: Path) -> None:
        """gh api --paginate must be used; no --limit flag allowed."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="1\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            fetch_issues_for_milestone("v1.0", tmp_path)
        for call_args in mock_run.call_args_list:
            cmd = call_args[0][0]
            assert "--limit" not in cmd, f"--limit should not appear in: {cmd}"


class TestFetchIssuesForLabel:
    def test_fetches_and_sorts_issues(self, tmp_path: Path) -> None:
        ndjson = "\n".join(
            [
                json.dumps({"number": 10, "title": "J"}),
                json.dumps({"number": 3, "title": "C"}),
            ]
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=ndjson, stderr="")
            issues = fetch_issues_for_label("sprint", tmp_path)
        assert issues == [{"number": 3, "title": "C"}, {"number": 10, "title": "J"}]

    def test_endpoint_includes_label(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            fetch_issues_for_label("my-label", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert any("my-label" in arg for arg in cmd)

    def test_url_encodes_label_with_spaces(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            fetch_issues_for_label("needs review", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert any("needs%20review" in arg for arg in cmd)

    def test_no_hardcoded_limit(self, tmp_path: Path) -> None:
        """gh api --paginate must be used; no --limit flag allowed."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            fetch_issues_for_label("sprint", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "--limit" not in cmd, f"--limit should not appear in: {cmd}"

    def test_uses_paginate_flag(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            fetch_issues_for_label("sprint", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert "--paginate" in cmd
