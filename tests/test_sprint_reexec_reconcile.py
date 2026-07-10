"""Seam tests for re-exec merged-state reconciliation.

When a mid-sprint source change re-execs the sprint process, the restarted
process keeps the original argv (no ``--resume``) but must still reconcile every
manifest story against merged state before dispatch. A story whose PR already
landed in the prior (killed) generation must be excluded from batch preflight,
pre-marked complete in the DAG, and never re-entered through WORKSPACE — and its
prior DONE outcome / cost must be preserved.

See issue-1575: the redispatch-after-restart bug where a merged story's DONE
outcome was overwritten with FAILED and its cost zeroed.
"""

from __future__ import annotations

import os
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
from theforge.sprint.audit import persist_accumulated_story_state
from theforge.sprint.dag import StoryTriage
from theforge.sprint.story_state import SprintStoryState, StoryOutcome, coerce_outcome


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


def _make_manifest(tmp_path: Path, specs: list[str], budget: float = 10.0) -> Path:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        yaml.dump({"name": "Test Sprint", "budget_usd": budget, "specs": specs}),
        encoding="utf-8",
    )
    return manifest_path


def _set_sprint_id(tmp_path: Path, sprint_id: str = "sprint-123") -> str:
    sprint_dir = tmp_path / ".forge" / "logs" / "Test Sprint"
    sprint_dir.mkdir(parents=True, exist_ok=True)
    (sprint_dir / ".sprint_id").write_text(sprint_id, encoding="utf-8")
    return sprint_id


def _write_prior_sprint_audit(tmp_path: Path, sprint_id: str, total_cost_usd: float) -> None:
    audits_dir = tmp_path / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    with open(audits_dir / "sprint-audit.yaml", "w", encoding="utf-8") as f:
        yaml.dump({"sprint": {"sprint_id": sprint_id, "total_cost_usd": total_cost_usd}}, f)


def _make_coordinator_result(
    success: bool = True,
    cost: float = 1.0,
    preflight_verdict: str = "PROCEED",
    phase: Phase = Phase.DONE,
    landing_status: str | None = None,
) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = preflight_verdict
    mock_preflight = MagicMock()
    mock_preflight.cost_usd = cost
    state.preflight_result = mock_preflight
    return CoordinatorResult(
        success=success,
        phase=phase,
        state=state,
        message="Done." if success else "Failed.",
        landing_status=landing_status,
    )


def test_reexec_excludes_merged_story_from_preflight_and_dag_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-exec'd fresh launch (reexec=True, no --resume) must reconcile a
    merged story: exclude it from run_batch_preflight, pre-mark it complete in
    the DAG so the scheduler never dispatches it through WORKSPACE, and preserve
    its prior DONE outcome + cost."""
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)

    sprint_id = _set_sprint_id(tmp_path)
    _write_prior_sprint_audit(tmp_path, sprint_id, 0.33)
    # Prior generation completed feature-a (DONE, $0.33) and persisted it.
    persist_accumulated_story_state(
        sprint_id,
        "Test Sprint",
        tmp_path,
        [
            {
                "canonical_ref": "feature-a.md",
                "slug": "feature-a",
                "path": "feature-a.md",
                "outcome": "DONE",
                "cost_usd": 0.33,
                "story_run_id": "run-prev",
                "depends_on": [],
            }
        ],
    )

    merged_triage = StoryTriage(
        story_path="feature-a.md",
        action="skip_merged",
        reason="already merged in PR #1572, skipping",
        worktree_path=None,
        slug="feature-a",
    )
    full_triage = StoryTriage(
        story_path="feature-b.md",
        action="full",
        reason="no worktree found",
        worktree_path=None,
        slug="feature-b",
    )

    def triage_side_effect(spec_path, config, project_root, *, task=None):
        return merged_triage if "feature-a" in spec_path else full_triage

    fresh_result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect),
        patch(
            "theforge.sprint.runner.run_batch_preflight", return_value={}
        ) as mock_batch_preflight,
        patch("theforge.sprint.runner.run_task", return_value=fresh_result) as mock_run_task,
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        # reexec=True, resume defaults to False — the re-exec case.
        result = run_sprint(config, manifest_path, reexec=True)

    # (a) The merged story was excluded from the batch preflight input list.
    preflight_tasks = mock_batch_preflight.call_args.args[0]
    preflight_slugs = [t.slug for t in preflight_tasks]
    assert "feature-a" not in preflight_slugs
    assert preflight_slugs == ["feature-b"]

    # (b) The merged story was pre-marked complete in the DAG, so the scheduler
    # never dispatched it through WORKSPACE. Only the fresh story ran.
    dispatched_slugs = [c.args[1].slug for c in mock_run_task.call_args_list]
    assert "feature-a" not in dispatched_slugs
    assert dispatched_slugs == ["feature-b"]

    # (c) The prior DONE outcome + cost is preserved (not overwritten to
    # FAILED/$0). feature-a stays succeeded; total carries the prior $0.33.
    assert result.specs_succeeded == 2
    assert result.specs_failed == 0
    assert result.total_cost_usd == pytest.approx(1.33)


def test_reexec_without_prev_run_id_does_not_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check on the non-reexec/non-resume path: without reexec or resume
    the triage reconciliation never runs, so the story goes through preflight."""
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
    config = _make_config(tmp_path)

    fresh_result = _make_coordinator_result(success=True, cost=1.0)

    with (
        patch("theforge.sprint.runner._triage_spec") as mock_triage,
        patch(
            "theforge.sprint.runner.run_batch_preflight", return_value={}
        ) as mock_batch_preflight,
        patch("theforge.sprint.runner.run_task", return_value=fresh_result),
    ):
        run_sprint(config, manifest_path, reexec=False, resume=False)

    mock_triage.assert_not_called()
    preflight_slugs = [t.slug for t in mock_batch_preflight.call_args.args[0]]
    assert preflight_slugs == ["feature-a"]


def test_queued_pr_poll_records_landed_done_and_blocks_later_failed(
    tmp_path: Path,
) -> None:
    """When a queued-PR poll confirms a PR merged, the runner records the story
    DONE with landed=True (via _set_outcome), and the immutability marker then
    rejects a subsequent DONE->FAILED transition attempt.

    This drives the runner's real queued-PR poll (a story whose auto-merge is
    queued and later confirmed merged) and asserts the landed marker is set on
    the same SprintStoryState the runner uses, then verifies the guard rejects a
    spurious later FAILED — the exact overwrite this bug produced.
    """
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
    config = _make_config(tmp_path)

    # feature-a is approved but its landing is deferred (queued auto-merge), so
    # it enters queued_prs and is confirmed merged by the poll.
    result_a = _make_coordinator_result(
        success=True, cost=1.0, landing_status="pending_integration"
    )
    result_a.state.branch_name = "forge/feature-a"

    captured: dict = {}
    real_transition = SprintStoryState.transition

    def _spy_transition(self, slug, outcome=None, **fields):
        if slug == "feature-a" and coerce_outcome(outcome) is StoryOutcome.DONE:
            captured["landed"] = fields.get("landed")
            captured["state"] = self
        return real_transition(self, slug, outcome=outcome, **fields)

    with (
        patch("theforge.sprint.runner.run_task", return_value=result_a),
        patch(
            "theforge.coordinator.completion.land_story",
            return_value=(
                {"merge_queued": True, "pr_url": "https://github.com/o/r/pull/1572"},
                "pending_integration",
            ),
        ),
        patch(
            "theforge.sprint.runner._poll_queued_pr",
            return_value={"status": "merged"},
        ),
        patch.object(SprintStoryState, "transition", _spy_transition),
    ):
        result = run_sprint(config, manifest_path)

    # The queued-PR poll recorded a landed DONE for feature-a.
    assert captured.get("landed") is True
    assert result.specs_succeeded == 1

    # The marker makes that DONE immutable: a later spurious FAILED is rejected.
    state = captured["state"]
    state.transition("feature-a", outcome=StoryOutcome.FAILED)
    assert state.get("feature-a").outcome is StoryOutcome.DONE
