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
    patch_gate_shell,
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
from theforge.coordinator.engine import _fresh_run_state, run_task
from theforge.coordinator.plan_flow import _record_plan_model_escalation
from theforge.coordinator.state import CoordinatorState, Phase

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


def _make_plan_agent_review_config(tmp_path: Path, *, dual_reviewer: bool = False) -> ForgeConfig:
    """Create a test config with PLAN and plan_agent_review enabled.

    When ``dual_reviewer=True``, configures two plan review profiles so that
    corroboration can detect cross-reviewer agreement on the same P1.
    """
    if dual_reviewer:
        _plan_review_a = ModelProfile(
            name="plan-review-a",
            cli="claude",
            model="sonnet",
            budget_usd=0.50,
            timeout_seconds=300,
            allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
        )
        _plan_review_b = ModelProfile(
            name="plan-review-b",
            cli="claude",
            model="sonnet",
            budget_usd=0.50,
            timeout_seconds=300,
            allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
        )
        par_config = PlanAgentReviewConfig(
            enabled=True, min_reviewers=2, pool=[_plan_review_a, _plan_review_b]
        )
    else:
        par_config = PlanAgentReviewConfig(enabled=True, cli="claude", model="sonnet")
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
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            # Default this helper to no parse retries so the many gate/recording
            # tests built on it exercise the escalation path in isolation; the
            # parse-retry tests opt in with an explicit positive value.
            max_plan_review_parse_retries=0,
        ),
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300, validate_spec=False),
        plan_agent_review=par_config,
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
    @patch_gate_shell()
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
    @patch_gate_shell()
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
        """Corroborated P1 (2 reviewers) blocks — plan regenerated, second review APPROVE."""
        config = _make_plan_agent_review_config(tmp_path, dual_reviewer=True)
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
        # Two reviewers raise same P1 → corroborated, so it blocks.
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P1,
                    cost_usd=0.08,
                    profile_name="plan-review-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_REJECT_P1,
                    cost_usd=0.08,
                    profile_name="plan-review-b",
                ),
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.06,
                    profile_name="plan-review-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.06,
                    profile_name="plan-review-b",
                ),
            ],
        ]
        # review_pool.run_agent_pool: code review
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_count > 0  # regen triggered by corroborated P1
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_output == "# Plan\n\nFixed plan."
        assert len(result.state.plan_results) == 2  # two plan attempts

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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
        audit = generate_audit_log(config, task, result)
        assert audit["plan_review"]["per_reviewer"] == [
            {
                "attempt": 0,
                "profile": "plan-review",
                "verdict": "REQUEST_CHANGES",
                "cost_usd": pytest.approx(0.08),
            },
            {
                "attempt": 1,
                "profile": "plan-review",
                "verdict": "APPROVE",
                "cost_usd": pytest.approx(0.08),
            },
        ]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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
        """Corroborated P1 (2 reviewers) triggers regen despite APPROVE verdict.

        Findings drive the verdict, not the reviewer's stated verdict.
        When 2 reviewers raise the same P1, corroboration keeps it as P1
        and the plan is rejected for regen.
        """
        config = _make_plan_agent_review_config(tmp_path, dual_reviewer=True)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        approve_with_p1 = """\
```yaml
verdict: APPROVE
findings:
  - severity: P1
    description: "run_startup_checks runs before _apply_dev_model_override is applied"
    suggestion: "Move run_startup_checks() to after _apply_dev_model_override()"
```
"""
        reject_with_p1 = """\
```yaml
verdict: REJECT
findings:
  - severity: P1
    description: "run_startup_checks called before _apply_dev_model_override"
    suggestion: "Reorder to apply overrides first"
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
        # Two reviewers both raise P1 about same issue → corroborated
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=approve_with_p1,
                    cost_usd=0.08,
                    profile_name="plan-review-a",
                ),
                _make_agent_result(
                    success=True,
                    output=reject_with_p1,
                    cost_usd=0.08,
                    profile_name="plan-review-b",
                ),
            ],
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.06,
                    profile_name="plan-review-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.06,
                    profile_name="plan-review-b",
                ),
            ],
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config=config, task=task, interactive=True)

        assert result.success is True
        assert result.state.plan_regen_count == 1  # regen triggered by corroborated P1
        assert result.state.plan_output == "# Plan\n\nFixed plan."
        assert len(result.state.plan_results) == 2

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_decompose_signal_escalates_before_dev(
        self, mock_shell, mock_dev, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Planner signals decompose → coordinator escalates before PLAN_REVIEW/DEV.

        Seam-level: verifies the PLAN → (PLAN_REVIEW/DEV) boundary. When the plan
        agent emits ``decompose: true``, the coordinator must stop at PLAN and
        escalate for manual splitting — never invoking the plan-review pool or the
        dev phase, so no dev budget is spent on an oversized story.
        """
        config = _make_plan_agent_review_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        _decompose_plan = (
            "plan:\n"
            "  decompose: true\n"
            '  decompose_reason: "Bundles an auth refactor and an unrelated API redesign."\n'
        )
        mock_plan_agent.return_value = _make_agent_result(
            success=True, output=_decompose_plan, cost_usd=0.10
        )

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.escalate_kind == "decompose"
        assert "DECOMPOSE_NEEDED" in result.message
        assert "auth refactor" in result.state.escalate_reason
        # Never reached plan review or dev — no auto-split, no dev budget spent.
        assert mock_pool.call_count == 0
        assert mock_dev.call_count == 0
        assert result.state.plan_structured is not None
        assert result.state.plan_structured.get("decompose") is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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
            models=["claude/sonnet", "claude/opus"],
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

        # Escalation must be recorded in the canonical routing_decision block
        # (ADR-0006 clause 7 / #1391), not only in a telemetry log line.
        assert isinstance(result.state.routing_decision, dict)
        planner_block = result.state.routing_decision.get("planner")
        assert isinstance(planner_block, dict)
        escalation = planner_block.get("plan_model_escalation")
        assert isinstance(escalation, dict)
        assert escalation["mechanism"] == "plan_model_escalation"
        assert escalation["fired"] is True
        # Clause-5 symmetry markers: story-scoped with a named return path.
        assert escalation["scope"] == "run"
        assert escalation["return_path"] == "fresh_run_state_reset"
        # Consecutive-rejection signal and the concrete planner model swap.
        assert escalation["signal"]["kind"] == "consecutive_plan_rejections"
        assert escalation["signal"]["rejections"] == 2
        assert escalation["model_swap"]["from_model"] == "sonnet"
        assert escalation["model_swap"]["to_model"] == "opus"

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

    def test_plan_escalation_does_not_persist_into_next_story(self):
        """Planner escalation is story-scoped (ADR-0006 clause 5 return path).

        A story that escalated its planner leaves run-scoped state
        (``plan_escalated``/``plan_escalation_note``) and the recorded
        ``routing_decision`` escalation block set. The next story is a freshly
        constructed ``CoordinatorState`` (``_fresh_run_state``), which resets all
        of those to their defaults — so the escalated planner tier never ratchets
        forward into the next story's planner assignment.
        """
        # Story 1: simulate an escalation having fired.
        story1 = CoordinatorState()
        story1.plan_regen_count = 2
        story1.plan_escalated = True
        story1.plan_escalation_note = "MODEL ESCALATION: rejected 2 time(s)."
        _record_plan_model_escalation(
            story1,
            previous_model="sonnet",
            escalated_model="opus",
            rejections=2,
            findings="P0: insufficient plan",
        )
        assert story1.plan_escalated is True
        assert story1.routing_decision["planner"]["plan_model_escalation"]["fired"] is True

        # Story 2: the return path is a fresh per-run state container. The escalated
        # planner tier does not carry over — the next story starts from defaults.
        story2 = _fresh_run_state()
        assert story2.plan_escalated is False
        assert story2.plan_escalation_note is None
        assert story2.plan_regen_count == 0
        assert story2.routing_decision is None

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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
    @patch_gate_shell()
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
    @patch_gate_shell()
    def test_plan_agent_review_parse_failure(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Agent produces garbage — all reviewers fail → escalate immediately (no regen)."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_regen_attempts=1,
                max_plan_review_parse_retries=0,
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
        ]
        # Plan review pool returns garbage → parse error → all reviewers fail → immediate escalate
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output="I think the plan looks okay!",
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Pool called once; escalated immediately — no regen attempted
        assert mock_pool.call_count == 1
        assert result.state.plan_review_decision == "reject"
        assert len(result.state.plan_review_failures) == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_agent_review_cost_in_audit(
        self,
        mock_shell,
        mock_agent,
        mock_preflight,
        mock_plan_agent,
        mock_pool,
        mock_human_review,
        mock_code_review_pool,
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
        ]
        mock_code_review_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
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
    @patch_gate_shell()
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
        assert "Reviewer Findings" in regen_prompt
        assert "architecturally broken" in regen_prompt.lower()
        # Regen prompt must include the planner's own previous plan
        assert "Bad plan." in regen_prompt

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
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
                min_reviewers=1,
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
    @patch_gate_shell()
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
    @patch_gate_shell()
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

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_plan_agent_review_backtrack_allowed_when_max_regen_is_one(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """With max_plan_regen_attempts=1 and recurring P0 theme, backtrack regen runs.

        Three consecutive P0 rejections share the snake_case anchor 'parse_config',
        so classify_disposition returns 'backtrack' after the second rejection.
        The fix ensures the backtrack attempt is NOT gated by the
        max_plan_regen_attempts ceiling, so the review pool is called a third time
        (for the backtrack regen output) before the run finally escalates.

        P0 severity is used (not P1) so the finding blocks on a single reviewer
        without requiring corroboration.

        Bug: without the fix, pool is only called twice — the ceiling check fires on
        the second rejection and escalates before the backtrack regen is dispatched.
        """
        # P0 with a snake_case anchor so extract_finding_themes produces an
        # overlapping non-file-path theme ('parse_config') across all three cycles.
        reject_p0_with_theme = """\
```yaml
verdict: REJECT
findings:
  - severity: P0
    description: "Plan calls parse_config() which does not exist"
    suggestion: "Replace parse_config with load_config throughout the plan"
```
"""
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
        # Three plan outputs: initial plan, patch regen, backtrack regen
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nInitial plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nPatch attempt.", cost_usd=0.12),
            _make_agent_result(success=True, output="# Plan\n\nBacktrack attempt.", cost_usd=0.12),
        ]
        # Three review rounds all REJECT P0 with the same snake_case theme.
        # Repeated parse_config across attempts → _has_sufficient_overlap returns
        # True across cycles, so classify_disposition returns "backtrack" after the
        # second rejection (and "escalate" after the third).
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=reject_p0_with_theme,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output=reject_p0_with_theme,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
            [
                _make_agent_result(
                    success=True,
                    output=reject_p0_with_theme,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Backtrack review ran — pool called 3 times; if bug present, only 2
        assert mock_pool.call_count == 3
        assert result.state.plan_backtrack_used is True
        assert result.state.plan_regen_count >= 2

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_early_backtrack_does_not_consume_patch_slot(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Backtrack regen dispatched before the ceiling is hit must not consume a patch slot.

        Scenario: max_plan_regen_attempts=2.  The first two rejections share the
        'parse_config' theme, triggering 'backtrack' at iteration 1 (before the
        ceiling).  After the backtrack the reviewer switches to a different theme
        ('validate_schema') so the disposition goes back to 'patch' — the early
        escalation guard (disposition=='escalate') stays silent and only the ceiling
        check governs exit.

          iter 0: REJECT parse_config  → regen_count=1, dispose="patch"  → patch regen
          iter 1: REJECT parse_config  → regen_count=2, dispose="backtrack" → backtrack regen
          iter 2: REJECT validate_schema → regen_count=3, dispose="patch"
                  effective=3-1=2 > 2? No (fix!) → patch regen
                  BUG: without fix, 3 > 2 → ESCALATE (only 1 patch after backtrack)
          iter 3: REJECT validate_schema → regen_count=4, backtrack used
                  effective=4-1=3 > 2 → ESCALATE

        Pool is therefore called 4 times with the fix (3 without).
        """
        reject_parse_config = """\
```yaml
verdict: REJECT
findings:
  - severity: P0
    description: "Plan calls parse_config() which does not exist"
    suggestion: "Replace parse_config with load_config throughout the plan"
```
"""
        reject_validate_schema = """\
```yaml
verdict: REJECT
findings:
  - severity: P0
    description: "Plan calls validate_schema() which is not defined"
    suggestion: "Remove validate_schema or define it in the plan"
```
"""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2, max_review_cycles=2, max_plan_regen_attempts=2
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
        # 4 plan outputs: initial + patch-1 + backtrack + patch-2
        # (escalation fires during iter-3 review before a 5th regen is needed)
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nInitial.", cost_usd=0.10),
            _make_agent_result(success=True, output="# Plan\n\nPatch-1.", cost_usd=0.12),
            _make_agent_result(success=True, output="# Plan\n\nBacktrack.", cost_usd=0.12),
            _make_agent_result(success=True, output="# Plan\n\nPatch-2.", cost_usd=0.12),
        ]

        # iter 0-1: parse_config theme  →  'backtrack' at iter 1
        # iter 2-3: validate_schema theme (no overlap with parse_config)  →  'patch' at iter 2,
        #           'backtrack' at iter 3 (validate_schema now overlaps, but backtrack used)
        def _r(output: str) -> list:
            return [
                _make_agent_result(
                    success=True, output=output, cost_usd=0.08, profile_name="plan-review"
                )
            ]

        mock_pool.side_effect = [
            _r(reject_parse_config),
            _r(reject_parse_config),
            _r(reject_validate_schema),
            _r(reject_validate_schema),
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # 4 pool calls with fix; without fix only 3 (escalates at iter 2 ceiling)
        assert mock_pool.call_count == 4
        assert result.state.plan_backtrack_used is True


# ── TestPlanReviewerFailureAudit ──────────────────────────────────────


class TestPlanReviewerFailureAudit:
    """Tests for per-reviewer failure recording and minimum-success escalation."""

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_single_reviewer_empty_output_recorded_in_failures(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Single reviewer returns empty output → failure recorded in plan_review_failures."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_regen_attempts=0,
                max_plan_review_parse_retries=0,
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
        ]
        # Reviewer returns empty output → parse error
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output="",
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ],
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert len(result.state.plan_review_failures) == 1
        failure = result.state.plan_review_failures[0]
        assert failure["attempt"] == 0
        assert failure["reviewer"] == "plan-review"
        assert len(failure["errors"]) > 0

        audit = generate_audit_log(config, task, result)
        assert audit["plan_review"]["reviewer_failures"] == result.state.plan_review_failures

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_all_reviewers_fail_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """All reviewers fail on first attempt → escalation with plan_review_decision='reject'."""
        pool_config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_regen_attempts=0,
                max_plan_review_parse_retries=0,
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
        ]
        # Both reviewers return unparseable output
        mock_pool.side_effect = [
            [
                _make_agent_result(
                    success=True, output="looks good to me!", profile_name="reviewer-a"
                ),
                _make_agent_result(success=True, output="seems fine!", profile_name="reviewer-b"),
            ]
        ]

        result = run_task(config=pool_config, task=task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.plan_review_decision == "reject"
        assert len(result.state.plan_review_failures) == 2
        reviewer_names = {f["reviewer"] for f in result.state.plan_review_failures}
        assert reviewer_names == {"reviewer-a", "reviewer-b"}

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_min_reviewers_two_one_failure_escalates(
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
        """Two reviewers configured with min_reviewers=2 → one failure escalates."""
        pool_config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            plan_agent_review=PlanAgentReviewConfig(
                enabled=True,
                min_reviewers=2,
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
            _make_agent_result(success=True, output="Implemented."),
        ]
        # reviewer-a fails (garbage), reviewer-b approves, but min_reviewers=2 blocks approval
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output="looks good I think",
                    cost_usd=0.08,
                    profile_name="reviewer-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.04,
                    profile_name="reviewer-b",
                ),
            ]
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config=pool_config, task=task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.plan_review_decision == "reject"
        assert "minimum required is 2" in (result.message or "")
        # Failure was recorded for reviewer-a
        assert len(result.state.plan_review_failures) == 1
        assert result.state.plan_review_failures[0]["reviewer"] == "reviewer-a"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_transient_plan_review_failure_retries_and_preserves_pool(
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
        """A transient 500 is retried before min_reviewers gating shrinks the pool."""
        pool_config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_review_transport_retries=2,
            ),
            plan_agent_review=PlanAgentReviewConfig(
                enabled=True,
                min_reviewers=2,
                pool=[
                    ModelProfile(
                        name="reviewer-a",
                        provider="google",
                        model="gemini-2.5-pro",
                        budget_usd=2.00,
                        timeout_seconds=600,
                        allowed_tools=("Read", "Glob"),
                    ),
                    ModelProfile(
                        name="reviewer-b",
                        provider="deepseek",
                        model="deepseek-chat",
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
            _make_agent_result(
                success=True,
                output=PLAN_AGENT_APPROVE,
                cost_usd=0.08,
                profile_name="reviewer-a",
            ),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_plan_pool.return_value = [
            _make_agent_result(
                success=False,
                output="google.genai.errors.ServerError: 500 INTERNAL",
                cost_usd=0.08,
                profile_name="reviewer-a",
            ),
            _make_agent_result(
                success=True,
                output=PLAN_AGENT_APPROVE,
                cost_usd=0.04,
                profile_name="reviewer-b",
            ),
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config=pool_config, task=task, interactive=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.plan_review_decision == "approve"
        assert result.state.plan_review_failures == []
        assert len(result.state.plan_review_transport_retries) == 1
        assert len(result.state.plan_review_results) == 3
        assert result.state.total_plan_review_cost == pytest.approx(0.20)
        retry = result.state.plan_review_transport_retries[0]
        assert retry["reviewer"] == "reviewer-a"
        assert retry["retry"] == 1

        audit = generate_audit_log(pool_config, task, result)
        assert (
            audit["plan_review"]["transport_retries"] == result.state.plan_review_transport_retries
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_transient_plan_review_failure_exhausts_retries_then_escalates(
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
        """Transient transport failures only shrink the pool after retry exhaustion."""
        pool_config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_review_transport_retries=2,
            ),
            plan_agent_review=PlanAgentReviewConfig(
                enabled=True,
                min_reviewers=2,
                pool=[
                    ModelProfile(
                        name="reviewer-a",
                        provider="google",
                        model="gemini-2.5-pro",
                        budget_usd=2.00,
                        timeout_seconds=600,
                        allowed_tools=("Read", "Glob"),
                    ),
                    ModelProfile(
                        name="reviewer-b",
                        provider="deepseek",
                        model="deepseek-chat",
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
            _make_agent_result(
                success=False,
                output="500 INTERNAL provider failure",
                cost_usd=0.03,
                profile_name="reviewer-a",
            ),
            _make_agent_result(
                success=False,
                output="connection reset by peer",
                cost_usd=0.03,
                profile_name="reviewer-a",
            ),
        ]
        mock_plan_pool.return_value = [
            _make_agent_result(
                success=False,
                output="google.genai.errors.ServerError: 500 INTERNAL",
                cost_usd=0.08,
                profile_name="reviewer-a",
            ),
            _make_agent_result(
                success=True,
                output=PLAN_AGENT_APPROVE,
                cost_usd=0.04,
                profile_name="reviewer-b",
            ),
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config=pool_config, task=task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # #1951: a reviewer lost to the transport cast no vote. The pool falling
        # below the minimum is a statement about the substrate, so the run ends
        # as an infrastructure abort — NOT as a plan-review rejection, which
        # would record a verdict no reviewer produced.
        assert result.infrastructure_failure is True
        assert result.state.plan_review_decision is None
        assert result.state.infrastructure_failure is not None
        assert result.state.infrastructure_failure["phase"] == "PLAN_REVIEW"
        assert result.state.infrastructure_failure["category"] == "transport"
        assert "not a plan rejection" in (result.message or "")
        # The pool loss survives as first-class metadata.
        assert result.state.degraded_pools[-1]["lost"] == ["reviewer-a"]
        assert result.state.degraded_pools[-1]["pool_size"] == 2
        _failed_profiles = [f["profile_name"] for f in result.state.agent_invocation_failures]
        assert _failed_profiles == ["reviewer-a"]
        assert len(result.state.plan_review_transport_retries) == 2
        assert len(result.state.plan_review_results) == 4
        assert result.state.total_plan_review_cost == pytest.approx(0.18)
        assert len(result.state.plan_review_failures) == 1
        failure = result.state.plan_review_failures[0]
        assert failure["reviewer"] == "reviewer-a"
        assert failure["failure_kind"] == "transport"
        assert failure["retryable"] is True
        assert failure["retry_count"] == 2
        assert failure["produced_model_output"] is False
        assert "Transport failure:" in failure["errors"][0]
        audit = generate_audit_log(pool_config, task, result)
        assert audit["plan_review"]["per_reviewer"] == [
            {
                "attempt": 0,
                "profile": "reviewer-a",
                "verdict": "CRASHED",
                "cost_usd": pytest.approx(0.03),
            },
            {
                "attempt": 0,
                "profile": "reviewer-b",
                "verdict": "APPROVE",
                "cost_usd": pytest.approx(0.04),
            },
        ]

    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_non_transient_plan_review_failure_is_not_labeled_transport(
        self, mock_shell, mock_agent, mock_preflight, mock_plan_agent, mock_pool, tmp_path
    ):
        """Non-retryable reviewer failures retain a non-transport failure kind."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_regen_attempts=0,
                max_plan_review_transport_retries=2,
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
        ]
        mock_pool.return_value = [
            _make_agent_result(
                success=False,
                output="authentication failed",
                cost_usd=0.08,
                profile_name="plan-review",
                session_id=None,
            )
        ]
        mock_pool.return_value[0] = dataclasses.replace(
            mock_pool.return_value[0], failure_code="auth_error"
        )

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.plan_review_transport_retries == []
        assert len(result.state.plan_review_failures) == 1
        failure = result.state.plan_review_failures[0]
        assert failure["reviewer"] == "plan-review"
        assert failure["failure_kind"] == "auth_error"
        assert failure["retryable"] is False
        assert failure["retry_count"] == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_reviewer_failures_appear_in_audit(
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
        """Audit log includes reviewer_failures when failures exist."""
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
            _make_agent_result(success=True, output="# Plan\n\nA plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # reviewer-a fails, reviewer-b approves
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output="unparseable garbage",
                    cost_usd=0.08,
                    profile_name="reviewer-a",
                ),
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.04,
                    profile_name="reviewer-b",
                ),
            ]
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config=pool_config, task=task, interactive=True)
        audit = generate_audit_log(pool_config, task, result)

        assert result.success is True
        assert "reviewer_failures" in audit["plan_review"]
        assert audit["plan_review"]["per_reviewer"] == [
            {
                "attempt": 0,
                "profile": "reviewer-a",
                "verdict": "PARSE_ERROR",
                "cost_usd": pytest.approx(0.08),
            },
            {
                "attempt": 0,
                "profile": "reviewer-b",
                "verdict": "APPROVE",
                "cost_usd": pytest.approx(0.04),
            },
        ]
        failures = audit["plan_review"]["reviewer_failures"]
        assert len(failures) == 1
        assert failures[0]["reviewer"] == "reviewer-a"
        assert len(failures[0]["errors"]) > 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_no_failures_audit_omits_reviewer_failures_key(
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
        """Audit log omits reviewer_failures key when no failures occurred."""
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
            _make_agent_result(success=True, output="# Plan\n\nA plan.", cost_usd=0.10),
            _make_agent_result(success=True, output="Implemented."),
        ]
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_AGENT_APPROVE,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ]
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)
        audit = generate_audit_log(config, task, result)

        assert result.success is True
        assert "reviewer_failures" not in audit["plan_review"]


# ── TestPlanReviewParseRetry ──────────────────────────────────────────

# Prose emitted by a reviewer that stalled instead of producing contract output
# (mirrors issue-75: "Waiting on the verification agent's results before I
# finalize the review.").  Parses to a non-mapping YAML root → parse error.
PLAN_REVIEW_PROSE = "Waiting on the verification agent's results before I finalize the review."


class TestPlanReviewParseRetry:
    """A reviewer completion that succeeds at transport level but emits unparseable
    output is a transient agent failure — it must be re-invoked (fresh session) up
    to max_plan_review_parse_retries times before the min-reviewers gate escalates
    the story (issue-1667)."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_parse_failure_retried_then_succeeds(
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
        """Single reviewer emits prose → retried once → parses → story proceeds to DONE."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_regen_attempts=0,
                max_plan_review_parse_retries=2,
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
        # run_agent sequence: PLAN → plan-review parse retry (now valid) → DEV
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nA plan.", cost_usd=0.10),
            _make_agent_result(
                success=True,
                output=PLAN_AGENT_APPROVE,
                cost_usd=0.06,
                profile_name="plan-review",
            ),
            _make_agent_result(success=True, output="Implemented."),
        ]
        # Initial pool completion succeeds at transport level but is unparseable prose.
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_REVIEW_PROSE,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ]
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.plan_review_decision == "approve"
        # Reviewer recovered on retry → no residual parse failure recorded.
        assert result.state.plan_review_failures == []
        assert len(result.state.plan_review_parse_retries) == 1
        retry = result.state.plan_review_parse_retries[0]
        assert retry["attempt"] == 0
        assert retry["reviewer"] == "plan-review"
        assert retry["retry"] == 1
        assert retry["errors"]
        # Both the prose completion and the successful retry are cost-tracked.
        assert len(result.state.plan_review_results) == 2
        assert result.state.total_plan_review_cost == pytest.approx(0.14)
        # Fresh session on retry (session_id=None passed to run_agent). The retry
        # is the last plan_flow.run_agent call (PLAN is the first).
        assert mock_plan_agent.call_args_list[-1].kwargs["session_id"] is None

        audit = generate_audit_log(config, task, result)
        assert audit["plan_review"]["parse_retries"] == result.state.plan_review_parse_retries

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_parse_failure_exhausts_retries_then_escalates(
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
        """Prose on every attempt → parse retries exhausted → min-reviewers gate escalates."""
        config = dataclasses.replace(
            _make_plan_agent_review_config(tmp_path),
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                max_plan_regen_attempts=0,
                max_plan_review_parse_retries=2,
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
        # run_agent sequence: PLAN → two parse retries, both still unparseable prose.
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="# Plan\n\nA plan.", cost_usd=0.10),
            _make_agent_result(
                success=True, output=PLAN_REVIEW_PROSE, cost_usd=0.06, profile_name="plan-review"
            ),
            _make_agent_result(
                success=True, output=PLAN_REVIEW_PROSE, cost_usd=0.06, profile_name="plan-review"
            ),
        ]
        mock_plan_pool.side_effect = [
            [
                _make_agent_result(
                    success=True,
                    output=PLAN_REVIEW_PROSE,
                    cost_usd=0.08,
                    profile_name="plan-review",
                )
            ]
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task, interactive=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.state.plan_review_decision == "reject"
        assert "minimum required is 1" in (result.message or "")
        # Two fresh-session retries attempted before escalating.
        assert len(result.state.plan_review_parse_retries) == 2
        # Prose completion + two prose retries are all cost-tracked.
        assert len(result.state.plan_review_results) == 3
        assert result.state.total_plan_review_cost == pytest.approx(0.20)
        assert len(result.state.plan_review_failures) == 1
        failure = result.state.plan_review_failures[0]
        assert failure["reviewer"] == "plan-review"
        assert failure["failure_kind"] == "parse"
        assert failure["retryable"] is False
        # retry_count reflects the exhausted parse-retry budget, not a first-attempt fluke.
        assert failure["retry_count"] == 2

        audit = generate_audit_log(config, task, result)
        assert audit["plan_review"]["parse_retries"] == result.state.plan_review_parse_retries
        # Per-reviewer reconstruction stays aligned despite the extra retry results.
        assert audit["plan_review"]["per_reviewer"] == [
            {
                "attempt": 0,
                "profile": "plan-review",
                "verdict": "PARSE_ERROR",
                "cost_usd": pytest.approx(0.06),
            }
        ]
