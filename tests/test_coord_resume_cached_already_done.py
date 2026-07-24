"""Seam test: an accepted cached ALREADY_DONE preflight verdict is terminal on
the resume path.

Regression for the empty-branch escalation: in sprint run 47acc396fb8f, story
#1904 carried a cached preflight verdict ``ALREADY_DONE`` whose cache validated
(``git_state_match``), yet the resume coordinator applied the cached state and
then fell straight through into DEV/GATE, escalating because the branch had no
commits ahead of base.

The resume path (``run_from_dev`` / ``run_from_review`` →
``_run_resume_coordinator``) must dispatch the cached verdict exactly as
``run_task`` does: a validated ALREADY_DONE verdict terminates in a DONE result
without invoking dev, running gate, or escalating an empty branch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_agent_result, _make_config, patch_gate_shell

from theforge.coordinator.engine import run_from_dev, run_from_review
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.task import TaskStory


def _make_already_done_cached_state() -> CoordinatorState:
    """Cached preflight state whose verdict is ALREADY_DONE and whose git-state
    snapshot matches the mocked worktree/base heads (so validation accepts)."""
    state = CoordinatorState()
    state.preflight_verdict = "ALREADY_DONE"
    state.preflight_reason = "All acceptance criteria are already satisfied."
    state.preflight_complexity = "small"
    state.preflight_sufficiency = "implementation_ready"
    state.preflight_work_type = "bug"
    state.preflight_likely_files = ["src/foo.py"]
    state.preflight_warnings = []
    state.preflight_cache_snapshot = {
        "worktree_head": "OK",
        "evaluation_base_branch": "main",
        "evaluation_base_branch_head": "OK",
    }
    state.run_id = "prior-run-id"
    return state


def _empty_branch_shell(cmd: str, cwd, **kwargs):
    """4-tuple ``_run_shell_detailed`` side_effect for an already-done, empty
    branch: rev-parse resolves to the snapshot head so the cache validates, and
    ``git log base..branch`` reports no commits ahead of base."""
    if "--oneline" in cmd and "git log" in cmd:
        return (True, "", 0, False)  # no commits ahead of base
    if "gate" in cmd:
        # Gate must never run on the terminal ALREADY_DONE path; make it loud.
        raise AssertionError("gate command executed on cached ALREADY_DONE path")
    # rev-parse (cache validation) and every other git op resolve cleanly.
    return (True, "OK", 0, False)


class TestResumeCachedAlreadyDoneTerminal:
    """A validated cached ALREADY_DONE verdict short-circuits the resume path."""

    @patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=False)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell(side_effect=_empty_branch_shell)
    def test_run_from_dev_cached_already_done_is_terminal(
        self, mock_shell, mock_dev, mock_pool, mock_merged, tmp_path: Path
    ) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        cached = _make_already_done_cached_state()

        result = run_from_dev(config, task, workspace, cached_preflight_state=cached)

        # Terminal DONE without entering DEV/GATE/REVIEW.
        assert result is not None
        assert result.success is True
        assert result.phase == Phase.DONE
        mock_dev.assert_not_called()
        mock_pool.assert_not_called()
        # The cache-accepted verdict was applied and honored.
        assert result.state.preflight_cached is True
        assert result.state.preflight_cached_original_verdict == "ALREADY_DONE"

    @patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=False)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell(side_effect=_empty_branch_shell)
    def test_run_from_review_cached_already_done_is_terminal(
        self, mock_shell, mock_dev, mock_pool, mock_merged, tmp_path: Path
    ) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        cached = _make_already_done_cached_state()

        result = run_from_review(config, task, workspace, cached_preflight_state=cached)

        assert result is not None
        assert result.success is True
        assert result.phase == Phase.DONE
        mock_dev.assert_not_called()
        mock_pool.assert_not_called()
        assert result.state.preflight_cached is True
        assert result.state.preflight_cached_original_verdict == "ALREADY_DONE"

    @patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=False)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_cached_already_done_with_commits_still_overrides_into_review(
        self, mock_shell, mock_dev, mock_pool, mock_merged, tmp_path: Path
    ) -> None:
        """The deliberate ALREADY_DONE override is preserved: when the branch
        carries commits without a prior APPROVE, the resume path re-enters REVIEW
        (skipping DEV) rather than terminating."""

        def shell(cmd: str, cwd, **kwargs):
            if "--oneline" in cmd and "git log" in cmd:
                return (True, "abc1234 feat: work", 0, False)  # commits ahead
            if "git status --porcelain" in cmd:
                return (True, "", 0, False)
            return (True, "OK", 0, False)

        mock_shell.side_effect = shell
        mock_pool.return_value = [
            _make_agent_result(
                success=True,
                output=(
                    "```yaml\nverdict: APPROVE\nsummary: ok\nfindings: []\n"
                    "story_compliance:\n  matches_spec: true\n"
                    "test_coverage:\n  adequate: true\n"
                    "ac_verification:\n  - criterion: c\n    status: VERIFIED\n"
                    "    evidence: e\n```\n"
                ),
                profile_name="review",
            )
        ]

        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n\nDo the thing.", encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        cached = _make_already_done_cached_state()

        with patch("theforge.coordinator.preflight_flow.has_review_approve", return_value=False):
            result = run_from_dev(config, task, workspace, cached_preflight_state=cached)

        # Override path re-enters the loop: DEV is skipped, REVIEW runs.
        assert result is not None
        mock_dev.assert_not_called()
        mock_pool.assert_called()
