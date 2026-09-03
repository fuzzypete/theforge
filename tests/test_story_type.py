"""Tests for the structured ``story.type`` field added in issue #1142."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from coord_test_helpers import patch_gate_shell

from theforge.shape_check import Shape, check
from theforge.shape_check.heuristics import check_missing_type
from theforge.sprint.manifest import _build_task_from_story
from theforge.sprint.sources import GitHubIssueSource, _derive_type_from_labels
from theforge.task import RECOGNIZED_STORY_TYPES, StoryTypeError, parse_story_frontmatter


class TestFrontmatterType:
    def test_recognized_type_set(self) -> None:
        assert RECOGNIZED_STORY_TYPES == {"bug", "enhancement", "epic", "spike", "task"}

    def test_parse_valid_type(self, tmp_path: Path) -> None:
        story = tmp_path / "s.md"
        story.write_text("---\nname: X\nslug: x\ntype: bug\n---\n# body\n")
        fm = parse_story_frontmatter(story)
        assert fm["type"] == "bug"

    def test_parse_lowercases_type(self, tmp_path: Path) -> None:
        story = tmp_path / "s.md"
        story.write_text("---\nname: X\nslug: x\ntype: Enhancement\n---\n# body\n")
        fm = parse_story_frontmatter(story)
        assert fm["type"] == "enhancement"

    def test_unknown_type_rejected_with_clear_error(self, tmp_path: Path) -> None:
        story = tmp_path / "s.md"
        story.write_text("---\nname: X\nslug: x\ntype: feature-ish\n---\n# body\n")
        with pytest.raises(StoryTypeError) as exc:
            parse_story_frontmatter(story)
        assert "feature-ish" in str(exc.value)
        assert "bug, enhancement, epic, spike, task" in str(exc.value)

    def test_non_string_type_rejected(self, tmp_path: Path) -> None:
        story = tmp_path / "s.md"
        story.write_text("---\nname: X\nslug: x\ntype: 7\n---\n# body\n")
        with pytest.raises(StoryTypeError):
            parse_story_frontmatter(story)


class TestFileSourceMigrationWarning:
    def test_missing_type_surfaces_warning(self, tmp_path: Path) -> None:
        story = tmp_path / "legacy.md"
        story.write_text("---\nname: legacy\nslug: legacy\n---\n# body\n")
        task = _build_task_from_story(story)
        assert task.type is None
        assert task.type_warnings, "expected a migration warning"
        assert "type" in task.type_warnings[0]

    def test_present_type_no_warning(self, tmp_path: Path) -> None:
        story = tmp_path / "good.md"
        story.write_text("---\nname: good\nslug: good\ntype: enhancement\n---\n# body\n")
        task = _build_task_from_story(story)
        assert task.type == "enhancement"
        assert task.type_warnings == []

    def test_malformed_frontmatter_surfaces_dependency_warning(self, tmp_path: Path) -> None:
        story = tmp_path / "broken.md"
        story.write_text(
            "---\nname: broken\nslug: broken\ndepends_on: story-a\nbroken: [\n---\n# body\n",
            encoding="utf-8",
        )

        task = _build_task_from_story(story)

        assert task.depends_on == []
        assert task.dependency_warnings
        assert "malformed YAML frontmatter" in task.dependency_warnings[0]
        assert "dependency declarations inside it" in task.dependency_warnings[0]


class TestGitHubLabelTypeMapping:
    def test_bug_label_yields_bug_type(self) -> None:
        story_type, warnings = _derive_type_from_labels(["bug", "area:cli"], 1)
        assert story_type == "bug"
        assert warnings == []

    def test_enhancement_label_yields_enhancement_type(self) -> None:
        story_type, warnings = _derive_type_from_labels(["enhancement"], 1)
        assert story_type == "enhancement"

    def test_no_recognized_label_yields_warning(self) -> None:
        story_type, warnings = _derive_type_from_labels(["area:cli"], 5)
        assert story_type is None
        assert warnings and "no recognized type label" in warnings[0]

    def test_multiple_type_labels_raises(self) -> None:
        with pytest.raises(StoryTypeError) as exc:
            _derive_type_from_labels(["bug", "enhancement"], 9)
        assert "multiple type labels" in str(exc.value)

    def test_fetch_propagates_label_to_story(self, tmp_path: Path) -> None:
        issue_data = json.dumps(
            {
                "title": "Fix it",
                "body": "## Details\n",
                "state": "OPEN",
                "labels": [{"name": "bug"}],
            }
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=0, stdout="[]", stderr=""),
            ]
            task = GitHubIssueSource().fetch("42", tmp_path)
        assert task.type == "bug"
        assert task.type_warnings == []

    def test_fetch_without_type_label_emits_warning(self, tmp_path: Path) -> None:
        issue_data = json.dumps(
            {
                "title": "Fix it",
                "body": "## Details\n",
                "state": "OPEN",
                "labels": [{"name": "area:cli"}],
            }
        )
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=issue_data, stderr=""),
                MagicMock(returncode=0, stdout="[]", stderr=""),
            ]
            task = GitHubIssueSource().fetch("42", tmp_path)
        assert task.type is None
        assert task.type_warnings


class TestShapeGateMissingType:
    def test_missing_type_blocks_with_clear_message(self) -> None:
        reason = check_missing_type("Title", "## What\nDo a thing.", [])
        assert reason is not None
        assert reason.code == "missing_type"
        assert "no recognized type label" in reason.detail

    def test_recognized_label_passes(self) -> None:
        assert check_missing_type("Title", "## body", ["enhancement"]) is None
        assert check_missing_type("Title", "## body", ["bug"]) is None

    def test_multiple_type_labels_blocks(self) -> None:
        reason = check_missing_type("Title", "## body", ["bug", "enhancement"])
        assert reason is not None
        assert "multiple type labels" in reason.detail

    def test_check_pipeline_flags_missing_type(self) -> None:
        body = (
            "## What\nDo a thing.\n\n## Acceptance Criteria\n"
            "- The command returns 0.\n"
            "## Example\n```\nfoo\nbar\nbaz\nqux\nquux\n```\n"
        )
        result = check("title", body, [])
        assert result.shape is Shape.NEEDS_GROOMING
        assert any(r.code == "missing_type" for r in result.reasons)

    def test_check_pipeline_passes_with_type(self) -> None:
        body = (
            "## What\nDo a thing.\n\n## Acceptance Criteria\n"
            "- The command returns 0 on success.\n"
            "## Example\n```\nfoo\nbar\nbaz\nqux\nquux\n```\n"
        )
        result = check("title", body, ["enhancement"])
        assert result.shape is Shape.RUNNABLE


class TestPreflightFlowReadsStructuredType:
    """Seam test: a task with structured type=bug must override AI inference."""

    def test_bug_type_overrides_agent_feature_inference(self, tmp_path: Path) -> None:
        from dataclasses import replace as dc_replace
        from unittest.mock import patch

        from tests.coord_test_helpers import (  # noqa: PLC0415
            PREFLIGHT_PROCEED,
            _make_agent_result,
            _make_task,
            _shell_with_gate,
        )
        from tests.test_coord_logging import TestProjectLocalLogDir  # noqa: PLC0415
        from theforge.coordinator.preflight_flow import _run_preflight_phase
        from theforge.coordinator.state import CoordinatorState, Phase

        # Agent output explicitly classifies as feature.
        feature_output = PREFLIGHT_PROCEED.replace(
            "verdict: PROCEED",
            "verdict: PROCEED\nwork_type: feature",
        )
        preflight_result = _make_agent_result(success=True, output=feature_output, cost_usd=0.05)

        config = TestProjectLocalLogDir()._make_config(tmp_path)
        base_task = _make_task(tmp_path)
        task = dc_replace(base_task, type="bug")
        workspace = tmp_path / task.slug
        workspace.mkdir()
        state = CoordinatorState(
            log_dir=tmp_path / ".forge" / "logs" / task.slug, phase=Phase.PREFLIGHT
        )

        with (
            patch(
                "theforge.coordinator.preflight_flow.run_agent",
                return_value=preflight_result,
            ),
            patch("theforge.coordinator.preflight_flow._is_branch_merged", return_value=True),
            patch_gate_shell(
                side_effect=_shell_with_gate(workspace, "PASS"),
            ),
        ):
            _run_preflight_phase(
                state,
                config,
                task,
                "story",
                workspace,
                f"forge/{task.slug}",
                notify=False,
                logger=None,
                task_start=0.0,
                state_update_fn=None,
                stop_phase=None,
            )

        assert state.preflight_work_type == "bug", (
            "structured task.type=bug must override agent's feature classification"
        )
