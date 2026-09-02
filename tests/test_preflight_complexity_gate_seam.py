"""Seam coverage for the preflight complexity gate (#2681).

The gate sits on a phase boundary: it is called from the one preflight-verdict
handoff, and what it decides then travels into the sprint's outcome
classification, the operator surfaces, the resume record, and the run audit.
Unit coverage of the gate alone would not catch a story that stops correctly and
is then reported as a failure, or one that pauses and blocks its siblings —
which are the two ways this feature fails in a way that matters.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from coord_test_helpers import (
    _make_agent_result,
    _make_config,
    _make_task,
    patch_gate_shell,
)

from theforge import pending
from theforge.cli.status import _preflight_gate_lines
from theforge.config import RetryPolicy, normalize_preflight_gate_no_decision
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight_complexity_gate import (
    PREFLIGHT_GATE_EXTRA_KEY,
    evaluate_preflight_complexity_gate,
    should_gate,
)
from theforge.coordinator.preflight_flow import _handle_preflight_verdict
from theforge.coordinator.resume_persistence import (
    apply_resume_record_to_state,
    load_resume_record,
    save_resume_record,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.dag import StoryDAG
from theforge.sprint.runner import _classify_and_record
from theforge.sprint.story_state import SprintStoryState, StoryOutcome


def _preflight_output(score: int, verdict: str = "PROCEED", criteria: bool = True) -> str:
    """A preflight result at *score*.

    ``criteria_checked`` is populated by default, but tests may pass
    ``criteria=False`` to cover the conservative no-evidence path that still
    opens the gate and must therefore remain visible to the operator.
    """
    checked = (
        """
criteria_checked:
  - criterion: "The gate opens at the configured threshold"
    files_checked: ["src/theforge/coordinator/preflight_complexity_gate.py"]
    runtime_path: "preflight verdict handoff"
    satisfied: false
    evidence: "Examined the verdict handoff and the gate module."
"""
        if criteria
        else "\ncriteria_checked: []\n"
    )
    return f"""\
```yaml
verdict: {verdict}
complexity: large
complexity_score: {score}
scope_exceeded: false
work_type: feature
sufficiency: needs_planning
reason: "A sizeable but well-formed story."
{checked.strip()}
```
"""


def _gated_state(
    score: int = 9,
    implementation: int = 9,
    validation: int = 3,
    *,
    founded: bool = True,
    degraded: bool = False,
) -> CoordinatorState:
    state = CoordinatorState()
    state.run_id = "7c1e04b9d3af"
    state.preflight_verdict = "PROCEED"
    state.preflight_complexity = "large"
    state.preflight_complexity_score = score
    state.preflight_implementation_complexity_score = implementation
    state.preflight_validation_complexity_score = validation
    state.preflight_degraded = degraded
    # Foundedness still matters for operator context and audit visibility even
    # though it no longer narrows the trigger.
    state.preflight_criteria_checked = (
        [{"criterion": "Sized against the codebase", "satisfied": False}] if founded else []
    )
    return state


def _config_with(tmp_path: Path, **retry_fields: object) -> object:
    config = _make_config(tmp_path)
    from dataclasses import replace  # noqa: PLC0415

    threshold = RetryPolicy().preflight_complexity_gate_threshold
    merged_retry_fields = {"preflight_complexity_gate_threshold": threshold, **retry_fields}
    return replace(
        config,
        retry=replace(
            config.retry,
            **merged_retry_fields,
        ),
    )


def _answer_with(action: str):
    """A ``poll_pending`` stand-in that records ``action`` the way an operator would."""

    def _poll(run_id, timeout_seconds, **kwargs):
        pending.resolve_pending(run_id, action, kwargs.get("project_root"))
        return action, "2026-01-01T00:00:00+00:00"

    return _poll


def _never_answered(run_id, timeout_seconds, **kwargs):
    """A gate nobody answered: the poller reports the expiry, the record stands."""
    return "timeout", None


# ── The gate opens where the money starts ────────────────────────────────


class TestGateOpensAtTheBoundary:
    @patch("theforge.pending.poll_pending", side_effect=_never_answered)
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_score_at_default_threshold_stops_before_plan_and_dev(
        self, mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, _poll, tmp_path
    ):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / "test-task").mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_preflight_output(9), cost_usd=0.37
        )

        result = run_task(config, task)

        # The shipped configuration emits the pause and, unanswered, returns the
        # story — without paying for planning or dev.
        assert result.state.preflight_complexity_gate_opened is True
        assert result.state.preflight_complexity_gate_decision == "decompose"
        assert result.state.preflight_complexity_gate_decision_source == "no_decision"
        assert result.state.preflight_complexity_gate_threshold == 9
        assert result.phase is Phase.PREFLIGHT
        assert result.success is False
        assert "decomposition" in result.message
        mock_plan.assert_not_called()
        mock_dev.assert_not_called()
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_score_below_threshold_never_pauses(
        self, mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
    ):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / "test-task").mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_preflight_output(8), cost_usd=0.37
        )
        mock_plan.return_value = _make_agent_result(success=True, output="Plan.")
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = []

        with patch("theforge.pending.write_pending") as mock_write:
            result = run_task(config, task)

        mock_write.assert_not_called()
        assert result.state.preflight_complexity_gate_opened is False
        assert result.state.preflight_complexity_gate_decision is None
        mock_dev.assert_called()

    def test_a_degraded_preflight_still_opens_the_gate(self, tmp_path: Path):
        config = _config_with(tmp_path)
        state = _gated_state(score=10, founded=False, degraded=True)
        state.preflight_degraded_reason = "timeout_no_verdict"

        assert should_gate(state, config, "PROCEED") is True

    def test_a_preflight_that_examined_nothing_still_opens_the_gate(self, tmp_path: Path):
        config = _config_with(tmp_path)

        assert should_gate(_gated_state(score=9, founded=False), config, "PROCEED") is True

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_unfounded_high_score_still_pauses_and_records_its_provenance(
        self, mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
    ):
        """End to end: the gate still opens, and the operator sees why."""
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / "test-task").mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_preflight_output(9, criteria=False), cost_usd=0.37
        )
        with patch("theforge.pending.poll_pending", side_effect=_never_answered):
            result = run_task(config, task)

        assert result.state.preflight_complexity_gate_opened is True
        assert result.state.preflight_complexity_gate_decision == "decompose"
        # Foundedness is context, not a suppression: the gate opened anyway, and
        # the provenance the operator ruled on is on the state for the audit.
        assert (
            result.state.preflight_complexity_gate_score_provenance
            == "preflight examined no criteria"
        )
        mock_plan.assert_not_called()
        mock_dev.assert_not_called()
        mock_pool.assert_not_called()

    def test_gate_is_disabled_by_a_threshold_above_the_highest_score(self, tmp_path: Path):
        """No enable switch exists: raising the threshold past 10 is how it turns off."""
        config = _config_with(tmp_path, preflight_complexity_gate_threshold=11)

        assert should_gate(_gated_state(score=10), config, "PROCEED") is False

    def test_shipped_configuration_gates_at_nine(self, tmp_path: Path):
        config = _config_with(tmp_path)

        assert RetryPolicy().preflight_complexity_gate_threshold == 9
        assert should_gate(_gated_state(score=9), config, "PROCEED") is True
        assert should_gate(_gated_state(score=8), config, "PROCEED") is False

    @pytest.mark.parametrize("verdict", ["ALREADY_DONE", "BLOCKED", "NO_JUDGMENT"])
    def test_non_proceed_verdicts_never_gate(self, verdict: str, tmp_path: Path):
        config = _config_with(tmp_path)
        state = _gated_state(score=10)
        state.preflight_verdict = verdict

        assert should_gate(state, config, verdict) is False

    @patch("theforge.pending.write_pending")
    def test_a_blocked_verdict_dispatches_without_writing_a_gate(self, mock_write, tmp_path: Path):
        """Anchored at the handoff, but only a PROCEED can reach the pause."""
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _gated_state(score=10)
        state.preflight_verdict = "BLOCKED"

        _config, result, _loop = _handle_preflight_verdict(
            verdict="BLOCKED",
            reason="Blocked for a real reason.",
            state=state,
            config=config,
            task=task,
            branch_name="forge/test-task",
            notify=False,
            logger=None,
            task_start=0.0,
        )

        mock_write.assert_not_called()
        assert result is not None and result.phase is Phase.ESCALATE

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_approve_continues_exactly_as_it_would_have(
        self, mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
    ):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / "test-task").mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_preflight_output(9), cost_usd=0.37
        )
        mock_plan.return_value = _make_agent_result(success=True, output="Plan.")
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_pool.return_value = []

        with patch("theforge.pending.poll_pending", side_effect=_answer_with("approve")):
            result = run_task(config, task)

        assert result.state.preflight_complexity_gate_decision == "approve"
        assert result.state.preflight_complexity_gate_decision_source == "operator"
        assert result.state.preflight_complexity_gate_score == 9
        # The run went on to implement the story, which is the whole content
        # of "continues exactly as it would have without the gate".
        mock_dev.assert_called()


# ── What the operator is shown, and what resolves it ─────────────────────


class TestOperatorSurface:
    def test_pending_record_is_keyed_by_the_story_run_id_and_carries_both_axes(
        self, tmp_path: Path
    ):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _gated_state()
        captured: dict = {}

        real_write = pending.write_pending

        def _capture(**kwargs):
            path = real_write(**kwargs)
            captured.update(kwargs)
            return path

        with (
            patch("theforge.pending.write_pending", side_effect=_capture),
            patch("theforge.pending.poll_pending", side_effect=_never_answered),
        ):
            evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        assert captured["run_id"] == "7c1e04b9d3af"
        assert captured["phase"] == "PREFLIGHT"
        # cmd_decide validates against this list, so the two actions an operator
        # may pick are exactly the two the no-decision policy accepts.
        assert captured["options"] == ["approve", "decompose"]
        payload = captured["extra"][PREFLIGHT_GATE_EXTRA_KEY]
        assert payload["complexity_score"] == 9
        assert payload["implementation_complexity_score"] == 9
        assert payload["validation_complexity_score"] == 3
        assert payload["threshold"] == 9
        assert payload["score_founded"] is True
        assert f"forge decide {state.run_id} approve" in captured["reason"]
        assert f"forge decide {state.run_id} decompose" in captured["reason"]

    def test_pending_record_carries_unfounded_score_provenance(self, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _gated_state(founded=False, degraded=True)
        state.preflight_degraded_reason = "timeout_no_verdict"
        captured: dict = {}

        real_write = pending.write_pending

        def _capture(**kwargs):
            path = real_write(**kwargs)
            captured.update(kwargs)
            return path

        with (
            patch("theforge.pending.write_pending", side_effect=_capture),
            patch("theforge.pending.poll_pending", side_effect=_never_answered),
        ):
            evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        payload = captured["extra"][PREFLIGHT_GATE_EXTRA_KEY]
        assert payload["score_founded"] is False
        assert payload["score_provenance_note"] == "degraded preflight (timeout_no_verdict)"
        assert "Score provenance: degraded preflight (timeout_no_verdict)." in captured["reason"]

    def test_status_renders_the_scores_the_decision_turns_on(self):
        entry = {
            "run_id": "7c1e04b9d3af",
            "options": ["approve", "decompose"],
            PREFLIGHT_GATE_EXTRA_KEY: {
                "complexity_score": 9,
                "implementation_complexity_score": 9,
                "validation_complexity_score": 3,
                "threshold": 9,
                "no_decision_action": "decompose",
                "no_decision_fallback": None,
                "score_provenance_note": "preflight examined no criteria",
            },
        }

        rendered = "\n".join(_preflight_gate_lines(entry, "7c1e04b9d3af"))

        assert "complexity 9 (impl 9, validation 3)" in rendered
        assert "threshold 9" in rendered
        assert "forge decide 7c1e04b9d3af approve" in rendered
        assert "forge decide 7c1e04b9d3af decompose" in rendered
        assert "on timeout: decompose" in rendered
        assert "score provenance: preflight examined no criteria" in rendered

    def test_a_non_gate_pending_record_renders_nothing_extra(self):
        assert _preflight_gate_lines({"run_id": "abc", "options": []}, "abc") == []


# ── Fail-closed on silence and on misconfiguration ───────────────────────


class TestNoDecisionIsFailClosed:
    @pytest.mark.parametrize(
        "configured",
        [None, "", "   ", "aprove", "escalate", "true"],
    )
    def test_unusable_configuration_returns_the_story(self, configured):
        action, fallback = normalize_preflight_gate_no_decision(configured)

        assert action == "decompose"
        assert fallback is not None

    @pytest.mark.parametrize("configured", ["approve", "decompose", "DECOMPOSE", " approve "])
    def test_the_two_accepted_values_are_taken_as_written(self, configured):
        action, fallback = normalize_preflight_gate_no_decision(configured)

        assert action == configured.strip().lower()
        assert fallback is None

    def test_an_expired_gate_with_broken_config_does_not_proceed(self, tmp_path: Path):
        config = _config_with(tmp_path, preflight_complexity_gate_no_decision="yes please")
        task = _make_task(tmp_path)
        state = _gated_state()

        with patch("theforge.pending.poll_pending", side_effect=_never_answered):
            result = evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        assert result is not None and result.success is False
        assert state.preflight_complexity_gate_decision == "decompose"
        assert state.preflight_complexity_gate_decision_source == "no_decision"
        assert state.preflight_complexity_gate_no_decision_fallback is not None

    def test_a_project_may_configure_approve_on_expiry(self, tmp_path: Path):
        config = _config_with(tmp_path, preflight_complexity_gate_no_decision="approve")
        task = _make_task(tmp_path)
        state = _gated_state()

        with patch("theforge.pending.poll_pending", side_effect=_never_answered):
            result = evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        assert result is None
        assert state.preflight_complexity_gate_decision == "approve"
        # Still recorded as nobody's decision — an applied default is not an
        # operator saying yes.
        assert state.preflight_complexity_gate_decision_source == "no_decision"


# ── Returned is not failed ───────────────────────────────────────────────


class TestReturnedIsNotFailed:
    def test_sprint_classifies_it_as_returned_for_decomposition(self, tmp_path: Path):
        task = _make_task(tmp_path)
        state = _gated_state()
        state.preflight_complexity_gate_decision = "decompose"
        result = CoordinatorResult(
            success=False, phase=Phase.PREFLIGHT, state=state, message="returned"
        )
        dag = StoryDAG([task])
        stories = SprintStoryState()
        stories.register(task.slug, str(task.story_path))

        outcome = _classify_and_record(task, result, dag, set(), story_state=stories)

        assert outcome is StoryOutcome.DECOMPOSED
        assert outcome.is_failed is False
        assert outcome.is_succeeded is False
        assert outcome.is_terminal is True
        entry = stories.get(task.slug)
        assert entry is not None
        assert entry.as_dict()["status"] == "decomposed"
        assert stories.counts()["failed"] == 0

    def test_the_audit_says_returned_rather_than_only_not_successful(self, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _gated_state()
        state.started_at = "2026-01-01T00:00:00+00:00"
        state.preflight_complexity_gate_opened = True
        state.preflight_complexity_gate_decision = "decompose"
        state.preflight_complexity_gate_decision_source = "operator"
        state.preflight_complexity_gate_threshold = 9
        state.preflight_complexity_gate_score = 9
        state.preflight_complexity_gate_implementation_score = 9
        state.preflight_complexity_gate_validation_score = 3

        record = generate_audit_log(
            config,
            task,
            CoordinatorResult(
                success=False, phase=Phase.PREFLIGHT, state=state, message="returned"
            ),
        )

        gate = record["preflight_complexity_gate"]
        assert gate["opened"] is True
        assert gate["decision"] == "decompose"
        assert gate["decision_source"] == "operator"
        assert gate["threshold"] == 9
        assert gate["implementation_complexity_score"] == 9
        assert gate["validation_complexity_score"] == 3
        assert record["outcome"]["returned_for_decomposition"] is True

    def test_an_ungated_run_records_the_gate_as_unopened(self, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        state.started_at = "2026-01-01T00:00:00+00:00"

        record = generate_audit_log(
            config,
            task,
            CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done"),
        )

        assert record["preflight_complexity_gate"]["opened"] is False
        assert record["outcome"]["returned_for_decomposition"] is False


# ── State handoff ────────────────────────────────────────────────────────


class TestDecisionSurvivesResume:
    def test_resume_record_round_trips_the_decision(self, tmp_path: Path):
        saved = _gated_state()
        saved.story_content = "# Test Spec"
        saved.preflight_complexity_gate_opened = True
        saved.preflight_complexity_gate_decision = "approve"
        saved.preflight_complexity_gate_decision_source = "operator"
        saved.preflight_complexity_gate_threshold = 9
        saved.preflight_complexity_gate_score = 9

        assert save_resume_record(tmp_path, saved, slug="test-task") is not None
        record = load_resume_record(tmp_path, "test-task")
        assert record is not None

        resumed = CoordinatorState()
        apply_resume_record_to_state(resumed, record)

        assert resumed.preflight_complexity_gate_decision == "approve"
        assert resumed.preflight_complexity_gate_decision_source == "operator"
        assert resumed.preflight_complexity_gate_threshold == 9

    @patch("theforge.pending.write_pending")
    def test_a_recorded_approval_is_not_asked_again(self, mock_write, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _gated_state()
        state.preflight_complexity_gate_decision = "approve"
        state.preflight_complexity_gate_decision_source = "operator"

        assert evaluate_preflight_complexity_gate(state, config, task, "PROCEED") is None
        mock_write.assert_not_called()

    @patch("theforge.pending.write_pending")
    def test_a_recorded_decomposition_still_stops_the_run(self, mock_write, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _gated_state()
        state.preflight_complexity_gate_decision = "decompose"
        state.preflight_complexity_gate_decision_source = "operator"

        result = evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        assert result is not None and result.success is False
        mock_write.assert_not_called()


# ── One story's pause is not the sprint's ────────────────────────────────


class TestSiblingStoriesAreNotBlocked:
    def test_a_waiting_gate_does_not_hold_up_an_unrelated_story(self, tmp_path: Path):
        """The gate must own nothing a sibling worker needs to make progress.

        A sprint runs each story in its own worker, so this asserts the property
        that makes that work: a story parked in the gate holds no lock, so a
        second story reaches its own gate decision while the first is still
        waiting. A shared lock anywhere on this path would deadlock the test.
        """
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        held = threading.Event()
        release = threading.Event()

        def _blocking_poll(run_id, timeout_seconds, **kwargs):
            held.set()
            release.wait(timeout=10)
            return "timeout", None

        waiting_state = _gated_state()
        waiting_state.run_id = "story-a"
        sibling_state = _gated_state()
        sibling_state.run_id = "story-b"

        with patch("theforge.pending.poll_pending", side_effect=_blocking_poll):
            waiter = threading.Thread(
                target=evaluate_preflight_complexity_gate,
                args=(waiting_state, config, task, "PROCEED"),
                daemon=True,
            )
            waiter.start()
            assert held.wait(timeout=10), "the first story never reached its gate"

            # Story B is below the threshold: it must resolve immediately, while
            # story A is still parked at its pause.
            below = _gated_state(score=8, implementation=8)
            below.run_id = "story-b"
            assert evaluate_preflight_complexity_gate(below, config, task, "PROCEED") is None

            release.set()
            waiter.join(timeout=10)

        assert waiting_state.preflight_complexity_gate_decision == "decompose"
        assert below.preflight_complexity_gate_decision is None


# ── The pause carries a decomposition assessment (#2686) ─────────────────


_ASSESSMENT_OUTPUT = """\
<decomposition_assessment>
atomic: false
slices:
  - id: 1
    title: "Parser and data contract"
    scope: "The pure-data types and the parser. Excludes every call site."
    depends_on: []
    covers_criteria: [1]
  - id: 2
    title: "Wire it at the gate"
    scope: "Only the call site and its failure handling."
    depends_on: [1]
    covers_criteria: [2]
unsettled:
  - "Whether criterion 2 belongs with slice 2 or its own slice."
</decomposition_assessment>
"""

_ATOMIC_OUTPUT = """\
<decomposition_assessment>
atomic: true
atomic_reason: "One indivisible schema change; every criterion depends on it."
</decomposition_assessment>
"""

_STORY_WITH_CRITERIA = """\
# A large story

## Acceptance criteria

- the parser validates its output
- the gate renders the artifact
"""


def _assessment_agent(output: str = _ASSESSMENT_OUTPUT, *, cost_usd=0.11, success: bool = True):
    """Patch the one agent the assessment invokes, and count the invocations."""
    return patch(
        "theforge.coordinator.preflight_decomposition_flow.run_agent",
        return_value=_make_agent_result(success=success, output=output, cost_usd=cost_usd),
    )


def _assessed_state(**kwargs) -> CoordinatorState:
    state = _gated_state(**kwargs)
    state.story_content = _STORY_WITH_CRITERIA
    return state


class TestThePauseCarriesAnAssessment:
    def test_the_pending_record_carries_the_slices_edges_and_coverage(self, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _assessed_state()
        captured: dict = {}

        real_write = pending.write_pending

        def _capture(**kwargs):
            path = real_write(**kwargs)
            captured.update(kwargs)
            return path

        with (
            _assessment_agent(),
            patch("theforge.pending.write_pending", side_effect=_capture),
            patch("theforge.pending.poll_pending", side_effect=_never_answered),
        ):
            evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        # The prose an operator reads at the pause, alongside the question.
        reason = captured["reason"]
        assert "Decomposition assessment (2 candidate slices)" in reason
        assert "1. Parser and data contract — scope:" in reason
        assert "depends_on: 1" in reason
        assert "covers AC 1" in reason
        assert "Unsettled:" in reason
        assert "forge decide 7c1e04b9d3af decompose" in reason

        # And the same thing as data, so status/notification surfaces do not
        # have to parse the prose.
        payload = captured["extra"][PREFLIGHT_GATE_EXTRA_KEY]
        assert payload["assessment_generated"] is True
        assert payload["assessment"]["slices"][1]["depends_on"] == [1]
        assert payload["assessment"]["unsettled"]
        assert payload["assessment_none_reason"] is None
        assert payload["assessment_cost_usd"] == 0.11

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_a_gated_run_assesses_before_the_pause_and_still_spends_nothing_after_it(
        self, mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
    ):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / "test-task").mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=_preflight_output(9), cost_usd=0.37
        )

        with (
            _assessment_agent() as mock_assess,
            patch("theforge.pending.poll_pending", side_effect=_never_answered),
        ):
            result = run_task(config, task)

        # One assessment, produced before the operator was asked.
        assert mock_assess.call_count == 1
        assert result.state.preflight_complexity_gate_assessment_generated is True
        assert len(result.state.preflight_complexity_gate_assessment["slices"]) == 2
        # And nothing past preflight was charged either way.
        mock_plan.assert_not_called()
        mock_dev.assert_not_called()
        mock_pool.assert_not_called()

    def test_the_assessment_is_not_produced_for_a_story_that_never_pauses(self, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _assessed_state(score=8, implementation=8)

        with _assessment_agent() as mock_assess:
            assert evaluate_preflight_complexity_gate(state, config, task, "PROCEED") is None

        mock_assess.assert_not_called()
        assert state.preflight_complexity_gate_assessment is None

    def test_the_original_story_is_left_intact_and_runnable_either_way(self, tmp_path: Path):
        """The assessment mutates nothing: not the story file, not the state's text."""
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        before = task.story_path.read_text(encoding="utf-8")
        state = _assessed_state()

        with (
            _assessment_agent(),
            patch("theforge.pending.poll_pending", side_effect=_answer_with("approve")),
        ):
            result = evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        assert result is None  # approved: the run continues on the original story
        assert task.story_path.read_text(encoding="utf-8") == before
        assert state.story_content == _STORY_WITH_CRITERIA


class TestTheAssessmentCannotMutateAnything:
    def test_the_invocation_is_sealed_read_only_regardless_of_the_configured_profile(
        self, tmp_path: Path
    ):
        """Non-mutation is a property of the invocation, not of the prompt."""
        from dataclasses import replace  # noqa: PLC0415

        from theforge.coordinator.preflight_decomposition_flow import (
            ASSESSMENT_PROFILE_NAME,
        )

        config = _config_with(tmp_path)
        # A project whose preflight profile was widened to a writable sandbox
        # with a shell must not carry that into the assessment.
        config = replace(
            config,
            preflight_profile=replace(
                config.preflight_profile,
                allowed_tools=("Read", "Bash", "Write", "Edit"),
                sandbox_mode="workspace-write",
            ),
            secrets={"GH_TOKEN": "tracker-write", "ANTHROPIC_API_KEY": "inference"},
        )
        task = _make_task(tmp_path)
        state = _assessed_state()
        captured: dict = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return _make_agent_result(success=True, output=_ASSESSMENT_OUTPUT, cost_usd=0.11)

        with (
            patch(
                "theforge.coordinator.preflight_decomposition_flow.run_agent",
                side_effect=_capture,
            ),
            patch("theforge.pending.poll_pending", side_effect=_never_answered),
        ):
            evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        profile = captured["profile"]
        assert profile.name == ASSESSMENT_PROFILE_NAME
        assert profile.sandbox_mode == "read-only"
        assert "Bash" not in profile.allowed_tools
        assert "Write" not in profile.allowed_tools
        assert "Edit" not in profile.allowed_tools
        assert profile.allowed_tools  # set, never emptied — empty means unrestricted
        # A tracker credential cannot reach an advisory stage.
        assert "GH_TOKEN" not in captured["secrets"]
        assert captured["secrets"]["ANTHROPIC_API_KEY"] == "inference"
        # And it reads a clean baseline checkout, not the story's worktree.
        assert Path(captured["working_dir"]) != Path(config.project_root)

    def test_the_assessment_is_bounded_below_the_planning_spend_it_displaces(self, tmp_path: Path):
        from theforge.coordinator.preflight_decomposition_flow import (
            assessment_budget_usd,
            assessment_timeout_seconds,
        )

        config = _config_with(tmp_path)
        planning = config.plan.budget_usd

        budget = assessment_budget_usd(config, config.preflight_profile)

        assert budget < planning
        assert budget <= config.preflight_profile.budget_usd
        # And the invocation cannot outlast the pause it is written before.
        assert assessment_timeout_seconds(config.preflight_profile, 4000) < 4000


class TestNoAssessmentNeverBlocksThePause:
    @pytest.mark.parametrize(
        ("output", "success", "expected"),
        [
            (_ATOMIC_OUTPUT, True, "judged the story atomic"),
            ("no block here at all", True, "no <decomposition_assessment> block"),
            (
                "<decomposition_assessment>\nslices: []\n</decomposition_assessment>",
                True,
                "failed validation",
            ),
            ("", False, "returned failure"),
        ],
    )
    def test_the_pause_still_opens_and_records_why_none_was_produced(
        self, output, success, expected, tmp_path: Path
    ):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _assessed_state()
        captured: dict = {}

        real_write = pending.write_pending

        def _capture(**kwargs):
            path = real_write(**kwargs)
            captured.update(kwargs)
            return path

        with (
            _assessment_agent(output, success=success),
            patch("theforge.pending.write_pending", side_effect=_capture),
            patch("theforge.pending.poll_pending", side_effect=_answer_with("approve")),
        ):
            result = evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        # The absence never blocks the pause from being answered.
        assert result is None
        assert state.preflight_complexity_gate_decision == "approve"
        assert state.preflight_complexity_gate_decision_source == "operator"
        # And the pause carried the question plus a recorded statement of absence.
        assert state.preflight_complexity_gate_assessment is None
        assert state.preflight_complexity_gate_assessment_generated is False
        assert expected in state.preflight_complexity_gate_assessment_none_reason
        assert "No decomposition assessment:" in captured["reason"]
        assert expected in captured["reason"]
        assert f"forge decide {state.run_id} decompose" in captured["reason"]

    def test_an_agent_that_cannot_be_invoked_leaves_the_gate_working(self, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _assessed_state()

        with (
            patch(
                "theforge.coordinator.preflight_decomposition_flow.run_agent",
                side_effect=RuntimeError("no runner"),
            ),
            patch("theforge.pending.poll_pending", side_effect=_answer_with("decompose")),
        ):
            result = evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        assert result is not None and result.success is False
        assert state.preflight_complexity_gate_assessment_generated is False
        assert "no runner" in state.preflight_complexity_gate_assessment_none_reason
        # No agent ran, so the run's measured total is not poisoned by it.
        assert state.total_decomposition_assessment_cost_measured == 0.0


class TestAssessmentCostIsRecordedPerRun:
    def test_a_measured_assessment_is_charged_to_the_run(self, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _assessed_state()

        with (
            _assessment_agent(cost_usd=0.11),
            patch("theforge.pending.poll_pending", side_effect=_answer_with("approve")),
        ):
            evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        assert state.preflight_complexity_gate_assessment_cost_usd == 0.11
        assert state.total_decomposition_assessment_cost == 0.11
        assert state.total_decomposition_assessment_cost_measured == 0.11
        assert state.total_cost == pytest.approx(0.11)
        assert state.total_cost_measured == pytest.approx(0.11)

    def test_an_unmeasured_assessment_poisons_the_run_total(self, tmp_path: Path):
        """A killed assessment reported no cost; coercing it to $0.00 would lie."""
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _assessed_state()

        with (
            _assessment_agent(cost_usd=None),
            patch("theforge.pending.poll_pending", side_effect=_answer_with("approve")),
        ):
            evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        assert state.preflight_complexity_gate_assessment_invoked is True
        assert state.total_decomposition_assessment_cost_measured is None
        assert state.total_cost_measured is None

    def test_a_run_that_produced_no_assessment_contributes_a_measured_zero(self, tmp_path: Path):
        state = CoordinatorState()

        assert state.total_decomposition_assessment_cost == 0.0
        assert state.total_decomposition_assessment_cost_measured == 0.0
        assert state.total_cost_measured == 0.0


class TestTheAssessmentAndItsDispositionAreAudited:
    def _record_for(self, tmp_path: Path, answer: str) -> dict:
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _assessed_state()
        state.started_at = "2026-01-01T00:00:00+00:00"

        with (
            _assessment_agent(),
            patch("theforge.pending.poll_pending", side_effect=_answer_with(answer)),
        ):
            evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        return generate_audit_log(
            config,
            task,
            CoordinatorResult(
                success=answer == "approve", phase=Phase.PREFLIGHT, state=state, message="m"
            ),
        )

    def test_the_artifact_and_an_acted_on_split_are_both_recorded(self, tmp_path: Path):
        gate = self._record_for(tmp_path, "decompose")["preflight_complexity_gate"]

        assert gate["assessment_generated"] is True
        assert len(gate["assessment"]["slices"]) == 2
        assert gate["assessment"]["slices"][1]["depends_on"] == [1]
        assert gate["assessment"]["unsettled"]
        assert gate["none_produced_reason"] is None
        assert gate["assessment_cost_usd"] == 0.11
        assert gate["assessment_duration_s"] is not None
        assert gate["assessment_profile"] == "preflight_decomposition_assessment"
        # The disposition is what lets assessment quality be measured later.
        assert gate["assessment_disposition"] == "operator_decompose"

    def test_an_overridden_assessment_records_the_override(self, tmp_path: Path):
        gate = self._record_for(tmp_path, "approve")["preflight_complexity_gate"]

        assert gate["assessment_generated"] is True
        assert gate["assessment_disposition"] == "operator_approve"

    def test_an_ungated_run_keeps_the_assessment_fields_present_and_empty(self, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        state.started_at = "2026-01-01T00:00:00+00:00"

        record = generate_audit_log(
            config,
            task,
            CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done"),
        )

        gate = record["preflight_complexity_gate"]
        assert gate["assessment"] is None
        assert gate["assessment_generated"] is False
        assert gate["none_produced_reason"] is None
        assert gate["assessment_disposition"] is None
        assert gate["assessment_cost_usd"] == 0.0


class TestTheAssessmentSurvivesResumeWithoutBeingPaidForTwice:
    def test_an_interrupted_pause_reuses_the_recorded_assessment(self, tmp_path: Path):
        """The pause is re-asked on resume; the artifact it carries is not re-bought."""
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        first = _assessed_state()

        with (
            _assessment_agent() as mock_assess,
            patch("theforge.pending.poll_pending", side_effect=_never_answered),
        ):
            evaluate_preflight_complexity_gate(first, config, task, "PROCEED")
        assert mock_assess.call_count == 1

        # The gate saved its own record after generating the assessment, so the
        # artifact is on disk even though preflight's save ran before the gate.
        record = load_resume_record(config.project_root, task.slug)
        assert record is not None
        resumed = CoordinatorState()
        resumed.run_id = "7c1e04b9d3af"
        resumed.story_content = _STORY_WITH_CRITERIA
        apply_resume_record_to_state(resumed, record)

        assert resumed.preflight_complexity_gate_assessment["slices"][0]["title"] == (
            "Parser and data contract"
        )
        assert resumed.preflight_complexity_gate_assessment_generated is True
        # Restored as provenance only: a prior attempt's spend is not this run's.
        assert resumed.preflight_complexity_gate_assessment_prior_cost_usd == 0.11
        assert resumed.total_decomposition_assessment_cost == 0.0
        assert resumed.total_decomposition_assessment_cost_measured == 0.0

        # Clear the recorded decision so the resumed attempt re-opens the pause,
        # which is the case where regeneration would otherwise happen.
        resumed.preflight_complexity_gate_decision = None
        resumed.preflight_complexity_gate_decision_source = None
        with (
            _assessment_agent() as mock_again,
            patch("theforge.pending.poll_pending", side_effect=_answer_with("approve")),
        ):
            assert evaluate_preflight_complexity_gate(resumed, config, task, "PROCEED") is None

        mock_again.assert_not_called()
        assert resumed.preflight_complexity_gate_assessment_generated is True

    def test_a_recorded_absence_is_not_re_attempted_either(self, tmp_path: Path):
        config = _config_with(tmp_path)
        task = _make_task(tmp_path)
        state = _assessed_state()
        state.preflight_complexity_gate_assessment_none_reason = (
            "the assessment judged the story atomic — it found no boundary to split on"
        )

        with (
            _assessment_agent() as mock_assess,
            patch("theforge.pending.poll_pending", side_effect=_answer_with("approve")),
        ):
            evaluate_preflight_complexity_gate(state, config, task, "PROCEED")

        mock_assess.assert_not_called()
