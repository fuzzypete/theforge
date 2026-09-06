from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import patch_gate_shell

from tests.coord_test_helpers import (
    APPROVE_REVIEW,
    PREFLIGHT_PROCEED,
    _as_detailed,
    _make_agent_result,
    _make_config,
    _make_task,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase
from theforge.runners import AgentResult
from theforge.sprint.audit import _write_sprint_audit


def _max_iter_no_submit_result(profile_name: str = "dev") -> AgentResult:
    return AgentResult(
        success=False,
        output=(
            "Agent loop terminated: max iterations reached after 55 iterations. "
            "Accumulated cost reported."
        ),
        session_id="sess-maxed",
        cost_usd=1.94,
        exit_code=1,
        raw={},
        profile_name=profile_name,
        failure_code="max_iterations_reached",
        dev_handoff=None,
    )


def _shell_pass(workspace: Path, gate_decisions: list[str] | None = None):
    gate_decisions = gate_decisions or ["PASS"]
    gate_idx = {"n": 0}

    def side_effect(cmd, cwd, **kwargs):
        if "gate" in cmd:
            decision = gate_decisions[min(gate_idx["n"], len(gate_decisions) - 1)]
            gate_idx["n"] += 1
            return (decision == "PASS", "OK" if decision == "PASS" else "FAIL")
        if "git status --porcelain" in cmd:
            return (True, "")
        if "rev-parse --abbrev-ref HEAD" in cmd:
            return (True, "forge/test-task")
        if "--oneline" in cmd and "git log" in cmd:
            return (True, "abc1234 feat: implement")
        if "--format=%ct" in cmd:
            return (True, "1234567890")
        return (True, "OK")

    return _as_detailed(side_effect)


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_max_iterations_without_submit_is_distinct_outcome(
    mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
):
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_pass(workspace)
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED, profile_name="preflight"
    )
    mock_dev.side_effect = [_max_iter_no_submit_result(), _make_agent_result(profile_name="dev")]
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    result = run_task(config, task)

    assert result.success is True
    assert result.state.dev_results[0].failure_code == "max_iterations_reached"
    assert result.state.dev_handoff_snapshots[0]["source"] == "missing"


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_max_iterations_without_submit_stops_at_dev_retry_budget(
    mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
):
    """No-submit failures consume the same bounded DEV retry budget as timeouts."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_pass(workspace)
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED, profile_name="preflight"
    )
    mock_dev.side_effect = [_max_iter_no_submit_result(), _max_iter_no_submit_result()]

    result = run_task(config, task)

    assert result.success is False
    assert result.phase == Phase.ESCALATE
    assert mock_dev.call_count == config.retry.max_dev_iterations
    assert result.state.budget.total_count == config.retry.max_dev_iterations
    assert result.state.budget.is_exhausted() is True
    assert "exhausted its retry budget" in (result.message or "")
    mock_pool.assert_not_called()


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_coordinator_cutoff_is_not_recorded_as_model_capability_failure(
    mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
):
    """End to end: a run forge itself cut off never becomes a model dev failure.

    The coordinator ends this run because ITS iteration budget is spent with
    submit never called — a statement about forge's budget, not about the model's
    work. The classification must survive from dev_phase all the way to the
    RunOutcome the profile aggregator folds, where a non-None termination cause
    keeps the run out of runs/success_rate (#2921).
    """
    from theforge.coordinator.model_profiles_bridge import build_run_outcome

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_pass(workspace)
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED, profile_name="preflight"
    )
    mock_dev.side_effect = [_max_iter_no_submit_result(), _max_iter_no_submit_result()]

    result = run_task(config, task)

    assert result.success is False
    # dev_phase recorded the cut-off on state at the branch that ended the run...
    assert result.state.dev_max_iterations_no_submit_terminated is True
    # ...and the bridge carries it across the seam as a harness termination, so
    # the aggregator segregates the run instead of decrementing the model.
    outcome = build_run_outcome(config, result.state, result.success)
    assert outcome.dev_termination_cause == "max_iterations_no_submit"
    assert outcome.dev_timeout_killed is False
    mock_pool.assert_not_called()


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_no_submit_retry_that_completes_is_credited_to_the_model(
    mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
):
    """End to end: a no-submit attempt whose retry finishes still credits the model.

    The coordinator ends a run for this reason only when the retry budget is
    spent. Here the first attempt burned its iterations without submitting but
    the retry completed and the run succeeded — the coordinator never terminated
    anything, so this is ordinary model evidence. Segregating it would drop the
    model's completed work from its capability history, denying it credit for a
    success it earned (#2921 review iter 1).
    """
    from theforge.coordinator.model_profiles_bridge import build_run_outcome

    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_pass(workspace)
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED, profile_name="preflight"
    )
    mock_dev.side_effect = [_max_iter_no_submit_result(), _make_agent_result(profile_name="dev")]
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    result = run_task(config, task)

    assert result.success is True
    # The no-submit classification did occur on the first attempt...
    assert result.state.dev_results[0].failure_code == "max_iterations_reached"
    # ...but the coordinator never ended the run for it, so the terminal flag is
    # clear and the run folds into capability stats as the success it was.
    assert result.state.dev_max_iterations_no_submit_terminated is False
    outcome = build_run_outcome(config, result.state, result.success)
    assert outcome.dev_termination_cause is None
    assert outcome.dev_success is True


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_audit_records_distinct_code_for_max_iterations_without_submit(
    mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
):
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_pass(workspace)
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED, profile_name="preflight"
    )
    mock_dev.side_effect = [_max_iter_no_submit_result(), _make_agent_result(profile_name="dev")]
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    result = run_task(config, task)
    audit = generate_audit_log(config, task, result)

    assert audit["outcome"]["error_type"] == "max_iterations_no_submit"


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch("theforge.coordinator.dev_phase.build_dev_prompt")
@patch_gate_shell()
def test_followup_after_max_iterations_no_submit_preserves_gate_failure_feedback(
    mock_shell, mock_build_dev_prompt, mock_dev, mock_preflight, mock_pool, tmp_path
):
    config = _make_config(tmp_path)
    config = config.__class__(
        **{
            **config.__dict__,
            "dev_profile": config.dev_profile.__class__(
                **{**config.dev_profile.__dict__, "budget_usd": 10.0}
            ),
            "retry": config.retry.__class__(max_dev_iterations=3, max_review_cycles=2),
        }
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_pass(workspace, ["FAIL", "PASS"])
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED, profile_name="preflight"
    )
    mock_build_dev_prompt.return_value = "dev prompt"

    def dev_side_effect(**kwargs):
        if mock_dev.call_count == 1:
            return _make_agent_result(profile_name="dev")
        if mock_dev.call_count == 2:
            return _max_iter_no_submit_result(profile_name="dev")
        return _make_agent_result(profile_name="dev")

    mock_dev.side_effect = dev_side_effect
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    result = run_task(config, task)

    assert result.success is True
    assert mock_build_dev_prompt.call_count == 3
    retry_feedback = mock_build_dev_prompt.call_args_list[2].kwargs["human_feedback"]
    assert retry_feedback is not None
    assert "Gate output" in retry_feedback
    assert "FAIL" in retry_feedback
    assert "submit tool" in retry_feedback
    assert "Additional retry guidance:" in retry_feedback
    assert result.state.gate_decisions == ["FAIL", "PASS"]


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch("theforge.coordinator.dev_phase.build_dev_prompt")
@patch_gate_shell()
def test_repeated_no_submit_retries_do_not_duplicate_guidance_block(
    mock_shell, mock_build_dev_prompt, mock_dev, mock_preflight, mock_pool, tmp_path
):
    config = _make_config(tmp_path)
    config = config.__class__(
        **{
            **config.__dict__,
            "dev_profile": config.dev_profile.__class__(
                **{**config.dev_profile.__dict__, "budget_usd": 10.0}
            ),
            "retry": config.retry.__class__(max_dev_iterations=5, max_review_cycles=2),
        }
    )
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_pass(workspace, ["FAIL", "FAIL", "PASS"])
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED, profile_name="preflight"
    )
    mock_build_dev_prompt.return_value = "dev prompt"
    mock_dev.side_effect = [
        _make_agent_result(profile_name="dev"),
        _max_iter_no_submit_result(profile_name="dev"),
        _make_agent_result(profile_name="dev"),
        _max_iter_no_submit_result(profile_name="dev"),
        _make_agent_result(profile_name="dev"),
    ]
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    result = run_task(config, task)

    assert result.success is True
    assert mock_build_dev_prompt.call_count == 5
    retry_feedback = mock_build_dev_prompt.call_args_list[4].kwargs["human_feedback"]
    assert retry_feedback is not None
    assert retry_feedback.count("Additional retry guidance:") == 1
    assert retry_feedback.count("submit tool") == 1
    assert "Gate output" in retry_feedback
    assert result.state.gate_decisions == ["FAIL", "FAIL", "PASS"]


def test_sprint_audit_records_stable_outcome_code(tmp_path):
    task = _make_task(tmp_path)
    state_result = type("R", (), {})()
    state_result.phase = type("P", (), {"name": "ESCALATE"})
    state_result.merge = None
    state_result.state = type(
        "S",
        (),
        {
            "total_cost": 0.0,
            "preflight_verdict": None,
            "preflight_cached_original_verdict": None,
            "preflight_cached_from_run_id": None,
            "error": "max iterations without submit",
            "error_type": "max_iterations_no_submit",
            "dev_iteration_telemetry": [],
            "review_iteration_telemetry": [],
            "review_results": [],
            "review_cycle_metadata": [],
            "run_id": "run-1",
        },
    )()

    manifest = type("M", (), {"name": "s", "budget_usd": 1.0, "max_parallel": 1})()
    sprint_result = type(
        "SR",
        (),
        {
            "results": [(str(task.story_path), state_result)],
            "total_cost_usd": 0.0,
            "specs_total": 1,
            "specs_succeeded": 0,
            "specs_failed": 1,
            "specs_skipped": 0,
            "stopped_reason": None,
        },
    )()

    _write_sprint_audit(
        project_root=tmp_path,
        manifest=manifest,
        result=sprint_result,
        canonical_refs=[str(task.story_path)],
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        finished_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        duration=0.1,
        sprint_id="sprint-1",
        tasks_by_slug={task.slug: task},
        slug_map={str(task.story_path): task.slug},
    )

    audit = yaml.safe_load((tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text())
    assert audit["specs"][0]["outcome_code"] == "max_iterations_no_submit"


def _met_without_pass_handoff() -> dict:
    """A completion claim (AC MET) with no self-reported gate_result — the
    coordinator-owned-gate contract the retry prompt now uses."""
    return {
        "summary": "Implemented after the submit-pressure retry.",
        "commits": [{"sha": "abc1234", "message": "feat(x): implement"}],
        "acceptance_criteria": [{"criterion": "It works", "status": "MET", "notes": "done"}],
        "story_deviations": "none",
        "deferred_items": "none",
    }


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_max_iterations_retry_completing_met_without_pass_delegates_not_escalates(
    mock_shell, mock_dev, mock_preflight, mock_pool, tmp_path
):
    """Regression (#1944): a max_iterations_no_submit retry that completes honestly
    with MET and no self-reported gate PASS must delegate to the coordinator's
    authoritative VALIDATE gate — not trip HANDOFF_NO_GATE_EVIDENCE at the dev seam.
    The retry prompt uses the coordinator-owned-gate contract, so its handoff omits
    a self-reported PASS by design; gate delegation must hold on this path too."""
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_pass(workspace)
    mock_preflight.return_value = _make_agent_result(
        success=True, output=PREFLIGHT_PROCEED, profile_name="preflight"
    )
    mock_dev.side_effect = [
        _max_iter_no_submit_result(),
        _make_agent_result(
            success=True,
            output="Done.",
            profile_name="dev",
            dev_handoff=_met_without_pass_handoff(),
        ),
    ]
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    result = run_task(config, task)

    # Not escalated at the dev seam: the retry delegated and reached VALIDATE.
    assert result.success is True
    assert "without gate PASS evidence" not in (result.message or "")
    assert result.state.gate_delegated_this_iteration is True
    assert "PASS" in result.state.gate_decisions
    assert all(
        t.gate_result != "HANDOFF_NO_GATE_EVIDENCE" for t in result.state.dev_iteration_telemetry
    )
