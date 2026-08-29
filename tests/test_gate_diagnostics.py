"""Tests for the stack-specific gate-timeout diagnostic re-run pass (issue #1217)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task, patch_gate_shell

from theforge.coordinator.state import CoordinatorState
from theforge.coordinator.validate_phase import _build_timeout_rca_packet
from theforge.gate_diagnostics import (
    diagnostic_workload_executed,
    extract_hanging_test,
    run_gate_diagnostic_pass,
)


def _diag_config(tmp_path: Path):
    """Config with the diagnostic pass tuned to small, deterministic values."""
    config = _make_config(tmp_path)
    return dataclasses.replace(
        config,
        validation=dataclasses.replace(
            config.validation,
            gate_diagnostic_per_test_timeout=10,
            gate_diagnostic_budget=60,
            gate_output_tail_chars=4000,
        ),
    )


def _stub_git(args, **kwargs):
    """Minimal stub for git rev-parse / git log inside the RCA packet builder."""

    class _R:
        returncode = 0
        stdout = b"abc1234 fix: thing\n" if "log" in args else b"abc1234\n"

    return _R


def test_extract_hanging_test_from_summary_line() -> None:
    output = (
        "tests/test_hang.py::test_deadlock \n"
        "+++++++ Timeout +++++++\n"
        "=== short test summary info ===\n"
        "FAILED tests/test_hang.py::test_deadlock - Failed: Timeout >10.0s"
    )
    assert extract_hanging_test(output) == "tests/test_hang.py::test_deadlock"


def test_extract_hanging_test_from_banner_when_no_summary() -> None:
    output = "tests/test_hang.py::test_spin \n+++++++++ Timeout +++++++++\nStack of MainThread"
    assert extract_hanging_test(output) == "tests/test_hang.py::test_spin"


def test_extract_hanging_test_none_when_multiple_timeouts() -> None:
    """Two distinct timed-out nodes → no single hanging test to highlight."""
    output = (
        "FAILED tests/test_a.py::test_one - Failed: Timeout >10.0s\n"
        "FAILED tests/test_b.py::test_two - Failed: Timeout >10.0s"
    )
    assert extract_hanging_test(output) is None


def test_extract_hanging_test_none_when_clean() -> None:
    assert extract_hanging_test("42 passed in 3.14s") is None


def test_diagnostic_workload_executed_false_when_pytest_rejects_arguments() -> None:
    output = (
        "ERROR: usage: __main__.py [options] [file_or_dir] [file_or_dir] [...]\n"
        "__main__.py: error: unrecognized arguments: --timeout=10 --timeout-method=thread\n"
    )
    assert (
        diagnostic_workload_executed(
            output,
            exit_code=4,
            timed_out=False,
            hanging_test=None,
        )
        is False
    )


def test_diagnostic_pass_hang_reproduces_single_threaded(tmp_path: Path) -> None:
    """Hang reproduces under serialized run: the hanging test node is isolated and highlighted."""
    config = _diag_config(tmp_path)
    task = _make_task(tmp_path)
    diag_output = (
        "tests/test_hang.py::test_deadlock \n"
        "+++++++ Timeout +++++++\n"
        '  File "tests/test_hang.py", line 5, in test_deadlock\n'
        "    lock.acquire()\n"
        "FAILED tests/test_hang.py::test_deadlock - Failed: Timeout >10.0s"
    )
    with patch_gate_shell(
        return_value=(False, diag_output, 1, False),
    ) as shell:
        telemetry = run_gate_diagnostic_pass(config, tmp_path, task=task, iter_num=2)

    assert telemetry is not None
    assert telemetry.ran is True
    assert telemetry.timed_out is False
    assert telemetry.hanging_test == "tests/test_hang.py::test_deadlock"
    assert telemetry.per_test_timeout_s == 10
    assert telemetry.budget_s == 60
    cmd = shell.call_args.args[0]
    assert "-n 0" in cmd
    assert "--timeout=10" in cmd
    assert "--timeout-method=thread" in cmd
    assert shell.call_args.kwargs["timeout"] == 60
    # Diagnostic output is recorded to the audit trace for post-hoc analysis.
    trace = tmp_path / ".forge" / "traces" / "2-gate-diagnostic.txt"
    assert "test_deadlock" in trace.read_text(encoding="utf-8")


def test_diagnostic_pass_hang_does_not_reproduce_single_threaded(tmp_path: Path) -> None:
    """Clean serialized run → no hanging test; packet flags a concurrency-specific bug."""
    config = _diag_config(tmp_path)
    task = _make_task(tmp_path)
    with patch_gate_shell(
        return_value=(True, "128 passed in 12.3s", 0, False),
    ):
        telemetry = run_gate_diagnostic_pass(config, tmp_path, task=task, iter_num=1)

    assert telemetry is not None
    assert telemetry.hanging_test is None
    assert telemetry.timed_out is False
    assert telemetry.exit_code == 0

    state = CoordinatorState(dev_iteration=1)
    with patch(
        "theforge.coordinator.validate_phase.subprocess.run",
        side_effect=_stub_git,
    ):
        packet = _build_timeout_rca_packet(
            state=state,
            config=config,
            gate_cmd="make gate",
            gate_output_tail="TIMEOUT after 45s",
            gate_err="Gate timed out after 45s",
            workspace_path=tmp_path,
            diagnostic=telemetry,
        )
    assert "concurrency-specific" in packet
    assert "Hanging test isolated" not in packet


def test_diagnostic_pass_rejected_before_workload_reports_no_evidence(tmp_path: Path) -> None:
    """Argument parsing failure means the diagnostic did not execute test workload."""
    config = _diag_config(tmp_path)
    task = _make_task(tmp_path)
    output = (
        "ERROR: usage: __main__.py [options] [file_or_dir] [file_or_dir] [...]\n"
        "__main__.py: error: unrecognized arguments: --timeout=10 --timeout-method=thread\n"
    )
    with patch_gate_shell(
        return_value=(False, output, 4, False),
    ):
        telemetry = run_gate_diagnostic_pass(config, tmp_path, task=task, iter_num=4)

    assert telemetry is not None
    assert telemetry.ran is False
    assert telemetry.hanging_test is None
    assert telemetry.timed_out is False
    assert telemetry.exit_code == 4

    state = CoordinatorState(dev_iteration=4)
    with patch(
        "theforge.coordinator.validate_phase.subprocess.run",
        side_effect=_stub_git,
    ):
        packet = _build_timeout_rca_packet(
            state=state,
            config=config,
            gate_cmd="make gate",
            gate_output_tail="TIMEOUT after 45s",
            gate_err="Gate timed out after 45s",
            workspace_path=tmp_path,
            diagnostic=telemetry,
        )
    assert "did not execute test workload" in packet
    assert "Do not infer a concurrency-specific bug from this result." in packet
    assert "This suggests a concurrency-specific bug" not in packet


def test_diagnostic_pass_non_timeout_failure_does_not_claim_concurrency(tmp_path: Path) -> None:
    """A serialized run that fails for another reason is not concurrency evidence."""
    config = _diag_config(tmp_path)
    task = _make_task(tmp_path)
    output = (
        "tests/test_api.py::test_contract FAILED\n"
        "=========================== short test summary info ===========================\n"
        "1 failed, 12 passed in 1.23s\n"
    )
    with patch_gate_shell(
        return_value=(False, output, 1, False),
    ):
        telemetry = run_gate_diagnostic_pass(config, tmp_path, task=task, iter_num=5)

    assert telemetry is not None
    assert telemetry.ran is True
    assert telemetry.hanging_test is None
    assert telemetry.timed_out is False
    assert telemetry.exit_code == 1

    state = CoordinatorState(dev_iteration=5)
    with patch(
        "theforge.coordinator.validate_phase.subprocess.run",
        side_effect=_stub_git,
    ):
        packet = _build_timeout_rca_packet(
            state=state,
            config=config,
            gate_cmd="make gate",
            gate_output_tail="TIMEOUT after 45s",
            gate_err="Gate timed out after 45s",
            workspace_path=tmp_path,
            diagnostic=telemetry,
        )
    assert "exited with code 1" in packet
    assert "This suggests a concurrency-specific bug" not in packet


def test_diagnostic_pass_itself_times_out(tmp_path: Path) -> None:
    """Diagnostic pass exhausts its own budget → recorded as timed_out, packet says so."""
    config = _diag_config(tmp_path)
    task = _make_task(tmp_path)
    with patch_gate_shell(
        return_value=(False, "TIMEOUT after 60s: diagnostic command", None, True),
    ):
        telemetry = run_gate_diagnostic_pass(config, tmp_path, task=task, iter_num=3)

    assert telemetry is not None
    assert telemetry.timed_out is True
    assert telemetry.hanging_test is None
    assert telemetry.exit_code is None

    state = CoordinatorState(dev_iteration=3)
    with patch(
        "theforge.coordinator.validate_phase.subprocess.run",
        side_effect=_stub_git,
    ):
        packet = _build_timeout_rca_packet(
            state=state,
            config=config,
            gate_cmd="make gate",
            gate_output_tail="TIMEOUT after 45s",
            gate_err="Gate timed out after 45s",
            workspace_path=tmp_path,
            diagnostic=telemetry,
        )
    assert "hit its time budget" in packet


def test_diagnostic_pass_honors_operator_command_override(tmp_path: Path) -> None:
    """A configured gate_diagnostic_command overrides the built-in default template."""
    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config,
        validation=dataclasses.replace(
            config.validation,
            gate_diagnostic_command="mytest --isolate --deadline={per_test_timeout} {test_target}",
            gate_diagnostic_per_test_timeout=15,
        ),
    )
    task = _make_task(tmp_path)
    with patch_gate_shell(
        return_value=(True, "ok", 0, False),
    ) as shell:
        telemetry = run_gate_diagnostic_pass(config, tmp_path, task=task, iter_num=1)

    assert telemetry is not None
    cmd = shell.call_args.args[0]
    assert cmd.startswith("mytest --isolate --deadline=15")


def test_diagnostic_pass_disabled_returns_none(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config,
        validation=dataclasses.replace(config.validation, gate_diagnostic_enabled=False),
    )
    task = _make_task(tmp_path)
    with patch_gate_shell() as shell:
        telemetry = run_gate_diagnostic_pass(config, tmp_path, task=task, iter_num=1)
    assert telemetry is None
    shell.assert_not_called()
