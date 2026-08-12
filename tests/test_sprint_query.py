"""Tests for sprint GitHub query helpers (query.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from theforge.sprint.audit import (
    persist_accumulated_story_state,
    persist_accepted_unmeasured_spend,
)
from theforge.sprint.query import (
    MilestoneNotFoundError,
    _get_milestone_number,
    _gh_api_paginate_issues,
    fetch_issues_for_label,
    fetch_issues_for_milestone,
    load_sprint_carry_budget_snapshot,
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

    def test_queries_all_milestones_including_closed(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="7\n", stderr="")
            _get_milestone_number("closed-milestone", tmp_path)
        cmd = mock_run.call_args[0][0]
        assert cmd[-1] == "repos/{owner}/{repo}/milestones?state=all"

    def test_raises_when_not_found(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with pytest.raises(MilestoneNotFoundError, match="not found"):
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

    def test_numeric_title_prefers_exact_match_before_id_fallback(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="12\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            fetch_issues_for_milestone("42", tmp_path)
        second_call_args = mock_run.call_args_list[1][0][0]
        assert "milestone=12" in " ".join(second_call_args)

    def test_closed_numeric_title_prefers_exact_match_before_id_fallback(
        self, tmp_path: Path
    ) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="99\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            fetch_issues_for_milestone("42", tmp_path)
        lookup_call_args = mock_run.call_args_list[0][0][0]
        issues_call_args = mock_run.call_args_list[1][0][0]
        assert lookup_call_args[-1] == "repos/{owner}/{repo}/milestones?state=all"
        assert "milestone=99" in " ".join(issues_call_args)

    def test_numeric_title_falls_back_to_id_when_match_missing(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            fetch_issues_for_milestone("42", tmp_path)
        second_call_args = mock_run.call_args_list[1][0][0]
        assert "milestone=42" in " ".join(second_call_args)

    def test_numeric_title_does_not_fall_back_when_lookup_fails(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth failure")
            with pytest.raises(RuntimeError, match="auth failure"):
                fetch_issues_for_milestone("42", tmp_path)
        assert mock_run.call_count == 1

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


def _set_existing_sprint_id(
    tmp_path: Path,
    sprint_name: str = "Test Sprint",
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


class TestLoadSprintCarryBudgetSnapshot:
    def test_resume_uses_sprint_audit_when_progressive_state_is_absent(
        self, tmp_path: Path
    ) -> None:
        sprint_id = _set_existing_sprint_id(tmp_path)
        _write_prior_sprint_audit(tmp_path, sprint_id, 6.0)

        snapshot = load_sprint_carry_budget_snapshot(
            project_root=tmp_path,
            sprint_name="Test Sprint",
            selected_slugs=["issue-1"],
            resume=True,
        )

        assert snapshot.sprint_id == sprint_id
        assert snapshot.carried_cost_usd == pytest.approx(6.0)
        assert snapshot.verification_spend_usd == pytest.approx(6.0)

    def test_resume_loads_accepted_unmeasured_ceiling_from_persisted_audit(
        self, tmp_path: Path
    ) -> None:
        sprint_id = _set_existing_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
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
                "Test Sprint",
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

        snapshot = load_sprint_carry_budget_snapshot(
            project_root=tmp_path,
            sprint_name="Test Sprint",
            selected_slugs=["issue-1"],
            resume=True,
        )

        assert snapshot.carried_cost_usd == pytest.approx(6.0)
        assert snapshot.accepted_unmeasured_ceiling_usd == pytest.approx(4.5)
        assert snapshot.unresolved_unmeasured_sources == ()
        assert snapshot.headroom_is_lower_bound is False
        assert snapshot.verification_spend_usd == pytest.approx(10.5)

    def test_resume_marks_incomplete_prior_cost_as_lower_bound(self, tmp_path: Path) -> None:
        sprint_id = _set_existing_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
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

        snapshot = load_sprint_carry_budget_snapshot(
            project_root=tmp_path,
            sprint_name="Test Sprint",
            selected_slugs=["issue-1"],
            resume=True,
        )

        assert snapshot.carried_cost_usd == pytest.approx(6.0)
        assert snapshot.accepted_unmeasured_ceiling_usd == 0.0
        assert snapshot.unresolved_unmeasured_sources == (
            "carried:issue-1",
            "carried:prior-generation",
        )
        assert snapshot.headroom_is_lower_bound is True
        assert snapshot.verification_spend_usd == pytest.approx(6.0)

    def test_non_resume_ignores_existing_prior_spend(self, tmp_path: Path) -> None:
        sprint_id = _set_existing_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "issue:1",
                    "slug": "issue-1",
                    "path": "issue-1.md",
                    "outcome": "DONE",
                    "cost_usd": 6.0,
                    "story_run_id": "run-prev",
                }
            ],
        )

        snapshot = load_sprint_carry_budget_snapshot(
            project_root=tmp_path,
            sprint_name="Test Sprint",
            selected_slugs=["issue-1"],
            resume=False,
            reexec=False,
        )

        assert snapshot.sprint_id == sprint_id
        assert snapshot.carried_cost_usd == 0.0
        assert snapshot.verification_spend_usd == 0.0
