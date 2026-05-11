"""Regression coverage for worktree-eval import isolation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from theforge.coordinator import util as coord_util


def test_worktree_eval_adds_checkout_src_before_ambient_pythonpath(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "test-task"
    workspace.mkdir()
    ambient = str(tmp_path / "ambient-site-packages")
    monkeypatch.setenv("PYTHONPATH", ambient)

    captured_env: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured_env.update(kwargs["env"])

        class Result:
            returncode = 0
            stderr = ""
            stdout = json.dumps({})

        return Result()

    with patch("theforge.coordinator.util._subprocess_run", side_effect=fake_run):
        coord_util._run_worktree_eval(workspace, "classify_families", {})

    entries = captured_env["PYTHONPATH"].split(os.pathsep)
    assert entries[0] == str((workspace / "src").resolve())
    assert entries[1] == str(coord_util._CHECKOUT_SRC)
    assert entries[2] == ambient
