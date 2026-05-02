"""Tests for on_approve: merge-pr — create PR and immediately merge it."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from coord_test_helpers import (
    _make_config,
    _make_task,
)

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
from theforge.coordinator.completion import MAX_MERGE_RETRIES, _create_pr, _merge_pr
from theforge.review import ReviewResult, parse_review_json

# ── Helpers ───────────────────────────────────────────────────────


def _make_merge_pr_config(tmp_path: Path, merge_strategy: str = "squash") -> ForgeConfig:
    """Config with on_approve=merge-pr, auto_push=True."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            on_approve="merge-pr",
            auto_push=True,
            merge_strategy=merge_strategy,
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_review_result(summary: str = "Looks good.") -> ReviewResult:
    return ReviewResult(
        verdict="APPROVE",
        summary=summary,
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _make_subprocess_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ── Config validation ─────────────────────────────────────────────


class TestMergePrConfigValidation:
    def test_merge_pr_requires_auto_push(self) -> None:
        from theforge.config._loaders import _parse_workspace

        with pytest.raises(ValueError, match="auto_push"):
            _parse_workspace({"on_approve": "merge-pr", "auto_push": False})

    def test_merge_pr_with_auto_push_ok(self) -> None:
        from theforge.config._loaders import _parse_workspace

        result = _parse_workspace({"on_approve": "merge-pr", "auto_push": True})
        assert result.on_approve == "merge-pr"
        assert result.auto_push is True

    def test_merge_pr_default_merge_strategy_squash(self) -> None:
        from theforge.config._loaders import _parse_workspace

        result = _parse_workspace({"on_approve": "merge-pr", "auto_push": True})
        assert result.merge_strategy == "squash"

    def test_merge_strategy_rebase(self) -> None:
        from theforge.config._loaders import _parse_workspace

        result = _parse_workspace(
            {"on_approve": "merge-pr", "auto_push": True, "merge_strategy": "rebase"}
        )
        assert result.merge_strategy == "rebase"

    def test_merge_strategy_merge(self) -> None:
        from theforge.config._loaders import _parse_workspace

        result = _parse_workspace(
            {"on_approve": "merge-pr", "auto_push": True, "merge_strategy": "merge"}
        )
        assert result.merge_strategy == "merge"

    def test_auto_push_false_with_other_on_approve_ok(self) -> None:
        """auto_push:false is fine for merge or pr."""
        from theforge.config._loaders import _parse_workspace

        result = _parse_workspace({"on_approve": "merge", "auto_push": False})
        assert result.on_approve == "merge"


# ── _merge_pr unit tests ──────────────────────────────────────────


class TestMergePrFunction:
    """Unit tests for the _merge_pr() function in coordinator/completion.py."""

    def _run_merge_pr(
        self,
        tmp_path: Path,
        *,
        merge_strategy: str = "squash",
        fetch_ok: bool = True,
        rebase_ok: bool = True,
        push_ok: bool = True,
        push_stderr: str = "push error",
        create_pr_result: dict | None = None,
        gh_merge_ok: bool = True,
        gh_merge_results: list[MagicMock] | None = None,
        fetch_after_ok: bool = True,
    ) -> dict:
        """Run _merge_pr with mocked subprocess.run and _create_pr."""
        config = _make_merge_pr_config(tmp_path, merge_strategy=merge_strategy)
        task = _make_task(tmp_path)
        branch_name = "forge/test-task"
        review = _make_review_result()

        state = MagicMock()
        state.total_cost = 1.5
        state.dev_iteration = 1
        state.cycle_history = []
        state.cycle_history_total = 0
        state.last_cycle_reviewer_results = []
        state.review_cycle_metadata = []
        state.review_results = [review]

        if create_pr_result is None:
            create_pr_result = {
                "action": "pr",
                "pr_url": "https://github.com/fuzzypete/theforge/pull/42",
                "success": True,
                "error": None,
            }

        def _fake_run(cmd, **kwargs):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "fetch" in cmd_str and "origin" in cmd_str and "merge" not in cmd_str:
                if cmd == ["git", "fetch", "origin"]:
                    # post-merge fetch (step 5)
                    return _make_subprocess_result(0 if fetch_after_ok else 1)
                return _make_subprocess_result(0 if fetch_ok else 1, stderr="fetch error")
            if "rebase" in cmd_str and "--abort" not in cmd_str:
                return _make_subprocess_result(0 if rebase_ok else 1, stderr="conflict!")
            if "rebase" in cmd_str and "--abort" in cmd_str:
                return _make_subprocess_result(0)
            if "push" in cmd_str and "-f" in cmd_str:
                return _make_subprocess_result(0 if push_ok else 1, stderr=push_stderr)
            if "merge" in cmd_str and "ff-only" in cmd_str:
                return _make_subprocess_result(0)
            return _make_subprocess_result(0)

        def _fake_gh_run(cmd, **kwargs):
            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "gh" in cmd_str and "pr" in cmd_str and "list" in cmd_str:
                return _make_subprocess_result(0, stdout="[]")
            if "gh" in cmd_str and "pr" in cmd_str and "merge" in cmd_str:
                if gh_merge_results is not None:
                    if gh_merge_results:
                        return gh_merge_results.pop(0)
                    return _make_subprocess_result(0)
                rc = 0 if gh_merge_ok else 1
                err = "" if gh_merge_ok else "merge failed"
                return _make_subprocess_result(rc, stderr=err)
            if "gh" in cmd_str and "pr" in cmd_str and "view" in cmd_str:
                # Default: report MERGED so merged=True in the common case.
                return _make_subprocess_result(0, stdout="MERGED")
            return _make_subprocess_result(0)

        with (
            patch(
                "theforge.coordinator.completion.subprocess.run",
                side_effect=lambda cmd, **kw: (
                    _fake_gh_run(cmd, **kw)
                    if isinstance(cmd, list) and cmd and cmd[0] == "gh"
                    else _fake_run(cmd, **kw)
                ),
            ),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value=create_pr_result,
            ),
        ):
            return _merge_pr(config, task, branch_name, review, state)

    def test_happy_path_returns_merged_true(self, tmp_path: Path) -> None:
        result = self._run_merge_pr(tmp_path)
        assert result["merged"] is True
        assert result["success"] is True
        assert result["action"] == "merge-pr"
        assert result["pr_url"] == "https://github.com/fuzzypete/theforge/pull/42"
        assert result["error"] is None

    def test_fetch_failure_returns_error(self, tmp_path: Path) -> None:
        result = self._run_merge_pr(tmp_path, fetch_ok=False)
        assert result["merged"] is False
        assert result["success"] is False
        assert "fetch" in result["error"].lower()

    def test_rebase_failure_escalates(self, tmp_path: Path) -> None:
        result = self._run_merge_pr(tmp_path, rebase_ok=False)
        assert result["merged"] is False
        assert result["success"] is False
        assert "rebase" in result["error"].lower() or "escalat" in result["error"].lower()

    def test_push_failure_returns_error(self, tmp_path: Path) -> None:
        result = self._run_merge_pr(tmp_path, push_ok=False)
        assert result["merged"] is False
        assert result["success"] is False
        assert "push" in result["error"].lower()

    def test_push_missing_refspec_after_remote_branch_deleted_is_non_fatal(
        self, tmp_path: Path
    ) -> None:
        result = self._run_merge_pr(
            tmp_path,
            push_ok=False,
            push_stderr="error: src refspec forge/test-task does not match any",
            gh_merge_ok=True,
            gh_merge_results=[_make_subprocess_result(0)],
        )
        assert result["merged"] is True
        assert result["success"] is True

    def test_already_merged_branch_skips_push_and_pr_recreation(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list):
                calls.append(cmd)
                if cmd[:3] == ["gh", "pr", "list"]:
                    return _make_subprocess_result(
                        0,
                        stdout=(
                            '[{"url": "https://github.com/fuzzypete/theforge/pull/42", '
                            '"mergedAt": "2025-01-01T00:00:00Z"}]'
                        ),
                    )
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch("theforge.coordinator.completion._create_pr") as mock_create_pr,
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result == {
            "action": "merge-pr",
            "pr_url": "https://github.com/fuzzypete/theforge/pull/42",
            "merged": True,
            "merge_queued": False,
            "auto_merge_queued": False,
            "success": True,
            "error": None,
        }
        mock_create_pr.assert_not_called()
        assert calls[0][:3] == ["gh", "pr", "list"]
        assert not any(cmd[:3] == ["git", "push", "-f"] for cmd in calls)
        assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)

    def test_pr_creation_failure_no_merge(self, tmp_path: Path) -> None:
        failed_pr = {"action": "pr", "pr_url": None, "success": False, "error": "gh auth failed"}
        result = self._run_merge_pr(tmp_path, create_pr_result=failed_pr)
        assert result["merged"] is False
        assert result["success"] is False
        assert result["pr_url"] is None

    def test_gh_merge_failure_returns_pr_url(self, tmp_path: Path) -> None:
        result = self._run_merge_pr(tmp_path, gh_merge_ok=False)
        assert result["merged"] is False
        assert result["success"] is False
        assert result["pr_url"] == "https://github.com/fuzzypete/theforge/pull/42"
        assert "merge failed" in result["error"] or "gh pr merge" in result["error"].lower()

    def test_base_branch_modified_retries_then_succeeds(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        call_counts = {"fetch": 0, "rebase": 0, "push": 0}
        merge_results = [
            _make_subprocess_result(
                1,
                stderr="GraphQL: Base branch was modified. Review and try the merge again.",
            ),
            _make_subprocess_result(0),
        ]

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:4] == [
                "git",
                "fetch",
                "origin",
                config.workspace.base_branch,
            ]:
                call_counts["fetch"] += 1
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--abort" not in cmd:
                call_counts["rebase"] += 1
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd[:4] == ["git", "push", "-f", "origin"]:
                call_counts["push"] += 1
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "merge"]:
                return merge_results.pop(0)
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "view"]:
                return _make_subprocess_result(0, stdout="MERGED")
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/42",
                    "success": True,
                    "error": None,
                },
            ) as mock_create_pr,
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        assert result["merged"] is True
        assert mock_create_pr.call_count == 1
        assert call_counts == {"fetch": 2, "rebase": 2, "push": 2}

    def test_base_branch_modified_exhausts_retry_limit(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        call_counts = {"fetch": 0, "rebase": 0, "push": 0, "merge": 0}
        merge_error = "GraphQL: Base branch was modified. Review and try the merge again."

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:4] == [
                "git",
                "fetch",
                "origin",
                config.workspace.base_branch,
            ]:
                call_counts["fetch"] += 1
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--abort" not in cmd:
                call_counts["rebase"] += 1
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd[:4] == ["git", "push", "-f", "origin"]:
                call_counts["push"] += 1
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "merge"]:
                call_counts["merge"] += 1
                return _make_subprocess_result(1, stderr=merge_error)
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/42",
                    "success": True,
                    "error": None,
                },
            ) as mock_create_pr,
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is False
        assert result["merged"] is False
        assert result["pr_url"] == "https://github.com/fuzzypete/theforge/pull/42"
        assert merge_error in result["error"]
        assert mock_create_pr.call_count == 1
        assert call_counts == {
            "fetch": MAX_MERGE_RETRIES,
            "rebase": MAX_MERGE_RETRIES,
            "push": MAX_MERGE_RETRIES,
            "merge": MAX_MERGE_RETRIES,
        }

    def test_non_retryable_merge_failure_does_not_retry(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        call_counts = {"fetch": 0, "rebase": 0, "push": 0, "merge": 0}

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:4] == [
                "git",
                "fetch",
                "origin",
                config.workspace.base_branch,
            ]:
                call_counts["fetch"] += 1
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"] and "--abort" not in cmd:
                call_counts["rebase"] += 1
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd[:4] == ["git", "push", "-f", "origin"]:
                call_counts["push"] += 1
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "merge"]:
                call_counts["merge"] += 1
                return _make_subprocess_result(1, stderr="merge blocked by branch protection")
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/42",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is False
        assert result["merged"] is False
        assert "branch protection" in result["error"]
        assert call_counts == {"fetch": 1, "rebase": 1, "push": 1, "merge": 1}

    def test_auto_flag_passed_to_gh_merge(self, tmp_path: Path) -> None:
        """Verify --auto is unconditionally passed to gh pr merge."""
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        gh_calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "gh":
                gh_calls.append(cmd)
                if cmd[:3] == ["gh", "pr", "view"]:
                    return _make_subprocess_result(0, stdout="MERGED")
                return _make_subprocess_result(0)
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/auto",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            _merge_pr(config, task, "forge/test-task", review, state)

        merge_calls = [c for c in gh_calls if len(c) >= 3 and c[1] == "pr" and c[2] == "merge"]
        assert merge_calls, "Expected at least one gh pr merge call"
        assert all("--auto" in c for c in merge_calls), "All gh pr merge calls must include --auto"

    def test_merge_strategy_squash_passed_to_gh(self, tmp_path: Path) -> None:
        """Verify --squash is passed to gh pr merge."""
        config = _make_merge_pr_config(tmp_path, merge_strategy="squash")
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        gh_calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "gh":
                gh_calls.append(cmd)
                return _make_subprocess_result(0)
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/1",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            _merge_pr(config, task, "forge/test-task", review, state)

        assert any("--squash" in c for c in gh_calls)

    def test_merge_strategy_rebase_passed_to_gh(self, tmp_path: Path) -> None:
        """Verify --rebase is passed to gh pr merge."""
        config = _make_merge_pr_config(tmp_path, merge_strategy="rebase")
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        gh_calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "gh":
                gh_calls.append(cmd)
                return _make_subprocess_result(0)
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/2",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            _merge_pr(config, task, "forge/test-task", review, state)

        assert any("--rebase" in c for c in gh_calls)

    def test_auto_merge_queue_skips_remote_branch_deletion(self, tmp_path: Path) -> None:
        """Queued auto-merge must not delete the remote branch before GitHub merges."""
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        commands: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list):
                commands.append(cmd)
                if cmd[:4] == [
                    "gh",
                    "pr",
                    "merge",
                    "https://github.com/fuzzypete/theforge/pull/queued",
                ]:
                    if "--auto" in cmd:
                        return _make_subprocess_result(0)
                    return _make_subprocess_result(1, stderr="required status checks must pass")
                if cmd[:3] == ["gh", "pr", "view"]:
                    return _make_subprocess_result(0, stdout="", stderr="OPEN")
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/queued",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        assert result["merged"] is False
        assert result["merge_queued"] is True
        assert ["git", "push", "origin", "--delete", "forge/test-task"] not in commands

    def test_immediate_merge_still_deletes_remote_branch(self, tmp_path: Path) -> None:
        """Completed merges should still perform remote branch cleanup."""
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        commands: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list):
                commands.append(cmd)
                if cmd[:3] == ["gh", "pr", "merge"]:
                    return _make_subprocess_result(0, stdout="Merged pull request")
                if cmd[:3] == ["gh", "pr", "view"]:
                    return _make_subprocess_result(0, stdout="MERGED")
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/merged",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        assert result["merged"] is True
        assert ["git", "push", "origin", "--delete", "forge/test-task"] in commands

    def test_branch_protection_fallback_immediate_merge(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "merge"]:
                if "--auto" in cmd:
                    return _make_subprocess_result(0)
                return _make_subprocess_result(1, stderr="required status checks must pass")
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "view"]:
                return _make_subprocess_result(0, stdout="MERGED")
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/42",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)
        assert result["success"] is True
        assert result["merged"] is True
        assert result["merge_queued"] is False

    def test_auto_merge_queued_when_checks_pending(self, tmp_path: Path) -> None:
        """--auto succeeds but PR is not yet MERGED → merge_queued=True."""
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "merge"]:
                assert "--auto" in cmd, "merge call must use --auto"
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "view"]:
                return _make_subprocess_result(0, stdout="OPEN")
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/42",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        assert result["merged"] is False
        assert result["merge_queued"] is True
        assert result["auto_merge_queued"] is True

    def test_non_protection_failure_returns_fail_without_auto(self, tmp_path: Path) -> None:
        result = self._run_merge_pr(
            tmp_path,
            gh_merge_results=[_make_subprocess_result(1, stderr="authentication failed")],
        )
        assert result["success"] is False
        assert result["merged"] is False
        assert result["merge_queued"] is False

    def test_delete_branch_not_passed_to_gh(self, tmp_path: Path) -> None:
        """gh pr merge avoids local cleanup flags that trip worktree constraints."""
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        gh_calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "gh":
                gh_calls.append(cmd)
                return _make_subprocess_result(0)
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/3",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            _merge_pr(config, task, "forge/test-task", review, state)

        assert gh_calls
        assert all("--delete-branch" not in c for c in gh_calls)

    def test_gh_merge_runs_from_project_root_not_worktree(self, tmp_path: Path) -> None:
        """gh pr merge should run from repo root so it avoids worktree checkout conflicts."""
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        merge_cwds: list[Path] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "merge"]:
                merge_cwds.append(Path(kwargs["cwd"]))
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/4",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            _merge_pr(config, task, "forge/test-task", review, state)

        assert merge_cwds == [tmp_path]

    def test_rebase_abort_called_on_conflict(self, tmp_path: Path) -> None:
        """When rebase fails, git rebase --abort is called."""
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]

        abort_called = {"v": False}

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and "rebase" in cmd:
                if "--abort" in cmd:
                    abort_called["v"] = True
                    return _make_subprocess_result(0)
                return _make_subprocess_result(1, stderr="conflict!")
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch("theforge.coordinator.completion._create_pr"),
        ):
            _merge_pr(config, task, "forge/test-task", review, state)

        assert abort_called["v"]


# ── _finalize_approve with merge-pr ──────────────────────────────


class TestFinalizeApproveMergePr:
    """Test _finalize_approve routes merge-pr correctly."""

    def test_merge_pr_path_in_finalize_approve(self, tmp_path: Path) -> None:
        """_finalize_approve defers landing — sets pending_integration, never calls _merge_pr."""
        from theforge.coordinator.completion import _finalize_approve
        from theforge.coordinator.state import CoordinatorState, Phase

        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()
        state.phase = Phase.REVIEW
        import time

        with patch(
            "theforge.coordinator.completion._merge_pr",
        ) as mock_merge_pr:
            result = _finalize_approve(
                state,
                config,
                task,
                review,
                tmp_path,
                "forge/test-task",
                time.monotonic(),
                auto_merge=False,
                notify=False,
                logger=None,
                review_cost=0.5,
                review_elapsed=1.0,
                message="done. ",
            )

        assert result.success is True
        assert result.merge is not None
        assert result.merge["action"] == "merge-pr"
        assert result.merge.get("pending") is True
        assert result.landing_status == "pending_integration"
        mock_merge_pr.assert_not_called()

    def test_merge_pr_failure_in_finalize_approve(self, tmp_path: Path) -> None:
        """_finalize_approve defers landing — returns pending_integration, never fails."""
        from theforge.coordinator.completion import _finalize_approve
        from theforge.coordinator.state import CoordinatorState, Phase

        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()
        state.phase = Phase.REVIEW
        import time

        result = _finalize_approve(
            state,
            config,
            task,
            review,
            tmp_path,
            "forge/test-task",
            time.monotonic(),
            auto_merge=False,
            notify=False,
            logger=None,
            review_cost=0.5,
            review_elapsed=1.0,
            message="done. ",
        )

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.landing_status == "pending_integration"
        assert result.merge == {"action": "merge-pr", "pending": True}

    def test_auto_merge_overrides_merge_pr(self, tmp_path: Path) -> None:
        """When auto_merge=True is passed, merge-pr config is overridden to use local merge."""
        from theforge.coordinator.completion import _finalize_approve
        from theforge.coordinator.state import CoordinatorState, Phase

        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()
        state.phase = Phase.REVIEW
        import time

        with (
            patch(
                "theforge.coordinator.completion._merge_pr",
            ) as mock_merge_pr,
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={"merged": True, "error": None},
            ),
        ):
            _finalize_approve(
                state,
                config,
                task,
                review,
                tmp_path,
                "forge/test-task",
                time.monotonic(),
                auto_merge=True,
                notify=False,
                logger=None,
                review_cost=0.5,
                review_elapsed=1.0,
                message="done. ",
            )

        mock_merge_pr.assert_not_called()


class TestPreMergeDirtyWorktreeCleanup:
    """Pre-merge worktree cleanup lives in land_story(), not _finalize_approve()."""

    def test_tracked_dirty_worktree_is_committed_before_merge(self, tmp_path: Path) -> None:
        from theforge.coordinator.completion import land_story
        from theforge.coordinator.state import CoordinatorState

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()

        shell_calls: list[str] = []

        def _fake_run_shell(cmd: str, cwd: Path):
            shell_calls.append(cmd)
            if cmd == "git status --porcelain":
                return True, " M tracked.py\nA  added.py\nD  removed.py"
            if cmd == "git add -- tracked.py added.py removed.py":
                return True, ""
            if cmd == 'git commit -m "chore: commit remaining worktree changes before merge"':
                return True, "[feat abc123] cleanup"
            raise AssertionError(cmd)

        with (
            patch("theforge.coordinator.completion._run_shell", side_effect=_fake_run_shell),
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={"merged": True, "error": None},
            ) as mock_merge_branch,
        ):
            merge_info, landing_status = land_story(
                config, task, "forge/test-task", tmp_path, review, state, "merge"
            )

        assert landing_status == "landed"
        assert shell_calls == [
            "git status --porcelain",
            "git add -- tracked.py added.py removed.py",
            'git commit -m "chore: commit remaining worktree changes before merge"',
        ]
        mock_merge_branch.assert_called_once()

    def test_clean_worktree_skips_pre_merge_commit(self, tmp_path: Path) -> None:
        from theforge.coordinator.completion import land_story
        from theforge.coordinator.state import CoordinatorState

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()

        shell_calls: list[str] = []

        def _fake_run_shell(cmd: str, cwd: Path):
            shell_calls.append(cmd)
            if cmd == "git status --porcelain":
                return True, ""
            raise AssertionError(cmd)

        with (
            patch("theforge.coordinator.completion._run_shell", side_effect=_fake_run_shell),
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={"merged": True, "error": None},
            ) as mock_merge_branch,
        ):
            merge_info, landing_status = land_story(
                config, task, "forge/test-task", tmp_path, review, state, "merge"
            )

        assert landing_status == "landed"
        assert shell_calls == ["git status --porcelain"]
        mock_merge_branch.assert_called_once()

    def test_untracked_files_are_warned_but_not_added(self, tmp_path: Path) -> None:
        from theforge.coordinator.completion import land_story
        from theforge.coordinator.state import CoordinatorState

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()

        shell_calls: list[str] = []
        log_messages: list[str] = []

        def _fake_run_shell(cmd: str, cwd: Path):
            shell_calls.append(cmd)
            if cmd == "git status --porcelain":
                return True, "?? scratch.txt\n M tracked.py"
            if cmd == "git add -- tracked.py":
                return True, ""
            if cmd == 'git commit -m "chore: commit remaining worktree changes before merge"':
                return True, "[feat abc123] cleanup"
            raise AssertionError(cmd)

        with (
            patch("theforge.coordinator.completion._run_shell", side_effect=_fake_run_shell),
            patch("theforge.coordinator.completion._log", side_effect=log_messages.append),
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={"merged": True, "error": None},
            ),
        ):
            merge_info, landing_status = land_story(
                config, task, "forge/test-task", tmp_path, review, state, "merge"
            )

        assert landing_status == "landed"
        assert shell_calls == [
            "git status --porcelain",
            "git add -- tracked.py",
            'git commit -m "chore: commit remaining worktree changes before merge"',
        ]
        assert any("untracked files left in worktree before merge" in msg for msg in log_messages)
        assert any("scratch.txt" in msg for msg in log_messages)


# ── on_approve: merge and pr unchanged ───────────────────────────


class TestExistingOnApproveUnchanged:
    """Smoke tests to confirm merge and pr behavior is unaffected."""

    def test_on_approve_merge_unaffected(self, tmp_path: Path) -> None:
        from theforge.coordinator.completion import _finalize_approve
        from theforge.coordinator.state import CoordinatorState, Phase

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()
        state.phase = Phase.REVIEW
        import time

        with (
            patch(
                "theforge.coordinator.completion._merge_pr",
            ) as mock_merge_pr,
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={"merged": True, "error": None},
            ),
        ):
            result = _finalize_approve(
                state,
                config,
                task,
                review,
                tmp_path,
                "forge/test-task",
                time.monotonic(),
                auto_merge=True,
                notify=False,
                logger=None,
                review_cost=0.5,
                review_elapsed=1.0,
                message="done. ",
            )

        mock_merge_pr.assert_not_called()
        assert result.success is True

    def test_on_approve_pr_unaffected(self, tmp_path: Path) -> None:
        from theforge.coordinator.completion import _finalize_approve
        from theforge.coordinator.state import CoordinatorState, Phase

        config = ForgeConfig(
            project="test",
            project_root=tmp_path,
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="pr",
            ),
            validation=DEFAULT_VALIDATION,
            dev_profile=DEFAULT_DEV_PROFILE,
            preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
            review_pool=[DEFAULT_REVIEW_PROFILE],
            synthesis_profile=None,
            retry=RetryPolicy(),
            log=LogConfig(enabled=False),
        )
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()
        state.phase = Phase.REVIEW
        import time

        with (
            patch(
                "theforge.coordinator.completion._merge_pr",
            ) as mock_merge_pr,
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/x/y/pull/1",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            result = _finalize_approve(
                state,
                config,
                task,
                review,
                tmp_path,
                "forge/test-task",
                time.monotonic(),
                auto_merge=False,
                notify=False,
                logger=None,
                review_cost=0.5,
                review_elapsed=1.0,
                message="done. ",
            )

        mock_merge_pr.assert_not_called()
        assert result.success is True
        assert result.merge["action"] == "pr"


# ── Sprint parallel merge lock ────────────────────────────────────


class TestSprintParallelMergeLock:
    """Verify that gh pr merge is serialized in parallel sprint mode."""

    def test_merge_pr_called_in_flush_loop(self, tmp_path: Path) -> None:
        """Flush loop calls _merge_pr (not _merge_branch) when on_approve=merge-pr."""

        merge_pr_calls: list[tuple] = []

        def _fake_merge_pr(config, task, branch, review, state):
            merge_pr_calls.append((task.slug, branch))
            return {
                "action": "merge-pr",
                "pr_url": f"https://github.com/x/y/pull/{len(merge_pr_calls)}",
                "merged": True,
                "success": True,
                "error": None,
            }

        # Build two tasks and simulate flush loop behavior
        config = _make_merge_pr_config(tmp_path)

        task_a = _make_task(tmp_path)
        review_a = _make_review_result()
        state_a = MagicMock()
        state_a.review_results = [review_a]

        with patch(
            "theforge.coordinator.completion._merge_pr",
            side_effect=_fake_merge_pr,
        ):
            # Simulate the flush loop calling _merge_pr
            from theforge.coordinator.completion import _merge_pr as patched_merge_pr

            result = patched_merge_pr(config, task_a, "forge/test-task", review_a, state_a)

        assert result["merged"] is True
        assert result["action"] == "merge-pr"


# ── PR body content ───────────────────────────────────────────────


class TestPrBodyContent:
    """Verify _create_pr includes story name, dev cost, and review summary."""

    def test_pr_body_includes_required_fields(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = ReviewResult(
            verdict="APPROVE",
            summary="Feature implemented correctly.",
            findings=[],
            story_matches=True,
            story_mismatches=[],
            test_adequate=True,
            test_gaps=[],
            parse_errors=[],
            raw_yaml={},
        )
        state = MagicMock()
        state.total_cost = 2.75
        state.dev_iteration = 3

        pr_bodies: list[str] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:3] == ["git", "rev-list", "--count"]:
                return _make_subprocess_result(0, stdout="1\n")
            m = _make_subprocess_result(0, stdout="https://github.com/x/y/pull/10")
            if isinstance(cmd, list) and "gh" in cmd:
                # Find --body arg
                for i, arg in enumerate(cmd):
                    if arg == "--body" and i + 1 < len(cmd):
                        pr_bodies.append(cmd[i + 1])
            return m

        with patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run):
            result = _create_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        assert len(pr_bodies) == 1
        body = pr_bodies[0]
        assert "Test Task" in body  # story name
        assert "2.75" in body  # dev cost
        assert "Feature implemented correctly" in body  # review summary

    def test_pr_body_uses_sanitized_review_text(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = parse_review_json(
            {
                "verdict": "APPROVE",
                "summary": "Feature\x00 implemented",
                "findings": [
                    {
                        "severity": "P2",
                        "file": "src/test.py",
                        "line": 9,
                        "description": "Fix\x00 this edge case",
                    }
                ],
                "story_compliance": {"matches_spec": True, "mismatches": []},
                "test_coverage": {"adequate": True, "gaps": []},
            }
        )
        state = MagicMock()
        state.total_cost = 1.0
        state.dev_iteration = 1

        pr_bodies: list[str] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:3] == ["git", "rev-list", "--count"]:
                return _make_subprocess_result(0, stdout="1\n")
            result = _make_subprocess_result(0, stdout="https://github.com/x/y/pull/11")
            if isinstance(cmd, list):
                for i, arg in enumerate(cmd):
                    if arg == "--body" and i + 1 < len(cmd):
                        pr_bodies.append(cmd[i + 1])
            return result

        with patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run):
            result = _create_pr(config, task, "forge/test-task", review, state)

        assert result["success"] is True
        assert "\x00" not in pr_bodies[0]
        assert "Feature implemented" in pr_bodies[0]
        assert "Fix this edge case" in pr_bodies[0]
        assert review.sanitization_audit == {
            "summary": {"sanitized_chars": 1},
            "findings[0].description": {"sanitized_chars": 1},
        }


# ── Escalate on merge-pr failure ─────────────────────────────────


class TestMergePrEscalate:
    """land_story() preserves approval when merge-pr landing fails."""

    def test_finalize_approve_escalates_on_merge_pr_failure(self, tmp_path: Path) -> None:
        """land_story returns landing_status='failed' when _merge_pr fails."""
        from theforge.coordinator.completion import land_story
        from theforge.coordinator.state import CoordinatorState

        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()

        with patch(
            "theforge.coordinator.completion._merge_pr",
            return_value={
                "action": "merge-pr",
                "pr_url": None,
                "merged": False,
                "success": False,
                "error": "rebase conflict on main",
            },
        ):
            merge_info, landing_status = land_story(
                config, task, "forge/test-task", tmp_path, review, state, "merge-pr"
            )

        assert merge_info["merged"] is False
        assert landing_status == "failed"

    def test_finalize_approve_done_on_merge_pr_success(self, tmp_path: Path) -> None:
        """land_story returns landing_status='landed' when _merge_pr succeeds."""
        from theforge.coordinator.completion import land_story
        from theforge.coordinator.state import CoordinatorState

        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()

        with patch(
            "theforge.coordinator.completion._merge_pr",
            return_value={
                "action": "merge-pr",
                "pr_url": "https://github.com/x/y/pull/5",
                "merged": True,
                "success": True,
                "error": None,
            },
        ):
            merge_info, landing_status = land_story(
                config, task, "forge/test-task", tmp_path, review, state, "merge-pr"
            )

        assert merge_info["merged"] is True
        assert landing_status == "landed"


# ── Config validation: merge_strategy ────────────────────────────


class TestMergeStrategyValidation:
    """merge_strategy must be validated at config load time."""

    def test_invalid_merge_strategy_raises(self) -> None:
        from theforge.config._loaders import _parse_workspace

        with pytest.raises(ValueError, match="merge_strategy"):
            _parse_workspace(
                {"on_approve": "merge-pr", "auto_push": True, "merge_strategy": "fast-forward"}
            )

    def test_valid_merge_strategies_accepted(self) -> None:
        from theforge.config._loaders import _parse_workspace

        for strategy in ("merge", "squash", "rebase"):
            result = _parse_workspace(
                {"on_approve": "merge-pr", "auto_push": True, "merge_strategy": strategy}
            )
            assert result.merge_strategy == strategy

    def test_non_merge_pr_modes_ignore_merge_strategy(self) -> None:
        from theforge.config._loaders import _parse_workspace

        for on_approve in ("merge", "pr", "none"):
            result = _parse_workspace({"on_approve": on_approve, "merge_strategy": "fast-forward"})
            assert result.on_approve == on_approve
            assert result.merge_strategy == "fast-forward"


# ── Fast-forward step called after successful merge ───────────────


class TestFastForwardAfterMerge:
    """Step 5 of _merge_pr: fetch + ff-only merge to update local base_branch."""

    def test_fast_forward_commands_called_on_success(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        ff_calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and "fetch" in cmd and "merge" not in cmd:
                if cmd == ["git", "fetch", "origin"]:
                    ff_calls.append(cmd)
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and "merge" in cmd and "--ff-only" in cmd:
                ff_calls.append(cmd)
                return _make_subprocess_result(0)
            if isinstance(cmd, list) and cmd and cmd[0] == "gh":
                if cmd[:3] == ["gh", "pr", "view"]:
                    return _make_subprocess_result(0, stdout="MERGED")
                return _make_subprocess_result(0)
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/x/y/pull/7",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["merged"] is True
        # Verify fetch and ff-only merge were issued for local base_branch update
        fetch_cmds = [c for c in ff_calls if "fetch" in c]
        ff_cmds = [c for c in ff_calls if "--ff-only" in c]
        assert len(fetch_cmds) >= 1
        assert len(ff_cmds) >= 1

    def test_cleanup_commands_called_after_successful_merge(self, tmp_path: Path) -> None:
        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = MagicMock()
        state.review_results = [review]
        state.total_cost = 1.0
        state.dev_iteration = 1

        cleanup_calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and (
                cmd[:4] == ["git", "worktree", "remove", "--force"]
                or cmd[:3] == ["git", "branch", "-D"]
                or cmd[:4] == ["git", "push", "origin", "--delete"]
            ):
                cleanup_calls.append(cmd)
            if isinstance(cmd, list) and cmd[:3] == ["gh", "pr", "view"]:
                return _make_subprocess_result(0, stdout="MERGED")
            return _make_subprocess_result(0)

        with (
            patch("theforge.coordinator.completion.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.coordinator.completion._create_pr",
                return_value={
                    "action": "pr",
                    "pr_url": "https://github.com/x/y/pull/8",
                    "success": True,
                    "error": None,
                },
            ),
        ):
            result = _merge_pr(config, task, "forge/test-task", review, state)

        assert result["merged"] is True
        assert any(cmd[:4] == ["git", "worktree", "remove", "--force"] for cmd in cleanup_calls)
        assert any(cmd[:3] == ["git", "branch", "-D"] for cmd in cleanup_calls)
        assert any(cmd[:4] == ["git", "push", "origin", "--delete"] for cmd in cleanup_calls)

    def test_finalize_approve_keeps_done_for_merge_queued(self, tmp_path: Path) -> None:
        """land_story returns landing_status='pending_integration' when PR merge is queued."""
        from theforge.coordinator.completion import land_story
        from theforge.coordinator.state import CoordinatorState

        config = _make_merge_pr_config(tmp_path)
        task = _make_task(tmp_path)
        review = _make_review_result()
        state = CoordinatorState()

        with patch(
            "theforge.coordinator.completion._merge_pr",
            return_value={
                "action": "merge-pr",
                "pr_url": "https://github.com/x/y/pull/6",
                "merged": False,
                "merge_queued": True,
                "auto_merge_queued": True,
                "success": True,
                "error": None,
            },
        ):
            merge_info, landing_status = land_story(
                config, task, "forge/test-task", tmp_path, review, state, "merge-pr"
            )

        assert merge_info["merge_queued"] is True
        assert landing_status == "pending_integration"
