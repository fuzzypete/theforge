"""Tests for sprint lifecycle: load manifest, run_sprint, notifications."""

from __future__ import annotations

from dataclasses import replace
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
    ModelProfile,
    NotificationConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.engine import run_task
from theforge.coordinator.notify import _notify, _osa_quote
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint import load_sprint_manifest, run_sprint

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
    def test_root_forge_artifacts_deindexed_before_sprint(self, tmp_path: Path) -> None:
        """run_sprint scrubs tracked .forge artifacts from the project root index first."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with (
            patch("theforge.sprint.runner._scrub_root_forge_artifacts") as mock_scrub,
            patch("theforge.sprint.runner.run_task", return_value=result_a),
        ):
            run_sprint(config, manifest_path)

        mock_scrub.assert_called_once_with(config)

    def test_api_non_claude_agents_do_not_emit_cost_tracking_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Claude CLI plus OpenAI/Google API agents should not warn about cost tracking."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        config = replace(
            config,
            review_pool=[
                replace(
                    DEFAULT_REVIEW_PROFILE,
                    name="openai-reviewer",
                    cli=None,
                    provider="openai",
                    model="gpt-5.4",
                ),
                ModelProfile(
                    name="google-reviewer",
                    cli=None,
                    provider="google",
                    model="gemini-3.1-pro-preview",
                    budget_usd=1.0,
                    timeout_seconds=300,
                    allowed_tools=DEFAULT_REVIEW_PROFILE.allowed_tools,
                ),
            ],
            synthesis_profile=None,
        )
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner.run_task", return_value=result_a):
            run_sprint(config, manifest_path)

        captured = capsys.readouterr()
        assert "Cost not tracked" not in captured.err
        assert "Budget tracks Claude costs only" not in captured.err

    def test_codex_cli_reviewer_emits_targeted_cost_tracking_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A CLI Codex reviewer should produce a targeted warning naming that reviewer."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=10.0)
        config = _make_config(tmp_path)
        config = replace(
            config,
            review_pool=[
                replace(
                    DEFAULT_REVIEW_PROFILE,
                    name="plan_reviewer",
                    cli="codex",
                    provider=None,
                    model="gpt-5.4",
                )
            ],
        )
        result_a = _make_coordinator_result(success=True, cost=1.0)

        with patch("theforge.sprint.runner.run_task", return_value=result_a):
            run_sprint(config, manifest_path)

        captured = capsys.readouterr()
        assert (
            "⚠ Cost not tracked for plan_reviewer (codex CLI, gpt-5.4). "
            "Audit totals will exclude this agent's usage."
        ) in captured.err
        assert "Budget tracks Claude costs only" not in captured.err

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

    def test_already_done_counted_as_succeeded(self, tmp_path: Path) -> None:
        """ALREADY_DONE is a terminal succeeded outcome — closed-at-fetch
        issues must surface in summary counts (per the SoT-state story)."""
        _make_spec_file(tmp_path, "Done Spec", "done-spec")
        manifest_path = _make_manifest(tmp_path, ["done-spec.md"], budget=10.0)
        config = _make_config(tmp_path)

        result = _make_coordinator_result(
            success=True, cost=0.15, preflight_verdict="ALREADY_DONE", phase=Phase.DONE
        )

        with patch("theforge.sprint.runner.run_task", return_value=result):
            sprint_result = run_sprint(config, manifest_path)

        assert sprint_result.specs_succeeded == 1
        assert sprint_result.specs_skipped == 0
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

    def test_audit_records_manifest_vs_inferred_dependencies(self, tmp_path: Path) -> None:
        """Sprint audit captures dependency provenance for each story."""
        from theforge.sprint.manifest import ResolvedSprint
        from theforge.sprint.sources import GitHubIssueSource
        from theforge.task import TaskStory

        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=True)
        resolved = ResolvedSprint(
            name="Test Sprint",
            budget_usd=10.0,
            stories=[
                (
                    TaskStory(name="Issue A", slug="issue-9", github_issue=9),
                    GitHubIssueSource(),
                    "issue:9",
                ),
                (
                    TaskStory(
                        name="Issue B",
                        slug="issue-2",
                        github_issue=2,
                        depends_on=["issue-9"],
                        inferred_dependencies=["issue-9"],
                    ),
                    GitHubIssueSource(),
                    "issue:2",
                ),
            ],
            max_parallel=2,
        )

        with patch("theforge.sprint.runner.run_task", return_value=result_a):
            run_sprint(config, resolved)

        audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
        assert audit["specs"][1]["depends_on"] == ["issue-9"]
        assert audit["specs"][1]["inferred_dependencies"] == {
            "manifest": [],
            "github_blockers": ["issue-9"],
        }

    def test_authoring_warning_for_dependency_shaped_prose_is_visible(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Sprint start logs issue-body prose that looks dependency-shaped."""
        from theforge.sprint.manifest import ResolvedSprint
        from theforge.sprint.sources import GitHubIssueSource
        from theforge.task import TaskStory

        config = _make_config(tmp_path)
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=True)
        resolved = ResolvedSprint(
            name="Test Sprint",
            budget_usd=10.0,
            stories=[
                (
                    TaskStory(
                        name="Issue B",
                        slug="issue-2",
                        github_issue=2,
                        dependency_warnings=["Depends on #265"],
                    ),
                    GitHubIssueSource(),
                    "issue:2",
                )
            ],
            max_parallel=1,
        )

        with patch("theforge.sprint.runner.run_task", return_value=result_a):
            run_sprint(config, resolved)

        captured = capsys.readouterr()
        assert "dependency-shaped prose ignored" in captured.err
        assert "issue-2 (Issue B)" in captured.err
        assert "Depends on #265" in captured.err
        assert (
            "declare dependencies with GitHub blocked-by relationships or leading issue metadata"
            in captured.err
        )


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
