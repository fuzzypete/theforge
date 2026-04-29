"""Tests for the zero-commit guard at DEV→VALIDATE→REVIEW seams.

A dev iteration that produces zero commits ahead of base must escalate at the
DEV phase (on agent failure), at VALIDATE (gate PASS over empty worktree), and
at REVIEW (refuse APPROVE on empty diff). Without these guards, a crashed dev
agent silently flowed through to APPROVE/DONE because:
- gate exits 0 (nothing to test)
- review summarises "no commits — nothing to review" then APPROVEs
- integration's no-commits check fires only at PR creation, after DONE.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.commit_guard import _has_commits_ahead_of_base
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.coordinator.validate_phase import _run_validate_phase, _ValidateOutcome
from theforge.runners import AgentResult
from theforge.task import TaskStory


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
            base_branch="main",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


# ── Helper unit tests ──────────────────────────────────────────────────


def test_has_commits_ahead_returns_false_for_empty_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    assert _has_commits_ahead_of_base(tmp_path, "main") is False


def test_has_commits_ahead_returns_true_with_new_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=tmp_path, check=True)
    assert _has_commits_ahead_of_base(tmp_path, "main") is True


def test_has_commits_ahead_fails_open_when_git_unavailable(tmp_path: Path) -> None:
    """Not a git repo at all — fail-open returns True so transient git issues
    don't trigger spurious escalations on healthy runs."""
    assert _has_commits_ahead_of_base(tmp_path, "main") is True


# ── Seam test: VALIDATE escalates gate-PASS over empty branch ─────────


def test_validate_escalates_on_pass_with_zero_commits(tmp_path: Path) -> None:
    """VALIDATE must not advance an empty worktree to REVIEW."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)

    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md", gate_override="none")
    state = CoordinatorState()

    outcome, result = _run_validate_phase(state, config, task, tmp_path, notify=False, logger=None)
    assert outcome == _ValidateOutcome.ESCALATE
    assert result is not None
    assert result.success is False
    assert state.phase == Phase.ESCALATE
    assert "no commits ahead of base" in (state.error or "")


def test_validate_passes_when_branch_has_commits(tmp_path: Path) -> None:
    """Sanity: a branch with commits ahead of base does not trip the new guard."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=tmp_path, check=True)

    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md", gate_override="none")
    state = CoordinatorState()

    outcome, _result = _run_validate_phase(
        state, config, task, tmp_path, notify=False, logger=None
    )
    # Not escalated by the zero-commits guard.
    assert outcome != _ValidateOutcome.ESCALATE or "no commits ahead of base" not in (
        state.error or ""
    )


# ── DEV-phase guard: failed dev + no commits → ESCALATE ──────────────


def _make_failed_agent_result() -> AgentResult:
    """Simulate a dev agent that crashed (signal kill, exit -2) with no output."""
    return AgentResult(
        success=False,
        output="",
        session_id=None,
        cost_usd=0.0,
        exit_code=-2,
        raw={},
        profile_name="dev",
    )


def test_dev_phase_escalates_on_failed_agent_with_no_commits(tmp_path: Path) -> None:
    """A failed dev exit on an empty branch must escalate, not advance to VALIDATE."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)

    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    state = CoordinatorState()

    spec = tmp_path / "specs" / "t.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# t\n", encoding="utf-8")

    from theforge.coordinator.dev_phase import _run_dev_phase

    with (
        patch(
            "theforge.coordinator.dev_phase.run_agent", return_value=_make_failed_agent_result()
        ),
        patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock()),
    ):
        result = _run_dev_phase(
            state,
            config,
            task,
            "# t\n",
            tmp_path,
            "feat/x",
            notify=False,
            logger=None,
        )

    assert isinstance(result, CoordinatorResult)
    assert result.success is False
    assert state.phase == Phase.ESCALATE
    assert "no commits ahead of base" in (state.error or "")


# ── REVIEW-phase guard: documented via helper behaviour ─────────────


def test_review_guard_helper_reports_empty_branch(tmp_path: Path) -> None:
    """The REVIEW empty-diff guard at review_phase keys off this helper.

    Verifying the helper's behaviour over an empty branch is the seam contract
    the REVIEW guard relies on; full _run_review_phase exercises require the
    review pool, synthesis, and audit machinery which are out of scope here.
    """
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    assert _has_commits_ahead_of_base(tmp_path, "main") is False
