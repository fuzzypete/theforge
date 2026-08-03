"""The routed code-review panel survives a resume that lost the preflight cache.

Adaptive routing sizes the code-review panel once, during preflight. That
decision lived only in the sprint scheduler's in-memory ``preflight_states``
map, so a mid-sprint process re-exec dropped it for stories already in flight
and the resumed REVIEW phase re-derived its panel from the ``forge.yaml``
roster instead — seating five reviewers where routing had chosen three, which
mis-sized the per-reviewer budget share and the quorum threshold derived from
the seat count (#2154).

These tests cover the seam in both directions: preflight persists the decision
it installed, and a resume with ``cached_preflight_state=None`` recovers it
before REVIEW runs. When no usable record exists the resume must say so rather
than pass the roster off as a routed panel.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_agent_result, _make_config, patch_gate_shell

from theforge.config import ModelProfile, RetryPolicy
from theforge.coordinator.engine import run_from_review
from theforge.coordinator.preflight import (
    persist_routing_decision,
    restore_routing_decision,
)
from theforge.coordinator.review_pool import _run_review_pool
from theforge.coordinator.routing_persistence import (
    RECORD_VERSION,
    build_routing_record,
    load_routing_record,
    routing_record_path,
    save_routing_record,
    story_content_hash,
    validate_routing_record,
)
from theforge.coordinator.state import CoordinatorState, Phase, ReviewCycleMetadata
from theforge.task import TaskStory

STORY_CONTENT = "# Test\n\nDo the thing."

APPROVE_YAML = (
    "```yaml\nverdict: APPROVE\nsummary: ok\nfindings: []\n"
    "story_compliance:\n  matches_spec: true\n"
    "test_coverage:\n  adequate: true\n"
    "ac_verification:\n  - criterion: c\n    status: VERIFIED\n"
    "    evidence: e\n```\n"
)

ROSTER_MODELS = ["sonnet", "gemini-3.5-flash", "gpt-5.4", "opus", "gpt-5.5"]
ROUTED_MODELS = ["gpt-5.4", "gemini-3.5-flash", "opus"]


def _profile(model: str, budget_usd: float = 1.0) -> ModelProfile:
    return ModelProfile(
        name=model,
        cli="claude",
        model=model,
        budget_usd=budget_usd,
        timeout_seconds=300,
        allowed_tools=("Read", "Bash"),
    )


def _roster_config(tmp_path: Path):
    """Config whose review_pool is the full static roster (the wrong panel)."""
    config = _make_config(tmp_path)
    return dataclasses.replace(config, review_pool=[_profile(m) for m in ROSTER_MODELS])


def _state_with_complexity() -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_complexity = "large"
    state.preflight_complexity_score = 9
    state.preflight_work_type = "bug"
    state.preflight_sufficiency = "implementation_ready"
    return state


def _write_routed_record(tmp_path: Path, slug: str = "test-story", **overrides) -> dict:
    record = build_routing_record(
        slug=slug,
        state=_state_with_complexity(),
        review_pool_models=list(ROUTED_MODELS),
        dev_model="opus",
        plan_model="opus",
        story_content=STORY_CONTENT,
        run_id="prior-run-id",
    )
    record.update(overrides)
    save_routing_record(tmp_path, record)
    return record


class TestRoutingRecordPersistence:
    """The record round-trips and refuses to be read when it cannot be trusted."""

    def test_round_trip_preserves_routed_panel(self, tmp_path: Path) -> None:
        record = _write_routed_record(tmp_path)
        loaded = load_routing_record(tmp_path, "test-story")

        assert loaded is not None
        assert loaded["code_reviewers"] == ROUTED_MODELS
        assert loaded["code_reviewer_count"] == 3
        assert loaded["complexity_score"] == 9
        assert loaded == record

    def test_missing_record_reads_as_none(self, tmp_path: Path) -> None:
        assert load_routing_record(tmp_path, "never-ran") is None

    def test_unknown_version_is_rejected(self, tmp_path: Path) -> None:
        _write_routed_record(tmp_path, version=RECORD_VERSION + 1)
        assert load_routing_record(tmp_path, "test-story") is None

    def test_corrupt_record_is_rejected(self, tmp_path: Path) -> None:
        path = routing_record_path(tmp_path, "test-story")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert load_routing_record(tmp_path, "test-story") is None

    def test_changed_story_invalidates_record(self, tmp_path: Path) -> None:
        record = _write_routed_record(tmp_path)
        usable, reason = validate_routing_record(record, story_content="# Test\n\nDo it later.")
        assert usable is False
        assert reason == "story_content_changed"

    def test_matching_story_validates(self, tmp_path: Path) -> None:
        record = _write_routed_record(tmp_path)
        usable, reason = validate_routing_record(record, story_content=STORY_CONTENT)
        assert usable is True
        assert reason == "story_content_match"

    def test_record_without_story_hash_is_usable_but_unverified(self, tmp_path: Path) -> None:
        """A hash-less record still carries a real decision; rejecting it would
        put the run back on the roster this module exists to prevent."""
        record = _write_routed_record(tmp_path, story_content_hash=None)
        usable, reason = validate_routing_record(record, story_content=STORY_CONTENT)
        assert usable is True
        assert reason == "unverified_story"

    def test_persist_writes_the_installed_panel(self, tmp_path: Path) -> None:
        config = dataclasses.replace(
            _make_config(tmp_path), review_pool=[_profile(m) for m in ROUTED_MODELS]
        )
        state = _state_with_complexity()

        path = persist_routing_decision(
            config,
            state,
            task_slug="test-story",
            story_content=STORY_CONTENT,
            run_id="run-1",
        )

        assert path is not None
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["code_reviewers"] == ROUTED_MODELS
        assert written["story_content_hash"] == story_content_hash(STORY_CONTENT)
        assert written["run_id"] == "run-1"

    def test_persist_is_best_effort(self, tmp_path: Path) -> None:
        """A record that cannot be written must not take down the run producing it."""
        config = dataclasses.replace(
            _make_config(tmp_path), review_pool=[_profile(m) for m in ROUTED_MODELS]
        )
        with patch(
            "theforge.coordinator.routing_persistence.Path.mkdir",
            side_effect=OSError("read-only"),
        ):
            assert (
                persist_routing_decision(config, _state_with_complexity(), task_slug="test-story")
                is None
            )


class TestRestoreRoutingDecision:
    """Recovery narrows the roster back to the routed panel — or says it cannot."""

    def test_recovered_panel_replaces_the_roster(self, tmp_path: Path) -> None:
        _write_routed_record(tmp_path)
        config = _roster_config(tmp_path)
        state = CoordinatorState()

        config, recovery = restore_routing_decision(
            config, state, task_slug="test-story", story_content=STORY_CONTENT
        )

        assert [p.model for p in config.review_pool] == ROUTED_MODELS
        assert recovery["status"] == "recovered"
        assert recovery["recorded_count"] == 3
        assert recovery["seated_count"] == 3
        # The complexity signal routing consumed is restored too, so every other
        # complexity-derived limit stops falling back to the static default.
        assert state.preflight_complexity == "large"
        assert state.preflight_complexity_score == 9

    def test_recovery_is_recorded_in_the_audit_trail(self, tmp_path: Path) -> None:
        _write_routed_record(tmp_path)
        config = _roster_config(tmp_path)
        state = CoordinatorState()

        restore_routing_decision(
            config, state, task_slug="test-story", story_content=STORY_CONTENT
        )

        recovery = (state.complexity_routing_audit or {}).get("routing_recovery")
        assert recovery is not None
        assert recovery["source_run_id"] == "prior-run-id"
        assert recovery["recorded_review_pool"] == ROUTED_MODELS
        assert recovery["seated_review_pool"] == ROUTED_MODELS

    def test_no_record_leaves_roster_and_says_so(self, tmp_path: Path) -> None:
        config = _roster_config(tmp_path)
        state = CoordinatorState()
        lines: list[str] = []

        config, recovery = restore_routing_decision(
            config,
            state,
            task_slug="test-story",
            story_content=STORY_CONTENT,
            log=lines.append,
        )

        assert [p.model for p in config.review_pool] == ROSTER_MODELS
        assert recovery["status"] == "unavailable"
        assert recovery["reason"] == "no_record"
        assert recovery["seated_count"] == 5
        assert any("no persisted routing decision" in line for line in lines)

    def test_stale_record_is_refused_out_loud(self, tmp_path: Path) -> None:
        _write_routed_record(tmp_path)
        config = _roster_config(tmp_path)
        state = CoordinatorState()
        lines: list[str] = []

        config, recovery = restore_routing_decision(
            config,
            state,
            task_slug="test-story",
            story_content="# Test\n\nSomething else entirely.",
            log=lines.append,
        )

        assert [p.model for p in config.review_pool] == ROSTER_MODELS
        assert recovery["status"] == "rejected"
        assert recovery["reason"] == "story_content_changed"
        assert any("rejected" in line for line in lines)
        # A refused record must not leak its complexity signal into the run.
        assert state.preflight_complexity is None

    def test_unhonourable_panel_is_reported_not_substituted(self, tmp_path: Path) -> None:
        """A recorded model absent from the current pool cannot be seated. The
        run says so instead of quietly seating a differently-sized panel."""
        _write_routed_record(tmp_path)
        config = dataclasses.replace(
            _roster_config(tmp_path),
            review_pool=[_profile("sonnet"), _profile("opus")],
        )
        state = CoordinatorState()
        lines: list[str] = []

        config, recovery = restore_routing_decision(
            config,
            state,
            task_slug="test-story",
            story_content=STORY_CONTENT,
            log=lines.append,
        )

        assert recovery["status"] == "recovered"
        assert recovery["reconciliation"] == "unhonoured"
        assert recovery["recorded_count"] == 3
        assert recovery["seated_count"] == 2
        assert any("cannot be honoured" in line for line in lines)

    def test_preflight_record_is_what_resume_recovers(self, tmp_path: Path) -> None:
        """End-to-end of the persistence seam: what preflight installs is what a
        later resume seats, across two independent config objects."""
        routed_config = dataclasses.replace(
            _make_config(tmp_path), review_pool=[_profile(m) for m in ROUTED_MODELS]
        )
        persist_routing_decision(
            routed_config,
            _state_with_complexity(),
            task_slug="test-story",
            story_content=STORY_CONTENT,
        )

        resumed_config, recovery = restore_routing_decision(
            _roster_config(tmp_path),
            CoordinatorState(),
            task_slug="test-story",
            story_content=STORY_CONTENT,
        )

        assert [p.model for p in resumed_config.review_pool] == ROUTED_MODELS
        assert recovery["status"] == "recovered"


class TestResumeSeatsRoutedPanel:
    """Seam test across the resume boundary: run_from_review with no cache."""

    @staticmethod
    def _shell(cmd: str, cwd, **kwargs):
        if "--oneline" in cmd and "git log" in cmd:
            return (True, "abc1234 feat: work", 0, False)
        if "git status --porcelain" in cmd:
            return (True, "", 0, False)
        return (True, "OK", 0, False)

    def _run(self, tmp_path: Path, mock_pool):
        spec = tmp_path / "spec.md"
        spec.write_text(STORY_CONTENT, encoding="utf-8")
        config = _roster_config(tmp_path)
        task = TaskStory(name="Test Story", story_path=spec, slug="test-story")
        workspace = tmp_path / "test-story"
        workspace.mkdir()

        mock_pool.side_effect = lambda **kwargs: [
            _make_agent_result(success=True, output=APPROVE_YAML, profile_name=p.name)
            for p in kwargs["profiles"]
        ]
        return run_from_review(config, task, workspace, cached_preflight_state=None)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell(side_effect=_shell.__func__)
    def test_review_seats_the_routed_panel(
        self, _mock_shell, _mock_dev, mock_pool, tmp_path: Path
    ) -> None:
        _write_routed_record(tmp_path)

        self._run(tmp_path, mock_pool)

        assert mock_pool.called
        seated = [p.model for p in mock_pool.call_args.kwargs["profiles"]]
        assert seated == ROUTED_MODELS, "resumed REVIEW re-derived the panel from the roster"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell(side_effect=_shell.__func__)
    def test_review_falls_back_to_roster_when_nothing_was_recorded(
        self, _mock_shell, _mock_dev, mock_pool, tmp_path: Path
    ) -> None:
        """No record to recover: the roster is seated, but the run records that
        it is a fallback rather than a routed decision."""
        result = self._run(tmp_path, mock_pool)

        seated = [p.model for p in mock_pool.call_args.kwargs["profiles"]]
        assert seated == ROSTER_MODELS
        recovery = (result.state.complexity_routing_audit or {}).get("routing_recovery")
        assert recovery is not None
        assert recovery["status"] == "unavailable"


class TestQuorumCountsEligibleSeats:
    """Quorum describes the seats that can answer, not the nominal pool."""

    @patch("theforge.coordinator.review_pool.log_agent_result")
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    def test_budget_excluded_reviewers_leave_the_denominator(
        self, mock_pool, _mock_log, tmp_path: Path
    ) -> None:
        profiles = [_profile("a"), _profile("b"), _profile("c")]
        config = dataclasses.replace(
            _make_config(tmp_path),
            review_pool=profiles,
            synthesis_profile=None,
            retry=RetryPolicy(
                max_dev_iterations=2,
                max_review_cycles=2,
                review_quorum_threshold=2,
                max_review_transport_retries=0,
            ),
        )
        task = TaskStory(
            name="Test Story",
            story_path=tmp_path / "spec.md",
            slug="test-story",
        )
        task.story_path.write_text(STORY_CONTENT, encoding="utf-8")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState(review_cycle=0, log_dir=tmp_path / "logs")

        # Two of three reviewers blow their budget share this cycle: they are
        # withdrawn as a spend decision, so only one seat can answer.
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_YAML, profile_name="a", cost_usd=0.10),
            _make_agent_result(success=True, output=APPROVE_YAML, profile_name="b", cost_usd=5.00),
            _make_agent_result(success=True, output=APPROVE_YAML, profile_name="c", cost_usd=5.00),
        ]

        meta = ReviewCycleMetadata(pool_models=[], successful=[], failed=[], synthesized=False)
        _successful, _failed, merged, _, _ = _run_review_pool(
            state,
            config,
            task,
            STORY_CONTENT,
            workspace,
            "branch",
            meta,
            notify=False,
            enforce_budgets=True,
        )

        # Threshold 2 against a nominal pool of 3 was unreachable with one
        # eligible seat; measured against the eligible seat it is met.
        assert meta.quorum_threshold == 1
        assert meta.quorum_met is True
        assert merged is not None
        assert state.phase != Phase.ESCALATE
