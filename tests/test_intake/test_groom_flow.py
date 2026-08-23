"""Seam-level tests for the forge groom flow.

Cover each branch of the three-state bug taxonomy plus the
no-cause-unknown-to-ready hard invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.intake import groom_flow
from theforge.intake.groom_flow import (
    BugDiagnosisState,
    GroomAction,
    GroomError,
    classify_bug_diagnosis,
    run_groom,
)
from theforge.shape_check import ShapeVerdict
from theforge.shape_check import check as shape_check
from theforge.shape_check.placeholders import has_placeholder_marker

# ── Bug fixtures ──────────────────────────────────────────────────────────

_CONFIRMED_BUG_BODY = """\
## What happened

Forge worktrees drop secrets.

## What was expected

Secrets propagate.

## Diagnosis

- Observed symptom: missing .env on dev branch
- Evidence: file absent in worktree
- Ruled out: agent role mismatch
- Confirmed cause: worktree-creation step skips .env copy at line 42
- Affected code path: theforge/coordinator/worktree.py
- Fix-success criterion: .env present after worktree creation
"""

_CAUSE_UNKNOWN_BUG_BODY = """\
## What happened

Reviewer agents sometimes write to the worktree.

## What was expected

Reviewers are read-only.

## Diagnosis

- Observed symptom: intermittent worktree writes from review phase
- Evidence: git log shows phantom commits
- Ruled out: permission misconfig
- Confirmed cause: not yet identified
- Affected code path: TBD
- Fix-success criterion: no review-phase worktree writes
"""

_NO_DIAGNOSIS_BUG_BODY = """\
## What happened

Tests sometimes fail flakily on CI.

## What was expected

Tests pass deterministically.
"""

# A shape-authored placeholder stub left in place above a landed artifact
# (#2263, hdp#259). The stub is a non-canonical-level (### not ##) heading
# that merely contains the word "diagnosis" in "no diagnosis yet".
_SHADOWED_BY_PLACEHOLDER_CONFIRMED_BUG_BODY = """\
## What happened

Forge worktrees drop secrets.

## What was expected

Secrets propagate.

### Diagnosis

Status: no diagnosis yet. Next step: run `forge diagnose`.

## Diagnosis

- Observed symptom: missing .env on dev branch
- Evidence: file absent in worktree
- Ruled out: agent role mismatch
- Confirmed cause: worktree-creation step skips .env copy at line 42
- Affected code path: theforge/coordinator/worktree.py
- Fix-success criterion: .env present after worktree creation
"""

# An ordinary operator-written prose heading that merely mentions the word
# "diagnosis" above a landed artifact (#2263, fuzzypete/theforge#2673).
_SHADOWED_BY_PROSE_HEADING_CONFIRMED_BUG_BODY = """\
## What happened

Forge worktrees drop secrets.

## What was expected

Secrets propagate.

## Further evidence — generated diagnosis text becomes scope-classification input on rerun

Some unrelated narrative about the diagnose flow itself.

## Diagnosis

- Observed symptom: missing .env on dev branch
- Evidence: file absent in worktree
- Ruled out: agent role mismatch
- Confirmed cause: worktree-creation step skips .env copy at line 42
- Affected code path: theforge/coordinator/worktree.py
- Fix-success criterion: .env present after worktree creation
"""

# A complete, confirmed-cause artifact followed by a *longer* stale
# placeholder — content length must not outrank artifact completeness, and
# neither should document position (#2263 review cycle 1, openai finding).
_ARTIFACT_BEFORE_LONGER_STALE_PLACEHOLDER_BUG_BODY = """\
## What happened

Forge worktrees drop secrets.

## What was expected

Secrets propagate.

## Diagnosis

- Observed symptom: missing .env on dev branch
- Evidence: file absent in worktree
- Ruled out: agent role mismatch
- Confirmed cause: worktree-creation step skips .env copy at line 42
- Affected code path: theforge/coordinator/worktree.py
- Fix-success criterion: .env present after worktree creation

## Diagnosis

Status: no diagnosis yet. Next step: run `forge diagnose`. This placeholder
stub has been padded with a great deal of extra explanatory prose so that,
measured purely by character count, it is considerably longer than the
complete diagnosis artifact that actually precedes it in the document —
which is exactly the scenario a length-only tie-break gets wrong.
"""

_SHADOWED_BY_PLACEHOLDER_CAUSE_UNKNOWN_BUG_BODY = """\
## What happened

Reviewer agents sometimes write to the worktree.

## What was expected

Reviewers are read-only.

### Diagnosis

Status: no diagnosis yet. Next step: run `forge diagnose`.

## Diagnosis

- Observed symptom: intermittent worktree writes from review phase
- Evidence: git log shows phantom commits
- Ruled out: permission misconfig
- Confirmed cause: not yet identified
- Affected code path: TBD
- Fix-success criterion: no review-phase worktree writes
"""

# A genuine, complete cause-unknown diagnosis followed by an entirely
# unfilled `forge shape` scaffold — every required label present, every
# value the literal `<fill in>` slot marker. Label-count alone cannot tell
# these apart; the scaffold must never outrank the real (if inconclusive)
# diagnosis that precedes it (#2263 review cycle 2).
_CAUSE_UNKNOWN_FOLLOWED_BY_UNFILLED_SCAFFOLD_BUG_BODY = """\
## What happened

Reviewer agents sometimes write to the worktree.

## What was expected

Reviewers are read-only.

## Diagnosis

- **Observed symptom:** intermittent worktree writes from review phase
- **Evidence:** git log shows phantom commits
- **Confirmed cause:** not yet identified
- **Affected code path:** TBD
- **Fix-success criterion:** no review-phase worktree writes

## Diagnosis

Status: no diagnosis yet. Next step: run `forge diagnose`.

- **Observed symptom:** <fill in>
- **Evidence:** <fill in>
- **Confirmed cause:** <fill in>
- **Affected code path:** <fill in>
- **Fix-success criterion:** <fill in>
"""

# A bug whose diagnosis narrative lives entirely under a "Root cause"
# heading rather than "Diagnosis" — the classifier and the gate must agree
# it is diagnosed (#2263 review cycle 2).
_ROOT_CAUSE_ONLY_HEADING_CONFIRMED_BUG_BODY = """\
## What happened

Forge worktrees drop secrets.

## What was expected

Secrets propagate.

## Root cause

- Observed symptom: missing .env on dev branch
- Evidence: file absent in worktree
- Confirmed cause: worktree-creation step skips .env copy at line 42
- Affected code path: theforge/coordinator/worktree.py
- Fix-success criterion: .env present after worktree creation
"""


# ── Issue-loading seam ────────────────────────────────────────────────────


def _fake_fetch(payload: dict):
    def fetch(_number: int, _project_root):
        return payload

    return fetch


def _no_op_edit(_number: int, _body: str, _project_root) -> bool:
    return True


# ── Bug classification ────────────────────────────────────────────────────


def test_classify_confirmed_cause():
    state = classify_bug_diagnosis(_CONFIRMED_BUG_BODY, ["bug"])
    assert state is BugDiagnosisState.CONFIRMED_CAUSE


def test_classify_cause_unknown():
    state = classify_bug_diagnosis(_CAUSE_UNKNOWN_BUG_BODY, ["bug"])
    assert state is BugDiagnosisState.CAUSE_UNKNOWN


def test_classify_no_diagnosis():
    state = classify_bug_diagnosis(_NO_DIAGNOSIS_BUG_BODY, ["bug"])
    assert state is BugDiagnosisState.NO_DIAGNOSIS


def test_classify_non_bug_returns_not_a_bug():
    state = classify_bug_diagnosis("body", ["enhancement"])
    assert state is BugDiagnosisState.NOT_A_BUG


def test_landed_diagnosis_outranks_stale_placeholder_stub_above_it():
    """A shape-authored placeholder stub left above a landed artifact must not
    shadow it (#2263, hdp#259)."""
    state = classify_bug_diagnosis(_SHADOWED_BY_PLACEHOLDER_CONFIRMED_BUG_BODY, ["bug"])
    assert state is BugDiagnosisState.CONFIRMED_CAUSE


def test_landed_diagnosis_outranks_ordinary_prose_heading_above_it():
    """An operator-written heading that merely mentions "diagnosis" must not
    shadow a landed artifact below it (#2263, fuzzypete/theforge#2673)."""
    state = classify_bug_diagnosis(_SHADOWED_BY_PROSE_HEADING_CONFIRMED_BUG_BODY, ["bug"])
    assert state is BugDiagnosisState.CONFIRMED_CAUSE


def test_shadowed_inconclusive_diagnosis_is_cause_unknown_not_no_diagnosis():
    """A genuinely inconclusive diagnosis is distinguishable from no diagnosis
    at all, even when shadowed by an earlier placeholder heading."""
    state = classify_bug_diagnosis(_SHADOWED_BY_PLACEHOLDER_CAUSE_UNKNOWN_BUG_BODY, ["bug"])
    assert state is BugDiagnosisState.CAUSE_UNKNOWN


def test_complete_artifact_outranks_longer_stale_placeholder_after_it():
    """Authority must come from artifact completeness, not raw section
    length or document position: a longer, later placeholder must not
    outrank an earlier, complete, confirmed-cause artifact (#2263)."""
    state = classify_bug_diagnosis(_ARTIFACT_BEFORE_LONGER_STALE_PLACEHOLDER_BUG_BODY, ["bug"])
    assert state is BugDiagnosisState.CONFIRMED_CAUSE


def test_cause_unknown_artifact_outranks_unfilled_scaffold_after_it():
    """An unfilled `forge shape` scaffold lists every required label, so
    label count alone cannot distinguish it from a genuine artifact — it
    must never outrank a real, if inconclusive, diagnosis that precedes it,
    and must never itself read as an asserted cause (#2263 review cycle 2)."""
    state = classify_bug_diagnosis(_CAUSE_UNKNOWN_FOLLOWED_BY_UNFILLED_SCAFFOLD_BUG_BODY, ["bug"])
    assert state is BugDiagnosisState.CAUSE_UNKNOWN


def test_root_cause_heading_alone_is_recognized_as_diagnosed():
    """The classifier and the gate must agree: a bug whose diagnosis lives
    under "## Root cause" (not "## Diagnosis") is diagnosed, not
    undiagnosed (#2263 review cycle 2)."""
    state = classify_bug_diagnosis(_ROOT_CAUSE_ONLY_HEADING_CONFIRMED_BUG_BODY, ["bug"])
    assert state is BugDiagnosisState.CONFIRMED_CAUSE


# ── Three-state branching ─────────────────────────────────────────────────


def test_groom_refuses_bug_with_no_diagnosis():
    fetch = _fake_fetch(
        {
            "title": "Tests sometimes fail flakily on CI",
            "body": _NO_DIAGNOSIS_BUG_BODY,
            "labels": ["bug"],
        }
    )
    result = run_groom("1234", fetch_issue=fetch, edit_issue_body=_no_op_edit)

    assert result.action is GroomAction.REFUSED
    assert result.refusal_reason is not None
    assert "needs diagnosis" in result.refusal_reason
    assert "forge diagnose" in result.refusal_reason
    assert result.proposed_body == result.original_body  # No body edits proposed
    assert result.next_command == "forge diagnose 1234"


def test_groom_cause_unknown_normalizes_only_and_refuses_ready():
    body_with_trailing_ws = _CAUSE_UNKNOWN_BUG_BODY + "    \n\n\n"
    fetch = _fake_fetch(
        {
            "title": "Reviewer-phase agents can write to the worktree",
            "body": body_with_trailing_ws,
            "labels": ["bug"],
        }
    )
    result = run_groom("1497", fetch_issue=fetch, edit_issue_body=_no_op_edit)

    assert result.action is GroomAction.NORMALIZED_ONLY
    assert result.bug_state is BugDiagnosisState.CAUSE_UNKNOWN
    assert result.investigation_only_notice is True

    # No-ready invariant: --next must not point at the ready label.
    assert "add-label ready" not in (result.next_command or "")
    assert "forge diagnose" in (result.next_command or "")


def test_groom_confirmed_cause_restructures_and_can_lead_to_ready():
    fetch = _fake_fetch(
        {
            "title": "Forge-created worktrees drop project-scoped .forge/.env secrets",
            "body": _CONFIRMED_BUG_BODY,
            "labels": ["bug"],
        }
    )
    result = run_groom("1503", fetch_issue=fetch, edit_issue_body=_no_op_edit)

    assert result.action is GroomAction.RESTRUCTURED
    assert result.bug_state is BugDiagnosisState.CONFIRMED_CAUSE
    assert result.investigation_only_notice is False
    assert result.post_verdict is ShapeVerdict.RUNNABLE
    assert result.next_command == "gh issue edit 1503 --add-label ready"


# ── Hard invariant: cause-unknown bugs cannot reach ready ────────────────


def test_hard_invariant_cause_unknown_never_recommends_ready():
    """Regardless of post-groom verdict, a cause-unknown bug must never
    surface ``--add-label ready`` as the next step."""
    fetch = _fake_fetch(
        {
            "title": "Cause unknown bug",
            "body": _CAUSE_UNKNOWN_BUG_BODY,
            "labels": ["bug"],
        }
    )
    result = run_groom("999", fetch_issue=fetch, edit_issue_body=_no_op_edit)
    assert "ready" not in (result.next_command or "").lower().split()
    # `forge diagnose` continuation is the only acceptable next-step.
    assert (result.next_command or "").startswith("forge diagnose")


# ── Feature/enhancement restructure ───────────────────────────────────────


def test_groom_feature_inserts_missing_ac_and_example():
    body = "## What\n\nUsers can export.\n"
    fetch = _fake_fetch(
        {
            "title": "Add export",
            "body": body,
            "labels": ["enhancement"],
        }
    )
    result = run_groom("42", fetch_issue=fetch, edit_issue_body=_no_op_edit)
    assert result.action is GroomAction.RESTRUCTURED
    assert "## Acceptance criteria" in result.proposed_body
    assert "## Example" in result.proposed_body
    assert result.needs_change
    # Groom scaffolds the missing sections but does not invent their content,
    # so the findings it could not resolve must still stand (#2129).
    assert result.post_verdict is not ShapeVerdict.RUNNABLE
    assert result.unsupplied_findings == ("missing_acceptance_criteria", "missing_example")


def test_groom_docs_reaches_runnable():
    """Docs-typed issues are first-class per ADR-0001; normalize them onto
    the shape gate's recognized type vocabulary so the post-groom verdict
    can reach runnable rather than tripping on missing_type.

    Body already carries real AC and example content — groom supplies
    neither (#2129), so type normalization is what's under test here."""
    body = (
        "## What\n\nDocument the new endpoint.\n\n"
        "## Acceptance criteria\n\n- Docs build produces a page for the export endpoint\n\n"
        "## Example\n\n```\nGET /api/export -> 200 text/csv\n```\n"
    )
    fetch = _fake_fetch(
        {
            "title": "Document export endpoint",
            "body": body,
            "labels": ["docs"],
        }
    )
    result = run_groom("88", fetch_issue=fetch, edit_issue_body=_no_op_edit)
    assert result.action is GroomAction.RESTRUCTURED
    assert result.issue_type == "docs"
    assert result.post_verdict is ShapeVerdict.RUNNABLE


def test_groom_story_reaches_runnable():
    """Story-typed issues are supported per the lifecycle spec; normalize
    them to a shape-gate-recognized label so they reach a real verdict.

    Body already carries real AC and example content — groom supplies
    neither (#2129), so type normalization is what's under test here."""
    body = (
        "## What\n\nUsers can filter by date.\n\n"
        "## Acceptance criteria\n\n- Filter emits only rows within the date range\n\n"
        "## Example\n\n```\nfilter --from 2026-01-01 --to 2026-02-01\n```\n"
    )
    fetch = _fake_fetch(
        {
            "title": "Filter by date",
            "body": body,
            "labels": ["story"],
        }
    )
    result = run_groom("99", fetch_issue=fetch, edit_issue_body=_no_op_edit)
    assert result.action is GroomAction.RESTRUCTURED
    assert result.issue_type == "story"
    assert result.post_verdict is ShapeVerdict.RUNNABLE


def test_groom_feature_no_changes_when_already_clean():
    body = (
        "## What\n\nUsers can export.\n\n"
        "## Acceptance criteria\n\n- Export emits a downloadable file\n\n"
        "## Example\n\n```\nexport.csv\n```\n"
    )
    fetch = _fake_fetch(
        {
            "title": "Add export",
            "body": body,
            "labels": ["enhancement"],
        }
    )
    result = run_groom("42", fetch_issue=fetch, edit_issue_body=_no_op_edit)
    assert result.action is GroomAction.RESTRUCTURED
    assert not result.needs_change


# ── --apply path ─────────────────────────────────────────────────────────


def test_apply_invokes_edit_seam_and_records_applied():
    captured = {}

    def edit(number, body, _root):
        captured["number"] = number
        captured["body"] = body
        return True

    fetch = _fake_fetch(
        {
            "title": "Confirmed cause bug",
            "body": _CONFIRMED_BUG_BODY + "   \n",
            "labels": ["bug"],
        }
    )
    result = run_groom("1503", apply_changes=True, fetch_issue=fetch, edit_issue_body=edit)
    assert result.applied is True
    assert captured["number"] == 1503
    assert captured["body"] == result.proposed_body


def test_apply_no_change_skips_edit():
    calls = []

    def edit(*args):
        calls.append(args)
        return True

    fetch = _fake_fetch(
        {
            "title": "Clean confirmed bug",
            "body": _CONFIRMED_BUG_BODY,
            "labels": ["bug"],
        }
    )
    result = run_groom("1503", apply_changes=True, fetch_issue=fetch, edit_issue_body=edit)
    assert result.applied is False
    assert calls == []


def test_apply_edit_failure_raises():
    fetch = _fake_fetch(
        {
            "title": "bug",
            "body": _CONFIRMED_BUG_BODY + "   \n",
            "labels": ["bug"],
        }
    )

    def failing_edit(*_args):
        return False

    with pytest.raises(GroomError):
        run_groom(
            "1503",
            apply_changes=True,
            fetch_issue=fetch,
            edit_issue_body=failing_edit,
        )


# ── Post-apply re-check ──────────────────────────────────────────────────


def test_apply_rechecks_persisted_body_via_refetch():
    """P1: post-groom verdict must reflect the body that actually landed
    upstream, not the in-memory proposal. If GitHub stored something other
    than what we sent (e.g. a hook munged it), the reported verdict tracks
    the persisted state, not the proposal."""
    # First fetch returns dirty body; refetch (after --apply) returns a
    # tampered body whose shape verdict differs from the proposal.
    fetch_calls = {"count": 0}
    tampered_body = "## What\n\ntampered with — no AC, no Example\n"

    def fetch(_n, _root):
        fetch_calls["count"] += 1
        if fetch_calls["count"] == 1:
            return {
                "title": "feature",
                "body": "## What\n\nusers can export.\n",
                "labels": ["enhancement"],
            }
        return {"title": "feature", "body": tampered_body, "labels": ["enhancement"]}

    result = run_groom("42", apply_changes=True, fetch_issue=fetch, edit_issue_body=_no_op_edit)
    assert fetch_calls["count"] == 2  # one for load, one for post-apply re-check
    assert result.applied is True
    # Proposed body still reflects what groom sent — not the tampered state.
    assert "## Acceptance criteria" in result.proposed_body
    # But the reported verdict reflects the persisted (tampered) body.
    assert result.post_verdict is not ShapeVerdict.RUNNABLE


def test_apply_rechecks_persisted_file_after_local_write(tmp_path: Path):
    """P1: post-apply re-check also runs for local file inputs — verdict
    is derived from what is actually on disk after the write."""
    f = tmp_path / "issue.md"
    f.write_text(
        "---\ntitle: x\nlabels: [enhancement]\n---\n## What\n\nUsers can export.\n",
        encoding="utf-8",
    )
    result = run_groom(str(f), apply_changes=True)
    assert result.applied is True
    # Re-read verdict is derived from disk. Groom scaffolded the missing
    # sections but supplied no content, so the findings still stand.
    assert result.post_verdict is not ShapeVerdict.RUNNABLE
    assert result.unsupplied_findings == ("missing_acceptance_criteria", "missing_example")
    on_disk = f.read_text(encoding="utf-8")
    assert "## Acceptance criteria" in on_disk
    assert "## Example" in on_disk


# ── Local file loading ───────────────────────────────────────────────────


def test_groom_reads_local_file(tmp_path: Path):
    f = tmp_path / "issue.md"
    f.write_text(
        "---\ntitle: My bug\nlabels: [bug]\n---\n" + _CAUSE_UNKNOWN_BUG_BODY,
        encoding="utf-8",
    )
    result = run_groom(str(f))
    assert result.title == "My bug"
    assert result.bug_state is BugDiagnosisState.CAUSE_UNKNOWN
    assert result.action is GroomAction.NORMALIZED_ONLY


def test_groom_unknown_ref_errors(tmp_path: Path):
    with pytest.raises(GroomError):
        run_groom(str(tmp_path / "nonexistent.md"))


# ── as_event payload ─────────────────────────────────────────────────────


def test_event_payload_for_refusal():
    fetch = _fake_fetch({"title": "t", "body": _NO_DIAGNOSIS_BUG_BODY, "labels": ["bug"]})
    result = run_groom("123", fetch_issue=fetch, edit_issue_body=_no_op_edit)
    ev = result.as_event()
    assert ev["kind"] == "groom"
    assert ev["action"] == "refused"
    assert ev["bug_diagnosis_state"] == "no-diagnosis"
    assert ev["applied"] is False
    assert ev["refusal_reason"] is not None


def test_event_payload_for_cause_unknown():
    fetch = _fake_fetch({"title": "t", "body": _CAUSE_UNKNOWN_BUG_BODY, "labels": ["bug"]})
    result = run_groom("123", fetch_issue=fetch, edit_issue_body=_no_op_edit)
    ev = result.as_event()
    assert ev["action"] == "normalized-only"
    assert ev["bug_diagnosis_state"] == "cause-unknown"


def test_event_payload_for_restructured():
    fetch = _fake_fetch({"title": "t", "body": _CONFIRMED_BUG_BODY, "labels": ["bug"]})
    result = run_groom("123", fetch_issue=fetch, edit_issue_body=_no_op_edit)
    ev = result.as_event()
    assert ev["action"] == "restructured"
    assert ev["bug_diagnosis_state"] == "confirmed-cause"


# ── Content groom cannot supply (#2129) ───────────────────────────────────


def _feature_fetch():
    return _fake_fetch(
        {
            "title": "Add export",
            "body": "## What\n\nUsers can export.\n",
            "labels": ["enhancement"],
        }
    )


def test_groom_stub_body_still_fails_the_checks_it_did_not_resolve():
    """The proposed body must not pass a check groom did not actually
    resolve — re-running the shape gate on it keeps both findings."""
    result = run_groom("42", fetch_issue=_feature_fetch(), edit_issue_body=_no_op_edit)
    codes = {
        r.code for r in shape_check("Add export", result.proposed_body, ["enhancement"]).reasons
    }
    assert "missing_example" in codes
    assert "missing_acceptance_criteria" in codes


def test_groom_marks_stub_sections_as_placeholders():
    result = run_groom("42", fetch_issue=_feature_fetch(), edit_issue_body=_no_op_edit)
    assert has_placeholder_marker(result.proposed_body)


def test_unsupplied_findings_are_reported_in_the_event_payload():
    result = run_groom("42", fetch_issue=_feature_fetch(), edit_issue_body=_no_op_edit)
    ev = result.as_event()
    assert ev["unsupplied_findings"] == ["missing_acceptance_criteria", "missing_example"]


def test_next_command_is_not_ready_when_content_is_unsupplied():
    result = run_groom("42", fetch_issue=_feature_fetch(), edit_issue_body=_no_op_edit)
    assert "--add-label ready" not in (result.next_command or "")


def test_fabricated_resolution_is_discarded_and_body_falls_back_to_normalization(monkeypatch):
    """Mechanical guard: any restructure that resolves an unsuppliable
    finding is discarded, whatever text produced it."""

    def fabricate(body: str) -> str:
        return (
            body.rstrip()
            + "\n\n## Acceptance criteria\n\n- Export writes a CSV file\n"
            + "\n## Example\n\n```\nexport --format csv > out.csv\n```\n"
        )

    monkeypatch.setattr(groom_flow, "_restructure_feature_body", fabricate)
    result = run_groom("42", fetch_issue=_feature_fetch(), edit_issue_body=_no_op_edit)
    assert "## Acceptance criteria" not in result.proposed_body
    assert result.post_verdict is not ShapeVerdict.RUNNABLE
    assert result.unsupplied_findings == ("missing_acceptance_criteria", "missing_example")


def test_body_with_real_content_keeps_no_unsupplied_findings():
    fetch = _fake_fetch(
        {
            "title": "Add export",
            "body": (
                "## What\n\nUsers can export.\n\n"
                "## Acceptance criteria\n\n- Export writes a CSV to the download directory\n\n"
                "## Example\n\n```\nexport --format csv > out.csv\n```\n"
            ),
            "labels": ["enhancement"],
        }
    )
    result = run_groom("42", fetch_issue=fetch, edit_issue_body=_no_op_edit)
    assert result.unsupplied_findings == ()
    assert result.post_verdict is ShapeVerdict.RUNNABLE
