"""Preflight complexity gate: ask before spending on an over-broad story (#2681).

Preflight already sizes a story for cents. Everything after it — planning, plan
review, dev, review — is where the money goes, and until this gate existed the
score bought only accommodation: longer timeouts, stronger models. It never
prompted a question. A story that scored at the top of the scale was planned at
full cost and only afterwards judged, by hand, to have been four stories.

This module is the question. At the end of PREFLIGHT, a PROCEED verdict whose
complexity score reaches ``retry.preflight_complexity_gate_threshold`` pauses and
offers the operator two actions:

* ``approve``   — plan and implement it as scoped. The run continues exactly as
  it would have without the gate, and the approval is recorded at that score, so
  a large-but-cohesive story is never blocked by its size alone.
* ``decompose`` — return it to be split. The run ends before any later phase is
  charged, reported as *returned for decomposition* rather than as a failure.

Three properties, one per acceptance criterion:

* **Anchored to the phase boundary, not to a phase.** The gate is called from
  ``_handle_preflight_verdict``, the one handoff every preflight path passes
  through — fresh, cached, and resumed alike. A story preflight classified
  ``implementation_ready`` skips PLAN entirely and still stops here.
* **Active by default, in both directions.** There is no enable switch. A
  threshold above the highest score preflight can assign
  (``COMPLEXITY_SCORE_MAX``) disables the gate; the shipped threshold opens it.
* **Fail-closed on silence.** An expiry takes ``retry.
  preflight_complexity_gate_no_decision``, which accepts only the two actions an
  operator may choose. Absent, empty, or unrecognised configuration resolves to
  ``decompose``, and the run records both which action it applied and that no
  operator decision was recorded.

**Which axis the threshold reads.** The comparison is against the *projected*
``complexity_score`` — the ``max(implementation, validation)`` figure that
routing, timeouts, and review budgets already consume — because this gate is a
question about *cost*, and cost tracks the projected score. That is deliberately
a different axis from #2680's ``scope_exceeded``, which reads the implementation
axis at its ceiling because it makes a claim about *divisibility*. A
validation-heavy story can be expensive without being splittable, which is
exactly the story an operator should get to approve rather than have refused.
Both axes are written into the pending record and the audit so the operator sees
which one carried the story over the line. When the score came from a degraded
or nothing-examined preflight, that provenance is surfaced as context for the
decision rather than suppressing the question.

Control flow lives here rather than in ``preflight_flow`` (convention 3): the
verdict handoff asks one question — "does this story need a scope decision?" —
and this module owns everything the answer implies.
"""

from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING, Any

from theforge.config.types import (
    PREFLIGHT_GATE_ACTIONS,
    PREFLIGHT_GATE_APPROVE,
    PREFLIGHT_GATE_DECOMPOSE,
    normalize_preflight_gate_no_decision,
)

from . import util as _cu
from .preflight import COMPLEXITY_SCORE_MAX, complexity_is_founded
from .state import CoordinatorResult, Phase

if TYPE_CHECKING:  # pragma: no cover - typing only
    from theforge.config import ForgeConfig
    from theforge.task import TaskStory

    from . import preflight_decomposition_flow as _pdf
    from . import state as _cs
    from .logging import StructuredLogger

#: Phase label written into the pending file and used for the gate's logs. The
#: gate is anchored to the end of PREFLIGHT, so that is what an operator reading
#: ``forge status`` is told the story is held at.
PREFLIGHT_GATE_PHASE = "PREFLIGHT"

#: ``extra`` key that marks a pending record as this gate's, so the status and
#: notification surfaces can render the scores rather than only the prose.
PREFLIGHT_GATE_EXTRA_KEY = "preflight_complexity_gate"

#: Values of ``preflight_complexity_gate_decision_source``.
DECISION_SOURCE_OPERATOR = "operator"
DECISION_SOURCE_NO_DECISION = "no_decision"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def gate_threshold(config: "ForgeConfig") -> int:
    """The configured score at which the gate opens."""
    from theforge.config.types import DEFAULT_PREFLIGHT_COMPLEXITY_GATE_THRESHOLD  # noqa: PLC0415

    raw = getattr(
        config.retry,
        "preflight_complexity_gate_threshold",
        DEFAULT_PREFLIGHT_COMPLEXITY_GATE_THRESHOLD,
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        # An unreadable threshold must not silently disable the gate: fall back
        # to the shipped one rather than to "never ask".
        return int(DEFAULT_PREFLIGHT_COMPLEXITY_GATE_THRESHOLD)


def gate_is_disabled(threshold: int) -> bool:
    """True when the threshold is above any score preflight can assign."""
    return threshold > COMPLEXITY_SCORE_MAX


def should_gate(state: "_cs.CoordinatorState", config: "ForgeConfig", verdict: str) -> bool:
    """Whether this story's preflight result warrants a scope decision.

    Every PROCEED verdict whose projected preflight complexity reaches the
    configured threshold must pause here. Foundedness still matters for the
    operator-facing context and the audit, but not for the trigger.
    """
    if verdict != "PROCEED":
        return False
    score = state.preflight_complexity_score
    if not isinstance(score, int):
        return False
    threshold = gate_threshold(config)
    if gate_is_disabled(threshold):
        return False
    return score >= threshold


def _score_provenance_note(state: "_cs.CoordinatorState") -> str | None:
    """Explain when a gating score came from a conservative preflight path."""
    if complexity_is_founded(state):
        return None
    if getattr(state, "preflight_degraded", False):
        reason = str(getattr(state, "preflight_degraded_reason", "") or "").strip()
        if reason:
            return f"degraded preflight ({reason})"
        return "degraded preflight"
    return "preflight examined no criteria"


def _resolve_no_decision(config: "ForgeConfig") -> tuple[str, str | None]:
    """The action an expiry applies, and why a fallback was needed if it was."""
    return normalize_preflight_gate_no_decision(
        getattr(config.retry, "preflight_complexity_gate_no_decision", None)
    )


def _assessment_lines(state: "_cs.CoordinatorState") -> list[str]:
    """The decomposition-assessment section of the pause, or its recorded absence.

    Both branches are content. A story with no assessment gets an explicit
    statement of that fact and why, because "nothing here" and "no assessment
    was produced because the step judged it atomic" are different things to read
    while deciding — and neither one changes what the pause accepts as an answer.
    """
    from theforge.decomposition_assessment import (  # noqa: PLC0415
        NONE_NOT_ATTEMPTED,
        CandidateSlice,
        DecompositionAssessment,
        render_assessment_lines,
    )

    payload = state.preflight_complexity_gate_assessment
    if state.preflight_complexity_gate_assessment_generated and isinstance(payload, dict):
        assessment = DecompositionAssessment(
            slices=tuple(
                CandidateSlice(
                    slice_id=int(entry.get("id", index + 1)),
                    title=str(entry.get("title", "")),
                    scope=str(entry.get("scope", "")),
                    depends_on=tuple(entry.get("depends_on") or ()),
                    covers_criteria=tuple(entry.get("covers_criteria") or ()),
                )
                for index, entry in enumerate(payload.get("slices") or [])
            ),
            unsettled=tuple(payload.get("unsettled") or ()),
        )
        return ["", *render_assessment_lines(assessment)]
    reason = state.preflight_complexity_gate_assessment_none_reason or NONE_NOT_ATTEMPTED
    return ["", f"No decomposition assessment: {reason}."]


def _render_reason(
    *,
    task: "TaskStory",
    state: "_cs.CoordinatorState",
    run_id: str,
    threshold: int,
    timeout_seconds: int,
    no_decision_action: str,
    no_decision_fallback: str | None,
) -> str:
    """Operator-facing text for the pending file.

    Presents the same three things the decision turns on: how big preflight
    judged the story on each axis, that nothing past preflight has been spent on
    it yet, and the two commands that resolve it.
    """
    impl = state.preflight_implementation_complexity_score
    validation = state.preflight_validation_complexity_score
    axes = []
    if impl is not None:
        axes.append(f"impl {impl}")
    if validation is not None:
        axes.append(f"validation {validation}")
    axis_text = f" ({', '.join(axes)})" if axes else ""

    lines = [
        f"PREFLIGHT complexity {state.preflight_complexity_score}{axis_text}"
        f" — {task.slug} needs a scope decision before anything further is spent.",
        "",
        f"Threshold {threshold}. Nothing has been spent beyond preflight for this story.",
    ]
    note = _score_provenance_note(state)
    if note is not None:
        lines.extend(["", f"Score provenance: {note}."])
    lines.extend(
        [
            "",
            f"  forge decide {run_id} {PREFLIGHT_GATE_APPROVE}"
            "      plan and implement it as scoped",
            f"  forge decide {run_id} {PREFLIGHT_GATE_DECOMPOSE}    return it to be split",
            "",
            f"No decision within {_cu._fmt_duration(timeout_seconds)}: {no_decision_action}"
            f"{' (default)' if no_decision_fallback is None else ''}."
            " Other stories continue meanwhile.",
        ]
    )
    if no_decision_fallback is not None:
        lines.append(
            f"  (retry.preflight_complexity_gate_no_decision {no_decision_fallback}; "
            f"falling back to {PREFLIGHT_GATE_DECOMPOSE})"
        )
    if state.preflight_scope_exceeded:
        lines.extend(
            [
                "",
                "Preflight also flagged scope_exceeded: its implementation axis is "
                "at the ceiling, so it judged this story over what one story "
                "should attempt.",
            ]
        )
    lines.extend(_assessment_lines(state))
    return "\n".join(lines)


def _gate_payload(
    state: "_cs.CoordinatorState",
    *,
    threshold: int,
    no_decision_action: str,
    no_decision_fallback: str | None,
) -> dict[str, Any]:
    """Machine-readable gate context carried on the pending record."""
    return {
        "complexity_score": state.preflight_complexity_score,
        "implementation_complexity_score": state.preflight_implementation_complexity_score,
        "validation_complexity_score": state.preflight_validation_complexity_score,
        "threshold": threshold,
        "score_founded": complexity_is_founded(state),
        "score_provenance_note": _score_provenance_note(state),
        "scope_exceeded": bool(state.preflight_scope_exceeded),
        "no_decision_action": no_decision_action,
        "no_decision_fallback": no_decision_fallback,
        "default_action": PREFLIGHT_GATE_DECOMPOSE,
        # The assessment as data, next to the same thing rendered as prose in
        # ``reason``. Status and notification surfaces read this rather than
        # parsing the text, and a run with no assessment carries the recorded
        # reason here for exactly the same reason (#2686).
        "assessment": state.preflight_complexity_gate_assessment,
        "assessment_generated": bool(state.preflight_complexity_gate_assessment_generated),
        "assessment_none_reason": state.preflight_complexity_gate_assessment_none_reason,
        "assessment_cost_usd": state.preflight_complexity_gate_assessment_cost_usd,
    }


def _has_recorded_assessment(state: "_cs.CoordinatorState") -> bool:
    """Whether an assessment attempt for this story has already been recorded.

    True for a produced artifact AND for a recorded absence: "the step ran and
    found nothing to split" is an outcome that was paid for too, and re-running
    it on resume would spend again to reach the answer already on the record.
    """
    return bool(
        state.preflight_complexity_gate_assessment_generated
        or state.preflight_complexity_gate_assessment_none_reason
    )


def _record_assessment(state: "_cs.CoordinatorState", attempt: "_pdf.AssessmentAttempt") -> None:
    """Write one assessment attempt — artifact or recorded absence — onto the state."""
    result = attempt.result
    state.preflight_complexity_gate_assessment = (
        result.assessment.to_dict() if result.assessment is not None else None
    )
    state.preflight_complexity_gate_assessment_generated = result.produced
    state.preflight_complexity_gate_assessment_none_reason = (
        None if result.produced else result.none_produced_reason
    )
    state.preflight_complexity_gate_assessment_errors = list(result.validation_errors)
    state.preflight_complexity_gate_assessment_invoked = attempt.invoked
    state.preflight_complexity_gate_assessment_cost_usd = attempt.cost_usd
    state.preflight_complexity_gate_assessment_cost_provenance = attempt.cost_provenance
    state.preflight_complexity_gate_assessment_duration_s = attempt.duration_s
    state.preflight_complexity_gate_assessment_model = attempt.model
    state.preflight_complexity_gate_assessment_profile = attempt.profile_name


def _persist_gate_state(
    state: "_cs.CoordinatorState",
    config: "ForgeConfig",
    task: "TaskStory",
    *,
    logger: "StructuredLogger | None",
) -> None:
    """Durably record what the gate has established so far.

    Called twice: once with the assessment in hand and the operator not yet
    asked, and again once they have answered. The preflight phase already saved
    its record *before* this gate ran, so without these an interruption at the
    pause — or a terminal decompose — would lose an artifact that was paid for,
    and a resumed run would produce it again.
    """
    from .resume_persistence import save_resume_record  # noqa: PLC0415

    try:
        save_resume_record(
            config.project_root,
            state,
            slug=task.slug,
            story_content=state.story_content,
            run_id=getattr(logger, "_run_id", None) or state.run_id,
        )
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort, never a gate
        _cu._log_verbose(f"  preflight gate resume-record save failed: {exc}")


def _record(
    state: "_cs.CoordinatorState",
    *,
    decision: str,
    source: str,
    threshold: int,
    opened: bool,
    no_decision_fallback: str | None,
    waited_seconds: float | None,
    decided_at: str | None,
) -> None:
    """Write the gate's outcome onto the run state."""
    state.preflight_complexity_gate_opened = opened
    state.preflight_complexity_gate_score = state.preflight_complexity_score
    state.preflight_complexity_gate_implementation_score = (
        state.preflight_implementation_complexity_score
    )
    state.preflight_complexity_gate_validation_score = state.preflight_validation_complexity_score
    state.preflight_complexity_gate_threshold = threshold
    state.preflight_complexity_gate_decision = decision
    state.preflight_complexity_gate_decision_source = source
    state.preflight_complexity_gate_no_decision_fallback = no_decision_fallback
    state.preflight_complexity_gate_waited_seconds = (
        round(waited_seconds, 2) if waited_seconds is not None else None
    )
    state.preflight_complexity_gate_decided_at = decided_at or _now_iso()
    # The provenance the operator was shown alongside the score. Recorded rather
    # than recomputed at read time so the audit says what they actually ruled on.
    state.preflight_complexity_gate_score_provenance = _score_provenance_note(state)


def _decompose_result(
    state: "_cs.CoordinatorState",
    task: "TaskStory",
    *,
    source: str,
) -> CoordinatorResult:
    """The terminal result for a story returned to be split.

    ``success`` is False because the story's work was not delivered, but the
    phase stays PREFLIGHT and no escalation is raised: nothing about this story
    failed, and the sprint layer branches on the recorded decision to report it
    as returned for decomposition rather than as a story that could not be made
    to work.
    """
    state.phase = Phase.PREFLIGHT
    if source == DECISION_SOURCE_OPERATOR:
        how = "the operator returned it to be split"
    else:
        how = "no operator decision was recorded, so the configured no-decision action was applied"
    message = (
        f"Returned for decomposition: preflight complexity "
        f"{state.preflight_complexity_gate_score} reached the gate threshold "
        f"{state.preflight_complexity_gate_threshold} and {how}."
    )
    return CoordinatorResult(
        success=False,
        phase=Phase.PREFLIGHT,
        state=state,
        message=f"{message} Nothing was spent past PREFLIGHT on {task.slug}.",
    )


def evaluate_preflight_complexity_gate(
    state: "_cs.CoordinatorState",
    config: "ForgeConfig",
    task: "TaskStory",
    verdict: str,
    *,
    logger: "StructuredLogger | None" = None,
) -> CoordinatorResult | None:
    """Put the scope decision to the operator when the score warrants it.

    Returns None when the run should continue — the gate did not apply, or the
    story was approved — and a terminal :class:`CoordinatorResult` when it was
    returned for decomposition.
    """
    if not should_gate(state, config, verdict):
        return None

    threshold = gate_threshold(config)
    no_decision_action, no_decision_fallback = _resolve_no_decision(config)

    # A decision already on this state came off the resume record: the operator
    # answered this question for this story text once, and re-asking would spend
    # their attention to reach the answer already recorded.
    prior = state.preflight_complexity_gate_decision
    if prior in PREFLIGHT_GATE_ACTIONS:
        _cu._log(
            f"  ↺ PREFLIGHT gate  honouring the recorded decision {prior!r} "
            f"(complexity {state.preflight_complexity_score} ≥ {threshold})"
        )
        if prior == PREFLIGHT_GATE_DECOMPOSE:
            return _decompose_result(
                state,
                task,
                source=state.preflight_complexity_gate_decision_source
                or DECISION_SOURCE_NO_DECISION,
            )
        return None

    decision, source, waited, decided_at = _open_gate(
        state,
        config,
        task,
        threshold=threshold,
        no_decision_action=no_decision_action,
        no_decision_fallback=no_decision_fallback,
        logger=logger,
    )
    _record(
        state,
        decision=decision,
        source=source,
        threshold=threshold,
        opened=True,
        no_decision_fallback=no_decision_fallback,
        waited_seconds=waited,
        decided_at=decided_at,
    )
    # The operator's disposition of the assessment, durable before the decompose
    # path returns terminally below.
    _persist_gate_state(state, config, task, logger=logger)
    if logger:
        logger._safe_emit(
            "preflight_complexity_gate",
            phase=PREFLIGHT_GATE_PHASE,
            decision=decision,
            source=source,
            complexity_score=state.preflight_complexity_score,
            threshold=threshold,
        )

    if decision == PREFLIGHT_GATE_APPROVE:
        _cu._log(
            f"  ✓ PREFLIGHT gate  approved at complexity "
            f"{state.preflight_complexity_score} "
            f"({'operator' if source == DECISION_SOURCE_OPERATOR else 'no decision'}) "
            "— continuing as scoped"
        )
        return None

    _cu._log(
        f"  ⤺ PREFLIGHT gate  returned for decomposition at complexity "
        f"{state.preflight_complexity_score} "
        f"({'operator' if source == DECISION_SOURCE_OPERATOR else 'no decision'}) "
        "— nothing spent past PREFLIGHT"
    )
    return _decompose_result(state, task, source=source)


def _open_gate(
    state: "_cs.CoordinatorState",
    config: "ForgeConfig",
    task: "TaskStory",
    *,
    threshold: int,
    no_decision_action: str,
    no_decision_fallback: str | None,
    logger: "StructuredLogger | None" = None,
) -> tuple[str, str, float, str | None]:
    """Write, notify on, and poll one PREFLIGHT scope decision.

    Returns ``(decision, source, waited_seconds, decided_at)``.
    """
    from theforge import pending as _pending  # noqa: PLC0415
    from theforge.notify_backends import (  # noqa: PLC0415
        format_pending_decision_notification,
        send_notifications,
    )

    # Bounded before write_pending so the timeout_at an operator reads is the
    # window this poller will honour — the same discipline the other gates keep.
    timeout_seconds = int(
        _pending.bounded_gate_wait(
            config.notifications.human_review_timeout_seconds, PREFLIGHT_GATE_PHASE
        )
    )
    # Keyed by the STORY's run id, which is what `forge status` reports for the
    # story that is held — not the sprint's, which no `forge decide` would find.
    eff_run_id = state.run_id or task.slug
    project_root = getattr(config, "project_root", None)

    _cu._log("─── Preflight Complexity Gate ───")
    _cu._log(
        f"  Complexity: {state.preflight_complexity_score} "
        f"(impl {state.preflight_implementation_complexity_score}, "
        f"validation {state.preflight_validation_complexity_score}) "
        f"≥ threshold {threshold}"
    )
    _cu._log(f"  Run ID:  {eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)} → {no_decision_action}")
    if no_decision_fallback is not None:
        _cu._log(
            f"  ⚠ retry.preflight_complexity_gate_no_decision {no_decision_fallback}; "
            f"applying {PREFLIGHT_GATE_DECOMPOSE} on expiry"
        )

    # One assessment attempt, here: after the score has already opened the gate
    # (so nothing is spent on a story that was never going to pause) and before
    # the operator is asked (so the artifact is on the pause they read rather
    # than arriving after they answered). Bounded well inside the wait window —
    # the notification below is what a hung assessment would otherwise delay.
    #
    # A resume that already carries one — an attempt interrupted while the pause
    # was open — re-asks the question but does not re-pay for the artifact.
    if _has_recorded_assessment(state):
        _cu._log(
            "  ↺ PREFLIGHT gate  reusing the recorded decomposition assessment "
            "(not regenerated on resume)"
        )
    else:
        from .preflight_decomposition_flow import (  # noqa: PLC0415
            generate_decomposition_assessment,
        )

        _record_assessment(
            state,
            generate_decomposition_assessment(
                state,
                config,
                task,
                gate_wait_seconds=timeout_seconds,
                score_provenance_note=_score_provenance_note(state),
            ),
        )
        # Durable before the operator is asked: an assessment was paid for, and
        # an interruption at the pause must not lose it or make a resume pay
        # again.
        _persist_gate_state(state, config, task, logger=logger)

    reason = _render_reason(
        task=task,
        state=state,
        run_id=eff_run_id,
        threshold=threshold,
        timeout_seconds=timeout_seconds,
        no_decision_action=no_decision_action,
        no_decision_fallback=no_decision_fallback,
    )
    pending_path = _pending.write_pending(
        run_id=eff_run_id,
        story=task.slug,
        phase=PREFLIGHT_GATE_PHASE,
        reason=reason,
        options=list(PREFLIGHT_GATE_ACTIONS),
        timeout_seconds=timeout_seconds,
        project_root=project_root,
        extra={
            "decision_required": True,
            PREFLIGHT_GATE_EXTRA_KEY: _gate_payload(
                state,
                threshold=threshold,
                no_decision_action=no_decision_action,
                no_decision_fallback=no_decision_fallback,
            ),
        },
    )

    pending_record = _pending.read_pending(eff_run_id, project_root=project_root) or {}
    send_notifications(
        config,
        title=(
            f"TheForge: scope decision — {task.slug} "
            f"(complexity {state.preflight_complexity_score})"
        ),
        body=format_pending_decision_notification(pending_record, pending_path=pending_path),
    )

    poll_start = time.monotonic()
    decision, decided_at = _pending.poll_pending(
        eff_run_id,
        timeout_seconds,
        project_root=project_root,
        phase_label=PREFLIGHT_GATE_PHASE,
        already_bounded=True,
    )
    waited = time.monotonic() - poll_start
    state.human_review_waited_seconds = (state.human_review_waited_seconds or 0.0) + waited

    # Read the record rather than trusting the poller's sentinel, and read it
    # before cleanup removes it. This gate enumerates its options, so `forge
    # decide` already refuses anything outside them; an answer that is somehow
    # neither is treated as no decision rather than guessed at.
    record = _pending.read_pending(eff_run_id, project_root=project_root) or {}
    answer = _pending.decision_of(record)
    if answer is None and not record and decision in PREFLIGHT_GATE_ACTIONS:
        # The record was swept between the poll and here; the poller's report is
        # all there is, and it names one of the two actions.
        answer = decision
        answer_at = decided_at
    else:
        answer_at = record.get("decided_at") or decided_at

    _pending.cleanup_pending(eff_run_id, project_root)

    normalized = str(answer).strip().lower() if answer is not None else None
    if normalized in PREFLIGHT_GATE_ACTIONS:
        return normalized, DECISION_SOURCE_OPERATOR, waited, answer_at
    if normalized is not None:
        _cu._log(
            f"  ⚠ PREFLIGHT gate  unrecognised decision {normalized!r} — applying "
            f"the no-decision action {no_decision_action}"
        )
    return no_decision_action, DECISION_SOURCE_NO_DECISION, waited, None
