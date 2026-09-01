"""Tests for parallel sprint execution: max_parallel, DAG, worker exception handling."""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from sprint_test_helpers import run_sprint_ctx

from theforge.config import (
    DEFAULT_DEV_PROFILE,
    DEFAULT_PREFLIGHT_PROFILE,
    DEFAULT_REVIEW_PROFILE,
    DEFAULT_VALIDATION,
    ForgeConfig,
    RetryPolicy,
    SprintConfig,
    WorkspaceConfig,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.review import ReviewResult
from theforge.sprint import load_sprint_manifest
from theforge.sprint import runner as _runner
from theforge.sprint.ci_checks import PrCheckState
from theforge.sprint.dag import StoryDAG, build_dag
from theforge.sprint.lock import integration_lock
from theforge.sprint.runner import _classify_and_record, _run_fresh
from theforge.task import TaskStory

# ── Helpers ──────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> ForgeConfig:
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            preflight_complexity_gate_threshold=11,
        ),
    )


def _make_config_with_sprint(tmp_path: Path, sprint_max_parallel: int = 1) -> ForgeConfig:
    """Build a ForgeConfig with a custom sprint.max_parallel default."""
    return ForgeConfig(
        project="test",
        project_root=tmp_path,
        workspace=WorkspaceConfig(
            create_command="mkdir -p {slug}",
            path_pattern="{slug}",
            branch_pattern="forge/{slug}",
        ),
        validation=DEFAULT_VALIDATION,
        dev_profile=DEFAULT_DEV_PROFILE,
        preflight_profile=DEFAULT_PREFLIGHT_PROFILE,
        review_pool=[DEFAULT_REVIEW_PROFILE],
        synthesis_profile=None,
        retry=RetryPolicy(
            max_dev_iterations=2,
            max_review_cycles=2,
            preflight_complexity_gate_threshold=11,
        ),
        sprint=SprintConfig(max_parallel=sprint_max_parallel),
    )


def _make_spec_file(
    tmp_path: Path, name: str, slug: str, depends_on: list[str] | None = None
) -> Path:
    spec = tmp_path / f"{slug}.md"
    frontmatter = f"name: {name}\nslug: {slug}"
    if depends_on is not None:
        if len(depends_on) == 1:
            frontmatter += f"\ndepends_on: {depends_on[0]}"
        else:
            frontmatter += "\ndepends_on:\n" + "".join(f"  - {d}\n" for d in depends_on)
    spec.write_text(
        f"---\n{frontmatter}\n---\n# {name}\nDo the thing.",
        encoding="utf-8",
    )
    return spec


def _make_task(
    slug: str, depends_on: list[str] | None = None, tmp_path: Path | None = None
) -> TaskStory:
    path = (tmp_path or Path("/tmp")) / f"{slug}.md"
    return TaskStory(
        name=slug.replace("-", " ").title(),
        story_path=path,
        slug=slug,
        depends_on=depends_on or [],
    )


def _make_manifest_parallel(
    tmp_path: Path, specs: list[str], budget: float = 10.0, max_parallel: int = 1
) -> Path:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        yaml.dump(
            {
                "name": "Parallel Sprint",
                "budget_usd": budget,
                "stories": specs,
                "max_parallel": max_parallel,
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _make_coordinator_result(
    success: bool = True,
    cost: float = 1.0,
    preflight_verdict: str = "PROCEED",
    phase: Phase = Phase.DONE,
    merged: bool = False,
    landing_status: str | None = None,
) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = preflight_verdict
    # Fake cost via preflight result mock
    mock_preflight = MagicMock()
    mock_preflight.cost_usd = cost
    state.preflight_result = mock_preflight
    return CoordinatorResult(
        success=success,
        phase=phase,
        state=state,
        message="Done." if success else "Failed.",
        merge={"merged": True} if merged else None,
        landing_status=landing_status,
    )


# ── max_parallel manifest parsing ────────────────────────────────────────────


class TestMaxParallelManifest:
    def test_parses_max_parallel(self, tmp_path: Path) -> None:
        """load_sprint_manifest parses max_parallel=3 correctly."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "stories": ["a.md"], "max_parallel": 3}),
            encoding="utf-8",
        )
        manifest = load_sprint_manifest(path)
        assert manifest.max_parallel == 3

    def test_defaults_to_none_when_absent(self, tmp_path: Path) -> None:
        """max_parallel is None (sentinel) when not specified in manifest."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "stories": ["a.md"]}),
            encoding="utf-8",
        )
        manifest = load_sprint_manifest(path)
        assert manifest.max_parallel is None

    def test_rejects_max_parallel_zero(self, tmp_path: Path) -> None:
        """max_parallel=0 is rejected with ValueError."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "stories": ["a.md"], "max_parallel": 0}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="max_parallel"):
            load_sprint_manifest(path)

    def test_rejects_max_parallel_negative(self, tmp_path: Path) -> None:
        """max_parallel=-1 is rejected with ValueError."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            yaml.dump({"name": "X", "budget_usd": 5.0, "stories": ["a.md"], "max_parallel": -1}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="max_parallel"):
            load_sprint_manifest(path)

    def test_rejects_max_parallel_non_integer(self, tmp_path: Path) -> None:
        """max_parallel as float string is rejected."""
        path = tmp_path / "sprint.yaml"
        path.write_text(
            'name: X\nbudget_usd: 5.0\nstories: [a.md]\nmax_parallel: "2"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="max_parallel"):
            load_sprint_manifest(path)


class TestMaxParallelPrecedence:
    """Tests for manifest vs forge.yaml max_parallel precedence."""

    def _make_manifest(self, tmp_path: Path, max_parallel: int | None = None) -> Path:
        data: dict = {"name": "X", "budget_usd": 5.0, "stories": ["story-a.md"]}
        if max_parallel is not None:
            data["max_parallel"] = max_parallel
        path = tmp_path / "sprint.yaml"
        path.write_text(yaml.dump(data), encoding="utf-8")
        return path

    def test_manifest_wins_over_config_default(self, tmp_path: Path) -> None:
        """Manifest max_parallel=2 overrides config default of 3."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = self._make_manifest(tmp_path, max_parallel=2)
        config = _make_config_with_sprint(tmp_path, sprint_max_parallel=3)

        with patch("theforge.sprint.runner.run_task") as mock_run:
            mock_run.return_value = _make_coordinator_result(success=True)
            run_sprint_ctx(config, manifest_path)

        manifest = load_sprint_manifest(manifest_path)
        assert manifest.max_parallel == 2  # unchanged after load (not None)

    def test_config_default_used_when_manifest_omits(self, tmp_path: Path) -> None:
        """When manifest omits max_parallel, config default (3) is used in run_sprint."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = self._make_manifest(tmp_path, max_parallel=None)
        config = _make_config_with_sprint(tmp_path, sprint_max_parallel=3)

        with patch("theforge.sprint.runner.run_task") as mock_run:
            mock_run.return_value = _make_coordinator_result(success=True)
            result = run_sprint_ctx(config, manifest_path)

        assert result.specs_succeeded == 1

    def test_neither_set_defaults_to_1(self, tmp_path: Path) -> None:
        """When neither manifest nor config set max_parallel, default is 1."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = self._make_manifest(tmp_path, max_parallel=None)
        config = _make_config(tmp_path)  # SprintConfig defaults to max_parallel=1

        with patch("theforge.sprint.runner.run_task") as mock_run:
            mock_run.return_value = _make_coordinator_result(success=True)
            run_sprint_ctx(config, manifest_path)

        assert config.sprint.max_parallel == 1

    def test_manifest_none_is_resolved_in_run_sprint(self, tmp_path: Path) -> None:
        """After load_sprint_manifest, max_parallel is None; run_sprint resolves it."""
        manifest_path = self._make_manifest(tmp_path, max_parallel=None)
        manifest = load_sprint_manifest(manifest_path)
        assert manifest.max_parallel is None


# ── StoryDAG unit tests ───────────────────────────────────────────────────────


class TestStoryDAGUnit:
    def test_no_dep_stories_immediately_ready(self) -> None:
        """Stories with no depends_on are ready immediately."""
        a = _make_task("a")
        b = _make_task("b")
        dag = StoryDAG([a, b])
        ready_slugs = {t.slug for t in dag.ready()}
        assert ready_slugs == {"a", "b"}

    def test_marking_complete_unlocks_dependent(self) -> None:
        """Marking a story complete makes its dependents ready."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])

        assert {t.slug for t in dag.ready()} == {"a"}
        dag.mark_complete("a")
        assert {t.slug for t in dag.ready()} == {"b"}

    def test_marking_skipped_does_not_unlock_dependent(self) -> None:
        """Marking a story skipped does NOT make its dependents ready."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])

        dag.mark_skipped("a")
        assert dag.ready() == []

    def test_is_done_false_until_all_finished(self) -> None:
        """is_done() is False until every task has been finished."""
        a = _make_task("a")
        b = _make_task("b")
        dag = StoryDAG([a, b])

        assert not dag.is_done()
        dag.mark_complete("a")
        assert not dag.is_done()
        dag.mark_skipped("b")
        assert dag.is_done()

    def test_is_done_true_with_mix_of_complete_and_skipped(self) -> None:
        """is_done() considers both completed and skipped as finished."""
        tasks = [_make_task(s) for s in ["a", "b", "c"]]
        dag = StoryDAG(tasks)
        dag.mark_complete("a")
        dag.mark_skipped("b")
        dag.mark_skipped("c")
        assert dag.is_done()

    def test_unmet_deps_returns_missing_completed_deps(self) -> None:
        """unmet_deps returns deps not yet completed."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a", "c"])
        dag = StoryDAG([a, b])
        dag.mark_complete("a")
        assert dag.unmet_deps("b") == ["c"]

    def test_remaining_excludes_finished(self) -> None:
        """remaining() returns only tasks not yet finished."""
        tasks = [_make_task(s) for s in ["a", "b", "c"]]
        dag = StoryDAG(tasks)
        dag.mark_complete("a")
        dag.mark_skipped("b")
        remaining_slugs = {t.slug for t in dag.remaining()}
        assert remaining_slugs == {"c"}

    def test_multi_dep_unlocked_only_when_all_complete(self) -> None:
        """Story with multiple deps only becomes ready when ALL deps complete."""
        a = _make_task("a")
        b = _make_task("b")
        c = _make_task("c", depends_on=["a", "b"])
        dag = StoryDAG([a, b, c])

        dag.mark_complete("a")
        assert "c" not in {t.slug for t in dag.ready()}
        dag.mark_complete("b")
        assert "c" in {t.slug for t in dag.ready()}

    def test_build_dag_returns_story_dag(self) -> None:
        """build_dag() returns a properly initialized StoryDAG."""
        a = _make_task("a")
        dag = build_dag([a])
        assert isinstance(dag, StoryDAG)
        assert {t.slug for t in dag.ready()} == {"a"}


# ── Parallel sprint execution tests ──────────────────────────────────────────


class TestParallelIndependentStories:
    def test_parallel_3_independent_all_succeed(self, tmp_path: Path) -> None:
        """max_parallel=3, 3 independent stories — all run, all counted."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        _make_spec_file(tmp_path, "Story C", "story-c")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md", "story-c.md"],
            budget=10.0,
            max_parallel=3,
        )
        config = _make_config(tmp_path)

        results = [
            _make_coordinator_result(success=True, cost=1.0),
            _make_coordinator_result(success=True, cost=1.0),
            _make_coordinator_result(success=True, cost=1.0),
        ]

        with patch("theforge.sprint.runner.run_task", side_effect=results) as mock_run:
            sprint = run_sprint_ctx(config, manifest_path)

        assert mock_run.call_count == 3
        assert sprint.specs_succeeded == 3
        assert sprint.specs_failed == 0
        assert sprint.specs_skipped == 0
        assert sprint.total_cost_usd == pytest.approx(3.0)
        # outcome: done requires no failures and no early-stop reason
        assert sprint.stopped_reason is None

    def test_parallel_one_fail_others_complete(self, tmp_path: Path) -> None:
        """One story fails — the other two still run and are counted correctly."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        _make_spec_file(tmp_path, "Story C", "story-c")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md", "story-c.md"],
            budget=10.0,
            max_parallel=3,
        )
        config = _make_config(tmp_path)

        results = [
            _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE),
            _make_coordinator_result(success=True, cost=1.0),
            _make_coordinator_result(success=True, cost=1.0),
        ]

        with patch("theforge.sprint.runner.run_task", side_effect=results) as mock_run:
            sprint = run_sprint_ctx(config, manifest_path)

        assert mock_run.call_count == 3
        assert sprint.specs_succeeded == 2
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 0
        assert sprint.total_cost_usd == pytest.approx(3.0)

    def test_parallel_auto_merge_false_in_worker(self, tmp_path: Path) -> None:
        """With max_parallel>1, workers run with auto_merge=False."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        results = [
            _make_coordinator_result(success=True, cost=1.0),
            _make_coordinator_result(success=True, cost=1.0),
        ]

        with patch("theforge.sprint.runner.run_task", side_effect=results) as mock_run:
            run_sprint_ctx(config, manifest_path, auto_merge=True)

        # In parallel mode, workers get auto_merge=False; merging is done in main thread
        for call in mock_run.call_args_list:
            assert call.kwargs["auto_merge"] is False


class TestParallelDependencyGating:
    def test_dependency_blocks_until_predecessor_completes(self, tmp_path: Path) -> None:
        """Story B is skipped when A's landing fails — landing failure is now fatal."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        # A succeeds but deferred merge fails → A=failed, B=skipped (dep unmet)
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            sprint = run_sprint_ctx(config, manifest_path)

        assert mock_run.call_count == 1  # only A ran
        assert sprint.specs_succeeded == 0
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 1

    def test_dependency_satisfied_by_merge_unlocks_dependent(self, tmp_path: Path) -> None:
        """Story B runs after A lands (in max_parallel=1 mode with eager merge)."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")
        result_b = _make_coordinator_result(success=True, cost=1.0)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert mock_run.call_count == 2
        assert sprint.specs_succeeded == 2

    def test_failed_dep_causes_dependent_to_skip(self, tmp_path: Path) -> None:
        """When A fails, B (dep: A) is marked skipped."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            sprint = run_sprint_ctx(config, manifest_path)

        assert mock_run.call_count == 1
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 1

    def test_collision_dependent_waits_for_queued_parent_before_dispatch(
        self, tmp_path: Path
    ) -> None:
        """Issue #1609: a collision-linked dependent must honor the queued-PR
        reachability gate on every dispatch path.

        The collision DAG links story-b to story-a via ``collision_deps`` (a
        soft edge). dag.ready() releases that soft edge the instant story-a
        reaches a terminal state — and a merge-queued parent is marked terminal
        (pending_integration -> mark_skipped) the moment its PR is queued, well
        before the merge commit is reachable on origin base. Before the fix, the
        dispatch loop's queued-PR gate only inspected ``depends_on``, so the
        dependent was dispatched onto a stale base while the parent's PR still
        sat in the auto-merge queue, then conflicted at merge time (MERGE_FAILED).

        This test stubs a queued-but-unmerged parent and asserts the dependent
        does not dispatch (reach REVIEW) until the queued PR poll reports the
        merge reachable, and that no MERGE_FAILED results from a stale base.
        """
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        # story-a: approved, deferred merge queued (pending_integration).
        result_a = _make_coordinator_result(success=True, cost=1.0)
        result_a.landing_status = "pending_integration"
        result_a.merge = {"action": "merge", "pending": True}
        # story-b: clean run once dispatched.
        result_b = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

        run_order: list[str] = []

        def _fake_run_task(cfg, task, **kwargs):  # noqa: ANN001
            run_order.append(task.slug)
            return {"story-a": result_a, "story-b": result_b}[task.slug]

        def _fake_land_story(*args, **kwargs):  # noqa: ANN002, ANN003
            return (
                {"merge_queued": True, "pr_url": "https://github.com/x/y/pull/1"},
                "pending_integration",
            )

        def _fake_poll(*args, **kwargs):  # noqa: ANN002, ANN003
            # The dependent must not have been dispatched while the parent's PR
            # was still queued-unmerged.
            assert "story-b" not in run_order
            run_order.append("poll")
            return {"status": "merged"}

        with (
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch(
                "theforge.sprint.runner.compute_synthetic_edges",
                return_value={"story-b": ["story-a"]},
            ),
            patch("theforge.coordinator.completion.land_story", side_effect=_fake_land_story),
            patch("theforge.sprint.runner._poll_queued_pr", side_effect=_fake_poll),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        # The queued-PR poll gated the dependent: it dispatched only after the
        # parent's merge became reachable.
        assert run_order == ["story-a", "poll", "story-b"]
        # No stale-base MERGE_FAILED; both stories succeed.
        assert sprint.specs_failed == 0
        assert sprint.specs_succeeded == 2

    def test_collision_dependent_proceeds_when_parent_abandoned(self, tmp_path: Path) -> None:
        """Abandon-and-proceed is preserved: a collision parent that reaches a
        terminal-but-not-merged state (never queued) releases the dependent.

        The #1609 gate keys strictly on ``queued_prs`` membership, so a genuinely
        failed collision parent — which never enters the queue — does not block
        the dependent. This guards against the gate over-blocking legitimate
        soft-edge release.
        """
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)
        result_b = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

        def _fake_run_task(cfg, task, **kwargs):  # noqa: ANN001
            return {"story-a": result_a, "story-b": result_b}[task.slug]

        with (
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch(
                "theforge.sprint.runner.compute_synthetic_edges",
                return_value={"story-b": ["story-a"]},
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        # story-a failed (never queued); the soft edge still releases story-b.
        assert sprint.specs_failed == 1
        assert sprint.specs_succeeded == 1
        assert sprint.specs_skipped == 0

    def test_collision_dependent_runs_on_current_base_when_queued_parent_times_out(
        self, tmp_path: Path
    ) -> None:
        """Issue #1792: a collision (soft-edge) dependent must be dispatched onto
        the current base when its queued-PR parent times out into MERGE_FAILED.

        The collision DAG links story-b to story-a via ``collision_deps`` (a soft
        edge whose sole purpose is to stop story-b from building on story-a's
        *unmerged* changes). story-a is approved and its PR auto-merge-queued, but
        the merge never lands: ``_poll_queued_pr`` reports ``timeout``. Because
        story-a's changes never reached the base, the base is exactly the clean
        state the soft edge was guarding, so story-b must run there — not be
        marked SKIPPED(blocked) as it was before the fix.

        This exercises the ready-loop queued-PR gate: story-b becomes ready the
        instant story-a is queued (terminal-but-not-merged), the gate polls the
        parent's PR, sees the timeout, marks story-a MERGE_FAILED, and re-enters
        dispatch so story-b runs on the current base.
        """
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        # story-a: approved, deferred merge queued (pending_integration).
        result_a = _make_coordinator_result(success=True, cost=1.0)
        result_a.landing_status = "pending_integration"
        result_a.merge = {"action": "merge", "pending": True}
        # story-b: clean run once dispatched onto the current base.
        result_b = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

        run_order: list[str] = []

        def _fake_run_task(cfg, task, **kwargs):  # noqa: ANN001
            run_order.append(task.slug)
            return {"story-a": result_a, "story-b": result_b}[task.slug]

        def _fake_land_story(*args, **kwargs):  # noqa: ANN002, ANN003
            return (
                {"merge_queued": True, "pr_url": "https://github.com/x/y/pull/1"},
                "pending_integration",
            )

        def _fake_poll(*args, **kwargs):  # noqa: ANN002, ANN003
            run_order.append("poll")
            return {"status": "timeout"}

        with (
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch(
                "theforge.sprint.runner.compute_synthetic_edges",
                return_value={"story-b": ["story-a"]},
            ),
            patch("theforge.coordinator.completion.land_story", side_effect=_fake_land_story),
            patch("theforge.sprint.runner._poll_queued_pr", side_effect=_fake_poll),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        # The parent's queued PR timed out (MERGE_FAILED); the dependent was still
        # dispatched onto the current base and ran to success — not skipped.
        assert "story-b" in run_order
        assert run_order.index("story-b") > run_order.index("poll")
        assert sprint.specs_failed == 1  # story-a MERGE_FAILED
        assert sprint.specs_succeeded == 1  # story-b ran on the clean base
        assert sprint.specs_skipped == 0  # story-b was NOT stranded

    def test_hard_dependent_still_skipped_when_queued_parent_times_out(
        self, tmp_path: Path
    ) -> None:
        """Issue #1792 (soft/hard distinction): a genuine ``depends_on`` (hard)
        dependent of a queued parent that times out into MERGE_FAILED must stay
        SKIPPED — only a collision soft edge dissolves on parent failure.

        story-h needs story-p's *result*, so a terminal-but-not-merged story-p
        must propagate its failure to story-h. story-h is never ready while
        story-p is only queued (a hard dep requires the parent to be *completed*,
        which queuing does not provide), so the parent is polled from the
        no-active-workers branch; when it times out, story-p is marked MERGE_FAILED
        and story-h remains blocked and is skipped.
        """
        _make_spec_file(tmp_path, "Story P", "story-p")
        _make_spec_file(tmp_path, "Story H", "story-h", depends_on=["story-p"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-p.md", "story-h.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        # story-p: approved, deferred merge queued.
        result_p = _make_coordinator_result(success=True, cost=1.0)
        result_p.landing_status = "pending_integration"
        result_p.merge = {"action": "merge", "pending": True}
        result_h = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

        run_order: list[str] = []

        def _fake_run_task(cfg, task, **kwargs):  # noqa: ANN001
            run_order.append(task.slug)
            return {"story-p": result_p, "story-h": result_h}[task.slug]

        def _fake_land_story(*args, **kwargs):  # noqa: ANN002, ANN003
            return (
                {"merge_queued": True, "pr_url": "https://github.com/x/y/pull/2"},
                "pending_integration",
            )

        def _fake_poll(*args, **kwargs):  # noqa: ANN002, ANN003
            run_order.append("poll")
            return {"status": "timeout"}

        with (
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch("theforge.coordinator.completion.land_story", side_effect=_fake_land_story),
            patch("theforge.sprint.runner._poll_queued_pr", side_effect=_fake_poll),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        # The hard dependent never ran; its parent's failure propagated as a skip.
        assert "story-h" not in run_order
        assert sprint.specs_failed == 1  # story-p MERGE_FAILED
        assert sprint.specs_skipped == 1  # story-h blocked by failed hard dep
        assert sprint.specs_succeeded == 0

    def test_resume_at_review_dependent_waits_for_queued_parent(self, tmp_path: Path) -> None:
        """Issue #1609 (incident path): a dependent re-entering via resume triage
        at REVIEW must honor the queued-parent reachability gate.

        This reproduces the exact production shape from run c3ae5c757180: the
        dependent (story-b) re-enters through ``_triage_spec`` action=review with
        an existing worktree -- dispatched via ``run_from_review``, NOT a fresh
        ``run_task`` -- while its collision parent (story-a) sits in the auto-merge
        queue but has not yet merged. The resumed dependent must be held behind
        ``_poll_queued_pr`` until the parent's merge is reachable on origin base,
        and must not be carried stale into a MERGE_FAILED integration.

        Before the fix, the scheduler's queued-PR gate inspected only
        ``depends_on``; the soft collision edge released the instant story-a was
        marked terminal (pending_integration), so ``run_from_review`` for story-b
        fired while story-a's PR was still queued.
        """
        from theforge.sprint.dag import StoryTriage

        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        # Both resumed stories have existing worktrees predating the parent merge.
        wt_a = tmp_path / "story-a"
        wt_b = tmp_path / "story-b"
        wt_a.mkdir(exist_ok=True)
        wt_b.mkdir(exist_ok=True)

        # story-a: approved on re-entry, deferred merge queued (pending_integration).
        result_a = _make_coordinator_result(success=True, cost=1.0)
        result_a.landing_status = "pending_integration"
        result_a.merge = {"action": "merge", "pending": True}
        # story-b: clean review re-entry once actually dispatched.
        result_b = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

        def _fake_triage(story_path, cfg, project_root, *, task=None, **_progress):  # noqa: ANN001
            slug = task.slug
            return StoryTriage(
                story_path=story_path,
                action="review",
                reason="worktree exists, gate passes",
                worktree_path=wt_a if slug == "story-a" else wt_b,
                slug=slug,
            )

        # Ordered record of resume-path dispatches (run_from_review) and the poll.
        dispatch_order: list[str] = []

        def _fake_run_from_review(cfg, task, worktree_path, **kwargs):  # noqa: ANN001
            dispatch_order.append(task.slug)
            return {"story-a": result_a, "story-b": result_b}[task.slug]

        def _fake_batch_run_task(cfg, task, **kwargs):  # noqa: ANN001
            # Batch preflight probe -- collision edges are forced below, so the
            # returned state content is irrelevant.
            return _make_coordinator_result(success=True, cost=0.0)

        def _fake_land_story(*args, **kwargs):  # noqa: ANN002, ANN003
            return (
                {"merge_queued": True, "pr_url": "https://github.com/x/y/pull/1"},
                "pending_integration",
            )

        def _fake_poll(*args, **kwargs):  # noqa: ANN002, ANN003
            # The resumed dependent must not have reached run_from_review while
            # the parent's queued PR was still unmerged.
            assert "story-b" not in dispatch_order
            dispatch_order.append("poll")
            return {"status": "merged"}

        with (
            patch("theforge.sprint.runner._triage_spec", side_effect=_fake_triage),
            patch("theforge.sprint.runner.run_from_review", side_effect=_fake_run_from_review),
            patch("theforge.sprint.collision.run_task", side_effect=_fake_batch_run_task),
            patch(
                "theforge.sprint.runner.compute_synthetic_edges",
                return_value={"story-b": ["story-a"]},
            ),
            patch("theforge.coordinator.completion.land_story", side_effect=_fake_land_story),
            patch("theforge.sprint.runner._poll_queued_pr", side_effect=_fake_poll),
        ):
            sprint = run_sprint_ctx(config, manifest_path, resume=True)

        # story-a re-entered review and queued; the queued-PR poll gated story-b;
        # only after the poll reported the parent merge reachable did story-b
        # re-enter run_from_review.
        assert dispatch_order == ["story-a", "poll", "story-b"]
        # No stale-base MERGE_FAILED; both resumed stories succeed.
        assert sprint.specs_failed == 0
        assert sprint.specs_succeeded == 2


class TestParallelBudgetPooling:
    def test_budget_pooled_across_workers(self, tmp_path: Path) -> None:
        """Third story is skipped once accumulated cost reaches budget.

        With max_parallel=2, A and B are submitted simultaneously.  Each costs
        1.0 against a budget of 1.0.  After the first story completes the
        accumulated cost is >= budget, so C is budget-skipped on the next
        scheduling pass regardless of whether B has also finished yet.
        """
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        _make_spec_file(tmp_path, "Story C", "story-c")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md", "story-c.md"],
            budget=1.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        # A and B each cost 1.0 against budget=1.0.  After at least one
        # completes, accumulated_cost >= 1.0 so C is budget-skipped.
        result_a = _make_coordinator_result(success=True, cost=1.0)
        result_b = _make_coordinator_result(success=True, cost=1.0)

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            sprint = run_sprint_ctx(config, manifest_path)

        # C was budget-skipped; A and B both ran (both submitted before cost accumulated)
        assert mock_run.call_count == 2
        assert sprint.specs_skipped == 1
        assert sprint.stopped_reason is not None
        assert "budget" in sprint.stopped_reason.lower()

    def test_max_parallel_1_budget_sequential(self, tmp_path: Path) -> None:
        """With max_parallel=1, budget check works story-by-story."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=1.5,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.5)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            sprint = run_sprint_ctx(config, manifest_path)

        assert mock_run.call_count == 1
        assert sprint.specs_skipped == 1


class TestClassifyAndRecord:
    """Unit tests for _classify_and_record helper."""

    def test_success_no_merge_skips_for_dag(self) -> None:
        """Success without merge counts as finished but does not unlock dependents."""
        from theforge.sprint.story_state import StoryOutcome

        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(success=True, cost=1.0, merged=False)
        outcome = _classify_and_record(a, result, dag, merged_slugs)

        assert outcome is StoryOutcome.DONE
        assert "a" not in merged_slugs
        # A should not be re-dispatched forever, and B must remain blocked.
        assert dag.ready() == []

    def test_success_with_merge_completes_for_dag(self) -> None:
        """landing_status=landed → DONE, dag.mark_complete (unlocks deps)."""
        from theforge.sprint.story_state import StoryOutcome

        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")
        outcome = _classify_and_record(a, result, dag, merged_slugs)

        assert outcome is StoryOutcome.DONE
        assert "a" in merged_slugs
        # B now ready (a completed)
        assert {t.slug for t in dag.ready()} == {"b"}

    def test_already_done_completes_for_dag(self) -> None:
        """ALREADY_DONE → terminal succeeded outcome, dag.mark_complete."""
        from theforge.sprint.story_state import StoryOutcome

        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(
            success=True, cost=0.0, preflight_verdict="ALREADY_DONE", phase=Phase.DONE
        )
        outcome = _classify_and_record(a, result, dag, merged_slugs)

        assert outcome is StoryOutcome.ALREADY_DONE
        assert outcome.is_succeeded
        assert "a" in merged_slugs
        assert {t.slug for t in dag.ready()} == {"b"}

    def test_validate_already_complete_classifies_as_already_done(self) -> None:
        """state.validate_already_complete=True with success → ALREADY_DONE outcome.

        Regression test: when VALIDATE recognizes that the dev cycle correctly
        determined no work was needed (handoff documents existing commits as
        satisfying all ACs), sprint must classify the story as a successful
        ALREADY_DONE rather than FAILED, even though preflight returned PROCEED.
        """
        from theforge.sprint.story_state import StoryOutcome

        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(
            success=True, cost=0.5, preflight_verdict="PROCEED", phase=Phase.DONE
        )
        result.state.validate_already_complete = True
        result.state.validate_already_complete_commits = [
            {"sha": "a0a1319" + "0" * 33, "message": "fix: already-landed"}
        ]
        outcome = _classify_and_record(a, result, dag, merged_slugs)

        assert outcome is StoryOutcome.ALREADY_DONE
        assert outcome.is_succeeded
        assert "a" in merged_slugs
        assert {t.slug for t in dag.ready()} == {"b"}

    def test_failed_result_does_not_get_promoted_to_already_done(self) -> None:
        """An ALREADY_DONE preflight verdict must not mask a later failed result."""
        from theforge.sprint.story_state import StoryOutcome

        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(
            success=False,
            cost=0.0,
            preflight_verdict="ALREADY_DONE",
            phase=Phase.ESCALATE,
        )
        outcome = _classify_and_record(a, result, dag, merged_slugs)

        assert outcome is StoryOutcome.FAILED
        assert outcome.is_succeeded is False
        assert "a" not in merged_slugs
        assert dag.ready() == []

    def test_failure_skips_for_dag(self) -> None:
        """Failure → FAILED, dag.mark_skipped."""
        from theforge.sprint.story_state import StoryOutcome

        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)
        outcome = _classify_and_record(a, result, dag, merged_slugs)

        assert outcome is StoryOutcome.FAILED
        assert "a" not in merged_slugs
        assert dag.ready() == []

    def test_landing_failed_counts_as_delta_failed(self) -> None:
        """landing_status=failed is classified as MERGE_FAILED — a failed terminal state
        distinct from generic FAILED, so the audit trail can name the cause precisely."""
        from theforge.sprint.story_state import StoryOutcome

        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        # success=True but landing_status=failed — the merge step failed after approval.
        result = _make_coordinator_result(success=True, cost=1.0, landing_status="failed")
        outcome = _classify_and_record(a, result, dag, merged_slugs)

        assert outcome is StoryOutcome.MERGE_FAILED
        assert outcome.is_failed
        assert "a" not in merged_slugs
        # B must remain blocked: a failed to land, so it cannot satisfy a's dependency.
        assert dag.ready() == []

    def test_landing_pending_integration_does_not_mark_complete(self) -> None:
        """landing_status=pending_integration does not mark the story complete or unblock deps."""
        from theforge.sprint.story_state import StoryOutcome

        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(
            success=True, cost=1.0, landing_status="pending_integration"
        )
        outcome = _classify_and_record(a, result, dag, merged_slugs)

        assert outcome is StoryOutcome.DONE
        assert "a" not in merged_slugs
        # B must remain blocked: a is only queued, not landed.
        assert dag.ready() == []

    def test_landing_landed_marks_complete_and_unblocks_deps(self) -> None:
        """landing_status=landed is the only merge-pr state that satisfies dependency gating."""
        from theforge.sprint.story_state import StoryOutcome

        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        result = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")
        outcome = _classify_and_record(a, result, dag, merged_slugs)

        assert outcome is StoryOutcome.DONE
        assert "a" in merged_slugs
        # B is now ready because a landed.
        assert {t.slug for t in dag.ready()} == {"b"}

    def test_sequential_dep_blocked_while_pending_integration(self) -> None:
        """Story B with depends_on=[A] is not dispatched while A is pending_integration."""
        a = _make_task("a")
        b = _make_task("b", depends_on=["a"])
        dag = StoryDAG([a, b])
        merged_slugs: set[str] = set()

        # A completes with pending_integration (auto-merge queued, not yet landed).
        result_a = _make_coordinator_result(
            success=True, cost=1.0, landing_status="pending_integration"
        )
        _classify_and_record(a, result_a, dag, merged_slugs)

        # B must still be blocked — only a "landed" outcome satisfies the dependency.
        assert dag.ready() == [], "B must not become ready while A is pending_integration"

    def test_parallel_deps_both_blocked_until_predecessor_lands(self) -> None:
        """Two parallel stories (C, D) depending on A both remain blocked until A lands."""
        a = _make_task("a")
        c = _make_task("c", depends_on=["a"])
        d = _make_task("d", depends_on=["a"])
        dag = StoryDAG([a, c, d])
        merged_slugs: set[str] = set()

        # A finishes with pending_integration.
        result_a = _make_coordinator_result(
            success=True, cost=1.0, landing_status="pending_integration"
        )
        _classify_and_record(a, result_a, dag, merged_slugs)

        assert dag.ready() == [], "C and D must not become ready while A is pending_integration"

        # Simulate A landing (a separate queued-PR completion event would update the DAG;
        # here we directly call mark_complete to prove the gate opens).
        dag.mark_complete("a")
        ready_slugs = {t.slug for t in dag.ready()}
        assert ready_slugs == {"c", "d"}, "Both C and D become ready after A lands"


class TestMergeOrdering:
    def test_merge_ordering_deferred_parallel(self, tmp_path: Path) -> None:
        """auto_merge=True, max_parallel=2, B depends_on A: merge happens in dep order."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=1,  # sequential to keep test deterministic
        )
        config = _make_config(tmp_path)

        # A lands, then B has satisfied dep, can be merged
        result_a = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")
        result_b = _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

        with patch(
            "theforge.sprint.runner.run_task", side_effect=[result_a, result_b]
        ) as mock_run:
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert mock_run.call_count == 2
        assert sprint.specs_succeeded == 2


class TestMaxParallel1Fallback:
    def test_max_parallel_1_sequential_behavior(self, tmp_path: Path) -> None:
        """max_parallel=1 produces identical outcome to the original sequential runner."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        _make_spec_file(tmp_path, "Story C", "story-c")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md", "story-c.md"],
            budget=10.0,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        results = [
            _make_coordinator_result(success=True, cost=1.0),
            _make_coordinator_result(success=False, cost=0.5, phase=Phase.ESCALATE),
            _make_coordinator_result(success=True, cost=2.0),
        ]

        with patch("theforge.sprint.runner.run_task", side_effect=results) as mock_run:
            sprint = run_sprint_ctx(config, manifest_path)

        assert mock_run.call_count == 3
        assert sprint.specs_succeeded == 2
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 0
        assert sprint.total_cost_usd == pytest.approx(3.5)
        assert sprint.stopped_reason is None

    def test_max_parallel_1_dep_check_same_as_sequential(self, tmp_path: Path) -> None:
        """Dependency gating works correctly with max_parallel=1."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        # A succeeds but does NOT merge → B should be skipped (same as old behavior)
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)

        with patch("theforge.sprint.runner.run_task", side_effect=[result_a]) as mock_run:
            sprint = run_sprint_ctx(config, manifest_path)

        assert mock_run.call_count == 1
        assert sprint.specs_succeeded == 1
        assert sprint.specs_skipped == 1
        assert sprint.stopped_reason is None


# ── TestBuildDagValidation ────────────────────────────────────────────────────


class TestBuildDagValidation:
    def test_missing_dep_raises_value_error(self) -> None:
        """build_dag raises ValueError when depends_on references a slug not in the manifest."""
        a = _make_task("story-a")
        b = _make_task("story-b", depends_on=["nonexistent-slug"])
        with pytest.raises(ValueError, match="unknown slug"):
            build_dag([a, b])

    def test_circular_dependency_raises_value_error(self) -> None:
        """build_dag raises ValueError on circular dependency (A → B → A)."""
        a = _make_task("story-a", depends_on=["story-b"])
        b = _make_task("story-b", depends_on=["story-a"])
        with pytest.raises(ValueError, match="[Cc]ircular"):
            build_dag([a, b])

    def test_self_dependency_raises_value_error(self) -> None:
        """build_dag raises ValueError when a story depends on itself."""
        a = _make_task("story-a", depends_on=["story-a"])
        with pytest.raises(ValueError, match="[Cc]ircular"):
            build_dag([a])

    def test_valid_dag_no_error(self) -> None:
        """build_dag does not raise for a valid linear dependency chain."""
        a = _make_task("story-a")
        b = _make_task("story-b", depends_on=["story-a"])
        c = _make_task("story-c", depends_on=["story-b"])
        dag = build_dag([a, b, c])
        assert dag.ready() == [a]

    def test_three_way_cycle_raises_value_error(self) -> None:
        """build_dag raises ValueError for a 3-story cycle (A → B → C → A)."""
        a = _make_task("story-a", depends_on=["story-c"])
        b = _make_task("story-b", depends_on=["story-a"])
        c = _make_task("story-c", depends_on=["story-b"])
        with pytest.raises(ValueError, match="[Cc]ircular"):
            build_dag([a, b, c])


# ── TestWorkerExceptionHandling ───────────────────────────────────────────────


class TestWorkerExceptionHandling:
    def test_worker_exception_marks_story_failed_sprint_continues(self, tmp_path: Path) -> None:
        """Worker exception from run_task is caught; story is marked failed; sprint finishes."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        result_b = _make_coordinator_result(success=True, cost=1.0)

        with patch(
            "theforge.sprint.runner.run_task",
            side_effect=[RuntimeError("agent crashed"), result_b],
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        # A failed via exception → counted as failed; B still ran
        assert sprint.specs_failed == 1
        assert sprint.specs_succeeded == 1

        sprint_audit_path = tmp_path / ".forge" / "audits" / "sprint-audit.yaml"
        sprint_audit = yaml.safe_load(sprint_audit_path.read_text(encoding="utf-8")) or {}
        story_a_entry = next(
            entry for entry in sprint_audit["specs"] if entry["path"] == "story-a.md"
        )
        assert story_a_entry["outcome"] == "ESCALATE"
        assert story_a_entry["error"] == "Worker exception: agent crashed"
        assert story_a_entry["error_type"] == "RuntimeError"
        assert story_a_entry["started_at"] is not None
        assert story_a_entry["finished_at"] is not None

        durable_audit_path = (
            tmp_path / ".forge" / "logs" / "Parallel Sprint" / "story-a" / "audit.yaml"
        )
        durable_audit = yaml.safe_load(durable_audit_path.read_text(encoding="utf-8")) or {}
        assert durable_audit["outcome"]["final_phase"] == "ESCALATE"
        assert durable_audit["error"] == "Worker exception: agent crashed"
        assert durable_audit["error_type"] == "RuntimeError"
        assert durable_audit["timing"]["started_at"] is not None

    def test_worker_exception_dependent_is_skipped(self, tmp_path: Path) -> None:
        """When a worker raises, the story is marked skipped in the DAG so dependents skip too."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=1,
        )
        config = _make_config(tmp_path)

        with patch(
            "theforge.sprint.runner.run_task",
            side_effect=[RuntimeError("agent crashed")],
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        # A raises → failed; B dep-skipped (A never completed)
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 1


# ── TestParallelMergeOrderingParallelMode ─────────────────────────────────────


class TestParallelDependencySafety:
    def test_parallel_dep_without_auto_merge_runs_dependent_story_and_warns(
        self, tmp_path: Path, capsys
    ) -> None:
        """Parallel depends_on auto-enables deferred merges so dependents are not skipped."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)

        merge_calls: list[str] = []

        def _fake_merge(project_root, base_branch, branch, slug, wt, **kwargs):  # noqa: ANN001
            merge_calls.append(slug)
            return {"merged": True}

        with (
            patch("theforge.sprint.runner.run_task", side_effect=[result_a, result_b]) as mock_run,
            patch("theforge.coordinator.completion._merge_branch", side_effect=_fake_merge),
        ):
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=False)

        assert sprint.specs_succeeded == 2
        assert sprint.specs_failed == 0
        assert sprint.specs_skipped == 0
        assert mock_run.call_count == 2
        assert [call.args[1].slug for call in mock_run.call_args_list] == ["story-a", "story-b"]
        assert merge_calls == ["story-a"]

        captured = capsys.readouterr()
        assert "parallel dependency merging auto-enabled" in captured.err
        assert "story-a" in captured.err


class TestParallelMergeOrderingParallelMode:
    def test_merge_ordering_parallel_pending_merges(self, tmp_path: Path) -> None:
        """auto_merge=True, max_parallel=2, B depends_on A: _merge_branch called for A then B."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        # Both complete successfully (no real merge needed in test)
        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)
        # Simulate what _finalize_approve sets when auto_merge=True
        result_a.landing_status = "pending_integration"
        result_a.merge = {"action": "merge", "pending": True}
        result_b.landing_status = "pending_integration"
        result_b.merge = {"action": "merge", "pending": True}

        merge_calls: list[str] = []

        def _fake_merge(project_root, base_branch, branch, slug, wt, **kwargs):  # noqa: ANN001
            merge_calls.append(slug)
            return {"merged": True}

        with (
            patch("theforge.sprint.runner.run_task", side_effect=[result_a, result_b]),
            patch("theforge.coordinator.completion._merge_branch", side_effect=_fake_merge),
        ):
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert sprint.specs_succeeded == 2
        # A must be merged before B (dependency order)
        assert merge_calls.index("story-a") < merge_calls.index("story-b")

    def test_merge_pr_success_keeps_merge_true_when_force_push_refspec_missing(
        self, tmp_path: Path
    ) -> None:
        """Merged PRs stay landed when post-merge force-push sees a deleted remote branch."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        (tmp_path / "story-a").mkdir()
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = dataclasses.replace(
            _make_config(tmp_path),
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="merge-pr",
                auto_push=True,
            ),
        )

        state = CoordinatorState()
        state.log_dir = tmp_path / ".forge" / "logs" / "Parallel Sprint" / "story-a"
        state.preflight_verdict = "PROCEED"
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = 1.0
        state.preflight_result = mock_preflight
        state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"action": "merge-pr", "pending": True},
            landing_status="pending_integration",
        )

        merge_info = {
            "action": "merge-pr",
            "pr_url": "https://github.com/fuzzypete/theforge/pull/42",
            "merged": True,
            "merge_queued": False,
            "success": True,
            "error": None,
            "auto_merge_queued": False,
        }

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch("theforge.coordinator.completion._merge_pr", return_value=merge_info),
            patch(
                "theforge.sprint.runner.poll_required_checks",
                return_value={
                    "status": "pass",
                    "sha": "deadbeef",
                    "failing_checks": [],
                    "message": "ok",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert sprint.specs_succeeded == 1
        audit = yaml.safe_load(
            (
                tmp_path / ".forge" / "logs" / "Parallel Sprint" / "story-a" / "audit.yaml"
            ).read_text()
        )
        assert audit["merge"]["merged"] is True
        assert audit["landing_status"] == "landed"
        assert audit["error"] is None

    def test_merge_pr_failure_rewrites_story_audit(self, tmp_path: Path) -> None:
        """Landing failures set success=False and record failed integration in audit."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        (tmp_path / "story-a").mkdir()
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = dataclasses.replace(
            _make_config(tmp_path),
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="merge-pr",
                auto_push=True,
            ),
        )

        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = 1.0
        state.preflight_result = mock_preflight
        state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"action": "merge-pr", "pending": True},
            landing_status="pending_integration",
        )

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_pr",
                return_value={
                    "action": "merge-pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/273",
                    "merged": False,
                    "success": False,
                    "error": "gh pr merge failed: branch protection",
                },
            ),
            patch(
                "theforge.sprint.runner.poll_required_checks",
                return_value={
                    "status": "pass",
                    "sha": "deadbeef",
                    "failing_checks": [],
                    "message": "ok",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        audit_path = tmp_path / ".forge" / "logs" / "Parallel Sprint" / "story-a" / "audit.yaml"
        audit = yaml.safe_load(audit_path.read_text(encoding="utf-8")) or {}
        assert sprint.specs_succeeded == 0
        assert sprint.specs_failed == 1
        assert audit["outcome"]["success"] is False
        assert audit["outcome"]["final_phase"] == "MERGE_FAILED"
        assert audit["landing_status"] == "failed"
        assert audit["merge"]["action"] == "merge-pr"
        assert audit["merge"]["merged"] is False
        assert audit["error"] == "gh pr merge failed: branch protection"

    def test_merge_pr_success_writes_final_merge_metadata_to_audit(self, tmp_path: Path) -> None:
        """Deferred merge-pr success must write the final merge metadata to audit."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        (tmp_path / "story-a").mkdir()
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = dataclasses.replace(
            _make_config(tmp_path),
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="merge-pr",
                auto_push=True,
            ),
        )

        state = CoordinatorState()
        state.log_dir = tmp_path / ".forge" / "logs" / "Parallel Sprint" / "story-a"
        state.preflight_verdict = "PROCEED"
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = 1.0
        state.preflight_result = mock_preflight
        state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"action": "merge-pr", "pending": True},
            landing_status="pending_integration",
        )

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_pr",
                return_value={
                    "action": "merge-pr",
                    "pr_url": "https://github.com/fuzzypete/theforge/pull/273",
                    "merged": True,
                    "success": True,
                    "error": None,
                },
            ),
            patch(
                "theforge.sprint.runner.poll_required_checks",
                return_value={
                    "status": "pass",
                    "sha": "deadbeef",
                    "failing_checks": [],
                    "message": "ok",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        audit_path = tmp_path / ".forge" / "logs" / "Parallel Sprint" / "story-a" / "audit.yaml"
        audit = yaml.safe_load(audit_path.read_text(encoding="utf-8")) or {}
        assert sprint.specs_succeeded == 1
        assert sprint.specs_failed == 0
        assert audit["outcome"]["success"] is True
        assert audit["outcome"]["final_phase"] == "DONE"
        assert audit["merge"]["action"] == "merge-pr"
        assert audit["merge"]["merged"] is True

    def test_deferred_local_merge_failure_rewrites_story_audit(self, tmp_path: Path) -> None:
        """Local landing failures set success=False and record failed integration in audit."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        (tmp_path / "story-a").mkdir()
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        state = CoordinatorState()
        state.log_dir = tmp_path / ".forge" / "logs" / "Parallel Sprint" / "story-a"
        state.preflight_verdict = "PROCEED"
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = 1.0
        state.preflight_result = mock_preflight
        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"action": "merge", "pending": True},
            landing_status="pending_integration",
        )

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={
                    "action": "merge",
                    "merged": False,
                    "success": False,
                    "error": "git merge failed: conflict in src/foo.py",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=True)

        audit_path = tmp_path / ".forge" / "logs" / "Parallel Sprint" / "story-a" / "audit.yaml"
        audit = yaml.safe_load(audit_path.read_text(encoding="utf-8")) or {}
        assert sprint.specs_succeeded == 0
        assert sprint.specs_failed == 1
        assert audit["outcome"]["success"] is False
        assert audit["outcome"]["final_phase"] == "MERGE_FAILED"
        assert audit["landing_status"] == "failed"
        assert audit["merge"]["action"] == "merge"
        assert audit["merge"]["merged"] is False
        assert audit["error"] == "git merge failed: conflict in src/foo.py"


# ── Pre-pull behaviour in run_sprint ─────────────────────────────────


class TestSprintPrePull:
    """run_sprint_ctx() propagates no_pull to per-story workers without a shared pre-pull."""

    def _make_manifest(self, tmp_path: Path) -> Path:
        (tmp_path / "story-a.md").write_text("---\nname: Story A\nslug: story-a\n---\n# Story A\n")
        manifest_path = tmp_path / "sprint.yaml"
        manifest_path.write_text(
            "name: test-sprint\nbudget_usd: 10\nmax_parallel: 2\nstories:\n  - story-a.md\n"
        )
        return manifest_path

    def test_workers_receive_caller_no_pull_value(self, tmp_path: Path) -> None:
        """Workers receive no_pull=False (the default) so each workspace pulls its own base."""
        config = _make_config(tmp_path)
        manifest_path = self._make_manifest(tmp_path)
        worker_no_pull_values: list[bool] = []

        def capture_no_pull(*args, **kwargs):
            worker_no_pull_values.append(kwargs.get("no_pull", args[8] if len(args) > 8 else None))
            state = CoordinatorState()
            state.preflight_verdict = "PROCEED"
            state.preflight_result = MagicMock(cost_usd=0.0)
            return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="Done.")

        with patch("theforge.sprint.runner.run_task", side_effect=capture_no_pull):
            run_sprint_ctx(config, manifest_path)

        assert all(v is False for v in worker_no_pull_values), (
            "Expected all workers no_pull=False so each workspace pulls a fresh base, "
            f"got {worker_no_pull_values}"
        )

    def test_caller_no_pull_true_skips_worker_pulls(self, tmp_path: Path) -> None:
        """When run_sprint is called with no_pull=True, workers receive no_pull=True."""
        config = _make_config(tmp_path)
        manifest_path = self._make_manifest(tmp_path)
        worker_no_pull_values: list[bool] = []

        def capture_no_pull(*args, **kwargs):
            worker_no_pull_values.append(kwargs.get("no_pull", args[8] if len(args) > 8 else None))
            state = CoordinatorState()
            state.preflight_verdict = "PROCEED"
            state.preflight_result = MagicMock(cost_usd=0.0)
            return CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="Done.")

        with patch("theforge.sprint.runner.run_task", side_effect=capture_no_pull):
            run_sprint_ctx(config, manifest_path, no_pull=True)

        assert all(v is True for v in worker_no_pull_values), (
            "Expected all workers no_pull=True when caller passes no_pull=True, "
            f"got {worker_no_pull_values}"
        )


class TestParallelLogIsolation:
    def test_begin_run_log_tee_skipped_for_parallel_story_threads(self, tmp_path: Path) -> None:
        """Worker-thread tee setup is skipped so parallel stories cannot stack stderr tees."""
        import sys
        import threading

        from theforge.coordinator.log_tee import _begin_run_log_tee, _TeeStderr
        from theforge.coordinator.logging import StructuredLogger

        config = _make_config(tmp_path)
        story_a_dir = tmp_path / ".forge" / "logs" / "parallel-sprint" / "story-a"
        story_b_dir = tmp_path / ".forge" / "logs" / "parallel-sprint" / "story-b"
        story_a_dir.mkdir(parents=True)
        story_b_dir.mkdir(parents=True)

        logger_a = StructuredLogger(
            run_id="parallel-run",
            project="test",
            task="story-a",
            log_file=str(tmp_path / "forge.log"),
            enabled=True,
            project_root=tmp_path,
        )
        logger_b = StructuredLogger(
            run_id="parallel-run",
            project="test",
            task="story-b",
            log_file=str(tmp_path / "forge.log"),
            enabled=True,
            project_root=tmp_path,
        )

        barrier = threading.Barrier(3)
        results: dict[str, object] = {}
        original_stderr = sys.stderr

        def worker(name: str, logger: StructuredLogger, log_dir: Path) -> None:
            barrier.wait()
            results[name] = _begin_run_log_tee(config, logger, name, log_dir=log_dir)
            barrier.wait()

        threads = [
            threading.Thread(target=worker, args=("story-a", logger_a, story_a_dir)),
            threading.Thread(target=worker, args=("story-b", logger_b, story_b_dir)),
        ]
        for thread in threads:
            thread.start()

        barrier.wait()
        barrier.wait()

        for thread in threads:
            thread.join()

        assert results == {"story-a": None, "story-b": None}
        assert sys.stderr is original_stderr
        assert not isinstance(sys.stderr, _TeeStderr)
        assert list(story_a_dir.glob("run-*.log")) == []
        assert list(story_b_dir.glob("run-*.log")) == []


def test_write_sprint_summary_records_ci_break_slug(tmp_path: Path) -> None:
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
        stopped_reason="ci failed",
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
        ci_break_slug="story-123",
    )

    summary = __import__("yaml").safe_load((sprint_log_dir / "sprint-summary.yaml").read_text())
    assert summary["sprint"]["ci_break_slug"] == "story-123"


class TestQueuedMergePolling:
    def test_poll_queued_pr_merged(self, tmp_path: Path) -> None:
        from theforge.sprint.runner import _poll_queued_pr

        states = ["OPEN", "OPEN", "MERGED"]

        def _fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout=states.pop(0), stderr="")

        with (
            patch("theforge.sprint.runner.subprocess.run", side_effect=_fake_run),
            patch("theforge.sprint.runner.time.sleep"),
        ):
            assert _poll_queued_pr("https://github.com/x/y/pull/1", tmp_path, 90) == {
                "status": "merged"
            }

    def test_poll_queued_pr_closed(self, tmp_path: Path) -> None:
        from theforge.sprint.runner import _poll_queued_pr

        with patch(
            "theforge.sprint.runner.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="CLOSED", stderr=""),
        ):
            assert _poll_queued_pr("https://github.com/x/y/pull/1", tmp_path, 90) == {
                "status": "closed"
            }

    def test_poll_queued_pr_timeout(self, tmp_path: Path) -> None:
        from theforge.sprint.runner import _poll_queued_pr

        monotonic_values = iter([0, 1, 31, 61])
        with (
            patch(
                "theforge.sprint.runner.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="OPEN", stderr=""),
            ),
            patch("theforge.sprint.runner.time.sleep"),
            patch(
                "theforge.sprint.runner.time.monotonic", side_effect=lambda: next(monotonic_values)
            ),
        ):
            assert _poll_queued_pr("https://github.com/x/y/pull/1", tmp_path, 60) == {
                "status": "timeout"
            }

    def test_poll_queued_pr_abandons_on_terminally_failed_required_checks(
        self, tmp_path: Path
    ) -> None:
        """Issue #1946: a decided-red PR can never merge. Waiting on it burns the
        whole merge-wait budget and then misreports the cause as a timeout, so the
        poll must return immediately naming the failing checks."""

        from theforge.sprint.runner import _poll_queued_pr

        with (
            patch(
                "theforge.sprint.runner.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="OPEN", stderr=""),
            ),
            patch(
                "theforge.sprint.runner._required_pr_check_state",
                return_value=PrCheckState(["gate", "lint"], []),
            ),
            patch("theforge.sprint.runner.time.sleep") as sleep,
        ):
            result = _poll_queued_pr(
                "https://github.com/x/y/pull/1",
                tmp_path,
                3600,
                base_branch="main",
            )

        assert result == {"status": "checks_failed", "failing_checks": "gate, lint"}
        sleep.assert_not_called()

    def test_poll_queued_pr_keeps_waiting_while_checks_pending(self, tmp_path: Path) -> None:
        """Pending checks are exactly what the wait budget is for: no failing
        required check means keep polling until MERGED (or the deadline)."""

        from theforge.sprint.runner import _poll_queued_pr

        states = ["OPEN", "OPEN", "MERGED"]

        def _fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout=states.pop(0), stderr="")

        with (
            patch("theforge.sprint.runner.subprocess.run", side_effect=_fake_run),
            patch(
                "theforge.sprint.runner._required_pr_check_state",
                return_value=PrCheckState([], []),
            ) as probe,
            patch("theforge.sprint.runner.time.sleep") as sleep,
        ):
            result = _poll_queued_pr(
                "https://github.com/x/y/pull/1",
                tmp_path,
                3600,
            )

        assert result == {"status": "merged"}
        assert sleep.call_count == 2
        # No base branch → no required-check set to resolve; never probe.
        probe.assert_not_called()

    def test_poll_queued_pr_timeout_reserved_for_deadline_expiry(self, tmp_path: Path) -> None:
        """With checks pending (none failing), only the deadline produces a
        timeout — the status must stay distinguishable from checks_failed."""

        from theforge.sprint.runner import _poll_queued_pr

        monotonic_values = iter([0, 1, 31, 61])
        with (
            patch(
                "theforge.sprint.runner.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="OPEN", stderr=""),
            ),
            patch(
                "theforge.sprint.runner._required_pr_check_state",
                return_value=PrCheckState([], []),
            ),
            patch("theforge.sprint.runner.time.sleep"),
            patch(
                "theforge.sprint.runner.time.monotonic", side_effect=lambda: next(monotonic_values)
            ),
        ):
            result = _poll_queued_pr(
                "https://github.com/x/y/pull/1",
                tmp_path,
                60,
                base_branch="main",
            )

        assert result == {"status": "timeout"}

    def test_queued_pr_failure_message_names_the_evidenced_cause(self) -> None:
        from theforge.sprint.runner import _queued_pr_failure_message

        url = "https://github.com/x/y/pull/7"
        assert (
            _queued_pr_failure_message(
                {"status": "checks_failed", "failing_checks": "gate"}, url, 3600
            )
            == f"Queued PR required checks failed (gate): {url}"
        )
        assert (
            _queued_pr_failure_message({"status": "timeout"}, url, 3600)
            == f"Queued PR timed out after 3600s: {url}"
        )
        assert (
            _queued_pr_failure_message({"status": "closed"}, url, 3600)
            == f"Queued PR closed: {url}"
        )

    def test_poll_queued_pr_keeps_waiting_on_a_cancelled_required_check(
        self, tmp_path: Path
    ) -> None:
        """Issue #2270: a cancelled check never judged the change, so it must not
        abandon the PR as decided-red — the wait budget governs it, and the
        expiry names the checks that produced no verdict."""

        from theforge.sprint.runner import _poll_queued_pr

        monotonic_values = iter([0, 1, 31, 61])
        with (
            patch(
                "theforge.sprint.runner.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="OPEN", stderr=""),
            ),
            patch(
                "theforge.sprint.runner._required_pr_check_state",
                return_value=PrCheckState([], ["gate (3.12)"]),
            ),
            patch("theforge.sprint.runner.time.sleep") as sleep,
            patch(
                "theforge.sprint.runner.time.monotonic", side_effect=lambda: next(monotonic_values)
            ),
        ):
            result = _poll_queued_pr(
                "https://github.com/x/y/pull/1",
                tmp_path,
                60,
                base_branch="main",
            )

        assert result == {"status": "timeout", "unjudged_checks": "gate (3.12)"}
        assert sleep.called  # it waited rather than abandoning on the first poll

    def test_queued_pr_failure_message_separates_unjudged_from_failed(self) -> None:
        """The recorded reason is what the operator reads to decide whether the
        work is salvageable, so an unjudged check must not read as a red one."""

        from theforge.sprint.runner import _queued_pr_failure_message

        url = "https://github.com/x/y/pull/7"
        message = _queued_pr_failure_message(
            {"status": "timeout", "unjudged_checks": "gate (3.12)"}, url, 3600
        )
        assert message == (
            f"Queued PR required checks never produced a verdict (gate (3.12)) within 3600s: {url}"
        )
        assert "required checks failed" not in message

    def test_poll_queued_pr_waits_for_origin_main_when_base_branch_set(
        self, tmp_path: Path
    ) -> None:
        """Issue #1402: GitHub MERGED alone is not enough — origin/<base> must
        carry the merge commit before a collision-serialized dependent is
        released. The poll keeps spinning while origin lags."""

        from theforge.sprint.runner import _poll_queued_pr

        # Sequence of subprocess calls per polling iteration when state==MERGED:
        #   1. gh pr view --json state           → "MERGED"
        #   2. gh pr view --json mergeCommit     → "abc1234"
        #   3. git fetch origin <base>           → returncode 0
        #   4. git merge-base --is-ancestor ...  → returncode 1 (not yet) / 0 (yes)
        # First iteration: not-an-ancestor → keep polling.
        # Second iteration: ancestor → return merged.
        responses = [
            MagicMock(returncode=0, stdout="MERGED", stderr=""),
            MagicMock(returncode=0, stdout="abc1234", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="MERGED", stderr=""),
            MagicMock(returncode=0, stdout="abc1234", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        with (
            patch("theforge.sprint.runner.subprocess.run", side_effect=responses),
            patch("theforge.sprint.runner.time.sleep"),
        ):
            assert _poll_queued_pr(
                "https://github.com/x/y/pull/1",
                tmp_path,
                90,
                base_branch="main",
            ) == {"status": "merged"}

    def test_poll_queued_pr_no_base_branch_keeps_legacy_behavior(self, tmp_path: Path) -> None:
        """Without base_branch (e.g. callers that don't care about origin
        propagation), MERGED is sufficient — preserves legacy semantics."""

        from theforge.sprint.runner import _poll_queued_pr

        with (
            patch(
                "theforge.sprint.runner.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="MERGED", stderr=""),
            ),
            patch("theforge.sprint.runner.time.sleep"),
        ):
            assert _poll_queued_pr("https://github.com/x/y/pull/1", tmp_path, 90) == {
                "status": "merged"
            }

    def test_poll_queued_pr_treats_empty_merge_sha_as_unreachable(self, tmp_path: Path) -> None:
        """If `gh pr view --json mergeCommit` comes back empty (race window
        between MERGED state and mergeCommit population), we must keep polling
        rather than release the dependent."""

        from theforge.sprint.runner import _poll_queued_pr

        # Iteration 1: state=MERGED, mergeCommit empty → no fetch/ancestor; loop.
        # Iteration 2: state=MERGED, mergeCommit=sha, fetch ok, ancestor ok.
        responses = [
            MagicMock(returncode=0, stdout="MERGED", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="MERGED", stderr=""),
            MagicMock(returncode=0, stdout="abc1234", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        with (
            patch("theforge.sprint.runner.subprocess.run", side_effect=responses),
            patch("theforge.sprint.runner.time.sleep"),
        ):
            assert _poll_queued_pr(
                "https://github.com/x/y/pull/1",
                tmp_path,
                90,
                base_branch="main",
            ) == {"status": "merged"}

    def test_merge_queued_timeout_escalates_during_wrap_up(self, tmp_path: Path) -> None:
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = dataclasses.replace(
            _make_config(tmp_path),
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="merge-pr",
                auto_push=True,
                ci_check_timeout_seconds=60,
                merge_wait_timeout_seconds=60,
            ),
        )

        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = 1.0
        state.preflight_result = mock_preflight
        state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"action": "merge-pr", "pending": True},
            landing_status="pending_integration",
        )

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_pr",
                return_value={
                    "action": "merge-pr",
                    "pr_url": "https://github.com/x/y/pull/7",
                    "merged": False,
                    "merge_queued": True,
                    "auto_merge_queued": True,
                    "success": True,
                    "error": None,
                },
            ),
            patch(
                "theforge.sprint.runner._poll_queued_pr",
                return_value={"status": "timeout"},
            ) as mock_poll,
            patch(
                "theforge.sprint.runner.poll_required_checks",
                return_value={
                    "status": "pass",
                    "sha": "deadbeef",
                    "failing_checks": [],
                    "message": "ok",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        assert mock_poll.call_args.args == (
            "https://github.com/x/y/pull/7",
            tmp_path,
            60,
        )
        assert result.landing_status == "failed"
        assert result.success is False
        assert result.state.error == "Queued PR timed out after 60s: https://github.com/x/y/pull/7"
        assert sprint.specs_succeeded == 0
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 0

    def test_merge_queued_failed_checks_recorded_as_check_failure(self, tmp_path: Path) -> None:
        """Issue #1946: a red PR must be recorded with the failing check names as
        the cause, not with the generic queue-timeout wording."""

        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = dataclasses.replace(
            _make_config(tmp_path),
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="merge-pr",
                auto_push=True,
                ci_check_timeout_seconds=60,
                merge_wait_timeout_seconds=3600,
            ),
        )

        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = 1.0
        state.preflight_result = mock_preflight
        state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"action": "merge-pr", "pending": True},
            landing_status="pending_integration",
        )

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_pr",
                return_value={
                    "action": "merge-pr",
                    "pr_url": "https://github.com/x/y/pull/7",
                    "merged": False,
                    "merge_queued": True,
                    "auto_merge_queued": True,
                    "success": True,
                    "error": None,
                },
            ),
            patch(
                "theforge.sprint.runner._poll_queued_pr",
                return_value={"status": "checks_failed", "failing_checks": "gate"},
            ),
            patch(
                "theforge.sprint.runner.poll_required_checks",
                return_value={
                    "status": "pass",
                    "sha": "deadbeef",
                    "failing_checks": [],
                    "message": "ok",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        assert result.landing_status == "failed"
        assert result.state.error == (
            "Queued PR required checks failed (gate): https://github.com/x/y/pull/7"
        )
        assert "timed out" not in (result.state.error or "")
        assert sprint.specs_failed == 1

    def test_independent_story_runs_while_merge_is_queued(self, tmp_path: Path) -> None:
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = dataclasses.replace(
            _make_config(tmp_path),
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="merge-pr",
                auto_push=True,
                ci_check_timeout_seconds=60,
                merge_wait_timeout_seconds=60,
            ),
        )

        queued_result = _make_coordinator_result(success=True, cost=1.0)
        queued_result.landing_status = "pending_integration"
        queued_result.merge = {"action": "merge-pr", "pending": True}
        queued_result.state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        landed_result = _make_coordinator_result(success=True, cost=1.0)
        landed_result.landing_status = "pending_integration"
        landed_result.merge = {"action": "merge-pr", "pending": True}
        landed_result.state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        events: list[str] = []

        def fake_run_task(*args, **kwargs):  # noqa: ANN001
            task = args[1]
            events.append(f"run:{task.slug}")
            if task.slug == "story-a":
                time.sleep(0.2)
                return queued_result
            return landed_result

        def fake_merge_pr(config, task, branch, parsed_review, state):  # noqa: ANN001
            events.append(f"merge:{task.slug}")
            if task.slug == "story-a":
                return {
                    "action": "merge-pr",
                    "pr_url": "https://github.com/x/y/pull/7",
                    "merged": False,
                    "merge_queued": True,
                    "auto_merge_queued": True,
                    "success": True,
                    "error": None,
                }
            return {
                "action": "merge-pr",
                "pr_url": "https://github.com/x/y/pull/8",
                "merged": True,
                "merge_queued": False,
                "auto_merge_queued": False,
                "success": True,
                "error": None,
            }

        with (
            patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
            patch("theforge.coordinator.completion._merge_pr", side_effect=fake_merge_pr),
            patch(
                "theforge.sprint.runner._poll_queued_pr",
                return_value={"status": "merged"},
            ) as mock_poll,
            patch(
                "theforge.sprint.runner.poll_required_checks",
                return_value={
                    "status": "pass",
                    "sha": "deadbeef",
                    "failing_checks": [],
                    "message": "ok",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        assert sprint.specs_succeeded == 2
        assert sprint.specs_failed == 0
        assert events.index("run:story-b") < events.index("merge:story-a")
        assert landed_result.landing_status == "landed"
        assert queued_result.landing_status == "landed"
        assert mock_poll.call_count == 1

    def test_queued_pr_closed_fail_closed_during_wrap_up(self, tmp_path: Path) -> None:
        """Closed queued PR → failed landing, error message references 'closed'."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path, ["story-a.md"], budget=10.0, max_parallel=2
        )
        config = dataclasses.replace(
            _make_config(tmp_path),
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="merge-pr",
                auto_push=True,
                merge_wait_timeout_seconds=60,
            ),
        )

        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = 1.0
        state.preflight_result = mock_preflight
        state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"action": "merge-pr", "pending": True},
            landing_status="pending_integration",
        )

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_pr",
                return_value={
                    "action": "merge-pr",
                    "pr_url": "https://github.com/x/y/pull/9",
                    "merged": False,
                    "merge_queued": True,
                    "auto_merge_queued": True,
                    "success": True,
                    "error": None,
                },
            ),
            patch(
                "theforge.sprint.runner._poll_queued_pr",
                return_value={"status": "closed"},
            ),
            patch(
                "theforge.sprint.runner.poll_required_checks",
                return_value={
                    "status": "pass",
                    "sha": "deadbeef",
                    "failing_checks": [],
                    "message": "ok",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        assert result.landing_status == "failed"
        assert result.success is False
        assert "closed" in (result.state.error or "")
        assert "timed out" not in (result.state.error or "")
        assert sprint.specs_succeeded == 0
        assert sprint.specs_failed == 1

    def test_queued_pr_timeout_blocks_dependent(self, tmp_path: Path) -> None:
        """Timeout on queued dep PR → dependent story is not dispatched."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = dataclasses.replace(
            _make_config(tmp_path),
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="merge-pr",
                auto_push=True,
                merge_wait_timeout_seconds=60,
            ),
        )

        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = 1.0
        state.preflight_result = mock_preflight
        state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        queued_result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"action": "merge-pr", "pending": True},
            landing_status="pending_integration",
        )

        dispatched: list[str] = []

        def fake_run_task(*args, **kwargs):  # noqa: ANN001
            task = args[1]
            dispatched.append(task.slug)
            return queued_result

        with (
            patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
            patch(
                "theforge.coordinator.completion._merge_pr",
                return_value={
                    "action": "merge-pr",
                    "pr_url": "https://github.com/x/y/pull/10",
                    "merged": False,
                    "merge_queued": True,
                    "auto_merge_queued": True,
                    "success": True,
                    "error": None,
                },
            ),
            patch(
                "theforge.sprint.runner._poll_queued_pr",
                return_value={"status": "timeout"},
            ),
            patch(
                "theforge.sprint.runner.poll_required_checks",
                return_value={
                    "status": "pass",
                    "sha": "deadbeef",
                    "failing_checks": [],
                    "message": "ok",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        # Dependent story-b must never be dispatched when story-a times out
        assert "story-b" not in dispatched
        assert queued_result.landing_status == "failed"
        assert queued_result.success is False
        assert "timed out" in (queued_result.state.error or "")
        assert sprint.specs_failed >= 1

    def test_dependent_not_skipped_when_dep_has_queued_pr(self, tmp_path: Path) -> None:
        """Dependent story must run after its dep's queued PR merges, not be skipped.

        Regression for: dependent stories skipped when dependency is
        pending_integration instead of waiting (#642).

        Scenario: story-a (no deps) completes REVIEW/APPROVE, PR is queued for
        auto-merge.  story-b depends on story-a.  When active workers are all
        done but story-a is still in queued_prs, the scheduler must poll the PR
        directly and dispatch story-b once it lands — not declare deadlock.
        """
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = dataclasses.replace(
            _make_config(tmp_path),
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="merge-pr",
                auto_push=True,
                merge_wait_timeout_seconds=60,
            ),
        )

        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        mock_preflight = MagicMock()
        mock_preflight.cost_usd = 1.0
        state.preflight_result = mock_preflight
        state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        queued_result = CoordinatorResult(
            success=True,
            phase=Phase.DONE,
            state=state,
            message="Done.",
            merge={"action": "merge-pr", "pending": True},
            landing_status="pending_integration",
        )
        dispatched: list[str] = []

        def fake_run_task(*args, **kwargs):  # noqa: ANN001
            task = args[1]
            dispatched.append(task.slug)
            if task.slug == "story-a":
                return queued_result
            return _make_coordinator_result(success=True, cost=1.0)

        with (
            patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
            patch(
                "theforge.coordinator.completion._merge_pr",
                return_value={
                    "action": "merge-pr",
                    "pr_url": "https://github.com/x/y/pull/11",
                    "merged": False,
                    "merge_queued": True,
                    "auto_merge_queued": True,
                    "success": True,
                    "error": None,
                },
            ),
            patch(
                "theforge.sprint.runner._poll_queued_pr",
                return_value={"status": "merged"},
            ),
            patch(
                "theforge.sprint.runner.poll_required_checks",
                return_value={
                    "status": "pass",
                    "sha": "deadbeef",
                    "failing_checks": [],
                    "message": "ok",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        assert "story-b" in dispatched, "story-b was skipped due to premature deadlock"
        assert queued_result.landing_status == "landed"
        assert sprint.specs_succeeded == 2
        assert sprint.specs_skipped == 0


class TestImmediateIntegrationLanding:
    def test_immediate_landing_on_approve(self, tmp_path: Path) -> None:
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        result_a = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result_b = _make_coordinator_result(success=True, cost=1.0, merged=False)
        # Simulate what _finalize_approve sets when auto_merge=True
        result_a.landing_status = "pending_integration"
        result_a.merge = {"action": "merge", "pending": True}
        result_b.landing_status = "pending_integration"
        result_b.merge = {"action": "merge", "pending": True}
        events: list[str] = []

        def fake_run_task(*args, **kwargs):  # noqa: ANN001
            task = args[1]
            events.append(f"run:{task.slug}")
            return result_a if task.slug == "story-a" else result_b

        def fake_merge(project_root, base_branch, branch, slug, wt, **kwargs):  # noqa: ANN001
            events.append(f"merge:{slug}")
            return {"merged": True, "success": True, "action": "merge"}

        with (
            patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
            patch("theforge.coordinator.completion._merge_branch", side_effect=fake_merge),
        ):
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert sprint.specs_succeeded == 2
        assert "merge:story-a" in events
        assert events.index("run:story-a") < events.index("merge:story-a")

    def test_dep_not_ready_yields_pending_integration(self, tmp_path: Path) -> None:
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b", depends_on=["story-a"])
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        state_a = CoordinatorState()
        state_a.preflight_verdict = "PROCEED"
        state_a.preflight_result = MagicMock(cost_usd=1.0)
        state_b = CoordinatorState()
        state_b.preflight_verdict = "PROCEED"
        state_b.preflight_result = MagicMock(cost_usd=1.0)
        result_a = CoordinatorResult(True, Phase.DONE, state_a, "Done.")
        result_b = CoordinatorResult(True, Phase.DONE, state_b, "Done.")
        # Simulate what _finalize_approve sets when auto_merge=True
        result_a.landing_status = "pending_integration"
        result_a.merge = {"action": "merge", "pending": True}
        result_b.landing_status = "pending_integration"
        result_b.merge = {"action": "merge", "pending": True}

        def fake_run_task(*args, **kwargs):  # noqa: ANN001
            task = args[1]
            if task.slug == "story-a":
                time.sleep(0.2)
                return result_a
            return result_b

        merge_calls: list[str] = []

        def fake_merge(project_root, base_branch, branch, slug, wt, **kwargs):  # noqa: ANN001
            merge_calls.append(slug)
            return {"merged": True, "success": True, "action": "merge"}

        with (
            patch("theforge.sprint.runner.run_task", side_effect=fake_run_task),
            patch("theforge.coordinator.completion._merge_branch", side_effect=fake_merge),
        ):
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert sprint.specs_succeeded == 2
        assert result_b.success is True
        assert result_b.phase is Phase.DONE
        assert result_b.landing_status == "landed"
        assert merge_calls == ["story-a", "story-b"]

    def test_landing_failure_sets_success_false(self, tmp_path: Path) -> None:
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = dataclasses.replace(
            _make_config(tmp_path),
            workspace=WorkspaceConfig(
                create_command="mkdir -p {slug}",
                path_pattern="{slug}",
                branch_pattern="forge/{slug}",
                on_approve="merge-pr",
                auto_push=True,
            ),
        )

        state = CoordinatorState()
        state.preflight_verdict = "PROCEED"
        state.preflight_result = MagicMock(cost_usd=1.0)
        state.review_results = [
            ReviewResult(
                verdict="APPROVE",
                summary="Looks good.",
                findings=[],
                story_matches=True,
                story_mismatches=[],
                test_adequate=True,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )
        ]
        result = CoordinatorResult(True, Phase.DONE, state, "Done.")
        result.landing_status = "pending_integration"
        result.merge = {"action": "merge-pr", "pending": True}

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_pr",
                return_value={
                    "action": "merge-pr",
                    "pr_url": "https://example.test/pr/1",
                    "merged": False,
                    "merge_queued": False,
                    "success": False,
                    "error": "conflict",
                },
            ),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        assert sprint.specs_succeeded == 0
        assert sprint.specs_failed == 1
        assert result.success is False
        assert result.phase is Phase.MERGE_FAILED
        assert result.landing_status == "failed"


class TestRunFreshPlanGatedForwarding:
    """The plan-gated split path must forward both landing-related flags.

    Phase 1 of the split is the call whose WORKSPACE step actually cuts the
    story's worktree, so dropping either flag there reintroduces a false abort
    on a base branch that a local-merge workflow legitimately left ahead.
    """

    def _run(self, tmp_path: Path, *, effective_auto_merge: bool, base_lands_locally: bool):
        task = TaskStory(name="Story A", slug="story-a", story_path=None, story_text="body")
        gate = threading.Event()
        gate.set()  # scheduler already released; do not block the test

        plan_result = _make_coordinator_result(success=True, cost=0.0)
        plan_result.state.workspace_path = tmp_path / "story-a"
        dev_result = _make_coordinator_result(success=True, cost=0.0)

        with (
            patch("theforge.sprint.runner.run_task", return_value=plan_result) as mock_run_task,
            patch(
                "theforge.sprint.runner.run_from_dev", return_value=dev_result
            ) as mock_run_from_dev,
        ):
            _run_fresh(
                _make_config(tmp_path),
                task,
                "sprint-run-1",
                "Sprint",
                False,
                False,
                effective_auto_merge,
                None,
                False,
                gate,
                None,
                base_lands_locally=base_lands_locally,
            )
        return mock_run_task, mock_run_from_dev

    def test_phase1_run_task_forwards_effective_auto_merge(self, tmp_path: Path) -> None:
        mock_run_task, _ = self._run(tmp_path, effective_auto_merge=True, base_lands_locally=True)
        assert mock_run_task.call_args.kwargs["auto_merge"] is True

    def test_phase1_run_task_forwards_base_lands_locally(self, tmp_path: Path) -> None:
        mock_run_task, _ = self._run(tmp_path, effective_auto_merge=False, base_lands_locally=True)
        # effective_auto_merge is False for this story, but an earlier story in
        # the sprint merged locally — the guard must hear about it anyway.
        assert mock_run_task.call_args.kwargs["auto_merge"] is False
        assert mock_run_task.call_args.kwargs["base_lands_locally"] is True

    def test_phase2_run_from_dev_forwards_both(self, tmp_path: Path) -> None:
        _, mock_run_from_dev = self._run(
            tmp_path, effective_auto_merge=True, base_lands_locally=True
        )
        assert mock_run_from_dev.call_args.kwargs["auto_merge"] is True
        assert mock_run_from_dev.call_args.kwargs["base_lands_locally"] is True


class TestCollisionGatePreservedWork:
    """#2234: a collision claim ends when the work ends, not when the story does.

    story-a and story-b plan the same file. story-a passes the gate first and
    then escalates — which *preserves* its worktree and commits for an operator
    decision. Before the fix, leaving the ``active`` set dropped story-a's
    footprint, the gate opened, and story-b built twelve files' worth of work on
    a base the pending decision was about to rewrite.
    """

    @pytest.fixture(autouse=True)
    def _fast_gate_tick(self, monkeypatch):
        """Drive the scheduling loop's gate-service tick faster.

        These tests wait through several ticks each. None of them asserts how
        long a tick is — only what the gate does when it fires — and at the 2s
        production default that put them among the slowest in the suite, one of
        them past the five-second per-test convention.
        """
        monkeypatch.setattr(_runner, "PLAN_GATE_TICK_SECONDS", 0.2)

    def _run(self, tmp_path: Path):
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md"],
            budget=10.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        a_in_dev = threading.Event()
        b_footprint_seen = threading.Event()
        dev_calls: list[str] = []

        def _fake_run_task(cfg, task, **kwargs):  # noqa: ANN001
            # Deterministic ordering: story-b only reaches PLAN_DONE after
            # story-a is past its gate, so story-a is the claim holder.
            if task.slug == "story-b":
                assert a_in_dev.wait(timeout=30)
            plan_result = _make_coordinator_result(success=True, cost=1.0, phase=Phase.PLAN_REVIEW)
            plan_result.state.workspace_path = tmp_path / task.slug
            return plan_result

        def _fake_run_from_dev(cfg, task, workspace_path, **kwargs):  # noqa: ANN001
            dev_calls.append(task.slug)
            a_in_dev.set()
            # Hold story-a in DEV until story-b is parked at the gate, so the
            # escalation lands on a scheduler that is already holding a gate.
            assert b_footprint_seen.wait(timeout=30)
            return _make_coordinator_result(success=False, cost=1.0, phase=Phase.ESCALATE)

        def _fake_footprint(workspace_path):  # noqa: ANN001
            if workspace_path is not None and Path(workspace_path).name == "story-b":
                b_footprint_seen.set()
            return {"src/shared.py"}

        with (
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch("theforge.sprint.runner.run_from_dev", side_effect=_fake_run_from_dev),
            patch("theforge.sprint.runner._extract_plan_footprint", side_effect=_fake_footprint),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        return sprint, dev_calls

    def test_gated_story_never_enters_dev_behind_preserved_escalation(
        self, tmp_path: Path
    ) -> None:
        sprint, dev_calls = self._run(tmp_path)

        # The whole point: story-b must not build on a base that excludes the
        # preserved rewrite it was gated on.
        assert dev_calls == ["story-a"]
        assert "story-b" not in dev_calls

    def test_stood_down_story_is_skipped_not_failed(self, tmp_path: Path) -> None:
        sprint, _ = self._run(tmp_path)

        # story-a escalated (failed); story-b is deferred work, not a verdict.
        assert sprint.specs_failed == 1
        assert sprint.specs_skipped == 1
        assert sprint.specs_succeeded == 0

    def test_stand_down_reason_is_recorded(self, tmp_path: Path, capsys) -> None:
        self._run(tmp_path)
        err = capsys.readouterr().err

        assert "STAND DOWN story-b" in err
        assert "preserved, undecided work from story-a" in err
        assert "src/shared.py" in err
        # The claim retention decision is visible too, not just its consequence.
        assert "Holding collision claim for story-a" in err


class TestCollisionGateQueuedParent:
    """#2234: a claim held for a queued PR must end when that PR lands.

    A ``CLAIM_PENDING_LANDING`` claim is resolvable *in this run*, but only the
    queued-PR polling paths resolve it — and the no-active-workers loop cannot
    run while a worker sits parked at its plan gate. Both remaining resolution
    paths (the gate-service probe and the dependent-dispatch pre-check) must
    therefore release the claim, or the gated sibling waits out its own timeout
    on work that landed minutes ago.
    """

    @pytest.fixture(autouse=True)
    def _fast_gate_tick(self, monkeypatch):
        """Drive the scheduling loop's gate-service tick faster.

        These tests wait through several ticks each. None of them asserts how
        long a tick is — only what the gate does when it fires — and at the 2s
        production default that put them among the slowest in the suite, one of
        them past the five-second per-test convention.
        """
        monkeypatch.setattr(_runner, "PLAN_GATE_TICK_SECONDS", 0.2)

    @staticmethod
    def _plan_result(tmp_path: Path, slug: str):
        plan_result = _make_coordinator_result(success=True, cost=1.0, phase=Phase.PLAN_REVIEW)
        plan_result.state.workspace_path = tmp_path / slug
        return plan_result

    @staticmethod
    def _queued_land(*args, **kwargs):  # noqa: ANN002, ANN003
        return (
            {"merge_queued": True, "pr_url": "https://github.com/x/y/pull/1"},
            "pending_integration",
        )

    def test_gate_service_probe_releases_claim_when_queued_pr_lands(
        self, tmp_path: Path, capsys
    ) -> None:
        """story-b is parked at its gate, so the loop never goes idle.

        Nothing else polls story-a's queued PR while story-b keeps a worker
        alive; the gate-service probe is what turns the merge into a released
        claim. Before the fix story-b waited out its 7200s gate timeout.
        """
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        manifest_path = _make_manifest_parallel(
            tmp_path, ["story-a.md", "story-b.md"], budget=10.0, max_parallel=2
        )
        config = _make_config(tmp_path)

        a_in_dev = threading.Event()
        b_footprint_seen = threading.Event()
        dev_calls: list[str] = []
        poll_timeouts: list[int] = []

        def _fake_run_task(cfg, task, **kwargs):  # noqa: ANN001
            if task.slug == "story-b":
                assert a_in_dev.wait(timeout=30)
            return self._plan_result(tmp_path, task.slug)

        def _fake_run_from_dev(cfg, task, workspace_path, **kwargs):  # noqa: ANN001
            dev_calls.append(task.slug)
            if task.slug == "story-a":
                a_in_dev.set()
                # Park story-b at its gate before story-a queues its PR, so the
                # claim is created while a worker still holds the loop open.
                assert b_footprint_seen.wait(timeout=30)
                return _make_coordinator_result(
                    success=True, cost=1.0, landing_status="pending_integration"
                )
            return _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

        def _fake_footprint(workspace_path):  # noqa: ANN001
            if workspace_path is not None and Path(workspace_path).name == "story-b":
                b_footprint_seen.set()
            return {"src/shared.py"}

        def _fake_poll(pr_url, project_root, timeout_seconds, **kwargs):  # noqa: ANN001
            poll_timeouts.append(timeout_seconds)
            return {"status": "merged"}

        with (
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch("theforge.sprint.runner.run_from_dev", side_effect=_fake_run_from_dev),
            patch("theforge.sprint.runner._extract_plan_footprint", side_effect=_fake_footprint),
            patch("theforge.coordinator.completion.land_story", side_effect=self._queued_land),
            patch("theforge.sprint.runner._poll_queued_pr", side_effect=_fake_poll),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        err = capsys.readouterr().err
        # The gate opened on the merge, not on a timeout, and not by dropping
        # the claim when story-a stopped being active.
        assert dev_calls == ["story-a", "story-b"]
        assert "queued PR merged (holding a collision gate)" in err
        assert "Collision claim released for story-a" in err
        # The probe is single-shot: it must not spend the merge-wait budget on
        # the 2s gate-service tick.
        assert poll_timeouts[0] == 0
        assert sprint.specs_succeeded == 2
        assert sprint.specs_skipped == 0

    def test_dependent_dispatch_precheck_releases_claim_when_queued_pr_lands(
        self, tmp_path: Path, capsys
    ) -> None:
        """The cited path: story-c's dispatch pre-check resolves story-a's PR.

        story-b is parked at its gate on story-a's claim. story-c is released
        by the collision edge and hits the queued-PR pre-check, which polls the
        PR as merged. That branch previously recorded neither the landing nor
        the claim release, leaving story-b parked on a claim nothing else in the
        run would ever clear.
        """
        _make_spec_file(tmp_path, "Story A", "story-a")
        _make_spec_file(tmp_path, "Story B", "story-b")
        _make_spec_file(tmp_path, "Story C", "story-c")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md", "story-b.md", "story-c.md"],
            budget=20.0,
            max_parallel=2,
        )
        config = _make_config(tmp_path)

        a_in_dev = threading.Event()
        b_footprint_seen = threading.Event()
        dev_calls: list[str] = []

        def _fake_run_task(cfg, task, **kwargs):  # noqa: ANN001
            if task.slug == "story-b":
                assert a_in_dev.wait(timeout=30)
            return self._plan_result(tmp_path, task.slug)

        def _fake_run_from_dev(cfg, task, workspace_path, **kwargs):  # noqa: ANN001
            dev_calls.append(task.slug)
            if task.slug == "story-a":
                a_in_dev.set()
                assert b_footprint_seen.wait(timeout=30)
                return _make_coordinator_result(
                    success=True, cost=1.0, landing_status="pending_integration"
                )
            return _make_coordinator_result(success=True, cost=1.0, landing_status="landed")

        def _fake_footprint(workspace_path):  # noqa: ANN001
            name = Path(workspace_path).name if workspace_path is not None else ""
            if name == "story-c":
                # Disjoint: story-c is gated by the DAG edge, not by a collision.
                return {"src/other.py"}
            if name == "story-b":
                b_footprint_seen.set()
            return {"src/shared.py"}

        def _fake_poll(pr_url, project_root, timeout_seconds, **kwargs):  # noqa: ANN001
            # Keep the single-shot gate-service probe (timeout 0) undecided so
            # resolution lands on the blocking dependent-dispatch pre-check,
            # which is the path under test.
            if timeout_seconds == 0:
                return {"status": "timeout"}
            return {"status": "merged"}

        with (
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch(
                "theforge.sprint.runner.compute_synthetic_edges",
                return_value={"story-c": ["story-a"]},
            ),
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch("theforge.sprint.runner.run_from_dev", side_effect=_fake_run_from_dev),
            patch("theforge.sprint.runner._extract_plan_footprint", side_effect=_fake_footprint),
            patch("theforge.coordinator.completion.land_story", side_effect=self._queued_land),
            patch("theforge.sprint.runner._poll_queued_pr", side_effect=_fake_poll),
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        err = capsys.readouterr().err
        assert "queued PR merged (before dependent dispatch)" in err
        assert "Collision claim released for story-a" in err
        # story-b got off the gate and ran; nothing was skipped or stood down.
        assert "story-b" in dev_calls
        assert "STAND DOWN" not in err
        assert sprint.specs_succeeded == 3
        assert sprint.specs_skipped == 0


class TestCollisionClaimWrapUpDrain:
    """#2234: the wrap-up drain must resolve a claim left on a still-queued PR.

    A story whose PR is queued is marked terminal immediately, so a sprint can
    finish its work loop with that PR outstanding and reach the wrap-up drain
    without any in-loop path having polled it. That drain is the last place a
    collision claim can be released, and it runs after every gate is gone — so
    it is also the path least likely to be exercised by the other tests.
    """

    def _run(self, tmp_path: Path, *, pr_status: str):
        _make_spec_file(tmp_path, "Story A", "story-a")
        # One story, but max_parallel=2 so plan gates (and therefore collision
        # claims) are active. Queuing its PR makes it terminal, so the work loop
        # ends with the PR still in flight and drops straight into wrap-up.
        manifest_path = _make_manifest_parallel(
            tmp_path, ["story-a.md"], budget=10.0, max_parallel=2
        )
        config = _make_config(tmp_path)

        def _fake_run_task(cfg, task, **kwargs):  # noqa: ANN001
            plan_result = _make_coordinator_result(success=True, cost=1.0, phase=Phase.PLAN_REVIEW)
            plan_result.state.workspace_path = tmp_path / task.slug
            return plan_result

        def _fake_run_from_dev(cfg, task, workspace_path, **kwargs):  # noqa: ANN001
            return _make_coordinator_result(
                success=True, cost=1.0, landing_status="pending_integration"
            )

        def _fake_land_story(*args, **kwargs):  # noqa: ANN002, ANN003
            return (
                {"merge_queued": True, "pr_url": "https://github.com/x/y/pull/1"},
                "pending_integration",
            )

        with (
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.run_task", side_effect=_fake_run_task),
            patch("theforge.sprint.runner.run_from_dev", side_effect=_fake_run_from_dev),
            patch(
                "theforge.sprint.runner._extract_plan_footprint",
                return_value={"src/shared.py"},
            ),
            patch("theforge.coordinator.completion.land_story", side_effect=_fake_land_story),
            patch(
                "theforge.sprint.runner._poll_queued_pr",
                return_value={"status": pr_status},
            ) as mock_poll,
        ):
            sprint = run_sprint_ctx(config, manifest_path)

        return sprint, mock_poll

    def test_wrap_up_drain_releases_claim_on_merged_pr(self, tmp_path: Path, capsys) -> None:
        sprint, mock_poll = self._run(tmp_path, pr_status="merged")
        err = capsys.readouterr().err

        # The drain ran (nothing in the loop polled this PR) and the claim went
        # with the landing verdict.
        assert mock_poll.call_count == 1
        assert "Collision claim released for story-a (queued PR merged at wrap-up)" in err
        assert sprint.specs_succeeded == 1

    def test_wrap_up_drain_releases_claim_on_failed_pr(self, tmp_path: Path, capsys) -> None:
        sprint, mock_poll = self._run(tmp_path, pr_status="closed")
        err = capsys.readouterr().err

        assert mock_poll.call_count == 1
        assert "queued PR closed during sprint wrap-up" in err
        assert "Collision claim released for story-a (queued PR closed at wrap-up)" in err
        assert sprint.specs_succeeded == 0


class TestSprintLandsLocallyResolution:
    """run_sprint answers the local-landing question sprint-wide, once."""

    def _lands_locally_seen(self, tmp_path: Path, *, max_parallel: int, auto_merge: bool):
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path, ["story-a.md"], budget=10.0, max_parallel=max_parallel
        )
        (tmp_path / ".git").mkdir()
        config = _make_workspace_config(tmp_path, on_approve="pr")
        seen: dict = {}

        def fake_pull(_config, *, lands_locally=None):
            seen["lands_locally"] = lands_locally
            raise RuntimeError("stop after base sync")

        with (
            patch("theforge.sprint.runner._project_root_is_git_checkout", return_value=True),
            patch("theforge.coordinator.workspace.assert_base_branch_checked_out"),
            patch("theforge.coordinator.workspace.pull_base_branch", side_effect=fake_pull),
            pytest.raises(RuntimeError, match="stop after base sync"),
        ):
            run_sprint_ctx(config, manifest_path, auto_merge=auto_merge)
        return seen["lands_locally"]

    def test_sequential_auto_merge_lands_locally(self, tmp_path: Path) -> None:
        """--auto-merge in sequential mode merges into the local base checkout."""
        assert self._lands_locally_seen(tmp_path, max_parallel=1, auto_merge=True) is True

    def test_parallel_auto_merge_does_not_land_locally(self, tmp_path: Path) -> None:
        """Parallel mode forces effective_am False, so nothing merges locally."""
        assert self._lands_locally_seen(tmp_path, max_parallel=2, auto_merge=True) is False

    def test_sequential_without_auto_merge_does_not_land_locally(self, tmp_path: Path) -> None:
        assert self._lands_locally_seen(tmp_path, max_parallel=1, auto_merge=False) is False


def _make_workspace_config(tmp_path: Path, **workspace_kwargs):
    """_make_config with workspace field overrides."""
    config = _make_config(tmp_path)
    return dataclasses.replace(
        config, workspace=dataclasses.replace(config.workspace, **workspace_kwargs)
    )


def _make_pushing_config(tmp_path: Path):
    """These sprints run with auto_merge=True, so auto_push decides publication.

    --auto-merge forces the local-merge landing path; with auto_push on, those
    merges reach the remote, so the base branch is expected to track origin and
    the audit commit must be pushed.
    """
    return _make_workspace_config(tmp_path, auto_push=True)


class TestSprintRunAuditCommit:
    def test_run_sprint_commits_story_run_audits_from_project_root(self, tmp_path: Path) -> None:
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=1,
        )
        (tmp_path / ".git").mkdir()
        config = _make_pushing_config(tmp_path)

        result = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result.landing_status = "pending_integration"
        result.merge = {"action": "merge", "pending": True}
        commit_cmd = 'git commit -m "chore(audit): record sprint run audits" -- .forge/audits/runs'
        shell_calls: list[tuple[str, Path]] = []

        def fake_shell(cmd, cwd, **kwargs):  # noqa: ANN001
            shell_calls.append((cmd, Path(cwd)))
            if cmd == "git rev-parse --abbrev-ref HEAD":
                return (True, "main\n")
            if cmd == "git status --porcelain -- .forge/audits/runs":
                return (True, "?? .forge/audits/runs/run-123.json")
            if cmd == "git add -- .forge/audits/runs":
                return (True, "")
            if cmd == commit_cmd:
                return (True, "")
            if cmd == "git rev-list --count origin/main..main":
                return (True, "0\n")
            return (True, "")

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={"merged": True, "success": True, "action": "merge"},
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=fake_shell),
        ):
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert sprint.specs_succeeded == 1
        # Slice from the audit status probe: the publish sequence is the tail of
        # the run, and rev-list also appears earlier from workspace base sync.
        start = shell_calls.index(("git status --porcelain -- .forge/audits/runs", tmp_path))
        assert shell_calls[start:] == [
            ("git status --porcelain -- .forge/audits/runs", tmp_path),
            # Landing evidence (#2598) is the third tracked memory tree, probed
            # like the others; it contributes nothing to the commit when clean.
            ("git status --porcelain -- .forge/audits/landing", tmp_path),
            ("git add -- .forge/audits/runs", tmp_path),
            (commit_cmd, tmp_path),
            ("git push origin main", tmp_path),
            ("git rev-list --count origin/main..main", tmp_path),
        ]

    def test_run_sprint_skips_audit_commit_when_project_root_is_clean(
        self, tmp_path: Path
    ) -> None:
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=1,
        )
        (tmp_path / ".git").mkdir()
        config = _make_pushing_config(tmp_path)

        result = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result.landing_status = "pending_integration"
        result.merge = {"action": "merge", "pending": True}
        shell_calls: list[tuple[str, Path]] = []

        def fake_shell(cmd, cwd, **kwargs):  # noqa: ANN001
            shell_calls.append((cmd, Path(cwd)))
            if cmd == "git rev-parse --abbrev-ref HEAD":
                return (True, "main\n")
            if cmd == "git status --porcelain -- .forge/audits/runs":
                return (True, "")
            return (True, "")

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={"merged": True, "success": True, "action": "merge"},
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=fake_shell),
        ):
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert sprint.specs_succeeded == 1
        audit_shell_calls = [
            call
            for call in shell_calls
            if ".forge/audits/runs" in call[0]
            or ".forge/knowledge/summaries" in call[0]
            or "chore(audit)" in call[0]
        ]
        assert audit_shell_calls == [("git status --porcelain -- .forge/audits/runs", tmp_path)]

    def test_run_sprint_raises_when_audit_publish_fails_after_state_cleanup(
        self, tmp_path: Path
    ) -> None:
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=1,
        )
        (tmp_path / ".git").mkdir()
        config = _make_pushing_config(tmp_path)

        result = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result.landing_status = "pending_integration"
        result.merge = {"action": "merge", "pending": True}
        commit_cmd = 'git commit -m "chore(audit): record sprint run audits" -- .forge/audits/runs'
        shell_calls: list[tuple[str, Path]] = []
        warnings: list[str] = []

        def fake_shell(cmd, cwd, **kwargs):  # noqa: ANN001
            shell_calls.append((cmd, Path(cwd)))
            if cmd == "git rev-parse --abbrev-ref HEAD":
                return (True, "main\n")
            if cmd == "git status --porcelain -- .forge/audits/runs":
                return (True, "?? .forge/audits/runs/run-123.json")
            if cmd == "git add -- .forge/audits/runs":
                return (True, "")
            if cmd == commit_cmd:
                return (True, "")
            if cmd == "git push origin main":
                return (False, "rejected: non-fast-forward")
            return (True, "")

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={"merged": True, "success": True, "action": "merge"},
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=fake_shell),
            patch("theforge.sprint.audit_publish._log", side_effect=warnings.append),
            pytest.raises(RuntimeError, match="Failed to push story run audits"),
        ):
            run_sprint_ctx(config, manifest_path, auto_merge=True)

        # Cleanup still ran before the raise, but the sprint does not report success.
        assert not any((tmp_path / ".forge" / "runs").glob("*.state"))
        assert any("canonical story run audit publish failed" in warning for warning in warnings)
        assert ("git push origin main", tmp_path) in shell_calls

    def test_run_sprint_raises_when_base_still_ahead_after_push(self, tmp_path: Path) -> None:
        """A push that reports success but leaves the base ahead is still a failure."""
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=1,
        )
        (tmp_path / ".git").mkdir()
        config = _make_pushing_config(tmp_path)

        result = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result.landing_status = "pending_integration"
        result.merge = {"action": "merge", "pending": True}
        warnings: list[str] = []

        def fake_shell(cmd, cwd, **kwargs):  # noqa: ANN001
            if cmd == "git rev-parse --abbrev-ref HEAD":
                return (True, "main\n")
            if cmd == "git status --porcelain -- .forge/audits/runs":
                return (True, "?? .forge/audits/runs/run-123.json")
            if cmd == "git rev-list --count origin/main..main":
                return (True, "1\n")
            return (True, "")

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={"merged": True, "success": True, "action": "merge"},
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=fake_shell),
            patch("theforge.sprint.audit_publish._log", side_effect=warnings.append),
            pytest.raises(RuntimeError, match="still 1 commit"),
        ):
            run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert any("canonical story run audit publish failed" in warning for warning in warnings)

    def test_run_sprint_publishes_audits_off_the_base_branch_under_a_pr_workflow(
        self, tmp_path: Path
    ) -> None:
        """on_approve: pr must not put an audit commit on the base checkout.

        Nothing merges into the local base branch under a PR workflow, and story
        branches are pushed for GitHub to diff against origin/<base> — which is
        exactly where an audit commit on the local base shows up as content of
        the wrong story. Forge used to answer that by committing and pushing the
        audit anyway. Since #2598 it answers it by not making the commit at all:
        the memory is drained out of the checkout and published from the memory
        branch, which a base branch that only advances by pull request accepts.
        """
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=1,
        )
        (tmp_path / ".git").mkdir()
        config = _make_workspace_config(tmp_path, on_approve="pr")
        assert config.workspace.auto_push is False

        result = _make_coordinator_result(success=True, cost=1.0, merged=False)
        commit_cmd = 'git commit -m "chore(audit): record sprint run audits" -- .forge/audits/runs'
        shell_calls: list[str] = []

        def fake_shell(cmd, cwd, **kwargs):  # noqa: ANN001
            shell_calls.append(cmd)
            if cmd == "git rev-parse --abbrev-ref HEAD":
                return (True, "main\n")
            if cmd.startswith("git status --porcelain -uall -- .forge/audits/runs"):
                return (True, "?? .forge/audits/runs/run-123.json")
            return (True, "")

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch("theforge.coordinator.util._run_shell", side_effect=fake_shell),
        ):
            run_sprint_ctx(config, manifest_path)

        assert commit_cmd not in shell_calls
        assert not any(call.startswith("git commit") for call in shell_calls)
        # The memory trees were drained instead, which is what leaves the
        # checkout clean without touching the protected branch.
        assert "git status --porcelain -uall -- .forge/audits/runs" in shell_calls

    def test_run_sprint_commits_without_push_under_local_merge(self, tmp_path: Path) -> None:
        """Local-merge landing without auto_push: pushing would over-publish.

        A branch push carries all its ancestors, so the audit commit cannot be
        published in isolation. The records stay local and the sprint warns.
        """
        _make_spec_file(tmp_path, "Story A", "story-a")
        manifest_path = _make_manifest_parallel(
            tmp_path,
            ["story-a.md"],
            budget=10.0,
            max_parallel=1,
        )
        (tmp_path / ".git").mkdir()
        config = _make_config(tmp_path)
        assert config.workspace.auto_push is False

        result = _make_coordinator_result(success=True, cost=1.0, merged=False)
        result.landing_status = "pending_integration"
        result.merge = {"action": "merge", "pending": True}
        commit_cmd = 'git commit -m "chore(audit): record sprint run audits" -- .forge/audits/runs'
        shell_calls: list[str] = []
        logs: list[str] = []

        def fake_shell(cmd, cwd, **kwargs):  # noqa: ANN001
            shell_calls.append(cmd)
            if cmd == "git rev-parse --abbrev-ref HEAD":
                return (True, "main\n")
            if cmd == "git status --porcelain -- .forge/audits/runs":
                return (True, "?? .forge/audits/runs/run-123.json")
            return (True, "")

        with (
            patch("theforge.sprint.runner.run_task", return_value=result),
            patch(
                "theforge.coordinator.completion._merge_branch",
                return_value={"merged": True, "success": True, "action": "merge"},
            ),
            patch("theforge.coordinator.util._run_shell", side_effect=fake_shell),
            patch("theforge.sprint.audit_publish._log", side_effect=logs.append),
        ):
            sprint = run_sprint_ctx(config, manifest_path, auto_merge=True)

        assert sprint.specs_succeeded == 1
        assert commit_cmd in shell_calls
        assert "git push origin main" not in shell_calls
        assert any("remain local" in line and "workspace.auto_push off" in line for line in logs)


def test_integration_lock_serializes(tmp_path: Path) -> None:
    entered: list[str] = []
    overlap = [False]
    inside = threading.Event()
    release = threading.Event()

    def worker(name: str) -> None:
        with integration_lock(tmp_path):
            if inside.is_set():
                overlap[0] = True
            inside.set()
            entered.append(name)
            if name == "first":
                release.wait(timeout=2)
            inside.clear()

    first = threading.Thread(target=worker, args=("first",))
    second = threading.Thread(target=worker, args=("second",))
    first.start()
    time.sleep(0.1)
    second.start()
    time.sleep(0.1)
    assert entered == ["first"]
    release.set()
    first.join()
    second.join()

    assert overlap[0] is False
    assert entered == ["first", "second"]


def test_write_sprint_summary_marks_cached_preflight(tmp_path):
    import datetime as _dt

    import yaml as _yaml

    from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
    from theforge.sprint.audit import _write_sprint_summary
    from theforge.sprint.manifest import SprintManifest, SprintResult

    manifest = SprintManifest(name="demo-sprint", budget_usd=5.0, stories=["issue:123"])
    state = CoordinatorState(
        preflight_verdict="PROCEED",
        preflight_cached=True,
        preflight_cached_original_verdict="PROCEED",
        preflight_cached_from_run_id="run-old",
    )
    result = SprintResult(
        name="demo-sprint",
        budget_usd=5.0,
        results=[
            (
                "issue:123",
                CoordinatorResult(success=True, phase=Phase.DONE, state=state, message="ok"),
            )
        ],
        total_cost_usd=0.0,
        specs_total=1,
        specs_succeeded=1,
        specs_failed=0,
        specs_skipped=0,
    )
    ts = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
    sprint_log_dir = tmp_path / ".forge" / "logs" / "demo-sprint"
    _write_sprint_summary(
        manifest,
        result,
        ["issue:123"],
        ts,
        ts,
        0.0,
        sprint_log_dir,
        slug_map={"issue:123": "story-123"},
    )
    summary = _yaml.safe_load((sprint_log_dir / "sprint-summary.yaml").read_text())
    story = summary["stories"][0]
    assert story["preflight"] == "cached"
