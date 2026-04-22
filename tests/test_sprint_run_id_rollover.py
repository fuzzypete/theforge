"""Tests for sprint reporting across run_id boundaries.

Verifies that when a sprint spans multiple worker processes (run_id rollover),
forge status and sprint-summary.yaml show all stories from the full logical
sprint, not just those completed under the terminal run_id.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint import run_sprint
from theforge.sprint.audit import (
    _get_or_create_sprint_id,
    _load_accumulated_stories,
    _save_accumulated_stories,
)
from theforge.sprint.dag import StoryTriage
from theforge.sprint.status_reader import (
    _follow_redirect_chain,
    find_sprint_summary,
    read_completed_status,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


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


def _make_spec_file(tmp_path: Path, name: str, slug: str) -> Path:
    spec = tmp_path / f"{slug}.md"
    spec.write_text(
        f"---\nname: {name}\nslug: {slug}\n---\n# {name}\nDo the thing.",
        encoding="utf-8",
    )
    return spec


def _make_manifest(tmp_path: Path, specs: list[str], name: str = "Test Sprint") -> Path:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        yaml.dump({"name": name, "budget_usd": 50.0, "specs": specs}),
        encoding="utf-8",
    )
    return manifest_path


def _make_coordinator_result(
    success: bool = True, cost: float = 1.0, phase: Phase = Phase.DONE
) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    mock_preflight = MagicMock()
    mock_preflight.cost_usd = cost
    state.preflight_result = mock_preflight
    return CoordinatorResult(
        success=success,
        phase=phase,
        state=state,
        message="Done." if success else "Failed.",
        merge={"merged": True} if success else None,
        landing_status="landed" if success else None,
    )


# ── sprint_id persistence ─────────────────────────────────────────────────────


class TestSprintIdPersistence:
    def test_creates_sprint_id_on_first_call(self, tmp_path: Path) -> None:
        sprint_id = _get_or_create_sprint_id("my-sprint", tmp_path)
        assert sprint_id
        assert len(sprint_id) == 12  # _generate_run_id returns 12-char hex

    def test_returns_same_id_on_subsequent_calls(self, tmp_path: Path) -> None:
        id1 = _get_or_create_sprint_id("my-sprint", tmp_path)
        id2 = _get_or_create_sprint_id("my-sprint", tmp_path)
        assert id1 == id2

    def test_different_sprints_get_different_ids(self, tmp_path: Path) -> None:
        id1 = _get_or_create_sprint_id("sprint-a", tmp_path)
        id2 = _get_or_create_sprint_id("sprint-b", tmp_path)
        assert id1 != id2

    def test_sprint_id_file_location(self, tmp_path: Path) -> None:
        sprint_id = _get_or_create_sprint_id("my-sprint", tmp_path)
        id_file = tmp_path / ".forge" / "logs" / "my-sprint" / ".sprint_id"
        assert id_file.exists()
        assert id_file.read_text(encoding="utf-8").strip() == sprint_id


# ── accumulated state round-trip ──────────────────────────────────────────────


class TestAccumulatedState:
    def test_load_returns_empty_when_missing(self, tmp_path: Path) -> None:
        stories = _load_accumulated_stories("nonexistent-id", tmp_path)
        assert stories == []

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        sprint_id = "testabc123456"
        stories = [
            {
                "canonical_ref": "feature-a.md",
                "slug": "feature-a",
                "path": "feature-a.md",
                "outcome": "DONE",
                "verdict": "APPROVE",
                "cost_usd": 1.5,
            }
        ]
        _save_accumulated_stories(sprint_id, "Test Sprint", tmp_path, stories)
        loaded = _load_accumulated_stories(sprint_id, tmp_path)
        assert len(loaded) == 1
        assert loaded[0]["canonical_ref"] == "feature-a.md"
        assert loaded[0]["outcome"] == "DONE"
        assert loaded[0]["cost_usd"] == pytest.approx(1.5)

    def test_state_yaml_location(self, tmp_path: Path) -> None:
        sprint_id = "testabc123456"
        _save_accumulated_stories(sprint_id, "Test Sprint", tmp_path, [])
        state_path = tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml"
        assert state_path.exists()


# ── run_id rollover: two runs, both stories visible ───────────────────────────


class TestRunIdRolloverReporting:
    def test_summary_includes_prior_run_stories(self, tmp_path: Path) -> None:
        """A resume run shows stories from prior run_ids in sprint-summary.yaml.

        Simulates: story-a ran under run_id A (already merged), story-b runs
        under run_id B (resume).  The final summary must show both.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
        config = _make_config(tmp_path)
        sprint_name = "Test Sprint"

        # Pre-create stable sprint_id and accumulated state to simulate a prior run
        # where feature-a completed under an earlier run_id.
        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        prior_stories = [
            {
                "canonical_ref": "feature-a.md",
                "slug": "feature-a",
                "path": "feature-a.md",
                "outcome": "DONE",
                "verdict": "APPROVE",
                "cost_usd": 1.18,
                "story_run_id": "run-a-test",
                "preflight": "PROCEED",
                "preflight_original_verdict": None,
                "preflight_source_run_id": None,
                "error": None,
                "error_type": None,
                "merge": True,
                "started_at": "2026-04-17T10:00:00Z",
                "finished_at": "2026-04-17T10:30:00Z",
                "batch": 0,
                "depends_on": [],
                "iteration_usage": {
                    "dev": {"used": 1, "max": 5, "hit_limit": False, "early_finish": True},
                    "review": {"used": 1, "max": 3, "hit_limit": False, "early_finish": True},
                },
            }
        ]
        _save_accumulated_stories(sprint_id, sprint_name, tmp_path, prior_stories)

        # Second invocation: feature-a is skip_merged, feature-b runs fresh.
        skip_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
            slug="feature-a",
        )
        full_triage = StoryTriage(
            story_path="feature-b.md",
            action="full",
            reason="no prior run",
            worktree_path=None,
            slug="feature-b",
        )

        def _mock_triage(canonical_ref, cfg, project_root, task=None):
            slug = task.slug if task else Path(canonical_ref).stem
            return skip_triage if slug == "feature-a" else full_triage

        result_b = _make_coordinator_result(success=True, cost=9.26)

        with (
            patch("theforge.sprint.runner._triage_spec", side_effect=_mock_triage),
            patch("theforge.sprint.runner.run_task", return_value=result_b),
        ):
            sprint_result = run_sprint(config, manifest_path, resume=True, run_id="run-b-test")

        # Both stories should be counted
        assert sprint_result.specs_total == 2

        # sprint-summary.yaml must have both stories
        summary_path = tmp_path / ".forge" / "logs" / sprint_name / "sprint-summary.yaml"
        assert summary_path.exists()
        with open(summary_path, encoding="utf-8") as f:
            summary = yaml.safe_load(f)

        stories = summary.get("stories", [])
        assert len(stories) == 2, f"Expected 2 stories, got {len(stories)}: {stories}"

        slugs = {s["slug"] for s in stories}
        assert "feature-a" in slugs, "feature-a (prior run) missing from summary"
        assert "feature-b" in slugs, "feature-b (current run) missing from summary"

        # feature-a must show DONE (from prior run), not SKIPPED
        fa = next(s for s in stories if s["slug"] == "feature-a")
        assert fa["outcome"] == "DONE", f"feature-a outcome should be DONE, got {fa['outcome']}"
        assert fa["cost_usd"] == pytest.approx(1.18)
        assert fa.get("verdict") == "APPROVE"

        # feature-b from current run
        fb = next(s for s in stories if s["slug"] == "feature-b")
        assert fb["outcome"] == "DONE"
        assert fb["cost_usd"] == pytest.approx(9.26)

        # sprint_id field is present
        assert summary["sprint"].get("sprint_id") == sprint_id

        # Aggregate totals must include costs from both runs
        assert summary["sprint"]["total_cost_usd"] == pytest.approx(1.18 + 9.26)
        assert summary["sprint"]["specs_succeeded"] == 2
        assert summary["sprint"]["specs_failed"] == 0
        assert summary["sprint"]["specs_skipped"] == 0

    def test_read_completed_status_shows_all_stories(self, tmp_path: Path) -> None:
        """read_completed_status returns entries for all stories including prior-run ones."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
        config = _make_config(tmp_path)
        sprint_name = "Test Sprint"

        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        prior_stories = [
            {
                "canonical_ref": "feature-a.md",
                "slug": "feature-a",
                "path": "feature-a.md",
                "outcome": "DONE",
                "verdict": "APPROVE",
                "cost_usd": 1.18,
                "story_run_id": "run-a-test",
                "preflight": "PROCEED",
                "preflight_original_verdict": None,
                "preflight_source_run_id": None,
                "error": None,
                "error_type": None,
                "merge": True,
                "batch": 0,
                "depends_on": [],
            }
        ]
        _save_accumulated_stories(sprint_id, sprint_name, tmp_path, prior_stories)

        skip_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged",
            worktree_path=None,
            slug="feature-a",
        )
        full_triage = StoryTriage(
            story_path="feature-b.md",
            action="full",
            reason="no prior run",
            worktree_path=None,
            slug="feature-b",
        )

        def _mock_triage(canonical_ref, cfg, project_root, task=None):
            slug = task.slug if task else Path(canonical_ref).stem
            return skip_triage if slug == "feature-a" else full_triage

        result_b = _make_coordinator_result(success=True, cost=9.26)

        with (
            patch("theforge.sprint.runner._triage_spec", side_effect=_mock_triage),
            patch("theforge.sprint.runner.run_task", return_value=result_b),
        ):
            run_sprint(config, manifest_path, resume=True, run_id="run-b-test")

        summary_path = tmp_path / ".forge" / "logs" / sprint_name / "sprint-summary.yaml"
        entries = read_completed_status(summary_path)
        assert len(entries) == 2

        slugs = {e.slug for e in entries}
        assert "feature-a" in slugs
        assert "feature-b" in slugs

        fa = next(e for e in entries if e.slug == "feature-a")
        assert fa.status == "done"
        assert fa.cost_usd == pytest.approx(1.18)

    def test_sprint_id_stable_across_two_run_invocations(self, tmp_path: Path) -> None:
        """sprint_id does not change between the first and second run_sprint() invocations."""
        sprint_name = "Test Sprint"

        # Pre-create sprint_id (first invocation would have created this)
        sprint_id_first = _get_or_create_sprint_id(sprint_name, tmp_path)

        # Simulate second invocation reading the same sprint_id
        sprint_id_second = _get_or_create_sprint_id(sprint_name, tmp_path)

        assert sprint_id_first == sprint_id_second

    def test_history_jsonl_includes_sprint_id(self, tmp_path: Path) -> None:
        """history.jsonl entries include sprint_id when it is set."""
        import json

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)
        sprint_name = "Test Sprint"

        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner.run_task", return_value=result_a):
            run_sprint(config, manifest_path, run_id="run-a-test")

        history_path = tmp_path / ".forge" / "audits" / "history.jsonl"
        assert history_path.exists()
        lines = [
            json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines()
        ]

        # Sprint-level audit entry should have sprint_id
        sprint_entries = [ln for ln in lines if "sprint" in ln and "specs" in ln]
        assert sprint_entries, "No sprint-level history entry found"
        assert sprint_entries[0]["sprint"].get("sprint_id") == sprint_id


# ── redirect chain: earlier run_id finds terminal summary ────────────────────


class TestRedirectChainResolution:
    def test_follow_redirect_chain_no_redirect(self, tmp_path: Path) -> None:
        result = _follow_redirect_chain("run-abc", tmp_path)
        assert result == "run-abc"

    def test_follow_redirect_chain_single_hop(self, tmp_path: Path) -> None:
        import json

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "run-a.redirect").write_text(
            json.dumps({"new_run_id": "run-b"}), encoding="utf-8"
        )
        result = _follow_redirect_chain("run-a", tmp_path)
        assert result == "run-b"

    def test_follow_redirect_chain_multi_hop(self, tmp_path: Path) -> None:
        import json

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "run-a.redirect").write_text(
            json.dumps({"new_run_id": "run-b"}), encoding="utf-8"
        )
        (runs_dir / "run-b.redirect").write_text(
            json.dumps({"new_run_id": "run-c"}), encoding="utf-8"
        )
        result = _follow_redirect_chain("run-a", tmp_path)
        assert result == "run-c"

    def test_find_sprint_summary_with_earlier_run_id(self, tmp_path: Path) -> None:
        """find_sprint_summary() called with an earlier run_id returns the terminal summary."""
        import json

        # Create redirect: run-a → run-b (terminal)
        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "run-a.redirect").write_text(
            json.dumps({"new_run_id": "run-b"}), encoding="utf-8"
        )

        # Write a summary file that records run_id = "run-b"
        sprint_log_dir = tmp_path / ".forge" / "logs" / "My Sprint"
        sprint_log_dir.mkdir(parents=True)
        summary_path = sprint_log_dir / "sprint-summary.yaml"
        import yaml

        summary_path.write_text(
            yaml.dump({"sprint": {"name": "My Sprint", "run_id": "run-b"}}),
            encoding="utf-8",
        )

        # Querying with the earlier run_id should still find the summary
        found = find_sprint_summary("run-a", tmp_path)
        assert found == summary_path

    def test_find_sprint_summary_direct_match_still_works(self, tmp_path: Path) -> None:
        """Direct run_id match (no redirect) continues to work."""
        import yaml

        sprint_log_dir = tmp_path / ".forge" / "logs" / "My Sprint"
        sprint_log_dir.mkdir(parents=True)
        summary_path = sprint_log_dir / "sprint-summary.yaml"
        summary_path.write_text(
            yaml.dump({"sprint": {"name": "My Sprint", "run_id": "run-xyz"}}),
            encoding="utf-8",
        )

        found = find_sprint_summary("run-xyz", tmp_path)
        assert found == summary_path

    def test_find_sprint_summary_matches_prior_worker_run_id_from_story_metadata(
        self, tmp_path: Path
    ) -> None:
        """Earlier worker run_ids still resolve to the terminal sprint summary."""
        import yaml

        sprint_log_dir = tmp_path / ".forge" / "logs" / "My Sprint"
        sprint_log_dir.mkdir(parents=True)
        summary_path = sprint_log_dir / "sprint-summary.yaml"
        summary_path.write_text(
            yaml.dump(
                {
                    "sprint": {"name": "My Sprint", "run_id": "run-terminal"},
                    "stories": [
                        {
                            "slug": "issue-940",
                            "path": "Issue #940",
                            "outcome": "DONE",
                            "cost_usd": 10.15,
                            "story_run_id": "run-c6448a1795c0",
                        },
                        {
                            "slug": "issue-930",
                            "path": "Issue #930",
                            "outcome": "DONE",
                            "cost_usd": 7.11,
                            "story_run_id": "run-656d888893ec",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        found = find_sprint_summary("run-c6448a1795c0", tmp_path)
        assert found == summary_path

    def test_summary_includes_removed_prior_run_stories(self, tmp_path: Path) -> None:
        """Final summary retains prior-run stories removed from the resumed manifest."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-b.md"])
        config = _make_config(tmp_path)
        sprint_name = "Test Sprint"

        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        prior_stories = [
            {
                "canonical_ref": "feature-a.md",
                "slug": "feature-a",
                "path": "feature-a.md",
                "outcome": "DONE",
                "verdict": "APPROVE",
                "cost_usd": 1.18,
                "story_run_id": "run-a-test",
                "preflight": "PROCEED",
                "preflight_original_verdict": None,
                "preflight_source_run_id": None,
                "error": None,
                "error_type": None,
                "merge": True,
                "batch": 0,
                "depends_on": [],
            }
        ]
        _save_accumulated_stories(sprint_id, sprint_name, tmp_path, prior_stories)

        result_b = _make_coordinator_result(success=True, cost=9.26)

        with patch("theforge.sprint.runner.run_task", return_value=result_b):
            run_sprint(config, manifest_path, resume=True, run_id="run-b-test")

        summary_path = tmp_path / ".forge" / "logs" / sprint_name / "sprint-summary.yaml"
        with open(summary_path, encoding="utf-8") as f:
            summary = yaml.safe_load(f)

        stories = {story["slug"]: story for story in summary["stories"]}
        assert set(stories) == {"feature-a", "feature-b"}
        assert stories["feature-a"]["outcome"] == "DONE"
        assert stories["feature-a"]["story_run_id"] == "run-a-test"
        assert stories["feature-b"]["story_run_id"] is not None
        assert summary["sprint"]["specs_total"] == 2
        assert summary["sprint"]["specs_succeeded"] == 2
        assert summary["sprint"]["total_cost_usd"] == pytest.approx(10.44)
