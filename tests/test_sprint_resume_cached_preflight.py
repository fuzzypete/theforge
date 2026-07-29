from pathlib import Path
from unittest.mock import patch

import yaml

from tests.test_sprint_resume import (
    _make_config,
    _make_coordinator_result,
    _make_manifest,
    _make_spec_file,
)
from theforge.coordinator.state import CoordinatorState
from theforge.sprint.dag import StoryTriage
from theforge.sprint.runner import run_sprint


def _set_snapshot(state: CoordinatorState) -> CoordinatorState:
    state.preflight_cache_snapshot = {
        "worktree_head": "OK",
        "evaluation_base_branch": "main",
        "evaluation_base_branch_head": "OK",
    }
    return state


class TestSprintResumeCachedPreflight:
    def test_resume_sprint_passes_cached_preflight_to_resumed_entrypoints(
        self, tmp_path: Path
    ) -> None:
        """Resume mode forwards cached sprint preflight state into review/dev entry points."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
        config = _make_config(tmp_path)

        review_worktree = tmp_path / "feature-a"
        review_worktree.mkdir()
        dev_worktree = tmp_path / "feature-b"
        dev_worktree.mkdir()

        review_triage = StoryTriage(
            story_path="feature-a.md",
            action="review",
            reason="gate passes",
            worktree_path=review_worktree,
        )
        dev_triage = StoryTriage(
            story_path="feature-b.md",
            action="dev",
            reason="gate fails",
            worktree_path=dev_worktree,
        )

        review_cached = _set_snapshot(
            CoordinatorState(
                preflight_verdict="PROCEED",
                preflight_reason="cached review",
                preflight_likely_files=["src/review.py"],
            )
        )
        review_cached.run_id = "run-review"
        dev_cached = _set_snapshot(
            CoordinatorState(
                preflight_verdict="PROCEED",
                preflight_reason="cached dev",
                preflight_likely_files=["src/dev.py"],
            )
        )
        dev_cached.run_id = "run-dev"

        def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
            if "feature-a" in spec_path:
                return review_triage
            return dev_triage

        def batch_preflight_side_effect(tasks, *_args, **_kwargs):
            return {
                "feature-a": review_cached,
                "feature-b": dev_cached,
            }

        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch(
                "theforge.sprint.runner.run_batch_preflight",
                side_effect=batch_preflight_side_effect,
            ):
                with patch(
                    "theforge.sprint.runner.run_from_review", return_value=coord_result
                ) as mock_rev:
                    with patch(
                        "theforge.sprint.runner.run_from_dev", return_value=coord_result
                    ) as mock_dev:
                        result = run_sprint(config, manifest_path, resume=True)

        assert result.specs_succeeded == 2
        assert mock_rev.call_args.kwargs["cached_preflight_state"] is review_cached
        assert mock_dev.call_args.kwargs["cached_preflight_state"] is dev_cached

    def test_resume_story_summary_marks_cached_preflight_for_review_and_dev(
        self, tmp_path: Path
    ) -> None:
        """Resumed review/dev stories write cached preflight metadata into sprint summary."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
        config = _make_config(tmp_path)

        review_worktree = tmp_path / "feature-a"
        review_worktree.mkdir()
        dev_worktree = tmp_path / "feature-b"
        dev_worktree.mkdir()

        review_triage = StoryTriage(
            story_path="feature-a.md",
            action="review",
            reason="gate passes",
            worktree_path=review_worktree,
        )
        dev_triage = StoryTriage(
            story_path="feature-b.md",
            action="dev",
            reason="gate fails",
            worktree_path=dev_worktree,
        )

        review_cached = _set_snapshot(
            CoordinatorState(
                preflight_verdict="PROCEED",
                preflight_reason="cached review",
                preflight_likely_files=["src/review.py"],
            )
        )
        review_cached.run_id = "run-review"
        dev_cached = _set_snapshot(
            CoordinatorState(
                preflight_verdict="ALREADY_DONE",
                preflight_reason="cached dev",
                preflight_likely_files=["src/dev.py"],
            )
        )
        dev_cached.run_id = "run-dev"

        review_result = _make_coordinator_result(success=True, cost=1.0)
        review_result.state.preflight_cached = True
        review_result.state.preflight_cached_original_verdict = "PROCEED"
        review_result.state.preflight_cached_from_run_id = "run-review"

        dev_result = _make_coordinator_result(success=True, cost=1.0)
        dev_result.state.preflight_cached = True
        dev_result.state.preflight_cached_original_verdict = "ALREADY_DONE"
        dev_result.state.preflight_cached_from_run_id = "run-dev"

        def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
            if "feature-a" in spec_path:
                return review_triage
            return dev_triage

        def batch_preflight_side_effect(tasks, *_args, **_kwargs):
            return {
                "feature-a": review_cached,
                "feature-b": dev_cached,
            }

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch(
                "theforge.sprint.runner.run_batch_preflight",
                side_effect=batch_preflight_side_effect,
            ):
                with patch("theforge.sprint.runner.run_from_review", return_value=review_result):
                    with patch("theforge.sprint.runner.run_from_dev", return_value=dev_result):
                        result = run_sprint(config, manifest_path, resume=True)

        assert result.specs_succeeded == 2
        summary_path = tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml"
        summary = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
        specs = {entry["slug"]: entry for entry in summary["stories"]}

        assert specs["feature-a"]["preflight"] == "cached"
        assert specs["feature-a"]["preflight_original_verdict"] == "PROCEED"
        assert specs["feature-a"]["preflight_source_run_id"] == "run-review"

        assert specs["feature-b"]["preflight"] == "cached"
        assert specs["feature-b"]["preflight_original_verdict"] == "ALREADY_DONE"
        assert specs["feature-b"]["preflight_source_run_id"] == "run-dev"
