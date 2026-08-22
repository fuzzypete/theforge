"""Tests for the triage taxonomy, evidence packet, and proposal schema boundary.

Covers the pure-data module (``theforge.triage_proposal``): the fixed
disposition taxonomy with its required payloads, the grounding rule that a
proposal may cite only evidence present in its packet, and the rendering an
operator reads. The agent invocation, the retry, and the audit persistence are
exercised in ``test_triage_proposal_flow.py``.
"""

from __future__ import annotations

from theforge.triage_proposal import (
    DISPOSITION_FIX_NOW,
    DISPOSITION_NEEDS_VERIFICATION,
    DISPOSITION_TAXONOMY,
    HYGIENE_POOL,
    PUNT_REASON_CODES,
    FindingPacket,
    FindingProposalResult,
    PacketEvidence,
    ProposalRunSummary,
    needs_verification_proposal,
    parse_triage_proposal,
    render_result,
    render_run_summary,
)


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
                summary="cited symbol absent from current tree",
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


def _block(body: str) -> str:
    return f"prose before\n<triage_proposal>\n{body}\n</triage_proposal>\nprose after"


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


# ── Valid payloads ────────────────────────────────────────────────────────────


class TestValidPayloads:
    def test_fix_now_with_current_milestone_parses(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: fix_now\n"
                "target_milestone: v0.12.0\n"
                "evidence: still reproduces at HEAD\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(),
        )
        assert proposal.ok, proposal.parse_errors
        assert proposal.disposition == DISPOSITION_FIX_NOW
        assert proposal.target_milestone == "v0.12.0"

    def test_fix_later_to_named_milestone_parses(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: fix_later\n"
                "target_milestone: v0.13.0\n"
                "evidence: low blast radius\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(),
        )
        assert proposal.ok, proposal.parse_errors
        assert proposal.target_milestone == "v0.13.0"

    def test_fix_later_to_hygiene_pool_parses(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: fix_later\n"
                f"target_milestone: {HYGIENE_POOL}\n"
                "evidence: keep, low blast radius\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(),
        )
        assert proposal.ok, proposal.parse_errors

    def test_every_punt_reason_code_is_accepted(self) -> None:
        for code in PUNT_REASON_CODES:
            proposal = parse_triage_proposal(
                _block(
                    "disposition: punt\n"
                    f"punt_reason_code: {code}\n"
                    "evidence: report shows the cited symbol is gone\n"
                    "evidence_refs: [symbol-absent]\n"
                ),
                _packet(),
            )
            assert proposal.ok, (code, proposal.parse_errors)
            assert proposal.punt_reason_code == code

    def test_needs_verification_without_payload_parses(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: needs_verification\n"
                "evidence: cannot distinguish stale from active\n"
                "evidence_refs: [symbol-absent]\n"
            ),
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
        proposal = parse_triage_proposal(
            _block("disposition: close_it\nevidence: x\nevidence_refs: [symbol-absent]\n"),
            _packet(),
        )
        assert not proposal.ok
        assert any("disposition must be one of" in e for e in proposal.parse_errors)

    def test_fix_now_without_target_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block("disposition: fix_now\nevidence: x\nevidence_refs: [symbol-absent]\n"),
            _packet(),
        )
        assert not proposal.ok
        assert any("fix_now requires target_milestone" in e for e in proposal.parse_errors)

    def test_fix_now_targeting_another_milestone_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: fix_now\n"
                "target_milestone: v0.13.0\n"
                "evidence: x\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(),
        )
        assert not proposal.ok
        assert any("current milestone" in e for e in proposal.parse_errors)

    def test_fix_now_is_rejected_when_no_current_milestone_is_known(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: fix_now\n"
                "target_milestone: v0.12.0\n"
                "evidence: x\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(current_milestone=None),
        )
        assert not proposal.ok
        assert any("not available for this finding" in e for e in proposal.parse_errors)

    def test_fix_later_to_unknown_milestone_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: fix_later\n"
                "target_milestone: v9.9.9\n"
                "evidence: x\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(),
        )
        assert not proposal.ok
        assert any("target_milestone must be one of" in e for e in proposal.parse_errors)

    def test_punt_without_reason_code_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block("disposition: punt\nevidence: x\nevidence_refs: [symbol-absent]\n"),
            _packet(),
        )
        assert not proposal.ok
        assert any("punt requires punt_reason_code" in e for e in proposal.parse_errors)

    def test_unknown_punt_reason_code_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: punt\n"
                "punt_reason_code: feels-old\n"
                "evidence: x\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(),
        )
        assert not proposal.ok
        assert any("punt_reason_code must be one of" in e for e in proposal.parse_errors)

    def test_needs_verification_with_a_target_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: needs_verification\n"
                "target_milestone: v0.12.0\n"
                "evidence: x\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(),
        )
        assert not proposal.ok
        assert any("must not carry target_milestone" in e for e in proposal.parse_errors)


class TestGrounding:
    def test_unknown_evidence_ref_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: punt\n"
                "punt_reason_code: verified-stale\n"
                "evidence: I recall this was fixed last quarter\n"
                "evidence_refs: [my-memory]\n"
            ),
            _packet(),
        )
        assert not proposal.ok
        assert any("not present in the packet" in e for e in proposal.parse_errors)

    def test_missing_evidence_refs_is_rejected_when_the_packet_has_some(self) -> None:
        proposal = parse_triage_proposal(
            _block("disposition: punt\npunt_reason_code: verified-stale\nevidence: gone\n"),
            _packet(),
        )
        assert not proposal.ok
        assert any("must cite evidence_refs" in e for e in proposal.parse_errors)

    def test_missing_evidence_prose_is_rejected(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: fix_later\n"
                "target_milestone: Hygiene\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(),
        )
        assert not proposal.ok
        assert any("missing evidence" in e for e in proposal.parse_errors)

    def test_a_proposal_citing_only_packet_ids_survives(self) -> None:
        packet = _packet(
            evidence=(
                PacketEvidence(evidence_id="a", kind="staleness", summary="s"),
                PacketEvidence(evidence_id="b", kind="churn", summary="s"),
            )
        )
        proposal = parse_triage_proposal(
            _block(
                "disposition: fix_later\n"
                "target_milestone: Hygiene\n"
                "evidence: churned but still matches\n"
                "evidence_refs: [a, b]\n"
            ),
            packet,
        )
        assert proposal.ok, proposal.parse_errors
        assert proposal.evidence_refs == ("a", "b")


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
        proposal = needs_verification_proposal(
            _packet(), evidence="nothing checkable", evidence_refs=("symbol-absent",)
        )
        assert proposal.disposition == DISPOSITION_NEEDS_VERIFICATION
        assert proposal.ok


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
                "disposition: punt\n"
                "punt_reason_code: verified-stale\n"
                "evidence: report shows cited symbol absent from current tree\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(),
        )
        text = render_result(_result(proposal))
        assert "#1312  PROPOSE punt (reason: verified-stale)" in text
        assert "evidence: report shows cited symbol absent" in text
        assert "$0.0123" in text

    def test_fix_later_renders_its_target(self) -> None:
        proposal = parse_triage_proposal(
            _block(
                "disposition: fix_later\n"
                "target_milestone: Hygiene\n"
                "evidence: keep\n"
                "evidence_refs: [symbol-absent]\n"
            ),
            _packet(),
        )
        assert "PROPOSE fix_later → Hygiene" in render_result(_result(proposal))

    def test_unmeasured_cost_is_labelled_not_zeroed(self) -> None:
        proposal = needs_verification_proposal(_packet(), evidence="thin")
        text = render_result(_result(proposal, cost_usd=None, cost_provenance="unknown"))
        assert "unmeasured" in text
        assert "$0.00" not in text

    def test_validation_errors_are_visible_in_the_fallback(self) -> None:
        proposal = needs_verification_proposal(_packet(), evidence="withheld")
        text = render_result(
            _result(
                proposal,
                fallback_reason="agent output failed validation on every attempt",
                validation_errors=("punt_reason_code must be one of [...]",),
            )
        )
        assert "fallback:" in text
        assert "validation error: punt_reason_code" in text

    def test_empty_run_says_nothing_was_spent(self) -> None:
        text = render_run_summary(
            ProposalRunSummary(results=(), total_cost_usd=0.0, cost_provenance="provider_reported")
        )
        assert "no findings" in text
        assert "$0.0000" in text
        assert "no agent was invoked" in text

    def test_run_summary_reports_total_and_advisory_status(self) -> None:
        proposal = needs_verification_proposal(_packet(), evidence="thin")
        summary = ProposalRunSummary(
            results=(_result(proposal),),
            total_cost_usd=0.0123,
            cost_provenance="provider_reported",
        )
        text = render_run_summary(summary)
        assert "TOTAL SPEND: $0.0123" in text
        assert "no issue was modified" in text
        assert summary.findings_count == 1
