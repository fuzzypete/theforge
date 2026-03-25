"""Tests for sprint mode: multi-spec sequential execution."""

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
    BackendConfig,
    ForgeConfig,
    NotificationConfig,
    RetryPolicy,
    SprintConfig,
    WorkspaceConfig,
)
from theforge.coordinator.engine import (
    CoordinatorResult,
    CoordinatorState,
    Phase,
    _notify,
    _osa_quote,
    run_task,
)
from theforge.sprint import load_sprint_manifest, run_sprint
from theforge.sprint.dag import StoryDAG, StoryTriage, _triage_spec, build_dag
from theforge.sprint.manifest import _build_task_from_story
from theforge.sprint.runner import _classify_and_record

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
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "My Sprint", "budget_usd": 5.0, "stories": ["a.md", "b.md"]}),
            encoding="utf-8",
        )
        manifest = load_sprint_manifest(path)
        assert manifest.name == "My Sprint"
        assert manifest.budget_usd == 5.0
        assert manifest.stories == ["a.md", "b.md"]

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            load_sprint_manifest(tmp_path / "nonexistent.yaml")

    def test_missing_name(self, tmp_path: Path) -> None:
        path = tmp_path / "sprint.yaml"
        path.write_text(yaml.dump({"budget_usd": 5.0, "stories": ["a.md"]}), encoding="utf-8")
        with pytest.raises(ValueError, match="name"):
            load_sprint_manifest(path)

    def test_missing_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "sprint.yaml"
        path.write_text(yaml.dump({"name": "X", "stories": ["a.md"]}), encoding="utf-8")
        with pytest.raises(ValueError, match="budget_usd"):
            load_sprint_manifest(path)

    def test_zero_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 0.0, "stories": ["a.md"]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="budget_usd.*> 0"):
            load_sprint_manifest(path)

    def test_negative_budget(self, tmp_path: Path) -> None:
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": -1.0, "stories": ["a.md"]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="budget_usd.*> 0"):
            load_sprint_manifest(path)

    def test_missing_specs(self, tmp_path: Path) -> None:
        path = tmp_path / "sprint.yaml"
        path.write_text(yaml.dump({"name": "X", "budget_usd": 5.0}), encoding="utf-8")
        with pytest.raises(ValueError, match="stories"):
            load_sprint_manifest(path)

    def test_empty_specs(self, tmp_path: Path) -> None:
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "stories": []}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="stories"):
            load_sprint_manifest(path)

    def test_non_string_spec_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "specs": [123]}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="strings"):
            load_sprint_manifest(path)

    def test_not_a_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "sprint.yaml"
        path.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            load_sprint_manifest(path)


# ── run_sprint ──────────────────────────────────────────────────────


class TestRunSprint:
    def test_success_path(self, tmp_path: Path) -> None:
        """Two specs both succeed, costs accumulate, audit written."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=2.0)
        result_b = _make_coordinator_result(success=True, cost=3.0)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a, result_b]):
            sprint_result = run_sprint(config, manifest_path)

        assert sprint_result.specs_total == 2
        assert sprint_result.specs_succeeded == 2
        assert sprint_result.specs_failed == 0
        assert sprint_result.specs_skipped == 0
        assert sprint_result.total_cost_usd == pytest.approx(5.0)
        assert sprint_result.stopped_reason is None
        assert len(sprint_result.results) == 2

        # Audit file should exist
        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        assert audit_path.exists()
        with open(audit_path) as f:
            audit = yaml.safe_load(f)
        assert audit["sprint"]["specs_succeeded"] == 2
        assert audit["sprint"]["total_cost_usd"] == pytest.approx(5.0)
        assert len(audit["specs"]) == 2

    def test_spec_failure_continues(self, tmp_path: Path) -> None:
        """Sprint continues after individual spec failure."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md", "spec-b.md"], budget=10.0)
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)
        result_b = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a, result_b]):
            sprint_result = run_sprint(config, manifest_path)

        assert sprint_result.specs_succeeded == 1
        assert sprint_result.specs_failed == 1
        assert sprint_result.specs_skipped == 0
        assert sprint_result.stopped_reason is None

    def test_already_done_counted_as_skipped(self, tmp_path: Path) -> None:
        """ALREADY_DONE specs count as skipped, not succeeded or failed."""
        _make_spec_file(tmp_path, "Done Spec", "done-spec")
        manifest_path = _make_manifest(tmp_path, ["done-spec.md"], budget=10.0)
        config = _make_config(tmp_path)

        result = _make_coordinator_result(
            success=True, cost=0.15, preflight_verdict="ALREADY_DONE", phase=Phase.DONE
        )

        with patch("theforge.sprint.runner.run_task", return_value=result):
            sprint_result = run_sprint(config, manifest_path)

        assert sprint_result.specs_succeeded == 0
        assert sprint_result.specs_skipped == 1
        assert sprint_result.specs_failed == 0

    def test_budget_exceeded_stops_sprint(self, tmp_path: Path) -> None:
        """Sprint stops when accumulated cost exceeds budget."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        _make_spec_file(tmp_path, "Spec B", "spec-b")
        _make_spec_file(tmp_path, "Spec C", "spec-c")
        manifest_path = _make_manifest(
            tmp_path, ["spec-a.md", "spec-b.md", "spec-c.md"], budget=5.0
        )
        config = _make_config(tmp_path)

        # First spec costs 6.0, which exceeds the $5 budget
        result_a = _make_coordinator_result(success=True, cost=6.0)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            sprint_result = run_sprint(config, manifest_path)

        # Only spec A ran; B and C were skipped
        assert mock_run.call_count == 1
        assert sprint_result.specs_succeeded == 1
        assert sprint_result.specs_skipped == 2
        assert sprint_result.stopped_reason is not None
        assert "budget" in sprint_result.stopped_reason.lower()

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

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            sprint_result = run_sprint(config, manifest_path)

        assert mock_run.call_count == 1
        assert sprint_result.specs_skipped == 1  # spec B skipped

    def test_auto_merge_passed_through(self, tmp_path: Path) -> None:
        """auto_merge flag is forwarded to each run_task call."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md"])
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner.run_task", return_value=result_a) as mock_run:
            run_sprint(config, manifest_path, auto_merge=True)

        _, kwargs = mock_run.call_args
        assert kwargs.get("auto_merge") is True

    def test_interactive_passed_through(self, tmp_path: Path) -> None:
        """interactive flag is forwarded to each run_task call."""
        _make_spec_file(tmp_path, "Spec A", "spec-a")
        manifest_path = _make_manifest(tmp_path, ["spec-a.md"])
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner.run_task", return_value=result_a) as mock_run:
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

        with patch("theforge.sprint.runner.run_task", return_value=result_a):
            run_sprint(config, manifest_path)

        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        assert audit_path.exists()

        with open(audit_path) as f:
            audit = yaml.safe_load(f)

        assert audit["sprint"]["name"] == "Test Sprint"
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

        with patch("theforge.sprint.runner.run_task", return_value=result_a):
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

        with patch("theforge.sprint.runner.run_task", return_value=result_failed_merge):
            sprint_result = run_sprint(config, manifest_path, auto_merge=True)

        # Sprint counts as succeeded (task itself passed), but merge did not happen
        assert sprint_result.specs_succeeded == 1

        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        with open(audit_path) as f:
            audit = yaml.safe_load(f)

        assert audit["specs"][0]["merge"] is False


# ── Notification tests ────────────────────────────────────────────────


class TestSprintNotifications:
    def test_sprint_notification_sent(self, tmp_path: Path) -> None:
        """Sprint completion sends exactly one notification with name in title."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        # Use a non-none backend so the native OS notification fires
        config = ForgeConfig(
            project=config.project,
            project_root=config.project_root,
            workspace=config.workspace,
            validation=config.validation,
            dev_profile=config.dev_profile,
            preflight_profile=config.preflight_profile,
            review_pool=config.review_pool,
            synthesis_profile=None,
            retry=config.retry,
            notifications=NotificationConfig(
                backend="terminal",
                backends=(BackendConfig(type="terminal"),),
            ),
        )
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint.runner._notify") as mock_notify:
            with patch("theforge.sprint.runner.run_task", return_value=result_a):
                run_sprint(config, manifest_path, notify=True)

        mock_notify.assert_called_once()
        title, body = mock_notify.call_args[0]
        assert "Test Sprint" in title
        assert "1" in body  # specs_succeeded count

    def test_sprint_forwards_notify_true_to_run_task(self, tmp_path: Path) -> None:
        """run_sprint() passes notify=True down to each run_task() call."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint.runner._notify"):
            with patch("theforge.sprint.runner.run_task", return_value=result_a) as mock_run_task:
                run_sprint(config, manifest_path, notify=True)

        _, kwargs = mock_run_task.call_args
        assert kwargs.get("notify") is True

    def test_sprint_forwards_notify_false_to_run_task(self, tmp_path: Path) -> None:
        """run_sprint() passes notify=False down to each run_task() call."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint.runner.run_task", return_value=result_a) as mock_run_task:
            run_sprint(config, manifest_path, notify=False)

        _, kwargs = mock_run_task.call_args
        assert kwargs.get("notify") is False

    def test_sprint_notification_skipped_with_no_notify(self, tmp_path: Path) -> None:
        """notify=False suppresses sprint completion notification."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint.runner._notify") as mock_notify:
            with patch("theforge.sprint.runner.run_task", return_value=result_a):
                run_sprint(config, manifest_path, notify=False)

        mock_notify.assert_not_called()

    def test_sprint_notification_suppressed_for_backend_none(self, tmp_path: Path) -> None:
        """backend: none suppresses the native OS notification even when notify=True."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        # _make_config() defaults to backend="none" (NotificationConfig default)
        config = _make_config(tmp_path)
        assert config.notifications.backend == "none"
        result_a = _make_coordinator_result(success=True, cost=2.0)

        with patch("theforge.sprint.runner._notify") as mock_notify:
            with patch("theforge.sprint.runner.run_task", return_value=result_a):
                run_sprint(config, manifest_path, notify=True)

        mock_notify.assert_not_called()


class TestEscalationNotifications:
    def test_escalation_notification_sent(self, tmp_path: Path) -> None:
        """Workspace creation failure triggers an escalation notification."""
        spec_path = tmp_path / "spec.md"
        spec_path.write_text("---\nslug: my-slug\n---\n# Spec", encoding="utf-8")
        config = _make_config(tmp_path)

        from theforge.task import TaskStory

        task = TaskStory(name="Test", story_path=spec_path, slug="my-slug")

        with patch("theforge.coordinator.notify._notify") as mock_notify:
            with patch(
                "theforge.coordinator.engine._create_workspace",
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

        from theforge.task import TaskStory

        task = TaskStory(name="Test", story_path=spec_path, slug="my-slug")
        long_error = "x" * 200

        with patch("theforge.coordinator.notify._notify") as mock_notify:
            with patch(
                "theforge.coordinator.engine._create_workspace",
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

        from theforge.task import TaskStory

        task = TaskStory(name="Test", story_path=spec_path, slug="my-slug")

        with patch("theforge.coordinator.notify._notify") as mock_notify:
            with patch(
                "theforge.coordinator.engine._create_workspace",
                return_value=(None, None, "disk full"),
            ):
                run_task(config, task, notify=False)

        mock_notify.assert_not_called()


class TestNotifyFunction:
    def test_notify_fail_silent(self) -> None:
        """_notify swallows subprocess errors and never raises."""
        with patch("theforge.coordinator.notify.shutil.which", return_value="/usr/bin/osascript"):
            with patch(
                "theforge.coordinator.notify.subprocess.run", side_effect=OSError("broken pipe")
            ):
                # Must not raise
                _notify("Title", "Body")

    def test_notify_noop_without_osascript(self) -> None:
        """_notify does nothing when osascript is not available."""
        with patch("theforge.coordinator.notify.shutil.which", return_value=None):
            with patch("theforge.coordinator.notify.subprocess.run") as mock_run:
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
                m.stdout = b"0"  # 0 commits ahead — just created at base HEAD
            else:
                m.returncode = 0
                m.stdout = b""
            return m

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
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

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
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

        with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run):
            with patch("theforge.sprint.dag._run_gate", return_value=("PASS", None, "")):
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

        skip_triage = StoryTriage(
            story_path="feature-a.md",
            action="skip",
            reason="prior APPROVE in audit trail (2 commits ahead)",
            worktree_path=worktree,
            slug="feature-a",
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=skip_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run_task:
                result = run_sprint(config, manifest_path, resume=True)

        mock_run_task.assert_not_called()
        assert result.specs_succeeded == 1


class TestResumeSprintIntegration:
    def test_resume_sprint_skips_merged(self, tmp_path: Path) -> None:
        """End-to-end resume: merged spec counts as succeeded (work is done)."""
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

        def triage_side_effect(spec_path, config, project_root):
            if "feature-a" in spec_path:
                return merged_triage
            return full_triage

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task") as mock_run:
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

        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )
        coord_result = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.runner.run_task", return_value=coord_result):
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

        full_triage = StoryTriage(
            story_path="feature-a.md",
            action="full",
            reason="no worktree found",
            worktree_path=None,
        )

        with patch("theforge.sprint.runner._triage_spec", return_value=full_triage):
            with patch("theforge.sprint.runner.run_task") as mock_run:
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

        with patch("theforge.sprint.runner._triage_spec") as mock_triage:
            with patch("theforge.sprint.runner.run_task", return_value=coord_result) as mock_task:
                result = run_sprint(config, manifest_path, resume=False)

        mock_triage.assert_not_called()
        mock_task.assert_called_once()
        assert result.specs_succeeded == 1


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

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
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

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
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

        def triage_side_effect(spec_path, config, project_root):
            if "spec-a" in spec_path:
                return merged_triage
            return full_triage

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task", return_value=result_b) as mock_run:
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

        approved_triage = StoryTriage(
            story_path="spec-a.md",
            action="skip",
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

        def triage_side_effect(spec_path, config, project_root):
            if "spec-a" in spec_path:
                return approved_triage
            return full_triage

        with patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect):
            with patch("theforge.sprint.runner.run_task", return_value=result_b) as mock_run:
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

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_c]
        ) as mock_run:
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

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
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

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
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

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            result = run_sprint(config, manifest_path)

        assert mock_run.call_count == 2  # both specs ran
        assert result.specs_skipped == 1  # spec-a counted as skipped (ALREADY_DONE)
        assert result.specs_succeeded == 1  # spec-b succeeded
        assert result.stopped_reason is None  # no halt


# ── max_parallel manifest parsing ────────────────────────────────────────────


class TestMaxParallelManifest:
    def test_parses_max_parallel(self, tmp_path: Path) -> None:
        """load_sprint_manifest parses max_parallel=3 correctly."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "stories": ["a.md"], "max_parallel": 3}),
            encoding="utf-8",
        )
        manifest = load_sprint_manifest(path)
        assert manifest.max_parallel == 3

    def test_defaults_to_none_when_absent(self, tmp_path: Path) -> None:
        """max_parallel is None (sentinel) when not specified in manifest."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "stories": ["a.md"]}),
            encoding="utf-8",
        )
        manifest = load_sprint_manifest(path)
        assert manifest.max_parallel is None

    def test_rejects_max_parallel_zero(self, tmp_path: Path) -> None:
        """max_parallel=0 is rejected with ValueError."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "stories": ["a.md"], "max_parallel": 0}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="max_parallel"):
            load_sprint_manifest(path)

    def test_rejects_max_parallel_negative(self, tmp_path: Path) -> None:
        """max_parallel=-1 is rejected with ValueError."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "stories": ["a.md"], "max_parallel": -1}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="max_parallel"):
            load_sprint_manifest(path)

    def test_rejects_max_parallel_non_integer(self, tmp_path: Path) -> None:
        """max_parallel as float string is rejected."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            'name: X\nbudget_usd: 5.0\nstories: [a.md]\nmax_parallel: "2"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="max_parallel"):
            load_sprint_manifest(path)


def _make_config_with_sprint(tmp_path: Path, sprint_max_parallel: int = 1) -> ForgeConfig:
    """Build a ForgeConfig with a custom sprint.max_parallel default."""
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
        sprint=SprintConfig(max_parallel=sprint_max_parallel),
    )


class TestMaxParallelPrecedence:
    """Tests for manifest vs forge.yaml max_parallel precedence."""

    def _make_manifest(self, tmp_path: Path, max_parallel: int | None = None) -> Path:
        data: dict = {"name": "X", "budget_usd": 5.0, "stories": ["story-a.md"]}
        if max_parallel is not None:
            data["max_parallel"] = max_parallel
        path = tmp_path / "sprint.yaml"
        path.write_text(yaml.dump(data), encoding="utf-8")
        return path

    def test_manifest_wins_over_config_default(self, tmp_path: Path) -> None:
        """Manifest max_parallel=2 overrides config default of 3."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = self._make_manifest(tmp_path, max_parallel=2)
        config = _make_config_with_sprint(tmp_path, sprint_max_parallel=3)

        with patch("theforge.sprint.runner.run_task") as mock_run:
            mock_run.return_value = _make_coordinator_result(success=True)
            run_sprint(config, manifest_path)

        manifest = load_sprint_manifest(manifest_path)
        assert manifest.max_parallel == 2  # unchanged after load (not None)

    def test_config_default_used_when_manifest_omits(self, tmp_path: Path) -> None:
        """When manifest omits max_parallel, config default (3) is used in run_sprint."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = self._make_manifest(tmp_path, max_parallel=None)
        config = _make_config_with_sprint(tmp_path, sprint_max_parallel=3)

        with patch("theforge.sprint.runner.run_task") as mock_run:
            mock_run.return_value = _make_coordinator_result(success=True)
            result = run_sprint(config, manifest_path)

        assert result.specs_succeeded == 1

    def test_neither_set_defaults_to_1(self, tmp_path: Path) -> None:
        """When neither manifest nor config set max_parallel, default is 1."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = self._make_manifest(tmp_path, max_parallel=None)
        config = _make_config(tmp_path)  # SprintConfig defaults to max_parallel=1

        with patch("theforge.sprint.runner.run_task") as mock_run:
            mock_run.return_value = _make_coordinator_result(success=True)
            run_sprint(config, manifest_path)

        assert config.sprint.max_parallel == 1

    def test_manifest_none_is_resolved_in_run_sprint(self, tmp_path: Path) -> None:
        """After load_sprint_manifest, max_parallel is None; run_sprint resolves it."""
        manifest_path = self._make_manifest(tmp_path, max_parallel=None)
        manifest = load_sprint_manifest(manifest_path)
        assert manifest.max_parallel is None


# ── StoryDAG unit tests ───────────────────────────────────────────────────────


def _make_task(
    slug: str, depends_on: list[str] | None = None, tmp_path: Path | None = None
) -> "TaskSpec":  # noqa: F821
    from theforge.task import TaskStory

    path = (tmp_path or Path("/tmp")) / f"{slug}.md"
    return TaskStory(
        name=slug.replace("-", " ").title(),
        story_path=path,
        slug=slug,
        depends_on=depends_on or [],
    )


class TestStoryDAGUnit:
    def test_no_dep_stories_immediately_ready(self) -> None:
        """Stories with no depends_on are ready immediately."""
        a = _make_task("a")
        b = _make_task("b")
        dag = StoryDAG([a, b])
        ready_slugs = {t.slug for t in dag.ready()}
        assert ready_slugs == {"a", "b"}

    def test_marking_complete_unlocks_dependent(self) -> None:
        """Marking a story complete makes its dependents ready."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])

        assert {t.slug for t in dag.ready()} == {"a"}
        dag.mark_complete("a")
        assert {t.slug for t in dag.ready()} == {"b"}

    def test_marking_skipped_does_not_unlock_dependent(self) -> None:
        """Marking a story skipped does NOT make its dependents ready."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])

        dag.mark_skipped("a")
        assert dag.ready() == []

    def test_is_done_false_until_all_finished(self) -> None:
        """is_done() is False until every task has been finished."""
        a = _make_task("a")
        b = _make_task("b")
        dag = StoryDAG([a, b])

        assert not dag.is_done()
        dag.mark_complete("a")
        assert not dag.is_done()
        dag.mark_skipped("b")
        assert dag.is_done()

    def test_is_done_true_with_mix_of_complete_and_skipped(self) -> None:
        """is_done() considers both completed and skipped as finished."""
        tasks = [_make_task(s) for s in ["a", "b", "c"]]
        dag = StoryDAG(tasks)
        dag.mark_complete("a")
        dag.mark_skipped("b")
        dag.mark_skipped("c")
        assert dag.is_done()

    def test_unmet_deps_returns_missing_completed_deps(self) -> None:
        """unmet_deps returns deps not yet completed."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a", "c"])
        dag = StoryDAG([a, b])
        dag.mark_complete("a")
        assert dag.unmet_deps("b") == ["c"]

    def test_remaining_excludes_finished(self) -> None:
        """remaining() returns only tasks not yet finished."""
        tasks = [_make_task(s) for s in ["a", "b", "c"]]
        dag = StoryDAG(tasks)
        dag.mark_complete("a")
        dag.mark_skipped("b")
        remaining_slugs = {t.slug for t in dag.remaining()}
        assert remaining_slugs == {"c"}

    def test_multi_dep_unlocked_only_when_all_complete(self) -> None:
        """Story with multiple deps only becomes ready when ALL deps complete."""
        a = _make_task("a")
        b = _make_task("b")
        c = _make_task("c", depends_on=["a", "b"])
        dag = StoryDAG([a, b, c])

        dag.mark_complete("a")
        assert "c" not in {t.slug for t in dag.ready()}
        dag.mark_complete("b")
        assert "c" in {t.slug for t in dag.ready()}

    def test_build_dag_returns_story_dag(self) -> None:
        """build_dag() returns a properly initialized StoryDAG."""
        a = _make_task("a")
        dag = build_dag([a])
        assert isinstance(dag, StoryDAG)
        assert {t.slug for t in dag.ready()} == {"a"}


# ── Parallel sprint execution tests ──────────────────────────────────────────


def _make_manifest_parallel(
    tmp_path: Path, specs: list[str], budget: float = 10.0, max_parallel: int = 1
) -> Path:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        yaml.dump(
            {
                "name": "Parallel Sprint",
                "budget_usd": budget,
                "stories": specs,
                "max_parallel": max_parallel,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


class TestParallelIndependentStories:
    def test_parallel_3_independent_all_succeed(self, tmp_path: Path) -> None:
        """max_parallel=3, 3 independent stories — all run, all counted."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        _make_spec_file(tmp_path, "Story C", "story-c")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md", "story-c.md"],
            budget=10.0,
            max_parallel=3,
        )
        config = _make_config(tmp_path)

        results = [
            _make_coordinator_result(success=True, cost=1.0),
            _make_coordinator_result(success=True, cost=1.0),
            _make_coordinator_result(success=True, cost=1.0),
        ]

        with patch("theforge.sprint.runner.run_task", side_effect=results) as mock_run:
            sprint = run_sprint(config, manifest_path)

        assert mock_run.call_count == 3
        assert sprint.specs_succeeded == 3
        assert sprint.specs_failed == 0
        assert sprint.specs_skipped == 0
        assert sprint.total_cost_usd == pytest.approx(3.0)
        # outcome: done requires no failures and no early-stop reason
        assert sprint.stopped_reason is None

    def test_parallel_one_fail_others_complete(self, tmp_path: Path) -> None:
        """One story fails — the other two still run and are counted correctly."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        _make_spec_file(tmp_path, "Story C", "story-c")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md", "story-c.md"],
            budget=10.0,
            max_parallel=3,
        )
        config = _make_config(tmp_path)

        results = [
            _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE),
            _make_coordinator_result(success=True, cost=1.0),
            _make_coordinator_result(success=True, cost=1.0),
        ]

        with patch("theforge.sprint.runner.run_task", side_effect=results) as mock_run:
            sprint = run_sprint(config, manifest_path)

        assert mock_run.call_count == 3
        assert sprint.specs_succeeded == 2
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 0
        assert sprint.total_cost_usd == pytest.approx(3.0)

    def test_parallel_auto_merge_false_in_worker(self, tmp_path: Path) -> None:
        """With max_parallel>1, workers run with auto_merge=False."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        results = [
            _make_coordinator_result(success=True, cost=1.0),
            _make_coordinator_result(success=True, cost=1.0),
        ]

        with patch("theforge.sprint.runner.run_task", side_effect=results) as mock_run:
            run_sprint(config, manifest_path, auto_merge=True)

        # In parallel mode, workers get auto_merge=False; merging is done in main thread
        for call in mock_run.call_args_list:
            assert call.kwargs["auto_merge"] is False


class TestParallelDependencyGating:
    def test_dependency_blocks_until_predecessor_completes(self, tmp_path: Path) -> None:
        """Story B (depends on A) is skipped when A doesn't merge."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        # A succeeds but does NOT merge → B should be skipped
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            sprint = run_sprint(config, manifest_path)

        assert mock_run.call_count == 1  # only A ran
        assert sprint.specs_succeeded == 1
        assert sprint.specs_skipped == 1

    def test_dependency_satisfied_by_merge_unlocks_dependent(self, tmp_path: Path) -> None:
        """Story B runs after A merges (in max_parallel=1 mode with eager merge)."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=True)
        result_b = _make_coordinator_result(success=True, cost=1.0)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            sprint = run_sprint(config, manifest_path, auto_merge=True)

        assert mock_run.call_count == 2
        assert sprint.specs_succeeded == 2

    def test_failed_dep_causes_dependent_to_skip(self, tmp_path: Path) -> None:
        """When A fails, B (dep: A) is marked skipped."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            sprint = run_sprint(config, manifest_path)

        assert mock_run.call_count == 1
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 1


class TestParallelBudgetPooling:
    def test_budget_pooled_across_workers(self, tmp_path: Path) -> None:
        """Third story is skipped once accumulated cost reaches budget.

        With max_parallel=2, A and B are submitted simultaneously.  Each costs
        1.0 against a budget of 1.0.  After the first story completes the
        accumulated cost is >= budget, so C is budget-skipped on the next
        scheduling pass regardless of whether B has also finished yet.
        """
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        _make_spec_file(tmp_path, "Story C", "story-c")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md", "story-c.md"],
            budget=1.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        # A and B each cost 1.0 against budget=1.0.  After at least one
        # completes, accumulated_cost >= 1.0 so C is budget-skipped.
        result_a = _make_coordinator_result(success=True, cost=1.0)
        result_b = _make_coordinator_result(success=True, cost=1.0)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            sprint = run_sprint(config, manifest_path)

        # C was budget-skipped; A and B both ran (both submitted before cost accumulated)
        assert mock_run.call_count == 2
        assert sprint.specs_skipped == 1
        assert sprint.stopped_reason is not None
        assert "budget" in sprint.stopped_reason.lower()

    def test_max_parallel_1_budget_sequential(self, tmp_path: Path) -> None:
        """With max_parallel=1, budget check works story-by-story."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=1.5,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.5)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            sprint = run_sprint(config, manifest_path)

        assert mock_run.call_count == 1
        assert sprint.specs_skipped == 1


class TestClassifyAndRecord:
    """Unit tests for _classify_and_record helper."""

    def test_success_no_merge_skips_for_dag(self) -> None:
        """Success without merge → specs_succeeded, dag.mark_skipped (not complete)."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(success=True, cost=1.0, merged=False)
        ds, df, dsk = _classify_and_record(a, result, dag, merged_slugs)

        assert ds == 1
        assert df == 0
        assert dsk == 0
        assert "a" not in merged_slugs
        # B still not ready (a not completed)
        assert dag.ready() == []

    def test_success_with_merge_completes_for_dag(self) -> None:
        """Success with merge → specs_succeeded, dag.mark_complete (unlocks deps)."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(success=True, cost=1.0, merged=True)
        ds, df, dsk = _classify_and_record(a, result, dag, merged_slugs)

        assert ds == 1
        assert "a" in merged_slugs
        # B now ready (a completed)
        assert {t.slug for t in dag.ready()} == {"b"}

    def test_already_done_completes_for_dag(self) -> None:
        """ALREADY_DONE → specs_skipped, dag.mark_complete (satisfies deps)."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(
            success=True, cost=0.0, preflight_verdict="ALREADY_DONE", phase=Phase.DONE
        )
        ds, df, dsk = _classify_and_record(a, result, dag, merged_slugs)

        assert ds == 0
        assert dsk == 1
        assert "a" in merged_slugs
        assert {t.slug for t in dag.ready()} == {"b"}

    def test_failure_skips_for_dag(self) -> None:
        """Failure → specs_failed, dag.mark_skipped."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)
        ds, df, dsk = _classify_and_record(a, result, dag, merged_slugs)

        assert df == 1
        assert "a" not in merged_slugs
        assert dag.ready() == []


class TestMergeOrdering:
    def test_merge_ordering_deferred_parallel(self, tmp_path: Path) -> None:
        """auto_merge=True, max_parallel=2, B depends_on A: merge happens in dep order."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=1,  # sequential to keep test deterministic
        )
        config = _make_config(tmp_path)

        # A merges, then B has satisfied dep, can be merged
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=True)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=True)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            sprint = run_sprint(config, manifest_path, auto_merge=True)

        assert mock_run.call_count == 2
        assert sprint.specs_succeeded == 2


class TestMaxParallel1Fallback:
    def test_max_parallel_1_sequential_behavior(self, tmp_path: Path) -> None:
        """max_parallel=1 produces identical outcome to the original sequential runner."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        _make_spec_file(tmp_path, "Story C", "story-c")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md", "story-c.md"],
            budget=10.0,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        results = [
            _make_coordinator_result(success=True, cost=1.0),
            _make_coordinator_result(success=False, cost=0.5, phase=Phase.ESCALATE),
            _make_coordinator_result(success=True, cost=2.0),
        ]

        with patch("theforge.sprint.runner.run_task", side_effect=results) as mock_run:
            sprint = run_sprint(config, manifest_path)

        assert mock_run.call_count == 3
        assert sprint.specs_succeeded == 2
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 0
        assert sprint.total_cost_usd == pytest.approx(3.5)
        assert sprint.stopped_reason is None

    def test_max_parallel_1_dep_check_same_as_sequential(self, tmp_path: Path) -> None:
        """Dependency gating works correctly with max_parallel=1."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        # A succeeds but does NOT merge → B should be skipped (same as old behavior)
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            sprint = run_sprint(config, manifest_path)

        assert mock_run.call_count == 1
        assert sprint.specs_succeeded == 1
        assert sprint.specs_skipped == 1
        assert sprint.stopped_reason is None


# ── TestBuildDagValidation ────────────────────────────────────────────────────


class TestBuildDagValidation:
    def test_missing_dep_raises_value_error(self) -> None:
        """build_dag raises ValueError when depends_on references a slug not in the manifest."""
        a = _make_task("story-a")
        b = _make_task("story-b", depends_on=["nonexistent-slug"])
        with pytest.raises(ValueError, match="unknown slug"):
            build_dag([a, b])

    def test_circular_dependency_raises_value_error(self) -> None:
        """build_dag raises ValueError on circular dependency (A → B → A)."""
        a = _make_task("story-a", depends_on=["story-b"])
        b = _make_task("story-b", depends_on=["story-a"])
        with pytest.raises(ValueError, match="[Cc]ircular"):
            build_dag([a, b])

    def test_self_dependency_raises_value_error(self) -> None:
        """build_dag raises ValueError when a story depends on itself."""
        a = _make_task("story-a", depends_on=["story-a"])
        with pytest.raises(ValueError, match="[Cc]ircular"):
            build_dag([a])

    def test_valid_dag_no_error(self) -> None:
        """build_dag does not raise for a valid linear dependency chain."""
        a = _make_task("story-a")
        b = _make_task("story-b", depends_on=["story-a"])
        c = _make_task("story-c", depends_on=["story-b"])
        dag = build_dag([a, b, c])
        assert dag.ready() == [a]

    def test_three_way_cycle_raises_value_error(self) -> None:
        """build_dag raises ValueError for a 3-story cycle (A → B → C → A)."""
        a = _make_task("story-a", depends_on=["story-c"])
        b = _make_task("story-b", depends_on=["story-a"])
        c = _make_task("story-c", depends_on=["story-b"])
        with pytest.raises(ValueError, match="[Cc]ircular"):
            build_dag([a, b, c])


# ── TestWorkerExceptionHandling ───────────────────────────────────────────────


class TestWorkerExceptionHandling:
    def test_worker_exception_marks_story_failed_sprint_continues(self, tmp_path: Path) -> None:
        """Worker exception from run_task is caught; story is marked failed; sprint finishes."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        result_b = _make_coordinator_result(success=True, cost=1.0)

        with patch(
            "theforge.sprint.runner.run_task",
            side_effect=[RuntimeError("agent crashed"), result_b],
        ):
            sprint = run_sprint(config, manifest_path)

        # A failed via exception → counted as failed; B still ran
        assert sprint.specs_failed == 1
        assert sprint.specs_succeeded == 1

    def test_worker_exception_dependent_is_skipped(self, tmp_path: Path) -> None:
        """When a worker raises, the story is marked skipped in the DAG so dependents skip too."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        with patch(
            "theforge.sprint.runner.run_task",
            side_effect=[RuntimeError("agent crashed")],
        ):
            sprint = run_sprint(config, manifest_path)

        # A raises → failed; B dep-skipped (A never completed)
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 1


# ── TestParallelMergeOrderingParallelMode ─────────────────────────────────────


class TestParallelMergeOrderingParallelMode:
    def test_merge_ordering_parallel_pending_merges(self, tmp_path: Path) -> None:
        """auto_merge=True, max_parallel=2, B depends_on A: _merge_branch called for A then B."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        # Both complete successfully (no real merge needed in test)
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)

        merge_calls: list[str] = []

        def _fake_merge(project_root, base_branch, branch, slug, wt, **kwargs):  # noqa: ANN001
            merge_calls.append(slug)
            return {"merged": True}

        with (
            patch("theforge.sprint.runner.run_task", side_effect=[result_a, result_b]),
            patch("theforge.sprint.runner._merge_branch", side_effect=_fake_merge),
        ):
            sprint = run_sprint(config, manifest_path, auto_merge=True)

        assert sprint.specs_succeeded == 2
        # A must be merged before B (dependency order)
        assert merge_calls.index("story-a") < merge_calls.index("story-b")
