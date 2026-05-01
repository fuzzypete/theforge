"""Invariants for the canonical sprint story state.

Validates the three guarantees that make ``SprintStoryState`` the single
source of truth for sprint story state:

1. Every story has exactly one entry, keyed by slug.
2. Outcome transitions are monotonic — once a story enters a terminal
   outcome it never returns to a non-terminal outcome (terminal-to-terminal
   corrections such as DONE -> FAILED are permitted because the story stays
   in the terminal set).
3. ``counts()``, ``as_dict()``, and ``stories()`` all project from the same
   in-memory map, so totals cannot drift between projections.
"""

from __future__ import annotations

from theforge.sprint.story_state import (
    SprintStoryState,
    StoryOutcome,
    coerce_outcome,
)


def test_register_assigns_one_entry_per_slug() -> None:
    state = SprintStoryState()
    state.register("a", "Issue #1")
    state.register("a", "Issue #1 (re-registered)")
    assert len(state.stories()) == 1
    entry = state.get("a")
    assert entry is not None
    assert entry.path == "Issue #1 (re-registered)"


def test_register_does_not_downgrade_terminal() -> None:
    state = SprintStoryState()
    state.register("a", "p", outcome=StoryOutcome.DONE)
    # Re-registering with WAITING must not unwind a terminal outcome.
    state.register("a", "p", outcome=StoryOutcome.WAITING)
    assert state.get("a").outcome is StoryOutcome.DONE


def test_transition_monotonic_terminal_to_non_terminal_blocked() -> None:
    state = SprintStoryState()
    state.register("a", "p", outcome=StoryOutcome.RUNNING)
    state.transition("a", outcome=StoryOutcome.DONE)
    state.transition("a", outcome=StoryOutcome.RUNNING)
    assert state.get("a").outcome is StoryOutcome.DONE


def test_transition_terminal_to_terminal_correction_allowed() -> None:
    """Optimistic DONE corrected to FAILED when a queued PR fails to land."""
    state = SprintStoryState()
    state.register("a", "p", outcome=StoryOutcome.DONE)
    state.transition("a", outcome=StoryOutcome.FAILED)
    assert state.get("a").outcome is StoryOutcome.FAILED


def test_counts_project_from_same_map_as_stories() -> None:
    state = SprintStoryState()
    state.register("a", "pa", outcome=StoryOutcome.DONE)
    state.register("b", "pb", outcome=StoryOutcome.FAILED)
    state.register("c", "pc", outcome=StoryOutcome.SKIPPED)
    state.register("d", "pd", outcome=StoryOutcome.RUNNING)
    state.register("e", "pe", outcome=StoryOutcome.ALREADY_DONE)
    counts = state.counts()
    assert counts == {"total": 5, "succeeded": 2, "failed": 1, "skipped": 1}
    # The same map drives stories() and as_dict()
    assert len(state.stories()) == counts["total"]
    assert len(state.as_dict()) == counts["total"]


def test_round_trip_via_as_dict_from_dict() -> None:
    state = SprintStoryState()
    state.register("a", "Issue #1", outcome=StoryOutcome.DONE, cost_usd=1.25)
    state.register("b", "Issue #2", outcome=StoryOutcome.SKIPPED, reason="needs-grooming")
    serialized = state.as_dict()
    restored = SprintStoryState.from_dict(serialized)
    assert restored.counts() == state.counts()
    assert restored.get("b").reason == "needs-grooming"


def test_coerce_outcome_handles_legacy_status_strings() -> None:
    assert coerce_outcome("done") is StoryOutcome.DONE
    assert coerce_outcome("FAILED") is StoryOutcome.FAILED
    assert coerce_outcome("preserved") is StoryOutcome.PRESERVED
    assert coerce_outcome("DROPPED") is StoryOutcome.DROPPED
    assert coerce_outcome("unknown") is StoryOutcome.WAITING
    assert coerce_outcome(None) is StoryOutcome.WAITING


def test_outcome_terminal_classification() -> None:
    assert StoryOutcome.WAITING.is_terminal is False
    assert StoryOutcome.RUNNING.is_terminal is False
    assert StoryOutcome.DONE.is_terminal is True
    assert StoryOutcome.DONE.is_succeeded is True
    assert StoryOutcome.ALREADY_DONE.is_succeeded is True
    assert StoryOutcome.FAILED.is_failed is True
    assert StoryOutcome.ESCALATED.is_failed is True
    assert StoryOutcome.SKIPPED.is_skipped is True
    assert StoryOutcome.PRESERVED.is_skipped is True
    # DROPPED stays in the failed bucket — operator surfaces (sprint-status
    # and sprint-audit) all treat launch-guard drops as failures.
    assert StoryOutcome.DROPPED.is_failed is True
    assert StoryOutcome.DROPPED.is_skipped is False


def test_transition_unknown_slug_returns_none() -> None:
    state = SprintStoryState()
    assert state.transition("missing", outcome=StoryOutcome.DONE) is None


def test_as_dict_overwrites_stale_final_outcome_on_terminal_transition() -> None:
    """A re-run story that flips DONE → ESCALATE must not surface stale DONE detail."""
    state = SprintStoryState()
    state.register(
        "a",
        "Issue #1",
        outcome=StoryOutcome.DONE,
        detail={
            "final_outcome": "DONE",
            "review_verdict": "APPROVE — all ACs met",
            "review_p1": 0,
            "review_p2": 0,
        },
    )
    state.transition("a", outcome=StoryOutcome.ESCALATED)
    serialized = state.as_dict()[0]
    assert serialized["detail"]["final_outcome"] == "ESCALATED"
    # Stale review summary must not be carried forward to a non-success outcome.
    assert "review_verdict" not in serialized["detail"]
    assert "review_p1" not in serialized["detail"]
    assert "review_p2" not in serialized["detail"]


def test_as_dict_preserves_review_fields_for_success_outcomes() -> None:
    state = SprintStoryState()
    state.register(
        "a",
        "Issue #1",
        outcome=StoryOutcome.DONE,
        detail={"review_verdict": "APPROVE", "review_p1": 0, "review_p2": 1},
    )
    serialized = state.as_dict()[0]
    assert serialized["detail"]["review_verdict"] == "APPROVE"
    assert serialized["detail"]["review_p1"] == 0
    assert serialized["detail"]["review_p2"] == 1
