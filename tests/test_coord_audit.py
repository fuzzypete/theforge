"""Tests for coord_audit helper functions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    ForgeConfig,
    RetryPolicy,
    ValidationConfig,
    WorkspaceConfig,
)
from theforge.coordinator.audit import generate_audit_log, has_review_approve
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.runners import AgentResult
from theforge.task import TaskStory


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=ValidationConfig(
            gate_command="make gate",
            handoff_file="handoff.yaml",
            gate_decision_key="gate_result",
        ),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
    )


def _make_task(tmp_path: Path) -> TaskStory:
    spec_path = tmp_path / "spec.md"
    spec_path.write_text("# Test spec", encoding="utf-8")
    return TaskStory(name="Test Task", slug="test-task", story_path=spec_path)


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


class TestHasReviewApprove:
    def test_no_history_file(self, tmp_path: Path) -> None:
        """Missing history.jsonl returns False (safe default)."""
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_empty_history_file(self, tmp_path: Path) -> None:
        """Empty history.jsonl returns False."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        (audits / "history.jsonl").write_text("", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_approve_present(self, tmp_path: Path) -> None:
        """Returns True when a matching slug has an APPROVE review."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec", "name": "My Spec"},
            "reviews": [{"cycle": 1, "verdict": "APPROVE", "summary": "Good"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is True

    def test_no_approve_request_changes(self, tmp_path: Path) -> None:
        """Returns False when reviews exist but none are APPROVE."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec", "name": "My Spec"},
            "reviews": [{"cycle": 1, "verdict": "REQUEST_CHANGES", "summary": "Fix this"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_approve_for_different_slug(self, tmp_path: Path) -> None:
        """Returns False when APPROVE exists but for a different slug."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "other-spec", "name": "Other Spec"},
            "reviews": [{"cycle": 1, "verdict": "APPROVE", "summary": "Good"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_approve_in_second_record(self, tmp_path: Path) -> None:
        """Returns True when APPROVE is in second record, first has REQUEST_CHANGES."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        rec1 = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "REQUEST_CHANGES"}],
        }
        rec2 = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        content = json.dumps(rec1) + "\n" + json.dumps(rec2) + "\n"
        (audits / "history.jsonl").write_text(content, encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is True

    def test_no_reviews_key(self, tmp_path: Path) -> None:
        """Returns False when record has no 'reviews' key (e.g. ALREADY_DONE run)."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {"task": {"slug": "my-spec"}, "outcome": {"final_phase": "DONE"}}
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_malformed_json_line_skipped(self, tmp_path: Path) -> None:
        """Malformed JSON lines are skipped; valid lines still checked."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        content = "not-valid-json\n" + json.dumps(record) + "\n"
        (audits / "history.jsonl").write_text(content, encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is True

    def test_empty_reviews_list(self, tmp_path: Path) -> None:
        """Returns False when reviews list is empty."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {"task": {"slug": "my-spec"}, "reviews": []}
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec") is False

    def test_stale_approve_branch_ahead(self, tmp_path: Path) -> None:
        """Returns False when APPROVE exists but branch has unmerged commits (abandoned run)."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"3\n", stderr=b"")
        with patch("theforge.coordinator.audit.subprocess.run", return_value=mock_result):
            assert has_review_approve(tmp_path, "my-spec", "main") is False

    def test_valid_approve_branch_merged(self, tmp_path: Path) -> None:
        """Returns True when APPROVE exists and branch is merged (0 commits ahead)."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"0\n", stderr=b"")
        with patch("theforge.coordinator.audit.subprocess.run", return_value=mock_result):
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_valid_approve_branch_absent(self, tmp_path: Path) -> None:
        """Returns True when APPROVE exists and branch is absent (non-zero git exit)."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=128, stdout=b"", stderr=b"unknown revision"
        )
        with patch("theforge.coordinator.audit.subprocess.run", return_value=mock_result):
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_no_approve_record_with_base_branch(self, tmp_path: Path) -> None:
        """Returns False when no APPROVE record exists (baseline for new signature)."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "REQUEST_CHANGES"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert has_review_approve(tmp_path, "my-spec", "main") is False

    def test_stale_approve_subprocess_timeout(self, tmp_path: Path) -> None:
        """Returns True (treat as valid) when git subprocess times out."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        with patch(
            "theforge.coordinator.audit.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
        ):
            # Timeout → helper returns False → APPROVE is not stale → True
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_stale_approve_non_integer_output(self, tmp_path: Path) -> None:
        """Returns True (treat as valid) when git outputs non-integer."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"not-a-number\n", stderr=b""
        )
        with patch("theforge.coordinator.audit.subprocess.run", return_value=mock_result):
            # ValueError from int() → helper returns False → APPROVE is not stale → True
            assert has_review_approve(tmp_path, "my-spec", "main") is True

    def test_custom_branch_pattern_passed_to_helper(self, tmp_path: Path) -> None:
        """Branch name is forwarded to git — verifies non-default branch patterns work."""
        audits = tmp_path / ".forge" / "audits"
        audits.mkdir(parents=True)
        record = {
            "task": {"slug": "my-spec"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        (audits / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"0\n", stderr=b"")
        with patch(
            "theforge.coordinator.audit.subprocess.run", return_value=mock_result
        ) as mock_run:
            result = has_review_approve(tmp_path, "my-spec", "main", branch="forge/my-spec")
        assert result is True
        # Verify the custom branch pattern was used in the git command
        call_args = mock_run.call_args[0][0]
        assert any("forge/my-spec" in arg for arg in call_args)


class TestDevHandoffsInAudit:
    """dev_handoffs key must appear in audit log and contain per-iteration snapshots."""

    def test_dev_handoffs_in_audit(self, tmp_path: Path) -> None:
        """Audit log must include dev_handoffs with one entry per dev invocation."""
        state = CoordinatorState()
        snap1 = {"gate_decision": "PASS", "dev_notes": "iteration 1 notes"}
        snap2 = {"gate_decision": "PASS", "dev_notes": "iteration 2 notes"}
        state.dev_handoff_snapshots.append(snap1)
        state.dev_handoff_snapshots.append(snap2)

        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))

        assert "dev_handoffs" in log
        handoffs = log["dev_handoffs"]
        assert len(handoffs) == 2
        assert handoffs[0]["iteration"] == 1
        assert handoffs[0]["handoff"] == snap1
        assert handoffs[1]["iteration"] == 2
        assert handoffs[1]["handoff"] == snap2

    def test_dev_handoffs_empty_when_no_dev_calls(self, tmp_path: Path) -> None:
        """dev_handoffs is an empty list when no dev invocations occurred."""
        state = CoordinatorState()
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["dev_handoffs"] == []

    def test_dev_handoffs_none_entry_when_handoff_absent(self, tmp_path: Path) -> None:
        """A None snapshot (handoff file absent) is preserved as None in the audit."""
        state = CoordinatorState()
        state.dev_handoff_snapshots.append(None)
        log = generate_audit_log(_make_config(tmp_path), _make_task(tmp_path), _make_result(state))
        assert log["dev_handoffs"][0]["handoff"] is None


class TestFixPromptDispositions:
    """build_fix_prompt() must annotate P1 findings with their disposition."""

    def test_fix_prompt_includes_p1_dispositions(self, tmp_path: Path) -> None:
        """classified_p1s must appear with [disposition] prefix in the fix prompt."""

        from theforge.coordinator.state import FindingRecord
        from theforge.task import TaskStory, build_fix_prompt

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
        from theforge.task import TaskStory, build_fix_prompt

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
