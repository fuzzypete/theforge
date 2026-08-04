"""Landing evidence in a recorded sprint story entry.

A sprint's accumulated story entry (``.forge/sprints/<id>/state.yaml``) records
the coordinator phase a story reached. That phase is written the moment the
coordinator returns — which for an approved story is *before* the sprint's
separate landing step runs. A generation that re-execs in that window leaves a
durable ``outcome: DONE`` behind for a story that never landed, and the re-exec
launch guard used to read that as proof of completion: the story was reconciled
as a finished success, never re-entered dispatch, and was counted as landed while
its issue stayed open with unmerged commits on a branch (#2189).

The predicates here are the shared answer to "does this record show a landing
that was owed *and* never resolved?". They are deliberately conservative in one
direction only: a record with no landing information at all is not evidence of
an unresolved landing, so it keeps the historical behaviour. Only an explicitly
recorded unresolved landing demotes a succeeded outcome.

Stdlib-only imports (per project convention 4: pure-data types in low-dependency
modules).
"""

from __future__ import annotations

from collections.abc import Mapping

# Recorded outcome strings (upper-cased) that claim the story succeeded.
PRIOR_SUCCEEDED_OUTCOMES = frozenset({"DONE", "ALREADY_DONE"})

# ``landing_status`` values that mean a landing was owed and has not completed.
#
#   pending_integration — approved, landing deferred to the sprint scheduler's
#     integration step. Recorded before that step runs (and re-recorded if it
#     could not run), so it is exactly the pre-landing state #2189 mistook for a
#     completed one.
#   failed — the landing ran and did not land the work.
#
# ``None`` is deliberately NOT in this set: it is the recorded status of a story
# with no landing obligation of its own (``on_approve`` ``pr`` / ``none``), which
# reaches its configured terminal without any merge. Demoting those would strand
# every story in a PR-only or branch-only sprint.
UNRESOLVED_LANDING_STATUSES = frozenset({"pending_integration", "failed"})


def as_prior_record(value: object) -> dict:
    """Normalise a prior-generation outcome value into a record dict.

    Accepts either a bare outcome string (the pre-#2189 shape, still used by
    callers that have nothing but the outcome) or a full story-entry mapping.
    ``outcome`` is upper-cased so callers can compare against
    :data:`PRIOR_SUCCEEDED_OUTCOMES` without re-normalising.
    """
    if isinstance(value, Mapping):
        record = dict(value)
        record["outcome"] = str(record.get("outcome") or "").upper()
        return record
    return {"outcome": str(value or "").upper()}


def landing_settled(record: object) -> bool:
    """True unless the record shows a landing that was owed and never completed.

    Positive evidence of a completed landing (a confirmed merge, a queued
    auto-merge, a created PR) settles it, as does the absence of any landing
    obligation. Only a recorded :data:`UNRESOLVED_LANDING_STATUSES` status with
    no such evidence reads as unsettled.
    """
    if not isinstance(record, Mapping):
        # No record at all is not evidence that a landing is outstanding.
        return True
    if record.get("landed") is True or record.get("merge") is True:
        return True
    landing = record.get("landing")
    if isinstance(landing, Mapping) and (
        landing.get("merged") or landing.get("merge_queued") or landing.get("pr_url")
    ):
        return True
    return str(record.get("landing_status") or "") not in UNRESOLVED_LANDING_STATUSES


def reconcilable_prior_success(record: object) -> bool:
    """True when the record is a succeeded outcome whose landing is settled.

    This is the gate a re-exec'd generation applies before preserving a prior
    generation's outcome instead of re-running the story. A ``DONE`` whose
    landing was owed and never completed is *not* reconcilable: the story has no
    completed outcome to carry forward, and asserting one reports work as landed
    that is still sitting unmerged on a branch (#2189).
    """
    normalized = as_prior_record(record)
    if normalized["outcome"] not in PRIOR_SUCCEEDED_OUTCOMES:
        return False
    return landing_settled(record if isinstance(record, Mapping) else None)
