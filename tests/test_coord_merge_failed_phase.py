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
