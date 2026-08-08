"""Tests for routing hard convention violations after gate PASS.

Covers: TestConventionViolationRouting.
"""

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    _make_agent_result,
    _make_config,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config.types import HardConventionsConfig
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase, RetryReason
from theforge.coordinator.validate_phase import (
    _check_conventions_parallel,
    _run_validate_phase,
    _ValidateOutcome,
)
from theforge.task import TaskStory


def _make_task(tmp_path: Path) -> TaskStory:
    """Create a test task with a real spec file."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
    )


class TestConventionViolationRouting:
    """Hard convention violations after gate PASS stay in review, not DEV retry."""

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_convention_violation_after_passing_gate_is_recorded_as_a_validate_block(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        base_config = _make_config(tmp_path)
        config = dataclasses.replace(
            base_config,
            conventions_hard=HardConventionsConfig(max_module_lines=500),
            retry=base_config.retry.__class__(
                max_dev_iterations=base_config.retry.max_dev_iterations,
                max_review_cycles=2,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace, decisions=["PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.", cost_usd=0.0)

        with patch("theforge.coordinator.engine._run_validate_phase") as mock_validate:
            from theforge.coordinator.validate_phase import _ValidateOutcome

            outcomes = [
                (_ValidateOutcome.RETRY_DEV_NEW_CYCLE, None),
                (
                    _ValidateOutcome.ESCALATE,
                    CoordinatorResult(
                        success=False,
                        phase=Phase.ESCALATE,
                        state=CoordinatorState(phase=Phase.ESCALATE),
                        message="forced stop after synthetic convention review",
                    ),
                ),
            ]

            def _validate(state, *_args, **_kwargs):
                # VALIDATE sets the retry reason before handing the finding back;
                # the engine reads it to pick the synthetic review flavour.
                state.retry_reason = RetryReason.CONVENTION_VIOLATIONS
                return outcomes.pop(0)

            mock_validate.side_effect = _validate
            result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert mock_validate.call_count == 2
        assert mock_dev_prompt.call_count == 2
        mock_fix_prompt.assert_not_called()
        mock_pool.assert_not_called()
        second_validate_state = mock_validate.call_args_list[1].args[0]
        # The coordinator's finding is recorded in its own channel. The reviewer
        # record stays untouched: an entry there means a reviewer pool ran, and
        # per-model attribution, the adaptive review-cycle learner, and the
        # persistent-P1 lookback all rely on that (#1981).
        assert second_validate_state.review_results == []
        assert second_validate_state.review_cycle_metadata == []
        assert second_validate_state.review_iteration_telemetry == []
        assert len(second_validate_state.validate_blocks) == 1
        block = second_validate_state.validate_blocks[0]
        assert block["kind"] == "convention"
        assert block["review_cycle"] == 0
        assert second_validate_state.validate_opened_review_cycles == 1
        assert result.message == "forced stop after synthetic convention review"

    @patch("theforge.coordinator.dev_phase.build_fix_prompt")
    @patch("theforge.coordinator.dev_phase.build_dev_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_convention_block_escalates_when_review_cycles_exhausted(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_dev_prompt,
        mock_fix_prompt,
        tmp_path,
    ):
        """A new-cycle request the review budget cannot fund escalates immediately.

        VALIDATE routes to ESCALATE itself when no cycle remains; this exercises
        the engine's own bound, which keeps the state machine finite no matter
        what a phase returns.
        """
        base_config = _make_config(tmp_path)
        config = dataclasses.replace(
            base_config,
            conventions_hard=HardConventionsConfig(max_module_lines=500),
            retry=base_config.retry.__class__(
                max_dev_iterations=base_config.retry.max_dev_iterations,
                max_review_cycles=1,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_dev_prompt.return_value = "dev prompt"
        mock_fix_prompt.return_value = "fix prompt"
        mock_shell.side_effect = _shell_with_gate(workspace, decisions=["PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.", cost_usd=0.0)

        with patch("theforge.coordinator.engine._run_validate_phase") as mock_validate:
            from theforge.coordinator.validate_phase import _ValidateOutcome

            def _validate(state, *_args, **_kwargs):
                state.retry_reason = RetryReason.CONVENTION_VIOLATIONS
                return (_ValidateOutcome.RETRY_DEV_NEW_CYCLE, None)

            mock_validate.side_effect = _validate
            result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "Convention violations persisted" in result.message
        assert "Max cycles (1) exhausted" in result.message
        # Only one validate call — escalated on the spot
        assert mock_validate.call_count == 1
        # Review pool never invoked
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_convention_violation_escalation_from_validate_is_passed_through(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        tmp_path,
    ):
        base_config = _make_config(tmp_path)
        config = dataclasses.replace(
            base_config,
            conventions_hard=HardConventionsConfig(max_module_lines=500),
            retry=base_config.retry.__class__(
                max_dev_iterations=1,
                max_review_cycles=2,
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, decisions=["PASS"])
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.", cost_usd=0.0)

        with patch("theforge.coordinator.engine._run_validate_phase") as mock_validate:
            from theforge.coordinator.validate_phase import _ValidateOutcome

            mock_validate.return_value = (
                _ValidateOutcome.ESCALATE,
                CoordinatorResult(
                    success=False,
                    phase=Phase.ESCALATE,
                    state=CoordinatorState(phase=Phase.ESCALATE),
                    message=(
                        "Hard convention violations after 1 validation run(s);"
                        " dev iterations and review cycles are both exhausted"
                    ),
                ),
            )
            result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.message == (
            "Hard convention violations after 1 validation run(s);"
            " dev iterations and review cycles are both exhausted"
        )
        assert len(result.state.review_results) == 0


def _violation(rule: str, file: str, detail: str, blocking: bool) -> SimpleNamespace:
    return SimpleNamespace(rule=rule, file=file, detail=detail, blocking=blocking)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _in_process_check_conventions(
    workspace_path: Path, command: str, payload: dict, timeout: int = 120
) -> dict:
    """Run the worktree eval command in-process, exercising the real wire format."""
    from theforge.coordinator._subprocess_eval import _COMMANDS

    return _COMMANDS[command](payload)


class TestModuleSizeRatchetSeam:
    """The two convention channels VALIDATE consumes carry different views.

    ``net_new`` is the ratchet (ADR-0008) and blocks; ``all_violations`` is the
    plain scan and feeds the advisory artifact, which must keep stating distance
    from the *configured* limit even for a module governed by a higher frozen
    ceiling.
    """

    def test_ratchet_violation_blocks_while_advisory_keeps_the_configured_limit(self, tmp_path):
        base_config = _make_config(tmp_path)
        config = dataclasses.replace(
            base_config,
            conventions_hard=HardConventionsConfig(max_module_lines=600),
        )
        task = _make_task(tmp_path)
        state = CoordinatorState(dev_iteration=1)
        state.budget.max_iterations = config.retry.max_dev_iterations
        state.dev_results.append(_make_agent_result())
        state.dev_durations.append(1.0)
        state.last_dev_start_commit = "HEAD"

        module = "src/theforge/sprint/runner.py"
        advisory_scan = [
            _violation(
                "max_module_lines",
                module,
                f"{module} has 7153 lines (limit 600)",
                blocking=False,
            )
        ]
        ratchet = [
            _violation(
                "max_module_lines",
                module,
                f"{module} has 7153 lines and may not exceed 6953 (exceeds it by 200) — "
                "it was already over the 600-line limit at the branch point, so it is "
                "frozen at that size and may not grow (it may shrink freely)",
                blocking=True,
            )
        ]

        def shell_side_effect(cmd, cwd, **kwargs):
            if cmd == "git status --porcelain":
                return True, " M src/theforge/sprint/runner.py"
            if cmd.startswith("git diff --name-only"):
                return True, "src/theforge/sprint/runner.py"
            return True, ""

        with (
            patch(
                "theforge.coordinator.validate_phase.run_gate_full",
                return_value=("PASS", None, "OK", "pytest tests/", 0),
            ),
            patch(
                "theforge.coordinator.validate_phase._get_raw_dev_notes",
                return_value="summary: grew the module",
            ),
            patch("theforge.coordinator.validate_phase._deindex_forge_artifacts"),
            patch("theforge.coordinator.util._run_shell", side_effect=shell_side_effect),
            patch("theforge.coordinator.validate_phase.subprocess.run"),
            patch(
                "theforge.coordinator.validate_phase._check_conventions_parallel",
                return_value=(advisory_scan, ratchet),
            ),
            patch(
                "theforge.coordinator.validate_phase.update_advisory_violations",
                return_value={"path": "advisory.yaml", "entry_count": 1, "newly_filed_issues": []},
            ) as mock_advisory,
        ):
            outcome, result = _run_validate_phase(
                state, config, task, tmp_path, notify=False, logger=None
            )

        # Gate PASSed; the ratchet violation is what refuses the change.
        assert outcome is not _ValidateOutcome.PASS
        assert result is None or result.success is False
        assert state.retry_reason == RetryReason.CONVENTION_VIOLATIONS
        assert module in (state.human_feedback or "")
        assert "may not exceed 6953" in (state.human_feedback or "")
        assert "exceeds it by 200" in (state.human_feedback or "")

        # The advisory channel receives the plain scan, still measured against 600.
        advisory_arg = mock_advisory.call_args.args[1]
        assert advisory_arg == [
            {
                "rule": "max_module_lines",
                "file": module,
                "detail": f"{module} has 7153 lines (limit 600)",
                "blocking": False,
            }
        ]

    def test_missing_baseline_fails_closed_on_oversized_modules(self, tmp_path):
        """With no baseline tree there is no derived ceiling, so the limit is it."""
        config = dataclasses.replace(
            _make_config(tmp_path),
            conventions_hard=HardConventionsConfig(max_module_lines=600),
        )
        _write(tmp_path / "src" / "theforge" / "big.py", "\n" * 900)
        _write(tmp_path / "tests" / "test_big.py", "\n" * 1200)

        with (
            patch(
                "theforge.coordinator.validate_phase._get_convention_baseline_ref",
                return_value=None,
            ),
            patch(
                "theforge.coordinator.util._run_worktree_eval",
                side_effect=_in_process_check_conventions,
            ),
        ):
            all_v, net_v = _check_conventions_parallel(config, tmp_path)

        # Advisory view is untouched — still the non-blocking configured-limit scan.
        assert [v.blocking for v in all_v] == [False, False]
        blocking = {v.file: v.blocking for v in net_v}
        assert blocking["src/theforge/big.py"] is True
        # Test-file size is not part of the module ratchet; it stays advisory.
        assert blocking["tests/test_big.py"] is False
