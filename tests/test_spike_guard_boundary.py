"""The ``gh`` boundary and the workflow entrypoint.

Mirrors :mod:`theforge.spike_guard.guard` — which facts get fetched, and what
happens when they cannot be — and :mod:`theforge.spike_guard.__main__`, the
``python -m theforge.spike_guard <issue>`` surface the repository workflows
call. The rule these facts feed is covered in
:mod:`tests.test_spike_guard_outcome`. Story #2600.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.spike_outcome_fixtures import CONDITIONAL, DO_NOT_PROCEED, TRIGGER_SECTION
from theforge.spike_guard import check_spike_closure
from theforge.spike_guard.__main__ import REFUSED_EXIT_CODE, main


class _FakeGh:
    """A ``gh`` boundary answering ``issue view`` from a fixture map."""

    def __init__(self, issues: dict[int, dict], fail: bool = False):
        self.issues = issues
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "gh: not authenticated")
        number = int(cmd[3])
        data = self.issues.get(number, {})
        payload = {
            "state": data.get("state", "OPEN"),
            "labels": [{"name": name} for name in data.get("labels", [])],
            "body": data.get("body", ""),
            "comments": [{"body": body} for body in data.get("comments", [])],
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")


@pytest.fixture
def gh(monkeypatch):
    def install(issues, fail=False):
        fake = _FakeGh(issues, fail=fail)
        monkeypatch.setattr("theforge.spike_guard.guard.subprocess.run", fake)
        return fake

    return install


class TestGhBoundary:
    def test_a_known_non_spike_type_never_touches_gh(self, gh, tmp_path):
        fake = gh({})
        decision = check_spike_closure(7, tmp_path, known_type="bug")
        assert decision.allowed and fake.calls == []

    def test_an_unreadable_issue_refuses_the_close(self, gh, tmp_path):
        gh({}, fail=True)
        decision = check_spike_closure(7, tmp_path)
        assert not decision.allowed and "could not read issue #7" in decision.reason

    def test_a_non_spike_closes(self, gh, tmp_path):
        gh({7: {"labels": ["bug"]}})
        assert check_spike_closure(7, tmp_path).allowed

    def test_an_outcome_in_a_comment_counts(self, gh, tmp_path):
        gh({2348: {"labels": ["spike"], "comments": [DO_NOT_PROCEED]}})
        assert check_spike_closure(2348, tmp_path).allowed

    def test_an_outcome_in_the_closing_comment_counts(self, gh, tmp_path):
        gh({2348: {"labels": ["spike"]}})
        decision = check_spike_closure(2348, tmp_path, closing_comment=DO_NOT_PROCEED)
        assert decision.allowed

    def test_the_follow_on_issue_is_fetched_and_checked(self, gh, tmp_path):
        gh(
            {
                2348: {"labels": ["spike"], "body": CONDITIONAL},
                2599: {"labels": ["enhancement"], "body": TRIGGER_SECTION},
            }
        )
        assert check_spike_closure(2348, tmp_path).allowed

    def test_a_spike_with_nothing_recorded_is_refused(self, gh, tmp_path):
        gh({2348: {"labels": ["spike"], "body": "A question."}})
        assert not check_spike_closure(2348, tmp_path).allowed


class TestEntrypoint:
    def test_exit_zero_when_the_close_is_allowed(self, gh, tmp_path):
        gh({7: {"labels": ["bug"]}})
        assert main(["7", "--project-root", str(tmp_path)]) == 0

    def test_refusal_exits_nonzero_with_the_reason_on_stderr(self, gh, tmp_path, capsys):
        gh({2348: {"labels": ["spike"]}})
        assert main(["2348", "--project-root", str(tmp_path)]) == REFUSED_EXIT_CODE
        assert "records no outcome" in capsys.readouterr().err
