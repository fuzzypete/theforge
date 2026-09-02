"""Tests for the knowledge-effectiveness payload and terminal view (#1867).

The renderer's contract is narrow but load-bearing: the structured payload must
carry every fact the terminal view shows (so a JSON consumer and an operator
never disagree), and an under-sampled metric must never be printed as a bare
number an operator would read as a finding.
"""

from __future__ import annotations

from tests.knowledge_effectiveness_test_helpers import cohorts, record
from theforge.knowledge_effectiveness import (
    METRIC_COST_PER_STORY,
    METRIC_NAMES,
    METRIC_PLAN_REGENERATION,
    METRIC_STORIES_PER_DOLLAR,
    build_report,
)
from theforge.knowledge_effectiveness_render import render_terminal, report_payload

# ── Structured payload ────────────────────────────────────────────────


class TestReportPayload:
    def test_payload_carries_the_window_and_cohort_counts(self) -> None:
        records = [*cohorts({}, {}), record("legacy", cohort="unclassified")]
        payload = report_payload(
            build_report(records, since="2026-08-01", until="2026-08-31", recent_run_count=25)
        )

        assert payload["window"] == {
            "since": "2026-08-01",
            "until": "2026-08-31",
            "recent_run_count": 25,
            "records_considered": 7,
        }
        assert payload["cohorts"] == {
            "with_prior_summary": 3,
            "without_prior_summary": 3,
            "unclassified": 1,
        }

    def test_payload_reports_every_metric_on_both_sides(self) -> None:
        payload = report_payload(build_report(cohorts({"cost": 2.0}, {"cost": 8.0})))

        assert [item["metric"] for item in payload["matched_comparison"]] == list(METRIC_NAMES)
        for item in payload["matched_comparison"]:
            assert set(item["with_prior_summary"]) == {
                "value",
                "sample_size",
                "available",
                "comparable",
                "lower_is_better",
            }
        cost = next(
            item
            for item in payload["matched_comparison"]
            if item["metric"] == METRIC_COST_PER_STORY
        )
        assert cost["with_prior_summary"]["value"] == 2.0
        assert cost["without_prior_summary"]["value"] == 8.0
        assert cost["comparative_claim_supported"] is False
        assert "descriptive only" in cost["comparison_note"]
        assert cost["delta"] is None
        assert cost["improved"] is None

    def test_payload_separates_overall_from_matched_populations(self) -> None:
        records = [
            *cohorts({}, {}),
            # Unmatched: no control counterpart in this bucket.
            record("lonely", cohort="with", work_type="bug", complexity="small"),
        ]
        payload = report_payload(build_report(records))

        assert payload["overall"]["with_prior_summary"]["run_count"] == 4
        assert payload["overall"]["with_prior_summary"]["measured_cost_run_count"] == 4
        assert payload["matched"]["with_prior_summary"]["run_count"] == 3
        assert payload["matched_buckets"] == [
            {
                "work_type": "feature",
                "complexity": "MEDIUM",
                "domains": ["backend"],
                "with_prior_runs": 3,
                "without_prior_runs": 3,
            }
        ]

    def test_payload_states_the_status_and_its_reason(self) -> None:
        payload = report_payload(
            build_report(cohorts({"dev_iterations": 1}, {"dev_iterations": 3}))
        )

        assert payload["status"] == "insufficient_data"
        assert "unreachable" in payload["status_reason"]

    def test_payload_carries_the_trend_points(self) -> None:
        records = [
            record("early", cohort="without", cost=10.0, started_at="2026-08-01T00:00:00+00:00"),
            record("late", cohort="with", cost=2.0, started_at="2026-08-04T00:00:00+00:00"),
        ]
        payload = report_payload(build_report(records))

        assert [point["label"] for point in payload["stories_per_dollar_trend"]] == [
            "earlier",
            "later",
        ]
        assert payload["stories_per_dollar_trend"][1]["stories_per_dollar"] == 0.5
        assert payload["stories_per_dollar_trend"][1]["measured_cost_run_count"] == 1

    def test_payload_is_json_serializable(self) -> None:
        import json

        payload = report_payload(build_report(cohorts({}, {})))

        assert json.loads(json.dumps(payload))["status"] == "insufficient_data"


# ── Terminal renderer ─────────────────────────────────────────────────


class TestRenderTerminal:
    def test_header_states_the_window_and_cohort_split(self) -> None:
        records = [*cohorts({}, {}), record("legacy", cohort="unclassified")]
        out = render_terminal(build_report(records, recent_run_count=30))

        assert "Knowledge-loop effectiveness" in out
        assert "most recent 30 run(s)" in out
        assert "3 with prior summaries, 3 without, 1 unclassified" in out
        assert out.endswith("\n")

    def test_window_label_falls_back_to_dates_then_to_all_runs(self) -> None:
        dated = render_terminal(build_report(cohorts({}, {}), since="2026-08-01"))
        undated = render_terminal(build_report(cohorts({}, {})))

        assert "2026-08-01 → now" in dated
        assert "all recorded runs" in undated

    def test_metric_rows_are_formatted_by_kind(self) -> None:
        out = render_terminal(
            build_report(cohorts({"cost": 2.0, "plan_regenerated": False}, {"cost": 8.0}))
        )

        assert "$2.00 (n=3)" in out  # money
        assert "0% (n=3)" in out  # rate
        assert "1.00 (n=3)" in out  # plain average
        assert "descriptive only" in out

    def test_under_sampled_metrics_read_as_insufficient_not_as_zero(self) -> None:
        """A metric nobody should act on must not render as a number."""
        out = render_terminal(build_report(cohorts({}, {}, count=1)))

        assert "insufficient_data" in out
        assert "descriptive only" in out
        assert "0.00" not in out

    def test_missing_denominator_renders_as_a_dash(self) -> None:
        records = cohorts({}, {})
        for entry in records:
            del entry["plan_review"]
        out = render_terminal(build_report(records))

        plan_row = next(line for line in out.splitlines() if "plan regeneration rate" in line)
        assert "—" in plan_row
        assert "descriptive only" in plan_row

    def test_current_comparison_rows_are_descriptive_only(self) -> None:
        out = render_terminal(build_report(cohorts({"dev_iterations": 1}, {"dev_iterations": 3})))

        assert "comparison mode: descriptive only" in out
        assert "descriptive only" in out
        assert "improved (" not in out
        assert "not improved (" not in out
        assert "unchanged" not in out

    def test_unmatched_buckets_are_stated_rather_than_left_blank(self) -> None:
        records = [record(f"with-{i}", cohort="with") for i in range(3)]
        out = render_terminal(build_report(records))

        assert "none — no bucket holds runs from both cohorts" in out

    def test_matched_buckets_are_listed(self) -> None:
        out = render_terminal(build_report(cohorts({}, {})))

        assert "feature/MEDIUM/backend: 3 with, 3 without" in out

    def test_trend_section_is_omitted_when_there_is_nothing_to_trend(self) -> None:
        single = render_terminal(build_report([record("only", cohort="with")]))
        paired = render_terminal(build_report(cohorts({"cost": 2.0}, {"cost": 8.0})))

        assert "Stories per dollar over the window" not in single
        assert "Stories per dollar over the window" in paired

    def test_stories_per_dollar_is_rendered_when_measurable(self) -> None:
        out = render_terminal(build_report(cohorts({"cost": 2.0}, {"cost": 8.0})))

        stories_row = next(
            line for line in out.splitlines() if "stories per dollar" in line.lower()
        )
        assert "0.50 (n=3)" in stories_row
        assert METRIC_STORIES_PER_DOLLAR not in stories_row
        assert "unreachable" in out.splitlines()[-1]

    def test_cost_telemetry_and_unmeasured_exclusions_are_rendered(self) -> None:
        records = [
            *cohorts({"cost": 2.0}, {"cost": 8.0}),
            record("with-failed", cohort="with", success=False, cost=5.0),
            record("without-unmeasured", cohort="without", cost=None),
        ]
        out = render_terminal(build_report(records))

        assert "cost telemetry: 4 measured, 0 unmeasured excluded with prior" in out
        assert "3 measured, 1 unmeasured excluded without prior" in out

    def test_trend_renders_unmeasured_exclusion_counts(self) -> None:
        records = [
            record("early", cohort="with", cost=4.0, started_at="2026-08-01T00:00:00+00:00"),
            record(
                "late-failed",
                cohort="with",
                success=False,
                cost=6.0,
                started_at="2026-08-02T00:00:00+00:00",
            ),
            record(
                "late-unmeasured",
                cohort="with",
                cost=None,
                started_at="2026-08-03T00:00:00+00:00",
            ),
        ]
        out = render_terminal(build_report(records))

        assert "$6.00 measured, 1 unmeasured excluded" in out

    def test_empty_report_renders_without_raising(self) -> None:
        out = render_terminal(build_report([]))

        assert "Records: 0" in out
        assert "insufficient_data" in out
        assert METRIC_PLAN_REGENERATION not in out
