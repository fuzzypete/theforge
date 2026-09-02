"""Tests for sprint resume/triage: _triage_spec, resume logic, depends_on parsing."""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from landing_evidence_test_helpers import publish_landed
from sprint_test_helpers import run_sprint_ctx

import theforge.pending as pending
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import audit_substrate
from theforge.coordinator.state import (
    CoordinatorResult,
    CoordinatorState,
    EntryGateOutcome,
    GateRunFacts,
    Phase,
)
from theforge.sprint.audit import (
    persist_accepted_unmeasured_spend,
    persist_accumulated_story_state,
)
from theforge.sprint.dag import StoryTriage, _triage_spec
from theforge.sprint.lock import acquire_story_locks, release_story_locks
from theforge.sprint.manifest import ResolvedSprint, _build_task_from_story
from theforge.sprint.runner import _read_prior_sprint_cost, _run_fresh
from theforge.sprint.sources import GitHubIssueSource
from theforge.task import TaskStory

# ── Helpers ──────────────────────────────────────────────────────────


def _is_issue_grep(cmd: list[str], issue_number: int) -> bool:
    """True when ``cmd`` is the base-commit closing-reference scan for the issue.

    Matches on the presence of a ``--grep=`` argument mentioning the issue
    rather than its exact spelling, so these mocks do not silently stop
    matching when the prefilter pattern changes (#2374).
    """
    return cmd[:2] == ["git", "log"] and any(
        c.startswith("--grep=") and str(issue_number) in c for c in cmd
    )


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_spec_file(
    tmp_path: Path, name: str, slug: str, depends_on: list[str] | None = None
) -> Path:
    spec = tmp_path / f"{slug}.md"
    frontmatter = f"name: {name}\nslug: {slug}"
    if depends_on is not None:
        if len(depends_on) == 1:
            frontmatter += f"\ndepends_on: {depends_on[0]}"
        else:
            frontmatter += "\ndepends_on:\n" + "".join(f"  - {d}\n" for d in depends_on)
    spec.write_text(
        f"---\n{frontmatter}\n---\n# {name}\nDo the thing.",
        encoding="utf-8",
    )
    return spec


def _make_manifest(tmp_path: Path, specs: list[str], budget: float = 10.0) -> Path:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        yaml.dump(
            {
                "name": "Test Sprint",
                "budget_usd": budget,
                "specs": specs,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _make_gen1_manifest(tmp_path: Path, budget: float = 10.0) -> Path:
    """First-generation manifest on its own path.

    Must not reuse ``sprint.yaml`` — the second generation reads that path, and
    overwriting it would silently shrink the sprint rather than exercising a
    re-exec of the same one.
    """
    manifest_path = tmp_path / "sprint-gen1.yaml"
    manifest_path.write_text(
        yaml.dump({"name": "Test Sprint", "budget_usd": budget, "specs": ["feature-a.md"]}),
        encoding="utf-8",
    )
    return manifest_path


def _set_sprint_id(
    tmp_path: Path,
    sprint_name: str = "Test Sprint",
    sprint_id: str = "sprint-123",
) -> str:
    sprint_dir = tmp_path / ".forge" / "logs" / sprint_name
    sprint_dir.mkdir(parents=True, exist_ok=True)
    (sprint_dir / ".sprint_id").write_text(sprint_id, encoding="utf-8")
    return sprint_id


def _write_prior_sprint_audit(tmp_path: Path, sprint_id: str, total_cost_usd: float) -> None:
    audits_dir = tmp_path / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    with open(audits_dir / "sprint-audit.yaml", "w", encoding="utf-8") as f:
        yaml.dump({"sprint": {"sprint_id": sprint_id, "total_cost_usd": total_cost_usd}}, f)


def _write_incomplete_prior_sprint_audit(
    tmp_path: Path,
    sprint_id: str,
    *,
    total_cost_measured_usd: float,
    unmeasured_spend_sources: list[str],
) -> None:
    audits_dir = tmp_path / ".forge" / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    with open(audits_dir / "sprint-audit.yaml", "w", encoding="utf-8") as f:
        yaml.dump(
            {
                "sprint": {
                    "sprint_id": sprint_id,
                    "total_cost_usd": None,
                    "total_cost_measured_usd": total_cost_measured_usd,
                    "cost_complete": False,
                    "unmeasured_spend_sources": unmeasured_spend_sources,
                }
            },
            f,
        )


def _make_coordinator_result(
    success: bool = True,
    cost: float = 1.0,
    preflight_verdict: str = "PROCEED",
    phase: Phase = Phase.DONE,
    merged: bool = False,
    landing_status: str | None = None,
) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = preflight_verdict
    # Fake cost via preflight result mock
    mock_preflight = MagicMock()
    mock_preflight.cost_usd = cost
    state.preflight_result = mock_preflight
    return CoordinatorResult(
        success=success,
        phase=phase,
        state=state,
        message="Done." if success else "Failed.",
        merge={"merged": True} if merged else None,
        landing_status=landing_status,
    )


# ── Sprint resume / triage tests ─────────────────────────────────────


class TestTriageSpec:
    def test_triage_merged_spec(self, tmp_path: Path) -> None:
        """Branch already merged to base → skip_merged."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 0  # is ancestor
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"3"  # base is 3 commits ahead of branch — truly merged
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert "merged" in triage.reason

    def test_triage_branch_at_base_head_not_merged(self, tmp_path: Path) -> None:
        """Branch at base HEAD with 0 commits ahead → full (not skip_merged)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 0  # is ancestor (trivially — same commit)
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"0"  # base not ahead of branch — created at base HEAD, not merged
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert triage.reason == "branch is at main HEAD with 0 commits ahead"

    def test_triage_worktree_with_passing_gate(self, tmp_path: Path) -> None:
        """Worktree exists, commits ahead, gate passes → review."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        # Create fake worktree directory
        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1  # not merged
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b"abc123 some commit\n"
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint.dag._run_gate", return_value=("PASS", None, "")):
                triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "review"
        assert triage.worktree_path == worktree

    def test_triage_worktree_with_failing_gate(self, tmp_path: Path) -> None:
        """Worktree exists, commits ahead, gate fails → dev."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b"abc123 some commit\n"
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint.dag._run_gate", return_value=("FAIL", "tests failed", "")):
                triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "dev"
        assert triage.worktree_path == worktree

    def test_triage_gate_timeout_carries_structured_outcome(self, tmp_path: Path) -> None:
        """A reuse gate killed at its budget routes to DEV *and* says so (#2796)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b"abc123 some commit\n"
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        def _timed_out_gate(*_args, facts_out=None, **_kwargs):
            if facts_out is not None:
                facts_out.append(
                    GateRunFacts(
                        timed_out=True, timeout_s=360, command="make gate", exit_code=None
                    )
                )
            return None, "Gate timed out after 360s", "collected 4000 items"

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint.dag._run_gate", side_effect=_timed_out_gate):
                triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "dev"
        assert triage.gate_outcome is not None
        assert triage.gate_outcome.outcome == "timeout"
        assert triage.gate_outcome.timeout_s == 360
        assert triage.gate_outcome.command == "make gate"
        assert triage.gate_outcome.elapsed_s >= 0.0
        assert triage.gate_outcome.output_tail == "collected 4000 items"

    def test_triage_failing_gate_carries_no_timeout_outcome(self, tmp_path: Path) -> None:
        """A gate that failed is not reported as one that ran out of time (#2796)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b"abc123 some commit\n"
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        def _failing_gate(*_args, facts_out=None, **_kwargs):
            if facts_out is not None:
                facts_out.append(
                    GateRunFacts(timed_out=False, timeout_s=360, command="make gate", exit_code=1)
                )
            return "FAIL", None, "1 failed, 300 passed"

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint.dag._run_gate", side_effect=_failing_gate):
                triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "dev"
        assert triage.gate_outcome is None

    def test_triage_no_worktree(self, tmp_path: Path) -> None:
        """No worktree found → full."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1  # not merged
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert "no worktree" in triage.reason

    def test_triage_stale_worktree_no_commits(self, tmp_path: Path) -> None:
        """Worktree exists but 0 commits ahead of base → full (stale)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b""  # no commits ahead
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert "stale" in triage.reason or "0 commits" in triage.reason

    def test_triage_same_tip_missing_worktree_without_audit_runs_full(
        self, tmp_path: Path
    ) -> None:
        """Missing worktree at base HEAD with no audit trail stays stale/full."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "log" in cmd:
                m.returncode = 0
                m.stdout = b""
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"2"
            elif "--is-ancestor" in cmd:
                m.returncode = 1
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with (
            patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run),
            patch("theforge.sprint.dag.has_review_approve", return_value=False),
        ):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert triage.reason == "no worktree found"


class TestReadPriorSprintCost:
    def test_returns_zero_without_reexec_signal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sprint_id = _set_sprint_id(tmp_path)
        _write_prior_sprint_audit(tmp_path, sprint_id, 3.5)

        monkeypatch.delenv("FORGE_PREV_RUN_ID", raising=False)

        assert _read_prior_sprint_cost(tmp_path, sprint_id) == 0.0

    def test_returns_zero_for_different_sprint_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sprint_id = _set_sprint_id(tmp_path)
        _write_prior_sprint_audit(tmp_path, "other-sprint", 3.5)

        monkeypatch.setenv("FORGE_PREV_RUN_ID", "run-prev-123")

        assert _read_prior_sprint_cost(tmp_path, sprint_id) == 0.0

    def test_returns_prior_cost_for_same_sprint_reexec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sprint_id = _set_sprint_id(tmp_path)
        _write_prior_sprint_audit(tmp_path, sprint_id, 3.5)

        monkeypatch.setenv("FORGE_PREV_RUN_ID", "run-prev-123")

        assert _read_prior_sprint_cost(tmp_path, sprint_id) == pytest.approx(3.5)

    def test_progressive_state_can_exceed_stale_sprint_audit_during_reexec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sprint_id = _set_sprint_id(tmp_path)
        _write_prior_sprint_audit(tmp_path, sprint_id, 1.0)
        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "DONE",
                    "cost_usd": 3.5,
                    "story_run_id": "run-prev",
                }
            ],
        )

        monkeypatch.setenv("FORGE_PREV_RUN_ID", "run-prev-123")

        # Legacy helper still exposes sprint-audit.yaml; integration tests below
        # verify run_sprint now prefers progressive state for real re-exec
        # accounting.
        assert _read_prior_sprint_cost(tmp_path, sprint_id) == pytest.approx(1.0)

    def test_triage_same_tip_missing_worktree_with_prior_approve_skips_when_squash_merged(
        self, tmp_path: Path
    ) -> None:
        """Missing worktree plus audit-backed squash-merged branch is treated as merged."""

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {
            "task": {"slug": "feature-a"},
            "landing_status": "landed",
            "reviews": [{"verdict": "APPROVE"}],
        }
        record.setdefault("run_id", "sr-rec")
        audit_substrate.seed_records(tmp_path, [record])
        # The landed answer comes from the published assertion since #2849; the
        # flattened column above is the completion-time snapshot, not evidence.
        publish_landed(tmp_path, "sr-rec", slug="feature-a")

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "log" in cmd:
                m.returncode = 0
                m.stdout = b""
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"0"
            elif "--is-ancestor" in cmd:
                m.returncode = 0
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert "merged" in triage.reason

    def test_triage_issue_merged_via_closed_pr_uses_pr_specific_reason(
        self, tmp_path: Path
    ) -> None:
        """Closed merged PRs are skipped before workspace setup with a PR-specific reason."""
        _make_spec_file(tmp_path, "Issue 1102", "issue-1102")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if cmd[:3] == ["gh", "pr", "list"]:
                m.returncode = 0
                m.stdout = (
                    '[{"number":1111,"url":"https://github.com/o/r/pull/1111",'
                    '"mergedAt":"2026-05-01T12:34:56Z"}]'
                )
            elif _is_issue_grep(cmd, 1102):
                m.returncode = 0
                m.stdout = b""
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b""
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"2"
            elif "--is-ancestor" in cmd:
                m.returncode = 1
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with (
            patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run),
            patch("theforge.sprint.dag._is_issue_closed", return_value=True),
            patch("theforge.sprint.dag.has_review_approve", return_value=False),
        ):
            triage = _triage_spec("issue-1102.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert triage.reason == "already merged to main (evidence: merged PR #1111)"

    def test_triage_same_tip_failed_landing_audit_stays_full(self, tmp_path: Path) -> None:
        """A zero-delta APPROVE with failed landing stays eligible during resume."""

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {
            "task": {"slug": "feature-a"},
            "landing_status": "failed",
            "reviews": [{"verdict": "APPROVE"}],
        }
        record.setdefault("run_id", "sr-rec")
        audit_substrate.seed_records(tmp_path, [record])

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "log" in cmd:
                m.returncode = 0
                m.stdout = b""
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"0"
            elif "--is-ancestor" in cmd:
                m.returncode = 0
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert triage.reason == "branch is at main HEAD with 0 commits ahead"

    def test_triage_same_tip_worktree_with_prior_approve_skips_when_merged(
        self, tmp_path: Path
    ) -> None:
        """Same-tip branch with audit-backed FF merge should still skip_merged."""

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {
            "task": {"slug": "feature-a"},
            "landing_status": "landed",
            "reviews": [{"verdict": "APPROVE"}],
        }
        record.setdefault("run_id", "sr-rec")
        audit_substrate.seed_records(tmp_path, [record])
        # The landed answer comes from the published assertion since #2849; the
        # flattened column above is the completion-time snapshot, not evidence.
        publish_landed(tmp_path, "sr-rec", slug="feature-a")

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "log" in cmd:
                m.returncode = 0
                m.stdout = b""  # 0 commits ahead
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"0"  # branch and base at same tip
            elif "--is-ancestor" in cmd:
                m.returncode = 0
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert "merged" in triage.reason

    def test_triage_open_issue_with_landed_audit_skips_merged(self, tmp_path: Path) -> None:
        """A landed audit record wins over an open issue (#2111).

        Symptom bugs are held open pending verification after their fix merges,
        so an open issue is no longer contradictory evidence — the run's own
        landed APPROVE record is what establishes the merge.
        """

        _make_spec_file(tmp_path, "Issue 1071", "issue-1071")
        config = _make_config(tmp_path)

        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {
            "task": {"slug": "issue-1071"},
            "landing_status": "landed",
            "reviews": [{"verdict": "APPROVE"}],
        }
        record.setdefault("run_id", "sr-rec")
        audit_substrate.seed_records(tmp_path, [record])
        publish_landed(tmp_path, "sr-rec", slug="issue-1071")

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "log" in cmd:
                m.returncode = 0
                m.stdout = b""
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"0"
            elif "--is-ancestor" in cmd:
                m.returncode = 0
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with (
            patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run),
            patch("theforge.sprint.dag._is_issue_closed", return_value=False),
        ):
            triage = _triage_spec("issue-1071.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert "audit trail" in triage.reason

    def test_triage_open_issue_with_base_commit_closing_reference_skips_merged(
        self, tmp_path: Path
    ) -> None:
        """A base commit *closing* the open issue is enough to skip (#2111, #2374)."""

        _make_spec_file(tmp_path, "Issue 1072", "issue-1072")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if _is_issue_grep(cmd, 1072):
                m.returncode = 0
                m.stdout = b"fix(sprint): land it\n\nCloses #1072\n\x1e"
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b""
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"0"
            elif "--is-ancestor" in cmd:
                m.returncode = 1
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with (
            patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run),
            patch("theforge.sprint.dag._is_issue_closed", return_value=False),
            patch("theforge.sprint.dag.has_review_approve", return_value=False),
        ):
            triage = _triage_spec("issue-1072.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert triage.reason == (
            "already merged to main (evidence: closing reference in a main commit message)"
        )

    def test_triage_bare_issue_mention_with_preserved_work_does_not_skip(
        self, tmp_path: Path
    ) -> None:
        """The #2374 shape end-to-end: a passing mention must not discard preserved work.

        Base carries an unrelated configuration commit citing the issue as
        context. The branch is not an ancestor of base, has five commits of
        preserved work, and its last recorded run was unsuccessful with a
        REQUEST_CHANGES verdict and no landing. Every authoritative source says
        the story has not landed, so triage must not skip it.
        """

        _make_spec_file(tmp_path, "Issue 1074", "issue-1074")
        config = _make_config(tmp_path)
        worktree = tmp_path / "issue-1074"
        worktree.mkdir()

        record = {
            "task": {"slug": "issue-1074"},
            "run_id": "sr-1074",
            "landing_status": "",
            "outcome": {"success": False},
            "reviews": [{"verdict": "REQUEST_CHANGES"}],
        }
        audit_substrate.seed_records(tmp_path, [record])

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if _is_issue_grep(cmd, 1074):
                m.returncode = 0
                m.stdout = b"config: raise timeout, disable model\n\nContext: #1074\n\x1e"
            elif cmd[:2] == ["git", "log"]:
                m.returncode = 0
                m.stdout = b"aaa1 five\nbbb2 four\nccc3 three\nddd4 two\neee5 one\n"
            elif "rev-list" in cmd and "--count" in cmd:
                m.returncode = 0
                m.stdout = b"5"
            elif "--is-ancestor" in cmd:
                m.returncode = 1
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with (
            patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run),
            patch("theforge.sprint.dag.has_review_approve", return_value=False),
            patch(
                "theforge.sprint.dag._run_gate",
                return_value=("dev", "gate failed", ""),
            ),
        ):
            triage = _triage_spec("issue-1074.md", config, tmp_path)

        assert triage.action != "skip_merged"
        assert "merged" not in triage.reason

    def test_triage_open_issue_with_topology_merge_skips_merged(self, tmp_path: Path) -> None:
        """A topologically merged branch skips even while its issue is open (#2111)."""

        _make_spec_file(tmp_path, "Issue 1073", "issue-1073")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 0
                m.stdout = b""
            elif "rev-list" in cmd and "--count" in cmd:
                # Both directions non-zero: base advanced past a branch that had
                # unique work of its own.
                m.returncode = 0
                m.stdout = b"2"
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b""
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with (
            patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run),
            patch("theforge.sprint.dag._is_issue_closed", return_value=False),
            patch("theforge.sprint.dag.has_review_approve", return_value=False),
        ):
            triage = _triage_spec("issue-1073.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert triage.reason == (
            "already merged to main (evidence: branch merged into main history)"
        )

    def test_triage_worktree_with_prior_approve(self, tmp_path: Path) -> None:
        """Worktree has commits ahead and prior APPROVE in audit trail → skip."""

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        # Write an APPROVE record to history.jsonl
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {
            "task": {"slug": "feature-a"},
            "reviews": [{"verdict": "APPROVE"}],
        }
        record.setdefault("run_id", "sr-rec")
        audit_substrate.seed_records(tmp_path, [record])

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1  # not merged
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b"abc123 some commit\n"
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "skip_merged"
        assert "APPROVE" in triage.reason or "approve" in triage.reason.lower()
        assert triage.worktree_path is None

    def test_triage_gate_pass_no_approve_routes_to_review(self, tmp_path: Path) -> None:
        """Worktree with commits, gate passes, but no APPROVE → review (not skip)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1
            elif "log" in cmd:
                m.returncode = 0
                m.stdout = b"abc123 some commit\n"
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint.dag._run_gate", return_value=("PASS", None, "")):
                triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "review"


class TestResumeSprintSkipApproved:
    def test_resume_sprint_skips_approved(self, tmp_path: Path) -> None:
        """Resume sprint: spec with prior APPROVE is treated as already satisfied."""

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        # Write an APPROVE record
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {"task": {"slug": "feature-a"}, "reviews": [{"verdict": "APPROVE"}]}
        record.setdefault("run_id", "sr-rec")
        audit_substrate.seed_records(tmp_path, [record])

        skip_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="prior APPROVE in audit trail; branch already satisfied (2 commits ahead)",
            worktree_path=None,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=skip_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run_task:
                result = run_sprint_ctx(config, manifest_path, resume=True)

        mock_run_task.assert_not_called()
        assert result.specs_succeeded == 0
        assert result.specs_skipped == 1


class TestResumeSprintIntegration:
    def test_resume_sprint_skips_merged(self, tmp_path: Path) -> None:
        """End-to-end resume: merged spec counts as skipped/already done."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        merged_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=merged_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run:
                result = run_sprint_ctx(config, manifest_path, resume=True)

        mock_run.assert_not_called()
        assert result.specs_succeeded == 0
        assert result.specs_skipped == 1

    def test_resume_sprint_enters_dev(self, tmp_path: Path) -> None:
        """End-to-end resume: gate-failing worktree uses run_from_dev."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        dev_triage = StoryTriage(
            story_path="feature-a.md",
            action="dev",
            reason="gate fails",
            worktree_path=worktree,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec", return_value=dev_triage):
            with patch(
                "theforge.sprint.runner.run_from_dev", return_value=coord_result
            ) as mock_dev:
                with patch("theforge.sprint.runner.run_task") as mock_task:
                    result = run_sprint_ctx(config, manifest_path, resume=True)

        mock_dev.assert_called_once()
        mock_task.assert_not_called()
        assert result.specs_succeeded == 1

    def test_resume_sprint_forwards_entry_gate_timeout_to_dev(self, tmp_path: Path) -> None:
        """The triage timeout handoff reaches the coordinator, not just the log (#2796)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        outcome = EntryGateOutcome(
            outcome="timeout",
            command="make gate",
            timeout_s=360,
            elapsed_s=361.4,
            output_tail="collected 4000 items",
            profile="complete (merge authority)",
        )
        dev_triage = StoryTriage(
            story_path="feature-a.md",
            action="dev",
            reason="worktree exists, gate fails (Gate timed out after 360s)",
            worktree_path=worktree,
            gate_outcome=outcome,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec", return_value=dev_triage):
            with patch(
                "theforge.sprint.runner.run_from_dev", return_value=coord_result
            ) as mock_dev:
                with patch("theforge.sprint.runner.run_task"):
                    run_sprint_ctx(config, manifest_path, resume=True)

        assert mock_dev.call_args.kwargs["entry_gate_outcome"] is outcome

    def test_resume_budget_exhausted_merged_spec_still_succeeds(self, tmp_path: Path) -> None:
        """Merged spec stays skipped/already done even when budget is exhausted."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=1.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)

        # Prior run spent $2 — budget exhausted
        _write_prior_sprint_audit(tmp_path, sprint_id, 2.0)

        merged_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
        )
        full_triage = StoryTriage(
            story_path="feature-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )

        def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
            if "feature-a" in spec_path:
                return merged_triage
            return full_triage

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task") as mock_run:
                with patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev-123"}, clear=False):
                    result = run_sprint_ctx(config, manifest_path, resume=True)

        # Merged spec should remain already-done/skipped, not budget-skipped.
        assert result.specs_succeeded == 0
        assert result.specs_skipped == 2  # feature-a already done, feature-b budget
        mock_run.assert_not_called()

    def test_resume_sprint_enters_review(self, tmp_path: Path) -> None:
        """End-to-end resume: gate-passing worktree uses run_from_review."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        review_triage = StoryTriage(
            story_path="feature-a.md",
            action="review",
            reason="gate passes",
            worktree_path=worktree,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec", return_value=review_triage):
            with patch(
                "theforge.sprint.runner.run_from_review", return_value=coord_result
            ) as mock_rev:
                with patch("theforge.sprint.runner.run_task") as mock_task:
                    result = run_sprint_ctx(config, manifest_path, resume=True)

        mock_rev.assert_called_once()
        mock_task.assert_not_called()
        assert result.specs_succeeded == 1

    def test_resume_cost_continuity(self, tmp_path: Path) -> None:
        """Same-sprint re-exec carries prior cost forward into total_cost_usd."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)

        _write_prior_sprint_audit(tmp_path, sprint_id, 3.50)

        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.runner.run_task", return_value=coord_result):
                with patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev-123"}, clear=False):
                    result = run_sprint_ctx(config, manifest_path, resume=True)

        # total should be prior (3.50) + new (1.00)
        assert result.total_cost_usd == pytest.approx(4.50)

    def test_resume_different_sprint_does_not_carry_prior_cost(self, tmp_path: Path) -> None:
        """A new sprint starts at $0 even if a different sprint left audit cost behind."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=5.0)
        config = _make_config(tmp_path)
        _set_sprint_id(tmp_path, sprint_id="current-sprint")
        _write_prior_sprint_audit(tmp_path, "older-sprint", 6.0)

        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.runner.run_task", return_value=coord_result) as mock_run:
                with patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev-123"}, clear=False):
                    result = run_sprint_ctx(config, manifest_path, resume=True)

        mock_run.assert_called_once()
        assert result.specs_succeeded == 1
        assert result.total_cost_usd == pytest.approx(1.0)
        assert result.stopped_reason is None

    def test_resume_prior_cost_exceeds_budget(self, tmp_path: Path, capsys) -> None:
        """When prior cost already meets/exceeds budget, first spec is skipped."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=5.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)

        # Prior run already spent $6 (over the $5 budget)
        _write_prior_sprint_audit(tmp_path, sprint_id, 6.0)

        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run:
                with patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev-123"}, clear=False):
                    result = run_sprint_ctx(config, manifest_path, resume=True)

        err = capsys.readouterr().err
        # Spec should be skipped — prior cost alone exceeds budget
        mock_run.assert_not_called()
        assert result.specs_skipped == 1
        assert (
            result.stopped_reason
            == "Budget exhausted (sprint $0.00 + carried $6.00 = $6.00 >= $5.00)"
        )
        assert "Budget $5.00 · carried $6.00 · usable headroom $0.00" in err
        assert "Selected run cannot dispatch under the supplied ceiling" in err
        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        audit_data = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        assert audit_data["specs"][0]["error"] == (
            "budget exhausted (sprint $0.00 + carried $6.00 = $6.00 >= $5.00)"
        )

    def test_resume_prior_cost_exceeds_budget_skips_dependency_chain_for_budget(
        self, tmp_path: Path, capsys
    ) -> None:
        """Startup headroom refusal applies to downstream selected dependencies too."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b", depends_on=["feature-a"])
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=5.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)

        _write_prior_sprint_audit(tmp_path, sprint_id, 6.0)

        triages = {
            "feature-a.md": StoryTriage(
                story_path="feature-a.md",
                action="full",
                reason="no worktree found",
                worktree_path=None,
                slug="feature-a",
            ),
            "feature-b.md": StoryTriage(
                story_path="feature-b.md",
                action="full",
                reason="no worktree found",
                worktree_path=None,
                slug="feature-b",
            ),
        }

        def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
            return triages[Path(spec_path).name]

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task") as mock_run:
                with patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev-123"}, clear=False):
                    result = run_sprint_ctx(config, manifest_path, resume=True)

        err = capsys.readouterr().err
        mock_run.assert_not_called()
        assert result.specs_skipped == 2
        assert (
            result.stopped_reason
            == "Budget exhausted (sprint $0.00 + carried $6.00 = $6.00 >= $5.00)"
        )
        assert "Budget $5.00 · carried $6.00 · usable headroom $0.00" in err
        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        audit_data = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        errors_by_slug = {spec["slug"]: spec["error"] for spec in audit_data["specs"]}
        expected = "budget exhausted (sprint $0.00 + carried $6.00 = $6.00 >= $5.00)"
        assert errors_by_slug == {"feature-a": expected, "feature-b": expected}
        assert "dependency failed" not in err

    def test_resume_without_previous_run_marker_does_not_refuse_from_audit_fallback(
        self, tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=5.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)
        _write_prior_sprint_audit(tmp_path, sprint_id, 6.0)
        coord_result = _make_coordinator_result(success=True, cost=1.0)
        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )
        monkeypatch.delenv("FORGE_PREV_RUN_ID", raising=False)

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.runner.run_task", return_value=coord_result) as mock_run:
                result = run_sprint_ctx(config, manifest_path, resume=True)

        err = capsys.readouterr().err
        mock_run.assert_called_once()
        assert result.specs_succeeded == 1
        assert result.stopped_reason is None
        assert "Budget $5.00 · carried $0.00 · usable headroom $5.00" in err
        assert "Selected run cannot dispatch under the supplied ceiling" not in err

    def test_resume_prior_state_cost_exceeds_budget_without_sprint_audit(
        self, tmp_path: Path, capsys
    ) -> None:
        """Budget checks use progressive prior story state during re-exec."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=5.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)

        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "DONE",
                    "cost_usd": 6.0,
                    "story_run_id": "run-prev",
                    "started_at": "2026-07-27T01:00:00Z",
                    "finished_at": "2026-07-27T01:05:00Z",
                }
            ],
        )

        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run:
                with patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev-123"}, clear=False):
                    result = run_sprint_ctx(config, manifest_path, resume=True)

        err = capsys.readouterr().err
        mock_run.assert_not_called()
        assert result.specs_skipped == 1
        assert (
            result.stopped_reason
            == "Budget exhausted (sprint $0.00 + carried $6.00 = $6.00 >= $5.00)"
        )
        assert "Budget $5.00 · carried $6.00 · usable headroom $0.00" in err
        assert "Selected run cannot dispatch under the supplied ceiling" in err
        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        audit_data = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        assert audit_data["specs"][0]["error"] == (
            "budget exhausted (sprint $0.00 + carried $6.00 = $6.00 >= $5.00)"
        )

    def test_reexec_summary_preserves_prior_story_accounting_from_progressive_state(
        self, tmp_path: Path
    ) -> None:
        """Re-exec summary keeps prior cost/timing and spans pre-reexec duration."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=10.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)
        prior_started_at = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=90)
        ).replace(microsecond=0)
        prior_finished_at = prior_started_at + datetime.timedelta(seconds=30)

        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "DONE",
                    "cost_usd": 3.5,
                    "story_run_id": "run-prev",
                    "started_at": prior_started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "finished_at": prior_finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ],
        )

        merged_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
            slug="feature-a",
        )
        full_triage = StoryTriage(
            story_path="feature-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="feature-b",
        )

        def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
            return merged_triage if "feature-a" in spec_path else full_triage

        coord_result = _make_coordinator_result(success=True, cost=1.25, landing_status="landed")

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task", return_value=coord_result):
                with patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev-123"}, clear=False):
                    result = run_sprint_ctx(config, manifest_path, reexec=True)

        assert result.total_cost_usd == pytest.approx(4.75)

        summary_path = tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml"
        summary = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
        by_slug = {story["slug"]: story for story in summary["stories"]}
        expected_started_at = prior_started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        expected_finished_at = prior_finished_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        assert summary["sprint"]["total_cost_usd"] == pytest.approx(4.75)
        assert summary["sprint"]["duration_seconds"] >= 80.0
        assert by_slug["feature-a"]["cost_usd"] == pytest.approx(3.5)
        assert by_slug["feature-a"]["started_at"] == expected_started_at
        assert by_slug["feature-a"]["finished_at"] == expected_finished_at
        # #2150: the prior generation of *this* sprint ran feature-a to DONE and
        # paid $3.50 for it. A post-re-exec triage observing the branch as merged
        # must not relabel that as pre-existing work — the recorded execution is
        # the authoritative account, so the row keeps DONE and no source tag.
        assert by_slug["feature-a"]["outcome"] == "DONE"
        assert by_slug["feature-a"]["outcome_source"] is None

    def test_fresh_sprint_persists_progressive_story_state_for_later_reexec(
        self, tmp_path: Path
    ) -> None:
        """A fresh generation writes per-story accounting as it proceeds.

        This is the half of the carry-forward that makes re-exec recovery
        possible at all: sprint-audit.yaml only exists once a sprint has
        finished, so the generation that later re-execs must have left its
        in-flight accounting on progressive state while it was still running.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)
        coord_result = _make_coordinator_result(success=True, cost=3.5, landing_status="landed")

        with patch("theforge.sprint.runner.run_task", return_value=coord_result):
            run_sprint_ctx(config, manifest_path, resume=False)

        state_path = tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml"
        assert state_path.exists(), "fresh run must persist progressive story state"
        stories = yaml.safe_load(state_path.read_text(encoding="utf-8"))["stories"]
        by_ref = {story["canonical_ref"]: story for story in stories}
        assert by_ref["feature-a.md"]["cost_usd"] == pytest.approx(3.5)
        assert by_ref["feature-a.md"]["started_at"]
        assert by_ref["feature-a.md"]["finished_at"]

    def test_reexec_after_fresh_generation_carries_that_generations_spend(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: fresh generation, then re-exec, no seeded state.yaml.

        Reproduces the reported failure without hand-authoring the carry-forward
        artifact — the second generation must recover the first's spend from
        whatever the first actually wrote, with no sprint-audit.yaml in play.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=10.0)
        config = _make_config(tmp_path)
        _set_sprint_id(tmp_path)

        # Generation 1: only feature-a completes, then the process re-execs.
        gen1_manifest = _make_gen1_manifest(tmp_path, budget=10.0)
        with patch(
            "theforge.sprint.runner.run_task",
            return_value=_make_coordinator_result(success=True, cost=6.0, landing_status="landed"),
        ):
            run_sprint_ctx(config, gen1_manifest, resume=False)

        merged_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
            slug="feature-a",
        )
        full_triage = StoryTriage(
            story_path="feature-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="feature-b",
        )

        def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
            return merged_triage if "feature-a" in spec_path else full_triage

        # Generation 2: re-exec of the same sprint picks up where gen 1 left off.
        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch(
                "theforge.sprint.runner.run_task",
                return_value=_make_coordinator_result(
                    success=True, cost=1.5, landing_status="landed"
                ),
            ):
                result = run_sprint_ctx(config, manifest_path, reexec=True)

        # $6.00 from before the re-exec is still counted, not dropped.
        assert result.total_cost_usd == pytest.approx(7.5)

        summary_path = tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml"
        summary = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
        by_slug = {story["slug"]: story for story in summary["stories"]}
        assert summary["sprint"]["total_cost_usd"] == pytest.approx(7.5)
        assert by_slug["feature-a"]["cost_usd"] == pytest.approx(6.0)

    def test_reexec_retried_story_keeps_prior_spend_in_summary_total(self, tmp_path: Path) -> None:
        """A story retried after the re-exec keeps its pre-restart spend.

        The startup preload deliberately does not seed a non-succeeded prior
        outcome for a story that re-enters the current generation, or the
        transition to RUNNING would be rejected as non-monotonic. That skip
        must not take the story's prior *cost* with it: SprintResult counts it
        via prior_cost, so if the canonical state drops it, sprint-summary.yaml
        (which sums the canonical state) reports a smaller total than the
        banner for the same run.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=100.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)

        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "FAILED",
                    "cost_usd": 6.0,
                    "story_run_id": "run-prev",
                    "started_at": "2026-07-27T01:00:00Z",
                    "finished_at": "2026-07-27T01:05:00Z",
                }
            ],
        )

        retry_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=retry_triage):
            with patch(
                "theforge.sprint.runner.run_task",
                return_value=_make_coordinator_result(
                    success=True, cost=1.5, landing_status="landed"
                ),
            ):
                result = run_sprint_ctx(config, manifest_path, reexec=True)

        summary_path = tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml"
        summary = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
        by_slug = {story["slug"]: story for story in summary["stories"]}

        # $6.00 spent before the restart + $1.50 spent retrying it.
        assert result.total_cost_usd == pytest.approx(7.5)
        assert summary["sprint"]["total_cost_usd"] == pytest.approx(7.5)
        # The two operator-facing totals must agree for the same run.
        assert summary["sprint"]["total_cost_usd"] == pytest.approx(result.total_cost_usd)
        # Per-row traceability: the retried story owns the whole of its spend.
        assert by_slug["feature-a"]["cost_usd"] == pytest.approx(7.5)
        # The retry's own outcome wins — carrying cost must not resurrect the
        # prior FAILED state.
        assert by_slug["feature-a"]["outcome"] == "DONE"

    def test_reexec_retried_succeeded_story_keeps_prior_spend_in_summary_total(
        self, tmp_path: Path
    ) -> None:
        """Same defect, other branch: a prior DONE story that re-runs.

        A succeeded prior outcome IS seeded into the canonical state with its
        cost, so it survives for a story that does not re-run (skip_merged).
        But when triage retries it — prior generation marked it DONE and the
        merge never landed, say — transition() overwrites the seeded cost with
        the current generation's total, losing the prior spend from the summary
        while SprintResult still counts it.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=100.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)

        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "DONE",
                    "cost_usd": 6.0,
                    "story_run_id": "run-prev",
                    "started_at": "2026-07-27T01:00:00Z",
                    "finished_at": "2026-07-27T01:05:00Z",
                }
            ],
        )

        retry_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="branch not merged",
            worktree_path=None,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=retry_triage):
            with patch(
                "theforge.sprint.runner.run_task",
                return_value=_make_coordinator_result(
                    success=True, cost=1.5, landing_status="landed"
                ),
            ):
                result = run_sprint_ctx(config, manifest_path, reexec=True)

        summary_path = tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml"
        summary = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
        by_slug = {story["slug"]: story for story in summary["stories"]}

        assert result.total_cost_usd == pytest.approx(7.5)
        assert summary["sprint"]["total_cost_usd"] == pytest.approx(7.5)
        assert summary["sprint"]["total_cost_usd"] == pytest.approx(result.total_cost_usd)
        assert by_slug["feature-a"]["cost_usd"] == pytest.approx(7.5)

    def test_reexec_skip_merged_story_does_not_double_count_seeded_cost(
        self, tmp_path: Path
    ) -> None:
        """The seeded-cost restore must not fire for a story that did not re-run.

        Guards the other side of the same conditional: a resume_skip_merged
        story keeps its seeded prior cost intact, so re-attaching it would
        report double the money actually spent.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=100.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)

        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "DONE",
                    "cost_usd": 6.0,
                    "story_run_id": "run-prev",
                    "started_at": "2026-07-27T01:00:00Z",
                    "finished_at": "2026-07-27T01:05:00Z",
                }
            ],
        )

        merged_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=merged_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run:
                result = run_sprint_ctx(config, manifest_path, reexec=True)

        mock_run.assert_not_called()
        summary_path = tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml"
        summary = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
        by_slug = {story["slug"]: story for story in summary["stories"]}

        # Exactly $6.00 — the prior spend, counted once.
        assert result.total_cost_usd == pytest.approx(6.0)
        assert summary["sprint"]["total_cost_usd"] == pytest.approx(6.0)
        assert by_slug["feature-a"]["cost_usd"] == pytest.approx(6.0)

    def test_reexec_budget_check_sees_spend_from_before_the_reexec(self, tmp_path: Path) -> None:
        """The dispatch-time check evaluates the carried figure, not $0.00."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=5.0)
        config = _make_config(tmp_path)
        _set_sprint_id(tmp_path)

        gen1_manifest = _make_gen1_manifest(tmp_path, budget=100.0)
        with patch(
            "theforge.sprint.runner.run_task",
            return_value=_make_coordinator_result(success=True, cost=6.0, landing_status="landed"),
        ):
            run_sprint_ctx(config, gen1_manifest, resume=False)

        merged_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
            slug="feature-a",
        )
        full_triage = StoryTriage(
            story_path="feature-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="feature-b",
        )

        def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
            return merged_triage if "feature-a" in spec_path else full_triage

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task") as mock_run:
                result = run_sprint_ctx(config, manifest_path, reexec=True)

        # feature-b must not dispatch: $0.00 + carried $6.00 already exceeds $5.
        mock_run.assert_not_called()
        assert result.stopped_reason == (
            "Budget exhausted (sprint $0.00 + carried $6.00 = $6.00 >= $5.00)"
        )

    def test_no_resume_flag_unchanged(self, tmp_path: Path) -> None:
        """Without --resume, behavior is unchanged (run_task called normally)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec") as mock_triage:
            with patch("theforge.sprint.runner.run_task", return_value=coord_result) as mock_task:
                result = run_sprint_ctx(config, manifest_path, resume=False)

        mock_triage.assert_not_called()
        mock_task.assert_called_once()
        assert result.specs_succeeded == 1

    def test_no_resume_existing_sprint_does_not_disclose_or_charge_carried_spend(
        self, tmp_path: Path, capsys
    ) -> None:
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=5.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "DONE",
                    "cost_usd": 6.0,
                    "story_run_id": "run-prev",
                }
            ],
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec") as mock_triage:
            with patch("theforge.sprint.runner.run_task", return_value=coord_result) as mock_task:
                result = run_sprint_ctx(config, manifest_path, resume=False)

        err = capsys.readouterr().err
        mock_triage.assert_not_called()
        mock_task.assert_called_once()
        assert result.specs_succeeded == 1
        assert "Budget $5.00 · carried $0.00 · usable headroom $5.00" in err
        assert "Selected run cannot dispatch under the supplied ceiling" not in err

    def test_resume_startup_discloses_accepted_unmeasured_ceiling(
        self, tmp_path: Path, capsys
    ) -> None:
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=20.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-b.md",
                    "slug": "feature-b",
                    "path": "feature-b.md",
                    "outcome": "DONE",
                    "cost_usd": 6.0,
                    "story_run_id": "run-prev",
                },
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "FAILED",
                    "cost_usd": None,
                    "story_run_id": "run-prev",
                },
            ],
        )
        _write_incomplete_prior_sprint_audit(
            tmp_path,
            sprint_id,
            total_cost_measured_usd=6.0,
            unmeasured_spend_sources=["carried:feature-a"],
        )
        assert (
            persist_accepted_unmeasured_spend(
                sprint_id,
                "Test Sprint",
                tmp_path,
                [
                    {
                        "source": "feature-a",
                        "accepted_ceiling_usd": 4.5,
                        "accepted_at": "2026-08-08T00:00:00+00:00",
                    }
                ],
            )
            is True
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)
        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage) as mock_triage:
            with patch("theforge.sprint.runner.run_task", return_value=coord_result) as mock_task:
                with patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev-123"}, clear=False):
                    result = run_sprint_ctx(config, manifest_path, resume=True)

        err = capsys.readouterr().err
        mock_triage.assert_called_once()
        mock_task.assert_called_once()
        assert result.specs_succeeded == 2
        assert result.stopped_reason is None
        assert "Budget $20.00 · carried $6.00 · accepted unmeasured ceiling $4.50" in err
        assert "usable headroom $9.50" in err
        assert "lower bound" not in err

    def test_resume_startup_marks_headroom_as_lower_bound_for_incomplete_prior_cost(
        self, tmp_path: Path, capsys
    ) -> None:
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=20.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-b.md",
                    "slug": "feature-b",
                    "path": "feature-b.md",
                    "outcome": "DONE",
                    "cost_usd": 6.0,
                    "story_run_id": "run-prev",
                },
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "FAILED",
                    "cost_usd": None,
                    "story_run_id": "run-prev",
                },
            ],
        )
        _write_incomplete_prior_sprint_audit(
            tmp_path,
            sprint_id,
            total_cost_measured_usd=6.0,
            unmeasured_spend_sources=["carried:feature-a"],
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)
        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage) as mock_triage:
            with patch("theforge.sprint.runner.run_task", return_value=coord_result) as mock_task:
                with patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev-123"}, clear=False):
                    result = run_sprint_ctx(config, manifest_path, resume=True)

        err = capsys.readouterr().err
        mock_triage.assert_called_once()
        mock_task.assert_not_called()
        assert result.specs_skipped == 1
        assert result.stopped_reason is not None
        assert result.stopped_reason.startswith("Budget unverifiable")
        assert "Budget $20.00 · carried $6.00 · usable headroom $14.00 lower bound" in err
        assert "Selected run cannot dispatch under the supplied ceiling" in err

    def test_no_resume_startup_discloses_carried_unmeasured_and_refuses_pre_dispatch(
        self, tmp_path: Path, capsys
    ) -> None:
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=20.0)
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-b.md",
                    "slug": "feature-b",
                    "path": "feature-b.md",
                    "outcome": "FAILED",
                    "cost_usd": None,
                    "story_run_id": "run-prev",
                }
            ],
        )

        with patch("theforge.sprint.runner.run_task") as mock_task:
            result = run_sprint_ctx(config, manifest_path, resume=False)

        err = capsys.readouterr().err
        mock_task.assert_not_called()
        assert result.specs_skipped == 1
        assert result.stopped_reason is not None
        assert result.stopped_reason.startswith("Budget unverifiable")
        assert "Budget $20.00 · carried $0.00 · usable headroom $20.00 lower bound" in err
        assert "Selected run cannot dispatch under the supplied ceiling" in err
        assert "carried:feature-b" in err


# ── _build_task_from_story depends_on parsing ─────────────────────────


class TestBuildTaskDependsOn:
    def test_depends_on_missing(self, tmp_path: Path) -> None:
        """No depends_on in frontmatter → depends_on == []."""
        spec = _make_spec_file(tmp_path, "Spec A", "spec-a")
        task = _build_task_from_story(spec)
        assert task.depends_on == []

    def test_depends_on_single_string(self, tmp_path: Path) -> None:
        """depends_on as single string → normalized to single-element list."""
        spec = _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["single-slug"])
        task = _build_task_from_story(spec)
        assert task.depends_on == ["single-slug"]

    def test_depends_on_list(self, tmp_path: Path) -> None:
        """depends_on as list → preserved as list."""
        spec = _make_spec_file(tmp_path, "Spec C", "spec-c", depends_on=["slug-a", "slug-b"])
        task = _build_task_from_story(spec)
        assert task.depends_on == ["slug-a", "slug-b"]


# ── Sprint dependency checking ────────────────────────────────────────


class TestSprintDependencies:
    def test_skips_dependent_spec_on_failed_dependency(self, tmp_path: Path) -> None:
        """Spec B is skipped (but sprint continues) when spec-a did not merge."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        # Spec A succeeds but does NOT merge (merge=None)
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            result = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert mock_run.call_count == 1  # only spec-a ran (spec-b skipped, no more specs)
        assert result.specs_succeeded == 1
        assert result.specs_skipped == 1  # spec-b skipped
        assert result.stopped_reason is None  # sprint was NOT halted

    def test_proceeds_when_dependency_merged(self, tmp_path: Path) -> None:
        """Spec B proceeds when spec-a merged successfully."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            result = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert mock_run.call_count == 2
        assert result.specs_succeeded == 2
        assert result.stopped_reason is None

    def test_proceeds_normally_when_no_depends_on(self, tmp_path: Path) -> None:
        """Existing behavior preserved: specs without depends_on always run."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)
        result_b = _make_coordinator_result(success=True, cost=1.0)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            result = run_sprint_ctx(config, manifest_path)

        assert mock_run.call_count == 2
        assert result.specs_failed == 1
        assert result.specs_succeeded == 1
        assert result.stopped_reason is None

    def test_resume_merged_satisfies_dependency(self, tmp_path: Path) -> None:
        """Resume mode: spec triaged as skip_merged counts as merged for deps."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        merged_triage = StoryTriage(
            story_path="spec-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
            slug="spec-a",
        )
        full_triage = StoryTriage(
            story_path="spec-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="spec-b",
        )
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=True)

        def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
            if "spec-a" in spec_path:
                return merged_triage
            return full_triage

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task", return_value=result_b) as mock_run:
                result = run_sprint_ctx(config, manifest_path, resume=True)

        # spec-a was skip_merged (already done), spec-b ran successfully
        mock_run.assert_called_once()
        assert result.specs_succeeded == 1
        assert result.specs_skipped == 1
        assert result.stopped_reason is None

    def test_resume_approved_satisfies_dependency(self, tmp_path: Path) -> None:
        """Resume mode: prior-APPROVE triage is treated as already satisfied for deps."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        approved_triage = StoryTriage(
            story_path="spec-a.md",
            action="skip_merged",
            reason="already approved",
            worktree_path=None,
            slug="spec-a",
        )
        full_triage = StoryTriage(
            story_path="spec-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="spec-b",
        )
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=True)

        def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
            if "spec-a" in spec_path:
                return approved_triage
            return full_triage

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task", return_value=result_b) as mock_run:
                result = run_sprint_ctx(config, manifest_path, resume=True)

        # spec-a was skip_merged (already done) — should satisfy dep so spec-b runs
        mock_run.assert_called_once()
        assert result.specs_succeeded == 1
        assert result.specs_skipped == 1
        assert result.stopped_reason is None

    def test_skips_dependent_continues_independent(self, tmp_path: Path) -> None:
        """Three specs: A, B (depends on spec-a), C. A doesn't merge → B skipped, C still runs."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        _make_spec_file(tmp_path, "Spec C", "spec-c")
        manifest_path = _make_manifest(
            tmp_path, ["spec-a.md", "spec-b.md", "spec-c.md"], budget=10.0
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result_c = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_c]
        ) as mock_run:
            result = run_sprint_ctx(config, manifest_path)

        # A ran, B was skipped (dependency failed), C still ran
        assert mock_run.call_count == 2
        assert result.specs_skipped == 1  # only B skipped
        assert result.specs_succeeded == 2  # A and C succeeded
        assert result.stopped_reason is None  # sprint was not halted

    def test_eager_merge_fires_for_spec_with_downstream_dependent(self, tmp_path: Path) -> None:
        """Eager merge: auto_merge=True is passed for specs that have downstream dependents."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=True)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            run_sprint_ctx(config, manifest_path, auto_merge=False)

        # First call (spec-a) must have auto_merge=True due to eager merge
        first_call_kwargs = mock_run.call_args_list[0].kwargs
        assert first_call_kwargs["auto_merge"] is True

    def test_eager_merge_does_not_fire_for_spec_without_downstream_dependent(
        self, tmp_path: Path
    ) -> None:
        """Spec A has no downstream dependents — auto_merge setting is respected as-is."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            run_sprint_ctx(config, manifest_path, auto_merge=False)

        # Neither call should override auto_merge
        for call in mock_run.call_args_list:
            assert call.kwargs["auto_merge"] is False

    def test_resume_prior_approve_does_not_run_review_or_dev(self, tmp_path: Path) -> None:
        """Resume mode: prior APPROVE stories do not re-enter review/dev flows."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        approved_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip_merged",
            reason="prior APPROVE in audit trail; branch already satisfied (2 commits ahead)",
            worktree_path=None,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=approved_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run_task:
                with patch("theforge.sprint.runner.run_from_review") as mock_review:
                    with patch("theforge.sprint.runner.run_from_dev") as mock_dev:
                        result = run_sprint_ctx(config, manifest_path, resume=True)

        mock_run_task.assert_not_called()
        mock_review.assert_not_called()
        mock_dev.assert_not_called()
        assert result.specs_succeeded == 0
        assert result.specs_skipped == 1
        assert result.total_cost_usd == 0.0

    def test_already_done_satisfies_dependency(self, tmp_path: Path) -> None:
        """ALREADY_DONE spec counts as merged for dependency purposes (changes already on main)."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(
            success=True, cost=0.1, preflight_verdict="ALREADY_DONE", phase=Phase.DONE
        )
        result_b = _make_coordinator_result(success=True, cost=1.0)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            result = run_sprint_ctx(config, manifest_path)

        assert mock_run.call_count == 2  # both specs ran
        # ALREADY_DONE is a terminal succeeded outcome under the canonical
        # state model — both specs count as succeeded.
        assert result.specs_skipped == 0
        assert result.specs_succeeded == 2
        assert result.stopped_reason is None  # no halt

    def test_resume_cleans_stale_lock_and_pending_files(self, tmp_path: Path) -> None:
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        lock_dir = tmp_path / ".forge" / "locks"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "feature-a.lock"
        dead_pid = os.getpid() + 99999
        lock_path.write_text(str(dead_pid), encoding="utf-8")

        pending_dir = tmp_path / ".forge" / "pending"
        pending_dir.mkdir(parents=True)
        pending_path = pending_dir / "resume-run.yaml"
        future = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60)
        ).isoformat()
        pending_path.write_text(
            yaml.safe_dump(
                {
                    "run_id": "resume-run",
                    "story": "feature-a",
                    "phase": "ESCALATE",
                    "reason": "r",
                    "options": ["approve"],
                    "created_at": future,
                    "timeout_at": future,
                    "pid": dead_pid,
                }
            ),
            encoding="utf-8",
        )

        review_triage = StoryTriage(
            story_path="feature-a.md",
            action="review",
            reason="gate passes",
            worktree_path=tmp_path / "feature-a",
        )
        review_triage.worktree_path.mkdir()
        coord_result = _make_coordinator_result(success=True, cost=1.0, merged=True)

        with patch("theforge.sprint.runner._triage_spec", return_value=review_triage):
            with patch("theforge.sprint.runner.run_from_review", return_value=coord_result):
                with patch("theforge.coordinator.workspace.pull_base_branch", return_value=True):
                    with patch("theforge.sprint.runner.run_batch_preflight", return_value={}):
                        result = run_sprint_ctx(config, manifest_path, resume=True)

        assert result.specs_succeeded == 1

        removed = pending.cleanup_stale(project_root=tmp_path)
        assert removed == 1
        assert not pending_path.exists()

        with patch("theforge.sprint.lock.fcntl.flock", side_effect=[BlockingIOError, None]):
            with patch("theforge.sprint.lock.os.kill", side_effect=ProcessLookupError):
                fds, conflicted = acquire_story_locks(["feature-a"], tmp_path)
        try:
            assert conflicted == []
            assert len(fds) == 1
        finally:
            release_story_locks(fds)


def test_issue_backed_stories_are_batch_preflighted_before_fresh_run(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = TaskStory(
        name="Issue 283",
        slug="issue-283",
        story_path=None,
        story_text="# Issue 283\n\nBody",
        github_issue=283,
    )
    resolved = ResolvedSprint(
        name="GitHub Sprint",
        budget_usd=5.0,
        stories=[(task, GitHubIssueSource(), "issue:283")],
        max_parallel=1,
    )
    cached_preflight = CoordinatorState(
        preflight_verdict="PROCEED",
        preflight_complexity="medium",
        preflight_complexity_score=6,
        preflight_sufficiency="implementation_ready",
        preflight_work_type="bug",
    )
    coord_result = _make_coordinator_result(success=True, cost=1.0)

    with patch(
        "theforge.sprint.runner.run_batch_preflight",
        return_value={task.slug: cached_preflight},
    ) as mock_batch_preflight:
        with patch("theforge.sprint.runner.run_task", return_value=coord_result) as mock_run_task:
            result = run_sprint_ctx(config, resolved)

    assert result.specs_succeeded == 1
    batch_tasks = mock_batch_preflight.call_args.args[0]
    assert [preflight_task.slug for preflight_task in batch_tasks] == ["issue-283"]
    assert batch_tasks[0].github_issue == 283
    assert batch_tasks[0].story_text == "# Issue 283\n\nBody"
    assert mock_run_task.call_args.kwargs["cached_preflight_state"] is cached_preflight


def test_run_fresh_issue_backed_story_does_not_fabricate_cached_preflight(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    task = TaskStory(
        name="Issue 283",
        slug="issue-283",
        story_path=None,
        story_text="# Issue 283\n\nBody",
        github_issue=283,
    )
    coord_result = _make_coordinator_result(success=True, cost=1.0)

    with patch("theforge.sprint.runner.run_task", return_value=coord_result) as mock_run_task:
        result = _run_fresh(
            config,
            task,
            sprint_run_id="run-123",
            sprint_name="GitHub Sprint",
            interactive=False,
            notify=False,
            effective_auto_merge=False,
            state_update_fn=None,
            no_pull=False,
            plan_gate=None,
            preflight_states={},
        )

    assert result is coord_result
    assert mock_run_task.call_args.kwargs["cached_preflight_state"] is None
