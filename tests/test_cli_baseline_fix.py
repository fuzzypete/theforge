from __future__ import annotations

import argparse
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from theforge.cli.baseline_fix import cmd_baseline_fix
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    LogConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.coordinator.state import Phase
from theforge.task import TaskStory


def _make_config(tmp_path: Path, *, on_approve: str = "merge") -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="feat/{slug}",
            on_approve=on_approve,
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(),
        log=LogConfig(enabled=False),
    )


def _make_args(
    tmp_path: Path,
    *,
    sprint_audit: str | None = None,
    run: str | None = None,
    auto_merge: bool = False,
) -> argparse.Namespace:
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project:\n  root: .\n", encoding="utf-8")
    return argparse.Namespace(
        sprint_audit=sprint_audit,
        run=run,
        auto_merge=auto_merge,
        config=str(forge_yaml),
        base_branch=None,
        interactive=False,
        verbose=False,
        no_notify=True,
        no_pull=False,
    )


def _write_sprint_audit(tmp_path: Path, *, run_id: str = "abc123") -> tuple[Path, Path, Path]:
    worktree = tmp_path / ".forge" / "baseline-repro" / run_id / "worktree"
    worktree.mkdir(parents=True)
    evidence_path = (
        tmp_path / ".forge" / "logs" / "Demo Sprint" / f"run-{run_id}-baseline-gate.txt"
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        "\n".join(
            [
                "# baseline gate FAIL on merge base abcdef1234567890",
                "# gate command: pytest -q",
                "# exit code: 1",
                f"# worktree: {worktree}",
                "",
                "FAILED tests/test_storage.py::test_dates",
                "AssertionError: dates drifted",
            ]
        ),
        encoding="utf-8",
    )
    audit_path = tmp_path / ".forge" / "audits" / f"run-{run_id}-sprint-audit.yaml"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        yaml.safe_dump(
            {
                "sprint": {"name": "Demo Sprint", "stopped_reason": "broken_baseline"},
                "baseline_check": {
                    "passed": False,
                    "failure_reproduced": True,
                    "merge_base": "abcdef1234567890",
                    "command": "pytest -q",
                    "validation_profile": "merge",
                    "validation_authority": "authoritative",
                    "output_tail": "FAILED tests/test_storage.py::test_dates",
                    "worktree": str(worktree),
                    "evidence_path": str(evidence_path),
                    "failing_targets": ["tests/test_storage.py::test_dates"],
                    "failing_target_extraction": {
                        "source": "builtin",
                        "format_recognized": True,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return audit_path, worktree, evidence_path


def _result(*, success: bool, phase: Phase, message: str) -> SimpleNamespace:
    return SimpleNamespace(
        success=success,
        phase=phase,
        state=SimpleNamespace(total_cost=1.25),
        message=message,
    )


def test_cmd_baseline_fix_creates_issue_fetches_story_and_runs_task(
    tmp_path: Path, capsys
) -> None:
    config = _make_config(tmp_path, on_approve="merge")
    audit_path, _worktree, _evidence_path = _write_sprint_audit(tmp_path)
    task = TaskStory(
        name="Fix baseline",
        slug="issue-2714",
        story_text="body",
        github_issue=2714,
        type="bug",
        fix_ready=True,
        investigation_ready=True,
    )
    result = _result(success=True, phase=Phase.DONE, message="Baseline fixed")

    with (
        patch("theforge.cli.baseline_fix.load_config", return_value=config),
        patch(
            "theforge.cli.baseline_fix._create_issue",
            return_value=(2714, "https://github.com/acme/repo/issues/2714"),
        ) as mock_create,
        patch(
            "theforge.cli.baseline_fix.GitHubIssueSource.fetch",
            return_value=task,
        ) as mock_fetch,
        patch("theforge.cli.baseline_fix.run_task", return_value=result) as mock_run,
        patch(
            "theforge.cli.baseline_fix._write_audit",
            return_value=tmp_path / ".forge" / "audits" / "forge_audit.yaml",
        ) as mock_write_audit,
        patch("theforge.cli.baseline_fix.GitHubIssueSource.on_complete") as mock_complete,
    ):
        rc = cmd_baseline_fix(_make_args(tmp_path, sprint_audit=str(audit_path)))

    assert rc == 0
    create_kwargs = mock_create.call_args.kwargs
    assert "tests/test_storage.py::test_dates" in create_kwargs["body"]
    assert str(audit_path.resolve()) in create_kwargs["body"]
    assert mock_fetch.call_args.args == ("2714", config.project_root)
    assert mock_run.call_args.args == (config, task)
    assert mock_run.call_args.kwargs["auto_merge"] is False
    assert mock_write_audit.call_args.kwargs["auto_merge"] is False
    mock_complete.assert_called_once_with(task, result, config)
    assert "Created baseline repair issue #2714" in capsys.readouterr().err


def test_cmd_baseline_fix_refuses_when_no_landing_workflow(tmp_path: Path, capsys) -> None:
    config = _make_config(tmp_path, on_approve="none")
    audit_path, _worktree, _evidence_path = _write_sprint_audit(tmp_path)

    with (
        patch("theforge.cli.baseline_fix.load_config", return_value=config),
        patch("theforge.cli.baseline_fix._create_issue") as mock_create,
    ):
        rc = cmd_baseline_fix(_make_args(tmp_path, sprint_audit=str(audit_path)))

    assert rc == 1
    mock_create.assert_not_called()
    assert "requires a landing workflow" in capsys.readouterr().err


def test_cmd_baseline_fix_failure_prints_preserved_evidence_and_escalates_issue(
    tmp_path: Path, capsys
) -> None:
    config = _make_config(tmp_path, on_approve="merge")
    audit_path, worktree, evidence_path = _write_sprint_audit(tmp_path)
    task = TaskStory(
        name="Fix baseline",
        slug="issue-2714",
        story_text="body",
        github_issue=2714,
        type="bug",
        fix_ready=True,
        investigation_ready=True,
    )
    result = _result(success=False, phase=Phase.ESCALATE, message="Gate still fails")

    with (
        patch("theforge.cli.baseline_fix.load_config", return_value=config),
        patch(
            "theforge.cli.baseline_fix._create_issue",
            return_value=(2714, "https://github.com/acme/repo/issues/2714"),
        ),
        patch("theforge.cli.baseline_fix.GitHubIssueSource.fetch", return_value=task),
        patch("theforge.cli.baseline_fix.run_task", return_value=result),
        patch(
            "theforge.cli.baseline_fix._write_audit",
            return_value=tmp_path / ".forge" / "audits" / "forge_audit.yaml",
        ),
        patch("theforge.cli.baseline_fix.GitHubIssueSource.on_escalate") as mock_escalate,
    ):
        rc = cmd_baseline_fix(_make_args(tmp_path, sprint_audit=str(audit_path)))

    assert rc == 1
    mock_escalate.assert_called_once_with(task, result.state, config)
    err = capsys.readouterr().err
    assert f"Baseline evidence: {evidence_path}" in err
    assert f"Baseline worktree: {worktree}" in err


def test_cmd_baseline_fix_refuses_ambiguous_latest_audit(tmp_path: Path, capsys) -> None:
    config = _make_config(tmp_path, on_approve="merge")
    first_audit, _worktree, _evidence_path = _write_sprint_audit(tmp_path, run_id="first")
    second_audit, _worktree2, _evidence_path2 = _write_sprint_audit(tmp_path, run_id="second")
    shared_time = 1_700_000_000
    os.utime(first_audit, (shared_time, shared_time))
    os.utime(second_audit, (shared_time, shared_time))

    with patch("theforge.cli.baseline_fix.load_config", return_value=config):
        rc = cmd_baseline_fix(_make_args(tmp_path))

    assert rc == 1
    assert "latest sprint audit is ambiguous" in capsys.readouterr().err
