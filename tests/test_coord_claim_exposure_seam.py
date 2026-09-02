"""Claim-exposure capture reaches every prior-run injection path (#2684).

Seam-level because the property is a cross-phase one: the uptake indicator can
only tell what an author could have acted on if *every* place that renders a
prior-run claim into a prompt records who received it and when. A path that
assembles context without that metadata does not fail loudly — it silently
contributes claims tagged with nobody, which the comparison must then refuse to
treat as eligible. So each injection site is asserted here rather than trusted.
"""

from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import patch_gate_shell

from tests.coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_plan_config,
    _make_task,
    _shell_with_gate,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.review_pool import _run_review_pool
from theforge.coordinator.state import CoordinatorState, ReviewCycleMetadata
from theforge.task.context_assembler import ContextPack

_PLAN_OUTPUT = (
    "```yaml\n"
    "plan:\n"
    "  approach: Update app behavior and coverage.\n"
    "  steps:\n"
    "    - id: 1\n"
    "      description: Update code\n"
    "      files:\n"
    "        - src/app.py\n"
    "        - tests/test_app.py\n"
    "      action: modify\n"
    "      details: Implement the feature.\n"
    "```\n"
)

_PREFLIGHT_OUTPUT = (
    "```yaml\n"
    "verdict: PROCEED\n"
    "complexity: medium\n"
    "reason: planning needed\n"
    "likely_files:\n"
    "  - src/app.py\n"
    "criteria_checked:\n"
    "  - criterion: Feature X\n"
    "    satisfied: false\n"
    "    evidence: Not found in codebase\n"
    "```\n"
)


class _AssemblerSpy:
    calls: list[dict] = []

    @classmethod
    def from_config(cls, _config):
        return cls()

    def assemble(self, **kwargs):
        type(self).calls.append(kwargs)
        return ContextPack(
            content="",
            included=(),
            dropped=(),
            budget=kwargs.get("budget", 0) or 0,
            line_count=0,
            phase=kwargs["phase"],
            structural_index_git_sha=None,
        )


def _meta() -> ReviewCycleMetadata:
    return ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_every_phase_records_the_role_and_iteration_that_received_its_claims(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=_PREFLIGHT_OUTPUT, profile_name="preflight"
    )
    mock_plan.return_value = _make_agent_result(
        success=True, output=_PLAN_OUTPUT, profile_name="plan"
    )
    mock_dev.return_value = _make_agent_result(success=True, output="Done.", profile_name="dev")
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]
    _AssemblerSpy.calls = []

    with (
        patch("theforge.coordinator.preflight_flow.ContextAssembler", _AssemblerSpy),
        patch("theforge.coordinator.plan_flow.ContextAssembler", _AssemblerSpy),
        patch("theforge.coordinator.dev_phase.ContextAssembler", _AssemblerSpy),
        patch("theforge.coordinator.review_pool.ContextAssembler", _AssemblerSpy, create=True),
    ):
        result = run_task(config, task)

    assert result.success is True
    by_phase = {call["phase"]: call for call in _AssemblerSpy.calls}
    assert set(by_phase) == {"preflight", "plan", "dev", "review"}

    # Every phase names its recipient. An unattributed claim is not eligible to
    # explain anything, so a missing role here would silently zero the indicator.
    for phase, call in by_phase.items():
        assert call["agent_role"], f"{phase} assembled context without naming a recipient role"
        assert call["phase_iteration"] is not None, f"{phase} recorded no phase iteration"

    assert by_phase["dev"]["agent_role"] == "dev"
    assert by_phase["review"]["agent_role"] == "review"
    assert by_phase["plan"]["agent_role"] == "plan"
    assert by_phase["preflight"]["agent_role"] == "preflight"


@patch("theforge.coordinator.review_pool.log_agent_result")
@patch("theforge.coordinator.review_pool.run_agent_pool")
def test_pooled_review_records_the_reviewer_role_and_cycle(mock_pool, _mock_log, tmp_path):
    """The normal review path is the pool, not the review-only entry point.

    Its manifest append is a separate call site; without its own capture, every
    ordinary run would render reviewer claims tagged with nobody instead of
    explicitly review-only — and the eligibility filter would have nothing to
    exclude them by.
    """
    config = _make_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = CoordinatorState(
        review_cycle=2,
        log_dir=tmp_path / "logs",
        plan_structured={"steps": [{"files": ["src/right.py"]}]},
    )
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]
    _AssemblerSpy.calls = []

    with patch("theforge.coordinator.review_pool.ContextAssembler", _AssemblerSpy, create=True):
        _run_review_pool(
            state,
            config,
            task,
            "story",
            workspace,
            "branch",
            _meta(),
            notify=False,
            enforce_budgets=False,
        )

    call = _AssemblerSpy.calls[0]
    assert call["phase"] == "review"
    assert call["agent_role"] == "review"
    assert call["phase_iteration"] == 2


@patch("theforge.coordinator.review_pool.run_agent_pool")
@patch("theforge.coordinator.plan_flow.run_agent")
@patch("theforge.coordinator.preflight_flow.run_agent")
@patch("theforge.coordinator.dev_phase.run_agent")
@patch_gate_shell()
def test_recorded_manifests_carry_claim_exposure_and_reach_the_audit_record(
    mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
):
    """End of the seam: what the phases captured is what the run's record says."""
    from theforge.coordinator.audit import generate_audit_log

    config = _make_plan_config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / task.slug
    workspace.mkdir()

    mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
    mock_preflight.return_value = _make_agent_result(
        success=True, output=_PREFLIGHT_OUTPUT, profile_name="preflight"
    )
    mock_plan.return_value = _make_agent_result(
        success=True, output=_PLAN_OUTPUT, profile_name="plan"
    )
    mock_dev.return_value = _make_agent_result(success=True, output="Done.", profile_name="dev")
    mock_pool.return_value = [
        _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
    ]

    result = run_task(config, task)
    record = generate_audit_log(config, task, result)

    for entry in record["context_manifests"]:
        exposure = entry["prior_run_context"]["claim_exposure"]
        assert exposure["capture_version"] == 1
        assert exposure["agent_role"]
        assert exposure["rendered_at"]

    uptake = record["prior_run_uptake"]
    # Capture is present, so this run is comparable — never "uncomparable".
    assert uptake["status"] != "uncomparable_pre_capture"
    assert uptake["method"]["name"] == "rendered-claim-overlap"
    assert "missed-uptake indicator only" in uptake["interpretation"]
