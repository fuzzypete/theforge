"""Regression: a stopped run's cost never falls below what it already spent.

A sprint that re-execs carries per-story spend from the generation before it.
That money used to be held in a local variable of ``run_sprint`` and re-attached
to the story rows in the end-of-run wrap-up — a wrap-up ``forge stop`` never
reaches, because the SIGTERM handler writes the terminal marker and exits. The
run then reported a final total *below* the carried figure it had itself
disclosed at startup, with nothing saying the two disagreed.

Two halves are asserted here:
  - the carried spend is on disk from the first state write onward, so no
    stop can lose it (``TestPreRestartSpendIsPersistedEagerly``);
  - where a contradiction survives anyway, the status header reports it as
    unreconciled instead of printing the lower sum as the sprint's cost
    (``TestUnreconciledCostReporting``).

Issue: https://github.com/fuzzypete/theforge/issues/2922
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from sprint_test_helpers import run_sprint_ctx

from tests.test_sprint_resume import (
    _make_config,
    _make_coordinator_result,
    _make_manifest,
    _make_spec_file,
    _set_sprint_id,
    _write_prior_sprint_audit,
)
from theforge.intake.remediation import IntakeOutcome, IntakeOutcomeKind
from theforge.sprint.audit import (
    build_cost_accounting_discrepancy,
    persist_accumulated_story_state,
)
from theforge.sprint.dag import StoryTriage
from theforge.sprint.state_writer import SprintStateWriter
from theforge.sprint.story_state import StoryOutcome

PRIOR_COST_USD = 6.0
CURRENT_COST_USD = 1.5


def _retry_triage() -> StoryTriage:
    return StoryTriage(
        story_path="feature-a.md",
        action="full",
        reason="no worktree found",
        worktree_path=None,
        slug="feature-a",
    )


def _skip_merged_triage() -> StoryTriage:
    return StoryTriage(
        story_path="feature-a.md",
        action="skip_merged",
        reason="already merged to main",
        worktree_path=None,
        slug="feature-a",
    )


def _seed_prior_generation(tmp_path: Path, *, outcome: str) -> str:
    """Accumulated state as an interrupted earlier generation would leave it."""
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
                "outcome": outcome,
                "cost_usd": PRIOR_COST_USD,
                "story_run_id": "run-prev",
                "started_at": "2026-09-05T01:00:00Z",
                "finished_at": "2026-09-05T01:05:00Z",
            }
        ],
    )
    return sprint_id


def _spy_on_state_writes(monkeypatch, snapshots: list[dict]) -> None:
    """Record the live ``.state`` file after every write the run makes.

    The point of the fix is *when* the money reaches disk, so the assertion has
    to see every intermediate file the run produced — not only the last one.
    """
    original = SprintStateWriter._write_locked

    def spy(self) -> None:
        original(self)
        try:
            data = yaml.safe_load(self._state_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return
        if isinstance(data, dict):
            snapshots.append(data)

    monkeypatch.setattr(SprintStateWriter, "_write_locked", spy)


def _spy_on_accumulated_writes(monkeypatch, published: list[list[dict]]) -> None:
    """Record every accumulated story list the run publishes, in order."""
    from theforge.sprint import runner as _runner

    original = _runner.persist_accumulated_story_state

    def spy(sprint_id, sprint_name, project_root, stories):
        published.append([dict(row) for row in stories])
        original(sprint_id, sprint_name, project_root, stories)

    monkeypatch.setattr(_runner, "persist_accumulated_story_state", spy)


def _config_with_intake(tmp_path: Path):
    import dataclasses

    from theforge.config.types import IntakeConfig

    return dataclasses.replace(
        _make_config(tmp_path),
        intake=IntakeConfig(grooming=True, auto_fix=True, auto_fix_mode="edit"),
    )


def _dropped_intake_outcome(slug: str) -> IntakeOutcome:
    """An intake gate rejection that spent nothing of its own."""
    return IntakeOutcome(
        slug=slug,
        kind=IntakeOutcomeKind.DROPPED_SHAPE,
        findings=(),
        detail="story shape rejected",
        audit={"remediation_source": "mechanical"},
    )


def _dropped_intake_outcome_with_cost(slug: str, cost_usd: float) -> IntakeOutcome:
    """An intake gate rejection that paid an agent to attempt a rewrite first."""
    return IntakeOutcome(
        slug=slug,
        kind=IntakeOutcomeKind.DROPPED_AFTER_FIX,
        findings=(),
        detail="still unshaped after the rewrite attempt",
        audit={
            "remediation_source": "agent",
            "agent": {
                "attempted": True,
                "detail": "rewrote ACs",
                "profile_name": "intake",
                "model_used": "claude",
                "cost_usd": cost_usd,
                "transport_used": "cli",
            },
            "issue_updated": False,
            "comment_posted": False,
        },
    )


def _row_costs(snapshots: list[dict], slug: str) -> list[float | None]:
    costs: list[float | None] = []
    for snapshot in snapshots:
        for story in snapshot.get("stories") or []:
            if isinstance(story, dict) and story.get("slug") == slug:
                costs.append(story.get("cost_usd"))
    return costs


class TestPreRestartSpendIsPersistedEagerly:
    def test_retried_story_row_never_regresses_below_prior_spend(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Every persisted row for a re-run story holds its pre-restart spend.

        The story's own coordinator reports $1.50 for this generation, which is
        *less* than the $6.00 the previous one spent on it. If the row is written
        with the smaller figure and only reconciled at wrap-up, a stop in between
        publishes a total that lost $6.00.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=100.0)
        config = _make_config(tmp_path)
        sprint_id = _seed_prior_generation(tmp_path, outcome="FAILED")

        snapshots: list[dict] = []
        _spy_on_state_writes(monkeypatch, snapshots)

        with (
            patch("theforge.sprint.runner._triage_spec", return_value=_retry_triage()),
            patch(
                "theforge.sprint.runner.run_task",
                return_value=_make_coordinator_result(
                    success=True, cost=CURRENT_COST_USD, landing_status="landed"
                ),
            ),
        ):
            result = run_sprint_ctx(config, manifest_path, reexec=True, run_id="run-gen2-2922")

        costs = _row_costs(snapshots, "feature-a")
        assert costs, "the run must have written a live state row for feature-a"
        assert all(c is not None and c >= PRIOR_COST_USD for c in costs), (
            "no persisted row may report less than the pre-restart spend already "
            f"attributed to the story; saw {costs}"
        )
        assert costs[-1] == pytest.approx(PRIOR_COST_USD + CURRENT_COST_USD)

        # The accumulated file the next generation reloads carries the same
        # figure, written while the story settled rather than at wrap-up.
        accumulated = yaml.safe_load(
            (tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml").read_text(
                encoding="utf-8"
            )
        )
        rows = {row["slug"]: row for row in accumulated.get("stories", [])}
        assert rows["feature-a"]["cost_usd"] == pytest.approx(PRIOR_COST_USD + CURRENT_COST_USD)
        # The run's own totals still agree — eager attribution must not
        # double-count what the wrap-up reconciliation used to add.
        assert result.total_cost_usd == pytest.approx(PRIOR_COST_USD + CURRENT_COST_USD)
        summary = yaml.safe_load(
            (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text(
                encoding="utf-8"
            )
        )
        by_slug = {s["slug"]: s for s in summary["stories"]}
        assert by_slug["feature-a"]["cost_usd"] == pytest.approx(PRIOR_COST_USD + CURRENT_COST_USD)

    def test_skip_merged_story_keeps_prior_spend_exactly_once(self, tmp_path: Path) -> None:
        """The other branch: a prior DONE story that does NOT re-run.

        Its cost is seeded onto the canonical row at startup and nothing
        overwrites it, so carrying it as attribution as well would report $12.00
        for $6.00 of work. Eager attribution has to leave this case alone.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=100.0)
        config = _make_config(tmp_path)
        sprint_id = _seed_prior_generation(tmp_path, outcome="DONE")

        with (
            patch("theforge.sprint.runner._triage_spec", return_value=_skip_merged_triage()),
            patch(
                "theforge.sprint.runner.run_task",
                return_value=_make_coordinator_result(
                    success=True, cost=CURRENT_COST_USD, landing_status="landed"
                ),
            ),
        ):
            result = run_sprint_ctx(
                config, manifest_path, reexec=True, run_id="run-gen2-skipmerged"
            )

        summary = yaml.safe_load(
            (tmp_path / ".forge" / "logs" / "Test Sprint" / "sprint-summary.yaml").read_text(
                encoding="utf-8"
            )
        )
        by_slug = {s["slug"]: s for s in summary["stories"]}
        assert by_slug["feature-a"]["cost_usd"] == pytest.approx(PRIOR_COST_USD), (
            "a story skipped as already-merged keeps the prior cost its row was "
            "seeded with — once, not twice"
        )
        assert result.total_cost_usd == pytest.approx(PRIOR_COST_USD)

        accumulated = yaml.safe_load(
            (tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml").read_text(
                encoding="utf-8"
            )
        )
        rows = {row["slug"]: row for row in accumulated.get("stories", [])}
        assert rows["feature-a"]["cost_usd"] == pytest.approx(PRIOR_COST_USD)

    def test_intake_dropped_story_never_publishes_a_zero_accumulated_row(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A re-entered story dropped before dispatch keeps its pre-restart spend.

        Nothing about this path produces a coordinator result, so every early
        terminal — an intake drop, an auth or budget skip, a gate stand-down —
        records the accumulated row with a defaulted $0.00. For a story carrying
        $6.00 from before the re-exec, publishing that zero and fixing it only at
        wrap-up leaves a window in which a stop keeps the zero.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=100.0)
        config = _config_with_intake(tmp_path)
        sprint_id = _seed_prior_generation(tmp_path, outcome="FAILED")

        published: list[list[dict]] = []
        _spy_on_accumulated_writes(monkeypatch, published)

        def fake_intake(_tasks, _root, **_kwargs):
            return {"feature-a": _dropped_intake_outcome("feature-a")}

        with (
            patch("theforge.sprint.runner._triage_spec", return_value=_retry_triage()),
            patch("theforge.sprint.runner.run_intake_remediation", side_effect=fake_intake),
            patch(
                "theforge.sprint.runner._build_intake_agent_caller",
                return_value=(lambda *a, **k: None, ""),
            ),
            patch(
                "theforge.sprint.runner.run_task",
                return_value=_make_coordinator_result(success=True, cost=CURRENT_COST_USD),
            ),
        ):
            run_sprint_ctx(config, manifest_path, reexec=True, run_id="run-gen2-intake-drop")

        costs = [
            row.get("cost_usd")
            for stories in published
            for row in stories
            if row.get("slug") == "feature-a"
        ]
        assert costs, "the run must have published an accumulated row for feature-a"
        assert all(c is not None and c >= PRIOR_COST_USD for c in costs), (
            "no accumulated row may be published below the pre-restart spend, not "
            f"even transiently; saw {costs}"
        )

        accumulated = yaml.safe_load(
            (tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml").read_text(
                encoding="utf-8"
            )
        )
        rows = {row["slug"]: row for row in accumulated.get("stories", [])}
        assert rows["feature-a"]["cost_usd"] == pytest.approx(PRIOR_COST_USD)


class TestRecordedSpendIsMonotonic:
    def test_rising_spend_is_recorded_while_status_is_unchanged(self, tmp_path: Path) -> None:
        """A run comfortably ``within`` its cap still records what it spends.

        ``set_budget_status`` short-circuits when the status and overrun have not
        moved, which is most of a run. Without spend in that check the recorded
        figure would sit at whatever the first publication happened to be.
        """
        writer = SprintStateWriter("run-mono", tmp_path, "Test Sprint", budget_usd=100.0)
        writer.init([{"slug": "feature-a", "path": "feature-a.md", "cost_usd": 0.0}])

        writer.set_budget_status("within", spend_usd=5.0)
        assert writer.recorded_spend_usd() == pytest.approx(5.0)
        writer.set_budget_status("within", spend_usd=12.0)
        assert writer.recorded_spend_usd() == pytest.approx(12.0)

        state = yaml.safe_load((tmp_path / ".forge" / "runs" / "run-mono.state").read_text())
        assert state["budget_spend_usd"] == pytest.approx(12.0)

    def test_recorded_spend_never_falls(self, tmp_path: Path) -> None:
        """Spend already disclosed stays disclosed — a lower report is not a refund."""
        writer = SprintStateWriter("run-mono2", tmp_path, "Test Sprint", budget_usd=100.0)
        writer.init([{"slug": "feature-a", "path": "feature-a.md", "cost_usd": 0.0}])

        writer.set_budget_status("within", spend_usd=31.44)
        writer.set_budget_status("within", spend_usd=21.91)
        assert writer.recorded_spend_usd() == pytest.approx(31.44)

        writer.set_budget_status("within", spend_usd=None)
        assert writer.recorded_spend_usd() == pytest.approx(31.44)

        state = yaml.safe_load((tmp_path / ".forge" / "runs" / "run-mono2.state").read_text())
        assert state["budget_spend_usd"] == pytest.approx(31.44)


def _write_stopped_state(
    tmp_path: Path,
    run_id: str,
    *,
    story_costs: dict[str, float],
    recorded_spend_usd: float,
) -> None:
    """A stopped run's leftovers: terminal ``.state`` plus its ``.ended`` marker."""
    runs_dir = tmp_path / ".forge" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.state").write_text(
        yaml.dump(
            {
                "sprint_name": "issues-2775,2906",
                "sprint_id": "sprint-2922",
                "sprint_phase": "stopped",
                "budget_usd": 150.0,
                "budget_status": "within",
                "budget_overrun_usd": 0.0,
                "budget_spend_usd": recorded_spend_usd,
                "max_parallel": 2,
                "stories": [
                    {
                        "slug": slug,
                        "path": f"Issue #{slug.removeprefix('issue-')}",
                        "outcome": StoryOutcome.FAILED.value,
                        "status": StoryOutcome.FAILED.value,
                        "phase": "STOPPED",
                        "cost_usd": cost,
                        "detail": {},
                        "blocked_by": [],
                        "depends_on": [],
                    }
                    for slug, cost in story_costs.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    (runs_dir / f"{run_id}.ended").write_text("stopped\n", encoding="utf-8")


def _make_state_live(tmp_path: Path, run_id: str) -> None:
    """Turn a stopped run's leftovers into a run that is still going.

    A PID file is what ``forge status`` reads as "still live", and the terminal
    marker must be gone or the header reports the recorded outcome instead.
    """
    runs_dir = tmp_path / ".forge" / "runs"
    (runs_dir / f"{run_id}.ended").unlink(missing_ok=True)
    (runs_dir / f"{run_id}.pid").write_text("999999\n", encoding="utf-8")
    state_path = runs_dir / f"{run_id}.state"
    data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    data["sprint_phase"] = "running"
    state_path.write_text(yaml.dump(data), encoding="utf-8")


class TestUnreconciledCostReporting:
    def test_live_run_shows_recorded_spend_as_the_floor(self, tmp_path: Path, capsys) -> None:
        """A live run never displays less than it has already recorded spending.

        Its rows can legitimately trail its ledger for a moment, so this is not a
        contradiction to refuse on — but the number that cannot be too low is the
        one to lead with, because the operator's continue-or-stop decision and
        the cap both read it.
        """
        from theforge.cli.sprint_status import display_sprint_status

        _write_stopped_state(
            tmp_path,
            "0210029b48b1",
            story_costs={"issue-2908": 2.54, "issue-2914": 19.37},
            recorded_spend_usd=31.44,
        )
        _make_state_live(tmp_path, "0210029b48b1")

        assert display_sprint_status("0210029b48b1", tmp_path) == 0
        header = capsys.readouterr().out.splitlines()[0]

        assert "cost: $31.44" in header, header
        assert "$21.91" in header, "the row sum must still be named beside it"
        assert "cost: $21.91" not in header, (
            "a live run must not present a row sum below its own recorded spend as the run's cost"
        )

    def test_live_run_whose_rows_account_for_its_spend_reads_plainly(
        self, tmp_path: Path, capsys
    ) -> None:
        """No floor annotation when there is no gap to annotate."""
        from theforge.cli.sprint_status import display_sprint_status

        _write_stopped_state(
            tmp_path,
            "0210029b48b2",
            story_costs={"issue-2908": 2.54, "issue-2914": 3.74},
            recorded_spend_usd=6.28,
        )
        _make_state_live(tmp_path, "0210029b48b2")

        assert display_sprint_status("0210029b48b2", tmp_path) == 0
        header = capsys.readouterr().out.splitlines()[0]

        assert "cost: $6.28" in header, header
        assert "recorded" not in header, header

    def test_stopped_run_below_its_recorded_spend_reports_unreconciled(
        self, tmp_path: Path, capsys
    ) -> None:
        """Rows that sum below the run's own recorded spend are not a total.

        Both figures describe one run and they contradict each other. Printing
        the smaller one as ``cost:`` is the outcome the operator has no signal
        about — and it under-reports against the cap.
        """
        from theforge.cli.sprint_status import display_sprint_status

        _write_stopped_state(
            tmp_path,
            "0210029b48a1",
            story_costs={"issue-2908": 2.54, "issue-2914": 3.74},
            recorded_spend_usd=31.44,
        )

        assert display_sprint_status("0210029b48a1", tmp_path) == 0
        header = capsys.readouterr().out.splitlines()[0]

        assert "unreconciled" in header, header
        assert "$31.44" in header, header
        assert "cost: $6.28" not in header, (
            "the contradicted per-story sum must not be presented as the run's cost"
        )

    def test_one_cent_below_recorded_spend_is_still_unreconciled(
        self, tmp_path: Path, capsys
    ) -> None:
        """A cent is not rounding noise — it is a cent that went missing.

        A tolerance would wave through exactly the decreases hardest to spot, so
        the comparison is made at the precision both figures are stored to.
        """
        from theforge.cli.sprint_status import display_sprint_status

        _write_stopped_state(
            tmp_path,
            "0210029b48a3",
            story_costs={"issue-2908": 2.54, "issue-2914": 3.74},
            recorded_spend_usd=6.29,
        )

        assert display_sprint_status("0210029b48a3", tmp_path) == 0
        header = capsys.readouterr().out.splitlines()[0]

        assert "unreconciled" in header, header
        assert "cost: $6.28" not in header, header

    def test_stopped_run_that_reconciles_reports_its_total(self, tmp_path: Path, capsys) -> None:
        """The guard is a contradiction check, not a blanket refusal."""
        from theforge.cli.sprint_status import display_sprint_status

        _write_stopped_state(
            tmp_path,
            "0210029b48a2",
            story_costs={"issue-2908": 2.54, "issue-2914": 3.74},
            recorded_spend_usd=6.28,
        )

        assert display_sprint_status("0210029b48a2", tmp_path) == 0
        header = capsys.readouterr().out.splitlines()[0]

        assert "unreconciled" not in header, header
        assert "cost: $6.28" in header, header

    def test_serialization_noise_below_the_stored_precision_is_not_flagged(
        self, tmp_path: Path, capsys
    ) -> None:
        """Below the persisted precision there is no decrease to report.

        Rows carry more places than the recorded figure does, so the two disagree
        in the sixth decimal on every ordinary run. That is the file format, not
        lost money, and flagging it would make the signal useless.
        """
        from theforge.cli.sprint_status import display_sprint_status

        _write_stopped_state(
            tmp_path,
            "0210029b48a4",
            story_costs={"issue-2908": 2.539976, "issue-2914": 3.741151},
            recorded_spend_usd=6.2811,
        )

        assert display_sprint_status("0210029b48a4", tmp_path) == 0
        header = capsys.readouterr().out.splitlines()[0]

        assert "unreconciled" not in header, header
        assert "cost: $6.28" in header, header

    def test_stopped_run_with_recorded_spend_and_no_rows_is_unreconciled(
        self, tmp_path: Path, capsys
    ) -> None:
        """No surviving rows is the extreme of the same contradiction.

        Rows that explain nothing explain $0.00. A run whose state records money
        spent must not print nothing about it merely because the record of where
        the money went did not survive.
        """
        from theforge.cli.sprint_status import display_sprint_status

        _write_stopped_state(
            tmp_path,
            "0210029b48a5",
            story_costs={},
            recorded_spend_usd=31.44,
        )

        assert display_sprint_status("0210029b48a5", tmp_path) == 0
        header = capsys.readouterr().out.splitlines()[0]

        assert "unreconciled" in header, header
        assert "$31.44" in header, header


class TestAuditWithholdsAContradictedTotal:
    """The same rule at the publication surfaces, not only in the status view."""

    def test_one_cent_below_recorded_spend_is_a_discrepancy(self) -> None:
        """The high-water comparison gets no rounding slack.

        The ledger-versus-rows check has a cent of it, and legitimately: those
        are two aggregations of the same money by different routes. The recorded
        high-water is not a second aggregation — it is a figure this run already
        published — so a cent below it is a cent that went missing.
        """
        block = build_cost_accounting_discrepancy(
            6.28,
            [("issue-2908", 2.54), ("issue-2914", 3.74)],
            recorded_spend_high_water_usd=6.29,
        )

        assert block is not None, "a one-cent shortfall against recorded spend is not rounding"
        assert block["unexplained_usd"] == pytest.approx(0.01)
        assert block["recorded_spend_high_water_usd"] == pytest.approx(6.29)
        assert block["sprint_ledger_usd"] == pytest.approx(6.28)
        assert "already recorded" in block["detail"]

    def test_rows_matching_recorded_spend_are_settled(self) -> None:
        """No gap, no block — the guard is a contradiction check."""
        assert (
            build_cost_accounting_discrepancy(
                6.28,
                [("issue-2908", 2.54), ("issue-2914", 3.74)],
                recorded_spend_high_water_usd=6.28,
            )
            is None
        )

    def test_sub_cent_ledger_noise_keeps_its_tolerance(self) -> None:
        """The ledger's own cent of slack survives the split.

        Tightening the high-water comparison must not tighten this one: the two
        totals round independently and disagreeing in the third place is the
        arithmetic, not lost money.
        """
        assert (
            build_cost_accounting_discrepancy(
                6.285,
                [("issue-2908", 2.54), ("issue-2914", 3.74)],
                recorded_spend_high_water_usd=0.0,
            )
            is None
        )

    def test_sprint_audit_withholds_the_total_when_rows_fall_below_recorded_spend(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """End to end: sprint-audit.yaml does not publish the contradicted figure.

        The run's rows come to $1.50 while the state file it wrote records $9.00
        of spend under the same run id. The audit must report the gap and
        withhold the total rather than certify the smaller number.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=100.0)
        config = _make_config(tmp_path)
        _set_sprint_id(tmp_path)

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run-audit-highwater.state").write_text(
            yaml.dump({"budget_status": "within", "budget_spend_usd": 9.0, "stories": []}),
            encoding="utf-8",
        )

        with patch(
            "theforge.sprint.runner.run_task",
            return_value=_make_coordinator_result(success=True, cost=CURRENT_COST_USD),
        ):
            run_sprint_ctx(config, manifest_path, run_id="run-audit-highwater")

        audit = yaml.safe_load(
            (tmp_path / ".forge" / "audits" / "sprint-audit.yaml").read_text(encoding="utf-8")
        )
        discrepancy = audit["sprint"]["cost_accounting_discrepancy"]
        assert discrepancy is not None, (
            "rows below the run's own recorded spend must be reported, not settled"
        )
        assert discrepancy["recorded_spend_high_water_usd"] == pytest.approx(9.0)
        assert audit["sprint"]["cost_complete"] is False
        assert audit["sprint"]["total_cost_usd"] is None


class TestReexecBudgetAdmission:
    def test_carried_spend_is_floored_at_the_runs_recorded_high_water(
        self, tmp_path: Path
    ) -> None:
        """A cap is enforced against what the run spent, not what its rows kept.

        The accumulated rows carry $2.00 because a lost generation took the rest
        with it; the run's own .state — which survives a re-exec, same run id,
        same file — records $9.00. Admitting work against $2.00 spends headroom
        the run does not have.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=100.0)
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
                    "cost_usd": 2.0,
                }
            ],
        )
        _write_prior_sprint_audit(tmp_path, sprint_id, 2.0)

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run-floor.state").write_text(
            yaml.dump({"budget_status": "within", "budget_spend_usd": 9.0, "stories": []}),
            encoding="utf-8",
        )

        with (
            patch("theforge.sprint.runner._triage_spec", return_value=_skip_merged_triage()),
            patch(
                "theforge.sprint.runner.run_task",
                return_value=_make_coordinator_result(success=True, cost=CURRENT_COST_USD),
            ),
        ):
            result = run_sprint_ctx(config, manifest_path, reexec=True, run_id="run-floor")

        assert result.total_cost_usd >= 9.0, (
            "the sprint must carry the spend its own run state recorded, not the "
            f"smaller figure its accumulated rows kept; got ${result.total_cost_usd:.2f}"
        )

    def test_a_high_water_over_the_cap_refuses_before_any_paid_pass(self, tmp_path: Path) -> None:
        """The refusal has to land ahead of the passes that spend money.

        Accumulated rows keep $21.91 of a $31.44 run under a $30 cap. Against the
        rows there is headroom and the sprint proceeds; against what the run
        actually spent there is none. Refusing only at per-story dispatch still
        pays for intake remediation and a batch preflight first, so the check is
        made before either — the same rule the landing precondition follows.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=30.0)
        config = _config_with_intake(tmp_path)
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
                    "cost_usd": 21.91,
                }
            ],
        )
        _write_prior_sprint_audit(tmp_path, sprint_id, 21.91)

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run-overcap.state").write_text(
            yaml.dump({"budget_status": "within", "budget_spend_usd": 31.44, "stories": []}),
            encoding="utf-8",
        )

        intake_calls: list[list] = []
        preflight_calls: list[list] = []

        def record_intake(tasks, root, **kwargs):
            intake_calls.append(list(tasks))
            return {}

        def record_preflight(tasks, *args, **kwargs):
            preflight_calls.append(list(tasks))
            return {}

        with (
            patch("theforge.sprint.runner._triage_spec", return_value=_retry_triage()),
            patch("theforge.sprint.runner.run_intake_remediation", side_effect=record_intake),
            patch(
                "theforge.sprint.runner._build_intake_agent_caller",
                return_value=(lambda *a, **k: None, ""),
            ),
            patch("theforge.sprint.runner.run_batch_preflight", side_effect=record_preflight),
            patch("theforge.sprint.runner.run_task") as run_task,
        ):
            result = run_sprint_ctx(config, manifest_path, reexec=True, run_id="run-overcap")

        assert run_task.call_count == 0, "no story may dispatch under an exhausted ceiling"
        assert all(not tasks for tasks in intake_calls), (
            "intake remediation spends agent money and must not run for a refused "
            f"run; it was handed {intake_calls}"
        )
        assert all(not tasks for tasks in preflight_calls), (
            "batch preflight spends agent money and must not run for a refused "
            f"run; it was handed {preflight_calls}"
        )
        assert result.specs_succeeded == 0

    def test_a_refused_run_settles_inherited_agents_before_skipping_their_stories(
        self, tmp_path: Path
    ) -> None:
        """A refusal that lets paid work continue is not a refusal.

        An inherited agent group is a process still spending inside a worktree
        this sprint owns. Marking its story skipped and exiting leaves it running
        against a ceiling the run has just declared exhausted, with no owner left
        to settle it.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=30.0)
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
                    "cost_usd": 21.91,
                }
            ],
        )
        _write_prior_sprint_audit(tmp_path, sprint_id, 21.91)

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run-refuse-reclaim.state").write_text(
            yaml.dump({"budget_status": "within", "budget_spend_usd": 31.44, "stories": []}),
            encoding="utf-8",
        )

        reclaimed: list[str] = []

        def record_reclaim(slug, **_kwargs):
            reclaimed.append(slug)
            return [4242]

        with (
            patch("theforge.sprint.runner._triage_spec", return_value=_retry_triage()),
            patch(
                "theforge.sprint.live_stories.reclaim_inherited_agents",
                side_effect=record_reclaim,
            ),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.run_task") as run_task,
        ):
            result = run_sprint_ctx(
                config, manifest_path, reexec=True, run_id="run-refuse-reclaim"
            )

        assert run_task.call_count == 0
        assert "feature-a" in reclaimed, (
            "the refusal must settle the story's inherited agent group before "
            f"recording it as skipped; reclaim was called for {reclaimed}"
        )
        assert result.specs_succeeded == 0


class TestSeededPriorCostSurvivesAnEarlyTerminal:
    def test_refused_prior_done_story_never_publishes_below_its_prior_cost(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The seeded branch of the early-terminal case.

        A prior-generation DONE story has its cost SEEDED onto the canonical row,
        not carried — so carried attribution is $0.00 for it. When it re-enters
        and is refused before dispatch, the accumulated row is replaced
        wholesale, and without reading the seed that replacement publishes $0.00
        over $6.00 of pre-restart spend until wrap-up.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=30.0)
        config = _make_config(tmp_path)
        sprint_id = _seed_prior_generation(tmp_path, outcome="DONE")
        _write_prior_sprint_audit(tmp_path, sprint_id, PRIOR_COST_USD)

        runs_dir = tmp_path / ".forge" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run-seeded-refused.state").write_text(
            yaml.dump({"budget_status": "within", "budget_spend_usd": 31.44, "stories": []}),
            encoding="utf-8",
        )

        published: list[list[dict]] = []
        _spy_on_accumulated_writes(monkeypatch, published)

        with (
            patch("theforge.sprint.runner._triage_spec", return_value=_retry_triage()),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.run_task") as run_task,
        ):
            run_sprint_ctx(config, manifest_path, reexec=True, run_id="run-seeded-refused")

        assert run_task.call_count == 0, "the run is over its ceiling and must not dispatch"

        costs = [
            row.get("cost_usd")
            for stories in published
            for row in stories
            if row.get("slug") == "feature-a"
        ]
        assert costs, "the refusal must have published an accumulated row for feature-a"
        assert all(c is not None and c >= PRIOR_COST_USD for c in costs), (
            "a seeded prior cost must not be replaced by $0.00, not even between "
            f"the refusal and wrap-up; saw {costs}"
        )

        accumulated = yaml.safe_load(
            (tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml").read_text(
                encoding="utf-8"
            )
        )
        rows = {row["slug"]: row for row in accumulated.get("stories", [])}
        assert rows["feature-a"]["cost_usd"] == pytest.approx(PRIOR_COST_USD), (
            "counted once — the seed is read, not consumed, so wrap-up does not "
            "add it a second time"
        )

    def test_seeded_prior_cost_survives_a_nonzero_intake_attribution(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Intake spend adds to a seeded prior cost — it does not replace it.

        A prior-generation DONE story carries its $6.00 as a SEED on the
        canonical row. ``register`` leaves that seed alone only while the
        incoming figure is falsy, so once a $1.00 intake-remediation attempt
        gives the story a nonzero attribution the initial live-state write
        overwrites $6.00 with $1.00 — while the ledger still counts $7.00.
        """
        _make_spec_file(tmp_path, "Feature A", "feature-a")
        manifest_path = _make_manifest(tmp_path, ["feature-a.md"], budget=100.0)
        config = _config_with_intake(tmp_path)
        sprint_id = _seed_prior_generation(tmp_path, outcome="DONE")
        _write_prior_sprint_audit(tmp_path, sprint_id, PRIOR_COST_USD)

        snapshots: list[dict] = []
        published: list[list[dict]] = []
        _spy_on_state_writes(monkeypatch, snapshots)
        _spy_on_accumulated_writes(monkeypatch, published)

        intake_cost = 1.0

        def fake_intake(_tasks, _root, **_kwargs):
            return {"feature-a": _dropped_intake_outcome_with_cost("feature-a", intake_cost)}

        with (
            patch("theforge.sprint.runner._triage_spec", return_value=_retry_triage()),
            patch("theforge.sprint.runner.run_intake_remediation", side_effect=fake_intake),
            patch(
                "theforge.sprint.runner._build_intake_agent_caller",
                return_value=(lambda *a, **k: None, ""),
            ),
            patch("theforge.sprint.runner.run_batch_preflight", return_value={}),
            patch("theforge.sprint.runner.run_task") as run_task,
        ):
            result = run_sprint_ctx(
                config, manifest_path, reexec=True, run_id="run-seeded-plus-intake"
            )

        assert run_task.call_count == 0, "the intake gate dropped the story before dispatch"
        expected = PRIOR_COST_USD + intake_cost

        live_costs = _row_costs(snapshots, "feature-a")
        assert live_costs, "the run must have written a live state row for feature-a"
        assert all(c is not None and c >= PRIOR_COST_USD for c in live_costs), (
            "a nonzero intake attribution must add to the seeded prior cost, not "
            f"replace it; saw {live_costs}"
        )
        assert live_costs[-1] == pytest.approx(expected)

        accumulated_costs = [
            row.get("cost_usd")
            for stories in published
            for row in stories
            if row.get("slug") == "feature-a"
        ]
        assert all(c is not None and c >= PRIOR_COST_USD for c in accumulated_costs), (
            f"the accumulated row must not regress below the prior spend; saw {accumulated_costs}"
        )

        accumulated = yaml.safe_load(
            (tmp_path / ".forge" / "sprints" / sprint_id / "state.yaml").read_text(
                encoding="utf-8"
            )
        )
        rows = {row["slug"]: row for row in accumulated.get("stories", [])}
        assert rows["feature-a"]["cost_usd"] == pytest.approx(expected)
        # The row and the ledger describe the same money — counted once each.
        assert result.total_cost_usd == pytest.approx(expected)
