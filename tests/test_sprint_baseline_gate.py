from __future__ import annotations

import os
import subprocess
from dataclasses import replace
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
from theforge.sprint.runner import _run_baseline_gate, run_sprint
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
        validation=replace(
            DEFAULT_VALIDATION,
            gate_command=(
                'python -c "import pathlib; '
                "print(pathlib.Path('baseline.txt').read_text().strip())\""
            ),
        ),
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


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_all(cwd: Path, message: str) -> str:
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


def _init_repo(tmp_path: Path) -> tuple[ForgeConfig, ResolvedSprint, str]:
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "baseline.txt").write_text("BASELINE\n", encoding="utf-8")
    base_commit = _commit_all(tmp_path, "base")
    _git(tmp_path, "checkout", "-b", "feat/test")
    (tmp_path / "baseline.txt").write_text("FEATURE\n", encoding="utf-8")
    _commit_all(tmp_path, "feature")
    return config, resolved, base_commit


def test_baseline_gate_uses_temp_worktree_and_restores_branch_state(
    tmp_path: Path,
) -> None:
    config, resolved, base_commit = _init_repo(tmp_path)
    original_branch = _git(tmp_path, "branch", "--show-current")
    original_head = _git(tmp_path, "rev-parse", "HEAD")

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["merge_base"] == base_commit
    assert baseline["exit_code"] == 0
    assert "BASELINE" in str(baseline.get("output_tail", ""))
    assert _git(tmp_path, "branch", "--show-current") == original_branch
    assert _git(tmp_path, "rev-parse", "HEAD") == original_head
    assert (tmp_path / "baseline.txt").read_text(encoding="utf-8") == "FEATURE\n"
    worktrees = _git(tmp_path, "worktree", "list")
    assert "forge-baseline-" not in worktrees
    forge_entries = [path.name for path in (tmp_path / ".forge").iterdir()]
    assert not any(name.startswith("forge-baseline-") for name in forge_entries)


def test_baseline_gate_preserves_actual_gate_exit_code(tmp_path: Path) -> None:
    config, resolved, base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command="python -c 'import sys; print(\"boom\"); sys.exit(2)'",
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is False
    assert baseline["status"] == "fail"
    assert baseline["merge_base"] == base_commit
    assert baseline["exit_code"] == 2
    assert "boom" in str(baseline.get("output_tail", ""))


def test_baseline_gate_substitutes_test_target_placeholder(tmp_path: Path) -> None:
    # A gate_command that uses {test_target}/{slug} (as HDP's did) must not
    # leak the literal placeholder text into the baseline gate's shell
    # command, since the baseline gate has no TaskStory to source them from.
    config, resolved, base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        validation=replace(
            config.validation,
            gate_command="echo {test_target} {slug}",
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["merge_base"] == base_commit
    resolved_cmd = baseline["command"]
    assert "{test_target}" not in resolved_cmd
    assert "{slug}" not in resolved_cmd


def test_baseline_gate_runs_setup_command_before_gate(tmp_path: Path) -> None:
    config, resolved, base_commit = _init_repo(tmp_path)
    # setup_command bootstraps a marker the gate depends on; the merge base has
    # no such marker, so the gate can only pass if setup runs first inside the
    # temporary baseline worktree.
    config = replace(
        config,
        workspace=replace(config.workspace, setup_command="echo READY > toolchain.txt"),
        validation=replace(
            config.validation,
            gate_command=(
                'python -c "import pathlib; '
                "print(pathlib.Path('toolchain.txt').read_text().strip())\""
            ),
        ),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["status"] == "pass"
    assert baseline["merge_base"] == base_commit
    assert "READY" in str(baseline.get("output_tail", ""))
    # The setup marker must not leak into the project root checkout.
    assert not (tmp_path / "toolchain.txt").exists()


def test_baseline_gate_reports_setup_command_failure(tmp_path: Path) -> None:
    config, resolved, base_commit = _init_repo(tmp_path)
    config = replace(
        config,
        workspace=replace(config.workspace, setup_command="exit 3"),
    )

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is False
    assert baseline["status"] == "error"
    assert baseline["merge_base"] == base_commit
    assert "workspace setup command failed" in str(baseline["message"]).lower()


def test_baseline_gate_accepts_symlinked_project_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    config, resolved, _base_commit = _init_repo(real_root)
    symlink_root = tmp_path / "linked-root"
    os.symlink(real_root, symlink_root)
    config = replace(config, project_root=symlink_root)

    baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is True
    assert baseline["status"] == "pass"


def test_baseline_gate_fail_aborts_before_any_agent_runner(tmp_path: Path) -> None:
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


def test_baseline_pass_proceeds_to_normal_sprint_flow(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    resolved = _make_resolved(tmp_path)

    with (
        patch(
            "theforge.sprint.runner._run_baseline_gate",
            return_value={"passed": True, "message": "ok"},
        ),
        patch(
            "theforge.sprint.runner.run_task",
            return_value=_fake_result(),
        ) as mock_run_task,
        patch("theforge.sprint.runner._write_sprint_audit"),
        patch("theforge.sprint.runner._write_sprint_summary"),
    ):
        result = run_sprint(config, resolved)

    assert result.specs_succeeded == 1
    assert mock_run_task.called
