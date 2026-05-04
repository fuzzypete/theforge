from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from theforge.cli.forge_yaml_guard import (
    ForgeYamlGuardResult,
    _changed_top_level_keys,
    _issue_number_from_branch,
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
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
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
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
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
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
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
        ("---\ngithub_issue: 1001\nallow_mutate_forge_yaml: true\n---\n# Config story\n"),
        encoding="utf-8",
    )

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
            MagicMock(returncode=0, stdout="forge.yaml\n", stderr=""),
            MagicMock(returncode=0, stdout="workspace:\n  auto_push: true\n", stderr=""),
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    assert result.ok is True
    assert result.override_active is True
    assert result.violating_keys == ("workspace",)


def test_evaluate_forge_yaml_guard_honors_slug_matched_local_story_override(
    tmp_path: Path,
) -> None:
    (tmp_path / "forge.yaml").write_text("workspace:\n  auto_push: false\n", encoding="utf-8")
    story = tmp_path / "stories" / "backlog" / "config-story.md"
    story.parent.mkdir(parents=True)
    story.write_text(
        ("---\nslug: config-story\nallow_mutate_forge_yaml: true\n---\n# Config story\n"),
        encoding="utf-8",
    )

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="feat/config-story\n", stderr=""),
            MagicMock(returncode=0, stdout="forge.yaml\n", stderr=""),
            MagicMock(returncode=0, stdout="workspace:\n  auto_push: true\n", stderr=""),
            MagicMock(returncode=0, stdout="feat/config-story\n", stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    assert result.ok is True
    assert result.override_active is True
    assert result.violating_keys == ("workspace",)


def test_evaluate_forge_yaml_guard_passes_for_v010_optin_keys(tmp_path: Path) -> None:
    """All v0.10.0 opt-in feature blocks must be operator-mutable without override."""
    current_yaml = (
        "intake:\n  grooming: true\n"
        "conventions_advisory:\n  enabled: true\n"
        "diagnose:\n  enabled: true\n"
    )
    (tmp_path / "forge.yaml").write_text(current_yaml, encoding="utf-8")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
            MagicMock(returncode=0, stdout="forge.yaml\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),  # base has no forge.yaml content
            MagicMock(returncode=0, stdout="feat/issue-1001\n", stderr=""),
            MagicMock(returncode=0, stdout=json.dumps({"body": ""}), stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    assert result.ok is True
    assert result.violating_keys == ()
    assert set(result.changed_keys) == {"intake", "conventions_advisory", "diagnose"}


def test_evaluate_forge_yaml_guard_skips_in_detached_head_worktree(tmp_path: Path) -> None:
    """Baseline gate runs in a detached worktree at the merge-base SHA — there is
    no story branch in scope and the per-story mutation guard must not apply.
    Regression test for #1375."""
    (tmp_path / "forge.yaml").write_text(
        "conventions:\n  one: a\nretry:\n  attempts: 2\n", encoding="utf-8"
    )

    with patch("subprocess.run") as mock_run:
        # `git rev-parse --abbrev-ref HEAD` returns the literal "HEAD" when
        # detached. The guard must short-circuit before any diff happens.
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="HEAD\n", stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    assert result.ok is True
    assert result.changed_keys == ()
    assert result.violating_keys == ()
    # Only the branch-detection call should have run; no diff was attempted.
    assert mock_run.call_count == 1


def test_evaluate_forge_yaml_guard_skips_when_current_branch_is_base(tmp_path: Path) -> None:
    """Running `make gate` directly on a release branch (current branch ==
    configured base) is not a story-mutation context. Regression test for #1375."""
    (tmp_path / "forge.yaml").write_text(
        "conventions:\n  one: a\nretry:\n  attempts: 2\n", encoding="utf-8"
    )

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="release/v0.10\n", stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="release/v0.10")

    assert result.ok is True
    assert result.changed_keys == ()
    assert result.violating_keys == ()
    assert mock_run.call_count == 1


def test_evaluate_forge_yaml_guard_skips_on_release_branch_with_main_as_base(
    tmp_path: Path,
) -> None:
    """cut-rc.sh runs `make gate` on `release/v0.10` while `forge.yaml`
    configures `main` as base. Without story context (no issue-N branch,
    no matching local story file), the guard must short-circuit even
    when current_branch != base_branch. Regression test for #1377."""
    (tmp_path / "forge.yaml").write_text(
        "conventions:\n  one: a\nretry:\n  attempts: 2\n", encoding="utf-8"
    )

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="release/v0.10\n", stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    assert result.ok is True
    assert result.changed_keys == ()
    assert result.violating_keys == ()
    # No diff attempted; the short-circuit fired on the absence of story context.
    assert mock_run.call_count == 1


def test_issue_number_from_branch_recognizes_descriptive_suffix() -> None:
    """Manual story branches commonly use `issue-N-summary` or `issue-N_summary`
    (e.g. `fix/issue-1377-guard-non-story-branches`). Both must resolve to the
    issue number so the mutation guard treats them as story context."""
    assert _issue_number_from_branch("feat/issue-283") == 283
    assert _issue_number_from_branch("fix/issue-1377-guard-non-story-branches") == 1377
    assert _issue_number_from_branch("fix/issue-42_describe_thing") == 42
    assert _issue_number_from_branch("issue-7") == 7
    # Non-story branches still return None.
    assert _issue_number_from_branch("release/v0.10") is None
    assert _issue_number_from_branch("main") is None
    assert _issue_number_from_branch("HEAD") is None
    # `issue-` followed by non-digit is not a story branch.
    assert _issue_number_from_branch("feat/issue-abc") is None
    # Digits must be followed by a separator or end-of-component.
    assert _issue_number_from_branch("feat/issue-12foo") is None


def test_evaluate_forge_yaml_guard_applies_on_descriptive_issue_branch(
    tmp_path: Path,
) -> None:
    """A manual story branch like `fix/issue-1377-guard-non-story-branches`
    encodes story context and the guard must apply (not silently skip).
    Regression test for the descriptive-suffix gap caught in PR #1378 review."""
    (tmp_path / "forge.yaml").write_text(
        "validation:\n  gate_command: make gate\n", encoding="utf-8"
    )

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="fix/issue-1377-guard-non-story-branches\n", stderr=""),
            MagicMock(returncode=0, stdout="forge.yaml\n", stderr=""),
            MagicMock(returncode=0, stdout="models:\n  - claude/sonnet\n", stderr=""),
            MagicMock(returncode=0, stdout="fix/issue-1377-guard-non-story-branches\n", stderr=""),
            MagicMock(returncode=0, stdout=json.dumps({"body": ""}), stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    # Branch encodes story context, so the guard runs and rejects the
    # non-allowlisted `validation` change.
    assert result.ok is False
    assert result.violating_keys == ("validation",)


def test_evaluate_forge_yaml_guard_skips_on_adhoc_non_story_branch(tmp_path: Path) -> None:
    """Ad-hoc maintenance branches (no issue-N token, no matching local
    story file) are not story-mutation contexts. Regression test for #1377."""
    (tmp_path / "forge.yaml").write_text(
        "validation:\n  gate_command: make gate\n", encoding="utf-8"
    )

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="chore/cleanup\n", stderr=""),
        ]

        result = evaluate_forge_yaml_guard(tmp_path, base_branch="main")

    assert result.ok is True
    assert mock_run.call_count == 1


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
    assert "local story file frontmatter" in err
