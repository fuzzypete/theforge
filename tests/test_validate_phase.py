"""Focused tests for VALIDATE phase helpers."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_agent_result, _make_config, _make_task

from theforge.coordinator.state import CoordinatorState
from theforge.coordinator.validate_phase import (
    _get_convention_baseline_ref,
    _run_validate_phase,
    _ValidateOutcome,
)


def test_get_convention_baseline_ref_returns_merge_base(tmp_path: Path) -> None:
    """Helper should resolve the merge-base between HEAD and the base branch."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")

    _write(tmp_path / "README.md", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    base_sha = _git(tmp_path, "rev-parse", "HEAD")

    _git(tmp_path, "checkout", "-b", "feature")
    _write(tmp_path / "feature.txt", "change\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "feature")

    assert _get_convention_baseline_ref(tmp_path, "main") == base_sha


def test_get_convention_baseline_ref_returns_none_when_base_missing(tmp_path: Path) -> None:
    """Helper should fail closed when the requested base branch is unavailable."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _write(tmp_path / "README.md", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    assert _get_convention_baseline_ref(tmp_path, "does-not-exist") is None


def test_run_validate_phase_records_failed_gate_iteration_telemetry(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(3.5)
    state.last_dev_start_commit = "HEAD"

    with (
        patch(
            "theforge.coordinator.validate_phase._run_gate_full",
            return_value=("FAIL", None, "FAILED tests/test_alpha.py::test_one"),
        ),
        patch(
            "theforge.coordinator.validate_phase._get_handoff_content",
            return_value="summary: pending",
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            dev_calls_this_cycle=1,
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.RETRY_DEV
    assert result is None
    assert len(state.dev_iteration_telemetry) == 1
    telemetry = state.dev_iteration_telemetry[0]
    assert telemetry.gate_result == "FAIL"
    assert telemetry.failed_tests == ["tests/test_alpha.py::test_one"]


def test_run_validate_phase_records_dirty_pass_iteration_once(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(2.0)
    state.last_dev_start_commit = "HEAD"

    def shell_side_effect(cmd, cwd, **kwargs):
        if cmd == "git status --porcelain":
            return True, " M src/example.py"
        if cmd.startswith("git diff --name-only"):
            return True, "src/example.py"
        if cmd == "git add -A":
            return True, ""
        return True, ""

    with (
        patch(
            "theforge.coordinator.validate_phase._run_gate_full", return_value=("PASS", None, "OK")
        ),
        patch(
            "theforge.coordinator.validate_phase._get_raw_dev_notes",
            return_value="summary: tidy worktree",
        ),
        patch("theforge.coordinator.validate_phase._deindex_forge_artifacts"),
        patch("theforge.coordinator.util._run_shell", side_effect=shell_side_effect),
        patch("theforge.coordinator.validate_phase.subprocess.run") as commit_run,
    ):
        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            dev_calls_this_cycle=1,
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.PASS
    assert result is None
    commit_run.assert_called_once()
    assert len(state.dev_iteration_telemetry) == 1
    telemetry = state.dev_iteration_telemetry[0]
    assert telemetry.gate_result == "PASS"
    assert telemetry.files_changed == ["src/example.py"]


def test_run_validate_phase_records_gate_error_escalation_once(tmp_path: Path) -> None:
    config = dataclasses.replace(
        _make_config(tmp_path),
        validation=dataclasses.replace(_make_config(tmp_path).validation, handoff_file=None),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"

    with patch(
        "theforge.coordinator.validate_phase._run_gate_full",
        return_value=(None, "gate crashed", "traceback tail"),
    ):
        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            dev_calls_this_cycle=1,
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.ESCALATE
    assert result is not None
    assert result.success is False
    assert result.message == "gate crashed"
    assert len(state.dev_iteration_telemetry) == 1
    telemetry = state.dev_iteration_telemetry[0]
    assert telemetry.gate_result == "ERROR"
    assert telemetry.failed_tests == []


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return proc.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
