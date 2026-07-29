"""Focused tests for VALIDATE phase helpers."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_agent_result, _make_config, _make_task, patch_gate_shell

from theforge.coordinator.gate import format_gate_failure_summary
from theforge.coordinator.state import CoordinatorState, DevIterationTelemetry
from theforge.coordinator.validate_phase import (
    _blocking_finding_route,
    _gate_attempts,
    _gate_trace_path,
    _get_convention_baseline_ref,
    _is_identical_failure,
    _run_validate_phase,
    _ValidateOutcome,
)


def test_gate_attempts_counts_gate_runs_not_dev_iterations() -> None:
    """The attempt count in terminal messages names gate runs performed (#1981)."""
    state = CoordinatorState(dev_iteration=3)
    assert _gate_attempts(state) == 0
    state.gate_runs += 1
    assert _gate_attempts(state) == 1


def test_gate_attempts_ignores_gate_decisions() -> None:
    """Decisions and executions are different counts and must not be conflated.

    ``gate_decisions`` misses timeouts/errors and gains a synthetic entry when
    the gate is skipped, so the operator-facing run count reads the execution
    counter instead (#1984).
    """
    state = CoordinatorState()
    state.gate_decisions.extend(["PASS", "FAIL"])
    assert _gate_attempts(state) == 0
    state.gate_runs = 5
    assert _gate_attempts(state) == 5


def test_record_gate_run_counts_and_persists(tmp_path: Path) -> None:
    """Each execution bumps the counter and lands in the resume sidecar."""
    from theforge.coordinator.run_setup import load_trajectory_state
    from theforge.coordinator.validate_phase import _record_gate_run

    state = CoordinatorState()
    _record_gate_run(state, tmp_path)
    _record_gate_run(state, tmp_path)
    assert state.gate_runs == 2

    restored = CoordinatorState()
    load_trajectory_state(tmp_path, restored)
    assert restored.gate_runs == 2


def test_record_gate_run_survives_an_unwritable_sidecar(tmp_path: Path) -> None:
    """A sidecar write failure degrades the count, it does not fail the run."""
    from theforge.coordinator.validate_phase import _record_gate_run

    state = CoordinatorState()
    with patch(
        "theforge.coordinator.validate_phase.save_trajectory_state",
        side_effect=OSError("read-only filesystem"),
    ):
        _record_gate_run(state, tmp_path)
    assert state.gate_runs == 1


def test_blocking_finding_route_spends_dev_iterations_first(tmp_path: Path) -> None:
    """With dev iterations left, a coordinator-observed finding stays in the cycle."""
    config = _make_config(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations  # 2 → one left

    route = _blocking_finding_route(state, config)
    assert route.outcome is _ValidateOutcome.RETRY_DEV
    assert route.reason == "dev_budget_remains"


def test_blocking_finding_route_buys_a_review_cycle_when_dev_pool_is_spent(
    tmp_path: Path,
) -> None:
    """A spent dev pool escalates the currency to a review cycle, not to failure."""
    config = _make_config(tmp_path)
    state = CoordinatorState(dev_iteration=2)
    state.budget.max_iterations = config.retry.max_dev_iterations  # exhausted

    route = _blocking_finding_route(state, config)
    assert route.outcome is _ValidateOutcome.RETRY_DEV_NEW_CYCLE
    assert route.reason == "review_cycle_bought"


def test_blocking_finding_route_is_terminal_when_both_budgets_are_spent(
    tmp_path: Path,
) -> None:
    """Terminal only after the dev pool and the review-cycle pool are both gone."""
    config = _make_config(tmp_path)
    state = CoordinatorState(dev_iteration=2)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.review_cycle = config.retry.max_review_cycles - 1

    route = _blocking_finding_route(state, config)
    assert route.outcome is _ValidateOutcome.ESCALATE
    assert route.reason == "budgets_exhausted"


def test_blocking_finding_route_never_buys_a_cycle_during_p2_cleanup(tmp_path: Path) -> None:
    """Post-APPROVE P2 cleanup is capped by the existing dev pool and stays terminal."""
    config = _make_config(tmp_path)
    state = CoordinatorState(dev_iteration=2)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.p2_cleanup_active = True

    route = _blocking_finding_route(state, config)
    assert route.outcome is _ValidateOutcome.ESCALATE
    assert route.reason == "p2_cleanup"


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
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=(
                "FAIL",
                None,
                "FAILED tests/test_alpha.py::test_one",
                "pytest tests/",
                1,
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
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("PASS", None, "OK", "pytest tests/", 0),
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
    # The dirty-worktree auto-commit must run exactly once; the new zero-commits
    # guard adds follow-up `git rev-list` calls to the same patched subprocess.run.
    commit_calls = [c for c in commit_run.call_args_list if c.args[0][:2] == ["git", "commit"]]
    assert len(commit_calls) == 1
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
        "theforge.coordinator.validate_phase.run_gate_full",
        return_value=(None, "gate crashed", "traceback tail", "pytest tests/", None),
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
    state.dev_trace_count = 1  # traces are keyed on the monotonic dev counter
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
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=(
                None,
                "Gate timed out after 120s",
                "TIMEOUT after 120s: pytest tests/",
                "pytest tests/",
                None,
            ),
        ),
        patch_gate_shell(side_effect=shell_side_effect),
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
    # The path the escalation quotes is the one the entry records (#1986).
    assert ".forge/traces/1-gate-debug.txt" in result.message
    assert "debug stderr" in result.message
    assert len(state.gate_debug_telemetry) == 1
    debug = state.gate_debug_telemetry[0]
    assert debug.trace_index == 1
    assert debug.trace_path == ".forge/traces/1-gate-debug.txt"
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
        "theforge.coordinator.validate_phase.run_gate_full",
        return_value=(None, "Gate timed out after 120s", "", "pytest tests/", None),
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


def test_run_validate_phase_timeout_with_commits_routes_to_dev_retry(tmp_path: Path) -> None:
    """Gate timeout with dev commits ahead of base → RETRY_DEV with RCA packet."""
    from theforge.coordinator.state import RetryReason

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(2.0)
    state.last_dev_start_commit = "HEAD"

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=(
                None,
                "Gate timed out after 45s",
                "TIMEOUT after 45s: pytest tests/test_hang.py",
                "make gate",
                None,
            ),
        ),
        patch(
            "theforge.coordinator.validate_phase._commits_exist_strict",
            return_value=True,
        ),
        patch(
            "theforge.coordinator.validate_phase._get_handoff_content",
            return_value="summary: implemented all three ACs",
        ),
        patch("theforge.coordinator.validate_phase.subprocess.run") as subproc,
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
        patch_gate_shell(
            return_value=(
                False,
                "tests/test_hang.py::test_deadlock\n"
                "+++++++ Timeout +++++++\n"
                "FAILED tests/test_hang.py::test_deadlock - Failed: Timeout >10.0s",
                1,
                False,
            ),
        ) as diag_shell,
    ):
        # Stub out git rev-parse / git log calls used inside the RCA packet.
        def _fake_run(args, **kwargs):
            class _R:
                returncode = 0
                stdout = b"abc1234\n"

            if "log" in args:
                _R.stdout = b"abc1234 fix: thing\n"
            return _R

        subproc.side_effect = _fake_run
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
    assert state.retry_reason is RetryReason.GATE_FAIL
    assert state.human_feedback is not None
    assert "timed out after" in state.human_feedback
    assert "make gate" in state.human_feedback
    assert "RCA" in state.human_feedback
    assert "TIMEOUT after 45s" in state.human_feedback
    # Diagnostic pass ran and its hanging-test highlight reached the retry packet.
    diag_shell.assert_called_once()
    assert "-n 0" in diag_shell.call_args.args[0]
    assert "--timeout-method=thread" in diag_shell.call_args.args[0]
    assert "tests/test_hang.py::test_deadlock" in state.human_feedback
    assert "Hanging test isolated" in state.human_feedback
    assert len(state.gate_diagnostic_telemetry) == 1
    assert state.gate_diagnostic_telemetry[0].hanging_test == "tests/test_hang.py::test_deadlock"
    # Telemetry recorded the timeout iteration.
    assert len(state.dev_iteration_telemetry) == 1
    assert state.dev_iteration_telemetry[0].is_timeout is True


def test_run_validate_phase_timeout_rca_packet_falls_back_to_stderr(
    tmp_path: Path,
) -> None:
    """Empty stdout tail with diagnostic stderr must still reach the RCA packet."""
    from theforge.coordinator.state import RetryReason

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(2.0)
    state.last_dev_start_commit = "HEAD"

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=(
                None,
                "Gate timed out after 45s: FATAL pytest internal error, exit code 3",
                "",
                "make gate",
                None,
            ),
        ),
        patch(
            "theforge.coordinator.validate_phase._commits_exist_strict",
            return_value=True,
        ),
        patch(
            "theforge.coordinator.validate_phase._get_handoff_content",
            return_value="summary: implemented all three ACs",
        ),
        patch("theforge.coordinator.validate_phase.subprocess.run") as subproc,
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
        patch_gate_shell(
            return_value=(True, "42 passed in 3.1s", 0, False),
        ),
    ):

        def _fake_run(args, **kwargs):
            class _R:
                returncode = 0
                stdout = b"abc1234\n"

            if "log" in args:
                _R.stdout = b"abc1234 fix: thing\n"
            return _R

        subproc.side_effect = _fake_run
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
    assert state.retry_reason is RetryReason.GATE_FAIL
    assert state.human_feedback is not None
    assert "(empty)" not in state.human_feedback
    assert "FATAL pytest internal error, exit code 3" in state.human_feedback


def test_run_validate_phase_timeout_without_commits_still_escalates(tmp_path: Path) -> None:
    """Gate timeout with NO commits ahead of base remains terminal (state 1)."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=(None, "Gate timed out after 45s", "", "make gate", None),
        ),
        patch(
            "theforge.coordinator.validate_phase._commits_exist_strict",
            return_value=False,
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
    assert state.human_feedback is None


def _run_timeout_with_commits(
    tmp_path: Path, config, task, state: CoordinatorState
) -> tuple[_ValidateOutcome, object]:
    """Run VALIDATE for a gate timeout over a worktree that has commits."""
    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=(None, "Gate timed out after 45s", "", "make gate", None),
        ),
        patch(
            "theforge.coordinator.validate_phase._commits_exist_strict",
            return_value=True,
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        return _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            notify=False,
            logger=None,
        )


def test_run_validate_phase_timeout_with_commits_spent_dev_pool_opens_review_cycle(
    tmp_path: Path,
) -> None:
    """A spent dev pool buys a review cycle rather than terminating the story (#1981)."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=2)
    state.budget.max_iterations = config.retry.max_dev_iterations  # 2 → exhausted
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"

    outcome, result = _run_timeout_with_commits(tmp_path, config, task, state)

    assert outcome is _ValidateOutcome.RETRY_DEV_NEW_CYCLE
    assert result is None
    assert state.human_feedback is not None


def test_run_validate_phase_timeout_with_commits_escalates_when_both_budgets_spent(
    tmp_path: Path,
) -> None:
    """Only when dev iterations AND review cycles are gone does a timeout terminate."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=2)
    state.budget.max_iterations = config.retry.max_dev_iterations  # 2 → exhausted
    state.review_cycle = config.retry.max_review_cycles - 1  # last cycle in flight
    # Gate traces are keyed on the monotonic dev counter, not the per-cycle one.
    state.dev_trace_count = 2
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"

    outcome, result = _run_timeout_with_commits(tmp_path, config, task, state)

    assert outcome is _ValidateOutcome.ESCALATE
    assert result is not None
    assert result.success is False


def test_run_validate_phase_already_complete_when_handoff_documents_work_done(
    tmp_path: Path,
) -> None:
    """Empty worktree + handoff with all ACs MET + cited commit on branch → ALREADY_COMPLETE.

    Regression test for the failure mode where a dev cycle correctly determined
    no work was needed (because the fix is already on the base branch) and
    documented this in the handoff YAML, but VALIDATE classified the empty
    worktree as a missing-work failure.
    """
    # Build a real git repo so _has_commits_ahead_of_base returns False
    # (HEAD == base) and _verified_handoff_commit_shas can resolve a real SHA.
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _write(tmp_path / "README.md", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "fix: already-landed work")
    landed_sha = _git(tmp_path, "rev-parse", "HEAD")

    # Write a handoff YAML at the path the coordinator records under
    # state.dev_handoff_snapshots so _latest_forge_handoff_path resolves it.
    handoff_dir = tmp_path / ".forge" / "handoffs" / "issue-1447"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = handoff_dir / "iter_1.yaml"
    handoff_path.write_text(
        "summary: 'The fix for this issue was already implemented and committed'\n"
        "commits:\n"
        f"  - sha: '{landed_sha}'\n"
        "    message: 'fix: already-landed work'\n"
        "acceptance_criteria:\n"
        "  - criterion: 'AC1 text from spec'\n"
        "    status: MET\n"
        "    notes: 'satisfied by existing commit'\n"
        "  - criterion: 'AC2 text from spec'\n"
        "    status: MET\n"
        "    notes: 'satisfied by existing commit'\n"
        "gate_result: PASS\n"
        "story_deviations: []\n"
        "deferred_items: []\n",
        encoding="utf-8",
    )

    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config,
        workspace=dataclasses.replace(config.workspace, base_branch="main"),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.0)
    state.last_dev_start_commit = landed_sha
    state.dev_handoff_snapshots.append(
        {"source": "structured_output", "path": str(handoff_path), "handoff": {}}
    )

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("PASS", None, "OK", "pytest tests/", 0),
        ),
        patch("theforge.coordinator.validate_phase._deindex_forge_artifacts"),
        # _check_conventions_parallel runs in a thread; stub it out so the test
        # doesn't depend on convention configuration.
        patch(
            "theforge.coordinator.validate_phase._check_conventions_parallel",
            return_value=None,
        ),
        # Clean worktree → git status --porcelain returns empty.
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

    assert outcome is _ValidateOutcome.ALREADY_COMPLETE
    assert result is not None
    assert result.success is True
    assert state.validate_already_complete is True
    assert any(c["sha"] == landed_sha for c in state.validate_already_complete_commits)
    assert state.validate_already_complete_reason is not None


def test_run_validate_phase_empty_worktree_without_handoff_still_escalates(
    tmp_path: Path,
) -> None:
    """Empty worktree with no handoff signal still routes to missing-work failure."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _write(tmp_path / "README.md", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config,
        workspace=dataclasses.replace(config.workspace, base_branch="main"),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.0)

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("PASS", None, "OK", "pytest tests/", 0),
        ),
        patch("theforge.coordinator.validate_phase._deindex_forge_artifacts"),
        patch(
            "theforge.coordinator.validate_phase._check_conventions_parallel",
            return_value=None,
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
    assert state.validate_already_complete is False
    assert "missing-work failure" in (result.message or "")


def test_run_validate_phase_empty_worktree_handoff_partial_ac_escalates(
    tmp_path: Path,
) -> None:
    """Handoff with NOT_MET acceptance criterion does not unlock ALREADY_COMPLETE."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _write(tmp_path / "README.md", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    landed_sha = _git(tmp_path, "rev-parse", "HEAD")

    handoff_dir = tmp_path / ".forge" / "handoffs" / "story"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = handoff_dir / "iter_1.yaml"
    handoff_path.write_text(
        "summary: 'Partial'\n"
        "commits:\n"
        f"  - sha: '{landed_sha}'\n"
        "    message: 'base'\n"
        "acceptance_criteria:\n"
        "  - criterion: 'AC1'\n"
        "    status: MET\n"
        "    notes: 'ok'\n"
        "  - criterion: 'AC2'\n"
        "    status: NOT_MET\n"
        "    notes: 'still pending'\n"
        "story_deviations: []\n"
        "deferred_items: []\n",
        encoding="utf-8",
    )

    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config,
        workspace=dataclasses.replace(config.workspace, base_branch="main"),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.0)
    state.dev_handoff_snapshots.append(
        {"source": "structured_output", "path": str(handoff_path), "handoff": {}}
    )

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("PASS", None, "OK", "pytest tests/", 0),
        ),
        patch("theforge.coordinator.validate_phase._deindex_forge_artifacts"),
        patch(
            "theforge.coordinator.validate_phase._check_conventions_parallel",
            return_value=None,
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        outcome, _result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.ESCALATE
    assert state.validate_already_complete is False


def test_run_validate_phase_empty_worktree_handoff_unverifiable_sha_escalates(
    tmp_path: Path,
) -> None:
    """A handoff that cites a SHA not present on the branch must not unlock success."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _write(tmp_path / "README.md", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    handoff_dir = tmp_path / ".forge" / "handoffs" / "story"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = handoff_dir / "iter_1.yaml"
    handoff_path.write_text(
        "summary: 'fabricated'\n"
        "commits:\n"
        "  - sha: 'deadbeefcafebabefeedfacedeadbeefcafebabe'\n"
        "    message: 'fake'\n"
        "acceptance_criteria:\n"
        "  - criterion: 'AC1'\n"
        "    status: MET\n"
        "    notes: 'fabricated'\n"
        "story_deviations: []\n"
        "deferred_items: []\n",
        encoding="utf-8",
    )

    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config,
        workspace=dataclasses.replace(config.workspace, base_branch="main"),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.0)
    state.dev_handoff_snapshots.append(
        {"source": "structured_output", "path": str(handoff_path), "handoff": {}}
    )

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("PASS", None, "OK", "pytest tests/", 0),
        ),
        patch("theforge.coordinator.validate_phase._deindex_forge_artifacts"),
        patch(
            "theforge.coordinator.validate_phase._check_conventions_parallel",
            return_value=None,
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        outcome, _result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.ESCALATE
    assert state.validate_already_complete is False


def test_run_validate_phase_empty_worktree_handoff_off_branch_sha_escalates(
    tmp_path: Path,
) -> None:
    """A handoff citing a real SHA from another branch (not reachable from HEAD or
    base) must not unlock ALREADY_COMPLETE — object existence is insufficient,
    reachability is required."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _write(tmp_path / "README.md", "base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base on main")

    # Create a sibling branch with its own commit, then return to main. The
    # sibling commit is a real object in the repo but is not reachable from
    # HEAD or from main, so it must not satisfy the citation check.
    _git(tmp_path, "checkout", "-b", "other")
    _write(tmp_path / "other.txt", "off-branch\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "off-branch work")
    off_branch_sha = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "main")

    handoff_dir = tmp_path / ".forge" / "handoffs" / "story"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = handoff_dir / "iter_1.yaml"
    handoff_path.write_text(
        "summary: 'cites off-branch sha'\n"
        "commits:\n"
        f"  - sha: '{off_branch_sha}'\n"
        "    message: 'off-branch work'\n"
        "acceptance_criteria:\n"
        "  - criterion: 'AC1'\n"
        "    status: MET\n"
        "    notes: 'claimed'\n"
        "story_deviations: []\n"
        "deferred_items: []\n",
        encoding="utf-8",
    )

    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config,
        workspace=dataclasses.replace(config.workspace, base_branch="main"),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.0)
    state.dev_handoff_snapshots.append(
        {"source": "structured_output", "path": str(handoff_path), "handoff": {}}
    )

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("PASS", None, "OK", "pytest tests/", 0),
        ),
        patch("theforge.coordinator.validate_phase._deindex_forge_artifacts"),
        patch(
            "theforge.coordinator.validate_phase._check_conventions_parallel",
            return_value=None,
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        outcome, _result = _run_validate_phase(
            state,
            config,
            task,
            tmp_path,
            notify=False,
            logger=None,
        )

    assert outcome is _ValidateOutcome.ESCALATE
    assert state.validate_already_complete is False


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
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=(
                "FAIL",
                None,
                "FAILED tests/test_existing.py::test_breaks\n"
                "FAILED generated/test_new.py::test_agent",
                "pytest tests/",
                1,
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


def test_format_failed_test_feedback_unrecognized_format_notes_degradation(
    tmp_path: Path,
) -> None:
    from theforge.coordinator.validate_phase import _format_failed_test_feedback

    xcode_output = (
        "Testing failed:\n"
        "  testBuildWindowPlanKeepsDisplayedCountBoundedByActualWindows()\n"
        "** TEST FAILED **\n"
        "make[2]: *** [test-ios] Error 65\n"
    )
    feedback, existing = _format_failed_test_feedback(xcode_output, tmp_path)

    assert existing is False
    assert "format is not recognized" in feedback
    assert "failed_test_pattern" in feedback


def test_format_failed_test_feedback_custom_pattern_extracts_from_non_pytest(
    tmp_path: Path,
) -> None:
    from theforge.coordinator.validate_phase import _format_failed_test_feedback

    xcode_output = (
        "Test Case '-[ForgeTests testWindowPlanBounded]' failed (0.003 seconds).\n"
        "** TEST FAILED **\n"
    )
    feedback, existing = _format_failed_test_feedback(
        xcode_output,
        tmp_path,
        failed_test_pattern=r"Test Case .*\s(?P<test>\w+)\]' failed",
    )

    assert existing is False  # no source file on disk for the extracted name
    assert "Extracted failing tests (best effort):" in feedback
    assert "- testWindowPlanBounded" in feedback
    assert "format is not recognized" not in feedback


def test_run_validate_phase_unrecognized_gate_format_surfaces_degradation(
    tmp_path: Path,
) -> None:
    """Seam test: a non-pytest gate failure records visibly that extraction did not apply."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.0)
    state.last_dev_start_commit = "HEAD"

    xcode_output = (
        "Testing failed:\n"
        "  testBuildWindowPlanKeepsDisplayedCountBoundedByActualWindows()\n"
        "** TEST FAILED **\n"
        "make[2]: *** [test-ios] Error 65\n"
    )
    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("FAIL", None, xcode_output, "make forge-gate", 65),
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        outcome, result = _run_validate_phase(
            state, config, task, tmp_path, notify=False, logger=None
        )

    assert outcome is _ValidateOutcome.RETRY_DEV
    assert result is None
    assert "format is not recognized" in state.human_feedback
    # The telemetry distinguishes this silent-absence from a genuine no-test-failure.
    assert state.dev_iteration_telemetry[-1].failed_tests == []
    assert state.dev_iteration_telemetry[-1].gate_output_format_recognized is False


def test_run_validate_phase_custom_pattern_extracts_failing_test(tmp_path: Path) -> None:
    """Seam test: a configured failed_test_pattern points the retry at the named test."""
    config = _make_config(tmp_path)
    config = dataclasses.replace(
        config,
        validation=dataclasses.replace(
            config.validation,
            failed_test_pattern=r"^\s*(?P<test>\w+)\(\)$",
        ),
    )
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.0)
    state.last_dev_start_commit = "HEAD"

    xcode_output = (
        "Testing failed:\n"
        "  testBuildWindowPlanKeepsDisplayedCountBoundedByActualWindows()\n"
        "** TEST FAILED **\n"
    )
    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("FAIL", None, xcode_output, "make forge-gate", 65),
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        outcome, result = _run_validate_phase(
            state, config, task, tmp_path, notify=False, logger=None
        )

    assert outcome is _ValidateOutcome.RETRY_DEV
    assert result is None
    extracted = "testBuildWindowPlanKeepsDisplayedCountBoundedByActualWindows"
    assert extracted in state.human_feedback
    assert state.dev_iteration_telemetry[-1].failed_tests == [extracted]
    assert state.dev_iteration_telemetry[-1].gate_output_format_recognized is True


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
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=(
                "FAIL",
                None,
                "FAILED tests/test_alpha.py::test_one",
                "pytest tests/",
                1,
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


def test_format_gate_failure_summary_carries_exit_code_tail_and_trace() -> None:
    """The summary attaches the exit code, output tail, and trace to the verdict phrase."""
    summary = format_gate_failure_summary(
        "Gate returned FAIL after 3 attempts",
        exit_code=65,
        output_tail="** TEST FAILED **\nmake[2]: *** [test-ios] Error 65",
        tail_chars=2000,
        trace_path=".forge/traces/3-gate.txt",
    )
    assert "Gate returned FAIL after 3 attempts (gate exit code 65)" in summary
    assert "Gate output tail (last 2000 chars):" in summary
    assert "** TEST FAILED **" in summary
    assert "make[2]: *** [test-ios] Error 65" in summary
    assert ".forge/traces/3-gate.txt" in summary


def test_format_gate_failure_summary_baseline_without_exit_code_or_trace() -> None:
    """A None exit code / trace (baseline gate, iter_num None) still carries the tail inline."""
    summary = format_gate_failure_summary(
        "Gate returned FAIL after 1 attempts",
        exit_code=None,
        output_tail="",
        tail_chars=2000,
        trace_path=None,
    )
    # No exit-code parenthetical, no trace line, but the outcome is still explicit.
    assert summary == "Gate returned FAIL after 1 attempts\nGate captured no output."


def test_gate_trace_path_none_for_baseline_run() -> None:
    """No trace artifact is named when the gate ran without an iteration number."""
    assert _gate_trace_path(None) is None
    assert _gate_trace_path(2) == ".forge/traces/2-gate.txt"


def test_run_validate_phase_gate_fail_escalation_carries_evidence(tmp_path: Path) -> None:
    """Budget-exhausted gate FAIL escalation carries exit code, output tail, and trace.

    Regression for issue #1736: the terminal outcome recorded only 'Gate
    returned FAIL after N attempts' with no reference to the exit code, output
    tail, or trace the gate already captured, so an operator reading `forge
    status` detail or the escalation issue comment could not tell a compile
    error from a flake from an infrastructure failure.
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=2)
    state.budget.max_iterations = config.retry.max_dev_iterations  # 2 → exhausted
    state.review_cycle = config.retry.max_review_cycles - 1  # last cycle in flight
    # Gate traces are keyed on the monotonic dev counter, not the per-cycle one.
    state.dev_trace_count = 2
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"

    gate_tail = (
        "HealthKitSyncServiceTests.swift:126:10: note: add '@MainActor'\n"
        "Testing failed:\n** TEST FAILED **\nmake[2]: *** [test-ios] Error 65"
    )

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("FAIL", None, gate_tail, "make gate", 65),
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
    # The record consumed by forge status / the escalation comment is state.error.
    assert result.message == state.error
    # Verdict phrase reports gate runs actually performed, not the dev iteration
    # counter that made one un-retried failure read as exhaustion (#1981).
    assert "Gate returned FAIL after 1 gate run(s)" in state.error
    assert "after 2 attempts" not in state.error
    # Exit code travels with the verdict.
    assert "gate exit code 65" in state.error
    # Output tail carried inline — a compile error is identifiable without the run log.
    assert "** TEST FAILED **" in state.error
    assert "make[2]: *** [test-ios] Error 65" in state.error
    # The full-output trace is named as the artifact holding the complete output.
    assert ".forge/traces/2-gate.txt" in state.error


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
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=(None, "gate timed out after 120s", "", "pytest tests/", None),
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
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=(
                "FAIL",
                None,
                "FAILED tests/test_alpha.py::test_two",
                "pytest tests/",
                1,
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


def test_gate_error_run_is_counted_even_though_no_decision_lands(tmp_path: Path) -> None:
    """A gate that errors ran; the count the operator reads must say so (#1984).

    The error path escalates before ``gate_decisions`` is appended, so a count
    derived from that list reported zero runs for a gate that executed.
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"

    with patch(
        "theforge.coordinator.validate_phase.run_gate_full",
        return_value=(None, "gate crashed", "traceback tail", "pytest tests/", None),
    ):
        outcome, _result = _run_validate_phase(
            state, config, task, tmp_path, notify=False, logger=None
        )

    assert outcome is _ValidateOutcome.ESCALATE
    assert state.gate_decisions == []  # no decision came back...
    assert state.gate_runs == 1  # ...but the gate did run
    assert _gate_attempts(state) == 1


def test_gate_fail_escalation_counts_earlier_timeout_runs(tmp_path: Path) -> None:
    """Timeouts that preceded the terminal failure are part of the reported count.

    The #1984 shape: three timed-out gate runs then a FAIL reported "after 1
    gate run(s)" for four executions.
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=2)
    state.budget.max_iterations = config.retry.max_dev_iterations  # exhausted
    state.review_cycle = config.retry.max_review_cycles - 1  # last cycle in flight
    state.dev_trace_count = 2
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"
    state.gate_runs = 3  # three earlier runs that timed out

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("FAIL", None, "FAILED tests/test_alpha.py::test_two", "make gate", 1),
        ),
        patch(
            "theforge.coordinator.validate_phase._get_handoff_content",
            return_value="summary: pending",
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        outcome, result = _run_validate_phase(
            state, config, task, tmp_path, notify=False, logger=None
        )

    assert outcome is _ValidateOutcome.ESCALATE
    assert result is not None
    assert state.gate_runs == 4
    assert "Gate returned FAIL after 4 gate run(s)" in state.error
    # gate_decisions holds one entry for the one decision that came back, which
    # is exactly why it cannot be the run count.
    assert state.gate_decisions == ["FAIL"]


def test_skipped_gate_is_not_counted_as_a_gate_run(tmp_path: Path) -> None:
    """``gate_override: none`` runs no gate, so it contributes no gate run (#1984)."""
    config = _make_config(tmp_path)
    task = dataclasses.replace(_make_task(tmp_path), gate_override="none")
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"

    with (
        patch("theforge.coordinator.validate_phase.run_gate_full") as gate_full,
        patch(
            "theforge.coordinator.validate_phase._get_raw_dev_notes",
            return_value="summary: done",
        ),
        patch("theforge.coordinator.validate_phase._deindex_forge_artifacts"),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
        patch("theforge.coordinator.validate_phase.subprocess.run"),
    ):
        outcome, _result = _run_validate_phase(
            state, config, task, tmp_path, notify=False, logger=None
        )

    assert outcome is _ValidateOutcome.PASS
    gate_full.assert_not_called()
    # The synthetic PASS still records the decision the phase acted on...
    assert state.gate_decisions == ["PASS"]
    # ...but nothing executed, so the run count stays at zero.
    assert state.gate_runs == 0
    assert _gate_attempts(state) == 0


def test_gate_run_count_survives_a_resume(tmp_path: Path) -> None:
    """The count VALIDATE reports crosses ``forge run --resume`` (#1984).

    Seam test for the VALIDATE → resume-restore boundary: the phase writes the
    count to the resume sidecar, and the fresh ``CoordinatorState`` a resume
    entry allocates picks it back up, so a story escalating after a resume does
    not report a count that restarted mid-story.
    """
    from theforge.coordinator.run_setup import load_trajectory_state

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    state = CoordinatorState(dev_iteration=1)
    state.budget.max_iterations = config.retry.max_dev_iterations
    state.dev_results.append(_make_agent_result())
    state.dev_durations.append(1.5)
    state.last_dev_start_commit = "HEAD"

    with patch(
        "theforge.coordinator.validate_phase.run_gate_full",
        return_value=(None, "gate timed out after 600s", "tail", "make gate", None),
    ):
        _run_validate_phase(state, config, task, tmp_path, notify=False, logger=None)

    assert state.gate_runs == 1

    # What a resume entry does: allocate a fresh state, then restore the sidecar.
    resumed = CoordinatorState(dev_iteration=1)
    assert resumed.gate_runs == 0
    load_trajectory_state(tmp_path, resumed)
    assert resumed.gate_runs == 1

    resumed.budget.max_iterations = config.retry.max_dev_iterations
    resumed.review_cycle = config.retry.max_review_cycles - 1
    resumed.dev_results.append(_make_agent_result())
    resumed.dev_durations.append(1.5)
    resumed.last_dev_start_commit = "HEAD"
    resumed.dev_iteration = 2

    with (
        patch(
            "theforge.coordinator.validate_phase.run_gate_full",
            return_value=("FAIL", None, "FAILED tests/test_alpha.py::test_two", "make gate", 1),
        ),
        patch(
            "theforge.coordinator.validate_phase._get_handoff_content",
            return_value="summary: pending",
        ),
        patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
    ):
        outcome, _result = _run_validate_phase(
            resumed, config, task, tmp_path, notify=False, logger=None
        )

    assert outcome is _ValidateOutcome.ESCALATE
    # Pre-resume run plus the post-resume run: the count did not restart at 1.
    assert "Gate returned FAIL after 2 gate run(s)" in resumed.error
