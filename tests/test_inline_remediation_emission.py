"""Inline intake remediation: training-wheels WARNING + audit-substrate emission.

Issue #1513 / ADR-0001 "Inline intake remediation posture". When the opt-in
``intake.grooming`` fallback fires at sprint entry, the daemon must (a) emit a
two-line WARNING naming ``forge groom`` as the intended primary path and (b)
write a structured record to the SQLite audit substrate so the
remediation-to-runnable cost ratio is queryable per milestone.
"""

from __future__ import annotations

import logging
import sqlite3
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.config.types import IntakeConfig
from theforge.coordinator.audit_substrate import (
    SubstrateError,
    inline_remediation_rollup_by_milestone,
    iter_inline_remediation_events,
    record_inline_remediation_event,
    substrate_path,
)
from theforge.intake import IntakeOutcome, IntakeOutcomeKind
from theforge.intake.findings import FixType, IntakeFinding, IntakeSeverity
from theforge.sprint.runner import _run_intake_remediation_pass
from theforge.task import TaskStory

# ── Substrate unit coverage ──────────────────────────────────────────────


def test_record_and_iter_inline_remediation_event(tmp_path: Path) -> None:
    event = {
        "issue_id": "1497",
        "sprint_id": "sprint-abc",
        "milestone": "v0.11.0",
        "shape_verdict": "needs_grooming_missing_ac",
        "action": "dropped_shape",
        "succeeded": False,
        "cost_usd": 0.42,
        "duration_seconds": 3.1,
    }
    event_id = record_inline_remediation_event(tmp_path, event)
    assert event_id > 0

    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        events = list(iter_inline_remediation_events(conn))
    finally:
        conn.close()
    assert len(events) == 1
    assert events[0]["issue_id"] == "1497"
    assert events[0]["action"] == "dropped_shape"
    assert events[0]["milestone"] == "v0.11.0"


def test_record_inline_remediation_requires_issue_id_and_action(tmp_path: Path) -> None:
    with pytest.raises(SubstrateError):
        record_inline_remediation_event(tmp_path, {"action": "dropped_shape"})
    with pytest.raises(SubstrateError):
        record_inline_remediation_event(tmp_path, {"issue_id": "1"})


def test_rollup_by_milestone_counts_and_sums_cost(tmp_path: Path) -> None:
    for issue, cost in (("1", 0.10), ("2", 0.25)):
        record_inline_remediation_event(
            tmp_path,
            {
                "issue_id": issue,
                "action": "dropped_shape",
                "succeeded": False,
                "milestone": "v0.11.0",
                "cost_usd": cost,
            },
        )
    record_inline_remediation_event(
        tmp_path,
        {
            "issue_id": "3",
            "action": "remediated",
            "succeeded": True,
            "milestone": "v0.12.0",
            "cost_usd": 1.00,
        },
    )

    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        rollup = inline_remediation_rollup_by_milestone(conn)
        # Raw SQL per the AC (count / total_cost per milestone) also works.
        raw = dict(
            conn.execute(
                "SELECT milestone, COUNT(*) FROM inline_remediation_events GROUP BY milestone"
            ).fetchall()
        )
    finally:
        conn.close()

    assert rollup["v0.11.0"]["count"] == 2
    assert rollup["v0.11.0"]["total_cost_usd"] == pytest.approx(0.35)
    assert rollup["v0.12.0"]["count"] == 1
    assert rollup["v0.12.0"]["total_cost_usd"] == pytest.approx(1.00)
    assert raw["v0.11.0"] == 2


def test_null_cost_contributes_zero(tmp_path: Path) -> None:
    record_inline_remediation_event(
        tmp_path,
        {"issue_id": "9", "action": "dropped_shape", "milestone": "v0.11.0"},
    )
    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        rollup = inline_remediation_rollup_by_milestone(conn)
    finally:
        conn.close()
    assert rollup["v0.11.0"]["total_cost_usd"] == 0.0


# ── Seam coverage: runner pass → WARNING + substrate row ─────────────────


def _dropped_outcome(slug: str) -> IntakeOutcome:
    finding = IntakeFinding(
        code="needs_grooming_missing_ac",
        severity=IntakeSeverity.BLOCK,
        location="acceptance_criteria",
        problem="no acceptance criteria section",
        fix_type=FixType.SEMANTIC,
    )
    return IntakeOutcome(
        slug=slug,
        kind=IntakeOutcomeKind.DROPPED_SHAPE,
        findings=(finding,),
        detail="auto-fix disabled",
        audit={"remediation_source": "none", "agent": {"cost_usd": 0.0}},
    )


def _grooming_config(tmp_path: Path) -> types.SimpleNamespace:
    # _run_intake_remediation_pass only reads config.intake and
    # config.project_root when grooming is enabled and auto_fix is off.
    return types.SimpleNamespace(
        intake=IntakeConfig(grooming=True, auto_fix=False),
        project_root=tmp_path,
    )


def test_grooming_firing_warns_and_records(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _grooming_config(tmp_path)
    task = TaskStory(name="Issue #1497", slug="issue-1497", github_issue=1497)
    logs: list[str] = []

    def _fake_remediation(tasks, project_root, **_kwargs):  # noqa: ARG001
        return {t.slug: _dropped_outcome(t.slug) for t in tasks}

    with (
        patch("theforge.sprint.runner.run_intake_remediation", side_effect=_fake_remediation),
        caplog.at_level(logging.WARNING, logger="theforge.intake"),
    ):
        outcomes = _run_intake_remediation_pass(
            config=config,
            tasks=[task],
            log=logs.append,
            sprint_id="sprint-xyz",
            milestone="v0.11.0",
        )

    assert outcomes["issue-1497"].kind is IntakeOutcomeKind.DROPPED_SHAPE

    # Two-line operator log naming forge groom (matches the ADR example).
    joined = "\n".join(logs)
    assert "Inline intake remediation ran at sprint entry for #1497." in joined
    assert "run `forge groom 1497` before sprint selection." in joined

    # WARNING-level severity carried by the intake logger.
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("forge groom 1497" in m for m in warnings)

    # Structured audit record with the required fields.
    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        events = list(iter_inline_remediation_events(conn, milestone="v0.11.0"))
    finally:
        conn.close()
    assert len(events) == 1
    ev = events[0]
    assert ev["issue_id"] == "1497"
    assert ev["sprint_id"] == "sprint-xyz"
    assert ev["milestone"] == "v0.11.0"
    assert ev["shape_verdict"] == "needs_grooming_missing_ac"
    assert ev["action"] == "dropped_shape"
    assert ev["succeeded"] is False
    assert "duration_seconds" in ev
    assert "cost_usd" in ev


def test_passed_outcome_does_not_warn_or_record(tmp_path: Path) -> None:
    config = _grooming_config(tmp_path)
    task = TaskStory(name="Issue #200", slug="issue-200", github_issue=200)
    logs: list[str] = []

    def _fake_remediation(tasks, project_root, **_kwargs):  # noqa: ARG001
        return {t.slug: IntakeOutcome(slug=t.slug, kind=IntakeOutcomeKind.PASSED) for t in tasks}

    with patch("theforge.sprint.runner.run_intake_remediation", side_effect=_fake_remediation):
        _run_intake_remediation_pass(config=config, tasks=[task], log=logs.append)

    assert not any("forge groom" in line for line in logs)
    assert not substrate_path(tmp_path).exists() or _count_events(tmp_path) == 0


def test_remediated_outcome_records_success(tmp_path: Path) -> None:
    config = _grooming_config(tmp_path)
    task = TaskStory(name="Issue #55", slug="issue-55", github_issue=55)

    def _fake_remediation(tasks, project_root, **_kwargs):  # noqa: ARG001
        return {
            t.slug: IntakeOutcome(
                slug=t.slug,
                kind=IntakeOutcomeKind.REMEDIATED,
                audit={"remediation_source": "agent", "agent": {"cost_usd": 0.9}},
            )
            for t in tasks
        }

    with patch("theforge.sprint.runner.run_intake_remediation", side_effect=_fake_remediation):
        _run_intake_remediation_pass(config=config, tasks=[task], log=lambda _m: None)

    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        events = list(iter_inline_remediation_events(conn))
    finally:
        conn.close()
    assert len(events) == 1
    assert events[0]["action"] == "remediated"
    assert events[0]["succeeded"] is True
    assert events[0]["cost_usd"] == pytest.approx(0.9)


def _count_events(project_root: Path) -> int:
    conn = sqlite3.connect(str(substrate_path(project_root)))
    try:
        return conn.execute("SELECT COUNT(*) FROM inline_remediation_events").fetchone()[0]
    finally:
        conn.close()
