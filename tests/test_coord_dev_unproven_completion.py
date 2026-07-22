"""Seam-level tests for the dev-phase unproven-completion guard (#927).

A dev that exits successfully but hands off a completion claim (an acceptance
criterion marked MET) without gate PASS evidence must be escalated at the dev
seam — the coordinator catches what the dev should have declared as a blocking
failure. A well-formed completion (MET + gate_result PASS) must pass the guard.
"""

from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.coordinator.engine import run_from_review, run_task
from theforge.coordinator.state import Phase


def _completion_handoff(*, gate_result: str | None) -> dict:
    """A dev handoff that marks its acceptance criterion MET (a completion claim)."""
    data: dict = {
        "summary": "Implemented the thing.",
        "commits": [{"sha": "abc1234", "message": "feat(x): implement"}],
        "acceptance_criteria": [{"criterion": "It works", "status": "MET", "notes": "tested"}],
        "story_deviations": "none",
        "deferred_items": "none",
    }
    if gate_result is not None:
        data["gate_result"] = gate_result
    return data


class TestUnprovenCompletionGuard:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_completion_without_gate_evidence_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """A successful dev claiming MET with no gate_result → ESCALATE at dev seam."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True,
            output="Done.",
            profile_name="dev",
            dev_handoff=_completion_handoff(gate_result=None),
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "without gate PASS evidence" in result.message
        # Escalated at the dev seam before any review cycle ran.
        assert mock_pool.call_count == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_completion_with_gate_blocked_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """MET + gate_result BLOCKED is still an unproven completion → ESCALATE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True,
            output="Done.",
            profile_name="dev",
            dev_handoff=_completion_handoff(gate_result="BLOCKED"),
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "without gate PASS evidence" in result.message

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_well_formed_completion_is_not_escalated(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """MET + gate_result PASS passes the guard and proceeds to review → DONE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True,
            output="Done.",
            profile_name="dev",
            dev_handoff=_completion_handoff(gate_result="PASS"),
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase != Phase.ESCALATE
        # The unproven-completion guard did not block the review cycle.
        assert mock_pool.call_count >= 1


def _delegated_fix_handoff() -> dict:
    """A review-fix handoff that marks criteria MET, omits gate PASS (the fix
    prompt told the agent not to re-run the gate), and records the coordinator
    gate delegation via ``gate_delegated: true``."""
    return {
        "summary": "Fixed the P1 finding from review.",
        "commits": [{"sha": "def5678", "message": "fix(x): address review finding"}],
        "acceptance_criteria": [{"criterion": "It works", "status": "MET", "notes": "fixed"}],
        "gate_delegated": True,
        "story_deviations": "none",
        "deferred_items": "none",
    }


class TestDelegatedFixHandoffCrossPhase:
    """Seam-level DEV→VALIDATE→REVIEW coverage for the gate-delegation exception
    (#1871). A review-rejected first commit followed by a fix handoff that marks
    criteria MET without self-reported gate PASS must NOT escalate at the dev
    seam: the coordinator's own authoritative VALIDATE gate runs on the latest
    fix commit and routes to re-review or gate-failure retry based on that
    result.
    """

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_delegated_fix_handoff_validates_latest_commit_and_reroutes(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """REQUEST_CHANGES → delegated fix handoff (MET, no PASS) → gate PASS on
        the fix commit → re-review APPROVE → DONE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_agent.return_value = _make_agent_result(
            success=True,
            output="Fixed.",
            profile_name="dev",
            dev_handoff=_delegated_fix_handoff(),
        )

        pool_n = {"n": 0}

        def pool_side(**kwargs):
            pool_n["n"] += 1
            if pool_n["n"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side

        result = run_from_review(config, task, workspace)

        assert result.success is True
        assert result.phase == Phase.DONE
        # The delegated fix handoff did NOT escalate at the dev seam.
        assert "without gate PASS evidence" not in (result.message or "")
        # It proceeded to VALIDATE, which recorded the authoritative gate result
        # for the latest fix commit.
        assert result.state.gate_decisions == ["PASS"]
        # Exactly one dev fix iteration ran, and the coordinator knew it delegated
        # the gate for that iteration.
        assert len(result.state.dev_results) == 1
        assert result.state.gate_delegated_this_iteration is True
        # Re-review ran on the fix commit (second review cycle → APPROVE).
        assert result.state.review_cycle == 2
        # No HANDOFF_NO_GATE_EVIDENCE telemetry was emitted for the delegated iter.
        assert all(
            t.gate_result != "HANDOFF_NO_GATE_EVIDENCE"
            for t in result.state.dev_iteration_telemetry
        )

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_delegated_fix_handoff_gate_fail_routes_to_retry_not_escalation(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """A delegated fix handoff whose coordinator gate FAILs routes to the
        authoritative gate-failure retry (recording FAIL for the fix commit),
        NOT the dev-seam unproven-completion escalation."""
        config = _make_config(tmp_path)  # max_dev_iterations=2
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        # Gate FAILs on the delegated fix commit, then PASSes on the retry.
        mock_shell.side_effect = _shell_with_gate(workspace, ["FAIL", "PASS"])

        # Iter 1: delegated fix handoff (MET, no PASS). Iter 2 (GATE_FAIL retry,
        # not a review-fix): a well-formed completion with gate_result PASS.
        agent_n = {"n": 0}

        def agent_side(*args, **kwargs):
            agent_n["n"] += 1
            if agent_n["n"] == 1:
                return _make_agent_result(
                    success=True,
                    output="Fixed.",
                    profile_name="dev",
                    dev_handoff=_delegated_fix_handoff(),
                )
            handoff = _delegated_fix_handoff()
            del handoff["gate_delegated"]
            handoff["gate_result"] = "PASS"
            return _make_agent_result(
                success=True, output="Fixed again.", profile_name="dev", dev_handoff=handoff
            )

        mock_agent.side_effect = agent_side

        pool_n = {"n": 0}

        def pool_side(**kwargs):
            pool_n["n"] += 1
            if pool_n["n"] == 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side

        result = run_from_review(config, task, workspace)

        # The delegated fix handoff was NOT escalated at the dev seam; the gate
        # FAIL was recorded authoritatively and drove a gate-failure retry.
        assert "without gate PASS evidence" not in (result.message or "")
        assert result.state.gate_decisions == ["FAIL", "PASS"]
        assert result.success is True
        assert result.phase == Phase.DONE
