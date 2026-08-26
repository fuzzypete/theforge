"""Specification-gap gate: pause DEV for an operator answer (#2122).

A dev agent that reaches an underspecified acceptance criterion emits a
``<forge_spec_gap>`` block (parsed by :mod:`theforge.task.spec_gap`) and stops.
This module is what happens next: it opens a pending operator decision at the
moment of ambiguity — before any review cycle is spent — records how that
decision resolved, and persists the resolution durably.

Three things it guarantees, one per acceptance criterion:

* **Bounded.** ``retry.max_spec_gap_pauses`` caps how many pauses a run may
  open. Past the bound, the gap is still recorded and still answered — by the
  agent's own stated assumption — rather than pausing again or failing.
* **Never indefinite, never silent.** An expired pause resolves by the same
  recorded-assumption path as an exhausted allowance and says that no answer was
  given. Every raised gap gets exactly one resolution.
* **Durable.** The resolution is written to the story's resume record *before*
  the pending file is cleaned up, so a crash at the gate boundary cannot destroy
  an answer an operator has already given.

Control flow lives here rather than in ``dev_phase`` (convention 3): the dev
phase asks one question — "did this iteration raise a gap?" — and this module
owns everything the answer implies.
"""

from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING, Any

from . import util as _cu

if TYPE_CHECKING:  # pragma: no cover - typing only
    from theforge.config import ForgeConfig
    from theforge.task import TaskStory

    from . import state as _cs
    from .logging import StructuredLogger

#: Phase label written into the pending file and used for the gate's logs.
SPEC_GAP_PHASE = "SPEC_GAP"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def spec_gap_pauses_remaining(state: "_cs.CoordinatorState", config: "ForgeConfig") -> int:
    """Gap pauses this run may still open. Never negative."""
    allowance = max(0, int(getattr(config.retry, "max_spec_gap_pauses", 0)))
    return max(0, allowance - int(state.spec_gap_pauses_used))


def resolved_gaps_for_prompt(state: "_cs.CoordinatorState") -> list[dict[str, Any]]:
    """Resolutions to render into the next dev/fix prompt (JSON-safe copies)."""
    return [dict(entry) for entry in state.spec_gap_resolutions if isinstance(entry, dict)]


def _render_reason(signal: Any, *, task: "TaskStory", state: "_cs.CoordinatorState") -> str:
    """Operator-facing text for the pending file.

    Presents the criterion, the undefined case, the options the agent weighed,
    and the assumption that takes effect if nobody answers — so the operator can
    decide whether an answer is even needed without opening the run log.
    """
    lines = [
        f"SPECIFICATION GAP — {task.slug} needs one decision to continue.",
        "",
        f"Criterion: {signal.criterion}",
        "",
        f"Undefined case: {signal.undefined_case}",
    ]
    if signal.options_considered:
        lines.extend(["", "Options the dev agent considered:"])
        lines.extend(f"  - {option}" for option in signal.options_considered)
    lines.extend(
        [
            "",
            f"If nobody answers, the run proceeds under: {signal.assumption}",
            "",
            "Answer in your own words — this gate takes free-form text, not a "
            "fixed option. The answer is injected into the dev agent's context "
            "and recorded in the run audit.",
            "",
            f'  forge decide {state.run_id or task.slug} "<what this case should do>"',
        ]
    )
    return "\n".join(lines)


def _record(
    state: "_cs.CoordinatorState",
    signal: Any,
    *,
    source: str,
    answer: str | None,
    decided_at: str | None,
    gated: bool,
    waited_seconds: float | None,
    timeout_seconds: int | None,
    allowance: int,
) -> dict[str, Any]:
    """Append the raise event and its resolution; return the resolution."""
    event = {
        "criterion": signal.criterion,
        "undefined_case": signal.undefined_case,
        "assumption": signal.assumption,
        "options_considered": list(signal.options_considered),
        "iteration": state.dev_iteration,
        "review_cycle": state.review_cycle,
        "raised_at": _now_iso(),
        # False means the allowance was already spent, so no pause was opened.
        # The distinction is the difference between "nobody answered" and
        # "nobody was asked", and both resolve the same way.
        "gated": gated,
        "pauses_used": state.spec_gap_pauses_used,
        "max_pauses": allowance,
    }
    state.spec_gap_events.append(event)

    resolution = {
        "criterion": signal.criterion,
        "undefined_case": signal.undefined_case,
        "assumption": signal.assumption,
        "options_considered": list(signal.options_considered),
        "source": source,
        "answer": answer,
        "decided_at": decided_at,
        "iteration": state.dev_iteration,
        "review_cycle": state.review_cycle,
        "gated": gated,
        "waited_seconds": (round(waited_seconds, 2) if waited_seconds is not None else None),
        "timeout_seconds": timeout_seconds,
        "recorded_at": _now_iso(),
    }
    # Newest answer per gap identity wins, so a criterion re-raised after a
    # timeout does not leave the prompt describing two contradictory outcomes.
    key = (signal.criterion.strip(), signal.undefined_case.strip())
    state.spec_gap_resolutions = [
        entry
        for entry in state.spec_gap_resolutions
        if (
            str(entry.get("criterion") or "").strip(),
            str(entry.get("undefined_case") or "").strip(),
        )
        != key
    ]
    state.spec_gap_resolutions.append(resolution)
    return resolution


def _persist(
    state: "_cs.CoordinatorState",
    config: "ForgeConfig",
    task: "TaskStory",
) -> None:
    """Write the resolution to the story's durable record. Best-effort.

    Keyed on ``state.story_content`` — the story text the run executed — never on
    whatever the caller happens to be holding. A batch-group leader's dev phase
    rebinds its local ``story_content`` to the rendered multi-story spec, and
    saving under that text would stamp the shared record with a hash no other
    save agrees with, discarding every block already on it.
    """
    from .resume_persistence import save_resume_record  # noqa: PLC0415

    project_root = getattr(config, "project_root", None)
    if project_root is None:
        return
    try:
        save_resume_record(
            project_root,
            state,
            slug=task.slug,
            story_content=state.story_content,
            run_id=state.run_id,
        )
    except Exception:  # noqa: BLE001 - a recovery aid must never fail the run
        _cu._log("  ⚠ SPEC_GAP  could not persist the gap resolution to the resume record")


def handle_spec_gap(
    state: "_cs.CoordinatorState",
    config: "ForgeConfig",
    task: "TaskStory",
    agent_output: str | None,
    *,
    logger: "StructuredLogger | None" = None,
) -> bool:
    """Resolve a specification gap raised by the dev iteration just finished.

    Returns True when a gap was raised and the coordinator should re-enter DEV
    with the resolution injected; False when the output carried no usable gap
    signal and the ordinary dev-phase paths should run.

    A malformed block returns False deliberately: a gap the coordinator cannot
    read is not a question it can put to an operator, and stalling the run on an
    unparseable ask would be strictly worse than the guess this channel replaces.
    The parse failure is logged and recorded so it is not silent.
    """
    from theforge.task.spec_gap import (  # noqa: PLC0415
        RESOLUTION_ALLOWANCE_EXHAUSTED,
        RESOLUTION_NO_ANSWER,
        RESOLUTION_OPERATOR,
        SpecGapParseError,
        extract_spec_gap,
    )

    try:
        signal = extract_spec_gap(agent_output or "")
    except SpecGapParseError as exc:
        _cu._log(f"  ⚠ SPEC_GAP  ignoring malformed <forge_spec_gap> block: {exc}")
        state.spec_gap_events.append(
            {
                "iteration": state.dev_iteration,
                "review_cycle": state.review_cycle,
                "raised_at": _now_iso(),
                "gated": False,
                "parse_error": str(exc),
            }
        )
        if logger:
            logger._safe_emit("spec_gap_parse_error", phase="DEV", reason=str(exc))
        return False

    if signal is None:
        return False

    allowance = max(0, int(getattr(config.retry, "max_spec_gap_pauses", 0)))

    _cu._log("─── Specification Gap Raised ───")
    _cu._log(f"  Criterion: {signal.criterion[:160]}")
    _cu._log(f"  Undefined: {signal.undefined_case[:160]}")

    if spec_gap_pauses_remaining(state, config) <= 0:
        # Bounded, not unlimited. The gap is still answered — by the agent's own
        # assumption — and the audit records that nobody was asked.
        _record(
            state,
            signal,
            source=RESOLUTION_ALLOWANCE_EXHAUSTED,
            answer=None,
            decided_at=None,
            gated=False,
            waited_seconds=None,
            timeout_seconds=None,
            allowance=allowance,
        )
        _cu._log(
            f"  Gap-pause allowance exhausted ({state.spec_gap_pauses_used}/{allowance}) "
            f"— proceeding under the recorded assumption: {signal.assumption[:160]}"
        )
        if logger:
            logger._safe_emit(
                "spec_gap_resolved",
                phase="DEV",
                source=RESOLUTION_ALLOWANCE_EXHAUSTED,
                criterion=signal.criterion,
            )
        _persist(state, config, task)
        return True

    # The gate persists its own resolution before removing the pending file;
    # see _open_gate.
    resolution = _open_gate(
        state,
        config,
        task,
        signal,
        allowance=allowance,
        logger=logger,
        operator_source=RESOLUTION_OPERATOR,
        no_answer_source=RESOLUTION_NO_ANSWER,
    )
    if logger:
        logger._safe_emit(
            "spec_gap_resolved",
            phase="DEV",
            source=resolution["source"],
            criterion=signal.criterion,
        )
    return True


def _gate_outcome(
    pending_module: Any,
    run_id: str,
    project_root: Any,
    *,
    polled_decision: str,
    polled_decided_at: str | None,
) -> tuple[bool, str | None, str | None]:
    """Return ``(answered, answer, decided_at)`` for a finished SPEC_GAP poll.

    :func:`theforge.pending.poll_pending` reports an expiry by returning the
    string ``"timeout"``, which is unambiguous for every gate that offers a fixed
    menu — none of them lists ``timeout`` as an option. This gate takes free-form
    text, so ``timeout`` is a perfectly ordinary thing for an operator to write
    ("timeout the request rather than blocking"), and reading the sentinel as an
    expiry would discard a real answer and proceed under the assumption instead.

    What settles it is whether the pending record carries a decision at all, and
    that question has exactly one answer: :func:`theforge.pending.decision_of`,
    the same predicate the poller itself uses. This function must never re-derive
    it. Two earlier attempts here did, each with a slightly different rule, and
    each disagreed with the poller about a different class of real file — first
    the literal answer ``timeout``, then YAML-native scalars like ``yes`` and
    ``42``, which arrive as ``bool``/``int`` and which a string-only test reads
    as silence. Sharing the predicate is what stops that recurring.

    The record is still on disk here (``cleanup_pending`` runs after), so it is
    re-read and believed over the poller's sentinel.

    ``decided_at`` is corroboration, never the discriminator: the pending file is
    deliberately writable by anything (CLI, webhook, a human with an editor, per
    :mod:`theforge.pending`), and a hand-written ``decision:`` may carry no
    timestamp at all. Keying on the timestamp would turn that answer into a
    phantom timeout — the same bug in a new place.

    The sentinel is consulted only when the authoritative record is gone (a
    sweep between the poll and here), where the poller's report is all there is.
    """
    record = pending_module.read_pending(run_id, project_root=project_root) or {}
    answer = pending_module.decision_of(record)
    if answer is not None:
        return True, answer, record.get("decided_at") or polled_decided_at
    if record:
        # The record is readable and carries no decision: the gate expired.
        return False, None, None
    if polled_decided_at is not None or polled_decision != "timeout":
        return True, polled_decision.strip() or None, polled_decided_at
    return False, None, None


def _open_gate(
    state: "_cs.CoordinatorState",
    config: "ForgeConfig",
    task: "TaskStory",
    signal: Any,
    *,
    allowance: int,
    logger: "StructuredLogger | None",
    operator_source: str,
    no_answer_source: str,
) -> dict[str, Any]:
    """Write, notify on, and poll one SPEC_GAP pending decision."""
    from theforge import pending as _pending  # noqa: PLC0415
    from theforge.notify_backends import (  # noqa: PLC0415
        format_pending_decision_notification,
        send_notifications,
    )

    # Same wait the other operator gates use. Bounded before write_pending so
    # the timeout_at an operator reads is the window this poller will honour.
    timeout_seconds = int(
        _pending.bounded_gate_wait(
            config.notifications.human_review_timeout_seconds, SPEC_GAP_PHASE
        )
    )
    _eff_run_id = state.run_id or task.slug
    project_root = getattr(config, "project_root", None)
    reason = _render_reason(signal, task=task, state=state)

    state.spec_gap_pauses_used += 1

    _cu._log(f"  Run ID:  {_eff_run_id}")
    _cu._log(f"  Timeout: {_cu._fmt_duration(timeout_seconds)}")
    _cu._log(f"  Pause:   {state.spec_gap_pauses_used}/{allowance}")

    pending_path = _pending.write_pending(
        run_id=_eff_run_id,
        story=task.slug,
        phase=SPEC_GAP_PHASE,
        reason=reason,
        # Deliberately empty: this gate takes a free-form answer, not a
        # selection. `forge decide` only validates an action against a non-empty
        # options list, so an empty one accepts the operator's own words without
        # loosening validation for the gates that do enumerate choices.
        options=[],
        timeout_seconds=timeout_seconds,
        project_root=project_root,
        extra={
            "decision_required": True,
            "free_form_answer": True,
            "spec_gap": signal.to_dict(),
        },
    )

    pending_record = _pending.read_pending(_eff_run_id, project_root=project_root) or {}
    send_notifications(
        config,
        title=f"TheForge: specification gap — {task.slug} ({SPEC_GAP_PHASE})",
        body=format_pending_decision_notification(pending_record, pending_path=pending_path),
    )

    _poll_start = time.monotonic()
    decision, decided_at = _pending.poll_pending(
        _eff_run_id,
        timeout_seconds,
        project_root=project_root,
        phase_label=SPEC_GAP_PHASE,
        already_bounded=True,
    )
    waited = time.monotonic() - _poll_start
    state.human_review_waited_seconds = (state.human_review_waited_seconds or 0.0) + waited

    # Decided from the record on disk, not from the poller's sentinel string —
    # see _gate_outcome. Must run before cleanup_pending removes the record.
    answered, answer, answered_at = _gate_outcome(
        _pending,
        _eff_run_id,
        project_root,
        polled_decision=decision,
        polled_decided_at=decided_at,
    )
    resolution = _record(
        state,
        signal,
        source=operator_source if answered else no_answer_source,
        answer=answer,
        decided_at=answered_at,
        gated=True,
        waited_seconds=waited,
        timeout_seconds=timeout_seconds,
        allowance=allowance,
    )
    # Durable BEFORE the checkpoint is removed. cleanup_pending is the point of
    # no return for the operator's answer: until the resolution is on disk, the
    # pending file is the only copy of it that survives this process, so a crash
    # between the two would lose an answer a human already gave with nothing left
    # to show it was ever asked.
    _persist(state, config, task)
    _pending.cleanup_pending(_eff_run_id, project_root)

    if answered:
        _cu._log(
            f"  ✓ SPEC_GAP  operator answered after {_cu._fmt_duration(waited)}: "
            f"{(answer or '')[:160]}"
        )
    else:
        _cu._log(
            f"  ✗ SPEC_GAP  no answer after {_cu._fmt_duration(waited)} — proceeding "
            f"under the recorded assumption: {signal.assumption[:160]}"
        )
    return resolution
