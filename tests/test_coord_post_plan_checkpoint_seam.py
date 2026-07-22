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
