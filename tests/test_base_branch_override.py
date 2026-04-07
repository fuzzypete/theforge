from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from coord_test_helpers import _make_config, _make_task

from theforge.cli.run import cmd_run
from theforge.cli.sprint import cmd_sprint
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.coordinator.workspace import _create_workspace


def _stub_result() -> CoordinatorResult:
    return CoordinatorResult(
        success=True, phase=Phase.DONE, state=CoordinatorState(), message="ok"
    )


def test_create_workspace_substitutes_base_branch(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config = config.__class__(
        **{
            **config.__dict__,
            "workspace": config.workspace.__class__(
                **{
                    **config.workspace.__dict__,
                    "create_command": (
                        "git worktree add .forge/worktrees/{slug} -b forge/{slug} {base_branch}"
                    ),
                    "path_pattern": ".forge/worktrees/{slug}",
                }
            ),
        }
    )
    task = _make_task(tmp_path)

    commands: list[str] = []

    def shell_side_effect(cmd, cwd, **kwargs):
        commands.append(cmd)
        if "rev-parse --abbrev-ref HEAD" in cmd:
            return True, config.workspace.base_branch
        if "pull --ff-only" in cmd:
            return True, ""
        if "git worktree add" in cmd:
            (tmp_path / ".forge" / "worktrees" / task.slug).mkdir(parents=True, exist_ok=True)
            return True, ""
        return True, ""

    with (
        patch("theforge.coordinator.workspace._cu._run_shell", side_effect=shell_side_effect),
        patch("theforge.coordinator.workspace._cu._log"),
    ):
        workspace_path, branch_name, err = _create_workspace(config, task)

    assert err is None
    assert workspace_path is not None
    assert branch_name == f"forge/{task.slug}"
    assert any(cmd.endswith(" main") for cmd in commands if "git worktree add" in cmd)


def test_cmd_run_base_branch_override_updates_config(tmp_path: Path) -> None:
    story = tmp_path / "story.md"
    story.write_text("# Story\n", encoding="utf-8")
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    config = _make_config(tmp_path)
    args = argparse.Namespace(
        story=str(story),
        slug=None,
        config=str(forge_yaml),
        base_branch="release/v0.4",
        plan=None,
        from_phase=None,
        until=None,
        reviewers=None,
        max_cycles=None,
        resume=False,
        dev_model=None,
        plan_model=None,
        dry_run=False,
        interactive=False,
        auto_merge=False,
        verbose=False,
        no_notify=True,
        fg=True,
        no_pull=False,
    )

    with (
        patch("theforge.cli.run.load_config", return_value=config),
        patch("theforge.cli.run.run_task", return_value=_stub_result()) as mock_run,
        patch("theforge.cli.run._write_audit", return_value=tmp_path / "audit.json"),
    ):
        rc = cmd_run(args)

    assert rc == 0
    passed_config = mock_run.call_args.args[0]
    assert passed_config.workspace.base_branch == "release/v0.4"


def test_cmd_sprint_base_branch_override_updates_config(tmp_path: Path) -> None:
    manifest = tmp_path / "sprint.yaml"
    manifest.write_text("stories: []\n", encoding="utf-8")
    forge_yaml = tmp_path / "forge.yaml"
    forge_yaml.write_text("project: test\n", encoding="utf-8")
    config = _make_config(tmp_path)
    args = argparse.Namespace(
        manifest=str(manifest),
        config=str(forge_yaml),
        base_branch="release/v0.4",
        fg=True,
        detach=False,
        resume=False,
        milestone=None,
        label=None,
        issues=None,
        budget=None,
        parallel=None,
        name=None,
        dry_run=False,
        auto_merge=False,
        interactive=False,
        verbose=False,
        no_notify=True,
        no_pull=False,
    )

    with (
        patch("theforge.cli.sprint.load_config", return_value=config),
        patch("theforge.cli.sprint.parse_manifest_slugs", return_value=[]),
        patch("theforge.cli.sprint.run_sprint") as mock_run_sprint,
        patch("theforge.cli.sprint.release_story_locks"),
    ):
        mock_run_sprint.return_value = type("Result", (), {"specs_failed": 0})()
        rc = cmd_sprint(args)

    assert rc == 0
    passed_config = mock_run_sprint.call_args.args[0]
    assert passed_config.workspace.base_branch == "release/v0.4"
