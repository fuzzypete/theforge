"""Seam tests for the VALIDATE → DEV return path (#1981).

Gate execution is coordinator-owned (#1948): the dev never sees a gate result
unless VALIDATE hands one back. These tests cover that hand-back at the
DEV ↔ VALIDATE seam — a coordinator-observed gate or convention failure returns
control to the dev while budget remains, spending dev iterations first and a
review cycle only once the dev pool is empty, and terminates only when both are
gone.
"""

import dataclasses
from pathlib import Path

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)
from coord_test_helpers import patch as _patch

from theforge.config.types import HardConventionsConfig
from theforge.coordinator.audit_render import build_reviews
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase, RetryReason


def _conventions_config(tmp_path: Path, **retry_kwargs):
    """Test config with hard conventions enabled and overridable retry limits."""
    base = _make_config(tmp_path)
    retry_fields = {
        "max_dev_iterations": base.retry.max_dev_iterations,
        "max_review_cycles": base.retry.max_review_cycles,
        **retry_kwargs,
    }
    return dataclasses.replace(
        base,
        conventions_hard=HardConventionsConfig(max_module_lines=500),
        retry=base.retry.__class__(**retry_fields),
    )


def _violation(rule: str = "no_circular_imports", file: str = "src/a.py"):
    import types

    return types.SimpleNamespace(
        rule=rule,
        file=file,
        detail=f"{rule} tripped in {file}",
        blocking=True,
    )


def _run(config, task, workspace: Path, decisions: list[str], conventions=None):
    """Run a full task with a scripted gate decision sequence."""
    with (
        patch_gate_shell() as mock_shell,
        _patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        _patch("theforge.coordinator.dev_phase.run_agent") as mock_agent,
        _patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        _patch("theforge.coordinator.validate_phase._check_conventions_parallel") as mock_cv,
        _patch("theforge.coordinator.validate_phase._record_advisory_convention_state"),
    ):
        mock_shell.side_effect = _shell_with_gate(workspace, decisions=decisions)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]
        mock_cv.side_effect = list(conventions) if conventions is not None else None
        if conventions is None:
            mock_cv.return_value = None
        return run_task(config, task), mock_pool


def test_gate_failure_that_empties_dev_pool_returns_to_dev_in_a_new_cycle(tmp_path: Path) -> None:
    """The #1972 shape: a gate failure on the last dev iteration is not terminal.

    Two dev iterations fail the gate, exhausting the per-cycle pool. Instead of
    escalating, the coordinator charges the finding to a review cycle, refills
    the dev pool, and the third iteration's PASS carries the story to DONE.
    """
    config = _make_config(tmp_path)  # 2 dev iterations, 2 review cycles
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    result, mock_pool = _run(config, task, workspace, ["FAIL", "FAIL", "PASS"])

    assert result.success is True
    assert result.phase == Phase.DONE
    assert result.state.gate_decisions == ["FAIL", "FAIL", "PASS"]
    # The finding was charged to a review cycle, recorded as the coordinator's
    # own REQUEST_CHANGES rather than vanishing into an escalation.
    gate_blocks = [
        r for r in result.state.review_results if r.raw_yaml.get("source") == "validate_gate_block"
    ]
    assert len(gate_blocks) == 1
    assert gate_blocks[0].verdict == "REQUEST_CHANGES"
    assert "gate" in gate_blocks[0].findings[0].observed.lower()
    mock_pool.assert_called_once()
    # The audit pairs review metadata with review results by index, so the
    # synthetic verdict must not shift the real review cycle's findings.
    rendered = build_reviews(result.state)
    assert [r["pool_models"] for r in rendered] == [["coordinator"], ["review"]]
    assert rendered[0]["verdict"] == "REQUEST_CHANGES"
    assert rendered[1]["verdict"] == "APPROVE"


def test_new_cycle_refills_the_dev_iteration_pool(tmp_path: Path) -> None:
    """The cycle the finding buys must come with iterations to spend in it."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    result, _ = _run(config, task, workspace, ["FAIL", "FAIL", "FAIL", "PASS"])

    assert result.success is True
    # Four dev calls happened: two in the first cycle, two more in the cycle the
    # gate failure bought. A cycle that did not reset the budget would have
    # escalated on the third.
    assert len(result.state.budget.consumption_log) == 4
    assert [c.cycle for c in result.state.budget.consumption_log] == [0, 0, 1, 1]
    assert [c.cycle_count for c in result.state.budget.consumption_log] == [1, 2, 1, 2]


def test_gate_failure_escalates_once_both_budgets_are_spent(tmp_path: Path) -> None:
    """Terminal only when dev iterations and review cycles are both gone."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    result, mock_pool = _run(config, task, workspace, ["FAIL"] * 10)

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    # 2 dev iterations × 2 review cycles, then terminal — not one and done.
    assert len(result.state.gate_decisions) == 4
    # The attempt count names gate runs performed, not the per-cycle dev counter.
    assert "after 4 gate run(s)" in result.message
    mock_pool.assert_not_called()


def test_convention_violation_returns_to_dev_and_the_fix_completes_the_story(
    tmp_path: Path,
) -> None:
    """The #1945 shape: a passing gate plus one convention finding is not terminal."""
    config = _conventions_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    viol = _violation()
    result, mock_pool = _run(
        config,
        task,
        workspace,
        ["PASS", "PASS"],
        conventions=[([viol], [viol]), ([], [])],
    )

    assert result.success is True
    assert result.phase == Phase.DONE
    # The violation was fixed inside the first review cycle, spending a dev
    # iteration rather than a review cycle.
    assert result.state.review_cycle == 1
    assert len(result.state.budget.consumption_log) == 2
    mock_pool.assert_called_once()


def test_convention_violation_escalates_once_both_budgets_are_spent(tmp_path: Path) -> None:
    """A convention violation that never gets fixed still terminates."""
    config = _conventions_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir()

    viol = _violation()
    result, mock_pool = _run(
        config,
        task,
        workspace,
        ["PASS"] * 10,
        conventions=[([viol], [viol])] * 10,
    )

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    assert result.state.retry_reason == RetryReason.CONVENTION_VIOLATIONS
    # Same bound as a gate failure: dev iterations × review cycles.
    assert len(result.state.gate_decisions) == 4
    # The count names validation runs performed, not the per-cycle dev counter.
    assert "after 4 validation run(s)" in result.message
    mock_pool.assert_not_called()
