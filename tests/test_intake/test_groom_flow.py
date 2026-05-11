"""Seam-level tests for the forge groom flow.

Cover each branch of the three-state bug taxonomy plus the
no-cause-unknown-to-ready hard invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from theforge.intake.groom_flow import (
    BugDiagnosisState,
    GroomAction,
    GroomError,
    classify_bug_diagnosis,
    run_groom,
)
from theforge.shape_check import ShapeVerdict

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
    # P1: the groomed feature body must be shape-gate-clean.
    assert result.post_verdict is ShapeVerdict.RUNNABLE


def test_groom_docs_reaches_runnable():
    """Docs-typed issues are first-class per ADR-0001; normalize them onto
    the shape gate's recognized type vocabulary so the post-groom verdict
    can reach runnable rather than tripping on missing_type."""
    body = "## What\n\nDocument the new endpoint.\n"
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
    them to a shape-gate-recognized label so they reach a real verdict."""
    body = "## What\n\nUsers can filter by date.\n"
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
    # File on disk has the canonical groomed body, so re-read verdict is runnable.
    assert result.post_verdict is ShapeVerdict.RUNNABLE
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
