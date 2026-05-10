"""Tests for sprint reporting across run_id boundaries.

Verifies that when a sprint spans multiple worker processes (run_id rollover),
forge status and sprint-summary.yaml show all stories from the full logical
sprint, not just those completed under the terminal run_id.
"""

from __future__ import annotations

import datetime
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
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.status_reader import (
    _follow_redirect_chain,
    find_sprint_summary,
    read_completed_status,
    read_live_status,
)
from theforge.task import TaskStory

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
        under run_id B (resume). The final summary must show both, and the
        resumed skip_merged story must remain ALREADY_DONE even if accumulated
        state was only created by the first run.
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

        # Operator-facing counters and summary now project from the same
        # canonical structure — the SoT story makes them agree by
        # construction (summary = banner = SprintResult).
        assert sprint_result.specs_succeeded == 2
        assert sprint_result.specs_skipped == 0

    def test_summary_marks_skip_merged_story_already_done_without_prior_state(
        self, tmp_path: Path
    ) -> None:
        """Resume summary uses triage to preserve ALREADY_DONE before sprint-end persistence."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
        config = _make_config(tmp_path)
        sprint_name = "Test Sprint"

        _get_or_create_sprint_id(sprint_name, tmp_path)

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

        assert sprint_result.specs_total == 2

        summary_path = tmp_path / ".forge" / "logs" / sprint_name / "sprint-summary.yaml"
        with open(summary_path, encoding="utf-8") as f:
            summary = yaml.safe_load(f)

        stories = {story["slug"]: story for story in summary["stories"]}
        # Resume skip_merged surfaces as canonical SKIPPED with the triage
        # reason — the SoT canonical structure routes legacy "already merged"
        # into the SKIPPED bucket so all surfaces agree by construction.
        assert stories["feature-a"]["outcome"] == "SKIPPED"
        assert stories["feature-b"]["outcome"] == "DONE"
        assert summary["sprint"]["specs_succeeded"] == 1
        assert summary["sprint"]["specs_skipped"] == 1

    def test_live_status_includes_closed_issue_dropped_at_fetch(self, tmp_path: Path) -> None:
        """Live status retains closed issue stories omitted from resolved.stories."""
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            yaml.dump(
                {
                    "name": "Test Sprint",
                    "budget_usd": 50.0,
                    "stories": [{"issue": 959}, {"issue": 960}],
                }
            ),
            encoding="utf-8",
        )
        config = _make_config(tmp_path)

        issue_960 = TaskStory(
            name="Issue 960", story_path=None, slug="issue-960", github_issue=960
        )
        resolved = ResolvedSprint(
            name="Test Sprint",
            budget_usd=50.0,
            stories=[(issue_960, MagicMock(), "issue:960")],
            closed_dependency_slugs={"issue-959"},
        )

        with patch("theforge.sprint.runner.resolve_from_manifest", return_value=resolved):
            with patch("theforge.sprint.runner.run_task", return_value=_make_coordinator_result()):
                with patch("theforge.sprint.runner.SprintStateWriter.remove"):
                    run_sprint(config, manifest_path, resume=True, run_id="run-live-test")

        entries = read_live_status("run-live-test", tmp_path)
        assert entries is not None
        stories = {entry.slug: entry for entry in entries}
        assert stories["issue-959"].status == "done"
        # Closed-dependency stories surface as resume-skip-merged to
        # distinguish them from preflight-verdict ALREADY_DONE outcomes.
        assert stories["issue-959"].detail == "ALREADY_DONE (merged)"
        # issue-960 now surfaces with its canonical terminal outcome — the
        # live status agrees with the banner and summary by construction.
        assert stories["issue-960"].status in {"done", "failed"}

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

    def test_substrate_audit_record_includes_sprint_id(self, tmp_path: Path) -> None:
        """Audit substrate rows carry sprint_id alongside the audit record."""
        from theforge.coordinator import audit_substrate

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)
        sprint_name = "Test Sprint"

        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner.run_task", return_value=result_a):
            run_sprint(config, manifest_path, run_id="run-a-test")

        # The substrate must contain at least one row referencing this sprint_id.
        sub_path = audit_substrate.substrate_path(tmp_path)
        assert sub_path.exists()
        conn = audit_substrate.require_substrate(tmp_path)
        try:
            sprint_carrying = [
                rec
                for rec in audit_substrate.iter_records(conn)
                if isinstance(rec.get("sprint"), dict)
                and rec["sprint"].get("sprint_id") == sprint_id
            ]
        finally:
            conn.close()
        assert sprint_carrying, "No substrate row carries the sprint_id"

    def test_resume_persists_already_done_story_before_reexec_handoff(
        self, tmp_path: Path
    ) -> None:
        """Resume-mode skip_merged stories are persisted before sprint-end summary writing."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)
        sprint_id = _get_or_create_sprint_id("Test Sprint", tmp_path)

        skip_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=skip_triage):
            run_sprint(config, manifest_path, resume=True, run_id="run-resume-test")

        stories = _load_accumulated_stories(sprint_id, tmp_path)
        assert len(stories) == 1
        assert stories[0]["canonical_ref"] == "feature-a.md"
        assert stories[0]["outcome"] == "ALREADY_DONE"

    def test_resume_preserves_prior_accumulated_stories_when_persisting_skip_merged(
        self, tmp_path: Path
    ) -> None:
        """Resume persistence merges new ALREADY_DONE entries into prior accumulated state."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-b.md"])
        config = _make_config(tmp_path)
        sprint_name = "Test Sprint"
        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        _save_accumulated_stories(
            sprint_id,
            sprint_name,
            tmp_path,
            [
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
            ],
        )

        skip_triage = StoryTriage(
            story_path="feature-b.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
            slug="feature-b",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=skip_triage):
            run_sprint(config, manifest_path, resume=True, run_id="run-resume-test")

        accumulated = _load_accumulated_stories(sprint_id, tmp_path)
        stories = {story["canonical_ref"]: story for story in accumulated}
        assert set(stories) == {"feature-a.md", "feature-b.md"}
        assert stories["feature-a.md"]["outcome"] == "DONE"
        assert stories["feature-b.md"]["outcome"] == "ALREADY_DONE"

    def test_run_sprint_removes_live_state_file_on_success(self, tmp_path: Path) -> None:
        """Successful sprint completion removes the live state file.

        This avoids false crash reports from stale live-state files.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner.run_task", return_value=result_a):
            run_sprint(config, manifest_path, run_id="run-cleanup-test")

        assert not (tmp_path / ".forge" / "runs" / "run-cleanup-test.state").exists()


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

    def test_live_state_does_not_show_running_story_as_skipped_from_prior_state(
        self, tmp_path: Path
    ) -> None:
        """A story actively running in the current run must not be shown as
        skipped due to a terminal outcome persisted from an earlier run.

        Regression: when a prior resume left a story with outcome=SKIPPED in
        the sprint-wide accumulated state (e.g. an unsatisfied dependency),
        seeding the canonical structure with that terminal outcome locked
        the story in SKIPPED via the monotonicity invariant. Subsequent
        transitions to RUNNING were rejected, while phase/cost continued to
        update, producing a live row that combined "skipped" status with
        active phase and cost — the exact symptom from issue #1146.
        """
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-b.md"])
        config = _make_config(tmp_path)
        sprint_name = "Test Sprint"

        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        _save_accumulated_stories(
            sprint_id,
            sprint_name,
            tmp_path,
            [
                {
                    "canonical_ref": "feature-b.md",
                    "slug": "feature-b",
                    "path": "feature-b.md",
                    "outcome": "SKIPPED",
                    "verdict": None,
                    "cost_usd": 0.31,
                    "story_run_id": "run-prior",
                    "preflight": None,
                    "merge": False,
                    "batch": 0,
                    "depends_on": [],
                }
            ],
        )

        full_triage = StoryTriage(
            story_path="feature-b.md",
            action="full",
            reason="ready to run",
            worktree_path=None,
            slug="feature-b",
        )

        def _mock_triage(canonical_ref, cfg, project_root, task=None):
            return full_triage

        captured: dict[str, object] = {}

        def _capture_live_state(*args, **kwargs):
            entries = read_live_status("run-current", tmp_path)
            assert entries is not None
            by_slug = {entry.slug: entry for entry in entries}
            captured["live"] = by_slug.get("feature-b")
            return _make_coordinator_result(success=True, cost=2.5)

        with (
            patch("theforge.sprint.runner._triage_spec", side_effect=_mock_triage),
            patch("theforge.sprint.runner.run_task", side_effect=_capture_live_state),
        ):
            run_sprint(config, manifest_path, resume=True, run_id="run-current")

        live = captured.get("live")
        assert live is not None, "feature-b should appear in live status while running"
        # The running story must not display the terminal "skipped" status from
        # the prior run's accumulated state.
        assert live.status != "skipped", (
            f"feature-b shown as skipped while actively running: {live!r}"
        )
        # Detail must not be the stale terminal "SKIPPED" carried from prior state.
        assert live.detail != "SKIPPED", (
            f"feature-b detail leaked stale terminal outcome: {live!r}"
        )

    def test_live_state_includes_prior_run_skip_merged_story(self, tmp_path: Path) -> None:
        """Live status keeps prior-run completed stories visible after resume triage."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
        config = _make_config(tmp_path)
        sprint_name = "Test Sprint"

        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        _save_accumulated_stories(
            sprint_id,
            sprint_name,
            tmp_path,
            [
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
            ],
        )

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

        def _capture_live_state(*args, **kwargs):
            entries = read_live_status("run-b-test", tmp_path)
            assert entries is not None
            by_slug = {entry.slug: entry for entry in entries}
            assert by_slug["feature-a"].status == "done"
            assert by_slug["feature-a"].detail == "ALREADY_DONE"
            assert by_slug["feature-b"].status in {"waiting", "running", "done"}
            return result_b

        with (
            patch("theforge.sprint.runner._triage_spec", side_effect=_mock_triage),
            patch("theforge.sprint.runner.run_task", side_effect=_capture_live_state),
        ):
            run_sprint(config, manifest_path, resume=True, run_id="run-b-test")

    def test_live_state_includes_closed_issue_dropped_at_query_time(self, tmp_path: Path) -> None:
        """Live status surfaces closed-at-fetch issues even when query mode drops them."""
        config = _make_config(tmp_path)
        result_b = _make_coordinator_result(success=True, cost=9.26)

        def _mock_build_resolved_sprint(*args, **kwargs):
            task = TaskStory(
                name="Issue 960",
                slug="issue-960",
                story_text="# Issue 960",
                depends_on=[],
                inferred_dependencies=[],
                dependency_warnings=[],
                github_issue=960,
            )
            return ResolvedSprint(
                name="Test Sprint",
                budget_usd=50.0,
                stories=[(task, MagicMock(), "issue:960")],
                max_parallel=None,
                worker_timeout_seconds=None,
                closed_dependency_slugs={"issue-959"},
            )

        def _capture_live_state(*args, **kwargs):
            entries = read_live_status("run-b-test", tmp_path)
            assert entries is not None
            by_slug = {entry.slug: entry for entry in entries}
            assert by_slug["issue-959"].status == "done"
            assert by_slug["issue-959"].detail == "ALREADY_DONE"
            assert by_slug["issue-960"].status in {"waiting", "running", "done"}
            return result_b

        with patch("theforge.sprint.runner.run_task", side_effect=_capture_live_state):
            sprint_result = run_sprint(
                config,
                sprint=_mock_build_resolved_sprint(),
                run_id="run-b-test",
            )

        # Canonical total now includes closed-dependency slugs registered in
        # the canonical structure (issue-959 + issue-960) — surfaces all agree.
        assert sprint_result.specs_total == 2

    def test_accumulated_state_preserves_prior_removed_story(self, tmp_path: Path) -> None:
        """Saving current-run state does not drop prior stories absent from this manifest."""
        sprint_name = "Test Sprint"
        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        prior_story = {
            "canonical_ref": "feature-a.md",
            "slug": "feature-a",
            "path": "feature-a.md",
            "outcome": "DONE",
            "verdict": "APPROVE",
            "cost_usd": 1.18,
        }
        _save_accumulated_stories(sprint_id, sprint_name, tmp_path, [prior_story])

        from theforge.sprint.audit import _write_sprint_summary
        from theforge.sprint.manifest import SprintManifest, SprintResult

        sprint_log_dir = tmp_path / ".forge" / "logs" / sprint_name
        manifest = SprintManifest(name=sprint_name, budget_usd=50.0, stories=["feature-b.md"])
        result = SprintResult(
            name=sprint_name,
            specs_total=1,
            specs_succeeded=1,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=9.26,
            budget_usd=50.0,
            results=[],
            stopped_reason=None,
        )

        _write_sprint_summary(
            manifest=manifest,
            result=result,
            canonical_refs=["feature-b.md"],
            started_at=datetime.datetime.now(datetime.timezone.utc),
            finished_at=datetime.datetime.now(datetime.timezone.utc),
            duration=1.0,
            sprint_log_dir=sprint_log_dir,
            story_times={},
            batch_assignments={},
            slug_map={"feature-b.md": "feature-b"},
            run_id="run-b-test",
            tasks_by_slug={},
            ci_break_slug=None,
            sprint_id=sprint_id,
            project_root=tmp_path,
            dropped_slugs={},
            skipped_issues=[],
        )

        accumulated = _load_accumulated_stories(sprint_id, tmp_path)
        assert {story["canonical_ref"] for story in accumulated} == {"feature-a.md"}

    def test_summary_carries_forward_prior_stories_absent_from_manifest(
        self, tmp_path: Path
    ) -> None:
        """Stories completed in earlier resumes must appear in the final summary
        even when the final resume's manifest no longer references them.

        Reproduces the bug: a sprint launched against [#119, #169, #510] has
        #119 and #169 merge in resume N. By resume N+1, those issues are closed
        and the re-resolved manifest only contains #510. The summary written by
        the final resume must still surface #119 and #169 as DONE, and the
        totals must reflect the full sprint lifespan (3 succeeded, not 1).
        """
        from theforge.sprint.audit import _write_sprint_summary
        from theforge.sprint.manifest import SprintManifest, SprintResult

        sprint_name = "issues-119,169,510"
        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        prior_stories = [
            {
                "canonical_ref": "issue:119",
                "slug": "issue-119",
                "path": "Issue #119",
                "outcome": "DONE",
                "verdict": "APPROVE",
                "cost_usd": 1.5,
                "merge": True,
                "story_run_id": "run-a",
            },
            {
                "canonical_ref": "issue:169",
                "slug": "issue-169",
                "path": "Issue #169",
                "outcome": "DONE",
                "verdict": "APPROVE",
                "cost_usd": 2.5,
                "merge": True,
                "story_run_id": "run-a",
            },
        ]
        _save_accumulated_stories(sprint_id, sprint_name, tmp_path, prior_stories)

        # Final resume: only issue:510 remains in the re-resolved manifest.
        manifest = SprintManifest(name=sprint_name, budget_usd=50.0, stories=["issue:510"])
        result = SprintResult(
            name=sprint_name,
            specs_total=1,
            specs_succeeded=1,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=3.0,
            budget_usd=50.0,
            results=[],
            stopped_reason=None,
        )
        sprint_log_dir = tmp_path / ".forge" / "logs" / sprint_name

        _write_sprint_summary(
            manifest=manifest,
            result=result,
            canonical_refs=["issue:510"],
            started_at=datetime.datetime.now(datetime.timezone.utc),
            finished_at=datetime.datetime.now(datetime.timezone.utc),
            duration=1.0,
            sprint_log_dir=sprint_log_dir,
            slug_map={"issue:510": "issue-510"},
            run_id="run-c",
            sprint_id=sprint_id,
            project_root=tmp_path,
        )

        summary = yaml.safe_load((sprint_log_dir / "sprint-summary.yaml").read_text())
        slugs = {s["slug"] for s in summary["stories"]}
        assert slugs == {"issue-119", "issue-169", "issue-510"}, (
            f"Summary missing prior-resume stories: {slugs}"
        )
        by_slug = {s["slug"]: s for s in summary["stories"]}
        assert by_slug["issue-119"]["outcome"] == "DONE"
        assert by_slug["issue-169"]["outcome"] == "DONE"
        # Totals reflect the full sprint lifespan, not just the final resume.
        assert summary["sprint"]["specs_total"] == 3
        # Accumulated state must retain the prior stories so subsequent resumes
        # also see the full lifespan.
        accumulated = _load_accumulated_stories(sprint_id, tmp_path)
        assert {s["canonical_ref"] for s in accumulated} >= {"issue:119", "issue:169"}

    def test_summary_carries_forward_prior_stories_with_story_state(self, tmp_path: Path) -> None:
        """Same scenario as the previous test, but exercising the canonical
        story_state projection path used by the live runner. story_state is
        pre-populated from accumulated state (as runner.py does at startup),
        and the summary's totals come from story_state.counts() — so prior
        stories absent from canonical_refs must still be represented.
        """
        from theforge.sprint.audit import _write_sprint_summary
        from theforge.sprint.manifest import SprintManifest, SprintResult
        from theforge.sprint.story_state import SprintStoryState, StoryOutcome

        sprint_name = "issues-119,169,510"
        sprint_id = _get_or_create_sprint_id(sprint_name, tmp_path)
        prior_stories = [
            {
                "canonical_ref": "issue:119",
                "slug": "issue-119",
                "path": "Issue #119",
                "outcome": "DONE",
                "cost_usd": 1.5,
                "merge": True,
            },
            {
                "canonical_ref": "issue:169",
                "slug": "issue-169",
                "path": "Issue #169",
                "outcome": "DONE",
                "cost_usd": 2.5,
                "merge": True,
            },
        ]
        _save_accumulated_stories(sprint_id, sprint_name, tmp_path, prior_stories)

        # Simulate the runner pre-populating canonical state from accumulated
        # state, then registering and completing the current-resume story.
        story_state = SprintStoryState()
        story_state.register(
            "issue-119",
            "Issue #119",
            outcome=StoryOutcome.DONE,
            cost_usd=1.5,
            canonical_ref="issue:119",
        )
        story_state.register(
            "issue-169",
            "Issue #169",
            outcome=StoryOutcome.DONE,
            cost_usd=2.5,
            canonical_ref="issue:169",
        )
        story_state.register(
            "issue-510",
            "Issue #510",
            outcome=StoryOutcome.DONE,
            cost_usd=3.0,
            canonical_ref="issue:510",
        )

        manifest = SprintManifest(name=sprint_name, budget_usd=50.0, stories=["issue:510"])
        result = SprintResult(
            name=sprint_name,
            specs_total=3,
            specs_succeeded=3,
            specs_failed=0,
            specs_skipped=0,
            total_cost_usd=7.0,
            budget_usd=50.0,
            results=[],
            stopped_reason=None,
        )
        sprint_log_dir = tmp_path / ".forge" / "logs" / sprint_name

        _write_sprint_summary(
            manifest=manifest,
            result=result,
            canonical_refs=["issue:510"],
            started_at=datetime.datetime.now(datetime.timezone.utc),
            finished_at=datetime.datetime.now(datetime.timezone.utc),
            duration=1.0,
            sprint_log_dir=sprint_log_dir,
            slug_map={"issue:510": "issue-510"},
            run_id="run-c",
            sprint_id=sprint_id,
            project_root=tmp_path,
            story_state=story_state,
        )

        summary = yaml.safe_load((sprint_log_dir / "sprint-summary.yaml").read_text())
        slugs = {s["slug"] for s in summary["stories"]}
        assert slugs == {"issue-119", "issue-169", "issue-510"}, (
            f"Summary missing prior-resume stories: {slugs}"
        )
        # Totals project from canonical story_state and must include prior
        # stories that were pre-populated from accumulated state.
        assert summary["sprint"]["specs_total"] == 3
        assert summary["sprint"]["specs_succeeded"] == 3
        assert summary["sprint"]["specs_failed"] == 0
