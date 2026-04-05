from pathlib import Path

from theforge.coordinator.state import CoordinatorState
from theforge.sprint.collision import compute_synthetic_edges, inject_synthetic_deps
from theforge.sprint.dag import StoryTriage
from theforge.sprint.runner import _register_resumed_story_footprints
from theforge.task import TaskStory


def _task(slug: str, issue: int | None, depends_on: list[str] | None = None) -> TaskStory:
    return TaskStory(
        name=slug,
        slug=slug,
        story_path=Path(f"stories/{slug}.md"),
        depends_on=depends_on or [],
        github_issue=issue,
    )


def test_compute_synthetic_edges_example_chain() -> None:
    tasks = [
        _task("story-12", 12),
        _task("story-15", 15),
        _task("story-25", 25),
        _task("story-30", 30),
    ]
    states = {
        "story-12": CoordinatorState(preflight_likely_files=["a.py", "b.py"]),
        "story-15": CoordinatorState(preflight_likely_files=["a.py"]),
        "story-25": CoordinatorState(preflight_likely_files=["b.py"]),
        "story-30": CoordinatorState(preflight_likely_files=["a.py"]),
    }

    assert compute_synthetic_edges(states, tasks) == {
        "story-15": ["story-12"],
        "story-25": ["story-12"],
        "story-30": ["story-15"],
    }


def test_inject_synthetic_deps_merges_without_duplicates() -> None:
    tasks = [
        _task("story-12", 12),
        _task("story-15", 15, depends_on=["base", "story-12"]),
    ]

    augmented = inject_synthetic_deps(tasks, {"story-15": ["story-12", "extra"]})

    assert augmented[1].depends_on == ["base", "extra", "story-12"]


def test_compute_synthetic_edges_ignores_missing_likely_files() -> None:
    tasks = [_task("story-12", 12), _task("story-15", 15)]
    states = {
        "story-12": CoordinatorState(preflight_likely_files=[]),
        "story-15": CoordinatorState(preflight_likely_files=["a.py"]),
    }

    assert compute_synthetic_edges(states, tasks) == {}


def test_compute_synthetic_edges_falls_back_to_slug_order_when_issue_missing() -> None:
    tasks = [_task("b-story", None), _task("a-story", None)]
    states = {
        "b-story": CoordinatorState(preflight_likely_files=["shared.py"]),
        "a-story": CoordinatorState(preflight_likely_files=["shared.py"]),
    }

    assert compute_synthetic_edges(states, tasks) == {"b-story": ["a-story"]}


def test_register_resumed_story_footprints_reads_plan_md(tmp_path: Path) -> None:
    slug = "story-12"
    workspace_path = tmp_path / slug
    plan_dir = workspace_path / ".forge"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text(
        """---
plan:
  approach: Resume implementation
  steps:
    - id: 1
      description: Update API
      files:
        - src/foo.py
        - tests/test_foo.py
      action: modify
      details: Continue the resumed story
""",
        encoding="utf-8",
    )

    preflight_states = {slug: CoordinatorState()}
    triages = {
        slug: StoryTriage(
            story_path="stories/story-12.md",
            action="dev",
            reason="resume",
            worktree_path=workspace_path,
            slug=slug,
        )
    }

    _register_resumed_story_footprints(triages, preflight_states)

    assert preflight_states[slug].preflight_likely_files == ["src/foo.py", "tests/test_foo.py"]
