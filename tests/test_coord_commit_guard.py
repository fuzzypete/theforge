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
from theforge.coordinator.commit_guard import (
    CHECKPOINT_COMMIT_SUBJECT,
    _checkpoint_commit,
    _has_commits_ahead_of_base,
    _worktree_has_changes,
)
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


# ── Checkpoint-commit helpers (#1746) ────────────────────────────────


def _last_commit_subject(path: Path) -> str:
    out = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_worktree_has_changes_detects_dirty_tracked_edit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("edited\n")
    assert _worktree_has_changes(tmp_path) is True


def test_worktree_has_changes_detects_untracked_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "brand_new.py").write_text("x = 1\n")
    assert _worktree_has_changes(tmp_path) is True


def test_worktree_has_changes_false_when_clean(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert _worktree_has_changes(tmp_path) is False


def test_worktree_has_changes_fails_closed_when_not_a_repo(tmp_path: Path) -> None:
    """Not a git repo — return False so no spurious checkpoint is attempted and
    the empty-diff guard's behaviour is left unchanged."""
    assert _worktree_has_changes(tmp_path) is False


def test_checkpoint_commit_creates_identifiable_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    # Simulate a killed iteration: production edit + a new (untracked) file.
    (tmp_path / "README.md").write_text("fix applied\n")
    (tmp_path / "new_module.py").write_text("def f():\n    return 1\n")

    assert _checkpoint_commit(tmp_path, "exit=-9") is True
    # Distinguishable from a normal dev commit by its fixed subject marker.
    assert _last_commit_subject(tmp_path) == CHECKPOINT_COMMIT_SUBJECT
    # Branch is now non-empty ahead of base — the zero-commit guard will pass.
    assert _has_commits_ahead_of_base(tmp_path, "main") is True
    # The reason is recorded in the commit body for audit.
    body = subprocess.run(
        ["git", "log", "-1", "--format=%b"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "exit=-9" in body
    # Both the edit and the new file are captured.
    files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "README.md" in files
    assert "new_module.py" in files
    # Worktree is clean afterwards.
    assert _worktree_has_changes(tmp_path) is False


def test_checkpoint_commit_no_commit_when_clean(tmp_path: Path) -> None:
    """A truly empty iteration produces no commit (no empty commit, still empty
    branch) so the empty-diff guard still escalates as before."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    assert _checkpoint_commit(tmp_path, "exit=-9") is False
    assert _has_commits_ahead_of_base(tmp_path, "main") is False


def test_checkpoint_commit_excludes_forge_artifacts(tmp_path: Path) -> None:
    """Transient .forge artifacts must not enter the checkpoint commit."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    (tmp_path / "real_work.py").write_text("x = 1\n")
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir(exist_ok=True)
    (forge_dir / "handoff.yaml").write_text("summary: partial\n")

    assert _checkpoint_commit(tmp_path, "exit=-9") is True
    files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "real_work.py" in files
    assert ".forge/handoff.yaml" not in files


def test_checkpoint_commit_no_commit_for_forge_only_churn(tmp_path: Path) -> None:
    """If the only dirty content is a forge artifact, no checkpoint is made."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir(exist_ok=True)
    (forge_dir / "handoff.yaml").write_text("summary: partial\n")
    assert _checkpoint_commit(tmp_path, "exit=-9") is False
    assert _has_commits_ahead_of_base(tmp_path, "main") is False


# ── DEV-phase seam: killed iteration is checkpointed before the guard ─


def _make_killed_agent_result() -> AgentResult:
    """Simulate a dev agent SIGKILLed at its per-iteration timeout (#1737)."""
    return AgentResult(
        success=False,
        output="TIMEOUT: Agent exceeded 900s limit",
        session_id=None,
        cost_usd=0.0,
        exit_code=-9,
        raw={},
        profile_name="dev",
        failure_code="timeout",
    )


def _run_failed_dev_with_uncommitted_work(tmp_path: Path, *, max_iterations: int) -> object:
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)

    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    state = CoordinatorState()
    state.budget.max_iterations = max_iterations

    spec = tmp_path / "specs" / "t.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# t\n", encoding="utf-8")

    from theforge.coordinator.dev_phase import _run_dev_phase

    def _kill_after_editing(*_args, **_kwargs):
        # The agent completes the production fix + starts migrating tests, then
        # is SIGKILLed mid-edit — leaving all of it uncommitted in the worktree.
        (tmp_path / "prod_fix.py").write_text("def fix():\n    return True\n")
        (tmp_path / "README.md").write_text("seed\nfix applied\n")
        return _make_killed_agent_result()

    with (
        patch("theforge.coordinator.dev_phase.run_agent", side_effect=_kill_after_editing),
        patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock()),
    ):
        result = _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/x", notify=False, logger=None
        )
    return result, state


def test_dev_phase_checkpoints_killed_iteration_before_guard(tmp_path: Path) -> None:
    """#1737 replay: a SIGKILLed iteration's uncommitted edits are committed to
    the branch before the zero-commit guard runs, so the guard sees a non-empty
    diff and does not escalate on a falsely-empty branch. Budget exhausted here
    so the timeout-retry path is not taken and the guard is actually reached."""
    result, state = _run_failed_dev_with_uncommitted_work(tmp_path, max_iterations=0)

    # A checkpoint commit exists, identifiable by its subject.
    assert _last_commit_subject(tmp_path) == CHECKPOINT_COMMIT_SUBJECT
    # The killed work is preserved on the branch.
    assert _has_commits_ahead_of_base(tmp_path, "main") is True
    files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "prod_fix.py" in files
    # The empty-diff guard did NOT escalate — the branch is genuinely non-empty.
    assert state.phase != Phase.ESCALATE
    assert "no commits ahead of base" not in (state.error or "")
    # The tracked worktree is clean — the killed work is committed, not stranded.
    # (Only transient .forge/ quarantine state remains, which is excluded by
    # design and gitignored in real worktrees.)
    tracked_dirty = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tracked_dirty == ""


def test_dev_phase_timeout_retry_starts_from_checkpointed_work(tmp_path: Path) -> None:
    """With iterations remaining, the timeout retry re-enters DEV over the
    checkpoint commit rather than an empty base."""
    result, state = _run_failed_dev_with_uncommitted_work(tmp_path, max_iterations=2)

    from theforge.coordinator.state import RetryReason

    assert result is None  # retry, not a terminal result
    assert state.retry_reason == RetryReason.TIMEOUT_RESUME
    # The killed iteration's work was committed before the retry decision.
    assert _last_commit_subject(tmp_path) == CHECKPOINT_COMMIT_SUBJECT
    assert _has_commits_ahead_of_base(tmp_path, "main") is True


def _make_max_iterations_agent_result() -> AgentResult:
    """Simulate a dev agent that exhausted its internal iteration budget without
    ever calling submit — no structured handoff, partial edits left uncommitted."""
    return AgentResult(
        success=False,
        output="ran out of iterations",
        session_id=None,
        cost_usd=0.0,
        exit_code=1,
        raw={},
        profile_name="dev",
        failure_code="max_iterations_reached",
        dev_handoff=None,
    )


def test_dev_phase_checkpoints_max_iterations_no_submit_before_retry(tmp_path: Path) -> None:
    """A max_iterations-without-submit iteration retries via return None; its
    partial edits must be checkpoint-committed first so the retry continues from
    committed work rather than branching from a falsely-empty base."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)

    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    state = CoordinatorState()
    state.budget.max_iterations = 2

    spec = tmp_path / "specs" / "t.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# t\n", encoding="utf-8")

    from theforge.coordinator.dev_phase import _run_dev_phase
    from theforge.coordinator.state import RetryReason

    def _burn_budget_after_editing(*_args, **_kwargs):
        (tmp_path / "partial_fix.py").write_text("def half_done():\n    return None\n")
        return _make_max_iterations_agent_result()

    with (
        patch(
            "theforge.coordinator.dev_phase.run_agent",
            side_effect=_burn_budget_after_editing,
        ),
        patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock()),
    ):
        result = _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/x", notify=False, logger=None
        )

    assert result is None  # retry, not terminal
    assert state.retry_reason == RetryReason.MAX_ITERATIONS_NO_SUBMIT
    # The partial work was checkpoint-committed before the retry.
    assert _last_commit_subject(tmp_path) == CHECKPOINT_COMMIT_SUBJECT
    assert _has_commits_ahead_of_base(tmp_path, "main") is True
    files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "partial_fix.py" in files


def test_dev_phase_empty_killed_iteration_still_escalates(tmp_path: Path) -> None:
    """A genuinely empty killed iteration (no worktree changes) creates no
    checkpoint commit and still escalates on the zero-commit guard — the guard's
    protection is unchanged for truly-empty runs."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)

    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    state = CoordinatorState()
    state.budget.max_iterations = 0  # exhausted → reach the guard

    spec = tmp_path / "specs" / "t.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# t\n", encoding="utf-8")

    from theforge.coordinator.dev_phase import _run_dev_phase

    with (
        patch(
            "theforge.coordinator.dev_phase.run_agent",
            return_value=_make_killed_agent_result(),
        ),
        patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock()),
    ):
        result = _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/x", notify=False, logger=None
        )

    assert isinstance(result, CoordinatorResult)
    assert result.success is False
    assert state.phase == Phase.ESCALATE
    assert "no commits ahead of base" in (state.error or "")
    assert _has_commits_ahead_of_base(tmp_path, "main") is False


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
