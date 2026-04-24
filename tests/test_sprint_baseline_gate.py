from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

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
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.runner import run_sprint
from theforge.sprint.sources import FileSource


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_resolved(tmp_path: Path) -> ResolvedSprint:
    story_file = tmp_path / "story.md"
    story_file.write_text(
        "---\nname: My Story\nslug: my-story\n---\n# Content\n",
        encoding="utf-8",
    )
    source = FileSource()
    task = source.fetch(str(story_file.relative_to(tmp_path)), tmp_path)
    return ResolvedSprint(
        name="Test Sprint",
        budget_usd=10.0,
        stories=[(task, source, "story.md")],
        max_parallel=1,
    )


def _fake_result():
    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase

    state = CoordinatorState()
    state.preflight_result = MagicMock(cost_usd=0.0)
    return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")


def test_baseline_pass_proceeds_to_normal_sprint_flow(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path)

    with (
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch("theforge.sprint.runner.run_task", return_value=_fake_result()) as mock_run_task,
        patch("theforge.sprint.runner._write_sprint_audit"),
        patch("theforge.sprint.runner._write_sprint_summary"),
    ):
        result = run_sprint(config, resolved)

    assert result.specs_succeeded == 1
    assert mock_run_task.called


def test_baseline_fail_aborts_before_any_agent_runner(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path)
    baseline = {
        "passed": False,
        "status": "fail",
        "exit_code": 2,
        "duration_seconds": 1.25,
        "message": (
            "Broken baseline: configured gate failed on sprint merge base abc123 "
            "before any dev work started (Gate returned FAIL)"
        ),
    }

    with (
        patch("theforge.sprint.runner._run_baseline_gate", return_value=baseline),
        patch("theforge.sprint.runner.run_batch_preflight") as mock_preflight,
        patch("theforge.sprint.runner.run_task") as mock_run_task,
    ):
        try:
            run_sprint(config, resolved)
            raise AssertionError("expected baseline failure")
        except RuntimeError as exc:
            assert "Broken baseline" in str(exc)

    assert not mock_preflight.called
    assert not mock_run_task.called

    audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
    audit = yaml.safe_load(audit_path.read_text(encoding="utf-8"))
    assert audit["baseline_check"]["passed"] is False
    assert audit["baseline_check"]["exit_code"] == 2
    assert audit["sprint"]["stopped_reason"] == "broken_baseline"
