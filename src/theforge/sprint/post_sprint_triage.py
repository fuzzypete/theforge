"""The opt-in post-sprint triage hook a finished sprint may trigger (#2231).

ADR-0010 shelved the headless proposal path this hook once ran. When a project
opts into ``sprint.post_sprint_triage``, this module now emits the same
terminal guidance as any other shelved headless triage invocation and returns
that refusal outcome to the sprint runner. It does not collect a report,
dispatch proposers, persist a pending triage decision, or touch a tracker.

Three properties this module is responsible for:

* **Opt-in.** It runs only when ``sprint.post_sprint_triage`` is true. The
  caller checks the flag; this module refuses to guess a default.
* **Best effort.** The hook never turns a sprint that already reached its
  terminal result into a failed sprint. It logs the shelving refusal and
  returns the corresponding outcome instead.
* **Loud.** The sprint log records the ADR-0010 refusal lines so an operator
  can see why the post-sprint pass did nothing and what command remains
  supported.

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
