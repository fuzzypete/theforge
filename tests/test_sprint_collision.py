from pathlib import Path

from theforge.coordinator.state import CoordinatorState
from theforge.sprint.collision import (
    compute_bundle_assignments,
    compute_synthetic_edges,
    inject_synthetic_deps,
)
from theforge.sprint.dag import StoryTriage, build_dag
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


def test_inject_synthetic_deps_writes_to_collision_deps() -> None:
    tasks = [
        _task("story-12", 12),
        _task("story-15", 15, depends_on=["base", "story-12"]),
    ]

    augmented = inject_synthetic_deps(tasks, {"story-15": ["story-12", "extra"]})

    # depends_on (hard edges) must be untouched; collision edges land in
    # collision_deps so the DAG can release them on terminal-but-not-merged.
    assert augmented[1].depends_on == ["base", "story-12"]
    assert augmented[1].collision_deps == ["extra", "story-12"]


def test_compute_synthetic_edges_serializes_empty_likely_files_conservatively() -> None:
    """An empty likely_files ([]) is an *unknown* footprint, not proof of no
    collision. The regression from #1771: story-12 emitted [] while story-15
    correctly predicted a shared file; the empty side contributed nothing to the
    intersection so no edge formed and both raced to a merge conflict. Now the
    unknown-footprint story is serialized conservatively against its siblings."""
    tasks = [_task("story-12", 12), _task("story-15", 15)]
    states = {
        "story-12": CoordinatorState(preflight_likely_files=[]),
        "story-15": CoordinatorState(preflight_likely_files=["a.py"]),
    }

    assert compute_synthetic_edges(states, tasks) == {"story-15": ["story-12"]}


def test_compute_synthetic_edges_serializes_none_likely_files_conservatively() -> None:
    """A None footprint (preflight failed/excluded) is also unknown and must be
    serialized conservatively rather than silently granted zero protection."""
    tasks = [_task("story-12", 12), _task("story-15", 15)]
    states = {
        "story-12": CoordinatorState(preflight_likely_files=None),
        "story-15": CoordinatorState(preflight_likely_files=["a.py"]),
    }

    assert compute_synthetic_edges(states, tasks) == {"story-15": ["story-12"]}


def test_compute_synthetic_edges_unknown_footprint_does_not_serialize_disjoint_siblings() -> None:
    """The unknown story pins itself against every sibling, but known, disjoint
    siblings must still run in parallel relative to each other (no blanket
    over-serialization)."""
    tasks = [_task("story-12", 12), _task("story-15", 15), _task("story-25", 25)]
    states = {
        # Unknown footprint — must serialize against both others.
        "story-12": CoordinatorState(preflight_likely_files=[]),
        # Known and disjoint — parallel to each other, after story-12.
        "story-15": CoordinatorState(preflight_likely_files=["a.py"]),
        "story-25": CoordinatorState(preflight_likely_files=["b.py"]),
    }

    synthetic = compute_synthetic_edges(states, tasks)

    assert synthetic == {
        "story-15": ["story-12"],
        "story-25": ["story-12"],
    }
    # story-15 and story-25 do not depend on each other — disjoint work stays parallel.
    build_dag(inject_synthetic_deps(tasks, synthetic))


def test_compute_synthetic_edges_two_unknown_footprints_not_serialized() -> None:
    """Two prediction-less stories are NOT serialized against each other: there
    is no concrete file claim on either side and no evidence of overlap, so
    chaining every prediction-less story would collapse all parallelism on zero
    signal (e.g. a fully offline sprint where preflight could not run). That
    residual overlap is left to the integration step. Conservative serialization
    only fires against a sibling that made a concrete, non-empty file claim."""
    tasks = [_task("story-12", 12), _task("story-15", 15)]
    states = {
        "story-12": CoordinatorState(preflight_likely_files=[]),
        "story-15": CoordinatorState(preflight_likely_files=None),
    }

    assert compute_synthetic_edges(states, tasks) == {}


def test_compute_synthetic_edges_falls_back_to_slug_order_when_issue_missing() -> None:
    tasks = [_task("b-story", None), _task("a-story", None)]
    states = {
        "b-story": CoordinatorState(preflight_likely_files=["shared.py"]),
        "a-story": CoordinatorState(preflight_likely_files=["shared.py"]),
    }

    assert compute_synthetic_edges(states, tasks) == {"b-story": ["a-story"]}


def test_compute_synthetic_edges_skips_edges_that_would_cycle() -> None:
    tasks = [
        _task("issue-267", 267, depends_on=["issue-750"]),
        _task("issue-750", 750),
    ]
    states = {
        "issue-267": CoordinatorState(preflight_likely_files=["tests/test_config_load.py"]),
        "issue-750": CoordinatorState(preflight_likely_files=["tests/test_config_load.py"]),
    }

    synthetic = compute_synthetic_edges(states, tasks)

    assert synthetic == {}
    build_dag(inject_synthetic_deps(tasks, synthetic))


def test_compute_synthetic_edges_skips_already_serialized_collisions() -> None:
    tasks = [
        _task("issue-267", 267),
        _task("issue-750", 750, depends_on=["issue-267"]),
    ]
    states = {
        "issue-267": CoordinatorState(preflight_likely_files=["tests/test_config_load.py"]),
        "issue-750": CoordinatorState(preflight_likely_files=["tests/test_config_load.py"]),
    }

    synthetic = compute_synthetic_edges(states, tasks)

    assert synthetic == {}
    build_dag(inject_synthetic_deps(tasks, synthetic))


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


def test_compute_bundle_assignments_does_not_require_preflight_flag() -> None:
    """Bundling eligibility is purely relational — no per-story LLM flag is consulted.

    The legacy preflight_bundle_candidate flag has been removed as a gating input
    (the preflight prompt never asked for it and the decision is cross-story).
    Two small bug stories sharing an area: tag must bundle regardless of whether
    the legacy flag was True or False on either side.
    """
    tasks = [
        TaskStory(
            name="story-12",
            slug="story-12",
            story_path=Path("stories/story-12.md"),
            depends_on=[],
            github_issue=12,
            story_text="Area: api",
        ),
        TaskStory(
            name="story-15",
            slug="story-15",
            story_path=Path("stories/story-15.md"),
            depends_on=[],
            github_issue=15,
            story_text="Area: api",
        ),
    ]
    states = {
        "story-12": CoordinatorState(
            preflight_work_type="bug",
            preflight_complexity="small",
            preflight_bundle_candidate=False,
            preflight_likely_files=["src/api.py"],
        ),
        "story-15": CoordinatorState(
            preflight_work_type="bug",
            preflight_complexity="small",
            preflight_bundle_candidate=False,
            preflight_likely_files=["src/api.py"],
        ),
    }

    assert compute_bundle_assignments(states, tasks) == [["story-12", "story-15"]]


def test_compute_bundle_assignments_uses_complexity_weights_under_ceiling() -> None:
    tasks = [
        TaskStory(
            name="story-12",
            slug="story-12",
            story_path=Path("stories/story-12.md"),
            depends_on=[],
            github_issue=12,
            story_text="Area: api",
        ),
        TaskStory(
            name="story-15",
            slug="story-15",
            story_path=Path("stories/story-15.md"),
            depends_on=[],
            github_issue=15,
            story_text="Area: api",
        ),
    ]
    states = {
        "story-12": CoordinatorState(
            preflight_work_type="bug",
            preflight_complexity="small",
            preflight_likely_files=["src/api.py"],
        ),
        "story-15": CoordinatorState(
            preflight_work_type="bug",
            preflight_complexity="small",
            preflight_likely_files=["src/api.py"],
        ),
    }

    assert compute_bundle_assignments(states, tasks, complexity_ceiling=2) == [
        ["story-12", "story-15"]
    ]
    assert compute_bundle_assignments(states, tasks, complexity_ceiling=1) == []


def test_compute_bundle_assignments_redact_py_pair() -> None:
    """The #799/#800 fixture: two small redact.py bug fixes must bundle.

    This is the canonical case from issue #1348 — two stories with work_type=bug,
    complexity=small, and identical single-file likely_files were never bundling
    because the prior gate required an LLM-emitted flag the preflight prompt did
    not ask for. The new contract is purely relational: shared known footprint
    + work_type + complexity is sufficient.
    """
    tasks = [
        TaskStory(
            name="issue-799",
            slug="issue-799",
            story_path=Path("stories/issue-799.md"),
            depends_on=[],
            github_issue=799,
        ),
        TaskStory(
            name="issue-800",
            slug="issue-800",
            story_path=Path("stories/issue-800.md"),
            depends_on=[],
            github_issue=800,
        ),
    ]
    states = {
        "issue-799": CoordinatorState(
            preflight_work_type="bug",
            preflight_complexity="small",
            preflight_likely_files=["src/theforge/coordinator/redact.py"],
        ),
        "issue-800": CoordinatorState(
            preflight_work_type="bug",
            preflight_complexity="small",
            preflight_likely_files=["src/theforge/coordinator/redact.py"],
        ),
    }

    assert compute_bundle_assignments(states, tasks) == [["issue-799", "issue-800"]]


def test_bundle_overlap_fails_closed_on_unknown_likely_files() -> None:
    """Bundling refuses to glue stories together when likely_files is unknown.

    Asymmetric default vs. collision-DAG: bundle path requires positive evidence
    of overlap (matching area: tag or known-files intersection). Two stories
    with no area: tag and likely_files=None must NOT bundle.
    """
    tasks = [
        _task("story-12", 12),
        _task("story-15", 15),
    ]
    states = {
        "story-12": CoordinatorState(
            preflight_work_type="bug",
            preflight_complexity="small",
            preflight_likely_files=None,
        ),
        "story-15": CoordinatorState(
            preflight_work_type="bug",
            preflight_complexity="small",
            preflight_likely_files=None,
        ),
    }

    assert compute_bundle_assignments(states, tasks) == []


def test_collision_dag_serializes_unknown_footprint_pair() -> None:
    """Collision-DAG must continue to fail-closed in the over-serialize direction.

    The asymmetric overlap-default contract: bundling refuses to glue unknown
    footprints (above), but the collision-DAG path is unaffected by the new
    bundle predicate — it uses likely_files indexing in compute_synthetic_edges,
    so two stories sharing a known file still get serialized. This test locks in
    that the bundle-side change does not regress the DAG-side behavior for the
    known-overlap case.
    """
    tasks = [
        _task("story-12", 12),
        _task("story-15", 15),
    ]
    states = {
        "story-12": CoordinatorState(preflight_likely_files=["src/shared.py"]),
        "story-15": CoordinatorState(preflight_likely_files=["src/shared.py"]),
    }

    assert compute_synthetic_edges(states, tasks) == {"story-15": ["story-12"]}
