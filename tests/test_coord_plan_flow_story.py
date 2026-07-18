"""Tests for story validation (pre-PLAN quality check)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED_MEDIUM,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    PlanConfig,
    PlanReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task

# ── Local helpers ─────────────────────────────────────────────────────

PREFLIGHT_PROCEED_SMALL = """\
```yaml
verdict: PROCEED
complexity: small
reason: "Small config change needed."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""


def _make_plan_review_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    mode: str = "blocking",
    timeout_seconds: int = 300,
) -> ForgeConfig:
    """Create a test config with PLAN and PLAN_REVIEW enabled."""
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
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300),
        plan_review=PlanReviewConfig(enabled=enabled, mode=mode, timeout_seconds=timeout_seconds),
        log=LogConfig(enabled=False),
    )


# ── TestStoryValidation ───────────────────────────────────────────────


class TestStoryValidation:
    """Tests for spec validation (pre-PLAN quality check)."""

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_spec_validation_pass_continues_to_plan(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        """PASS verdict → run continues to PLAN normally."""
        from theforge.story_validator import StoryValidationResult

        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_validate.return_value = StoryValidationResult(verdict="PASS")
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nThe plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=True)

        assert result.success is True
        mock_validate.assert_called_once()
        assert result.state.story_validation_result is not None
        assert result.state.story_validation_result.verdict == "PASS"
        # Plan still ran
        assert len(result.state.plan_results) == 1

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_spec_validation_warn_logs_and_continues(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_plan_review,
        mock_human_review,
        tmp_path,
        capsys,
    ):
        """WARN verdict → findings logged, run continues to PLAN."""
        from theforge.story_validator import StoryValidationFinding, StoryValidationResult

        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_validate.return_value = StoryValidationResult(
            verdict="WARN",
            findings=[
                StoryValidationFinding(
                    category="requirement",
                    description="AC-3 contradicts Requirement-2",
                    split_suggestion=None,
                )
            ],
        )
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nThe plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=True)

        assert result.success is True
        # Validation ran and returned WARN
        assert result.state.story_validation_result.verdict == "WARN"
        # Run still continued to PLAN
        assert len(result.state.plan_results) == 1
        # Finding was logged
        captured = capsys.readouterr()
        assert "AC-3 contradicts Requirement-2" in captured.err

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_spec_validation_warn_dict_findings_are_coerced(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_plan_review,
        mock_human_review,
        tmp_path,
        capsys,
    ):
        """WARN verdict with dict findings should not crash PLAN logging."""
        from theforge.story_validator import StoryValidationResult

        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_validate.return_value = StoryValidationResult(
            verdict="WARN",
            findings=[
                {
                    "category": "requirement",
                    "description": "Validation produced a plain dict finding",
                    "split_suggestion": None,
                }
            ],
        )
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nThe plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.story_validation_result is not None
        assert result.state.story_validation_result.findings[0].category == "requirement"
        captured = capsys.readouterr()
        assert "Validation produced a plain dict finding" in captured.err

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_spec_validation_skipped_on_plan_injection(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        """validate_story not called when --plan is injected."""
        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Injected plan\n\nUse this.", encoding="utf-8")

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [_make_agent_result(success=True, output="Implemented.")]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True, plan_path=plan_file)

        assert result.success is True
        mock_validate.assert_not_called()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_spec_validation_skipped_for_small_complexity(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_human_review,
        tmp_path,
    ):
        """validate_story not called when preflight complexity is small."""
        config = _make_config(tmp_path)  # plan not enabled → small specs skip plan
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_SMALL, cost_usd=0.05
        )
        mock_agent.side_effect = [_make_agent_result(success=True, output="Implemented.")]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        mock_validate.assert_not_called()

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow._plan_review_interactive")
    @patch("theforge.story_validator.validate_story")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell_detailed")
    def test_spec_validation_warn_scope_appears_in_audit(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_validate,
        mock_plan_review,
        mock_human_review,
        tmp_path,
    ):
        """WARN with scope finding → spec_validation.findings appears in audit log."""
        from theforge.story_validator import StoryValidationFinding, StoryValidationResult

        config = _make_plan_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        split = {
            "stories": [{"name": "Story A", "acs": ["AC1"]}, {"name": "Story B", "acs": ["AC2"]}]
        }
        mock_validate.return_value = StoryValidationResult(
            verdict="WARN",
            cost_usd=0.005,
            findings=[
                StoryValidationFinding(
                    category="scope",
                    description="Spec covers two independent subsystems",
                    split_suggestion=split,
                )
            ],
        )
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nThe plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_plan_review.return_value = "approve"

        result = run_task(config, task, interactive=True)
        audit = generate_audit_log(config, task, result)

        assert result.success is True
        sv = audit["story_validation"]
        assert sv is not None
        assert sv["verdict"] == "WARN"
        assert sv["cost_usd"] == 0.005
        assert len(sv["findings"]) == 1
        f = sv["findings"][0]
        assert f["category"] == "scope"
        assert f["split_suggestion"] == split
