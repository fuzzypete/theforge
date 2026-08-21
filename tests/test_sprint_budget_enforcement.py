"""Mid-story enforcement of the sprint's spending cap (#2547).

Before this, ``budget_usd`` was checked in exactly one place: whether the *next*
story could be dispatched. A sprint whose work is one long story never returns
to that boundary, so the cap bounded nothing — a $50 run reached $70.44 across
five review cycles without halting, warning, or marking the overrun anywhere.

What is covered here:

- a story that crosses the cap while running is cancelled at the next
  coordinator phase boundary, before another dev/review cycle is paid for;
- the sprint's stop reason names the budget, and the cancelled story is
  recorded as skipped rather than judged as failed;
- later stories are not dispatched;
- in-flight spend is charged exactly once — never twice while a sibling's
  terminal cost is being folded into the ledger, and never zero afterwards;
- unmeasured spend still fails closed at dispatch rather than reading as free.

Every fixture is a fake: no provider CLI is invoked, and the "coordinator" is a
stand-in that reports costs through the same ``state_update_fn`` the real one
uses and checks the same ``stop_event`` at the same boundaries.
"""

from __future__ import annotations

import datetime
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from sprint_test_helpers import run_sprint_ctx

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.types import SprintBatchConfig, SprintConfig
from theforge.coordinator.cancellation import (
    BUDGET_CANCEL_ERROR_TYPE,
    StopSignal,
    cancel_cause,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.budget_runtime import SprintCostLedger
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.sources import FileSource

# ── fixtures ─────────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
        validation=replace(DEFAULT_VALIDATION, gate_command="true"),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=5),
    )


def _make_resolved(
    tmp_path: Path, slugs: tuple[str, ...], *, budget_usd: float, max_parallel: int = 1
) -> ResolvedSprint:
    source = FileSource()
    stories = []
    for slug in slugs:
        story_file = tmp_path / f"{slug}.md"
        story_file.write_text(
            f"---\nname: Story {slug}\nslug: {slug}\n---\n# Content\n",
            encoding="utf-8",
        )
        task = source.fetch(f"{slug}.md", tmp_path)
        stories.append((task, source, f"{slug}.md"))
    return ResolvedSprint(
        name="Test Sprint",
        budget_usd=budget_usd,
        stories=stories,
        max_parallel=max_parallel,
    )


def _make_batch_config(
    tmp_path: Path, *, max_stories: int = 2, max_complexity_budget: int = 2
) -> ForgeConfig:
    return replace(
        _make_config(tmp_path),
        sprint=SprintConfig(
            batch=SprintBatchConfig(
                max_stories=max_stories,
                max_complexity_budget=max_complexity_budget,
            )
        ),
    )


def _done_result(cost: float) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_result = MagicMock(cost_usd=0.0)
    state.dev_results.append(MagicMock(cost_usd=cost))
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")


def _cancelled_result(stop_event: object, cost: float) -> CoordinatorResult:
    """What coordinator/engine.py returns for a story killed by stop_event.

    Built through the same ``cancel_cause`` seam the engine uses, so this
    fixture cannot drift from the labelling the engine actually applies.
    """
    reason, error_type = cancel_cause(stop_event)  # type: ignore[arg-type]
    state = CoordinatorState()
    state.preflight_result = MagicMock(cost_usd=0.0)
    state.dev_results.append(MagicMock(cost_usd=cost))
    state.phase = Phase.ESCALATE
    state.error = reason
    state.error_type = error_type
    return CoordinatorResult(success=False, phase=Phase.ESCALATE, state=state, message=reason)


def _run(config: ForgeConfig, resolved: ResolvedSprint, fake_run_task) -> object:
    with (
        patch("theforge.sprint.runner.enforce_sprint_auth_readiness"),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
        patch("theforge.sprint.audit_publish._write_sprint_audit"),
        patch("theforge.sprint.audit_publish._write_sprint_summary"),
        patch("theforge.sprint.runner._write_story_audit"),
    ):
        return run_sprint_ctx(config, resolved)


# ── mid-story enforcement ────────────────────────────────────────────────


def test_story_crossing_the_cap_is_stopped_before_another_review_cycle(
    tmp_path: Path,
) -> None:
    """The reported bug: five review cycles at $50 cap, nothing halted.

    The fake coordinator runs the same loop the real one does — report cost at
    the phase boundary, then check the stop signal — and spends past the cap on
    its third cycle. The sprint must stop it there rather than let it keep
    buying cycles.
    """
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a", "story-b"), budget_usd=10.0)
    dispatched: list[str] = []
    cycles: list[int] = []

    def _fake_run_task(_config, task, **kwargs):
        dispatched.append(task.slug)
        state_update_fn = kwargs.get("state_update_fn")
        stop_event = kwargs.get("stop_event")
        spent = 0.0
        for cycle in range(1, 6):
            spent += 4.0
            state_update_fn({"phase": "REVIEW", "cost_usd": spent})
            if stop_event is not None and stop_event.is_set():
                cycles.append(cycle)
                return _cancelled_result(stop_event, spent)
        cycles.append(5)
        return _done_result(spent)

    result = _run(config, resolved, _fake_run_task)

    # Stopped on the cycle that crossed $10, not at the cycle limit.
    assert cycles == [3]
    # The later story never started: the cap is exhausted.
    assert dispatched == ["story-a"]
    assert result.stopped_reason is not None
    assert result.stopped_reason.startswith("Budget exhausted")
    # Nothing judged story-a — it was stopped, not failed.
    assert result.specs_failed == 0
    assert result.specs_skipped == 2


def test_two_running_stories_cross_the_cap_together(tmp_path: Path) -> None:
    """Parallel mode: neither story exceeds the cap alone, together they do.

    Both are genuinely in flight when the second one's report tips the total
    past the cap, so both must be stopped. This is also where double-counting
    would show: if the ledger charged either story's spend twice, the halt would
    fire on the first report instead of the second.
    """
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a", "story-b"), budget_usd=10.0, max_parallel=2)
    both_running = threading.Barrier(2, timeout=30)
    a_reported = threading.Event()
    halted: dict[str, bool] = {}

    def _fake_run_task(_config, task, **kwargs):
        state_update_fn = kwargs["state_update_fn"]
        stop_event = kwargs["stop_event"]
        both_running.wait()
        if task.slug == "story-a":
            # $6 of a $10 cap: on its own, affordable.
            state_update_fn({"phase": "REVIEW", "cost_usd": 6.0})
            halted["story-a-after-own-report"] = stop_event.is_set()
            a_reported.set()
            assert stop_event.wait(timeout=30), "sibling's spend never stopped this story"
        else:
            assert a_reported.wait(timeout=30)
            # $6 + $6 = $12: the pair is not.
            state_update_fn({"phase": "REVIEW", "cost_usd": 6.0})
        halted[task.slug] = stop_event.is_set()
        return _cancelled_result(stop_event, 6.0)

    result = _run(config, resolved, _fake_run_task)

    # story-a alone did not trip the cap — only the pair did.
    assert halted["story-a-after-own-report"] is False
    assert halted["story-a"] is True
    assert halted["story-b"] is True
    assert result.stopped_reason.startswith("Budget exhausted")
    # $6 + $6 recorded once each, not once per in-flight report.
    assert round(result.total_cost_usd, 2) == 12.0


def test_worker_exception_recovery_promotes_the_last_measured_spend() -> None:
    """A raised worker's last live figure becomes terminal sprint spend.

    The scheduler has no terminal ``CoordinatorResult`` to fold in on this
    path, so it must promote the provisional in-flight amount itself or the
    sprint silently forgets money it already spent.
    """
    ledger = SprintCostLedger()
    ledger.record_in_flight_cost("story-a", 4.0)

    recovered = ledger.recover_in_flight_cost("story-a")
    assert recovered.accumulated == 4.0
    assert recovered.in_flight == 0.0

    ledger.record_story_cost("story-b", 1.5, measured=1.5)
    assert round(ledger.snapshot().spent_including_in_flight, 2) == 5.5


def test_worker_exception_recovery_keeps_a_lower_bound_total_unmeasured() -> None:
    """Promoting an unmeasured-phase lower bound must not certify the sprint total."""
    ledger = SprintCostLedger()
    ledger.record_in_flight_cost("story-a", 6.0, measured=False)

    recovered = ledger.recover_in_flight_cost("story-a")

    assert recovered.accumulated == 6.0
    assert recovered.measured is False
    assert recovered.unmeasured == ("story-a",)


def test_batched_worker_exception_recovers_the_shared_spend_once(tmp_path: Path) -> None:
    """A shared worker future must not charge its spend once per member slug."""
    config = _make_batch_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a", "story-b"), budget_usd=100.0)
    states: dict[str, CoordinatorState] = {}
    for slug in ("story-a", "story-b"):
        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        state.preflight_complexity = "small"
        state.preflight_work_type = "bug"
        state.preflight_sufficiency = "implementation_ready"
        state.preflight_likely_files = [f"src/{slug}.py"]
        states[slug] = state

    def _fake_run_batch_group(
        _config,
        leader_task,
        member_tasks,
        _sprint_run_id,
        _sprint_name,
        _interactive,
        _notify,
        _effective_auto_merge,
        state_update_fns,
        _no_pull,
        _preflight_states,
        _stop_event,
        *,
        base_lands_locally=None,
        lands_in_project_root=None,
    ):
        del base_lands_locally, lands_in_project_root
        state_update_fns[leader_task.slug]({"phase": "DEV", "cost_usd": 4.0})
        for member in member_tasks:
            state_update_fns[member.slug]({"phase": "DEV", "cost_usd": 4.0, "cost_mirrored": True})
        raise RuntimeError("batch exploded")

    with (
        patch("theforge.sprint.runner.enforce_sprint_auth_readiness"),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value=states),
        patch("theforge.sprint.runner._run_batch_group", side_effect=_fake_run_batch_group),
        patch("theforge.sprint.audit_publish._write_sprint_audit"),
        patch("theforge.sprint.audit_publish._write_sprint_summary"),
        patch("theforge.sprint.runner._write_story_audit"),
    ):
        result = run_sprint_ctx(config, resolved)

    assert result.specs_failed == 2
    assert result.total_cost_usd == 4.0
    assert result.cost_complete is True


def test_batched_worker_timeout_recovers_the_shared_spend_once(tmp_path: Path) -> None:
    """The deadline recovery path must preserve the shared batch spend once."""
    config = _make_batch_config(tmp_path)
    resolved = replace(
        _make_resolved(tmp_path, ("story-a", "story-b"), budget_usd=100.0),
        worker_timeout_seconds=1,
    )
    states: dict[str, CoordinatorState] = {}
    for slug in ("story-a", "story-b"):
        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        state.preflight_complexity = "small"
        state.preflight_work_type = "bug"
        state.preflight_sufficiency = "implementation_ready"
        state.preflight_likely_files = [f"src/{slug}.py"]
        states[slug] = state

    def _fake_run_batch_group(
        _config,
        leader_task,
        member_tasks,
        _sprint_run_id,
        _sprint_name,
        _interactive,
        _notify,
        _effective_auto_merge,
        state_update_fns,
        _no_pull,
        _preflight_states,
        stop_event,
        *,
        base_lands_locally=None,
        lands_in_project_root=None,
    ):
        del base_lands_locally, lands_in_project_root
        state_update_fns[leader_task.slug]({"phase": "DEV", "cost_usd": 4.0})
        for member in member_tasks:
            state_update_fns[member.slug]({"phase": "DEV", "cost_usd": 4.0, "cost_mirrored": True})
        assert stop_event.wait(timeout=10), "worker deadline never stopped the batch"
        finished = datetime.datetime.now(datetime.timezone.utc)
        leader_result = _cancelled_result(stop_event, 4.0)
        member_result = _cancelled_result(stop_event, 4.0)
        return {
            leader_task.slug: (leader_task, leader_result, 0.0, finished, finished),
            member_tasks[0].slug: (member_tasks[0], member_result, 0.0, finished, finished),
        }

    with (
        patch("theforge.sprint.runner.enforce_sprint_auth_readiness"),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value=states),
        patch("theforge.sprint.runner._run_batch_group", side_effect=_fake_run_batch_group),
        patch("theforge.sprint.audit_publish._write_sprint_audit"),
        patch("theforge.sprint.audit_publish._write_sprint_summary"),
        patch("theforge.sprint.runner._write_story_audit"),
    ):
        result = run_sprint_ctx(config, resolved)

    assert result.specs_failed == 2
    assert result.total_cost_usd == 4.0
    assert result.cost_complete is True


def test_worker_exception_lower_bound_recovery_keeps_sprint_cost_incomplete(
    tmp_path: Path,
) -> None:
    """A raised worker cannot turn a lower bound into a measured sprint total."""
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a",), budget_usd=100.0)
    lower_bound_state = CoordinatorState()
    lower_bound_state.preflight_result = MagicMock(cost_usd=6.0)
    lower_bound_state.dev_results.append(MagicMock(cost_usd=None))
    assert lower_bound_state.total_cost_measured is None
    assert lower_bound_state.total_cost == 6.0

    def _fake_run_single_story(
        _config,
        _task,
        _triage,
        _sprint_run_id,
        _sprint_name,
        _interactive,
        _notify,
        _resume,
        _effective_auto_merge,
        state_update_fn,
        _no_pull=False,
        _plan_gate=None,
        _preflight_states=None,
        _stop_event=None,
        base_lands_locally=None,
        lands_in_project_root=None,
    ):
        del base_lands_locally, lands_in_project_root
        state_update_fn(
            {
                "phase": "REVIEW",
                "cost_usd": None,
                "coordinator_state": lower_bound_state,
            }
        )
        raise RuntimeError("agent crashed after unmeasured phase")

    with (
        patch("theforge.sprint.runner.enforce_sprint_auth_readiness"),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner._run_single_story", side_effect=_fake_run_single_story),
        patch("theforge.sprint.audit_publish._write_sprint_audit"),
        patch("theforge.sprint.audit_publish._write_sprint_summary"),
        patch("theforge.sprint.runner._write_story_audit"),
    ):
        result = run_sprint_ctx(config, resolved)

    assert result.total_cost_usd == 6.0
    assert result.cost_complete is False
    assert result.unmeasured_spend_sources == ("story-a",)
    assert result.budget_verification_spend_usd == 6.0


def test_a_budget_halt_is_not_labelled_as_a_worker_timeout(tmp_path: Path) -> None:
    """A cancelled story must say which lever the sprint pulled.

    The stop_event path exists for worker deadlines. Reusing it unmodified for a
    budget halt would report an operator's spending decision as an unresponsive
    worker — the same conflation #1951 removed for auth aborts.
    """
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a",), budget_usd=5.0)
    seen: dict[str, tuple[str, str]] = {}

    def _fake_run_task(_config, task, **kwargs):
        state_update_fn = kwargs.get("state_update_fn")
        stop_event = kwargs.get("stop_event")
        state_update_fn({"phase": "REVIEW", "cost_usd": 6.0})
        assert stop_event is not None and stop_event.is_set()
        seen[task.slug] = cancel_cause(stop_event)
        return _cancelled_result(stop_event, 6.0)

    result = _run(config, resolved, _fake_run_task)

    reason, error_type = seen["story-a"]
    assert error_type == BUDGET_CANCEL_ERROR_TYPE
    assert "budget" in reason.lower()
    assert "timeout" not in reason.lower()
    _spec, cancelled = result.results[0]
    assert cancelled.state.error_type == BUDGET_CANCEL_ERROR_TYPE
    assert "budget" in (cancelled.state.error or "").lower()


def test_a_budget_cancelled_worker_timeout_stays_skipped_and_keeps_its_spend(
    tmp_path: Path,
) -> None:
    """A budget halt that returns through the deadline path stays a budget halt."""
    config = _make_config(tmp_path)
    resolved = replace(
        _make_resolved(tmp_path, ("story-a",), budget_usd=5.0),
        worker_timeout_seconds=1,
    )
    saw_budget_stop = threading.Event()

    def _fake_run_task(_config, task, **kwargs):
        state_update_fn = kwargs["state_update_fn"]
        stop_event = kwargs["stop_event"]
        state_update_fn({"phase": "REVIEW", "cost_usd": 6.0})
        assert stop_event.wait(timeout=5), "budget halt never reached the worker"
        saw_budget_stop.set()
        time.sleep(1.2)
        return _cancelled_result(stop_event, 6.0)

    result = _run(config, resolved, _fake_run_task)

    assert saw_budget_stop.is_set()
    assert result.stopped_reason is not None
    assert result.stopped_reason.startswith("Budget exhausted")
    assert result.specs_failed == 0
    assert result.specs_skipped == 1
    assert round(result.total_cost_usd, 2) == 6.0
    assert result.budget_status == "over"
    _spec, cancelled = result.results[0]
    assert cancelled.state.error_type == BUDGET_CANCEL_ERROR_TYPE
    assert "budget" in (cancelled.state.error or "").lower()
    assert "timeout" not in (cancelled.state.error or "").lower()


def test_a_timed_out_storys_late_cost_report_cannot_block_the_next_dispatch(
    tmp_path: Path,
) -> None:
    """A retired worker cannot recreate in-flight spend after its timeout."""
    config = _make_config(tmp_path)
    resolved = replace(
        _make_resolved(tmp_path, ("story-a", "story-b"), budget_usd=7.0),
        worker_timeout_seconds=1,
    )
    late_update_sent = threading.Event()
    dispatched: list[str] = []

    def _fake_run_task(_config, task, **kwargs):
        dispatched.append(task.slug)
        state_update_fn = kwargs["state_update_fn"]
        stop_event = kwargs["stop_event"]
        if task.slug == "story-a":
            state_update_fn({"phase": "REVIEW", "cost_usd": 4.0})
            assert stop_event.wait(timeout=5), "worker timeout never stopped story-a"
            state_update_fn({"phase": "REVIEW", "cost_usd": 9.0})
            late_update_sent.set()
            time.sleep(0.2)
            return _cancelled_result(stop_event, 4.0)
        assert late_update_sent.wait(timeout=5), "story-a never sent its stale post-timeout update"
        state_update_fn({"phase": "REVIEW", "cost_usd": 2.0})
        return _done_result(2.0)

    result = _run(config, resolved, _fake_run_task)

    assert dispatched == ["story-a", "story-b"]
    assert result.stopped_reason is None
    assert result.specs_succeeded == 1
    assert result.specs_failed == 1
    assert result.specs_skipped == 0
    assert round(result.total_cost_usd, 2) == 6.0
    assert result.budget_status == "within"


def test_batched_member_reviews_stop_before_starting_the_next_member_when_over_budget(
    tmp_path: Path,
) -> None:
    """A member review that exhausts the cap must block later member reviews."""
    config = _make_batch_config(tmp_path, max_stories=3, max_complexity_budget=3)
    resolved = _make_resolved(tmp_path, ("story-a", "story-b", "story-c"), budget_usd=5.0)
    states: dict[str, CoordinatorState] = {}
    for slug in ("story-a", "story-b", "story-c"):
        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        state.preflight_complexity = "small"
        state.preflight_work_type = "bug"
        state.preflight_sufficiency = "implementation_ready"
        state.preflight_likely_files = [f"src/{slug}.py"]
        states[slug] = state
    workspace = tmp_path / "batch-workspace"
    workspace.mkdir()
    review_calls: list[str] = []

    def _fake_run_single_story(
        _config,
        task,
        _triage,
        _sprint_run_id,
        _sprint_name,
        _interactive,
        _notify,
        _resume,
        _effective_auto_merge,
        state_update_fn,
        _no_pull=False,
        _plan_gate=None,
        _preflight_states=None,
        _stop_event=None,
        base_lands_locally=None,
        lands_in_project_root=None,
    ):
        del base_lands_locally, lands_in_project_root
        state_update_fn({"phase": "DEV", "cost_usd": 1.0})
        state = CoordinatorState()
        state.preflight_result = MagicMock(cost_usd=1.0)
        state.workspace_path = workspace
        state.branch_name = f"forge/{task.slug}"
        finished = datetime.datetime.now(datetime.timezone.utc)
        return (
            task,
            CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok"),
            0.0,
            finished,
            finished,
        )

    def _fake_run_review_only(_config, task, workspace_path, **kwargs):
        del kwargs
        assert workspace_path == workspace
        review_calls.append(task.slug)
        if task.slug == "story-b":
            return _done_result(5.0)
        raise AssertionError("story-c review started after story-b exhausted the budget")

    with (
        patch("theforge.sprint.runner.enforce_sprint_auth_readiness"),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value=states),
        patch("theforge.sprint.runner._run_single_story", side_effect=_fake_run_single_story),
        patch("theforge.sprint.runner.run_review_only", side_effect=_fake_run_review_only),
        patch("theforge.sprint.audit_publish._write_sprint_audit"),
        patch("theforge.sprint.audit_publish._write_sprint_summary"),
        patch("theforge.sprint.runner._write_story_audit"),
    ):
        result = run_sprint_ctx(config, resolved)

    assert review_calls == ["story-b"]
    assert result.stopped_reason is not None
    assert result.stopped_reason.startswith("Budget exhausted")
    assert result.specs_succeeded == 2
    assert result.specs_failed == 0
    assert result.specs_skipped == 1
    assert round(result.total_cost_usd, 2) == 6.0
    by_slug = {Path(spec).stem: story_result for spec, story_result in result.results}
    assert by_slug["story-c"].state.error_type == BUDGET_CANCEL_ERROR_TYPE
    assert "budget" in (by_slug["story-c"].state.error or "").lower()


def test_unmeasured_phase_uses_the_coordinator_lower_bound_for_budget_checks(
    tmp_path: Path,
) -> None:
    """An unknown total is still charged at its measured lower bound.

    Once a phase reports ``cost_usd: null``, ``total_cost_measured`` is poisoned
    to ``None`` for the rest of the run. Mid-story enforcement must then fall
    back to the coordinator state's measured lower bound instead of letting the
    story bypass every later sprint-budget checkpoint.
    """
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a",), budget_usd=5.0)
    lower_bound_state = CoordinatorState()
    lower_bound_state.preflight_result = MagicMock(cost_usd=6.0)
    lower_bound_state.dev_results.append(MagicMock(cost_usd=None))
    assert lower_bound_state.total_cost_measured is None
    assert lower_bound_state.total_cost == 6.0
    seen: dict[str, tuple[str, str]] = {}

    def _fake_run_task(_config, task, **kwargs):
        state_update_fn = kwargs["state_update_fn"]
        stop_event = kwargs["stop_event"]
        state_update_fn(
            {
                "phase": "REVIEW",
                "cost_usd": None,
                "coordinator_state": lower_bound_state,
            }
        )
        assert stop_event.is_set(), "lower-bound checkpoint did not stop the over-budget story"
        seen[task.slug] = cancel_cause(stop_event)
        return _cancelled_result(stop_event, lower_bound_state.total_cost)

    result = _run(config, resolved, _fake_run_task)

    reason, error_type = seen["story-a"]
    assert error_type == BUDGET_CANCEL_ERROR_TYPE
    assert "budget" in reason.lower()
    assert result.stopped_reason.startswith("Budget exhausted")
    assert round(result.total_cost_usd, 2) == 6.0


def test_a_run_inside_its_cap_is_left_alone(tmp_path: Path) -> None:
    """Enforcement must not become a new way for affordable sprints to fail."""
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a", "story-b"), budget_usd=100.0)
    dispatched: list[str] = []

    def _fake_run_task(_config, task, **kwargs):
        dispatched.append(task.slug)
        state_update_fn = kwargs.get("state_update_fn")
        state_update_fn({"phase": "REVIEW", "cost_usd": 2.0})
        assert not kwargs["stop_event"].is_set()
        return _done_result(2.0)

    result = _run(config, resolved, _fake_run_task)

    assert dispatched == ["story-a", "story-b"]
    assert result.stopped_reason is None
    assert result.specs_succeeded == 2
    assert result.budget_status == "within"
    assert result.budget_overrun_usd == 0.0


def test_an_overrun_is_reported_on_the_result(tmp_path: Path) -> None:
    """A run that finished past its cap says so where its cost is reported."""
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a",), budget_usd=5.0)

    def _fake_run_task(_config, task, **kwargs):
        kwargs["state_update_fn"]({"phase": "REVIEW", "cost_usd": 8.0})
        return _cancelled_result(kwargs["stop_event"], 8.0)

    result = _run(config, resolved, _fake_run_task)

    assert result.budget_status == "over"
    assert round(result.budget_overrun_usd, 2) == 3.0


def test_the_audit_and_summary_record_the_overrun(tmp_path: Path) -> None:
    """An operator reading the run afterwards can tell it passed its cap.

    The terminal records carry the standing, not just the two numbers: a run
    that respected its budget and one that did not must not be distinguishable
    only by doing the arithmetic yourself (#2547).
    """
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a",), budget_usd=5.0)

    def _fake_run_task(_config, task, **kwargs):
        kwargs["state_update_fn"]({"phase": "REVIEW", "cost_usd": 9.0})
        return _cancelled_result(kwargs["stop_event"], 9.0)

    with (
        patch("theforge.sprint.runner.enforce_sprint_auth_readiness"),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
        patch("theforge.sprint.runner._write_story_audit"),
    ):
        run_sprint_ctx(config, resolved)

    audit = yaml.safe_load(
        (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
    )["sprint"]
    assert audit["budget_status"] == "over"
    assert round(audit["budget_overrun_usd"], 2) == 4.0

    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text(
            encoding="utf-8"
        )
    )["sprint"]
    assert summary["budget_status"] == "over"
    assert round(summary["budget_overrun_usd"], 2) == 4.0
    assert summary["stopped_reason"].startswith("Budget exhausted")


# ── the ledger: in-flight spend counted exactly once ──────────────────────


def test_in_flight_spend_is_not_double_counted_when_a_sibling_lands() -> None:
    """Two stories running; one lands while the other is still spending.

    The moment ``record_story_cost`` folds a story's terminal figure in is the
    moment its provisional figure must disappear — in that order, under one
    lock. Either mistake corrupts the cap: counted twice it refuses affordable
    work, dropped early it under-reports what the sprint has spent.
    """
    ledger = SprintCostLedger()
    ledger.record_in_flight_cost("story-a", 4.0)
    ledger.record_in_flight_cost("story-b", 3.0)

    assert ledger.snapshot().spent_including_in_flight == 7.0

    ledger.record_story_cost("story-a", 4.5, measured=4.5)
    snapshot = ledger.snapshot()

    # story-a is counted once, at its terminal figure; story-b still in flight.
    assert snapshot.accumulated == 4.5
    assert snapshot.in_flight == 3.0
    assert snapshot.spent_including_in_flight == 7.5

    ledger.record_story_cost("story-b", 3.25, measured=3.25)
    assert ledger.snapshot().in_flight == 0.0
    assert ledger.snapshot().spent_including_in_flight == 7.75


def test_a_running_story_updates_its_own_in_flight_figure() -> None:
    """The coordinator reports a running total, so the last word wins."""
    ledger = SprintCostLedger()
    ledger.record_in_flight_cost("story-a", 2.0)
    ledger.record_in_flight_cost("story-a", 5.0)

    assert ledger.snapshot().in_flight == 5.0


def test_a_dropped_story_stops_being_charged() -> None:
    """A worker that raised leaves no terminal cost to replace its estimate."""
    ledger = SprintCostLedger()
    ledger.record_in_flight_cost("story-a", 2.0)
    ledger.drop_in_flight_cost("story-a")

    assert ledger.snapshot().in_flight == 0.0
    assert ledger.snapshot().spent_including_in_flight == 0.0


def test_unmeasured_terminal_cost_still_poisons_the_total() -> None:
    """Fail-closed on unmeasured spend survives the in-flight bookkeeping."""
    ledger = SprintCostLedger()
    ledger.record_in_flight_cost("story-a", 1.0)
    ledger.record_story_cost("story-a", 0.0, measured=None)

    snapshot = ledger.snapshot()
    assert snapshot.unmeasured == ("story-a",)
    assert snapshot.in_flight == 0.0


# ── unmeasured spend keeps its dispatch-only semantics ───────────────────


def test_unmeasured_in_flight_cost_does_not_kill_a_running_story(
    tmp_path: Path,
) -> None:
    """An unmeasured phase is not a licence to destroy paid-for work.

    Unknown spend still fails closed — at the dispatch gate, which refuses the
    next story. What it must not do is cancel the story that is already running,
    which would trade a story for a comparison the dispatch gate re-runs anyway.
    """
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path, ("story-a", "story-b"), budget_usd=100.0)
    dispatched: list[str] = []

    def _fake_run_task(_config, task, **kwargs):
        dispatched.append(task.slug)
        state_update_fn = kwargs.get("state_update_fn")
        state_update_fn({"phase": "REVIEW", "cost_usd": None})
        assert not kwargs["stop_event"].is_set(), "unmeasured spend cancelled a running story"
        state = CoordinatorState()
        state.preflight_result = MagicMock(cost_usd=0.0)
        # An unmeasured story: total_cost_measured is None, total_cost is 0.0.
        state.dev_results.append(MagicMock(cost_usd=None))
        return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")

    result = _run(config, resolved, _fake_run_task)

    # story-a ran to completion; the sprint then refused to dispatch story-b
    # against a total it knows is a lower bound.
    assert dispatched == ["story-a"]
    assert result.stopped_reason is not None
    assert "unverifiable" in result.stopped_reason.lower()


# ── the stop signal itself ───────────────────────────────────────────────


def test_a_bare_event_still_reads_as_a_timeout() -> None:
    """Callers that predate the distinction keep the historical wording."""
    reason, error_type = cancel_cause(threading.Event())

    assert error_type == "StoryCancelled"
    assert "timeout" in reason.lower()


def test_a_stop_signal_carries_its_cause() -> None:
    signal = StopSignal()
    signal.stop("sprint budget exhausted", error_type=BUDGET_CANCEL_ERROR_TYPE)

    assert signal.is_set()
    assert cancel_cause(signal) == ("sprint budget exhausted", BUDGET_CANCEL_ERROR_TYPE)


def test_a_plain_set_keeps_the_timeout_cause() -> None:
    signal = StopSignal()
    signal.set()

    assert cancel_cause(signal)[1] == "StoryCancelled"
