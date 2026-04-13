"""Golden story set for preflight model evaluation.

Defines the GoldenStory dataclass and loader. Each entry in the golden set
represents a story with a known expected preflight outcome, drawn from recent
sprint history.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml


@dataclass(frozen=True)
class GoldenStory:
    """A curated story with a known expected preflight outcome."""

    id: str
    title: str
    story_text: str
    expected_verdict: Literal["PROCEED", "ALREADY_DONE", "BLOCKED"]
    # git ref of the codebase state at evaluation time; None = use current HEAD
    repo_snapshot_ref: str | None = None
    # "small" | "medium" | "large" | None when not known
    expected_complexity: str | None = None
    notes: str | None = None


def load_golden_set(path: Path) -> list[GoldenStory]:
    """Load and validate a YAML golden story file.

    Warns when repo_snapshot_ref is None (evaluation may not be reproducible
    after major refactors).
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Golden story file must be a YAML list, got {type(raw).__name__}")

    stories: list[GoldenStory] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(
                f"Each golden story entry must be a mapping, got {type(entry).__name__}"
            )

        story = GoldenStory(
            id=entry["id"],
            title=entry["title"],
            story_text=entry["story_text"],
            expected_verdict=entry["expected_verdict"],
            repo_snapshot_ref=entry.get("repo_snapshot_ref"),
            expected_complexity=entry.get("expected_complexity"),
            notes=entry.get("notes"),
        )

        if story.repo_snapshot_ref is None:
            warnings.warn(
                f"Golden story {story.id!r} has no repo_snapshot_ref — evaluation "
                "reproducibility depends on current HEAD. Update after major refactors.",
                UserWarning,
                stacklevel=2,
            )

        if story.expected_verdict not in ("PROCEED", "ALREADY_DONE", "BLOCKED"):
            raise ValueError(
                f"Story {story.id!r}: expected_verdict must be PROCEED, ALREADY_DONE, "
                f"or BLOCKED; got {story.expected_verdict!r}"
            )

        stories.append(story)

    return stories
