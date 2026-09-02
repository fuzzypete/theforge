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
from landing_evidence_test_helpers import publish_landed
from sprint_test_helpers import run_sprint_ctx, stub_resolved

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import audit_substrate
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
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


def _make_spec_file(
    tmp_path: Path, name: str, slug: str, depends_on: list[str] | None = None
) -> Path:
    spec = tmp_path / f"{slug}.md"
    deps_line = f"depends_on: {depends_on}\n" if depends_on else ""
    spec.write_text(
        f"---\nname: {name}\nslug: {slug}\n{deps_line}---\n# {name}\nDo the thing.",
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

    def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
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
        result = run_sprint_ctx(config, manifest_path, reexec=True)

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


def test_reexec_reconcile_prior_done_drop_ends_succeeded_and_preserves_cost(
    tmp_path: Path,
) -> None:
    """Issue #1838: a re-exec drop tagged ``reconciled-prior-generation-done``
    (as the CLI passes when a worktree matches a prior-generation DONE story)
    must end succeeded, be excluded from preflight + dispatch, and carry the
    prior generation's cost — never re-run as a fresh collision.

    Issue #2042 narrowed *which* succeeded outcome: the prior generation
    recorded DONE here, so the reconciled story reports DONE. It previously
    reported ALREADY_DONE, which told the operator a story that ran and landed
    a commit had done nothing.
    """
    from theforge.sprint.launch_guard import REASON_RECONCILE_PRIOR_DONE

    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)

    sprint_id = _set_sprint_id(tmp_path)
    _write_prior_sprint_audit(tmp_path, sprint_id, 0.33)
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

    def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
        return StoryTriage(
            story_path=spec_path,
            action="full",
            reason="x",
            worktree_path=None,
            slug=Path(spec_path).stem,
        )

    fresh_result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect),
        patch(
            "theforge.sprint.runner.run_batch_preflight", return_value={}
        ) as mock_batch_preflight,
        patch("theforge.sprint.runner.run_task", return_value=fresh_result) as mock_run_task,
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        result = run_sprint_ctx(
            config,
            manifest_path,
            reexec=True,
            dropped_slugs={"feature-a": REASON_RECONCILE_PRIOR_DONE},
        )

    # (a) The reconciled story is excluded from preflight and dispatch.
    preflight_slugs = [t.slug for t in mock_batch_preflight.call_args.args[0]]
    assert "feature-a" not in preflight_slugs
    dispatched_slugs = [c.args[1].slug for c in mock_run_task.call_args_list]
    assert "feature-a" not in dispatched_slugs
    assert dispatched_slugs == ["feature-b"]

    # (b) It ends succeeded and carries the prior cost.
    assert result.specs_succeeded == 2
    assert result.specs_failed == 0
    assert result.total_cost_usd == pytest.approx(1.33)

    # (c) The summary preserves the prior generation's DONE — not ALREADY_DONE,
    #     and not tagged as a no-op reconcile source (#2042).
    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
    )
    by_slug = {s["slug"]: s for s in summary["stories"]}
    assert by_slug["feature-a"]["outcome"] == "DONE"
    assert by_slug["feature-a"].get("outcome_source") is None


def test_reexec_reconcile_prior_done_renders_done_in_live_status(
    tmp_path: Path,
) -> None:
    """Issue #2042: the operator-facing row for a reconciled prior-DONE story.

    The reported symptom was read from ``forge status`` *while the sprint was
    still running*: a story showed DONE on completion, then flipped to
    ALREADY_DONE minutes later when a mid-sprint re-exec ("source updated after
    pull") re-initialised the live state file. This asserts the live row at
    exactly that moment — during the next story's dispatch — because that is the
    surface the operator consults to decide whether a re-run is needed.
    """
    from theforge.sprint.launch_guard import REASON_RECONCILE_PRIOR_DONE
    from theforge.sprint.status_reader import read_live_status

    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)

    sprint_id = _set_sprint_id(tmp_path)
    _write_prior_sprint_audit(tmp_path, sprint_id, 0.33)
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

    def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
        return StoryTriage(
            story_path=spec_path,
            action="full",
            reason="x",
            worktree_path=None,
            slug=Path(spec_path).stem,
        )

    observed: dict = {}

    def run_task_side_effect(*args, **kwargs):
        # Mid-sprint read, standing in for the operator's `forge status`.
        entries = read_live_status("run-live", tmp_path) or []
        for entry in entries:
            if entry.slug == "feature-a":
                observed["status"] = entry.status
                observed["detail"] = entry.detail
        return _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", side_effect=run_task_side_effect),
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        run_sprint_ctx(
            config,
            manifest_path,
            reexec=True,
            run_id="run-live",
            dropped_slugs={"feature-a": REASON_RECONCILE_PRIOR_DONE},
        )

    assert observed, "run_task never ran; live status was not sampled mid-sprint"
    assert observed["status"] == "done"
    # The regression: this read "ALREADY_DONE (...)" for a story that ran,
    # spent budget and landed a commit in the prior generation.
    assert observed["detail"] == "DONE"
    assert "ALREADY_DONE" not in observed["detail"]


def test_reexec_reconcile_preserves_prior_already_done_as_noop(
    tmp_path: Path,
) -> None:
    """A reconciled story whose prior generation was a genuine no-op keeps
    ALREADY_DONE and its ``reexec_reconcile`` source tag.

    The #2042 fix makes the prior terminal decide the reported outcome, so this
    pins the other half of that rule: no landed DONE evidence means the no-op
    classification survives, with the source tag that distinguishes it from a
    preflight short-circuit.
    """
    from theforge.sprint.launch_guard import REASON_RECONCILE_PRIOR_DONE

    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)

    sprint_id = _set_sprint_id(tmp_path)
    _write_prior_sprint_audit(tmp_path, sprint_id, 0.0)
    persist_accumulated_story_state(
        sprint_id,
        "Test Sprint",
        tmp_path,
        [
            {
                "canonical_ref": "feature-a.md",
                "slug": "feature-a",
                "path": "feature-a.md",
                "outcome": "ALREADY_DONE",
                "outcome_source": "preflight_verdict",
                "cost_usd": 0.0,
                "story_run_id": "run-prev",
                "depends_on": [],
            }
        ],
    )

    def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
        return StoryTriage(
            story_path=spec_path,
            action="full",
            reason="x",
            worktree_path=None,
            slug=Path(spec_path).stem,
        )

    fresh_result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=fresh_result),
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        result = run_sprint_ctx(
            config,
            manifest_path,
            reexec=True,
            dropped_slugs={"feature-a": REASON_RECONCILE_PRIOR_DONE},
        )

    # Still succeeded and still satisfies dependency gating (marked complete).
    assert result.specs_succeeded == 2
    assert result.specs_failed == 0

    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
    )
    by_slug = {s["slug"]: s for s in summary["stories"]}
    assert by_slug["feature-a"]["outcome"] == "ALREADY_DONE"


def test_reexec_reconcile_prior_done_satisfies_dependent_story(
    tmp_path: Path,
) -> None:
    """Issue #1838 (iter 1): a story that hard-depends on a reconciled
    prior-DONE slug must still run — the reconciled slug is a *met* dependency
    (marked complete in the DAG), not a skip that strands its dependents."""
    from theforge.sprint.launch_guard import REASON_RECONCILE_PRIOR_DONE

    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b", depends_on=["feature-a"])
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)

    sprint_id = _set_sprint_id(tmp_path)
    _write_prior_sprint_audit(tmp_path, sprint_id, 0.33)
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

    def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
        return StoryTriage(
            story_path=spec_path,
            action="full",
            reason="x",
            worktree_path=None,
            slug=Path(spec_path).stem,
        )

    fresh_result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=fresh_result) as mock_run_task,
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        result = run_sprint_ctx(
            config,
            manifest_path,
            reexec=True,
            dropped_slugs={"feature-a": REASON_RECONCILE_PRIOR_DONE},
        )

    # The dependent story ran (was NOT stranded/skipped as dependency-blocked)
    # because feature-a reconciled as a met dependency.
    dispatched_slugs = [c.args[1].slug for c in mock_run_task.call_args_list]
    assert dispatched_slugs == ["feature-b"]

    # Both stories succeed: feature-a ALREADY_DONE, feature-b freshly done.
    assert result.specs_succeeded == 2
    assert result.specs_failed == 0
    assert result.specs_skipped == 0


def test_reexec_stranded_drop_does_not_rerun_and_keeps_distinct_reason(
    tmp_path: Path,
) -> None:
    """Issue #1838: a re-exec drop tagged ``stranded-prior-generation-worktree``
    is a recoverable DROPPED (not re-run), and the distinct reason is retained on
    the summary entry so RCA/audit can tell it apart from a fresh collision."""
    from theforge.sprint.launch_guard import REASON_STRANDED_WORKTREE

    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)
    _set_sprint_id(tmp_path)

    def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
        return StoryTriage(
            story_path=spec_path,
            action="full",
            reason="x",
            worktree_path=None,
            slug=Path(spec_path).stem,
        )

    fresh_result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect),
        patch(
            "theforge.sprint.runner.run_batch_preflight", return_value={}
        ) as mock_batch_preflight,
        patch("theforge.sprint.runner.run_task", return_value=fresh_result) as mock_run_task,
    ):
        result = run_sprint_ctx(
            config,
            manifest_path,
            reexec=True,
            dropped_slugs={"feature-a": REASON_STRANDED_WORKTREE},
        )

    # Stranded story never re-runs and never consumes preflight budget.
    preflight_slugs = [t.slug for t in mock_batch_preflight.call_args.args[0]]
    assert "feature-a" not in preflight_slugs
    dispatched_slugs = [c.args[1].slug for c in mock_run_task.call_args_list]
    assert "feature-a" not in dispatched_slugs

    # It is DROPPED (recoverable failure), distinct from the reconciled success.
    assert result.specs_failed == 1
    assert result.specs_succeeded == 1

    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
    )
    by_slug = {s["slug"]: s for s in summary["stories"]}
    assert by_slug["feature-a"]["outcome"] == "DROPPED"
    assert by_slug["feature-a"]["drop_reason"] == REASON_STRANDED_WORKTREE


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
        run_sprint_ctx(config, manifest_path, reexec=False, resume=False)

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
        result = run_sprint_ctx(config, manifest_path)

    # The queued-PR poll recorded a landed DONE for feature-a.
    assert captured.get("landed") is True
    assert result.specs_succeeded == 1

    # The marker makes that DONE immutable: a later spurious FAILED is rejected.
    state = captured["state"]
    state.transition("feature-a", outcome=StoryOutcome.FAILED)
    assert state.get("feature-a").outcome is StoryOutcome.DONE


def test_mid_loop_queued_pr_poll_records_landed_done_immutable(
    tmp_path: Path,
) -> None:
    """The mid-loop queued-PR poll (``if not active and queued_prs``) records a
    confirmed-merged story DONE with landed=True.

    Setup: feature-b depends on feature-a; feature-a is approved with landing
    deferred (queued auto-merge). Because StoryDAG.ready() gates hard deps on
    _completed (set only by mark_complete), and a pending_integration story is
    only mark_skipped, feature-b is NOT ready while feature-a is queued. So the
    dependent-dispatch pre-check never consumes the queued PR — the mid-loop
    branch does, calling _set_outcome(feature-a, DONE, landed=True).

    feature-b succeeding is the proof feature-a was merged in the mid-loop (which
    unblocks the dependent); had it only been handled at wrap-up, feature-b would
    have been skipped for an unmet dependency.
    """
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    spec_b = tmp_path / "feature-b.md"
    spec_b.write_text(
        "---\nname: Feature B\nslug: feature-b\ndepends_on: feature-a\n---\n# Feature B\nGo.",
        encoding="utf-8",
    )
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)

    result_a = _make_coordinator_result(
        success=True, cost=1.0, landing_status="pending_integration"
    )
    result_a.state.branch_name = "forge/feature-a"
    result_b = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    def _run_task(cfg, task, **kwargs):
        return result_a if task.slug == "feature-a" else result_b

    captured: dict = {}
    real_transition = SprintStoryState.transition

    def _spy_transition(self, slug, outcome=None, **fields):
        if slug == "feature-a" and coerce_outcome(outcome) is StoryOutcome.DONE:
            if fields.get("landed"):
                captured["landed"] = True
                captured["state"] = self
        return real_transition(self, slug, outcome=outcome, **fields)

    with (
        patch("theforge.sprint.runner.run_task", side_effect=_run_task),
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
        result = run_sprint_ctx(config, manifest_path)

    # The mid-loop poll recorded a landed DONE for feature-a...
    assert captured.get("landed") is True
    # ...and unblocked the dependent, so both stories succeeded (proves mid-loop,
    # not wrap-up — a wrap-up-only merge would leave feature-b skipped).
    assert result.specs_succeeded == 2

    # The marker makes feature-a's DONE immutable against a later spurious FAILED.
    state = captured["state"]
    state.transition("feature-a", outcome=StoryOutcome.FAILED)
    assert state.get("feature-a").outcome is StoryOutcome.DONE


def test_cmd_sprint_detached_child_reexec_passes_reexec_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI-layer regression: on the real coordinator re-exec path the detached
    child inherits FORGE_DETACHED=1 and FORGE_PREV_RUN_ID, enters the
    is_detached_child() branch, and calls the REAL setup_detached_child() —
    which pops FORGE_PREV_RUN_ID. cmd_sprint must have captured the re-exec
    signal BEFORE that pop, so run_sprint still receives reexec=True.

    This drives cmd_sprint end-to-end (not run_sprint directly) so it catches the
    CLI-layer bug where _is_reexec() was read after setup_detached_child cleared
    the env var, zeroing the signal on exactly the production path this fixes.
    """
    import argparse

    from theforge import detach as _detach_mod
    from theforge.cli import sprint as sprint_cli

    manifest_path = _make_manifest(tmp_path, ["issue-1278.md"])
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: test\n", encoding="utf-8")

    fake_config = MagicMock()
    fake_config.project_root = tmp_path
    fake_config.project = "test"

    args = argparse.Namespace(
        manifest=str(manifest_path),
        milestone=None,
        label=None,
        issues=None,
        config=str(config_path),
        base_branch=None,
        name=None,
        fg=False,
        detach=False,
        dry_run=False,
        verbose=False,
        auto_merge=False,
        interactive=False,
        resume=False,
        no_pull=False,
        parallel=None,
        force=False,
        no_notify=True,
    )

    # Simulate the real coordinator re-exec: a detached child whose parent set
    # FORGE_PREV_RUN_ID just before os.execv (env survives execv).
    monkeypatch.setenv("FORGE_DETACHED", "1")
    monkeypatch.setenv("FORGE_DETACHED_RUN_ID", "child-run-id")
    monkeypatch.setenv("FORGE_PREV_RUN_ID", "prev-run-id")

    fake_result = MagicMock()
    fake_result.specs_failed = 0

    with (
        patch("theforge.cli.sprint.load_config", return_value=fake_config),
        patch("theforge.cli.sprint.apply_base_branch_override", return_value=fake_config),
        patch("theforge.cli.sprint._find_config", return_value=config_path),
        patch("theforge.cli.sprint.parse_manifest_slugs", return_value=["issue-1278"]),
        patch(
            "theforge.cli.sprint._acquire_launch_locks",
            return_value=([], None, {}),
        ),
        patch("theforge.cli.sprint.reacquire_story_locks_in_daemon", return_value=[]),
        patch("theforge.cli.sprint.release_story_locks"),
        patch("theforge.cli.sprint.run_sprint", return_value=fake_result) as mock_run_sprint,
        patch("theforge.sprint.runner.resolve_from_manifest", return_value=stub_resolved()),
        # Let the REAL setup_detached_child run so it actually pops
        # FORGE_PREV_RUN_ID — the exact behavior that used to zero the signal.
        patch.object(_detach_mod, "install_cleanup_handler"),
        patch.object(_detach_mod, "write_run_ended"),
        patch.object(_detach_mod, "remove_pid"),
    ):
        rc = sprint_cli.cmd_sprint(args)

    assert rc == 0
    # setup_detached_child popped FORGE_PREV_RUN_ID during the detach handoff...
    assert os.environ.get("FORGE_PREV_RUN_ID") is None
    # ...but cmd_sprint captured the signal first, so reexec reached run_sprint.
    mock_run_sprint.assert_called_once()
    assert mock_run_sprint.call_args.args[0].reexec is True


def test_reexec_landed_story_with_open_issue_is_not_redispatched(
    tmp_path: Path,
) -> None:
    """#2111: a landed story whose GitHub issue is deliberately held open must
    still triage as skip_merged and keep its recorded DONE outcome.

    This exercises the real ``_triage_spec`` (not a stubbed StoryTriage) so the
    whole chain is covered: merge evidence is drawn from the run's own audit
    trail rather than issue state, the story is pre-marked complete in the DAG,
    and the scheduler never re-enters it through WORKSPACE where a workspace
    failure could overwrite its outcome.
    """
    _make_spec_file(tmp_path, "Issue 2054", "issue-2054")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["issue-2054.md", "feature-b.md"])
    config = _make_config(tmp_path)

    sprint_id = _set_sprint_id(tmp_path)
    _write_prior_sprint_audit(tmp_path, sprint_id, 0.33)
    persist_accumulated_story_state(
        sprint_id,
        "Test Sprint",
        tmp_path,
        [
            {
                "canonical_ref": "issue-2054.md",
                "slug": "issue-2054",
                "path": "issue-2054.md",
                "outcome": "DONE",
                "cost_usd": 0.33,
                "story_run_id": "run-prev",
                "depends_on": [],
            }
        ],
    )

    # The run's own landed APPROVE record — the evidence it wrote when it merged.
    audit_substrate.seed_records(
        tmp_path,
        [
            {
                "run_id": "run-prev",
                "task": {"slug": "issue-2054"},
                "landing_status": "landed",
                "reviews": [{"verdict": "APPROVE"}],
            }
        ],
    )
    # ...and the landing assertion published when the merge was observed, which
    # is what the landed query reads since #2849.
    publish_landed(tmp_path, "run-prev", slug="issue-2054")

    def _mock_git(cmd, **kwargs):
        m = MagicMock()
        # Patching subprocess.run via the dag module replaces the attribute on
        # the shared stdlib module, so the runner's own probes land here too.
        # tmp_path is not a git checkout: keep it that way so no base-branch
        # pull is attempted.
        if cmd[:2] == ["git", "rev-parse"]:
            m.returncode = 128
            m.stdout = ""
            m.stderr = "fatal: not a git repository"
        # Branch is at base HEAD: 0 commits ahead, 0 behind — the ambiguous
        # same-tip state a squash merge plus rebase leaves behind.
        elif "log" in cmd:
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

    fresh_result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_git),
        # The symptom bug is held open pending verification after its fix landed.
        patch("theforge.sprint.dag._is_issue_closed", return_value=False),
        patch(
            "theforge.sprint.runner.run_batch_preflight", return_value={}
        ) as mock_batch_preflight,
        patch("theforge.sprint.runner.run_task", return_value=fresh_result) as mock_run_task,
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        result = run_sprint_ctx(config, manifest_path, reexec=True)

    preflight_slugs = [t.slug for t in mock_batch_preflight.call_args.args[0]]
    assert preflight_slugs == ["feature-b"]

    dispatched_slugs = [c.args[1].slug for c in mock_run_task.call_args_list]
    assert dispatched_slugs == ["feature-b"]

    assert result.specs_succeeded == 2
    assert result.specs_failed == 0
    assert result.total_cost_usd == pytest.approx(1.33)


def _accumulated_by_slug(tmp_path: Path, sprint_id: str) -> dict[str, dict]:
    """Read ``.forge/sprints/<id>/state.yaml`` — the durable per-story record."""
    state_path = tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml"
    data = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
    return {s["slug"]: s for s in data.get("stories", [])}


def _persist_prior_story(
    tmp_path: Path,
    sprint_id: str,
    *,
    outcome: str,
    cost_usd: float,
    outcome_source: str | None = None,
    story_run_id: str = "run-prev",
) -> dict:
    entry: dict = {
        "canonical_ref": "feature-a.md",
        "slug": "feature-a",
        "path": "feature-a.md",
        "outcome": outcome,
        "cost_usd": cost_usd,
        "story_run_id": story_run_id,
        "started_at": "2026-08-02T02:32:00Z",
        "finished_at": "2026-08-02T02:51:46Z",
        "depends_on": [],
    }
    if outcome_source is not None:
        entry["outcome_source"] = outcome_source
    persist_accumulated_story_state(sprint_id, "Test Sprint", tmp_path, [entry])
    return entry


def _skip_merged_triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
    if "feature-a" in spec_path:
        return StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged in PR #2146, skipping",
            worktree_path=None,
            slug="feature-a",
        )
    return StoryTriage(
        story_path="feature-b.md",
        action="full",
        reason="no worktree found",
        worktree_path=None,
        slug="feature-b",
    )


def test_reexec_skip_merged_keeps_prior_done_in_accumulated_state(
    tmp_path: Path,
) -> None:
    """Issue #2150: a story this run carried to DONE, whose queued PR merged
    around the re-exec boundary, must keep DONE in the durable sprint state.

    The post-re-exec triage sees the branch as merged and returns
    ``skip_merged``. That is a factually correct statement about git *now* — it
    is not evidence that the work pre-dated this run. The prior generation's own
    record (DONE, $11.93, this run's story_run_id) is, so the row must survive
    intact rather than being restamped ALREADY_DONE / resume_skip_merged while
    still reporting the spend that contradicts it.
    """
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=100.0)
    config = _make_config(tmp_path)

    sprint_id = _set_sprint_id(tmp_path)
    _write_prior_sprint_audit(tmp_path, sprint_id, 11.93)
    prior = _persist_prior_story(tmp_path, sprint_id, outcome="DONE", cost_usd=11.93)

    fresh_result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_skip_merged_triage_side_effect),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=fresh_result),
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        result = run_sprint_ctx(config, manifest_path, reexec=True)

    accumulated = _accumulated_by_slug(tmp_path, sprint_id)["feature-a"]
    assert accumulated["outcome"] == "DONE"
    assert accumulated.get("outcome_source") is None
    # Outcome, cost and elapsed agree: the same run owns all three.
    assert accumulated["cost_usd"] == pytest.approx(11.93)
    assert accumulated["story_run_id"] == prior["story_run_id"]
    assert accumulated["started_at"] == prior["started_at"]
    assert accumulated["finished_at"] == prior["finished_at"]

    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
    )
    by_slug = {s["slug"]: s for s in summary["stories"]}
    assert by_slug["feature-a"]["outcome"] == "DONE"
    assert by_slug["feature-a"].get("outcome_source") is None
    assert result.specs_succeeded == 2
    assert result.specs_failed == 0


def test_reexec_skip_merged_renders_done_in_live_status(tmp_path: Path) -> None:
    """Issue #2150, operator surface: ``forge status`` mid-sprint.

    The symptom was read from the live status file — five stories showing
    ``ALREADY_DONE (merged)`` alongside the non-zero cost the same run had just
    spent on them. This samples that row during the next story's dispatch,
    which is exactly when the operator consults it.
    """
    from theforge.sprint.status_reader import read_live_status

    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=100.0)
    config = _make_config(tmp_path)

    sprint_id = _set_sprint_id(tmp_path)
    _write_prior_sprint_audit(tmp_path, sprint_id, 11.93)
    _persist_prior_story(tmp_path, sprint_id, outcome="DONE", cost_usd=11.93)

    observed: dict = {}

    def run_task_side_effect(*args, **kwargs):
        for entry in read_live_status("run-live", tmp_path) or []:
            if entry.slug == "feature-a":
                observed["status"] = entry.status
                observed["detail"] = entry.detail
                observed["cost_usd"] = entry.cost_usd
        return _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_skip_merged_triage_side_effect),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", side_effect=run_task_side_effect),
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        run_sprint_ctx(config, manifest_path, reexec=True, run_id="run-live")

    assert observed, "run_task never ran; live status was not sampled mid-sprint"
    assert observed["status"] == "done"
    assert observed["detail"] == "DONE"
    assert "ALREADY_DONE" not in observed["detail"]
    # The row that reports the spend must be the row that claims the work.
    assert observed["cost_usd"] == pytest.approx(11.93)


def test_reexec_skip_merged_preserves_prior_already_done_as_noop(
    tmp_path: Path,
) -> None:
    """The other half of the #2150 rule: a prior generation that really was a
    no-op keeps ALREADY_DONE and its ``resume_skip_merged`` source tag.

    The fix lets the recorded execution decide the label, so this pins that it
    only ever *preserves* a record — a prior entry with no run behind it (a
    preflight short-circuit, $0, no elapsed) is still classified as
    pre-existing merged work.
    """
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=100.0)
    config = _make_config(tmp_path)

    sprint_id = _set_sprint_id(tmp_path)
    _write_prior_sprint_audit(tmp_path, sprint_id, 0.0)
    _persist_prior_story(
        tmp_path,
        sprint_id,
        outcome="ALREADY_DONE",
        cost_usd=0.0,
        outcome_source="preflight_verdict",
    )

    fresh_result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_skip_merged_triage_side_effect),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=fresh_result),
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        result = run_sprint_ctx(config, manifest_path, reexec=True)

    accumulated = _accumulated_by_slug(tmp_path, sprint_id)["feature-a"]
    assert accumulated["outcome"] == "ALREADY_DONE"
    assert accumulated["outcome_source"] == "resume_skip_merged"
    assert result.specs_failed == 0


def test_resume_skip_merged_keeps_prior_done_in_accumulated_state(
    tmp_path: Path,
) -> None:
    """The same rule on the explicit ``--resume`` path, not just re-exec.

    ``resume=True`` takes a second, distinct write of the accumulated state
    (the resume-time persistence block), so the DONE-preserving rule is pinned
    on both entry points rather than only the one the incident happened to hit.
    """
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=100.0)
    config = _make_config(tmp_path)

    sprint_id = _set_sprint_id(tmp_path)
    _write_prior_sprint_audit(tmp_path, sprint_id, 11.93)
    _persist_prior_story(tmp_path, sprint_id, outcome="DONE", cost_usd=11.93)

    fresh_result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_skip_merged_triage_side_effect),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=fresh_result),
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        run_sprint_ctx(config, manifest_path, resume=True)

    accumulated = _accumulated_by_slug(tmp_path, sprint_id)["feature-a"]
    assert accumulated["outcome"] == "DONE"
    assert accumulated.get("outcome_source") is None
    assert accumulated["cost_usd"] == pytest.approx(11.93)
