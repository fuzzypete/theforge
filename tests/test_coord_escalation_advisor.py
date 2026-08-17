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

from theforge.config import AgentDef, BackendConfig, NotificationConfig, transport_for
from theforge.coordinator import escalation_advisor_flow as flow
from theforge.coordinator import review_phase as rp
from theforge.coordinator.pending_hitl import _pending_escalate_gate
from theforge.coordinator.resume_persistence import _apply_escalation, _escalation_block
from theforge.coordinator.review_phase import _run_escalate_gate
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.escalation_advisor import AdvisoryOption, AdvisoryReport
from theforge.model_capabilities import (
    CAPABILITY_TOOL_STRUCTURED,
    capabilities_path,
    signature_for_agent,
)
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


def _report_with_errors() -> AdvisoryReport:
    """An invalid advisory report (parse_errors set → .ok is False)."""
    return AdvisoryReport(
        recommendation="",
        rationale="",
        options=[],
        parse_errors=["no <advisory_report> block found"],
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
    agents = [
        AgentDef(
            name="advisor-fast",
            provider="anthropic",
            model="sonnet",
            budget_usd=3.0,
            timeout_seconds=600,
            tier="fast",
            transport=transport_for("anthropic", "api"),
            registry_id="anthropic/sonnet/api",
        ),
        AgentDef(
            name="advisor-strong",
            provider="openai",
            model="gpt-5.4",
            budget_usd=8.0,
            timeout_seconds=900,
            tier="strong",
            transport=transport_for("openai", "api"),
            registry_id="openai/gpt-5.4/api",
        ),
    ]
    return dataclasses.replace(base, notifications=notifications, retry=new_retry, agents=agents)


def _write_absent_capability_record(tmp_path: Path, *agents: AgentDef) -> None:
    identities: dict[str, dict] = {}
    for agent in agents:
        identity_key = f"{agent.effective_provider}/{agent.model}/{agent.transport.kind}"
        identities[identity_key] = {
            "provider": agent.effective_provider,
            "model": agent.model,
            "transport": agent.transport.kind,
            "capabilities": {
                CAPABILITY_TOOL_STRUCTURED: {
                    "outcome": "absent",
                    "established_at": "2026-08-15T09:00:00Z",
                    "subject_signature": signature_for_agent(agent),
                    "detail": "no valid verdict in structured output",
                    "probe_role": "agent-code-review",
                }
            },
        }
    path = capabilities_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 1, "identities": identities}), encoding="utf-8")


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
        assert captured["profile"].phase == "advisor"
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
        # An advisor that RAN and produced nothing usable is not a launch defect.
        assert state.advisory_launch_failure is False
        assert (
            state.advisory_unavailable_reason == "advisor returned failure before a usable report"
        )

    def test_launch_failure_recorded_distinctly_from_advisory_unavailable(
        self, tmp_path, monkeypatch
    ):
        """A pre-turn advisor exit is a configuration defect, not "no advice today" (#2164)."""
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()

        launch_stderr = (
            "warning: `--full-auto` is deprecated; use `--sandbox workspace-write` instead.\n"
            "Not inside a trusted directory and --skip-git-repo-check was not specified."
        )
        monkeypatch.setattr(
            flow,
            "run_agent",
            lambda **k: _make_agent_result(
                success=False,
                output=launch_stderr,
                cost_usd=0.0,
                profile_name="advisor",
                startup_failure=True,
                failure_code="cli_launch_failure",
            ),
        )
        monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)

        report = rp.run_escalation_advisor(state, config, task, tmp_path / "ws")
        assert report is None
        assert state.advisory_generated is False
        assert state.advisory_launch_failure is True
        assert "trusted directory" in state.advisory_launch_reason

    def test_ineligible_structured_incapable_models_are_not_contacted(self, tmp_path, monkeypatch):
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()
        _write_absent_capability_record(tmp_path, *config.agents)

        called = {"run_agent": False}

        def _should_not_run(**kwargs):
            called["run_agent"] = True
            return _make_agent_result(success=True, output="", profile_name="advisor")

        monkeypatch.setattr(flow, "run_agent", _should_not_run)
        monkeypatch.setattr(flow, "log_agent_result", lambda *a, **k: None)

        report = rp.run_escalation_advisor(state, config, task, tmp_path / "ws")

        assert report is None
        assert called["run_agent"] is False
        assert "no candidate can serve role 'advisor'" in (state.advisory_unavailable_reason or "")


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
        assert seen["options"] == ["accept", "defer_or_abandon"]
        assert seen["advisory"]["recommendation"] == ""
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

    def test_timeout_preserves_selectable_pending_checkpoint(self, tmp_path):
        # P1 regression: on timeout the pending file must NOT be deleted — the
        # operator still has to be able to select an action. It must remain on disk
        # with the taxonomy options + advisory payload and an awaiting-decision marker.
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()
        state.advisory_packet = {"cycles": []}
        report = _canned_report("redirect")

        with patch("theforge.pending.poll_pending", return_value=("timeout", None)):
            with patch("theforge.notify_backends.send_notifications"):
                _pending_escalate_gate(
                    state,
                    task,
                    config,
                    state.error,
                    {"reviewer-a": "REQUEST_CHANGES"},
                    None,
                    run_id="run-preserve",
                    advisory=report,
                )

        pending_file = tmp_path / ".forge" / "pending" / "run-preserve.yaml"
        assert pending_file.exists(), "pending checkpoint must survive a timeout"
        data = yaml.safe_load(pending_file.read_text())
        assert data["options"] == ["accept", "defer_or_abandon"]
        assert data["advisory"]["recommendation"] == ""
        assert data["timed_out_awaiting_decision"] is True
        assert data.get("decision") is None  # still resolvable — no auto-decision written

        # And the operator can still resolve it after the timeout.
        from theforge import pending as _pending

        assert _pending.resolve_pending("run-preserve", "redirect", project_root=tmp_path)
        assert yaml.safe_load(pending_file.read_text())["decision"] == "redirect"

    def _write_checkpoint_without_advisory(self, tmp_path, state, run_id: str) -> dict:
        """Drive the gate with no usable advisory and return the pending payload."""
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        with patch("theforge.pending.poll_pending", return_value=("timeout", None)):
            with patch("theforge.notify_backends.send_notifications"):
                _pending_escalate_gate(
                    state,
                    task,
                    config,
                    state.error,
                    {"reviewer-a": "REQUEST_CHANGES"},
                    "PASS",
                    run_id=run_id,
                    advisory=None,
                )
        path = tmp_path / ".forge" / "pending" / f"{run_id}.yaml"
        return yaml.safe_load(path.read_text())

    def test_launch_failure_checkpoint_names_the_defect_and_zero_cost(self, tmp_path):
        """The operator must be able to tell a launch defect from a no-conclusion run."""
        state = _escalated_state()
        state.advisory_launch_failure = True
        state.advisory_launch_reason = (
            "Not inside a trusted directory and --skip-git-repo-check was not specified."
        )

        data = self._write_checkpoint_without_advisory(tmp_path, state, "run-launchfail")

        assert data["advisory_launch_failure"] is True
        assert "trusted directory" in data["advisory_launch_reason"]
        assert data["advisory_cost_usd"] == 0.0
        assert "FAILED TO LAUNCH" in data["reason"]
        assert "$0.00" in data["reason"]
        # Only executable actions are offered, even when the advisor is unavailable.
        assert data["options"] == ["accept", "defer_or_abandon"]

    def test_advisory_unavailable_checkpoint_unchanged_without_launch_failure(self, tmp_path):
        """An advisor that ran and produced nothing keeps the plain unavailable wording."""
        data = self._write_checkpoint_without_advisory(
            tmp_path, _escalated_state(), "run-nounavail"
        )

        assert data["advisory_unavailable"] is True
        assert "advisory_launch_failure" not in data
        assert "advisory_cost_usd" not in data
        assert "FAILED TO LAUNCH" not in data["reason"]
        assert "advisory report unavailable" in data["reason"]

    def test_missing_advisory_input_reason_is_rendered_and_persisted(self, tmp_path):
        state = _escalated_state()
        state.advisory_unavailable_reason = (
            "no candidate can serve role 'advisor': every agent in the pool has "
            "tool-structured demonstrated absent"
        )

        data = self._write_checkpoint_without_advisory(tmp_path, state, "run-missing-input")

        assert data["advisory_unavailable"] is True
        assert "advisory_unavailable_reason" in data
        assert "advisory input missing" in data["reason"]
        assert "tool-structured demonstrated absent" in data["reason"]

    def test_resumed_state_keeps_missing_advisory_input_reason(self, tmp_path):
        original = _escalated_state()
        original.escalate_decision = "advisory_pending"
        original.escalate_reason = original.error
        original.advisory_unavailable_reason = (
            "no configured model is phase-eligible for advisor "
            "(routing.phase_eligibility excludes advisor)"
        )
        block = _escalation_block(original)
        assert block is not None

        restored = CoordinatorState()
        assert _apply_escalation(restored, block) is True
        assert restored.advisory_unavailable_reason == original.advisory_unavailable_reason

        data = self._write_checkpoint_without_advisory(tmp_path, restored, "run-restored-missing")

        assert "phase-eligible for advisor" in data["reason"]
        assert data["advisory_unavailable_reason"] == original.advisory_unavailable_reason

    def test_valid_no_recommendation_stays_distinct_from_unavailable(self, tmp_path):
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()
        report = AdvisoryReport(
            recommendation="",
            rationale="The evidence does not support one action over the others.",
            options=[],
            parse_errors=[],
            raw={},
        )

        with patch("theforge.pending.poll_pending", return_value=("timeout", None)):
            with patch("theforge.notify_backends.send_notifications"):
                _pending_escalate_gate(
                    state,
                    task,
                    config,
                    state.error,
                    {"reviewer-a": "REQUEST_CHANGES"},
                    "PASS",
                    run_id="run-no-rec",
                    advisory=report,
                )

        data = yaml.safe_load((tmp_path / ".forge" / "pending" / "run-no-rec.yaml").read_text())
        assert data["advisory_no_recommendation"] is True
        assert data.get("advisory_unavailable") is None
        assert "recommended no action" in data["reason"]

    def test_decision_cleans_up_pending_file(self, tmp_path):
        # The complementary case: when the operator DOES select, the resolved
        # pending file is cleaned up (not left dangling).
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()
        state.advisory_packet = {"cycles": []}

        with patch("theforge.pending.poll_pending", return_value=("accept", "t")):
            with patch("theforge.notify_backends.send_notifications"):
                decision = _pending_escalate_gate(
                    state,
                    task,
                    config,
                    state.error,
                    {"reviewer-a": "REQUEST_CHANGES"},
                    None,
                    run_id="run-decided",
                    advisory=_canned_report("accept"),
                )
        assert decision == "accept"
        assert not (tmp_path / ".forge" / "pending" / "run-decided.yaml").exists()


# ── _run_escalate_gate: disposition normalisation ─────────────────────────────


class TestRunEscalateGateDispositions:
    def _call(self, tmp_path, monkeypatch, gate_decision, *, report=None):
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()

        # Fresh advisor + pending decision are both stubbed so we exercise the
        # normalisation logic in _run_escalate_gate deterministically. The advisor
        # stub sets state.advisory_generated like the real flow so the preserve
        # message can distinguish advisory vs no-advisory timeouts.
        def _fake_advisor(*a, **k):
            rep = report or _canned_report("redirect")
            state.advisory_generated = rep.ok
            state.advisory_report = rep.to_dict()
            return rep

        monkeypatch.setattr(rp, "run_escalation_advisor", _fake_advisor)
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

    def test_redirect_is_declined_not_recorded_as_resolved(self, tmp_path, monkeypatch):
        state, result = self._call(tmp_path, monkeypatch, "redirect")
        assert result is not None and result.success is False
        assert state.escalate_selected_action == "redirect"
        assert state.escalate_declined_action == "redirect"
        assert state.escalate_decision is None
        assert "was not carried out" in result.message

    def test_elevate_is_declined_not_recorded_as_resolved(self, tmp_path, monkeypatch):
        state, result = self._call(
            tmp_path, monkeypatch, "elevate", report=_canned_report("elevate")
        )
        assert result.success is False
        assert state.escalate_declined_action == "elevate"
        assert state.escalate_decision is None
        assert "was not carried out" in result.message

    def test_timeout_does_not_auto_reject(self, tmp_path, monkeypatch):
        state, result = self._call(tmp_path, monkeypatch, "timeout")
        assert result is not None and result.success is False
        # Contract change: timeout preserves rather than auto-rejecting.
        assert state.escalate_decision == "advisory_pending"
        assert state.escalate_decision != "reject"
        # Advisory was generated on this (pending-file) path — message says so.
        assert state.advisory_generated is True
        assert "advisory report was generated" in result.message

    def test_timeout_preserve_message_omits_advisory_when_none_generated(
        self, tmp_path, monkeypatch
    ):
        # Remote/interactive timeouts also preserve, but no advisory is produced
        # there — the preserve message must not falsely claim one was generated.
        state, result = self._call(tmp_path, monkeypatch, "timeout", report=_report_with_errors())
        assert result.success is False
        assert state.escalate_decision == "advisory_pending"
        assert state.advisory_generated is False
        assert "advisory report was generated" not in result.message
        assert "operator action selection is still" in result.message

    def test_timeout_preserve_message_names_an_advisor_launch_failure(self, tmp_path, monkeypatch):
        """Preserve message must distinguish "never launched" from "no advisory"."""
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        state = _escalated_state()

        def _fake_advisor(*a, **k):
            state.advisory_generated = False
            state.advisory_launch_failure = True
            state.advisory_launch_reason = "Not inside a trusted directory"
            return None

        monkeypatch.setattr(rp, "run_escalation_advisor", _fake_advisor)
        monkeypatch.setattr(rp, "_pending_escalate_gate", lambda *a, **k: "timeout")
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
            run_id="run-lf",
        )
        assert state.escalate_decision == "advisory_pending"
        assert "FAILED TO LAUNCH" in result.message
        assert "Not inside a trusted directory" in result.message
        assert "advisory report was generated" not in result.message

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
