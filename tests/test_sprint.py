"""Tests for campaign mode: multi-spec sequential execution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    _notify,
    _osa_quote,
    run_task,
)
from theforge.sprint import (
    SpecTriage,
    _build_task_from_spec,
    _triage_spec,
    load_sprint_manifest,
    run_sprint,
)

# ── Helpers ──────────────────────────────────────────────────────────


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
    manifest_path = tmp_path / "campaign.yaml"
    manifest_path.write_text(
        yaml.dump(
            {
                "name": "Test Campaign",
                "budget_usd": budget,
                "specs": specs,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _make_coordinator_result(
    success: bool = True,
    cost: float = 1.0,
    preflight_verdict: str = "PROCEED",
    phase: Phase = Phase.DONE,
    merged: bool = False,
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
    )


# ── load_sprint_manifest ─────────────────────────────────────────────────────


class TestLoadManifest:
    def test_valid_manifest(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(
            yaml.dump({"name": "My Campaign", "budget_usd": 5.0, "specs": ["a.md", "b.md"]}),
            encoding="utf-8",
        )
        manifest = load_sprint_manifest(path)
        assert manifest.name == "My Campaign"
        assert manifest.budget_usd == 5.0
        assert manifest.specs == ["a.md", "b.md"]

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            load_sprint_manifest(tmp_path / "nonexistent.yaml")

    def test_missing_name(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(yaml.dump({"budget_usd": 5.0, "specs": ["a.md"]}), encoding="utf-8")
        with pytest.raises(ValueError, match="name"):
            load_sprint_manifest(path)

    def test_missing_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(yaml.dump({"name": "X", "specs": ["a.md"]}), encoding="utf-8")
        with pytest.raises(ValueError, match="budget_usd"):
            load_sprint_manifest(path)

    def test_zero_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 0.0, "specs": ["a.md"]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="budget_usd.*> 0"):
            load_sprint_manifest(path)

    def test_negative_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": -1.0, "specs": ["a.md"]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="budget_usd.*> 0"):
            load_sprint_manifest(path)

    def test_missing_specs(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(yaml.dump({"name": "X", "budget_usd": 5.0}), encoding="utf-8")
        with pytest.raises(ValueError, match="specs"):
            load_sprint_manifest(path)

    def test_empty_specs(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(yaml.dump({"name": "X", "budget_usd": 5.0, "specs": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="specs"):
            load_sprint_manifest(path)

    def test_non_string_spec_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "specs": [123]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="strings"):
            load_sprint_manifest(path)

    def test_not_a_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_sprint_manifest(path)


# ── run_sprint ──────────────────────────────────────────────────────


class TestRunCampaign:
    def test_success_path(self, tmp_path: Path) -> None:
        """Two specs both succeed, costs accumulate, audit written."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=2.0)
        result_b = _make_coordinator_result(success=True, cost=3.0)

        with patch("theforge.sprint.run_task", side_effect=[result_a, result_b]):
            campaign = run_sprint(config, manifest_path)

        assert campaign.specs_total == 2
        assert campaign.specs_succeeded == 2
        assert campaign.specs_failed == 0
        assert campaign.specs_skipped == 0
        assert campaign.total_cost_usd == pytest.approx(5.0)
        assert campaign.stopped_reason is None
        assert len(campaign.results) == 2

        # Audit file should exist
        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        assert audit_path.exists()
        with open(audit_path) as f:
            audit = yaml.safe_load(f)
        assert audit["sprint"]["specs_succeeded"] == 2
        assert audit["sprint"]["total_cost_usd"] == pytest.approx(5.0)
        assert len(audit["specs"]) == 2

    def test_spec_failure_continues(self, tmp_path: Path) -> None:
        """Campaign continues after individual spec failure."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)
        result_b = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint.run_task", side_effect=[result_a, result_b]):
            campaign = run_sprint(config, manifest_path)

        assert campaign.specs_succeeded == 1
        assert campaign.specs_failed == 1
        assert campaign.specs_skipped == 0
        assert campaign.stopped_reason is None

    def test_already_done_counted_as_skipped(self, tmp_path: Path) -> None:
        """ALREADY_DONE specs count as skipped, not succeeded or failed."""
        _make_spec_file(tmp_path, "Done Spec", "done-spec")
        manifest_path = _make_manifest(tmp_path, ["done-spec.md"], budget=10.0)
        config = _make_config(tmp_path)

        result = _make_coordinator_result(
            success=True, cost=0.15, preflight_verdict="ALREADY_DONE", phase=Phase.DONE
        )

        with patch("theforge.sprint.run_task", return_value=result):
            campaign = run_sprint(config, manifest_path)

        assert campaign.specs_succeeded == 0
        assert campaign.specs_skipped == 1
        assert campaign.specs_failed == 0

    def test_budget_exceeded_stops_campaign(self, tmp_path: Path) -> None:
        """Campaign stops when accumulated cost exceeds budget."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        _make_spec_file(tmp_path, "Spec C", "spec-c")
        manifest_path = _make_manifest(
            tmp_path, ["spec-a.md", "spec-b.md", "spec-c.md"], budget=5.0
        )
        config = _make_config(tmp_path)

        # First spec costs 6.0, which exceeds the $5 budget
        result_a = _make_coordinator_result(success=True, cost=6.0)

        with patch("theforge.sprint.run_task", side_effect=[result_a]) as mock_run:
            campaign = run_sprint(config, manifest_path)

        # Only spec A ran; B and C were skipped
        assert mock_run.call_count == 1
        assert campaign.specs_succeeded == 1
        assert campaign.specs_skipped == 2
        assert campaign.stopped_reason is not None
        assert "budget" in campaign.stopped_reason.lower()

    def test_budget_check_before_first_spec(self, tmp_path: Path) -> None:
        """If budget is already 0 (impossible given validation), specs are run.
        More usefully: if budget is exhausted before a spec starts, it's skipped."""
        # We can't start over budget (budget > 0 enforced), but we can test
        # that the pre-run check works by running enough specs to exhaust it
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=3.0)
        config = _make_config(tmp_path)

        # First spec costs exactly the budget
        result_a = _make_coordinator_result(success=True, cost=3.0)

        with patch("theforge.sprint.run_task", side_effect=[result_a]) as mock_run:
            campaign = run_sprint(config, manifest_path)

        assert mock_run.call_count == 1
        assert campaign.specs_skipped == 1  # spec B skipped

    def test_auto_merge_passed_through(self, tmp_path: Path) -> None:
        """auto_merge flag is forwarded to each run_task call."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md"])
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.run_task", return_value=result_a) as mock_run:
            run_sprint(config, manifest_path, auto_merge=True)

        _, kwargs = mock_run.call_args
        assert kwargs.get("auto_merge") is True

    def test_interactive_passed_through(self, tmp_path: Path) -> None:
        """interactive flag is forwarded to each run_task call."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md"])
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.run_task", return_value=result_a) as mock_run:
            run_sprint(config, manifest_path, interactive=True)

        _, kwargs = mock_run.call_args
        assert kwargs.get("interactive") is True

    def test_manifest_missing_spec_file_raises(self, tmp_path: Path) -> None:
        """run_sprint raises ValueError if spec files don't exist."""
        manifest_path = _make_manifest(tmp_path, ["nonexistent.md"])
        config = _make_config(tmp_path)

        with pytest.raises(ValueError, match="missing"):
            run_sprint(config, manifest_path)

    def test_audit_yaml_written(self, tmp_path: Path) -> None:
        """sprint-audit.yaml is written to project root."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md"])
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.5, merged=True)

        with patch("theforge.sprint.run_task", return_value=result_a):
            run_sprint(config, manifest_path)

        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        assert audit_path.exists()

        with open(audit_path) as f:
            audit = yaml.safe_load(f)

        assert audit["sprint"]["name"] == "Test Campaign"
        assert audit["sprint"]["budget_usd"] == pytest.approx(10.0)
        assert "Claude" in audit["sprint"]["budget_note"]
        assert audit["specs"][0]["path"] == "spec-a.md"
        assert audit["specs"][0]["merge"] is True

    def test_skipped_specs_appear_in_audit(self, tmp_path: Path) -> None:
        """Specs skipped due to budget appear in audit with SKIPPED outcome."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=1.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint.run_task", return_value=result_a):
            run_sprint(config, manifest_path)

        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        with open(audit_path) as f:
            audit = yaml.safe_load(f)

        assert len(audit["specs"]) == 2
        assert audit["specs"][1]["outcome"] == "SKIPPED"
        assert audit["specs"][1]["cost_usd"] == 0.0

    def test_failed_merge_not_reported_as_merged(self, tmp_path: Path) -> None:
        """When merge fails, audit reports merge=false and log omits ', merged'."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md"])
        config = _make_config(tmp_path)

        # Simulate a merge attempt that failed (dirty root, etc.)
        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = 1.0
        state.preflight_result = mock_preflight
        result_failed_merge = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"attempted": True, "merged": False, "error": "dirty working tree"},
        )

        with patch("theforge.sprint.run_task", return_value=result_failed_merge):
            campaign = run_sprint(config, manifest_path, auto_merge=True)

        # Campaign counts as succeeded (task itself passed), but merge did not happen
        assert campaign.specs_succeeded == 1

        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        with open(audit_path) as f:
            audit = yaml.safe_load(f)

        assert audit["specs"][0]["merge"] is False


# ── Notification tests ────────────────────────────────────────────────


class TestCampaignNotifications:
    def test_campaign_notification_sent(self, tmp_path: Path) -> None:
        """Campaign completion sends exactly one notification with name in title."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint._notify") as mock_notify:
            with patch("theforge.sprint.run_task", return_value=result_a):
                run_sprint(config, manifest_path, notify=True)

        mock_notify.assert_called_once()
        title, body = mock_notify.call_args[0]
        assert "Test Campaign" in title
        assert "1" in body  # specs_succeeded count

    def test_campaign_forwards_notify_true_to_run_task(self, tmp_path: Path) -> None:
        """run_sprint() passes notify=True down to each run_task() call."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint._notify"):
            with patch("theforge.sprint.run_task", return_value=result_a) as mock_run_task:
                run_sprint(config, manifest_path, notify=True)

        _, kwargs = mock_run_task.call_args
        assert kwargs.get("notify") is True

    def test_campaign_forwards_notify_false_to_run_task(self, tmp_path: Path) -> None:
        """run_sprint() passes notify=False down to each run_task() call."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint.run_task", return_value=result_a) as mock_run_task:
            run_sprint(config, manifest_path, notify=False)

        _, kwargs = mock_run_task.call_args
        assert kwargs.get("notify") is False

    def test_campaign_notification_skipped_with_no_notify(self, tmp_path: Path) -> None:
        """notify=False suppresses campaign completion notification."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint._notify") as mock_notify:
            with patch("theforge.sprint.run_task", return_value=result_a):
                run_sprint(config, manifest_path, notify=False)

        mock_notify.assert_not_called()


class TestEscalationNotifications:
    def test_escalation_notification_sent(self, tmp_path: Path) -> None:
        """Workspace creation failure triggers an escalation notification."""
        spec_path = tmp_path / "spec.md"
        spec_path.write_text("---\nslug: my-slug\n---\n# Spec", encoding="utf-8")
        config = _make_config(tmp_path)

        from theforge.task import TaskSpec

        task = TaskSpec(name="Test", spec_path=spec_path, slug="my-slug")

        with patch("theforge.coord_notify._notify") as mock_notify:
            with patch(
                "theforge.coordinator._create_workspace",
                return_value=(None, None, "disk full"),
            ):
                result = run_task(config, task, notify=True)

        assert result.success is False
        assert result.phase == Phase.ESCALATE
        mock_notify.assert_called_once()
        title, body = mock_notify.call_args[0]
        assert "escalated" in title
        assert "my-slug" in title
        assert body == "disk full"

    def test_escalation_notification_body_truncated_at_120(self, tmp_path: Path) -> None:
        """Escalation notification body is truncated to 120 chars per R2."""
        spec_path = tmp_path / "spec.md"
        spec_path.write_text("---\nslug: my-slug\n---\n# Spec", encoding="utf-8")
        config = _make_config(tmp_path)

        from theforge.task import TaskSpec

        task = TaskSpec(name="Test", spec_path=spec_path, slug="my-slug")
        long_error = "x" * 200

        with patch("theforge.coord_notify._notify") as mock_notify:
            with patch(
                "theforge.coordinator._create_workspace",
                return_value=(None, None, long_error),
            ):
                run_task(config, task, notify=True)

        mock_notify.assert_called_once()
        _, body = mock_notify.call_args[0]
        assert body == long_error[:120]
        assert len(body) == 120

    def test_escalation_no_notification_when_notify_false(self, tmp_path: Path) -> None:
        """notify=False suppresses escalation notifications."""
        spec_path = tmp_path / "spec.md"
        spec_path.write_text("---\nslug: my-slug\n---\n# Spec", encoding="utf-8")
        config = _make_config(tmp_path)

        from theforge.task import TaskSpec

        task = TaskSpec(name="Test", spec_path=spec_path, slug="my-slug")

        with patch("theforge.coord_notify._notify") as mock_notify:
            with patch(
                "theforge.coordinator._create_workspace",
                return_value=(None, None, "disk full"),
            ):
                run_task(config, task, notify=False)

        mock_notify.assert_not_called()


class TestNotifyFunction:
    def test_notify_fail_silent(self) -> None:
        """_notify swallows subprocess errors and never raises."""
        with patch("theforge.coord_notify.shutil.which", return_value="/usr/bin/osascript"):
            with patch("theforge.coord_notify.subprocess.run", side_effect=OSError("broken pipe")):
                # Must not raise
                _notify("Title", "Body")

    def test_notify_noop_without_osascript(self) -> None:
        """_notify does nothing when osascript is not available."""
        with patch("theforge.coord_notify.shutil.which", return_value=None):
            with patch("theforge.coord_notify.subprocess.run") as mock_run:
                _notify("Title", "Body")

        mock_run.assert_not_called()

    def test_osa_quote_escapes_backslash_and_quote(self) -> None:
        """_osa_quote wraps in double quotes and escapes special chars."""
        result = _osa_quote('say "hello\\world"')
        assert result == '"say \\"hello\\\\world\\""'


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
                m.stdout = b"3"  # 3 commits ahead — truly merged, not just at base
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.subprocess.run", side_effect=_mock_run):
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
                m.stdout = b"0"  # 0 commits ahead — just created at base HEAD
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert "no worktree" in triage.reason

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

        with patch("theforge.sprint.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint._run_gate", return_value=("PASS", None, "")):
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

        with patch("theforge.sprint.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint._run_gate", return_value=("FAIL", "tests failed", "")):
                triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "dev"
        assert triage.worktree_path == worktree

    def test_triage_no_worktree(self, tmp_path: Path) -> None:
        """No worktree found → full."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        config = _make_config(tmp_path)

        def _mock_run(cmd, **kwargs):
            m = MagicMock()
            if "--is-ancestor" in cmd:
                m.returncode = 1  # not merged
            return m

        with patch("theforge.sprint.subprocess.run", side_effect=_mock_run):
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

        with patch("theforge.sprint.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "full"
        assert "stale" in triage.reason or "0 commits" in triage.reason

    def test_triage_worktree_with_prior_approve(self, tmp_path: Path) -> None:
        """Worktree has commits ahead and prior APPROVE in audit trail → skip."""
        import json

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
        (audits_dir / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

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

        with patch("theforge.sprint.subprocess.run", side_effect=_mock_run):
            triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "skip"
        assert "APPROVE" in triage.reason or "approve" in triage.reason.lower()
        assert triage.worktree_path == worktree

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

        with patch("theforge.sprint.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint._run_gate", return_value=("PASS", None, "")):
                triage = _triage_spec("feature-a.md", config, tmp_path)

        assert triage.action == "review"


class TestResumeSprintSkipApproved:
    def test_resume_sprint_skips_approved(self, tmp_path: Path) -> None:
        """Resume sprint: spec with prior APPROVE is skipped without running."""
        import json

        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        # Write an APPROVE record
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        record = {"task": {"slug": "feature-a"}, "reviews": [{"verdict": "APPROVE"}]}
        (audits_dir / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        skip_triage = SpecTriage(
            spec_path="feature-a.md",
            action="skip",
            reason="prior APPROVE in audit trail (2 commits ahead)",
            worktree_path=worktree,
            slug="feature-a",
        )

        with patch("theforge.sprint._triage_spec", return_value=skip_triage):
            with patch("theforge.sprint.run_task") as mock_run_task:
                result = run_sprint(config, manifest_path, resume=True)

        mock_run_task.assert_not_called()
        assert result.specs_succeeded == 1


class TestResumeSprintIntegration:
    def test_resume_sprint_skips_merged(self, tmp_path: Path) -> None:
        """End-to-end resume: merged spec counts as succeeded (work is done)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        merged_triage = SpecTriage(
            spec_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
        )

        with patch("theforge.sprint._triage_spec", return_value=merged_triage):
            with patch("theforge.sprint.run_task") as mock_run:
                result = run_sprint(config, manifest_path, resume=True)

        mock_run.assert_not_called()
        assert result.specs_succeeded == 1  # already-merged = success
        assert result.specs_skipped == 0

    def test_resume_sprint_enters_dev(self, tmp_path: Path) -> None:
        """End-to-end resume: gate-failing worktree uses run_from_dev."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        dev_triage = SpecTriage(
            spec_path="feature-a.md",
            action="dev",
            reason="gate fails",
            worktree_path=worktree,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint._triage_spec", return_value=dev_triage):
            with patch("theforge.sprint.run_from_dev", return_value=coord_result) as mock_dev:
                with patch("theforge.sprint.run_task") as mock_task:
                    result = run_sprint(config, manifest_path, resume=True)

        mock_dev.assert_called_once()
        mock_task.assert_not_called()
        assert result.specs_succeeded == 1

    def test_resume_budget_exhausted_merged_spec_still_succeeds(self, tmp_path: Path) -> None:
        """Merged spec counts as succeeded even when budget is exhausted."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=1.0)
        config = _make_config(tmp_path)

        # Prior run spent $2 — budget exhausted
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        prior_audit = {"sprint": {"total_cost_usd": 2.0}}
        with open(audits_dir / "sprint-audit.yaml", "w") as f:
            yaml.dump(prior_audit, f)

        merged_triage = SpecTriage(
            spec_path="feature-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
        )
        full_triage = SpecTriage(
            spec_path="feature-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )

        def triage_side_effect(spec_path, config, project_root):
            if "feature-a" in spec_path:
                return merged_triage
            return full_triage

        with patch("theforge.sprint._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.run_task") as mock_run:
                result = run_sprint(config, manifest_path, resume=True)

        # Merged spec should be succeeded, not budget-skipped
        assert result.specs_succeeded == 1  # feature-a (merged)
        assert result.specs_skipped == 1  # feature-b (budget)
        mock_run.assert_not_called()

    def test_resume_sprint_enters_review(self, tmp_path: Path) -> None:
        """End-to-end resume: gate-passing worktree uses run_from_review."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        worktree = tmp_path / "feature-a"
        worktree.mkdir()

        review_triage = SpecTriage(
            spec_path="feature-a.md",
            action="review",
            reason="gate passes",
            worktree_path=worktree,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint._triage_spec", return_value=review_triage):
            with patch("theforge.sprint.run_from_review", return_value=coord_result) as mock_rev:
                with patch("theforge.sprint.run_task") as mock_task:
                    result = run_sprint(config, manifest_path, resume=True)

        mock_rev.assert_called_once()
        mock_task.assert_not_called()
        assert result.specs_succeeded == 1

    def test_resume_cost_continuity(self, tmp_path: Path) -> None:
        """Prior sprint cost is carried forward into total_cost_usd."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)

        # Write a prior sprint-audit.yaml with a known cost
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        prior_audit = {"sprint": {"total_cost_usd": 3.50}}
        with open(audits_dir / "sprint-audit.yaml", "w") as f:
            yaml.dump(prior_audit, f)

        full_triage = SpecTriage(
            spec_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.run_task", return_value=coord_result):
                result = run_sprint(config, manifest_path, resume=True)

        # total should be prior (3.50) + new (1.00)
        assert result.total_cost_usd == pytest.approx(4.50)

    def test_resume_prior_cost_exceeds_budget(self, tmp_path: Path) -> None:
        """When prior cost already meets/exceeds budget, first spec is skipped."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=5.0)
        config = _make_config(tmp_path)

        # Prior run already spent $6 (over the $5 budget)
        audits_dir = tmp_path / ".forge" / "audits"
        audits_dir.mkdir(parents=True)
        prior_audit = {"sprint": {"total_cost_usd": 6.0}}
        with open(audits_dir / "sprint-audit.yaml", "w") as f:
            yaml.dump(prior_audit, f)

        full_triage = SpecTriage(
            spec_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )

        with patch("theforge.sprint._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.run_task") as mock_run:
                result = run_sprint(config, manifest_path, resume=True)

        # Spec should be skipped — prior cost alone exceeds budget
        mock_run.assert_not_called()
        assert result.specs_skipped == 1
        assert result.stopped_reason is not None
        assert "budget" in result.stopped_reason.lower()

    def test_no_resume_flag_unchanged(self, tmp_path: Path) -> None:
        """Without --resume, behavior is unchanged (run_task called normally)."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint._triage_spec") as mock_triage:
            with patch("theforge.sprint.run_task", return_value=coord_result) as mock_task:
                result = run_sprint(config, manifest_path, resume=False)

        mock_triage.assert_not_called()
        mock_task.assert_called_once()
        assert result.specs_succeeded == 1


# ── _build_task_from_spec depends_on parsing ─────────────────────────


class TestBuildTaskDependsOn:
    def test_depends_on_missing(self, tmp_path: Path) -> None:
        """No depends_on in frontmatter → depends_on == []."""
        spec = _make_spec_file(tmp_path, "Spec A", "spec-a")
        task = _build_task_from_spec(spec)
        assert task.depends_on == []

    def test_depends_on_single_string(self, tmp_path: Path) -> None:
        """depends_on as single string → normalized to single-element list."""
        spec = _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["single-slug"])
        task = _build_task_from_spec(spec)
        assert task.depends_on == ["single-slug"]

    def test_depends_on_list(self, tmp_path: Path) -> None:
        """depends_on as list → preserved as list."""
        spec = _make_spec_file(tmp_path, "Spec C", "spec-c", depends_on=["slug-a", "slug-b"])
        task = _build_task_from_spec(spec)
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

        with patch("theforge.sprint.run_task", side_effect=[result_a]) as mock_run:
            result = run_sprint(config, manifest_path, auto_merge=True)

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

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=True)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch("theforge.sprint.run_task", side_effect=[result_a, result_b]) as mock_run:
            result = run_sprint(config, manifest_path, auto_merge=True)

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

        with patch("theforge.sprint.run_task", side_effect=[result_a, result_b]) as mock_run:
            result = run_sprint(config, manifest_path)

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

        merged_triage = SpecTriage(
            spec_path="spec-a.md",
            action="skip_merged",
            reason="already merged to main",
            worktree_path=None,
            slug="spec-a",
        )
        full_triage = SpecTriage(
            spec_path="spec-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="spec-b",
        )
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=True)

        def triage_side_effect(spec_path, config, project_root):
            if "spec-a" in spec_path:
                return merged_triage
            return full_triage

        with patch("theforge.sprint._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.run_task", return_value=result_b) as mock_run:
                result = run_sprint(config, manifest_path, resume=True)

        # spec-a was skip_merged (counted as succeeded), spec-b ran successfully
        mock_run.assert_called_once()
        assert result.specs_succeeded == 2
        assert result.stopped_reason is None

    def test_resume_approved_satisfies_dependency(self, tmp_path: Path) -> None:
        """Resume mode: spec triaged as 'skip' (prior APPROVE) satisfies downstream deps."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b", depends_on=["spec-a"])
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        approved_triage = SpecTriage(
            spec_path="spec-a.md",
            action="skip",
            reason="already approved",
            worktree_path=None,
            slug="spec-a",
        )
        full_triage = SpecTriage(
            spec_path="spec-b.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
            slug="spec-b",
        )
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=True)

        def triage_side_effect(spec_path, config, project_root):
            if "spec-a" in spec_path:
                return approved_triage
            return full_triage

        with patch("theforge.sprint._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.run_task", return_value=result_b) as mock_run:
                result = run_sprint(config, manifest_path, resume=True)

        # spec-a was skip (prior APPROVE) — should satisfy dep so spec-b runs
        mock_run.assert_called_once()
        assert result.specs_succeeded == 2
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

        with patch("theforge.sprint.run_task", side_effect=[result_a, result_c]) as mock_run:
            result = run_sprint(config, manifest_path)

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

        with patch("theforge.sprint.run_task", side_effect=[result_a, result_b]) as mock_run:
            run_sprint(config, manifest_path, auto_merge=False)

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

        with patch("theforge.sprint.run_task", side_effect=[result_a, result_b]) as mock_run:
            run_sprint(config, manifest_path, auto_merge=False)

        # Neither call should override auto_merge
        for call in mock_run.call_args_list:
            assert call.kwargs["auto_merge"] is False

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

        with patch("theforge.sprint.run_task", side_effect=[result_a, result_b]) as mock_run:
            result = run_sprint(config, manifest_path)

        assert mock_run.call_count == 2  # both specs ran
        assert result.specs_skipped == 1  # spec-a counted as skipped (ALREADY_DONE)
        assert result.specs_succeeded == 1  # spec-b succeeded
        assert result.stopped_reason is None  # no halt
