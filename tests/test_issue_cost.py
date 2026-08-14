"""Issue-level cost aggregation and the surfaces that report it (#2365).

Two levels are covered here: the pure aggregation over run records (grouping,
carry-forward de-duplication, the unmeasured flag, the attempt count) and the
CLI seams that render it — the live sprint table, the postmortem digest, the
pending-decision list and the re-entry disclosure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.coordinator import audit_substrate
from theforge.coordinator.issue_cost import (
    IssueCostAggregate,
    aggregate_issue_cost,
    issue_number_from_slug,
    load_issue_cost,
)


def _record(
    run_id: str,
    slug: str,
    *,
    cost: float | None,
    started_at: str,
    issue_id: int | None = None,
    final_phase: str = "COMPLETE",
    verdict: str | None = None,
    landing_status: str | None = None,
    parent_run_id: str | None = None,
    prior_independently_recorded: bool | None = None,
) -> dict:
    """A minimal run record in the shape the substrate stores."""
    record: dict = {
        "run_id": run_id,
        "task": {"slug": slug},
        "timing": {"started_at": started_at, "finished_at": started_at},
        "cost": {"total_usd": cost},
        "outcome": {"final_phase": final_phase},
    }
    if issue_id is not None:
        record["task"]["github_issue"] = issue_id
    if verdict is not None:
        record["reviews"] = [{"cycle": 1, "verdict": verdict}]
    if landing_status is not None:
        record["landing_status"] = landing_status
    if parent_run_id is not None:
        record["parent_run_id"] = parent_run_id
    if prior_independently_recorded is not None:
        record["prior_generation"] = {
            "run_id": parent_run_id,
            "independently_recorded": prior_independently_recorded,
        }
    return record


# ── Pure aggregation ──────────────────────────────────────────────────────


class TestAggregateIssueCost:
    def test_sums_every_run_and_counts_attempts(self) -> None:
        agg = aggregate_issue_cost(
            [
                _record("r1", "issue-2365", cost=137.69, started_at="2026-08-01T00:00:00Z"),
                _record("r2", "issue-2365", cost=103.41, started_at="2026-08-02T00:00:00Z"),
                _record("r3", "issue-2365", cost=100.00, started_at="2026-08-03T00:00:00Z"),
            ],
            key="#2365",
        )
        assert agg is not None
        assert agg.attempts == 3
        assert agg.measured_total_usd == pytest.approx(341.10)
        assert agg.total_usd == pytest.approx(341.10)
        assert agg.complete is True
        assert agg.describe() == "$341.10 across 3 runs"

    def test_records_outcomes_in_run_order(self) -> None:
        agg = aggregate_issue_cost(
            [
                _record(
                    "r2",
                    "issue-1",
                    cost=2.0,
                    started_at="2026-08-02T00:00:00Z",
                    verdict="APPROVE",
                    landing_status="landed",
                ),
                _record(
                    "r1",
                    "issue-1",
                    cost=1.0,
                    started_at="2026-08-01T00:00:00Z",
                    verdict="REQUEST_CHANGES",
                ),
            ],
            key="#1",
        )
        assert agg is not None
        assert agg.outcomes == ("REQUEST_CHANGES", "landed")
        assert agg.run_ids == ("r1", "r2")

    def test_no_records_yields_none(self) -> None:
        assert aggregate_issue_cost([], key="#1") is None

    def test_single_run_reads_as_the_run_cost(self) -> None:
        agg = aggregate_issue_cost(
            [_record("r1", "issue-9", cost=12.61, started_at="2026-08-01T00:00:00Z")],
            key="#9",
        )
        assert agg is not None
        assert agg.attempts == 1
        assert agg.has_prior_attempts is False
        assert agg.describe() == "$12.61 across 1 run"

    def test_carried_forward_spend_is_counted_once(self) -> None:
        """A successor that folded in a parent's spend subsumes the parent row."""
        parent = _record("r1", "issue-2365", cost=50.0, started_at="2026-08-01T00:00:00Z")
        successor = _record(
            "r2",
            "issue-2365",
            # 50 carried from r1 + 30 of its own, as carry_prior_generation_work
            # writes it when the parent left no record of its own.
            cost=80.0,
            started_at="2026-08-02T00:00:00Z",
            parent_run_id="r1",
            prior_independently_recorded=False,
        )
        agg = aggregate_issue_cost([parent, successor], key="#2365")
        assert agg is not None
        assert agg.measured_total_usd == pytest.approx(80.0)
        assert agg.attempts == 1
        assert agg.run_ids == ("r2",)

    def test_independently_recorded_parent_is_still_counted(self) -> None:
        """When the parent reported its own dollars, the successor did not restate them."""
        parent = _record("r1", "issue-2365", cost=50.0, started_at="2026-08-01T00:00:00Z")
        successor = _record(
            "r2",
            "issue-2365",
            cost=30.0,
            started_at="2026-08-02T00:00:00Z",
            parent_run_id="r1",
            prior_independently_recorded=True,
        )
        agg = aggregate_issue_cost([parent, successor], key="#2365")
        assert agg is not None
        assert agg.measured_total_usd == pytest.approx(80.0)
        assert agg.attempts == 2

    def test_unmeasured_contributor_makes_the_total_a_lower_bound(self) -> None:
        agg = aggregate_issue_cost(
            [
                _record("r1", "issue-2365", cost=137.69, started_at="2026-08-01T00:00:00Z"),
                _record("r2", "issue-2365", cost=None, started_at="2026-08-02T00:00:00Z"),
            ],
            key="#2365",
        )
        assert agg is not None
        assert agg.complete is False
        assert agg.total_usd is None
        assert agg.measured_total_usd == pytest.approx(137.69)
        assert agg.describe() == "unknown (>= $137.69 measured) across 2 runs"

    def test_wholly_unmeasured_issue_never_renders_a_figure(self) -> None:
        agg = aggregate_issue_cost(
            [
                _record("r1", "issue-2365", cost=None, started_at="2026-08-01T00:00:00Z"),
                _record("r2", "issue-2365", cost=None, started_at="2026-08-02T00:00:00Z"),
            ],
            key="#2365",
        )
        assert agg is not None
        assert agg.describe() == "unknown across 2 runs"

    def test_duplicate_run_ids_collapse(self) -> None:
        rec = _record("r1", "issue-1", cost=5.0, started_at="2026-08-01T00:00:00Z")
        agg = aggregate_issue_cost([rec, dict(rec)], key="#1")
        assert agg is not None
        assert agg.attempts == 1
        assert agg.measured_total_usd == pytest.approx(5.0)


class TestIssueNumberFromSlug:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("issue-2365", 2365),
            ("#2365", 2365),
            ("Issue #2365", 2365),
            ("story-without-number", None),
            ("", None),
            (None, None),
        ],
    )
    def test_variants(self, text: object, expected: int | None) -> None:
        assert issue_number_from_slug(text) == expected


# ── Substrate-backed loading ──────────────────────────────────────────────


class TestLoadIssueCost:
    def test_missing_substrate_yields_none(self, tmp_path: Path) -> None:
        assert load_issue_cost(tmp_path, slug="issue-2365") is None

    def test_none_project_root_yields_none(self) -> None:
        assert load_issue_cost(None, slug="issue-2365") is None

    def test_unidentifiable_story_yields_none(self, tmp_path: Path) -> None:
        assert load_issue_cost(tmp_path, slug="") is None

    def test_sums_runs_recorded_for_the_issue(self, tmp_path: Path) -> None:
        audit_substrate.seed_records(
            tmp_path,
            [
                _record(
                    "r1", "issue-2365", cost=10.0, started_at="2026-08-01T00:00:00Z", issue_id=2365
                ),
                _record(
                    "r2", "issue-2365", cost=20.0, started_at="2026-08-02T00:00:00Z", issue_id=2365
                ),
            ],
        )
        agg = load_issue_cost(tmp_path, slug="issue-2365")
        assert agg is not None
        assert agg.key == "#2365"
        assert agg.attempts == 2
        assert agg.measured_total_usd == pytest.approx(30.0)

    def test_groups_a_run_that_recorded_no_issue_id_with_one_that_did(
        self, tmp_path: Path
    ) -> None:
        """``issue_id`` is nullable; the slug is the fallback join, not a second issue."""
        audit_substrate.seed_records(
            tmp_path,
            [
                _record(
                    "r1", "issue-2365", cost=10.0, started_at="2026-08-01T00:00:00Z", issue_id=2365
                ),
                _record("r2", "issue-2365", cost=20.0, started_at="2026-08-02T00:00:00Z"),
            ],
        )
        agg = load_issue_cost(tmp_path, slug="issue-2365")
        assert agg is not None
        assert agg.attempts == 2
        assert agg.measured_total_usd == pytest.approx(30.0)

    def test_distinct_slugs_without_issue_ids_are_not_merged(self, tmp_path: Path) -> None:
        audit_substrate.seed_records(
            tmp_path,
            [
                _record("r1", "add-widget", cost=10.0, started_at="2026-08-01T00:00:00Z"),
                _record("r2", "add-gadget", cost=20.0, started_at="2026-08-02T00:00:00Z"),
            ],
        )
        agg = load_issue_cost(tmp_path, slug="add-widget")
        assert agg is not None
        assert agg.key == "add-widget"
        assert agg.attempts == 1
        assert agg.measured_total_usd == pytest.approx(10.0)

    def test_lookup_by_issue_id_alone(self, tmp_path: Path) -> None:
        audit_substrate.seed_records(
            tmp_path,
            [
                _record(
                    "r1", "issue-2365", cost=10.0, started_at="2026-08-01T00:00:00Z", issue_id=2365
                ),
            ],
        )
        agg = load_issue_cost(tmp_path, issue_id=2365)
        assert agg is not None
        assert agg.attempts == 1

    def test_carry_forward_dedup_survives_the_substrate_round_trip(self, tmp_path: Path) -> None:
        audit_substrate.seed_records(
            tmp_path,
            [
                _record(
                    "r1", "issue-2365", cost=50.0, started_at="2026-08-01T00:00:00Z", issue_id=2365
                ),
                _record(
                    "r2",
                    "issue-2365",
                    cost=80.0,
                    started_at="2026-08-02T00:00:00Z",
                    issue_id=2365,
                    parent_run_id="r1",
                    prior_independently_recorded=False,
                ),
            ],
        )
        agg = load_issue_cost(tmp_path, slug="issue-2365")
        assert agg is not None
        assert agg.measured_total_usd == pytest.approx(80.0)
        assert agg.attempts == 1

    def test_unmeasured_run_flags_the_substrate_aggregate(self, tmp_path: Path) -> None:
        audit_substrate.seed_records(
            tmp_path,
            [
                _record(
                    "r1", "issue-2365", cost=10.0, started_at="2026-08-01T00:00:00Z", issue_id=2365
                ),
                _record(
                    "r2", "issue-2365", cost=None, started_at="2026-08-02T00:00:00Z", issue_id=2365
                ),
            ],
        )
        agg = load_issue_cost(tmp_path, slug="issue-2365")
        assert agg is not None
        assert agg.complete is False
        assert "unknown" in agg.describe()


def _seed_multi_run_issue(project_root: Path, slug: str = "issue-2365") -> None:
    audit_substrate.seed_records(
        project_root,
        [
            _record("r1", slug, cost=137.69, started_at="2026-08-01T00:00:00Z", issue_id=2365),
            _record("r2", slug, cost=103.41, started_at="2026-08-02T00:00:00Z", issue_id=2365),
            _record("r3", slug, cost=100.00, started_at="2026-08-03T00:00:00Z", issue_id=2365),
        ],
    )


def _seed_single_run_issue(project_root: Path, slug: str = "issue-2365") -> None:
    audit_substrate.seed_records(
        project_root,
        [_record("r1", slug, cost=137.69, started_at="2026-08-01T00:00:00Z", issue_id=2365)],
    )


# ── Re-entry disclosure ───────────────────────────────────────────────────


class TestReentryDisclosure:
    def test_issue_total_and_next_attempt_number_at_re_entry(self, tmp_path: Path) -> None:
        from theforge.cli.reentry_display import reentry_lines

        _seed_multi_run_issue(tmp_path)
        lines = reentry_lines(tmp_path, "issue-2365")
        assert lines == ["    issue to date: $341.10 across 3 runs  (next would be run 4)"]

    def test_single_run_story_discloses_nothing_new(self, tmp_path: Path) -> None:
        from theforge.cli.reentry_display import reentry_lines

        _seed_single_run_issue(tmp_path)
        assert reentry_lines(tmp_path, "issue-2365") == []

    def test_unmeasured_contributor_is_named_at_re_entry(self, tmp_path: Path) -> None:
        from theforge.cli.reentry_display import reentry_lines

        audit_substrate.seed_records(
            tmp_path,
            [
                _record(
                    "r1",
                    "issue-2365",
                    cost=137.69,
                    started_at="2026-08-01T00:00:00Z",
                    issue_id=2365,
                ),
                _record(
                    "r2", "issue-2365", cost=None, started_at="2026-08-02T00:00:00Z", issue_id=2365
                ),
            ],
        )
        (line,) = reentry_lines(tmp_path, "issue-2365")
        assert "unknown (>= $137.69 measured) across 2 runs" in line

    def test_no_substrate_still_renders_nothing(self, tmp_path: Path) -> None:
        from theforge.cli.reentry_display import reentry_lines

        assert reentry_lines(tmp_path, "issue-2365") == []


class TestPendingDecisionSurface:
    def test_pending_decision_carries_the_issue_total(self, tmp_path: Path) -> None:
        from theforge.cli.status import _pending_reentry_lines

        _seed_multi_run_issue(tmp_path)
        lines = _pending_reentry_lines(tmp_path, "issue-2365")
        assert any("issue to date: $341.10 across 3 runs" in line for line in lines)

    def test_unknown_story_is_skipped(self, tmp_path: Path) -> None:
        from theforge.cli.status import _pending_reentry_lines

        _seed_multi_run_issue(tmp_path)
        assert _pending_reentry_lines(tmp_path, "?") == []


# ── Digest ────────────────────────────────────────────────────────────────


class TestDigestStoryRow:
    def test_run_cost_is_preserved_and_the_issue_total_appended(self, tmp_path: Path) -> None:
        from theforge.cli.sprint_digest import _story_row

        _seed_multi_run_issue(tmp_path)
        story = {"slug": "issue-2365", "cost_usd": 100.00}
        row = _story_row(story, tmp_path)
        assert "$100.00" in row
        assert "[issue: $341.10 across 3 runs]" in row

    def test_single_run_row_is_unchanged(self, tmp_path: Path) -> None:
        from theforge.cli.sprint_digest import _story_row

        _seed_single_run_issue(tmp_path)
        story = {"slug": "issue-2365", "cost_usd": 137.69}
        assert _story_row(story, tmp_path) == _story_row(story)

    def test_no_project_root_leaves_the_row_alone(self, tmp_path: Path) -> None:
        from theforge.cli.sprint_digest import _story_row

        _seed_multi_run_issue(tmp_path)
        assert "[issue:" not in _story_row({"slug": "issue-2365", "cost_usd": 1.0})


# ── Live sprint table ─────────────────────────────────────────────────────


class _Entry:
    def __init__(self, slug: str, cost_usd: float | None) -> None:
        self.slug = slug
        self.path = slug
        self.status = "failed"
        self.phase = "REVIEW"
        self.stage = ""
        self.cost_usd = cost_usd
        self.elapsed_seconds = 60.0
        self.detail = ""
        self.complexity = None
        self.complexity_score = None
        self.model = None
        self.outstanding_phases: list[str] = []
        self.reentry_note = ""


class TestSprintStatusStoryLine:
    def test_run_cost_column_kept_and_issue_total_disclosed(self, tmp_path: Path, capsys) -> None:
        from theforge.cli.sprint_status import _print_story_line

        _seed_multi_run_issue(tmp_path)
        _print_story_line(
            _Entry("issue-2365", 100.00), {"failed": "✗"}, indent=0, project_root=tmp_path
        )
        out = capsys.readouterr().out
        assert "$100.00" in out
        assert "issue to date: $341.10 across 3 runs" in out

    def test_single_run_story_line_gains_nothing(self, tmp_path: Path, capsys) -> None:
        from theforge.cli.sprint_status import _print_story_line

        _seed_single_run_issue(tmp_path)
        _print_story_line(
            _Entry("issue-2365", 137.69), {"failed": "✗"}, indent=0, project_root=tmp_path
        )
        out = capsys.readouterr().out
        assert "$137.69" in out
        assert "issue to date" not in out

    def test_without_project_root_the_line_is_unchanged(self, capsys) -> None:
        from theforge.cli.sprint_status import _print_story_line

        _print_story_line(_Entry("issue-2365", 100.00), {"failed": "✗"}, indent=0)
        out = capsys.readouterr().out
        assert "$100.00" in out
        assert "issue to date" not in out


def test_aggregate_is_a_value_type() -> None:
    """Frozen so a surface cannot mutate the figure it was handed."""
    agg = IssueCostAggregate(key="#1", attempts=2, measured_total_usd=1.0)
    with pytest.raises(Exception):
        agg.attempts = 3  # type: ignore[misc]
