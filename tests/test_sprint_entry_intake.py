"""Tests for the entry shape-gate -> intake remediation bridge."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
        plan_agent_review=PlanAgentReviewConfig(enabled=False),
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
