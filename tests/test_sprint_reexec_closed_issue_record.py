"""A story this sprint ran keeps its record when its own landing closes the issue.

Issue #2847: a story that landed just before a mid-sprint re-exec has closed its
GitHub issue by the time the new process image re-resolves the sprint. Query-mode
resolution classified any closed issue as a pre-existing external dependency, so
the story was written out of the sprint's own record while its spend stayed in
the sprint total — $29.20 counted and unaccountable.

Two guarantees are covered here:

* whether a story earns an outcome record is settled by whether the sprint did
  the work, never by a condition the work itself brought about; and
* spend and accountability do not separate — where a total admits an amount, a
  per-story record explains it, or the gap is reported rather than absorbed.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

from theforge.sprint.audit import (
    _write_sprint_audit,
    _write_sprint_summary,
    build_cost_accounting_discrepancy,
)
from theforge.sprint.manifest import ResolvedSprint, SprintResult
from theforge.sprint.prior_landing import (
    prior_execution_landed,
    prior_execution_recorded,
)
from theforge.sprint.query import build_resolved_sprint
from theforge.sprint.sources import IssueClosedError
from theforge.sprint.story_state import SprintStoryState

LANDED_PRIOR = {
    "outcome": "DONE",
    "cost_usd": 29.2,
    "landing_status": "merged",
    "landed": True,
    "slug": "issue-2686",
    "path": "Issue #2686",
    "depends_on": ["issue-2500"],
    "story_run_id": "story-2686",
}


def _fetch_raising_closed(numbers: set[str]):
    """A ``GitHubIssueSource.fetch`` that reports ``numbers`` as already closed."""
    from theforge.task import TaskStory

    def _fetch(self, ref, project_root):  # noqa: ANN001 - patched bound method
        if str(ref) in numbers:
            raise IssueClosedError(f"issue #{ref} is closed")
        return TaskStory(name=f"Issue #{ref}", slug=f"issue-{ref}", github_issue=int(ref))

    return _fetch


class TestClosedIssueResolutionIsReexecAware:
    """``build_resolved_sprint`` distinguishes "we closed it" from "it was closed"."""

    def _resolve(self, tmp_path: Path, prior_outcomes: dict | None) -> ResolvedSprint:
        with patch(
            "theforge.sprint.sources.GitHubIssueSource.fetch",
            _fetch_raising_closed({"2686"}),
        ):
            return build_resolved_sprint(
                issues=[
                    {"number": 2686, "title": "Landed before the re-exec"},
                    {"number": 2796, "title": "Still in flight"},
                ],
                name="issues-2686,2796",
                budget_usd=50.0,
                max_parallel=2,
                project_root=tmp_path,
                prior_outcomes=prior_outcomes,
            )

    def test_closed_issue_with_prior_landed_done_stays_a_story(self, tmp_path: Path) -> None:
        resolved = self._resolve(tmp_path, {"issue-2686": dict(LANDED_PRIOR)})

        slugs = [task.slug for task, _src, _ref in resolved.stories]
        assert slugs == ["issue-2686", "issue-2796"]
        assert "issue-2686" not in resolved.closed_dependency_slugs
        assert resolved.reconciled_prior_slugs == {"issue-2686"}
        restored = next(task for task, _s, _r in resolved.stories if task.slug == "issue-2686")
        assert restored.github_issue == 2686
        # Scheduling fields come from the record, not from a body nobody fetched.
        assert restored.depends_on == ["issue-2500"]
        assert restored.story_text is None

    def test_externally_closed_issue_remains_a_closed_dependency(self, tmp_path: Path) -> None:
        resolved = self._resolve(tmp_path, prior_outcomes=None)

        assert [t.slug for t, _s, _r in resolved.stories] == ["issue-2796"]
        assert resolved.closed_dependency_slugs == {"issue-2686"}
        assert resolved.reconciled_prior_slugs == set()

    def test_prior_already_done_at_no_cost_remains_a_closed_dependency(
        self, tmp_path: Path
    ) -> None:
        """ALREADY_DONE means the sprint declined to run it — not that it ran it."""
        resolved = self._resolve(
            tmp_path,
            {"issue-2686": {"outcome": "ALREADY_DONE", "cost_usd": 0.0, "landed": True}},
        )

        assert [t.slug for t, _s, _r in resolved.stories] == ["issue-2796"]
        assert resolved.closed_dependency_slugs == {"issue-2686"}

    def test_prior_paid_non_success_execution_stays_a_story(self, tmp_path: Path) -> None:
        """A story this sprint ran and failed is still a story this sprint ran."""
        resolved = self._resolve(
            tmp_path,
            {"issue-2686": {"outcome": "FAILED", "cost_usd": 4.5, "slug": "issue-2686"}},
        )

        assert [t.slug for t, _s, _r in resolved.stories] == ["issue-2686", "issue-2796"]
        assert resolved.closed_dependency_slugs == set()
        assert resolved.reconciled_prior_slugs == {"issue-2686"}


class TestPriorExecutionPredicates:
    def test_landed_requires_done_and_a_settled_landing(self) -> None:
        assert prior_execution_landed(LANDED_PRIOR) is True
        assert prior_execution_landed({"outcome": "DONE", "landing_status": "failed"}) is False
        assert prior_execution_landed({"outcome": "ALREADY_DONE", "landed": True}) is False
        assert prior_execution_landed({"outcome": "FAILED", "cost_usd": 4.5}) is False

    def test_execution_recorded_accepts_spend_but_not_a_free_already_done(self) -> None:
        assert prior_execution_recorded({"outcome": "ESCALATED"}) is True
        assert prior_execution_recorded({"outcome": "SKIPPED", "cost_usd": 0.0}) is False
        assert prior_execution_recorded({"outcome": "ALREADY_DONE", "cost_usd": 0.0}) is False
        # Spend admitted into the total must keep an addressable row whatever
        # outcome the record names.
        assert prior_execution_recorded({"outcome": "ALREADY_DONE", "cost_usd": 1.5}) is True
        assert prior_execution_recorded(None) is False


class TestQueryModeResolvesPriorOutcomesBeforeResolution:
    """The recovery data must be in hand *before* slugs are derived from it."""

    def _run(self, tmp_path: Path, *, reexec: bool) -> dict:
        import argparse

        import theforge.cli.sprint as sprint_cli
        from tests.test_cli_sprint_query import _make_forge_config
        from theforge.sprint.sources import GitHubIssueSource
        from theforge.task import TaskStory

        seen: dict = {}
        resolved = ResolvedSprint(
            name="issues-2686",
            budget_usd=10.0,
            stories=[
                (
                    TaskStory(name="A", slug="issue-2686", github_issue=2686),
                    GitHubIssueSource(),
                    "issue:2686",
                )
            ],
        )

        def _fake_build(**kwargs):
            seen.update(kwargs)
            return resolved

        args = argparse.Namespace(name=None, verbose=False, no_notify=True)
        with (
            patch.object(
                sprint_cli,
                "_resolve_prior_outcomes",
                return_value={"issue-2686": dict(LANDED_PRIOR)},
            ),
            patch("theforge.sprint.query.build_resolved_sprint", _fake_build),
            patch(
                "theforge.sprint.query.fetch_issues_by_numbers",
                return_value=[{"number": 2686, "title": "A"}],
            ),
        ):
            sprint_cli._run_query_mode(
                args=args,
                config=_make_forge_config(tmp_path),
                config_path=tmp_path / "forge.yaml",
                milestone=None,
                label=None,
                issues_arg="2686",
                budget_str="10",
                dry_run=True,
                max_parallel=1,
                auto_merge=False,
                interactive=False,
                resume=False,
                no_pull=True,
                reexec=reexec,
                _daemon=None,
                _detach=None,
                _generate_run_id=lambda: "run-1",
            )
        return seen

    def test_reexec_hands_the_prior_record_to_resolution(self, tmp_path: Path) -> None:
        """Resolution must see the record, or the story is gone before recovery runs."""
        assert self._run(tmp_path, reexec=True)["prior_outcomes"] == {
            "issue-2686": dict(LANDED_PRIOR)
        }

    def test_a_first_generation_resolves_without_a_prior_record(self, tmp_path: Path) -> None:
        assert self._run(tmp_path, reexec=False)["prior_outcomes"] is None

    def test_query_mode_source_resolves_prior_outcomes_ahead_of_resolution(self) -> None:
        """Order is the whole fix: derived after resolution, it is already too late."""
        source = Path("src/theforge/cli/sprint.py").read_text(encoding="utf-8")
        body = source[source.index("def _run_query_mode(") :]
        resolve_at = body.index("prior_outcomes = _resolve_prior_outcomes(config, sprint_name)")
        build_at = body.index("resolved = build_resolved_sprint(")
        slugs_at = body.index("slugs = [task.slug for task, _src, _ref in resolved.stories]")
        assert resolve_at < build_at < slugs_at


def _sprint_result(total: float, **kwargs) -> SprintResult:
    return SprintResult(
        name="issues-2686,2796",
        specs_total=1,
        specs_succeeded=1,
        specs_failed=0,
        specs_skipped=0,
        total_cost_usd=total,
        budget_usd=50.0,
        results=[],
        **kwargs,
    )


class TestCostAccountingDiscrepancy:
    def test_rows_that_explain_the_total_produce_no_block(self) -> None:
        assert build_cost_accounting_discrepancy(45.58, [("a", 16.38), ("b", 29.2)]) is None

    def test_rounding_slack_is_not_a_discrepancy(self) -> None:
        assert build_cost_accounting_discrepancy(45.585, [("a", 45.58)]) is None

    def test_declared_non_story_spend_counts_as_explained(self) -> None:
        assert (
            build_cost_accounting_discrepancy(0.1, [("a", 0.0)], declared_non_story_usd=0.1)
            is None
        )

    def test_rows_summing_higher_than_the_total_is_not_a_discrepancy(self) -> None:
        """A resume's carried-forward rows legitimately exceed this run's ledger."""
        assert build_cost_accounting_discrepancy(16.38, [("a", 16.38), ("b", 29.2)]) is None

    def test_unexplained_spend_is_reported_with_its_amount(self) -> None:
        block = build_cost_accounting_discrepancy(45.579, [("issue-2796", 16.38)])
        assert block is not None
        assert block["sprint_measured_usd"] == 45.579
        assert block["explained_story_usd"] == 16.38
        assert block["unexplained_usd"] == 29.199

    def test_unpriced_rows_are_named(self) -> None:
        block = build_cost_accounting_discrepancy(10.0, [("issue-1", None), ("issue-2", 1.0)])
        assert block is not None
        assert block["stories_without_measured_cost"] == ["issue-1"]


class TestAuditWithholdsATotalItCannotExplain:
    def _write(self, tmp_path: Path, result: SprintResult) -> dict:
        now = datetime.datetime(2026, 9, 2, 6, 33, tzinfo=datetime.timezone.utc)
        _write_sprint_audit(
            manifest=ResolvedSprint(name="issues-2686,2796", budget_usd=50.0, stories=[]),
            result=result,
            canonical_refs=[],
            started_at=now,
            finished_at=now,
            duration=1.0,
            project_root=tmp_path,
            sprint_id="sprint-1",
        )
        return yaml.safe_load(
            (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
        )

    def test_unexplained_spend_nulls_the_total_and_reports_the_gap(self, tmp_path: Path) -> None:
        audit = self._write(tmp_path, _sprint_result(29.2))

        assert audit["sprint"]["total_cost_usd"] is None
        assert audit["sprint"]["cost_complete"] is False
        # The measured lower bound survives — only the claim of completeness goes.
        assert audit["sprint"]["total_cost_measured_usd"] == 29.2
        block = audit["sprint"]["cost_accounting_discrepancy"]
        assert block["unexplained_usd"] == 29.2

    def test_a_fully_explained_total_stays_complete(self, tmp_path: Path) -> None:
        audit = self._write(tmp_path, _sprint_result(0.0))

        assert audit["sprint"]["cost_complete"] is True
        assert audit["sprint"]["cost_accounting_discrepancy"] is None

    def test_declared_non_story_spend_leaves_the_total_complete(self, tmp_path: Path) -> None:
        audit = self._write(tmp_path, _sprint_result(0.1, non_story_spend_usd=0.1))

        assert audit["sprint"]["cost_complete"] is True
        assert audit["sprint"]["total_cost_usd"] == 0.1
        assert audit["sprint"]["cost_accounting_discrepancy"] is None


class TestAuditAccountsForStoriesOnlyCanonicalStateHolds:
    """A story the re-exec's issue query no longer returns is still the sprint's.

    Its ref is absent from ``canonical_refs``, so nothing in this process's
    results produces a row for it — but its spend is inside the sprint total.
    The audit must name it in ``specs:`` rather than report a complete total
    assembled from a set of stories that omits one of its own contributors
    (#2847).
    """

    def _state(self) -> "SprintStoryState":
        state = SprintStoryState()
        state.register(
            "issue-2686",
            "Issue #2686",
            outcome="DONE",
            cost_usd=29.2,
            canonical_ref="issue:2686",
        )
        return state

    def _write(self, tmp_path: Path, **kwargs: object) -> dict:
        now = datetime.datetime(2026, 9, 2, 6, 33, tzinfo=datetime.timezone.utc)
        _write_sprint_audit(
            manifest=ResolvedSprint(name="issues-2686,2796", budget_usd=50.0, stories=[]),
            result=_sprint_result(29.2),
            canonical_refs=[],
            started_at=now,
            finished_at=now,
            duration=1.0,
            project_root=tmp_path,
            sprint_id="sprint-1",
            **kwargs,
        )
        return yaml.safe_load(
            (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
        )

    def test_carried_story_is_named_in_the_audit_rows(self, tmp_path: Path) -> None:
        audit = self._write(tmp_path, story_state=self._state())

        rows = audit["specs"]
        assert [r["slug"] for r in rows] == ["issue-2686"]
        assert rows[0]["outcome"] == "DONE"
        assert rows[0]["cost_usd"] == 29.2
        assert rows[0]["outcome_source"] == "carried_from_accumulated_state"

    def test_the_total_the_rows_now_explain_stays_complete(self, tmp_path: Path) -> None:
        audit = self._write(tmp_path, story_state=self._state())

        assert audit["sprint"]["total_cost_usd"] == 29.2
        assert audit["sprint"]["cost_complete"] is True
        assert audit["sprint"]["cost_accounting_discrepancy"] is None

    def test_spend_no_row_accounts_for_is_still_reported(self, tmp_path: Path) -> None:
        """The projection is not a way to make every total look explained."""
        state = SprintStoryState()
        state.register("issue-2686", "Issue #2686", outcome="DONE", cost_usd=1.0)

        audit = self._write(tmp_path, story_state=state)

        assert audit["sprint"]["total_cost_usd"] is None
        assert audit["sprint"]["cost_complete"] is False
        assert audit["sprint"]["cost_accounting_discrepancy"]["unexplained_usd"] == 28.2


class TestCarriedStoryRowsBecomeAddressableRecords:
    def test_carried_row_is_written_into_the_audit_substrate(self, tmp_path: Path) -> None:
        from theforge.coordinator import audit_read_model, audit_substrate
        from theforge.sprint.audit import persist_accumulated_story_state

        # A story completed under an earlier generation: an accumulated entry
        # exists, but no run record was ever flushed for it in this process.
        persist_accumulated_story_state(
            "sprint-1",
            "issues-2686,2796",
            tmp_path,
            [
                {
                    "canonical_ref": "issue:2686",
                    "slug": "issue-2686",
                    "path": "Issue #2686",
                    "outcome": "DONE",
                    "cost_usd": 29.2,
                    "story_run_id": "story-2686",
                    "started_at": "2026-09-02T05:46:00Z",
                    "finished_at": "2026-09-02T06:32:00Z",
                    "landing_status": "merged",
                }
            ],
        )
        now = datetime.datetime(2026, 9, 2, 6, 33, tzinfo=datetime.timezone.utc)
        log_dir = tmp_path / ".forge" / "logs" / "issues-2686,2796"
        log_dir.mkdir(parents=True, exist_ok=True)

        _write_sprint_summary(
            manifest=ResolvedSprint(name="issues-2686,2796", budget_usd=50.0, stories=[]),
            result=_sprint_result(29.2),
            canonical_refs=[],
            started_at=now,
            finished_at=now,
            duration=1.0,
            sprint_log_dir=log_dir,
            sprint_id="sprint-1",
            project_root=tmp_path,
        )

        summary = yaml.safe_load((log_dir / "sprint-summary.yaml").read_text(encoding="utf-8"))
        assert [s["slug"] for s in summary["stories"]] == ["issue-2686"]

        conn = audit_substrate.create_or_open(tmp_path)
        try:
            record = audit_read_model.latest_record_for(conn, slug="issue-2686")
        finally:
            conn.close()
        assert record is not None, "carried story must be queryable by slug"
        assert record["run_id"] == "story-2686"
        assert record["totals"]["cost_usd"] == 29.2
        assert record["carried_from_accumulated_state"] is True

    def test_restored_story_is_recorded_without_being_dispatched(self, tmp_path: Path) -> None:
        """The seam the fix crosses: resolution → runner → sprint audit.

        No worktree and no merged branch exist, so neither the launch guard's
        reconcile drop nor the ``skip_merged`` triage would neutralise the
        restored story. It must still reach the audit as a DONE row carrying its
        prior spend, and must never be dispatched — its issue body was never
        fetched, so running it would re-spend on landed work with no story text.
        """
        from sprint_test_helpers import run_sprint_ctx

        from tests.test_sprint_resume import _make_config
        from theforge.sprint.audit import persist_accumulated_story_state
        from theforge.sprint.sources import GitHubIssueSource
        from theforge.task import TaskStory

        config = _make_config(tmp_path)
        sprint_dir = tmp_path / ".forge" / "logs" / "issues-2686"
        sprint_dir.mkdir(parents=True, exist_ok=True)
        (sprint_dir / ".sprint_id").write_text("sprint-1", encoding="utf-8")
        persist_accumulated_story_state(
            "sprint-1",
            "issues-2686",
            tmp_path,
            [
                {
                    "canonical_ref": "issue:2686",
                    "slug": "issue-2686",
                    "path": "Issue #2686",
                    "outcome": "DONE",
                    "cost_usd": 29.2,
                    "story_run_id": "story-2686",
                    "landing_status": "merged",
                    "landed": True,
                }
            ],
        )

        resolved = ResolvedSprint(
            name="issues-2686",
            budget_usd=50.0,
            stories=[
                (
                    TaskStory(name="Issue #2686", slug="issue-2686", github_issue=2686),
                    GitHubIssueSource(),
                    "issue:2686",
                )
            ],
            max_parallel=1,
            reconciled_prior_slugs={"issue-2686"},
        )

        with (
            patch("theforge.sprint.runner._run_baseline_gate", return_value={"passed": True}),
            patch("theforge.sprint.runner.resolve_satisfied_dependencies", return_value=set()),
            patch("theforge.sprint.runner.sweep_orphan_worktrees"),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}) as mock_preflight,
            patch("theforge.sprint.runner.run_task") as mock_run_task,
            patch("theforge.coordinator.util._run_shell", return_value=(True, "")),
        ):
            result = run_sprint_ctx(config, resolved, no_pull=True, reexec=True)

        assert not mock_run_task.called, "a restored story must never be dispatched"
        preflighted = mock_preflight.call_args.args[0] if mock_preflight.called else []
        assert [t.slug for t in preflighted] == [], "a restored story must not reach preflight"

        audit = yaml.safe_load(
            (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
        )
        rows = {s["path"]: s for s in audit["specs"]}
        assert "Issue #2686" in rows, "the story this sprint ran must keep its own row"
        assert rows["Issue #2686"]["outcome"] == "DONE"
        assert rows["Issue #2686"]["cost_usd"] == 29.2
        # Reclassification into "not executed here" is not available for it.
        assert audit["closed_dependency_slugs"] == []
        # Spend and record agree, so the total stands as a complete figure.
        assert audit["sprint"]["cost_accounting_discrepancy"] is None
        assert audit["sprint"]["cost_complete"] is True
        assert result.total_cost_usd == 29.2

    def test_a_row_without_identity_is_not_invented(self, tmp_path: Path) -> None:
        from theforge.sprint.audit import _ensure_carried_story_records

        runs_dir = tmp_path / ".forge" / "audits" / "runs"
        _ensure_carried_story_records(
            tmp_path,
            [
                {"slug": "issue-1", "cost_usd": 1.0},  # no story_run_id
                {"story_run_id": "r2", "cost_usd": 1.0},  # no slug
                {"slug": "issue-3", "story_run_id": "sprint-run", "cost_usd": 1.0},
            ],
            sprint_id="sprint-1",
            sprint_name="s",
            sprint_run_id="sprint-run",
        )
        assert not runs_dir.exists() or list(runs_dir.glob("*.json")) == []
