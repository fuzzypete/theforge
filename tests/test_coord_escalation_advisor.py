"""Seam-level tests for escalation-advisor wiring at the escalate gate.

Covers the coordinator boundary the story touches:

* ``run_escalation_advisor`` invokes a FRESH agent (not the dev/review sessions)
  and records the packet + report on state.
* the pending escalate gate presents the fixed action taxonomy + embeds the
  advisory payload, and no longer auto-rejects on timeout.
* ``_run_escalate_gate`` normalises taxonomy actions and timeout into the right
  coordinator dispositions (approve / reject / named-preserve / timeout-preserve).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import _make_agent_result, _make_config, _make_task

from theforge.config import BackendConfig, NotificationConfig
from theforge.coordinator import escalation_advisor_flow as flow
from theforge.coordinator import review_phase as rp
from theforge.coordinator.pending_hitl import _pending_escalate_gate
from theforge.coordinator.review_phase import _run_escalate_gate
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.escalation_advisor import AdvisoryOption, AdvisoryReport
from theforge.review import ReviewFinding, ReviewResult


def _review(verdict: str = "REQUEST_CHANGES") -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary="still not there",
        findings=[
            ReviewFinding(severity="P1", file="f.py", line=1, observed="bug", suggestion=None)
        ],
        story_matches=False,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _canned_report(recommendation: str = "redirect") -> AdvisoryReport:
    return AdvisoryReport(
        recommendation=recommendation,
        rationale="churn indicates an unbounded approach",
        options=[
            AdvisoryOption(
                action=recommendation,
                evidence="cycles 1-5 each found a new bypass",
                forge_operation="re-run with constraint",
                risk="framing may be under-specified",
                consequence="dev re-runs against the invariant",
            ),
        ],
        parse_errors=[],
        raw={},
    )


def _pending_config(tmp_path: Path, timeout: int = 1):
    """Config that activates pending-file HITL mode (backends non-empty)."""
    base = _make_config(tmp_path)
    notifications = NotificationConfig(
        backend="ntfy",
        human_review_timeout_seconds=timeout,
        backends=(BackendConfig(type="ntfy", url="https://ntfy.sh/t"),),
    )
    new_retry = dataclasses.replace(base.retry, escalate_policy="prompt")
    return dataclasses.replace(base, notifications=notifications, retry=new_retry)


def _escalated_state() -> CoordinatorState:
    state = CoordinatorState()
    state.phase = Phase.ESCALATE
    state.review_cycle = 5
    state.story_content = "Body.\n\n## Acceptance criteria\n\n- do the thing\n"
    rr = _review()
    state.review_results = [rr]
    state.last_cycle_reviewer_results = [("reviewer-a", rr)]
    state.error = "Review requested changes after 5 cycles. Max cycles (5) exhausted."
    return state


# ── run_escalation_advisor: fresh context ─────────────────────────────────────


class TestRunEscalationAdvisor:
    def test_invokes_fresh_agent_and_records_report(self, tmp_path, monkeypatch):
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()

        advisor_output = """
<advisory_report>
recommendation: redirect
rationale: unbounded blocklist
options:
  - action: redirect
    evidence: cycles 1-5 each found a bypass
    forge_operation: re-run with constraint
    risk: rr
    consequence: cc
</advisory_report>
"""
        captured = {}

        def fake_run_agent(*, prompt, profile, working_dir, secrets):
            captured["prompt"] = prompt
            captured["profile"] = profile
            return _make_agent_result(success=True, output=advisor_output, profile_name="advisor")

        # Patch the flow module's lazy runner slots directly (fresh agent).
        monkeypatch.setattr(flow, "run_agent", fake_run_agent)
        monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)

        report = rp.run_escalation_advisor(state, config, task, tmp_path / "ws")

        assert report is not None and report.ok
        assert report.recommendation == "redirect"
        # Fresh context: the advisor prompt is not a dev/review prompt and carries
        # the evidence packet.
        assert "ESCALATION ADVISOR" in captured["prompt"]
        assert "Acceptance criteria" in captured["prompt"]
        # Recorded on state for the audit trail.
        assert state.advisory_generated is True
        assert state.advisory_report["recommendation"] == "redirect"
        assert state.advisory_packet is not None

    def test_agent_failure_returns_none_and_preserves(self, tmp_path, monkeypatch):
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()

        monkeypatch.setattr(
            flow,
            "run_agent",
            lambda **k: _make_agent_result(success=False, output="", profile_name="advisor"),
        )
        monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)

        report = rp.run_escalation_advisor(state, config, task, tmp_path / "ws")
        assert report is None
        assert state.advisory_generated is False


# ── Pending escalate gate: taxonomy options + no auto-reject on timeout ────────


class TestPendingEscalateGate:
    def test_writes_taxonomy_options_and_advisory_payload(self, tmp_path):
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()
        state.advisory_packet = {"story_name": "s", "issue_ref": "#1", "cycles": []}
        report = _canned_report("redirect")

        seen = {}

        def fake_poll(run_id, timeout_seconds, project_root=None, **kw):
            path = Path(project_root) / ".forge" / "pending" / f"{run_id}.yaml"
            data = yaml.safe_load(path.read_text())
            seen["options"] = data["options"]
            seen["advisory"] = data.get("advisory")
            seen["decision_required"] = data.get("decision_required")
            return "redirect", "2026-07-16T00:00:00Z"

        with patch("theforge.pending.poll_pending", side_effect=fake_poll):
            with patch("theforge.notify_backends.send_notifications"):
                decision = _pending_escalate_gate(
                    state,
                    task,
                    config,
                    state.error,
                    {"reviewer-a": "REQUEST_CHANGES"},
                    "PASS",
                    run_id="run-1",
                    advisory=report,
                )

        assert decision == "redirect"
        assert seen["options"] == list(rp.ACTION_TAXONOMY)
        assert seen["advisory"]["recommendation"] == "redirect"
        assert seen["decision_required"] is True

    def test_timeout_returns_timeout_not_reject(self, tmp_path):
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()
        state.advisory_packet = {"cycles": []}
        report = _canned_report("redirect")

        with patch("theforge.pending.poll_pending", return_value=("timeout", None)):
            with patch("theforge.notify_backends.send_notifications"):
                decision = _pending_escalate_gate(
                    state,
                    task,
                    config,
                    state.error,
                    {"reviewer-a": "REQUEST_CHANGES"},
                    None,
                    run_id="run-2",
                    advisory=report,
                )
        assert decision == "timeout"


# ── _run_escalate_gate: disposition normalisation ─────────────────────────────


class TestRunEscalateGateDispositions:
    def _call(self, tmp_path, monkeypatch, gate_decision, *, report=None):
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()

        # Fresh advisor + pending decision are both stubbed so we exercise the
        # normalisation logic in _run_escalate_gate deterministically.
        monkeypatch.setattr(
            rp, "run_escalation_advisor", lambda *a, **k: report or _canned_report("redirect")
        )
        monkeypatch.setattr(rp, "_pending_escalate_gate", lambda *a, **k: gate_decision)
        # Approve path calls _finalize_approve — stub it to a DONE result.
        monkeypatch.setattr(
            rp,
            "_finalize_approve",
            lambda *a, **k: CoordinatorResult(
                success=True, phase=Phase.DONE, state=state, message="approved"
            ),
        )
        monkeypatch.setattr(rp, "_append_cycle_history", lambda *a, **k: None)
        monkeypatch.setattr(rp, "_escalate_notify", lambda *a, **k: None)

        return state, _run_escalate_gate(
            state,
            config,
            task,
            tmp_path / "ws",
            "forge/test",
            0.0,
            auto_merge=False,
            notify=True,
            logger=None,
            run_id="run-x",
        )

    def test_accept_maps_to_approve(self, tmp_path, monkeypatch):
        state, result = self._call(
            tmp_path, monkeypatch, "accept", report=_canned_report("accept")
        )
        assert result is not None and result.success is True
        assert result.phase == Phase.DONE
        assert state.escalate_selected_action == "accept"

    def test_defer_or_abandon_maps_to_reject(self, tmp_path, monkeypatch):
        state, result = self._call(tmp_path, monkeypatch, "defer_or_abandon")
        assert result is not None and result.success is False
        assert state.escalate_decision == "reject"
        assert state.escalate_selected_action == "defer_or_abandon"

    def test_redirect_maps_to_named_preserve(self, tmp_path, monkeypatch):
        state, result = self._call(tmp_path, monkeypatch, "redirect")
        assert result is not None and result.success is False
        # Named action preserves the worktree and records the taxonomy action —
        # it is NOT a plain reject that discards the work.
        assert state.escalate_decision == "redirect"
        assert state.escalate_selected_action == "redirect"
        assert "re-run with constraint" in result.message

    def test_elevate_maps_to_named_preserve(self, tmp_path, monkeypatch):
        state, result = self._call(
            tmp_path, monkeypatch, "elevate", report=_canned_report("elevate")
        )
        assert result.success is False
        assert state.escalate_decision == "elevate"
        assert "bump" in result.message

    def test_timeout_does_not_auto_reject(self, tmp_path, monkeypatch):
        state, result = self._call(tmp_path, monkeypatch, "timeout")
        assert result is not None and result.success is False
        # Contract change: timeout preserves rather than auto-rejecting.
        assert state.escalate_decision == "advisory_pending"
        assert state.escalate_decision != "reject"

    def test_reject_policy_still_short_circuits_without_advisor(self, tmp_path, monkeypatch):
        # escalate_policy=reject must not even generate an advisory.
        config = _pending_config(tmp_path)
        config = dataclasses.replace(
            config, retry=dataclasses.replace(config.retry, escalate_policy="reject")
        )
        task = _make_task(tmp_path)
        state = _escalated_state()
        called = {"advisor": False}

        def _should_not_run(*a, **k):
            called["advisor"] = True
            return _canned_report()

        monkeypatch.setattr(rp, "run_escalation_advisor", _should_not_run)
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
            run_id="run-r",
        )
        assert result is not None and result.success is False
        assert state.escalate_decision == "reject"
        assert called["advisor"] is False
