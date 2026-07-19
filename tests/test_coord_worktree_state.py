"""Tests for the worktree git-state consistency boundary (#1365).

A dev iteration has no legitimate need to mutate the branch state of its
worktree. Residue from a partially applied operation (in-progress rebase /
merge / cherry-pick / revert / bisect), a clean-but-illegitimate HEAD/ref
change (reset --hard behind the pre-dev base, a dev-introduced merge commit, a
checkout onto the wrong branch), is corrupted state that must never flow
silently into review or integration.

These tests cover:

* the pure checker (`check_worktree_git_consistency`) against real temp repos;
* the DEV-phase seam — a successful dev result that left residue escalates with
  a DEV-attributed error instead of returning None to advance;
* the integration seam — `_step_fetch_rebase` returns the structured
  DEV-attributed error (and never invokes `git rebase`) when residue is present,
  replaying the issue-1338 shape.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from coord_test_helpers import _make_agent_result  # noqa: E402

from theforge.config import (  # noqa: E402
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.state import (  # noqa: E402
    CoordinatorResult,
    CoordinatorState,
    Phase,
)
from theforge.coordinator.worktree_state import (  # noqa: E402
    WorktreeStateResult,
    check_worktree_git_consistency,
)
from theforge.task import TaskStory  # noqa: E402

# ── Helpers ─────────────────────────────────────────────────────────────


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


def _head_sha(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(path: Path, name: str, content: str = "x") -> str:
    (path / name).write_text(content)
    subprocess.run(["git", "add", name], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"add {name}"], cwd=path, check=True)
    return _head_sha(path)


# ── Unit tests: the pure checker ────────────────────────────────────────


def test_clean_worktree_is_consistent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _head_sha(tmp_path)
    result = check_worktree_git_consistency(tmp_path, expected_base_sha=base, base_branch="main")
    assert isinstance(result, WorktreeStateResult)
    assert result.consistent is True
    assert result.inconsistency is None


def test_clean_worktree_with_new_commits_is_consistent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _head_sha(tmp_path)
    _commit(tmp_path, "a.py")
    _commit(tmp_path, "b.py")
    result = check_worktree_git_consistency(tmp_path, expected_base_sha=base, base_branch="main")
    assert result.consistent is True


def test_rebase_merge_residue_is_inconsistent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _head_sha(tmp_path)
    # Fabricate a rebase-merge directory as git would leave mid-rebase.
    (tmp_path / ".git" / "rebase-merge").mkdir()
    result = check_worktree_git_consistency(tmp_path, expected_base_sha=base)
    assert result.consistent is False
    assert "rebase-merge" in (result.inconsistency or "")


def test_merge_head_residue_is_inconsistent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".git" / "MERGE_HEAD").write_text(_head_sha(tmp_path) + "\n")
    result = check_worktree_git_consistency(tmp_path)
    assert result.consistent is False
    assert "MERGE_HEAD" in (result.inconsistency or "")


def test_genuine_interrupted_rebase_is_inconsistent(tmp_path: Path) -> None:
    """A real conflicted `git rebase` leaves rebase-merge residue the checker catches."""
    _init_repo(tmp_path)
    base = _head_sha(tmp_path)
    # Two branches that both edit the same line → a rebase conflict.
    subprocess.run(["git", "checkout", "-q", "-b", "feat"], cwd=tmp_path, check=True)
    (tmp_path / "conflict.txt").write_text("feat\n")
    subprocess.run(["git", "add", "conflict.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=True)
    (tmp_path / "conflict.txt").write_text("main\n")
    subprocess.run(["git", "add", "conflict.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "feat"], cwd=tmp_path, check=True)
    # This rebase conflicts and leaves the worktree mid-rebase.
    proc = subprocess.run(["git", "rebase", "main"], cwd=tmp_path, capture_output=True)
    assert proc.returncode != 0, "expected the rebase to conflict"
    try:
        result = check_worktree_git_consistency(tmp_path, expected_base_sha=base)
        assert result.consistent is False
        assert "rebase" in (result.inconsistency or "").lower()
    finally:
        subprocess.run(["git", "rebase", "--abort"], cwd=tmp_path, capture_output=True)


def test_reset_hard_behind_base_is_non_descendant(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "a.py")
    pre_dev = _head_sha(tmp_path)  # base captured before dev
    _commit(tmp_path, "b.py")
    # Dev then rewinds HEAD behind the pre-dev base with a reset --hard.
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~2"], cwd=tmp_path, check=True)
    result = check_worktree_git_consistency(tmp_path, expected_base_sha=pre_dev)
    assert result.consistent is False
    assert "descends" in (result.inconsistency or "")


def test_dev_introduced_merge_commit_is_inconsistent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _head_sha(tmp_path)
    # Create a side branch, then merge it back with a real merge commit.
    subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=tmp_path, check=True)
    _commit(tmp_path, "side.py")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=True)
    _commit(tmp_path, "main.py")
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "-m", "merge side", "side"], cwd=tmp_path, check=True
    )
    result = check_worktree_git_consistency(tmp_path, expected_base_sha=base)
    assert result.consistent is False
    assert "merge commit" in (result.inconsistency or "")


def test_wrong_branch_is_inconsistent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _head_sha(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=tmp_path, check=True)
    result = check_worktree_git_consistency(
        tmp_path, expected_base_sha=base, expected_branch_name="feat/story"
    )
    assert result.consistent is False
    assert "unexpected branch" in (result.inconsistency or "")


def test_detached_head_is_inconsistent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _head_sha(tmp_path)
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=tmp_path, check=True)
    result = check_worktree_git_consistency(
        tmp_path, expected_base_sha=base, expected_branch_name="feat/story"
    )
    assert result.consistent is False
    assert "detached" in (result.inconsistency or "")


def test_expected_branch_match_is_consistent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _head_sha(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/story"], cwd=tmp_path, check=True)
    result = check_worktree_git_consistency(
        tmp_path, expected_base_sha=base, expected_branch_name="feat/story"
    )
    assert result.consistent is True


def test_residue_resolves_via_linked_worktree_git_path(tmp_path: Path) -> None:
    """In a linked worktree, residue lives under .git/worktrees/<slug>/ — the
    checker must resolve it via `git rev-parse --git-path`, not the shared .git."""
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _init_repo(main_repo)
    base = _head_sha(main_repo)
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feat/story", str(wt)],
        cwd=main_repo,
        check=True,
    )
    # Clean linked worktree is consistent.
    assert check_worktree_git_consistency(wt, expected_base_sha=base).consistent is True
    # Residue in the linked worktree's private git dir must be detected.
    git_path = subprocess.run(
        ["git", "rev-parse", "--git-path", "rebase-merge"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    resolved = Path(git_path)
    if not resolved.is_absolute():
        resolved = wt / resolved
    resolved.mkdir(parents=True)
    result = check_worktree_git_consistency(wt, expected_base_sha=base)
    assert result.consistent is False
    assert "rebase-merge" in (result.inconsistency or "")


# ── Seam test 1: DEV-phase boundary escalates on inherited residue ──────


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
        retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _run_dev(config, task, state, workspace, agent_result):
    from theforge.coordinator.dev_phase import _run_dev_phase

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.run_agent", return_value=agent_result)
        )
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock())
        )
        return _run_dev_phase(
            state, config, task, "# t\n", workspace, "feat/t", notify=False, logger=None
        )


def test_dev_phase_escalates_when_successful_result_left_residue(tmp_path: Path) -> None:
    """A successful dev result that left rebase residue must escalate with a
    DEV-attributed error instead of returning None to advance to review."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/t"], cwd=tmp_path, check=True)
    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    state = CoordinatorState()
    state.adaptive_dev_max = 3
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)

    # Dev produced a legitimate commit, but also left rebase-merge residue.
    def agent_side_effect(**_kwargs):
        _commit(tmp_path, "work.py")
        (tmp_path / ".git" / "rebase-merge").mkdir()
        return _make_agent_result(success=True, output="Done.")

    from theforge.coordinator.dev_phase import _run_dev_phase

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=agent_side_effect)
        )
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock())
        )
        result = _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/t", notify=False, logger=None
        )

    assert isinstance(result, CoordinatorResult)
    assert result.success is False
    assert state.phase == Phase.ESCALATE
    assert "inconsistent git state" in (state.error or "")
    assert "rebase-merge" in (state.error or "")
    assert "DEV phase" in (state.error or "")


def test_dev_phase_escalates_when_dev_left_head_detached(tmp_path: Path) -> None:
    """A dev result that ends with a detached HEAD (committed off the story
    branch) must escalate with a DEV-attributed error, not advance — integration
    force-pushes the named branch, so the detached commit would be silently lost."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/t"], cwd=tmp_path, check=True)
    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    state = CoordinatorState()
    state.adaptive_dev_max = 3
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)

    # Dev commits on the branch, then detaches HEAD and commits there — the
    # branch ref no longer points at HEAD.
    def agent_side_effect(**_kwargs):
        _commit(tmp_path, "onbranch.py")
        subprocess.run(["git", "checkout", "-q", "--detach"], cwd=tmp_path, check=True)
        _commit(tmp_path, "detached.py")
        return _make_agent_result(success=True, output="Done.")

    from theforge.coordinator.dev_phase import _run_dev_phase

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=agent_side_effect)
        )
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock())
        )
        result = _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/t", notify=False, logger=None
        )

    assert isinstance(result, CoordinatorResult)
    assert result.success is False
    assert state.phase == Phase.ESCALATE
    assert "inconsistent git state" in (state.error or "")
    assert "detached" in (state.error or "")
    assert "DEV phase" in (state.error or "")


def test_dev_phase_escalates_when_dev_left_wrong_branch(tmp_path: Path) -> None:
    """A dev result that ends checked out on a different branch than the
    coordinator's story branch must escalate rather than advance."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/t"], cwd=tmp_path, check=True)
    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    state = CoordinatorState()
    state.adaptive_dev_max = 3
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)

    def agent_side_effect(**_kwargs):
        _commit(tmp_path, "work.py")
        # Dev switches the worktree onto a different branch.
        subprocess.run(["git", "checkout", "-q", "-b", "feat/other"], cwd=tmp_path, check=True)
        return _make_agent_result(success=True, output="Done.")

    from theforge.coordinator.dev_phase import _run_dev_phase

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=agent_side_effect)
        )
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock())
        )
        result = _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/t", notify=False, logger=None
        )

    assert isinstance(result, CoordinatorResult)
    assert result.success is False
    assert state.phase == Phase.ESCALATE
    assert "unexpected branch" in (state.error or "")
    assert "DEV phase" in (state.error or "")


def test_dev_phase_advances_when_worktree_clean(tmp_path: Path) -> None:
    """A successful dev result with a clean worktree returns None to advance."""
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/t"], cwd=tmp_path, check=True)
    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    state = CoordinatorState()
    state.adaptive_dev_max = 3
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)

    def agent_side_effect(**_kwargs):
        _commit(tmp_path, "work.py")
        return _make_agent_result(success=True, output="Done.")

    from theforge.coordinator.dev_phase import _run_dev_phase

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=agent_side_effect)
        )
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock())
        )
        result = _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/t", notify=False, logger=None
        )

    assert result is None
    assert state.phase != Phase.ESCALATE


# ── Seam test 2: integration refuses inherited residue, never rebases ───


def test_step_fetch_rebase_refuses_inherited_residue_without_rebasing(tmp_path: Path) -> None:
    """Replays issue-1338: a rebase-merge dir left by dev makes _step_fetch_rebase
    return a structured DEV-attributed error and NEVER invoke `git rebase`."""
    from theforge.coordinator import completion

    _init_repo(tmp_path)
    # Dev left half-completed rebase residue in the worktree.
    (tmp_path / ".git" / "rebase-merge").mkdir()

    called_git = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "git":
            called_git.append(cmd[1])
        return real_run(cmd, *args, **kwargs)

    with patch.object(completion.subprocess, "run", side_effect=spy_run):
        result = completion._step_fetch_rebase(tmp_path, "main")

    assert result["success"] is False
    assert "inconsistent git state" in result["error"]
    assert "rebase-merge" in result["error"]
    assert "DEV phase" in result["error"]
    # DEV-attribution flag so landing paths escalate instead of MERGE_FAILED.
    assert result["inherited_dev_residue"] is True
    # The rebase must never have been attempted — no raw git rebase error.
    assert "rebase" not in called_git
    assert "fetch" not in called_git


def test_step_fetch_rebase_proceeds_when_no_residue(tmp_path: Path) -> None:
    """With a clean worktree the fetch/rebase path runs unchanged (fetch attempted)."""
    from theforge.coordinator import completion

    _init_repo(tmp_path)

    attempted = []
    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "git":
            attempted.append(cmd[1])
        return real_run(cmd, *args, **kwargs)

    with patch.object(completion.subprocess, "run", side_effect=spy_run):
        result = completion._step_fetch_rebase(tmp_path, "main")

    # No origin remote → fetch fails, but the point is the residue guard did NOT
    # short-circuit: the fetch was actually attempted.
    assert "fetch" in attempted
    assert result["success"] is False
    assert "inconsistent git state" not in result["error"]


# ── Seam test 3: inherited residue is classified as DEV escalation, ─────
#     never as a merge failure pinned on integration.


def _merge_pr_config(tmp_path: Path):
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

    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            on_approve="merge-pr",
            auto_push=True,
            base_branch="main",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def test_merge_pr_residue_propagates_dev_attribution_flag(tmp_path: Path) -> None:
    """_merge_pr must surface the inherited_dev_residue flag (and never rebase)
    when the worktree carries residue from a prior DEV iteration (#1365)."""
    from theforge.coordinator import completion

    config = _merge_pr_config(tmp_path)
    task = TaskStory(name="test-task", slug="test-task", story_path="specs/t.md")
    # Build the worktree the way _merge_pr resolves it and leave residue in it.
    worktree = tmp_path / "test-task"
    worktree.mkdir()
    _init_repo(worktree)
    (worktree / ".git" / "rebase-merge").mkdir()

    review = MagicMock()
    state = MagicMock()

    git_verbs = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd:
            if cmd[0] == "gh":
                # "already merged?" lookup → not merged, proceed to fetch/rebase.
                return _make_mock_proc(0, "[]")
            if cmd[0] == "git":
                git_verbs.append(cmd[1] if len(cmd) > 1 else "")
        return real_run(cmd, *args, **kwargs)

    with patch.object(completion.subprocess, "run", side_effect=fake_run):
        merge_info = completion._merge_pr(config, task, "forge/test-task", review, state)

    assert merge_info["success"] is False
    assert merge_info.get("inherited_dev_residue") is True
    assert "DEV phase" in merge_info["error"]
    # Residue short-circuits before any rebase is attempted.
    assert "rebase" not in git_verbs


def _make_mock_proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_landing_failure_outcome_classifies_inherited_residue_as_escalated() -> None:
    """The shared landing-failure classifier maps inherited DEV residue to a
    DEV-attributed ESCALATED outcome, arming failures to MERGE_ARMING_FAILED, and
    everything else to the generic MERGE_FAILED."""
    from theforge.sprint.story_state import StoryOutcome, landing_failure_outcome

    assert landing_failure_outcome({"inherited_dev_residue": True}) is StoryOutcome.ESCALATED
    # Residue attribution takes precedence over a co-present arming flag.
    assert (
        landing_failure_outcome({"inherited_dev_residue": True, "arming_failed": True})
        is StoryOutcome.ESCALATED
    )
    assert landing_failure_outcome({"arming_failed": True}) is StoryOutcome.MERGE_ARMING_FAILED
    assert landing_failure_outcome({}) is StoryOutcome.MERGE_FAILED
    assert landing_failure_outcome(None) is StoryOutcome.MERGE_FAILED


def test_mark_merge_failed_inherited_residue_escalates_not_merge_failed() -> None:
    """mark_merge_failed(inherited_dev_residue=True) records a DEV-attributed
    Phase.ESCALATE — not Phase.MERGE_FAILED — so integration is not blamed for
    state the DEV phase created."""
    from theforge.coordinator.completion import mark_merge_failed
    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase

    state = CoordinatorState()
    result = CoordinatorResult(
        success=True, phase=Phase.DONE, state=state, message="done", landing_status="failed"
    )

    mark_merge_failed(
        state,
        result,
        "dev left inconsistent git state: in-progress rebase (rebase-merge)",
        "forge/test-task",
        inherited_dev_residue=True,
    )

    assert state.phase is Phase.ESCALATE
    assert result.phase is Phase.ESCALATE
    assert result.success is False
    assert result.landing_status == "failed"
    assert "DEV" in result.message
    assert "merge failure" in result.message  # message disavows the merge-failure framing

    # Contrast: a normal merge failure still records MERGE_FAILED.
    state2 = CoordinatorState()
    result2 = CoordinatorResult(
        success=True, phase=Phase.DONE, state=state2, message="done", landing_status="failed"
    )
    mark_merge_failed(state2, result2, "conflict", "forge/test-task")
    assert state2.phase is Phase.MERGE_FAILED
    assert result2.phase is Phase.MERGE_FAILED
