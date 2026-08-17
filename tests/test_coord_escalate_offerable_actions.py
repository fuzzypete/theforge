"""Seam tests: an escalate gate offers only actions it can actually perform (#2300).

The defect these close over: the gate presented the full action taxonomy
unconditionally, and when an operator selected ``accept`` with no retained
reviewer result the approve path substituted ``reject`` — recording the opposite
of what was chosen, in the one direction that cannot be revisited.

Covered here, across the phase boundaries the fix touches:

* the pending / interactive / ntfy gates withhold ``accept`` when no approvable
  reviewer result is retained, and say why rather than silently dropping it,
* a quorum-unmet cycle's surviving APPROVE verdict makes ``accept`` performable
  again through an explicit operator selection (without auto-bypassing quorum),
* a stale ``accept`` selection is DECLINED — the selection is recorded, no
  substitute decision is written, and the story is left as it was,
* the declined selection survives into the audit trail and the resume record.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import _make_config, _make_task

from theforge.config import BackendConfig, NotificationConfig
from theforge.coordinator import review_phase as rp
from theforge.coordinator.escalate_actions import (
    ACCEPT_UNAVAILABLE_REASON,
    NAMED_ACTION_UNAVAILABLE_REASON,
    approvable_review_result,
    available_escalate_actions,
)
from theforge.coordinator.pending_hitl import _pending_escalate_gate
from theforge.coordinator.review_phase import _run_escalate_gate
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.escalation_advisor import ACTION_TAXONOMY, AdvisoryOption, AdvisoryReport
from theforge.review import ReviewResult


def _result(verdict: str, *, parse_errors: list | None = None) -> ReviewResult:
    return ReviewResult(
        verdict=verdict,
        summary="s",
        findings=[],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=list(parse_errors or []),
        raw_yaml={},
    )


def _pending_config(tmp_path: Path, timeout: int = 1):
    base = _make_config(tmp_path)
    notifications = NotificationConfig(
        backend="ntfy",
        human_review_timeout_seconds=timeout,
        backends=(BackendConfig(type="ntfy", url="https://ntfy.sh/t"),),
    )
    return dataclasses.replace(
        base,
        notifications=notifications,
        retry=dataclasses.replace(base.retry, escalate_policy="prompt"),
    )


def _quorum_collapsed_state(survivor_verdict: str | None = None) -> CoordinatorState:
    """The observed shape: quorum unmet, no merged result, phase ESCALATE."""
    state = CoordinatorState()
    state.phase = Phase.ESCALATE
    state.review_cycle = 1
    state.review_results = []
    state.last_cycle_reviewer_results = (
        [("reviewer-a", _result(survivor_verdict))] if survivor_verdict else []
    )
    state.error = "Quorum unmet: 1/2 succeeded < threshold 2; failed: r2 (exit=1)"
    return state


def _advisory(recommendation: str, actions: list[str]) -> AdvisoryReport:
    return AdvisoryReport(
        recommendation=recommendation,
        rationale="r",
        options=[
            AdvisoryOption(
                action=a,
                evidence="e",
                forge_operation="op",
                risk="k",
                consequence="c",
            )
            for a in actions
        ],
        parse_errors=[],
        raw={},
    )


# ── The state-derived question itself ─────────────────────────────────────────


class TestApprovableResolution:
    def test_merged_result_is_preferred(self):
        state = _quorum_collapsed_state("APPROVE")
        merged = _result("REQUEST_CHANGES")
        state.review_results = [merged]
        assert approvable_review_result(state) is merged

    def test_quorum_unmet_survivor_approve_is_approvable(self):
        state = _quorum_collapsed_state("APPROVE")
        resolved = approvable_review_result(state)
        assert resolved is not None and resolved.verdict == "APPROVE"

    def test_survivor_request_changes_is_not_approvable(self):
        assert approvable_review_result(_quorum_collapsed_state("REQUEST_CHANGES")) is None

    def test_unparseable_survivor_is_not_approvable(self):
        state = _quorum_collapsed_state()
        state.last_cycle_reviewer_results = [("r", _result("APPROVE", parse_errors=["boom"]))]
        assert approvable_review_result(state) is None

    def test_accept_is_the_only_state_gated_action(self):
        available, omitted = available_escalate_actions(_quorum_collapsed_state(), ACTION_TAXONOMY)
        assert "accept" not in available
        assert available == ["defer_or_abandon"]
        assert omitted == {
            "accept": ACCEPT_UNAVAILABLE_REASON,
            "land_core_defer_edges": NAMED_ACTION_UNAVAILABLE_REASON,
            "redirect": NAMED_ACTION_UNAVAILABLE_REASON,
            "decompose": NAMED_ACTION_UNAVAILABLE_REASON,
            "elevate": NAMED_ACTION_UNAVAILABLE_REASON,
        }

    def test_nothing_is_withheld_when_a_result_exists(self):
        state = _quorum_collapsed_state("APPROVE")
        available, omitted = available_escalate_actions(state, ACTION_TAXONOMY)
        assert available == ["accept", "defer_or_abandon"]
        assert omitted == {
            "land_core_defer_edges": NAMED_ACTION_UNAVAILABLE_REASON,
            "redirect": NAMED_ACTION_UNAVAILABLE_REASON,
            "decompose": NAMED_ACTION_UNAVAILABLE_REASON,
            "elevate": NAMED_ACTION_UNAVAILABLE_REASON,
        }


# ── Pending-file gate: what is written to the checkpoint ──────────────────────


class TestPendingGateOptions:
    def _write(self, tmp_path, state, run_id, advisory):
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        with patch("theforge.pending.poll_pending", return_value=("timeout", None)):
            with patch("theforge.notify_backends.send_notifications"):
                _pending_escalate_gate(
                    state,
                    task,
                    config,
                    state.error,
                    {"reviewer-a": "APPROVE"},
                    "PASS",
                    run_id=run_id,
                    advisory=advisory,
                )
        path = tmp_path / ".forge" / "pending" / f"{run_id}.yaml"
        return yaml.safe_load(path.read_text())

    def test_accept_is_not_offered_without_an_approvable_result(self, tmp_path):
        state = _quorum_collapsed_state()
        state.advisory_packet = {"cycles": []}
        data = self._write(
            tmp_path, state, "run-noaccept", _advisory("redirect", ["redirect", "accept"])
        )

        assert "accept" not in data["options"]
        assert data["options"] == ["defer_or_abandon"]
        # Withheld, not silently dropped: the reason is legible at the checkpoint.
        assert data["omitted_actions"]["accept"] == ACCEPT_UNAVAILABLE_REASON
        assert data["omitted_actions"]["redirect"] == NAMED_ACTION_UNAVAILABLE_REASON
        assert "NOT OFFERED" in data["reason"]
        # And the advisory payload does not advertise it as a selectable option.
        assert data["advisory"]["options"] == []
        assert data["advisory"]["recommendation"] == ""

    def test_recommendation_the_gate_cannot_honour_is_dropped_and_explained(self, tmp_path):
        state = _quorum_collapsed_state()
        state.advisory_packet = {"cycles": []}
        data = self._write(
            tmp_path, state, "run-recdrop", _advisory("accept", ["accept", "redirect"])
        )

        assert "accept" not in data["options"]
        assert data["advisory"]["recommendation"] == ""
        assert "the advisor recommended 'accept'" in data["reason"]
        assert "RECOMMENDED ACTION: accept" not in data["reason"]

    def test_retained_quorum_survivor_makes_accept_offerable(self, tmp_path):
        state = _quorum_collapsed_state("APPROVE")
        state.advisory_packet = {"cycles": []}
        data = self._write(
            tmp_path, state, "run-survivor", _advisory("accept", ["accept", "redirect"])
        )

        assert data["options"] == ["accept", "defer_or_abandon"]
        assert data["omitted_actions"]["redirect"] == NAMED_ACTION_UNAVAILABLE_REASON
        assert data["advisory"]["recommendation"] == "accept"

    def test_unavailable_advisory_checkpoint_also_filters(self, tmp_path):
        state = _quorum_collapsed_state()
        data = self._write(tmp_path, state, "run-noadvisory", None)

        assert data["options"] == ["defer_or_abandon"]
        assert data["omitted_actions"]["accept"] == ACCEPT_UNAVAILABLE_REASON
        assert data["omitted_actions"]["redirect"] == NAMED_ACTION_UNAVAILABLE_REASON
        assert ACCEPT_UNAVAILABLE_REASON in data["reason"]


# ── Interactive + ntfy gates present the same filtered menu ───────────────────


class TestOtherGateSurfaces:
    def test_interactive_gate_hides_approve_and_refuses_the_key(self, tmp_path, monkeypatch):
        from theforge.coordinator import notify as notify_mod

        state = _quorum_collapsed_state()
        lines: list[str] = []
        monkeypatch.setattr(notify_mod._cu, "_log", lambda msg="": lines.append(str(msg)))
        # Operator tries 'a' anyway, then falls back to reject.
        replies = iter(["a\n", "r\n"])
        monkeypatch.setattr(notify_mod.sys.stdin, "readline", lambda: next(replies))

        decision = notify_mod._escalate_gate_interactive(state, "Quorum unmet", {}, "PASS")

        assert decision == "reject"
        rendered = "\n".join(lines)
        assert "NOT AVAILABLE" in rendered
        assert "Approve is not available" in rendered

    def test_interactive_gate_still_offers_approve_when_performable(self, tmp_path, monkeypatch):
        from theforge.coordinator import notify as notify_mod

        state = _quorum_collapsed_state("APPROVE")
        lines: list[str] = []
        monkeypatch.setattr(notify_mod._cu, "_log", lambda msg="": lines.append(str(msg)))
        monkeypatch.setattr(notify_mod.sys.stdin, "readline", lambda: "a\n")

        assert notify_mod._escalate_gate_interactive(state, "r", {}, "PASS") == "approve"
        assert "NOT AVAILABLE" not in "\n".join(lines)

    def test_ntfy_gate_omits_the_approve_action_button(self, tmp_path, monkeypatch):
        from theforge.coordinator import remote_gates as rg

        base = _make_config(tmp_path)
        from theforge.config import NtfyConfig

        config = dataclasses.replace(
            base,
            notifications=NotificationConfig(
                backend="ntfy",
                human_review_timeout_seconds=1,
                ntfy=NtfyConfig(url="https://ntfy.sh/t"),
            ),
        )
        published: dict = {}

        def _capture(url, title, body, priority=None, actions=None):
            published["body"] = body
            published["actions"] = actions

        monkeypatch.setattr(rg, "_ntfy_publish", _capture)
        monkeypatch.setattr(rg, "_ntfy_poll_escalate_reply", lambda *a, **k: "reject")

        rg._escalate_gate_remote(
            _quorum_collapsed_state(), _make_task(tmp_path), config, "Quorum unmet", {}, "PASS"
        )

        assert "Approve" not in published["actions"]
        assert "Reject" in published["actions"] and "Continue" in published["actions"]
        assert ACCEPT_UNAVAILABLE_REASON in published["body"]


# ── The disposition itself: decline, never substitute ─────────────────────────


class TestApproveDispositionDeclines:
    def _call(self, tmp_path, monkeypatch, state, gate_decision):
        config = _pending_config(tmp_path)
        task = _make_task(tmp_path)
        finalized: dict = {}

        def _finalize(_state, _config, _task, review, *a, **k):
            finalized["review"] = review
            return CoordinatorResult(
                success=True, phase=Phase.DONE, state=_state, message="approved"
            )

        monkeypatch.setattr(rp, "run_escalation_advisor", lambda *a, **k: None)
        monkeypatch.setattr(rp, "_pending_escalate_gate", lambda *a, **k: gate_decision)
        monkeypatch.setattr(rp, "_finalize_approve", _finalize)
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
            run_id="run-decline",
        )
        return result, finalized

    def test_stale_accept_is_declined_not_converted_to_reject(self, tmp_path, monkeypatch):
        state = _quorum_collapsed_state()
        result, finalized = self._call(tmp_path, monkeypatch, state, "accept")

        assert result is not None and result.success is False
        # The operator's selection is recorded; the opposite outcome is NOT.
        assert state.escalate_selected_action == "accept"
        assert state.escalate_declined_action == "accept"
        assert state.escalate_declined_reason == ACCEPT_UNAVAILABLE_REASON
        assert state.escalate_decision is None, "no substitute decision may be recorded"
        # The story is left where it was — no finalize, phase unchanged.
        assert finalized == {}
        assert state.phase == Phase.ESCALATE
        assert "declined" in result.message

    def test_named_action_is_declined_without_recording_resolution(self, tmp_path, monkeypatch):
        state = _quorum_collapsed_state("APPROVE")
        result, finalized = self._call(tmp_path, monkeypatch, state, "redirect")

        assert result is not None and result.success is False
        assert state.escalate_selected_action == "redirect"
        assert state.escalate_declined_action == "redirect"
        assert state.escalate_declined_reason == NAMED_ACTION_UNAVAILABLE_REASON
        assert state.escalate_decision is None
        assert finalized == {}
        assert "was not carried out" in result.message

    def test_legacy_approve_selection_is_declined_the_same_way(self, tmp_path, monkeypatch):
        state = _quorum_collapsed_state()
        result, finalized = self._call(tmp_path, monkeypatch, state, "approve")

        assert result is not None and result.success is False
        assert state.escalate_declined_action == "approve"
        assert state.escalate_decision is None
        assert finalized == {}

    def test_accept_on_a_retained_survivor_approves_on_that_verdict(self, tmp_path, monkeypatch):
        state = _quorum_collapsed_state("APPROVE")
        result, finalized = self._call(tmp_path, monkeypatch, state, "accept")

        assert result is not None and result.success is True
        assert result.phase == Phase.DONE
        assert state.escalate_decision == "accept"
        assert finalized["review"].verdict == "APPROVE"

    def test_quorum_collapse_is_not_auto_approved_by_a_survivor(self, tmp_path, monkeypatch):
        """A retained APPROVE makes accept SELECTABLE, never automatic.

        auto_approve stays keyed on a merged result, so a collapsed quorum still
        escalates for an operator decision instead of landing on one voice.
        """
        state = _quorum_collapsed_state("APPROVE")
        config = dataclasses.replace(
            _pending_config(tmp_path),
            retry=dataclasses.replace(
                _make_config(tmp_path).retry, escalate_policy="auto_approve"
            ),
        )
        state.gate_decisions = ["PASS"]
        task = _make_task(tmp_path)
        monkeypatch.setattr(rp, "run_escalation_advisor", lambda *a, **k: None)
        monkeypatch.setattr(rp, "_pending_escalate_gate", lambda *a, **k: "timeout")
        monkeypatch.setattr(rp, "_escalate_notify", lambda *a, **k: None)
        monkeypatch.setattr(
            rp,
            "_finalize_approve",
            lambda *a, **k: _must_not_finalize(),
        )

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
            run_id="run-noauto",
        )
        assert result is not None and result.success is False
        assert state.escalate_decision == "advisory_pending"


def _must_not_finalize():  # pragma: no cover - only reached on regression
    raise AssertionError("auto_approve must not finalize on a collapsed quorum")


# ── Seam: an offered accept must survive all the way through landing ──────────


class TestAcceptOnSurvivorLandsThroughMergePr:
    """The gate offering accept is only half the promise — landing must honour it.

    merge-pr landing fails closed when it has no ReviewResult to post, and every
    land_story caller used to re-derive one from state.review_results — which is
    empty on exactly this path. An accept taken on a retained quorum-unmet
    survivor would then report "no review result available" for an action the
    gate had offered and the operator had chosen.
    """

    def _merge_pr_config(self, tmp_path):
        base = _pending_config(tmp_path)
        return dataclasses.replace(
            base, workspace=dataclasses.replace(base.workspace, on_approve="merge-pr")
        )

    def _accept_on_survivor(self, tmp_path, monkeypatch, config):
        """Drive the real _finalize_approve through an accept on a survivor."""
        state = _quorum_collapsed_state("APPROVE")
        monkeypatch.setattr(rp, "run_escalation_advisor", lambda *a, **k: None)
        monkeypatch.setattr(rp, "_pending_escalate_gate", lambda *a, **k: "accept")
        monkeypatch.setattr(rp, "_escalate_notify", lambda *a, **k: None)
        monkeypatch.setattr(rp, "_append_cycle_history", lambda *a, **k: None)
        monkeypatch.setattr(
            "theforge.coordinator.completion._ntfy_done_notify", lambda *a, **k: None
        )
        result = _run_escalate_gate(
            state,
            config,
            _make_task(tmp_path),
            tmp_path / "ws",
            "forge/test",
            0.0,
            auto_merge=False,
            notify=True,
            logger=None,
            run_id="run-land",
        )
        return state, result

    def test_landing_carries_the_gate_selected_survivor_review(self, tmp_path, monkeypatch):
        from theforge.coordinator.completion import resolve_landing_review

        config = self._merge_pr_config(tmp_path)
        state, result = self._accept_on_survivor(tmp_path, monkeypatch, config)

        assert result is not None and result.success is True
        assert result.landing_status == "pending_integration"
        # state.review_results stays empty — quorum never merged anything — so
        # the landing caller has to get the review from the stamp, not a re-derive.
        assert state.review_results == []
        assert resolve_landing_review(state) is not None
        assert resolve_landing_review(state).verdict == "APPROVE"
        assert state.landing_review_source == "escalate_gate_selection"

    def test_merge_pr_landing_posts_the_survivor_instead_of_short_circuiting(
        self, tmp_path, monkeypatch
    ):
        from theforge.coordinator.completion import land_story, resolve_landing_review

        config = self._merge_pr_config(tmp_path)
        state, _result = self._accept_on_survivor(tmp_path, monkeypatch, config)

        posted: dict = {}

        def _fake_merge_pr(_config, _task, _branch, parsed_review, _state):
            posted["review"] = parsed_review
            return {
                "action": "merge-pr",
                "merged": True,
                "merge_queued": False,
                "landing_path": "fresh-merge",
                "pr_url": "https://example/pr/1",
            }

        monkeypatch.setattr("theforge.coordinator.completion._merge_pr", _fake_merge_pr)

        merge_info, landing_status = land_story(
            config,
            _make_task(tmp_path),
            "forge/test",
            tmp_path / "ws",
            resolve_landing_review(state),
            state,
            "merge-pr",
        )

        assert landing_status == "landed"
        assert merge_info.get("landing_path") != "missing-review"
        assert merge_info.get("error") != "no review result available"
        assert posted["review"].verdict == "APPROVE"

    def test_merged_review_landings_are_unchanged(self, tmp_path, monkeypatch):
        """The normal path still lands on the merged cycle review, tagged as such."""
        from theforge.coordinator.completion import _finalize_approve, resolve_landing_review

        config = self._merge_pr_config(tmp_path)
        state = _quorum_collapsed_state()
        merged = _result("APPROVE")
        state.review_results = [merged]
        monkeypatch.setattr(
            "theforge.coordinator.completion._ntfy_done_notify", lambda *a, **k: None
        )

        _finalize_approve(
            state,
            config,
            _make_task(tmp_path),
            merged,
            tmp_path / "ws",
            "forge/test",
            0.0,
            auto_merge=False,
            notify=False,
            logger=None,
            review_cost=0.0,
            review_elapsed=0.0,
            message="done. ",
        )

        assert resolve_landing_review(state) is merged
        assert state.landing_review_source == "merged_cycle_review"

    def test_audit_names_which_review_the_landing_used(self, tmp_path, monkeypatch):
        from theforge.coordinator.audit import generate_audit_log

        config = self._merge_pr_config(tmp_path)
        state, result = self._accept_on_survivor(tmp_path, monkeypatch, config)

        record = generate_audit_log(config, _make_task(tmp_path), result)

        assert record["landing_review"] == {
            "source": "escalate_gate_selection",
            "verdict": "APPROVE",
        }

    def test_no_landing_leaves_the_audit_field_empty(self, tmp_path):
        from theforge.coordinator.audit import generate_audit_log

        state = _quorum_collapsed_state()
        result = CoordinatorResult(
            success=False, phase=Phase.ESCALATE, state=state, message="escalated"
        )
        record = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)
        assert record["landing_review"] is None


# ── The declined selection is recoverable from the run afterwards ─────────────


class TestDeclinedSelectionIsRecorded:
    def test_audit_escalation_block_carries_the_declined_selection(self, tmp_path):
        from theforge.coordinator.audit import generate_audit_log

        state = _quorum_collapsed_state()
        state.escalate_selected_action = "accept"
        state.escalate_declined_action = "accept"
        state.escalate_declined_reason = ACCEPT_UNAVAILABLE_REASON
        state.escalate_reason = state.error
        result = CoordinatorResult(
            success=False, phase=Phase.ESCALATE, state=state, message="declined"
        )

        record = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), result)
        escalation = record["escalation"]

        # Present even though no decision was made — the choice must not survive
        # only as a log line.
        assert escalation is not None
        assert escalation["human_decision"] is None
        assert escalation["selected_action"] == "accept"
        assert escalation["declined_action"] == "accept"
        assert escalation["declined_reason"] == ACCEPT_UNAVAILABLE_REASON

    def test_resume_record_round_trips_the_declined_selection(self, tmp_path):
        from theforge.coordinator import resume_persistence as persist

        state = _quorum_collapsed_state()
        state.escalate_selected_action = "accept"
        state.escalate_declined_action = "accept"
        state.escalate_declined_reason = ACCEPT_UNAVAILABLE_REASON
        state.escalate_reason = state.error

        block = persist._escalation_block(state)
        assert block is not None
        assert block["declined_action"] == "accept"
        assert block["decision"] is None

        restored = CoordinatorState()
        assert persist._apply_escalation(restored, block)
        assert restored.escalate_declined_action == "accept"
        assert restored.escalate_declined_reason == ACCEPT_UNAVAILABLE_REASON
        # Still undecided, so a later real operator decision can still be recorded.
        assert restored.escalate_decision is None
