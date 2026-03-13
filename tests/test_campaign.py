"""Tests for campaign mode: multi-spec sequential execution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from theforge.campaign import (
    load_manifest,
    run_campaign,
)
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


def _make_spec_file(tmp_path: Path, name: str, slug: str) -> Path:
    spec = tmp_path / f"{slug}.md"
    spec.write_text(
        f"---\nname: {name}\nslug: {slug}\n---\n# {name}\nDo the thing.",
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


# ── load_manifest ─────────────────────────────────────────────────────


class TestLoadManifest:
    def test_valid_manifest(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(
            yaml.dump({"name": "My Campaign", "budget_usd": 5.0, "specs": ["a.md", "b.md"]}),
            encoding="utf-8",
        )
        manifest = load_manifest(path)
        assert manifest.name == "My Campaign"
        assert manifest.budget_usd == 5.0
        assert manifest.specs == ["a.md", "b.md"]

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            load_manifest(tmp_path / "nonexistent.yaml")

    def test_missing_name(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(yaml.dump({"budget_usd": 5.0, "specs": ["a.md"]}), encoding="utf-8")
        with pytest.raises(ValueError, match="name"):
            load_manifest(path)

    def test_missing_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(yaml.dump({"name": "X", "specs": ["a.md"]}), encoding="utf-8")
        with pytest.raises(ValueError, match="budget_usd"):
            load_manifest(path)

    def test_zero_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 0.0, "specs": ["a.md"]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="budget_usd.*> 0"):
            load_manifest(path)

    def test_negative_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": -1.0, "specs": ["a.md"]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="budget_usd.*> 0"):
            load_manifest(path)

    def test_missing_specs(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(yaml.dump({"name": "X", "budget_usd": 5.0}), encoding="utf-8")
        with pytest.raises(ValueError, match="specs"):
            load_manifest(path)

    def test_empty_specs(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(yaml.dump({"name": "X", "budget_usd": 5.0, "specs": []}), encoding="utf-8")
        with pytest.raises(ValueError, match="specs"):
            load_manifest(path)

    def test_non_string_spec_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "specs": [123]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="strings"):
            load_manifest(path)

    def test_not_a_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "campaign.yaml"
        path.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_manifest(path)


# ── run_campaign ──────────────────────────────────────────────────────


class TestRunCampaign:
    def test_success_path(self, tmp_path: Path) -> None:
        """Two specs both succeed, costs accumulate, audit written."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=2.0)
        result_b = _make_coordinator_result(success=True, cost=3.0)

        with patch("theforge.campaign.run_task", side_effect=[result_a, result_b]):
            campaign = run_campaign(config, manifest_path)

        assert campaign.specs_total == 2
        assert campaign.specs_succeeded == 2
        assert campaign.specs_failed == 0
        assert campaign.specs_skipped == 0
        assert campaign.total_cost_usd == pytest.approx(5.0)
        assert campaign.stopped_reason is None
        assert len(campaign.results) == 2

        # Audit file should exist
        audit_path = tmp_path / "campaign-audit.yaml"
        assert audit_path.exists()
        with open(audit_path) as f:
            audit = yaml.safe_load(f)
        assert audit["campaign"]["specs_succeeded"] == 2
        assert audit["campaign"]["total_cost_usd"] == pytest.approx(5.0)
        assert len(audit["specs"]) == 2

    def test_spec_failure_continues(self, tmp_path: Path) -> None:
        """Campaign continues after individual spec failure."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)
        result_b = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.campaign.run_task", side_effect=[result_a, result_b]):
            campaign = run_campaign(config, manifest_path)

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

        with patch("theforge.campaign.run_task", return_value=result):
            campaign = run_campaign(config, manifest_path)

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

        with patch("theforge.campaign.run_task", side_effect=[result_a]) as mock_run:
            campaign = run_campaign(config, manifest_path)

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

        with patch("theforge.campaign.run_task", side_effect=[result_a]) as mock_run:
            campaign = run_campaign(config, manifest_path)

        assert mock_run.call_count == 1
        assert campaign.specs_skipped == 1  # spec B skipped

    def test_auto_merge_passed_through(self, tmp_path: Path) -> None:
        """auto_merge flag is forwarded to each run_task call."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md"])
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.campaign.run_task", return_value=result_a) as mock_run:
            run_campaign(config, manifest_path, auto_merge=True)

        _, kwargs = mock_run.call_args
        assert kwargs.get("auto_merge") is True

    def test_interactive_passed_through(self, tmp_path: Path) -> None:
        """interactive flag is forwarded to each run_task call."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md"])
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.campaign.run_task", return_value=result_a) as mock_run:
            run_campaign(config, manifest_path, interactive=True)

        _, kwargs = mock_run.call_args
        assert kwargs.get("interactive") is True

    def test_manifest_missing_spec_file_raises(self, tmp_path: Path) -> None:
        """run_campaign raises ValueError if spec files don't exist."""
        manifest_path = _make_manifest(tmp_path, ["nonexistent.md"])
        config = _make_config(tmp_path)

        with pytest.raises(ValueError, match="missing"):
            run_campaign(config, manifest_path)

    def test_audit_yaml_written(self, tmp_path: Path) -> None:
        """campaign-audit.yaml is written to project root."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md"])
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.5, merged=True)

        with patch("theforge.campaign.run_task", return_value=result_a):
            run_campaign(config, manifest_path)

        audit_path = tmp_path / "campaign-audit.yaml"
        assert audit_path.exists()

        with open(audit_path) as f:
            audit = yaml.safe_load(f)

        assert audit["campaign"]["name"] == "Test Campaign"
        assert audit["campaign"]["budget_usd"] == pytest.approx(10.0)
        assert "Claude" in audit["campaign"]["budget_note"]
        assert audit["specs"][0]["path"] == "spec-a.md"
        assert audit["specs"][0]["merge"] is True

    def test_skipped_specs_appear_in_audit(self, tmp_path: Path) -> None:
        """Specs skipped due to budget appear in audit with SKIPPED outcome."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=1.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.campaign.run_task", return_value=result_a):
            run_campaign(config, manifest_path)

        audit_path = tmp_path / "campaign-audit.yaml"
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

        with patch("theforge.campaign.run_task", return_value=result_failed_merge):
            campaign = run_campaign(config, manifest_path, auto_merge=True)

        # Campaign counts as succeeded (task itself passed), but merge did not happen
        assert campaign.specs_succeeded == 1

        audit_path = tmp_path / "campaign-audit.yaml"
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

        with patch("theforge.campaign._notify") as mock_notify:
            with patch("theforge.campaign.run_task", return_value=result_a):
                run_campaign(config, manifest_path, notify=True)

        mock_notify.assert_called_once()
        title, body = mock_notify.call_args[0]
        assert "Test Campaign" in title
        assert "1" in body  # specs_succeeded count

    def test_campaign_forwards_notify_true_to_run_task(self, tmp_path: Path) -> None:
        """run_campaign() passes notify=True down to each run_task() call."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.campaign._notify"):
            with patch("theforge.campaign.run_task", return_value=result_a) as mock_run_task:
                run_campaign(config, manifest_path, notify=True)

        _, kwargs = mock_run_task.call_args
        assert kwargs.get("notify") is True

    def test_campaign_forwards_notify_false_to_run_task(self, tmp_path: Path) -> None:
        """run_campaign() passes notify=False down to each run_task() call."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.campaign.run_task", return_value=result_a) as mock_run_task:
            run_campaign(config, manifest_path, notify=False)

        _, kwargs = mock_run_task.call_args
        assert kwargs.get("notify") is False

    def test_campaign_notification_skipped_with_no_notify(self, tmp_path: Path) -> None:
        """notify=False suppresses campaign completion notification."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.campaign._notify") as mock_notify:
            with patch("theforge.campaign.run_task", return_value=result_a):
                run_campaign(config, manifest_path, notify=False)

        mock_notify.assert_not_called()


class TestEscalationNotifications:
    def test_escalation_notification_sent(self, tmp_path: Path) -> None:
        """Workspace creation failure triggers an escalation notification."""
        spec_path = tmp_path / "spec.md"
        spec_path.write_text("---\nslug: my-slug\n---\n# Spec", encoding="utf-8")
        config = _make_config(tmp_path)

        from theforge.task import TaskSpec

        task = TaskSpec(name="Test", spec_path=spec_path, slug="my-slug", file_scope=[])

        with patch("theforge.coordinator._notify") as mock_notify:
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

        task = TaskSpec(name="Test", spec_path=spec_path, slug="my-slug", file_scope=[])
        long_error = "x" * 200

        with patch("theforge.coordinator._notify") as mock_notify:
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

        task = TaskSpec(name="Test", spec_path=spec_path, slug="my-slug", file_scope=[])

        with patch("theforge.coordinator._notify") as mock_notify:
            with patch(
                "theforge.coordinator._create_workspace",
                return_value=(None, None, "disk full"),
            ):
                run_task(config, task, notify=False)

        mock_notify.assert_not_called()


class TestNotifyFunction:
    def test_notify_fail_silent(self) -> None:
        """_notify swallows subprocess errors and never raises."""
        with patch("theforge.coordinator.shutil.which", return_value="/usr/bin/osascript"):
            with patch("theforge.coordinator.subprocess.run", side_effect=OSError("broken pipe")):
                # Must not raise
                _notify("Title", "Body")

    def test_notify_noop_without_osascript(self) -> None:
        """_notify does nothing when osascript is not available."""
        with patch("theforge.coordinator.shutil.which", return_value=None):
            with patch("theforge.coordinator.subprocess.run") as mock_run:
                _notify("Title", "Body")

        mock_run.assert_not_called()

    def test_osa_quote_escapes_backslash_and_quote(self) -> None:
        """_osa_quote wraps in double quotes and escapes special chars."""
        result = _osa_quote('say "hello\\world"')
        assert result == '"say \\"hello\\\\world\\""'
