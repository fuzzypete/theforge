"""Diagnose parse-retry and full-output recoverability seams (issue #2055).

A completed `forge diagnose` investigation is the expensive part of the run; a
YAML syntax slip in its serialization is the cheap part. These tests pin both
halves of the fix:

- a single recoverable syntax defect in the emitted document lands a diagnosis
  via a bounded, reformat-only retry instead of failing terminally, and
- for any run that reaches PARSE, the agent's COMPLETE output is retrievable
  from disk afterward — including when the retries are exhausted.

The seam under test is the PARSE transition of ``_run_diagnose_flow_body``: it
spans prompt construction (``task.diagnose_prompts``), strict parsing, the
retry-limit config, and the audit payload, so the coverage is flow-level rather
than unit-level.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import _make_config

from theforge.config.types import RetryPolicy
from theforge.diagnose_types import DiagnosePhase
from theforge.task.diagnose_prompts import (
    build_diagnose_reformat_prompt,
    parse_diagnose_output,
    parse_diagnose_output_result,
)

# Long enough that the audit's 2000-char ``raw_output_tail`` cannot possibly
# cover the confirmed cause — the exact condition that made #2029 unrecoverable.
_LONG_NOTES = ("The investigation walked the whole PARSE transition and the audit writer. ") * 40


def _payload(*, with_related: bool = True) -> dict:
    payload: dict = {
        "observed_symptom": "Sprint flow drops the third story silently",
        "reproduction_or_evidence": "Run forge sprint --issues 1,2,3 — story 3 never starts",
        "hypotheses": [
            {
                "statement": "DAG scheduler skips dependents when a blocker fails",
                "status": "ruled_out",
                "evidence": "Logs show no failed blockers in this run",
                "claim_verification": {
                    "verification_type": "source",
                    "detail": "Checked against the target repository source.",
                },
                "evidence_provenance": {
                    "source_type": "observed",
                    "detail": "Observed directly in the run log.",
                },
            },
            {
                "statement": "Worker pool size off-by-one",
                "status": "confirmed",
                "evidence": "scheduler.py:142 reserves N-1 slots when N requested",
                "claim_verification": {
                    "verification_type": "source",
                    "detail": "Checked against the target repository source.",
                },
                "evidence_provenance": {
                    "source_type": "prior_assertion",
                    "detail": "An earlier note already states the same mechanism.",
                },
            },
        ],
        "confirmed_cause": "Worker pool reserves N-1 slots in scheduler.py:142",
        "confirmed_cause_verification": {
            "verification_type": "source",
            "detail": "Checked against the target repository source.",
        },
        "confirmed_cause_support": "scheduler.py:142 reserves N-1 slots when N requested",
        "confirmed_cause_support_provenance": {
            "source_type": "observed",
            "detail": "Observed directly in the current HEAD source.",
        },
        "affected_code_path": "src/theforge/sprint/scheduler.py:142",
        "fix_success_criterion": "Running with --parallel 3 completes all 3 stories",
        "advisory_repair_proposal": (
            "Likely repair in the scheduler reservation helper; this is not verified."
        ),
        "notes": _LONG_NOTES,
        "inspected_files": ["src/theforge/sprint/scheduler.py"],
        "premise_anchors": [
            {"file": "src/theforge/sprint/scheduler.py", "pattern": "reserve_slots"}
        ],
    }
    if with_related:
        payload["related_findings"] = [{"summary": "an adjacent defect", "related": "#1649"}]
    return payload


def _valid_output() -> str:
    return f"```yaml\n{yaml.safe_dump(_payload(), sort_keys=False)}```"


def _output_with_unterminated_quote() -> str:
    """The #2029 byte shape: one unterminated double-quoted scalar, nothing else wrong.

    Every required key is present and well-formed; the single defect is an opening
    double quote in ``related_findings[0].summary`` that runs several lines without
    a closing quote before the next key. ``related_findings`` is the OPTIONAL
    field — the confirmed cause, hypotheses and evidence are all intact.
    """
    body = yaml.safe_dump(_payload(with_related=False), sort_keys=False)
    defect = (
        "related_findings:\n"
        "  - summary: \"agent_failure.py's substrate-failure classifier "
        "(_NO_OUTPUT_MARKERS,\n"
        "      _SUBSTRATE_MARKERS) duplicates the capability-profile reasoning\n"
        "      without any awareness of it — worth checking whether one shared\n"
        "      surface should own substrate-explains-the-symptom reasoning.\n"
        '    related: ""\n'
    )
    return f"```yaml\n{body}{defect}```"


def _fake_agent_result(
    output: str,
    *,
    success: bool = True,
    cost: float | None = 0.05,
    tool_trace: tuple[dict, ...] = (),
    failure_code: str | None = None,
):
    from theforge.agent_types import AgentResult

    return AgentResult(
        success=success,
        output=output,
        session_id=None,
        cost_usd=cost,
        exit_code=0 if success else 1,
        raw={},
        tool_trace=tool_trace,
        failure_code=failure_code,
    )


def _config(tmp_path: Path, *, parse_retries: int = 2):
    config = _make_config(tmp_path)
    return dataclasses.replace(
        config,
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            max_diagnose_parse_retries=parse_retries,
        ),
    )


def _issue(number: int = 2029) -> dict:
    return {
        "number": number,
        "title": "diagnose discards a completed investigation on a YAML slip",
        "body": "Run 59bdfe42256b investigated for 141s and then failed at PARSE.",
        "state": "OPEN",
    }


def _audit_for(tmp_path: Path, issue_number: int, run_id: str) -> dict:
    path = tmp_path / ".forge" / "audits" / f"diagnose-issue-{issue_number}-{run_id}.yaml"
    assert path.exists(), f"missing audit at {path}"
    return yaml.safe_load(path.read_text())


# ── Parser: failures now report why ───────────────────────────────────


class TestStrictParseReportsReason:
    def test_nested_fence_inside_yaml_scalar_preserves_following_fields(self):
        output = """```yaml
observed_symptom: |
  diagnose truncates after evidence
reproduction_or_evidence: |
  This evidence quotes source:
    ```
    def buggy():
        return True
    ```
hypotheses:
  - statement: Nested fence breaks extraction
    status: confirmed
    evidence: Extraction must continue past inner fences
    claim_verification:
      verification_type: source
      detail: Checked against the target repository source.
confirmed_cause: |
  The outer envelope closes later than the nested fence.
confirmed_cause_verification:
  verification_type: source
  detail: Checked against the target repository source.
affected_code_path: |
  src/theforge/task/diagnose_prompts.py
fix_success_criterion: |
  All supplied fields survive parsing.
```"""
        outcome = parse_diagnose_output_result(output, issue_number=2191)
        assert outcome.error == ""
        assert outcome.artifact is not None
        assert "def buggy()" in outcome.artifact.reproduction_or_evidence
        assert outcome.artifact.confirmed_cause.startswith("The outer envelope closes")
        assert outcome.artifact.is_complete()

    def test_top_level_fence_after_closed_envelope_is_ignored(self):
        output = """```yaml
observed_symptom: x
reproduction_or_evidence: y
hypotheses:
  - statement: z
    status: confirmed
    evidence: e
    claim_verification:
      verification_type: source
      detail: Checked against the target repository source.
confirmed_cause: c
confirmed_cause_verification:
  verification_type: source
  detail: Checked against the target repository source.
affected_code_path: p
fix_success_criterion: f
```
```python
print("trailing snippet")
```"""
        outcome = parse_diagnose_output_result(output, issue_number=2191)
        assert outcome.error == ""
        assert outcome.artifact is not None
        assert outcome.artifact.confirmed_cause == "c"

    def test_unclosed_yaml_envelope_is_rejected(self):
        output = """```yaml
observed_symptom: x
reproduction_or_evidence: y
hypotheses:
  - statement: z
    status: confirmed
    evidence: e
confirmed_cause: c
affected_code_path: p
fix_success_criterion: f
"""
        outcome = parse_diagnose_output_result(output, issue_number=2191)
        assert outcome.artifact is None
        assert "missing a matching close" in outcome.error

    def test_yaml_syntax_error_is_named(self):
        outcome = parse_diagnose_output_result(
            _output_with_unterminated_quote(), issue_number=2029
        )
        assert outcome.artifact is None
        assert "YAML syntax error" in outcome.error
        # The parser's own detail survives, so the retry prompt and the audit can
        # both quote what actually broke.
        assert "quoted scalar" in outcome.error

    def test_non_mapping_root_is_rejected_and_named(self):
        outcome = parse_diagnose_output_result("```yaml\n- just\n- a\n- list\n```", issue_number=1)
        assert outcome.artifact is None
        assert "mapping" in outcome.error
        assert "list" in outcome.error

    def test_well_formed_output_reports_no_error(self):
        outcome = parse_diagnose_output_result(_valid_output(), issue_number=7)
        assert outcome.artifact is not None
        assert outcome.artifact.is_complete()
        assert outcome.error == ""

    def test_none_returning_wrapper_behavior_is_unchanged(self):
        # Existing callers keep the artifact-or-None contract.
        assert parse_diagnose_output("not yaml at all: just: : :", issue_number=1) is None
        assert parse_diagnose_output(_valid_output(), issue_number=1) is not None


# ── Reformat prompt: repair only, never re-investigation ──────────────


class TestReformatPrompt:
    def test_carries_prior_output_and_parse_error(self):
        original = _output_with_unterminated_quote()
        error = parse_diagnose_output_result(original, issue_number=2029).error
        prompt = build_diagnose_reformat_prompt(original_output=original, parse_error=error)
        assert original in prompt
        assert error in prompt

    def test_forbids_renewed_investigation_and_schema_relaxation(self):
        prompt = build_diagnose_reformat_prompt(original_output="x", parse_error="boom")
        assert "Do NOT investigate anything further." in prompt
        # Dropping the optional field that failed to encode is named as wrong,
        # so "reformat" cannot be read as "emit something easier to encode".
        assert "related_findings" in prompt
        assert "WRONG" in prompt
        # The same output contract is restated, not a relaxed variant.
        for key in (
            "confirmed_cause",
            "confirmed_cause_support",
            "confirmed_cause_support_provenance",
            "advisory_repair_proposal",
            "evidence_provenance",
            "premise_anchors",
            "inspected_files",
            "symptom_scope_coverage",
        ):
            assert key in prompt
        assert "src/theforge/example.py" not in prompt
        assert "src/theforge/routing.py" not in prompt


# ── Flow seam: recovery ───────────────────────────────────────────────


class TestParseRetryRecovery:
    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_single_syntax_defect_lands_diagnosis_after_reformat_retry(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        """The fix-success criterion: one recoverable defect → a landed diagnosis."""
        mock_fetch.return_value = _issue()
        mock_agent.side_effect = [
            _fake_agent_result(_output_with_unterminated_quote(), cost=0.725),
            _fake_agent_result(_valid_output(), cost=0.04),
        ]
        mock_post.return_value = "https://example/comment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success, result.message
        assert result.state.phase == DiagnosePhase.DONE
        assert result.state.artifact is not None
        assert result.state.artifact.confirmed_cause.startswith("Worker pool reserves N-1")
        assert mock_agent.call_count == 2
        # The retry re-serialized; it did not re-investigate.
        retry_prompt = mock_agent.call_args_list[1].kwargs["prompt"]
        assert "Do NOT investigate anything further." in retry_prompt

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_retry_attempt_is_audit_visible_with_cost_and_duration(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        mock_fetch.return_value = _issue()
        mock_agent.side_effect = [
            _fake_agent_result(_output_with_unterminated_quote(), cost=0.725),
            _fake_agent_result(_valid_output(), cost=0.04),
        ]
        mock_post.return_value = "https://example/comment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path),
            project_root=tmp_path,
            output_destination="comment",
        )

        audit = _audit_for(tmp_path, 2029, result.state.run_id)
        retries = audit["agent"]["parse_retries"]
        assert len(retries) == 1
        entry = retries[0]
        assert entry["attempt"] == 1
        assert entry["success"] is True
        assert "YAML syntax error" in entry["parse_error"]
        assert entry["cost_usd"] == 0.04
        assert entry["duration_s"] >= 0.0
        # The retry's spend is folded into the run total, not silently dropped.
        assert audit["agent"]["cost_usd"] == 0.765

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_failed_repair_does_not_replace_the_original_investigation(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        """A repair that still does not parse must not overwrite the salvageable output."""
        mock_fetch.return_value = _issue()
        original = _output_with_unterminated_quote()
        mock_agent.side_effect = [
            _fake_agent_result(original, cost=0.725),
            _fake_agent_result("I could not reformat that. Sorry!", cost=0.01),
            _fake_agent_result(_valid_output(), cost=0.03),
        ]
        mock_post.return_value = "https://example/comment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success, result.message
        assert mock_agent.call_count == 3
        # The second retry prompt still anchors on the ORIGINAL emission, not on
        # the failed repair — otherwise the investigation content would be lost.
        second_retry_prompt = mock_agent.call_args_list[2].kwargs["prompt"]
        assert original in second_retry_prompt
        assert "I could not reformat that" not in second_retry_prompt
        # And the original emission is still on disk verbatim.
        audits = tmp_path / ".forge" / "audits"
        initial = audits / f"diagnose-issue-2029-{result.state.run_id}.raw.txt"
        assert initial.read_text() == original

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_zero_retry_budget_disables_the_retry_path(self, mock_agent, mock_fetch, tmp_path):
        mock_fetch.return_value = _issue()
        mock_agent.return_value = _fake_agent_result(_output_with_unterminated_quote())

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path, parse_retries=0),
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert mock_agent.call_count == 1
        assert result.state.parse_retries == []

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_empty_output_is_not_retried(self, mock_agent, mock_fetch, tmp_path):
        """A killed/timed-out agent emitted nothing — there is no serialization to repair."""
        mock_fetch.return_value = _issue()
        mock_agent.return_value = _fake_agent_result("", success=False, cost=0.4)
        mock_agent.return_value = dataclasses.replace(
            mock_agent.return_value, failure_code="timeout"
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path, parse_retries=2),
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success
        assert mock_agent.call_count == 1, "spent budget re-serializing an empty investigation"
        assert result.state.parse_retries == []
        # No output means no sidecar to claim.
        assert result.state.raw_output_path == ""

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_already_over_budget_run_is_not_retried(self, mock_agent, mock_fetch, tmp_path):
        """More spend is exactly what a breached envelope forbids."""
        mock_fetch.return_value = _issue()
        config = _config(tmp_path, parse_retries=2)
        over = config.diagnose.budget_usd * 5
        mock_agent.return_value = _fake_agent_result(_output_with_unterminated_quote(), cost=over)

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2029,
            config=config,
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success
        assert mock_agent.call_count == 1
        assert result.state.parse_retries == []
        # The investigation is still recoverable from disk without a retry.
        assert Path(result.state.raw_output_path).exists()

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_delegated_waiting_placeholder_is_not_retried_or_reported_as_parse_failure(
        self, mock_agent, mock_fetch, tmp_path
    ):
        """A delegated top-level status line is not a diagnosis waiting to be reformatted."""
        mock_fetch.return_value = _issue(2798)
        placeholder = (
            "Still waiting on the investigation agent to finish examining the "
            "history-lookup and plan-dedup code paths for issue #419."
        )
        mock_agent.return_value = _fake_agent_result(
            placeholder,
            cost=0.45,
            tool_trace=(
                {"tool": "Agent", "target": "Investigate hamstring curl history/dedup bug"},
                {
                    "tool": "ScheduleWakeup",
                    "target": (
                        "Fallback in case the Explore agent notification doesn't land promptly"
                    ),
                },
            ),
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2798,
            config=_config(tmp_path, parse_retries=2),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert mock_agent.call_count == 1
        assert result.state.parse_retries == []
        assert result.state.agent_failure_code == "delegated_without_observed_outcome"
        assert "used Agent, ScheduleWakeup" in result.message
        assert "delegation placeholder" in result.message
        assert "Skipping YAML reformat retries" in result.message
        assert "parseable YAML block" not in result.message
        assert Path(result.state.raw_output_path).read_text() == placeholder

        audit = _audit_for(tmp_path, 2798, result.state.run_id)
        assert audit["agent"]["failure_code"] == "delegated_without_observed_outcome"
        assert audit["agent"]["parse_retries"] == []

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_delegated_results_to_follow_placeholder_is_not_retried(
        self, mock_agent, mock_fetch, tmp_path
    ):
        """Delegation-tool evidence plus a deferred-results status is terminal."""
        mock_fetch.return_value = _issue(2798)
        placeholder = "Dispatched a subagent to trace the history-lookup path; results to follow."
        mock_agent.return_value = _fake_agent_result(
            placeholder,
            cost=0.31,
            tool_trace=({"tool": "Agent", "target": "Trace the history-lookup path"},),
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2798,
            config=_config(tmp_path, parse_retries=2),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert mock_agent.call_count == 1
        assert result.state.parse_retries == []
        assert result.state.agent_failure_code == "delegated_without_observed_outcome"
        assert "used Agent" in result.message
        assert "Skipping YAML reformat retries" in result.message
        assert "parseable YAML block" not in result.message

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_tool_trace_delegation_does_not_require_waiting_phrase_match(
        self, mock_agent, mock_fetch, tmp_path
    ):
        """Tool-trace-confirmed delegation is sufficient even when wording drifts."""
        mock_fetch.return_value = _issue(2798)
        placeholder = (
            "I'll wait for the background agent's completion notification rather than polling."
        )
        mock_agent.return_value = _fake_agent_result(
            placeholder,
            cost=0.32,
            tool_trace=({"tool": "Agent", "target": "Investigate the background task"},),
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2798,
            config=_config(tmp_path, parse_retries=2),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert mock_agent.call_count == 1
        assert result.state.parse_retries == []
        assert result.state.agent_failure_code == "delegated_without_observed_outcome"
        assert "used Agent" in result.message
        assert "Skipping YAML reformat retries" in result.message
        assert "parseable YAML block" not in result.message

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_text_only_delegation_placeholder_uses_text_specific_message(
        self, mock_agent, mock_fetch, tmp_path
    ):
        """The text-only fallback requires explicit delegated-agent wording."""
        mock_fetch.return_value = _issue(2798)
        placeholder = (
            "Delegated the trace to a subagent. Still waiting for the investigation "
            "agent to finish; I'll report back when it returns."
        )
        mock_agent.return_value = _fake_agent_result(placeholder, cost=0.18)

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2798,
            config=_config(tmp_path, parse_retries=2),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert mock_agent.call_count == 1
        assert result.state.parse_retries == []
        assert result.state.agent_failure_code == "delegated_without_observed_outcome"
        assert "described delegated work in its own output" in result.message
        assert "used Agent" not in result.message

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_failed_result_with_text_only_waiting_placeholder_is_not_retried(
        self, mock_agent, mock_fetch, tmp_path
    ):
        """A nonzero runner exit does not hide a real delegated placeholder result."""
        mock_fetch.return_value = _issue(431)
        placeholder = (
            "I'll wait for the actual completion notification from the "
            "investigation agent rather than polling further."
        )
        mock_agent.return_value = _fake_agent_result(placeholder, success=False, cost=0.49)

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=431,
            config=_config(tmp_path, parse_retries=2),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert mock_agent.call_count == 1
        assert result.state.parse_retries == []
        assert result.state.agent_failure_code == "delegated_without_observed_outcome"
        assert "described delegated work in its own output" in result.message
        assert "Skipping YAML reformat retries" in result.message
        assert "parseable YAML block" not in result.message

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_delegated_placeholder_preserves_terminal_runner_failure_code(
        self, mock_agent, mock_fetch, tmp_path
    ):
        """Delegation evidence must not erase a runner-reported terminal code."""
        mock_fetch.return_value = _issue(431)
        placeholder = (
            "Still waiting on the investigation agent to finish examining the "
            "history-lookup path before I can report back."
        )
        mock_agent.return_value = _fake_agent_result(
            placeholder,
            success=False,
            cost=0.49,
            failure_code="timeout",
            tool_trace=({"tool": "Agent", "target": "Investigate the history-lookup path"},),
        )

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=431,
            config=_config(tmp_path, parse_retries=2),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert mock_agent.call_count == 1
        assert result.state.parse_retries == []
        assert result.state.agent_failure_code == "timeout"
        assert "used Agent" in result.message
        assert "delegation placeholder" in result.message
        assert "Skipping YAML reformat retries" in result.message

        audit = _audit_for(tmp_path, 431, result.state.run_id)
        assert audit["agent"]["failure_code"] == "timeout"


# ── Flow seam: recoverability when retries are exhausted ──────────────


class TestExhaustedRetriesStayRecoverable:
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_exhausted_retries_fail_with_full_output_on_disk(
        self, mock_agent, mock_fetch, tmp_path
    ):
        mock_fetch.return_value = _issue()
        original = _output_with_unterminated_quote()
        mock_agent.side_effect = [
            _fake_agent_result(original, cost=0.725),
            _fake_agent_result('still broken: "unterminated', cost=0.02),
            _fake_agent_result("- not a mapping either", cost=0.02),
        ]

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path, parse_retries=2),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        assert mock_agent.call_count == 3
        # The terminal message points the operator at the recoverable output
        # rather than at a truncated tail alone.
        assert "reformat retry attempt(s)" in result.message
        assert result.state.raw_output_path in result.message

        raw = Path(result.state.raw_output_path)
        assert raw.exists()
        assert raw.read_text() == original
        # Full output — not the last 2000 characters. The confirmed cause the
        # tail could not reach is retrievable.
        assert len(original) > 2000
        assert "Worker pool reserves N-1 slots" in raw.read_text()

        audit = _audit_for(tmp_path, 2029, result.state.run_id)
        agent = audit["agent"]
        assert agent["raw_output_path"] == str(raw)
        assert agent["raw_output_chars"] == len(original)
        assert agent["raw_output_sha256"]
        assert agent["raw_output_error"] == ""
        # Both retry attempts, with their own parse errors, are audit-visible.
        retries = agent["parse_retries"]
        assert [e["attempt"] for e in retries] == [1, 2]
        assert all(e["success"] is False for e in retries)
        assert any("mapping" in e["parse_error_after"] for e in retries)
        # Every emission this run paid for is enumerable and on disk.
        assert len(agent["raw_output_paths"]) == 3
        for recorded in agent["raw_output_paths"]:
            assert Path(recorded).exists()

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_successful_run_also_persists_full_output(
        self, mock_agent, mock_fetch, mock_post, tmp_path
    ):
        """Recoverability is a property of reaching PARSE, not of failing there."""
        mock_fetch.return_value = _issue()
        output = _valid_output()
        mock_agent.return_value = _fake_agent_result(output)
        mock_post.return_value = "https://example/comment-1"

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success, result.message
        raw = Path(result.state.raw_output_path)
        assert raw.read_text() == output
        audit = _audit_for(tmp_path, 2029, result.state.run_id)
        assert audit["agent"]["raw_output_chars"] == len(output)
        assert audit["agent"]["parse_retries"] == []
        # Nothing needed the inline carrier.
        assert audit["agent"]["raw_output"] == ""


# ── Persistence is guaranteed, not best-effort ────────────────────────
#
# An unwritable sidecar must not silently downgrade the recoverability
# guarantee to the bounded 2000-char tail. Persistence escalates through the
# audit sidecar, then the run-log fallback, then the audit record itself.


def _unwritable(tmp_path: Path, name: str):
    """Return a path factory whose parent is a regular file, so mkdir/write fail."""
    blocker = tmp_path / name
    blocker.write_text("not a directory", encoding="utf-8")

    def _factory(*_args, **kwargs):
        attempt = kwargs.get("attempt", 0)
        return blocker / f"attempt-{attempt}.raw.txt"

    return _factory


class TestPersistenceIsGuaranteed:
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_unwritable_sidecar_falls_back_to_the_run_log_location(
        self, mock_agent, mock_fetch, tmp_path, monkeypatch
    ):
        import theforge.coordinator.diagnose_flow as flow

        monkeypatch.setattr(
            flow, "diagnose_raw_output_path", _unwritable(tmp_path, "blocked-audits")
        )
        mock_fetch.return_value = _issue()
        original = _output_with_unterminated_quote()
        mock_agent.return_value = _fake_agent_result(original, cost=0.7)

        result = flow.run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path, parse_retries=0),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        # Tier 2 accepted the write, so the guarantee holds without the sidecar.
        fallback = Path(result.state.raw_output_path)
        assert fallback.exists()
        assert fallback.read_text() == original
        assert ".forge/logs/" in str(fallback).replace("\\", "/")
        audit = _audit_for(tmp_path, 2029, result.state.run_id)
        assert audit["agent"]["raw_output_path"] == str(fallback)
        assert audit["agent"]["raw_output_chars"] == len(original)
        assert audit["agent"]["raw_output_error"] == ""
        # The operator is pointed at the real location, not at a missing file.
        assert str(fallback) in result.message

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_no_writable_location_carries_full_output_inline_in_the_audit(
        self, mock_agent, mock_fetch, tmp_path, monkeypatch
    ):
        """The P1 case: every file location refuses, and the output is STILL recoverable."""
        import theforge.coordinator.diagnose_flow as flow

        monkeypatch.setattr(
            flow, "diagnose_raw_output_path", _unwritable(tmp_path, "blocked-audits")
        )
        monkeypatch.setattr(
            flow, "diagnose_fallback_output_path", _unwritable(tmp_path, "blocked-logs")
        )
        mock_fetch.return_value = _issue()
        original = _output_with_unterminated_quote()
        mock_agent.return_value = _fake_agent_result(original, cost=0.7)

        result = flow.run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path, parse_retries=0),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        assert result.state.raw_output_path == ""
        audit = _audit_for(tmp_path, 2029, result.state.run_id)
        agent = audit["agent"]
        # The COMPLETE output — not the bounded tail — survives in the audit the
        # operator reads the failure from.
        assert agent["raw_output"] == original
        assert len(original) > 2000
        assert "Worker pool reserves N-1 slots" in agent["raw_output"]
        assert agent["raw_output_chars"] == len(original)
        assert agent["raw_output_sha256"]
        # Both refused locations are named, so the audit never claims a file that
        # is not there.
        assert "blocked-audits" in agent["raw_output_error"]
        assert "blocked-logs" in agent["raw_output_error"]
        assert agent["raw_output_paths"] == []
        # And the terminal message routes the operator to the surviving copy.
        assert "agent.raw_output" in result.message

    @patch("theforge.coordinator.diagnose_flow._gh_post_comment")
    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_retry_still_recovers_the_diagnosis_when_no_location_is_writable(
        self, mock_agent, mock_fetch, mock_post, tmp_path, monkeypatch
    ):
        """A persistence failure must not block the retry path from landing a diagnosis."""
        import theforge.coordinator.diagnose_flow as flow

        monkeypatch.setattr(
            flow, "diagnose_raw_output_path", _unwritable(tmp_path, "blocked-audits")
        )
        monkeypatch.setattr(
            flow, "diagnose_fallback_output_path", _unwritable(tmp_path, "blocked-logs")
        )
        mock_fetch.return_value = _issue()
        mock_agent.side_effect = [
            _fake_agent_result(_output_with_unterminated_quote(), cost=0.7),
            _fake_agent_result(_valid_output(), cost=0.04),
        ]
        mock_post.return_value = "https://example/comment-1"

        result = flow.run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert result.success, result.message
        assert result.state.phase == DiagnosePhase.DONE
        audit = _audit_for(tmp_path, 2029, result.state.run_id)
        # The repaired output is the run's output, carried inline since no file
        # location would take it.
        assert audit["agent"]["raw_output"] == _valid_output()
        entry = audit["agent"]["parse_retries"][0]
        assert entry["success"] is True
        # The original emission is not destroyed by the repair that replaced it.
        assert entry["previous_output"] == _output_with_unterminated_quote()

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_failed_repair_output_is_carried_in_its_attempt_record(
        self, mock_agent, mock_fetch, tmp_path, monkeypatch
    ):
        import theforge.coordinator.diagnose_flow as flow

        monkeypatch.setattr(
            flow, "diagnose_raw_output_path", _unwritable(tmp_path, "blocked-audits")
        )
        monkeypatch.setattr(
            flow, "diagnose_fallback_output_path", _unwritable(tmp_path, "blocked-logs")
        )
        mock_fetch.return_value = _issue()
        broken_repair = 'still broken: "unterminated' + ("x" * 3000)
        mock_agent.side_effect = [
            _fake_agent_result(_output_with_unterminated_quote(), cost=0.7),
            _fake_agent_result(broken_repair, cost=0.02),
        ]

        result = flow.run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path, parse_retries=1),
            project_root=tmp_path,
            output_destination="comment",
        )

        assert not result.success
        entry = _audit_for(tmp_path, 2029, result.state.run_id)["agent"]["parse_retries"][0]
        # A retry that cost money and produced an unparseable repair is itself
        # evidence; it survives in full, not as a 2000-char tail.
        assert entry["output"] == broken_repair
        assert len(broken_repair) > 2000
        assert entry["output_path"] == ""
        assert "blocked-logs" in entry["output_path_error"]

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_unmeasured_retry_cost_marks_the_run_total_unknown(
        self, mock_agent, mock_fetch, tmp_path
    ):
        """A retry whose spend was not measured must not be recorded as free."""
        mock_fetch.return_value = _issue()
        mock_agent.side_effect = [
            _fake_agent_result(_output_with_unterminated_quote(), cost=0.725),
            _fake_agent_result("nope", success=False, cost=None),
        ]

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path, parse_retries=1),
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success
        assert result.state.agent_cost_usd is None
        audit = _audit_for(tmp_path, 2029, result.state.run_id)
        assert audit["agent"]["cost_usd"] is None
        assert audit["agent"]["parse_retries"][0]["cost_usd"] is None

    @patch("theforge.coordinator.diagnose_flow._gh_fetch_issue")
    @patch("theforge.coordinator.diagnose_flow.run_agent")
    def test_crashing_retry_is_recorded_and_stops_the_loop(self, mock_agent, mock_fetch, tmp_path):
        mock_fetch.return_value = _issue()
        mock_agent.side_effect = [
            _fake_agent_result(_output_with_unterminated_quote(), cost=0.725),
            RuntimeError("transport exploded"),
        ]

        from theforge.coordinator.diagnose_flow import run_diagnose_flow

        result = run_diagnose_flow(
            issue_number=2029,
            config=_config(tmp_path, parse_retries=2),
            project_root=tmp_path,
            output_destination="comment",
        )
        assert not result.success
        assert result.state.phase == DiagnosePhase.FAILED
        # Crash breaks the loop rather than burning the remaining budget.
        assert mock_agent.call_count == 2
        audit = _audit_for(tmp_path, 2029, result.state.run_id)
        retries = audit["agent"]["parse_retries"]
        assert len(retries) == 1
        assert "transport exploded" in retries[0]["error"]
        # The investigation is still recoverable despite the crash.
        assert Path(result.state.raw_output_path).exists()
