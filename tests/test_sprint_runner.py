"""Tests for sprint DAG satisfied-dependency handling and runner seams.

Covers the case where a depends_on slug references a story already merged to
main (not present in the current sprint manifest).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from theforge.agent_types import AgentResult, ModelUsage
from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    WorkspaceConfig,
)
from theforge.config.types import IntakeConfig
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.dag import (
    StoryDAG,
    _is_branch_merged,
    build_dag,
    resolve_satisfied_dependencies,
)
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.runner import (
    _build_intake_agent_caller,
    _make_worker_phase_fn,
    _refresh_external_satisfied,
    _run_baseline_gate,
    _run_intake_remediation_pass,
    _terminal_story_model,
    run_sprint,
)
from theforge.task import TaskStory


def _make_story(slug: str, depends_on: list[str] | None = None) -> TaskStory:
    return TaskStory(
        name=slug,
        slug=slug,
        story_path=f"specs/{slug}.md",
        depends_on=depends_on or [],
    )


def _make_runner_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
            base_branch="main",
        ),
        validation=replace(DEFAULT_VALIDATION, gate_command="make gate"),
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_empty_resolved() -> ResolvedSprint:
    return ResolvedSprint(name="Test Sprint", budget_usd=10.0, stories=[], max_parallel=1)


def _make_result_with_dev_runs(*dev_results: AgentResult) -> CoordinatorResult:
    state = CoordinatorState()
    state.dev_results.extend(dev_results)
    return CoordinatorResult(
        success=True,
        phase=Phase.DONE,
        state=state,
        message="done",
    )


def test_build_intake_agent_caller_uses_configured_runner(tmp_path: Path) -> None:
    config = _make_runner_config(tmp_path)
    with (
        patch("theforge.sprint.runner.check_agent_auth", return_value=(True, "")),
        patch("theforge.sprint.runner.run_agent") as mock_run_agent,
        patch("theforge.sprint.runner.log_agent_result"),
    ):
        mock_run_agent.return_value = AgentResult(
            success=True,
            output="```markdown\n## What\n\nRewritten.\n```",
            session_id=None,
            cost_usd=0.12,
            exit_code=0,
            raw={},
            profile_name="intake-remediation",
            model_used="sonnet",
            transport_used="cli",
        )
        caller, detail = _build_intake_agent_caller(config=config, log=lambda *_: None)
        assert detail == ""
        assert caller is not None
        result = caller("old body", [], [])

    assert result.replacement == "## What\n\nRewritten."
    assert result.model_used == "sonnet"
    assert result.cost_usd == 0.12
    assert result.transport_used == "cli"


def test_run_intake_remediation_pass_records_missing_caller_reason(tmp_path: Path) -> None:
    config = _make_runner_config(tmp_path)
    config = replace(
        config,
        intake=IntakeConfig(grooming=True, auto_fix=True, auto_fix_mode="edit"),
    )
    task = TaskStory(name="Issue 7", slug="issue-7", github_issue=7)

    with (
        patch(
            "theforge.sprint.runner._build_intake_agent_caller",
            return_value=(None, "auth missing"),
        ),
        patch("theforge.sprint.runner.run_intake_remediation") as mock_run,
    ):
        mock_run.return_value = {"issue-7": MagicMock()}
        _run_intake_remediation_pass(config=config, tasks=[task], log=lambda *_: None)

    assert mock_run.call_args.kwargs["agent_caller"] is None
    assert mock_run.call_args.kwargs["missing_agent_detail"] == "auth missing"


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


def test_build_dag_external_issue_dep_blocks_without_error() -> None:
    """An external GitHub issue slug can block until a live state refresh satisfies it."""
    stories = [_make_story("issue-831", depends_on=["issue-807"])]
    dag = build_dag(stories)

    assert dag.ready() == []
    assert dag.unmet_deps("issue-831") == ["issue-807"]


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


def test_resolve_satisfied_dependencies_closed_issue(tmp_path: Path) -> None:
    """A CLOSED external issue dependency is treated as satisfied."""
    stories = [_make_story("issue-831", depends_on=["issue-807"])]
    with (
        patch("theforge.sprint.dag._is_branch_merged", return_value=False),
        patch("theforge.sprint.dag._is_issue_closed", return_value=True) as is_issue_closed,
    ):
        satisfied = resolve_satisfied_dependencies(
            stories,
            project_root=tmp_path,
            base_branch="main",
            branch_pattern="forge/{slug}",
        )

    assert "issue-807" in satisfied
    is_issue_closed.assert_called_once_with(807, tmp_path)


def test_scheduler_tick_unblocks_on_issue_close(tmp_path: Path) -> None:
    """A live scheduler refresh marks a newly CLOSED external issue dep satisfied."""
    from types import SimpleNamespace

    ready = _make_story("issue-749")
    blocked = _make_story("issue-751", depends_on=["issue-750"])
    all_tasks = [ready, blocked]
    dag = build_dag(all_tasks)
    config = SimpleNamespace(
        project_root=tmp_path,
        workspace=SimpleNamespace(base_branch="main", branch_pattern="forge/{slug}"),
    )
    issue_closed = False
    merged_slugs: set[str] = set()

    def _mock_issue_closed(issue_number: int, project_root: Path) -> bool:
        assert issue_number == 750
        assert project_root == tmp_path
        return issue_closed

    with (
        patch("theforge.sprint.dag._is_branch_merged", return_value=False),
        patch("theforge.sprint.dag._is_issue_closed", side_effect=_mock_issue_closed),
    ):
        assert {task.slug for task in dag.ready()} == {"issue-749"}
        assert _refresh_external_satisfied(dag, all_tasks, config, merged_slugs) == set()
        assert {task.slug for task in dag.ready()} == {"issue-749"}

        issue_closed = True
        assert _refresh_external_satisfied(dag, all_tasks, config, merged_slugs) == {"issue-750"}

    assert "issue-750" in merged_slugs
    assert {task.slug for task in dag.ready()} == {"issue-749", "issue-751"}
    assert not dag.is_done()


def test_run_sprint_pulls_base_branch_before_baseline_by_default(tmp_path: Path) -> None:
    config = _make_runner_config(tmp_path)
    resolved = _make_empty_resolved()
    call_order: list[str] = []

    def _fake_pull(_config: ForgeConfig, *, auto_merge: bool = False) -> None:
        call_order.append("pull")

    def _fake_baseline(_config: ForgeConfig, _resolved: ResolvedSprint) -> dict[str, object]:
        call_order.append("baseline")
        return {"passed": True, "message": "ok"}

    with (
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._get_or_create_sprint_id", return_value=None),
        patch("theforge.sprint.runner._project_root_is_git_checkout", return_value=True),
        patch(
            "theforge.coordinator.workspace.pull_base_branch",
            side_effect=_fake_pull,
        ) as mock_pull,
        patch("theforge.sprint.runner._run_baseline_gate", side_effect=_fake_baseline),
        patch("theforge.sprint.runner.resolve_satisfied_dependencies", return_value=set()),
        patch(
            "theforge.sprint.runner.normalize_dependency_plan",
            return_value=SimpleNamespace(tasks=[], blocked={}),
        ),
        patch(
            "theforge.sprint.runner.run_batch_preflight",
            side_effect=RuntimeError("stop after baseline"),
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after baseline"):
            run_sprint(config, resolved)

    assert call_order == ["pull", "baseline"]
    mock_pull.assert_called_once_with(config, auto_merge=False)


def test_run_sprint_no_pull_skips_prebaseline_pull(tmp_path: Path) -> None:
    config = _make_runner_config(tmp_path)
    resolved = _make_empty_resolved()
    call_order: list[str] = []

    def _fake_baseline(_config: ForgeConfig, _resolved: ResolvedSprint) -> dict[str, object]:
        call_order.append("baseline")
        return {"passed": True, "message": "ok"}

    with (
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._get_or_create_sprint_id", return_value=None),
        patch("theforge.coordinator.workspace.pull_base_branch") as mock_pull,
        patch("theforge.sprint.runner._run_baseline_gate", side_effect=_fake_baseline),
        patch("theforge.sprint.runner.resolve_satisfied_dependencies", return_value=set()),
        patch(
            "theforge.sprint.runner.normalize_dependency_plan",
            return_value=SimpleNamespace(tasks=[], blocked={}),
        ),
        patch(
            "theforge.sprint.runner.run_batch_preflight",
            side_effect=RuntimeError("stop after baseline"),
        ),
    ):
        with pytest.raises(RuntimeError, match="stop after baseline"):
            run_sprint(config, resolved, no_pull=True)

    assert call_order == ["baseline"]
    mock_pull.assert_not_called()


def test_run_sprint_pull_base_branch_failure_aborts_before_baseline(tmp_path: Path) -> None:
    config = _make_runner_config(tmp_path)
    resolved = _make_empty_resolved()

    with (
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._get_or_create_sprint_id", return_value=None),
        patch("theforge.sprint.runner._project_root_is_git_checkout", return_value=True),
        patch(
            "theforge.coordinator.workspace.pull_base_branch",
            side_effect=RuntimeError("WORKSPACE abort: pull failed"),
        ),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_baseline,
    ):
        with pytest.raises(RuntimeError, match="WORKSPACE abort: pull failed"):
            run_sprint(config, resolved)

    mock_baseline.assert_not_called()


def test_terminal_story_model_prefers_last_dev_model_used() -> None:
    first = AgentResult(
        success=True,
        output="",
        session_id=None,
        cost_usd=0.1,
        exit_code=0,
        raw={},
        model_used="sonnet",
    )
    last = AgentResult(
        success=True,
        output="",
        session_id=None,
        cost_usd=0.2,
        exit_code=0,
        raw={},
        model_used="opus",
    )

    result = _make_result_with_dev_runs(first, last)

    assert _terminal_story_model(result) == "opus"


def test_terminal_story_model_falls_back_to_model_usage_when_model_used_missing() -> None:
    legacy = AgentResult(
        success=True,
        output="",
        session_id=None,
        cost_usd=0.1,
        exit_code=0,
        raw={},
        model_usage=(
            ModelUsage(
                model="gpt-5.4",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                cost_usd=0.1,
            ),
        ),
    )

    result = _make_result_with_dev_runs(legacy)

    assert _terminal_story_model(result) == "gpt-5.4"


def test_worker_phase_fn_flushes_cost_updates_without_phase_transition() -> None:
    worker_phases: dict[str, str] = {}
    state_writer = MagicMock()
    outer_fn = MagicMock()

    class _Lock:
        def __enter__(self) -> None:
            return None

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    update = _make_worker_phase_fn(
        "story-a",
        worker_phases,
        _Lock(),  # type: ignore[arg-type]
        outer_fn,
        state_writer=state_writer,
    )

    update(
        {
            "cost_usd": 0.42,
            "current_model": "opus",
            "detail": {"heartbeat": "tick"},
        }
    )

    assert worker_phases == {}
    state_writer.update.assert_called_once_with(
        "story-a",
        cost_usd=0.42,
        current_model="opus",
        detail={"heartbeat": "tick"},
    )
    outer_fn.assert_called_once()


def test_run_baseline_gate_reports_local_origin_sha_gap(tmp_path: Path) -> None:
    config = _make_runner_config(tmp_path)
    resolved = _make_empty_resolved()
    top_level = str(tmp_path)

    def _completed_process(
        returncode: int,
        *,
        stdout: str = "",
        stderr: str = "",
    ) -> MagicMock:
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return proc

    run_calls = [
        _completed_process(0, stdout=".git\n"),
        _completed_process(0, stdout="abc123def456\n"),
        _completed_process(0, stdout=f"{top_level}\n"),
        _completed_process(0),
        _completed_process(0),
    ]

    with (
        patch("theforge.sprint.runner.subprocess.run", side_effect=run_calls),
        patch(
            "theforge.sprint.runner.run_gate_full",
            return_value=("FAIL", "Gate returned FAIL", "tail", "make gate", 1),
        ),
        patch(
            "theforge.sprint.runner.subprocess.check_output",
            side_effect=[
                "1111111111111111111111111111111111111111\n",
                "2222222222222222222222222222222222222222\n",
            ],
        ),
    ):
        baseline = _run_baseline_gate(config, resolved)

    assert baseline["passed"] is False
    assert "local main is at 111111111111" in str(baseline["message"])
    assert "origin is at 222222222222" in str(baseline["message"])
    assert "omit --no-pull" in str(baseline["message"])


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


def test_is_branch_merged_squash_merge_with_audit_approve(tmp_path: Path) -> None:
    """Squash merges still resolve via audit trail when git topology is not ancestor."""
    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_git_not_ancestor),
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
        if cmd[:2] == ["git", "rev-list"] and cmd[2] == "forge/story-a..main":
            m.stdout = b"3"  # base is 3 commits ahead of branch
        elif cmd[:2] == ["git", "rev-list"] and cmd[2] == "main..forge/story-a":
            m.stdout = b"2"  # branch had unique commits before merge
        else:
            m.stdout = b""
        return m

    with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_regular):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is True


def test_is_branch_merged_regular_merge_falls_back_to_audit(tmp_path: Path) -> None:
    """Regular merge fallback: ahead > 0, unique == 0, audit APPROVE → True."""

    def _mock_regular_fallback(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        if cmd[:2] == ["git", "rev-list"] and cmd[2] == "forge/story-a..main":
            m.stdout = b"3"
        elif cmd[:2] == ["git", "rev-list"] and cmd[2] == "main..forge/story-a":
            m.stdout = b"0"
        else:
            m.stdout = b""
        return m

    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_regular_fallback),
        patch("theforge.sprint.dag.has_review_approve", return_value=True),
    ):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is True


def test_is_branch_merged_stale_empty_branch(tmp_path: Path) -> None:
    """Base moving past an empty branch must not count as merged."""

    def _mock_stale(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        if cmd[:2] == ["git", "rev-list"] and cmd[2] == "forge/story-a..main":
            m.stdout = b"3"  # base advanced beyond the stale branch tip
        elif cmd[:2] == ["git", "rev-list"] and cmd[2] == "main..forge/story-a":
            m.stdout = b"0"  # branch never had unique commits
        else:
            m.stdout = b""
        return m

    with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_stale):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is False


def test_is_branch_merged_not_ancestor_without_audit(tmp_path: Path) -> None:
    """Branch not an ancestor of base with no APPROVE audit stays unmerged."""
    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_git_not_ancestor),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
    ):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is False


def test_is_branch_merged_external_squash_merge_by_issue_commit(tmp_path: Path) -> None:
    """A GitHub squash commit referencing the issue counts even without audit."""

    def _mock_external_squash(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif cmd[:2] == ["git", "log"] and "--grep=(#265)" in cmd:
            m.returncode = 0
            m.stdout = b"abc123\n"
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_external_squash),
        patch("theforge.sprint.dag._is_issue_closed", return_value=True),
    ):
        result = _is_branch_merged("feat/issue-265", "main", tmp_path)
    assert result is True


def test_is_branch_merged_external_squash_merge_by_merged_pr_lookup(tmp_path: Path) -> None:
    """A merged PR for the issue branch counts even without audit or commit grep."""

    def _mock_external_pr(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif cmd[:2] == ["git", "log"] and "--grep=(#1102)" in cmd:
            m.returncode = 0
            m.stdout = b""
        elif cmd[:3] == ["gh", "pr", "list"]:
            m.returncode = 0
            m.stdout = (
                '[{"number":1111,"url":"https://github.com/o/r/pull/1111",'
                '"mergedAt":"2026-05-01T12:34:56Z"}]'
            )
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_external_pr),
        patch("theforge.sprint.dag._is_issue_closed", return_value=True),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
    ):
        result = _is_branch_merged("feat/issue-1102", "main", tmp_path, slug="issue-1102")
    assert result is True


def test_is_branch_merged_issue_branch_without_base_commit_or_audit(tmp_path: Path) -> None:
    """Issue branch stays unmerged when base has no matching squash commit."""

    def _mock_no_external_squash(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif cmd[:2] == ["git", "log"] and "--grep=(#265)" in cmd:
            m.returncode = 0
            m.stdout = b""
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_no_external_squash),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
    ):
        result = _is_branch_merged("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert result is False


def test_is_branch_merged_issue_branch_open_issue_blocks_merge_evidence(tmp_path: Path) -> None:
    """Open GitHub issues stay eligible even when audit or git hints look merged."""

    def _mock_external_squash(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif cmd[:2] == ["git", "log"] and "--grep=(#265)" in cmd:
            m.returncode = 0
            m.stdout = b"abc123\n"
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_external_squash),
        patch("theforge.sprint.dag._is_issue_closed", return_value=False),
    ):
        result = _is_branch_merged("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert result is False


def test_run_sprint_summary_records_run_log(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from theforge.sprint.audit import _write_sprint_summary

    manifest = SimpleNamespace(name="demo-sprint", budget_usd=12.5, max_parallel=1)
    state = SimpleNamespace(
        preflight_verdict="PROCEED",
        review_results=[],
        total_cost=1.25,
        error=None,
        error_type=None,
    )
    result = SimpleNamespace(
        results=[
            (
                "issue:123",
                SimpleNamespace(
                    state=state, phase=SimpleNamespace(name="DONE"), success=True, merge=None
                ),
            )
        ],
        total_cost_usd=1.25,
        specs_total=1,
        specs_succeeded=1,
        specs_failed=0,
        specs_skipped=0,
        stopped_reason=None,
    )
    started_at = finished_at = __import__("datetime").datetime(
        2024, 1, 1, tzinfo=__import__("datetime").timezone.utc
    )
    sprint_log_dir = tmp_path / ".forge" / "logs" / "demo-sprint"

    _write_sprint_summary(
        manifest=manifest,
        result=result,
        canonical_refs=["issue:123"],
        started_at=started_at,
        finished_at=finished_at,
        duration=0.0,
        sprint_log_dir=sprint_log_dir,
        slug_map={"issue:123": "story-123"},
        run_id="run-abc",
    )

    summary = __import__("yaml").safe_load((sprint_log_dir / "sprint-summary.yaml").read_text())
    assert summary["sprint"]["run_id"] == "run-abc"
    assert summary["sprint"]["run_log"] == "run-run-abc.log"


def test_is_branch_merged_squash_merge_reads_real_audit_history(tmp_path: Path) -> None:
    """Squash merges use persisted APPROVE history even though branch stays ahead."""
    from theforge.coordinator import audit_substrate

    audit_substrate.seed_records(
        tmp_path,
        [
            {
                "run_id": "rec-landed",
                "task": {"slug": "story-a"},
                "landing_status": "landed",
                "reviews": [{"verdict": "APPROVE"}],
            }
        ],
    )

    def _mock_squash(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif cmd[:2] == ["git", "rev-list"] and cmd[2] == "main..forge/story-a":
            m.returncode = 0
            m.stdout = b"2"
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_squash):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is True


def test_is_branch_merged_squash_merge_ignores_failed_landing_audit(tmp_path: Path) -> None:
    """Failed landing history must not satisfy squash-merge detection."""
    from theforge.coordinator import audit_substrate

    audit_substrate.seed_records(
        tmp_path,
        [
            {
                "run_id": "rec-failed",
                "task": {"slug": "story-a"},
                "landing_status": "failed",
                "reviews": [{"verdict": "APPROVE"}],
            }
        ],
    )

    def _mock_squash(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif cmd[:2] == ["git", "rev-list"] and cmd[2] == "main..forge/story-a":
            m.returncode = 0
            m.stdout = b"2"
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_squash):
        result = _is_branch_merged("forge/story-a", "main", tmp_path, slug="story-a")
    assert result is False
