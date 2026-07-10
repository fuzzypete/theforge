"""Tests for the intake remediation orchestrator."""

from __future__ import annotations

from theforge.intake import (
    AgentRewriteResult,
    IntakeOutcomeKind,
    run_intake_remediation,
)
from theforge.task import TaskStory

# Issue body that fails shape gate (missing AC, missing example).
_FAILING_BODY = "## What\n\nDo a thing.\n\n## Why\n\nReason.\n"

# A valid body that passes shape and grooming.
_PASSING_BODY = (
    "## What\n\nUsers can export.\n\n"
    "## Why\n\nLegal needs it.\n\n"
    "## Acceptance criteria\n\n"
    "- The export download produces a CSV with user records\n"
    "- The download is available within 60 seconds for accounts with <1k users\n\n"
    "## Example\n\n"
    "```csv\nuser_id,email\n1,a@example.com\n```\n"
)


def _make_task(slug: str = "task-1", issue: int | None = 7) -> TaskStory:
    return TaskStory(name="t", slug=slug, github_issue=issue)


def _make_fetch(detail: dict) -> object:
    def fetch(_n, _root):
        return detail

    return fetch


def _record_calls():
    """Return (callable, calls list) tuple recording invocations."""
    calls: list[tuple] = []

    def f(*args):
        calls.append(args)
        return True

    return f, calls


def test_no_op_when_grooming_and_auto_fix_disabled():
    task = _make_task()
    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=False,
        auto_fix_enabled=False,
        fetch_detail=_make_fetch({"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}),
    )
    assert outcomes[task.slug].kind is IntakeOutcomeKind.PASSED
    # No findings recorded — full backward-compat path.
    assert outcomes[task.slug].findings == ()


def test_passing_issue_with_grooming_enabled():
    task = _make_task()
    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=False,
        fetch_detail=_make_fetch({"title": "T", "body": _PASSING_BODY, "labels": ["enhancement"]}),
    )
    assert outcomes[task.slug].kind is IntakeOutcomeKind.PASSED


def test_failing_issue_dropped_when_auto_fix_off():
    task = _make_task()
    post_comment, post_calls = _record_calls()
    edit_body, edit_calls = _record_calls()
    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=False,
        fetch_detail=_make_fetch({"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}),
        post_comment=post_comment,
        edit_body=edit_body,
    )
    out = outcomes[task.slug]
    assert out.kind is IntakeOutcomeKind.DROPPED_SHAPE
    assert len(out.findings) >= 1
    assert post_calls == []
    assert edit_calls == []


def test_auto_fix_comment_mode_posts_replacement_and_drops():
    task = _make_task()
    post_comment, post_calls = _record_calls()
    edit_body, edit_calls = _record_calls()

    def agent(body, findings, comments=()):
        return AgentRewriteResult(replacement=_PASSING_BODY, detail="agent produced replacement")

    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        auto_fix_mode="comment",
        agent_caller=agent,
        fetch_detail=_make_fetch({"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}),
        post_comment=post_comment,
        edit_body=edit_body,
    )
    out = outcomes[task.slug]
    # Comment mode: story is dropped from current sprint even on agent success.
    assert out.kind is IntakeOutcomeKind.DROPPED_SHAPE
    assert out.proposed_replacement == _PASSING_BODY
    assert len(post_calls) == 1
    assert edit_calls == []


def test_auto_fix_edit_mode_updates_body_and_remediates():
    task = _make_task()
    post_comment, post_calls = _record_calls()
    edit_body, edit_calls = _record_calls()

    def agent(body, findings, comments=()):
        return AgentRewriteResult(replacement=_PASSING_BODY, detail="agent produced replacement")

    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        agent_caller=agent,
        fetch_detail=_make_fetch({"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}),
        post_comment=post_comment,
        edit_body=edit_body,
    )
    out = outcomes[task.slug]
    assert out.kind is IntakeOutcomeKind.REMEDIATED
    assert out.proposed_replacement == _PASSING_BODY
    assert out.audit["remediation_source"] == "agent"
    assert len(edit_calls) == 1
    assert post_calls == []


def test_auto_fix_edit_mode_keeps_failing_body_off_issue():
    task = _make_task()
    post_comment, _ = _record_calls()
    edit_body, edit_calls = _record_calls()

    def agent(body, findings, comments=()):
        return AgentRewriteResult(replacement=_FAILING_BODY, detail="agent produced replacement")

    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        agent_caller=agent,
        fetch_detail=_make_fetch({"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}),
        post_comment=post_comment,
        edit_body=edit_body,
    )
    out = outcomes[task.slug]
    assert out.kind is IntakeOutcomeKind.DROPPED_AFTER_FIX
    assert edit_calls == []  # never wrote a still-failing body


def test_agent_called_at_most_once_per_story():
    task = _make_task()
    calls: list[int] = []

    def agent(body, findings, comments=()):
        calls.append(1)
        return AgentRewriteResult(replacement=_PASSING_BODY, detail="agent produced replacement")

    run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        agent_caller=agent,
        fetch_detail=_make_fetch({"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}),
        post_comment=lambda *_: True,
        edit_body=lambda *_: True,
    )
    assert sum(calls) == 1


def test_file_based_story_short_circuits():
    task = _make_task(issue=None)
    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        fetch_detail=lambda *_: None,
    )
    assert outcomes[task.slug].kind is IntakeOutcomeKind.PASSED


def test_failed_fetch_falls_open():
    task = _make_task()
    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        fetch_detail=lambda *_: None,
    )
    assert outcomes[task.slug].kind is IntakeOutcomeKind.PASSED


def test_missing_agent_caller_fails_explicitly():
    task = _make_task()
    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        agent_caller=None,
        missing_agent_detail=(
            "auto-fix enabled but no intake agent caller is available: auth missing"
        ),
        fetch_detail=_make_fetch({"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}),
    )
    out = outcomes[task.slug]
    assert out.kind is IntakeOutcomeKind.DROPPED_SHAPE
    assert out.detail == "auto-fix enabled but no intake agent caller is available: auth missing"
    assert out.audit["remediation_source"] == "missing_agent"
    assert out.audit["agent"]["attempted"] is False


def test_mechanical_only_remediation_is_distinguished_from_agent_flow():
    task = _make_task()
    post_comment, post_calls = _record_calls()
    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=False,
        auto_fix_enabled=True,
        auto_fix_mode="comment",
        fetch_detail=_make_fetch({"title": "T", "body": _PASSING_BODY, "labels": []}),
        post_comment=post_comment,
    )
    out = outcomes[task.slug]
    assert out.kind is IntakeOutcomeKind.DROPPED_SHAPE
    assert out.audit["remediation_source"] == "mechanical"
    assert out.audit["agent"]["attempted"] is False
    assert len(out.audit["mechanical_findings"]) == 1
    assert post_calls


def test_edit_mode_rerun_failure_persists_candidate_as_comment():
    """When the edit-mode rerun gate still fails, the candidate body must not
    be silently discarded — it must be posted as an issue comment with the
    remaining blocking findings so the operator can act on it.
    """
    task = _make_task()
    post_comment, post_calls = _record_calls()
    edit_body, edit_calls = _record_calls()

    candidate = "## What\n\nstill not enough\n"

    def agent(body, findings, comments=()):
        return AgentRewriteResult(replacement=candidate, detail="agent produced replacement")

    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        agent_caller=agent,
        fetch_detail=_make_fetch({"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}),
        post_comment=post_comment,
        edit_body=edit_body,
    )
    out = outcomes[task.slug]
    assert out.kind is IntakeOutcomeKind.DROPPED_AFTER_FIX
    assert out.proposed_replacement == candidate
    assert edit_calls == []  # still must not write a failing body
    # Candidate must be posted as a comment so the operator can find it.
    assert len(post_calls) == 1
    posted_body = post_calls[0][1]
    assert candidate in posted_body
    assert "Remaining blocking findings" in posted_body
    assert out.audit["comment_posted"] is True
    assert "candidate posted" in out.detail


def test_edit_mode_rerun_failure_falls_back_to_artifact_when_post_fails(tmp_path):
    """If post_comment returns False on edit-mode rerun-gate failure, the
    candidate body and remaining findings must still be persisted somewhere
    operator-visible — written to a durable artifact under
    ``.forge/intake/candidates/`` and surfaced in the outcome detail/audit.
    """
    task = _make_task()
    edit_body, edit_calls = _record_calls()

    def failing_post(_n, _body, _root):
        return False

    candidate = "## What\n\nstill not enough\n"

    def agent(body, findings, comments=()):
        return AgentRewriteResult(replacement=candidate, detail="agent produced replacement")

    outcomes = run_intake_remediation(
        [task],
        tmp_path,
        grooming_enabled=True,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        agent_caller=agent,
        fetch_detail=_make_fetch({"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}),
        post_comment=failing_post,
        edit_body=edit_body,
    )
    out = outcomes[task.slug]
    assert out.kind is IntakeOutcomeKind.DROPPED_AFTER_FIX
    assert out.proposed_replacement == candidate
    assert edit_calls == []
    assert out.audit["comment_posted"] is False
    artifact_path = out.audit["candidate_artifact_path"]
    assert artifact_path is not None
    artifact = __import__("pathlib").Path(artifact_path)
    assert artifact.exists()
    contents = artifact.read_text(encoding="utf-8")
    assert candidate in contents
    assert "Remaining blocking findings" in contents
    # Detail must surface the artifact path so the operator can find it
    # without grepping logs.
    assert artifact_path in out.detail


def test_agent_receives_issue_comments_as_input():
    """The remediator must receive the issue's comment thread as agent input
    so it can synthesize required structural sections (e.g. ``## Diagnosis``)
    that exist in raw form in a comment but are missing from the body.
    """
    task = _make_task()
    captured: dict = {}

    def agent(body, findings, comments=()):
        captured["body"] = body
        captured["comments"] = list(comments)
        return AgentRewriteResult(replacement=_PASSING_BODY, detail="agent produced replacement")

    diag_comment = (
        "## Diagnosis\n\nObserved symptom: foo. Confirmed cause: bar. "
        "Fix-success criterion: baz.\n"
    )
    run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        agent_caller=agent,
        fetch_detail=_make_fetch(
            {
                "title": "T",
                "body": _FAILING_BODY,
                "labels": ["enhancement"],
                "comments": [diag_comment, "another comment"],
            }
        ),
        post_comment=lambda *_: True,
        edit_body=lambda *_: True,
    )
    assert captured["comments"] == [diag_comment, "another comment"]


def test_agent_no_output_is_distinguished_in_audit():
    task = _make_task()

    def agent(body, findings, comments=()):
        return AgentRewriteResult(replacement=None, detail="model refused rewrite", attempted=True)

    outcomes = run_intake_remediation(
        [task],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        agent_caller=agent,
        fetch_detail=_make_fetch({"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}),
        post_comment=lambda *_: True,
        edit_body=lambda *_: True,
    )
    out = outcomes[task.slug]
    assert out.kind is IntakeOutcomeKind.DROPPED_AFTER_FIX
    assert out.detail == "model refused rewrite"
    assert out.audit["remediation_source"] == "agent_no_output"
    assert out.audit["agent"]["attempted"] is True


def test_force_bypass_skips_fetch_and_agent_for_every_task():
    task_a = _make_task(slug="a", issue=11)
    task_b = _make_task(slug="b", issue=12)
    task_c = _make_task(slug="c", issue=None)

    fetch_calls: list = []

    def fetch(n, root):
        fetch_calls.append((n, root))
        return {"title": "T", "body": _FAILING_BODY, "labels": ["enhancement"]}

    def agent(*_a, **_kw):
        raise AssertionError("agent must not run under --force")

    outcomes = run_intake_remediation(
        [task_a, task_b, task_c],
        None,
        grooming_enabled=True,
        auto_fix_enabled=True,
        auto_fix_mode="edit",
        agent_caller=agent,
        fetch_detail=fetch,
        post_comment=lambda *_: True,
        edit_body=lambda *_: True,
        force=True,
    )

    assert fetch_calls == []
    for slug in ("a", "b", "c"):
        out = outcomes[slug]
        assert out.kind is IntakeOutcomeKind.PASSED
        assert out.audit.get("force_bypass") is True
        assert out.detail == "intake remediation bypassed by --force"
