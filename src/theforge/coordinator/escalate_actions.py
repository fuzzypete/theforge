"""What an escalate gate can actually perform, derived from coordinator state.

A gate that offers an action it cannot carry out spends the operator's attention
on a choice the run will not honour, and the substitution it makes instead
records an outcome nobody selected (#2300). Both halves of that defect are fixed
by one question, asked in one place: *is there a reviewer verdict this run could
finalize on?* Every escalate-gate surface (pending-file, interactive, ntfy) asks
it before rendering its options, and the approve disposition asks it again before
acting — so what is offered and what can be performed cannot drift apart.

Stdlib-only by design: gate front-ends and the review phase both import it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from theforge.escalation_advisor import ACTION_TAXONOMY, action_disposition

if TYPE_CHECKING:
    from theforge.review import ReviewResult

    from . import state as _cs


#: The taxonomy action whose disposition is "approve" — the only one whose
#: performability depends on run state rather than on the operator alone.
ACCEPT_ACTION = "accept"

#: Taxonomy actions that still name historical/manual forge operations but are
#: not mechanically executable by this run. Keep them recognizable for parsing
#: stale/manual selections, but do not offer them as real gate choices.
NON_EXECUTABLE_NAMED_ACTIONS = frozenset(
    action for action in ACTION_TAXONOMY if action_disposition(action) == "named"
)

#: Reason text used when accept cannot be offered/performed. Rendered to the
#: operator at the gate and recorded in the audit trail when a stale selection
#: is declined, so "why wasn't this offered" and "why was this refused" read the
#: same in both places.
ACCEPT_UNAVAILABLE_REASON = (
    "no approvable reviewer result is retained for this run — "
    "there is no verdict the gate could finalize on"
)

NAMED_ACTION_UNAVAILABLE_REASON = "action is not executable by this run"


def approvable_review_result(
    state: "_cs.CoordinatorState",
) -> "ReviewResult | None":
    """Return the ReviewResult an approve disposition would finalize, or None.

    Preference order:

    1. the merged review of the last completed cycle (``state.review_results``),
    2. a retained surviving reviewer APPROVE from a cycle whose quorum collapsed.

    (2) exists because a verdict a reviewer actually produced should not become
    invisible to a later, full-knowledge operator decision merely because a
    quorum rule was not satisfied. Quorum still governs *automatic* progression:
    the collapse still escalates, and this result is reachable only through an
    explicit operator selection at the gate.
    """
    review_results = getattr(state, "review_results", None) or []
    if review_results:
        return review_results[-1]
    for _name, rr in getattr(state, "last_cycle_reviewer_results", None) or []:
        if rr is None:
            continue
        if rr.parse_errors:
            continue
        if (rr.verdict or "").strip().upper() == "APPROVE":
            return rr
    return None


def available_escalate_actions(
    state: "_cs.CoordinatorState",
    actions: "list[str] | tuple[str, ...]",
) -> tuple[list[str], dict[str, str]]:
    """Split ``actions`` into what this state can perform and what it cannot.

    Returns ``(available, omitted)`` where ``omitted`` maps each withheld action
    to the reason it was withheld. Order of ``available`` follows ``actions``.
    """
    omitted: dict[str, str] = {}
    available: list[str] = []
    accept_ok = approvable_review_result(state) is not None
    for action in actions:
        if action == ACCEPT_ACTION and not accept_ok:
            omitted[action] = ACCEPT_UNAVAILABLE_REASON
            continue
        if action in NON_EXECUTABLE_NAMED_ACTIONS:
            omitted[action] = NAMED_ACTION_UNAVAILABLE_REASON
            continue
        available.append(action)
    return available, omitted


def omitted_actions_note(omitted: dict[str, str]) -> str:
    """One-line-per-action explanation of what the gate could not offer.

    Empty string when nothing was withheld, so callers can splice it into a
    reason block with ``filter(None, ...)``.
    """
    if not omitted:
        return ""
    lines = ["NOT OFFERED (this run cannot perform it):"]
    lines.extend(f"  • {action}: {reason}" for action, reason in sorted(omitted.items()))
    return "\n".join(lines)
