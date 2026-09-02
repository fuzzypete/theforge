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
import subprocess
from pathlib import Path
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

from theforge.agent_types import (
    FAILURE_ENDED_WITHOUT_RESULT,
    FAILURE_KILLED_BEFORE_OUTPUT,
    KILLED_BEFORE_OUTPUT_MARKER,
    ModelUsage,
)
from theforge.config.types import ModelProfile, PlanAgentReviewConfig, PlanConfig
from theforge.coordinator import audit_substrate
from theforge.coordinator.agent_failure import (
    CATEGORY_AUTH,
    CATEGORY_NO_RESULT,
    NO_JUDGMENT,
    AgentInvocationFailure,
    classify_agent_failure,
    classify_failure_category,
    produced_model_output,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.dev_phase import _run_dev_phase
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import CoordinatorState, Phase
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


def _generic_process_failure(profile_name: str = "dev") -> AgentResult:
    return AgentResult(
        success=False,
        output="runner exited before agent output was available",
        session_id=None,
        cost_usd=0.0,
        exit_code=1,
        raw={},
        profile_name=profile_name,
    )


def _transient_transport_failure(
    *, profile_name: str = "dev", cost_usd: float | None = 0.42
) -> AgentResult:
    return AgentResult(
        success=False,
        output="http 429 rate limited",
        session_id=None,
        cost_usd=cost_usd,
        exit_code=1,
        raw={},
        profile_name=profile_name,
        failure_code="rate_limit",
    )


def _ended_without_result_failure(profile_name: str = "dev") -> AgentResult:
    return AgentResult(
        success=False,
        output="CLAUDE_STREAM_NO_TEXT: reason=missing_result_event",
        session_id=None,
        cost_usd=0.0,
        exit_code=-9,
        raw={},
        profile_name=profile_name,
        failure_code=FAILURE_ENDED_WITHOUT_RESULT,
    )


def _killed_before_output_failure(profile_name: str = "dev") -> AgentResult:
    """The result `runner_claude` now returns for a #2832 invocation that never ran.

    Kept faithful to what the runner actually builds — measured ``cost_usd=0.0``,
    no session, empty ``raw`` — because the retry refund turns on exactly those
    fields. ``tests/test_runner_claude.py`` proves a real subprocess produces
    this shape; this fixture is how the coordinator half is exercised without one.
    """
    return AgentResult(
        success=False,
        output=KILLED_BEFORE_OUTPUT_MARKER,
        session_id=None,
        cost_usd=0.0,
        exit_code=-9,
        raw={},
        profile_name=profile_name,
        failure_code=FAILURE_KILLED_BEFORE_OUTPUT,
    )


def _cost_unknown_no_result_failure(profile_name: str = "dev") -> AgentResult:
    """The pre-#2832 shape for the same event: same exit, but spend unreadable.

    This is what the runner used to return for a killed-before-anything
    invocation. It is retained as a contrast case, not as history: an invocation
    whose spend genuinely could not be read must keep consuming its retry slot,
    because forge cannot show that nothing was billed.
    """
    return AgentResult(
        success=False,
        output="CLAUDE_STREAM_NO_TEXT: reason=missing_result_event",
        session_id=None,
        cost_usd=None,
        exit_code=-9,
        raw={},
        profile_name=profile_name,
        failure_code=FAILURE_ENDED_WITHOUT_RESULT,
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / ".gitignore").write_text(".forge/\nspec.md\n", encoding="utf-8")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


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

    def test_zero_charge_process_failure_without_model_artifacts_is_process(self):
        result = _generic_process_failure()
        assert produced_model_output(result) is False
        failure = classify_agent_failure(result, phase="DEV")
        assert failure is not None
        assert failure.category == "process"
        assert failure.exit_code == 1

    def test_zero_cost_local_model_usage_still_counts_as_model_output(self):
        result = AgentResult(
            success=False,
            output="Agent loop terminated: max iterations reached after 3 iterations.",
            session_id=None,
            cost_usd=0.0,
            exit_code=1,
            raw={},
            profile_name="dev",
            failure_code="max_iterations_reached",
            model_usage=(
                ModelUsage(
                    model="codestral",
                    input_tokens=100,
                    output_tokens=25,
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    cost_usd=0.0,
                ),
            ),
        )
        assert produced_model_output(result) is True
        assert classify_agent_failure(result, phase="DEV") is None

    def test_agent_ended_without_result_is_not_classified_as_process(self):
        result = _ended_without_result_failure()
        assert produced_model_output(result) is False
        failure = classify_agent_failure(result, phase="DEV")
        assert failure is not None
        assert failure.category == CATEGORY_NO_RESULT
        assert classify_failure_category(result) == CATEGORY_NO_RESULT

    def test_no_text_marker_beats_usage_telemetry(self):
        result = AgentResult(
            success=False,
            output=(
                "CLAUDE_STREAM_NO_TEXT: reason=result_missing_text subtype=error_during_execution"
            ),
            session_id="sess-poisoned",
            cost_usd=0.0,
            exit_code=1,
            raw={},
            profile_name="dev",
            model_usage=(
                ModelUsage(
                    model="claude-sonnet-4-5",
                    input_tokens=321,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    cost_usd=0.0,
                ),
            ),
        )
        assert produced_model_output(result) is False
        failure = classify_agent_failure(result, phase="DEV")
        assert failure is not None
        assert failure.category == "process"

    def test_unrecognized_failure_text_is_not_reclassified(self):
        """Errs toward the pre-existing path when it cannot identify a substrate event."""
        result = _make_agent_result(success=False, output="The plan is wrong because ...")
        assert produced_model_output(result) is True
        assert classify_agent_failure(result, phase="PLAN_REVIEW") is None

    def test_category_vocabulary_is_enforced(self):
        with pytest.raises(ValueError):
            AgentInvocationFailure(phase="DEV", category="made_up")

    @pytest.mark.parametrize(
        "output",
        [
            # Digits that look like status codes but are line numbers, issue
            # refs, counts, and durations in ordinary agent narration. A bare
            # substring match on "401"/"429"/"500" would call each of these a
            # substrate failure and abort a run that a model actually judged.
            "I updated src/theforge/coordinator/engine.py:401 to guard the branch.",
            "Refs #1429; the loop ran 500 times before converging.",
            "The plan is wrong because it skips migration 403.",
            "Timeout budget was 504 seconds, which I lowered to 300.",
            "Response code handling for 401/403 is now covered by tests.",
            # Mentioning an API error is not being one.
            "Fixed the API error handling path in runners/api.py.",
        ],
    )
    def test_status_code_digits_in_agent_prose_are_not_substrate_failures(self, output):
        result = _make_agent_result(success=False, output=output)
        assert produced_model_output(result) is True
        assert classify_agent_failure(result, phase="DEV") is None

    @pytest.mark.parametrize(
        ("output", "expected_category"),
        [
            ("API Error: 401 Unauthorized", "auth"),
            ("Error 403 Forbidden", "auth"),
            ("http 429 rate limited", "transport"),
            ("provider 503 service unavailable", "transport"),
            ("status_code=502 upstream", "transport"),
            ("google.genai.errors.ServerError: 500 INTERNAL", "transport"),
            ("API Error: Connection closed mid-response.", "transport"),
        ],
    )
    def test_status_codes_in_provider_context_are_still_recognized(
        self, output, expected_category
    ):
        """Tightening the digit patterns must not lose real substrate failures."""
        result = _make_agent_result(success=False, output=output)
        assert produced_model_output(result) is False
        assert classify_failure_category(result) == expected_category


# ── DEV: no model output must not become a story ESCALATE ────────────────


class TestDevNoJudgment:
    @pytest.mark.parametrize(
        ("retry_cost", "expected_dev_usd"),
        [
            pytest.param(0.42, 0.42, id="measured"),
            pytest.param(None, None, id="unmeasured"),
        ],
    )
    @patch("theforge.coordinator.model_profiles_bridge.update_profiles_from_run")
    @patch("theforge.coordinator.dev_phase.time.sleep", return_value=None)
    @patch("theforge.coordinator.dev_phase._has_commits_ahead_of_base", return_value=False)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_transport_retry_cost_survives_discounted_zero_charge_abort(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        _mock_commits,
        _mock_sleep,
        mock_profiles,
        tmp_path,
        retry_cost,
        expected_dev_usd,
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_PREFLIGHT_PROCEED, cost_usd=0.05
        )
        mock_dev.side_effect = [
            _transient_transport_failure(cost_usd=retry_cost),
            _generic_process_failure("dev"),
        ]
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.infrastructure_failure is True
        assert result.unused_dev_iteration is True
        assert len(result.state.dev_results) == 1
        assert len(result.state.dev_durations) == 1
        assert result.state.dev_results[0].failure_code == "rate_limit"
        assert result.state.dev_results[0].cost_usd == retry_cost
        assert result.state.dev_iteration_telemetry == []
        assert result.state.budget.consumption_log == []
        assert result.state.budget.total_count == 0
        assert result.state.budget.cycle_count == 0
        assert result.state.budget.remaining() == config.retry.max_dev_iterations
        failure_extra = result.state.infrastructure_failure["extra"]
        assert failure_extra["transport_retry_count"] == 1
        assert len(failure_extra["transport_retry_events"]) == 1
        assert failure_extra["transport_retry_events"][0]["iteration"] == 1
        assert failure_extra["transport_retry_events"][0]["retry"] == 1
        assert "rate_limit" in failure_extra["transport_retry_events"][0]["error"]
        assert "429" in failure_extra["transport_retry_events"][0]["error"]

        audit = generate_audit_log(config, task, result)
        assert audit["iterations"]["usage_summary"]["dev"]["used"] == 0
        assert audit["cost"]["dev_invocations"] == 1
        assert audit["cost"]["dev_usd"] == expected_dev_usd
        assert (
            audit["agent_invocation"]["infrastructure_failure"]["extra"]["transport_retry_events"]
            == failure_extra["transport_retry_events"]
        )
        assert mock_profiles.call_count == 0

    @patch("theforge.coordinator.model_profiles_bridge.update_profiles_from_run")
    @patch("theforge.coordinator.dev_phase._worktree_changed_since_commit", return_value=False)
    @patch("theforge.coordinator.dev_phase._has_commits_ahead_of_base", return_value=True)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_engine_releases_the_reserved_slot_for_a_retry_that_never_ran(
        self,
        mock_shell,
        mock_dev,
        mock_preflight,
        mock_pool,
        _mock_commits,
        _mock_changed,
        mock_profiles,
        tmp_path,
    ):
        """End to end: the slot a never-run iteration reserved comes back (#2832).

        ``_run_dev_phase`` only reports ``unused_dev_iteration``; the engine is
        what acts on it. This drives the whole run so the reservation and the
        release are made by the same budget object, which is the only way to see
        that the story's allowance is actually intact afterwards.
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_PREFLIGHT_PROCEED, cost_usd=0.05
        )
        mock_dev.return_value = _killed_before_output_failure()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.infrastructure_failure is True
        assert result.unused_dev_iteration is True
        # Nothing was drawn down: the story is entitled to every dev iteration it
        # started with, because none of them ran.
        assert result.state.budget.total_count == 0
        assert result.state.budget.consumption_log == []
        assert result.state.budget.remaining() == config.retry.max_dev_iterations
        # And nothing durable was taught about the story.
        assert mock_profiles.call_count == 0

        # The run record has to explain the released slot, so the code that
        # released it is the code the audit carries (#2832's second criterion).
        audit = generate_audit_log(config, task, result)
        failure = audit["agent_invocation"]["infrastructure_failure"]
        assert failure["failure_code"] == FAILURE_KILLED_BEFORE_OUTPUT
        assert failure["category"] == "process"
        assert audit["iterations"]["usage_summary"]["dev"]["used"] == 0

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
        assert result.unused_dev_iteration is True
        assert result.state.error_type == "infrastructure_abort"
        assert result.state.infrastructure_failure["phase"] == "DEV"
        assert result.state.infrastructure_failure["category"] == CATEGORY_AUTH
        assert "no judgment was obtained" in (result.message or "")
        assert result.state.dev_results == []
        assert result.state.dev_durations == []
        assert result.state.dev_iteration_telemetry == []
        assert result.state.dev_iteration == 0

        # Nothing durable was written about the story.
        assert mock_profiles.call_count == 0
        audit = generate_audit_log(config, task, result)
        assert audit["trust_status"] == TRUST_TAINTED
        assert audit["agent_invocation"]["infrastructure_failure"]["category"] == CATEGORY_AUTH
        assert audit["agent_invocation"]["no_judgment_failures"][0]["phase"] == "DEV"
        assert audit["iterations"]["usage_summary"]["dev"]["used"] == 0
        assert audit["cost"]["dev_invocations"] == 0

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

    def test_preexisting_branch_work_unchanged_by_zero_charge_failure_aborts(self, tmp_path):
        _init_repo(tmp_path)
        subprocess.run(["git", "checkout", "-q", "-b", "feat/test-task"], cwd=tmp_path, check=True)
        (tmp_path / "feature.txt").write_text("preserved work\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "preserved"], cwd=tmp_path, check=True)

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        state.adaptive_dev_max = config.retry.max_dev_iterations
        state.budget.max_iterations = config.retry.max_dev_iterations
        state.budget.consume(review_cycle=0)

        with (
            patch(
                "theforge.coordinator.dev_phase.run_agent",
                return_value=_generic_process_failure(),
            ),
            patch("theforge.coordinator.dev_phase.log_agent_result"),
            patch_gate_shell(side_effect=_shell_with_gate(tmp_path, "PASS")),
        ):
            result = _run_dev_phase(
                state,
                config,
                task,
                "# Test Spec\n",
                tmp_path,
                "feat/test-task",
                notify=False,
                logger=None,
            )

        assert result is not None
        assert result.infrastructure_failure is True
        assert result.unused_dev_iteration is True
        assert result.state.infrastructure_failure["category"] == "process"
        assert "left the preserved branch unchanged" in (result.message or "")
        assert result.state.dev_results == []
        assert result.state.dev_iteration_telemetry == []

    def _dev_phase_after_a_committed_iteration(self, tmp_path, dev_result):
        """Run one DEV phase in the #2832 position: a prior iteration committed.

        This is the arrangement the observed run was actually in, and it is the
        one the fix has to survive. Iteration 1 succeeded, cost $1.859 and
        committed; VALIDATE then failed; iteration 2 was killed before it
        produced anything. So the branch has commits ahead of base and is
        unchanged since this iteration started — the second disjunct of
        ``_left_no_observable_work``, not the no-commits-at-all first one.
        """
        _init_repo(tmp_path)
        subprocess.run(["git", "checkout", "-q", "-b", "feat/test-task"], cwd=tmp_path, check=True)
        (tmp_path / "feature.txt").write_text("what iteration 1 committed\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "iteration 1"], cwd=tmp_path, check=True)

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        state.adaptive_dev_max = config.retry.max_dev_iterations
        state.budget.max_iterations = config.retry.max_dev_iterations
        # The slot this iteration reserved. Whether it comes back is the test.
        state.budget.consume(review_cycle=0)
        reserved = state.budget.remaining()

        with (
            patch("theforge.coordinator.dev_phase.run_agent", return_value=dev_result),
            patch("theforge.coordinator.dev_phase.log_agent_result"),
            patch_gate_shell(side_effect=_shell_with_gate(tmp_path, "PASS")),
        ):
            result = _run_dev_phase(
                state,
                config,
                task,
                "# Test Spec\n",
                tmp_path,
                "feat/test-task",
                notify=False,
                logger=None,
            )
        return result, state, config, reserved

    def test_killed_before_output_after_a_committed_iteration_refunds_the_retry(self, tmp_path):
        """A retry that never executed is not charged to the story (#2832).

        The third acceptance criterion, at the seam that decides it. The
        invocation was killed before producing anything, so it is evidence about
        the host and not about the story; spending an allowance on it converts an
        infrastructure fault into an exhausted story.

        Asserted through ``release_unused()`` on the real budget rather than on
        the flag alone, because the flag is only a request — the engine is what
        honours it, and ``test_engine_releases_the_reserved_slot_for_a_retry_that_never_ran``
        covers that half.
        """
        result, state, config, reserved = self._dev_phase_after_a_committed_iteration(
            tmp_path, _killed_before_output_failure()
        )

        assert result is not None
        assert result.infrastructure_failure is True
        assert result.unused_dev_iteration is True
        # A process fact, not "the agent ran and reported nothing".
        assert result.state.infrastructure_failure["category"] == "process"
        assert result.state.infrastructure_failure["failure_code"] == FAILURE_KILLED_BEFORE_OUTPUT
        # The observed run reached this branch by the same clause.
        assert "left the preserved branch unchanged" in (result.message or "")
        # The attempt is rolled out of dev accounting entirely: it never ran, so
        # it is not an iteration the story took.
        assert result.state.dev_results == []
        assert result.state.dev_iteration_telemetry == []

        assert state.budget.release_unused() is True
        assert state.budget.remaining() == reserved + 1 == config.retry.max_dev_iterations

    def test_cost_unknown_invocation_still_spends_its_retry(self, tmp_path):
        """The contrast case: unreadable spend is not proof that nothing was spent.

        Same exit code, same empty output, same position in the run — and
        deliberately the opposite outcome. The refund is earned by the runner
        being able to *show* the invocation produced and cost nothing, not by
        ``exit=-9`` on its own. Without this, #2832's fix would be a blanket
        exemption for every signal-killed dev iteration.
        """
        result, state, _config, reserved = self._dev_phase_after_a_committed_iteration(
            tmp_path, _cost_unknown_no_result_failure()
        )

        assert result is not None
        assert result.infrastructure_failure is True
        assert result.unused_dev_iteration is False
        assert state.budget.remaining() == reserved

    def test_preexisting_branch_work_with_model_output_is_not_reclassified_as_infrastructure(
        self, tmp_path
    ):
        _init_repo(tmp_path)
        subprocess.run(["git", "checkout", "-q", "-b", "feat/test-task"], cwd=tmp_path, check=True)
        (tmp_path / "feature.txt").write_text("preserved work\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "preserved"], cwd=tmp_path, check=True)

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        state.adaptive_dev_max = config.retry.max_dev_iterations
        state.budget.max_iterations = config.retry.max_dev_iterations
        state.budget.consume(review_cycle=0)

        with (
            patch(
                "theforge.coordinator.dev_phase.run_agent",
                return_value=AgentResult(
                    success=False,
                    output="I checked the preserved branch state and it already contains the fix.",
                    session_id="sess-1",
                    cost_usd=0.5,
                    exit_code=1,
                    raw={},
                    profile_name="dev",
                ),
            ),
            patch("theforge.coordinator.dev_phase.log_agent_result"),
            patch_gate_shell(side_effect=_shell_with_gate(tmp_path, "PASS")),
        ):
            result = _run_dev_phase(
                state,
                config,
                task,
                "# Test Spec\n",
                tmp_path,
                "feat/test-task",
                notify=False,
                logger=None,
            )

        assert result is None
        assert state.infrastructure_failure is None
        assert len(state.dev_results) == 1
        assert state.dev_results[0].cost_usd == 0.5
        assert state.dev_iteration_telemetry == []


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
        plan=PlanConfig.of(enabled=True, budget_usd=0.50, timeout=300, validate_spec=False),
        plan_agent_review=PlanAgentReviewConfig.of(
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

    @patch("theforge.coordinator.model_profiles_bridge.update_profiles_from_run")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_no_result_failure_keeps_its_own_category(
        self, mock_shell, mock_dev, mock_preflight, mock_pool, mock_profiles, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _ended_without_result_failure("preflight")
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.infrastructure_failure is True
        assert result.state.preflight_verdict is None
        assert result.state.preflight_failure_action == "infrastructure_abort"
        assert result.state.infrastructure_failure["category"] == CATEGORY_NO_RESULT
        assert mock_dev.call_count == 0
        assert mock_profiles.call_count == 0
