"""The approve message reports cross-cycle dev iterations, not per-cycle (#1983).

``state.dev_iteration`` aliases ``budget.cycle_count``, which ``reset_cycle()``
zeroes whenever a new review cycle opens (engine.py). Reporting it as a story
total under-counts every multi-cycle story on the success path the operator and
the sprint summary read. The cumulative value is ``budget.total_count``.

The seam under test is the DEV→REVIEW counter handoff across a cycle boundary,
so the test drives ``_run_review_phase`` through a real REQUEST_CHANGES →
RETRY_DEV → APPROVE sequence over a real git worktree, mirroring the engine's
own budget mutations (``budget.consume()`` per dev call at engine.py:450,
``budget.reset_cycle()`` on RETRY_DEV at engine.py:715).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.review_phase import _ReviewOutcome, _run_review_phase
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.review import ReviewFinding, ReviewResult


def _init_repo_with_dev_commit(path: Path) -> None:
    """A repo whose feature branch is ahead of base — the approve path's
    empty-diff guard refuses to mark DONE otherwise."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "forge/test-task"], cwd=path, check=True)
    (path / "src.py").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: implement"], cwd=path, check=True)


def _request_changes_review() -> ReviewResult:
    return ReviewResult(
        verdict="REQUEST_CHANGES",
        summary="needs changes",
        findings=[
            ReviewFinding(
                severity="P1",
                file="src.py",
                line=1,
                observed="Off by one",
                suggestion="Fix it",
            )
        ],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _approve_review() -> ReviewResult:
    return ReviewResult(
        verdict="APPROVE",
        summary="looks good",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _pool_returning(review: ReviewResult):
    return lambda *_a, **_kw: ([], [], review, [review], [("review", review)])


def _drive_two_cycles_to_approve(tmp_path: Path, *, interactive: bool):
    """REQUEST_CHANGES (2 dev calls) → RETRY_DEV → APPROVE (1 dev call) → DONE.

    Returns ``(state, result)`` from the approving cycle.
    """
    _init_repo_with_dev_commit(tmp_path)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(log_dir=tmp_path / "logs")
    state.run_id = "abcd1234"
    state.budget.max_iterations = config.retry.max_dev_iterations

    # ── Cycle 1: two dev iterations, then REQUEST_CHANGES → back to DEV ──
    state.budget.consume(review_cycle=state.review_cycle)
    state.budget.consume(review_cycle=state.review_cycle)

    with patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=_pool_returning(_request_changes_review()),
    ):
        outcome, result, config = _run_review_phase(
            state,
            config,
            task,
            "# spec\n",
            tmp_path,
            "forge/test-task",
            task_start=0.0,
            interactive=interactive,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    assert outcome is _ReviewOutcome.RETRY_DEV
    assert result is None

    # Cycle boundary: the engine refills the per-cycle pool (engine.py:715).
    state.budget.reset_cycle()
    assert state.dev_iteration == 0

    # ── Cycle 2: one more dev iteration, then APPROVE → DONE ──
    state.budget.consume(review_cycle=state.review_cycle)

    with patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=_pool_returning(_approve_review()),
    ):
        outcome, result, config = _run_review_phase(
            state,
            config,
            task,
            "# spec\n",
            tmp_path,
            "forge/test-task",
            task_start=0.0,
            interactive=interactive,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    assert outcome is _ReviewOutcome.DONE
    assert result is not None and result.success is True

    # The per-cycle counter is 1 here; the story took 3 dev iterations.
    assert state.dev_iteration == 1
    assert state.budget.total_count == 3
    return state, result


def test_approve_message_reports_dev_iterations_across_all_cycles(tmp_path: Path) -> None:
    """2 dev calls in cycle 1 + 1 in cycle 2 → the message must say 3, not 1."""
    state, result = _drive_two_cycles_to_approve(tmp_path, interactive=False)

    assert state.phase is Phase.DONE
    # This message becomes the run's audit outcome.message.
    assert "Review approved after 2 cycle(s), 3 dev iteration(s)." in result.message


def test_interactive_approve_message_reports_dev_iterations_across_all_cycles(
    tmp_path: Path,
) -> None:
    """The human-review approve message has the same contract as the automatic one."""
    with patch(
        "theforge.coordinator.review_phase._human_review",
        return_value=("approve", None),
    ):
        state, result = _drive_two_cycles_to_approve(tmp_path, interactive=True)

    assert state.phase is Phase.DONE
    assert state.human_review_decision == "approve"
    assert "Human approved after 2 cycle(s), 3 dev iteration(s)." in result.message
