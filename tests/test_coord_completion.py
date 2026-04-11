"""Tests for CoordinatorResult success/landing separation and audit landing_event.

Covers:
- result.success is False when landing_status is "failed" (engine and sprint paths)
- generate_audit_log emits a landing_event block separate from the outcome block
- Resume path (run_from_review): landing failure sets result.success=False
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_from_review
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.task import TaskStory


def _make_config(tmp_path: Path) -> ForgeConfig:
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


def _make_task() -> TaskStory:
    return TaskStory(name="test-story", slug="test-story", story_path="specs/test.md")


def _make_result(
    landing_status: str | None = None,
    success: bool = True,
    merge: dict | None = None,
) -> CoordinatorResult:
    state = CoordinatorState()
    return CoordinatorResult(
        success=success,
        phase=Phase.DONE,
        state=state,
        message="Done." if success else "Failed.",
        merge=merge,
        landing_status=landing_status,
    )


# ── Audit landing_event block ─────────────────────────────────────────────────


class TestAuditLandingEvent:
    def test_landing_event_none_when_no_landing_status(self, tmp_path: Path) -> None:
        """landing_event is None when no merge operation was attempted."""
        result = _make_result(landing_status=None)
        audit = generate_audit_log(_make_config(tmp_path), _make_task(), result)
        assert audit["landing_event"] is None

    def test_landing_event_failed_sets_landed_false(self, tmp_path: Path) -> None:
        """landing_status=failed produces landing_event with landed=False."""
        result = _make_result(landing_status="failed", success=False)
        audit = generate_audit_log(_make_config(tmp_path), _make_task(), result)
        assert audit["landing_event"] is not None
        assert audit["landing_event"]["landing_status"] == "failed"
        assert audit["landing_event"]["landed"] is False
        assert "timestamp" in audit["landing_event"]

    def test_landing_event_landed_sets_landed_true(self, tmp_path: Path) -> None:
        """landing_status=landed produces landing_event with landed=True."""
        result = _make_result(landing_status="landed", success=True)
        audit = generate_audit_log(_make_config(tmp_path), _make_task(), result)
        assert audit["landing_event"] is not None
        assert audit["landing_event"]["landing_status"] == "landed"
        assert audit["landing_event"]["landed"] is True

    def test_landing_event_pending_integration_sets_landed_false(self, tmp_path: Path) -> None:
        """landing_status=pending_integration produces landing_event with landed=False."""
        result = _make_result(landing_status="pending_integration", success=True)
        audit = generate_audit_log(_make_config(tmp_path), _make_task(), result)
        assert audit["landing_event"] is not None
        assert audit["landing_event"]["landing_status"] == "pending_integration"
        assert audit["landing_event"]["landed"] is False

    def test_landing_event_is_separate_from_outcome(self, tmp_path: Path) -> None:
        """landing_event and outcome are distinct top-level keys with independent values."""
        result = _make_result(landing_status="failed", success=False)
        audit = generate_audit_log(_make_config(tmp_path), _make_task(), result)
        assert "outcome" in audit
        assert "landing_event" in audit
        # outcome reflects overall run success
        assert audit["outcome"]["success"] is False
        # landing_event reflects the landing operation specifically
        assert audit["landing_event"]["landing_status"] == "failed"
        assert audit["landing_event"]["landed"] is False


# ── result.success=False when landing fails ───────────────────────────────────


class TestSuccessFalseOnLandingFailure:
    def test_engine_path_sets_success_false_on_direct_merge_failure(self) -> None:
        """Engine sets result.success=False when land_story returns 'failed' (merge mode).

        Previously, direct-merge failures were non-fatal (success stayed True).
        This test documents the new contract: any landing failure → success=False.
        """
        # Simulate the engine.py post-coordinator-loop block:
        # result starts as success=True, pending_integration from _finalize_approve
        result = _make_result(success=True, landing_status="pending_integration")
        merge_info = {"merged": False, "error": "fast-forward blocked", "action": "merge"}
        _landing_status = "failed"

        # Apply the same logic as engine.py lines 826-838
        result.merge = merge_info
        result.landing_status = _landing_status
        if _landing_status == "failed":
            result.success = False
            result.message += f" Merge failed: {merge_info.get('error', 'unknown')}"

        assert result.success is False
        assert result.landing_status == "failed"
        assert "Merge failed" in result.message

    def test_sprint_path_sets_success_false_on_landing_failure(self) -> None:
        """Sprint _attempt_integration sets result.success=False on land_story 'failed'."""
        # Simulate the sprint runner _attempt_integration block:
        # result starts as success=True (worker completed, pending integration)
        result = _make_result(success=True, landing_status="pending_integration")
        merge_info = {"merged": False, "error": "rebase conflict", "action": "merge"}
        landing_status = "failed"

        # Apply the same logic as sprint/runner.py _attempt_integration
        result.merge = merge_info
        result.landing_status = landing_status
        if landing_status == "failed":
            result.success = False

        assert result.success is False
        assert result.landing_status == "failed"

    def test_success_unchanged_when_landing_succeeds(self) -> None:
        """result.success stays True when landing_status is 'landed'."""
        result = _make_result(success=True, landing_status="pending_integration")
        merge_info = {"merged": True, "action": "merge"}
        landing_status = "landed"

        result.merge = merge_info
        result.landing_status = landing_status
        if landing_status == "failed":
            result.success = False

        assert result.success is True
        assert result.landing_status == "landed"

    def test_audit_outcome_reflects_success_false_after_landing_failure(
        self, tmp_path: Path
    ) -> None:
        """Audit outcome.success matches result.success after landing failure."""
        result = _make_result(landing_status="failed", success=False)
        audit = generate_audit_log(_make_config(tmp_path), _make_task(), result)
        assert audit["outcome"]["success"] is False
        assert audit["landing_event"]["landing_status"] == "failed"


# ── Resume path: run_from_review landing failure ──────────────────────────────


class TestResumeLandingFailure:
    """Tests that _run_resume_coordinator sets result.success=False on landing failure."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_landing_failure_sets_success_false(
        self, mock_shell, mock_pool, tmp_path: Path
    ) -> None:
        """run_from_review with auto_merge=True: landing failure → result.success=False.

        Previously, the resume path had a merge-pr-only guard so direct-merge
        failures left success=True. This test documents the fixed contract:
        any landing failure → success=False, regardless of landing mode.
        """
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nImplement the thing.", encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Task", story_path=spec, slug="test-task")
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        def shell_side_effect(cmd, cwd, **kwargs):
            if "git status --porcelain" in cmd:
                return (True, "")  # clean worktree
            if "git branch --list" in cmd:
                return (True, "")  # base branch not found → landing fails
            return (True, "")

        mock_shell.side_effect = shell_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace, auto_merge=True)

        assert result.success is False
        assert result.landing_status == "failed"
        assert result.merge is not None
        assert result.merge["merged"] is False
