"""Seam tests: a prior generation's DONE is only inherited when it landed.

Issue #2189. The coordinator reaches ``Phase.DONE`` the moment review approves,
and the sprint runner persists that phase as the accumulated story outcome
*before* the separate landing step runs. A mid-sprint ``os.execv`` ("source
updated after pull") in that window leaves ``outcome: DONE`` durably on disk for
a story that never landed. The re-exec'd generation's launch guard read that as
proof of completion, reconciled the worktree collision as a success, marked the
DAG node complete, and reported the story as landed — while its issue stayed open
with approved, unmerged commits on a branch. Its measured $20.56 was also
overwritten with the 0.0 default, dropping the spend from the run total and
rendering a known amount as absent.

These tests pin the three seams that produced that: the launch guard's
classification, the runner's drop handling and cost carry, and the durable record
the guard reads (which must state the landing obligation and its resolution).
"""

from __future__ import annotations

import os
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
    WorkspaceConfig,
)
from theforge.coordinator.state import CoordinatorResult, CoordinatorState, Phase
from theforge.sprint.audit import persist_accumulated_story_state
from theforge.sprint.dag import StoryTriage
from theforge.sprint.launch_guard import (
    REASON_RECONCILE_PRIOR_DONE,
    REASON_STRANDED_WORKTREE,
)
from theforge.sprint.prior_landing import (
    as_prior_record,
    landing_settled,
    reconcilable_prior_success,
)

# ── prior-landing predicate (the shared policy) ───────────────────────────────


class TestLandingEvidencePredicates:
    def test_pre_landing_done_is_not_reconcilable(self) -> None:
        """The exact record #2189 was built on: approved, landing owed, unrun."""
        record = {"outcome": "DONE", "landing_status": "pending_integration", "merge": False}
        assert landing_settled(record) is False
        assert reconcilable_prior_success(record) is False

    def test_landed_done_is_reconcilable(self) -> None:
        record = {"outcome": "DONE", "landing_status": "landed", "merge": True}
        assert reconcilable_prior_success(record) is True

    def test_done_with_no_landing_obligation_is_reconcilable(self) -> None:
        """``on_approve: pr`` / ``none`` reach DONE with landing_status None.

        Those stories owe no landing of their own, so demoting them would strand
        every story in a PR-only or branch-only sprint.
        """
        record = {"outcome": "DONE", "landing_status": None, "merge": False}
        assert reconcilable_prior_success(record) is True

    def test_queued_pr_counts_as_completed_landing(self) -> None:
        """A queued auto-merge is recorded landing, not an unresolved one."""
        record = {
            "outcome": "DONE",
            "landing_status": "pending_integration",
            "landing": {"merge_queued": True, "pr_url": "https://github.com/o/r/pull/1"},
        }
        assert reconcilable_prior_success(record) is True

    def test_failed_landing_is_not_reconcilable(self) -> None:
        record = {"outcome": "DONE", "landing_status": "failed", "merge": False}
        assert reconcilable_prior_success(record) is False

    def test_bare_outcome_string_keeps_prior_behaviour(self) -> None:
        """A record with no landing fields is not evidence of an owed landing."""
        assert as_prior_record("done")["outcome"] == "DONE"
        assert reconcilable_prior_success("DONE") is True
        assert reconcilable_prior_success({"outcome": "DONE"}) is True
        assert reconcilable_prior_success("FAILED") is False
        assert reconcilable_prior_success(None) is False


# ── launch guard classification ───────────────────────────────────────────────


def _guard_config(tmp_path: Path) -> MagicMock:
    config = MagicMock()
    config.project_root = tmp_path
    config.workspace.path_pattern = "worktrees/{slug}"
    config.workspace.branch_pattern = "forge/{slug}"
    config.workspace.base_branch = "main"
    return config


def _make_active_worktree(tmp_path: Path, slug: str) -> None:
    wt = tmp_path / "worktrees" / slug
    wt.mkdir(parents=True, exist_ok=True)
    (wt / "file.txt").write_text("work", encoding="utf-8")


def _classify(tmp_path: Path, prior_outcomes: dict) -> dict[str, str]:
    from theforge.sprint.launch_guard import acquire_launch_story_locks
    from theforge.sprint.lock import release_story_locks

    _make_active_worktree(tmp_path, "issue-1108")
    config = _guard_config(tmp_path)
    completed = MagicMock(returncode=0, stdout="2\n")
    with patch("theforge.sprint.lock.subprocess.run", return_value=completed):
        locked_fds, launch_error, dropped = acquire_launch_story_locks(
            slugs=["issue-1108"],
            config=config,
            resume=False,
            allow_drop=True,
            prior_outcomes=prior_outcomes,
        )
    release_story_locks(locked_fds)
    assert launch_error is None
    return dropped


class TestLaunchGuardRejectsUnlandedPriorDone:
    def test_pre_landing_done_is_stranded_not_reconciled(self, tmp_path: Path, capsys) -> None:
        dropped = _classify(
            tmp_path,
            {
                "issue-1108": {
                    "slug": "issue-1108",
                    "outcome": "DONE",
                    "landing_status": "pending_integration",
                    "merge": False,
                }
            },
        )
        assert dropped["issue-1108"] == REASON_STRANDED_WORKTREE
        assert dropped["issue-1108"] != REASON_RECONCILE_PRIOR_DONE
        assert "STRANDED" in capsys.readouterr().err

    def test_landed_done_still_reconciles(self, tmp_path: Path) -> None:
        dropped = _classify(
            tmp_path,
            {
                "issue-1108": {
                    "slug": "issue-1108",
                    "outcome": "DONE",
                    "landing_status": "landed",
                    "merge": True,
                }
            },
        )
        assert dropped["issue-1108"] == REASON_RECONCILE_PRIOR_DONE

    def test_done_without_landing_obligation_still_reconciles(self, tmp_path: Path) -> None:
        dropped = _classify(
            tmp_path,
            {"issue-1108": {"slug": "issue-1108", "outcome": "DONE", "landing_status": None}},
        )
        assert dropped["issue-1108"] == REASON_RECONCILE_PRIOR_DONE


# ── runner drop handling + cost carry ─────────────────────────────────────────


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
        retry=RetryPolicy(max_dev_iterations=2, max_review_cycles=2),
    )


def _make_spec_file(tmp_path: Path, name: str, slug: str) -> Path:
    spec = tmp_path / f"{slug}.md"
    spec.write_text(
        f"---\nname: {name}\nslug: {slug}\n---\n# {name}\nDo the thing.",
        encoding="utf-8",
    )
    return spec


def _make_manifest(tmp_path: Path, specs: list[str], budget: float = 200.0) -> Path:
    manifest_path = tmp_path / "sprint.yaml"
    manifest_path.write_text(
        yaml.dump({"name": "Test Sprint", "budget_usd": budget, "specs": specs}),
        encoding="utf-8",
    )
    return manifest_path


def _set_sprint_id(tmp_path: Path, sprint_id: str = "sprint-2189") -> str:
    sprint_dir = tmp_path / ".forge" / "logs" / "Test Sprint"
    sprint_dir.mkdir(parents=True, exist_ok=True)
    (sprint_dir / ".sprint_id").write_text(sprint_id, encoding="utf-8")
    return sprint_id


def _make_result(
    *,
    success: bool = True,
    cost: float = 1.0,
    phase: Phase = Phase.DONE,
    landing_status: str | None = None,
) -> CoordinatorResult:
    state = CoordinatorState()
    state.preflight_verdict = "PROCEED"
    mock_preflight = MagicMock()
    mock_preflight.cost_usd = cost
    state.preflight_result = mock_preflight
    state.branch_name = "forge/feature-a"
    return CoordinatorResult(
        success=success,
        phase=phase,
        state=state,
        message="Done." if success else "Failed.",
        landing_status=landing_status,
    )


def _triage_full(spec_path, config, project_root, *, task=None, **_progress):
    return StoryTriage(
        story_path=spec_path,
        action="full",
        reason="x",
        worktree_path=None,
        slug=Path(spec_path).stem,
    )


def _accumulated_by_slug(tmp_path: Path, sprint_id: str) -> dict[str, dict]:
    state_path = tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml"
    data = yaml.safe_load(state_path.read_text(encoding="utf-8")) or {}
    return {s["slug"]: s for s in data.get("stories", [])}


def _summary_by_slug(tmp_path: Path) -> dict[str, dict]:
    summary = yaml.safe_load(
        (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text()
    )
    return {s["slug"]: s for s in summary["stories"]}


def _run_reexec_with_unlanded_prior_done(tmp_path: Path, writes: list | None = None):
    """A re-exec whose prior generation left feature-a DONE-but-unlanded.

    When ``writes`` is given, every accumulated-state write for feature-a is
    appended to it. Those intermediate writes matter on their own: each one is
    what the *next* re-exec reads, and the reported symptom compounded across
    three of them (the first generation to write ``cost_usd: 0.0`` is what made
    the later generation's summary report $0 for a story that spent $20.56).
    """
    _make_spec_file(tmp_path, "Feature A", "feature-a")
    _make_spec_file(tmp_path, "Feature B", "feature-b")
    manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
    config = _make_config(tmp_path)
    sprint_id = _set_sprint_id(tmp_path)
    persist_accumulated_story_state(
        sprint_id,
        "Test Sprint",
        tmp_path,
        [
            {
                "canonical_ref": "feature-a.md",
                "slug": "feature-a",
                "path": "feature-a.md",
                # Phase.DONE persisted before the landing step ran.
                "outcome": "DONE",
                "landing_status": "pending_integration",
                "landing": None,
                "merge": False,
                "cost_usd": 20.556,
                "story_run_id": "run-prev",
                "depends_on": [],
            }
        ],
    )

    def _capture(_sprint_id, sprint_name, project_root, stories):
        if writes is not None:
            for story in stories:
                if story.get("slug") == "feature-a":
                    writes.append(dict(story))
        persist_accumulated_story_state(_sprint_id, sprint_name, project_root, stories)

    fresh = _make_result(landing_status="landed")
    with (
        patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
        patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
        patch("theforge.sprint.runner.run_task", return_value=fresh) as mock_run_task,
        patch(
            "theforge.sprint.runner.persist_accumulated_story_state",
            side_effect=_capture,
        ),
        patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
    ):
        result = run_sprint_ctx(
            config,
            manifest_path,
            reexec=True,
            dropped_slugs={"feature-a": REASON_RECONCILE_PRIOR_DONE},
        )
    return result, sprint_id, mock_run_task


class TestRunnerRefusesUnlandedPriorDone:
    def test_unlanded_prior_done_is_not_recorded_as_done_or_landed(self, tmp_path: Path) -> None:
        """The reported symptom: ``LANDED (9 of 9)`` counting an unlanded story.

        Even when the drop reason still says ``reconciled-prior-generation-done``
        (a stale prior-outcome map, or the guard classifying from a record without
        landing fields), the runner must not settle the story on DONE.
        """
        result, sprint_id, _ = _run_reexec_with_unlanded_prior_done(tmp_path)

        summary = _summary_by_slug(tmp_path)["feature-a"]
        assert summary["outcome"] != "DONE"
        assert summary["outcome"] == "DROPPED"
        assert summary["drop_reason"] == REASON_STRANDED_WORKTREE

        accumulated = _accumulated_by_slug(tmp_path, sprint_id)["feature-a"]
        assert accumulated["outcome"] == "DROPPED"

        # It is not counted as a succeeded/landed story. Only the story that
        # actually ran and landed this generation is.
        assert result.specs_succeeded == 1
        assert result.specs_failed == 1

    def test_measured_cost_survives_the_reroute(self, tmp_path: Path) -> None:
        """$20.56 already measured must stay a number, and stay in the total.

        Every durable write is checked, not just the last one: the row this
        generation persists is what the next re-exec seeds from, so a single
        intermediate write of ``cost_usd: 0.0`` is enough to lose the spend for
        good — which is how the reported run's summary came to claim $75.04
        against an actual $95.60.
        """
        writes: list[dict] = []
        result, sprint_id, _ = _run_reexec_with_unlanded_prior_done(tmp_path, writes)

        assert writes, "no accumulated-state write was captured for feature-a"
        for write in writes:
            assert write["cost_usd"] == pytest.approx(20.556), (
                f"a durable write replaced the measured spend: {write['cost_usd']!r}"
            )

        accumulated = _accumulated_by_slug(tmp_path, sprint_id)["feature-a"]
        assert accumulated["cost_usd"] == pytest.approx(20.556)
        assert _summary_by_slug(tmp_path)["feature-a"]["cost_usd"] == pytest.approx(20.556)
        # The run total reports the spend it recorded (prior $20.556 + fresh $1).
        assert result.total_cost_usd == pytest.approx(21.556)

    def test_landed_prior_done_still_reconciles_as_done(self, tmp_path: Path) -> None:
        """The other half of the rule: a real landing is still inherited."""
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        _make_spec_file(tmp_path, "Feature B", "feature-b")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md", "feature-b.md"])
        config = _make_config(tmp_path)
        sprint_id = _set_sprint_id(tmp_path)
        persist_accumulated_story_state(
            sprint_id,
            "Test Sprint",
            tmp_path,
            [
                {
                    "canonical_ref": "feature-a.md",
                    "slug": "feature-a",
                    "path": "feature-a.md",
                    "outcome": "DONE",
                    "landing_status": "landed",
                    "merge": True,
                    "cost_usd": 0.33,
                    "story_run_id": "run-prev",
                    "depends_on": [],
                }
            ],
        )

        fresh = _make_result(landing_status="landed")
        with (
            patch("theforge.sprint.runner._triage_spec", side_effect=_triage_full),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.run_task", return_value=fresh),
            patch.dict(os.environ, {"FORGE_PREV_RUN_ID": "run-prev"}, clear=False),
        ):
            result = run_sprint_ctx(
                config,
                manifest_path,
                reexec=True,
                dropped_slugs={"feature-a": REASON_RECONCILE_PRIOR_DONE},
            )

        assert _summary_by_slug(tmp_path)["feature-a"]["outcome"] == "DONE"
        assert result.specs_succeeded == 2
        assert result.specs_failed == 0


class TestPersistedRecordStatesLandingObligation:
    def test_pre_landing_record_is_not_reconcilable_and_is_corrected(self, tmp_path: Path) -> None:
        """The durable record must never read as a landed success before it is one.

        Captures every write of the accumulated state during a normal run whose
        landing succeeds. The write that happens *before* the landing step must
        say the landing is owed (so a re-exec reading it strands the story rather
        than inheriting a DONE), and the write after it must say it landed (so a
        story that really did land is still reconciled, not stranded).
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"])
        config = _make_config(tmp_path)
        _set_sprint_id(tmp_path)

        writes: list[dict] = []

        def _capture(sprint_id, sprint_name, project_root, stories):
            for story in stories:
                if story.get("slug") == "feature-a":
                    writes.append(dict(story))

        result_a = _make_result(landing_status="pending_integration")
        with (
            patch("theforge.sprint.runner.persist_accumulated_story_state", side_effect=_capture),
            patch("theforge.sprint.runner.run_task", return_value=result_a),
            patch(
                "theforge.coordinator.completion.land_story",
                return_value=({"action": "merge", "merged": True}, "landed"),
            ),
        ):
            run_sprint_ctx(config, manifest_path)

        assert len(writes) >= 2, f"expected a pre- and post-landing write, got {writes}"
        pre_landing = writes[0]
        assert pre_landing["outcome"] == "DONE"
        assert pre_landing["landing_status"] == "pending_integration"
        assert reconcilable_prior_success(pre_landing) is False

        post_landing = writes[-1]
        assert post_landing["landing_status"] == "landed"
        assert post_landing["merge"] is True
        assert reconcilable_prior_success(post_landing) is True


# ── digest rendering ─────────────────────────────────────────────────────────


class TestDigestCostRendering:
    def test_zero_renders_as_a_number_and_missing_renders_absent(self) -> None:
        from theforge.cli.sprint_digest import _story_row

        assert "$0.00" in _story_row({"slug": "issue-1", "cost_usd": 0.0})
        assert "$20.56" in _story_row({"slug": "issue-1", "cost_usd": 20.556})
        # Cost-unknown and no-cost-recorded stay the explicit absent marker.
        assert "—" in _story_row({"slug": "issue-1", "cost_usd": None})
        assert "—" in _story_row({"slug": "issue-1"})
