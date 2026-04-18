"""Focused tests for VALIDATE phase helpers."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_agent_result, _make_config, _make_task

from theforge.coordinator.state import CoordinatorState, DevIterationTelemetry
from theforge.coordinator.validate_phase import (
    _get_convention_baseline_ref,
    _is_identical_failure,
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
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(3.5)
    state.last_dev_start_commit = "HEAD"
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_alpha.py").write_text(
        "def test_one():\n    pass\n", encoding="utf-8"
    )

    with (
        patch(
            "theforge.coordinator.validate_phase._run_gate_full",
            return_value=("FAIL", None, "FAILED tests/test_alpha.py::test_one", "pytest tests/"),
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
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.RETRY_DEV
    assert result is None
    assert len(state.dev_iteration_telemetry) == 1
    telemetry = state.dev_iteration_telemetry[0]
    assert telemetry.gate_result == "FAIL"
    assert telemetry.failed_tests == ["tests/test_alpha.py::test_one"]
    assert telemetry.existing_test_failures is True


def test_run_validate_phase_records_dirty_pass_iteration_once(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(2.0)
    state.last_dev_start_commit = "HEAD"
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_alpha.py").write_text(
        "def test_one():\n    pass\n", encoding="utf-8"
    )

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
            "theforge.coordinator.validate_phase._run_gate_full",
            return_value=("PASS", None, "OK", "pytest tests/"),
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
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_alpha.py").write_text(
        "def test_one():\n    pass\n", encoding="utf-8"
    )

    with patch(
        "theforge.coordinator.validate_phase._run_gate_full",
        return_value=(None, "gate crashed", "traceback tail", "pytest tests/"),
    ):
        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
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
    assert telemetry.existing_test_failures is False


def test_run_validate_phase_runs_gate_debug_command_on_timeout(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config,
        validation=dataclasses.replace(
            config.validation,
            gate_debug_command="pytest -x -v -n 0",
            gate_debug_timeout=7,
            gate_output_tail_chars=40,
        ),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"

    def shell_side_effect(cmd, cwd, **kwargs):
        if cmd == "pytest -x -v -n 0":
            assert kwargs["timeout"] == 7
            return False, "debug stdout\n" + ("x" * 80) + "\ndebug stderr", 5, False
        return True, "", 0, False

    with (
        patch(
            "theforge.coordinator.validate_phase._run_gate_full",
            return_value=(
                None,
                "Gate timed out after 120s",
                "TIMEOUT after 120s: pytest tests/",
                "pytest tests/",
            ),
        ),
        patch("theforge.coordinator.util._run_shell_detailed", side_effect=shell_side_effect),
    ):
        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.ESCALATE
    assert result is not None
    assert result.success is False
    assert "Gate debug command ran" in result.message
    assert "iterations.gate_debug[-1]" in result.message
    assert "debug stderr" in result.message
    assert len(state.gate_debug_telemetry) == 1
    debug = state.gate_debug_telemetry[0]
    assert debug.ran is True
    assert debug.exit_code == 5
    assert debug.timeout_s == 7
    assert debug.output_truncated is True
    trace = tmp_path / ".forge" / "traces" / "1-gate-debug.txt"
    assert trace.read_text(encoding="utf-8").startswith("debug stdout")


def test_run_validate_phase_timeout_without_gate_debug_command_is_unchanged(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"

    with patch(
        "theforge.coordinator.validate_phase._run_gate_full",
        return_value=(None, "Gate timed out after 120s", "", "pytest tests/"),
    ):
        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.ESCALATE
    assert result is not None
    assert result.message == "Gate timed out after 120s"
    assert state.gate_debug_telemetry == []


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


def test_format_failed_test_feedback_marks_existing_tests(tmp_path: Path) -> None:
    from theforge.coordinator.validate_phase import _format_failed_test_feedback

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_existing.py").write_text(
        "def test_breaks():\n    pass\n", encoding="utf-8"
    )
    feedback, existing = _format_failed_test_feedback(
        "FAILED tests/test_existing.py::test_breaks\nFAILED generated/test_new.py::test_agent",
        tmp_path,
    )

    assert existing is True
    assert "Extracted failing tests (best effort):" in feedback
    assert "- tests/test_existing.py::test_breaks" in feedback
    assert "- generated/test_new.py::test_agent" in feedback
    assert "These are existing tests your changes broke" in feedback


def test_run_validate_phase_retry_feedback_includes_extracted_failures(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.0)
    state.last_dev_start_commit = "HEAD"
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_alpha.py").write_text(
        "def test_one():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "test_new.py").write_text(
        "def test_agent():\n    pass\n", encoding="utf-8"
    )

    with (
        patch(
            "theforge.coordinator.validate_phase._run_gate_full",
            return_value=(
                "FAIL",
                None,
                "FAILED tests/test_existing.py::test_breaks\n"
                "FAILED generated/test_new.py::test_agent",
                "pytest tests/",
            ),
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.RETRY_DEV
    assert result is None
    assert "Extracted failing tests (best effort):" in state.human_feedback
    assert "- tests/test_existing.py::test_breaks" in state.human_feedback
    assert "- generated/test_new.py::test_agent" in state.human_feedback
    assert "These are existing tests your changes broke" in state.human_feedback


def test_format_failed_test_feedback_contract_change_uses_contract_message(
    tmp_path: Path,
) -> None:
    from theforge.coordinator.validate_phase import _format_failed_test_feedback

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_existing.py").write_text(
        "def test_breaks():\n    pass\n", encoding="utf-8"
    )
    feedback, existing = _format_failed_test_feedback(
        "FAILED tests/test_existing.py::test_breaks",
        tmp_path,
        contract_change=True,
    )

    assert existing is True
    assert "Some of these tests may assert the old behavioral contract" in feedback
    assert "These are existing tests your changes broke" not in feedback
    assert "do not edit these test files" not in feedback


def test_format_failed_test_feedback_no_contract_change_uses_default_message(
    tmp_path: Path,
) -> None:
    from theforge.coordinator.validate_phase import _format_failed_test_feedback

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_existing.py").write_text(
        "def test_breaks():\n    pass\n", encoding="utf-8"
    )
    feedback, existing = _format_failed_test_feedback(
        "FAILED tests/test_existing.py::test_breaks",
        tmp_path,
        contract_change=False,
    )

    assert existing is True
    assert "These are existing tests your changes broke" in feedback
    assert "do not edit these test files" in feedback
    assert "old behavioral contract" not in feedback


def _make_telemetry(**kwargs) -> DevIterationTelemetry:
    defaults = dict(
        iteration=1,
        max_iterations=3,
        cost_usd=0.5,
        duration_s=10.0,
        gate_result="FAIL",
        failed_tests=[],
        existing_test_failures=False,
        is_timeout=False,
        files_changed=[],
        files_changed_count=0,
        tests_fixed_count=0,
        meaningful_progress=False,
    )
    defaults.update(kwargs)
    return DevIterationTelemetry(**defaults)


def test_is_identical_failure_returns_false_with_fewer_than_two_entries() -> None:
    assert _is_identical_failure([]) is False
    assert _is_identical_failure([_make_telemetry()]) is False


def test_is_identical_failure_same_failing_tests_escalates() -> None:
    prev = _make_telemetry(failed_tests=["tests/test_a.py::test_one"])
    curr = _make_telemetry(failed_tests=["tests/test_a.py::test_one"])
    assert _is_identical_failure([prev, curr]) is True


def test_is_identical_failure_different_failing_tests_does_not_escalate() -> None:
    prev = _make_telemetry(failed_tests=["tests/test_a.py::test_one"])
    curr = _make_telemetry(failed_tests=["tests/test_b.py::test_two"])
    assert _is_identical_failure([prev, curr]) is False


def test_is_identical_failure_empty_failed_tests_does_not_escalate() -> None:
    prev = _make_telemetry(failed_tests=[])
    curr = _make_telemetry(failed_tests=[])
    assert _is_identical_failure([prev, curr]) is False


def test_is_identical_failure_both_timeout_escalates() -> None:
    prev = _make_telemetry(is_timeout=True, gate_result="ERROR")
    curr = _make_telemetry(is_timeout=True, gate_result="ERROR")
    assert _is_identical_failure([prev, curr]) is True


def test_is_identical_failure_only_one_timeout_does_not_escalate() -> None:
    prev = _make_telemetry(is_timeout=True, gate_result="ERROR")
    curr = _make_telemetry(is_timeout=False, gate_result="FAIL")
    assert _is_identical_failure([prev, curr]) is False


def test_is_identical_failure_checks_last_two_entries() -> None:
    """Circuit breaker compares the final two entries, not just any two."""
    old = _make_telemetry(iteration=1, failed_tests=["tests/test_a.py::test_one"])
    prev = _make_telemetry(iteration=2, failed_tests=["tests/test_b.py::test_two"])
    curr = _make_telemetry(iteration=3, failed_tests=["tests/test_b.py::test_two"])
    assert _is_identical_failure([old, prev, curr]) is True


def test_run_validate_phase_escalates_on_identical_gate_fail(tmp_path: Path) -> None:
    """Circuit breaker escalates when consecutive FAIL iterations have the same test failures."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    # dev_iteration=1 means cycle_count=1 (per-cycle); max_iterations=2 so budget is not yet
    # exhausted, allowing the circuit breaker to fire before the exhaustion check.
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(2.0)
    state.last_dev_start_commit = "HEAD"
    # Seed prior iteration telemetry with identical failing test
    state.dev_iteration_telemetry.append(
        _make_telemetry(
            iteration=1,
            failed_tests=["tests/test_alpha.py::test_one"],
        )
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_alpha.py").write_text(
        "def test_one():\n    pass\n", encoding="utf-8"
    )

    with (
        patch(
            "theforge.coordinator.validate_phase._run_gate_full",
            return_value=(
                "FAIL",
                None,
                "FAILED tests/test_alpha.py::test_one",
                "pytest tests/",
            ),
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
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.ESCALATE
    assert result is not None
    assert result.success is False
    assert "Identical gate failure" in result.message
    assert "Remaining retry budget:" in result.message


def test_run_validate_phase_escalates_on_consecutive_timeouts(tmp_path: Path) -> None:
    """Circuit breaker escalates when two consecutive gate errors are both timeouts."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    # dev_iteration=1 means cycle_count=1 (per-cycle); max_iterations=2 so budget is not yet
    # exhausted, allowing the circuit breaker to fire before the exhaustion check.
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(2.0)
    state.last_dev_start_commit = "HEAD"
    # Seed prior iteration telemetry with a timeout
    state.dev_iteration_telemetry.append(
        _make_telemetry(
            iteration=1,
            gate_result="ERROR",
            is_timeout=True,
        )
    )

    with (
        patch(
            "theforge.coordinator.validate_phase._run_gate_full",
            return_value=(None, "gate timed out after 120s", "", "pytest tests/"),
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        outcome, result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.ESCALATE
    assert result is not None
    assert result.success is False
    assert "Identical gate failure" in result.message
    assert "Remaining retry budget:" in result.message


def test_run_validate_phase_retries_when_failures_differ(tmp_path: Path) -> None:
    """Circuit breaker does not fire when failure signatures change between iterations."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    # dev_iteration=1 means cycle_count=1 (per-cycle); max_iterations=2 so budget not exhausted.
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(2.0)
    state.last_dev_start_commit = "HEAD"
    # Seed prior iteration telemetry with a different failing test
    state.dev_iteration_telemetry.append(
        _make_telemetry(
            iteration=1,
            failed_tests=["tests/test_alpha.py::test_one"],
        )
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_alpha.py").write_text(
        "def test_two():\n    pass\n", encoding="utf-8"
    )

    with (
        patch(
            "theforge.coordinator.validate_phase._run_gate_full",
            return_value=(
                "FAIL",
                None,
                "FAILED tests/test_alpha.py::test_two",
                "pytest tests/",
            ),
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
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.RETRY_DEV
    assert result is None
