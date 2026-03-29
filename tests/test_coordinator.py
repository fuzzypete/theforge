"""Tests for the coordinator state machine.

Uses mocked runner to test all state transitions without real agent calls.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from coord_test_helpers import (
    PREFLIGHT_PROCEED_MEDIUM,
    _make_plan_config,
    _shell_with_gate,
)

from theforge.config import (
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
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase, parse_phase_name
from theforge.runners import AgentResult, LogLevel
from theforge.task import TaskStory

# ── Fixtures ─────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    """Create a test config pointing at tmp_path (single reviewer, no synthesis)."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_pool_config(
    tmp_path: Path, profiles: list[ModelProfile], synthesis: ModelProfile | None
) -> ForgeConfig:
    """Create a test config with multi-model review pool."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=profiles,
        synthesis_profile=synthesis,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        log=LogConfig(enabled=False),
    )


def _make_task(tmp_path: Path) -> TaskStory:
    """Create a test task with a real spec file."""
    spec = tmp_path / "spec.md"
    spec.write_text("# Test Spec\n\nImplement the thing.", encoding="utf-8")
    return TaskStory(
        name="Test Task",
        story_path=spec,
        slug="test-task",
    )


def _make_agent_result(
    success: bool = True,
    output: str = "Done.",
    session_id: str | None = "sess-1",
    cost_usd: float | None = 0.50,
    profile_name: str = "",
    structured_data: dict | None = None,
) -> AgentResult:
    return AgentResult(
        success=success,
        output=output,
        session_id=session_id,
        cost_usd=cost_usd,
        exit_code=0 if success else 1,
        raw={},
        profile_name=profile_name,
        structured_data=structured_data,
    )


APPROVE_REVIEW = """\
```yaml
verdict: APPROVE
summary: "Looks good."
findings: []
story_compliance:
  matches_spec: true
test_coverage:
  adequate: true
```
"""

APPROVE_REVIEW_JSON = {
    "verdict": "APPROVE",
    "summary": "Looks good.",
    "findings": [],
    "story_compliance": {"matches_spec": True},
    "test_coverage": {"adequate": True},
}


REQUEST_CHANGES_REVIEW = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Bug found."
findings:
  - severity: P1
    file: src/foo.py
    line: 10
    description: "Off by one"
    suggestion: "Fix it"
story_compliance:
  matches_spec: false
  mismatches:
    - "Missing batch config"
test_coverage:
  adequate: false
  gaps:
    - "No edge case test"
```
"""

PREFLIGHT_PROCEED = """\
```yaml
verdict: PROCEED
reason: "Spec requirements are not yet implemented."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""

_PREFLIGHT_RESULT = _make_agent_result(
    success=True, output=PREFLIGHT_PROCEED, cost_usd=0.05, profile_name="review"
)


# ── Tests ────────────────────────────────────────────────────────────


class TestCoordinatorHybridRunner:
    @pytest.fixture
    def api_profile(self) -> ModelProfile:
        return ModelProfile(
            name="api-reviewer",
            provider="openai",
            model="o4-mini",
            budget_usd=1.0,
            timeout_seconds=120,
            allowed_tools=(),
        )

    @pytest.fixture
    def cli_profile(self) -> ModelProfile:
        return ModelProfile(
            name="cli-reviewer",
            cli="claude",
            provider=None,
            model="sonnet",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Bash"),
        )

    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_mixed_pool_happy_path(
        self, mock_shell, mock_agent, mock_preflight, tmp_path, api_profile, cli_profile
    ):
        """Test a mixed pool of API and CLI reviewers."""
        config = _make_pool_config(tmp_path, [cli_profile, api_profile], None)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        with patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool:
            mock_pool.return_value = [
                _make_agent_result(
                    success=True, output=APPROVE_REVIEW, profile_name="cli-reviewer"
                ),
                _make_agent_result(
                    success=True,
                    output=json.dumps(APPROVE_REVIEW_JSON),
                    profile_name="api-reviewer",
                    structured_data=APPROVE_REVIEW_JSON,
                ),
            ]
            result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Check that parse_review_json was called
        # (implicitly tested by the fact that the merge succeeds)
        assert len(result.state.review_results) == 1
        merged_review = result.state.review_results[0]
        assert merged_review.verdict == "APPROVE"

    @patch("theforge.coordinator.review_pool.build_review_prompt")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_mode_aware_prompt_builder(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_pool,
        mock_prompt_builder,
        tmp_path,
        api_profile,
    ):
        """Test that the correct mode is passed to the prompt builder."""
        config = _make_pool_config(tmp_path, [api_profile], None)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, structured_data=APPROVE_REVIEW_JSON)
        ]

        run_task(config, task)

        mock_prompt_builder.assert_called()
        # The first call to build_review_prompt is what we want to check
        call_args = mock_prompt_builder.call_args_list[0]
        assert call_args.kwargs["mode"] == "api"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_cost_summation_with_none(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Test that total_cost handles None from CLI reviewers."""
        cli_profile = ModelProfile(
            name="cli-reviewer",
            cli="codex",
            provider=None,
            model="o4-mini",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=(),
        )
        config = _make_pool_config(tmp_path, [cli_profile], None)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True, output="Implemented.", cost_usd=None
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, cost_usd=None)
        ]

        result = run_task(config, task)

        assert result.success is True
        # preflight(0.05) + dev(None) + review(None) = 0.05
        assert result.state.total_cost == pytest.approx(0.05)


class TestCoordinatorHappyPath:
    """Test the golden path: dev succeeds, gate passes, review approves."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_single_pass(self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        assert result.state.dev_trace_count == 1
        assert len(result.state.dev_results) == 1
        assert len(result.state.review_results) == 1


class TestCoordinatorEscalation:
    """Test that exhausting retries escalates to human."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_gate_exhaustion(self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "FAIL")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.dev_iteration == 2  # hit max

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_exhaustion(self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "cycles" in result.message.lower() or "exhausted" in result.message.lower()


class TestCoordinatorCostTracking:
    """Test that both dev and review costs are tracked."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_total_cost_includes_review(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        dev_result = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.75,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        review_result = AgentResult(
            success=True,
            output=APPROVE_REVIEW,
            session_id="s2",
            cost_usd=0.90,
            exit_code=0,
            raw={},
            profile_name="review",
        )

        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = dev_result
        mock_pool.return_value = [review_result]

        result = run_task(config, task)

        assert result.success is True
        assert result.state.total_dev_cost == 0.75
        assert result.state.total_review_cost == 0.90
        # total_cost includes preflight ($0.05) + dev + review
        assert result.state.total_cost == pytest.approx(0.05 + 0.75 + 0.90)


def _make_review_profile(name: str, budget_usd: float = 1.0) -> ModelProfile:
    return ModelProfile(
        name=name,
        cli="claude",
        provider=None,
        model="opus",
        budget_usd=budget_usd,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash"),
    )


SYNTHESIS_PROFILE = ModelProfile(
    name="synthesis",
    cli="claude",
    provider=None,
    model="opus",
    budget_usd=1.50,
    timeout_seconds=300,
    allowed_tools=("Read", "Bash"),
)


# ── Structured logging tests ──────────────────────────────────────────


class TestVerboseFlagEnablesToolLines:
    """Tool activity is printed in VERBOSE mode and suppressed in PROGRESS mode."""

    def test_verbose_prints_tool_lines(self, capsys):
        import theforge.runners.runner_claude as runner_claude_mod
        from theforge.log_level import set_log_level

        set_log_level(LogLevel.VERBOSE)
        try:
            # Simulate a tool_use assistant event
            tool_event = (
                '{"type": "assistant", "message": {"content": '
                '[{"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}}]}}'
            )
            runner_claude_mod._process_stream_event(tool_event, "test-label")
            captured = capsys.readouterr()
            assert "↳ Read" in captured.err
        finally:
            set_log_level(LogLevel.PROGRESS)

    def test_progress_suppresses_tool_lines(self, capsys):
        import theforge.runners.runner_claude as runner_claude_mod
        from theforge.log_level import set_log_level

        set_log_level(LogLevel.PROGRESS)
        tool_event = (
            '{"type": "assistant", "message": {"content": '
            '[{"type": "tool_use", "name": "Read", "input": {"file_path": "/foo.py"}}]}}'
        )
        runner_claude_mod._process_stream_event(tool_event, "test-label")
        captured = capsys.readouterr()
        assert "↳ Read" not in captured.err


class TestProgressShowsPhaseTransitions:
    """Phase transition lines always appear at PROGRESS level."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_phase_transitions_always_shown(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path, capsys
    ):
        import theforge.coordinator.util as coord_mod
        from theforge.log_level import set_log_level

        # Ensure we are at PROGRESS level
        coord_mod.set_log_level(LogLevel.PROGRESS)
        set_log_level(LogLevel.PROGRESS)

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_task(config, task)

        captured = capsys.readouterr()
        # Phase transitions must appear even at PROGRESS level
        assert "▸ WORKSPACE" in captured.err
        assert "▸ DEV" in captured.err
        assert "▸ VALIDATE" in captured.err
        assert "▸ REVIEW" in captured.err
        assert "✓ DONE" in captured.err


class TestSprintSpecHeaderPrinted:
    """Sprint emits [N/total] slug header before each spec."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_spec_header_emitted(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path, capsys
    ):
        import yaml as _yaml

        from theforge.sprint import run_sprint

        # Write a minimal forge.yaml
        config = _make_config(tmp_path)

        # Write a spec file with frontmatter
        spec_path = tmp_path / "test-spec.md"
        spec_path.write_text(
            "---\nname: Test Spec\nslug: test-spec\n---\n# Test\n",
            encoding="utf-8",
        )

        # Write a sprint manifest
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            _yaml.dump(
                {
                    "name": "test sprint",
                    "budget_usd": 10.0,
                    "specs": ["test-spec.md"],
                }
            ),
            encoding="utf-8",
        )

        workspace = tmp_path / "test-spec"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        run_sprint(config, manifest_path)

        captured = capsys.readouterr()
        # Header banner for spec [1/1] must appear
        assert "[1/1]" in captured.err
        assert "test-spec" in captured.err


class TestParsePhaseNameUtility:
    """Unit tests for parse_phase_name()."""

    def test_all_valid_names(self):
        expected = {
            "init": Phase.INIT,
            "workspace": Phase.WORKSPACE,
            "preflight": Phase.PREFLIGHT,
            "plan": Phase.PLAN,
            "plan-review": Phase.PLAN_REVIEW,
            "dev": Phase.DEV,
            "validate": Phase.VALIDATE,
            "review": Phase.REVIEW,
            "human-review": Phase.HUMAN_REVIEW,
        }
        for name, phase in expected.items():
            assert parse_phase_name(name) == phase

    def test_case_insensitive(self):
        assert parse_phase_name("DEV") == Phase.DEV
        assert parse_phase_name("Plan-Review") == Phase.PLAN_REVIEW

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown phase name"):
            parse_phase_name("unknown-phase")

    def test_phase_ordering(self):
        """Phase enum values must be ordered INIT < WORKSPACE < ... < REVIEW."""
        phases = [
            Phase.INIT,
            Phase.WORKSPACE,
            Phase.PREFLIGHT,
            Phase.PLAN,
            Phase.PLAN_REVIEW,
            Phase.DEV,
            Phase.VALIDATE,
            Phase.REVIEW,
        ]
        for i in range(len(phases) - 1):
            assert phases[i].value < phases[i + 1].value, (
                f"{phases[i].name}.value ({phases[i].value}) should be < "
                f"{phases[i + 1].name}.value ({phases[i + 1].value})"
            )


class TestUntilPhaseStop:
    """Tests for --until phase stop behaviour."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_until_preflight_stops_after_preflight(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--until preflight: run PREFLIGHT, then stop without entering DEV."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(success=True, output=PREFLIGHT_PROCEED)

        result = run_task(config, task, stop_phase=Phase.PREFLIGHT)

        assert result.success is True
        assert result.message == "Stopped at --until preflight"
        assert result.phase == Phase.PREFLIGHT
        # DEV agent should NOT have been called (only preflight)
        assert result.state.dev_iteration == 0
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_until_validate_stops_after_validate(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--until validate: run DEV+VALIDATE, then stop without REVIEW."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        result = run_task(config, task, stop_phase=Phase.VALIDATE)

        assert result.success is True
        assert result.message == "Stopped at --until validate"
        assert result.phase == Phase.VALIDATE
        assert result.state.dev_trace_count == 1
        # REVIEW should NOT have been called
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_until_review_stops_after_first_review(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--until review: run DEV+VALIDATE+REVIEW, then stop (no retry on REQUEST_CHANGES)."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        # Return REQUEST_CHANGES — without --until, this would retry DEV
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, stop_phase=Phase.REVIEW)

        assert result.success is True
        assert result.message == "Stopped at --until review"
        assert result.phase == Phase.REVIEW
        # Only one DEV call — did not loop back (dev_iteration resets after review)
        assert result.state.dev_trace_count == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_until_plan_stops_before_dev(
        self, mock_shell, mock_dev_agent, mock_plan_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--until plan: run PREFLIGHT+PLAN, then stop before DEV."""
        config = _make_plan_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_plan_agent.return_value = _make_agent_result(
            success=True, output="# Plan\n\n- step 1", cost_usd=0.10
        )

        result = run_task(config, task, stop_phase=Phase.PLAN)

        assert result.success is True
        assert result.phase == Phase.PLAN
        assert "Stopped at --until plan" in result.message
        # DEV must NOT have been invoked
        assert result.state.dev_iteration == 0
        mock_dev_agent.assert_not_called()
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_until_dev_stops_before_review(
        self, mock_shell, mock_dev_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--until dev: run DEV+VALIDATE, then stop before REVIEW."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_dev_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        result = run_task(config, task, stop_phase=Phase.DEV)

        assert result.success is True
        assert result.state.dev_trace_count == 1
        # REVIEW must NOT have been invoked
        mock_pool.assert_not_called()


class TestFromPhaseSkip:
    """Tests for --from phase skip behaviour."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_from_dev_skips_preflight_and_plan(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--from dev: WORKSPACE is reused, PREFLIGHT/PLAN skipped, DEV runs."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        # Put .forge/plan.md so the coordinator doesn't complain
        plan_path = workspace / ".forge" / "plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("# Plan\n- step 1", encoding="utf-8")

        def shell_side(cmd, cwd=None, **kw):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "forge/test-task")
            return _shell_with_gate(workspace, "PASS")(cmd, cwd, **kw)

        mock_shell.side_effect = shell_side
        # Only dev agent and review pool should be called
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, start_phase=Phase.DEV)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Preflight verdict should be SKIPPED
        assert result.state.preflight_verdict == "SKIPPED"
        # run_agent called only once (for dev, not preflight)
        assert mock_agent.call_count == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_from_dev_until_validate_combined(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """--from dev --until validate: DEV→VALIDATE then stop."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_path = workspace / ".forge" / "plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("# Plan\n", encoding="utf-8")

        def shell_side(cmd, cwd=None, **kw):
            if "rev-parse --abbrev-ref HEAD" in cmd:
                return (True, "forge/test-task")
            return _shell_with_gate(workspace, "PASS")(cmd, cwd, **kw)

        mock_shell.side_effect = shell_side
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")

        result = run_task(config, task, start_phase=Phase.DEV, stop_phase=Phase.VALIDATE)

        assert result.success is True
        assert result.message == "Stopped at --until validate"
        assert result.phase == Phase.VALIDATE
        assert result.state.preflight_verdict == "SKIPPED"
        mock_pool.assert_not_called()
