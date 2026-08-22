"""Prompt construction for the fresh-context backlog triage proposer.

The proposer is a fresh model that did not file the finding and does not
inherit whatever framing produced it. It receives one prepared evidence packet
and must emit a single constrained proposal: one disposition from the fixed
taxonomy, its required payload, and citations back into the packet.

This module only builds the prompt string. The output schema and its validator
live in :mod:`theforge.triage_proposal`; the control flow that invokes the agent
lives in :mod:`theforge.coordinator.triage_proposal_flow`. The taxonomy, the
punt reason codes, and the milestone vocabulary are all *rendered from* those
definitions rather than restated here, so the prompt cannot drift from what the
validator will accept.
"""

from __future__ import annotations

from collections.abc import Sequence

from theforge.triage_proposal import (
    DISPOSITION_FIX_LATER,
    DISPOSITION_FIX_NOW,
    DISPOSITION_NEEDS_VERIFICATION,
    DISPOSITION_PUNT,
    HYGIENE_POOL,
    MIN_QUOTE_WORDS,
    PUNT_REASON_CODES,
    PUNT_REASON_GUIDE,
    FindingPacket,
)

_DISPOSITION_GUIDE: dict[str, str] = {
    DISPOSITION_FIX_NOW: (
        "The finding is real, still applies, and is worth the current milestone's "
        "attention. Payload: `target_milestone` MUST be the current milestone."
    ),
    DISPOSITION_FIX_LATER: (
        "The finding is real and still applies, but does not have to be paid for "
        "now. Payload: `target_milestone` is a named milestone, or the standing "
        f"`{HYGIENE_POOL}` pool when no milestone owns it."
    ),
    DISPOSITION_PUNT: (
        "The finding should be discarded — the packet SHOWS it no longer applies. "
        "Payload: `punt_reason_code` from the fixed set below. A punt is "
        "irreversible in practice, so it needs positive evidence, not the absence "
        "of contrary evidence."
    ),
    DISPOSITION_NEEDS_VERIFICATION: (
        "The packet does not let you distinguish stale from active. This is the "
        "correct answer whenever you would otherwise be guessing. Payload: none."
    ),
}


def _render_taxonomy(dispositions: Sequence[str], packet: FindingPacket) -> str:
    lines: list[str] = []
    for disposition in dispositions:
        lines.append(f"- `{disposition}`: {_DISPOSITION_GUIDE.get(disposition, '')}")
        if disposition == DISPOSITION_FIX_NOW and packet.current_milestone:
            lines.append(f"    current milestone: {packet.current_milestone}")
        if disposition == DISPOSITION_FIX_LATER:
            lines.append(f"    allowed targets: {list(packet.fix_later_targets())}")
    return "\n".join(lines)


def _render_punt_reasons() -> str:
    return "\n".join(
        f"- `{code}`: {PUNT_REASON_GUIDE.get(code, '')}" for code in PUNT_REASON_CODES
    )


def _render_packet(packet: FindingPacket) -> str:
    lines: list[str] = []
    lines.append(f"## Finding {packet.finding_id} ({packet.issue_ref})")
    lines.append("")
    lines.append("### Finding body (the claim as filed — NOT evidence that it still holds)")
    lines.append(packet.finding_body or "(none)")
    lines.append("")
    lines.append("### Evidence (the ONLY things you may cite)")
    if packet.evidence:
        for item in packet.evidence:
            flag = "checkable" if item.checkable else "not independently checkable"
            lines.append(f"- id: `{item.evidence_id}` [{item.kind}, {flag}]")
            lines.append(f"    {item.summary or '(no summary)'}")
            if item.detail:
                lines.append(f"    detail: {item.detail}")
    else:
        lines.append("(none — the report computed no evidence for this finding)")
    lines.append("")
    lines.append("### Disposition history for this finding (from the audit substrate)")
    if packet.disposition_history:
        for row in packet.disposition_history:
            lines.append(
                f"- {row.get('emitted_at', '?')}: {row.get('disposition', '?')}"
                f" {row.get('target_milestone') or row.get('punt_reason_code') or ''}".rstrip()
            )
    else:
        lines.append("(no prior disposition rows — this finding has not been triaged before)")
    return "\n".join(lines)


def _example_citation(packet: FindingPacket) -> tuple[str, str]:
    """Return a (ref, quote) pair the example block can show.

    Built from the packet's own first entry so the illustration demonstrates a
    citation that would actually pass validation. A generic placeholder would
    show the shape while modelling the exact mistake the validator rejects —
    words that are not in any entry.
    """
    if not packet.evidence:
        return "<evidence-id>", "<verbatim words from that entry>"
    entry = packet.evidence[0]
    words = entry.citable_text().replace('"', "").split()
    if len(words) < MIN_QUOTE_WORDS:
        return entry.evidence_id, entry.citable_text().replace('"', "")
    return entry.evidence_id, " ".join(words[: max(MIN_QUOTE_WORDS, 6)])


def build_triage_prompt(packet: FindingPacket, previous_errors: Sequence[str] = ()) -> str:
    """Build the fresh-context triage-proposal prompt for one finding packet.

    ``previous_errors`` is the validator's output from a prior attempt on this
    same packet. It is appended verbatim so the retry is a correction of a named
    schema violation rather than a second blind draw.
    """
    available = packet.available_dispositions()
    taxonomy = _render_taxonomy(available, packet)
    packet_text = _render_packet(packet)
    evidence_ids = list(packet.evidence_ids())
    example_ref, example_quote = _example_citation(packet)

    retry_block = ""
    if previous_errors:
        rendered = "\n".join(f"- {error}" for error in previous_errors)
        retry_block = (
            "\n## Your previous attempt was REJECTED\n\n"
            "It failed schema/grounding validation for these reasons. Fix exactly "
            "these and emit a new proposal:\n\n"
            f"{rendered}\n"
        )

    unavailable_note = ""
    if DISPOSITION_FIX_NOW not in available:
        unavailable_note = (
            "\nNote: `fix_now` is NOT available for this finding — no current "
            "milestone was supplied, so there is no target it could name.\n"
        )

    return f"""You are the BACKLOG TRIAGE PROPOSER for an autonomous \
software-development orchestrator.

You did NOT file this finding. Read the prepared evidence packet below with \
fresh eyes and propose exactly ONE disposition for it. Your proposal is \
advisory: a human operator decides, and nothing you say is applied \
automatically. No issue will be modified by this run.

## Disposition taxonomy (your `disposition` MUST be one of these exact values)

{taxonomy}
{unavailable_note}
## Punt reason codes (fixed set — `punt_reason_code` MUST be one of these)

{_render_punt_reasons()}

## Grounding rule (this is the rule that gets proposals rejected)

You do not write the evidence — you SELECT it. Every proposal must cite \
`evidence` as a list of `{{ref, quote}}` pairs, where:

- `ref` is one of the packet evidence ids below — {evidence_ids or "(none)"} — \
and nothing else, and
- `quote` is a span of THAT entry's own text, copied **verbatim**, at least \
{MIN_QUOTE_WORDS} consecutive words long.

The quote is checked mechanically against the entry you cited. A paraphrase, a \
summary, an inference, or a claim the entry does not make will be rejected even \
when the `ref` is real — citing a valid id does not license a claim beside it. \
Do not investigate the repository to manufacture new evidence; the packet is \
the record.

Anything you want to say in your own words belongs in `rationale`. It is shown \
to the operator labelled as unverified reasoning and it is NOT evidence: it \
cannot make a disposition admissible on its own.

Absence of evidence is not evidence for discard. If the packet does not let you \
tell a stale finding from an active one, the answer is \
`{DISPOSITION_NEEDS_VERIFICATION}`, never `{DISPOSITION_PUNT}`.

## Evidence packet

{packet_text}
{retry_block}
## Required output

Emit EXACTLY ONE `<triage_proposal>` block containing a YAML mapping. Do not \
put the block inside a code fence. Free-form triage prose is NOT allowed.

The YAML mapping must have:
- `disposition`: one of {list(available)}
- `evidence`: a non-empty list of mappings, each with `ref` (a packet evidence \
id) and `quote` (that entry's words, verbatim).
- `rationale` (optional): your own reasoning — why this disposition rather than \
the neighbouring one. Never a substitute for a quote.
- `target_milestone`: required for `{DISPOSITION_FIX_NOW}` and \
`{DISPOSITION_FIX_LATER}`; MUST be omitted otherwise.
- `punt_reason_code`: required for `{DISPOSITION_PUNT}`; MUST be omitted otherwise.

Example shape (illustrative only — analyse the real packet and quote ITS text):

<triage_proposal>
disposition: {DISPOSITION_NEEDS_VERIFICATION}
evidence:
  - ref: {example_ref}
    quote: "{example_quote}"
rationale: "Nothing here distinguishes a stale finding from an active one, so \
deciding either way would be a guess."
</triage_proposal>
"""
