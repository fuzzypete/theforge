"""Tests for the traces.write_trace helper and coordinator trace behaviour."""

from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.engine import run_task
from theforge.traces import write_trace


def test_write_trace_creates_file(tmp_path):
    target = tmp_path / "traces" / "1-dev-prompt.txt"
    write_trace(target, "hello prompt")
    assert target.read_text(encoding="utf-8") == "hello prompt"


def test_write_trace_creates_parent_dirs(tmp_path):
    target = tmp_path / "a" / "b" / "c" / "trace.txt"
    write_trace(target, "content")
    assert target.exists()


def test_write_trace_overwrites_existing(tmp_path):
    target = tmp_path / "plan.txt"
    write_trace(target, "first")
    write_trace(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_write_trace_failure_logs_warning(tmp_path, capsys):
    # Pass a path whose parent is a file (not a dir) to trigger an error.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    bad_path = blocker / "child.txt"

    # Should not raise — failure is best-effort.
    write_trace(bad_path, "data")

    # Warning should have been emitted via _cu._log (which prints to stderr).
    captured = capsys.readouterr()
    assert "Warning: trace write failed" in captured.err


def test_write_trace_empty_content(tmp_path):
    target = tmp_path / "empty.txt"
    write_trace(target, "")
    assert target.read_text(encoding="utf-8") == ""


# ── Coordinator-level trace tests ─────────────────────────────────────


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch("theforge.coordinator.util._run_shell_detailed")
def test_dev_traces_written_for_iteration_2(
    mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
):
    """After REQUEST_CHANGES, fix-cycle trace must be 2-dev-*.txt, not overwrite 1-dev-*.txt."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _PREFLIGHT_RESULT
    mock_agent.side_effect = [
        _make_agent_result(output="first dev output"),
        _make_agent_result(output="second dev output"),
    ]

    pool_calls = {"n": 0}

    def pool_side_effect(**kwargs):
        pool_calls["n"] += 1
        if pool_calls["n"] == 1:
            return [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ]
        return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

    mock_pool.side_effect = pool_side_effect

    result = run_task(config, task)

    assert result.success is True

    traces_dir = workspace / ".forge" / "traces"
    assert (traces_dir / "1-dev-prompt.txt").exists(), "iteration-1 prompt trace must exist"
    assert (traces_dir / "1-dev-output.txt").exists(), "iteration-1 output trace must exist"
    assert (traces_dir / "2-dev-prompt.txt").exists(), "iteration-2 prompt trace must exist"
    assert (traces_dir / "2-dev-output.txt").exists(), "iteration-2 output trace must exist"

    # The two output traces must contain distinct content (not overwritten)
    out1 = (traces_dir / "1-dev-output.txt").read_text(encoding="utf-8")
    out2 = (traces_dir / "2-dev-output.txt").read_text(encoding="utf-8")
    assert out1 != out2, "iteration-1 and iteration-2 traces must not have the same content"


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch("theforge.coordinator.util._run_shell_detailed")
def test_dev_traces_written_for_iteration_3(
    mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
):
    """After two REQUEST_CHANGES cycles, trace files 1-3 must all exist with distinct content."""
    from dataclasses import replace

    from theforge.config import DEFAULT_REVIEW_PROFILE, RetryPolicy

    base_config = _make_config(tmp_path)
    # Use a high-budget review profile so 3 review calls don't hit the budget cap.
    high_budget_review = replace(DEFAULT_REVIEW_PROFILE, budget_usd=10.00)
    config = replace(
        base_config,
        retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=3),
        review_pool=[high_budget_review],
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _PREFLIGHT_RESULT
    mock_agent.side_effect = [
        _make_agent_result(output="first dev output"),
        _make_agent_result(output="second dev output"),
        _make_agent_result(output="third dev output"),
    ]

    pool_calls = {"n": 0}

    def pool_side_effect(**kwargs):
        pool_calls["n"] += 1
        if pool_calls["n"] == 1:
            return [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ]
        if pool_calls["n"] == 2:
            return [
                _make_agent_result(
                    success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                )
            ]
        return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

    mock_pool.side_effect = pool_side_effect

    result = run_task(config, task)

    assert result.success is True

    traces_dir = workspace / ".forge" / "traces"
    for i in range(1, 4):
        assert (traces_dir / f"{i}-dev-prompt.txt").exists(), (
            f"iteration-{i} prompt trace must exist"
        )
        assert (traces_dir / f"{i}-dev-output.txt").exists(), (
            f"iteration-{i} output trace must exist"
        )

    out1 = (traces_dir / "1-dev-output.txt").read_text(encoding="utf-8")
    out2 = (traces_dir / "2-dev-output.txt").read_text(encoding="utf-8")
    out3 = (traces_dir / "3-dev-output.txt").read_text(encoding="utf-8")
    assert out1 != out2, "iteration-1 and iteration-2 output traces must differ"
    assert out2 != out3, "iteration-2 and iteration-3 output traces must differ"
