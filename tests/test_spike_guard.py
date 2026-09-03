"""The spike closure rule: a spike closes on a recorded outcome or not at all.

Covers the pure rule (:mod:`theforge.spike_guard.outcome`), the ``gh`` boundary
that feeds it (:mod:`theforge.spike_guard.guard`), and the module entrypoint the
repository workflows call. Story #2600.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from theforge.spike_guard import (
    IssueFacts,
    SpikeOutcomeKind,
    check_spike_closure,
    evaluate_spike_closure,
    find_spike_outcome,
    missing_trigger_condition_fields,
)
from theforge.spike_guard.__main__ import REFUSED_EXIT_CODE, main

DO_NOT_PROCEED = (
    "<!-- forge-spike-outcome-v1\n"
    "outcome: do_not_proceed\n"
    "reason: the trust threshold is unreachable with the signal available\n"
    "-->"
)
FOLLOW_UP = "<!-- forge-spike-outcome-v1\noutcome: follow_up\nfollow-up: #2599\n-->"
CONDITIONAL = "<!-- forge-spike-outcome-v1\noutcome: conditional_follow_up\nfollow-up: #2599\n-->"
TRIGGER_SECTION = (
    "## Spike trigger condition\n\n"
    "- **What must be true:** the observer beats the naive baseline on real sprints.\n"
    "- **How to know:** the comparison hook reports its trust threshold met.\n"
)


def _spike(body: str = "", labels: tuple[str, ...] = ("spike",)) -> IssueFacts:
    return IssueFacts(number=2348, state="OPEN", labels=labels, body=body)


def _follow_up(body: str = "", labels: tuple[str, ...] = ("enhancement",), state="OPEN"):
    return IssueFacts(number=2599, state=state, labels=labels, body=body)


class TestParsing:
    def test_do_not_proceed_carries_its_reason(self):
        outcome = find_spike_outcome([DO_NOT_PROCEED])
        assert outcome is not None
        assert outcome.kind is SpikeOutcomeKind.DO_NOT_PROCEED
        assert "trust threshold" in outcome.reason

    def test_follow_up_carries_the_issue_number(self):
        outcome = find_spike_outcome([FOLLOW_UP])
        assert outcome is not None and outcome.follow_up == 2599

    def test_no_marker_is_no_outcome(self):
        assert find_spike_outcome(["Just some prose about the spike."]) is None

    def test_do_not_proceed_without_a_reason_is_malformed(self):
        outcome = find_spike_outcome(["<!-- forge-spike-outcome-v1\noutcome: do_not_proceed\n-->"])
        assert outcome is not None and "reason" in outcome.malformed

    def test_unknown_outcome_names_the_legal_exits(self):
        outcome = find_spike_outcome(["<!-- forge-spike-outcome-v1\noutcome: maybe\n-->"])
        assert outcome is not None
        assert "do_not_proceed" in outcome.malformed and "follow_up" in outcome.malformed

    def test_a_well_formed_marker_supersedes_an_earlier_typo(self):
        outcome = find_spike_outcome(
            ["<!-- forge-spike-outcome-v1\noutcome: maybe\n-->", DO_NOT_PROCEED]
        )
        assert outcome is not None and not outcome.malformed

    def test_hyphenated_and_spaced_keys_are_accepted(self):
        outcome = find_spike_outcome(
            ["<!-- forge-spike-outcome-v1\noutcome: follow-up\nfollow up: 2599\n-->"]
        )
        assert outcome is not None
        assert outcome.kind is SpikeOutcomeKind.FOLLOW_UP and outcome.follow_up == 2599


class TestTriggerCondition:
    def test_complete_section_is_satisfied(self):
        assert missing_trigger_condition_fields(TRIGGER_SECTION) == ()

    def test_missing_section_reports_every_field(self):
        assert missing_trigger_condition_fields("Adopt the observer.") == (
            "What must be true",
            "How to know",
        )

    def test_empty_field_value_does_not_satisfy_it(self):
        body = "## Spike trigger condition\n\n- **What must be true:**\n- **How to know:** CI.\n"
        assert missing_trigger_condition_fields(body) == ("What must be true",)

    def test_a_later_section_does_not_bleed_in(self):
        body = TRIGGER_SECTION.replace(
            "- **How to know:** the comparison hook reports its trust threshold met.\n", ""
        )
        body += "## Acceptance criteria\n\n- **How to know:** not here.\n"
        assert missing_trigger_condition_fields(body) == ("How to know",)


class TestEvaluation:
    def test_a_non_spike_is_unchanged(self):
        decision = evaluate_spike_closure(_spike(labels=("enhancement",)), texts=[""])
        assert decision.allowed

    def test_a_spike_without_an_outcome_cannot_close(self):
        decision = evaluate_spike_closure(_spike("A question."), texts=["A question."])
        assert not decision.allowed
        assert "records no outcome" in decision.reason
        assert "forge-spike-outcome-v1" in decision.reason, "the refusal must say how to fix it"

    def test_do_not_proceed_is_a_complete_outcome(self):
        decision = evaluate_spike_closure(_spike(DO_NOT_PROCEED), texts=[DO_NOT_PROCEED])
        assert decision.allowed
        assert "do-not-proceed" in decision.reason

    def test_follow_up_needs_an_open_issue(self):
        decision = evaluate_spike_closure(
            _spike(FOLLOW_UP), texts=[FOLLOW_UP], follow_up=_follow_up(state="CLOSED")
        )
        assert not decision.allowed and "CLOSED" in decision.reason

    def test_follow_up_needs_exactly_one_type_label(self):
        decision = evaluate_spike_closure(
            _spike(FOLLOW_UP), texts=[FOLLOW_UP], follow_up=_follow_up(labels=())
        )
        assert not decision.allowed and "type label" in decision.reason

    def test_follow_up_may_not_be_a_draft(self):
        decision = evaluate_spike_closure(
            _spike(FOLLOW_UP),
            texts=[FOLLOW_UP],
            follow_up=_follow_up(labels=("enhancement", "todo:draft")),
        )
        assert not decision.allowed and "todo:draft" in decision.reason

    def test_a_valid_follow_up_closes_the_spike(self):
        decision = evaluate_spike_closure(
            _spike(FOLLOW_UP), texts=[FOLLOW_UP], follow_up=_follow_up()
        )
        assert decision.allowed and "#2599" in decision.reason

    def test_conditional_requires_the_condition_on_the_follow_on(self):
        spike_body = f"It works when the threshold is met.\n\n{CONDITIONAL}"
        decision = evaluate_spike_closure(
            _spike(spike_body), texts=[spike_body], follow_up=_follow_up("Adopt the observer.")
        )
        assert not decision.allowed
        assert "Spike trigger condition" in decision.reason

    def test_conditional_closes_when_the_follow_on_carries_the_condition(self):
        decision = evaluate_spike_closure(
            _spike(CONDITIONAL), texts=[CONDITIONAL], follow_up=_follow_up(TRIGGER_SECTION)
        )
        assert decision.allowed

    def test_the_condition_on_the_spike_itself_does_not_count(self):
        """AC: the condition is carried by the follow-on artifact, not by closed-spike prose."""
        spike_body = f"{TRIGGER_SECTION}\n{CONDITIONAL}"
        decision = evaluate_spike_closure(
            _spike(spike_body), texts=[spike_body], follow_up=_follow_up("Adopt it.")
        )
        assert not decision.allowed


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
    def test_exit_zero_when_the_close_is_allowed(self, gh, tmp_path, capsys):
        gh({7: {"labels": ["bug"]}})
        assert main(["7", "--project-root", str(tmp_path)]) == 0

    def test_refusal_exits_nonzero_with_the_reason_on_stderr(self, gh, tmp_path, capsys):
        gh({2348: {"labels": ["spike"]}})
        assert main(["2348", "--project-root", str(tmp_path)]) == REFUSED_EXIT_CODE
        assert "records no outcome" in capsys.readouterr().err
