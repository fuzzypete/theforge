"""Recorded spike outcomes, in one place, for the guard's two test mirrors.

:mod:`tests.test_spike_guard_outcome` exercises the rule against these
directly; :mod:`tests.test_spike_guard_boundary` feeds the same strings through
a stubbed ``gh``. Keeping the corpus here means the two mirrors agree on what a
recorded outcome looks like (#2600).
"""

from __future__ import annotations

from theforge.spike_guard import IssueFacts

DO_NOT_PROCEED = (
    "<!-- forge-spike-outcome-v1\n"
    "outcome: do_not_proceed\n"
    "reason: the trust threshold is unreachable with the signal available\n"
    "-->"
)
FOLLOW_UP = "<!-- forge-spike-outcome-v1\noutcome: follow_up\nfollow-up: #2599\n-->"
CONDITIONAL = "<!-- forge-spike-outcome-v1\noutcome: conditional_follow_up\nfollow-up: #2599\n-->"
TRIGGER_SECTION = (
    "## Spike trigger condition\n\n"
    "- **What must be true:** the observer beats the naive baseline on real sprints.\n"
    "- **How to know:** the comparison hook reports its trust threshold met.\n"
)


def spike(body: str = "", labels: tuple[str, ...] = ("spike",)) -> IssueFacts:
    return IssueFacts(number=2348, state="OPEN", labels=labels, body=body)


def follow_up(
    body: str = "",
    labels: tuple[str, ...] = ("enhancement",),
    state: str = "OPEN",
) -> IssueFacts:
    return IssueFacts(number=2599, state=state, labels=labels, body=body)
