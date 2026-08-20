"""Resume must not schedule apart-sequenced stories together (#2610).

Seam-level coverage of the boundary the bug crossed: preflight state → resume
footprint registration → collision edges → DAG. A resumed story past preflight
gets a *fresh* live preflight whose result, against a preserved and diverged
worktree, is routinely no file claim at all. The footprint the original run
actually scheduled on is still on disk in the coordinator's resume record, and
scheduling has to consume it before dispatch — otherwise two stories the first
run deliberately serialized become co-ready and race to a merge conflict.
"""

from pathlib import Path
from unittest.mock import patch

import yaml
from sprint_test_helpers import run_sprint_ctx

from tests.test_sprint_resume import (
    _make_config,
    _make_coordinator_result,
    _make_spec_file,
)
from theforge.coordinator.resume_persistence import save_resume_record
from theforge.coordinator.state import CoordinatorState
from theforge.sprint.dag import StoryTriage, build_dag

#: Both stories touched this file in the original run — the overlap that made
#: the first run's planner put them in different batches.
SHARED_FILE = "hdp_mcp/schema_gen.py"


def _make_parallel_manifest(tmp_path: Path, specs: list[str]) -> Path:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        yaml.dump(
            {
                "name": "Test Sprint",
                "budget_usd": 10.0,
                "max_parallel": 2,
                "specs": specs,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _record_prior_preflight(project_root: Path, slug: str, likely_files: list[str]) -> None:
    state = CoordinatorState(
        preflight_verdict="PROCEED",
        preflight_reason="original run",
        preflight_likely_files=likely_files,
    )
    save_resume_record(project_root, state, slug=slug, run_id="run-original")


def test_resume_reconstructs_collision_edge_from_recorded_preflight(tmp_path: Path) -> None:
    """The resumed DAG cannot make both stories ready in the same generation."""
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_parallel_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)

    _record_prior_preflight(tmp_path, "feature-a", [SHARED_FILE, "src/a.py"])
    _record_prior_preflight(tmp_path, "feature-b", [SHARED_FILE, "src/b.py"])

    triages = {}
    for slug, action in (("feature-a", "review"), ("feature-b", "dev")):
        worktree = tmp_path / slug
        worktree.mkdir(exist_ok=True)
        triages[slug] = StoryTriage(
            story_path=f"{slug}.md",
            action=action,
            reason="resume",
            worktree_path=worktree,
            slug=slug,
        )

    def triage_side_effect(spec_path, config, project_root, *, task=None, **_progress):
        return triages["feature-a" if "feature-a" in spec_path else "feature-b"]

    def batch_preflight_side_effect(tasks, *_args, **_kwargs):
        # The failure condition being reproduced: the resumed run's own
        # preflight produced no file claim for either story.
        return {
            "feature-a": CoordinatorState(preflight_likely_files=None),
            "feature-b": CoordinatorState(preflight_likely_files=[]),
        }

    captured_tasks: list = []

    def capture_build_dag(tasks, **kwargs):
        captured_tasks.append(list(tasks))
        return build_dag(tasks, **kwargs)

    coord_result = _make_coordinator_result(success=True, cost=1.0)

    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=triage_side_effect),
        patch(
            "theforge.sprint.runner.run_batch_preflight",
            side_effect=batch_preflight_side_effect,
        ),
        # No plan.md exists in either worktree; make the weaker fallback's
        # emptiness explicit rather than incidental.
        patch("theforge.sprint.runner._extract_plan_footprint", return_value=set()),
        patch("theforge.sprint.runner.build_dag", side_effect=capture_build_dag),
        patch("theforge.sprint.runner.run_from_review", return_value=coord_result),
        patch("theforge.sprint.runner.run_from_dev", return_value=coord_result),
    ):
        result = run_sprint_ctx(config, manifest_path, resume=True)

    assert result.specs_succeeded == 2
    assert captured_tasks, "build_dag was never called"

    collision = {t.slug: list(t.collision_deps or []) for t in captured_tasks[0]}
    assert set(collision) == {"feature-a", "feature-b"}
    # Deterministic direction: the collision edge is ordered by slug, so
    # feature-b is the one pinned behind feature-a.
    assert collision["feature-b"] == ["feature-a"]
    assert collision["feature-a"] == []

    dag = build_dag(captured_tasks[0])
    assert [t.slug for t in dag.ready()] == ["feature-a"]


def test_story_rows_record_the_footprint_scheduling_used(tmp_path: Path) -> None:
    """The scheduling footprint is on the run record, not only in a log line.

    Diagnosing a lost collision edge means asking what each story claimed; that
    has to be recoverable from the sprint audit after the fact (#2610).
    """
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    manifest_path = _make_parallel_manifest(tmp_path, ["feature-a.md"])
    config = _make_config(tmp_path)

    worktree = tmp_path / "feature-a"
    worktree.mkdir()
    triage = StoryTriage(
        story_path="feature-a.md",
        action="review",
        reason="resume",
        worktree_path=worktree,
        slug="feature-a",
    )

    result_a = _make_coordinator_result(success=True, cost=1.0)
    result_a.state.preflight_likely_files = [SHARED_FILE]

    with (
        patch("theforge.sprint.runner._triage_spec", return_value=triage),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner._extract_plan_footprint", return_value=set()),
        patch("theforge.sprint.runner.run_from_review", return_value=result_a),
    ):
        run_sprint_ctx(config, manifest_path, resume=True)

    audit = yaml.safe_load(
        (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
    )
    rows = audit["specs"]
    assert [row["preflight_likely_files"] for row in rows] == [[SHARED_FILE]]

    state_files = list((tmp_path / ".forge" / "sprints").glob("*/state.yaml"))
    assert len(state_files) == 1, state_files
    state = yaml.safe_load(state_files[0].read_text(encoding="utf-8"))
    assert [s["preflight_likely_files"] for s in state["stories"]] == [[SHARED_FILE]]
