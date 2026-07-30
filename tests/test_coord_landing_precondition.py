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
