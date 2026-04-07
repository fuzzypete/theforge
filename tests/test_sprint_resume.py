"""Tests for sprint resume/triage: _triage_spec, resume logic, depends_on parsing."""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import theforge.pending as pending
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
from theforge.sprint.dag import StoryTriage, _triage_spec
from theforge.sprint.lock import acquire_story_locks, release_story_locks
from theforge.sprint.manifest import _build_task_from_story

# ── Helpers ──────────────────────────────────────────────────────────


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


def _make_spec_file(
    tmp_path: Path, name: str, slug: str, depends_on: list[str] | None = None
) -> Path:
    spec = tmp_path / f"{slug}.md"
    frontmatter = f"name: {name}\nslug: {slug}"
    if depends_on is not None:
        if len(depends_on) == 1:
            frontmatter += f"\ndepends_on: {depends_on[0]}"
        else:
            frontmatter += "\ndepends_on:\n" + "".join(f"  - {d}\n" for d in depends_on)
    spec.write_text(
        f"---\n{frontmatter}\n---\n# {name}\nDo the thing.",
        encoding="utf-8",
    )
    return spec


def _make_manifest(tmp_path: Path, specs: list[str], budget: float = 10.0) -> Path:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        yaml.dump(
            {
                "name": "Test Sprint",
                "budget_usd": budget,
                "specs": specs,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _make_coordinator_result(
    success: bool = True,
    cost: float = 1.0,
    preflight_verdict: str = "PROCEED",
    phase: Phase = Phase.DONE,
    merged: bool = False,
) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = preflight_verdict
    # Fake cost via preflight result mock
    mock_preflight = MagicMock()
    mock_preflight.cost_usd = cost
    state.preflight_result = mock_preflight
    return CoordinatorResult(
        success=success,
        phase=phase,
        state=state,
        message="Done." if success else "Failed.",
        merge={"merged": True} if merged else None,
    )


# ── Sprint resume / triage tests ─────────────────────────────────────


class TestTriageSpec:
    def test_triage_merged_spec(self, tmp_path: Path) -> None:
        """Branch already merged to base → skip_merged."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 0  # is ancestor
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"3"  # base is 3 commits ahead of branch — truly merged
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert "merged" in triage.reason

    def test_triage_branch_at_base_head_not_merged(self, tmp_path: Path) -> None:
        """Branch at base HEAD with 0 commits ahead → full (not skip_merged)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 0  # is ancestor (trivially — same commit)
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"0"  # base not ahead of branch — created at base HEAD, not merged
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert triage.reason == "branch is at main HEAD with 0 commits ahead"

    def test_triage_worktree_with_passing_gate(self, tmp_path: Path) -> None:
        """Worktree exists, commits ahead, gate passes → review."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        # Create fake worktree directory
        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1  # not merged
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b"abc123 some commit\n"
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint.dag._run_gate", return_value=("PASS", None, "")):
                triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "review"
        assert triage.worktree_path == worktree

    def test_triage_worktree_with_failing_gate(self, tmp_path: Path) -> None:
        """Worktree exists, commits ahead, gate fails → dev."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b"abc123 some commit\n"
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint.dag._run_gate", return_value=("FAIL", "tests failed", "")):
                triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "dev"
        assert triage.worktree_path == worktree

    def test_triage_no_worktree(self, tmp_path: Path) -> None:
        """No worktree found → full."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1  # not merged
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert "no worktree" in triage.reason

    def test_triage_stale_worktree_no_commits(self, tmp_path: Path) -> None:
        """Worktree exists but 0 commits ahead of base → full (stale)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b""  # no commits ahead
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert "stale" in triage.reason or "0 commits" in triage.reason

    def test_triage_same_tip_missing_worktree_without_audit_runs_full(
        self, tmp_path: Path
    ) -> None:
        """Missing worktree at base HEAD with no audit trail stays stale/full."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "log" in cmd:
                m.returncode = 0
                m.stdout = b""
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"2"
            elif "--is-ancestor" in cmd:
                m.returncode = 1
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with (
            patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run),
            patch("theforge.sprint.dag.has_review_approve", return_value=False),
        ):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert triage.reason == "no worktree found"

    def test_triage_same_tip_missing_worktree_with_prior_approve_skips_when_squash_merged(
        self, tmp_path: Path
    ) -> None:
        """Missing worktree plus audit-backed squash-merged branch is treated as merged."""
        import json

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {
            "task": {"slug": "feature-a"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits_dir / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "log" in cmd:
                m.returncode = 0
                m.stdout = b""
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"0"
            elif "--is-ancestor" in cmd:
                m.returncode = 0
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert "merged" in triage.reason

    def test_triage_same_tip_worktree_with_prior_approve_skips_when_merged(
        self, tmp_path: Path
    ) -> None:
        """Same-tip branch with audit-backed FF merge should still skip_merged."""
        import json

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {
            "task": {"slug": "feature-a"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits_dir / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "log" in cmd:
                m.returncode = 0
                m.stdout = b""  # 0 commits ahead
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"0"  # branch and base at same tip
            elif "--is-ancestor" in cmd:
                m.returncode = 0
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert "merged" in triage.reason

    def test_triage_worktree_with_prior_approve(self, tmp_path: Path) -> None:
        """Worktree has commits ahead and prior APPROVE in audit trail → skip."""
        import json

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        # Write an APPROVE record to history.jsonl
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {
            "task": {"slug": "feature-a"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits_dir / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1  # not merged
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b"abc123 some commit\n"
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert "APPROVE" in triage.reason or "approve" in triage.reason.lower()
        assert triage.worktree_path is None

    def test_triage_gate_pass_no_approve_routes_to_review(self, tmp_path: Path) -> None:
        """Worktree with commits, gate passes, but no APPROVE → review (not skip)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b"abc123 some commit\n"
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint.dag._run_gate", return_value=("PASS", None, "")):
                triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "review"


class TestResumeSprintSkipApproved:
    def test_resume_sprint_skips_approved(self, tmp_path: Path) -> None:
        """Resume sprint: spec with prior APPROVE is treated as already satisfied."""
        import json

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        # Write an APPROVE record
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {"task": {"slug": "feature-a"}, "reviews": [{"verdict": "APPROVE"}]}
        (audits_dir / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        skip_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="prior APPROVE in audit trail; branch already satisfied (2 commits ahead)",
            worktree_path=None,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=skip_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run_task:
                result = run_sprint(config, manifest_path, resume=True)

        mock_run_task.assert_not_called()
        assert result.specs_succeeded == 1


class TestResumeSprintIntegration:
    def test_resume_sprint_skips_merged(self, tmp_path: Path) -> None:
        """End-to-end resume: merged spec counts as succeeded (work is done)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        merged_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=merged_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run:
                result = run_sprint(config, manifest_path, resume=True)

        mock_run.assert_not_called()
        assert result.specs_succeeded == 1  # already-merged = success
        assert result.specs_skipped == 0

    def test_resume_sprint_enters_dev(self, tmp_path: Path) -> None:
        """End-to-end resume: gate-failing worktree uses run_from_dev."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        dev_triage = StoryTriage(
            story_path="feature-a.md",
            action="dev",
            reason="gate fails",
            worktree_path=worktree,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec", return_value=dev_triage):
            with patch(
                "theforge.sprint.runner.run_from_dev", return_value=coord_result
            ) as mock_dev:
                with patch("theforge.sprint.runner.run_task") as mock_task:
                    result = run_sprint(config, manifest_path, resume=True)

        mock_dev.assert_called_once()
        mock_task.assert_not_called()
        assert result.specs_succeeded == 1

    def test_resume_budget_exhausted_merged_spec_still_succeeds(self, tmp_path: Path) -> None:
        """Merged spec counts as succeeded even when budget is exhausted."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=1.0)
        config = _make_config(tmp_path)

        # Prior run spent $2 — budget exhausted
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        prior_audit = {"sprint": {"total_cost_usd": 2.0}}
        with open(audits_dir / "sprint-audit.yaml", "w") as f:
            yaml.dump(prior_audit, f)

        merged_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
        )
        full_triage = StoryTriage(
            story_path="feature-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )

        def triage_side_effect(spec_path, config, project_root, *, task=None):
            if "feature-a" in spec_path:
                return merged_triage
            return full_triage

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task") as mock_run:
                result = run_sprint(config, manifest_path, resume=True)

        # Merged spec should be succeeded, not budget-skipped
        assert result.specs_succeeded == 1  # feature-a (merged)
        assert result.specs_skipped == 1  # feature-b (budget)
        mock_run.assert_not_called()

    def test_resume_sprint_enters_review(self, tmp_path: Path) -> None:
        """End-to-end resume: gate-passing worktree uses run_from_review."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        review_triage = StoryTriage(
            story_path="feature-a.md",
            action="review",
            reason="gate passes",
            worktree_path=worktree,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec", return_value=review_triage):
            with patch(
                "theforge.sprint.runner.run_from_review", return_value=coord_result
            ) as mock_rev:
                with patch("theforge.sprint.runner.run_task") as mock_task:
                    result = run_sprint(config, manifest_path, resume=True)

        mock_rev.assert_called_once()
        mock_task.assert_not_called()
        assert result.specs_succeeded == 1

    def test_resume_cost_continuity(self, tmp_path: Path) -> None:
        """Prior sprint cost is carried forward into total_cost_usd."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        # Write a prior sprint-audit.yaml with a known cost
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        prior_audit = {"sprint": {"total_cost_usd": 3.50}}
        with open(audits_dir / "sprint-audit.yaml", "w") as f:
            yaml.dump(prior_audit, f)

        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.runner.run_task", return_value=coord_result):
                result = run_sprint(config, manifest_path, resume=True)

        # total should be prior (3.50) + new (1.00)
        assert result.total_cost_usd == pytest.approx(4.50)

    def test_resume_prior_cost_exceeds_budget(self, tmp_path: Path) -> None:
        """When prior cost already meets/exceeds budget, first spec is skipped."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=5.0)
        config = _make_config(tmp_path)

        # Prior run already spent $6 (over the $5 budget)
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        prior_audit = {"sprint": {"total_cost_usd": 6.0}}
        with open(audits_dir / "sprint-audit.yaml", "w") as f:
            yaml.dump(prior_audit, f)

        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run:
                result = run_sprint(config, manifest_path, resume=True)

        # Spec should be skipped — prior cost alone exceeds budget
        mock_run.assert_not_called()
        assert result.specs_skipped == 1
        assert result.stopped_reason is not None
        assert "budget" in result.stopped_reason.lower()

    def test_no_resume_flag_unchanged(self, tmp_path: Path) -> None:
        """Without --resume, behavior is unchanged (run_task called normally)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec") as mock_triage:
            with patch("theforge.sprint.runner.run_task", return_value=coord_result) as mock_task:
                result = run_sprint(config, manifest_path, resume=False)

        mock_triage.assert_not_called()
        mock_task.assert_called_once()
        assert result.specs_succeeded == 1


# ── _build_task_from_story depends_on parsing ─────────────────────────


class TestBuildTaskDependsOn:
    def test_depends_on_missing(self, tmp_path: Path) -> None:
        """No depends_on in frontmatter → depends_on == []."""
        spec = _make_spec_file(tmp_path, "Spec A", "spec-a")
        task = _build_task_from_story(spec)
        assert task.depends_on == []

    def test_depends_on_single_string(self, tmp_path: Path) -> None:
        """depends_on as single string → normalized to single-element list."""
        spec = _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["single-slug"])
        task = _build_task_from_story(spec)
        assert task.depends_on == ["single-slug"]

    def test_depends_on_list(self, tmp_path: Path) -> None:
        """depends_on as list → preserved as list."""
        spec = _make_spec_file(tmp_path, "Spec C", "spec-c", depends_on=["slug-a", "slug-b"])
        task = _build_task_from_story(spec)
        assert task.depends_on == ["slug-a", "slug-b"]


# ── Sprint dependency checking ────────────────────────────────────────


class TestSprintDependencies:
    def test_skips_dependent_spec_on_failed_dependency(self, tmp_path: Path) -> None:
        """Spec B is skipped (but sprint continues) when spec-a did not merge."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        # Spec A succeeds but does NOT merge (merge=None)
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            result = run_sprint(config, manifest_path, auto_merge=True)

        assert mock_run.call_count == 1  # only spec-a ran (spec-b skipped, no more specs)
        assert result.specs_succeeded == 1
        assert result.specs_skipped == 1  # spec-b skipped
        assert result.stopped_reason is None  # sprint was NOT halted

    def test_proceeds_when_dependency_merged(self, tmp_path: Path) -> None:
        """Spec B proceeds when spec-a merged successfully."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=True)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            result = run_sprint(config, manifest_path, auto_merge=True)

        assert mock_run.call_count == 2
        assert result.specs_succeeded == 2
        assert result.stopped_reason is None

    def test_proceeds_normally_when_no_depends_on(self, tmp_path: Path) -> None:
        """Existing behavior preserved: specs without depends_on always run."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)
        result_b = _make_coordinator_result(success=True, cost=1.0)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            result = run_sprint(config, manifest_path)

        assert mock_run.call_count == 2
        assert result.specs_failed == 1
        assert result.specs_succeeded == 1
        assert result.stopped_reason is None

    def test_resume_merged_satisfies_dependency(self, tmp_path: Path) -> None:
        """Resume mode: spec triaged as skip_merged counts as merged for deps."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        merged_triage = StoryTriage(
            story_path="spec-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
            slug="spec-a",
        )
        full_triage = StoryTriage(
            story_path="spec-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="spec-b",
        )
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=True)

        def triage_side_effect(spec_path, config, project_root, *, task=None):
            if "spec-a" in spec_path:
                return merged_triage
            return full_triage

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task", return_value=result_b) as mock_run:
                result = run_sprint(config, manifest_path, resume=True)

        # spec-a was skip_merged (counted as succeeded), spec-b ran successfully
        mock_run.assert_called_once()
        assert result.specs_succeeded == 2
        assert result.stopped_reason is None

    def test_resume_approved_satisfies_dependency(self, tmp_path: Path) -> None:
        """Resume mode: prior-APPROVE triage is treated as already satisfied for deps."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        approved_triage = StoryTriage(
            story_path="spec-a.md",
            action="skip_merged",
            reason="already approved",
            worktree_path=None,
            slug="spec-a",
        )
        full_triage = StoryTriage(
            story_path="spec-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="spec-b",
        )
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=True)

        def triage_side_effect(spec_path, config, project_root, *, task=None):
            if "spec-a" in spec_path:
                return approved_triage
            return full_triage

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task", return_value=result_b) as mock_run:
                result = run_sprint(config, manifest_path, resume=True)

        # spec-a was skip (prior APPROVE) — should satisfy dep so spec-b runs
        mock_run.assert_called_once()
        assert result.specs_succeeded == 2
        assert result.stopped_reason is None

    def test_skips_dependent_continues_independent(self, tmp_path: Path) -> None:
        """Three specs: A, B (depends on spec-a), C. A doesn't merge → B skipped, C still runs."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        _make_spec_file(tmp_path, "Spec C", "spec-c")
        manifest_path = _make_manifest(
            tmp_path, ["spec-a.md", "spec-b.md", "spec-c.md"], budget=10.0
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result_c = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_c]
        ) as mock_run:
            result = run_sprint(config, manifest_path)

        # A ran, B was skipped (dependency failed), C still ran
        assert mock_run.call_count == 2
        assert result.specs_skipped == 1  # only B skipped
        assert result.specs_succeeded == 2  # A and C succeeded
        assert result.stopped_reason is None  # sprint was not halted

    def test_eager_merge_fires_for_spec_with_downstream_dependent(self, tmp_path: Path) -> None:
        """Eager merge: auto_merge=True is passed for specs that have downstream dependents."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=True)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            run_sprint(config, manifest_path, auto_merge=False)

        # First call (spec-a) must have auto_merge=True due to eager merge
        first_call_kwargs = mock_run.call_args_list[0].kwargs
        assert first_call_kwargs["auto_merge"] is True

    def test_eager_merge_does_not_fire_for_spec_without_downstream_dependent(
        self, tmp_path: Path
    ) -> None:
        """Spec A has no downstream dependents — auto_merge setting is respected as-is."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            run_sprint(config, manifest_path, auto_merge=False)

        # Neither call should override auto_merge
        for call in mock_run.call_args_list:
            assert call.kwargs["auto_merge"] is False

    def test_resume_prior_approve_does_not_run_review_or_dev(self, tmp_path: Path) -> None:
        """Resume mode: prior APPROVE stories do not re-enter review/dev flows."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        approved_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="prior APPROVE in audit trail; branch already satisfied (2 commits ahead)",
            worktree_path=None,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=approved_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run_task:
                with patch("theforge.sprint.runner.run_from_review") as mock_review:
                    with patch("theforge.sprint.runner.run_from_dev") as mock_dev:
                        result = run_sprint(config, manifest_path, resume=True)

        mock_run_task.assert_not_called()
        mock_review.assert_not_called()
        mock_dev.assert_not_called()
        assert result.specs_succeeded == 1
        assert result.total_cost_usd == 0.0

    def test_already_done_satisfies_dependency(self, tmp_path: Path) -> None:
        """ALREADY_DONE spec counts as merged for dependency purposes (changes already on main)."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(
            success=True, cost=0.1, preflight_verdict="ALREADY_DONE", phase=Phase.DONE
        )
        result_b = _make_coordinator_result(success=True, cost=1.0)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            result = run_sprint(config, manifest_path)

        assert mock_run.call_count == 2  # both specs ran
        assert result.specs_skipped == 1  # spec-a counted as skipped (ALREADY_DONE)
        assert result.specs_succeeded == 1  # spec-b succeeded
        assert result.stopped_reason is None  # no halt

    def test_resume_cleans_stale_lock_and_pending_files(self, tmp_path: Path) -> None:
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        lock_dir = tmp_path / ".forge" / "locks"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "feature-a.lock"
        dead_pid = os.getpid() + 99999
        lock_path.write_text(str(dead_pid), encoding="utf-8")

        pending_dir = tmp_path / ".forge" / "pending"
        pending_dir.mkdir(parents=True)
        pending_path = pending_dir / "resume-run.yaml"
        future = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60)
        ).isoformat()
        pending_path.write_text(
            yaml.safe_dump(
                {
                    "run_id": "resume-run",
                    "story": "feature-a",
                    "phase": "ESCALATE",
                    "reason": "r",
                    "options": ["approve"],
                    "created_at": future,
                    "timeout_at": future,
                    "pid": dead_pid,
                }
            ),
            encoding="utf-8",
        )

        review_triage = StoryTriage(
            story_path="feature-a.md",
            action="review",
            reason="gate passes",
            worktree_path=tmp_path / "feature-a",
        )
        review_triage.worktree_path.mkdir()
        coord_result = _make_coordinator_result(success=True, cost=1.0, merged=True)

        with patch("theforge.sprint.runner._triage_spec", return_value=review_triage):
            with patch("theforge.sprint.runner.run_from_review", return_value=coord_result):
                with patch("theforge.sprint.runner.pull_base_branch", return_value=True):
                    with patch("theforge.sprint.runner.run_batch_preflight", return_value={}):
                        result = run_sprint(config, manifest_path, resume=True)

        assert result.specs_succeeded == 1

        removed = pending.cleanup_stale(project_root=tmp_path)
        assert removed == 1
        assert not pending_path.exists()

        with patch("theforge.sprint.lock.fcntl.flock", side_effect=[BlockingIOError, None]):
            with patch("theforge.sprint.lock.os.kill", side_effect=ProcessLookupError):
                fds, conflicted = acquire_story_locks(["feature-a"], tmp_path)
        try:
            assert conflicted == []
            assert len(fds) == 1
        finally:
            release_story_locks(fds)


def test_run_sprint_timeout_writes_story_audit(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    spec = _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest = _make_manifest(tmp_path, [spec.name])

    class _NeverDoneFuture:
        def cancel(self):
            return True

        def result(self):
            raise AssertionError("should not be called")

    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, *args, **kwargs):
            return _NeverDoneFuture()

    with (
        patch("theforge.sprint.runner.pull_base_branch", return_value=True),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
        patch("theforge.sprint.runner.wait", return_value=(set(), set())),
    ):
        run_sprint(config, manifest)

    audit_path = tmp_path / ".forge" / "audits" / "history.jsonl"
    assert audit_path.exists()
    assert "feature-a" in audit_path.read_text(encoding="utf-8")
