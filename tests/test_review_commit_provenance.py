"""Reviewed-commit provenance in audit records (#2052).

A review verdict recorded without the commit it judged, and without that
commit's verification state, is indistinguishable from a stale verdict that
later commits already superseded. These tests pin the provenance at each seam
it has to survive: capture at cycle open, rendering into the run audit record,
and forwarding through the operator-facing sprint summary projections.
"""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path

import yaml

from theforge.coordinator.audit_render import build_reviews
from theforge.coordinator.review_phase import _record_reviewed_commit_provenance
from theforge.coordinator.state import (
    CoordinatorState,
    ReviewCycleMetadata,
    ReviewedCommitVerification,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "first")
    return repo


def _meta() -> ReviewCycleMetadata:
    return ReviewCycleMetadata(pool_models=["r1"], successful=["r1"], failed=[], synthesized=False)


class TestVerificationDerivation:
    """The verification state is derived from the gate record, never verdict text."""

    def test_gate_on_the_reviewed_commit_is_gate_passed(self) -> None:
        v = ReviewedCommitVerification.derive(
            reviewed_commit="abc", gate_commit="abc", gate_decision="PASS", gate_runs=1
        )
        assert v.state == "gate_passed"
        assert v.gate_commit == "abc"
        assert v.gate_runs == 1

    def test_failing_gate_on_the_reviewed_commit_is_gate_failed(self) -> None:
        v = ReviewedCommitVerification.derive(
            reviewed_commit="abc", gate_commit="abc", gate_decision="FAIL"
        )
        assert v.state == "gate_failed"

    def test_gate_error_on_the_reviewed_commit_is_not_a_pass(self) -> None:
        v = ReviewedCommitVerification.derive(
            reviewed_commit="abc", gate_commit="abc", gate_decision="ERROR"
        )
        assert v.state == "gate_failed"

    def test_gate_on_an_earlier_commit_is_stale(self) -> None:
        """The central case: the verdict is about code the gate never saw."""
        v = ReviewedCommitVerification.derive(
            reviewed_commit="def", gate_commit="abc", gate_decision="PASS"
        )
        assert v.state == "gate_stale"

    def test_story_override_reads_as_skipped_not_passed(self) -> None:
        v = ReviewedCommitVerification.derive(
            reviewed_commit="abc", gate_commit="abc", gate_decision="SKIPPED"
        )
        assert v.state == "gate_skipped"

    def test_no_gate_yet_is_ungated(self) -> None:
        v = ReviewedCommitVerification.derive(
            reviewed_commit="abc", gate_commit=None, gate_decision=None
        )
        assert v.state == "ungated"

    def test_unresolvable_commit_is_unknown(self) -> None:
        v = ReviewedCommitVerification.derive(
            reviewed_commit=None, gate_commit="abc", gate_decision="PASS"
        )
        assert v.state == "unknown"


class TestCaptureAtCycleOpen:
    """Cycle open must stamp the HEAD reviewers are about to read."""

    def test_records_head_and_gate_state_for_the_same_commit(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        head = _git(repo, "rev-parse", "HEAD")

        state = CoordinatorState()
        state.last_gate_commit = head
        state.last_gate_decision = "PASS"
        state.gate_runs = 2
        state.validate_blocks = [{"kind": "gate_fail"}]

        meta = _meta()
        _record_reviewed_commit_provenance(state, meta, repo)

        assert meta.reviewed_commit == head
        assert meta.verification is not None
        assert meta.verification.state == "gate_passed"
        assert meta.verification.gate_runs == 2
        assert meta.verification.validate_blocks == 1

    def test_commit_after_the_gate_ran_records_as_stale(self, tmp_path: Path) -> None:
        """A dev commit landed after VALIDATE must not read as gated."""
        repo = _init_repo(tmp_path)
        gated = _git(repo, "rev-parse", "HEAD")
        state = CoordinatorState()
        state.last_gate_commit = gated
        state.last_gate_decision = "PASS"
        state.gate_runs = 1

        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        _git(repo, "commit", "-aqm", "second")
        new_head = _git(repo, "rev-parse", "HEAD")

        meta = _meta()
        _record_reviewed_commit_provenance(state, meta, repo)

        assert meta.reviewed_commit == new_head
        assert meta.verification is not None
        assert meta.verification.state == "gate_stale"
        assert meta.verification.gate_commit == gated

    def test_no_gate_run_records_ungated(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        meta = _meta()
        _record_reviewed_commit_provenance(CoordinatorState(), meta, repo)
        assert meta.reviewed_commit
        assert meta.verification is not None
        assert meta.verification.state == "ungated"


class TestGateProvenanceCapture:
    """VALIDATE must record which commit the gate actually judged."""

    def test_gate_run_records_commit_and_decision(self, tmp_path: Path) -> None:
        from theforge.coordinator.validate_phase import _record_gate_commit

        repo = _init_repo(tmp_path)
        state = CoordinatorState()
        _record_gate_commit(state, repo, "FAIL")
        assert state.last_gate_commit == _git(repo, "rev-parse", "HEAD")
        assert state.last_gate_decision == "FAIL"

    def test_provenance_survives_resume(self, tmp_path: Path) -> None:
        """Without persistence a resumed run reports every cycle as ungated."""
        from theforge.coordinator.run_setup import load_trajectory_state, save_trajectory_state

        workspace = tmp_path / "ws"
        workspace.mkdir()
        state = CoordinatorState()
        state.last_gate_commit = "e" * 40
        state.last_gate_decision = "PASS"
        state.gate_runs = 3
        save_trajectory_state(workspace, state)

        restored = CoordinatorState()
        load_trajectory_state(workspace, restored)
        assert restored.last_gate_commit == "e" * 40
        assert restored.last_gate_decision == "PASS"
        assert restored.gate_runs == 3


class TestAuditRendering:
    """Every rendered review cycle names its commit and verification state."""

    def test_rendered_cycle_carries_commit_and_verification(self) -> None:
        state = CoordinatorState()
        meta = _meta()
        meta.reviewed_commit = "a" * 40
        meta.verification = ReviewedCommitVerification.derive(
            reviewed_commit="a" * 40,
            gate_commit="a" * 40,
            gate_decision="PASS",
            gate_runs=1,
        )
        state.review_cycle_metadata.append(meta)

        entry = build_reviews(state)[0]
        assert entry["commit"] == "a" * 40
        assert entry["verification"]["state"] == "gate_passed"
        assert entry["verification"]["gate_decision"] == "PASS"
        assert entry["verification"]["gate_commit"] == "a" * 40

    def test_legacy_metadata_renders_explicit_unknown_not_current(self) -> None:
        """Records from before this change must not read as verified."""
        state = CoordinatorState()
        state.review_cycle_metadata.append(_meta())

        entry = build_reviews(state)[0]
        assert entry["commit"] is None
        assert entry["verification"]["state"] == "unknown"

    def test_existing_review_keys_are_preserved(self) -> None:
        state = CoordinatorState()
        state.review_cycle_metadata.append(_meta())
        entry = build_reviews(state)[0]
        for key in ("cycle", "pool_models", "successful", "failed", "quorum_met"):
            assert key in entry


class TestSprintSummaryProjections:
    """Sprint-level summaries must not drop the provenance the record carries."""

    def test_story_summary_forwards_verdict_provenance(self, tmp_path: Path) -> None:
        from theforge.sprint.audit import _load_story_summary_entry_from_audit

        sprint_dir = tmp_path / "sprint"
        story_dir = sprint_dir / "issue-2052"
        story_dir.mkdir(parents=True)
        (story_dir / "audit.yaml").write_text(
            yaml.dump(
                {
                    "outcome": {"final_phase": "DONE", "success": True},
                    "reviews": [
                        {
                            "cycle": 1,
                            "verdict": "APPROVE",
                            "commit": "b" * 40,
                            "verification": {"state": "gate_stale", "gate_decision": "PASS"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        entry = _load_story_summary_entry_from_audit(sprint_dir, "issue:2052", "issue-2052")
        assert entry is not None
        assert entry["verdict"] == "APPROVE"
        assert entry["verdict_commit"] == "b" * 40
        assert entry["verdict_verification_state"] == "gate_stale"

    def test_story_summary_marks_legacy_reviews_unknown(self, tmp_path: Path) -> None:
        from theforge.sprint.audit import _load_story_summary_entry_from_audit

        sprint_dir = tmp_path / "sprint"
        story_dir = sprint_dir / "issue-2052"
        story_dir.mkdir(parents=True)
        (story_dir / "audit.yaml").write_text(
            yaml.dump(
                {
                    "outcome": {"final_phase": "DONE", "success": True},
                    "reviews": [{"cycle": 1, "verdict": "APPROVE"}],
                }
            ),
            encoding="utf-8",
        )

        entry = _load_story_summary_entry_from_audit(sprint_dir, "issue:2052", "issue-2052")
        assert entry is not None
        assert entry["verdict_commit"] is None
        assert entry["verdict_verification_state"] == "unknown"

    def test_sprint_audit_reviews_summary_keeps_provenance(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from theforge.sprint.audit import _write_sprint_audit
        from theforge.sprint.manifest import ResolvedSprint, SprintResult

        meta = _meta()
        meta.reviewed_commit = "c" * 40
        meta.verification = ReviewedCommitVerification.derive(
            reviewed_commit="c" * 40, gate_commit="d" * 40, gate_decision="PASS"
        )

        story_state = MagicMock()
        story_state.preflight_cached = False
        story_state.preflight_verdict = "PROCEED"
        story_state.preflight_reason = None
        story_state.preflight_cached_original_verdict = None
        story_state.preflight_cached_from_run_id = None
        story_state.total_cost = 0.05
        story_state.error = None
        story_state.error_type = None
        story_state.review_results = []
        story_state.review_cycle_metadata = [meta]
        story_state.dev_iteration_telemetry = []
        story_state.review_iteration_telemetry = []

        phase = MagicMock()
        phase.name = "DONE"
        result = MagicMock()
        result.state = story_state
        result.phase = phase
        result.merge = None

        started = datetime.datetime(2026, 7, 31, 9, 0, tzinfo=datetime.timezone.utc)
        _write_sprint_audit(
            manifest=ResolvedSprint(name="s", budget_usd=10.0, stories=[]),
            result=SprintResult(
                name="s",
                specs_total=1,
                specs_succeeded=1,
                specs_failed=0,
                specs_skipped=0,
                total_cost_usd=0.05,
                budget_usd=10.0,
                results=[("issue:2052", result)],
                stopped_reason=None,
            ),
            canonical_refs=["issue:2052"],
            started_at=started,
            finished_at=started + datetime.timedelta(seconds=300),
            duration=300.0,
            project_root=tmp_path,
            slug_map={"issue:2052": "issue-2052"},
        )

        data = yaml.safe_load(
            (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
        )
        cycle = data["specs"][0]["reviews"][0]
        assert cycle["commit"] == "c" * 40
        assert cycle["verification_state"] == "gate_stale"
        assert cycle["gate_decision"] == "PASS"
