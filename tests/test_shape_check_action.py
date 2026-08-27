"""Tests for the shape_check GitHub Action entrypoint.

Covers the Action-level behavior: label apply/remove, single bot comment with
update-in-place, tracking_only, superseded, and the idempotent edit flows
required by #811's acceptance criteria.
"""

from __future__ import annotations

import io
import textwrap
from pathlib import Path
from typing import Any
from urllib import error as urlerror

from theforge.shape_check import Reason, Severity, Shape, ShapeResult, SuggestedAction, check
from theforge.shape_check.action import (
    COMMENT_MARKER,
    ActionConfig,
    HttpGitHubAPI,
    find_bot_comment,
    main,
    render_comment,
    run_action,
    run_sweep,
)
from theforge.shape_check.policy_digest import POLICY_SOURCE_FILES, compute_policy_digest

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

MISSING_AC_BODY = textwrap.dedent(
    """\
    ## What
    Add a CLI flag.

    ## Why
    Users need a way to bypass the gate.

    ## Example
    ```text
    $ forge sprint --force
    [forge] sprint started with every issue
    ```
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

    def __init__(
        self,
        *,
        issue_number: int,
        labels: list[str] | None = None,
        open_issues: list[dict[str, Any]] | None = None,
    ) -> None:
        self.issue_number = issue_number
        self.labels: list[str] = list(labels or [])
        self.comments: list[dict[str, Any]] = []
        self.open_issues: list[dict[str, Any]] = list(open_issues or [])
        self._next_comment_id = 1000
        self.calls: list[tuple[str, Any]] = []

    def add_label(self, issue_number: int, label: str) -> None:
        self.calls.append(("add_label", (issue_number, label)))
        if label not in self.labels:
            self.labels.append(label)
        for issue in self.open_issues:
            if int(issue["number"]) == issue_number:
                labels = issue.setdefault("labels", [])
                if all(existing.get("name") != label for existing in labels):
                    labels.append({"name": label})

    def remove_label(self, issue_number: int, label: str) -> None:
        self.calls.append(("remove_label", (issue_number, label)))
        if label in self.labels:
            self.labels.remove(label)
        for issue in self.open_issues:
            if int(issue["number"]) == issue_number:
                issue["labels"] = [
                    existing
                    for existing in issue.get("labels") or []
                    if existing.get("name") != label
                ]

    def list_open_issues(self) -> list[dict[str, Any]]:
        self.calls.append(("list_open_issues", None))
        return list(self.open_issues)

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        self.calls.append(("list_comments", issue_number))
        if self.open_issues:
            for issue in self.open_issues:
                if int(issue["number"]) == issue_number:
                    return list(issue.get("comments") or [])
        return list(self.comments)

    def create_comment(self, issue_number: int, body: str) -> None:
        self.calls.append(("create_comment", (issue_number, body)))
        comment = {
            "id": self._next_comment_id,
            "user": {"login": BOT_LOGIN},
            "body": body,
        }
        self.comments.append(comment)
        for issue in self.open_issues:
            if int(issue["number"]) == issue_number:
                issue.setdefault("comments", []).append(comment.copy())
        self._next_comment_id += 1

    def update_comment(self, comment_id: int, body: str) -> None:
        self.calls.append(("update_comment", (comment_id, body)))
        for c in self.comments:
            if c["id"] == comment_id:
                c["body"] = body
        for issue in self.open_issues:
            for comment in issue.get("comments") or []:
                if comment["id"] == comment_id:
                    comment["body"] = body
                    return
        if any(comment["id"] == comment_id for comment in self.comments):
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
        out = render_comment(result, "sha256:test")
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
        out = render_comment(result, "sha256:test")
        assert "missing_acceptance_criteria" in out
        assert "blocking" in out
        assert "No AC section found." in out

    def test_includes_admission_verdict(self):
        result = ShapeResult(
            shape=Shape.RUNNABLE,
            suggested_action=SuggestedAction.PROCEED,
        )
        out = render_comment(result, "sha256:test")
        assert "- admission verdict: `runnable`" in out
        assert "- policy digest: `sha256:test`" in out


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

    def test_edit_updates_comment_when_policy_digest_changes(self):
        api = FakeGitHubAPI(issue_number=13, labels=["enhancement"])
        prior = render_comment(
            ShapeResult(shape=Shape.RUNNABLE, suggested_action=SuggestedAction.PROCEED),
            "sha256:stale",
        )
        api.comments.append({"id": 801, "user": {"login": BOT_LOGIN}, "body": prior})
        event = _event(
            action="edited",
            number=13,
            title="Add a flag",
            body=WELL_FORMED_BODY,
            labels=["enhancement"],
        )

        run_action(event, api)

        assert len(api.comments) == 1
        assert api.comments[0]["id"] == 801
        assert compute_policy_digest() in api.comments[0]["body"]
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

    def test_does_not_remove_operator_tracking_label_when_issue_is_no_longer_tracking(self):
        api = FakeGitHubAPI(issue_number=57, labels=["tracking", "enhancement"])
        config = ActionConfig(tracking_label="tracking")
        event = _event(
            action="edited",
            number=57,
            title="Add a flag",
            body=WELL_FORMED_BODY,
            labels=["tracking", "enhancement"],
        )

        run_action(event, api, config)

        assert "tracking" in api.labels
        assert not any(
            name == "remove_label" and payload == (57, "tracking") for name, payload in api.calls
        )


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


class TestSweep:
    def test_preview_reports_deltas_without_writing(self):
        digest = "sha256:test"
        runnable_result = check("Add a flag", WELL_FORMED_BODY, ["enhancement"])
        unchanged_comment = render_comment(
            runnable_result,
            digest,
        )
        stale_comment = render_comment(
            runnable_result,
            "sha256:stale",
        )
        api = FakeGitHubAPI(
            issue_number=1,
            open_issues=[
                {
                    "number": 2,
                    "title": "Add a flag",
                    "body": WELL_FORMED_BODY,
                    "labels": [{"name": "needs-grooming"}, {"name": "enhancement"}],
                    "comments": [{"id": 10, "user": {"login": BOT_LOGIN}, "body": stale_comment}],
                },
                {
                    "number": 1,
                    "title": "Add a flag",
                    "body": MISSING_AC_BODY,
                    "labels": [{"name": "enhancement"}],
                    "comments": [],
                },
                {
                    "number": 3,
                    "title": "Add a flag",
                    "body": WELL_FORMED_BODY,
                    "labels": [{"name": "enhancement"}],
                    "comments": [
                        {
                            "id": 11,
                            "user": {"login": BOT_LOGIN},
                            "body": unchanged_comment,
                        }
                    ],
                },
            ],
        )
        output = io.StringIO()

        plan = run_sweep(api, mode="preview", output=output, policy_digest=digest)

        assert plan.open_issue_count == 3
        assert plan.unchanged_count == 1
        assert plan.change_count == 4
        assert output.getvalue().splitlines() == [
            "shape-check sweep — preview (3 open issues)",
            "  + needs-grooming  #1  missing_acceptance_criteria,no_observable_done_state",
            "  + comment  #1  create",
            "  - needs-grooming  #2  (now runnable)",
            "  ~ comment  #2  update",
            "1 unchanged",
            "4 changes",
        ]
        assert not any(
            name in {"add_label", "remove_label", "create_comment", "update_comment"}
            for name, _ in api.calls
        )

    def test_apply_writes_only_diffs_and_second_run_is_zero_change(self):
        digest = "sha256:test"
        runnable_result = check("Add a flag", WELL_FORMED_BODY, ["enhancement"])
        stale_comment = render_comment(
            runnable_result,
            "sha256:stale",
        )
        api = FakeGitHubAPI(
            issue_number=1,
            open_issues=[
                {
                    "number": 1,
                    "title": "Add a flag",
                    "body": MISSING_AC_BODY,
                    "labels": [{"name": "enhancement"}],
                    "comments": [],
                },
                {
                    "number": 2,
                    "title": "Add a flag",
                    "body": WELL_FORMED_BODY,
                    "labels": [{"name": "needs-grooming"}, {"name": "enhancement"}],
                    "comments": [{"id": 10, "user": {"login": BOT_LOGIN}, "body": stale_comment}],
                },
            ],
        )
        first = io.StringIO()
        second = io.StringIO()

        run_sweep(api, mode="apply", output=first, policy_digest=digest)
        call_count = len(api.calls)
        run_sweep(api, mode="apply", output=second, policy_digest=digest)

        assert "needs-grooming" in [label["name"] for label in api.open_issues[0]["labels"]]
        assert "needs-grooming" not in [label["name"] for label in api.open_issues[1]["labels"]]
        assert f"- policy digest: `{digest}`" in api.open_issues[0]["comments"][0]["body"]
        assert api.open_issues[1]["comments"][0]["body"] == render_comment(runnable_result, digest)
        assert len(api.calls) == call_count + 3
        assert second.getvalue().splitlines() == [
            "shape-check sweep — apply (2 open issues)",
            "2 unchanged",
            "0 changes",
        ]

    def test_apply_keeps_epic_label_and_second_run_is_zero_change(self):
        digest = "sha256:test"
        api = FakeGitHubAPI(
            issue_number=1,
            open_issues=[
                {
                    "number": 1,
                    "title": "Epic: retired initiative",
                    "body": "Superseded by #999.",
                    "labels": [{"name": "epic"}],
                    "comments": [],
                }
            ],
        )
        first = io.StringIO()
        second = io.StringIO()

        run_sweep(api, mode="apply", output=first, policy_digest=digest)
        call_count = len(api.calls)
        run_sweep(api, mode="apply", output=second, policy_digest=digest)

        assert [label["name"] for label in api.open_issues[0]["labels"]] == ["epic"]
        assert not any(
            name == "remove_label" and payload == (1, "epic") for name, payload in api.calls
        )
        assert f"- policy digest: `{digest}`" in api.open_issues[0]["comments"][0]["body"]
        assert len(api.calls) == call_count + 2
        assert second.getvalue().splitlines() == [
            "shape-check sweep — apply (1 open issues)",
            "1 unchanged",
            "0 changes",
        ]


class TestPolicyDigest:
    def test_manifest_entries_exist_under_shape_check_package(self):
        package_dir = Path(__file__).resolve().parents[1] / "src/theforge/shape_check"

        assert all((package_dir / name).is_file() for name in POLICY_SOURCE_FILES)

    def test_ignores_unlisted_files(self, tmp_path):
        for name in POLICY_SOURCE_FILES:
            (tmp_path / name).write_text(f"{name}\n", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("first\n", encoding="utf-8")

        baseline = compute_policy_digest(tmp_path)
        (tmp_path / "notes.txt").write_text("second\n", encoding="utf-8")

        assert compute_policy_digest(tmp_path) == baseline

    def test_changes_when_manifest_file_changes(self, tmp_path):
        for name in POLICY_SOURCE_FILES:
            (tmp_path / name).write_text(f"{name}\n", encoding="utf-8")

        baseline = compute_policy_digest(tmp_path)
        (tmp_path / "heuristics.py").write_text("changed\n", encoding="utf-8")

        assert compute_policy_digest(tmp_path) != baseline


class TestHttpGitHubAPI:
    def test_list_open_issues_paginates_and_excludes_pull_requests(self):
        api = _PagedHttpGitHubAPI(
            responses={
                "/issues?state=open&per_page=100": (
                    [
                        {"number": 2, "labels": []},
                        {"number": 99, "labels": [], "pull_request": {"url": "pr"}},
                    ],
                    {
                        "Link": (
                            "<https://api.github.com/repos/example/repo/issues?"
                            'state=open&per_page=100&page=2>; rel="next"'
                        )
                    },
                ),
                "/issues?state=open&per_page=100&page=2": ([{"number": 3, "labels": []}], {}),
            }
        )

        assert [issue["number"] for issue in api.list_open_issues()] == [2, 3]

    def test_list_open_issues_follows_repositories_pagination_links(self):
        api = _PagedHttpGitHubAPI(
            responses={
                "/issues?state=open&per_page=100": (
                    [{"number": 2, "labels": []}],
                    {
                        "Link": (
                            "<https://api.github.com/repositories/123/issues?"
                            'state=open&per_page=100&page=2>; rel="next"'
                        )
                    },
                ),
                "https://api.github.com/repositories/123/issues?state=open&per_page=100&page=2": (
                    [{"number": 3, "labels": []}],
                    {},
                ),
            }
        )

        assert [issue["number"] for issue in api.list_open_issues()] == [2, 3]

    def test_list_open_issues_stops_on_plaintext_pagination_link(self):
        api = _RecordingPagedHttpGitHubAPI(
            responses={
                "/issues?state=open&per_page=100": (
                    [{"number": 2, "labels": []}],
                    {
                        "Link": (
                            "<http://api.github.com/repositories/123/issues?"
                            'state=open&per_page=100&page=2>; rel="next"'
                        )
                    },
                )
            }
        )

        assert [issue["number"] for issue in api.list_open_issues()] == [2]
        assert api.requested_paths == ["/issues?state=open&per_page=100"]

    def test_list_open_issues_stops_on_cross_host_pagination_link(self):
        api = _RecordingPagedHttpGitHubAPI(
            responses={
                "/issues?state=open&per_page=100": (
                    [{"number": 2, "labels": []}],
                    {
                        "Link": (
                            "<https://example.invalid/repositories/123/issues?"
                            'state=open&per_page=100&page=2>; rel="next"'
                        )
                    },
                )
            }
        )

        assert [issue["number"] for issue in api.list_open_issues()] == [2]
        assert api.requested_paths == ["/issues?state=open&per_page=100"]

    def test_list_comments_paginates_to_existing_marker_on_second_page(self):
        api = _PagedHttpGitHubAPI(
            responses={
                "/issues/42/comments?per_page=100": (
                    [{"id": 1, "user": {"login": "someone-else"}, "body": "first"}],
                    {
                        "Link": (
                            "<https://api.github.com/repos/example/repo/issues/42/comments?"
                            'per_page=100&page=2>; rel="next"'
                        )
                    },
                ),
                "/issues/42/comments?per_page=100&page=2": (
                    [
                        {
                            "id": 2,
                            "user": {"login": BOT_LOGIN},
                            "body": f"{COMMENT_MARKER}\ncurrent",
                        }
                    ],
                    {},
                ),
            }
        )

        comments = api.list_comments(42)

        assert find_bot_comment(comments, BOT_LOGIN)["id"] == 2


class TestMain:
    def test_preview_403_exits_cleanly(self, monkeypatch, capsys):
        class ForbiddenPreviewAPI:
            def __init__(self, *args, **kwargs):
                del args, kwargs

            def list_open_issues(self):
                raise urlerror.HTTPError(
                    "https://api.github.com/repos/example/repo/issues",
                    403,
                    "forbidden",
                    {},
                    None,
                )

        monkeypatch.setattr("theforge.shape_check.action.HttpGitHubAPI", ForbiddenPreviewAPI)
        monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "token")
        monkeypatch.setenv("SHAPE_CHECK_MODE", "preview")

        assert main([]) == 0
        assert "preview unavailable" in capsys.readouterr().out


class _PagedHttpGitHubAPI(HttpGitHubAPI):
    def __init__(self, *, responses):
        super().__init__(repo="example/repo", token="token", bot_login=BOT_LOGIN)
        self._responses = responses

    def _request_with_headers(self, method, path, body=None):
        del method, body
        payload, headers = self._responses[path]
        return payload, headers


class _RecordingPagedHttpGitHubAPI(_PagedHttpGitHubAPI):
    def __init__(self, *, responses):
        super().__init__(responses=responses)
        self.requested_paths: list[str] = []

    def _request_with_headers(self, method, path, body=None):
        self.requested_paths.append(path)
        return super()._request_with_headers(method, path, body)
