"""Tests for cached preflight resume path with None preflight fields.

Regression test for the crash: 'NoneType' object is not iterable when
_run_resume_coordinator copies preflight_likely_files / preflight_warnings
from cached state without None guards.

Evidence: sprint v0.7.0 run 83e41828cb1f — issue-332 and issue-333 both
crashed in this path with TypeError after PLAN and PLAN_REVIEW succeeded.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _shell_with_gate,
)

from theforge.coordinator.engine import run_from_dev, run_from_review
from theforge.coordinator.state import CoordinatorState
from theforge.task import TaskStory


def _make_cached_state(
    *,
    preflight_likely_files: list[str] | None,
    preflight_warnings: list[str] | None,
) -> CoordinatorState:
    """Build a minimal cached CoordinatorState with explicit None fields."""
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    state.preflight_reason = "tests pass"
    state.preflight_complexity = "small"
    state.preflight_sufficiency = "implementation_ready"
    state.preflight_work_type = "feature"
    state.preflight_likely_files = preflight_likely_files
    state.preflight_warnings = preflight_warnings  # type: ignore[assignment]
    state.run_id = "prior-run-id"
    return state


class TestResumeNonePreflightFields:
    """Ensure _run_resume_coordinator does not crash on None preflight fields."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_none_preflight_likely_files(
        self, mock_shell, mock_pool, tmp_path: Path
    ) -> None:
        """run_from_review with cached preflight where likely_files=None must not crash."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        cached = _make_cached_state(
            preflight_likely_files=None,
            preflight_warnings=[],
        )

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        # Must not raise TypeError: 'NoneType' object is not iterable
        result = run_from_review(config, task, workspace, cached_preflight_state=cached)
        assert result is not None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_none_preflight_warnings(
        self, mock_shell, mock_pool, tmp_path: Path
    ) -> None:
        """run_from_review with cached preflight where warnings=None must not crash."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        cached = _make_cached_state(
            preflight_likely_files=["src/foo.py"],
            preflight_warnings=None,
        )

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace, cached_preflight_state=cached)
        assert result is not None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_review_both_none(self, mock_shell, mock_pool, tmp_path: Path) -> None:
        """run_from_review with both preflight fields None must not crash."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        cached = _make_cached_state(
            preflight_likely_files=None,
            preflight_warnings=None,
        )

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace, cached_preflight_state=cached)
        assert result is not None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_dev_none_preflight_likely_files(
        self, mock_shell, mock_dev, mock_pool, tmp_path: Path
    ) -> None:
        """run_from_dev with cached preflight where likely_files=None must not crash."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        cached = _make_cached_state(
            preflight_likely_files=None,
            preflight_warnings=[],
        )

        # Gate passes → skips to review phase directly
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(
            success=True, output="implemented", profile_name="dev"
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        # Must not raise TypeError: 'NoneType' object is not iterable
        result = run_from_dev(config, task, workspace, cached_preflight_state=cached)
        assert result is not None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_run_from_dev_both_none(self, mock_shell, mock_dev, mock_pool, tmp_path: Path) -> None:
        """run_from_dev with both preflight fields None must not crash."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        cached = _make_cached_state(
            preflight_likely_files=None,
            preflight_warnings=None,
        )

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_dev.return_value = _make_agent_result(
            success=True, output="implemented", profile_name="dev"
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_dev(config, task, workspace, cached_preflight_state=cached)
        assert result is not None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.util._run_shell")
    def test_resume_state_preflight_likely_files_preserved_as_none(
        self, mock_shell, mock_pool, tmp_path: Path
    ) -> None:
        """When cached likely_files is None, resumed state.preflight_likely_files is also None."""
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        cached = _make_cached_state(
            preflight_likely_files=None,
            preflight_warnings=[],
        )

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_from_review(config, task, workspace, cached_preflight_state=cached)
        # The final state should have preflight_likely_files=None (not [] or anything else)
        assert result.state.preflight_likely_files is None
