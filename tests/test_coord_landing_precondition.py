"""Tests for the entry-time landing precondition (dirty project root).

A dirty project root makes ``_merge_branch`` refuse, but that refusal used to
arrive only after dev and review had been paid for (#2048). These tests pin the
condition being evaluated at the points where the spend can still be avoided:

- ``landing_precondition_error``: the predicate itself — only for runs that
  merge into the project-root checkout, naming the offending files
- ``run_task``: refuses in WORKSPACE, before the workspace is created and
  therefore before any dev or review agent is dispatched
- ``_run_resume_coordinator``: same refusal at the resume entry point, which is
  the first point a mid-sprint dirtying can be observed by the next story
- ``run_sprint``: refuses at sprint entry, before the base-branch pull and
  before the baseline gate
- ``_merge_branch``: still refuses on the same condition (parity — the entry
  check must not be able to drift away from the landing check)
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from coord_test_helpers import _make_config, _make_task
from test_sprint_runner import _make_empty_resolved, _make_runner_config

from theforge.config import ForgeConfig
from theforge.coordinator.engine import _run_resume_coordinator, run_task
from theforge.coordinator.logging import StructuredLogger
from theforge.coordinator.state import CoordinatorState, Phase
from theforge.coordinator.workspace import (
    _merge_branch,
    landing_precondition_error,
    project_root_dirty_status,
    story_lands_in_project_root,
)
from theforge.sprint.runner import run_sprint

DIRTY = " M forge.yaml"


def _merge_config(tmp_path: Path, *, on_approve: str = "merge") -> ForgeConfig:
    """Config whose approvals land in the project-root checkout."""
    config = _make_config(tmp_path)
    (tmp_path / ".git").mkdir(exist_ok=True)
    return replace(config, workspace=replace(config.workspace, on_approve=on_approve))


def _shell(status_out: str):
    """_run_shell stub answering `git status --porcelain` with status_out."""

    def _side_effect(cmd, cwd, **kwargs):
        if "status --porcelain" in cmd:
            return (True, status_out)
        return (True, "")

    return _side_effect


# ── The predicate ─────────────────────────────────────────────────────


class TestLandingPreconditionError:
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_dirty_root_under_merge_names_the_file(self, mock_shell, tmp_path):
        mock_shell.side_effect = _shell(DIRTY)
        err = landing_precondition_error(_merge_config(tmp_path))
        assert err is not None
        assert "forge.yaml" in err
        assert str(tmp_path) in err

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_clean_root_passes(self, mock_shell, tmp_path):
        mock_shell.side_effect = _shell("")
        assert landing_precondition_error(_merge_config(tmp_path)) is None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_untracked_file_refused_like_merge_does(self, mock_shell, tmp_path):
        """Parity with _merge_branch, which blocks on untracked files too."""
        mock_shell.side_effect = _shell("?? scratch.txt")
        err = landing_precondition_error(_merge_config(tmp_path))
        assert err is not None
        assert "scratch.txt" in err

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_pr_workflow_ignores_dirty_root(self, mock_shell, tmp_path):
        """on_approve: pr never touches the project-root checkout."""
        mock_shell.side_effect = _shell(DIRTY)
        assert landing_precondition_error(_merge_config(tmp_path, on_approve="pr")) is None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_auto_merge_flag_forces_the_check(self, mock_shell, tmp_path):
        """--auto-merge forces "merge" regardless of configuration."""
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path, on_approve="pr")
        assert landing_precondition_error(config, auto_merge=True) is not None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_non_git_project_root_passes(self, mock_shell, tmp_path):
        mock_shell.side_effect = _shell(DIRTY)
        config = _make_config(tmp_path)
        config = replace(config, workspace=replace(config.workspace, on_approve="merge"))
        assert landing_precondition_error(config) is None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_unreadable_status_fails_open(self, mock_shell, tmp_path):
        """A root we cannot inspect is not evidence of dirt."""
        mock_shell.return_value = (False, "fatal: not a git repository")
        assert project_root_dirty_status(tmp_path) == ""
        assert landing_precondition_error(_merge_config(tmp_path)) is None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_caller_supplied_obligation_overrides_the_derivation(self, mock_shell, tmp_path):
        """The scheduler's answer wins over auto_merge + on_approve.

        A parallel dependency parent has auto_merge=False and on_approve="none"
        yet is forced into a local merge after it returns, so the derivation
        alone would let it spend before the refusal.
        """
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path, on_approve="none")

        assert story_lands_in_project_root(config) is False
        assert story_lands_in_project_root(config, lands_in_project_root=True) is True
        assert landing_precondition_error(config) is None
        assert landing_precondition_error(config, lands_in_project_root=True) is not None

    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_caller_can_waive_a_derived_obligation(self, mock_shell, tmp_path):
        """An explicit False also wins — the override is not an OR."""
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path, on_approve="merge")
        assert landing_precondition_error(config, lands_in_project_root=False) is None


# ── Parity with the landing-time refusal ──────────────────────────────


class TestMergeBranchParity:
    @patch("theforge.coordinator.workspace._cu._log")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_merge_branch_still_refuses_a_dirty_root(self, mock_shell, _mock_log, tmp_path):
        def _side_effect(cmd, cwd, **kwargs):
            if "branch --list" in cmd:
                return (True, "  main\n")
            if "status --porcelain" in cmd:
                return (True, DIRTY)
            return (True, "")

        mock_shell.side_effect = _side_effect

        info = _merge_branch(tmp_path, "main", "forge/x", "x", tmp_path / "wt")

        assert info["merged"] is False
        assert "Uncommitted changes in project root" in info["error"]
        assert "forge.yaml" in info["error"]


# ── run_task: refuse before the workspace exists ──────────────────────


class TestRunTaskEntry:
    @patch("theforge.coordinator.engine._create_workspace")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_dirty_root_escalates_before_workspace_creation(
        self, mock_shell, mock_create, tmp_path
    ):
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path)
        task = _make_task(tmp_path)

        result = run_task(config, task)

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert "forge.yaml" in result.message
        mock_create.assert_not_called()

    @patch("theforge.coordinator.engine._create_workspace")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_scheduler_obligation_escalates_under_on_approve_none(
        self, mock_shell, mock_create, tmp_path
    ):
        """A parallel dependency parent refuses before spend.

        auto_merge is False and on_approve is "none", so only the scheduler's
        obligation reveals that this story will be forced into a local merge.
        """
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path, on_approve="none")
        task = _make_task(tmp_path)

        result = run_task(config, task, auto_merge=False, lands_in_project_root=True)

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert "forge.yaml" in result.message
        mock_create.assert_not_called()

    @patch("theforge.coordinator.engine._create_workspace")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_clean_root_proceeds_to_workspace(self, mock_shell, mock_create, tmp_path):
        mock_shell.side_effect = _shell("")
        mock_create.return_value = (None, None, "stop here")
        config = _merge_config(tmp_path)
        task = _make_task(tmp_path)

        result = run_task(config, task)

        mock_create.assert_called_once()
        assert "stop here" in result.message


# ── Resume entry: same refusal before re-running dev/review ───────────


class TestResumeEntry:
    @patch("theforge.coordinator.engine._setup_resume_entry")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_dirty_root_escalates_at_resume_entry(self, mock_shell, mock_setup, tmp_path):
        mock_shell.side_effect = _shell(DIRTY)
        config = _merge_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir(exist_ok=True)

        state = CoordinatorState(phase=Phase.REVIEW)
        logger = StructuredLogger(
            run_id="r1",
            project="test",
            task=task.slug,
            log_file=None,
            enabled=False,
            project_root=tmp_path,
        )
        mock_setup.return_value = (state, logger, "forge/test-task", "story", 0.0)

        result = _run_resume_coordinator(
            config,
            task,
            workspace,
            initial_phase=Phase.REVIEW,
            skip_dev_first_iter=True,
            interactive=False,
            auto_merge=False,
            notify=False,
            run_id="r1",
            sprint_name=None,
            state_update_fn=None,
            no_pull=True,
        )

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert "forge.yaml" in result.message


# ── Sprint entry: refuse before pull and before the baseline gate ─────


def _sprint_config(tmp_path: Path) -> ForgeConfig:
    config = _make_runner_config(tmp_path)
    (tmp_path / ".git").mkdir(exist_ok=True)
    return replace(config, workspace=replace(config.workspace, on_approve="merge"))


def test_run_sprint_dirty_root_aborts_before_pull_and_baseline(tmp_path: Path) -> None:
    config = _sprint_config(tmp_path)
    resolved = _make_empty_resolved()

    with (
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._get_or_create_sprint_id", return_value=None),
        patch("theforge.sprint.runner._project_root_is_git_checkout", return_value=True),
        patch("theforge.coordinator.workspace._cu._run_shell", side_effect=_shell(DIRTY)),
        patch("theforge.coordinator.workspace.pull_base_branch") as mock_pull,
        patch("theforge.sprint.runner._run_baseline_gate") as mock_baseline,
    ):
        with pytest.raises(RuntimeError, match="forge.yaml"):
            run_sprint(config, resolved)

    mock_pull.assert_not_called()
    mock_baseline.assert_not_called()


def test_run_sprint_clean_root_proceeds(tmp_path: Path) -> None:
    """The entry check must not block a sprint whose project root is clean."""
    config = _sprint_config(tmp_path)
    resolved = _make_empty_resolved()

    with (
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._get_or_create_sprint_id", return_value=None),
        patch("theforge.sprint.runner._project_root_is_git_checkout", return_value=True),
        patch("theforge.coordinator.workspace._cu._run_shell", side_effect=_shell("")),
        patch("theforge.coordinator.workspace.pull_base_branch") as mock_pull,
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.resolve_satisfied_dependencies", return_value=set()),
        patch(
            "theforge.sprint.runner.normalize_dependency_plan",
            return_value=SimpleNamespace(tasks=[], blocked={}),
        ),
        patch(
            "theforge.sprint.runner.run_batch_preflight",
            side_effect=RuntimeError("stop after baseline"),
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after baseline"):
            run_sprint(config, resolved)

    mock_pull.assert_called_once()


# ── Scheduler seam: the per-story landing obligation reaches the worker ──


class _StopDispatch(RuntimeError):
    """Raised from the fake executor to end the sprint at first submit."""


def _capturing_executor(captured: dict):
    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, *args, **kwargs):
            # _run_single_story positionals: config, task, triage, run_id,
            # sprint_name, interactive, notify, resume, effective_auto_merge, ...
            captured[args[1].slug] = {
                "lands_in_project_root": kwargs.get("lands_in_project_root"),
                "effective_auto_merge": args[8],
            }
            raise _StopDispatch(args[1].slug)

    return _FakeExecutor


def test_parallel_dependency_parent_dispatched_with_landing_obligation(tmp_path: Path) -> None:
    """A parallel dependency parent must reach its worker knowing it lands locally.

    on_approve is "none" and --auto-merge is off, so ``effective_am`` is False
    and the story's own flags reveal no landing. The scheduler nevertheless
    rewrites its result to a local merge once it returns, so without the
    threaded obligation the dirty-root refusal would land after dev and review
    (#2048 review iteration 1).
    """
    from tests.test_sprint_resume import _make_config as _make_sprint_config
    from tests.test_sprint_resume import _make_spec_file

    config = _make_sprint_config(tmp_path)
    assert config.workspace.on_approve == "none"

    parent = _make_spec_file(tmp_path, "Parent", "parent")
    child = _make_spec_file(tmp_path, "Child", "child", depends_on=["parent"])
    manifest = tmp_path / "sprint.yaml"
    manifest.write_text(
        "name: Test Sprint\nbudget_usd: 10.0\nmax_parallel: 2\nspecs:\n"
        f"  - {parent.name}\n  - {child.name}\n",
        encoding="utf-8",
    )

    captured: dict[str, bool | None] = {}

    with (
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _capturing_executor(captured)),
    ):
        with pytest.raises(_StopDispatch):
            run_sprint(config, manifest)

    parent_kwargs = captured.get("parent")
    assert parent_kwargs is not None, "dependency parent was never dispatched"
    # Proves the parallel path: the story's own auto-merge flag says no landing.
    assert parent_kwargs["effective_auto_merge"] is False
    assert parent_kwargs["lands_in_project_root"] is True, (
        "dependency parent dispatched without its scheduler-forced landing obligation"
    )


# ── Sprint entry: the obligation follows in-manifest dependency parents ──


def _dependency_sprint(
    tmp_path: Path, *, on_approve: str, dep: str, in_manifest_parent: bool
) -> tuple[ForgeConfig, Path]:
    """A dirty-rooted sprint whose only dependency edge is `dep`."""
    from tests.test_sprint_resume import _make_config as _make_sprint_config
    from tests.test_sprint_resume import _make_spec_file

    config = _make_sprint_config(tmp_path)
    config = replace(config, workspace=replace(config.workspace, on_approve=on_approve))
    (tmp_path / ".git").mkdir(exist_ok=True)

    specs = []
    if in_manifest_parent:
        specs.append(_make_spec_file(tmp_path, "Parent", dep).name)
    specs.append(_make_spec_file(tmp_path, "Child", "child", depends_on=[dep]).name)

    manifest = tmp_path / "sprint.yaml"
    manifest.write_text(
        "name: Test Sprint\nbudget_usd: 10.0\nspecs:\n" + "".join(f"  - {s}\n" for s in specs),
        encoding="utf-8",
    )
    return config, manifest


def _entry_patches(satisfied: set[str]):
    return (
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._project_root_is_git_checkout", return_value=True),
        patch("theforge.coordinator.workspace._cu._run_shell", side_effect=_shell(DIRTY)),
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch("theforge.sprint.runner.resolve_satisfied_dependencies", return_value=satisfied),
        patch(
            "theforge.sprint.runner.run_batch_preflight",
            side_effect=RuntimeError("stop after baseline"),
        ),
    )


def test_external_satisfied_dependency_does_not_impose_a_landing_obligation(
    tmp_path: Path,
) -> None:
    """A PR-landing sprint whose only edge points outside the manifest must run.

    The dependency target is an already-satisfied external issue: no story here
    merges into the project-root checkout, so its dirtiness is not forge's
    business and refusing would block a workflow that cannot hit the landing
    failure (#2048 review iteration 2).
    """
    config, manifest = _dependency_sprint(
        tmp_path, on_approve="pr", dep="external-issue", in_manifest_parent=False
    )

    with ExitStack() as stack:
        for cm in _entry_patches({"external-issue"}):
            stack.enter_context(cm)
        # Reaching the baseline gate proves the entry check did not refuse.
        with pytest.raises(RuntimeError, match="stop after baseline"):
            run_sprint(config, manifest)


def test_in_manifest_dependency_parent_still_imposes_a_landing_obligation(
    tmp_path: Path,
) -> None:
    """A parent carried by this sprint is merged locally to unblock its child.

    on_approve is "pr" and --auto-merge is off, so only the dependency edge
    reveals the landing — the sprint must still refuse a dirty root, and must
    do so before batch preflight, the first agent spend.
    """
    config, manifest = _dependency_sprint(
        tmp_path, on_approve="pr", dep="parent", in_manifest_parent=True
    )

    with ExitStack() as stack:
        mocks = [stack.enter_context(cm) for cm in _entry_patches(set())]
        mock_preflight = mocks[-1]
        with pytest.raises(RuntimeError, match="forge.yaml"):
            run_sprint(config, manifest)

    mock_preflight.assert_not_called()


def test_satisfied_in_manifest_parent_imposes_no_landing_obligation(tmp_path: Path) -> None:
    """A parent already merged is never dispatched, so it owes no landing.

    Same shape as the test above — an in-manifest dependency parent under
    on_approve "pr" — except dependency resolution reports it satisfied. Nothing
    in this sprint can merge into the project-root checkout, so the dirty root
    must not refuse it (#2048 review iteration 3). Sprint entry cannot know
    this, which is why the dependency-derived term is asserted only after the
    satisfied and resume-triage sets are resolved.
    """
    config, manifest = _dependency_sprint(
        tmp_path, on_approve="pr", dep="parent", in_manifest_parent=True
    )

    with ExitStack() as stack:
        for cm in _entry_patches({"parent"}):
            stack.enter_context(cm)
        # Reaching batch preflight proves neither pass refused.
        with pytest.raises(RuntimeError, match="stop after baseline"):
            run_sprint(config, manifest)
