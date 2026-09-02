"""Prior-run uptake: claim exposure, eligibility, correspondence, reporting (#2684).

The behaviour under test is a *comparison of two recorded artifacts*, so these
tests build the artifacts directly and assert what the record then says. What
they guard hardest is the set of distinctions the indicator is worthless
without: eligible vs merely-present claims, unmatched vs indeterminate,
compared-and-found-nothing vs nothing-to-compare vs never-recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from theforge import knowledge_uptake as ku
from theforge.knowledge_uptake_render import render_run_uptake

_GATE_CLAIM = (
    "A coordinator-owned gate result is not derivable from an agent handoff "
    "report, so the coordinator has to record the gate result itself."
)
_GATE_FINDING = (
    "A coordinator-owned gate result is not derivable from the agent handoff "
    "report; the coordinator must record the gate result itself."
)
_STALENESS = (
    "The branch is stale against the base branch and staleness is unchecked "
    "before auto-merge is armed."
)


def _claim(
    text: str,
    *,
    role: str = "dev",
    phase: str = "dev",
    iteration: int = 1,
    rendered_at: str | None = "2026-08-01T10:00:00+00:00",
    run_id: str = "27bb13e86070",
    index: int = 1,
) -> dict:
    return {
        "claim_ref": f"{run_id}:{abs(hash(text)) % 10**12:012d}",
        "index": index,
        "claim": text,
        "run_id": run_id,
        "phase": phase,
        "agent_role": role,
        "phase_iteration": iteration,
        "rendered_at": rendered_at,
    }


def _manifest(claims: list[dict], *, phase: str = "dev", captured: bool = True) -> dict:
    prior: dict = {
        "enabled": True,
        "included": [{"run_id": "27bb13e86070", "claims": claims}],
        "dropped": [],
    }
    if captured:
        prior["claim_exposure"] = {"capture_version": 1}
    return {"phase": phase, "prior_run_context": prior}


def _finding(
    description: str,
    *,
    finding_id: str = "f1",
    recorded_at: str | None = "2026-08-01T14:00:00+00:00",
) -> dict:
    return {
        "finding_id": finding_id,
        "description": description,
        "severity": "P1",
        "recorded_at": recorded_at,
    }


def _report(claims: list[dict], findings: list[dict], **kwargs) -> dict:
    return ku.build_uptake_report(
        context_manifests=[_manifest(claims)],
        findings=findings,
        **kwargs,
    )


def _outcomes(report: dict) -> list[str]:
    return [c["outcome"] for c in report["correspondences"]]


# ── Correspondence outcomes ──────────────────────────────────────────────────


def test_finding_restating_a_dev_claim_is_matched_and_names_the_claim() -> None:
    claim = _claim(_GATE_CLAIM)
    report = _report([claim], [_finding(_GATE_FINDING)])

    assert report["status"] == ku.STATUS_COMPARED
    assert report["counts"][ku.OUTCOME_MATCHED] == 1
    correspondence = report["correspondences"][0]
    assert correspondence["outcome"] == ku.OUTCOME_MATCHED
    # The claim a correspondence refers to must survive into the record, or the
    # population it identifies cannot be inspected.
    assert correspondence["claim_ref"] == claim["claim_ref"]
    assert correspondence["claim_run_id"] == "27bb13e86070"
    assert correspondence["claim_phase_iteration"] == 1


def test_unrelated_finding_is_not_matched_rather_than_novel() -> None:
    report = _report(
        [_claim(_GATE_CLAIM)],
        [_finding("The changelog omits the operator action required for the template")],
    )
    assert _outcomes(report) == [ku.OUTCOME_NOT_MATCHED]


def test_finding_with_too_little_text_is_indeterminate_not_unmatched() -> None:
    report = _report([_claim(_GATE_CLAIM)], [_finding("Fix it")])

    assert _outcomes(report) == [ku.OUTCOME_INDETERMINATE]
    assert report["correspondences"][0]["reason"] == "finding_text_insufficient"
    assert report["counts"][ku.OUTCOME_INDETERMINATE] == 1


def test_indeterminate_count_appears_beside_the_totals() -> None:
    report = _report(
        [_claim(_GATE_CLAIM)],
        [
            _finding(_GATE_FINDING, finding_id="a"),
            _finding("Totally separate concern about template rendering", finding_id="b"),
            _finding("Fix it", finding_id="c"),
        ],
    )
    assert report["counts"] == {
        ku.OUTCOME_MATCHED: 1,
        ku.OUTCOME_NOT_MATCHED: 1,
        ku.OUTCOME_INDETERMINATE: 1,
    }


# ── Eligibility ──────────────────────────────────────────────────────────────


def test_review_only_claim_never_explains_the_reviewers_own_finding() -> None:
    """The story's central case: the loop working must not read as it failing."""
    report = ku.build_uptake_report(
        context_manifests=[
            _manifest([_claim(_GATE_CLAIM)], phase="dev"),
            _manifest(
                [_claim(_STALENESS, role="review", phase="review")],
                phase="review",
            ),
        ],
        findings=[_finding(_GATE_FINDING, finding_id="a"), _finding(_STALENESS, finding_id="b")],
    )

    assert report["claims_rendered"] == 2
    assert report["claims_eligible"] == 1
    assert {c["finding_id"]: c["outcome"] for c in report["correspondences"]} == {
        "a": ku.OUTCOME_MATCHED,
        "b": ku.OUTCOME_NOT_MATCHED,
    }
    assert report["claims_excluded"] == [
        {"reason": "rendered_to_review_only_not_author", "count": 1}
    ]


def test_claim_rendered_after_the_finding_cannot_explain_it() -> None:
    report = _report(
        [_claim(_GATE_CLAIM, rendered_at="2026-08-01T18:00:00+00:00")],
        [_finding(_GATE_FINDING, recorded_at="2026-08-01T14:00:00+00:00")],
    )
    assert _outcomes(report) == [ku.OUTCOME_NOT_MATCHED]


def test_claim_rendered_before_the_finding_can_explain_it() -> None:
    report = _report(
        [_claim(_GATE_CLAIM, rendered_at="2026-08-01T09:00:00+00:00")],
        [_finding(_GATE_FINDING, recorded_at="2026-08-01T14:00:00+00:00")],
    )
    assert _outcomes(report) == [ku.OUTCOME_MATCHED]


def test_unorderable_matching_claim_is_indeterminate_not_matched() -> None:
    """A finding with no recorded time cannot be ordered against any claim."""
    report = _report([_claim(_GATE_CLAIM)], [_finding(_GATE_FINDING, recorded_at=None)])

    assert _outcomes(report) == [ku.OUTCOME_INDETERMINATE]
    assert report["correspondences"][0]["reason"] == "claim_finding_order_unknown"


def test_plan_claims_are_not_eligible_for_the_dev_authors_work() -> None:
    report = _report(
        [_claim(_GATE_CLAIM, role="plan", phase="plan")],
        [_finding(_GATE_FINDING)],
    )
    assert report["status"] == ku.STATUS_NO_ELIGIBLE_CLAIMS
    assert report["claims_eligible"] == 0


def test_preflight_manifest_contributes_no_claims_and_is_never_eligible() -> None:
    """Preflight is signal-only (ADR-0002 clause 5): it renders no claim prose.

    Asserted so a future preflight change cannot silently start feeding this
    indicator with material no author was ever shown as a claim.
    """
    preflight = {
        "phase": "preflight",
        "prior_run_context": {
            "enabled": True,
            "claim_exposure": {"capture_version": 1},
            "included": [{"run_id": "r", "rendering_mode": "signal_only", "claims": []}],
            "dropped": [],
        },
    }
    report = ku.build_uptake_report(
        context_manifests=[preflight], findings=[_finding(_GATE_FINDING)]
    )
    assert report["claims_rendered"] == 0
    assert report["status"] == ku.STATUS_NO_ELIGIBLE_CLAIMS


def test_claim_with_unrecorded_recipient_role_is_excluded_not_assumed_eligible() -> None:
    report = _report([_claim(_GATE_CLAIM, role="")], [_finding(_GATE_FINDING)])
    assert report["status"] == ku.STATUS_NO_ELIGIBLE_CLAIMS
    assert report["claims_excluded"] == [{"reason": "recipient_role_unrecorded", "count": 1}]


# ── Run-level statuses ───────────────────────────────────────────────────────


def test_no_eligible_claims_emits_no_correspondence_block() -> None:
    report = ku.build_uptake_report(
        context_manifests=[_manifest([])], findings=[_finding(_GATE_FINDING)]
    )
    assert report["status"] == ku.STATUS_NO_ELIGIBLE_CLAIMS
    assert report["counts"] is None
    assert report["correspondences"] is None


def test_no_review_findings_emits_no_correspondence_block() -> None:
    report = _report([_claim(_GATE_CLAIM)], [])
    assert report["status"] == ku.STATUS_NO_REVIEW_FINDINGS
    assert report["correspondences"] is None


def test_compared_and_matched_nothing_is_distinct_from_nothing_to_compare() -> None:
    compared = _report(
        [_claim(_GATE_CLAIM)],
        [_finding("The changelog omits the operator action required for the template")],
    )
    nothing = ku.build_uptake_report(
        context_manifests=[_manifest([])], findings=[_finding(_GATE_FINDING)]
    )

    assert compared["status"] == ku.STATUS_COMPARED
    assert compared["counts"][ku.OUTCOME_NOT_MATCHED] == 1
    assert nothing["status"] == ku.STATUS_NO_ELIGIBLE_CLAIMS
    assert nothing["counts"] is None
    assert compared["status"] != nothing["status"]


def test_run_predating_capture_is_uncomparable_not_zero_correspondence() -> None:
    report = ku.build_uptake_report(
        context_manifests=[_manifest([_claim(_GATE_CLAIM)], captured=False)],
        findings=[_finding(_GATE_FINDING)],
    )
    assert report["status"] == ku.STATUS_UNCOMPARABLE
    assert report["claims_rendered"] is None
    assert report["counts"] is None
    assert report["review_findings"] == 1


def test_one_uncaptured_manifest_makes_the_whole_run_uncomparable() -> None:
    report = ku.build_uptake_report(
        context_manifests=[
            _manifest([_claim(_GATE_CLAIM)], phase="dev"),
            _manifest([], phase="review", captured=False),
        ],
        findings=[_finding(_GATE_FINDING)],
    )
    assert report["status"] == ku.STATUS_UNCOMPARABLE


# ── Method identity and validation ───────────────────────────────────────────


def test_every_result_names_the_method_and_its_version() -> None:
    report = _report([_claim(_GATE_CLAIM)], [_finding(_GATE_FINDING)])
    assert report["method"] == {"name": ku.METHOD_NAME, "version": ku.METHOD_VERSION}


def test_unmeasured_agreement_reports_figures_as_unvalidated_not_omitted() -> None:
    report = _report([_claim(_GATE_CLAIM)], [_finding(_GATE_FINDING)], validation=None)

    assert report["validation"]["status"] == ku.VALIDATION_UNVALIDATED
    # The figures are still there: an unvalidated number is still a number.
    assert report["counts"][ku.OUTCOME_MATCHED] == 1
    assert "UNVALIDATED" in render_run_uptake(report)


def test_agreement_is_measured_against_the_stored_labelled_set() -> None:
    root = Path(__file__).resolve().parent.parent
    validation = ku.measure_agreement(ku.load_labelled_examples(root))

    assert validation["status"] == ku.VALIDATION_MEASURED
    assert validation["n"] >= 9
    assert 0.0 <= validation["agreement"] <= 1.0


def test_stored_labelled_set_covers_every_required_category() -> None:
    root = Path(__file__).resolve().parent.parent
    labelled = ku.load_labelled_examples(root)
    categories = {e.get("category") for e in labelled["examples"]}
    labels = {e.get("label") for e in labelled["examples"]}

    assert {"correspondence", "non_correspondence", "eligibility_excluded"} <= categories
    assert {
        ku.OUTCOME_MATCHED,
        ku.OUTCOME_NOT_MATCHED,
        ku.OUTCOME_INDETERMINATE,
    } <= labels


def test_labelled_set_from_a_different_method_version_does_not_validate(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "knowledge-uptake-labels.yaml").write_text(
        yaml.safe_dump(
            {
                "method": ku.METHOD_NAME,
                "method_version": "v99",
                "examples": [{"label": ku.OUTCOME_MATCHED, "finding": {}, "claims": []}],
            }
        ),
        encoding="utf-8",
    )
    validation = ku.measure_agreement(ku.load_labelled_examples(tmp_path))

    assert validation["status"] == ku.VALIDATION_UNVALIDATED
    assert validation["reason"] == "labelled_set_method_mismatch"


def test_missing_labelled_set_reports_unvalidated(tmp_path: Path) -> None:
    validation = ku.measure_agreement(ku.load_labelled_examples(tmp_path))
    assert validation["status"] == ku.VALIDATION_UNVALIDATED
    assert validation["agreement"] is None


# ── Reporting wording ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "report",
    [
        _report([_claim(_GATE_CLAIM)], [_finding(_GATE_FINDING)]),
        _report([_claim(_GATE_CLAIM)], []),
        ku.build_uptake_report(context_manifests=[_manifest([])], findings=[_finding("x y z q")]),
        ku.build_uptake_report(
            context_manifests=[_manifest([], captured=False)], findings=[_finding("x y z q")]
        ),
    ],
)
def test_every_rendering_carries_the_missed_uptake_framing(report: dict) -> None:
    text = render_run_uptake(report)
    assert ku.INTERPRETATION_NOTE in text


@pytest.mark.parametrize("word", ["novel", "new finding", "helped", "failed to help"])
def test_report_never_frames_findings_as_novel_or_knowledge_as_helping(word: str) -> None:
    report = _report(
        [_claim(_GATE_CLAIM)],
        [
            _finding(_GATE_FINDING, finding_id="a"),
            _finding(
                "The changelog omits the operator action required for the template",
                finding_id="b",
            ),
        ],
    )
    text = render_run_uptake(report).lower()

    assert word not in text
    assert "not matched to an eligible injected claim" in text


def test_uncomparable_rendering_does_not_present_findings_as_matching_nothing() -> None:
    report = ku.build_uptake_report(
        context_manifests=[_manifest([], captured=False)],
        findings=[_finding(_GATE_FINDING)],
    )
    text = render_run_uptake(report)

    assert "uncomparable" in text
    assert "predates claim-exposure capture" in text
    assert "not matched to an eligible injected claim" not in text
