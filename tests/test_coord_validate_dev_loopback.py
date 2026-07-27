"""Seam tests for the VALIDATE → DEV return path (#1981).

Gate execution is coordinator-owned (#1948): the dev never sees a gate result
unless VALIDATE hands it back. These tests cover that hand-back at the
DEV ↔ VALIDATE seam — a coordinator-observed gate or convention failure returns
control to the dev while budget remains, spending dev iterations first and a
review cycle only once the dev pool is empty, and terminates only when both are
gone or the failure stops moving.

They also pin the boundary the fix must not cross: VALIDATE records its findings
in ``state.validate_blocks``, never in the reviewer record (``review_results`` /
``review_cycle_metadata`` / ``review_iteration_telemetry``), because an entry in
those means a reviewer pool ran and downstream consumers depend on that.
"""

import dataclasses
import types
from pathlib import Path

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _handle_stale_check_cmd,
    _make_agent_result,
    _make_config,
    _make_task,
    patch_gate_shell,
)
from coord_test_helpers import patch as _patch

from theforge.config.types import HardConventionsConfig
from theforge.coordinator.engine import run_task
from theforge.coordinator.model_profiles_bridge import _extract_reviewers
from theforge.coordinator.state import Phase, RetryReason


def _gate_script(decisions: list[str], *, vary_output: bool = True):
    """Gate/shell side_effect that can give each failing gate run distinct output.

    The shared helper returns one constant failure string, which is the signature
    of a *stalled* story. A converging dev changes what the gate says, so tests
    that model progress need output that differs run to run — otherwise the
    identical-output circuit breaker correctly stops them early.
    """
    calls = {"n": 0}

    def side_effect(cmd, cwd, **kwargs):
        if "gate" in cmd:
            decision = decisions[min(calls["n"], len(decisions) - 1)]
            calls["n"] += 1
            if decision == "PASS":
                notes = Path(cwd) / ".forge"
                notes.mkdir(parents=True, exist_ok=True)
                (notes / "dev_notes.md").write_text("done\n", encoding="utf-8")
                return (True, "OK", 0, False)
            suffix = f" (run {calls['n']})" if vary_output else ""
            return (False, f"FAIL: tests failed{suffix}", 1, False)
        if "git status --porcelain" in cmd:
            return (True, "", 0, False)
        stale = _handle_stale_check_cmd(cmd)
        if stale is not None:
            ok, out = stale
            return (ok, out, 0 if ok else 1, False)
        return (True, "OK", 0, False)

    return side_effect


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
    return types.SimpleNamespace(
        rule=rule,
        file=file,
        detail=f"{rule} tripped in {file}",
        blocking=True,
    )


def _run(config, task, decisions: list[str], conventions=None, *, vary_output: bool = True):
    """Run a full task with a scripted gate decision sequence."""
    with (
        patch_gate_shell() as mock_shell,
        _patch("theforge.coordinator.preflight_flow.run_agent") as mock_preflight,
        _patch("theforge.coordinator.dev_phase.run_agent") as mock_agent,
        _patch("theforge.coordinator.review_pool.run_agent_pool") as mock_pool,
        _patch("theforge.coordinator.validate_phase._check_conventions_parallel") as mock_cv,
        _patch("theforge.coordinator.validate_phase._record_advisory_convention_state"),
    ):
        mock_shell.side_effect = _gate_script(decisions, vary_output=vary_output)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(
                success=True, output=APPROVE_REVIEW, profile_name="review", cost_usd=0.42
            )
        ]
        mock_cv.side_effect = list(conventions) if conventions is not None else None
        if conventions is None:
            mock_cv.return_value = None
        return run_task(config, task), mock_pool


def _workspace(tmp_path: Path) -> None:
    (tmp_path / "test-task").mkdir()


def test_gate_failure_that_empties_dev_pool_returns_to_dev_in_a_new_cycle(tmp_path: Path) -> None:
    """The #1972 shape: a gate failure on the last dev iteration is not terminal.

    Two dev iterations fail the gate, exhausting the per-cycle pool. Instead of
    escalating, the coordinator charges the finding to a review cycle, refills
    the dev pool, and the third iteration's PASS carries the story to DONE.
    """
    config = _make_config(tmp_path)  # 2 dev iterations, 2 review cycles
    task = _make_task(tmp_path)
    _workspace(tmp_path)

    result, mock_pool = _run(config, task, ["FAIL", "FAIL", "PASS"])

    assert result.success is True
    assert result.phase == Phase.DONE
    assert result.state.gate_decisions == ["FAIL", "FAIL", "PASS"]
    assert result.state.validate_opened_review_cycles == 1
    mock_pool.assert_called_once()


def test_validate_block_is_recorded_outside_the_reviewer_record(tmp_path: Path) -> None:
    """VALIDATE's finding lands in its own channel and never impersonates a reviewer.

    ``review_results`` / ``review_cycle_metadata`` / ``review_iteration_telemetry``
    mean "a reviewer pool ran". Writing a coordinator finding into them credited a
    model named ``coordinator`` with the real reviewer's cost in
    ``model_profiles.yaml`` and zeroed the reviewer that actually ran (#1981).
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    _workspace(tmp_path)

    result, _ = _run(config, task, ["FAIL", "FAIL", "PASS"])
    state = result.state

    assert len(state.validate_blocks) == 1
    block = state.validate_blocks[0]
    assert block["kind"] == "gate"
    assert block["gate_decision"] == "FAIL"
    assert block["review_cycle"] == 0
    assert block["dev_iterations_spent"] == config.retry.max_dev_iterations
    assert block["detail"]

    # One reviewer cycle ran, and it is the only thing in the reviewer record.
    assert len(state.review_results) == 1
    assert state.review_results[0].verdict == "APPROVE"
    assert [m.pool_models for m in state.review_cycle_metadata] == [["review"]]
    assert len(state.review_iteration_telemetry) == 1

    # Attribution reaches the reviewer that actually ran, with its real cost.
    attribution = _extract_reviewers(state)
    assert "coordinator" not in attribution
    assert attribution["review"][0] == 1
    assert attribution["review"][2] == 0.42


def test_new_cycle_refills_the_dev_iteration_pool(tmp_path: Path) -> None:
    """The cycle the finding buys must come with iterations to spend in it."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    _workspace(tmp_path)

    result, _ = _run(config, task, ["FAIL", "FAIL", "FAIL", "PASS"])

    assert result.success is True
    # Four dev calls: two in the first cycle, two more in the cycle the gate
    # failure bought. A cycle that did not reset the budget would have escalated
    # on the third.
    assert len(result.state.budget.consumption_log) == 4
    assert [c.cycle for c in result.state.budget.consumption_log] == [0, 0, 1, 1]
    assert [c.cycle_count for c in result.state.budget.consumption_log] == [1, 2, 1, 2]


def test_gate_failure_escalates_once_both_budgets_are_spent(tmp_path: Path) -> None:
    """Terminal when dev iterations and review cycles are both gone."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    _workspace(tmp_path)

    result, mock_pool = _run(config, task, ["FAIL"] * 10)

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    # 2 dev iterations × 2 review cycles, then terminal — not one and done.
    assert len(result.state.gate_decisions) == 4
    # The attempt count names gate runs performed, not the per-cycle dev counter.
    assert "after 4 gate run(s)" in result.message
    assert "both exhausted" in result.message
    mock_pool.assert_not_called()


def test_unchanging_gate_output_stops_before_buying_another_cycle(tmp_path: Path) -> None:
    """A failure that stops moving does not get to spend the full cross-product.

    ``_is_identical_failure`` needs named failing tests, so a lint- or
    format-only failure — the exact #1972 shape — has no convergence signal.
    Comparing gate output fingerprints supplies one, and it gates only the
    purchase of a new cycle, so the dev still gets its full in-cycle iterations.
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    _workspace(tmp_path)

    result, _ = _run(config, task, ["FAIL"] * 10, vary_output=False)

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    # Both in-cycle iterations still ran; only the cycle purchase was refused.
    assert len(result.state.gate_decisions) == config.retry.max_dev_iterations
    assert result.state.validate_opened_review_cycles == 0
    assert "identical output on the last two iterations" in result.message


def test_convention_violation_returns_to_dev_and_the_fix_completes_the_story(
    tmp_path: Path,
) -> None:
    """The #1945 shape: a passing gate plus one convention finding is not terminal."""
    config = _conventions_config(tmp_path)
    task = _make_task(tmp_path)
    _workspace(tmp_path)

    viol = _violation()
    result, mock_pool = _run(
        config,
        task,
        ["PASS", "PASS"],
        conventions=[([viol], [viol]), ([], [])],
    )

    assert result.success is True
    assert result.phase == Phase.DONE
    # The violation was fixed inside the first review cycle, spending a dev
    # iteration rather than a review cycle.
    assert result.state.validate_opened_review_cycles == 0
    assert result.state.validate_blocks == []
    assert len(result.state.budget.consumption_log) == 2
    mock_pool.assert_called_once()


def test_convention_violation_escalates_once_both_budgets_are_spent(tmp_path: Path) -> None:
    """A convention violation that never gets fixed still terminates."""
    config = _conventions_config(tmp_path)
    task = _make_task(tmp_path)
    _workspace(tmp_path)

    viol = _violation()
    result, mock_pool = _run(
        config,
        task,
        ["PASS"] * 10,
        conventions=[([viol], [viol])] * 10,
    )

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    assert result.state.retry_reason == RetryReason.CONVENTION_VIOLATIONS
    # Same bound as a gate failure: dev iterations × review cycles. A passing
    # gate writes no failing output, so the fingerprint breaker cannot fire here.
    assert len(result.state.gate_decisions) == 4
    assert result.state.validate_opened_review_cycles == 1
    assert [b["kind"] for b in result.state.validate_blocks] == ["convention"]
    # The count names validation runs performed, not the per-cycle dev counter.
    assert "after 4 validation run(s)" in result.message


def test_exhausted_review_budget_is_reported_as_a_hit_limit(tmp_path: Path) -> None:
    """A story stopped for want of a review cycle must not read as finishing early.

    The refusal leaves one cycle in flight, so comparing used-vs-max alone
    reported ``hit_limit: false`` and ``early_finish: true`` on a story that died
    because no further cycle could be opened (#1981).
    """
    from theforge.coordinator.audit import _build_iteration_usage_summary

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    _workspace(tmp_path)

    result, _ = _run(config, task, ["FAIL"] * 10)

    assert result.phase == Phase.ESCALATE
    assert result.state.review_budget_exhausted is True
    usage = _build_iteration_usage_summary(result.state, config)
    assert usage["review"]["used"] == 1
    assert usage["review"]["hit_limit"] is True
    assert usage["review"]["early_finish"] is False


def test_reviewer_cycle_counts_exclude_the_cycle_validate_bought(tmp_path: Path) -> None:
    """The adaptive learner must not read a gate failure as demand for reviewer cycles.

    ``review_cycles_total`` feeds ``derive_limits``, which percentiles it to set
    future ``max_review_cycles``. Counting a cycle VALIDATE opened for its own
    finding taught the router that gate failures need more reviewers (#1981).
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    _workspace(tmp_path)

    result, _ = _run(config, task, ["FAIL", "FAIL", "PASS"])
    state = result.state

    assert state.validate_opened_review_cycles == 1
    # One reviewer cycle ran; the cycle the gate failure bought is not counted
    # as reviewer demand, but is still visible as budget the story spent.
    assert state.reviewer_cycles_run == 1
    assert state.review_cycles_spent == 2
