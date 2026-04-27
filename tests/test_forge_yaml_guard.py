from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from theforge.cli.forge_yaml_guard import (
    ForgeYamlGuardResult,
    _changed_top_level_keys,
    cmd_check_story_config,
    evaluate_forge_yaml_guard,
)
from theforge.cli.shared import _build_task
from theforge.sprint.manifest import _build_task_from_story
from theforge.sprint.sources import GitHubIssueSource


def test_build_task_parses_forge_yaml_override_frontmatter(tmp_path: Path) -> None:
    story = tmp_path / "story.md"
    story.write_text("---\nallow_mutate_forge_yaml: true\n---\n", encoding="utf-8")

    task = _build_task(story)

    assert task.allow_mutate_forge_yaml is True


def test_build_task_from_story_parses_forge_yaml_override_frontmatter(tmp_path: Path) -> None:
    story = tmp_path / "story.md"
    story.write_text("---\nallow_mutate_forge_yaml: true\n---\n", encoding="utf-8")

    task = _build_task_from_story(story)

    assert task.allow_mutate_forge_yaml is True


def test_github_issue_source_parses_forge_yaml_override_metadata(tmp_path: Path) -> None:
    issue_data = json.dumps(
        {
            "title": "Config story",
            "body": "---\nallow_mutate_forge_yaml: true\n---\n\nBody",
            "state": "OPEN",
        }
    )
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=issue_data, stderr=""),
            MagicMock(returncode=1, stdout="", stderr="preview unavailable"),
        ]

        task = GitHubIssueSource().fetch("42", tmp_path)

    assert task.allow_mutate_forge_yaml is True


def test_changed_top_level_keys_uses_top_level_sections() -> None:
    base = {"models": ["a"], "validation": {"gate_command": "make gate"}}
    current = {"models": ["b"], "validation": {"gate_command": "make gate"}, "retry": {"x": 1}}

    changed = _changed_top_level_keys(base, current)

    assert changed == ("models", "retry")


def test_evaluate_forge_yaml_guard_passes_for_allowed_keys(tmp_path: Path) -> None:
    (tmp_path / "forge.yaml").write_text("models:\n  - openai/gpt-5.4\n", encoding="utf-8")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="forge.yaml\n", stderr=""),
            MagicMock(returncode=0, stdout="models:\n  - claude/sonnet\n", stderr=""),
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
            MagicMock(returncode=0, stdout=json.dumps({"body": ""}), stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    assert result.ok is True
    assert result.changed_keys == ("models",)
    assert result.violating_keys == ()


def test_evaluate_forge_yaml_guard_fails_for_non_allowlisted_keys(tmp_path: Path) -> None:
    (tmp_path / "forge.yaml").write_text(
        "models:\n  - claude/sonnet\nvalidation:\n  gate_command: make gate\n",
        encoding="utf-8",
    )

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="forge.yaml\n", stderr=""),
            MagicMock(returncode=0, stdout="models:\n  - claude/sonnet\n", stderr=""),
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
            MagicMock(returncode=0, stdout=json.dumps({"body": ""}), stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    assert result.ok is False
    assert result.changed_keys == ("validation",)
    assert result.violating_keys == ("validation",)


def test_evaluate_forge_yaml_guard_honors_issue_override(tmp_path: Path) -> None:
    (tmp_path / "forge.yaml").write_text("workspace:\n  auto_push: false\n", encoding="utf-8")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="forge.yaml\n", stderr=""),
            MagicMock(returncode=0, stdout="workspace:\n  auto_push: true\n", stderr=""),
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
            MagicMock(
                returncode=0,
                stdout=json.dumps({"body": "---\nallow_mutate_forge_yaml: true\n---\n"}),
                stderr="",
            ),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    assert result.ok is True
    assert result.override_active is True
    assert result.violating_keys == ("workspace",)


def test_evaluate_forge_yaml_guard_honors_local_story_override(tmp_path: Path) -> None:
    (tmp_path / "forge.yaml").write_text("workspace:\n  auto_push: false\n", encoding="utf-8")
    story = tmp_path / "stories" / "backlog" / "config-story.md"
    story.parent.mkdir(parents=True)
    story.write_text(
        (
            "---\n"
            "github_issue: 1001\n"
            "allow_mutate_forge_yaml: true\n"
            "---\n"
            "# Config story\n"
        ),
        encoding="utf-8",
    )

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="forge.yaml\n", stderr=""),
            MagicMock(returncode=0, stdout="workspace:\n  auto_push: true\n", stderr=""),
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    assert result.ok is True
    assert result.override_active is True
    assert result.violating_keys == ("workspace",)


def test_cmd_check_story_config_prints_violating_keys(capsys, tmp_path: Path) -> None:
    config_path = tmp_path / "forge.yaml"
    config_path.write_text("project: test\n", encoding="utf-8")
    config = SimpleNamespace(project_root=tmp_path, workspace=SimpleNamespace(base_branch="main"))

    with (
        patch("theforge.cli.forge_yaml_guard._find_config", return_value=config_path),
        patch("theforge.cli.forge_yaml_guard.load_config", return_value=config),
        patch(
            "theforge.cli.forge_yaml_guard.evaluate_forge_yaml_guard",
            return_value=ForgeYamlGuardResult(
                ok=False,
                violating_keys=("validation", "workspace"),
            ),
        ),
    ):
        rc = cmd_check_story_config(SimpleNamespace(config=None))

    assert rc == 1
    err = capsys.readouterr().err
    assert "validation, workspace" in err
    assert "allow_mutate_forge_yaml: true" in err
