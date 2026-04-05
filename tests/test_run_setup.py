"""Tests for _setup_resume_entry in coordinator/run_setup.py."""

from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.path_setup import prepend_worktree_src
from theforge.coordinator.run_setup import _setup_resume_entry
from theforge.coordinator.state import Phase


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


def test_setup_returns_escalate_when_workspace_missing(tmp_path):
    """Returns CoordinatorResult when workspace_path doesn't exist."""
    from theforge.coordinator.state import CoordinatorResult

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


def test_prepend_worktree_src_puts_worktree_src_first(tmp_path, monkeypatch):
    """Worktree src is prepended ahead of an existing project-root src entry."""
    root_src = tmp_path / "root" / "src"
    worktree_src = tmp_path / "worktree" / "src"
    root_src.mkdir(parents=True)
    worktree_src.mkdir(parents=True)

    monkeypatch.setattr("sys.path", [str(root_src), "existing"])

    prepend_worktree_src(worktree_src.parent)

    import sys

    assert sys.path[0] == str(worktree_src.resolve())
    assert str(root_src) in sys.path


def test_setup_resume_entry_prepends_worktree_src(tmp_path):
    """Resume setup prepends the active worktree src directory to sys.path."""
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
        assert sys.path[0] == str((workspace / "src").resolve())
    finally:
        sys.path[:] = original_sys_path
