"""Tests for sprint DAG satisfied-dependency handling and runner seams.

Covers the case where a depends_on slug references a story already merged to
main (not present in the current sprint manifest).
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from landing_evidence_test_helpers import publish_landed
from sprint_test_helpers import run_sprint_ctx

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
from theforge.sprint.budget_runtime import SprintCostSnapshot
from theforge.sprint.dag import (
    StoryDAG,
    _branch_adds_content_to_base,
    _branch_merge_evidence,
    _has_base_commit_closing_issue,
    _is_branch_merged,
    build_dag,
    resolve_satisfied_dependencies,
)
from theforge.sprint.manifest import ResolvedSprint
from theforge.sprint.runner import (
    SprintExecutionState,
    SprintRunContext,
    SprintStop,
    SprintStopCondition,
    _build_intake_agent_caller,
    _make_worker_phase_fn,
    _refresh_external_satisfied,
    _run_baseline_gate,
    _run_intake_remediation_pass,
    _terminal_story_model,
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


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


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

    def _fake_pull(_config: ForgeConfig, *, lands_locally: bool | None = None) -> None:
        call_order.append("pull")

    def _fake_baseline(
        _config: ForgeConfig, _resolved: ResolvedSprint, **_kwargs: object
    ) -> dict[str, object]:
        call_order.append("baseline")
        return {"passed": True, "message": "ok"}

    with (
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._get_or_create_sprint_id", return_value=None),
        patch("theforge.sprint.runner._project_root_is_git_checkout", return_value=True),
        patch("theforge.coordinator.workspace.assert_base_branch_checked_out"),
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
            run_sprint_ctx(config, resolved)

    assert call_order == ["pull", "baseline"]
    mock_pull.assert_called_once_with(config, lands_locally=False)


def test_run_sprint_no_pull_skips_prebaseline_pull(tmp_path: Path) -> None:
    config = _make_runner_config(tmp_path)
    resolved = _make_empty_resolved()
    call_order: list[str] = []

    def _fake_baseline(
        _config: ForgeConfig, _resolved: ResolvedSprint, **_kwargs: object
    ) -> dict[str, object]:
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
            run_sprint_ctx(config, resolved, no_pull=True)

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
        patch("theforge.coordinator.workspace.assert_base_branch_checked_out"),
        patch(
            "theforge.coordinator.workspace.pull_base_branch",
            side_effect=RuntimeError("WORKSPACE abort: pull failed"),
        ),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_baseline,
    ):
        with pytest.raises(RuntimeError, match="WORKSPACE abort: pull failed"):
            run_sprint_ctx(config, resolved)

    mock_baseline.assert_not_called()


def test_run_sprint_refuses_launch_when_project_root_is_on_the_wrong_branch(
    tmp_path: Path,
) -> None:
    config = _make_runner_config(tmp_path)
    resolved = _make_empty_resolved()
    _git(tmp_path, "init", "--initial-branch", "feature/wrong-branch")
    _git(tmp_path, "config", "user.email", "forge@example.com")
    _git(tmp_path, "config", "user.name", "Forge Test")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "seed")

    with (
        patch("theforge.sprint.runner.enforce_sprint_auth_readiness"),
        patch("theforge.sprint.runner._scrub_root_forge_artifacts"),
        patch("theforge.sprint.runner.sweep_orphan_worktrees"),
        patch("theforge.sprint.runner._get_or_create_sprint_id", return_value=None),
        patch("theforge.sprint.runner._run_baseline_gate") as mock_baseline,
    ):
        with pytest.raises(RuntimeError) as excinfo:
            run_sprint_ctx(config, resolved)

    message = str(excinfo.value)
    assert "main" in message
    assert "feature/wrong-branch" in message
    assert "Check out main and rerun" in message
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


def _is_issue_grep(cmd: list[str], issue_number: int) -> bool:
    """True when ``cmd`` is the base-commit scan for ``issue_number``.

    Matches on the presence of a ``--grep=`` argument mentioning the issue
    rather than on its exact spelling: the prefilter pattern is an
    implementation detail, and pinning it here would make these mocks agree
    with a prefilter that no longer matches what git is actually asked.
    """
    return cmd[:2] == ["git", "log"] and any(
        c.startswith("--grep=") and str(issue_number) in c for c in cmd
    )


def _mock_base_commit(message: bytes, issue_number: int = 265):
    """Git mock: branch is not an ancestor, base carries ``message`` for the issue."""

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif _is_issue_grep(cmd, issue_number):
            m.returncode = 0
            m.stdout = message
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    return _run


def test_is_branch_merged_external_squash_merge_by_closing_reference(tmp_path: Path) -> None:
    """A GitHub squash commit that *closes* the issue counts even without audit."""

    with (
        patch(
            "theforge.sprint.dag.subprocess.run",
            side_effect=_mock_base_commit(b"fix(sprint): tighten evidence\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.sprint.dag._is_issue_closed", return_value=True),
    ):
        result = _is_branch_merged("feat/issue-265", "main", tmp_path)
    assert result is True


def test_is_branch_merged_bare_issue_mention_is_not_merge_evidence(tmp_path: Path) -> None:
    """A commit that merely mentions the issue is not evidence its branch merged (#2374).

    The commit below is the shape that caused the bug: a configuration change
    citing an unrelated open issue as context for a separate decision.
    """

    with (
        patch(
            "theforge.sprint.dag.subprocess.run",
            side_effect=_mock_base_commit(
                b"config: raise timeout and disable model\n\nDisabled pending #265\n\x1e"
            ),
        ),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
    ):
        evidence = _branch_merge_evidence("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.merged is False
    assert evidence.source is None


def test_is_branch_merged_closing_reference_for_other_issue_does_not_match(
    tmp_path: Path,
) -> None:
    """``Closes #2650`` must not satisfy issue 265 — the digits are a prefix."""

    with (
        patch(
            "theforge.sprint.dag.subprocess.run",
            side_effect=_mock_base_commit(b"fix: other thing\n\nCloses #2650\n\x1e"),
        ),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
    ):
        evidence = _branch_merge_evidence("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.merged is False


def _git(path: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _seed_repo(path: Path) -> None:
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("seed\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "seed")


def _squash_merged_repo(path: Path, base_message: str) -> None:
    """Real squash merge: branch survives with unique commits, work is on base.

    This is the shape ``git merge-base --is-ancestor`` fails on while
    ``git rev-list main..branch`` is non-zero — topologically indistinguishable
    from an unmerged branch, which is why this evidence source exists.
    """
    _seed_repo(path)
    _git(path, "checkout", "-q", "-b", "feat/issue-265")
    for i in (1, 2):
        (path / f"w{i}.txt").write_text(f"work {i}\n")
        _git(path, "add", ".")
        _git(path, "commit", "-q", "-m", f"wip {i}")
    _git(path, "checkout", "-q", "main")
    _git(path, "merge", "-q", "--squash", "feat/issue-265")
    _git(path, "commit", "-q", "-m", base_message)


def _unmerged_repo(path: Path, base_message: str) -> None:
    """The reported shape: five preserved commits, none of that work on base."""
    _seed_repo(path)
    _git(path, "checkout", "-q", "-b", "feat/issue-265")
    for i in range(1, 6):
        (path / f"w{i}.txt").write_text(f"work {i}\n")
        _git(path, "add", ".")
        _git(path, "commit", "-q", "-m", f"wip {i}")
    _git(path, "checkout", "-q", "main")
    (path / "config.yml").write_text("timeout: 30\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", base_message)


def _real_git_evidence(tmp_path: Path):
    """Resolve merge evidence against real git, with only external deps mocked."""
    with (
        patch("theforge.sprint.dag._lookup_merged_pr_for_branch", return_value=None),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
    ):
        return _branch_merge_evidence("feat/issue-265", "main", tmp_path, slug="issue-265")


def test_real_squash_merge_with_closing_reference_is_merged(tmp_path: Path) -> None:
    """A real squash merge is detected even though the branch still has unique commits.

    Regression for the cycle-2 P1: vetoing on non-ancestor-plus-unique-commits
    suppressed every genuine squash merge whose source branch still existed,
    which is exactly what the issue-commit source is for. Topology cannot tell
    this repo from the unmerged one below; only content can.
    """
    _squash_merged_repo(tmp_path, "feat: the thing (#900)\n\nCloses #265")

    evidence = _real_git_evidence(tmp_path)

    assert evidence.merged is True
    assert evidence.source == "issue_commit"


def test_real_unmerged_branch_with_closing_reference_is_not_merged(tmp_path: Path) -> None:
    """A closing reference does not land work that is absent from base.

    Same topology as the squash repo above — non-ancestor, unique commits — but
    the branch's files are genuinely not on base, so the content veto fires.
    """
    _unmerged_repo(tmp_path, "chore: sweep\n\nCloses #265")

    evidence = _real_git_evidence(tmp_path)

    assert evidence.merged is False


def test_real_unmerged_branch_with_bare_mention_is_not_merged(tmp_path: Path) -> None:
    """The reported incident, end to end against real git (#2374)."""
    _unmerged_repo(
        tmp_path,
        "config: raise timeout, disable model\n\nDisabled pending #265",
    )

    evidence = _real_git_evidence(tmp_path)

    assert evidence.merged is False


def test_merge_tree_refusal_on_stdout_is_not_read_as_a_conflict(tmp_path: Path) -> None:
    """Exit 1 with a non-oid first line is a refusal, not a conflict.

    Today git writes "not something we can merge" to stderr and leaves stdout
    empty, so this shape cannot be produced from a real repo — it is mocked
    deliberately. It guards the branch of the exit-1 check that the real-git
    deleted-branch test cannot reach.
    """

    def _mock_refusal(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if cmd[:2] == ["git", "merge-tree"]:
            m.returncode = 1
            m.stdout = b"merge-tree: nosuch - not something we can merge\n"
        else:
            m.returncode = 0
            m.stdout = b""
        return m

    with patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_refusal):
        assert _branch_adds_content_to_base(tmp_path, "main", "feat/issue-265") is False


def _conflicting_repo(path: Path, base_message: str) -> None:
    """Unmerged branch that edits the same lines base edits, so replay conflicts."""
    _seed_repo(path)
    (path / "f.txt").write_text("one\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "add f")
    _git(path, "checkout", "-q", "-b", "feat/issue-265")
    (path / "f.txt").write_text("branch side\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "branch edit")
    _git(path, "checkout", "-q", "main")
    (path / "f.txt").write_text("base side\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", base_message)


def test_real_conflicting_unmerged_branch_is_not_merged(tmp_path: Path) -> None:
    """A conflict is content evidence of divergence, not an inconclusive result.

    Regression for the cycle-3 P1: treating every non-zero ``merge-tree`` exit
    as "cannot determine" let a conflicting, definitively unmerged branch be
    skipped on the strength of a closing reference in a base commit.
    """
    _conflicting_repo(tmp_path, "fix: base side wins\n\nCloses #265")

    evidence = _real_git_evidence(tmp_path)

    assert evidence.merged is False


def test_real_deleted_branch_with_closing_reference_is_merged(tmp_path: Path) -> None:
    """A missing branch must not be mistaken for a conflict.

    ``merge-tree`` reports an unknown ref with exit 1 — the same status as a
    conflict — so only the tree-oid check separates them. An externally merged
    branch that was then deleted has no evidence left but the closing
    reference, and vetoing it here would discard that entirely.
    """
    _seed_repo(tmp_path)
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "feat: landed\n\nCloses #265")

    evidence = _real_git_evidence(tmp_path)

    assert evidence.merged is True
    assert evidence.source == "issue_commit"


def test_real_squash_merge_vetoed_by_unsuccessful_audit(tmp_path: Path) -> None:
    """A recorded unsuccessful run with nothing landed still outranks the text.

    Content alone would accept this repo — the work *is* on base — so this pins
    the audit veto specifically, not the content check.
    """
    _squash_merged_repo(tmp_path, "feat: the thing (#900)\n\nCloses #265")
    from theforge.coordinator import audit_substrate

    audit_substrate.seed_records(
        tmp_path,
        [
            {
                "task": {"slug": "issue-265"},
                "run_id": "sr-265",
                "landing_status": "",
                "outcome": {"success": False},
                "reviews": [{"verdict": "REQUEST_CHANGES"}],
            }
        ],
    )

    evidence = _real_git_evidence(tmp_path)

    assert evidence.merged is False


def test_branch_merge_evidence_unsuccessful_audit_vetoes_closing_reference(
    tmp_path: Path,
) -> None:
    """An unsuccessful last run with nothing landed outranks a textual match (#2374)."""

    with (
        patch(
            "theforge.sprint.dag.subprocess.run",
            side_effect=_mock_base_commit(b"chore: sweep\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
        patch(
            "theforge.sprint.dag.latest_run_outcome",
            return_value={
                "outcome_success": 0,
                "verdict": "REQUEST_CHANGES",
                "landing_status": "",
            },
        ),
    ):
        evidence = _branch_merge_evidence("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.merged is False


def test_branch_merge_evidence_landed_audit_does_not_veto_closing_reference(
    tmp_path: Path,
) -> None:
    """A landed prior run is not a contradiction, so the closing reference stands."""

    with (
        patch(
            "theforge.sprint.dag.subprocess.run",
            side_effect=_mock_base_commit(b"chore: sweep\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
        patch(
            "theforge.sprint.dag.latest_run_outcome",
            return_value={
                "outcome_success": 1,
                "verdict": "APPROVE",
                "landing_status": "landed",
            },
        ),
    ):
        evidence = _branch_merge_evidence("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.merged is True
    assert evidence.source == "issue_commit"


def _init_repo_with_commit(path: Path, message: str) -> None:
    """Create a real single-branch git repo whose HEAD commit carries ``message``."""
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)


@pytest.mark.parametrize(
    ("message", "issue_number", "expected"),
    [
        # Every spelling the matcher advertises must survive the git prefilter.
        ("fix: land it\n\nCloses #265", 265, True),
        ("fix: land it\n\nCloses GH-265", 265, True),
        ("fix: land it\n\ncloses gh-265", 265, True),
        ("fix: land it\n\nResolves owner/repo#265", 265, True),
        ("fix: land it\n\nFixes: #265", 265, True),
        # Bare mentions are not evidence.
        ("config: disable model\n\nDisabled pending #265", 265, False),
        ("feat: thing (#265)", 265, False),
        # Adjacent issue numbers must not collide.
        ("fix: other\n\nCloses #2650", 265, False),
        ("fix: other\n\nCloses GH-2650", 265, False),
    ],
)
def test_has_base_commit_closing_issue_against_real_git(
    tmp_path: Path, message: str, issue_number: int, expected: bool
) -> None:
    """Exercise the real ``git log`` prefilter, not a mock of it (#2374).

    The prefilter and the Python matcher are two separate patterns over the
    same message. A mocked ``subprocess.run`` cannot catch them disagreeing —
    which is exactly how ``Closes GH-N`` came to be advertised by the matcher
    while the ``--fixed-strings --grep=#N`` prefilter dropped it before the
    matcher ever ran.
    """
    _init_repo_with_commit(tmp_path, message)
    assert _has_base_commit_closing_issue(tmp_path, "main", issue_number) is expected


def test_branch_merge_evidence_audit_veto_reads_real_substrate(tmp_path: Path) -> None:
    """The audit veto works end-to-end against a seeded substrate, not just a mock.

    Topology here is squash-merge-shaped (not an ancestor, no unique commits) so
    it cannot veto on its own — the recorded unsuccessful outcome must.
    """
    from theforge.coordinator import audit_substrate

    audit_substrate.seed_records(
        tmp_path,
        [
            {
                "task": {"slug": "issue-265"},
                "run_id": "sr-265",
                "landing_status": "",
                "outcome": {"success": False},
                "reviews": [{"verdict": "REQUEST_CHANGES"}],
            }
        ],
    )

    with (
        patch(
            "theforge.sprint.dag.subprocess.run",
            side_effect=_mock_base_commit(b"chore: sweep\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
    ):
        evidence = _branch_merge_evidence("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.merged is False


def test_branch_merge_evidence_audit_read_failure_does_not_veto(tmp_path: Path) -> None:
    """An unreadable audit has no opinion — it must not invert into a veto."""

    with (
        patch(
            "theforge.sprint.dag.subprocess.run",
            side_effect=_mock_base_commit(b"chore: sweep\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
        patch(
            "theforge.sprint.dag.latest_run_outcome",
            side_effect=OSError("audit unreadable"),
        ),
    ):
        evidence = _branch_merge_evidence("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert evidence.merged is True


def test_is_branch_merged_external_squash_merge_by_merged_pr_lookup(tmp_path: Path) -> None:
    """A merged PR for the issue branch counts even without audit or commit grep."""

    def _mock_external_pr(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "--is-ancestor" in cmd:
            m.returncode = 1
            m.stdout = b""
        elif _is_issue_grep(cmd, 1102):
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
        elif _is_issue_grep(cmd, 265):
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


def test_is_branch_merged_open_issue_does_not_block_merge_evidence(tmp_path: Path) -> None:
    """An open GitHub issue no longer suppresses merge evidence (#2111).

    Symptom bugs are deliberately held open pending verification after their fix
    lands, so issue state is not a precondition for detecting the merge.
    """

    with (
        patch(
            "theforge.sprint.dag.subprocess.run",
            side_effect=_mock_base_commit(b"fix: land it\n\nCloses #265\n\x1e"),
        ),
        patch("theforge.sprint.dag._is_issue_closed", return_value=False),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
    ):
        result = _is_branch_merged("feat/issue-265", "main", tmp_path, slug="issue-265")
    assert result is True


def test_branch_merge_evidence_never_consults_issue_state(tmp_path: Path) -> None:
    """Issue state is not a gate on branch merge detection at all (#2111)."""

    def _mock_no_evidence(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 1
        m.stdout = b""
        return m

    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_no_evidence),
        patch("theforge.sprint.dag.has_review_approve", return_value=False),
        patch("theforge.sprint.dag._is_issue_closed") as is_issue_closed,
    ):
        evidence = _branch_merge_evidence("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert evidence.merged is False
    is_issue_closed.assert_not_called()


def test_branch_merge_evidence_prefers_owned_audit_over_issue_commit(tmp_path: Path) -> None:
    """Owned audit evidence is consulted before the loose issue-commit grep."""

    grepped: list[list[str]] = []

    def _mock_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if cmd[:2] == ["git", "log"] and any(c.startswith("--grep=") for c in cmd):
            grepped.append(cmd)
        m.returncode = 1
        m.stdout = b""
        return m

    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run),
        patch("theforge.sprint.dag.has_review_approve", return_value=True),
    ):
        evidence = _branch_merge_evidence("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert evidence.merged is True
    assert evidence.source == "audit"
    assert grepped == []


def test_branch_merge_evidence_audit_error_falls_through_to_pr_lookup(tmp_path: Path) -> None:
    """A transient audit-read failure must not discard the merged-PR fallback."""

    def _mock_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if cmd[:3] == ["gh", "pr", "list"]:
            m.returncode = 0
            m.stdout = (
                '[{"number":1111,"url":"https://github.com/o/r/pull/1111",'
                '"mergedAt":"2026-05-01T12:34:56Z"}]'
            )
        else:
            m.returncode = 1
            m.stdout = b""
        return m

    with (
        patch("theforge.sprint.dag.subprocess.run", side_effect=_mock_run),
        patch("theforge.sprint.dag.has_review_approve", side_effect=OSError("audit unreadable")),
    ):
        evidence = _branch_merge_evidence("feat/issue-265", "main", tmp_path, slug="issue-265")

    assert evidence.merged is True
    assert evidence.source == "github_pr"
    assert evidence.pr_number == 1111


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
    # Since #2849 the landed query reads the published assertion, not the
    # flattened column above.
    publish_landed(tmp_path, "rec-landed", slug="story-a")

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


# ── Sprint execution state (#2325) ────────────────────────────────────
#
# ``run_sprint`` used to hold its execution state in a stack frame nothing
# could name: no caller could construct it, no test could assert against it
# without running a whole sprint, and three closures advanced it through
# ``nonlocal``. These cover the named replacement — that it is constructible
# and assertable on its own, and that cost and stop each have exactly one
# owner that no shared-variable assignment can go around.


def _run_context(**overrides) -> SprintRunContext:
    """A context for a sprint that is never dispatched."""
    resolved = ResolvedSprint(name="state-unit", budget_usd=25.0, max_parallel=1, stories=[])
    kwargs = {
        "config": SimpleNamespace(project_root=Path("/nonexistent")),
        "resolved": resolved,
        "sprint_id": "sprint-abc",
        "run_id": "run-abc",
    }
    kwargs.update(overrides)
    return SprintRunContext(**kwargs)  # type: ignore[arg-type]


class TestSprintExecutionStateConstructability:
    """The state a sprint holds is reachable by name, without a sprint."""

    def test_state_constructs_from_a_context_alone(self) -> None:
        state = SprintExecutionState(context=_run_context())

        assert state.context.sprint_id == "sprint-abc"
        assert state.context.budget_usd == 25.0
        assert state.context.name == "state-unit"
        assert state.cost.snapshot() == SprintCostSnapshot(
            accumulated=0.0, prior=0.0, unmeasured=(), current_generation_unmeasured=frozenset()
        )
        assert state.stop.record is None
        assert state.merged_slugs == set()
        assert state.results == []
        assert state.batch_number == 0
        assert state.stories.counts() == {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
        }

    def test_two_states_do_not_share_their_owners(self) -> None:
        """Defaults are per-instance, so one sprint cannot spend another's."""
        first = SprintExecutionState(context=_run_context())
        second = SprintExecutionState(context=_run_context())

        first.cost.add(3.0)
        first.stop.stop("first stopped")
        first.merged_slugs.add("story-a")

        assert second.cost.accumulated == 0.0
        assert second.stop.record is None
        assert second.merged_slugs == set()

    def test_context_is_read_only(self) -> None:
        """What the sprint consults cannot be rebound by anything reading it."""
        ctx = _run_context()

        with pytest.raises(FrozenInstanceError):
            ctx.auto_merge = True  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            ctx.run_id = "somewhere-else"  # type: ignore[misc]

    def test_cost_snapshot_is_read_only(self) -> None:
        snapshot = SprintExecutionState(context=_run_context()).cost.snapshot()

        with pytest.raises(FrozenInstanceError):
            snapshot.accumulated = 99.0  # type: ignore[misc]


class TestSprintStopConditionOwnership:
    """The sprint stop condition has exactly one owner."""

    def test_stop_records_reason_and_halt_slug_together(self) -> None:
        stop = SprintStopCondition()
        assert stop.stopped is False
        assert stop.reason is None
        assert stop.halt_slug is None

        stop.stop("Required CI checks fail after merging story-a", halt_slug="story-a")

        assert stop.stopped is True
        assert stop.record == SprintStop(
            reason="Required CI checks fail after merging story-a", halt_slug="story-a"
        )
        assert stop.reason == "Required CI checks fail after merging story-a"
        assert stop.halt_slug == "story-a"

    def test_a_stop_without_a_halt_slug_records_none(self) -> None:
        stop = SprintStopCondition()
        stop.stop("budget exhausted")

        assert stop.record == SprintStop(reason="budget exhausted", halt_slug=None)

    def test_the_stop_is_not_settable(self) -> None:
        stop = SprintStopCondition()
        stop.stop("budget exhausted")

        with pytest.raises(AttributeError):
            stop.reason = "something else"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            stop.halt_slug = "story-z"  # type: ignore[misc]

        assert stop.reason == "budget exhausted"

    def test_stop_if_unset_is_first_writer_wins(self) -> None:
        """A downstream consequence must not overwrite the original cause."""
        stop = SprintStopCondition()

        assert stop.stop_if_unset("agent authentication failed") is True
        assert stop.stop_if_unset("budget exhausted") is False
        assert stop.reason == "agent authentication failed"

    def test_stop_replaces_an_earlier_reason(self) -> None:
        """The CI halt is authoritative and names its own story."""
        stop = SprintStopCondition()
        stop.stop_if_unset("budget exhausted")

        stop.stop("Required CI checks fail after merging story-a", halt_slug="story-a")

        assert stop.reason == "Required CI checks fail after merging story-a"
        assert stop.halt_slug == "story-a"

    def test_the_stop_record_is_read_only(self) -> None:
        stop = SprintStopCondition()
        record = stop.stop("budget exhausted", halt_slug="story-a")

        with pytest.raises(FrozenInstanceError):
            record.reason = "rewritten"  # type: ignore[misc]

    def test_only_one_concurrent_caller_owns_the_stop(self) -> None:
        stop = SprintStopCondition()
        barrier = threading.Barrier(8)
        winners: list[int] = []
        lock = threading.Lock()

        def _halt(index: int) -> None:
            barrier.wait()
            if stop.stop_if_unset(f"halted by {index}", halt_slug=f"story-{index}"):
                with lock:
                    winners.append(index)

        threads = [threading.Thread(target=_halt, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(winners) == 1
        assert stop.record == SprintStop(
            reason=f"halted by {winners[0]}", halt_slug=f"story-{winners[0]}"
        )
