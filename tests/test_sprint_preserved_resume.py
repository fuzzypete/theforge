"""Tests for preserved-story operator guidance."""

from __future__ import annotations

from pathlib import Path

from theforge.sprint.preserved_resume import (
    PRESERVED_REVIEW_COMMAND,
    preserved_review_command,
)

ROOT = Path(__file__).resolve().parents[1]


def test_preserved_review_command_names_issue_backed_story() -> None:
    assert (
        preserved_review_command(path="Issue #2475", slug="issue-2475")
        == "forge review --issue 2475"
    )


def test_preserved_review_command_names_file_backed_story_path() -> None:
    assert (
        preserved_review_command(path="stories/my story.md", slug="my-story")
        == "forge review 'stories/my story.md'"
    )


def test_preserved_review_command_uses_file_backed_canonical_ref_when_path_missing() -> None:
    assert preserved_review_command(canonical_ref="stories/my story.md", slug="my-story") == (
        "forge review 'stories/my story.md'"
    )


def test_controller_runbook_preserved_section_matches_runtime_guidance() -> None:
    text = (ROOT / "docs" / "guides" / "controller-runbook.md").read_text()
    preserved_section = text.split("### PRESERVED", 1)[1].split("### Auth readiness gate", 1)[0]

    assert (
        f"resolve with `{preserved_review_command(path='Issue #2475', slug='issue-2475')}`"
        in preserved_section
    )
    assert f"resolve with `{PRESERVED_REVIEW_COMMAND}`" in preserved_section
    assert "substitutes the concrete path when it already knows it" in preserved_section
    assert "forge run --resume <story-file>" not in preserved_section
