"""Seam test: per-plan-reviewer value telemetry captured at pool completion (#1443).

Exercises the real PLAN_REVIEW phase boundary through ``run_task``: after a plan
review pool completes, deterministic per-reviewer signals (unique P1 count, total
P1 count, latency, parse-error count) must land on ``CoordinatorState`` and in the
native audit record. Parse-error count is derived from the existing parse step —
no parallel parse-failure writer.
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

# Reviewer A raises a P1 anchored on a distinct snake_case symbol; reviewer B
# returns unparseable output (no YAML mapping).
PLAN_A_P1 = """\
```yaml
verdict: REJECT
findings:
  - severity: P1
    description: "The alpha_helper routine in engine.py mishandles retries"
    suggestion: "Guard alpha_helper against empty input"
```
"""

PLAN_B_GARBAGE = "this is not valid plan-review yaml at all"


def _dual_pool_config(tmp_path: Path, *, min_reviewers: int) -> ForgeConfig:
    a = ModelProfile(
        name="plan-review-a",
        cli="claude",
        model="sonnet",
        budget_usd=0.50,
        timeout_seconds=300,
        allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
    )
    b = ModelProfile(
        name="plan-review-b",
        cli="claude",
        model="opus",
        budget_usd=0.50,
        timeout_seconds=300,
        allowed_tools=DEFAULT_PREFLIGHT_PROFILE.allowed_tools,
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
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            max_plan_review_parse_retries=0,
            max_plan_review_transport_retries=0,
        ),
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300, validate_spec=False),
        plan_agent_review=PlanAgentReviewConfig(
            enabled=True, min_reviewers=min_reviewers, pool=[a, b]
        ),
        log=LogConfig(enabled=False),
    )


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.review_phase._human_review", return_value=("approve", None))
@patch("theforge.coordinator.plan_flow.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_plan_reviewer_value_captured_in_state_and_audit(
    mock_shell,
    mock_agent,
    mock_preflight,
    mock_plan_agent,
    mock_plan_pool,
    mock_human_review,
    mock_code_pool,
    tmp_path,
):
    # min_reviewers=1 so reviewer B's parse failure does not collapse the pool;
    # reviewer A's lone P1 is corroboration-downgraded to advisory → APPROVE.
    config = _dual_pool_config(tmp_path, min_reviewers=1)
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
    mock_plan_pool.return_value = [
        _make_agent_result(
            success=True, output=PLAN_A_P1, cost_usd=0.08, profile_name="plan-review-a"
        ),
        _make_agent_result(
            success=True, output=PLAN_B_GARBAGE, cost_usd=0.08, profile_name="plan-review-b"
        ),
    ]
    mock_code_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    result = run_task(config, task, interactive=True)

    assert result.success is True
    rows = result.state.plan_reviewer_value
    assert len(rows) == 2  # one pool attempt, two reviewers
    by_name = {r["reviewer"]: r for r in rows}

    # Reviewer A parsed cleanly with a single P1 no peer corroborated → unique.
    a = by_name["plan-review-a"]
    assert a["total_p1_count"] == 1
    assert a["unique_p1_count"] == 1
    assert a["parse_error_count"] == 0
    assert a["complexity"] == "medium"

    # Reviewer B failed to parse → derived parse-error count, no structured P1s.
    b = by_name["plan-review-b"]
    assert b["parse_error_count"] > 0
    assert b["total_p1_count"] == 0
    assert b["unique_p1_count"] == 0

    # Audit record surfaces the per-reviewer value telemetry (operator-facing).
    audit = generate_audit_log(config, task, result)
    per_reviewer_value = audit["plan_review"]["per_reviewer_value"]
    assert {e["reviewer"] for e in per_reviewer_value} == {"plan-review-a", "plan-review-b"}
    a_entry = next(e for e in per_reviewer_value if e["reviewer"] == "plan-review-a")
    assert a_entry["unique_p1_count"] == 1
    assert a_entry["total_p1_count"] == 1
    # The wall-clock cost fields are present so an operator can answer "is this
    # reviewer earning its cost?" without grepping logs (latency is None here
    # because run_agent_pool is mocked and never populates durations_out).
    assert "latency_s" in a_entry
    assert "latency_per_p1_s" in a_entry
