"""Tests for pull_base_branch re-exec behavior after source changes.

Covers:
- No re-exec when tree hash is unchanged after pull
- Re-exec (os.execv) when tree hash changes after pull
- SystemExit raised when os.execv itself raises OSError
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from coord_test_helpers import _make_config

from theforge.coordinator.workspace import pull_base_branch

_HASH_BEFORE = "abc123def456abc123def456abc123def456abc123"
_HASH_AFTER_SAME = "abc123def456abc123def456abc123def456abc123"
_HASH_AFTER_CHANGED = "999999999999999999999999999999999999999999"


def _make_shell_side_effect(tree_before: str, tree_after: str, pull_ok: bool = True):
    """Build a _run_shell side_effect for pull_base_branch tests."""
    calls = {"count": 0}

    def side_effect(cmd, cwd, **kwargs):
        if "rev-parse HEAD:src/theforge" in cmd:
            if calls["count"] == 0:
                calls["count"] += 1
                return (True, tree_before)
            else:
                return (True, tree_after)
        if "rev-parse --abbrev-ref HEAD" in cmd:
            return (True, "feat/other-branch")
        if "fetch origin" in cmd:
            return (pull_ok, "")
        return (True, "")

    return side_effect


class TestPullBaseReexec:
    @patch("theforge.coordinator.workspace._cu._log")
    @patch("theforge.coordinator.workspace.os.execv")
    @patch("theforge.coordinator.workspace.os.chdir")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_no_reexec_when_tree_unchanged(
        self, mock_shell, mock_chdir, mock_execv, mock_log, tmp_path
    ):
        """When tree hash is identical before and after pull, os.execv is NOT called."""
        config = _make_config(tmp_path)
        mock_shell.side_effect = _make_shell_side_effect(_HASH_BEFORE, _HASH_AFTER_SAME)

        result = pull_base_branch(config)

        assert result is True
        mock_execv.assert_not_called()
        mock_chdir.assert_not_called()

    @patch("theforge.coordinator.workspace._cu._log")
    @patch("theforge.coordinator.workspace.os.execv")
    @patch("theforge.coordinator.workspace.os.chdir")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_reexec_when_tree_changed(
        self, mock_shell, mock_chdir, mock_execv, mock_log, tmp_path
    ):
        """When tree hash differs after pull, os.execv is called with sys.executable + sys.argv."""
        config = _make_config(tmp_path)
        mock_shell.side_effect = _make_shell_side_effect(_HASH_BEFORE, _HASH_AFTER_CHANGED)

        pull_base_branch(config)

        mock_chdir.assert_called_once_with(str(config.project_root))
        mock_execv.assert_called_once_with(sys.executable, [sys.executable] + sys.argv)

    @patch("theforge.coordinator.workspace._cu._log")
    @patch("theforge.coordinator.workspace.os.execv", side_effect=OSError("exec failed"))
    @patch("theforge.coordinator.workspace.os.chdir")
    @patch("theforge.coordinator.workspace._cu._run_shell")
    def test_abort_when_execv_raises(self, mock_shell, mock_chdir, mock_execv, mock_log, tmp_path):
        """When os.execv raises OSError, SystemExit is raised with the re-run message."""
        config = _make_config(tmp_path)
        mock_shell.side_effect = _make_shell_side_effect(_HASH_BEFORE, _HASH_AFTER_CHANGED)

        with pytest.raises(SystemExit) as exc_info:
            pull_base_branch(config)

        assert "re-run forge sprint" in str(exc_info.value)
