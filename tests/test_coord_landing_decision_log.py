"""Tests that land_story / _merge_pr / _merge_branch surface their landing
decision via both an operator-visible log line and structured fields in the
returned (audit-bound) merge dict.

Story #1423: silent early-return paths (already-merged, zero-delta) must name
the path taken and the evidence acted on so the audit trail is reviewable
without re-reading source.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.coordinator.completion import _merge_pr
from theforge.coordinator.workspace import _merge_branch

PR_URL = "https://github.com/owner/repo/pull/1143"


def _ok(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _make_config(tmp_path: Path):
    from theforge.config import (
        DEFAULT_DEV_PROFILE,
        DEFAULT_PREFLIGHT_PROFILE,
        DEFAULT_REVIEW_PROFILE,
        DEFAULT_VALIDATION,
        ForgeConfig,
        LogConfig,
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
            on_approve="merge-pr",
            auto_push=True,
            merge_strategy="squash",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_task(tmp_path: Path):
    from theforge.task import TaskStory

    spec = tmp_path / "spec.md"
    spec.write_text("# Test", encoding="utf-8")
    return TaskStory(name="Test Task", story_path=spec, slug="test-task")


def _make_review():
    from theforge.review import ReviewResult

    return ReviewResult(
        verdict="APPROVE",
        summary="Looks good.",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


class TestMergePrAlreadyMergedShortCircuit:
    def test_emits_operator_log_line_and_audit_fields(self, tmp_path: Path, capfd) -> None:
        """already-merged guard must log LANDING line + populate landing_path/guard_evidence."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        merged_pr_payload = json.dumps(
            [
                {
                    "number": 1143,
                    "url": PR_URL,
                    "mergedAt": "2026-04-30T14:21:12Z",
                }
            ]
        )

        head_sha = "abc1234abc1234abc1234abc1234abc1234abc1"
        (tmp_path / "test-task").mkdir(parents=True, exist_ok=True)

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout=merged_pr_payload)
            if isinstance(cmd, list) and cmd[:2] == ["git", "rev-parse"]:
                return _ok(0, stdout=f"{head_sha}\n")
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "view"]:
                return _ok(0, stdout=f'{{"commits": [{{"oid": "{head_sha}"}}]}}')
            return _ok(0, stdout="")

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch("theforge.coordinator.completion._create_pr") as mock_create_pr,
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/issue-1135", review, state)

        # AC1: Operator-visible log line names path + evidence.
        captured = capfd.readouterr()
        log_text = captured.err + captured.out
        assert "LANDING already-merged short-circuit" in log_text
        assert "branch forge/issue-1135" in log_text
        assert "PR #1143" in log_text
        assert "mergedAt=2026-04-30T14:21:12Z" in log_text

        # AC2/AC3: Audit-bound dict identifies guard + evidence.
        assert result["success"] is True
        assert result["merged"] is True
        assert result["landing_path"] == "already-merged"
        ge = result["guard_evidence"]
        assert ge["pr_number"] == 1143
        assert ge["pr_url"] == PR_URL
        assert ge["merged_at"] == "2026-04-30T14:21:12Z"
        assert ge["branch"] == "forge/issue-1135"

        # No fresh PR was created — distinguishes a guard fire from a real merge.
        mock_create_pr.assert_not_called()


class TestMergePrZeroDeltaShortCircuit:
    def test_emits_operator_log_line_and_audit_fields(self, tmp_path: Path, capfd) -> None:
        """zero-delta must log LANDING line + populate landing_path/guard_evidence."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        zero_delta_pr_result = {
            "action": "pr",
            "pr_url": None,
            "success": True,
            "error": None,
            "skipped": True,
            "skip_reason": "zero-delta branch",
            "ahead_commit_count": 0,
        }

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout="[]")
            return _ok(0, stdout="MERGED")

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value=zero_delta_pr_result,
            ),
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/issue-1135", review, state)

        captured = capfd.readouterr()
        log_text = captured.err + captured.out
        assert "LANDING zero-delta short-circuit" in log_text
        assert "branch forge/issue-1135" in log_text
        assert "0 commits ahead of origin/main" in log_text

        assert result["success"] is True
        assert result["pr_url"] is None
        assert result["landing_path"] == "zero-delta"
        ge = result["guard_evidence"]
        assert ge["branch"] == "forge/issue-1135"
        assert ge["base_branch"] == "main"
        assert ge["ahead_commit_count"] == 0


class TestMergePrFreshMergeAnnotation:
    def test_fresh_merge_includes_landing_path_for_audit_distinguishability(
        self, tmp_path: Path
    ) -> None:
        """A normal landing must annotate landing_path so reviewers can tell it
        from a guard short-circuit reading the audit alone."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review()
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        def fake_run(cmd, **kw):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "list"]:
                return _ok(0, stdout="[]")
            return _ok(0, stdout="MERGED")

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": PR_URL,
                    "success": True,
                    "error": None,
                },
            ),
            patch("theforge.coordinator.completion._deindex_forge_artifacts"),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        assert result["landing_path"] == "fresh-merge"
        assert result["guard_evidence"]["pr_url"] == PR_URL
        assert result["guard_evidence"]["branch"] == "forge/test-task"


class TestMergeBranchZeroDelta:
    def test_zero_delta_local_merge_logs_landing_line_and_annotates_info(
        self, tmp_path: Path, capfd
    ) -> None:
        """The local _merge_branch path (effective_on_approve='merge') must
        also surface the zero-delta short-circuit."""

        # Stub _run_shell so base branch exists, project root is clean, but
        # the branch has no commits ahead.
        def fake_shell(cmd, cwd):
            cmd_s = str(cmd)
            if "git branch --list" in cmd_s:
                return True, "main\n"
            if "git status --porcelain" in cmd_s:
                return True, ""
            if "git log" in cmd_s:
                return True, ""  # no commits ahead
            return True, ""

        with patch(
            "theforge.coordinator.workspace._cu._run_shell",
            side_effect=fake_shell,
        ):
            info = _merge_branch(
                project_root=tmp_path,
                base_branch="main",
                branch_name="forge/issue-1135",
                slug="issue-1135",
                workspace_path=tmp_path / "issue-1135",
            )

        captured = capfd.readouterr()
        log_text = captured.err + captured.out
        assert "LANDING zero-delta short-circuit" in log_text
        assert "branch forge/issue-1135" in log_text

        assert info["merged"] is False
        assert info["landing_path"] == "zero-delta"
        assert info["skipped"] is True
        assert info["skip_reason"] == "zero-delta branch"
        ge = info["guard_evidence"]
        assert ge["branch"] == "forge/issue-1135"
        assert ge["base_branch"] == "main"
        assert ge["ahead_commit_count"] == 0
