"""The opt-in headless triage pass a finished sprint may trigger (#2231).

A sprint that just changed the backlog is the moment a triage pass is worth the
most, and the moment nobody is at the keyboard. So this pass runs only the
stages that are safe with no operator: it collects a backlog report, proposes
and adversarially reviews dispositions, and persists the reviewed package as a
pending operator decision. It never ratifies and never touches a tracker.

Three properties this module is responsible for:

* **Opt-in.** It runs only when ``sprint.post_sprint_triage`` is true. The
  caller checks the flag; this module refuses to guess a default.
* **Best effort.** Every failure inside the pass is caught here and returned as
  a reported outcome. A triage stage that breaks must not turn a sprint that
  succeeded into one that failed — the sprint's result was already terminal
  when this ran.
* **Loud.** A pass that produced nothing says why in the sprint log, including
  the supersession case: skipping because an earlier decision is still pending
  is only useful if the operator can see it happened and which record blocked it.

Like :mod:`theforge.sprint.audit_publish`, this module does not import the
runner; it takes the execution state object and reads ``state.context`` off it.
"""

from __future__ import annotations

from typing import Any

from ..log_util import _log_line


def _log(msg: str) -> None:
    _log_line("[sprint]", msg)


def run_post_sprint_triage(state: Any) -> Any:
    """Run the headless triage pass for a finished sprint; never raise.

    ``state`` is the sprint's ``SprintExecutionState`` — see the module
    docstring on why it is not typed. Returns the
    :class:`~theforge.coordinator.triage_headless_flow.HeadlessTriageOutcome`
    the pass produced, or a failure outcome describing why it produced none.
    """
    from ..coordinator.triage_headless_flow import (  # noqa: PLC0415
        shelved_headless_outcome,
    )

    outcome = shelved_headless_outcome()
    for line in outcome.lines:
        _log(line)
    return outcome


__all__ = ["run_post_sprint_triage"]
