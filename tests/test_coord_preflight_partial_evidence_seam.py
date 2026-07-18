"""Seam test: failed-preflight partial evidence flows into the PLAN phase (#706).

Covers the preflight -> plan state handoff (CONVENTIONS.md §8): a preflight run
that fails after inspecting files must (a) persist a partial-evidence artifact on
run state and (b) surface it into the plan agent's prompt so planning doesn't
re-read the same files. Exercised end-to-end through run_task with the runner
boundary mocked, so no provider SDK is required.
"""

from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_plan_config,
    _make_task,
    _shell_with_gate,
)

from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase
from theforge.runners import AgentResult

# A preflight run that crashed on a wall-clock timeout after inspecting two
# files and running a search — carrying the best-effort tool trace + partial
# conclusion the Claude runner now retains on kill paths.
_FAILED_PREFLIGHT = AgentResult(
    success=False,
    output="TIMEOUT: Agent exceeded 300s limit",
    session_id=None,
    cost_usd=2.54,
    exit_code=-9,
    raw={},
    profile_name="preflight",
    failure_code="timeout",
    tool_trace=(
        {"tool": "Read", "target": "src/theforge/runners/adapters/anthropic.py"},
        {"tool": "Grep", "target": "plan_review"},
        {"tool": "Read", "target": "src/theforge/coordinator/plan_flow.py"},
    ),
    partial_output="The adapter path routes plan review through _resolve_model_info.",
)


def test_failed_preflight_evidence_reaches_plan_prompt(tmp_path):
    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    captured: dict[str, str | None] = {}

    def _plan_side_effect(**kwargs):
        captured["prompt"] = kwargs.get("prompt")
        return _make_agent_result(success=True, output="Implemented.")

    with (
        patch("theforge.coordinator.util._run_shell") as mock_shell,
        patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
        patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
        patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _FAILED_PREFLIGHT
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = _plan_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

    # (a) Artifact persisted on run state.
    evidence = result.state.preflight_partial_evidence
    assert evidence is not None
    assert evidence["files_inspected"] == [
        "src/theforge/runners/adapters/anthropic.py",
        "src/theforge/coordinator/plan_flow.py",
    ]
    assert evidence["failure_code"] == "timeout"
    assert evidence["cost_usd"] == 2.54
    assert (
        evidence["partial_conclusion"]
        == "The adapter path routes plan review through _resolve_model_info."
    )

    # A failed preflight with no risk signals falls through to a conservative
    # (degraded) PROCEED and still runs planning.
    assert result.state.preflight_verdict == "PROCEED"
    assert result.state.preflight_degraded is True

    # (b) The evidence is surfaced into the plan agent's prompt.
    assert "prompt" in captured, "plan agent was not invoked"
    plan_prompt = captured["prompt"] or ""
    assert "Partial Evidence from a Failed Preflight" in plan_prompt
    assert "src/theforge/runners/adapters/anthropic.py" in plan_prompt
    assert "The adapter path routes plan review through _resolve_model_info." in plan_prompt


def test_successful_preflight_leaves_no_partial_evidence(tmp_path):
    """A clean preflight must not produce a partial-evidence artifact."""
    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    preflight_ok = """\
```yaml
verdict: PROCEED
reason: "Needs implementation."
complexity: medium
sufficiency: needs_planning
work_type: feature
criteria_checked:
  - criterion: "Feature X"
    satisfied: false
    evidence: "Not found"
```
"""

    captured: dict[str, str | None] = {}

    def _plan_side_effect(**kwargs):
        captured["prompt"] = kwargs.get("prompt")
        return _make_agent_result(success=True, output="Implemented.")

    with (
        patch("theforge.coordinator.util._run_shell") as mock_shell,
        patch("theforge.coordinator.dev_phase.run_agent") as mock_dev,
        patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        patch("theforge.coordinator.plan_flow.run_agent") as mock_plan_agent,
        patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=preflight_ok, cost_usd=0.10, profile_name="preflight"
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan_agent.side_effect = _plan_side_effect
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

    assert result.phase == Phase.DONE
    assert result.state.preflight_partial_evidence is None
    plan_prompt = captured.get("prompt") or ""
    assert "Partial Evidence from a Failed Preflight" not in plan_prompt
