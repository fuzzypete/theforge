"""Cost-aware batch groups (#727) — the third scheduling primitive.

Batch groups pack small *independent* stories into one dev assignment for cost
and throughput. They are distinct from DAG dependencies (ordering) and from
conflict bundles (merge-pain avoidance), and subordinate to both. These tests
pin the seams that make that true: eligibility, independence, precedence,
operator visibility, the shared dev prompt, and per-story reporting.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from theforge.config.types import SprintBatchConfig
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.collision import (
    batch_group_id,
    build_batch_hint,
    compute_batch_groups,
)
from theforge.task import BatchMember, TaskStory, build_batch_dev_prompt

ENABLED = SprintBatchConfig(max_stories=2, max_complexity_budget=2, max_touched_files=6)
ENABLED_3 = SprintBatchConfig(max_stories=3, max_complexity_budget=3, max_touched_files=6)


def _task(
    slug: str,
    issue: int | None = None,
    depends_on: list[str] | None = None,
    collision_deps: list[str] | None = None,
    story_text: str = "story body",
) -> TaskStory:
    return TaskStory(
        name=f"Story {slug}",
        slug=slug,
        story_path=Path(f"stories/{slug}.md"),
        story_text=story_text,
        depends_on=depends_on or [],
        collision_deps=collision_deps or [],
        github_issue=issue,
    )


def _state(
    *,
    complexity: str | None = "small",
    work_type: str | None = "bug",
    sufficiency: str | None = "implementation_ready",
    likely_files: list[str] | None,
) -> CoordinatorState:
    state = CoordinatorState()
    state.preflight_complexity = complexity
    state.preflight_work_type = work_type
    state.preflight_sufficiency = sufficiency
    state.preflight_likely_files = likely_files
    return state


# ── Eligibility ──────────────────────────────────────────────────────────


def test_small_independent_implementation_ready_bugs_batch() -> None:
    tasks = [_task("issue-10", 10), _task("issue-11", 11)]
    states = {
        "issue-10": _state(likely_files=["a.py"]),
        "issue-11": _state(likely_files=["b.py"]),
    }

    groups = compute_batch_groups(states, tasks, batch_config=ENABLED)

    assert groups == [["issue-10", "issue-11"]]


def test_mechanical_work_type_is_eligible_alongside_bug() -> None:
    tasks = [_task("issue-10", 10), _task("issue-11", 11)]
    states = {
        "issue-10": _state(work_type="mechanical", likely_files=["a.py"]),
        "issue-11": _state(work_type="bug", likely_files=["b.py"]),
    }

    assert compute_batch_groups(states, tasks, batch_config=ENABLED) == [["issue-10", "issue-11"]]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("complexity", "medium"),
        ("complexity", None),
        ("work_type", "feature"),
        ("work_type", "refactor"),
        ("work_type", None),
        ("sufficiency", "needs_planning"),
        ("sufficiency", None),
    ],
)
def test_ineligible_preflight_fields_block_batching(field: str, value: str | None) -> None:
    tasks = [_task("issue-10", 10), _task("issue-11", 11)]
    states = {
        "issue-10": _state(likely_files=["a.py"], **{field: value}),
        "issue-11": _state(likely_files=["b.py"]),
    }

    assert compute_batch_groups(states, tasks, batch_config=ENABLED) == []


@pytest.mark.parametrize("footprint", [None, []])
def test_unknown_footprint_never_batches(footprint: list[str] | None) -> None:
    tasks = [_task("issue-10", 10), _task("issue-11", 11)]
    states = {
        "issue-10": _state(likely_files=footprint),
        "issue-11": _state(likely_files=["b.py"]),
    }

    assert compute_batch_groups(states, tasks, batch_config=ENABLED) == []


def test_wide_footprint_story_is_not_small_enough_to_batch() -> None:
    tasks = [_task("issue-10", 10), _task("issue-11", 11)]
    states = {
        "issue-10": _state(likely_files=[f"f{i}.py" for i in range(9)]),
        "issue-11": _state(likely_files=["b.py"]),
    }

    config = SprintBatchConfig(max_stories=2, max_complexity_budget=2, max_touched_files=3)
    assert compute_batch_groups(states, tasks, batch_config=config) == []


def test_build_batch_hint_names_the_reason_a_story_is_ineligible() -> None:
    hint = build_batch_hint(
        _task("issue-10", 10),
        _state(complexity="medium", likely_files=["a.py"]),
        max_touched_files=6,
    )
    assert hint.eligible is False
    assert "medium" in (hint.ineligible_reason or "")

    ok = build_batch_hint(
        _task("issue-11", 11), _state(likely_files=["b.py"]), max_touched_files=6
    )
    assert ok.eligible is True
    assert ok.ineligible_reason is None


# ── Independence, and precedence over the other two primitives ───────────


def test_overlapping_stories_are_a_bundle_question_not_a_batch() -> None:
    tasks = [_task("issue-10", 10), _task("issue-11", 11)]
    states = {
        "issue-10": _state(likely_files=["shared.py"]),
        "issue-11": _state(likely_files=["shared.py", "b.py"]),
    }

    assert compute_batch_groups(states, tasks, batch_config=ENABLED) == []


def test_stories_sharing_an_area_label_are_not_independent() -> None:
    tasks = [
        _task("issue-10", 10, story_text="area: cli\nbody"),
        _task("issue-11", 11, story_text="area: cli\nbody"),
    ]
    states = {
        "issue-10": _state(likely_files=["a.py"]),
        "issue-11": _state(likely_files=["b.py"]),
    }

    assert compute_batch_groups(states, tasks, batch_config=ENABLED) == []


def test_conflict_bundled_slugs_are_excluded_from_batch_groups() -> None:
    tasks = [_task("issue-10", 10), _task("issue-11", 11), _task("issue-12", 12)]
    states = {
        "issue-10": _state(likely_files=["a.py"]),
        "issue-11": _state(likely_files=["b.py"]),
        "issue-12": _state(likely_files=["c.py"]),
    }

    groups = compute_batch_groups(
        states, tasks, batch_config=ENABLED, excluded_slugs={"issue-10", "issue-12"}
    )

    # Only issue-11 is left; a group of one is not a batch.
    assert groups == []


@pytest.mark.parametrize(
    "edges",
    [
        {"depends_on": ["issue-11"]},
        {"collision_deps": ["issue-11"]},
    ],
)
def test_a_story_with_a_dependency_edge_never_batches(edges: dict) -> None:
    tasks = [_task("issue-10", 10, **edges), _task("issue-11", 11), _task("issue-12", 12)]
    states = {
        "issue-10": _state(likely_files=["a.py"]),
        "issue-11": _state(likely_files=["b.py"]),
        "issue-12": _state(likely_files=["c.py"]),
    }

    groups = compute_batch_groups(states, tasks, batch_config=ENABLED)

    # Both endpoints of the edge are excluded; issue-12 is left alone.
    assert groups == []


def test_a_dependency_parent_is_excluded_even_though_its_own_edges_are_empty() -> None:
    tasks = [
        _task("issue-10", 10),  # parent: issue-30 depends on it
        _task("issue-11", 11),
        _task("issue-30", 30, depends_on=["issue-10"]),
    ]
    states = {
        "issue-10": _state(likely_files=["a.py"]),
        "issue-11": _state(likely_files=["b.py"]),
        "issue-30": _state(likely_files=["c.py"]),
    }

    assert compute_batch_groups(states, tasks, batch_config=ENABLED) == []


# ── Config knob ──────────────────────────────────────────────────────────


def test_batching_is_off_by_default() -> None:
    tasks = [_task("issue-10", 10), _task("issue-11", 11)]
    states = {
        "issue-10": _state(likely_files=["a.py"]),
        "issue-11": _state(likely_files=["b.py"]),
    }

    assert compute_batch_groups(states, tasks, batch_config=SprintBatchConfig()) == []
    assert compute_batch_groups(states, tasks) == []


def test_max_stories_caps_group_size() -> None:
    tasks = [_task(f"issue-1{i}", 10 + i) for i in range(4)]
    states = {t.slug: _state(likely_files=[f"{t.slug}.py"]) for t in tasks}

    two = compute_batch_groups(states, tasks, batch_config=ENABLED)
    assert [len(g) for g in two] == [2, 2]

    three = compute_batch_groups(states, tasks, batch_config=ENABLED_3)
    assert [len(g) for g in three] == [3]


def test_max_complexity_budget_caps_the_group() -> None:
    tasks = [_task("issue-10", 10), _task("issue-11", 11), _task("issue-12", 12)]
    states = {t.slug: _state(likely_files=[f"{t.slug}.py"]) for t in tasks}

    config = SprintBatchConfig(max_stories=3, max_complexity_budget=2, max_touched_files=6)
    assert compute_batch_groups(states, tasks, batch_config=config) == [["issue-10", "issue-11"]]


def test_max_touched_files_caps_the_combined_footprint() -> None:
    tasks = [_task("issue-10", 10), _task("issue-11", 11)]
    states = {
        "issue-10": _state(likely_files=["a.py", "b.py"]),
        "issue-11": _state(likely_files=["c.py", "d.py"]),
    }

    config = SprintBatchConfig(max_stories=2, max_complexity_budget=2, max_touched_files=3)
    assert compute_batch_groups(states, tasks, batch_config=config) == []


def test_groups_are_deterministic_in_issue_order() -> None:
    tasks = [_task("issue-30", 30), _task("issue-10", 10), _task("issue-20", 20)]
    states = {t.slug: _state(likely_files=[f"{t.slug}.py"]) for t in tasks}

    groups = compute_batch_groups(states, tasks, batch_config=ENABLED)
    assert groups == [["issue-10", "issue-20"]]
    assert batch_group_id(groups[0]) == "batch-issue-10"


# ── Config loading ───────────────────────────────────────────────────────


def _load(tmp_path: Path, sprint_block: str):
    from theforge.config import load_config

    (tmp_path / "forge.yaml").write_text(
        "project: demo\n"
        "models:\n  - claude/sonnet\n"
        "workspace:\n  base_branch: main\n"
        f"{sprint_block}",
        encoding="utf-8",
    )
    return load_config(tmp_path / "forge.yaml")


def test_sprint_batch_knobs_load_from_forge_yaml(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        "sprint:\n"
        "  max_parallel: 2\n"
        "  batch:\n"
        "    max_stories: 3\n"
        "    max_complexity_budget: 4\n"
        "    max_touched_files: 8\n",
    )
    assert config.sprint.batch.max_stories == 3
    assert config.sprint.batch.max_complexity_budget == 4
    assert config.sprint.batch.max_touched_files == 8


def test_sprint_batch_defaults_to_disabled(tmp_path: Path) -> None:
    config = _load(tmp_path, "sprint:\n  max_parallel: 1\n")
    assert config.sprint.batch.max_stories == 1


@pytest.mark.parametrize(
    "block",
    [
        "sprint:\n  batch:\n    max_stories: two\n",
        "sprint:\n  batch:\n    max_stories: true\n",
        "sprint:\n  batch:\n    max_stories: 0\n",
        "sprint:\n  batch:\n    max_complexity_budget: -1\n",
        "sprint:\n  batch:\n    max_touched_files: 1.5\n",
        "sprint:\n  batch: 3\n",
    ],
)
def test_invalid_sprint_batch_config_is_rejected(tmp_path: Path, block: str) -> None:
    with pytest.raises(ValueError, match="sprint.batch"):
        _load(tmp_path, block)


# ── forge status rendering ───────────────────────────────────────────────


def _live_story(slug: str, *, bundle: bool = False, batch: str | None = None) -> dict:
    return {
        "slug": slug,
        "path": slug,
        "status": "running",
        "phase": "DEV",
        "cost_usd": 0.0,
        "bundle_candidate": bundle,
        "batch_group": batch,
        "blocked_by": [],
    }


def test_status_renders_batch_groups_separately_from_conflict_bundles(
    tmp_path: Path, capsys
) -> None:
    import yaml

    from theforge.cli.sprint_status import display_sprint_status

    runs = tmp_path / ".forge" / "runs"
    runs.mkdir(parents=True)
    (runs / "run-1.pid").write_text("1\n", encoding="utf-8")
    (runs / "run-1.state").write_text(
        yaml.safe_dump(
            {
                "sprint_name": "s1",
                "stories": [
                    _live_story("issue-1", bundle=True),
                    _live_story("issue-2", bundle=True),
                    _live_story("issue-10", batch="batch-issue-10"),
                    _live_story("issue-11", batch="batch-issue-10"),
                    _live_story("issue-99"),
                ],
            }
        ),
        encoding="utf-8",
    )

    with patch(
        "theforge.cli.sprint_status._ensure_titles",
        lambda *a, **k: None,
    ):
        assert display_sprint_status("run-1", tmp_path) == 0

    out = capsys.readouterr().out
    assert "[bundle: issue-1  issue-2]" in out
    assert "[batch: batch-issue-10  issue-10  issue-11]" in out
    # Conflict bundles first, then batch groups, then ungrouped stories — three
    # primitives, three distinguishable renderings.
    assert out.index("[bundle:") < out.index("[batch:") < out.rindex("issue-99")


def test_status_reader_carries_batch_group_from_live_state(tmp_path: Path) -> None:
    import yaml

    from theforge.sprint.status_reader import read_live_status

    runs = tmp_path / ".forge" / "runs"
    runs.mkdir(parents=True)
    (runs / "run-1.state").write_text(
        yaml.safe_dump(
            {
                "stories": [
                    {
                        "slug": "issue-10",
                        "path": "Issue #10",
                        "status": "running",
                        "batch_group": "batch-issue-10",
                        "bundle_candidate": False,
                    },
                    {"slug": "issue-99", "path": "Issue #99", "status": "waiting"},
                ]
            }
        ),
        encoding="utf-8",
    )

    entries = read_live_status("run-1", tmp_path)
    assert entries is not None
    by_slug = {e.slug: e for e in entries}
    assert by_slug["issue-10"].batch_group == "batch-issue-10"
    assert by_slug["issue-99"].batch_group is None


def test_story_state_round_trips_batch_group() -> None:
    from theforge.sprint.story_state import SprintStoryState

    state = SprintStoryState()
    state.register("issue-10", "Issue #10", batch_group="batch-issue-10")
    assert state.as_dict()[0]["batch_group"] == "batch-issue-10"

    restored = SprintStoryState.from_dict(state.as_dict())
    assert restored.get("issue-10").batch_group == "batch-issue-10"


# ── Batch dev prompt ─────────────────────────────────────────────────────


def _members() -> tuple[BatchMember, ...]:
    return (
        BatchMember(
            name="Fix the widget",
            slug="issue-10",
            story_text="## What\nWidget is broken.\nUNIQUE-SPEC-ALPHA",
            display_ref="Issue #10",
        ),
        BatchMember(
            name="Rename the gadget",
            slug="issue-11",
            story_text="## What\nGadget has the wrong name.\nUNIQUE-SPEC-BETA",
            display_ref="Issue #11",
        ),
    )


def _prompt() -> str:
    task = TaskStory(name="Fix the widget", slug="issue-10", story_text="ignored")
    return build_batch_dev_prompt(
        task,
        members=_members(),
        workspace_path=Path("/tmp/wt"),
        branch_name="forge/issue-10",
        story_content="ignored",
        gate_command="make gate",
    )


def test_batch_prompt_contains_every_member_spec() -> None:
    prompt = _prompt()
    assert "UNIQUE-SPEC-ALPHA" in prompt
    assert "UNIQUE-SPEC-BETA" in prompt
    assert "issue-10" in prompt
    assert "issue-11" in prompt
    assert "Issue #10" in prompt
    assert "Issue #11" in prompt


def test_batch_prompt_requires_per_story_completion_notes() -> None:
    prompt = _prompt()
    assert "Per-Story Handoff (required)" in prompt
    assert "`slug` key" in prompt
    assert "acceptance_criteria" in prompt
    assert "validated and reviewed" in prompt


def test_batch_prompt_states_the_stories_are_independent() -> None:
    prompt = _prompt()
    assert "independent" in prompt.lower()
    assert "Do **not** refactor across stories" in prompt


def test_batch_prompt_keeps_the_single_story_workflow_language() -> None:
    """Policy/gate/handoff language must not drift between batched and unbatched."""
    from theforge.task import build_dev_prompt

    task = TaskStory(name="Fix the widget", slug="issue-10", story_text="ignored")
    kwargs = dict(
        workspace_path=Path("/tmp/wt"),
        branch_name="forge/issue-10",
        gate_command="make gate",
    )
    single = build_dev_prompt(task, story_content="single spec", **kwargs)
    batched = build_batch_dev_prompt(task, members=_members(), story_content="x", **kwargs)

    for shared in ("## Dev P2 Policy", "<forge_handoff>", "Do NOT merge to main.", "## Workflow"):
        assert shared in single
        assert shared in batched


def test_batch_prompt_builder_falls_back_when_there_are_no_members() -> None:
    task = TaskStory(name="Fix the widget", slug="issue-10", story_text="ignored")
    prompt = build_batch_dev_prompt(
        task,
        members=(),
        workspace_path=Path("/tmp/wt"),
        branch_name="forge/issue-10",
        story_content="LONE-SPEC",
        gate_command="make gate",
    )
    assert "LONE-SPEC" in prompt
    assert "Batch Assignment" not in prompt


def test_dev_phase_selects_the_batch_prompt_builder_from_batch_members() -> None:
    from theforge.coordinator.dev_phase import _dev_prompt_builder
    from theforge.task import build_dev_prompt

    plain = TaskStory(name="s", slug="issue-10")
    assert _dev_prompt_builder(plain) is build_dev_prompt

    leader = TaskStory(name="s", slug="issue-10", batch_members=_members())
    assert _dev_prompt_builder(leader) is not build_dev_prompt


# ── Per-story reporting across a shared dev pass ─────────────────────────


def _batch_config(tmp_path: Path) -> MagicMock:
    config = MagicMock()
    config.project_root = tmp_path
    config.workspace.path_pattern = "worktrees/{slug}"
    config.workspace.branch_pattern = "forge/{slug}"
    return config


def _ok_result(slug: str, *, workspace: Path, cost: float) -> CoordinatorResult:
    state = CoordinatorState(phase=Phase.DONE)
    state.workspace_path = workspace
    state.branch_name = f"forge/{slug}"
    state.gate_decisions = ["PASS"]
    state.gate_runs = 1
    state.last_gate_commit = "abc1234"
    state.last_gate_decision = "PASS"
    state.dev_results.append(SimpleNamespace(cost_usd=cost))
    result = CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok")
    result.landing_status = "pending_integration"
    return result


def test_batch_run_returns_one_result_row_per_original_story(tmp_path: Path) -> None:
    from theforge.sprint import runner as runner_mod

    config = _batch_config(tmp_path)
    workspace = tmp_path / "worktrees" / "issue-10"
    leader = TaskStory(
        name="Leader", slug="issue-10", story_text="a", batch_group="batch-issue-10"
    )
    member = TaskStory(name="Member", slug="issue-11", story_text="b")
    leader_task = runner_mod.replace(
        leader,
        batch_members=_members(),
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    leader_result = _ok_result("issue-10", workspace=workspace, cost=0.40)

    member_state = CoordinatorState(phase=Phase.DONE)
    member_state.review_agent_results.append(SimpleNamespace(cost_usd=0.10))
    member_result = CoordinatorResult(
        success=True, phase=Phase.DONE, state=member_state, message="approved"
    )

    with (
        patch.object(
            runner_mod,
            "_run_single_story",
            return_value=(leader_task, leader_result, 12.0, now, now),
        ) as single,
        patch.object(runner_mod, "run_review_only", return_value=member_result) as review_only,
    ):
        results = runner_mod._run_batch_group(
            config,
            leader_task,
            [member],
            "sprint-run",
            "sprint-1",
            False,
            False,
            False,
            {"issue-10": None, "issue-11": None},
            False,
            None,
            None,
        )

    # One dev pass for the group...
    assert single.call_count == 1
    # ...and one review per remaining story, against that story's own spec.
    assert review_only.call_count == 1
    assert review_only.call_args.args[1] is member
    assert review_only.call_args.args[2] == workspace

    # One result row per original slug, each with its own cost and outcome.
    assert set(results) == {"issue-10", "issue-11"}
    assert results["issue-10"][1] is leader_result
    assert results["issue-11"][1].success is True
    assert results["issue-10"][1].state.total_cost == 0.40
    assert results["issue-11"][1].state.total_cost == 0.10

    # Both members carry the group id, and the shared gate result is reported
    # on the member too rather than showing an empty validation record.
    assert results["issue-10"][1].state.preflight_batch_group == "batch-issue-10"
    assert results["issue-11"][1].state.preflight_batch_group == "batch-issue-10"
    assert results["issue-11"][1].state.gate_decisions == ["PASS"]
    assert results["issue-11"][1].state.last_gate_decision == "PASS"

    # Only the leader lands: one branch carries the group's commits.
    assert results["issue-10"][1].landing_status == "pending_integration"
    assert results["issue-11"][1].landing_status is None


def test_failed_shared_dev_pass_fails_each_member_on_its_own_row(tmp_path: Path) -> None:
    from theforge.sprint import runner as runner_mod

    config = _batch_config(tmp_path)
    leader = TaskStory(
        name="Leader", slug="issue-10", story_text="a", batch_group="batch-issue-10"
    )
    member = TaskStory(name="Member", slug="issue-11", story_text="b")

    failed_state = CoordinatorState(phase=Phase.ESCALATE)
    failed_state.error = "dev blew up"
    failed = CoordinatorResult(
        success=False, phase=Phase.ESCALATE, state=failed_state, message="dev blew up"
    )
    now = datetime.datetime.now(datetime.timezone.utc)

    with (
        patch.object(
            runner_mod, "_run_single_story", return_value=(leader, failed, 1.0, now, now)
        ),
        patch.object(runner_mod, "run_review_only") as review_only,
    ):
        results = runner_mod._run_batch_group(
            config,
            leader,
            [member],
            "sprint-run",
            "sprint-1",
            False,
            False,
            False,
            {"issue-10": None, "issue-11": None},
            False,
            None,
            None,
        )

    review_only.assert_not_called()
    assert set(results) == {"issue-10", "issue-11"}
    # The member is neither silently dropped nor credited with a verdict it
    # never received: it gets its own failed row naming the group.
    member_result = results["issue-11"][1]
    assert member_result.success is False
    assert member_result.phase is Phase.ESCALATE
    assert "batch group batch-issue-10" in member_result.message


def test_member_review_exception_stays_on_that_member(tmp_path: Path) -> None:
    from theforge.sprint import runner as runner_mod

    config = _batch_config(tmp_path)
    workspace = tmp_path / "worktrees" / "issue-10"
    leader = TaskStory(
        name="Leader", slug="issue-10", story_text="a", batch_group="batch-issue-10"
    )
    members = [TaskStory(name="M1", slug="issue-11"), TaskStory(name="M2", slug="issue-12")]

    ok_state = CoordinatorState(phase=Phase.DONE)
    ok = CoordinatorResult(success=True, phase=Phase.DONE, state=ok_state, message="ok")
    now = datetime.datetime.now(datetime.timezone.utc)

    with (
        patch.object(
            runner_mod,
            "_run_single_story",
            return_value=(
                leader,
                _ok_result("issue-10", workspace=workspace, cost=0.4),
                5.0,
                now,
                now,
            ),
        ),
        patch.object(runner_mod, "run_review_only", side_effect=[RuntimeError("boom"), ok]),
    ):
        results = runner_mod._run_batch_group(
            config,
            leader,
            members,
            "sprint-run",
            "sprint-1",
            False,
            False,
            False,
            dict.fromkeys(["issue-10", "issue-11", "issue-12"]),
            False,
            None,
            None,
        )

    assert results["issue-11"][1].success is False
    assert "RuntimeError" in results["issue-11"][1].message
    # The sibling still gets its own review and its own successful row.
    assert results["issue-12"][1].success is True


# ── Scheduler seam: dispatch and per-story reporting through run_sprint ──


def _batch_sprint_config(tmp_path: Path):
    """A sprint config with batching enabled at two stories per group."""
    from dataclasses import replace as dc_replace

    from tests.test_sprint_resume import _make_config
    from theforge.config.types import SprintConfig

    config = _make_config(tmp_path)
    return dc_replace(
        config,
        sprint=SprintConfig(batch=SprintBatchConfig(max_stories=2, max_complexity_budget=2)),
    )


def _preflight_states_for(*slugs: str, files: dict[str, list[str]]) -> dict:
    states = {}
    for slug in slugs:
        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        state.preflight_complexity = "small"
        state.preflight_work_type = "bug"
        state.preflight_sufficiency = "implementation_ready"
        state.preflight_likely_files = files[slug]
        states[slug] = state
    return states


def test_run_sprint_dispatches_one_dev_pass_and_reports_each_story(tmp_path: Path) -> None:
    """The scheduler seam: two eligible stories, one dev pass, two story rows."""
    from tests.test_sprint_resume import _make_coordinator_result, _make_manifest, _make_spec_file
    from theforge.sprint.runner import run_sprint

    _make_spec_file(tmp_path, "Bug A", "bug-a")
    _make_spec_file(tmp_path, "Bug B", "bug-b")
    manifest_path = _make_manifest(tmp_path, ["bug-a.md", "bug-b.md"])
    config = _batch_sprint_config(tmp_path)

    states = _preflight_states_for(
        "bug-a", "bug-b", files={"bug-a": ["src/a.py"], "bug-b": ["src/b.py"]}
    )
    leader_result = _make_coordinator_result(success=True, cost=0.50)
    leader_result.state.workspace_path = tmp_path / "bug-a"
    leader_result.state.branch_name = "forge/bug-a"
    member_result = _make_coordinator_result(success=True, cost=0.10)

    with (
        patch("theforge.sprint.runner.run_batch_preflight", side_effect=lambda *a, **k: states),
        patch("theforge.sprint.runner.run_task", return_value=leader_result) as run_task_mock,
        patch("theforge.sprint.runner.run_review_only", return_value=member_result) as review_only,
    ):
        result = run_sprint(config, manifest_path)

    # One dev pass for the group — not one per story.
    assert run_task_mock.call_count == 1
    assert run_task_mock.call_args.args[1].slug == "bug-a"
    # The dev assignment names both stories.
    assert [m.slug for m in run_task_mock.call_args.args[1].batch_members] == ["bug-a", "bug-b"]
    # ...and the second story is still reviewed against its own spec.
    assert review_only.call_count == 1
    assert review_only.call_args.args[1].slug == "bug-b"

    # Per-story reporting survives the shared implementation.
    assert result.specs_total == 2
    assert result.specs_succeeded == 2


def test_run_sprint_does_not_batch_when_the_knob_is_off(tmp_path: Path) -> None:
    from tests.test_sprint_resume import (
        _make_config,
        _make_coordinator_result,
        _make_manifest,
        _make_spec_file,
    )
    from theforge.sprint.runner import run_sprint

    _make_spec_file(tmp_path, "Bug A", "bug-a")
    _make_spec_file(tmp_path, "Bug B", "bug-b")
    manifest_path = _make_manifest(tmp_path, ["bug-a.md", "bug-b.md"])
    config = _make_config(tmp_path)  # default sprint config: batching off

    states = _preflight_states_for(
        "bug-a", "bug-b", files={"bug-a": ["src/a.py"], "bug-b": ["src/b.py"]}
    )

    with (
        patch("theforge.sprint.runner.run_batch_preflight", side_effect=lambda *a, **k: states),
        patch(
            "theforge.sprint.runner.run_task", return_value=_make_coordinator_result(success=True)
        ) as run_task_mock,
        patch("theforge.sprint.runner.run_review_only") as review_only,
    ):
        result = run_sprint(config, manifest_path)

    assert run_task_mock.call_count == 2
    review_only.assert_not_called()
    assert result.specs_succeeded == 2


def test_batched_stories_are_visible_as_a_group_in_live_state(tmp_path: Path) -> None:
    """`forge status` reads batch_group from the live state file the sprint writes."""
    import yaml

    from tests.test_sprint_resume import _make_coordinator_result, _make_manifest, _make_spec_file
    from theforge.sprint.runner import run_sprint

    _make_spec_file(tmp_path, "Bug A", "bug-a")
    _make_spec_file(tmp_path, "Bug B", "bug-b")
    manifest_path = _make_manifest(tmp_path, ["bug-a.md", "bug-b.md"])
    config = _batch_sprint_config(tmp_path)

    states = _preflight_states_for(
        "bug-a", "bug-b", files={"bug-a": ["src/a.py"], "bug-b": ["src/b.py"]}
    )
    leader_result = _make_coordinator_result(success=True, cost=0.5)
    leader_result.state.workspace_path = tmp_path / "bug-a"

    captured: list[dict] = []
    real_init = None

    from theforge.sprint.state_writer import SprintStateWriter

    real_init = SprintStateWriter.init

    def capturing_init(self, stories):
        captured.extend(stories)
        return real_init(self, stories)

    with (
        patch("theforge.sprint.runner.run_batch_preflight", side_effect=lambda *a, **k: states),
        patch("theforge.sprint.runner.run_task", return_value=leader_result),
        patch(
            "theforge.sprint.runner.run_review_only",
            return_value=_make_coordinator_result(success=True, cost=0.1),
        ),
        patch.object(SprintStateWriter, "init", capturing_init),
    ):
        run_sprint(config, manifest_path, run_id="run-batch-1")

    by_slug = {s["slug"]: s for s in captured}
    assert by_slug["bug-a"]["batch_group"] == "batch-bug-a"
    assert by_slug["bug-b"]["batch_group"] == "batch-bug-a"
    assert yaml.safe_dump(by_slug["bug-a"])  # serialisable for the .state file
