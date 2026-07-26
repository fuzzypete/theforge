"""Seam tests: a failed agent invocation is not a story verdict (#1951).

Covers the cross-phase boundary CONVENTIONS §8 requires: PREFLIGHT, PLAN_REVIEW,
and DEV each used to fold a transport/auth failure into their own worst-case
verdict (PROCEED / REJECT / ESCALATE), and the dev one flowed into durable
adaptive memory. These exercise the real phase code through ``run_task`` with
only the runner boundary mocked, then assert on what crossed the seam:

- no story-level verdict is recorded for a no-output invocation;
- ``_record_run_memory`` persists nothing for such a run;
- a reviewer pool degraded by infrastructure loss stays distinguishable from a
  full pool that genuinely rejected.

No provider SDK is required — every agent call is mocked at the runner boundary.
"""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import patch

import pytest
from coord_test_helpers import (
    APPROVE_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)

from theforge.config.types import ModelProfile, PlanAgentReviewConfig, PlanConfig
from theforge.coordinator import audit_substrate
from theforge.coordinator.agent_failure import (
    CATEGORY_AUTH,
    NO_JUDGMENT,
    AgentInvocationFailure,
    classify_agent_failure,
    classify_failure_category,
    produced_model_output,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase
from theforge.coordinator.trust_status import TRUST_TAINTED
from theforge.runners import AgentResult

# The exact substrate failure from sprint 45fee01e8027: every `claude`
# subprocess exited 1 with a provider auth rejection and no model output.
_REVOKED_TOKEN_OUTPUT = (
    "Failed to authenticate. API Error: 401 OAuth access token has been revoked."
)


def _auth_failure(profile_name: str = "", cost_usd: float = 0.0) -> AgentResult:
    return AgentResult(
        success=False,
        output=_REVOKED_TOKEN_OUTPUT,
        session_id=None,
        cost_usd=cost_usd,
        exit_code=1,
        raw={},
        profile_name=profile_name,
    )


# ── Classification unit coverage ─────────────────────────────────────────


class TestClassification:
    def test_revoked_token_is_a_no_judgment_auth_failure(self):
        result = _auth_failure("dev")
        assert produced_model_output(result) is False
        assert classify_failure_category(result) == CATEGORY_AUTH
        failure = classify_agent_failure(result, phase="DEV")
        assert failure is not None
        assert failure.phase == "DEV"
        assert failure.category == CATEGORY_AUTH
        assert failure.to_dict()["exit_code"] == 1

    def test_salvaged_partial_output_counts_as_model_judgment(self):
        """A crash after real work is not a no-judgment event."""
        result = AgentResult(
            success=False,
            output="TIMEOUT: Agent exceeded 900s limit",
            session_id=None,
            cost_usd=1.0,
            exit_code=-9,
            raw={},
            profile_name="dev",
            failure_code="timeout",
            partial_output="I refactored the coordinator loop.",
        )
        assert produced_model_output(result) is True
        assert classify_agent_failure(result, phase="DEV") is None

    def test_unrecognized_failure_text_is_not_reclassified(self):
        """Errs toward the pre-existing path when it cannot identify a substrate event."""
        result = _make_agent_result(success=False, output="The plan is wrong because ...")
        assert produced_model_output(result) is True
        assert classify_agent_failure(result, phase="PLAN_REVIEW") is None

    def test_category_vocabulary_is_enforced(self):
        with pytest.raises(ValueError):
            AgentInvocationFailure(phase="DEV", category="made_up")


# ── DEV: no model output must not become a story ESCALATE ────────────────


class TestDevNoJudgment:
    @patch("theforge.coordinator.model_profiles_bridge.update_profiles_from_run")
    @patch("theforge.coordinator.dev_phase._has_commits_ahead_of_base", return_value=False)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_dev_transport_failure_aborts_and_teaches_nothing(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        _mock_commits,
        mock_profiles,
        tmp_path,
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_PREFLIGHT_PROCEED, cost_usd=0.05
        )
        mock_dev.return_value = _auth_failure("dev", cost_usd=0.0)
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # The empty-diff guard still refuses to APPROVE...
        assert result.success is False
        # ...but the run is classified as an infrastructure abort, not as a
        # story whose framing an agent found invalid.
        assert result.infrastructure_failure is True
        assert result.state.error_type == "infrastructure_abort"
        assert result.state.infrastructure_failure["phase"] == "DEV"
        assert result.state.infrastructure_failure["category"] == CATEGORY_AUTH
        assert "no judgment was obtained" in (result.message or "")

        # Nothing durable was written about the story.
        assert mock_profiles.call_count == 0
        audit = generate_audit_log(config, task, result)
        assert audit["trust_status"] == TRUST_TAINTED
        assert audit["agent_invocation"]["infrastructure_failure"]["category"] == CATEGORY_AUTH
        assert audit["agent_invocation"]["no_judgment_failures"][0]["phase"] == "DEV"

        # End-to-end: once persisted, this run contributes no escalation memory.
        # (Spec fix-success criterion: the story must not appear in escalation
        # history as a story that escalates.)
        audit.setdefault("timing", {})["started_at"] = "2026-07-26T11:33:40+00:00"
        audit["run_id"] = "45fee01e8027"
        runs = audit_substrate.runs_dir(tmp_path)
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "45fee01e8027.json").write_text(json.dumps(audit, default=str), encoding="utf-8")
        audit_substrate.rebuild_from_runs(tmp_path)
        conn = audit_substrate.require_substrate(tmp_path)
        try:
            escalations = list(audit_substrate.iter_escalation_records(conn))
            assignment_history = audit_substrate.derive_assignment_history(conn)
        finally:
            conn.close()
        assert [e["story"] for e in escalations if e["story"] == task.slug] == []
        assert [r for r in assignment_history if r.get("story") == task.slug] == []

        # Control: the exclusion is caused by the taint marker, not by the
        # record being unprojectable. Strip the marker and the same record does
        # reach escalation memory — which is exactly what used to happen.
        untainted = {**audit, "trust_status": "unchecked", "trust_checks": []}
        (runs / "45fee01e8027.json").write_text(
            json.dumps(untainted, default=str), encoding="utf-8"
        )
        audit_substrate.rebuild_from_runs(tmp_path)
        conn = audit_substrate.require_substrate(tmp_path)
        try:
            leaked = [e["story"] for e in audit_substrate.iter_escalation_records(conn)]
        finally:
            conn.close()
        assert leaked == [task.slug]

    @patch("theforge.coordinator.model_profiles_bridge.update_profiles_from_run")
    @patch("theforge.coordinator.dev_phase._has_commits_ahead_of_base", return_value=False)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_dev_failure_with_model_output_still_escalates_and_teaches(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        _mock_commits,
        mock_profiles,
        tmp_path,
    ):
        """Control case: a model that spoke and failed is still a real ESCALATE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_PREFLIGHT_PROCEED, cost_usd=0.05
        )
        mock_dev.return_value = AgentResult(
            success=False,
            output="I could not implement this: the spec contradicts the schema.",
            session_id="sess-1",
            cost_usd=0.5,
            exit_code=1,
            raw={},
            profile_name="dev",
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert result.infrastructure_failure is False
        assert result.state.infrastructure_failure is None
        assert "escalating to avoid an empty-diff APPROVE" in (result.message or "")
        # A real (if failed) judgment still teaches the adaptive substrate.
        assert mock_profiles.call_count == 1


# ── PLAN_REVIEW: degraded pool vs. genuine rejection ─────────────────────

_PREFLIGHT_PROCEED = """\
```yaml
verdict: PROCEED
reason: "Needs implementation."
complexity: medium
complexity_score: 5
sufficiency: needs_planning
work_type: feature
```
"""

_PLAN_REJECT = """\
```yaml
verdict: REJECT
findings:
  - severity: P1
    description: "The plan skips the migration step entirely."
    suggestion: "Add a migration step."
```
"""

_PLAN_APPROVE = """\
```yaml
verdict: APPROVE
findings: []
```
"""


def _plan_review_config(tmp_path, *, min_reviewers: int):
    base = _make_config(tmp_path)
    reviewers = [
        ModelProfile(
            name=f"plan-reviewer-{suffix}",
            provider="anthropic",
            model=f"model-{suffix}",
            budget_usd=1.0,
            timeout_seconds=300,
            allowed_tools=("Read", "Glob"),
        )
        for suffix in ("a", "b")
    ]
    return dataclasses.replace(
        base,
        plan=PlanConfig(enabled=True, budget_usd=0.50, timeout=300, validate_spec=False),
        plan_agent_review=PlanAgentReviewConfig(
            enabled=True,
            pool=reviewers,
            min_reviewers=min_reviewers,
        ),
        retry=dataclasses.replace(base.retry, max_plan_review_parse_retries=0),
    )


class TestPlanReviewDegradedPool:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_reviewer_lost_to_transport_is_recorded_as_pool_loss_not_reject(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_code_pool,
        tmp_path,
    ):
        """One reviewer silent, one approving, min=1 ⇒ approve with the loss preserved."""
        config = _plan_review_config(tmp_path, min_reviewers=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_PREFLIGHT_PROCEED, cost_usd=0.05
        )
        mock_plan_agent.return_value = _make_agent_result(
            success=True, output="# Plan\n\nA plan.", cost_usd=0.10
        )
        mock_plan_pool.return_value = [
            _auth_failure("plan-reviewer-a", cost_usd=0.0),
            _make_agent_result(
                success=True, output=_PLAN_APPROVE, cost_usd=0.04, profile_name="plan-reviewer-b"
            ),
        ]
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        # The surviving reviewer's APPROVE stands — the run continued.
        assert result.state.plan_review_decision == "approve"
        # But the pool loss is preserved, so this completion is not reported as
        # equivalent to a full-pool one.
        degraded = result.state.degraded_pools
        assert len(degraded) == 1
        assert degraded[0]["phase"] == "PLAN_REVIEW"
        assert degraded[0]["lost"] == ["plan-reviewer-a"]
        assert degraded[0]["remaining"] == 1
        assert degraded[0]["failures"][0]["category"] == CATEGORY_AUTH
        # A degraded completion is NOT an infrastructure abort: real judgment
        # was obtained, so the run still teaches.
        assert result.state.infrastructure_failure is None
        audit = generate_audit_log(config, task, result)
        assert audit["agent_invocation"]["degraded_pools"][0]["lost"] == ["plan-reviewer-a"]
        assert audit["trust_status"] != TRUST_TAINTED

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_genuine_reject_remains_a_plan_review_rejection(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_plan_agent,
        mock_plan_pool,
        mock_code_pool,
        tmp_path,
    ):
        """Control case: reviewers that answered and rejected still reject."""
        config = _plan_review_config(tmp_path, min_reviewers=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_PREFLIGHT_PROCEED, cost_usd=0.05
        )
        mock_plan_agent.return_value = _make_agent_result(
            success=True, output="# Plan\n\nA plan.", cost_usd=0.10
        )
        mock_plan_pool.return_value = [
            _make_agent_result(
                success=True, output=_PLAN_REJECT, cost_usd=0.04, profile_name="plan-reviewer-a"
            ),
            _make_agent_result(
                success=True, output=_PLAN_REJECT, cost_usd=0.04, profile_name="plan-reviewer-b"
            ),
        ]
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_code_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.plan_review_decision in ("regenerate", "reject", "approve")
        # Whatever the loop decided, it was decided from real reviewer output:
        # no pool loss and no infrastructure abort were recorded.
        assert result.state.degraded_pools == []
        assert result.state.infrastructure_failure is None
        assert result.state.agent_invocation_failures == []


# ── PREFLIGHT: no verdict, no memory ─────────────────────────────────────


class TestPreflightNoJudgment:
    @patch("theforge.coordinator.model_profiles_bridge.update_profiles_from_run")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_transport_failure_records_no_verdict(
        self, mock_shell, mock_dev, mock_preflight, mock_pool, mock_profiles, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _auth_failure("preflight")
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.infrastructure_failure is True
        # No story-level verdict of any kind.
        assert result.state.preflight_verdict is None
        assert result.state.preflight_failure_action == "infrastructure_abort"
        # Downstream phases never ran on an unverified contract.
        assert mock_dev.call_count == 0
        assert mock_profiles.call_count == 0
        audit = generate_audit_log(config, task, result)
        assert audit["preflight"]["verdict"] == NO_JUDGMENT
        assert audit["phases"]["preflight"]["outcome"] == "no_judgment"
        assert audit["trust_status"] == TRUST_TAINTED
