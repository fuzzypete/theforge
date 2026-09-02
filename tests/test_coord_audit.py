"""Tests for coord_audit helper functions."""

from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import (
    _PREFLIGHT_RESULT,
    APPROVE_REVIEW,
    REQUEST_CHANGES_REVIEW,
    _make_agent_result,
    _make_config,
    _make_task,
    _shell_with_gate,
    patch_gate_shell,
)
from landing_evidence_test_helpers import publish_landed

from theforge.coordinator import audit_substrate
from theforge.coordinator.audit import generate_audit_log, has_review_approve
from theforge.coordinator.engine import run_task
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.runners import AgentResult
from theforge.task import TaskStory


def _seed_substrate(project_root: Path, records: list[dict]) -> None:
    """Stamp a run_id on each record (when missing) and write it to the substrate.

    The runtime substrate read path does not auto-import history.jsonl any
    more; tests bootstrap by writing native rows directly.
    """
    stamped = []
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        copy = dict(rec)
        copy.setdefault("run_id", f"test-run-{i:04d}")
        stamped.append(copy)
    audit_substrate.seed_records(project_root, stamped)


def _make_result(state: CoordinatorState) -> CoordinatorResult:
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")


class TestPlanValidationAuditBlock:
    """plan_validation block shape: empty findings vs skipped vs populated."""

    def test_clean_pass_emits_empty_findings_not_none(self, tmp_path: Path) -> None:
        """When plan ran and produced zero findings, block must be present with findings=[]."""
        state = CoordinatorState()
        state.plan_structured = {"steps": []}  # non-None: plan ran
        state.plan_validation_findings = []
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        pv = log["plan_validation"]
        assert pv is not None
        assert pv["skipped"] is False
        assert pv["findings"] == []
        assert pv["finding_count"] == 0

    def test_skipped_when_plan_structured_is_none(self, tmp_path: Path) -> None:
        """When plan_structured is None (plan didn't run), block must have skipped=True."""
        state = CoordinatorState()
        assert state.plan_structured is None
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        pv = log["plan_validation"]
        assert pv["skipped"] is True

    def test_findings_present_when_populated(self, tmp_path: Path) -> None:
        """When plan ran and produced findings, they appear in the block."""
        state = CoordinatorState()
        state.plan_structured = {"steps": []}
        state.plan_validation_findings = [{"severity": "P2", "description": "missing file"}]
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        pv = log["plan_validation"]
        assert pv["skipped"] is False
        assert pv["finding_count"] == 1
        assert pv["findings"][0]["description"] == "missing file"


class TestDurationAndCostNoneChecks:
    """Duration and cost fields with a legitimate 0.0 value must not be masked."""

    def test_preflight_duration_zero_preserved_in_totals(self, tmp_path: Path) -> None:
        """preflight_duration_s=0.0 is a real measurement; must appear in total duration."""
        state = CoordinatorState()
        state.preflight_duration_s = 0.0
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        # totals.duration_s must include the 0.0 contribution (sum stays 0.0 but shouldn't error)
        assert log["totals"]["duration_s"] is not None

    def test_reviewer_cost_zero_included_in_per_reviewer(self, tmp_path: Path) -> None:
        """A reviewer with cost_usd=0.0 must appear in per_reviewer with cost=0.0."""
        state = CoordinatorState()
        r = AgentResult(
            success=True,
            output="ok",
            session_id=None,
            cost_usd=0.0,
            exit_code=0,
            raw={},
            profile_name="fast-reviewer",
        )
        state.review_agent_results.append(r)
        state.review_durations.append(5.0)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        per_reviewer = log["phases"]["review"]["per_reviewer"]
        # A reviewer with cost_usd=0.0 must still appear in per_reviewer (not masked by or 0.0)
        assert "fast-reviewer" in per_reviewer
        assert per_reviewer["fast-reviewer"]["cost"] == 0.0

    def test_preflight_cache_snapshot_and_validation_appear_in_audit(self, tmp_path: Path) -> None:
        """Preflight audit block serializes cache git-state details for diagnosis."""
        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        state.preflight_reason = "ok"
        state.preflight_cache_snapshot = {
            "worktree_head": "abc123",
            "evaluation_base_branch": "main",
            "evaluation_base_branch_head": "def456",
        }
        state.preflight_cache_validation = {
            "status": "invalidated",
            "reason": "worktree_head_changed",
            "cached_worktree_head": "abc123",
            "current_worktree_head": "fedcba",
        }

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert log["preflight"]["cache_snapshot"]["worktree_head"] == "abc123"
        assert log["preflight"]["cache_validation"]["reason"] == "worktree_head_changed"


class TestUnmeasuredCostPreservedInAudit:
    """A kill-path run with unmeasured cost (None) must stay null in the audit.

    Covers the runner -> coordinator-state -> audit seam: an AgentResult whose
    cost_usd is None (e.g. killed before its cost-bearing result event, no usage
    reconstructable) must serialize to null in every coordinator audit surface,
    never a coerced 0.0 that reads as "genuinely free".
    """

    @staticmethod
    def _unmeasured(profile_name: str = "agent") -> AgentResult:
        return AgentResult(
            success=False,
            output="TIMEOUT: Agent exceeded limit",
            session_id=None,
            cost_usd=None,
            exit_code=-9,
            raw={},
            profile_name=profile_name,
        )

    def test_dev_phase_cost_null_not_zero(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.dev_results.append(self._unmeasured())
        state.dev_durations.append(5.0)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["phases"]["dev"]["cost_usd"] is None

    def test_review_phase_and_per_reviewer_cost_null_not_zero(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.review_agent_results.append(self._unmeasured(profile_name="slow-reviewer"))
        state.review_durations.append(5.0)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        review = log["phases"]["review"]
        assert review["cost_usd"] is None
        assert review["per_reviewer"]["slow-reviewer"]["cost"] is None

    def test_preflight_phase_cost_null_not_zero(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        state.preflight_result = self._unmeasured(profile_name="preflight")
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["phases"]["preflight"]["cost_usd"] is None

    def test_plan_phase_cost_null_not_zero(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.plan_results.append(self._unmeasured(profile_name="planner"))
        state.plan_durations.append(5.0)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["phases"]["plan"]["cost_usd"] is None

    def test_plan_review_cost_null_not_zero_on_both_surfaces(self, tmp_path: Path) -> None:
        """Both plan_review cost surfaces (phases block AND top-level block) stay null."""
        state = CoordinatorState()
        state.plan_review_decision = "APPROVE"
        state.plan_review_results.append(self._unmeasured(profile_name="plan-reviewer"))
        state.plan_review_durations.append(5.0)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        # phases.plan_review.cost_usd and the sibling top-level plan_review.cost_usd
        # read the same measured source, so neither may coerce None to 0.0.
        assert log["phases"]["plan_review"]["cost_usd"] is None
        assert log["plan_review"]["cost_usd"] is None

    def test_totals_and_cost_summary_null_when_any_phase_unmeasured(self, tmp_path: Path) -> None:
        state = CoordinatorState()
        state.dev_results.append(self._unmeasured())
        state.dev_durations.append(5.0)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["totals"]["cost_usd"] is None
        assert log["cost"]["total_usd"] is None
        assert log["cost"]["dev_usd"] is None

    def test_measured_zero_still_records_zero_not_null(self, tmp_path: Path) -> None:
        """A genuinely free run (cost 0.0) stays 0.0 — the fix must not null it out."""
        state = CoordinatorState()
        r = AgentResult(
            success=True,
            output="ok",
            session_id=None,
            cost_usd=0.0,
            exit_code=0,
            raw={},
            profile_name="free-dev",
        )
        state.dev_results.append(r)
        state.dev_durations.append(5.0)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["phases"]["dev"]["cost_usd"] == 0.0

    def test_serialize_plan_review_result_preserves_none(self) -> None:
        from theforge.coordinator.audit import _serialize_plan_review_result

        entry = _serialize_plan_review_result(self._unmeasured(profile_name="pr"), attempt=0)
        assert entry["cost_usd"] is None
        assert entry["verdict"] == "CRASHED"


class TestHasReviewApprove:
    def test_no_history_file(self, tmp_path: Path) -> None:
        """Fresh repo (no audit inputs) returns False (safe default)."""
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_empty_substrate(self, tmp_path: Path) -> None:
        """Substrate exists but has no rows: returns False."""
        _seed_substrate(tmp_path, [])
        # Nothing seeded — touch the audits dir so has_audit_inputs evaluates the substrate path
        audit_substrate.create_or_open(tmp_path).close()
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_approve_present(self, tmp_path: Path) -> None:
        """Returns True when a matching slug has an APPROVE review."""
        record = {
            "task": {"slug": "my-spec", "name": "My Spec"},
            "reviews": [{"cycle": 1, "verdict": "APPROVE", "summary": "Good"}],
        }
        _seed_substrate(tmp_path, [record])
        assert has_review_approve(tmp_path, "my-spec") is True

    def test_no_approve_request_changes(self, tmp_path: Path) -> None:
        """Returns False when reviews exist but none are APPROVE."""
        record = {
            "task": {"slug": "my-spec", "name": "My Spec"},
            "reviews": [{"cycle": 1, "verdict": "REQUEST_CHANGES", "summary": "Fix this"}],
        }
        _seed_substrate(tmp_path, [record])
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_approve_for_different_slug(self, tmp_path: Path) -> None:
        """Returns False when APPROVE exists but for a different slug."""
        record = {
            "task": {"slug": "other-spec", "name": "Other Spec"},
            "reviews": [{"cycle": 1, "verdict": "APPROVE", "summary": "Good"}],
        }
        _seed_substrate(tmp_path, [record])
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_approve_in_second_record(self, tmp_path: Path) -> None:
        """Returns True when APPROVE is in second record, first has REQUEST_CHANGES."""
        rec1 = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "REQUEST_CHANGES"}],
        }
        rec2 = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        _seed_substrate(tmp_path, [rec1, rec2])
        assert has_review_approve(tmp_path, "my-spec") is True

    def test_no_reviews_key(self, tmp_path: Path) -> None:
        """Returns False when record has no 'reviews' key (e.g. ALREADY_DONE run)."""
        record = {"task": {"slug": "my-spec"}, "outcome": {"final_phase": "DONE"}}
        _seed_substrate(tmp_path, [record])
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_empty_reviews_list(self, tmp_path: Path) -> None:
        """Returns False when reviews list is empty."""
        record = {"task": {"slug": "my-spec"}, "reviews": []}
        _seed_substrate(tmp_path, [record])
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_stale_approve_branch_ahead(self, tmp_path: Path) -> None:
        """Returns False when APPROVE exists but branch has unmerged commits (abandoned run)."""
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        _seed_substrate(tmp_path, [record])
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"3\n", stderr=b"")
        with patch("theforge.coordinator.audit.subprocess.run", return_value=mock_result):
            assert has_review_approve(tmp_path, "my-spec", "main") is False

    def test_valid_approve_branch_merged(self, tmp_path: Path) -> None:
        """Returns True when APPROVE exists and branch is merged (0 commits ahead)."""
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        _seed_substrate(tmp_path, [record])
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"0\n", stderr=b"")
        with patch("theforge.coordinator.audit.subprocess.run", return_value=mock_result):
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_valid_approve_branch_absent(self, tmp_path: Path) -> None:
        """Returns True when APPROVE exists and branch is absent (non-zero git exit)."""
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        _seed_substrate(tmp_path, [record])
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=128, stdout=b"", stderr=b"unknown revision"
        )
        with patch("theforge.coordinator.audit.subprocess.run", return_value=mock_result):
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_require_landed_rejects_failed_landing(self, tmp_path: Path) -> None:
        """Landed-only mode ignores APPROVE records whose landing failed."""
        record = {
            "task": {"slug": "my-spec"},
            "landing_status": "failed",
            "reviews": [{"verdict": "APPROVE"}],
        }
        _seed_substrate(tmp_path, [record])
        assert has_review_approve(tmp_path, "my-spec", require_landed=True) is False

    def test_require_landed_accepts_landed_approve(self, tmp_path: Path) -> None:
        """Landed-only mode keeps working for APPROVE records that actually landed.

        "Actually landed" now means a published landing assertion (#2849), which
        is why the record carries no ``landing_status`` here: the flattened
        column is written at completion, before a queued PR resolves, and is no
        longer what this query reads.
        """
        record = {
            "run_id": "landed-run",
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        _seed_substrate(tmp_path, [record])
        publish_landed(tmp_path, "landed-run", slug="my-spec")
        assert has_review_approve(tmp_path, "my-spec", require_landed=True) is True

    def test_require_landed_rejects_flattened_landed_without_evidence(
        self, tmp_path: Path
    ) -> None:
        """A completion-time snapshot is not an observation (#2849).

        ``landing_status='landed'`` with no assertion means nobody has observed
        the landing — unresolved, not landed — so the landed query must not
        answer yes on the strength of the column alone.
        """
        record = {
            "run_id": "snapshot-run",
            "task": {"slug": "my-spec"},
            "landing_status": "landed",
            "reviews": [{"verdict": "APPROVE"}],
        }
        _seed_substrate(tmp_path, [record])
        assert has_review_approve(tmp_path, "my-spec", require_landed=True) is False

    def test_no_approve_record_with_base_branch(self, tmp_path: Path) -> None:
        """Returns False when no APPROVE record exists (baseline for new signature)."""
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "REQUEST_CHANGES"}],
        }
        _seed_substrate(tmp_path, [record])
        assert has_review_approve(tmp_path, "my-spec", "main") is False

    def test_stale_approve_subprocess_timeout(self, tmp_path: Path) -> None:
        """Returns True (treat as valid) when git subprocess times out."""
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        _seed_substrate(tmp_path, [record])
        with patch(
            "theforge.coordinator.audit.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
        ):
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_stale_approve_non_integer_output(self, tmp_path: Path) -> None:
        """Returns True (treat as valid) when git outputs non-integer."""
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        _seed_substrate(tmp_path, [record])
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"not-a-number\n", stderr=b""
        )
        with patch("theforge.coordinator.audit.subprocess.run", return_value=mock_result):
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_custom_branch_pattern_passed_to_helper(self, tmp_path: Path) -> None:
        """Branch name is forwarded to git — verifies non-default branch patterns work."""
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        _seed_substrate(tmp_path, [record])
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"0\n", stderr=b"")
        with patch(
            "theforge.coordinator.audit.subprocess.run", return_value=mock_result
        ) as mock_run:
            result = has_review_approve(tmp_path, "my-spec", "main", branch="forge/my-spec")
        assert result is True
        call_args = mock_run.call_args[0][0]
        assert any("forge/my-spec" in arg for arg in call_args)


class TestDevHandoffsInAudit:
    """dev_handoffs key must appear in audit log and contain per-iteration snapshots."""

    def test_dev_handoffs_in_audit(self, tmp_path: Path) -> None:
        """Audit log must include dev_handoffs with source, path, and handoff per iteration."""
        state = CoordinatorState()
        content1 = {"gate_decision": "PASS", "dev_notes": "iteration 1 notes"}
        content2 = {"gate_decision": "PASS", "dev_notes": "iteration 2 notes"}
        snap1 = {"source": "file", "path": None, "handoff": content1}
        snap2 = {"source": "structured_output", "path": "/some/path.yaml", "handoff": content2}
        state.dev_handoff_snapshots.append(snap1)
        state.dev_handoff_snapshots.append(snap2)

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert "dev_handoffs" in log
        handoffs = log["dev_handoffs"]
        assert len(handoffs) == 2
        assert handoffs[0]["iteration"] == 1
        assert handoffs[0]["source"] == "file"
        assert handoffs[0]["path"] is None
        assert handoffs[0]["handoff"] == content1
        assert handoffs[1]["iteration"] == 2
        assert handoffs[1]["source"] == "structured_output"
        assert handoffs[1]["handoff"] == content2

    def test_dev_handoffs_empty_when_no_dev_calls(self, tmp_path: Path) -> None:
        """dev_handoffs is an empty list when no dev invocations occurred."""
        state = CoordinatorState()
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["dev_handoffs"] == []

    def test_dev_handoffs_none_entry_when_handoff_absent(self, tmp_path: Path) -> None:
        """A missing snapshot uses source=missing and handoff=None in the audit."""
        state = CoordinatorState()
        state.dev_handoff_snapshots.append({"source": "missing", "path": None, "handoff": None})
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["dev_handoffs"][0]["handoff"] is None
        assert log["dev_handoffs"][0]["source"] == "missing"

    def test_dev_prompt_injections_in_audit(self, tmp_path: Path) -> None:
        """Audit log includes finding IDs injected into each dev prompt."""
        state = CoordinatorState()
        state.dev_prompt_injected_finding_ids.append([])
        state.dev_prompt_injected_finding_ids.append(["aaa", "bbb"])

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert log["dev_prompt_injections"] == [
            {"iteration": 1, "finding_ids": []},
            {"iteration": 2, "finding_ids": ["aaa", "bbb"]},
        ]

    def test_dev_prompt_injections_remain_aligned_with_timeout_resume_passes(
        self, tmp_path: Path
    ) -> None:
        """Timeout resumes still emit an audit row, even when no findings were injected."""
        state = CoordinatorState()
        state.dev_results.extend([_make_agent_result(), _make_agent_result()])
        state.dev_prompt_injected_finding_ids.extend([[], []])

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert len(log["dev_prompt_injections"]) == len(state.dev_results)
        assert log["dev_prompt_injections"][1] == {"iteration": 2, "finding_ids": []}


class TestFixPromptDispositions:
    """build_fix_prompt() must annotate P1 findings with their disposition."""

    def test_fix_prompt_includes_p1_dispositions(self, tmp_path: Path) -> None:
        """classified_p1s must appear with [disposition] prefix in the fix prompt."""

        from theforge.coordinator.state import FindingRecord
        from theforge.task import build_fix_prompt

        task = TaskStory(name="My Task", slug="my-task", story_path=tmp_path / "spec.md")
        (tmp_path / "spec.md").write_text("# spec", encoding="utf-8")

        p1s = [
            FindingRecord(
                finding_id="aaa",
                cycle_first_seen=1,
                cycle_last_seen=2,
                file="src/foo.py",
                line=42,
                severity="P1",
                description="Off by one error",
                reporter="reviewer",
                disposition="regression",
            ),
            FindingRecord(
                finding_id="bbb",
                cycle_first_seen=1,
                cycle_last_seen=2,
                file="src/bar.py",
                line=None,
                severity="P1",
                description="Missing validation",
                reporter="reviewer",
                disposition="unresolved",
            ),
        ]

        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path,
            branch_name="feat/my-task",
            review_findings="Raw findings text here.",
            gate_command="make gate",
            classified_p1s=p1s,
        )

        assert "[regression]" in prompt
        assert "[unresolved]" in prompt
        assert "Off by one error" in prompt
        assert "Missing validation" in prompt

    def test_fix_prompt_no_classified_p1s_uses_review_findings(self, tmp_path: Path) -> None:
        """When classified_p1s is None, review_findings renders as normal."""
        from theforge.task import build_fix_prompt

        task = TaskStory(name="My Task", slug="my-task", story_path=tmp_path / "spec.md")
        (tmp_path / "spec.md").write_text("# spec", encoding="utf-8")

        prompt = build_fix_prompt(
            task,
            workspace_path=tmp_path,
            branch_name="feat/my-task",
            review_findings="Some findings.",
            gate_command="make gate",
        )

        assert "Some findings." in prompt
        assert "[regression]" not in prompt
        assert "[unresolved]" not in prompt


# ── Audit timing tests ──────────────────────────────────────────────────


class TestCoordinatorAuditTiming:
    """Test that audit log includes timing and started_at fields."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_started_at_set_in_state(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """CoordinatorState.started_at is set when run_task() begins."""
        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)

        assert result.state.started_at is not None
        # Should be a valid ISO timestamp

        dt = datetime.datetime.fromisoformat(result.state.started_at)
        assert dt.tzinfo is not None  # timezone-aware

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_audit_log_timing_fields(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """generate_audit_log() includes started_at, finished_at, duration_seconds."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        timing = audit["timing"]
        assert "started_at" in timing
        assert "finished_at" in timing
        assert "duration_seconds" in timing
        assert timing["started_at"] is not None
        assert timing["finished_at"] is not None
        assert timing["duration_seconds"] is not None
        assert timing["duration_seconds"] >= 0


# ── Audit agent breakdown tests ─────────────────────────────────────────


class TestCoordinatorAuditAgentBreakdown:
    """Test per-agent cost breakdown in audit log."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_cost_agents_list(self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path):
        """cost.agents contains one entry per dev and review invocation."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        dev_result_30 = AgentResult(
            success=True,
            output="Done.",
            session_id="s1",
            cost_usd=0.30,
            exit_code=0,
            raw={},
            profile_name="dev",
        )
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = dev_result_30
        mock_pool.return_value = [
            AgentResult(
                success=True,
                output=APPROVE_REVIEW,
                session_id="s2",
                cost_usd=0.20,
                exit_code=0,
                raw={},
                profile_name="review",
            )
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        agents = audit["cost"]["agents"]
        # Every agent-invoking phase is listed, not just dev + review (#2205):
        # "every invocation records its ledger" has to be a property of this
        # list, so preflight is here too.
        assert [a["role"] for a in agents] == ["preflight", "dev", "review"]

        dev_entry = next(a for a in agents if a["role"] == "dev")
        assert dev_entry["profile"] == "dev"
        assert dev_entry["cost_usd"] == 0.30
        assert "duration_seconds" in dev_entry
        assert dev_entry["duration_seconds"] is not None
        assert dev_entry["duration_seconds"] >= 0

        review_entry = next(a for a in agents if a["role"] == "review")
        assert review_entry["profile"] == "review"
        assert review_entry["cost_usd"] == 0.20
        assert review_entry["duration_seconds"] is not None
        assert review_entry["duration_seconds"] >= 0


# ── Audit findings tests ────────────────────────────────────────────────


class TestCoordinatorAuditFindings:
    """Test that review findings are included in audit log."""

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_review_findings_in_audit(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """Audit reviews[] entries include findings list with severity, file, line, description."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result()

        call_count = {"pool": 0}

        def pool_side_effect(**kwargs):
            call_count["pool"] += 1
            if call_count["pool"] <= 1:
                return [
                    _make_agent_result(
                        success=True, output=REQUEST_CHANGES_REVIEW, profile_name="review"
                    )
                ]
            return [_make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")]

        mock_pool.side_effect = pool_side_effect

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        assert len(audit["reviews"]) == 2

        # First review has findings
        first_rev = audit["reviews"][0]
        assert "findings" in first_rev
        assert first_rev["p1_count"] == 1
        assert len(first_rev["findings"]) == 1
        finding = first_rev["findings"][0]
        assert finding["severity"] == "P1"
        assert finding["file"] == "src/foo.py"
        assert finding["line"] == 10
        assert "Off by one" in finding["description"]

        # Second review (APPROVE) has empty findings
        second_rev = audit["reviews"][1]
        assert "findings" in second_rev
        assert second_rev["findings"] == []
        assert second_rev["p1_count"] == 0

    @patch("theforge.coordinator.review_pool.run_agent_pool")
    @patch("theforge.coordinator.preflight_flow.run_agent")
    @patch("theforge.coordinator.dev_phase.run_agent")
    @patch_gate_shell()
    def test_approve_review_has_empty_findings(
        self, mock_shell, mock_agent, mock_preflight, mock_pool, tmp_path
    ):
        """APPROVE review in audit has findings: [] (not missing key)."""

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        workspace = tmp_path / "test-task"
        workspace.mkdir()

        mock_shell.side_effect = _shell_with_gate(workspace, "PASS")
        mock_preflight.return_value = _PREFLIGHT_RESULT
        mock_agent.return_value = _make_agent_result(success=True, output="Done.")
        mock_pool.return_value = [
            _make_agent_result(success=True, output=APPROVE_REVIEW, profile_name="review")
        ]

        result = run_task(config, task)
        audit = generate_audit_log(config, task, result)

        rev = audit["reviews"][0]
        assert "findings" in rev
        assert rev["findings"] == []


# ── Audit start/stop phase tests ────────────────────────────────────────


class TestAuditStartStopPhase:
    """Audit log records start/stop phases."""

    def test_audit_records_start_stop_phase(self, tmp_path):
        from theforge.coordinator.state import CoordinatorState

        state = CoordinatorState()
        state.start_phase = Phase.DEV
        state.stop_phase = Phase.VALIDATE

        from theforge.coordinator.audit import generate_audit_log
        from theforge.coordinator.state import CoordinatorResult

        result = CoordinatorResult(
            success=True,
            phase=Phase.VALIDATE,
            state=state,
            message="Stopped at --until validate",
        )
        task = _make_task(tmp_path)
        audit = generate_audit_log(_make_config(tmp_path), task, result)

        assert audit["outcome"]["start_phase"] == "DEV"
        assert audit["outcome"]["stop_phase"] == "VALIDATE"

    def test_audit_none_start_stop_when_unset(self, tmp_path):
        from theforge.coordinator.state import CoordinatorState

        state = CoordinatorState()

        from theforge.coordinator.audit import generate_audit_log
        from theforge.coordinator.state import CoordinatorResult

        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
        )
        task = _make_task(tmp_path)
        audit = generate_audit_log(_make_config(tmp_path), task, result)

        assert audit["outcome"]["start_phase"] is None
        assert audit["outcome"]["stop_phase"] is None


class TestSprintStoryAuditHistory:
    def test_write_story_audit_persists_to_substrate(self, tmp_path: Path) -> None:
        """Story audit goes into rebuildable native storage; legacy history.jsonl is gone."""
        from theforge.sprint.audit import _write_story_audit

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        state.log_dir = tmp_path / ".forge" / "logs" / task.slug
        state.workspace_path = tmp_path / task.slug
        state.workspace_path.mkdir(parents=True)
        state.branch_name = f"forge/{task.slug}"
        state.run_id = "test-run-001"
        result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")

        _write_story_audit(config, task, result)

        run_file = audit_substrate.runs_dir(tmp_path) / "test-run-001.json"
        assert run_file.exists()

        # Substrate is the query path and must point at the canonical run file.
        sub_path = audit_substrate.substrate_path(tmp_path)
        assert sub_path.exists()
        conn = audit_substrate.open_readonly(tmp_path)
        try:
            row = conn.execute(
                "SELECT source_path FROM audit_records WHERE run_id = ?",
                ("test-run-001",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["source_path"] == ".forge/audits/runs/test-run-001.json"

        audit_substrate.rebuild_from_runs(tmp_path)
        conn = audit_substrate.open_readonly(tmp_path)
        try:
            rebuilt = audit_substrate.latest_record_for(conn, run_id="test-run-001")
        finally:
            conn.close()
        assert rebuilt is not None
        assert rebuilt["task"]["slug"] == task.slug

        # Legacy jsonl path must NOT be written.
        history_path = tmp_path / ".forge" / "audits" / "history.jsonl"
        assert not history_path.exists()

    def test_write_story_audit_repairs_malformed_existing_run_file(self, tmp_path: Path) -> None:
        from theforge.sprint.audit import _write_story_audit

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        scenarios = {
            "invalid-json": "not json{{{",
            "non-object": "[1, 2, 3]",
        }

        for run_id, seeded in scenarios.items():
            state = CoordinatorState()
            state.log_dir = tmp_path / ".forge" / "logs" / task.slug
            state.workspace_path = tmp_path / task.slug
            state.workspace_path.mkdir(parents=True, exist_ok=True)
            state.branch_name = f"forge/{task.slug}"
            state.run_id = run_id
            result = CoordinatorResult(
                success=True,
                phase=Phase.DONE,
                state=state,
                message="done",
            )

            run_file = audit_substrate.runs_dir(tmp_path) / f"{run_id}.json"
            run_file.parent.mkdir(parents=True, exist_ok=True)
            run_file.write_text(seeded, encoding="utf-8")

            _write_story_audit(config, task, result)

            persisted = json.loads(run_file.read_text(encoding="utf-8"))
            assert isinstance(persisted, dict)
            assert persisted["run_id"] == run_id
            assert persisted["task"]["slug"] == task.slug

            conn = audit_substrate.open_readonly(tmp_path)
            try:
                row = audit_substrate.latest_record_for(conn, run_id=run_id)
            finally:
                conn.close()
            assert row is not None
            assert row["task"]["slug"] == task.slug

        summary = audit_substrate.rebuild_from_runs(tmp_path)
        assert summary.failed == 0

        for run_id in scenarios:
            conn = audit_substrate.open_readonly(tmp_path)
            try:
                rebuilt = audit_substrate.latest_record_for(conn, run_id=run_id)
                source_row = conn.execute(
                    "SELECT source_path FROM audit_records WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            finally:
                conn.close()
            assert rebuilt is not None
            assert rebuilt["task"]["slug"] == task.slug
            assert source_row is not None
            assert source_row["source_path"] == f".forge/audits/runs/{run_id}.json"

    def test_write_story_audit_logs_generate_failure(self, tmp_path: Path, capsys) -> None:
        from theforge.sprint.audit import _write_story_audit

        config = _make_config(tmp_path)
        task = _make_task(tmp_path)
        state = CoordinatorState()
        result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="done")

        with patch(
            "theforge.coordinator.audit.generate_audit_log", side_effect=RuntimeError("boom")
        ):
            _write_story_audit(config, task, result)

        captured = capsys.readouterr()
        assert "failed to generate story audit log" in captured.err
        assert task.slug in captured.err
