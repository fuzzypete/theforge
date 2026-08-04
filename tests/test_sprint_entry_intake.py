"""Tests for the entry shape-gate -> intake remediation bridge."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from theforge.config import (
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    ModelProfile,
    PlanAgentReviewConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.types import IntakeConfig
from theforge.coordinator.audit_substrate import (
    iter_inline_remediation_events,
    substrate_path,
)
from theforge.intake import IntakeOutcome, IntakeOutcomeKind
from theforge.sprint.entry_intake import remediate_entry_skipped_issues
from theforge.sprint.shape_gate import SkippedIssue


def _make_config(tmp_path: Path, *, intake: IntakeConfig | None = None) -> ForgeConfig:
    cfg = ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=ModelProfile(
            name="dev",
            cli="claude",
            model="sonnet",
            budget_usd=2.0,
            timeout_seconds=300,
            allowed_tools=("Read",),
        ),
        preflight_profile=ModelProfile(
            name="preflight",
            cli="claude",
            model="sonnet",
            budget_usd=0.5,
            timeout_seconds=120,
            allowed_tools=("Read",),
        ),
        review_pool=[],
        synthesis_profile=None,
        retry=RetryPolicy(),
        plan_agent_review=PlanAgentReviewConfig.of(enabled=False),
        log=LogConfig(enabled=False),
    )
    if intake is not None:
        object.__setattr__(cfg, "intake", intake)
    return cfg


def _skipped(num: int, *codes: str, source: str = "local_check") -> SkippedIssue:
    return SkippedIssue(
        issue_number=num,
        reason_codes=tuple(codes),
        source=source,
        title=f"#{num}",
        detail="; ".join(codes),
    )


def test_returns_empty_when_intake_disabled(tmp_path: Path) -> None:
    config = _make_config(tmp_path)  # default intake: both disabled
    outcomes = remediate_entry_skipped_issues(
        [_skipped(1, "implementation_plan_in_body")],
        config=config,
        log=lambda _m: None,
    )
    assert outcomes == {}


def test_returns_empty_when_no_skipped(tmp_path: Path) -> None:
    config = _make_config(
        tmp_path,
        intake=IntakeConfig(grooming=True, auto_fix=True, auto_fix_mode="comment"),
    )
    assert remediate_entry_skipped_issues([], config=config, log=lambda _m: None) == {}


def test_passing_body_with_entry_skip_records_declined(tmp_path: Path) -> None:
    """Entry skip whose category isn't covered by body-only checks lands as
    DROPPED_SHAPE with a written 'declined' detail rather than silence."""
    config = _make_config(
        tmp_path,
        intake=IntakeConfig(grooming=True, auto_fix=False, auto_fix_mode="comment"),
    )

    def fake_pass(*, config, tasks, log, **_kwargs):  # noqa: ARG001
        return {t.slug: IntakeOutcome(slug=t.slug, kind=IntakeOutcomeKind.PASSED) for t in tasks}

    logs: list[str] = []
    with patch("theforge.sprint.runner._run_intake_remediation_pass", side_effect=fake_pass):
        outcomes = remediate_entry_skipped_issues(
            [_skipped(1135, "reopened_stale_contract")],
            config=config,
            log=logs.append,
        )

    assert 1135 in outcomes
    outcome = outcomes[1135]
    assert outcome.kind is IntakeOutcomeKind.DROPPED_SHAPE
    assert "declined" in outcome.detail
    assert "reopened_stale_contract" in outcome.detail
    assert outcome.audit["remediation_source"] == "declined"
    assert outcome.audit["shape_gate_codes"] == ["reopened_stale_contract"]
    # Operator-visible log line ensures non-silence.
    assert any("issue #1135" in line for line in logs)


def test_declined_entry_skip_emits_warning_and_records(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A body-PASSED outcome converted to a declined DROPPED_SHAPE must still
    emit the training-wheels WARNING and write an inline_remediation_events
    row with action='declined' — the pass loop skipped it while PASSED."""
    config = _make_config(
        tmp_path,
        intake=IntakeConfig(grooming=True, auto_fix=False, auto_fix_mode="comment"),
    )

    def fake_pass(*, config, tasks, log, **_kwargs):  # noqa: ARG001
        return {t.slug: IntakeOutcome(slug=t.slug, kind=IntakeOutcomeKind.PASSED) for t in tasks}

    logs: list[str] = []
    with (
        patch("theforge.sprint.runner._run_intake_remediation_pass", side_effect=fake_pass),
        caplog.at_level(logging.WARNING, logger="theforge.intake"),
    ):
        outcomes = remediate_entry_skipped_issues(
            [_skipped(1135, "reopened_stale_contract")],
            config=config,
            log=logs.append,
            sprint_id="sprint-decl",
            milestone="v0.11.0",
        )

    assert outcomes[1135].kind is IntakeOutcomeKind.DROPPED_SHAPE

    joined = "\n".join(logs)
    assert "Inline intake remediation ran at sprint entry for #1135." in joined
    assert "run `forge groom 1135` before sprint selection." in joined
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("forge groom 1135" in m for m in warnings)

    conn = sqlite3.connect(str(substrate_path(tmp_path)))
    try:
        events = list(iter_inline_remediation_events(conn, milestone="v0.11.0"))
    finally:
        conn.close()
    assert len(events) == 1
    ev = events[0]
    assert ev["issue_id"] == "1135"
    assert ev["sprint_id"] == "sprint-decl"
    assert ev["action"] == "declined"
    assert ev["shape_verdict"] == "reopened_stale_contract"
    assert ev["succeeded"] is False


def test_declined_entry_skip_no_emit_when_grooming_disabled(tmp_path: Path) -> None:
    """auto_fix-only runs (grooming off) still record the declined outcome but
    do not emit the grooming training-wheels WARNING or an audit row."""
    config = _make_config(
        tmp_path,
        intake=IntakeConfig(grooming=False, auto_fix=True, auto_fix_mode="comment"),
    )

    def fake_pass(*, config, tasks, log, **_kwargs):  # noqa: ARG001
        return {t.slug: IntakeOutcome(slug=t.slug, kind=IntakeOutcomeKind.PASSED) for t in tasks}

    logs: list[str] = []
    with patch("theforge.sprint.runner._run_intake_remediation_pass", side_effect=fake_pass):
        outcomes = remediate_entry_skipped_issues(
            [_skipped(1136, "reopened_stale_contract")],
            config=config,
            log=logs.append,
        )

    assert outcomes[1136].kind is IntakeOutcomeKind.DROPPED_SHAPE
    assert not any("forge groom" in line for line in logs)
    assert not substrate_path(tmp_path).exists()


def test_remediation_outcome_passes_through(tmp_path: Path) -> None:
    """Non-PASSED remediation outcomes flow back unchanged, keyed by issue#."""
    config = _make_config(
        tmp_path,
        intake=IntakeConfig(grooming=True, auto_fix=True, auto_fix_mode="comment"),
    )

    def fake_pass(*, config, tasks, log, **_kwargs):  # noqa: ARG001
        return {
            tasks[0].slug: IntakeOutcome(
                slug=tasks[0].slug,
                kind=IntakeOutcomeKind.DROPPED_SHAPE,
                detail="comment mode: proposed replacement posted; story dropped",
                audit={"remediation_source": "agent", "comment_posted": True},
            )
        }

    with patch("theforge.sprint.runner._run_intake_remediation_pass", side_effect=fake_pass):
        outcomes = remediate_entry_skipped_issues(
            [_skipped(1014, "implementation_plan_in_body")],
            config=config,
            log=lambda _m: None,
        )

    assert outcomes[1014].kind is IntakeOutcomeKind.DROPPED_SHAPE
    assert outcomes[1014].audit.get("comment_posted") is True
