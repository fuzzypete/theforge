"""Tests for sprint DAG satisfied-dependency handling.

Covers the case where a depends_on slug references a story already merged to
main (not present in the current sprint manifest).
"""

from __future__ import annotations

import pytest

from theforge.sprint.dag import StoryDAG, build_dag
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
