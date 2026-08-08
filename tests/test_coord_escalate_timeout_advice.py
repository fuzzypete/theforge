"""Seam-level tests for applying advisory advice at an EXPIRED escalate gate.

Covers the coordinator boundary issue #2279 touches:

* ``retry.escalate_timeout_policy`` is opt-in, validated at config load, and
  defaults to today's preserve-on-expiry behaviour.
* an opted-in expiry applies a usable, performable recommendation and reaches the
  same outcome an operator selecting that action deliberately would.
* ``elevate`` and every flavour of absent advice still preserve the story, and
  the run states WHICH of those situations occurred.
* a selection that arrives before expiry governs, whatever the advisor said.
* decision provenance survives into the audit record and the resume record.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from coord_test_helpers import _make_config, _make_task

from theforge.config import (
    ESCALATE_TIMEOUT_APPLY_ADVICE,
    ESCALATE_TIMEOUT_PRESERVE,
    BackendConfig,
    NotificationConfig,
    RetryPolicy,
)
from theforge.config.load import load_config
from theforge.coordinator import review_phase as rp
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.resume_persistence import (
    _apply_escalation,
    _escalation_block,
)
from theforge.coordinator.review_phase import _run_escalate_gate
from theforge.coordinator.state import (
    ADVICE_APPLIED,
    ADVICE_ELEVATE,
    ADVICE_LAUNCH_FAILURE,
    ADVICE_NO_RECOMMENDATION,
    ADVICE_NOT_PERFORMABLE,
    ADVICE_POLICY_PRESERVE,
    ADVICE_UNAVAILABLE,
    ADVICE_UNPARSEABLE,
    ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT,
    ESCALATE_SOURCE_OPERATOR,
    ESCALATE_SOURCE_POLICY_AUTO_APPROVE,
    ESCALATE_SOURCE_POLICY_REJECT,
    ESCALATE_SOURCE_TIMEOUT_PENDING,
    CoordinatorResult,
    CoordinatorState,
    Phase,
)
from theforge.escalation_advisor import AdvisoryOption, AdvisoryReport
from theforge.review import ReviewFinding, ReviewResult

# ── fixtures ──────────────────────────────────────────────────────────────────


def _review(verdict: str = "REQUEST_CHANGES") -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary="one unresolved P2",
        findings=[
            ReviewFinding(severity="P2", file="f.py", line=1, observed="edge", suggestion=None)
        ],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _report(recommendation: str = "land_core_defer_edges") -> AdvisoryReport:
    """A valid report recommending ``recommendation``."""
    return AdvisoryReport(
        recommendation=recommendation,
        rationale="gate passes; the remaining P2 is an edge",
        options=[
            AdvisoryOption(
                action=recommendation or "redirect",
                evidence="gate PASS with one unresolved P2",
                forge_operation="land-core",
                risk="the edge ships as a follow-up",
                consequence="core lands, edge is filed",
            ),
        ],
        parse_errors=[],
        raw={},
    )


def _unparseable_report() -> AdvisoryReport:
    return AdvisoryReport(
        recommendation="",
        rationale="",
        options=[],
        parse_errors=["no <advisory_report> block found"],
        raw={},
    )


def _empty_recommendation_report() -> AdvisoryReport:
    """A report that PARSES but names no action — advice absent, not invalid."""
    return AdvisoryReport(
        recommendation="",
        rationale="the evidence supports no single action",
        options=[],
        parse_errors=[],
        raw={},
    )


def _config(tmp_path: Path, *, timeout_policy: str = ESCALATE_TIMEOUT_PRESERVE):
    """Pending-file HITL config with the escalate-timeout policy under test."""
    base = _make_config(tmp_path)
    notifications = NotificationConfig(
        backend="ntfy",
        human_review_timeout_seconds=1,
        backends=(BackendConfig(type="ntfy", url="https://ntfy.sh/t"),),
    )
    retry = dataclasses.replace(
        base.retry,
        escalate_policy="prompt",
        escalate_timeout_policy=timeout_policy,
    )
    return dataclasses.replace(base, notifications=notifications, retry=retry)


def _escalated_state(*, approvable: bool = True) -> CoordinatorState:
    state = CoordinatorState()
    state.phase = Phase.ESCALATE
    state.review_cycle = 5
    state.story_content = "Body.\n\n## Acceptance criteria\n\n- do the thing\n"
    rr = _review()
    if approvable:
        state.review_results = [rr]
    state.last_cycle_reviewer_results = [("reviewer-a", rr)]
    state.error = "Review requested changes after 5 cycles. Max cycles (5) exhausted."
    return state


def _drive_gate(
    tmp_path,
    monkeypatch,
    *,
    gate_decision: str,
    report: AdvisoryReport | None,
    timeout_policy: str = ESCALATE_TIMEOUT_PRESERVE,
    approvable: bool = True,
    launch_failure: bool = False,
    run_id: str = "run-t",
    config=None,
):
    """Run the escalate gate with a stubbed advisor + gate surface."""
    config = config or _config(tmp_path, timeout_policy=timeout_policy)
    task = _make_task(tmp_path)
    state = _escalated_state(approvable=approvable)

    def _fake_advisor(*a, **k):
        if launch_failure:
            state.advisory_generated = False
            state.advisory_launch_failure = True
            state.advisory_launch_reason = "Not inside a trusted directory"
            return None
        if report is None:
            return None
        state.advisory_generated = report.ok
        state.advisory_report = report.to_dict()
        return report

    monkeypatch.setattr(rp, "run_escalation_advisor", _fake_advisor)
    monkeypatch.setattr(rp, "_pending_escalate_gate", lambda *a, **k: gate_decision)
    monkeypatch.setattr(
        rp,
        "_finalize_approve",
        lambda *a, **k: CoordinatorResult(
            success=True, phase=Phase.DONE, state=state, message="approved"
        ),
    )
    monkeypatch.setattr(rp, "_append_cycle_history", lambda *a, **k: None)
    monkeypatch.setattr(rp, "_escalate_notify", lambda *a, **k: None)

    result = _run_escalate_gate(
        state,
        config,
        task,
        tmp_path / "ws",
        "forge/test",
        0.0,
        auto_merge=False,
        notify=True,
        logger=None,
        run_id=run_id,
    )
    return state, result


# ── the opt-in itself ─────────────────────────────────────────────────────────


class TestEscalateTimeoutPolicyConfig:
    def test_default_is_preserve(self):
        assert RetryPolicy().escalate_timeout_policy == ESCALATE_TIMEOUT_PRESERVE

    def _write(self, tmp_path: Path, retry_yaml: str) -> Path:
        path = tmp_path / "forge.yaml"
        path.write_text(
            "project: t\n"
            "workspace:\n"
            "  create_command: 'mkdir -p {slug}'\n"
            "  path_pattern: '{slug}'\n"
            "  branch_pattern: 'forge/{slug}'\n"
            "validation:\n"
            "  gate_command: 'make gate'\n"
            f"{retry_yaml}",
            encoding="utf-8",
        )
        return path

    def test_absent_field_loads_as_preserve(self, tmp_path):
        config = load_config(self._write(tmp_path, "retry:\n  max_review_cycles: 2\n"))
        assert config.retry.escalate_timeout_policy == ESCALATE_TIMEOUT_PRESERVE

    def test_apply_advice_loads(self, tmp_path):
        config = load_config(
            self._write(tmp_path, "retry:\n  escalate_timeout_policy: apply_advice\n")
        )
        assert config.retry.escalate_timeout_policy == ESCALATE_TIMEOUT_APPLY_ADVICE

    def test_unknown_value_is_refused(self, tmp_path):
        # Config loading is an integrity boundary: a typo here would silently
        # decide what an unattended overnight expiry does.
        path = self._write(tmp_path, "retry:\n  escalate_timeout_policy: apply_advise\n")
        with pytest.raises(ValueError, match="escalate_timeout_policy"):
            load_config(path)

    def test_escalate_policy_tolerance_is_unchanged(self, tmp_path):
        # The last AC: existing policies keep their current meanings. The new
        # validation is deliberately scoped to the new field only.
        config = load_config(self._write(tmp_path, "retry:\n  escalate_policy: whatever\n"))
        assert config.retry.escalate_policy == "whatever"


# ── default (not opted in) behaviour is untouched ─────────────────────────────


class TestDefaultPolicyUnchanged:
    def test_timeout_still_preserves_when_not_opted_in(self, tmp_path, monkeypatch):
        state, result = _drive_gate(
            tmp_path, monkeypatch, gate_decision="timeout", report=_report("land_core_defer_edges")
        )
        assert result.success is False
        assert state.escalate_decision == "advisory_pending"
        assert state.escalate_selected_action is None
        assert state.escalate_decision_source == ESCALATE_SOURCE_TIMEOUT_PENDING
        assert state.escalate_timeout_advice == ADVICE_POLICY_PRESERVE
        # The un-opted-in message is exactly what it was before this change.
        assert "advisory report was generated" in result.message

    def test_reject_policy_records_policy_provenance(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        config = dataclasses.replace(
            config, retry=dataclasses.replace(config.retry, escalate_policy="reject")
        )
        state, result = _drive_gate(
            tmp_path, monkeypatch, gate_decision="timeout", report=None, config=config
        )
        assert state.escalate_decision == "reject"
        assert state.escalate_decision_source == ESCALATE_SOURCE_POLICY_REJECT

    def test_auto_approve_policy_records_policy_provenance(self, tmp_path, monkeypatch):
        config = _config(tmp_path)
        config = dataclasses.replace(
            config, retry=dataclasses.replace(config.retry, escalate_policy="auto_approve")
        )
        state = _escalated_state()
        state.review_results = [_review("APPROVE")]
        state.last_cycle_reviewer_results = [("reviewer-a", _review("APPROVE"))]
        state.gate_decisions = ["PASS"]
        task = _make_task(tmp_path)
        monkeypatch.setattr(
            rp,
            "_finalize_approve",
            lambda *a, **k: CoordinatorResult(
                success=True, phase=Phase.DONE, state=state, message="approved"
            ),
        )
        monkeypatch.setattr(rp, "_append_cycle_history", lambda *a, **k: None)
        monkeypatch.setattr(rp, "_escalate_notify", lambda *a, **k: None)
        _run_escalate_gate(
            state,
            config,
            task,
            tmp_path / "ws",
            "forge/test",
            0.0,
            auto_merge=False,
            notify=True,
            logger=None,
            run_id="run-aa",
        )
        assert state.escalate_decision == "approve"
        assert state.escalate_decision_source == ESCALATE_SOURCE_POLICY_AUTO_APPROVE


# ── opted in: the recommendation is applied ───────────────────────────────────


class TestAppliesAdviceOnExpiry:
    def test_named_recommendation_is_applied_like_a_deliberate_selection(
        self, tmp_path, monkeypatch
    ):
        state, result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=_report("land_core_defer_edges"),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        # Identical to what an operator selecting land_core_defer_edges gets: the
        # named disposition, the named forge operation, the worktree preserved.
        assert state.escalate_decision == "land_core_defer_edges"
        assert state.escalate_selected_action == "land_core_defer_edges"
        assert "land-core" in result.message
        assert state.escalate_decision_source == ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT
        assert state.escalate_timeout_advice == ADVICE_APPLIED

    def test_accept_recommendation_finalizes_the_approval(self, tmp_path, monkeypatch):
        state, result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=_report("accept"),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        assert result.success is True
        assert result.phase == Phase.DONE
        assert state.escalate_decision == "accept"
        assert state.escalate_decision_source == ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT

    def test_defer_or_abandon_recommendation_rejects(self, tmp_path, monkeypatch):
        state, result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=_report("defer_or_abandon"),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        assert result.success is False
        assert state.escalate_decision == "reject"
        assert state.escalate_selected_action == "defer_or_abandon"
        assert state.escalate_decision_source == ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT


# ── opted in: every way advice can be missing still preserves ─────────────────


class TestPreservesWhenAdviceCannotBeApplied:
    def _preserved(self, state, result) -> None:
        assert result.success is False
        assert state.escalate_decision == "advisory_pending"
        assert state.escalate_selected_action is None
        assert state.escalate_decision_source == ESCALATE_SOURCE_TIMEOUT_PENDING

    def test_elevate_leaves_the_story_awaiting_a_human(self, tmp_path, monkeypatch):
        state, result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=_report("elevate"),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        self._preserved(state, result)
        assert state.escalate_timeout_advice == ADVICE_ELEVATE
        assert "elevate" in result.message

    def test_unparseable_report_preserves_and_says_so(self, tmp_path, monkeypatch):
        state, result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=_unparseable_report(),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        self._preserved(state, result)
        assert state.escalate_timeout_advice == ADVICE_UNPARSEABLE
        assert "no parseable report" in result.message

    def test_empty_recommendation_preserves_and_says_so(self, tmp_path, monkeypatch):
        state, result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=_empty_recommendation_report(),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        self._preserved(state, result)
        assert state.escalate_timeout_advice == ADVICE_NO_RECOMMENDATION
        assert "recommended no action" in result.message

    def test_launch_failure_preserves_and_stays_distinct(self, tmp_path, monkeypatch):
        state, result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=None,
            launch_failure=True,
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        self._preserved(state, result)
        assert state.escalate_timeout_advice == ADVICE_LAUNCH_FAILURE
        assert "FAILED TO LAUNCH" in result.message
        assert "never launched" in result.message

    def test_surface_without_an_advisory_preserves(self, tmp_path, monkeypatch):
        # Remote/interactive expiry: no advisory is produced there at all, which
        # is a different absence from an advisor that ran and failed.
        state, result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=None,
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        self._preserved(state, result)
        assert state.escalate_timeout_advice == ADVICE_UNAVAILABLE
        assert "no advisory report" in result.message

    def test_recommendation_this_run_cannot_perform_is_not_substituted(
        self, tmp_path, monkeypatch
    ):
        # `accept` with no approvable reviewer result: applying it is impossible
        # and substituting anything else would record an outcome nobody chose.
        state, result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=_report("accept"),
            approvable=False,
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        self._preserved(state, result)
        assert state.escalate_timeout_advice == ADVICE_NOT_PERFORMABLE
        assert state.escalate_declined_action is None
        assert "cannot perform it" in result.message


# ── a present operator always governs ─────────────────────────────────────────


class TestOperatorSelectionBeatsAdvice:
    def test_selection_before_expiry_governs_over_a_different_recommendation(
        self, tmp_path, monkeypatch
    ):
        state, result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="defer_or_abandon",  # operator chose this...
            report=_report("accept"),  # ...advisor recommended this
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        assert state.escalate_selected_action == "defer_or_abandon"
        assert state.escalate_decision == "reject"
        assert state.escalate_decision_source == ESCALATE_SOURCE_OPERATOR
        assert state.escalate_timeout_advice is None
        assert result.success is False

    def test_pending_file_selection_before_expiry_governs(self, tmp_path, monkeypatch):
        """Through the real pending gate: a resolved file wins over the advice."""
        config = _config(tmp_path, timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE)
        task = _make_task(tmp_path)
        state = _escalated_state()
        report = _report("accept")

        def _fake_advisor(*a, **k):
            state.advisory_generated = True
            state.advisory_report = report.to_dict()
            return report

        monkeypatch.setattr(rp, "run_escalation_advisor", _fake_advisor)
        monkeypatch.setattr(rp, "_append_cycle_history", lambda *a, **k: None)
        monkeypatch.setattr(rp, "_escalate_notify", lambda *a, **k: None)

        with patch("theforge.pending.poll_pending", return_value=("redirect", "t")):
            with patch("theforge.notify_backends.send_notifications"):
                result = _run_escalate_gate(
                    state,
                    config,
                    task,
                    tmp_path / "ws",
                    "forge/test",
                    0.0,
                    auto_merge=False,
                    notify=True,
                    logger=None,
                    run_id="run-sel",
                )

        assert state.escalate_selected_action == "redirect"
        assert state.escalate_decision == "redirect"
        assert state.escalate_decision_source == ESCALATE_SOURCE_OPERATOR
        assert result.success is False


# ── the pending checkpoint tracks whether anything is still owed ──────────────


class TestPendingCheckpointLifecycle:
    def _run_real_gate(self, tmp_path, monkeypatch, *, report, timeout_policy, run_id):
        config = _config(tmp_path, timeout_policy=timeout_policy)
        task = _make_task(tmp_path)
        state = _escalated_state()
        state.advisory_packet = {"cycles": []}

        def _fake_advisor(*a, **k):
            state.advisory_generated = report.ok
            state.advisory_report = report.to_dict()
            return report

        monkeypatch.setattr(rp, "run_escalation_advisor", _fake_advisor)
        monkeypatch.setattr(rp, "_append_cycle_history", lambda *a, **k: None)
        monkeypatch.setattr(rp, "_escalate_notify", lambda *a, **k: None)

        with patch("theforge.pending.poll_pending", return_value=("timeout", None)):
            with patch("theforge.notify_backends.send_notifications"):
                _run_escalate_gate(
                    state,
                    config,
                    task,
                    tmp_path / "ws",
                    "forge/test",
                    0.0,
                    auto_merge=False,
                    notify=True,
                    logger=None,
                    run_id=run_id,
                )
        return state, tmp_path / ".forge" / "pending" / f"{run_id}.yaml"

    def test_applied_advice_clears_the_expired_checkpoint(self, tmp_path, monkeypatch):
        state, pending_file = self._run_real_gate(
            tmp_path,
            monkeypatch,
            report=_report("land_core_defer_edges"),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
            run_id="run-applied",
        )
        assert state.escalate_decision == "land_core_defer_edges"
        assert not pending_file.exists(), "a decided story must not look still-pending"

    def test_elevate_leaves_the_checkpoint_for_the_operator(self, tmp_path, monkeypatch):
        state, pending_file = self._run_real_gate(
            tmp_path,
            monkeypatch,
            report=_report("elevate"),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
            run_id="run-elevate",
        )
        assert state.escalate_decision == "advisory_pending"
        assert pending_file.exists()
        data = yaml.safe_load(pending_file.read_text())
        assert data["timed_out_awaiting_decision"] is True
        assert data.get("decision") is None


# ── provenance survives into the durable records ──────────────────────────────


class TestDecisionProvenanceIsRecorded:
    def test_audit_record_exposes_source_action_and_pending_status(self, tmp_path, monkeypatch):
        state, _result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=_report("land_core_defer_edges"),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        record = generate_audit_log(
            _config(tmp_path),
            _make_task(tmp_path),
            CoordinatorResult(success=False, phase=Phase.ESCALATE, state=state, message="m"),
        )
        block = record["escalation"]
        assert block["decision_source"] == ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT
        assert block["timeout_advice"] == ADVICE_APPLIED
        assert block["selected_action"] == "land_core_defer_edges"
        assert block["advisory_recommendation"] == "land_core_defer_edges"
        assert block["awaiting_operator"] is False

    def test_audit_record_marks_a_still_waiting_gate(self, tmp_path, monkeypatch):
        state, _result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=_report("elevate"),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        record = generate_audit_log(
            _config(tmp_path),
            _make_task(tmp_path),
            CoordinatorResult(success=False, phase=Phase.ESCALATE, state=state, message="m"),
        )
        block = record["escalation"]
        assert block["awaiting_operator"] is True
        assert block["decision_source"] == ESCALATE_SOURCE_TIMEOUT_PENDING
        assert block["timeout_advice"] == ADVICE_ELEVATE

    def test_resume_record_round_trips_provenance(self, tmp_path, monkeypatch):
        state, _result = _drive_gate(
            tmp_path,
            monkeypatch,
            gate_decision="timeout",
            report=_report("land_core_defer_edges"),
            timeout_policy=ESCALATE_TIMEOUT_APPLY_ADVICE,
        )
        block = _escalation_block(state)
        assert block is not None
        assert block["decision_source"] == ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT
        assert block["timeout_advice"] == ADVICE_APPLIED
        assert block["awaiting_operator"] is False

        restored = CoordinatorState()
        assert _apply_escalation(restored, block)
        assert restored.escalate_decision == "land_core_defer_edges"
        assert restored.escalate_decision_source == ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT
        assert restored.escalate_timeout_advice == ADVICE_APPLIED
