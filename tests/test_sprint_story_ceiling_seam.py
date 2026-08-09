"""Seam tests for the story ceiling that bounds phase and gate allowances (#2333).

Three boundaries are covered, each at the seam where the bug actually lived:

1. coordinator → dev invocation: a multi-cycle story's per-invocation development
   timeouts fit inside the enclosing sprint worker ceiling, at every cycle count
   the run can reach — not only at the first.
2. pending gate → sprint scheduler: time spent waiting for an operator decision
   does not expire the story, and the gate cannot ask for a wait the story's own
   window could never honour.
3. scheduler → operator-facing state: an elapsed deadline is recorded as an
   abnormal *timeout*, distinct in kind and wording from a quality failure.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))

from tests.test_sprint_parallel import (  # noqa: E402
    _make_config,
    _make_config_with_sprint,
    _make_spec_file,
)
from theforge import pending, worker_budget  # noqa: E402
from theforge.coordinator.state import CoordinatorState  # noqa: E402
from theforge.coordinator.util import (  # noqa: E402
    DEV_INVOCATION_STORY_SHARE,
    cap_timeout_to_story_ceiling,
    clamp_timeout_to_remaining,
)
from theforge.log_util import set_worker_slug  # noqa: E402
from theforge.sprint import run_sprint  # noqa: E402
from theforge.sprint.abnormal import ABNORMAL_WORKER_TIMEOUT  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Every test starts with an empty registry and an unslugged thread."""
    worker_budget.clear_worker_budgets()
    set_worker_slug("")
    yield
    worker_budget.clear_worker_budgets()
    set_worker_slug("")


def _make_preflight_state(complexity: str | None, score: int | None = None) -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_complexity = complexity
    state.preflight_complexity_score = score
    return state


# ── 1. Phase allowance vs the ceiling containing it ──────────────────────────


def test_multi_cycle_dev_allowances_stay_inside_the_enclosing_ceiling() -> None:
    """Every reachable cycle count fits: the *sum* of grants never exceeds the ceiling.

    The reported story ran three development cycles inside a per-invocation
    allowance sized at ~92% of its whole ceiling. Here the two guards are driven
    together exactly as dev dispatch drives them — the static cap first, then the
    per-invocation clamp against what the story has left — and the invariant
    asserted is the one the spec states: consistency at every cycle count that
    can actually occur, not only the smallest.
    """
    ceiling = 5400.0
    raw_timeout = 4950  # what the complexity derivation produced, unbounded
    dev_max = 3

    capped, cap_audit = cap_timeout_to_story_ceiling(raw_timeout, ceiling, dev_max)
    assert capped < raw_timeout
    assert cap_audit["capped"] is True
    # The audit carries the real inputs, not a reconstruction (conventions #6).
    assert cap_audit["raw_timeout_seconds"] == raw_timeout
    assert cap_audit["story_ceiling_seconds"] == 5400
    assert cap_audit["max_invocations"] == dev_max
    assert cap_audit["final_timeout_seconds"] == capped

    budget = worker_budget.register_worker_budget("story-a", ceiling, started_at=0.0)
    granted: list[int] = []
    consumed = 0.0
    for _cycle in range(6):  # more cycles than dev_max: dev_max × review cycles
        remaining = budget.worker_timeout_seconds - consumed
        grant, _clamp = clamp_timeout_to_remaining(capped, remaining)
        granted.append(grant)
        consumed += grant
        if remaining <= 0:
            break

    assert granted[0] <= int(ceiling * DEV_INVOCATION_STORY_SHARE / dev_max)
    # No single grant can eat the story, and no reachable run of grants can
    # overrun the ceiling it is measured against.
    assert max(granted) < ceiling
    assert sum(granted) <= ceiling


def test_a_story_near_its_deadline_is_granted_only_the_time_it_has_left() -> None:
    """The clamp is what turns a SIGKILL mid-edit into a recorded, costed timeout."""
    grant, audit = clamp_timeout_to_remaining(1800, remaining_seconds=400.0)
    assert grant < 1800
    assert grant <= 400
    assert audit is not None
    assert audit["requested_timeout_seconds"] == 1800
    assert audit["granted_timeout_seconds"] == grant

    # No enclosing budget (standalone ``forge run``) leaves the allowance alone.
    assert clamp_timeout_to_remaining(1800, None) == (1800, None)
    assert cap_timeout_to_story_ceiling(1800, None, 3)[0] == 1800


@pytest.mark.parametrize("remaining", [59.0, 40.0, 1.0])
def test_the_clamp_floor_never_outlives_the_deadline_it_is_bounded_by(remaining: float) -> None:
    """A story with less than the floor left funds less than the floor.

    The floor is a minimum *useful* invocation, not a licence to outlive the
    window: granting a 60s invocation to a story with 40s of working time left
    reintroduces the reported shape — an allowance whose expiry necessarily falls
    outside the budget enclosing it.
    """
    grant, audit = clamp_timeout_to_remaining(1800, remaining_seconds=remaining)

    assert grant <= remaining
    assert audit is not None
    assert audit["floor_capped_by_remaining"] is True
    assert audit["granted_timeout_seconds"] == grant


def test_an_exhausted_deadline_grants_nothing_and_says_so() -> None:
    """At or past the deadline there is no allowance to give, and the audit says it."""
    for remaining in (0.0, -120.0):
        grant, audit = clamp_timeout_to_remaining(1800, remaining_seconds=remaining)
        assert grant == 0
        assert audit is not None
        assert audit["no_working_time_left"] is True


def test_the_static_floor_never_exceeds_the_development_share_of_the_ceiling() -> None:
    """A small ceiling cannot be handed a single-cycle allowance via the floor.

    ``MIN_DEV_INVOCATION_SECONDS`` (900) is larger than the whole development
    share of a 1000s ceiling; without the share bound the floor would grant 90%
    of the story to one invocation — the reported 92% shape, from below.
    """
    ceiling = 1000.0
    final, audit = cap_timeout_to_story_ceiling(4950, ceiling, 3)

    assert final <= int(ceiling * DEV_INVOCATION_STORY_SHARE)
    assert audit["floor_applied"] is True
    assert audit["floor_capped_by_share"] is True
    assert audit["dev_share_seconds"] == int(ceiling * DEV_INVOCATION_STORY_SHARE)


def test_gate_wait_floor_never_outlives_the_deadline_either() -> None:
    """Same pattern, same fix: the gate's minimum offer is cut to what remains."""
    worker_budget.register_worker_budget("story-a", 3600.0, started_at=time.monotonic() - 3590)
    set_worker_slug("story-a")

    allowed = pending.bounded_gate_wait(3600, "ESCALATE")
    assert allowed <= 10.0  # ~10s of working time left, floor notwithstanding
    assert allowed >= 0.0


# ── 2. Operator waits are not worker unresponsiveness ────────────────────────


def test_gate_wait_cannot_exceed_the_story_window_containing_it() -> None:
    """A gate configured equal to the worker timeout is bounded by what is left."""
    worker_budget.register_worker_budget("story-a", 3600.0, started_at=time.monotonic() - 3000)
    set_worker_slug("story-a")

    # Configured 3600s — exactly the base worker timeout, the shape that made the
    # observed gate unanswerable by anyone.
    allowed = pending.bounded_gate_wait(3600, "ESCALATE")
    assert allowed < 3600
    assert allowed <= 600  # cannot outlive the ~600s the story has left

    # Outside a sprint worker there is no enclosing budget and nothing is bounded.
    set_worker_slug("")
    assert pending.bounded_gate_wait(3600, "ESCALATE") == 3600


def test_operator_wait_time_is_credited_back_to_the_story_deadline() -> None:
    """Wait time is excluded from elapsed, so a waiting story keeps its budget."""
    budget = worker_budget.register_worker_budget("story-a", 100.0, started_at=time.monotonic())
    set_worker_slug("story-a")

    with worker_budget.operator_wait("ESCALATE"):
        time.sleep(0.2)
        waiting, label, _waited = worker_budget.waiting_on_operator("story-a")
        assert waiting is True
        assert label == "ESCALATE"
        # Mid-wait, the credit already covers the elapsed wall clock.
        assert worker_budget.operator_wait_credit("story-a") >= 0.2
        assert budget.remaining() > 99.0

    assert worker_budget.waiting_on_operator("story-a")[0] is False
    assert budget.operator_wait_seconds >= 0.2
    assert budget.remaining() > 99.0


def test_batch_members_share_one_window_and_one_operator_wait_credit(tmp_path: Path) -> None:
    """Dispatch seam: a batched group is registered under one shared budget.

    One worker thread serves the whole group under one summed deadline, so a gate
    entered under the leader's slug must credit the deadline every member is
    measured against — otherwise the members the leader is waiting on behalf of
    expire while it waits.
    """
    from tests.test_sprint_batch_groups import _batch_sprint_config, _preflight_states_for
    from tests.test_sprint_resume import _make_coordinator_result
    from tests.test_sprint_resume import _make_manifest as _make_batch_manifest
    from tests.test_sprint_resume import _make_spec_file as _make_batch_spec

    _make_batch_spec(tmp_path, "Bug A", "bug-a")
    _make_batch_spec(tmp_path, "Bug B", "bug-b")
    manifest_path = _make_batch_manifest(tmp_path, ["bug-a.md", "bug-b.md"])
    config = _batch_sprint_config(tmp_path)
    states = _preflight_states_for(
        "bug-a", "bug-b", files={"bug-a": ["src/a.py"], "bug-b": ["src/b.py"]}
    )

    leader_result = _make_coordinator_result(success=True, cost=0.50)
    leader_result.state.workspace_path = tmp_path / "bug-a"
    leader_result.state.branch_name = "forge/bug-a"
    member_result = _make_coordinator_result(success=True, cost=0.10)
    observed: dict = {}

    def _leader_run(*_args, **_kwargs):
        # Runs on the batch worker thread, mid-dispatch: exactly where a gate
        # would open. Both members must already be registered.
        leader = worker_budget.get_worker_budget("bug-a")
        member = worker_budget.get_worker_budget("bug-b")
        assert leader is not None and member is not None
        observed["same_group"] = leader.group == member.group is not None
        observed["same_window"] = leader.worker_timeout_seconds == member.worker_timeout_seconds
        observed["window"] = leader.worker_timeout_seconds
        with worker_budget.operator_wait("ESCALATE", budget=leader):
            time.sleep(0.05)
            observed["member_credited_mid_wait"] = worker_budget.operator_wait_credit("bug-b")
        observed["member_credited_after"] = worker_budget.operator_wait_credit("bug-b")
        return leader_result

    with (
        patch("theforge.sprint.runner.run_batch_preflight", side_effect=lambda *a, **k: states),
        patch("theforge.sprint.runner.run_task", side_effect=_leader_run),
        patch("theforge.sprint.runner.run_review_only", return_value=member_result),
    ):
        result = run_sprint(config, manifest_path)

    assert observed["same_group"] is True
    assert observed["same_window"] is True
    # The shared window is the sum of what the members would each have been
    # allowed on their own, not one member's.
    assert observed["window"] == 2 * config.sprint.worker_timeout_seconds
    # The leader's wait credits the member's deadline both during and after.
    assert observed["member_credited_mid_wait"] >= 0.05
    assert observed["member_credited_after"] >= 0.05
    assert result.specs_total == 2


def test_pending_wait_does_not_expire_the_story_but_working_time_still_does(
    tmp_path: Path, capsys
) -> None:
    """Scheduler seam: a story at a gate outlives its raw deadline; work time does not.

    The scheduler polls twice. Between the polls the story is inside an operator
    wait, and its raw deadline elapses — under the old accounting that killed it
    as "worker unresponsive". Here it survives, and only once the wait is closed
    and working time runs out does the deadline actually expire it.
    """
    _make_spec_file(tmp_path, "Story A", "story-a")
    manifest = tmp_path / "sprint.yaml"
    manifest.write_text(
        yaml.dump(
            {
                "name": "Gated",
                "budget_usd": 5.0,
                "stories": ["story-a.md"],
                "worker_timeout_seconds": 1,
            }
        ),
        encoding="utf-8",
    )
    config = _make_config_with_sprint(tmp_path, sprint_max_parallel=1)

    class _NeverDoneFuture:
        def cancel(self):
            return True

        def result(self):
            raise AssertionError("should not be called")

    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, *args, **kwargs):
            return _NeverDoneFuture()

    polls = {"n": 0}
    survived_the_wait = {"yes": False}

    def _fake_wait(_futs, *, return_when, timeout):
        polls["n"] += 1
        budget = worker_budget.get_worker_budget("story-a")
        assert budget is not None, "the runner must publish the enclosing ceiling"
        if polls["n"] == 1:
            budget.begin_wait("ESCALATE")
            time.sleep(1.3)  # longer than the 1s worker timeout
            return (set(), set())
        # Reaching a second poll at all proves the wait did not expire the story.
        survived_the_wait["yes"] = True
        budget.end_wait()
        time.sleep(1.3)  # now spend real *working* time past the deadline
        return (set(), set())

    with (
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch(
            "theforge.sprint.runner.run_batch_preflight",
            return_value={"story-a": _make_preflight_state("small", 3)},
        ),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
        patch("theforge.sprint.runner.wait", side_effect=_fake_wait),
    ):
        result = run_sprint(config, manifest)

    assert survived_the_wait["yes"] is True, (
        "the story was expired while blocked on an operator decision"
    )
    stderr = capsys.readouterr().err
    assert "operator-decision wait" in stderr
    assert "worker unresponsive" not in stderr
    assert result.results


# ── 3. A timeout outcome is not a quality verdict ────────────────────────────


def test_elapsed_deadline_records_a_timeout_cause_distinct_from_quality_failure(
    tmp_path: Path,
) -> None:
    """The recorded cause names deadline exhaustion and keeps its abnormal kind."""
    from tests.test_sprint_resume import _make_manifest as _make_basic_manifest
    from tests.test_sprint_resume import _make_spec_file as _make_resume_spec

    spec = _make_resume_spec(tmp_path, "Feature A", "feature-a")
    manifest = _make_basic_manifest(tmp_path, [spec.name])
    manifest_data = yaml.safe_load(manifest.read_text())
    manifest_data["worker_timeout_seconds"] = 42
    manifest.write_text(yaml.dump(manifest_data), encoding="utf-8")

    config = _make_config(tmp_path)

    class _NeverDoneFuture:
        def cancel(self):
            return True

        def result(self):
            raise AssertionError("should not be called")

    class _FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, *args, **kwargs):
            return _NeverDoneFuture()

    with (
        patch("theforge.coordinator.workspace.pull_base_branch", return_value=True),
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "duration_seconds": 0.0, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.ThreadPoolExecutor", _FakeExecutor),
        patch("theforge.sprint.runner.wait", return_value=(set(), set())),
        patch("theforge.sprint.runner.time.monotonic", side_effect=[0.0, 50.0, 50.0]),
    ):
        result = run_sprint(config, manifest)

    assert result.results
    _, story_result = result.results[0]
    cause = story_result.state.abnormal_termination
    assert cause is not None
    # Kind survives: an operator triaging this needs "ran out of time", not
    # "produced an unacceptable result" — different actions, and only one of the
    # two is evidence about the work.
    assert cause["kind"] == ABNORMAL_WORKER_TIMEOUT
    assert "deadline exhausted" in cause["cause"].lower()
    assert "unresponsive" not in cause["cause"].lower()
    assert "not a review or quality failure" in story_result.message
    assert "42" in story_result.message
