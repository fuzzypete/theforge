"""Tests for the review-retry corrective prompt.

The retry prompt must anchor the reviewer's prior content and foreclose the
field-dropping interpretation that produces the APPROVE-empty-AC ↔
REQUEST_CHANGES-no-P1 oscillation trap (issue #1546).
"""

from theforge.coordinator.review_pool import (
    _CORRECTIVE_YAML_STRUCTURE,
    _build_review_retry_prompt,
)

_PRIOR_OUTPUT = """\
verdict: APPROVE
summary: "Looks good"
ac_verification:
  - criterion: "Handles the retry's failure mode"
    status: VERIFIED
    evidence: "src/x.py:10"
"""


class TestBuildReviewRetryPrompt:
    def test_includes_prior_output_verbatim(self):
        prompt = _build_review_retry_prompt("YAML syntax error", _PRIOR_OUTPUT)
        assert _PRIOR_OUTPUT in prompt

    def test_includes_error_description(self):
        prompt = _build_review_retry_prompt("apostrophe in single-quote", _PRIOR_OUTPUT)
        assert "apostrophe in single-quote" in prompt

    def test_names_dropping_a_field_as_wrong(self):
        prompt = _build_review_retry_prompt("err", _PRIOR_OUTPUT).lower()
        assert "wrong" in prompt
        assert "drop" in prompt

    def test_forbids_verdict_flip(self):
        prompt = _build_review_retry_prompt("err", _PRIOR_OUTPUT)
        assert "APPROVE" in prompt and "REQUEST_CHANGES" in prompt
        # The prompt must instruct keeping the same verdict.
        assert "SAME verdict" in prompt

    def test_anchors_content_as_correct(self):
        prompt = _build_review_retry_prompt("err", _PRIOR_OUTPUT)
        assert "CONTENT" in prompt and "ENCODING" in prompt

    def test_points_to_escape_valve_instead_of_dropping(self):
        prompt = _build_review_retry_prompt("err", _PRIOR_OUTPUT)
        assert "criteria_enumerable" in prompt

    def test_still_says_do_not_re_review(self):
        prompt = _build_review_retry_prompt("err", _PRIOR_OUTPUT)
        assert "Do NOT re-review" in prompt

    def test_corrective_structure_lists_ac_verification(self):
        # The required-structure block must not omit ac_verification — omitting
        # it is itself an inducement to drop the field.
        assert "ac_verification:" in _CORRECTIVE_YAML_STRUCTURE
        assert "criteria_enumerable" in _CORRECTIVE_YAML_STRUCTURE
