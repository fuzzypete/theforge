"""Phases that ran in an earlier attempt survive into a resumed run's audit.

A resumed coordinator attempt allocates a fresh ``CoordinatorState`` and used to
restore only trajectory bookkeeping and the parsed plan, while hardcoding
``preflight_verdict="SKIPPED"``. Everything preflight, plan review, and the
escalate gate produced was lost, so the final audit for a story reported phases
as skipped or absent that the run log shows completing and the coordinator
acting on — adaptive routing derived from the preflight result, two plan-review
cycles, a charged escalation advisory rendered to the operator (#2155).

These tests cover the seam in both directions: each phase writes its output to
the durable record as it produces it, and a resume that allocates a fresh state
lifts them back before anything can finalize an audit from the empty one. The
missing-record case is covered too, because "we could not recover this" and "the
coordinator deliberately bypassed the phase" must stay distinguishable.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_agent_result, _make_config, patch_gate_shell

from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_from_review
from theforge.coordinator.resume_persistence import (
    RECORD_VERSION,
    apply_resume_record_to_state,
    capture_phase_blocks,
    load_resume_record,
    merge_resume_record,
    recover_phase_state,
    resume_record_path,
    save_resume_record,
    story_content_hash,
    validate_resume_record,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.task import TaskStory

STORY_CONTENT = "# Test\n\nDo the thing."

APPROVE_YAML = (
    "```yaml\nverdict: APPROVE\nsummary: ok\nfindings: []\n"
    "story_compliance:\n  matches_spec: true\n"
    "test_coverage:\n  adequate: true\n"
    "ac_verification:\n  - criterion: c\n    status: VERIFIED\n"
    "    evidence: e\n```\n"
)

ROUTING_BLOCK = {
    "origin": "preflight",
    "code_review": {"final": {"models": ["opus", "gpt-5.4", "gemini-3.5-flash"]}},
}


def _preflight_state() -> CoordinatorState:
    """The state preflight leaves behind on a story it routed."""
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    state.preflight_reason = "Confirmed unfixed cause."
    state.preflight_complexity = "large"
    state.preflight_complexity_score = 9
    state.preflight_work_type = "bug"
    state.preflight_sufficiency = "needs_planning"
    state.preflight_domains = ["coordinator"]
    state.preflight_contract_change = True
    state.preflight_likely_files = ["src/theforge/coordinator/run_setup.py"]
    state.preflight_duration_s = 47.0
    state.routing_decision = dict(ROUTING_BLOCK)
    state.complexity_routing_audit = {"complexity": "large"}
    return state


def _plan_review_state() -> CoordinatorState:
    state = CoordinatorState()
    state.plan_review_decision = "approve"
    state.plan_review_mode = "agent"
    state.plan_review_waited_seconds = 12.5
    state.plan_review_durations = [30.0, 25.0]
    state.plan_regen_count = 1
    state.plan_agent_review_findings = "[P1-impl] bump the schema version"
    return state


def _escalation_state() -> CoordinatorState:
    state = CoordinatorState()
    state.escalate_decision = "advisory_pending"
    state.escalate_selected_action = "elevate"
    state.escalate_reason = "review cycles exhausted"
    state.advisory_generated = True
    state.advisory_report = {"options": [{"action": "elevate"}]}
    state.timeout_escalation_audit = {
        "original_model": "sonnet",
        "new_model": "opus",
        "reason": "timeout",
    }
    return state


#: Routing decision a degraded attempt produced — distinguishable from the one
#: the founded attempts produced, so adopting the wrong one is visible.
DEGRADED_ROUTING_BLOCK = {
    "origin": "degraded_fallback",
    "code_review": {"final": {"models": ["opus"]}},
}


def _founded_preflight_state(*, criteria: int = 2, score: int = 8) -> CoordinatorState:
    """A preflight attempt that exited cleanly having checked ``criteria`` criteria."""
    state = _preflight_state()
    state.preflight_complexity_score = score
    state.preflight_criteria_checked = [
        {"criterion": f"criterion {index}", "satisfied": True} for index in range(criteria)
    ]
    state.preflight_degraded = False
    state.complexity_routing_audit = {
        "complexity": "large",
        "complexity_source": "preflight",
        "preflight_degraded": {"degraded": False, "complexity_source": "preflight"},
        "story_allocation": {"allocation_usd": 12.0, "complexity_score": score},
    }
    return state


def _degraded_preflight_state() -> CoordinatorState:
    """A preflight attempt killed by signal: nothing checked, scored conservatively."""
    state = _preflight_state()
    state.preflight_complexity_score = 9
    state.preflight_criteria_checked = []
    state.preflight_degraded = True
    state.preflight_degraded_reason = "agent_failed_with_risk_signals"
    state.preflight_failure_action = "escalate"
    state.preflight_risk_signals = ["killed_by_signal"]
    state.routing_decision = dict(DEGRADED_ROUTING_BLOCK)
    state.complexity_routing_audit = {
        "complexity": "large",
        "complexity_source": "preflight_degraded_conservative",
        "preflight_degraded": {
            "degraded": True,
            "degraded_reason": "agent_failed_with_risk_signals",
            "complexity_source": "preflight_degraded_conservative",
        },
        "story_allocation": {"allocation_usd": 50.8, "complexity_score": 9},
    }
    return state


class TestFoundedBlockSelection:
    """A failed attempt never displaces a stored result of the same phase (#2351).

    The record is written so later work can proceed; an attempt that examined
    nothing carries strictly less of what later work needs, whatever its order.
    """

    def test_degraded_save_does_not_displace_a_founded_one(self, tmp_path: Path) -> None:
        save_resume_record(
            tmp_path,
            _founded_preflight_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-clean",
        )
        save_resume_record(
            tmp_path,
            _degraded_preflight_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-killed",
        )

        loaded = load_resume_record(tmp_path, "test-story")
        assert loaded is not None
        assert loaded["preflight"]["degraded"] is False
        assert loaded["preflight"]["complexity_score"] == 8
        assert len(loaded["preflight"]["criteria_checked"]) == 2
        assert loaded["routing_decision"] == ROUTING_BLOCK
        assert loaded["complexity_routing_audit"]["complexity_source"] == "preflight"
        assert loaded["complexity_routing_audit"]["story_allocation"]["allocation_usd"] == 12.0

    def test_resume_proceeds_from_the_founded_attempt(self, tmp_path: Path) -> None:
        """The fix-success criterion: resuming continues from the clean result."""
        save_resume_record(
            tmp_path,
            _founded_preflight_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-clean",
        )
        save_resume_record(
            tmp_path,
            _degraded_preflight_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-killed",
        )

        resumed = CoordinatorState()
        recovery = recover_phase_state(
            tmp_path, resumed, slug="test-story", story_content=STORY_CONTENT
        )

        assert recovery["status"] == "recovered"
        assert resumed.preflight_degraded is False
        assert resumed.preflight_complexity_score == 8
        assert resumed.preflight_failure_action is None
        assert resumed.routing_decision == ROUTING_BLOCK
        # The founded figure is founded: nothing is marked carried-unfounded.
        assert "unfounded" not in resumed.complexity_routing_audit
        assert "unfounded" not in resumed.story_allocation

    def test_a_zero_criteria_save_cannot_supersede_a_checked_one(self, tmp_path: Path) -> None:
        """Foundedness, not degraded-ness alone, decides between two clean saves."""
        checked = _founded_preflight_state(criteria=2, score=8)
        nothing_checked = _founded_preflight_state(criteria=0, score=9)

        save_resume_record(
            tmp_path, checked, slug="test-story", story_content=STORY_CONTENT, run_id="run-a"
        )
        save_resume_record(
            tmp_path,
            nothing_checked,
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-b",
        )

        loaded = load_resume_record(tmp_path, "test-story")
        assert loaded is not None
        assert loaded["preflight"]["complexity_score"] == 8

    def test_a_better_founded_later_save_replaces_the_stored_one(self, tmp_path: Path) -> None:
        save_resume_record(
            tmp_path,
            _founded_preflight_state(criteria=1, score=8),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-thin",
        )
        thorough = _founded_preflight_state(criteria=3, score=6)
        save_resume_record(
            tmp_path,
            thorough,
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-thorough",
        )

        loaded = load_resume_record(tmp_path, "test-story")
        assert loaded is not None
        assert loaded["preflight"]["complexity_score"] == 6
        assert len(loaded["preflight"]["criteria_checked"]) == 3

        entries = loaded["resume_selection"]
        replaced = [e for e in entries if e["phase"] == "preflight"]
        assert [e["action"] for e in replaced] == ["replaced_existing"]
        assert replaced[0]["reason"] == "incoming_block_more_founded"
        assert replaced[0]["prior_foundation"] == {"degraded": False, "examined": 1}
        assert replaced[0]["incoming_foundation"] == {"degraded": False, "examined": 3}
        assert replaced[0]["prior_run_id"] == "run-thin"
        assert replaced[0]["incoming_run_id"] == "run-thorough"

    def test_selection_is_disclosed_to_the_operator_on_recovery(self, tmp_path: Path) -> None:
        """What the story resumes from is readable without diffing run dirs."""
        save_resume_record(
            tmp_path,
            _founded_preflight_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-clean",
        )
        save_resume_record(
            tmp_path,
            _degraded_preflight_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-killed",
        )

        recovery = recover_phase_state(
            tmp_path, CoordinatorState(), slug="test-story", story_content=STORY_CONTENT
        )

        by_phase = {entry["phase"]: entry for entry in recovery["block_selection"]}
        assert by_phase["preflight"]["action"] == "kept_existing"
        assert by_phase["preflight"]["reason"] == "incoming_block_less_founded"
        assert by_phase["preflight"]["prior_foundation"] == {"degraded": False, "examined": 2}
        assert by_phase["preflight"]["incoming_foundation"] == {"degraded": True, "examined": 0}
        assert by_phase["preflight"]["incoming_run_id"] == "run-killed"
        assert by_phase["complexity_routing_audit"]["action"] == "kept_existing"
        assert by_phase["routing_decision"]["action"] == "kept_existing"

    def test_selection_metadata_does_not_re_enter_the_block_merge(self, tmp_path: Path) -> None:
        """The disclosure is metadata: it never becomes a phase block itself."""
        for state, run_id in (
            (_founded_preflight_state(), "run-clean"),
            (_degraded_preflight_state(), "run-killed"),
            (_plan_review_state(), "run-later"),
        ):
            save_resume_record(
                tmp_path, state, slug="test-story", story_content=STORY_CONTENT, run_id=run_id
            )

        loaded = load_resume_record(tmp_path, "test-story")
        assert loaded is not None
        # Survives the save that carried no selection decision of its own.
        assert [e["phase"] for e in loaded["resume_selection"]] == [
            "preflight",
            "routing_decision",
            "complexity_routing_audit",
        ]
        recovery = recover_phase_state(
            tmp_path, CoordinatorState(), slug="test-story", story_content=STORY_CONTENT
        )
        assert "resume_selection" not in recovery["recorded_phases"]
        assert "resume_selection" not in recovery["recovered_phases"]

    def test_blocks_with_no_foundedness_signal_keep_incoming_wins(self) -> None:
        """A decision is not an observation: the latest one is what the
        coordinator acted on, so escalation and plan review still merge by order."""
        existing = {
            "version": RECORD_VERSION,
            "slug": "test-story",
            "escalation": {"decision": "advisory_pending"},
            "plan_review": {"decision": "reject"},
        }
        merged = merge_resume_record(
            existing,
            {
                "escalation": {"decision": "operator_approved"},
                "plan_review": {"decision": "approve"},
            },
            slug="test-story",
            story_hash=None,
            run_id="run-2",
        )
        assert merged["escalation"]["decision"] == "operator_approved"
        assert merged["plan_review"]["decision"] == "approve"
        assert "resume_selection" not in merged


class TestDegradedValuesCarryForwardAsUnfounded:
    """A figure derived while degraded is marked wherever later stages read it."""

    def test_degraded_only_record_marks_the_routing_audit_and_allocation(
        self, tmp_path: Path
    ) -> None:
        save_resume_record(
            tmp_path,
            _degraded_preflight_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-killed",
        )

        resumed = CoordinatorState()
        recover_phase_state(tmp_path, resumed, slug="test-story", story_content=STORY_CONTENT)

        assert resumed.complexity_routing_audit["unfounded"] is True
        assert resumed.complexity_routing_audit["unfounded_reason"] == (
            "agent_failed_with_risk_signals"
        )
        assert resumed.story_allocation["unfounded"] is True
        assert resumed.story_allocation["unfounded_source"] == "preflight"
        assert resumed.complexity_routing_audit["story_allocation"]["unfounded"] is True

    def test_a_value_this_attempt_derived_is_not_overwritten(self, tmp_path: Path) -> None:
        save_resume_record(
            tmp_path,
            _degraded_preflight_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-killed",
        )

        live = CoordinatorState()
        live.complexity_routing_audit = {"complexity": "medium", "complexity_source": "preflight"}
        live.story_allocation = {"allocation_usd": 3.0, "complexity_score": 5}

        recover_phase_state(tmp_path, live, slug="test-story", story_content=STORY_CONTENT)

        assert live.complexity_routing_audit["complexity"] == "medium"
        assert "unfounded" not in live.complexity_routing_audit
        assert live.story_allocation["allocation_usd"] == 3.0

    def test_founded_record_carries_no_unfounded_marker(self, tmp_path: Path) -> None:
        save_resume_record(
            tmp_path,
            _founded_preflight_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-clean",
        )

        resumed = CoordinatorState()
        recover_phase_state(tmp_path, resumed, slug="test-story", story_content=STORY_CONTENT)

        assert "unfounded" not in resumed.complexity_routing_audit
        assert "unfounded" not in resumed.story_allocation


class TestRecordPersistence:
    """The record round-trips, merges across phases, and refuses bad input."""

    def test_preflight_round_trips(self, tmp_path: Path) -> None:
        save_resume_record(
            tmp_path,
            _preflight_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="prior-run",
        )
        loaded = load_resume_record(tmp_path, "test-story")

        assert loaded is not None
        assert loaded["preflight"]["verdict"] == "PROCEED"
        assert loaded["preflight"]["complexity_score"] == 9
        assert loaded["preflight"]["likely_files"] == ["src/theforge/coordinator/run_setup.py"]
        assert loaded["routing_decision"] == ROUTING_BLOCK
        assert loaded["story_content_hash"] == story_content_hash(STORY_CONTENT)
        assert loaded["run_id"] == "prior-run"

    def test_later_phase_does_not_erase_an_earlier_one(self, tmp_path: Path) -> None:
        """The bug this record exists to prevent, in miniature.

        Each phase saves from whatever state it holds. A plan-review save whose
        state carries no preflight fields must not blank the preflight block a
        prior save wrote — that is exactly the "replaced with the shape of a run
        that never happened" failure.
        """
        save_resume_record(
            tmp_path, _preflight_state(), slug="test-story", story_content=STORY_CONTENT
        )
        save_resume_record(
            tmp_path, _plan_review_state(), slug="test-story", story_content=STORY_CONTENT
        )
        save_resume_record(
            tmp_path, _escalation_state(), slug="test-story", story_content=STORY_CONTENT
        )

        loaded = load_resume_record(tmp_path, "test-story")
        assert loaded is not None
        assert loaded["preflight"]["verdict"] == "PROCEED"
        assert loaded["routing_decision"] == ROUTING_BLOCK
        assert loaded["plan_review"]["decision"] == "approve"
        assert loaded["escalation"]["selected_action"] == "elevate"
        assert loaded["timeout_escalation"]["new_model"] == "opus"

    def test_changed_story_discards_prior_blocks_on_merge(self) -> None:
        existing = {
            "version": RECORD_VERSION,
            "slug": "test-story",
            "story_content_hash": story_content_hash("old story"),
            "preflight": {"verdict": "PROCEED"},
        }
        merged = merge_resume_record(
            existing,
            {"plan_review": {"decision": "approve"}},
            slug="test-story",
            story_hash=story_content_hash("new story"),
            run_id=None,
        )
        assert "preflight" not in merged
        assert merged["plan_review"]["decision"] == "approve"

    def test_state_with_no_phase_output_writes_nothing(self, tmp_path: Path) -> None:
        assert (
            save_resume_record(
                tmp_path, CoordinatorState(), slug="test-story", story_content=STORY_CONTENT
            )
            is None
        )
        assert load_resume_record(tmp_path, "test-story") is None

    def test_missing_record_reads_as_none(self, tmp_path: Path) -> None:
        assert load_resume_record(tmp_path, "never-ran") is None

    def test_corrupt_record_is_rejected(self, tmp_path: Path) -> None:
        path = resume_record_path(tmp_path, "test-story")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert load_resume_record(tmp_path, "test-story") is None

    def test_unknown_version_is_rejected(self, tmp_path: Path) -> None:
        path = resume_record_path(tmp_path, "test-story")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": RECORD_VERSION + 1, "preflight": {"verdict": "PROCEED"}}),
            encoding="utf-8",
        )
        assert load_resume_record(tmp_path, "test-story") is None

    def test_changed_story_invalidates_record(self, tmp_path: Path) -> None:
        save_resume_record(
            tmp_path, _preflight_state(), slug="test-story", story_content=STORY_CONTENT
        )
        record = load_resume_record(tmp_path, "test-story")
        usable, reason = validate_resume_record(record, story_content="# Test\n\nLater.")
        assert usable is False
        assert reason == "story_content_changed"

    def test_save_is_best_effort(self, tmp_path: Path) -> None:
        """An unwritable record must not take down the phase that produced it."""
        with patch(
            "theforge.coordinator.resume_persistence.Path.mkdir",
            side_effect=OSError("read-only"),
        ):
            assert (
                save_resume_record(
                    tmp_path,
                    _preflight_state(),
                    slug="test-story",
                    story_content=STORY_CONTENT,
                )
                is None
            )

    def test_deliberate_skip_is_recorded_as_skipped(self, tmp_path: Path) -> None:
        """``--from <phase>`` is the one real bypass; a resume past it must still
        report SKIPPED rather than degrading the claim to "no record"."""
        state = CoordinatorState()
        state.preflight_verdict = "SKIPPED"
        save_resume_record(tmp_path, state, slug="test-story", story_content=STORY_CONTENT)

        resumed = CoordinatorState()
        recovery = recover_phase_state(
            tmp_path, resumed, slug="test-story", story_content=STORY_CONTENT
        )
        assert resumed.preflight_verdict == "SKIPPED"
        assert recovery["status"] == "recovered"


class TestApplyToState:
    """Recovery fills gaps; it never overwrites what this attempt produced."""

    def test_recovers_preflight_routing_plan_review_and_escalation(self, tmp_path: Path) -> None:
        for producing_state in (
            _preflight_state(),
            _plan_review_state(),
            _escalation_state(),
        ):
            save_resume_record(
                tmp_path, producing_state, slug="test-story", story_content=STORY_CONTENT
            )

        resumed = CoordinatorState()
        recovery = recover_phase_state(
            tmp_path, resumed, slug="test-story", story_content=STORY_CONTENT
        )

        assert recovery["status"] == "recovered"
        assert recovery["recovered_phases"] == [
            "preflight",
            "routing_decision",
            "plan_review",
            "escalation",
            "timeout_escalation",
        ]
        assert resumed.preflight_verdict == "PROCEED"
        assert resumed.preflight_complexity == "large"
        assert resumed.preflight_complexity_score == 9
        assert resumed.preflight_work_type == "bug"
        assert resumed.preflight_contract_change is True
        assert resumed.routing_decision == ROUTING_BLOCK
        assert resumed.complexity_routing_audit == {"complexity": "large"}
        assert resumed.plan_review_decision == "approve"
        assert resumed.plan_review_durations == [30.0, 25.0]
        assert resumed.escalate_decision == "advisory_pending"
        assert resumed.advisory_generated is True
        assert resumed.advisory_report == {"options": [{"action": "elevate"}]}
        assert resumed.timeout_escalation_audit["new_model"] == "opus"

    def test_live_preflight_beats_the_record(self, tmp_path: Path) -> None:
        """A verdict this attempt produced is authoritative — the record is a
        fallback for a phase that did not run, not a second opinion."""
        save_resume_record(
            tmp_path, _preflight_state(), slug="test-story", story_content=STORY_CONTENT
        )
        live = CoordinatorState()
        live.preflight_verdict = "ALREADY_DONE"
        live.preflight_complexity = "small"

        recovery = recover_phase_state(
            tmp_path, live, slug="test-story", story_content=STORY_CONTENT
        )

        assert live.preflight_verdict == "ALREADY_DONE"
        assert live.preflight_complexity == "small"
        assert "preflight" not in recovery["recovered_phases"]

    def test_plan_review_skipped_when_this_attempt_ran_one(self, tmp_path: Path) -> None:
        """Splicing a prior attempt's durations into a state that has its own
        results would misattribute reviewers in the per-attempt audit builder."""
        record = {"plan_review": {"decision": "approve", "durations": [30.0, 25.0]}}
        live = CoordinatorState()
        live.plan_review_results.append(_make_agent_result(output="x", profile_name="pr"))
        live.plan_review_durations = [5.0]

        recovered = apply_resume_record_to_state(live, record)

        assert recovered == []
        assert live.plan_review_durations == [5.0]

    def test_missing_record_reports_unavailable(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        recovery = recover_phase_state(
            tmp_path, state, slug="never-ran", story_content=STORY_CONTENT
        )
        assert recovery["status"] == "unavailable"
        assert recovery["recovered_phases"] == []
        assert state.preflight_verdict is None

    def test_rejected_record_reports_rejected(self, tmp_path: Path) -> None:
        save_resume_record(
            tmp_path, _preflight_state(), slug="test-story", story_content=STORY_CONTENT
        )
        state = CoordinatorState()
        recovery = recover_phase_state(
            tmp_path, state, slug="test-story", story_content="# Different story"
        )
        assert recovery["status"] == "rejected"
        assert recovery["reason"] == "story_content_changed"
        assert state.preflight_verdict is None

    def test_capture_omits_phases_that_produced_nothing(self) -> None:
        assert capture_phase_blocks(CoordinatorState()) == {}
        assert set(capture_phase_blocks(_plan_review_state())) == {"plan_review"}


def _shell(cmd: str, cwd, **kwargs):
    if "--oneline" in cmd and "git log" in cmd:
        return (True, "abc1234 feat: work", 0, False)
    if "git status --porcelain" in cmd:
        return (True, "", 0, False)
    return (True, "OK", 0, False)


class TestResumedAuditReportsPhasesThatRan:
    """Seam test across the resume boundary: run_from_review with a fresh state."""

    def _run(self, tmp_path: Path, mock_pool):
        spec = tmp_path / "spec.md"
        spec.write_text(STORY_CONTENT, encoding="utf-8")
        config = _make_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        mock_pool.side_effect = lambda **kwargs: [
            _make_agent_result(success=True, output=APPROVE_YAML, profile_name=p.name)
            for p in kwargs["profiles"]
        ]
        result = run_from_review(config, task, workspace, cached_preflight_state=None)
        return config, task, result

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell(side_effect=_shell)
    def test_audit_reports_preflight_routing_and_plan_review_that_ran(
        self, _mock_shell, _mock_dev, mock_pool, tmp_path: Path
    ) -> None:
        """The reported symptom, end to end: a story whose preflight and plan
        review ran in an earlier attempt resumes and still reports both."""
        for producing_state in (_preflight_state(), _plan_review_state()):
            save_resume_record(
                tmp_path,
                producing_state,
                slug="test-story",
                story_content=STORY_CONTENT,
                run_id="prior-run",
            )

        config, task, result = self._run(tmp_path, mock_pool)
        audit = generate_audit_log(config, task, result)

        assert audit["preflight"] is not None
        assert audit["preflight"]["verdict"] == "PROCEED"
        assert audit["preflight"]["complexity"] == "large"
        assert audit["preflight"]["complexity_score"] == 9
        assert audit["routing_decision"] == ROUTING_BLOCK
        assert audit["plan_review"] is not None
        assert audit["plan_review"]["decision"] == "approve"
        assert audit["phases"]["preflight"]["outcome"] == "proceed"
        assert audit["phase_recovery"]["status"] == "recovered"
        assert audit["phase_recovery"]["source_run_id"] == "prior-run"
        assert "preflight" in audit["phase_recovery"]["recovered_phases"]

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell(side_effect=_shell)
    def test_escalation_and_timeout_escalation_survive_the_resume(
        self, _mock_shell, _mock_dev, mock_pool, tmp_path: Path
    ) -> None:
        save_resume_record(
            tmp_path,
            _escalation_state(),
            slug="test-story",
            story_content=STORY_CONTENT,
            run_id="prior-run",
        )

        config, task, result = self._run(tmp_path, mock_pool)
        audit = generate_audit_log(config, task, result)

        assert audit["escalation"] is not None
        assert audit["escalation"]["human_decision"] == "advisory_pending"
        assert audit["escalation"]["selected_action"] == "elevate"
        assert audit["escalation"]["advisory_generated"] is True
        assert audit["timeout_escalation"]["new_model"] == "opus"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell(side_effect=_shell)
    def test_no_record_reports_absent_not_skipped(
        self, _mock_shell, _mock_dev, mock_pool, tmp_path: Path
    ) -> None:
        """Nothing to recover: the audit must not claim a bypass it never made.

        A resumed run with no recoverable evidence reports the phase as absent
        and names the recovery failure. "SKIPPED" is reserved for a coordinator
        that deliberately bypassed preflight, so an operator can still tell the
        two apart.
        """
        config, task, result = self._run(tmp_path, mock_pool)
        audit = generate_audit_log(config, task, result)

        assert audit["preflight"] is None
        assert audit["phases"]["preflight"] is None
        assert audit["routing_decision"] is None
        assert audit["phase_recovery"]["status"] == "unavailable"
        assert audit["phase_recovery"]["reason"] == "no_record"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell(side_effect=_shell)
    def test_recovered_state_reaches_an_abnormally_terminated_audit(
        self, _mock_shell, _mock_dev, mock_pool, tmp_path: Path
    ) -> None:
        """The artifacts most likely to be needed come from runs least likely to
        finish cleanly: an audit synthesized from a salvaged live state must
        carry the recovered phases too, not a bare synthetic shape."""
        save_resume_record(
            tmp_path, _preflight_state(), slug="test-story", story_content=STORY_CONTENT
        )

        config, task, result = self._run(tmp_path, mock_pool)

        abnormal = CoordinatorResult(
            success=False,
            phase=Phase.ESCALATE,
            state=result.state,
            message="worker deadline expired",
        )
        audit = generate_audit_log(config, task, abnormal)

        assert audit["preflight"]["verdict"] == "PROCEED"
        assert audit["routing_decision"] == ROUTING_BLOCK


class TestPhasesWriteTheRecordAsTheyRun:
    """The write side: a phase's output is durable the moment it exists."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell(side_effect=_shell)
    def test_escalate_gate_persists_its_decision(
        self, _mock_shell, _mock_dev, _mock_pool, tmp_path: Path
    ) -> None:
        from theforge.coordinator.review_phase import _run_escalate_gate

        _base = _make_config(tmp_path)
        config = dataclasses.replace(
            _base, retry=dataclasses.replace(_base.retry, escalate_policy="reject")
        )
        task = TaskStory(name="Test Story", story_path=tmp_path / "spec.md", slug="test-story")
        task.story_path.write_text(STORY_CONTENT, encoding="utf-8")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        state = CoordinatorState()
        state.story_content = STORY_CONTENT
        state.error = "review cycles exhausted"

        _run_escalate_gate(
            state,
            config,
            task,
            workspace,
            "forge/test-story",
            0.0,
            auto_merge=False,
            notify=False,
            logger=None,
            run_id="run-1",
        )

        record = load_resume_record(tmp_path, "test-story")
        assert record is not None
        assert record["escalation"]["decision"] == "reject"
        assert record["escalation"]["reason"] == "review cycles exhausted"
