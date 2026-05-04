"""Round-trip tests for the MERGE_FAILED terminal state.

Covers AC: a coordinator-state with merge-step failure produces audit fields with
no contradictions, and the status renderer maps those fields to a non-contradictory
row (STATUS=failed, PHASE=MERGE_FAILED, COST reflects spend, DETAIL describes
the merge failure rather than a stale review APPROVE summary).
"""

from __future__ import annotations

from pathlib import Path

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
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.completion import mark_merge_failed
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.status_reader import (
    _outcome_to_status,
    _terminal_phase,
    read_completed_status,
)
from theforge.sprint.story_state import StoryOutcome
from theforge.task import TaskStory


def _config(tmp_path: Path) -> ForgeConfig:
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


def _task() -> TaskStory:
    return TaskStory(name="story-x", slug="story-x", story_path="specs/x.md")


def _approved_pending_result() -> CoordinatorResult:
    """Mimic _finalize_approve output: phase=DONE, landing pending."""
    state = CoordinatorState()
    state.phase = Phase.DONE
    return CoordinatorResult(
        success=True,
        phase=Phase.DONE,
        state=state,
        message="Task 'story-x' completed. Branch: forge/story-x",
        merge={"action": "merge", "pending": True},
        landing_status="pending_integration",
    )


# ── mark_merge_failed: coherence by construction ─────────────────────────


def test_mark_merge_failed_mutates_state_and_result_coherently() -> None:
    result = _approved_pending_result()
    mark_merge_failed(result.state, result, "embedded null byte", "forge/story-x")

    assert result.phase is Phase.MERGE_FAILED
    assert result.state.phase is Phase.MERGE_FAILED
    assert result.success is False
    assert result.landing_status == "failed"
    assert "Merge failed" in result.message
    assert "embedded null byte" in result.message
    assert "completed" not in result.message


# ── audit field mutual consistency ───────────────────────────────────────


def test_audit_final_phase_is_merge_failed_not_done(tmp_path: Path) -> None:
    """final_phase must reflect MERGE_FAILED, never DONE, when landing failed."""
    result = _approved_pending_result()
    mark_merge_failed(result.state, result, "embedded null byte", "forge/story-x")

    audit = generate_audit_log(_config(tmp_path), _task(), result)

    assert audit["outcome"]["final_phase"] == "MERGE_FAILED"
    assert audit["outcome"]["final_phase"] != "DONE"
    assert audit["outcome"]["success"] is False
    assert audit["landing_status"] == "failed"
    assert audit["landing_event"]["landing_status"] == "failed"
    # Mutual consistency: final_phase is a failure state and message names the cause.
    assert "completed" not in audit["outcome"]["message"]
    assert "Merge failed" in audit["outcome"]["message"]


# ── status renderer: failed row with merge cause, not stale APPROVE ─────


def test_status_renderer_round_trip_for_merge_failed(tmp_path: Path) -> None:
    """A sprint summary entry with outcome=MERGE_FAILED renders as
    STATUS=failed, PHASE=MERGE_FAILED, COST=actual spend, DETAIL=merge cause
    (not the stale APPROVE summary from the review step)."""
    sprint_dir = tmp_path / ".forge" / "logs" / "sprint-test"
    sprint_dir.mkdir(parents=True)
    summary = {
        "sprint": {"run_id": "abc"},
        "stories": [
            {
                "slug": "story-x",
                "path": "Issue #1179",
                "outcome": "MERGE_FAILED",
                "cost_usd": 4.27,
                "verdict": "APPROVE",
            }
        ],
    }
    summary_path = sprint_dir / "sprint-summary.yaml"
    summary_path.write_text(yaml.safe_dump(summary), encoding="utf-8")

    story_dir = sprint_dir / "story-x"
    story_dir.mkdir()
    audit_yaml = {
        "outcome": {
            "final_phase": "MERGE_FAILED",
            "success": False,
            "message": (
                "Merge failed: embedded null byte. Branch 'forge/story-x' carries reviewed work."
            ),
        },
        "landing_status": "failed",
        "error": "embedded null byte",
        "reviews": [
            {
                "verdict": "APPROVE",
                "summary": "All ACs are met; ready to land.",
                "findings_by_severity": {"P1": 0, "P2": 0},
            }
        ],
        "preflight": {"complexity": "large"},
    }
    (story_dir / "audit.yaml").write_text(yaml.safe_dump(audit_yaml), encoding="utf-8")

    entries = read_completed_status(summary_path)
    assert len(entries) == 1
    row = entries[0]

    assert row.status == "failed"
    assert row.phase == "MERGE_FAILED"
    assert row.cost_usd == 4.27
    # DETAIL must describe the merge failure, not surface the APPROVE summary.
    assert "Merge failed" in row.detail
    assert "embedded null byte" in row.detail
    assert "All ACs are met" not in row.detail
    # If the review verdict is preserved at all, it must be labeled, not bare.
    if "APPROVE" in row.detail:
        assert "review verdict" in row.detail


def test_outcome_to_status_maps_merge_failed_to_failed() -> None:
    assert _outcome_to_status("MERGE_FAILED") == "failed"


def test_terminal_phase_returns_merge_failed() -> None:
    """PHASE column comes from the outcome string — must surface MERGE_FAILED,
    not DROPPED (which is reserved for stories that did not run)."""
    assert _terminal_phase("MERGE_FAILED", []) == "MERGE_FAILED"
    # DROPPED stays distinct.
    assert _terminal_phase("DROPPED", []) == "DROPPED"


# ── canonical outcome semantics ─────────────────────────────────────────


def test_story_outcome_merge_failed_is_terminal_failed_not_skipped() -> None:
    assert StoryOutcome.MERGE_FAILED.is_terminal
    assert StoryOutcome.MERGE_FAILED.is_failed
    assert not StoryOutcome.MERGE_FAILED.is_succeeded
    assert not StoryOutcome.MERGE_FAILED.is_skipped


def test_dropped_remains_distinct_from_merge_failed() -> None:
    """DROPPED is reserved for stories that did not execute. MERGE_FAILED is a
    distinct state for stories that ran but failed at the merge step."""
    assert StoryOutcome.DROPPED is not StoryOutcome.MERGE_FAILED
    assert StoryOutcome.DROPPED.value != StoryOutcome.MERGE_FAILED.value


# ── MERGE_ARMING_FAILED: distinct from MERGE_FAILED ─────────────────────


def test_merge_arming_failed_is_distinct_terminal_failed_outcome() -> None:
    """MERGE_ARMING_FAILED is its own terminal failed outcome — separate enum
    value, separate name, but same failure-bucket semantics."""
    assert StoryOutcome.MERGE_ARMING_FAILED is not StoryOutcome.MERGE_FAILED
    assert StoryOutcome.MERGE_ARMING_FAILED.value != StoryOutcome.MERGE_FAILED.value
    assert StoryOutcome.MERGE_ARMING_FAILED.is_terminal
    assert StoryOutcome.MERGE_ARMING_FAILED.is_failed
    assert not StoryOutcome.MERGE_ARMING_FAILED.is_succeeded


def test_mark_merge_failed_arming_message_names_arming_distinction() -> None:
    """When arming_failed=True, the result message must name the arming
    distinction and point at branch-protection remediation, not at the PR."""
    result = _approved_pending_result()
    mark_merge_failed(
        result.state,
        result,
        "enablePullRequestAutoMerge: Protected branch rules not configured",
        "forge/story-x",
        arming_failed=True,
    )

    assert result.phase is Phase.MERGE_FAILED
    assert result.success is False
    assert result.landing_status == "failed"
    assert "Auto-merge arming failed" in result.message
    assert "branch protection" in result.message.lower()
    # Must NOT use the generic "story is recoverable" phrasing reserved for
    # genuine rejections — operators should pursue config, not PR investigation.
    assert "Story is recoverable but unmerged" not in result.message


def test_outcome_to_status_maps_merge_arming_failed_to_failed() -> None:
    assert _outcome_to_status("MERGE_ARMING_FAILED") == "failed"


def test_terminal_phase_returns_merge_arming_failed() -> None:
    """STAGE column must surface MERGE_ARMING_FAILED distinctly from
    MERGE_FAILED so operators can choose the right next action."""
    assert _terminal_phase("MERGE_ARMING_FAILED", []) == "MERGE_ARMING_FAILED"
    assert _terminal_phase("MERGE_FAILED", []) == "MERGE_FAILED"


def test_status_renderer_distinguishes_arming_from_rejection(tmp_path: Path) -> None:
    """sprint-summary outcome=MERGE_ARMING_FAILED renders as STATUS=failed,
    PHASE=MERGE_ARMING_FAILED with a detail naming the arming distinction —
    distinct from a MERGE_FAILED row whose detail describes a real PR rejection."""
    sprint_dir = tmp_path / ".forge" / "logs" / "sprint-test"
    sprint_dir.mkdir(parents=True)
    summary = {
        "sprint": {"run_id": "abc"},
        "stories": [
            {
                "slug": "story-arming",
                "path": "Issue #1357",
                "outcome": "MERGE_ARMING_FAILED",
                "cost_usd": 4.27,
                "verdict": "APPROVE",
            },
            {
                "slug": "story-rejected",
                "path": "Issue #1358",
                "outcome": "MERGE_FAILED",
                "cost_usd": 4.27,
                "verdict": "APPROVE",
            },
        ],
    }
    summary_path = sprint_dir / "sprint-summary.yaml"
    summary_path.write_text(yaml.safe_dump(summary), encoding="utf-8")

    arming_dir = sprint_dir / "story-arming"
    arming_dir.mkdir()
    (arming_dir / "audit.yaml").write_text(
        yaml.safe_dump(
            {
                "outcome": {
                    "final_phase": "MERGE_FAILED",
                    "success": False,
                    "message": (
                        "Auto-merge arming failed: enablePullRequestAutoMerge. "
                        "Configure branch protection on the target branch or merge "
                        "manually with `gh pr merge <PR> --squash`."
                    ),
                },
                "landing_status": "failed",
                "error": "enablePullRequestAutoMerge: Protected branch rules not configured",
            }
        ),
        encoding="utf-8",
    )

    rejected_dir = sprint_dir / "story-rejected"
    rejected_dir.mkdir()
    (rejected_dir / "audit.yaml").write_text(
        yaml.safe_dump(
            {
                "outcome": {
                    "final_phase": "MERGE_FAILED",
                    "success": False,
                    "message": "Merge failed: required CI check failed.",
                },
                "landing_status": "failed",
                "error": "required CI check failed",
            }
        ),
        encoding="utf-8",
    )

    entries = read_completed_status(summary_path)
    assert len(entries) == 2
    by_slug = {row.slug: row for row in entries}

    arming_row = by_slug["story-arming"]
    rejected_row = by_slug["story-rejected"]

    assert arming_row.status == "failed"
    assert rejected_row.status == "failed"

    # PHASE column must distinguish the two failure modes.
    assert arming_row.phase == "MERGE_ARMING_FAILED"
    assert rejected_row.phase == "MERGE_FAILED"

    # DETAIL column must surface the operator-actionable distinction.
    assert "arming" in arming_row.detail.lower()
    assert "branch protection" in arming_row.detail.lower()
    assert "arming" not in rejected_row.detail.lower()


# ── runner-level seam: outcome assignment from merge_info.arming_failed ──


def test_runner_classify_records_merge_arming_failed_when_flag_set() -> None:
    """runner._classify_and_record reads merge_info.arming_failed from the
    coordinator result and assigns MERGE_ARMING_FAILED instead of MERGE_FAILED.
    This is the seam test that covers the path from coordinator merge_info →
    sprint outcome → sprint-summary serialization."""
    from theforge.sprint.dag import StoryDAG
    from theforge.sprint.runner import _classify_and_record
    from theforge.sprint.story_state import SprintStoryState
    from theforge.task import TaskStory as _TaskStory

    task = _TaskStory(name="story-arming", slug="story-arming", story_path="specs/x.md")
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    state.phase = Phase.MERGE_FAILED

    arming_result = CoordinatorResult(
        success=False,
        phase=Phase.MERGE_FAILED,
        state=state,
        message="Auto-merge arming failed.",
        merge={"action": "merge-pr", "merged": False, "arming_failed": True},
        landing_status="failed",
    )

    rejected_state = CoordinatorState()
    rejected_state.preflight_verdict = "PROCEED"
    rejected_state.phase = Phase.MERGE_FAILED
    rejected_result = CoordinatorResult(
        success=False,
        phase=Phase.MERGE_FAILED,
        state=rejected_state,
        message="Merge failed: required CI check failed.",
        merge={"action": "merge-pr", "merged": False, "arming_failed": False},
        landing_status="failed",
    )

    story_state = SprintStoryState()
    story_state.register("story-arming", "specs/x.md")
    story_state.register("story-rejected", "specs/y.md")

    dag = StoryDAG([task, _TaskStory(name="story-rejected", slug="story-rejected")])

    arming_outcome = _classify_and_record(task, arming_result, dag, set(), story_state=story_state)
    rejected_outcome = _classify_and_record(
        _TaskStory(name="story-rejected", slug="story-rejected"),
        rejected_result,
        dag,
        set(),
        story_state=story_state,
    )

    assert arming_outcome == StoryOutcome.MERGE_ARMING_FAILED
    assert rejected_outcome == StoryOutcome.MERGE_FAILED
    # The sprint-summary serialization derives from .name on the canonical
    # outcome (audit.py:816), so the two failure modes produce distinct strings.
    assert arming_outcome.name == "MERGE_ARMING_FAILED"
    assert rejected_outcome.name == "MERGE_FAILED"
