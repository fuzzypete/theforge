"""Tests for the coordinator review phase — pool behaviour.

Covers: REQUEST_CHANGES loop-back, schema-error override, multi-model review,
parse-retry resilience, and review pool resilience.
"""

from __future__ import annotations

from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    PARSE_ERROR_OUTPUT,
    REQUEST_CHANGES_REVIEW,
    SYNTHESIS_PROFILE,
    _make_agent_result,
    _make_config,
    _make_pool_config,
    _make_review_profile,
    _make_task,
    _shell_with_gate,
)

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import Phase

# Invalid verdict "MAYBE" → schema error (repair layer can't fix this).
# Note: coord_test_helpers.SCHEMA_ERROR_OUTPUT uses a different schema error
# (APPROVE with missing fields), so we define the MAYBE-verdict version locally.
_SCHEMA_ERROR_MAYBE_VERDICT = """\
```yaml
verdict: MAYBE
summary: "Looks good."
findings: []
```
"""


class TestCoordinatorReviewRequestChanges:
    """Test that review REQUEST_CHANGES loops back to dev."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_review_then_approve(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)

        assert result.success is True
        assert result.state.review_cycle == 2


class TestCoordinatorSchemaErrorOverride:
    """Test that APPROVE with schema errors triggers reviewer retry (not a full dev cycle)."""

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_approve_with_schema_errors_triggers_retry(
        self, mock_shell, mock_engine_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # Review YAML with invalid verdict — repair layer can't fix this
        malformed_approve = """\
```yaml
verdict: MAYBE
summary: "Looks good."
findings: []
```
"""
        # Pool returns malformed; per-reviewer retry via review_pool.run_agent returns APPROVE
        mock_pool.return_value = [
            _make_agent_result(success=True, output=malformed_approve, profile_name="review")
        ]
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_engine_agent.return_value = _make_agent_result()  # dev call
        mock_review_agent.return_value = _make_agent_result(output=APPROVE_REVIEW)

        result = run_task(config, task)

        assert result.success is True
        # Schema error triggers reviewer retry (not a dev cycle increment)
        assert result.state.review_cycle == 1
        # Parse retry was tracked in cycle metadata
        assert result.state.review_cycle_metadata[0].parse_retries == 1


class TestCoordinatorSchemaErrorOnRequestChanges:
    """Test that malformed REQUEST_CHANGES triggers reviewer retry (not a full dev cycle)."""

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_malformed_request_changes_triggers_retry(
        self, mock_shell, mock_engine_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # Malformed REQUEST_CHANGES: invalid severity (P0 is not a valid finding severity)
        malformed_review = """\
```yaml
verdict: REQUEST_CHANGES
summary: "Needs work"
findings:
  - severity: P0
    file: src/foo.py
    line: 10
    observed: "Bug"
    expected: "Behaviour conforms to project contract for this category of inputs."
    evidence: "(test fixture evidence)"
    suggestion: "Fix"
test_coverage:
  adequate: false
```
"""
        # Pool returns malformed; per-reviewer retry via review_pool.run_agent returns APPROVE
        mock_pool.return_value = [
            _make_agent_result(success=True, output=malformed_review, profile_name="review")
        ]
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_engine_agent.return_value = _make_agent_result(
            success=True, output="Done."
        )  # dev call
        mock_review_agent.return_value = _make_agent_result(output=APPROVE_REVIEW)

        result = run_task(config, task)

        # Schema error on REQUEST_CHANGES triggers retry — task completes on second attempt
        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        # Parse retry was tracked
        assert result.state.review_cycle_metadata[0].parse_retries == 1


class TestCoordinatorMultiModelReview:
    """Tests for pool of 2+ reviewers with synthesis."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_pool_of_2_approve(self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path):
        """Pool of 2 reviews → merge → APPROVE."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent: DEV (preflight handled by mock_preflight)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(
            success=True, output="Implemented.", profile_name="dev"
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_pool_of_2_request_changes(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Pool of 2 reviews → REQUEST_CHANGES (merged) → ESCALATE after max cycles."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent calls: preflight, DEV (cycle 1), DEV (cycle 2)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output="Fixed.", profile_name="dev"),
        ]
        # Both reviewers return valid REQUEST_CHANGES — no parse retries needed
        mock_pool.return_value = [
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="r1"),
            _make_agent_result(success=True, output=REQUEST_CHANGES_REVIEW, profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_pool_of_1_skips_synthesis(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Pool of 1 → uses output directly, no synthesis call."""
        # synthesis_profile is set but pool has only 1 entry
        single_profile = _make_review_profile("solo")
        config = ForgeConfig(
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
            review_pool=[single_profile],
            synthesis_profile=None,  # pool of 1 → no synthesis
            retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="solo"),
        ]

        result = run_task(config, task)

        assert result.success is True
        # run_agent called for DEV only (no synthesis for pool of 1; preflight mocked separately)
        assert mock_agent.call_count == 1

    @patch("theforge.coordinator.review_pool.time.sleep", lambda *_a, **_k: None)
    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_transient_reviewer_crash_retries_and_meets_quorum(
        self,
        mock_shell,
        mock_dev_agent,
        mock_preflight,
        mock_pool,
        mock_review_agent,
        tmp_path,
    ):
        """Phase-boundary seam: transient crash in a 4-reviewer panel is retried, then
        partial-failure quorum lets synthesis proceed without escalating."""
        profiles = [
            _make_review_profile("r1"),
            _make_review_profile("r2"),
            _make_review_profile("r3"),
            _make_review_profile("r4"),
        ]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        config = config.__class__(
            **{
                **config.__dict__,
                "retry": RetryPolicy(
                    review_quorum_threshold=2,
                    max_review_transport_retries=2,
                    review_transport_retry_backoff_seconds=0.0,
                ),
            }
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        # DEV + synthesis (pool of 4 → synthesis runs)
        mock_dev_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented.", profile_name="dev"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="synthesis"),
        ]
        # r1 crashes transiently (exit=1 + empty output); retries also fail.
        mock_pool.return_value = [
            _make_agent_result(success=False, output="", profile_name="r1"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r2"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r3"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r4"),
        ]
        mock_review_agent.return_value = _make_agent_result(
            success=False, output="", profile_name="r1"
        )

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Retries actually fired
        assert mock_review_agent.call_count == 2
        # Audit captures retry + quorum metadata
        meta = result.state.review_cycle_metadata[-1]
        assert meta.transient_retries["r1"] == 2
        assert meta.transient_outcomes["r1"] == "transient_retried_then_failed"
        assert meta.quorum_met is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_mixed_success_failure_degrades_to_single_no_synthesis(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """1 of 2 reviewers succeeds → single output used directly (no synthesis)."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        # Allow single-success quorum so the legacy "degrade to single" path
        # remains exercised under the new quorum contract.
        config = config.__class__(
            **{**config.__dict__, "retry": RetryPolicy(review_quorum_threshold=1)}
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent: only DEV — no synthesis since we degrade to 1 successful
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="r1"),
            _make_agent_result(success=False, output="TIMEOUT", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is True
        # DEV called run_agent; synthesis was skipped (preflight mocked separately)
        assert mock_agent.call_count == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_all_reviewers_fail_escalates(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """All reviewers fail → ESCALATE."""
        profiles = [_make_review_profile("r1"), _make_review_profile("r2")]
        config = _make_pool_config(tmp_path, profiles, SYNTHESIS_PROFILE)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = [
            _make_agent_result(success=False, output="TIMEOUT", profile_name="r1"),
            _make_agent_result(success=False, output="CRASH", profile_name="r2"),
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "failed" in result.message.lower()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_per_profile_budget_excludes_reviewer(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """One pool profile over budget → excluded, run continues with rest."""
        tight_profile = _make_review_profile("tight", budget_usd=0.10)
        normal_profile = _make_review_profile("normal", budget_usd=5.00)
        config = _make_pool_config(tmp_path, [tight_profile, normal_profile], SYNTHESIS_PROFILE)
        # Threshold=1 so the remaining reviewer meets quorum after budget exclusion.
        config = config.__class__(
            **{**config.__dict__, "retry": RetryPolicy(review_quorum_threshold=1)}
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # tight profile costs $0.50 which exceeds its $0.10 budget;
        # normal profile's review is synthesised into APPROVE.
        pool_approve = [
            _make_agent_result(success=True, output="R1", profile_name="tight", cost_usd=0.50),
            _make_agent_result(
                success=True, output=APPROVE_REVIEW, profile_name="normal", cost_usd=0.10
            ),
        ]
        synthesis_approve = _make_agent_result(
            success=True, output=APPROVE_REVIEW, profile_name="synthesis"
        )
        mock_pool.return_value = pool_approve
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.side_effect = [
            _make_agent_result(success=True, output="Implemented."),
            synthesis_approve,
        ]

        result = run_task(config, task)

        # Run should succeed — over-budget reviewer excluded, not escalated
        assert result.success is True
        assert result.phase == Phase.DONE


class TestReviewParseRetry:
    """Tests for reviewer retry on parse/schema errors (spec: review-parse-retry)."""

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_parse_error_does_not_increment_cycle(
        self, mock_shell, mock_engine_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        """Parse error on first review attempt → retry → APPROVE: review_cycle == 1."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # Pool returns parse error; per-reviewer retry via review_pool.run_agent returns APPROVE
        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="review")
        ]
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_engine_agent.return_value = _make_agent_result()  # dev call
        mock_review_agent.return_value = _make_agent_result(output=APPROVE_REVIEW)  # retry

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Parse error did NOT increment review_cycle — only the valid APPROVE did
        assert result.state.review_cycle == 1

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_parse_error_then_request_changes(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        """Parse error then real REQUEST_CHANGES → cycle increments once, DEV retried."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # Pool call 1: PARSE_ERROR_OUTPUT; per-reviewer retry returns REQUEST_CHANGES
        # Pool call 2: APPROVE (cycle 2)
        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] == 1:
                return [
                    _make_agent_result(
                        success=True,
                        output=PARSE_ERROR_OUTPUT,
                        profile_name="review",
                        cost_usd=0.1,
                    )
                ]
            # Cycle 2: APPROVE
            return [
                _make_agent_result(
                    success=True, output=APPROVE_REVIEW, profile_name="review", cost_usd=0.1
                )
            ]

        mock_pool.side_effect = pool_side_effect
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_review_agent.return_value = _make_agent_result(output=REQUEST_CHANGES_REVIEW)
        mock_agent.side_effect = [
            _make_agent_result(),
            # dev cycle 1
            _make_agent_result(),
            # dev cycle 2 (after REQUEST_CHANGES),
        ]

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # review_cycle == 2: cycle 1 (parse error + REQUEST_CHANGES), cycle 2 (APPROVE)
        assert result.state.review_cycle == 2

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_all_parse_retries_exhausted(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        """All per-reviewer parse retries exhausted → direct review-infrastructure ESCALATE."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        # run_agent returns: preflight, then dev results (output="Done." fails review parsing)
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_review_agent.return_value = _make_agent_result()

        # Pool always returns unparseable output; run_agent retries also return "Done." (fails)
        # All reviewers unparseable → direct ESCALATE with review-infrastructure reason
        mock_pool.return_value = [
            _make_agent_result(
                success=True, output=PARSE_ERROR_OUTPUT, profile_name="review", cost_usd=0.1
            )
        ]

        result = run_task(config, task)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Escalation reason is review-infrastructure failure, not exhausted cycles or no-changes
        assert "review infrastructure" in result.message.lower()
        assert "parseable" in result.message.lower()
        # Only one review cycle attempted — no DEV iteration for synthetic finding
        assert result.state.review_cycle == 0

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_parse_retry_count_in_audit(
        self, mock_shell, mock_engine_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        """Audit log records parse_retries: 1 when one per-reviewer retry occurred."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # Pool returns parse error; per-reviewer retry via review_pool.run_agent returns APPROVE
        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="review")
        ]
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_engine_agent.return_value = _make_agent_result()  # dev call
        mock_review_agent.return_value = _make_agent_result(output=APPROVE_REVIEW)  # retry

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert result.success is True
        assert len(audit["reviews"]) == 1
        assert audit["reviews"][0]["parse_retries"] == 1

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_operator_log_classifies_rejection_stage(
        self,
        mock_shell,
        mock_engine_agent,
        mock_preflight,
        mock_pool,
        mock_review_agent,
        tmp_path,
        capsys,
    ):
        """Operator-facing retry log must name the validation stage that rejected
        the output (YAML_SYNTAX / SCHEMA_VALIDATION / CONTRACT_CROSS_VALIDATION),
        not just "parse failed". The spec's RCA workflow depends on this
        distinction to pick the right remediation surface."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_engine_agent.return_value = _make_agent_result()

        # APPROVE with empty ac_verification — valid YAML, valid types, but a
        # contract cross-check failure. Pre-fix this would have been logged
        # identically to a YAML syntax error.
        cross_validation_output = (
            "```yaml\n"
            "verdict: APPROVE\n"
            'summary: "ok"\n'
            "findings: []\n"
            "story_compliance:\n"
            "  matches_spec: true\n"
            "  mismatches: []\n"
            "test_coverage:\n"
            "  adequate: true\n"
            "  gaps: []\n"
            "ac_verification: []\n"
            "```\n"
        )
        mock_pool.return_value = [
            _make_agent_result(success=True, output=cross_validation_output, profile_name="review")
        ]
        mock_review_agent.return_value = _make_agent_result(output=cross_validation_output)

        run_task(config, task)
        captured = capsys.readouterr()
        log_output = captured.out + captured.err

        assert "CONTRACT_CROSS_VALIDATION" in log_output, (
            "operator-facing retry log must surface the stage tag; got:\n" + log_output
        )
        assert "YAML_SYNTAX" not in log_output, (
            "schema/contract failure must not be conflated with YAML_SYNTAX"
        )

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_schema_error_also_retried(
        self, mock_shell, mock_engine_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        """Schema validation error (not just YAML parse error) also triggers per-reviewer retry."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # Pool returns schema error; per-reviewer retry via review_pool.run_agent returns APPROVE
        mock_pool.return_value = [
            _make_agent_result(
                success=True, output=_SCHEMA_ERROR_MAYBE_VERDICT, profile_name="review"
            )
        ]
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_engine_agent.return_value = _make_agent_result()  # dev call
        mock_review_agent.return_value = _make_agent_result(output=APPROVE_REVIEW)  # retry

        result = run_task(config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        # Schema error triggered retry — only 1 review cycle
        assert result.state.review_cycle == 1
        # parse_retries tracked in metadata
        assert result.state.review_cycle_metadata[0].parse_retries == 1


class TestReviewPoolResilience:
    """Integration tests for per-reviewer retry and graceful empty-pool handling."""

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_per_reviewer_retry_only_affects_failing_reviewer(
        self, mock_shell, mock_engine_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        """Parse failure retries that reviewer only; other reviewers are unaffected."""
        pool_config = _make_pool_config(
            tmp_path,
            profiles=[
                _make_review_profile("reviewer-a"),
                _make_review_profile("reviewer-b"),
            ],
            synthesis=None,
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")

        # Pool: reviewer-a returns bad YAML, reviewer-b returns valid APPROVE
        mock_pool.return_value = [
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="reviewer-a"),
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="reviewer-b"),
        ]
        # preflight_flow: preflight; phases: dev; review_pool.run_agent: reviewer-a retry
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_engine_agent.return_value = _make_agent_result()
        mock_review_agent.return_value = _make_agent_result(output=APPROVE_REVIEW)

        result = run_task(pool_config, task)

        assert result.success is True
        assert result.phase == Phase.DONE
        assert result.state.review_cycle == 1
        # reviewer-a retry counted; parse_retries == 1
        assert result.state.review_cycle_metadata[0].parse_retries == 1

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_max_review_parse_retries_zero_disables_retry(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """max_review_parse_retries=0 → no per-reviewer retry; direct infrastructure ESCALATE."""
        from dataclasses import replace as _dc_replace

        from theforge.config import RetryPolicy

        base_config = _make_config(tmp_path)
        config = _dc_replace(
            base_config, retry=RetryPolicy(max_dev_iterations=2, max_review_parse_retries=0)
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(
                success=True, output=PARSE_ERROR_OUTPUT, profile_name="review", cost_usd=0.1
            )
        ]

        result = run_task(config, task)

        # No retries → all reviewers unparseable → direct review-infrastructure ESCALATE
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        assert "review infrastructure" in result.message.lower()
        # parse_retries == 0 (none attempted)
        assert result.state.review_cycle_metadata[0].parse_retries == 0

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_empty_merge_falls_back_to_best_individual(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        """Merged result has parse errors but individual results exist → use best individual."""
        pool_config = _make_pool_config(
            tmp_path,
            profiles=[
                _make_review_profile("reviewer-a"),
                _make_review_profile("reviewer-b"),
            ],
            synthesis=None,
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_review_agent.return_value = _make_agent_result()

        # reviewer-a: valid APPROVE; reviewer-b: schema error (causes merge parse errors)
        # After reviewer-b's retries fail (agent returns "Done."), merge excludes reviewer-b
        # but reviewer-a's valid result is in individual_results → fallback returns APPROVE
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="reviewer-a"),
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="reviewer-b"),
        ]

        result = run_task(pool_config, task)

        # reviewer-a's APPROVE used as fallback → success
        assert result.success is True
        assert result.phase == Phase.DONE

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_all_reviewers_unparseable_escalates_as_infrastructure(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        """All reviewers unparseable → direct ESCALATE with review-infrastructure reason.

        Regression test for issue #1502: previously the sprint injected a synthetic
        P1, looped through DEV (which cannot fix reviewer output), and escalated
        with a misleading no-changes reason. The fix escalates directly at the
        review layer with a clear infrastructure reason.
        """
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_review_agent.return_value = _make_agent_result()
        # Pool always returns parse error; run_agent retries return "Done." (fails parse)
        mock_pool.return_value = [
            _make_agent_result(
                success=True, output=PARSE_ERROR_OUTPUT, profile_name="review", cost_usd=0.1
            )
        ]

        result = run_task(config, task)

        # All reviewers unparseable → direct ESCALATE at review layer
        assert result.success is False
        assert result.phase == Phase.ESCALATE
        # Reason names review-infrastructure failure, not DEV no-changes or cycles
        assert "review infrastructure" in result.message.lower()
        assert "parseable" in result.message.lower()
        # No review_cycle incremented (escalation happens before increment)
        assert result.state.review_cycle == 0
        # No synthetic finding was recorded as an actionable review result
        assert result.state.review_results == []

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_best_individual_p1_over_approve(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        """When merge fails, best-individual prefers REQUEST_CHANGES with P1 over APPROVE."""
        pool_config = _make_pool_config(
            tmp_path,
            profiles=[
                _make_review_profile("reviewer-a"),
                _make_review_profile("reviewer-b"),
            ],
            synthesis=None,
        )
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()
        mock_review_agent.return_value = _make_agent_result()

        # reviewer-a: APPROVE (valid); reviewer-b: parse error, retries fail
        # merge_review_results returns result with parse_errors (since reviewer-b fails)
        # But reviewer-a's APPROVE is in individual_results → best_individual returns APPROVE
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="reviewer-a"),
            _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="reviewer-b"),
        ]

        result = run_task(pool_config, task)

        # Best individual is reviewer-a's APPROVE → task succeeds
        assert result.success is True
        assert result.phase == Phase.DONE


_ESCAPE_VALVE_APPROVE = (
    "```yaml\n"
    "verdict: APPROVE\n"
    'summary: "Dependency bump, nothing to enumerate"\n'
    "findings: []\n"
    "story_compliance:\n"
    "  matches_spec: true\n"
    "  mismatches: []\n"
    "test_coverage:\n"
    "  adequate: true\n"
    "  gaps: []\n"
    "ac_verification: []\n"
    "criteria_enumerable: false\n"
    'criteria_enumerable_rationale: "Chore with no acceptance criteria."\n'
    "```\n"
)


class TestCriteriaEnumerableEscapeValveSeam:
    """Seam-level: the escape valve is review-phase output data that flows to
    gating and the audit trail. Verify APPROVE-with-empty-AC passes the review
    phase (no oscillation retry) and the flag reaches the audit."""

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_escape_valve_approve_passes_without_retry(
        self, mock_shell, mock_engine_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_engine_agent.return_value = _make_agent_result()
        mock_pool.return_value = [
            _make_agent_result(success=True, output=_ESCAPE_VALVE_APPROVE, profile_name="review")
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert result.success is True
        assert result.state.review_cycle == 1
        # No oscillation retry — the escape valve made APPROVE-with-empty-AC legal.
        assert result.state.review_cycle_metadata[0].parse_retries == 0
        # The reviewer's retry entrypoint was never called.
        mock_review_agent.assert_not_called()
        # The flag is surfaced in the audit trail.
        assert audit["reviews"][0]["criteria_enumerable"] is False

    @patch("theforge.coordinator.review_pool.run_agent")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch("theforge.coordinator.util._run_shell")
    def test_retry_prompt_anchors_prior_content(
        self, mock_shell, mock_engine_agent, mock_preflight, mock_pool, mock_review_agent, tmp_path
    ):
        """When a reviewer output needs a parse retry, the corrective prompt sent
        to run_agent must anchor the prior output and forbid dropping fields."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_engine_agent.return_value = _make_agent_result()

        broken = _make_agent_result(success=True, output=PARSE_ERROR_OUTPUT, profile_name="review")
        mock_pool.return_value = [broken]
        mock_review_agent.return_value = _make_agent_result(output=APPROVE_REVIEW)

        run_task(config, task)

        assert mock_review_agent.called
        retry_prompt = mock_review_agent.call_args.kwargs["prompt"]
        # Anchors prior content and forecloses the field-dropping interpretation.
        assert "CONTENT" in retry_prompt and "ENCODING" in retry_prompt
        assert "WRONG" in retry_prompt
        assert PARSE_ERROR_OUTPUT in retry_prompt
