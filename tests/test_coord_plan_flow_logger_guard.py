"""Regression test: plan_flow escalation path must tolerate logger=None.

plan_flow._run_plan_phase declares ``logger: StructuredLogger | None`` but
previously called ``logger._safe_emit(...)`` unconditionally in the
mutation-detected escalation branch, raising AttributeError when no logger
was supplied. Every call site must guard with ``if logger:``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_agent_result, _make_config, _make_task

from theforge.config import PlanConfig
from theforge.coordinator import plan_flow
from theforge.coordinator.state import CoordinatorState, Phase


def test_run_plan_phase_escalation_with_logger_none_does_not_raise(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config = dataclasses.replace(config, plan=PlanConfig.of(enabled=True))
    task = _make_task(tmp_path)

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()

    state = CoordinatorState(log_dir=tmp_path / "logs")
    state.preflight_complexity = "medium"
    state.preflight_sufficiency = "needs_planning"

    with (
        patch.object(plan_flow, "run_agent", return_value=_make_agent_result(output="plan text")),
        patch(
            "theforge.coordinator.workspace_hygiene.check_phase_no_mutation",
            return_value=(False, "PLAN phase mutated the worktree", ["scratch.py"]),
        ),
    ):
        result = plan_flow._run_plan_phase(
            state=state,
            config=config,
            task=task,
            story_content="Implement the thing.",
            workspace_path=workspace_path,
            plan_path=None,
            preflight_result=None,
            notify=False,
            logger=None,
            run_id=None,
            state_update_fn=None,
        )

    assert result is not None
    assert result.success is False
    assert result.phase == Phase.ESCALATE
