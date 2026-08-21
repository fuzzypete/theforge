"""Tests for sprint-entry shape gate (needs-grooming + local re-check)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from theforge.sprint.shape_gate import (
    NEEDS_GROOMING_LABEL,
    REOPENED_STALE_CONTRACT_CODE,
    ShapeGateResult,
    SkippedIssue,
    _fetch_bot_reason_codes,
    _fetch_issue_timeline,
    apply_shape_gate,
    format_skipped_warning,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────

_RUNNABLE_BODY = """## What

Add a CLI flag.

## Why

Users need a way to bypass the gate.

## Example

```text
$ forge sprint --force
[forge] 2 issue(s) flagged by shape gate
```

## Acceptance Criteria

- `forge sprint --force` emits every issue regardless of shape check
- the warning output reports every skipped issue's reason codes
"""

_BAD_BODY = "just a one-liner, no acceptance criteria, no structure"

# A story whose third acceptance criterion depends on the recorded outcome of a
# live run — knowable at intake, unsatisfiable by any diff (#1735 / #1425).
_LIVE_EVIDENCE_BODY = """## What

Enrich the diagnose prompt with environment context.

## Acceptance criteria

- The diagnose prompt includes an environment briefing section.
- The briefing is templated from project structure, not hardcoded.
- A diagnose run on a sparse-body issue produces a complete artifact within
  budget on a representative landing-failure bug.
"""

_BUG_WITH_FEATURE_AC_BODY = """## What happened

Contract tests never run in CI.

## What was expected

Provider argv drift is caught before release.

## Diagnosis

- **Observed symptom:** Contract tests never run in CI.
- **Evidence:** CI job logs show the contract target never executes.
- **Ruled out:** Missing test files; the suite is present locally.
- **Confirmed cause:** The sprint runner never invokes the contract target.
- **Affected code path:** sprint.runner dispatch setup.
- **Fix-success criterion:** CI runs the contract target before release.

## Acceptance criteria

- A `make test-contract` target exists and runs in CI.
"""

_DIAGNOSED_BUG_WITH_FIX_NOTES = """## What happened

Contract tests never run in CI.

## What was expected

Provider argv drift is caught before release.

## Diagnosis

- **Observed symptom:** Contract tests never run in CI.
- **Evidence:** CI job logs show the contract target never executes.
- **Ruled out:** Missing test files; the suite is present locally.
- **Confirmed cause:** The sprint runner never invokes the contract target.
- **Affected code path:** sprint.runner dispatch setup.
- **Fix-success criterion:** CI runs the contract target before release.

## Notes

the fix belongs in `runners/api.py`
"""

_CAUSE_UNKNOWN_BUG_WITH_FIX_NOTES = """## Observed behavior

`forge status --ready` lists issues the gate refuses.

## Expected behavior

The listing agrees with the gate.

## Diagnosis

- **Observed symptom:** the ready listing and the gate disagree.
- **Evidence:** run id `1ff6b0bb7992` — five ready issues, five refusals.
- **Confirmed cause:** unknown
- **Affected code path:** `ready_queue.build_ready_queue`.
- **Fix-success criterion:** the listing and sprint entry cannot disagree.

## Notes

the fix belongs in `ready_queue.py`
"""


def _fake_detail(body: str, labels: list[str], title: str = "Some issue"):
    def _fetch(_number, _project_root):
        return {"title": title, "body": body, "labels": labels}

    return _fetch


# ── Label-skip path ─────────────────────────────────────────────────────────


def test_stale_bot_comment_does_not_affect_runnable_issue(tmp_path: Path) -> None:
    issues = [{"number": 42, "title": "Runnable issue"}]

    with patch(
        "theforge.sprint.shape_gate._fetch_bot_reason_codes",
        side_effect=AssertionError("bot comment must not be consulted for verdicts"),
    ):
        result = apply_shape_gate(
            issues,
            tmp_path,
            fetch_detail=_fake_detail(_RUNNABLE_BODY, ["enhancement"]),
        )

    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    assert result.skipped == []


def test_label_skip_uses_live_reason_details_when_issue_is_tracking_only(
    tmp_path: Path,
) -> None:
    issues = [{"number": 7, "title": "Stale label"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_RUNNABLE_BODY, [NEEDS_GROOMING_LABEL, "epic"]),
    )

    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.source == "label"
    assert entry.reason_codes == ("epic_or_tracking",)
    assert "Label 'epic' present" in entry.detail


def test_label_skip_falls_back_to_label_detail_when_live_check_is_runnable(
    tmp_path: Path,
) -> None:
    issues = [{"number": 8, "title": "Label only"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_RUNNABLE_BODY, [NEEDS_GROOMING_LABEL, "enhancement"]),
    )

    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.source == "label"
    assert entry.reason_codes == ("needs_grooming_label",)
    assert entry.detail == "issue carries 'needs-grooming' label"


# ── Live-run evidence criterion (#1735) ─────────────────────────────────────


def test_live_run_evidence_criterion_is_not_dispatched(tmp_path: Path) -> None:
    """A story carrying a live-run-outcome criterion must not silently dispatch.

    Seam test across the sprint-entry boundary: the criterion is detectable
    from the story text, so the gate keeps the issue out of the dev loop and
    surfaces which criterion the loop cannot satisfy before any dev budget is
    spent.
    """
    issues = [{"number": 1425, "title": "Enrich diagnose prompt"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_LIVE_EVIDENCE_BODY, ["enhancement"]),
    )

    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.source == "local_check"
    assert "criterion_needs_live_evidence" in entry.reason_codes
    assert entry.verdict == "needs_operator_action"
    # The operator is told which criterion cannot be satisfied and why.
    assert "complete artifact within" in entry.detail
    assert "remain dispatchable" in entry.detail


# ── Intake-remediated label suppression (re-exec carry) ─────────────────────


def test_intake_remediated_issue_with_stale_grooming_label_runs(tmp_path: Path) -> None:
    """If a sprint just remediated an issue's body, the post-re-exec gate
    must trust the local re-check over the async-lagging ``needs-grooming``
    label that the GH labeler workflow has not yet reconciled.
    """
    issues = [{"number": 1545, "title": "Just-remediated"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_RUNNABLE_BODY, [NEEDS_GROOMING_LABEL, "enhancement"]),
        intake_remediated_numbers={1545},
    )

    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    assert result.skipped == []


def test_intake_remediated_set_does_not_rescue_unrelated_issues(tmp_path: Path) -> None:
    """The suppression is per-issue: a remediated issue is rescued, an
    unrelated needs-grooming issue is still skipped on the same call.
    """
    issues = [
        {"number": 1545, "title": "Just-remediated"},
        {"number": 9999, "title": "Stale label, not remediated"},
    ]

    def fetch(number, _project_root):
        return {
            "title": f"#{number}",
            "body": _RUNNABLE_BODY,
            "labels": [NEEDS_GROOMING_LABEL, "enhancement"],
        }

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=fetch,
        intake_remediated_numbers={1545},
    )

    assert [i["number"] for i in result.runnable] == [1545]
    assert len(result.skipped) == 1
    assert result.skipped[0].issue_number == 9999


def test_intake_remediated_does_not_paper_over_broken_body(tmp_path: Path) -> None:
    """Suppression of the label-source skip must NOT bypass the local check.
    If the body is still malformed (e.g. body-edit didn't actually fix it),
    the issue still drops via the local_check path.
    """
    issues = [{"number": 1545, "title": "Edit didn't fix it"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_BAD_BODY, [NEEDS_GROOMING_LABEL]),
        intake_remediated_numbers={1545},
    )

    assert result.runnable == []
    assert len(result.skipped) == 1
    assert result.skipped[0].source == "local_check"


# ── Local-check-skip path ───────────────────────────────────────────────────


def test_local_check_skip_catches_stale_label_loophole(tmp_path: Path) -> None:
    """Issue without needs-grooming but still malformed must be skipped."""
    issues = [{"number": 13, "title": "Edited but Action never ran"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_BAD_BODY, []),
    )

    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.source == "local_check"
    assert entry.reason_codes
    assert "No acceptance criteria section" in entry.detail


def test_local_check_allows_runnable_issue(tmp_path: Path) -> None:
    issues = [{"number": 99, "title": "Well-formed"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_RUNNABLE_BODY, ["enhancement"]),
    )

    assert len(result.runnable) == 1
    assert result.runnable[0]["number"] == 99
    assert result.runnable[0]["shape_verdict"] == "runnable"
    assert result.skipped == []


def test_bug_body_with_feature_section_is_refused_with_type_shape_verdict(
    tmp_path: Path,
) -> None:
    issues = [{"number": 2509, "title": "Contract target missing"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(_BUG_WITH_FEATURE_AC_BODY, ["bug"], "Contract target missing"),
    )

    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.source == "local_check"
    assert entry.verdict == "needs_grooming_type_shape"
    assert entry.reason_codes == ("type_shape_contradiction",)
    assert "acceptance-criteria section" in entry.detail
    assert "bugs use observed/expected plus diagnosis" in entry.detail


def test_runnable_bug_surfaces_local_advisories_without_changing_verdict(
    tmp_path: Path,
) -> None:
    issues = [{"number": 2510, "title": "Contract target missing"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(
            _DIAGNOSED_BUG_WITH_FIX_NOTES,
            ["bug"],
            "Contract target missing",
        ),
    )

    assert [r["number"] for r in result.runnable] == [2510]
    assert result.runnable[0]["shape_verdict"] == "runnable"
    assert result.runnable[0]["shape_advisories"] == ["bug_fix_location_prescription"]
    assert result.skipped == []
    assert len(result.advisories) == 1
    entry = result.advisories[0]
    assert entry.reason_codes == ("bug_fix_location_prescription",)
    assert "fix location" in entry.detail


def test_cause_unknown_refusal_does_not_promote_phrase_advice_to_skipped_codes(
    tmp_path: Path,
) -> None:
    issues = [{"number": 2511, "title": "Ready queue disagreement"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=_fake_detail(
            _CAUSE_UNKNOWN_BUG_WITH_FIX_NOTES,
            ["bug"],
            "Ready queue disagreement",
        ),
    )

    assert result.runnable == []
    assert len(result.skipped) == 1
    entry = result.skipped[0]
    assert entry.verdict == "diagnosis_cause_unknown"
    assert entry.reason_codes == ("diagnosis_cause_unknown",)
    assert "fix location" not in entry.detail
    assert result.advisories == []


def test_reopened_issue_with_stale_body_is_advisory_not_blocking(tmp_path: Path) -> None:
    """A reopened bug whose body predates the reopen event is no longer
    refused at sprint entry; the rule is downgraded to a non-blocking
    advisory because the underlying check verifies only timeline-event
    ordering, not body content."""
    issues = [{"number": 55, "title": "Reopened"}]

    def fetch(_number, _project_root):
        return {
            "title": "Reopened",
            "body": _RUNNABLE_BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "comments": [
                {
                    "author": {"login": "operator"},
                    "body": "The original close only covered half the work.",
                    "createdAt": "2026-05-02T12:30:00Z",
                }
            ],
            "timeline": [
                {
                    "event": "reopened",
                    "created_at": "2026-05-02T12:00:00Z",
                    "actor": {"login": "operator"},
                }
            ],
        }

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)

    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    assert result.skipped == []
    assert len(result.advisories) == 1
    entry = result.advisories[0]
    assert entry.reason_codes == (REOPENED_STALE_CONTRACT_CODE,)
    # Honest framing: no instruction to "reconcile the body before sprinting".
    assert "reconcile the body before sprinting" not in entry.detail
    assert "postdates the last body edit" in entry.detail


def test_reopened_issue_with_lastEditedAt_after_reopen_comment_is_runnable(
    tmp_path: Path,
) -> None:
    """Body's lastEditedAt > reopen-comment timestamp must clear the gate.

    Repro for #1135: operator edits body via ``gh issue edit`` (or web UI)
    after a reopen-context comment is posted. ``gh issue edit`` updates
    ``lastEditedAt`` reliably even when the timeline doesn't include an
    ``edited`` event with ``changes.body``.
    """
    issues = [{"number": 1135, "title": "Reconciled body"}]

    def fetch(_number, _project_root):
        return {
            "title": "Reconciled body",
            "body": _RUNNABLE_BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "comments": [
                {
                    "author": {"login": "operator"},
                    "body": "Reopen with new context.",
                    "createdAt": "2026-05-02T14:35:00Z",
                }
            ],
            # lastEditedAt set by `gh issue edit` after operator reconciled body.
            "lastEditedAt": "2026-05-03T10:00:00Z",
            "timeline": [
                {
                    "event": "reopened",
                    "created_at": "2026-05-02T14:00:00Z",
                    "actor": {"login": "operator"},
                }
                # No timeline ``edited`` event — gh issue edit doesn't always
                # produce one. The lastEditedAt field is the reliable signal.
            ],
        }

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)

    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    assert result.skipped == []


def test_reopened_issue_lastEditedAt_between_two_comments_still_flagged(
    tmp_path: Path,
) -> None:
    """Two post-reopen comments + body edit between them must still flag.

    The contract is defined against the *most recent* reopen-context
    comment. If the body edit reconciles an earlier comment but predates a
    later operator comment, the body is still stale relative to the newest
    context, and the advisory must keep firing until the body's
    lastEditedAt is newer than the latest comment. The finding is
    non-blocking (advisory channel), so the issue still runs.
    """
    issues = [{"number": 61, "title": "Stale vs. latest comment"}]

    def fetch(_number, _project_root):
        return {
            "title": "Stale vs. latest comment",
            "body": _RUNNABLE_BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "comments": [
                {
                    "author": {"login": "operator"},
                    "body": "First reopen-context comment.",
                    "createdAt": "2026-05-02T12:30:00Z",
                },
                {
                    "author": {"login": "operator"},
                    "body": "Second reopen-context comment with new context.",
                    "createdAt": "2026-05-04T09:00:00Z",
                },
            ],
            # Body edit lands between the two comments — newer than the
            # first, older than the second. Contract is still stale.
            "lastEditedAt": "2026-05-03T10:00:00Z",
            "timeline": [
                {
                    "event": "reopened",
                    "created_at": "2026-05-02T12:00:00Z",
                    "actor": {"login": "operator"},
                }
            ],
        }

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)

    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    assert result.skipped == []
    assert len(result.advisories) == 1
    entry = result.advisories[0]
    assert entry.reason_codes == (REOPENED_STALE_CONTRACT_CODE,)
    # The detail must reference the *latest* comment's timestamp, not the
    # earlier one — otherwise an operator might think editing past the
    # earlier comment was sufficient.
    assert "2026-05-04T09:00:00Z" in entry.detail


def test_reopened_issue_lastEditedAt_before_comment_still_flagged(
    tmp_path: Path,
) -> None:
    """A body edit that predates the reopen comment does not clear the
    advisory; the finding is non-blocking, so the issue still runs."""
    issues = [{"number": 60, "title": "Stale despite older edit"}]

    def fetch(_number, _project_root):
        return {
            "title": "Stale despite older edit",
            "body": _RUNNABLE_BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "comments": [
                {
                    "author": {"login": "operator"},
                    "body": "Reopen with new context.",
                    "createdAt": "2026-05-02T14:35:00Z",
                }
            ],
            # Body edit is older than the reopen-context comment.
            "lastEditedAt": "2026-05-01T09:00:00Z",
            "timeline": [
                {
                    "event": "reopened",
                    "created_at": "2026-05-02T14:00:00Z",
                    "actor": {"login": "operator"},
                }
            ],
        }

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)

    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    assert result.skipped == []
    assert len(result.advisories) == 1
    assert result.advisories[0].reason_codes == (REOPENED_STALE_CONTRACT_CODE,)


def test_reopened_issue_with_body_edit_after_reopen_is_runnable(tmp_path: Path) -> None:
    issues = [{"number": 56, "title": "Reconciled"}]

    def fetch(_number, _project_root):
        return {
            "title": "Reconciled",
            "body": _RUNNABLE_BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "comments": [
                {
                    "author": {"login": "operator"},
                    "body": "Need one more follow-up change.",
                    "createdAt": "2026-05-02T12:30:00Z",
                }
            ],
            "timeline": [
                {
                    "event": "reopened",
                    "created_at": "2026-05-02T12:00:00Z",
                    "actor": {"login": "operator"},
                },
                {
                    "event": "edited",
                    "created_at": "2026-05-02T13:00:00Z",
                    "changes": {"body": {"from": "old body"}},
                },
            ],
        }

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch)

    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    assert result.skipped == []


# ── Force override ─────────────────────────────────────────────────────────


def test_force_override_returns_all_issues_runnable_but_keeps_skipped_list(
    tmp_path: Path,
) -> None:
    issues = [
        {"number": 1, "title": "Good"},
        {"number": 2, "title": "Flagged"},
    ]

    def fetch(number, _project_root):
        if number == 1:
            return {"title": "Good", "body": _RUNNABLE_BODY, "labels": ["enhancement"]}
        return {"title": "Flagged", "body": _RUNNABLE_BODY, "labels": [NEEDS_GROOMING_LABEL]}

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=fetch,
        force=True,
    )

    # Force: every input issue is runnable...
    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    # ...but the skipped list is still populated so the CLI can warn.
    assert len(result.skipped) == 1
    assert result.skipped[0].issue_number == 2
    assert result.skipped[0].source == "label"


def test_force_override_preserves_reopened_stale_contract_advisory(tmp_path: Path) -> None:
    """Under --force the reopen advisory remains visible (advisory channel),
    matching the prior force-mode behavior of preserving the warning while
    still running every issue."""
    issues = [{"number": 57, "title": "Reopened"}]

    def fetch(_number, _project_root):
        return {
            "title": "Reopened",
            "body": _RUNNABLE_BODY,
            "labels": ["enhancement"],
            "state": "OPEN",
            "comments": [
                {
                    "author": {"login": "operator"},
                    "body": "Still missing the reopen follow-up.",
                    "createdAt": "2026-05-02T12:30:00Z",
                }
            ],
            "timeline": [
                {
                    "event": "reopened",
                    "created_at": "2026-05-02T12:00:00Z",
                    "actor": {"login": "operator"},
                }
            ],
        }

    result = apply_shape_gate(issues, tmp_path, fetch_detail=fetch, force=True)

    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    assert result.skipped == []
    assert len(result.advisories) == 1
    assert result.advisories[0].reason_codes == (REOPENED_STALE_CONTRACT_CODE,)


# ── Mixed sprint ───────────────────────────────────────────────────────────


def test_mixed_sprint_partitions_runnable_vs_skipped(tmp_path: Path) -> None:
    issues = [
        {"number": 1, "title": "runnable"},
        {"number": 2, "title": "stale label"},
        {"number": 3, "title": "malformed"},
    ]

    def fetch(number, _project_root):
        if number == 1:
            return {"title": "runnable", "body": _RUNNABLE_BODY, "labels": ["enhancement"]}
        if number == 2:
            return {
                "title": "stale label",
                "body": _RUNNABLE_BODY,
                "labels": [NEEDS_GROOMING_LABEL],
            }
        return {"title": "malformed", "body": _BAD_BODY, "labels": []}

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=fetch,
    )

    assert [i["number"] for i in result.runnable] == [1]
    sources = {s.issue_number: s.source for s in result.skipped}
    assert sources == {2: "label", 3: "local_check"}


# ── Fail-open on gh errors ─────────────────────────────────────────────────


def test_fetch_failure_leaves_issue_runnable(tmp_path: Path) -> None:
    """If gh returns nothing, do not invent a skip — leave it to sources.py."""
    issues = [{"number": 1, "title": "unknown"}]

    result = apply_shape_gate(
        issues,
        tmp_path,
        fetch_detail=lambda _n, _r: None,
    )

    assert [r["number"] for r in result.runnable] == [i["number"] for i in issues]
    assert result.skipped == []


# ── Warning rendering ──────────────────────────────────────────────────────


def test_format_skipped_warning_lists_every_issue() -> None:
    skipped = [
        SkippedIssue(
            issue_number=7,
            reason_codes=("too_many_behavioral_clusters",),
            source="local_check",
            title="Too big",
            detail="Body covers multiple independent behaviors.",
        ),
        SkippedIssue(
            issue_number=12,
            reason_codes=("needs-grooming-label",),
            source="label",
            title="Stale",
            detail="issue carries 'needs-grooming' label",
        ),
    ]

    rendered = format_skipped_warning(skipped)
    assert "#7" in rendered
    assert "#12" in rendered
    assert "local_check" in rendered
    assert "label" in rendered
    assert "too_many_behavioral_clusters" in rendered
    assert "Body covers multiple independent behaviors." in rendered


def test_format_skipped_warning_empty_returns_empty_string() -> None:
    assert format_skipped_warning([]) == ""


# ── SkippedIssue.as_dict is machine-readable ───────────────────────────────


def test_skipped_issue_as_dict_is_machine_readable() -> None:
    entry = SkippedIssue(
        issue_number=5,
        reason_codes=("a", "b"),
        source="label",
        title="t",
    )
    data = entry.as_dict()
    assert data["issue_number"] == 5
    assert data["reason_codes"] == ["a", "b"]
    assert data["source"] == "label"


# ── Bot-comment parser (integration-ish) ───────────────────────────────────


def test_fetch_bot_reason_codes_parses_csv_marker(tmp_path: Path) -> None:
    stdout = (
        '{"comments":[{"body":"some text\\n'
        "<!-- shape-check-reasons: too_many_behavioral_clusters,missing_ac -->"
        '"}]}'
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        codes = _fetch_bot_reason_codes(42, tmp_path)
    assert codes == ["too_many_behavioral_clusters", "missing_ac"]


def test_fetch_bot_reason_codes_parses_json_array(tmp_path: Path) -> None:
    stdout = '{"comments":[{"body":"<!-- shape-check-reasons: [\\"a\\", \\"b\\"] -->"}]}'
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=stdout, stderr="")
        codes = _fetch_bot_reason_codes(42, tmp_path)
    assert codes == ["a", "b"]


def test_fetch_bot_reason_codes_returns_empty_on_gh_failure(tmp_path: Path) -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="fail")
        assert _fetch_bot_reason_codes(42, tmp_path) == []


# ── Timeline pagination (#1271) ────────────────────────────────────────────


def test_fetch_issue_timeline_single_short_page_stops_after_one_call(
    tmp_path: Path,
) -> None:
    page = [{"event": "labeled"}, {"event": "closed"}]
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(page), stderr="")
        events = _fetch_issue_timeline(42, tmp_path)
    assert events == page
    assert mock_run.call_count == 1


def test_fetch_issue_timeline_follows_pagination_beyond_first_page(
    tmp_path: Path,
) -> None:
    first_page = [{"event": "commented", "id": i} for i in range(100)]
    second_page = [{"event": "reopened", "id": 100}]
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(first_page), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(second_page), stderr=""),
        ]
        events = _fetch_issue_timeline(42, tmp_path)
    assert mock_run.call_count == 2
    assert len(events) == 101
    assert events[-1] == {"event": "reopened", "id": 100}


def test_fetch_issue_timeline_stops_at_page_boundary_when_last_page_is_full(
    tmp_path: Path,
) -> None:
    first_page = [{"event": "commented", "id": i} for i in range(100)]
    second_page = [{"event": "reopened", "id": i} for i in range(100, 200)]
    third_page: list[dict] = []
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(first_page), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(second_page), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(third_page), stderr=""),
        ]
        events = _fetch_issue_timeline(42, tmp_path)
    assert mock_run.call_count == 3
    assert len(events) == 200


def test_fetch_issue_timeline_full_page_with_non_dict_entry_still_continues(
    tmp_path: Path,
) -> None:
    """A full page containing a stray non-dict item must not be mistaken for
    a short (final) page — the continuation check is on the raw page length,
    not the post-filter dict count."""
    first_page = [{"event": "commented", "id": i} for i in range(99)] + [None]
    second_page = [{"event": "reopened", "id": 100}]
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(first_page), stderr=""),
            MagicMock(returncode=0, stdout=json.dumps(second_page), stderr=""),
        ]
        events = _fetch_issue_timeline(42, tmp_path)
    assert mock_run.call_count == 2
    assert len(events) == 100
    assert events[-1] == {"event": "reopened", "id": 100}


def test_fetch_issue_timeline_returns_empty_on_first_page_failure(
    tmp_path: Path,
) -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="fail")
        assert _fetch_issue_timeline(42, tmp_path) == []


def test_fetch_issue_timeline_returns_partial_data_on_later_page_failure(
    tmp_path: Path,
) -> None:
    first_page = [{"event": "commented", "id": i} for i in range(100)]
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(first_page), stderr=""),
            MagicMock(returncode=1, stdout="", stderr="fail"),
        ]
        events = _fetch_issue_timeline(42, tmp_path)
    assert len(events) == 100


# ── Sanity: result dataclass ───────────────────────────────────────────────


def test_shape_gate_result_defaults() -> None:
    r = ShapeGateResult()
    assert r.runnable == []
    assert r.skipped == []


# ── Classifier resolution ─────────────────────────────────────────────────


def test_resolve_classifier_honors_heuristic_and_off() -> None:
    from theforge.sprint.shape_gate import _resolve_classifier

    assert _resolve_classifier("heuristic") == "heuristic"
    assert _resolve_classifier("off") == "off"


def test_resolve_classifier_falls_back_when_llm_has_no_caller() -> None:
    from theforge.sprint.shape_gate import _resolve_classifier

    # "llm" mode with no llm_caller can't actually refine — fall back.
    assert _resolve_classifier("llm", llm_caller=None) == "heuristic"


def test_resolve_classifier_passes_through_llm_when_caller_available() -> None:
    from theforge.sprint.shape_gate import _resolve_classifier

    assert _resolve_classifier("llm", llm_caller=lambda _b, _r: None) == "llm"


def test_resolve_classifier_falls_back_on_unknown_mode() -> None:
    from theforge.sprint.shape_gate import _resolve_classifier

    assert _resolve_classifier("bogus-mode") == "heuristic"
