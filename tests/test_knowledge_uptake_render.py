"""Rendering of the prior-run uptake indicator (#2684).

The renderer carries obligations the computation cannot: the acceptance
criteria constrain the *words* a reader sees, not just the numbers behind them.
An unmatched finding must never read as novel, every block must carry the
missed-uptake framing, and "nothing to compare" must not render like "compared,
matched nothing". Those are properties of this module alone, so they are tested
here rather than through the core.

Most cases build the report dict by hand: that is the renderer's actual input
contract, and it reaches branches (an absent claim reference, an unattributed
recipient) that the computation does not currently produce but the renderer
must still survive. A final group renders reports built by
``build_uptake_report`` so the renderer cannot drift from the shape the
pipeline really emits.
"""

from __future__ import annotations

import pytest

from theforge import knowledge_uptake as ku
from theforge.knowledge_uptake_render import render_run_uptake

_MEASURED = {"status": ku.VALIDATION_MEASURED, "agreement": 0.909, "n": 11}
_UNVALIDATED = {"status": ku.VALIDATION_UNVALIDATED, "reason": "not_measured", "agreement": None}
_METHOD = {"name": ku.METHOD_NAME, "version": ku.METHOD_VERSION}


def _base(**overrides) -> dict:
    report = {
        "status": ku.STATUS_COMPARED,
        "method": _METHOD,
        "author_role": "dev",
        "validation": _MEASURED,
        "interpretation": ku.INTERPRETATION_NOTE,
        "claims_rendered": 9,
        "claims_rendered_by_recipient": [
            {"agent_role": "dev", "phase": "dev", "phase_iteration": 1, "count": 7},
            {"agent_role": "review", "phase": "review", "phase_iteration": 1, "count": 2},
        ],
        "claims_eligible": 7,
        "claims_excluded": [{"reason": "rendered_to_review_only_not_author", "count": 2}],
        "review_findings": 4,
        "counts": {
            ku.OUTCOME_MATCHED: 1,
            ku.OUTCOME_NOT_MATCHED: 3,
            ku.OUTCOME_INDETERMINATE: 0,
        },
        "correspondences": [
            {
                "outcome": ku.OUTCOME_MATCHED,
                "finding_id": "a",
                "claim_index": 3,
                "claim_ref": "27bb13e86070:a1b2c3d4e5f6",
                "claim_run_id": "27bb13e86070",
                "claim_agent_role": "dev",
                "claim_phase_iteration": 1,
            },
            {"outcome": ku.OUTCOME_NOT_MATCHED, "finding_id": "b"},
        ],
    }
    report.update(overrides)
    return report


# ── Required wording ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "report"),
    [
        ("compared", _base()),
        (
            "no_eligible",
            _base(status=ku.STATUS_NO_ELIGIBLE_CLAIMS, counts=None, correspondences=None),
        ),
        (
            "no_findings",
            _base(status=ku.STATUS_NO_REVIEW_FINDINGS, counts=None, correspondences=None),
        ),
        (
            "uncomparable",
            _base(
                status=ku.STATUS_UNCOMPARABLE,
                claims_rendered=None,
                claims_eligible=None,
                counts=None,
                correspondences=None,
            ),
        ),
    ],
)
def test_every_status_carries_the_missed_uptake_framing(label: str, report: dict) -> None:
    """No status may render a figure without the sentence that bounds it."""
    assert ku.INTERPRETATION_NOTE in render_run_uptake(report), label


@pytest.mark.parametrize("word", ["novel", "new finding", "helped", "failed to help", "ignored"])
def test_no_status_describes_a_finding_as_novel_or_knowledge_as_helping(word: str) -> None:
    statuses = [
        _base(),
        _base(status=ku.STATUS_NO_ELIGIBLE_CLAIMS, counts=None, correspondences=None),
        _base(status=ku.STATUS_NO_REVIEW_FINDINGS, counts=None, correspondences=None),
        _base(status=ku.STATUS_UNCOMPARABLE, counts=None, correspondences=None),
    ]
    for report in statuses:
        assert word not in render_run_uptake(report).lower(), report["status"]


def test_unmatched_bucket_is_spelled_as_absence_of_a_correspondence() -> None:
    text = render_run_uptake(_base())
    assert "not matched to an eligible injected claim   3" in text


def test_every_status_names_the_method_and_version() -> None:
    for status in (
        ku.STATUS_COMPARED,
        ku.STATUS_NO_ELIGIBLE_CLAIMS,
        ku.STATUS_NO_REVIEW_FINDINGS,
        ku.STATUS_UNCOMPARABLE,
    ):
        text = render_run_uptake(_base(status=status, counts=None, correspondences=None))
        assert f"{ku.METHOD_NAME} {ku.METHOD_VERSION}" in text, status


# ── Status distinctions ──────────────────────────────────────────────────────


def test_compared_block_reports_all_three_outcome_totals() -> None:
    text = render_run_uptake(_base())

    assert "corresponding to an eligible claim   1" in text
    assert "not matched to an eligible injected claim   3" in text
    # The indeterminate count sits beside the totals even when it is zero.
    assert "indeterminate   0" in text


def test_no_eligible_claims_shows_zero_and_no_correspondence_block() -> None:
    """The spec's explicit contrast: not a block reporting zero correspondences."""
    text = render_run_uptake(
        _base(
            status=ku.STATUS_NO_ELIGIBLE_CLAIMS,
            claims_eligible=0,
            claims_excluded=[],
            counts=None,
            correspondences=None,
        )
    )

    assert "claims eligible         0" in text
    assert "no correspondence computed" in text
    assert "corresponding to an eligible claim" not in text
    assert "not matched to an eligible injected claim" not in text


def test_no_review_findings_says_so_rather_than_rendering_totals() -> None:
    text = render_run_uptake(
        _base(
            status=ku.STATUS_NO_REVIEW_FINDINGS,
            review_findings=0,
            counts=None,
            correspondences=None,
        )
    )

    assert "the review recorded no findings" in text
    assert "corresponding to an eligible claim" not in text


def test_compared_with_no_matches_still_renders_the_correspondence_block() -> None:
    """Compared-and-matched-nothing must not render like nothing-to-compare."""
    text = render_run_uptake(
        _base(
            counts={
                ku.OUTCOME_MATCHED: 0,
                ku.OUTCOME_NOT_MATCHED: 4,
                ku.OUTCOME_INDETERMINATE: 0,
            },
            correspondences=[{"outcome": ku.OUTCOME_NOT_MATCHED, "finding_id": "a"}],
        )
    )

    assert "corresponding to an eligible claim   0" in text
    assert "no correspondence computed" not in text


def test_uncomparable_refuses_to_show_counts_it_does_not_have() -> None:
    text = render_run_uptake(
        _base(
            status=ku.STATUS_UNCOMPARABLE,
            claims_rendered=None,
            claims_eligible=None,
            counts=None,
            correspondences=None,
            validation=_UNVALIDATED,
        )
    )

    assert "uncomparable — run predates claim-exposure capture" in text
    assert "not reported as corresponding to nothing" in text
    assert "claims rendered" not in text
    assert "not matched to an eligible injected claim" not in text


# ── Claim citation ───────────────────────────────────────────────────────────


def test_matched_finding_cites_the_claim_it_corresponds_to() -> None:
    text = render_run_uptake(_base())
    assert "-> claim 3, ref 27bb13e86070:a1b2c3d4e5f6, run 27bb13e86070, dev iteration 1" in text


def test_only_matched_findings_are_cited() -> None:
    text = render_run_uptake(_base())
    assert text.count("-> ") == 1


def test_citation_degrades_rather_than_inventing_a_reference() -> None:
    text = render_run_uptake(
        _base(
            counts={ku.OUTCOME_MATCHED: 1, ku.OUTCOME_NOT_MATCHED: 0, ku.OUTCOME_INDETERMINATE: 0},
            correspondences=[{"outcome": ku.OUTCOME_MATCHED, "finding_id": "a"}],
        )
    )
    assert "claim reference unavailable" in text


# ── Breakdown lines ──────────────────────────────────────────────────────────


def test_recipient_breakdown_shows_which_role_and_iteration_received_claims() -> None:
    text = render_run_uptake(_base())
    assert "claims rendered         9   (7 to dev iter 1, 2 to review iter 1)" in text


def test_exclusion_reasons_are_named_beside_the_eligible_count() -> None:
    text = render_run_uptake(_base())
    assert "claims eligible         7   (excluded: 2 rendered_to_review_only_not_author)" in text


def test_breakdowns_are_omitted_when_empty_rather_than_rendered_blank() -> None:
    text = render_run_uptake(_base(claims_rendered_by_recipient=[], claims_excluded=[]))
    assert "claims rendered         9\n" in text
    assert "(excluded:" not in text


def test_unattributed_recipient_is_named_as_such() -> None:
    text = render_run_uptake(
        _base(
            claims_rendered_by_recipient=[
                {"agent_role": "", "phase": "dev", "phase_iteration": 1, "count": 2}
            ]
        )
    )
    assert "2 to unattributed iter 1" in text


# ── Validation line ──────────────────────────────────────────────────────────


def test_measured_agreement_is_reported_with_its_sample_size() -> None:
    text = render_run_uptake(_base())
    assert "agreement with labelled set 0.909 (n=11)" in text


def test_unmeasured_agreement_marks_figures_unvalidated_without_hiding_them() -> None:
    text = render_run_uptake(_base(validation=_UNVALIDATED))

    assert "UNVALIDATED (agreement not measured: not_measured)" in text
    # The figures the method produced are still shown.
    assert "corresponding to an eligible claim   1" in text


def test_method_version_mismatch_reason_reaches_the_reader() -> None:
    text = render_run_uptake(
        _base(
            validation={
                "status": ku.VALIDATION_UNVALIDATED,
                "reason": "labelled_set_method_mismatch",
                "agreement": None,
            }
        )
    )
    assert "labelled_set_method_mismatch" in text


# ── Degenerate input ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "not a report", 42])
def test_non_report_input_renders_nothing_rather_than_raising(value: object) -> None:
    assert render_run_uptake(value) == ""  # type: ignore[arg-type]


def test_absent_counts_render_as_unknown_not_as_zero() -> None:
    text = render_run_uptake(_base(claims_rendered=None, claims_eligible=None))
    assert "claims rendered         —" in text
    assert "claims rendered         0" not in text


# ── Fidelity to the real pipeline ────────────────────────────────────────────


def _pipeline_report(claims: list[dict], findings: list[dict]) -> dict:
    manifest = {
        "phase": "dev",
        "prior_run_context": {
            "enabled": True,
            "claim_exposure": {"capture_version": 1},
            "included": [{"run_id": "27bb13e86070", "claims": claims}],
            "dropped": [],
        },
    }
    return ku.build_uptake_report(context_manifests=[manifest], findings=findings)


def test_renders_a_report_produced_by_the_real_computation() -> None:
    """Guards against the renderer and the core disagreeing about field names."""
    claim = {
        "claim_ref": "27bb13e86070:abc123abc123",
        "index": 1,
        "claim": (
            "A coordinator-owned gate result is not derivable from an agent handoff "
            "report, so the coordinator has to record the gate result itself."
        ),
        "run_id": "27bb13e86070",
        "phase": "dev",
        "agent_role": "dev",
        "phase_iteration": 2,
        "rendered_at": "2026-08-01T10:00:00+00:00",
    }
    finding = {
        "finding_id": "f1",
        "description": (
            "A coordinator-owned gate result is not derivable from the agent handoff "
            "report; the coordinator must record the gate result itself."
        ),
        "severity": "P1",
        "recorded_at": "2026-08-01T14:00:00+00:00",
    }
    text = render_run_uptake(_pipeline_report([claim], [finding]))

    assert "corresponding to an eligible claim   1" in text
    assert "ref 27bb13e86070:abc123abc123" in text
    assert "dev iteration 2" in text
    assert ku.INTERPRETATION_NOTE in text


def test_renders_the_real_no_eligible_claims_report() -> None:
    text = render_run_uptake(
        _pipeline_report([], [{"finding_id": "f", "description": "a b c d e"}])
    )

    assert "claims eligible         0" in text
    assert "no correspondence computed" in text
    assert ku.INTERPRETATION_NOTE in text
