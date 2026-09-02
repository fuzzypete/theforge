"""Seam coverage for policy-assertion provenance at preflight intake (#2137).

The behaviour crosses a phase boundary: preflight adjudicates, and the verdict it
produces then travels through the batch-preflight cache, the resume sidecar, and
the run audit. Unit coverage of the adjudicator alone would not catch a field
that fails to survive one of those hops — which is how a ratified refusal would
lose the ability to name its assertion.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from coord_test_helpers import (
    _make_agent_result,
    _make_config,
    _make_task,
    patch_gate_shell,
)

from theforge.coordinator import audit_storage
from theforge.coordinator.audit import generate_audit_log
from theforge.coordinator.engine import run_task
from theforge.coordinator.preflight_cache import apply_cached_preflight_state
from theforge.coordinator.preflight_flow import _handle_preflight_verdict
from theforge.coordinator.resume_persistence import (
    apply_resume_record_to_state,
    load_resume_record,
    save_resume_record,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.policy_provenance import policy_assertions_path

_EFFORT_ASSERTION = "Reasoning effort is intentionally not score-controlled."

# A BLOCKED that cites the #1108 rationale as a standing architectural decision.
PREFLIGHT_BLOCKED_ON_POLICY = """\
```yaml
verdict: BLOCKED
complexity: small
complexity_score: 2
reason: "Story contradicts an already-implemented, deliberate architectural decision."
blocking_basis: policy_assertion
policy_assertions_cited:
  - text: "Reasoning effort is intentionally not score-controlled."
    source: "docs/guides/routing-policy.md:52"
    claimed_provenance: unknown
criteria_checked: []
```
"""

# A BLOCKED with a real hard blocker and no policy claim in it.
PREFLIGHT_BLOCKED_ON_CREDENTIALS = """\
```yaml
verdict: BLOCKED
complexity: small
complexity_score: 2
reason: "The deploy API key is absent and the dev agent cannot create it."
blocking_basis: missing_credentials
policy_assertions_cited: []
criteria_checked: []
```
"""

# A BLOCKED that mentions architecture incidentally while blocking for an
# unrelated hard reason — the prose fallback must not fire on it.
PREFLIGHT_BLOCKED_MENTIONS_ARCHITECTURE = """\
```yaml
verdict: BLOCKED
complexity: small
complexity_score: 2
reason: "The architecture package this story extends does not exist on any index."
blocking_basis: missing_dependency
policy_assertions_cited: []
criteria_checked: []
```
"""


def _write_ratified(project_root: Path) -> None:
    path = policy_assertions_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(
            {
                "version": 1,
                "assertions": [
                    {
                        "id": "effort-not-scored",
                        "text": _EFFORT_ASSERTION,
                        "provenance": "ratified",
                        "reference": "docs/adr/0006-adaptive-router.md#clause-4",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _blocked_policy_state(*, ratified: bool) -> CoordinatorState:
    """A state as preflight leaves it after adjudicating a policy-founded BLOCKED."""
    state = CoordinatorState()
    state.preflight_verdict = "BLOCKED"
    state.preflight_reason = "Story contradicts a standing decision."
    state.preflight_complexity = "small"
    state.preflight_blocking_basis = "policy_assertion"
    state.preflight_policy_assertions_cited = [
        {"text": _EFFORT_ASSERTION, "source": "docs/guides/routing-policy.md:52"}
    ]
    state.preflight_policy_assertions_resolved = [
        {
            "text": _EFFORT_ASSERTION,
            "provenance": "ratified" if ratified else "generated",
            "carries_blocking_authority": ratified,
            "label": (
                f'"{_EFFORT_ASSERTION}" — ratified (docs/adr/0006.md#clause-4)'
                if ratified
                else f'"{_EFFORT_ASSERTION}" — generated (no ratification recorded)'
            ),
        }
    ]
    state.preflight_policy_blocking_authority = ratified
    if not ratified:
        state.preflight_policy_retraction_candidates = [{"assertion": _EFFORT_ASSERTION}]
        state.preflight_policy_ratification_candidates = [{"assertion": _EFFORT_ASSERTION}]
    return state


# ── Live preflight ────────────────────────────────────────────────────


class TestLivePreflightAdjudication:
    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_generated_only_conflict_enters_the_sprint(
        self, mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
    ):
        """The #1108 regression: chartered work blocked only by run-authored prose."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / "test-task").mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BLOCKED_ON_POLICY, cost_usd=0.08
        )
        mock_dev.return_value = _make_agent_result(success=True, output="Implemented.")
        mock_plan.side_effect = mock_dev
        mock_pool.return_value = []

        result = run_task(config, task, stop_phase=Phase.PREFLIGHT)

        state = result.state
        assert state.preflight_verdict == "PROCEED"
        assert result.phase is Phase.PREFLIGHT
        assert state.preflight_policy_blocking_authority is False
        assert state.preflight_policy_retraction_candidates
        assert state.preflight_policy_retraction_candidates[0]["assertion"] == _EFFORT_ASSERTION
        assert state.preflight_policy_ratification_candidates
        assert any("downgraded to PROCEED" in w for w in state.preflight_warnings)

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_ratified_conflict_still_blocks_and_names_the_assertion(
        self, mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / "test-task").mkdir()
        _write_ratified(tmp_path)

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BLOCKED_ON_POLICY, cost_usd=0.08
        )

        result = run_task(config, task)

        assert result.success is False
        assert result.phase is Phase.ESCALATE
        assert result.state.preflight_verdict == "BLOCKED"
        assert result.state.preflight_policy_blocking_authority is True
        # The refusal names the assertion and its provenance class, so the
        # operator can see which kind of authority stopped the work.
        assert _EFFORT_ASSERTION in result.state.error
        assert "ratified" in result.state.error
        assert "docs/adr/0006-adaptive-router.md#clause-4" in result.state.error
        mock_pool.assert_not_called()

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_missing_credentials_blocker_is_not_downgraded(
        self, mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
    ):
        """Non-policy blockers must keep blocking with no registry in sight."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / "test-task").mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BLOCKED_ON_CREDENTIALS, cost_usd=0.08
        )

        result = run_task(config, task)

        assert result.phase is Phase.ESCALATE
        assert result.state.preflight_verdict == "BLOCKED"
        assert result.state.preflight_policy_adjudication == {}

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_blocker_mentioning_architecture_incidentally_is_not_downgraded(
        self, mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
    ):
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / "test-task").mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BLOCKED_MENTIONS_ARCHITECTURE, cost_usd=0.08
        )

        result = run_task(config, task)

        assert result.phase is Phase.ESCALATE
        assert result.state.preflight_verdict == "BLOCKED"

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.plan_flow.run_agent")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_preflight_artifact_records_the_adjudication(
        self, mock_shell, mock_dev, mock_preflight, mock_plan, mock_pool, tmp_path
    ):
        """preflight.yaml carries the provenance fields, not just the verdict."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        (tmp_path / "test-task").mkdir()
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        mock_shell.return_value = (True, "OK", 0, False)
        mock_preflight.return_value = _make_agent_result(
            success=True, output=PREFLIGHT_BLOCKED_ON_POLICY, cost_usd=0.08
        )

        with patch("theforge.coordinator.preflight_flow._write_log_artifact") as mock_artifact:
            run_task(config, task, stop_phase=Phase.PREFLIGHT)

        written = {call.args[1]: call.args[2] for call in mock_artifact.call_args_list}
        artifact = yaml.safe_load(written["preflight.yaml"])
        assert artifact["blocking_basis"] == "policy_assertion"
        assert artifact["policy_blocking_authority"] is False
        assert artifact["policy_retraction_candidates"]
        assert artifact["policy_assertions_resolved"][0]["provenance"] == "generated"


# ── Cached, resumed, and audited paths ────────────────────────────────


class TestProvenanceSurvivesStateHandoff:
    def test_cached_preflight_carries_provenance_to_the_story_run(self) -> None:
        """A batch preflight adjudicates; the story's own run must inherit it."""
        cached = _blocked_policy_state(ratified=True)
        live = CoordinatorState()

        apply_cached_preflight_state(live, cached)

        assert live.preflight_blocking_basis == "policy_assertion"
        assert live.preflight_policy_blocking_authority is True
        assert live.preflight_policy_assertions_resolved[0]["text"] == _EFFORT_ASSERTION

    def test_cached_downgrade_carries_its_candidates(self) -> None:
        cached = _blocked_policy_state(ratified=False)
        live = CoordinatorState()

        apply_cached_preflight_state(live, cached)

        assert live.preflight_policy_retraction_candidates
        assert live.preflight_policy_ratification_candidates
        assert live.preflight_policy_blocking_authority is False

    def test_resume_record_round_trips_provenance(self, tmp_path: Path) -> None:
        saved = _blocked_policy_state(ratified=True)

        assert save_resume_record(tmp_path, saved, slug="test-task") is not None
        record = load_resume_record(tmp_path, "test-task")
        assert record is not None

        resumed = CoordinatorState()
        apply_resume_record_to_state(resumed, record)

        assert resumed.preflight_blocking_basis == "policy_assertion"
        assert resumed.preflight_policy_blocking_authority is True
        assert resumed.preflight_policy_assertions_resolved[0]["text"] == _EFFORT_ASSERTION

    def test_resumed_blocked_refusal_still_names_its_assertion(self, tmp_path: Path) -> None:
        """The dispatch rebuilds the refusal text from restored state, not from the run."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _blocked_policy_state(ratified=True)

        _config, result, already_done = _handle_preflight_verdict(
            verdict="BLOCKED",
            reason=state.preflight_reason,
            state=state,
            config=config,
            task=task,
            branch_name="forge/test-task",
            notify=False,
            logger=None,
            task_start=0.0,
        )

        assert already_done is False
        assert result is not None and result.success is False
        assert _EFFORT_ASSERTION in state.error
        assert "ratified" in state.error

    def test_downgraded_verdict_leaves_no_ratified_refusal_text(self, tmp_path: Path) -> None:
        """A generated-only assertion must never appear as blocking authority."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _blocked_policy_state(ratified=False)

        _config, result, _already_done = _handle_preflight_verdict(
            verdict="BLOCKED",
            reason=state.preflight_reason,
            state=state,
            config=config,
            task=task,
            branch_name="forge/test-task",
            notify=False,
            logger=None,
            task_start=0.0,
        )

        assert result is not None
        assert "ratified policy assertion" not in state.error

    def test_audit_record_carries_the_provenance_fields(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = _blocked_policy_state(ratified=False)
        state.started_at = "2026-01-01T00:00:00+00:00"
        state.run_id = "deadbeefcafe"

        record = generate_audit_log(
            config,
            task,
            CoordinatorResult(success=False, phase=Phase.ESCALATE, state=state, message="x"),
        )

        preflight = record["preflight"]
        assert preflight["blocking_basis"] == "policy_assertion"
        assert preflight["policy_blocking_authority"] is False
        assert preflight["policy_retraction_candidates"]
        assert preflight["policy_ratification_candidates"]


class TestRecordMigration:
    def test_v32_record_reads_with_empty_policy_provenance(self) -> None:
        """An old run cited nothing structurally; empty states that, not a citation."""
        migrated = audit_storage._migrate_v32_to_v33(
            {"preflight": {"verdict": "BLOCKED", "reason": "old"}}
        )

        preflight = migrated["preflight"]
        assert preflight["blocking_basis"] is None
        assert preflight["policy_assertions_cited"] == []
        assert preflight["policy_assertions_resolved"] == []
        assert preflight["policy_retraction_candidates"] == []
        assert preflight["policy_ratification_candidates"] == []
        assert preflight["policy_blocking_authority"] is False
        assert preflight["policy_adjudication"] == {}
        # The original block is not mutated in place (ADR-0002 refusal-to-forget).
        assert preflight["verdict"] == "BLOCKED"

    def test_record_without_a_preflight_block_is_left_alone(self) -> None:
        record = {"preflight": None, "task": {"slug": "x"}}

        assert audit_storage._migrate_v32_to_v33(record) == record

    def test_v33_record_reads_with_legacy_prior_run_defaults(self) -> None:
        record = {
            "context_manifests": [
                {
                    "phase": "dev",
                    "prior_run_context": {
                        "enabled": True,
                        "included": [],
                        "dropped": [],
                        "note": "legacy",
                    },
                },
                {
                    "phase": "review",
                    "prior_run_context": {
                        "enabled": False,
                        "included": [],
                        "dropped": [],
                        "note": "disabled",
                    },
                },
            ],
            "knowledge_summary": {
                "status": "written",
                "attempted": True,
                "written": True,
            },
        }

        migrated = audit_storage._migrate_v33_to_v34(record)

        prior_dev = migrated["context_manifests"][0]["prior_run_context"]
        prior_review = migrated["context_manifests"][1]["prior_run_context"]
        assert prior_dev["index_state"] == "ready"
        assert prior_review["index_state"] is None
        assert migrated["knowledge_summary"]["index_rebuild"] is None

    def test_migration_is_registered_for_the_current_version(self) -> None:
        assert audit_storage.CURRENT_RECORD_SCHEMA_VERSION == 44
        assert audit_storage.MIGRATION_HELPERS[43] is audit_storage._migrate_v43_to_v44
        assert audit_storage.MIGRATION_HELPERS[42] is audit_storage._migrate_v42_to_v43
        assert audit_storage.MIGRATION_HELPERS[41] is audit_storage._migrate_v41_to_v42
        assert audit_storage.MIGRATION_HELPERS[40] is audit_storage._migrate_v40_to_v41
        assert audit_storage.MIGRATION_HELPERS[39] is audit_storage._migrate_v39_to_v40
        assert audit_storage.MIGRATION_HELPERS[38] is audit_storage._migrate_v38_to_v39
        assert audit_storage.MIGRATION_HELPERS[37] is audit_storage._migrate_v37_to_v38
        # The prior step stays registered: migration is a chain, and dropping a
        # link would strand every record written before it.
        assert audit_storage.MIGRATION_HELPERS[36] is audit_storage._migrate_v36_to_v37
