"""Unit tests for the ``<forge_spec_gap>`` parser (#2122).

The parser is the integrity boundary for the dev→operator backchannel: a gap it
mis-reads becomes a question the operator answers about the wrong criterion.
"""

from __future__ import annotations

import pytest

from theforge.task.spec_gap import (
    SpecGapParseError,
    dedupe_resolutions,
    extract_spec_gap,
)

WELL_FORMED = """\
I cannot proceed without a decision.

<forge_spec_gap>
criterion: "Ending a workout marks the linked Polar session ended"
undefined_case: "no correlated Polar session exists for the workout"
assumption: "leave the workout unmarked and attach nothing"
options_considered:
  - "leave unmarked"
  - "attach nearest by time"
  - "fail the end"
</forge_spec_gap>
"""


class TestExtraction:
    def test_absence_is_not_an_error(self):
        assert extract_spec_gap("no block here") is None
        assert extract_spec_gap("") is None

    def test_well_formed_block_parses(self):
        signal = extract_spec_gap(WELL_FORMED)
        assert signal is not None
        assert signal.criterion.startswith("Ending a workout")
        assert signal.undefined_case == "no correlated Polar session exists for the workout"
        assert signal.assumption == "leave the workout unmarked and attach nothing"
        assert signal.options_considered == (
            "leave unmarked",
            "attach nearest by time",
            "fail the end",
        )

    def test_options_considered_is_optional(self):
        signal = extract_spec_gap(
            "<forge_spec_gap>\ncriterion: c\nundefined_case: u\nassumption: a\n</forge_spec_gap>"
        )
        assert signal is not None
        assert signal.options_considered == ()

    def test_block_quoted_inside_a_fence_is_not_a_raise(self):
        """The prompt shows the block shape; an agent echoing it has not asked."""
        text = "Here is the shape:\n```\n" + WELL_FORMED + "\n```\nCarrying on.\n"
        assert extract_spec_gap(text) is None

    def test_to_dict_round_trips_the_payload(self):
        signal = extract_spec_gap(WELL_FORMED)
        assert signal is not None
        payload = signal.to_dict()
        assert payload["criterion"] == signal.criterion
        assert payload["options_considered"] == list(signal.options_considered)


class TestRefusals:
    @pytest.mark.parametrize(
        "text,fragment",
        [
            (WELL_FORMED + WELL_FORMED, "expected exactly one block"),
            ("</forge_spec_gap>", "without opening"),
            ("<forge_spec_gap>\ncriterion: c\n", "without closing"),
            ("<forge_spec_gap></forge_spec_gap>", "is empty"),
            ("<forge_spec_gap>\n- a\n- b\n</forge_spec_gap>", "must be a YAML mapping"),
            (
                "<forge_spec_gap>\ncriterion: c\nassumption: a\n</forge_spec_gap>",
                "missing required keys",
            ),
            (
                "<forge_spec_gap>\ncriterion: c\nundefined_case: u\n"
                "assumption: a\noptions_considered: nope\n</forge_spec_gap>",
                "must be a list",
            ),
            (
                "<forge_spec_gap>\ncriterion: '  '\nundefined_case: u\n"
                "assumption: a\n</forge_spec_gap>",
                "must not be empty",
            ),
            (
                "<forge_spec_gap>\ncriterion: [1,\nundefined_case: u\n</forge_spec_gap>",
                "YAML parse error",
            ),
        ],
    )
    def test_malformed_blocks_are_refused_not_salvaged(self, text, fragment):
        with pytest.raises(SpecGapParseError) as exc:
            extract_spec_gap(text)
        assert fragment in str(exc.value)

    def test_assumption_is_required(self):
        """The no-answer and exhausted-allowance paths both proceed under it."""
        with pytest.raises(SpecGapParseError) as exc:
            extract_spec_gap(
                "<forge_spec_gap>\ncriterion: c\nundefined_case: u\n</forge_spec_gap>"
            )
        assert "assumption" in str(exc.value)


class TestDedupeResolutions:
    def test_newest_answer_per_gap_wins_and_order_is_stable(self):
        merged = dedupe_resolutions(
            [
                {"criterion": "A", "undefined_case": "x", "answer": "old"},
                {"criterion": "B", "undefined_case": "y", "answer": "b"},
                {"criterion": "A", "undefined_case": "x", "answer": "new"},
            ]
        )
        assert [entry["criterion"] for entry in merged] == ["A", "B"]
        assert merged[0]["answer"] == "new"

    def test_unreadable_entries_are_dropped(self):
        assert dedupe_resolutions(["nope", None, {"criterion": "", "undefined_case": ""}]) == []
        assert dedupe_resolutions("not a list") == []
