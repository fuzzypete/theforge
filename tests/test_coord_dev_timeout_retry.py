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
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,  # noqa: E402
)

from theforge.config import (  # noqa: E402
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.commit_guard import _has_commits_ahead_of_base  # noqa: E402
from theforge.coordinator.engine import run_task  # noqa: E402
from theforge.coordinator.state import (  # noqa: E402
    CoordinatorResult,
    CoordinatorState,
    Phase,
    RetryReason,
)
from theforge.runners import AgentResult  # noqa: E402
from theforge.task import TaskStory  # noqa: E402

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


def _captured_dev_profile(config, task, state, tmp_path: Path):
    """Run the dev phase and return the profile handed to the runner."""
    from theforge.coordinator.dev_phase import _run_dev_phase

    captured: list = []

    def capture_profile(*, profile, **_kwargs):
        captured.append(profile)
        return _timeout_agent_result()

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=capture_profile)
        )
        stack.enter_context(
            patch("theforge.coordinator.dev_phase.log_agent_result", new=MagicMock())
        )
        _run_dev_phase(state, config, task, "# t\n", tmp_path, "feat/x", notify=False, logger=None)

    return captured[0]


@pytest.mark.parametrize("retry_max", [1, 3, 9])
def test_retry_budget_never_becomes_the_agent_turn_ceiling(tmp_path: Path, retry_max: int) -> None:
    """Retry-attempt policy must not reach the runner's per-invocation ceiling.

    Seam test for #2920: ``retry.max_dev_iterations`` counts phase attempts and
    ``dev_profile.max_iterations`` counts agent turns inside one attempt. An
    unset profile value must stay ``None`` across the coordinator/runner
    boundary so the runner applies its own documented default (75) — never the
    retry count, and never the adaptive value derived from it.
    """
    config, task, state = _setup(tmp_path)
    config = replace(
        config,
        dev_profile=replace(config.dev_profile, max_iterations=None),
        retry=replace(config.retry, max_dev_iterations=retry_max),
    )
    # Whatever the adaptive layer derived for retry/timeout sizing is in
    # attempt units and must not leak into the profile.
    state.adaptive_dev_max = retry_max
    state.budget.max_iterations = retry_max

    profile = _captured_dev_profile(config, task, state, tmp_path)

    assert profile.max_iterations is None


def test_explicit_agent_turn_ceiling_passes_through_and_leaves_retries_alone(
    tmp_path: Path,
) -> None:
    """An operator-set turn ceiling reaches the runner without touching retries."""
    config, task, state = _setup(tmp_path)
    config = replace(
        config,
        dev_profile=replace(config.dev_profile, max_iterations=40),
        retry=replace(config.retry, max_dev_iterations=3),
    )
    state.adaptive_dev_max = 3
    state.budget.max_iterations = 3

    profile = _captured_dev_profile(config, task, state, tmp_path)

    assert profile.max_iterations == 40
    # The other direction of the independence: raising the turn ceiling did not
    # buy the story more dev attempts.
    assert state.budget.max_iterations == 3


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


def test_timeout_retry_preserves_existing_gate_feedback(tmp_path: Path) -> None:
    """A timeout retry keeps prior gate feedback and appends timeout guidance once."""
    config, task, state = _setup(tmp_path)
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)
    state.human_feedback = "Gate output:\nFAIL\nFix formatting before retrying."

    result = _run_dev(config, task, state, tmp_path, _timeout_agent_result())

    assert result is None
    assert state.retry_reason == RetryReason.TIMEOUT_RESUME
    assert state.human_feedback is not None
    assert "Gate output:\nFAIL" in state.human_feedback
    assert "cut off by a timeout" in state.human_feedback
    assert state.human_feedback.count("Additional retry guidance:") == 1


def test_timeout_kill_sets_sticky_state_flag_on_retry_path(tmp_path: Path) -> None:
    """A dev wall-clock timeout sets state.dev_process_timeout_killed at kill time,
    independent of which downstream branch (retry here) runs. This is the signal
    model_profiles_bridge reads to segregate the run as a censored observation."""
    config, task, state = _setup(tmp_path)
    state.budget.max_iterations = 3
    state.budget.consume(review_cycle=0)

    assert state.dev_process_timeout_killed is False
    _run_dev(config, task, state, tmp_path, _timeout_agent_result())

    assert state.dev_process_timeout_killed is True


def test_terminal_timeout_with_checkpointed_work_sets_kill_flag_and_falls_through(
    tmp_path: Path,
) -> None:
    """The exact reported scenario (#1754): a dev iteration is killed at its
    timeout with the budget exhausted AND checkpoint-committed work present, so
    neither the timeout-retry branch nor the zero-commit guard fires and
    execution falls through to VALIDATE. No telemetry entry with is_timeout=True
    is recorded for the killed iteration, yet the sticky state flag must still
    mark the kill so max_killed_timeout_s is populated for this run."""
    config, task, state = _setup(tmp_path)
    state.adaptive_dev_timeout_seconds = 900
    # Exhaust the budget so the timeout-retry branch is skipped (terminal kill).
    state.budget.max_iterations = 1
    state.budget.consume(review_cycle=0)
    # Stranded work in the worktree → checkpoint-commit salvages it, so the run
    # HAS commits ahead of base and the zero-commit guard does not fire. Use a
    # tracked-file edit (an untracked stray file would be quarantined first).
    (tmp_path / "README.md").write_text("seed\nsalvaged work\n", encoding="utf-8")

    result = _run_dev(config, task, state, tmp_path, _timeout_agent_result())

    # Fell through to VALIDATE (no terminal escalation).
    assert result is None
    assert state.phase != Phase.ESCALATE
    # The checkpoint commit put real work ahead of base.
    assert _has_commits_ahead_of_base(tmp_path, "main") is True
    # No dev telemetry entry recorded is_timeout=True on this fall-through path...
    assert not any(t.is_timeout for t in state.dev_iteration_telemetry)
    # ...but the sticky kill flag still marks the wall-clock kill.
    assert state.dev_process_timeout_killed is True

    # And the bridge turns that into a censored-observation RunOutcome with the
    # granted timeout as the kill limit.
    from theforge.coordinator.model_profiles_bridge import build_run_outcome

    outcome = build_run_outcome(config, state, success=False)
    assert outcome.dev_timeout_killed is True
    assert outcome.dev_timeout_limit_s == 900


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


# ── Seam test: timeout retry through the engine loop into a 2nd DEV ──────


def _make_seam_config(tmp_path: Path) -> ForgeConfig:
    dev_profile = ModelProfile(
        name="dev",
        cli="claude",
        model="sonnet",
        budget_usd=30.0,
        timeout_seconds=900,
        timeout_medium_seconds=1200,
        timeout_large_seconds=1800,
        allowed_tools=("Read", "Edit", "Write", "Bash", "Glob", "Grep"),
    )
    review_profile = ModelProfile(
        name="claude-opus",
        cli="claude",
        model="opus",
        budget_usd=10.0,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash", "Glob", "Grep"),
    )
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=dev_profile,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[review_profile],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=3,
            max_review_cycles=2,
            auto_model_escalation=True,
        ),
        models=["claude/sonnet", "claude/opus"],
    )


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_timeout_retry_reenters_dev_through_engine_loop(
    mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
) -> None:
    """Full DEV→(retry)→DEV seam: a dev timeout on the first call re-enters DEV
    directly (skipping VALIDATE) and the second DEV prompt carries the timeout
    text and its limit.

    This is the engine-level regression the unit tests above cannot cover: the
    timeout must flow through run_task's coordinator loop into a *second* DEV
    invocation, not merely be handled at the dev-phase boundary in isolation.
    """
    config = _make_seam_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    # The retry path skips VALIDATE after the timeout, so the gate only runs
    # once — after the second (successful) DEV call.
    mock_shell.side_effect = _shell_with_gate(workspace, ["PASS"])

    dev_calls: list[dict] = []

    def dev_side_effect(**kwargs):
        dev_calls.append({"prompt": kwargs.get("prompt"), "profile": kwargs.get("profile")})
        if len(dev_calls) == 1:
            # First call: killed at its per-iteration timeout (SIGKILL, exit -9).
            return AgentResult(
                success=False,
                output=_TIMEOUT_OUTPUT,
                session_id="sess-1",
                cost_usd=0.10,
                exit_code=-9,
                raw={},
                profile_name="dev",
                failure_code="timeout",
            )
        # Second call: continues from the timeout and succeeds.
        return _make_agent_result(success=True, output="Done.", session_id="sess-1")

    mock_preflight.return_value = _PREFLIGHT_RESULT
    mock_dev.side_effect = dev_side_effect
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="claude-opus")
    ]

    result = run_task(config, task)

    # ── Boundary: the run continues rather than ending on the timeout ──
    assert result.success is True, f"Expected success, got: {result.message}"

    # ── Boundary: exactly two DEV invocations occurred ─────────────────
    assert len(dev_calls) == 2, f"Expected 2 dev calls, got {len(dev_calls)}"

    # ── Boundary: the timeout and its limit reach the 2nd DEV prompt ───
    second_prompt = dev_calls[1]["prompt"] or ""
    assert "TIMEOUT" in second_prompt
    assert "900s" in second_prompt

    # ── Boundary: this was the dev-phase timeout retry, NOT the VALIDATE
    # model-escalation path — no model escalation should have fired. ───
    assert result.state.timeout_escalation_used is False
