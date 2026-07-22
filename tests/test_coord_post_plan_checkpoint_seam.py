"""End-to-end coordinator seam for the post-plan dev-tier checkpoint (#1387).

Proves that a clean agent plan-review APPROVE on a medium story re-evaluates
ONLY the dev tier, swaps ``config.dev_profile`` before DEV runs, and records the
outcome in ``routing_decision.dev.post_plan_checkpoint``. Complements the pure
gate-condition unit coverage in ``test_post_plan_checkpoint.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED_MEDIUM,
    _make_agent_result,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    AgentDef,
    AssignmentConfig,
    ForgeConfig,
    LogConfig,
    PlanAgentReviewConfig,
    PlanConfig,
    PlanReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task

PLAN_AGENT_APPROVE = """\
```yaml
verdict: APPROVE
findings: []
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

# Medium story that preflight classifies as refactor work → advisory plan review.
PREFLIGHT_PROCEED_MEDIUM_REFACTOR = """\
```yaml
verdict: PROCEED
complexity: medium
work_type: refactor
reason: "Refactor of an existing module."
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found in codebase"
```
"""


def _agents() -> list[AgentDef]:
    return [
        AgentDef(
            name="haiku",
            provider="anthropic",
            model="haiku",
            budget_usd=1.0,
            timeout_seconds=600,
            tier="cheap",
        ),
        AgentDef(
            name="sonnet",
            provider="anthropic",
            model="sonnet",
            budget_usd=3.0,
            timeout_seconds=600,
            tier="mid",
        ),
        AgentDef(
            name="opus",
            provider="anthropic",
            model="opus",
            budget_usd=5.0,
            timeout_seconds=900,
            tier="strong",
        ),
    ]


def _make_config(tmp_path: Path, *, plan_tier_reduction: bool = True) -> ForgeConfig:
    """Adaptive config: agents pool + plan_agent_review, medium → mid dev."""
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
        review_pool_is_default=True,
        synthesis_profile=None,
        agents=_agents(),
        secrets={"ANTHROPIC_API_KEY": "k"},
        assignment=AssignmentConfig(
            enabled=True,
            escalation_memory=False,
            min_reviewers=1,
            max_reviewers=1,
            prefer_cross_provider=False,
            plan_tier_reduction=plan_tier_reduction,
        ),
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300, validate_spec=False),
        plan_agent_review=par_config,
        log=LogConfig(enabled=False),
    )


def _run(config, tmp_path, dev_models: list):
    """Drive run_task, capturing the dev profile model that reaches DEV."""
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    def _capture_dev(*args, **kwargs):
        dev_models.append(kwargs["profile"].model)
        return _make_agent_result(success=True, output="Implemented.")

    with (
        patch("theforge.coordinator.review_pool.run_agent_pool") as mock_code_pool,
        patch(
            "theforge.coordinator.review_phase._human_review",
            return_value=("approve", None),
        ),
        patch("theforge.coordinator.plan_flow.run_agent_pool") as mock_plan_pool,
        patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
        patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        patch("theforge.coordinator.dev_phase.run_agent", side_effect=_capture_dev),
        patch_gate_shell() as mock_shell,
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_plan_agent.return_value = _make_agent_result(
            success=True, output="# Plan\n\nGood plan.", cost_usd=0.10
        )
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
        return run_task(config, task, interactive=True)


def test_clean_plan_review_downgrades_dev_before_dev_phase(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    config = _make_config(tmp_path)
    dev_models: list = []
    result = _run(config, tmp_path, dev_models)

    assert result.success is True
    assert result.state.plan_review_decision == "approve"

    # The medium story assigned dev at mid tier (score 5). The clean plan-review
    # stepped it down one level to cheap BEFORE the dev phase ran.
    cp = result.state.routing_decision["dev"]["post_plan_checkpoint"]
    assert cp["fired"] is True
    assert cp["decision"] == "downgrade"
    assert cp["baseline_tier"] == "mid"
    assert cp["final_tier"] == "cheap"
    assert cp["plan_present"] is True
    assert cp["rationale"] == "plan_review_clean_medium"

    # The dev phase actually ran on the demoted (cheap-tier) model.
    assert dev_models == ["haiku"]
    assert result.state._adaptive_decision.dev.model == "haiku"


def test_disabled_flag_keeps_preflight_dev_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    config = _make_config(tmp_path, plan_tier_reduction=False)
    dev_models: list = []
    result = _run(config, tmp_path, dev_models)

    assert result.success is True
    cp = result.state.routing_decision["dev"]["post_plan_checkpoint"]
    assert cp["fired"] is False
    assert cp["decision"] == "skipped"
    assert cp["rationale"] == "plan_tier_reduction_disabled"
    # Dev ran on the preflight-assigned mid tier, untouched.
    assert dev_models == ["sonnet"]


def _make_human_config(tmp_path: Path) -> ForgeConfig:
    """Adaptive config with HUMAN plan-review (blocking) instead of agent review."""
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
        review_pool_is_default=True,
        synthesis_profile=None,
        agents=_agents(),
        secrets={"ANTHROPIC_API_KEY": "k"},
        assignment=AssignmentConfig(
            # min/max reviewers = 0 → adaptive assigns no plan reviewers, so
            # plan_agent_review stays disabled and the human plan-review path is
            # exercised while _adaptive_decision is still populated. Code review
            # falls back to the default review_pool (unchanged when 0 reviewers).
            enabled=True,
            escalation_memory=False,
            min_reviewers=0,
            max_reviewers=0,
            prefer_cross_provider=False,
            plan_tier_reduction=True,
        ),
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300, validate_spec=False),
        plan_review=PlanReviewConfig(enabled=True, mode="blocking", timeout_seconds=300),
        log=LogConfig(enabled=False),
    )


def test_human_plan_review_approve_also_runs_checkpoint(tmp_path, monkeypatch):
    """A clean HUMAN plan-review APPROVE demotes dev too — not just agent review.

    Regression guard for the iter-1 P1: the checkpoint was only wired into the
    agent-review APPROVE branch, so human/pending-file/remote approvals reached
    DEV with the preflight dev profile and a ``pending`` audit block.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    config = _make_human_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    dev_models: list = []

    def _capture_dev(*args, **kwargs):
        dev_models.append(kwargs["profile"].model)
        return _make_agent_result(success=True, output="Implemented.")

    with (
        patch("theforge.coordinator.review_pool.run_agent_pool") as mock_code_pool,
        patch(
            "theforge.coordinator.review_phase._human_review",
            return_value=("approve", None),
        ),
        patch(
            "theforge.coordinator.plan_flow._plan_review_interactive",
            return_value="approve",
        ),
        patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
        patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        patch("theforge.coordinator.dev_phase.run_agent", side_effect=_capture_dev),
        patch_gate_shell() as mock_shell,
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM, cost_usd=0.05
        )
        mock_plan_agent.return_value = _make_agent_result(
            success=True, output="# Plan\n\nGood plan.", cost_usd=0.10
        )
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        result = run_task(config, task, interactive=True)

    assert result.success is True
    assert result.state.plan_review_decision == "approve"
    cp = result.state.routing_decision["dev"]["post_plan_checkpoint"]
    assert cp["fired"] is True
    assert cp["decision"] == "downgrade"
    assert cp["baseline_tier"] == "mid"
    assert cp["final_tier"] == "cheap"
    assert cp["rationale"] == "plan_review_clean_medium"
    assert dev_models == ["haiku"]


def test_advisory_reject_records_preserve_and_keeps_dev_tier(tmp_path, monkeypatch):
    """A refactor advisory plan-review REJECT proceeds to DEV but records the
    checkpoint as preserve/plan_review_not_approve — never the pending sentinel.

    Regression guard for the iter-1 P1: the advisory continue-to-DEV branch left
    routing_decision.dev.post_plan_checkpoint at the pending sentinel.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    dev_models: list = []

    def _capture_dev(*args, **kwargs):
        dev_models.append(kwargs["profile"].model)
        return _make_agent_result(success=True, output="Implemented.")

    with (
        patch("theforge.coordinator.review_pool.run_agent_pool") as mock_code_pool,
        patch(
            "theforge.coordinator.review_phase._human_review",
            return_value=("approve", None),
        ),
        patch("theforge.coordinator.plan_flow.run_agent_pool") as mock_plan_pool,
        patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
        patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        patch("theforge.coordinator.dev_phase.run_agent", side_effect=_capture_dev),
        patch_gate_shell() as mock_shell,
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_PROCEED_MEDIUM_REFACTOR, cost_usd=0.05
        )
        mock_plan_agent.return_value = _make_agent_result(
            success=True, output="# Plan\n\nRefactor plan.", cost_usd=0.10
        )
        # Advisory mode: a REJECT (P0) verdict is logged but does NOT block; the
        # run proceeds to DEV via the advisory continue-to-DEV branch.
        mock_plan_pool.return_value = [
            _make_agent_result(
                success=True,
                output=PLAN_AGENT_REJECT_P0,
                cost_usd=0.08,
                profile_name="plan-review",
            )
        ]
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        result = run_task(config, task, interactive=True)

    assert result.success is True
    assert result.state.preflight_work_type == "refactor"
    cp = result.state.routing_decision["dev"]["post_plan_checkpoint"]
    assert cp["fired"] is False
    assert cp["decision"] == "preserve"
    assert cp["baseline_tier"] == "mid"
    assert cp["final_tier"] == "mid"
    assert cp["rationale"] == "plan_review_not_approve"
    # Dev ran on the untouched preflight-assigned mid tier.
    assert dev_models == ["sonnet"]
