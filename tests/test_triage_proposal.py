"""Tests for the triage taxonomy, evidence packet, and proposal schema boundary.

Covers the pure-data module (``theforge.triage_proposal``): the fixed
disposition taxonomy with its required payloads, the grounding rule that a
proposal may only cite packet entries *and must quote them verbatim*, and the
rendering an operator reads. The agent invocation, the retry, and the audit
persistence are exercised in ``test_triage_proposal_flow.py``.
"""

from __future__ import annotations

from theforge.triage_proposal import (
    DISPOSITION_FIX_NOW,
    DISPOSITION_NEEDS_VERIFICATION,
    DISPOSITION_TAXONOMY,
    HYGIENE_POOL,
    MIN_QUOTE_WORDS,
    PUNT_REASON_CODES,
    PUNT_REVIEW_CHALLENGE,
    PUNT_REVIEW_CONCUR,
    FindingPacket,
    FindingProposalResult,
    PacketEvidence,
    ProposalRunSummary,
    PuntReviewStage,
    challenged_punt_review,
    needs_verification_proposal,
    parse_triage_proposal,
    parse_triage_punt_review,
    render_result,
    render_run_summary,
)

# The one evidence entry every packet below carries, and the words a citation
# must be drawn from.
_SUMMARY = "cited symbol absent from current tree"
_QUOTE = "cited symbol absent"


def _packet(
    *,
    evidence: tuple[PacketEvidence, ...] | None = None,
    current_milestone: str | None = "v0.12.0",
    named_milestones: tuple[str, ...] = ("v0.13.0",),
) -> FindingPacket:
    if evidence is None:
        evidence = (
            PacketEvidence(
                evidence_id="symbol-absent",
                kind="staleness",
                summary=_SUMMARY,
                checkable=True,
            ),
        )
    return FindingPacket(
        finding_id="1312:audit-count",
        issue_ref="#1312",
        finding_body="audit count is off by one",
        evidence=evidence,
        current_milestone=current_milestone,
        named_milestones=named_milestones,
    )


def _cite(quote: str = _QUOTE, ref: str = "symbol-absent") -> str:
    return f"evidence:\n  - ref: {ref}\n    quote: {quote}\n"


def _block(body: str) -> str:
    return f"prose before\n<triage_proposal>\n{body}\n</triage_proposal>\nprose after"


def _review_block(body: str) -> str:
    return f"prose before\n<triage_punt_review>\n{body}\n</triage_punt_review>\nprose after"


# ── Taxonomy shape ────────────────────────────────────────────────────────────


class TestTaxonomy:
    def test_taxonomy_is_the_four_spec_values(self) -> None:
        assert DISPOSITION_TAXONOMY == (
            "fix_now",
            "fix_later",
            "punt",
            "needs_verification",
        )

    def test_punt_reason_codes_include_the_spec_example(self) -> None:
        assert "verified-stale" in PUNT_REASON_CODES

    def test_fix_now_unavailable_without_a_current_milestone(self) -> None:
        packet = _packet(current_milestone=None)
        assert DISPOSITION_FIX_NOW not in packet.available_dispositions()
        assert DISPOSITION_NEEDS_VERIFICATION in packet.available_dispositions()

    def test_fix_later_targets_include_the_hygiene_pool(self) -> None:
        assert HYGIENE_POOL in _packet().fix_later_targets()

    def test_packet_hash_is_stable_and_evidence_sensitive(self) -> None:
        first = _packet()
        assert first.packet_hash() == _packet().packet_hash()
        other = _packet(
            evidence=(
                PacketEvidence(
                    evidence_id="symbol-absent",
                    kind="staleness",
                    summary="different summary",
                ),
            )
        )
        assert other.packet_hash() != first.packet_hash()

    def test_citable_text_is_the_entrys_own_words_only(self) -> None:
        entry = PacketEvidence(
            evidence_id="e", kind="staleness", summary="summary words", detail="detail words"
        )
        assert entry.citable_text() == "summary words detail words"


# ── Valid payloads ────────────────────────────────────────────────────────────


class TestValidPayloads:
    def test_fix_now_with_current_milestone_parses(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: fix_now\ntarget_milestone: v0.12.0\n{_cite()}"),
            _packet(),
        )
        assert proposal.ok, proposal.parse_errors
        assert proposal.disposition == DISPOSITION_FIX_NOW
        assert proposal.target_milestone == "v0.12.0"

    def test_fix_later_to_named_milestone_parses(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: fix_later\ntarget_milestone: v0.13.0\n{_cite()}"),
            _packet(),
        )
        assert proposal.ok, proposal.parse_errors
        assert proposal.target_milestone == "v0.13.0"

    def test_fix_later_to_hygiene_pool_parses(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: fix_later\ntarget_milestone: {HYGIENE_POOL}\n{_cite()}"),
            _packet(),
        )
        assert proposal.ok, proposal.parse_errors

    def test_every_punt_reason_code_is_accepted(self) -> None:
        for code in PUNT_REASON_CODES:
            proposal = parse_triage_proposal(
                _block(f"disposition: punt\npunt_reason_code: {code}\n{_cite()}"),
                _packet(),
            )
            assert proposal.ok, (code, proposal.parse_errors)
            assert proposal.punt_reason_code == code

    def test_needs_verification_without_payload_parses(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: needs_verification\n{_cite()}"),
            _packet(),
        )
        assert proposal.ok, proposal.parse_errors
        assert proposal.target_milestone is None
        assert proposal.punt_reason_code is None


# ── Rejections ────────────────────────────────────────────────────────────────


class TestSchemaRejections:
    def test_missing_block_is_rejected(self) -> None:
        proposal = parse_triage_proposal("I think we should punt this one.", _packet())
        assert not proposal.ok
        assert "no <triage_proposal> block" in proposal.parse_errors[0]

    def test_fenced_example_does_not_count_as_a_block(self) -> None:
        text = "```\n<triage_proposal>\ndisposition: punt\n</triage_proposal>\n```"
        proposal = parse_triage_proposal(text, _packet())
        assert not proposal.ok

    def test_unknown_disposition_is_rejected(self) -> None:
        proposal = parse_triage_proposal(_block(f"disposition: close_it\n{_cite()}"), _packet())
        assert not proposal.ok
        assert any("disposition must be one of" in e for e in proposal.parse_errors)

    def test_fix_now_without_target_is_rejected(self) -> None:
        proposal = parse_triage_proposal(_block(f"disposition: fix_now\n{_cite()}"), _packet())
        assert not proposal.ok
        assert any("fix_now requires target_milestone" in e for e in proposal.parse_errors)

    def test_fix_now_targeting_another_milestone_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: fix_now\ntarget_milestone: v0.13.0\n{_cite()}"),
            _packet(),
        )
        assert not proposal.ok
        assert any("current milestone" in e for e in proposal.parse_errors)

    def test_fix_now_is_rejected_when_no_current_milestone_is_known(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: fix_now\ntarget_milestone: v0.12.0\n{_cite()}"),
            _packet(current_milestone=None),
        )
        assert not proposal.ok
        assert any("not available for this finding" in e for e in proposal.parse_errors)

    def test_fix_later_to_unknown_milestone_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: fix_later\ntarget_milestone: v9.9.9\n{_cite()}"),
            _packet(),
        )
        assert not proposal.ok
        assert any("target_milestone must be one of" in e for e in proposal.parse_errors)

    def test_punt_without_reason_code_is_rejected(self) -> None:
        proposal = parse_triage_proposal(_block(f"disposition: punt\n{_cite()}"), _packet())
        assert not proposal.ok
        assert any("punt requires punt_reason_code" in e for e in proposal.parse_errors)

    def test_unknown_punt_reason_code_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: punt\npunt_reason_code: feels-old\n{_cite()}"),
            _packet(),
        )
        assert not proposal.ok
        assert any("punt_reason_code must be one of" in e for e in proposal.parse_errors)

    def test_needs_verification_with_a_target_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: needs_verification\ntarget_milestone: v0.12.0\n{_cite()}"),
            _packet(),
        )
        assert not proposal.ok
        assert any("must not carry target_milestone" in e for e in proposal.parse_errors)


class TestGrounding:
    """A citation must name a packet entry AND quote that entry's own words.

    The id-only version of this rule accepted a proposal that cited a real
    entry while asserting something the entry never said, which is the failure
    mode the grounding AC exists to prevent.
    """

    def test_unknown_evidence_ref_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: punt\n"
                "punt_reason_code: verified-stale\n"
                + _cite(quote="I recall this was fixed", ref="my-memory")
            ),
            _packet(),
        )
        assert not proposal.ok
        assert any("not in the packet" in e for e in proposal.parse_errors)

    def test_a_real_ref_does_not_license_an_unsupported_claim(self) -> None:
        """The P1 this rule was tightened for: valid id, invented evidence text."""
        proposal = parse_triage_proposal(
            _block(
                "disposition: punt\n"
                "punt_reason_code: verified-stale\n"
                + _cite(quote="the maintainer confirmed this was fixed last quarter")
            ),
            _packet(),
        )
        assert not proposal.ok
        assert any("not in that entry" in e for e in proposal.parse_errors)

    def test_a_paraphrase_of_the_entry_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: punt\n"
                "punt_reason_code: verified-stale\n"
                + _cite(quote="the symbol is no longer present in the tree")
            ),
            _packet(),
        )
        assert not proposal.ok
        assert any("do not paraphrase" in e for e in proposal.parse_errors)

    def test_a_quote_from_a_different_entry_is_rejected(self) -> None:
        packet = _packet(
            evidence=(
                PacketEvidence(evidence_id="a", kind="staleness", summary="alpha words here"),
                PacketEvidence(evidence_id="b", kind="churn", summary="beta words there"),
            )
        )
        proposal = parse_triage_proposal(
            _block(f"disposition: needs_verification\n{_cite(quote='beta words there', ref='a')}"),
            packet,
        )
        assert not proposal.ok
        assert any("not in that entry" in e for e in proposal.parse_errors)

    def test_a_quote_shorter_than_the_minimum_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: needs_verification\n{_cite(quote='cited symbol')}"),
            _packet(),
        )
        assert not proposal.ok
        assert any(f"quote at least {MIN_QUOTE_WORDS}" in e for e in proposal.parse_errors)

    def test_a_citation_without_a_quote_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block("disposition: needs_verification\nevidence:\n  - ref: symbol-absent\n"),
            _packet(),
        )
        assert not proposal.ok
        assert any("empty quote" in e for e in proposal.parse_errors)

    def test_a_bare_list_of_ids_is_rejected_with_a_shape_error(self) -> None:
        proposal = parse_triage_proposal(
            _block("disposition: needs_verification\nevidence: [symbol-absent]\n"),
            _packet(),
        )
        assert not proposal.ok
        assert any("must be a mapping with 'ref' and 'quote'" in e for e in proposal.parse_errors)

    def test_free_text_evidence_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block("disposition: needs_verification\nevidence: it looks stale to me\n"),
            _packet(),
        )
        assert not proposal.ok
        assert any("must be a list" in e for e in proposal.parse_errors)

    def test_no_evidence_at_all_is_rejected_when_the_packet_has_some(self) -> None:
        proposal = parse_triage_proposal(
            _block("disposition: punt\npunt_reason_code: verified-stale\n"),
            _packet(),
        )
        assert not proposal.ok
        assert any("must cite evidence" in e for e in proposal.parse_errors)

    def test_case_and_punctuation_differences_do_not_break_a_real_quote(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: needs_verification\n{_cite(quote='Cited SYMBOL absent.')}"),
            _packet(),
        )
        assert proposal.ok, proposal.parse_errors

    def test_a_multi_entry_proposal_verifies_every_citation(self) -> None:
        packet = _packet(
            evidence=(
                PacketEvidence(evidence_id="a", kind="staleness", summary="alpha words here"),
                PacketEvidence(evidence_id="b", kind="churn", summary="beta words there"),
            )
        )
        body = (
            "disposition: fix_later\n"
            "target_milestone: Hygiene\n"
            "evidence:\n"
            "  - ref: a\n    quote: alpha words here\n"
            "  - ref: b\n    quote: beta words invented\n"
        )
        proposal = parse_triage_proposal(_block(body), packet)
        assert not proposal.ok
        assert any("'b'" in e and "not in that entry" in e for e in proposal.parse_errors)

    def test_quotes_may_be_drawn_from_the_entry_detail(self) -> None:
        packet = _packet(
            evidence=(
                PacketEvidence(
                    evidence_id="symbol-absent",
                    kind="staleness",
                    summary=_SUMMARY,
                    detail="rg for audit_count at HEAD returns no match",
                ),
            )
        )
        proposal = parse_triage_proposal(
            _block(f"disposition: needs_verification\n{_cite(quote='at HEAD returns no match')}"),
            packet,
        )
        assert proposal.ok, proposal.parse_errors

    def test_a_grounded_proposal_exposes_refs_and_quoted_evidence(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: needs_verification\n{_cite()}"), _packet()
        )
        assert proposal.ok, proposal.parse_errors
        assert proposal.evidence_refs == ("symbol-absent",)
        assert proposal.evidence == _QUOTE
        assert proposal.citations[0].quote == _QUOTE

    def test_rationale_is_kept_but_never_becomes_evidence(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                f"disposition: needs_verification\n{_cite()}"
                "rationale: everyone knows this module was rewritten\n"
            ),
            _packet(),
        )
        assert proposal.ok, proposal.parse_errors
        assert proposal.rationale == "everyone knows this module was rewritten"
        assert proposal.rationale not in proposal.evidence


class TestNoCheckableEvidenceHelper:
    def test_packet_with_only_unchecked_entries_has_no_checkable_evidence(self) -> None:
        packet = _packet(
            evidence=(
                PacketEvidence(
                    evidence_id="restated",
                    kind="finding_body",
                    summary="the finding says so",
                    checkable=False,
                ),
            )
        )
        assert not packet.has_checkable_evidence()
        assert packet.evidence_ids() == ("restated",)

    def test_the_fallback_constructor_only_makes_needs_verification(self) -> None:
        proposal = needs_verification_proposal(_packet(), basis="nothing checkable here")
        assert proposal.disposition == DISPOSITION_NEEDS_VERIFICATION
        assert proposal.ok

    def test_the_fallback_carries_a_basis_and_never_fabricates_citations(self) -> None:
        proposal = needs_verification_proposal(_packet(), basis="nothing checkable here")
        assert proposal.citations == ()
        assert proposal.evidence_refs == ()
        assert proposal.evidence == "nothing checkable here"


class TestPuntReviewParsing:
    def test_concur_review_with_grounded_evidence_parses(self) -> None:
        review = parse_triage_punt_review(
            _review_block(f"verdict: concur\n{_cite()}"),
            _packet(),
        )
        assert review.ok, review.parse_errors
        assert review.verdict == PUNT_REVIEW_CONCUR

    def test_challenge_review_with_grounded_evidence_parses(self) -> None:
        review = parse_triage_punt_review(
            _review_block(f"verdict: challenge\n{_cite()}"),
            _packet(),
        )
        assert review.ok, review.parse_errors
        assert review.verdict == PUNT_REVIEW_CHALLENGE

    def test_review_without_evidence_is_rejected(self) -> None:
        review = parse_triage_punt_review(_review_block("verdict: concur\n"), _packet())
        assert not review.ok
        assert any("must cite evidence" in error for error in review.parse_errors)

    def test_review_with_invalid_quote_is_rejected(self) -> None:
        review = parse_triage_punt_review(
            _review_block(
                "verdict: challenge\n"
                + _cite(quote="the maintainer confirmed this was fixed last quarter")
            ),
            _packet(),
        )
        assert not review.ok
        assert any("not in that entry" in error for error in review.parse_errors)


# ── Rendering ─────────────────────────────────────────────────────────────────


def _result(proposal, **kwargs) -> FindingProposalResult:
    defaults = {
        "finding_id": "1312:audit-count",
        "issue_ref": "#1312",
        "packet_hash": "abc123",
        "proposal": proposal,
        "cost_usd": 0.0123,
        "cost_provenance": "provider_reported",
    }
    defaults.update(kwargs)
    return FindingProposalResult(**defaults)


class TestRendering:
    def test_punt_renders_its_reason_code_and_cost(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: punt\npunt_reason_code: verified-stale\n" + _cite(quote=_SUMMARY)
            ),
            _packet(),
        )
        text = render_result(_result(proposal))
        assert "#1312  PROPOSE punt (reason: verified-stale)" in text
        assert f"evidence: {_SUMMARY}" in text
        assert "cites: symbol-absent" in text
        assert "$0.0123" in text

    def test_fix_later_renders_its_target(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: fix_later\ntarget_milestone: Hygiene\n{_cite()}"),
            _packet(),
        )
        assert "PROPOSE fix_later → Hygiene" in render_result(_result(proposal))

    def test_rationale_renders_labelled_as_unverified(self) -> None:
        proposal = parse_triage_proposal(
            _block(f"disposition: needs_verification\n{_cite()}rationale: my own hunch\n"),
            _packet(),
        )
        text = render_result(_result(proposal))
        assert "reasoning (unverified): my own hunch" in text
        # And it never appears on the evidence line.
        evidence_line = next(ln for ln in text.splitlines() if "evidence:" in ln)
        assert "my own hunch" not in evidence_line

    def test_unmeasured_cost_is_labelled_not_zeroed(self) -> None:
        proposal = needs_verification_proposal(_packet(), basis="thin")
        text = render_result(_result(proposal, cost_usd=None, cost_provenance="unknown"))
        assert "unmeasured" in text
        assert "$0.00" not in text

    def test_validation_errors_are_visible_in_the_fallback(self) -> None:
        proposal = needs_verification_proposal(_packet(), basis="withheld")
        text = render_result(
            _result(
                proposal,
                fallback_reason="agent output failed validation on every attempt",
                validation_errors=("punt_reason_code must be one of [...]",),
            )
        )
        assert "fallback:" in text
        assert "validation error: punt_reason_code" in text

    def test_challenged_punt_review_renders_original_and_review_evidence(self) -> None:
        packet = _packet(
            evidence=(
                PacketEvidence(evidence_id="a", kind="staleness", summary="alpha words here"),
                PacketEvidence(evidence_id="b", kind="churn", summary="beta words there"),
            )
        )
        proposal = parse_triage_proposal(
            _block(
                "disposition: punt\n"
                "punt_reason_code: verified-stale\n"
                "evidence:\n"
                "  - ref: a\n"
                "    quote: alpha words here\n"
            ),
            packet,
        )
        review = parse_triage_punt_review(
            _review_block(
                "verdict: challenge\nevidence:\n  - ref: b\n    quote: beta words there\n"
            ),
            packet,
        )
        text = render_result(_result(proposal, punt_review=review, review_cost_usd=0.0045))
        assert "evidence: alpha words here" in text
        assert "REVIEW: challenge" in text
        assert "review evidence: beta words there" in text

    def test_challenged_fallback_renders_review_errors_without_fabricated_evidence(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: punt\npunt_reason_code: verified-stale\n" + _cite(quote=_SUMMARY)
            ),
            _packet(),
        )
        text = render_result(
            _result(
                proposal,
                punt_review=challenged_punt_review(basis="review withheld safely"),
                review_fallback_reason="reviewer output failed validation on every attempt",
                review_validation_errors=("verdict must be one of ['concur', 'challenge']",),
                review_cost_usd=None,
                review_cost_provenance="unknown",
            )
        )
        assert "REVIEW: challenge" in text
        assert "review basis: review withheld safely" in text
        assert "review evidence:" not in text
        assert "review fallback: reviewer output failed validation on every attempt" in text
        assert "review validation error: verdict must be one of" in text

    def test_empty_run_says_nothing_was_spent(self) -> None:
        text = render_run_summary(
            ProposalRunSummary(
                results=(),
                total_cost_usd=0.0,
                cost_provenance="provider_reported",
                triage_run_id="run123",
            )
        )
        assert "run run123" in text
        assert "no findings" in text
        assert "$0.0000" in text
        assert "no agent was invoked" in text
        assert "REVIEW STAGE: no-op" in text

    def test_run_summary_reports_total_and_advisory_status(self) -> None:
        proposal = needs_verification_proposal(_packet(), basis="thin")
        summary = ProposalRunSummary(
            results=(_result(proposal),),
            total_cost_usd=0.0123,
            cost_provenance="provider_reported",
            triage_run_id="run123",
            review_stage=PuntReviewStage(),
        )
        text = render_run_summary(summary)
        assert "run run123" in text
        assert "TOTAL SPEND: $0.0123" in text
        assert "no issue was modified" in text
        assert "REVIEW STAGE: no-op" in text
        assert summary.findings_count == 1

    def test_run_summary_renders_a_run_level_failure_once(self) -> None:
        proposal = needs_verification_proposal(_packet(), basis="thin")
        summary = ProposalRunSummary(
            results=(_result(proposal), _result(proposal)),
            total_cost_usd=0.0,
            cost_provenance="provider_reported",
            triage_run_id="run123",
            review_stage=PuntReviewStage(),
            run_level_failure=(
                "triage aborted agent dispatch before any proposer ran: "
                "claude credential store at /tmp/stale/.credentials.json holds no access token"
            ),
        )
        text = render_run_summary(summary)
        assert text.count("RUN-LEVEL FAILURE:") == 1
        assert text.count("/tmp/stale/.credentials.json") == 1
