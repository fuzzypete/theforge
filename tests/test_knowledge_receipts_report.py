"""The receipt distribution over a run window, and its refusals (#2866)."""

from __future__ import annotations

from theforge.knowledge_receipts import (
    OUTCOME_CORROBORATED_USE,
    OUTCOME_UNCORROBORATED_USE,
)
from theforge.knowledge_receipts_report import (
    build_receipt_distribution,
    render_terminal,
    report_payload,
)


def _record(**counts: int) -> dict:
    base = {
        "phases_with_injected_knowledge": 0,
        "phases_debriefed": 0,
        "phases_undebriefed": 0,
        "phases_nothing_to_debrief": 0,
        "claims_injected": 0,
        OUTCOME_CORROBORATED_USE: 0,
        OUTCOME_UNCORROBORATED_USE: 0,
        "confirmed_approach": 0,
        "already_known": 0,
        "irrelevant": 0,
        "stale_or_wrong": 0,
        "unaddressed_claims": 0,
        "unmatched_citations": 0,
        "unrecognised_dispositions": 0,
    }
    base.update(counts)
    return {"knowledge_receipts": {"status": "captured", "counts": base}}


class TestAggregation:
    def test_counts_sum_across_runs(self) -> None:
        distribution = build_receipt_distribution(
            [
                _record(claims_injected=3, **{OUTCOME_CORROBORATED_USE: 1}),
                _record(claims_injected=2, **{OUTCOME_CORROBORATED_USE: 2}),
            ]
        )
        assert distribution["runs_counted"] == 2
        assert distribution["counts"]["claims_injected"] == 5
        assert distribution["counts"][OUTCOME_CORROBORATED_USE] == 3

    def test_pre_instrument_runs_are_excluded_rather_than_counted_as_zero(self) -> None:
        distribution = build_receipt_distribution(
            [
                _record(claims_injected=4),
                {"knowledge_receipts": {"status": "uncomparable_pre_capture", "counts": None}},
                {"run_id": "ancient"},
            ]
        )
        assert distribution["runs_counted"] == 1
        assert distribution["runs_uncomparable"] == 1
        assert distribution["runs_without_receipt_block"] == 1
        assert distribution["counts"]["claims_injected"] == 4

    def test_the_structured_payload_and_the_terminal_view_share_one_source(self) -> None:
        distribution = build_receipt_distribution([_record(claims_injected=7)])
        assert report_payload(distribution)["counts"]["claims_injected"] == 7
        assert "7" in render_terminal(distribution)


class TestRenderingRefusals:
    def _rendered(self) -> str:
        return render_terminal(
            build_receipt_distribution(
                [
                    _record(
                        phases_with_injected_knowledge=2,
                        phases_debriefed=1,
                        phases_undebriefed=1,
                        claims_injected=10,
                        already_known=3,
                        unaddressed_claims=2,
                        unmatched_citations=1,
                        **{OUTCOME_CORROBORATED_USE: 3, OUTCOME_UNCORROBORATED_USE: 1},
                    )
                ]
            )
        )

    def test_the_two_use_populations_are_reported_separately(self) -> None:
        rendered = self._rendered()
        assert "corroborated use claims" in rendered
        assert "uncorroborated use claims" in rendered

    def test_no_readout_sums_the_two_use_populations(self) -> None:
        """3 corroborated + 1 uncorroborated must never appear as a 4."""
        distribution = build_receipt_distribution(
            [_record(**{OUTCOME_CORROBORATED_USE: 3, OUTCOME_UNCORROBORATED_USE: 1})]
        )
        assert "use_claims_total" not in distribution["counts"]
        assert set(distribution["counts"]) == {
            "phases_with_injected_knowledge",
            "phases_debriefed",
            "phases_undebriefed",
            "phases_nothing_to_debrief",
            "claims_injected",
            OUTCOME_CORROBORATED_USE,
            OUTCOME_UNCORROBORATED_USE,
            "confirmed_approach",
            "already_known",
            "irrelevant",
            "stale_or_wrong",
            "unaddressed_claims",
            "unmatched_citations",
            "unrecognised_dispositions",
        }

    def test_nothing_is_named_verified_confirmed_or_effective_use(self) -> None:
        rendered = self._rendered().lower()
        for forbidden in ("verified use", "confirmed use", "effective use"):
            assert forbidden not in rendered

    def test_absence_is_never_rendered_as_unused(self) -> None:
        rendered = self._rendered().lower()
        assert "unused" not in rendered
        assert "undebriefed" in rendered
        assert "unaddressed" in rendered
        assert "nothing to debrief" in rendered

    def test_the_disclaimer_is_always_present_and_no_conclusion_is_drawn(self) -> None:
        rendered = self._rendered()
        assert "No effectiveness or ROI conclusion follows." in rendered
        assert "Corroborated uptake claims: 3 of 10 exposed claims." in rendered

    def test_an_empty_window_says_so_rather_than_printing_a_table_of_zeroes(self) -> None:
        rendered = render_terminal(build_receipt_distribution([]))
        assert "no run in this window carries a receipt block" in rendered
        assert "claims injected" not in rendered
