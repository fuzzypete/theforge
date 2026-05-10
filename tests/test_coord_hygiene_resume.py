"""Seam-level integration tests for hygiene-escalation → resume → consensus replay.

Covers issue #1499: when the REVIEW phase escalates because a non-DEV phase
mutated the worktree, but the reviewer pool had already produced an APPROVE
consensus for the current dev commit, a subsequent ``forge sprint --resume``
must replay that consensus instead of re-running the reviewer pool against
an unchanged dev commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.review_phase import _ReviewOutcome, _run_review_phase
from theforge.coordinator.run_setup import load_trajectory_state
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.review import ReviewResult


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / ".gitignore").write_text(".forge/\n", encoding="utf-8")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return sha


def _approve_review() -> ReviewResult:
    return ReviewResult(
        verdict="APPROVE",
        summary="ok",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={"verdict": "APPROVE"},
    )


def _stub_review_pool_with_hygiene_mutation(workspace: Path):
    """Return a side_effect that simulates a reviewer pool producing APPROVE consensus
    while leaving a stray scratch file behind (workspace hygiene violation)."""

    def _side_effect(*args, **kwargs):
        # Inject a workspace mutation that the hygiene check will catch.
        (workspace / "reviewer_scratch.txt").write_text("oops\n", encoding="utf-8")
        candidate = _approve_review()
        named = [("reviewer_a", candidate)]
        return ([], [], candidate, [candidate], named)

    return _side_effect


def _run_first_pass(workspace: Path, config, task) -> CoordinatorState:
    """Drive _run_review_phase to the hygiene-escalation branch.

    Returns the resulting state with hygiene-escalation fields populated.
    """
    state = CoordinatorState(
        phase=Phase.REVIEW,
        dev_iteration=0,
        review_cycle=0,
        preflight_verdict="SKIPPED",
    )
    state.workspace_path = workspace
    state.branch_name = "main"
    state.run_id = "test-run-1"
    state.adaptive_review_max = 2

    with patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=_stub_review_pool_with_hygiene_mutation(workspace),
    ):
        outcome, result, _config2 = _run_review_phase(
            state,
            config,
            task,
            "story",
            workspace,
            "main",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    assert outcome == _ReviewOutcome.ESCALATE
    assert result is not None
    return state


def test_hygiene_escalation_captures_prior_consensus_and_persists_to_sidecar(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    head_sha = _init_repo(workspace)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    state = _run_first_pass(workspace, config, task)

    assert state.escalate_kind == "hygiene"
    assert state.hygiene_escalation_dev_commit_sha == head_sha
    assert state.hygiene_escalation_prior_review is not None
    assert state.hygiene_escalation_prior_review.verdict == "APPROVE"
    assert state.hygiene_escalation_prior_approve_count == 1
    assert state.hygiene_escalation_total_count == 1

    # Sidecar contains the same data round-tripped from disk.
    sidecar = workspace / ".forge" / "trajectory.yaml"
    assert sidecar.exists()
    fresh = CoordinatorState(
        phase=Phase.REVIEW,
        dev_iteration=0,
        review_cycle=0,
        preflight_verdict="SKIPPED",
    )
    load_trajectory_state(workspace, fresh)
    assert fresh.escalate_kind == "hygiene"
    assert fresh.hygiene_escalation_dev_commit_sha == head_sha
    assert fresh.hygiene_escalation_prior_review is not None
    assert fresh.hygiene_escalation_prior_review.verdict == "APPROVE"


def test_resume_replays_consensus_when_dev_commit_unchanged(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    head_sha = _init_repo(workspace)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    _run_first_pass(workspace, config, task)
    # Clean up the stray file before "resume" — operator removed it manually.
    (workspace / "reviewer_scratch.txt").unlink()

    # Build a fresh state that represents the resume entry: load sidecar.
    resume_state = CoordinatorState(
        phase=Phase.REVIEW,
        dev_iteration=0,
        review_cycle=0,
        preflight_verdict="SKIPPED",
    )
    resume_state.workspace_path = workspace
    resume_state.branch_name = "main"
    resume_state.run_id = "test-run-2"
    resume_state.adaptive_review_max = 2
    load_trajectory_state(workspace, resume_state)

    assert resume_state.escalate_kind == "hygiene"
    assert resume_state.hygiene_escalation_dev_commit_sha == head_sha

    def _pool_must_not_run(*args, **kwargs):
        raise AssertionError("review pool must not be invoked when replaying consensus")

    with patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=_pool_must_not_run,
    ):
        outcome, result, _config2 = _run_review_phase(
            resume_state,
            config,
            task,
            "story",
            workspace,
            "main",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    assert outcome == _ReviewOutcome.DONE
    assert result is not None
    assert result.success is True
    assert result.phase == Phase.DONE
    assert len(resume_state.review_results) == 1
    assert resume_state.review_results[0].verdict == "APPROVE"
    assert resume_state.review_cycle == 1
    audit = resume_state.hygiene_resume_audit
    assert audit is not None
    assert audit["resume_action"] == "replayed_consensus"
    assert audit["dev_commit_sha_at_resume"] == head_sha
    # Replayed flag is cleared so a subsequent escalation does not re-replay.
    assert resume_state.escalate_kind is None
    assert resume_state.hygiene_escalation_prior_review is None


def _build_resume_state(workspace: Path) -> CoordinatorState:
    state = CoordinatorState(
        phase=Phase.REVIEW,
        dev_iteration=0,
        review_cycle=0,
        preflight_verdict="SKIPPED",
    )
    state.workspace_path = workspace
    state.branch_name = "main"
    state.run_id = "test-resume"
    state.adaptive_review_max = 2
    load_trajectory_state(workspace, state)
    return state


def test_resume_refuses_replay_when_untracked_mutation_persists(tmp_path):
    """Reviewer-created untracked file still present at resume → fresh review re-trips hygiene."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    head_sha = _init_repo(workspace)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    _run_first_pass(workspace, config, task)
    # Operator did NOT clean up reviewer_scratch.txt — it is still present.
    assert (workspace / "reviewer_scratch.txt").exists()

    resume_state = _build_resume_state(workspace)
    assert resume_state.escalate_kind == "hygiene"

    pool_calls = {"n": 0}

    def _fresh_pool(*args, **kwargs):
        pool_calls["n"] += 1
        candidate = _approve_review()
        return ([], [], candidate, [candidate], [("reviewer_a", candidate)])

    with patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=_fresh_pool,
    ):
        outcome, result, _config2 = _run_review_phase(
            resume_state,
            config,
            task,
            "story",
            workspace,
            "main",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    audit = resume_state.hygiene_resume_audit
    assert audit is not None
    assert audit["resume_action"] == "rerun_dirty_worktree"
    assert audit["dev_commit_sha_at_resume"] == head_sha
    assert "reviewer_scratch.txt" in audit["offending_paths"]
    # Fresh review pool ran once. The run did NOT mark DONE under the prior approval —
    # control flowed into a fresh review path that ultimately escalated rather than
    # finalizing approval on the still-dirty worktree.
    assert pool_calls["n"] == 1
    assert outcome == _ReviewOutcome.ESCALATE
    assert result is not None
    assert result.success is False
    assert result.phase != Phase.DONE


def test_resume_refuses_replay_when_tracked_file_mutated(tmp_path):
    """Reviewer modified a tracked file (HEAD unchanged) — replay must refuse."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    head_sha = _init_repo(workspace)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    # Stage hygiene-escalation state directly: prior consensus captured,
    # HEAD unchanged, but a tracked file has been modified after the trip.
    state = CoordinatorState(
        phase=Phase.REVIEW,
        dev_iteration=0,
        review_cycle=0,
        preflight_verdict="SKIPPED",
    )
    state.workspace_path = workspace
    state.branch_name = "main"
    state.run_id = "test-tracked"
    state.adaptive_review_max = 2
    state.escalate_kind = "hygiene"
    state.hygiene_escalation_dev_commit_sha = head_sha
    state.hygiene_escalation_prior_review = _approve_review()
    state.hygiene_escalation_prior_approve_count = 1
    state.hygiene_escalation_total_count = 1

    # Mutate a tracked file — HEAD is unchanged, but the worktree is dirty.
    (workspace / "README.md").write_text("seed\nhostile reviewer mutation\n", encoding="utf-8")

    pool_calls = {"n": 0}

    def _fresh_pool(*args, **kwargs):
        pool_calls["n"] += 1
        candidate = _approve_review()
        return ([], [], candidate, [candidate], [("reviewer_a", candidate)])

    with patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=_fresh_pool,
    ):
        outcome, result, _config2 = _run_review_phase(
            state,
            config,
            task,
            "story",
            workspace,
            "main",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    audit = state.hygiene_resume_audit
    assert audit is not None
    assert audit["resume_action"] == "rerun_dirty_worktree"
    assert any("README.md" in p for p in audit["offending_paths"])
    # Critical: the run did NOT mark DONE under the prior dev-commit approval.
    assert outcome != _ReviewOutcome.DONE
    if result is not None:
        assert result.phase != Phase.DONE
    # The fresh review pool was invoked (then hygiene re-tripped on the modified tracked file).
    assert pool_calls["n"] == 1


def test_resume_runs_fresh_review_when_dev_commit_changed(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _init_repo(workspace)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    _run_first_pass(workspace, config, task)
    (workspace / "reviewer_scratch.txt").unlink()

    # Advance HEAD so the SHA differs at resume.
    (workspace / "more.txt").write_text("more\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "more"], cwd=workspace, check=True)
    new_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    resume_state = CoordinatorState(
        phase=Phase.REVIEW,
        dev_iteration=0,
        review_cycle=0,
        preflight_verdict="SKIPPED",
    )
    resume_state.workspace_path = workspace
    resume_state.branch_name = "main"
    resume_state.run_id = "test-run-3"
    resume_state.adaptive_review_max = 2
    load_trajectory_state(workspace, resume_state)

    pool_calls = {"n": 0}

    def _fresh_pool(*args, **kwargs):
        pool_calls["n"] += 1
        candidate = _approve_review()
        return ([], [], candidate, [candidate], [("reviewer_a", candidate)])

    # Patch the empty-diff guard to True so the fresh review reaches the APPROVE path.
    with (
        patch(
            "theforge.coordinator.review_phase._run_review_pool",
            side_effect=_fresh_pool,
        ),
        patch(
            "theforge.coordinator.review_phase._has_commits_ahead_of_base",
            return_value=True,
        ),
    ):
        outcome, _result, _config2 = _run_review_phase(
            resume_state,
            config,
            task,
            "story",
            workspace,
            "main",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )

    assert pool_calls["n"] == 1, "fresh review pool must be invoked on dev-commit-changed resume"
    audit = resume_state.hygiene_resume_audit
    assert audit is not None
    assert audit["resume_action"] == "rerun_dev_commit_changed"
    assert audit["dev_commit_sha_at_resume"] == new_head
    # Stale escalation state cleared so a future hygiene escalation does not replay.
    assert resume_state.escalate_kind is None or resume_state.escalate_kind == "hygiene"
    # Outcome is DONE because the fresh APPROVE landed on a non-empty diff.
    assert outcome == _ReviewOutcome.DONE
