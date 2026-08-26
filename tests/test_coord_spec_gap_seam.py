"""Seam test: the DEV phase can raise a specification gap and pause for an answer (#2122).

The boundary under test is the dev→coordinator→operator→dev round trip. A dev
agent that hits an underspecified acceptance criterion emits ``<forge_spec_gap>``
instead of guessing; the coordinator opens a pending operator decision *at that
moment*, records how it resolved, injects the resolution into the next dev
prompt, and re-enters DEV without spending a review cycle on the gap.

Unit coverage for the block parser lives in ``test_spec_gap_parser.py``; what is
proven here is the cross-phase handoff — prompt → pause → audit → resume.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    _gate_side_effect,
    _make_agent_result,
    _make_config,
    _make_task,
    patch_gate_shell,
)

from theforge import pending as _pending
from theforge.config.types import NotificationConfig, RetryPolicy
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.resume_persistence import load_spec_gap_resolutions

CRITERION = "Ending a workout marks the linked Polar session ended"
UNDEFINED = "no correlated Polar session exists for the workout"
ASSUMPTION = "leave the workout unmarked and attach nothing"

SPEC_GAP_OUTPUT = f"""\
I cannot implement this criterion without a decision.

<forge_spec_gap>
criterion: "{CRITERION}"
undefined_case: "{UNDEFINED}"
assumption: "{ASSUMPTION}"
options_considered:
  - "leave unmarked"
  - "attach nearest by time"
</forge_spec_gap>
"""


def _config(tmp_path: Path, *, allowance: int = 1, timeout: int = 30):
    base = _make_config(tmp_path)
    return dataclasses.replace(
        base,
        retry=RetryPolicy(
            max_dev_iterations=3,
            max_review_cycles=2,
            max_spec_gap_pauses=allowance,
        ),
        notifications=NotificationConfig(human_review_timeout_seconds=timeout),
    )


def _dev_agent(outputs: list[str]):
    """Fake dev runner returning ``outputs`` in order; records every prompt."""
    seen: dict[str, list] = {"prompts": []}
    idx = {"n": 0}

    def run_agent(*, prompt, **kwargs):
        seen["prompts"].append(prompt)
        i = min(idx["n"], len(outputs) - 1)
        idx["n"] += 1
        return _make_agent_result(output=outputs[i])

    run_agent.seen = seen  # type: ignore[attr-defined]
    return run_agent


class _Answerer(threading.Thread):
    """Stand-in operator: waits for the SPEC_GAP pending file and answers it."""

    def __init__(self, project_root: Path, answer: str, *, deadline: float = 20.0):
        super().__init__(daemon=True)
        self.project_root = project_root
        self.answer = answer
        self.deadline = deadline
        self.answered = False
        self.seen_entry: dict | None = None

    def run(self) -> None:
        end = time.monotonic() + self.deadline
        while time.monotonic() < end:
            for entry in _pending.list_pending(self.project_root):
                if entry.get("phase") != "SPEC_GAP" or entry.get("decision"):
                    continue
                self.seen_entry = dict(entry)
                run_id = str(entry.get("run_id") or "")
                if _pending.resolve_pending(run_id, self.answer, self.project_root):
                    self.answered = True
                    return
            time.sleep(0.05)


def _run(tmp_path: Path, dev_outputs: list[str], *, config=None, answerer=None):
    config = config or _config(tmp_path)
    task = _make_task(tmp_path)
    workspace = tmp_path / "test-task"
    workspace.mkdir(exist_ok=True)
    agent = _dev_agent(dev_outputs)
    if answerer is not None:
        answerer.start()
    with (
        patch_gate_shell(side_effect=_gate_side_effect(workspace, "PASS")),
        patch("theforge.coordinator.dev_phase.run_agent", side_effect=agent),
        patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
        patch(
            "theforge.coordinator.review_pool.run_agent_pool",
            return_value=[
                _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
            ],
        ),
    ):
        result = run_task(config, task)
    if answerer is not None:
        answerer.join(timeout=5)
    return result, agent.seen, config, task  # type: ignore[attr-defined]


def _resolutions(result):
    return result.state.spec_gap_resolutions


class TestAnsweredGap:
    def test_run_pauses_gets_an_answer_and_finishes_without_a_review_cycle_for_the_gap(
        self, tmp_path
    ):
        answerer = _Answerer(tmp_path, "leave unmarked; do not attach an uncorrelated session")
        result, seen, _config_used, _task = _run(
            tmp_path,
            [SPEC_GAP_OUTPUT, "Done."],
            answerer=answerer,
        )

        assert answerer.answered, "the SPEC_GAP pending file never appeared"
        assert result.success is True

        (resolution,) = _resolutions(result)
        assert resolution["source"] == "operator"
        assert resolution["answer"] == "leave unmarked; do not attach an uncorrelated session"
        assert resolution["criterion"] == CRITERION
        assert resolution["undefined_case"] == UNDEFINED

        # The gap itself cost no review cycle: exactly one review ran, on the
        # work the answered dev iteration produced.
        assert result.state.reviewer_cycles_run == 1
        assert result.state.review_cycle == 1
        assert result.state.validate_opened_review_cycles == 0
        assert result.state.review_cycles_spent == 1

    def test_the_pause_is_reachable_before_any_review_cycle_is_consumed(self, tmp_path):
        """The gate opens at the moment of ambiguity, not after review exhausts."""
        observed: dict = {}

        answerer = _Answerer(tmp_path, "leave unmarked")
        original = _pending.write_pending

        def spy(**kwargs):
            if kwargs.get("phase") == "SPEC_GAP":
                observed["reviews_run_when_gate_opened"] = _reviews["n"]
            return original(**kwargs)

        _reviews = {"n": 0}
        pool_result = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        def counting_pool(*args, **kwargs):
            _reviews["n"] += 1
            return pool_result

        config = _config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir(exist_ok=True)
        agent = _dev_agent([SPEC_GAP_OUTPUT, "Done."])
        answerer.start()
        with (
            patch_gate_shell(side_effect=_gate_side_effect(workspace, "PASS")),
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=agent),
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch("theforge.coordinator.review_pool.run_agent_pool", side_effect=counting_pool),
            patch("theforge.pending.write_pending", side_effect=spy),
        ):
            result = run_task(config, task)
        answerer.join(timeout=5)

        assert result.success is True
        assert observed["reviews_run_when_gate_opened"] == 0

    def test_the_answer_reaches_the_next_dev_prompt(self, tmp_path):
        answerer = _Answerer(tmp_path, "leave unmarked; do not attach an uncorrelated session")
        _result, seen, _config_used, _task = _run(
            tmp_path, [SPEC_GAP_OUTPUT, "Done."], answerer=answerer
        )

        assert len(seen["prompts"]) >= 2
        first, second = seen["prompts"][0], seen["prompts"][1]
        # The channel was offered before the gap was raised...
        assert "## Raising a Specification Gap" in first
        assert "Resolved Specification Gaps" not in first
        # ...and the answer travels into the iteration that resumes.
        assert "## Resolved Specification Gaps" in second
        assert "do not attach an uncorrelated session" in second
        assert CRITERION in second

    def test_the_gap_and_its_answer_are_in_the_run_audit(self, tmp_path):
        answerer = _Answerer(tmp_path, "leave unmarked")
        result, _seen, config, task = _run(tmp_path, [SPEC_GAP_OUTPUT, "Done."], answerer=answerer)
        record = generate_audit_log(config, task, result)

        block = record["spec_gaps"]
        assert block["allowance"] == 1
        assert block["pauses_used"] == 1
        (event,) = block["events"]
        assert event["criterion"] == CRITERION
        assert event["gated"] is True
        (resolution,) = block["resolutions"]
        assert resolution["source"] == "operator"
        assert resolution["answer"] == "leave unmarked"
        assert resolution["assumption"] == ASSUMPTION

    def test_the_answer_is_durable_before_the_pending_file_is_removed(self, tmp_path):
        """A crash at the gate boundary must not erase an answer already given."""
        seen_at_cleanup: dict = {}
        original_cleanup = _pending.cleanup_pending

        def spy_cleanup(run_id, project_root=None):
            seen_at_cleanup["stored"] = load_spec_gap_resolutions(tmp_path, "test-task")
            return original_cleanup(run_id, project_root)

        answerer = _Answerer(tmp_path, "leave unmarked")
        config = _config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir(exist_ok=True)
        agent = _dev_agent([SPEC_GAP_OUTPUT, "Done."])
        answerer.start()
        with (
            patch_gate_shell(side_effect=_gate_side_effect(workspace, "PASS")),
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=agent),
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
                return_value=[
                    _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
                ],
            ),
            patch("theforge.pending.cleanup_pending", side_effect=spy_cleanup),
        ):
            run_task(config, task)
        answerer.join(timeout=5)

        # The resolution is on disk the moment the pending checkpoint goes away.
        assert [entry["answer"] for entry in seen_at_cleanup["stored"]] == ["leave unmarked"]
        stored = load_spec_gap_resolutions(tmp_path, "test-task")
        assert [entry["answer"] for entry in stored] == ["leave unmarked"]


class TestUnansweredAndBoundedGaps:
    def test_an_unanswered_pause_resolves_by_the_recorded_assumption(self, tmp_path):
        result, seen, _config_used, _task = _run(
            tmp_path,
            [SPEC_GAP_OUTPUT, "Done."],
            config=_config(tmp_path, timeout=1),
        )

        assert result.success is True
        (resolution,) = _resolutions(result)
        assert resolution["source"] == "no_answer"
        assert resolution["answer"] is None
        assert resolution["assumption"] == ASSUMPTION
        assert resolution["gated"] is True
        # No pause blocks a run indefinitely, and none is discarded silently.
        assert _pending.list_pending(tmp_path) == []
        assert "the pause expired without one" in seen["prompts"][1]
        assert ASSUMPTION in seen["prompts"][1]

    def test_an_exhausted_allowance_proceeds_under_the_assumption_without_pausing(self, tmp_path):
        """A second gap on a one-pause run is answered by the agent's assumption."""
        gates_opened: list[str] = []
        original = _pending.write_pending

        def spy(**kwargs):
            if kwargs.get("phase") == "SPEC_GAP":
                gates_opened.append(kwargs["run_id"])
            return original(**kwargs)

        second_gap = SPEC_GAP_OUTPUT.replace(UNDEFINED, "a second undefined case")
        config = _config(tmp_path, allowance=1, timeout=1)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir(exist_ok=True)
        agent = _dev_agent([SPEC_GAP_OUTPUT, second_gap, "Done."])
        with (
            patch_gate_shell(side_effect=_gate_side_effect(workspace, "PASS")),
            patch("theforge.coordinator.dev_phase.run_agent", side_effect=agent),
            patch("theforge.coordinator.preflight_flow.run_agent", return_value=_PREFLIGHT_RESULT),
            patch(
                "theforge.coordinator.review_pool.run_agent_pool",
                return_value=[
                    _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
                ],
            ),
            patch("theforge.pending.write_pending", side_effect=spy),
        ):
            result = run_task(config, task)

        assert len(gates_opened) == 1, "the second gap must not open another pause"
        sources = [entry["source"] for entry in _resolutions(result)]
        assert sources == ["no_answer", "allowance_exhausted"]
        exhausted = _resolutions(result)[1]
        assert exhausted["gated"] is False
        assert exhausted["assumption"] == ASSUMPTION
        assert result.state.spec_gap_pauses_used == 1

    def test_a_zero_allowance_never_offers_the_channel(self, tmp_path):
        config = _config(tmp_path, allowance=0)
        _result, seen, _config_used, _task = _run(tmp_path, ["Done."], config=config)
        assert "## Raising a Specification Gap" not in seen["prompts"][0]

    def test_a_malformed_block_does_not_stall_the_run(self, tmp_path):
        """An unreadable ask is no question at all — record it and carry on."""
        malformed = "<forge_spec_gap>\ncriterion: only\n</forge_spec_gap>"
        result, _seen, _config_used, _task = _run(tmp_path, [malformed, "Done."])

        assert result.success is True
        assert _resolutions(result) == []
        (event,) = result.state.spec_gap_events
        assert "missing required keys" in event["parse_error"]
        assert _pending.list_pending(tmp_path) == []


class TestResolutionsSurviveIntoLaterRuns:
    def test_a_later_fresh_run_of_the_same_story_receives_the_earlier_answer(self, tmp_path):
        answerer = _Answerer(tmp_path, "leave unmarked; do not attach an uncorrelated session")
        _run(tmp_path, [SPEC_GAP_OUTPUT, "Done."], answerer=answerer)
        assert answerer.answered

        # A brand new run: fresh CoordinatorState, no resume, same story.
        second_result, second_seen, _config_used, _task = _run(tmp_path, ["Done."])

        assert "## Resolved Specification Gaps" in second_seen["prompts"][0]
        assert "do not attach an uncorrelated session" in second_seen["prompts"][0]
        assert [entry["source"] for entry in _resolutions(second_result)] == ["operator"]
        # And the second run never re-asked.
        assert second_result.state.spec_gap_pauses_used == 0

    def test_a_changed_story_does_not_inherit_the_old_answer(self, tmp_path):
        """The answer settles a criterion; different story text, different criterion."""
        answerer = _Answerer(tmp_path, "leave unmarked")
        _run(tmp_path, [SPEC_GAP_OUTPUT, "Done."], answerer=answerer)
        assert answerer.answered

        (tmp_path / "spec.md").write_text("# Test Spec\n\nSomething else entirely.", "utf-8")
        assert (
            load_spec_gap_resolutions(
                tmp_path, "test-task", story_content="# Test Spec\n\nSomething else entirely."
            )
            == []
        )
