"""Tests for sprint DAG satisfied-dependency handling.

Covers the case where a depends_on slug references a story already merged to
main (not present in the current sprint manifest).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from theforge.sprint.dag import StoryDAG, _is_branch_merged, build_dag
from theforge.task import TaskStory


def _make_story(slug: str, depends_on: list[str] | None = None) -> TaskStory:
    return TaskStory(
        name=slug,
        slug=slug,
        story_path=f"specs/{slug}.md",
        depends_on=depends_on or [],
    )


# ── build_dag: unknown slug handling ──────────────────────────────────


def test_build_dag_unknown_dep_raises() -> None:
    """A depends_on slug not in manifest and not in satisfied raises ValueError."""
    stories = [_make_story("story-b", depends_on=["story-a"])]
    with pytest.raises(ValueError, match="unknown slug"):
        build_dag(stories)


def test_build_dag_dep_not_in_manifest_but_satisfied_no_error() -> None:
    """A depends_on slug not in manifest but in satisfied does not raise."""
    stories = [_make_story("story-b", depends_on=["story-a"])]
    dag = build_dag(stories, satisfied={"story-a"})
    assert dag is not None


def test_build_dag_satisfied_returns_story_dag() -> None:
    """build_dag with satisfied dep returns a proper StoryDAG instance."""
    stories = [_make_story("story-b", depends_on=["story-a"])]
    dag = build_dag(stories, satisfied={"story-a"})
    assert isinstance(dag, StoryDAG)


def test_build_dag_unknown_dep_not_in_satisfied_raises() -> None:
    """A dep not in manifest and not in satisfied still raises even when satisfied is non-empty."""
    stories = [_make_story("story-c", depends_on=["story-unknown"])]
    with pytest.raises(ValueError, match="unknown slug"):
        build_dag(stories, satisfied={"story-a"})


# ── StoryDAG.ready(): satisfied deps unlock immediately ───────────────


def test_story_dag_ready_with_satisfied_dep() -> None:
    """A story whose dep is in the satisfied set is immediately ready."""
    stories = [_make_story("story-b", depends_on=["story-a"])]
    dag = build_dag(stories, satisfied={"story-a"})
    ready = dag.ready()
    assert len(ready) == 1
    assert ready[0].slug == "story-b"


def test_story_dag_ready_without_satisfied_dep_blocked() -> None:
    """A story whose dep is in the manifest but not yet complete is not ready."""
    stories = [
        _make_story("story-a"),
        _make_story("story-b", depends_on=["story-a"]),
    ]
    dag = build_dag(stories)
    ready_slugs = {t.slug for t in dag.ready()}
    assert "story-b" not in ready_slugs
    assert "story-a" in ready_slugs


def test_story_dag_ready_after_mark_complete() -> None:
    """story-b becomes ready after story-a is marked complete."""
    stories = [
        _make_story("story-a"),
        _make_story("story-b", depends_on=["story-a"]),
    ]
    dag = build_dag(stories)
    dag.mark_complete("story-a")
    ready_slugs = {t.slug for t in dag.ready()}
    assert "story-b" in ready_slugs


# ── Circular dependency detection with satisfied slugs present ────────


def test_circular_dep_detected_with_satisfied_present() -> None:
    """Circular dep detection still works when satisfied slugs are provided."""
    stories = [
        _make_story("story-a", depends_on=["story-b"]),
        _make_story("story-b", depends_on=["story-a"]),
    ]
    with pytest.raises(ValueError, match="Circular dependency"):
        build_dag(stories, satisfied={"story-external"})


def test_satisfied_slugs_not_in_cycle_detection() -> None:
    """Satisfied slugs are external and do not participate in cycle detection."""
    # story-b depends on story-a (satisfied) and story-c (in manifest)
    # story-c has no deps — no cycle should be detected
    stories = [
        _make_story("story-b", depends_on=["story-a", "story-c"]),
        _make_story("story-c"),
    ]
    dag = build_dag(stories, satisfied={"story-a"})
    assert dag is not None


# ── StoryDAG._completed pre-seeding ──────────────────────────────────


def test_story_dag_init_seeds_completed_from_satisfied() -> None:
    """StoryDAG._completed is pre-seeded with the satisfied set."""
    stories = [_make_story("story-b", depends_on=["story-a"])]
    dag = StoryDAG(stories, satisfied={"story-a"})
    assert "story-a" in dag._completed


def test_story_dag_init_no_satisfied_empty_completed() -> None:
    """StoryDAG with no satisfied starts with empty _completed."""
    stories = [_make_story("story-a")]
    dag = StoryDAG(stories)
    assert dag._completed == set()


def test_story_dag_satisfied_not_in_tasks() -> None:
    """Satisfied slugs are not added to _tasks — they are external."""
    stories = [_make_story("story-b", depends_on=["story-a"])]
    dag = StoryDAG(stories, satisfied={"story-a"})
    assert "story-a" not in dag._tasks
    assert "story-b" in dag._tasks


# ── _is_branch_merged: fast-forward merge regression ─────────────────


def _mock_git_ff(cmd: list[str], **kwargs: object) -> MagicMock:
    """Mock git: --is-ancestor returns 0, rev-list count returns 0 (same tip = FF)."""
    m = MagicMock()
    m.returncode = 0
    if "rev-list" in cmd and "--count" in cmd:
        m.stdout = b"0"  # branch and base at same commit after FF
    else:
        m.stdout = b""
    return m


def _mock_git_not_ancestor(cmd: list[str], **kwargs: object) -> MagicMock:
    """Mock git: --is-ancestor returns 1 (branch not ancestor of base)."""
    m = MagicMock()
    if "--is-ancestor" in cmd:
        m.returncode = 1
    else:
        m.returncode = 0
        m.stdout = b""
    return m


def test_is_branch_merged_ff_with_audit_approve(tmp_path: Path) -> None:
    """After FF merge (branch = base tip), audit trail APPROVE → True."""
    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_git_ff),
        patch("theforge.sprint.dag.has_review_approve", return_value=True),
    ):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is True


def test_is_branch_merged_ff_no_audit(tmp_path: Path) -> None:
    """After FF merge (branch = base tip), no audit trail entry → False (fresh branch)."""
    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_git_ff),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
    ):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is False


def test_is_branch_merged_ff_no_slug(tmp_path: Path) -> None:
    """After FF merge with no slug → False (no audit check possible, conservative)."""
    with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_git_ff):
        result = _is_branch_merged("forge/story-a", "main", tmp_path)
    assert result is False


def test_is_branch_merged_regular_merge(tmp_path: Path) -> None:
    """Regular merge commit: base has moved ahead of branch → True without audit."""

    def _mock_regular(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        if "rev-list" in cmd and "--count" in cmd:
            m.stdout = b"3"  # base is 3 commits ahead of branch
        else:
            m.stdout = b""
        return m

    with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_regular):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is True


def test_is_branch_merged_not_ancestor(tmp_path: Path) -> None:
    """Branch not an ancestor of base → False."""
    with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_git_not_ancestor):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is False
