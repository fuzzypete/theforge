"""Tests for automated plan-agent review with regen/escalation logic."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import pytest
from coord_test_helpers import (
    _PREFLIGHT_RESULT,
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
    ModelProfile,
    PlanAgentReviewConfig,
    PlanConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase

# ── Local helpers ─────────────────────────────────────────────────────

PLAN_AGENT_APPROVE = """\
```yaml
verdict: APPROVE
findings: []
```
"""

PLAN_AGENT_REJECT_P1 = """\
```yaml
verdict: REJECT
findings:
  - severity: P1
    description: "Plan references nonexistent function parse_config()"
    suggestion: "Use load_config() from config.py instead"
```
"""

PLAN_AGENT_REJECT_P0 = """\
```yaml
verdict: REJECT
findings:
  - severity: P0
    description: "Plan is architecturally broken — wrong module entirely"
    suggestion: "Rethink the approach"
```
"""


def _make_plan_agent_review_config(tmp_path: Path) -> ForgeConfig:
    """Create a test config with PLAN and plan_agent_review enabled."""
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
        plan_agent_review=PlanAgentReviewConfig(enabled=True, cli="claude", model="sonnet"),
        log=LogConfig(enabled=False),
    )


# ── TestPlanAgentReview ───────────────────────────────────────────────


class TestPlanAgentReview:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_approve(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """Agent returns APPROVE, pipeline continues to DEV."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nGood plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_plan_pool.return_value = [
            _make_agent_result(
                success=True,
                output=PLAN_AGENT_APPROVE,
                cost_usd=0.08,
                profile_name="plan-review",
            )
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_review_mode == "agent"
        assert len(result.state.plan_review_results) == 1
        audit = generate_audit_log(config, task, result)
        assert audit["plan_review"]["reviewer"] == "agent"
        assert audit["plan_review"]["decision"] == "approve"
        assert audit["plan_review"]["cost_usd"] == pytest.approx(0.08)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_p1_blocking_triggers_regen(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """P1 findings block — plan regenerated, second review APPROVE."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # engine.run_agent_pool: plan review calls (reject then approve)
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P1,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.06,
                    profile_name="plan-review",
                )
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_count > 0  # regen triggered by P1
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_output == "# Plan\n\nFixed plan."
        assert len(result.state.plan_results) == 2  # two plan attempts

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_p0_reject_then_approve(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """P0 finding blocks — plan regenerated, second review APPROVE."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # engine.run_agent_pool: plan review calls (reject then approve)
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_count > 0
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_output == "# Plan\n\nFixed plan."
        assert len(result.state.plan_results) == 2
        assert len(result.state.plan_review_results) == 2

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_approve_with_p1_triggers_regen(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """APPROVE verdict carrying a P1 finding must trigger regen.

        Findings drive the verdict, not the reviewer's stated verdict.
        A reviewer saying APPROVE with a P1 must still block — this was
        the core bug where the advisory-downgrade hack allowed P1s through.
        """
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        approve_with_p1 = """\
```yaml
verdict: APPROVE
findings:
  - severity: P1
    description: "startup validation runs before CLI overrides are applied"
    suggestion: "Move run_startup_checks() to after _apply_dev_model_override()"
```
"""
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_plan_pool.side_effect = [
            [_make_agent_result(success=True, output=approve_with_p1, cost_usd=0.08)],
            [_make_agent_result(success=True, output=PLAN_AGENT_APPROVE, cost_usd=0.06)],
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config=config, task=task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_count == 1  # regen triggered despite APPROVE verdict
        assert result.state.plan_output == "# Plan\n\nFixed plan."
        assert len(result.state.plan_results) == 2

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_double_p0_reject_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Two P0 REJECTs, run escalates with findings."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=1
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nStill bad.", cost_usd=0.12),
        ]
        # Two plan review pool calls (both REJECT); code review pool never reached
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "rejected" in result.message.lower()
        assert result.state.plan_regen_count > 0
        # plan review pool called twice; code review never reached
        assert mock_pool.call_count == 2

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_model_escalation_on_repeated_rejection(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """After 2 plan rejections, planner model escalates sonnet→opus; 3rd review approves."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_regen_attempts=3,
                plan_escalation_threshold=2,
            ),
            smart_config_models=["claude/sonnet", "claude/opus"],
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            # initial plan (sonnet)
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            # 1st regen (sonnet — rejection 1, below threshold)
            _make_agent_result(success=True, output="# Plan\n\nStill bad.", cost_usd=0.12),
            # 2nd regen (opus — rejection 2, escalation fires before this call)
            _make_agent_result(success=True, output="# Plan\n\nGood plan.", cost_usd=0.20),
            # dev
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_plan_pool.side_effect = [
            # 1st plan review → REJECT (rejection 1)
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            # 2nd plan review → REJECT (rejection 2, triggers escalation)
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            # 3rd plan review → APPROVE (after escalation)
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.06,
                    profile_name="plan-review",
                )
            ],
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_escalated is True
        assert result.state.plan_regen_count == 2
        assert result.state.plan_escalation_note is not None
        assert "MODEL ESCALATION" in result.state.plan_escalation_note

        # The 3rd run_agent call (index 2) is the 2nd regen — should use opus
        # (call[0]=plan, call[1]=1st regen, call[2]=2nd regen, call[3]=dev;
        # preflight mocked separately)
        regen_call = mock_agent.call_args_list[2]
        regen_profile = regen_call.kwargs.get("profile") or regen_call[1].get("profile")
        assert regen_profile.model == "opus", (
            f"Expected opus model after escalation, got {regen_profile.model}"
        )

        # The regen prompt should contain the escalation note
        regen_prompt = regen_call.kwargs.get("prompt") or regen_call[1].get("prompt")
        assert "MODEL ESCALATION" in regen_prompt

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_disabled_by_default(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """Config without plan_agent_review section — PLAN_REVIEW is skipped."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision is None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_skipped_on_plan_injection(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """`--plan` flag skips agent review."""
        config = _make_plan_agent_review_config(tmp_path)
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
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True, plan_path=plan_file)

        assert result.success is True
        assert result.state.plan_review_decision is None
        assert len(result.state.plan_review_results) == 0

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_parse_failure(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Agent produces garbage — treated as REJECT, escalates after max retries."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=1
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nA plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nBetter plan.", cost_usd=0.12),
        ]
        # Plan review pool returns garbage → parse error → REJECT each time
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output="I think the plan looks okay!",
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output="Still looks fine to me",
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # plan review pool called twice; code review never reached
        assert mock_pool.call_count == 2

    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_agent_review_cost_in_audit(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_human_review,
        tmp_path,
    ):
        """Plan review cost appears in audit log."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nGood plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.25,
                    profile_name="plan-review",
                )
            ],
            [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")],
        ]

        result = run_task(config, task, interactive=True)
        audit = generate_audit_log(config, task, result)

        assert audit["plan_review"]["cost_usd"] == pytest.approx(0.25)
        assert result.state.total_plan_review_cost == pytest.approx(0.25)
        assert result.state.total_cost >= 0.25

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_regen_receives_rejection_findings(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """Regenerated plan prompt includes rejection findings from P0 review."""
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # engine.run_agent_pool: plan review calls (reject then approve)
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        # Plan review via pool; regen at index 1
        # (bad_plan=0, regen=1, dev=2; preflight mocked separately)
        regen_call = mock_agent.call_args_list[1]
        regen_prompt = regen_call.kwargs.get(
            "prompt", regen_call.args[0] if regen_call.args else ""
        )
        assert "Previous Plan Review Findings" in regen_prompt
        assert "architecturally broken" in regen_prompt.lower()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_pool_p0_from_one_reviewer_rejects(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """Pool: P0 from one reviewer + APPROVE from another -> merged REJECT."""
        pool_config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            plan_agent_review=PlanAgentReviewConfig(
                enabled=True,
                pool=[
                    ModelProfile(
                        name="reviewer-a",
                        cli="claude",
                        model="opus",
                        budget_usd=2.00,
                        timeout_seconds=600,
                        allowed_tools=("Read", "Glob", "Grep"),
                    ),
                    ModelProfile(
                        name="reviewer-b",
                        cli="claude",
                        model="sonnet",
                        budget_usd=1.00,
                        timeout_seconds=300,
                        allowed_tools=("Read", "Glob"),
                    ),
                ],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nBad plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # engine.run_agent_pool: plan review (reject then approve)
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P0,
                    cost_usd=0.08,
                    profile_name="reviewer-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.04,
                    profile_name="reviewer-b",
                ),
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.08,
                    profile_name="reviewer-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.04,
                    profile_name="reviewer-b",
                ),
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config=pool_config, task=task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_regen_count == 1
        assert len(result.state.plan_review_results) == 4  # 2 reviewers x 2 rounds

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_pool_all_fail_rejects(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Pool: all reviewers fail (exit_code != 0) -> REJECT."""
        pool_config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=1
            ),
            plan_agent_review=PlanAgentReviewConfig(
                enabled=True,
                pool=[
                    ModelProfile(
                        name="reviewer-a",
                        cli="claude",
                        model="opus",
                        budget_usd=2.00,
                        timeout_seconds=600,
                        allowed_tools=("Read", "Glob"),
                    ),
                    ModelProfile(
                        name="reviewer-b",
                        cli="claude",
                        model="sonnet",
                        budget_usd=1.00,
                        timeout_seconds=300,
                        allowed_tools=("Read", "Glob"),
                    ),
                ],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nA plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nRetried plan.", cost_usd=0.10),
        ]
        # Both reviewers fail each round
        mock_pool.side_effect = [
            [
                _make_agent_result(success=False, output="", profile_name="reviewer-a"),
                _make_agent_result(success=False, output="", profile_name="reviewer-b"),
            ],
            [
                _make_agent_result(success=False, output="", profile_name="reviewer-a"),
                _make_agent_result(success=False, output="", profile_name="reviewer-b"),
            ],
        ]

        result = run_task(config=pool_config, task=task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_plan_review_pool_p1_blocking_triggers_regen(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_human_review,
        mock_code_pool,
        tmp_path,
    ):
        """Pool: P1s from multiple reviewers block — regen triggered, findings attributed."""
        pool_config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            plan_agent_review=PlanAgentReviewConfig(
                enabled=True,
                pool=[
                    ModelProfile(
                        name="reviewer-a",
                        cli="claude",
                        model="opus",
                        budget_usd=2.00,
                        timeout_seconds=600,
                        allowed_tools=("Read", "Glob"),
                    ),
                    ModelProfile(
                        name="reviewer-b",
                        cli="claude",
                        model="sonnet",
                        budget_usd=1.00,
                        timeout_seconds=300,
                        allowed_tools=("Read", "Glob"),
                    ),
                ],
            ),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_plan_agent.side_effect = mock_agent
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nOriginal plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nFixed plan.", cost_usd=0.12),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # engine.run_agent_pool: both reviewers REJECT(P1), then both APPROVE
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P1,
                    cost_usd=0.08,
                    profile_name="reviewer-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P1,
                    cost_usd=0.04,
                    profile_name="reviewer-b",
                ),
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.06,
                    profile_name="reviewer-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.03,
                    profile_name="reviewer-b",
                ),
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config=pool_config, task=task, interactive=True)

        assert result.success is True
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_regen_count > 0  # regen triggered by P1 pool findings
        assert result.state.plan_output == "# Plan\n\nFixed plan."
        assert len(result.state.plan_results) == 2
