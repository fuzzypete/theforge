"""Seam tests: a detected topology walk routes to the escalate gate before the
cycle ceiling, and a gate "continue" resumes rather than replays (#2372).

These exercise the REVIEW phase boundary end to end — the real family
classifier, the real detector, the real routing branch — with only the reviewer
pool and the gate itself mocked. Unit tests over the detector live in
``test_review_topology.py``; what is under test here is the control flow: WHEN
the gate is reached, what state the story is in when it gets there, and what
happens on each side of the gate's answer.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.coordinator.review_phase import _ReviewOutcome, _run_review_phase
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.review import ReviewFinding, ReviewResult

# One concern (`unpriced_dispatch`) discovered at a new location every cycle —
# the signature from the spec's real run.
_WALK = [
    (
        "src/routing/dispatch.py",
        10,
        "unpriced_dispatch: seated primaries are dispatched without a price lookup",
    ),
    (
        "src/routing/fallback.py",
        22,
        "unpriced_dispatch: fallback_models are dispatched without a price lookup",
    ),
    (
        "src/routing/transport.py",
        44,
        "unpriced_dispatch: transport_fallback is dispatched without a price lookup",
    ),
    (
        "src/routing/adaptive.py",
        66,
        "unpriced_dispatch: the adaptive pool primary is dispatched without a price lookup",
    ),
]


def _init_repo_with_dev_commit(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "forge/test-task"], cwd=path, check=True)
    (path / "src.py").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: implement"], cwd=path, check=True)


def _review(file: str, line: int, description: str) -> ReviewResult:
    return ReviewResult(
        verdict="REQUEST_CHANGES",
        summary=f"changes needed: {description}",
        findings=[
            ReviewFinding(
                severity="P1",
                file=file,
                line=line,
                observed=description,
                suggestion="Price it at dispatch.",
            )
        ],
        story_matches=True,
        story_mismatches=[],
        test_adequate=True,
        test_gaps=[],
        parse_errors=[],
        raw_yaml={},
    )


def _config(tmp_path: Path, *, max_review_cycles: int):
    config = _make_config(tmp_path)
    return dataclasses.replace(
        config,
        retry=dataclasses.replace(config.retry, max_review_cycles=max_review_cycles),
    )


def _run_cycle(state, config, task, tmp_path, review):
    """Run one REVIEW cycle (the phase advances ``review_cycle`` itself)."""
    with patch(
        "theforge.coordinator.review_phase._run_review_pool",
        side_effect=lambda *_a, **_kw: ([], [], review, [review], [("review", review)]),
    ):
        return _run_review_phase(
            state,
            config,
            task,
            "# spec\n",
            tmp_path,
            "forge/test-task",
            task_start=0.0,
            interactive=False,
            auto_merge=False,
            notify=False,
            logger=None,
        )


def _fresh_state(tmp_path: Path, config) -> CoordinatorState:
    state = CoordinatorState(log_dir=tmp_path / "logs")
    state.run_id = "abcd1234"
    state.budget.max_iterations = config.retry.max_dev_iterations
    return state


class TestGateReachedBeforeCeiling:
    def test_walk_routes_to_the_gate_at_cycle_three_of_five(self, tmp_path):
        _init_repo_with_dev_commit(tmp_path)
        config = _config(tmp_path, max_review_cycles=5)
        task = _make_task(tmp_path)
        state = _fresh_state(tmp_path, config)

        gate = patch(
            "theforge.coordinator.review_phase._run_escalate_gate",
            return_value=CoordinatorResult(
                success=False, phase=Phase.ESCALATE, state=state, message="escalated"
            ),
        )
        with gate as gate_mock:
            # Cycles 1 and 2 are indistinguishable from a converging change: the
            # loop must NOT stop while it cannot tell them apart.
            for file, line, desc in _WALK[:2]:
                outcome, result, config = _run_cycle(
                    state, config, task, tmp_path, _review(file, line, desc)
                )
                assert outcome is _ReviewOutcome.RETRY_DEV
                assert gate_mock.call_count == 0

            file, line, desc = _WALK[2]
            outcome, result, config = _run_cycle(
                state, config, task, tmp_path, _review(file, line, desc)
            )

        # Cycle 3, with the ceiling at 5: two development cycles' worth of
        # budget remain, which is exactly what makes the signal worth having.
        assert gate_mock.call_count == 1
        assert state.review_cycle == 3
        assert state.review_cycle < config.retry.max_review_cycles
        assert outcome is _ReviewOutcome.ESCALATE
        assert result is not None
        assert state.phase is Phase.ESCALATE
        assert state.escalate_kind == "content"
        assert "Topology walk detected" in (state.error or "")
        assert "unpriced_dispatch" in (state.error or "")

    def test_no_fourth_dev_pass_is_consumed_before_the_operator_decides(self, tmp_path):
        _init_repo_with_dev_commit(tmp_path)
        config = _config(tmp_path, max_review_cycles=5)
        task = _make_task(tmp_path)
        state = _fresh_state(tmp_path, config)

        with patch(
            "theforge.coordinator.review_phase._run_escalate_gate",
            return_value=CoordinatorResult(
                success=False, phase=Phase.ESCALATE, state=state, message="escalated"
            ),
        ):
            for file, line, desc in _WALK[:3]:
                outcome, _result, config = _run_cycle(
                    state, config, task, tmp_path, _review(file, line, desc)
                )

        # RETRY_DEV is the outcome that buys another development pass. The
        # escalating cycle does not return it, so no fourth pass is dispatched.
        assert outcome is _ReviewOutcome.ESCALATE
        assert state.retry_reason is None or outcome is not _ReviewOutcome.RETRY_DEV

    def test_work_and_findings_are_preserved_at_the_gate(self, tmp_path):
        _init_repo_with_dev_commit(tmp_path)
        config = _config(tmp_path, max_review_cycles=5)
        task = _make_task(tmp_path)
        state = _fresh_state(tmp_path, config)

        with patch(
            "theforge.coordinator.review_phase._run_escalate_gate",
            return_value=CoordinatorResult(
                success=False, phase=Phase.ESCALATE, state=state, message="escalated"
            ),
        ):
            for file, line, desc in _WALK[:3]:
                _run_cycle(state, config, task, tmp_path, _review(file, line, desc))

        assert len(state.review_results) == 3
        assert state.trajectory_cycle == 3
        assert len(state.review_cycle_findings) == 3
        assert state.finding_registry, "the finding registry must survive the escalation"
        # The triggering cycle is in the history the advisor reads — appended
        # exactly once, so it does not evict an earlier cycle from the window.
        assert [entry.cycle for entry in state.cycle_history] == [1, 2, 3]
        assert state.cycle_history[-1].p1_findings == [_WALK[2][2]]

    def test_signal_is_recorded_on_state_and_names_the_sequence(self, tmp_path):
        _init_repo_with_dev_commit(tmp_path)
        config = _config(tmp_path, max_review_cycles=5)
        task = _make_task(tmp_path)
        state = _fresh_state(tmp_path, config)

        with patch(
            "theforge.coordinator.review_phase._run_escalate_gate",
            return_value=CoordinatorResult(
                success=False, phase=Phase.ESCALATE, state=state, message="escalated"
            ),
        ):
            for file, line, desc in _WALK[:3]:
                _run_cycle(state, config, task, tmp_path, _review(file, line, desc))

        signal = state.review_topology_signal
        assert signal is not None
        assert signal["seed_anchor"] == "unpriced_dispatch"
        assert signal["cycles"] == [1, 2, 3]
        assert [item["file"] for item in signal["sequence"]] == [w[0] for w in _WALK[:3]]
        assert state.review_topology_escalated is True
        # Persisted alongside the trajectory so a --resume neither loses the
        # evidence nor re-escalates a decided pattern.
        sidecar = tmp_path / ".forge" / "trajectory.yaml"
        assert "review_topology_signal" in sidecar.read_text(encoding="utf-8")


class TestGateContinueDoesNotReplay:
    def test_continue_advances_from_the_cycles_already_run(self, tmp_path):
        _init_repo_with_dev_commit(tmp_path)
        config = _config(tmp_path, max_review_cycles=5)
        task = _make_task(tmp_path)
        state = _fresh_state(tmp_path, config)

        with patch(
            "theforge.coordinator.review_phase._run_escalate_gate", return_value=None
        ) as gate_mock:
            for file, line, desc in _WALK[:3]:
                outcome, result, config = _run_cycle(
                    state, config, task, tmp_path, _review(file, line, desc)
                )

        assert gate_mock.call_count == 1
        # Continue re-enters the normal loop: no result to return, dev runs again.
        assert outcome is _ReviewOutcome.RETRY_DEV
        assert result is None
        # The exhausted-cycles branch decrements review_cycle to make room for
        # the granted cycle. This escalation happened WITH budget remaining, so
        # there is nothing to undo — cycle 3 stays run and the next one is 4.
        assert state.review_cycle == 3
        assert state.trajectory_cycle == 3
        assert len(state.review_cycle_findings) == 3
        # The escalation is cleared so the story re-enters REVIEW/RETRY_DEV
        # normally rather than looking like a live escalation.
        assert state.error is None
        assert state.escalate_kind is None
        assert state.last_review_findings
        assert "unpriced_dispatch" in state.last_review_findings

    def test_continue_does_not_re_escalate_the_same_pattern_next_cycle(self, tmp_path):
        _init_repo_with_dev_commit(tmp_path)
        config = _config(tmp_path, max_review_cycles=6)
        task = _make_task(tmp_path)
        state = _fresh_state(tmp_path, config)

        with patch(
            "theforge.coordinator.review_phase._run_escalate_gate", return_value=None
        ) as gate_mock:
            for file, line, desc in _WALK[:4]:
                outcome, _result, config = _run_cycle(
                    state, config, task, tmp_path, _review(file, line, desc)
                )

        # Cycle 4 still walks the topology, but the operator has already been
        # asked and said continue — re-raising it would spend that decision.
        assert gate_mock.call_count == 1
        assert outcome is _ReviewOutcome.RETRY_DEV
        assert state.review_cycle == 4
        assert state.review_topology_signal is not None


class TestSmallCeilingDegradesToExistingBehaviour:
    def test_ceiling_of_three_still_takes_the_exhausted_cycles_branch(self, tmp_path):
        """With the ceiling at 3, cycle 3 IS exhaustion. Detection must not fire
        a second escalation for the same cycle — the existing branch owns it."""
        _init_repo_with_dev_commit(tmp_path)
        config = _config(tmp_path, max_review_cycles=3)
        task = _make_task(tmp_path)
        state = _fresh_state(tmp_path, config)

        with patch(
            "theforge.coordinator.review_phase._run_escalate_gate",
            return_value=CoordinatorResult(
                success=False, phase=Phase.ESCALATE, state=state, message="escalated"
            ),
        ) as gate_mock:
            for file, line, desc in _WALK[:3]:
                outcome, _result, config = _run_cycle(
                    state, config, task, tmp_path, _review(file, line, desc)
                )

        assert gate_mock.call_count == 1
        assert outcome is _ReviewOutcome.ESCALATE
        assert state.review_topology_escalated is False
        assert "Max cycles" in (state.error or "")
        # The signal is still computed and recorded — the operator gets the
        # evidence even when the ceiling, not the detector, was the trigger.
        assert state.review_topology_signal is not None
