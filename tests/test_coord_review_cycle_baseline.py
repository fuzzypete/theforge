"""A story's first-ever reviewer verdict must be classified as a first verdict,
even when VALIDATE has already advanced ``state.review_cycle`` for a
gate/convention finding before any reviewer ran (#1987).

``state.review_cycle`` is shared by two independent writers: engine.py's
VALIDATE-phase RETRY_DEV_NEW_CYCLE handling (opened for a gate/convention
finding, no reviewer involved) and review_phase.py's real-reviewer-verdict
path. Classification must key on a counter that only real reviewer verdicts
advance (``state.trajectory_cycle``), not on ``review_cycle``.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

from coord_test_helpers import _make_config, _make_task

from theforge.config import FindingClassifierConfig
from theforge.coordinator.review_phase import _ReviewOutcome, _run_review_phase
from theforge.coordinator.state import CoordinatorState
from theforge.review import ReviewFinding, ReviewResult


def _init_repo_with_dev_commit(path: Path) -> None:
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


def _request_changes_review_with_p1() -> ReviewResult:
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


def _pool_returning(review: ReviewResult):
    return lambda *_a, **_kw: ([], [], review, [review], [("review", review)])


class TestFirstVerdictAfterValidateOpenedCycle:
    """VALIDATE opening a review cycle before any reviewer runs must not make
    the story's first real reviewer verdict classify as cycle 2+."""

    def test_first_reviewer_p1_blocks_even_after_validate_bumped_review_cycle(self, tmp_path):
        _init_repo_with_dev_commit(tmp_path)

        config = _make_config(tmp_path)
        # allow_net_new_bypass=True isolates the classification branch: a true
        # cycle-1 verdict blocks on any P1 regardless of this flag, while a
        # cycle-2+ (baseline) verdict would let a net_new P1 bypass under it.
        # A high cycle cap keeps the assertion about classification, not about
        # cycle exhaustion.
        config = dataclasses.replace(
            config,
            finding_classifier=FindingClassifierConfig(allow_net_new_bypass=True),
            retry=dataclasses.replace(config.retry, max_review_cycles=5),
        )
        task = _make_task(tmp_path)

        state = CoordinatorState(log_dir=tmp_path / "logs")
        state.run_id = "abcd1234"
        state.budget.max_iterations = config.retry.max_dev_iterations

        # Simulate VALIDATE having already opened one review cycle for a
        # gate/convention finding (engine.py:561) before any reviewer ran.
        state.review_cycle = 1
        state.validate_opened_review_cycles = 1
        # trajectory_cycle is untouched by VALIDATE — it is still 0, correctly
        # reflecting that zero real reviewer verdicts have happened yet.
        assert state.trajectory_cycle == 0

        with patch_review_pool(_request_changes_review_with_p1()):
            outcome, result, _config = _run_review_phase(
                state,
                config,
                task,
                "# spec\n",
                tmp_path,
                "forge/test-task",
                task_start=0.0,
                interactive=False,
                auto_merge=False,
                notify=False,
                logger=None,
            )

        # This is the story's first-ever reviewer verdict. It must be treated
        # as cycle 1 (no baseline exists yet): the P1 blocks unconditionally,
        # so the coordinator loops back to DEV rather than accepting the
        # net_new bypass a real cycle-2+ verdict would be eligible for.
        assert outcome is _ReviewOutcome.RETRY_DEV
        assert result is None
        assert state.trajectory_cycle == 1


def patch_review_pool(review: ReviewResult):
    from unittest.mock import patch

    return patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=_pool_returning(review),
    )
