"""Tests for the DEV-phase timeout-retry seam.

A dev iteration killed at its per-iteration timeout is a *retryable* failure,
not a terminal escalation. While dev iterations remain, the coordinator must
spend one and re-enter DEV with the timeout and its limit stated in context,
rather than letting the zero-commit guard declare the story terminal with
unused budget (issue #1745). Only once the iteration budget is exhausted does a
timeout escalate — and the recorded error then names the timeout and its limit
rather than the raw signal number.

The empty-diff guard's job (keep a zero-commit run from reaching APPROVE) is
preserved: a terminal timeout with no commits still escalates.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    RetryReason,
)
from theforge.runners import AgentResult
from theforge.task import TaskStory

_TIMEOUT_OUTPUT = "TIMEOUT: Agent exceeded 900s limit"


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
            base_branch="main",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        preflight_fallback_profile=None,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=3, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _timeout_agent_result() -> AgentResult:
    """A dev agent killed at its per-iteration timeout (SIGKILL, exit -9)."""
    return AgentResult(
        success=False,
        output=_TIMEOUT_OUTPUT,
        session_id="sess-abc",
        cost_usd=0.5,
        exit_code=-9,
        raw={},
        profile_name="dev",
        failure_code="timeout",
    )


def _crash_agent_result() -> AgentResult:
    """A dev agent that crashed (non-timeout failure)."""
    return AgentResult(
        success=False,
        output="",
        session_id=None,
        cost_usd=0.0,
        exit_code=-2,
        raw={},
        profile_name="dev",
    )


def _setup(tmp_path: Path) -> tuple[ForgeConfig, TaskStory, CoordinatorState]:
    _init_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "feat/x"], cwd=tmp_path, check=True)
    config = _make_config(tmp_path)
    task = TaskStory(name="t", slug="t", story_path="specs/t.md")
    spec = tmp_path / "specs" / "t.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# t\n", encoding="utf-8")
    state = CoordinatorState()
    state.adaptive_dev_max = 3
    return config, task, state


def _run_dev(config, task, state, tmp_path, agent_result, captured_logs=None):
    from theforge.coordinator.dev_phase import _run_dev_phase

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.run_agent", return_value=agent_result)
        )
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock())
        )
        if captured_logs is not None:
            stack.enter_context(
                patch(
                    "theforge.coordinator.dev_phase._log",
                    new=lambda msg: captured_logs.append(msg),
                )
            )
        return _run_dev_phase(
            state, config, task, "# t\n", tmp_path, "feat/x", notify=False, logger=None
        )


def test_timeout_with_iterations_remaining_retries_not_escalates(tmp_path: Path) -> None:
    """A timeout with budget remaining consumes one iteration and re-enters DEV."""
    config, task, state = _setup(tmp_path)
    # Budget for 3 dev iterations; this is the first — two remain after it.
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)

    result = _run_dev(config, task, state, tmp_path, _timeout_agent_result())

    # Signals the engine loop to re-enter DEV rather than ending the story.
    assert result is None
    assert state.phase != Phase.ESCALATE
    assert state.retry_reason == RetryReason.TIMEOUT_RESUME
    # The timeout and its limit reach the next dev iteration's context.
    assert "TIMEOUT" in (state.human_feedback or "")
    assert "900s" in (state.human_feedback or "")
    # The killed iteration is counted as spent.
    assert len(state.dev_iteration_telemetry) == 1
    assert state.dev_iteration_telemetry[-1].is_timeout is True


def test_timeout_when_iterations_exhausted_escalates_naming_timeout(tmp_path: Path) -> None:
    """A timeout with no budget left escalates, naming the timeout not the signal."""
    config, task, state = _setup(tmp_path)
    # Only one iteration allowed; consuming it exhausts the budget.
    state.budget.max_iterations = 1
    state.budget.consume(review_cycle=0)

    result = _run_dev(config, task, state, tmp_path, _timeout_agent_result())

    assert isinstance(result, CoordinatorResult)
    assert result.success is False
    assert state.phase == Phase.ESCALATE
    # Empty-diff guard still blocks a zero-commit run from reaching APPROVE.
    assert "no commits ahead of base" in (state.error or "")
    # The recorded error names the timeout and its limit, not the signal number.
    assert _TIMEOUT_OUTPUT in (state.error or "")
    assert "exit=-9" not in (state.error or "")


def test_non_timeout_failure_escalates_even_with_iterations_remaining(tmp_path: Path) -> None:
    """Only timeouts retry; a crash with no commits still escalates immediately."""
    config, task, state = _setup(tmp_path)
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)

    result = _run_dev(config, task, state, tmp_path, _crash_agent_result())

    assert isinstance(result, CoordinatorResult)
    assert state.phase == Phase.ESCALATE
    assert state.retry_reason != RetryReason.TIMEOUT_RESUME
    assert "no commits ahead of base" in (state.error or "")
    # A crash carries no runner explanation, so the exit code is the best signal.
    assert "exit=-2" in (state.error or "")


def test_killed_iteration_not_rendered_with_success_glyph(tmp_path: Path) -> None:
    """The DEV phase-line glyph reflects the actual outcome of a killed run."""
    config, task, state = _setup(tmp_path)
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)
    logs: list[str] = []

    _run_dev(config, task, state, tmp_path, _timeout_agent_result(), captured_logs=logs)

    dev_lines = [m for m in logs if "DEV" in m and ("✓" in m or "✗" in m)]
    assert dev_lines, "expected a DEV phase-line to be logged"
    phase_line = next(m for m in dev_lines if "m " in m or "s" in m)
    assert "✓ DEV" not in phase_line
    assert "✗ DEV" in phase_line
