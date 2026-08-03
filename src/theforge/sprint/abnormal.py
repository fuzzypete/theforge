"""Attempt-scoped records for stories that ended abnormally.

A story normally ends by handing its worker's ``CoordinatorResult`` back to the
scheduler, and that result is what the audit trail is written from. Three exits
never produce one:

* the launch guard drops the story before it is ever dispatched,
* the worker thread raises,
* the worker thread crosses its deadline and is cancelled.

Those are precisely the runs with the least recoverable context, so leaving them
without a record makes the least diagnosable failures the only undiagnosable
ones (#2030). This module holds the shared vocabulary for the synthetic record
the scheduler writes instead: what kind of abnormal termination it was, what the
primary cause was, and which attempt it belonged to.

Attempt scoping is the second half. A resume rewrites the sprint's accumulated
story state, so a later attempt at the same story used to replace the only
recorded cause of the earlier one — the record an operator files the bug from
was destroyed by the command run to investigate it. ``accumulate_failure_history``
keeps every attempt's cause instead of the last one's.

Pure data: stdlib only, no forge imports, so every layer (runner, sprint audit,
story state) can depend on it.
"""

from __future__ import annotations

import datetime

# ── Abnormal-termination kinds ────────────────────────────────────────
#
# The kind answers "who ended this story", which the error text alone does not:
# a drop reason and an exception message are both prose, and an operator sorting
# failures needs to separate a story that never ran from one that died running.

ABNORMAL_LAUNCH_GUARD_DROP = "launch_guard_drop"
ABNORMAL_WORKER_EXCEPTION = "worker_exception"
ABNORMAL_WORKER_TIMEOUT = "worker_timeout"
#: A resource every story of the sprint shares failed under this story (#2107).
#: Kept distinct from ``worker_exception`` because the two carry opposite
#: attributions: one is about the story, the other about the substrate.
ABNORMAL_SHARED_INFRASTRUCTURE = "shared_infrastructure_failure"

ABNORMAL_KINDS = frozenset(
    {
        ABNORMAL_LAUNCH_GUARD_DROP,
        ABNORMAL_WORKER_EXCEPTION,
        ABNORMAL_WORKER_TIMEOUT,
        ABNORMAL_SHARED_INFRASTRUCTURE,
    }
)

#: Outcomes whose recorded ``error`` is a failure cause worth retaining across
#: attempts. Deliberately string-keyed so this module stays import-free.
_FAILED_OUTCOMES = frozenset(
    {
        "FAILED",
        "DROPPED",
        "ESCALATED",
        "MERGE_FAILED",
        "MERGE_ARMING_FAILED",
        "TIMEOUT",
    }
)


def build_abnormal_cause(
    *,
    kind: str,
    cause: str,
    error_type: str | None = None,
    phase: str | None = None,
    run_id: str | None = None,
    source: str | None = None,
    recorded_at: str | None = None,
) -> dict:
    """Build one attempt's abnormal-termination record.

    ``cause`` is the primary evidence: the launch guard's drop reason, the
    exception's text, the timeout's budget. ``source`` names where that evidence
    came from (the code path that observed it) so a reader can tell a captured
    fact from a reconstruction.
    """
    return {
        "kind": kind,
        "cause": cause,
        "error_type": error_type,
        "phase": phase,
        "run_id": run_id,
        "source": source,
        "recorded_at": (recorded_at or datetime.datetime.now(datetime.timezone.utc).isoformat()),
    }


def derive_failure_cause(entry: dict | None) -> dict | None:
    """Recover a cause record from a persisted story entry, or None.

    Entries written before this module existed (and ordinary failures that were
    never routed through :func:`build_abnormal_cause`) carry their cause in
    ``error`` / ``error_type``. Deriving from those is what lets a resume retain
    a prior generation's recorded cause instead of dropping it for lack of shape.
    """
    if not isinstance(entry, dict):
        return None
    error = entry.get("error")
    if not isinstance(error, str) or not error.strip():
        return None
    outcome = str(entry.get("outcome") or "").upper()
    if outcome and outcome not in _FAILED_OUTCOMES:
        return None

    def _text(key: str) -> str | None:
        value = entry.get(key)
        return value if isinstance(value, str) else None

    return {
        "kind": entry.get("abnormal_kind"),
        "cause": error,
        "error_type": _text("error_type"),
        "phase": _text("phase"),
        "run_id": _text("story_run_id"),
        "source": "story_state_entry",
        "recorded_at": _text("finished_at"),
    }


def _cause_identity(cause: dict) -> tuple:
    """Identity used to recognise a cause already retained for an attempt."""
    return (cause.get("run_id"), cause.get("kind"), cause.get("cause"))


def accumulate_failure_history(
    prior_entry: dict | None,
    current_entry: dict | None,
) -> list[dict]:
    """Return the attempt-scoped failure history for a story across two attempts.

    The prior attempt's causes always survive; the current attempt's cause is
    appended, never substituted. ``attempt`` is renumbered from position so the
    ordering is readable without cross-referencing timestamps.
    """
    history: list[dict] = []
    seen: set[tuple] = set()

    def _extend(source: dict | None) -> None:
        if not isinstance(source, dict):
            return
        candidates: list[dict] = []
        raw = source.get("failure_history")
        if isinstance(raw, list):
            candidates.extend(item for item in raw if isinstance(item, dict))
        if not candidates:
            # Derivation is the fallback for an entry that records its cause only
            # as ``error`` prose. An entry that already carries structured causes
            # has said everything it knows; deriving as well would record the same
            # failure twice under two different run ids.
            derived = derive_failure_cause(source)
            if derived is not None:
                candidates.append(derived)
        for cause in candidates:
            identity = _cause_identity(cause)
            if identity in seen:
                continue
            seen.add(identity)
            history.append({k: v for k, v in cause.items() if k != "attempt"})

    _extend(prior_entry)
    _extend(current_entry)

    for index, cause in enumerate(history, start=1):
        cause["attempt"] = index
    return history
