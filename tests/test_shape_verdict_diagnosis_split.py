"""Seam-level integration test for the three-state bug-diagnosis verdict split.

Drives three bug bodies through ``apply_shape_gate`` to assert the verdict
each lands on per ADR-0001:

1. No Diagnosis section → ``needs_diagnosis`` (skipped, blocking).
2. Diagnosis with complete required labels but cause unknown →
   ``diagnosis_cause_unknown`` (skipped from implementation sprints, but
   admissible — not malformed).
3. Complete Diagnosis with confirmed cause → ``runnable``.

The test runs against the cross-phase shape-gate boundary so a regression
that re-routes the verdict at any layer (heuristics, verdict mapping,
classifier rebuild, gate dispatch) is caught here.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from theforge.shape_check.types import VERDICT_DESCRIPTIONS, ShapeVerdict
from theforge.sprint.shape_gate import (
    SkippedIssue,
    apply_shape_gate,
    skipped_issue_state_fields,
)

NO_DIAGNOSIS_BODY = "## What happened\nThings broke.\n\n## What was expected\nThey work.\n"

CAUSE_UNKNOWN_BODY = textwrap.dedent(
    """\
    ## What happened
    The CLI exits 1 intermittently.

    ## What was expected
    The CLI exits 0.

    ## Diagnosis

    - **Observed symptom.** Exit code flips between 0 and 1.
    - **Evidence.** Run id `abcd1234`, log lines 42-55.
    - **Confirmed cause.** Not yet identified — investigating provider retry path.
    - **Affected code path.** unknown; suspected coordinator/runner.py.
    - **Fix-success criterion.** Exit code is deterministic across 100 runs.
    """
)

COMPLETE_DIAGNOSIS_BODY = textwrap.dedent(
    """\
    ## What happened
    Resume false-skips zero-delta APPROVE stories.

    ## What was expected
    Resume identifies them as already merged.

    ## Diagnosis

    - **Observed symptom.** Sprint resume false-skips zero-delta APPROVE stories.
    - **Evidence.** Run id `1ff6b0bb7992`, story #1102.
    - **Confirmed cause.** `_is_already_merged` requires at least one commit ahead.
    - **Affected code path.** sprint.runner._is_already_merged.
    - **Fix-success criterion.** Resume identifies zero-delta APPROVE as merged.
    """
)


def _fetch_for(body: str, labels: list[str]):
    def fetch(_number, _root):
        return {"title": "Bug", "body": body, "labels": labels}

    return fetch


def test_no_diagnosis_lands_on_needs_diagnosis(tmp_path: Path) -> None:
    issues = [{"number": 1, "title": "Bug"}]
    result = apply_shape_gate(
        issues, tmp_path, fetch_detail=_fetch_for(NO_DIAGNOSIS_BODY, ["bug"])
    )
    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.verdict == ShapeVerdict.NEEDS_DIAGNOSIS.value
    assert entry.verdict_description == VERDICT_DESCRIPTIONS[ShapeVerdict.NEEDS_DIAGNOSIS]
    assert entry.verdict_description.strip() != ""
    assert "needs_diagnosis" in entry.reason_codes


def test_cause_unknown_lands_on_diagnosis_cause_unknown(tmp_path: Path) -> None:
    issues = [{"number": 2, "title": "Bug"}]
    result = apply_shape_gate(
        issues, tmp_path, fetch_detail=_fetch_for(CAUSE_UNKNOWN_BODY, ["bug"])
    )
    # Admissible (not malformed) but not implementation-runnable: keep out
    # of runnable, surface as skipped with the typed verdict.
    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.verdict == ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN.value
    assert entry.verdict_description == VERDICT_DESCRIPTIONS[ShapeVerdict.DIAGNOSIS_CAUSE_UNKNOWN]
    assert entry.verdict_description.strip() != ""
    assert "diagnosis_cause_unknown" in entry.reason_codes


def test_complete_diagnosis_lands_on_runnable(tmp_path: Path) -> None:
    issues = [{"number": 3, "title": "Bug"}]
    result = apply_shape_gate(
        issues, tmp_path, fetch_detail=_fetch_for(COMPLETE_DIAGNOSIS_BODY, ["bug"])
    )
    assert result.skipped == []
    assert len(result.runnable) == 1
    runnable = result.runnable[0]
    assert runnable["number"] == 3
    assert runnable["shape_verdict"] == ShapeVerdict.RUNNABLE.value
    assert VERDICT_DESCRIPTIONS[ShapeVerdict.RUNNABLE].strip() != ""


def test_skipped_issue_state_fields_prefers_typed_verdict() -> None:
    """Mixed-sprint regression guard: the helper used by sprint.runner,
    state_writer, and cli.sprint must surface the verdict identifier in the
    canonical reason so forge sprint-status displays it (AC).
    """
    sk = SkippedIssue(
        issue_number=1497,
        reason_codes=("missing_acceptance_criteria",),
        source="local_check",
        title="Feature without ACs",
        detail="No acceptance criteria section found.",
        verdict=ShapeVerdict.NEEDS_GROOMING_MISSING_AC.value,
        verdict_description=VERDICT_DESCRIPTIONS[ShapeVerdict.NEEDS_GROOMING_MISSING_AC],
    )
    reason, detail = skipped_issue_state_fields(sk)
    # forge sprint-status renders ``story.reason`` for skipped rows; this is
    # the operator-visible verdict surface.
    assert reason == ShapeVerdict.NEEDS_GROOMING_MISSING_AC.value
    assert detail["shape_verdict"] == ShapeVerdict.NEEDS_GROOMING_MISSING_AC.value
    assert (
        detail["shape_verdict_description"]
        == VERDICT_DESCRIPTIONS[ShapeVerdict.NEEDS_GROOMING_MISSING_AC]
    )
    assert detail["shape_gate_codes"] == ["missing_acceptance_criteria"]
    assert detail["final_outcome"] == "SKIPPED"


def test_skipped_issue_state_fields_falls_back_to_codes_for_legacy_records() -> None:
    """Records without the verdict field (older skip artifacts) must still
    project a non-empty reason so the canonical state isn't blank."""
    legacy = {
        "issue_number": 9,
        "reason_codes": ["legacy_code"],
        "source": "local_check",
        "title": "Legacy",
        "detail": "",
    }
    reason, detail = skipped_issue_state_fields(legacy)
    assert reason == "legacy_code"
    assert detail["shape_verdict"] is None
    assert detail["shape_gate_codes"] == ["legacy_code"]
