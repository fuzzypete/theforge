"""Pending-gate notifications must be actionable away from the machine."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from theforge.coordinator.pending_hitl import (
    _pending_escalate_gate,
    _pending_human_review,
    _pending_plan_review,
)

DECIDED_AT = "2026-08-14T19:05:00+00:00"


def _config(project_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=project_root,
        notifications=SimpleNamespace(human_review_timeout_seconds=3600, backends=()),
        plan_review=SimpleNamespace(timeout_seconds=1800, mode="blocking"),
    )


def _capture_notification(sent: dict[str, str]):
    def _capture(*_args, **kwargs):
        sent.update(kwargs)

    return _capture


def test_pending_human_review_notification_includes_run_id_options_and_deadline(
    tmp_path: Path,
) -> None:
    state = SimpleNamespace(
        total_cost_measured=1.25,
        total_cost=1.25,
        human_review_waited_seconds=None,
        human_review_mode=None,
    )
    parsed_review = SimpleNamespace(
        verdict="REQUEST_CHANGES",
        findings=[SimpleNamespace(severity="P1"), SimpleNamespace(severity="P2")],
        summary="One blocking finding needs a decision.",
    )
    task = SimpleNamespace(slug="issue-2312")
    sent: dict[str, str] = {}

    with (
        patch(
            "theforge.notify_backends.send_notifications",
            side_effect=_capture_notification(sent),
        ),
        patch("theforge.pending.poll_pending", return_value=("approve", DECIDED_AT)),
    ):
        decision, _feedback = _pending_human_review(
            state,
            parsed_review,
            tmp_path,
            "feat/issue-2312",
            task,
            _config(tmp_path),
            task_start=0.0,
            run_id="65273305e89c",
        )

    assert decision == "approve"
    assert sent["title"] == "TheForge: review needed — issue-2312 (HUMAN_REVIEW)"
    assert "Run ID: 65273305e89c" in sent["body"]
    assert "forge decide 65273305e89c <approve|reject|escalate|extend>" in sent["body"]
    assert "Deadline:" in sent["body"]


def test_pending_escalate_notification_uses_pending_record_options(tmp_path: Path) -> None:
    state = SimpleNamespace(
        human_review_waited_seconds=0.0,
        advisory_packet=None,
        advisory_launch_failure=False,
        advisory_launch_reason=None,
    )
    task = SimpleNamespace(slug="issue-2312")
    sent: dict[str, str] = {}

    with (
        patch(
            "theforge.coordinator.escalate_actions.available_escalate_actions",
            return_value=(["accept", "redirect", "defer_or_abandon"], {}),
        ),
        patch(
            "theforge.notify_backends.send_notifications",
            side_effect=_capture_notification(sent),
        ),
        patch("theforge.pending.poll_pending", return_value=("redirect", DECIDED_AT)),
    ):
        decision = _pending_escalate_gate(
            state,
            task,
            _config(tmp_path),
            "Quorum unmet.",
            reviewer_verdicts={"r1": "APPROVE", "r2": "REQUEST_CHANGES"},
            gate_result="threshold not met",
            run_id="65273305e89c",
            advisory=None,
        )

    assert decision == "redirect"
    assert sent["title"] == "TheForge: decision needed — issue-2312 (ESCALATE)"
    assert "Run ID: 65273305e89c" in sent["body"]
    assert "forge decide 65273305e89c <accept|redirect|defer_or_abandon>" in sent["body"]
    assert "Deadline:" in sent["body"]


def test_pending_plan_review_notification_includes_run_id_and_actions(tmp_path: Path) -> None:
    state = SimpleNamespace(plan_review_mode=None, plan_review_waited_seconds=None)
    task = SimpleNamespace(slug="issue-2312")
    sent: dict[str, str] = {}

    with (
        patch(
            "theforge.notify_backends.send_notifications",
            side_effect=_capture_notification(sent),
        ),
        patch("theforge.pending.poll_pending", return_value=("approve", DECIDED_AT)),
    ):
        decision = _pending_plan_review(
            state,
            "# Plan\n\n1. Fix notifications.\n2. Add tests.",
            tmp_path,
            task,
            _config(tmp_path),
            run_id="65273305e89c",
        )

    assert decision == "approve"
    assert sent["title"] == "TheForge: plan ready — issue-2312 (PLAN_REVIEW)"
    assert "Run ID: 65273305e89c" in sent["body"]
    assert "forge decide 65273305e89c <approve|regenerate|abandon>" in sent["body"]
    assert "Deadline:" in sent["body"]
