"""Bridge: run intake remediation on issues skipped by the sprint-entry shape gate.

The entry shape gate at ``cli/sprint.py`` filters issues before ``run_sprint``
is invoked. Without this bridge, those skipped issues bypass the intake
remediation pass that lives inside ``run_sprint``, defeating the operator-
visible contract that remediation must run in response to every shape-gate
rejection.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..intake import IntakeOutcome, IntakeOutcomeKind
from ..task import TaskStory
from .shape_gate import SkippedIssue

if TYPE_CHECKING:
    from ..config import ForgeConfig


def remediate_entry_skipped_issues(
    skipped_issues: list[SkippedIssue],
    *,
    config: "ForgeConfig",
    log: Callable[[str], None],
    sprint_id: str | None = None,
    milestone: str | None = None,
) -> dict[int, IntakeOutcome]:
    """Run the intake remediation gate over entry-skipped issues.

    Returns a mapping of ``issue_number`` → :class:`IntakeOutcome`. When intake
    is fully disabled (both ``grooming`` and ``auto_fix`` False), returns an
    empty dict so callers behave exactly as before.

    Outcomes whose ``kind`` is ``PASSED`` despite a non-empty entry-skip
    reason set are converted into ``DROPPED_SHAPE`` with an explicit
    ``intake remediation declined`` detail. The body-only intake checks
    can't see entry-skip categories like ``reopened_stale_contract`` that
    depend on the issue timeline; recording the decline gives the operator
    a written reason rather than silence.
    """
    if not skipped_issues:
        return {}

    intake_cfg = getattr(config, "intake", None)
    grooming_enabled = bool(getattr(intake_cfg, "grooming", False))
    auto_fix_enabled = bool(getattr(intake_cfg, "auto_fix", False))
    if not grooming_enabled and not auto_fix_enabled:
        return {}

    # Lazy import: runner.py is heavy and pulls in coordinator-side code.
    from .runner import _run_intake_remediation_pass

    tasks: list[TaskStory] = []
    slug_to_number: dict[str, int] = {}
    for sk in skipped_issues:
        slug = f"issue-{sk.issue_number}"
        tasks.append(
            TaskStory(
                name=f"Issue #{sk.issue_number}",
                slug=slug,
                github_issue=sk.issue_number,
            )
        )
        slug_to_number[slug] = sk.issue_number

    slug_outcomes = _run_intake_remediation_pass(
        config=config,
        tasks=tasks,
        log=log,
        sprint_id=sprint_id,
        milestone=milestone,
    )

    outcomes: dict[int, IntakeOutcome] = {}
    skip_by_number = {sk.issue_number: sk for sk in skipped_issues}
    for slug, outcome in slug_outcomes.items():
        issue_num = slug_to_number.get(slug)
        if issue_num is None:
            continue
        sk = skip_by_number.get(issue_num)
        if outcome.kind is IntakeOutcomeKind.PASSED and sk is not None and sk.reason_codes:
            codes = ", ".join(sk.reason_codes)
            outcome = IntakeOutcome(
                slug=slug,
                kind=IntakeOutcomeKind.DROPPED_SHAPE,
                findings=outcome.findings,
                detail=(
                    f"intake remediation declined: shape-gate skip reason "
                    f"'{codes}' is not covered by body-only intake checks"
                ),
                audit={
                    "remediation_source": "declined",
                    "shape_gate_codes": list(sk.reason_codes),
                    "shape_gate_source": sk.source,
                    "shape_gate_detail": sk.detail,
                },
            )
            # The pass loop only fires on non-PASSED outcomes, so it skipped
            # this issue while the body check was still PASSED. Now that the
            # bridge has converted it to a declined DROPPED_SHAPE, emit the
            # training-wheels WARNING + audit row here so the decline is not
            # silent. Gated on grooming (the opt-in fallback this posture is
            # about); auto_fix-only runs never emit the grooming memo.
            if grooming_enabled:
                from .runner import emit_inline_remediation_event  # noqa: PLC0415

                emit_inline_remediation_event(
                    config=config,
                    issue=issue_num,
                    slug=slug,
                    outcome=outcome,
                    log=log,
                    sprint_id=sprint_id,
                    milestone=milestone,
                    duration_seconds=0.0,
                )
        outcomes[issue_num] = outcome
        log(
            f"Entry shape-gate skip remediation: issue #{issue_num} "
            f"-> {outcome.kind.value}" + (f" ({outcome.detail})" if outcome.detail else "")
        )
    return outcomes
