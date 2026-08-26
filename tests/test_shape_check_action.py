"""Tests for the shape_check GitHub Action entrypoint.

Covers the Action-level behavior: label apply/remove, single bot comment with
update-in-place, tracking_only, superseded, and the idempotent edit flows
required by #811's acceptance criteria.
"""

from __future__ import annotations

import textwrap
from typing import Any

from theforge.shape_check import Reason, Severity, Shape, ShapeResult, SuggestedAction
from theforge.shape_check.action import (
    COMMENT_MARKER,
    ActionConfig,
    find_bot_comment,
    render_comment,
    run_action,
)

BOT_LOGIN = "github-actions[bot]"


WELL_FORMED_BODY = textwrap.dedent(
    """\
    ## What
    Do a thing.

    ## Acceptance Criteria
    - The command returns 0 on success.
    - On failure, writes a diagnostic to stderr.
    """
)

ADVISORY_DONE_STATE_BODY = textwrap.dedent(
    """\
    ## What
    Add a CLI flag.

    ## Why
    Users need a way to bypass the gate.

    ## Example
    ```text
    Before:
    $ forge sprint
    [forge] 2 issue(s) flagged by shape gate

    After:
    $ forge sprint --force
    [forge] sprint started with every issue
    ```

    ## Acceptance Criteria
    - The toggle is available to operators.
    """
)

CAUSE_UNKNOWN_BUG_BODY = textwrap.dedent(
    """\
    ## Observed behavior
    `forge status --ready` lists issues the gate refuses.

    ## Expected behavior
    The listing agrees with sprint entry.

    ## Diagnosis
    - **Observed symptom:** the queue and the gate disagree.
    - **Evidence:** run id `abc123`.
    - **Confirmed cause:** unknown
    - **Affected code path:** `ready_queue.build_ready_queue`.
    - **Fix-success criterion:** the queue and the gate cannot disagree.
    """
)

SUPERSEDED_CAUSE_UNKNOWN_BUG_BODY = textwrap.dedent(
    f"""\
    Superseded by #999.

    {CAUSE_UNKNOWN_BUG_BODY}"""
)


class FakeGitHubAPI:
    """In-memory GitHubAPI double. Records calls; models labels + comments."""

    def __init__(self, *, issue_number: int, labels: list[str] | None = None) -> None:
        self.issue_number = issue_number
        self.labels: list[str] = list(labels or [])
        self.comments: list[dict[str, Any]] = []
        self._next_comment_id = 1000
        self.calls: list[tuple[str, Any]] = []

    def add_label(self, issue_number: int, label: str) -> None:
        self.calls.append(("add_label", (issue_number, label)))
        if label not in self.labels:
            self.labels.append(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.calls.append(("remove_label", (issue_number, label)))
        if label in self.labels:
            self.labels.remove(label)

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        self.calls.append(("list_comments", issue_number))
        return list(self.comments)

    def create_comment(self, issue_number: int, body: str) -> None:
        self.calls.append(("create_comment", (issue_number, body)))
        self.comments.append(
            {
                "id": self._next_comment_id,
                "user": {"login": BOT_LOGIN},
                "body": body,
            }
        )
        self._next_comment_id += 1

    def update_comment(self, comment_id: int, body: str) -> None:
        self.calls.append(("update_comment", (comment_id, body)))
        for c in self.comments:
            if c["id"] == comment_id:
                c["body"] = body
                return
        raise AssertionError(f"no comment with id={comment_id}")

    def bot_login(self) -> str:
        return BOT_LOGIN


def _event(
    *,
    action: str,
    number: int,
    title: str,
    body: str,
    labels: list[str] | None = None,
    author: str = "some-user",
) -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": n} for n in (labels or [])],
            "user": {"login": author},
        },
    }


# ----- render_comment / find_bot_comment ----------------------------------


class TestRenderComment:
    def test_includes_marker_on_first_line(self):
        result = ShapeResult(
            shape=Shape.RUNNABLE,
            reasons=(),
            suggested_action=SuggestedAction.PROCEED,
        )
        out = render_comment(result)
        assert out.splitlines()[0] == COMMENT_MARKER

    def test_includes_reason_codes_and_severity(self):
        result = ShapeResult(
            shape=Shape.NEEDS_GROOMING,
            reasons=(
                Reason(
                    code="missing_acceptance_criteria",
                    severity=Severity.BLOCKING,
                    detail="No AC section found.",
                ),
            ),
            suggested_action=SuggestedAction.CLARIFY,
        )
        out = render_comment(result)
        assert "missing_acceptance_criteria" in out
        assert "blocking" in out
        assert "No AC section found." in out

    def test_includes_admission_verdict(self):
        result = ShapeResult(
            shape=Shape.RUNNABLE,
            suggested_action=SuggestedAction.PROCEED,
        )
        out = render_comment(result)
        assert "- admission verdict: `runnable`" in out


class TestFindBotComment:
    def test_matches_by_author_and_marker(self):
        comments = [
            {"id": 1, "user": {"login": "someone-else"}, "body": COMMENT_MARKER},
            {"id": 2, "user": {"login": BOT_LOGIN}, "body": "no marker here"},
            {"id": 3, "user": {"login": BOT_LOGIN}, "body": f"{COMMENT_MARKER}\nfoo"},
        ]
        match = find_bot_comment(comments, BOT_LOGIN)
        assert match is not None and match["id"] == 3

    def test_returns_none_when_missing(self):
        assert find_bot_comment([], BOT_LOGIN) is None


# ----- run_action scenarios -----------------------------------------------


class TestRunActionOpened:
    def test_blocking_reasons_applies_label_and_posts_comment(self):
        api = FakeGitHubAPI(issue_number=42)
        event = _event(
            action="opened",
            number=42,
            title="Do a thing",
            body="## What\nDo stuff.",  # missing AC -> blocking
            labels=[],
        )

        result = run_action(event, api)

        assert result.shape is Shape.NEEDS_GROOMING
        assert "needs-grooming" in api.labels
        assert len(api.comments) == 1
        assert COMMENT_MARKER in api.comments[0]["body"]

    def test_runnable_issue_no_label_no_comment_reasons(self):
        api = FakeGitHubAPI(issue_number=7)
        event = _event(
            action="opened",
            number=7,
            title="Add a flag",
            body=WELL_FORMED_BODY,
            labels=["enhancement"],
        )

        result = run_action(event, api)

        assert result.shape is Shape.RUNNABLE
        assert "needs-grooming" not in api.labels
        # Still posts a single comment (no findings); AC says "exactly one".
        assert len(api.comments) == 1


class TestRunActionEdited:
    def test_edit_clears_reasons_removes_label_and_updates_comment(self):
        api = FakeGitHubAPI(issue_number=99, labels=["needs-grooming"])
        # Seed prior bot comment simulating previous run.
        api.comments.append(
            {
                "id": 500,
                "user": {"login": BOT_LOGIN},
                "body": f"{COMMENT_MARKER}\nold findings here",
            }
        )
        event = _event(
            action="edited",
            number=99,
            title="Add a flag",
            body=WELL_FORMED_BODY,
            labels=["needs-grooming", "enhancement"],
        )

        result = run_action(event, api)

        assert result.shape is Shape.RUNNABLE
        assert "needs-grooming" not in api.labels
        # Same comment id — updated in place, no new comment created.
        assert len(api.comments) == 1
        assert api.comments[0]["id"] == 500
        assert "old findings here" not in api.comments[0]["body"]
        # No create_comment call should appear in the call log.
        assert not any(name == "create_comment" for name, _ in api.calls)

    def test_edit_adds_new_reasons_updates_comment_in_place(self):
        api = FakeGitHubAPI(issue_number=12)
        api.comments.append(
            {
                "id": 800,
                "user": {"login": BOT_LOGIN},
                "body": f"{COMMENT_MARKER}\nOld body",
            }
        )
        event = _event(
            action="edited",
            number=12,
            title="Do a thing",
            body="## What\nDo stuff.",  # now missing AC
            labels=[],
        )

        run_action(event, api)

        assert len(api.comments) == 1
        assert api.comments[0]["id"] == 800
        # Updated content, not a new comment.
        assert not any(name == "create_comment" for name, _ in api.calls)
        assert any(name == "update_comment" for name, _ in api.calls)

    def test_repeated_edit_with_same_findings_does_not_spam(self):
        api = FakeGitHubAPI(issue_number=33)
        event = _event(
            action="opened",
            number=33,
            title="Do a thing",
            body="## What\nDo stuff.",
            labels=[],
        )
        run_action(event, api)
        first_body = api.comments[0]["body"]

        # Replay identical edited event.
        event["action"] = "edited"
        run_action(event, api)

        assert len(api.comments) == 1
        assert api.comments[0]["body"] == first_body
        # No update_comment call on second run since body is identical.
        update_calls = [c for c in api.calls if c[0] == "update_comment"]
        assert update_calls == []

    def test_edit_with_advisory_only_done_state_does_not_apply_needs_grooming(self):
        api = FakeGitHubAPI(issue_number=34, labels=["enhancement"])
        event = _event(
            action="edited",
            number=34,
            title="Add a flag",
            body=ADVISORY_DONE_STATE_BODY,
            labels=["enhancement"],
        )

        result = run_action(event, api)

        assert result.shape is Shape.RUNNABLE
        assert "needs-grooming" not in api.labels

    def test_edit_with_cause_unknown_bug_applies_needs_grooming_and_reports_verdict(self):
        api = FakeGitHubAPI(issue_number=35, labels=["bug"])
        event = _event(
            action="edited",
            number=35,
            title="Queue disagreement",
            body=CAUSE_UNKNOWN_BUG_BODY,
            labels=["bug"],
        )

        result = run_action(event, api)

        assert result.shape is Shape.RUNNABLE
        assert "needs-grooming" in api.labels
        assert result.verdict.value == "diagnosis_cause_unknown"
        assert any(
            "admission verdict: `diagnosis_cause_unknown`" in c["body"] for c in api.comments
        )

    def test_edit_with_superseded_cause_unknown_bug_keeps_blocking_verdict(self):
        api = FakeGitHubAPI(issue_number=36, labels=["bug", "needs-grooming"])
        event = _event(
            action="edited",
            number=36,
            title="Queue disagreement",
            body=SUPERSEDED_CAUSE_UNKNOWN_BUG_BODY,
            labels=["bug", "needs-grooming"],
        )

        result = run_action(event, api)

        assert result.shape is Shape.SUPERSEDED
        assert result.verdict.value == "duplicate_or_stale"
        assert "needs-grooming" not in api.labels
        assert any("admission verdict: `duplicate_or_stale`" in c["body"] for c in api.comments)
        assert any("| `blocking` | `superseded` |" in c["body"] for c in api.comments)


class TestRunActionTrackingOnly:
    def test_applies_tracking_label(self):
        api = FakeGitHubAPI(issue_number=55)
        event = _event(
            action="opened",
            number=55,
            title="Epic: big initiative",
            body=WELL_FORMED_BODY,
            labels=[],
        )

        result = run_action(event, api)

        assert result.shape is Shape.TRACKING_ONLY
        assert "epic" in api.labels
        # Not needs-grooming — tracking issues are a separate shape.
        assert "needs-grooming" not in api.labels

    def test_respects_custom_tracking_label(self):
        api = FakeGitHubAPI(issue_number=56)
        event = _event(
            action="opened",
            number=56,
            title="Epic: big initiative",
            body=WELL_FORMED_BODY,
            labels=[],
        )
        config = ActionConfig(tracking_label="tracking")

        run_action(event, api, config)

        assert "tracking" in api.labels


class TestRunActionSuperseded:
    def test_comment_only_no_close(self):
        api = FakeGitHubAPI(issue_number=77)
        event = _event(
            action="opened",
            number=77,
            title="Old ticket",
            body="This is superseded by #999.",
            labels=[],
        )

        result = run_action(event, api)

        assert result.shape is Shape.SUPERSEDED
        # Default config: we do NOT auto-close; we only comment + don't label.
        assert len(api.comments) == 1
        # The action never invokes any close/state API on the issue.
        assert not any(name in ("close_issue", "update_state") for name, _ in api.calls)


class TestRunActionNeverRejects:
    """The AC says a malformed issue still gets created — the Action never rejects."""

    def test_blocking_result_does_not_raise(self):
        api = FakeGitHubAPI(issue_number=1)
        event = _event(
            action="opened",
            number=1,
            title="empty",
            body="",
            labels=[],
        )
        # Must not raise even with a maximally bad issue.
        result = run_action(event, api)
        assert result.shape is Shape.NEEDS_GROOMING


class TestLogging:
    def test_emits_greppable_log_line(self, caplog):
        import logging

        api = FakeGitHubAPI(issue_number=4242)
        event = _event(
            action="opened",
            number=4242,
            title="bad issue",
            body="## What\nno AC here",
            labels=[],
            author="some-bot",
        )
        with caplog.at_level(logging.INFO, logger="shape_check.action"):
            run_action(event, api)

        assert any(
            "shape_check issue=4242" in rec.message
            and "author=some-bot" in rec.message
            and "shape=needs_grooming" in rec.message
            for rec in caplog.records
        )
