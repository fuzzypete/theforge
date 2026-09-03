"""The rule itself: :mod:`theforge.spike_guard.outcome`.

A spike closes on a recorded outcome or not at all. This mirror covers the pure
half — marker parsing, what each outcome kind must carry, what makes a follow-on
a pipeline work item, and where a conditional answer's trigger condition must
live. No ``gh``, no subprocess; :mod:`tests.test_spike_guard_boundary` covers
the layer that fetches these facts. Story #2600.
"""

from __future__ import annotations

from tests.spike_outcome_fixtures import (
    CONDITIONAL,
    DO_NOT_PROCEED,
    FOLLOW_UP,
    TRIGGER_SECTION,
    follow_up,
    spike,
)
from theforge.spike_guard import (
    SpikeOutcomeKind,
    evaluate_spike_closure,
    find_spike_outcome,
    missing_trigger_condition_fields,
)


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
        decision = evaluate_spike_closure(spike(labels=("enhancement",)), texts=[""])
        assert decision.allowed

    def test_a_spike_without_an_outcome_cannot_close(self):
        decision = evaluate_spike_closure(spike("A question."), texts=["A question."])
        assert not decision.allowed
        assert "records no outcome" in decision.reason
        assert "forge-spike-outcome-v1" in decision.reason, "the refusal must say how to fix it"

    def test_do_not_proceed_is_a_complete_outcome(self):
        decision = evaluate_spike_closure(spike(DO_NOT_PROCEED), texts=[DO_NOT_PROCEED])
        assert decision.allowed
        assert "do-not-proceed" in decision.reason

    def test_follow_up_needs_an_open_issue(self):
        decision = evaluate_spike_closure(
            spike(FOLLOW_UP), texts=[FOLLOW_UP], follow_up=follow_up(state="CLOSED")
        )
        assert not decision.allowed and "CLOSED" in decision.reason

    def test_follow_up_needs_exactly_one_type_label(self):
        decision = evaluate_spike_closure(
            spike(FOLLOW_UP), texts=[FOLLOW_UP], follow_up=follow_up(labels=())
        )
        assert not decision.allowed and "type label" in decision.reason

    def test_follow_up_may_not_be_a_draft(self):
        decision = evaluate_spike_closure(
            spike(FOLLOW_UP),
            texts=[FOLLOW_UP],
            follow_up=follow_up(labels=("enhancement", "todo:draft")),
        )
        assert not decision.allowed and "todo:draft" in decision.reason

    def test_a_valid_follow_up_closes_the_spike(self):
        decision = evaluate_spike_closure(
            spike(FOLLOW_UP), texts=[FOLLOW_UP], follow_up=follow_up()
        )
        assert decision.allowed and "#2599" in decision.reason

    def test_conditional_requires_the_condition_on_the_follow_on(self):
        spike_body = f"It works when the threshold is met.\n\n{CONDITIONAL}"
        decision = evaluate_spike_closure(
            spike(spike_body), texts=[spike_body], follow_up=follow_up("Adopt the observer.")
        )
        assert not decision.allowed
        assert "Spike trigger condition" in decision.reason

    def test_conditional_closes_when_the_follow_on_carries_the_condition(self):
        decision = evaluate_spike_closure(
            spike(CONDITIONAL), texts=[CONDITIONAL], follow_up=follow_up(TRIGGER_SECTION)
        )
        assert decision.allowed

    def test_the_condition_on_the_spike_itself_does_not_count(self):
        """AC: the condition is carried by the follow-on artifact, not by closed-spike prose."""
        spike_body = f"{TRIGGER_SECTION}\n{CONDITIONAL}"
        decision = evaluate_spike_closure(
            spike(spike_body), texts=[spike_body], follow_up=follow_up("Adopt it.")
        )
        assert not decision.allowed
