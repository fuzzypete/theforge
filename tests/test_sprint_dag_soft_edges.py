"""Soft (collision-derived) dependency edge semantics in StoryDAG.

Collision-derived edges sit in ``TaskStory.collision_deps`` and have weaker
release semantics than explicit ``depends_on``: a soft upstream that reaches
any terminal state (mark_skipped or mark_complete) releases its downstream,
because main is unchanged at terminal-but-not-merged and the collision the
synthetic edge guarded against does not exist.
"""

from pathlib import Path

from theforge.coordinator.state import CoordinatorState
from theforge.sprint.collision import compute_synthetic_edges, inject_synthetic_deps
from theforge.sprint.dag import StoryDAG, build_dag
from theforge.task import TaskStory


def _task(
    slug: str,
    *,
    depends_on: list[str] | None = None,
    collision_deps: list[str] | None = None,
) -> TaskStory:
    return TaskStory(
        name=slug,
        slug=slug,
        story_path=Path(f"stories/{slug}.md"),
        depends_on=depends_on or [],
        collision_deps=collision_deps or [],
    )


def test_soft_dep_released_when_upstream_skipped() -> None:
    upstream = _task("up")
    down = _task("down", collision_deps=["up"])
    dag = StoryDAG([upstream, down])

    # Initially blocked (upstream not yet terminal).
    assert {t.slug for t in dag.ready()} == {"up"}

    dag.mark_skipped("up")

    assert "down" in {t.slug for t in dag.ready()}


def test_soft_dep_released_when_upstream_completed() -> None:
    upstream = _task("up")
    down = _task("down", collision_deps=["up"])
    dag = StoryDAG([upstream, down])

    dag.mark_complete("up")

    assert "down" in {t.slug for t in dag.ready()}


def test_hard_dep_blocks_when_upstream_skipped() -> None:
    upstream = _task("up")
    down = _task("down", depends_on=["up"])
    dag = StoryDAG([upstream, down])

    dag.mark_skipped("up")

    # Hard edge stays blocking even when upstream reaches a terminal state.
    assert "down" not in {t.slug for t in dag.ready()}


def test_mixed_hard_and_soft_release_on_completion_and_skip() -> None:
    hard_up = _task("hard-up")
    soft_up = _task("soft-up")
    down = _task("down", depends_on=["hard-up"], collision_deps=["soft-up"])
    dag = StoryDAG([hard_up, soft_up, down])

    # Neither upstream finished yet.
    assert "down" not in {t.slug for t in dag.ready()}

    dag.mark_skipped("soft-up")
    # Soft released, but hard upstream not yet completed.
    assert "down" not in {t.slug for t in dag.ready()}

    dag.mark_complete("hard-up")
    assert "down" in {t.slug for t in dag.ready()}


def test_soft_dep_blocks_until_upstream_terminal() -> None:
    upstream = _task("up")
    down = _task("down", collision_deps=["up"])
    dag = StoryDAG([upstream, down])

    # Upstream not yet finished — soft dep still blocks.
    assert "down" not in {t.slug for t in dag.ready()}


def test_unmet_deps_excludes_terminal_soft_upstream() -> None:
    hard_up = _task("hard-up")
    soft_up = _task("soft-up")
    down = _task("down", depends_on=["hard-up"], collision_deps=["soft-up"])
    dag = StoryDAG([hard_up, soft_up, down])

    dag.mark_skipped("soft-up")

    # soft-up reached a terminal state, so it is no longer reported as unmet.
    # hard-up is still pending and stays in unmet.
    assert dag.unmet_deps("down") == ["hard-up"]


def test_unmet_deps_includes_pending_soft_upstream() -> None:
    soft_up = _task("soft-up")
    down = _task("down", collision_deps=["soft-up"])
    dag = StoryDAG([soft_up, down])

    # Until upstream is finished, the soft edge is genuinely blocking.
    assert dag.unmet_deps("down") == ["soft-up"]


def test_collision_seam_soft_release_on_skipped_upstream() -> None:
    """Seam-level integration: runner pipeline (compute → inject → build_dag).

    Stories that collide on likely_files get collision_deps (not depends_on).
    A skipped upstream releases the downstream; an explicit depends_on edge
    in the same flow still hard-blocks.
    """
    a = _task("issue-1", depends_on=[])
    b = _task("issue-2", depends_on=[])
    c = _task("issue-3", depends_on=["issue-2"])  # explicit hard edge to b
    tasks = [a, b, c]
    states = {
        "issue-1": CoordinatorState(preflight_likely_files=["src/runner.py"]),
        "issue-2": CoordinatorState(preflight_likely_files=["src/runner.py"]),
        "issue-3": CoordinatorState(preflight_likely_files=["src/other.py"]),
    }

    synthetic = compute_synthetic_edges(states, tasks)
    augmented = inject_synthetic_deps(tasks, synthetic)
    dag = build_dag(augmented)

    augmented_by_slug = {t.slug: t for t in augmented}
    # issue-2 collided with issue-1, so issue-2 has a soft (collision) edge to issue-1.
    assert augmented_by_slug["issue-2"].collision_deps == ["issue-1"]
    assert augmented_by_slug["issue-2"].depends_on == []
    # issue-3's explicit dep stays in depends_on.
    assert augmented_by_slug["issue-3"].depends_on == ["issue-2"]
    assert augmented_by_slug["issue-3"].collision_deps == []

    # Initial: issue-1 is the only ready story.
    assert {t.slug for t in dag.ready()} == {"issue-1"}

    # Skipping issue-1 (terminal-not-merged) releases issue-2 via the soft edge.
    dag.mark_skipped("issue-1")
    assert "issue-2" in {t.slug for t in dag.ready()}
    # issue-3 still hard-blocked on issue-2.
    assert "issue-3" not in {t.slug for t in dag.ready()}

    # Skipping issue-2 must NOT release issue-3 — that edge is hard.
    dag.mark_skipped("issue-2")
    assert "issue-3" not in {t.slug for t in dag.ready()}

    # Only completion of issue-2 releases issue-3.
    dag.mark_complete("issue-2")
    assert "issue-3" in {t.slug for t in dag.ready()}
