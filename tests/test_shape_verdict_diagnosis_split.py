"""Seam-level integration test for the three-state bug-diagnosis verdict split.

Drives three bug bodies through ``apply_shape_gate`` to assert the verdict
each lands on per ADR-0001:

1. No Diagnosis section → ``needs_diagnosis`` (skipped, blocking).
2. Diagnosis with hypotheses ruled out but cause unknown →
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
from theforge.sprint.shape_gate import apply_shape_gate

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
    - **Ruled out.** Network flake (verified offline), gate cache (cleared).
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
    - **Ruled out.** Workspace creation actually succeeds (verified in logs).
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
