"""A per-story routing stop ends the sprint without failing the story (#2950).

The account cannot invoke a model for one of a story's phases. That is the same
answer for every story still queued — they route the same pool against the same
account — so re-deriving it once per story is spend for nothing. And nothing
judged the story, so recording it FAILED would present a substrate condition as
a property of the work, which is the conflation #1951 exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sprint_test_helpers import run_sprint_ctx

from tests.test_sprint_resume import (
    _make_config,
    _make_coordinator_result,
    _make_manifest,
    _make_spec_file,
)
from theforge.assignment import ROUTING_STOPPED_ERROR_TYPE
from theforge.coordinator.state import Phase

_STOP_MESSAGE = (
    "no model available for phase dev: opus excluded (not available to this "
    "account under ChatGPT-account auth (not in account catalog, checked "
    "2026-09-05T12:00:00+00:00)). Nothing dispatched, $0.00 spent."
)


def _routing_stopped_result():
    """What the coordinator returns when availability empties a phase."""
    result = _make_coordinator_result(
        success=False,
        cost=0.0,
        phase=Phase.PREFLIGHT,
    )
    result.message = _STOP_MESSAGE
    result.infrastructure_failure = True
    result.state.error = _STOP_MESSAGE
    result.state.error_type = ROUTING_STOPPED_ERROR_TYPE
    return result


def _two_story_sprint(tmp_path: Path) -> Path:
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    return _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])


def test_a_routing_stop_is_skipped_not_failed_and_halts_the_sprint(tmp_path):
    config = _make_config(tmp_path)
    manifest = _two_story_sprint(tmp_path)

    with patch("theforge.sprint.runner.run_task", return_value=_routing_stopped_result()) as task:
        with patch("theforge.sprint.runner.run_batch_preflight", return_value={}):
            result = run_sprint_ctx(config, manifest)

    assert result.specs_failed == 0, "nothing judged the story, so it is not a failure"
    assert result.specs_skipped == 2, "the refused story and the one never dispatched"
    assert task.call_count == 1, "the second story is not dispatched into the same refusal"
    assert result.stopped_reason
    assert "availability" in result.stopped_reason.lower()
    assert result.total_cost_usd == 0.0


def test_the_stop_reason_reaches_the_operator_verbatim(tmp_path):
    config = _make_config(tmp_path)
    manifest = _two_story_sprint(tmp_path)

    with patch("theforge.sprint.runner.run_task", return_value=_routing_stopped_result()):
        with patch("theforge.sprint.runner.run_batch_preflight", return_value={}):
            result = run_sprint_ctx(config, manifest)

    assert "no model available for phase dev" in (result.stopped_reason or "")
    assert "$0.00 spent" in (result.stopped_reason or "")
    # And the per-story record carries the same sentence, not a generic failure.
    _slug, coordinator_result = result.results[0]
    assert coordinator_result.state.error_type == ROUTING_STOPPED_ERROR_TYPE
    assert "no model available for phase dev" in coordinator_result.message


def test_an_ordinary_failure_still_fails_and_does_not_halt(tmp_path):
    """The stop branch is scoped to routing, not to every unsuccessful result."""
    config = _make_config(tmp_path)
    manifest = _two_story_sprint(tmp_path)

    with patch(
        "theforge.sprint.runner.run_task",
        return_value=_make_coordinator_result(success=False, cost=1.0),
    ) as task:
        with patch("theforge.sprint.runner.run_batch_preflight", return_value={}):
            result = run_sprint_ctx(config, manifest)

    assert result.specs_failed == 2
    assert task.call_count == 2, "an ordinary failure does not stop the sprint"
    assert not result.stopped_reason


def test_the_launch_gate_runs_before_any_story_is_dispatched(tmp_path):
    """Ordering is the whole point of the launch gate: refuse before spending."""
    config = _make_config(tmp_path)
    manifest = _two_story_sprint(tmp_path)
    calls: list[str] = []

    from theforge.sprint.availability_gate import SprintNoAvailableModel

    def _gate(*_args, **_kwargs):
        calls.append("gate")
        raise SprintNoAvailableModel("no model available for phase dev")

    with patch("theforge.sprint.runner.enforce_sprint_availability", side_effect=_gate):
        with patch("theforge.sprint.runner.run_task") as task:
            with patch("theforge.sprint.runner.run_batch_preflight") as batch:
                try:
                    run_sprint_ctx(config, manifest)
                except SprintNoAvailableModel:
                    pass

    assert calls == ["gate"]
    assert task.call_count == 0, "no story is dispatched"
    assert batch.call_count == 0, "not even batch preflight is paid for"


def test_a_sibling_cancelled_by_a_routing_stop_is_not_blamed_on_credentials(tmp_path):
    """A routing stop is not an auth failure, and its casualties must not read as one.

    The sibling cancellation reused the auth breaker's set, so a story the
    routing stop killed was stamped as a CATEGORY_AUTH infrastructure abort with
    an empty authentication reason — a credential rejection that never happened
    (#2950 review).
    """
    from theforge.sprint.runner import _mark_story_routing_cancelled

    result = _make_coordinator_result(success=False, cost=0.0, phase=Phase.PREFLIGHT)
    reason = "cancelled mid-flight: Model availability stopped routing (no model for dev)"
    _mark_story_routing_cancelled(result, reason=reason)

    assert result.state.error_type == ROUTING_STOPPED_ERROR_TYPE
    assert result.state.error == reason
    assert result.message == reason
    # Nothing about a credential: no auth category, no infrastructure-abort
    # stamp carrying an empty authentication detail.
    assert getattr(result.state, "infrastructure_failure", None) is None
    assert getattr(result.state, "agent_invocation_failures", []) == []


def test_the_routing_cancel_reason_comes_from_the_stop_condition(tmp_path):
    """One owner for why the sprint stopped, so the story and the run agree."""
    from theforge.sprint.runner import SprintExecutionState, _routing_cancel_reason

    state = SprintExecutionState.__new__(SprintExecutionState)
    from theforge.sprint.runner import SprintStopCondition

    state.stop = SprintStopCondition()
    assert _routing_cancel_reason(state, "fallback text") == "cancelled mid-flight: fallback text"

    state.stop.stop_if_unset("Model availability stopped routing (dev)")
    assert _routing_cancel_reason(state, "fallback text") == (
        "cancelled mid-flight: Model availability stopped routing (dev)"
    )
