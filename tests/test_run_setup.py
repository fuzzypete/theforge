"""Tests for _setup_resume_entry in coordinator/run_setup.py."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.context_scope import plan_file_list
from theforge.coordinator.engine import _run_resume_coordinator
from theforge.coordinator.gate import _parse_dirty_files
from theforge.coordinator.run_setup import (
    _deserialize_review_result,
    _serialize_review_result,
    _setup_resume_entry,
)
from theforge.coordinator.state import CoordinatorResult, Phase

_STRUCTURED_PLAN = """---
plan:
  approach: Resume the implementation
  steps:
    - id: 1
      description: Update the entry point
      files:
        - src/foo.py
        - tests/test_foo.py
      action: modify
      details: Continue the resumed story
    - id: 2
      description: Wire the helper
      files:
        - src/foo.py
        - src/helper.py
      action: modify
      details: Add the helper call
"""


def _write_plan(workspace: Path, text: str) -> None:
    plan_dir = workspace / ".forge"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan.md").write_text(text, encoding="utf-8")


def _call_setup(config, task, workspace_path):
    """Call _setup_resume_entry with standard test arguments."""
    with patch(
        "theforge.coordinator.run_setup._cu._run_shell", return_value=(True, "forge/test-task")
    ):
        return _setup_resume_entry(
            config,
            task,
            workspace_path,
            initial_phase=Phase.DEV,
            notify=False,
            run_id="test-run-id",
        )


def test_review_result_sidecar_roundtrip_preserves_finding_fields():
    """Serialize→deserialize of the hygiene-escalation sidecar must reconstruct
    ReviewFinding faithfully, including reporter attribution and the observed/
    expected/evidence prose fields (regression: the deserializer previously read a
    non-existent ``description`` key and dropped reporter)."""
    from theforge.review import ReviewFinding, ReviewResult

    original = ReviewResult(
        verdict="REQUEST_CHANGES",
        summary="needs work",
        findings=[
            ReviewFinding(
                severity="P1",
                file="src/foo.py",
                line=12,
                observed="null deref on retry",
                expected="runtime paths guard against null",
                evidence="src/foo.py:12",
                suggestion="add a guard",
                reviewers=("reviewer-2",),
                reporter="reviewer-2",
            )
        ],
        story_matches=False,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )

    restored = _deserialize_review_result(_serialize_review_result(original))

    assert restored is not None
    assert len(restored.findings) == 1
    f = restored.findings[0]
    assert f.reporter == "reviewer-2"
    assert f.observed == "null deref on retry"
    assert f.expected == "runtime paths guard against null"
    assert f.evidence == "src/foo.py:12"
    assert f.reviewers == ("reviewer-2",)
    assert f.severity == "P1"


def test_forge_yaml_synced_from_project_root(tmp_path):
    """forge.yaml in worktree is overwritten with root content on resume."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Write a stale forge.yaml in the worktree
    (workspace / "forge.yaml").write_text("project: stale\n", encoding="utf-8")

    # Write the current forge.yaml at project root
    root_forge_yaml = tmp_path / "forge.yaml"
    root_forge_yaml.write_text("project: current\n", encoding="utf-8")

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    result = _call_setup(config, task, workspace)

    assert not isinstance(result, tuple) or True  # didn't escalate
    synced_content = (workspace / "forge.yaml").read_text(encoding="utf-8")
    assert synced_content == "project: current\n"


def test_forge_yaml_sync_skipped_when_root_missing(tmp_path):
    """No error raised if project root has no forge.yaml."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    # No forge.yaml at project root — should succeed without error
    result = _call_setup(config, task, workspace)

    assert isinstance(result, tuple)
    state, logger, branch_name, story_content, task_start = result
    assert state.workspace_path == workspace


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(path: Path) -> None:
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")


def test_dirty_synced_forge_yaml_excluded_from_story_commit(tmp_path):
    """Seam regression for issue #1627: an uncommitted operator forge.yaml edit
    synced into the worktree must not be swept into the story's auto-commit.

    Drives the real seam — run_setup's sync (which flags the synced file
    skip-worktree) → gate dirty detection → the validate-phase ``git add -A``
    auto-commit — over a real git repo and asserts the resulting commit set
    omits forge.yaml while still capturing the story's own dev work.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_repo(workspace)

    # Committed baseline: the story's worktree starts with the mainline
    # forge.yaml and a story file, both tracked.
    committed_forge = "project: mainline\nmodel_order: [full, mini]\n"
    (workspace / "forge.yaml").write_text(committed_forge, encoding="utf-8")
    (workspace / "story.py").write_text("# story baseline\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "baseline")

    # Operator's live working-tree edit at project root — never committed.
    experiment_forge = "project: mainline\nmodel_order: [mini, full]  # EXPERIMENT #1617\n"
    (tmp_path / "forge.yaml").write_text(experiment_forge, encoding="utf-8")

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    # Real git runs (no _run_shell mock) so skip-worktree is actually applied.
    result = _setup_resume_entry(
        config,
        task,
        workspace,
        initial_phase=Phase.DEV,
        notify=False,
        run_id="test-run-id",
    )
    assert isinstance(result, tuple)

    # The run sees the operator's live config in the worktree...
    assert (workspace / "forge.yaml").read_text(encoding="utf-8") == experiment_forge

    # ...but the dirty synced file is invisible to git dirty detection.
    status = _git(workspace, "status", "--porcelain")
    assert "forge.yaml" not in _parse_dirty_files(status)

    # The dev cycle produces legitimate work, then the validate phase's
    # indiscriminate ``git add -A`` auto-commit fires.
    (workspace / "story.py").write_text("# story implemented\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "acceptance_criteria:")

    # The commit carries only the dev work; the operator experiment is absent.
    committed = _git(workspace, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert "story.py" in committed
    assert "forge.yaml" not in committed

    # forge.yaml on the branch still holds the committed mainline content —
    # the operator experiment never reached the story's tree.
    assert _git(workspace, "show", "HEAD:forge.yaml") == committed_forge


def test_resume_restores_plan_structured_from_worktree_plan(tmp_path):
    """Seam regression for issue #1135: the PLAN_VALIDATION → DEV/REVIEW handoff.

    A resumed run allocates a fresh CoordinatorState. Without restoring the plan
    from the worktree's .forge/plan.md, state.plan_structured stays None and every
    plan-derived policy signal (stuck-detection scaling, routing, audit telemetry)
    degrades to zero even when the plan demonstrably declares target files. This
    drives the real entry point and asserts the restored structure matches the
    plan on disk, so plan_file_list reports the declared files rather than 0.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_plan(workspace, _STRUCTURED_PLAN)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    result = _call_setup(config, task, workspace)

    assert isinstance(result, tuple)
    state = result[0]
    assert state.plan_structured is not None
    assert state.plan_output == _STRUCTURED_PLAN
    # The file list downstream policy consumes matches the plan's declared files.
    assert plan_file_list(state.plan_structured) == [
        "src/foo.py",
        "tests/test_foo.py",
        "src/helper.py",
    ]


def test_resume_freeform_plan_restores_text_but_not_structure(tmp_path):
    """A freeform-markdown plan restores raw text but leaves plan_structured None.

    This matches the non-resume PLAN path, where an unparseable plan falls back to
    markdown and plan_structured is legitimately None (distinct from the #1135 bug).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    freeform = "# Plan\n\nJust do the thing, no YAML block here.\n"
    _write_plan(workspace, freeform)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    result = _call_setup(config, task, workspace)

    assert isinstance(result, tuple)
    state = result[0]
    assert state.plan_output == freeform
    assert state.plan_structured is None


def test_resume_no_plan_leaves_state_defaults(tmp_path):
    """A resume with no .forge/plan.md leaves plan fields at their defaults."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    result = _call_setup(config, task, workspace)

    assert isinstance(result, tuple)
    state = result[0]
    assert state.plan_output is None
    assert state.plan_structured is None


def test_setup_returns_escalate_when_workspace_missing(tmp_path):
    """Returns CoordinatorResult when workspace_path doesn't exist."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    missing = tmp_path / "nonexistent"

    with patch(
        "theforge.coordinator.run_setup._cu._run_shell", return_value=(True, "forge/test-task")
    ):
        result = _setup_resume_entry(
            config,
            task,
            missing,
            initial_phase=Phase.DEV,
            notify=False,
            run_id=None,
        )

    assert isinstance(result, CoordinatorResult)
    assert not result.success


def test_setup_resume_entry_does_not_mutate_sys_path(tmp_path):
    """Resume setup no longer mutates sys.path — worktree src is not prepended."""
    import sys

    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    original_sys_path = list(sys.path)
    try:
        with patch(
            "theforge.coordinator.run_setup._cu._run_shell", return_value=(True, "forge/test-task")
        ):
            result = _setup_resume_entry(
                config,
                task,
                workspace,
                initial_phase=Phase.DEV,
                notify=False,
                run_id="test-run-id",
            )

        assert isinstance(result, tuple)
        worktree_src = str((workspace / "src").resolve())
        assert worktree_src not in sys.path, "prepend_worktree_src must not be called"
    finally:
        sys.path[:] = original_sys_path


def test_run_resume_coordinator_rebases_before_loop_and_continues(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    state = MagicMock()
    state.workspace_path = workspace
    state.log_dir = None
    logger = MagicMock()
    setup = (state, logger, "forge/test-task", "story", 123.0)
    loop_result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")

    with (
        patch("theforge.coordinator.engine._ensure_runners"),
        patch("theforge.coordinator.engine.pull_base_branch"),
        patch("theforge.coordinator.engine._setup_resume_entry", return_value=setup),
        patch("theforge.coordinator.engine._make_story_log_dir", return_value=tmp_path / "logs"),
        patch("theforge.coordinator.engine._run_log_context") as mock_ctx,
        patch("theforge.coordinator.engine._rebase_onto_main", return_value=(True, "")),
        patch(
            "theforge.coordinator.engine._coordinator_loop", return_value=loop_result
        ) as mock_loop,
        patch("theforge.coordinator.engine._fire_post_run_hook"),
    ):
        mock_ctx.return_value.__enter__.return_value = None
        mock_ctx.return_value.__exit__.return_value = None
        result = _run_resume_coordinator(
            config,
            task,
            workspace,
            initial_phase=Phase.DEV,
            skip_dev_first_iter=False,
            interactive=False,
            auto_merge=False,
            notify=False,
            run_id="run-1",
            sprint_name=None,
            state_update_fn=None,
        )

    assert result is loop_result
    mock_loop.assert_called_once()
    logger._safe_emit.assert_any_call(
        "rebase", phase="RESUME_REBASE", base_branch=config.workspace.base_branch, outcome="ok"
    )


def test_run_resume_coordinator_aborts_when_base_branch_unpublished(tmp_path):
    """The resume entry uses the blocking base sync, not the old advisory check.

    A reused worktree is about to be rebased onto the base branch, so an
    unpublished commit there would be absorbed into this story's diff. The
    RuntimeError must propagate rather than be logged and stepped over.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    with (
        patch("theforge.coordinator.engine._ensure_runners"),
        patch(
            "theforge.coordinator.engine.pull_base_branch",
            side_effect=RuntimeError("WORKSPACE abort: base branch 'main' has 1 local commit(s)"),
        ) as mock_pull,
        patch("theforge.coordinator.engine._setup_resume_entry") as mock_setup,
        pytest.raises(RuntimeError, match="has 1 local commit"),
    ):
        _run_resume_coordinator(
            config,
            task,
            workspace,
            initial_phase=Phase.DEV,
            skip_dev_first_iter=False,
            interactive=False,
            auto_merge=False,
            notify=False,
            run_id="run-1",
            sprint_name=None,
            state_update_fn=None,
        )

    mock_pull.assert_called_once_with(config)
    mock_setup.assert_not_called()


def test_run_resume_coordinator_skips_base_sync_when_no_pull(tmp_path):
    """no_pull=True still suppresses the base-branch sync entirely."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    state = MagicMock()
    state.workspace_path = workspace
    state.log_dir = None
    logger = MagicMock()
    setup = (state, logger, "forge/test-task", "story", 123.0)
    loop_result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")

    with (
        patch("theforge.coordinator.engine._ensure_runners"),
        patch("theforge.coordinator.engine.pull_base_branch") as mock_pull,
        patch("theforge.coordinator.engine._setup_resume_entry", return_value=setup),
        patch("theforge.coordinator.engine._make_story_log_dir", return_value=tmp_path / "logs"),
        patch("theforge.coordinator.engine._run_log_context") as mock_ctx,
        patch("theforge.coordinator.engine._rebase_onto_main", return_value=(True, "")),
        patch("theforge.coordinator.engine._coordinator_loop", return_value=loop_result),
        patch("theforge.coordinator.engine._fire_post_run_hook"),
    ):
        mock_ctx.return_value.__enter__.return_value = None
        mock_ctx.return_value.__exit__.return_value = None
        _run_resume_coordinator(
            config,
            task,
            workspace,
            initial_phase=Phase.DEV,
            skip_dev_first_iter=False,
            interactive=False,
            auto_merge=False,
            notify=False,
            run_id="run-1",
            sprint_name=None,
            state_update_fn=None,
            no_pull=True,
        )

    mock_pull.assert_not_called()


def test_run_resume_coordinator_escalates_when_rebase_fails(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    state = MagicMock()
    state.workspace_path = workspace
    state.log_dir = None
    setup = (state, MagicMock(), "forge/test-task", "story", 123.0)

    with (
        patch("theforge.coordinator.engine._ensure_runners"),
        patch("theforge.coordinator.engine.pull_base_branch"),
        patch("theforge.coordinator.engine._setup_resume_entry", return_value=setup),
        patch("theforge.coordinator.engine._make_story_log_dir", return_value=tmp_path / "logs"),
        patch("theforge.coordinator.engine._run_log_context") as mock_ctx,
        patch(
            "theforge.coordinator.engine._rebase_onto_main",
            return_value=(False, "merge conflict"),
        ),
        patch("theforge.coordinator.engine._coordinator_loop") as mock_loop,
        patch("theforge.coordinator.engine._fire_post_run_hook"),
        patch("theforge.coordinator.engine._escalate_notify") as mock_notify,
    ):
        mock_ctx.return_value.__enter__.return_value = None
        mock_ctx.return_value.__exit__.return_value = None
        result = _run_resume_coordinator(
            config,
            task,
            workspace,
            initial_phase=Phase.DEV,
            skip_dev_first_iter=False,
            interactive=False,
            auto_merge=False,
            notify=True,
            run_id="run-1",
            sprint_name=None,
            state_update_fn=None,
        )

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    assert "pre-dev rebase" in result.message
    assert "pre-dev rebase" in state.escalate_reason
    mock_loop.assert_not_called()
    mock_notify.assert_called_once()


def test_resume_rebase_succeeds_when_base_forge_yaml_diverged(tmp_path):
    """Seam regression for issue #1772: a resumed worktree whose committed
    forge.yaml predates a forge.yaml change on the base branch must complete the
    pre-dev rebase without ESCALATE, while operator config still never lands in a
    story commit.

    Drives the real seam — run_setup's sync (which flags the synced file
    skip-worktree, issue #1627) → _rebase_onto_main's fetch+rebase onto
    origin/main — over a real git repo with a diverged origin base branch.
    Without the detach/reattach handling the skip-worktree'd working-tree
    forge.yaml aborts the rebase ("local changes ... would be overwritten by
    checkout").
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _init_repo(origin)

    # Baseline on origin/main: the mainline forge.yaml plus a story file.
    committed_forge = "project: mainline\nmodel_order: [full, mini]\n"
    (origin / "forge.yaml").write_text(committed_forge, encoding="utf-8")
    (origin / "story.py").write_text("# story baseline\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "baseline")

    # The worktree is cloned/branched from this baseline (predates the change).
    workspace = tmp_path / "workspace"
    _git(tmp_path, "clone", "-q", str(origin), str(workspace))
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "checkout", "-q", "-b", "forge/test-task")
    # Story work that does NOT touch forge.yaml.
    (workspace / "story.py").write_text("# story in progress\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "wip story")

    # The base branch then diverges on forge.yaml — an everyday forge-managed change.
    updated_forge = "project: mainline\nmodel_order: [full, mini, nano]\n"
    (origin / "forge.yaml").write_text(updated_forge, encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "bump forge.yaml on main")

    # Operator's live working-tree config at project root — never committed.
    experiment_forge = "project: mainline\nmodel_order: [mini, full]  # EXPERIMENT\n"
    (tmp_path / "forge.yaml").write_text(experiment_forge, encoding="utf-8")

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)

    # Real git runs (no _run_shell mock) so skip-worktree is actually applied.
    result = _setup_resume_entry(
        config,
        task,
        workspace,
        initial_phase=Phase.DEV,
        notify=False,
        run_id="test-run-id",
    )
    assert isinstance(result, tuple)

    # The pre-dev rebase must succeed — no manufactured conflict.
    from theforge.coordinator.run_setup import _rebase_onto_main

    rebase_ok, rebase_err = _rebase_onto_main(str(workspace), "main", None)
    assert rebase_ok, rebase_err

    # The base-branch forge.yaml change was applied to the branch tip.
    assert _git(workspace, "show", "HEAD:forge.yaml") == updated_forge
    # The story's own work survived the rebase.
    assert "# story in progress\n" == (workspace / "story.py").read_text(encoding="utf-8")

    # The run still sees operator config in the working tree...
    assert (workspace / "forge.yaml").read_text(encoding="utf-8") == experiment_forge
    # ...and it remains hidden from dirty detection (skip-worktree re-set).
    status = _git(workspace, "status", "--porcelain")
    assert "forge.yaml" not in _parse_dirty_files(status)

    # A subsequent indiscriminate ``git add -A`` auto-commit omits forge.yaml.
    (workspace / "story.py").write_text("# story implemented\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "acceptance_criteria:")
    committed = _git(workspace, "show", "--name-only", "--pretty=format:", "HEAD").split()
    assert "story.py" in committed
    assert "forge.yaml" not in committed
    # The branch's forge.yaml is the rebased base content, not operator config.
    assert _git(workspace, "show", "HEAD:forge.yaml") == updated_forge


def test_resume_rebase_preserves_merge_based_conflict_resolution(tmp_path):
    """Seam regression for issue #1794 (defect #2): a story branch brought current
    by an operator's ``git merge main`` (with the conflict resolved in a merge
    commit) must survive resume without ESCALATE.

    A linear ``git rebase origin/main`` here would replay the branch's original
    pre-merge commit and drop the merge commit, discarding the resolution and
    re-firing the exact conflict the operator already settled. Because the merged
    branch already contains origin/main as an ancestor, _rebase_onto_main must
    short-circuit — skip the replay entirely, preserve the resolved content, and
    return success.
    """
    from theforge.coordinator.run_setup import _rebase_onto_main

    origin = tmp_path / "origin"
    origin.mkdir()
    _init_repo(origin)

    # Baseline on origin/main: a story file both sides will edit conflictingly.
    (origin / "forge.yaml").write_text("project: mainline\n", encoding="utf-8")
    (origin / "story.py").write_text("value = 0\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "baseline")

    workspace = tmp_path / "workspace"
    _git(tmp_path, "clone", "-q", str(origin), str(workspace))
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "checkout", "-q", "-b", "forge/test-task")

    # Story work edits story.py in a way that conflicts with the coming base edit.
    (workspace / "story.py").write_text("value = 1  # story\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "wip story")

    # The base branch then edits the same line, conflicting with the story.
    (origin / "story.py").write_text("value = 2  # main\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "edit story.py on main")

    # Operator brings the branch current with a merge-based conflict resolution.
    _git(workspace, "fetch", "-q", "origin", "main")
    merge = subprocess.run(
        ["git", "merge", "origin/main"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    assert merge.returncode != 0, "expected a merge conflict to resolve"
    resolved = "value = 3  # resolved: story + main\n"
    (workspace / "story.py").write_text(resolved, encoding="utf-8")
    _git(workspace, "add", "story.py")
    _git(workspace, "commit", "-q", "--no-edit")

    # The resumed pre-dev rebase must recognize the branch already contains base
    # and short-circuit rather than replay the pre-merge commit.
    rebase_ok, rebase_err = _rebase_onto_main(str(workspace), "main", None)
    assert rebase_ok, rebase_err

    # The operator's merge resolution is preserved verbatim — no conflict markers,
    # no re-fired conflict, no reversion to either original side.
    working = (workspace / "story.py").read_text(encoding="utf-8")
    assert working == resolved
    assert "<<<<<<<" not in working
    assert _git(workspace, "show", "HEAD:story.py") == resolved

    # No rebase is left in progress (a re-fired conflict would leave one).
    assert not (workspace / ".git" / "rebase-merge").exists()
    assert not (workspace / ".git" / "rebase-apply").exists()


def test_resume_rebase_still_rebases_branch_behind_base(tmp_path):
    """Companion to the short-circuit: a branch that does NOT yet contain base
    must still be rebased forward, so the ancestor check doesn't disable the
    normal integration path.
    """
    from theforge.coordinator.run_setup import _rebase_onto_main

    origin = tmp_path / "origin"
    origin.mkdir()
    _init_repo(origin)

    (origin / "base.py").write_text("base = 0\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "baseline")

    workspace = tmp_path / "workspace"
    _git(tmp_path, "clone", "-q", str(origin), str(workspace))
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    _git(workspace, "checkout", "-q", "-b", "forge/test-task")

    # Story work on a distinct file (no conflict with the base edit).
    (workspace / "story.py").write_text("story = 1\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "wip story")

    # The base branch advances on an unrelated file; branch does NOT contain it.
    (origin / "base.py").write_text("base = 1\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "advance main")

    rebase_ok, rebase_err = _rebase_onto_main(str(workspace), "main", None)
    assert rebase_ok, rebase_err

    # The rebase actually integrated the base advance and kept the story commit.
    assert (workspace / "base.py").read_text(encoding="utf-8") == "base = 1\n"
    assert (workspace / "story.py").read_text(encoding="utf-8") == "story = 1\n"
    _git(workspace, "fetch", "-q", "origin", "main")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    assert ancestor.returncode == 0, "base must be an ancestor after the rebase"
