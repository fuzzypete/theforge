"""Prompt construction for the fresh-context escalation advisor.

The advisor is a fresh model that did NOT run the failed dev/review cycles — it
must not inherit the failed framing. It receives a prepared evidence packet and
must emit a constrained advisory report: a recommendation drawn from the fixed
action taxonomy plus a menu of evidence-backed options.

This module only builds the prompt string. The output schema/validation lives in
``theforge.escalation_advisor`` and the coordinator control flow that invokes the
agent lives in ``theforge.coordinator.escalation_advisor_flow``.
"""

from __future__ import annotations

from theforge.escalation_advisor import (
    ACTION_FORGE_OPERATIONS,
    ACTION_LABELS,
    ACTION_TAXONOMY,
    EvidencePacket,
)

_TAXONOMY_GUIDE: dict[str, str] = {
    "accept": (
        "The current work is actually good enough against the acceptance criteria; "
        "the review churn was over-strict. Finalize/merge as-is."
    ),
    "land_core_defer_edges": (
        "The core deliverable is sound but reviewers kept flagging edge cases. Merge "
        "the core and file follow-up issues for the edges rather than churning further."
    ),
    "redirect": (
        "The task is winnable but the *approach* the dev kept taking cannot succeed. "
        "Re-run the dev phase under a corrected framing/constraint that names the "
        "winnable primitive."
    ),
    "decompose": (
        "The story bundles independent concerns that fight each other in one cycle. "
        "Split it into smaller runnable pieces."
    ),
    "elevate": (
        "This cannot be resolved by another dev cycle — it is an unmade "
        "design/architecture decision masquerading as an implementation failure. Route "
        "it to a crisp human decision. Most escalations are mis-classified design "
        "decisions; prefer this when the churn is whack-a-mole on an unbounded space."
    ),
    "defer_or_abandon": (
        "The work is not worth continuing now: defer to a later milestone or abandon it."
    ),
}


def _render_taxonomy() -> str:
    lines: list[str] = []
    for action in ACTION_TAXONOMY:
        label = ACTION_LABELS.get(action, action)
        guide = _TAXONOMY_GUIDE.get(action, "")
        op = ACTION_FORGE_OPERATIONS.get(action, "")
        lines.append(f"- {label} (`{action}`): {guide}")
        lines.append(f"    forge operation: {op}")
    return "\n".join(lines)


def _render_packet(packet: EvidencePacket) -> str:
    lines: list[str] = []
    lines.append(f"## Story: {packet.story_name} ({packet.issue_ref})")
    lines.append("")
    lines.append("### Issue body")
    lines.append(packet.issue_body or "(none)")
    lines.append("")
    lines.append("### Acceptance criteria")
    if packet.acceptance_criteria:
        for ac in packet.acceptance_criteria:
            lines.append(f"- {ac}")
    else:
        lines.append("(none extracted)")
    lines.append("")
    lines.append("### Review cycle history (the churn pattern — the key signal)")
    if packet.cycles:
        for c in packet.cycles:
            lines.append(f"#### Cycle {c.cycle} — verdict {c.verdict}")
            if c.summary:
                lines.append(f"summary: {c.summary}")
            if c.findings:
                lines.append("findings:")
                for f in c.findings:
                    lines.append(f"  - {f}")
    else:
        lines.append("(no per-cycle history captured)")
    lines.append("")
    lines.append("### Final reviewer verdicts")
    if packet.reviewer_verdicts:
        for name, verdict in packet.reviewer_verdicts.items():
            lines.append(f"- {name}: {verdict}")
    else:
        lines.append("(none)")
    lines.append(f"final merged verdict: {packet.final_verdict or '(unknown)'}")
    lines.append("")
    lines.append("### Dev diff (what the dev produced)")
    lines.append(packet.dev_diff or "(no diff captured)")
    lines.append("")
    lines.append("### Test / gate failures")
    lines.append(packet.test_failures or "(none captured)")
    lines.append("")
    lines.append("### Escalation reason")
    lines.append(packet.escalation_reason or "(none)")
    return "\n".join(lines)


def build_advisor_prompt(packet: EvidencePacket) -> str:
    """Build the fresh-context escalation-advisor prompt from an evidence packet."""
    taxonomy = _render_taxonomy()
    packet_text = _render_packet(packet)
    example_action = ACTION_TAXONOMY[2]  # "redirect"

    return f"""You are the ESCALATION ADVISOR for an autonomous software-development \
orchestrator.

A story has escalated: the dev agent could not converge and the review cycles \
were exhausted. You did NOT run those cycles. Your job is to read the prepared \
evidence packet BELOW with fresh eyes and route this escalation into a small, \
constrained menu of evidence-backed action choices for a human operator.

An escalation is not just a failed implementation attempt — it is evidence that \
the current task framing may be invalid. The churn pattern (what reviewers kept \
flagging, what the dev kept re-breaking) is the most valuable signal. Look for \
the case where the dev kept attacking an unbounded space (whack-a-mole) when the \
issue itself already named a winnable primitive — that is usually a Redirect or \
an Elevate, not another dev cycle.

## Action taxonomy (your recommendation and every option's `action` MUST be one \
of these exact values)

{taxonomy}

## Evidence packet

{packet_text}

## Required output

Emit EXACTLY ONE `<advisory_report>` block containing a YAML mapping. Do not put \
the block inside a code fence. Free-form prose recommendations are NOT allowed — \
the recommendation must be one of the taxonomy values above.

The YAML mapping must have:
- `recommendation`: one of {list(ACTION_TAXONOMY)} — your single best action.
- `rationale`: one or two sentences citing the packet for why.
- `options`: a non-empty list. Include the recommended action AND any other \
credible actions. Each option is a mapping with:
    - `action`: one of {list(ACTION_TAXONOMY)}
    - `evidence`: what in the packet supports this action (cite cycles/findings/ACs)
    - `forge_operation`: the concrete forge operation this action triggers
    - `risk`: the risk of taking this action
    - `consequence`: what happens if the operator selects this action

The `recommendation` value MUST also appear as one of the `options[].action` values.

Example shape (illustrative only — analyse the real packet):

<advisory_report>
recommendation: {example_action}
rationale: "Reviewers flagged a different bypass every cycle; the enforcement \
approach cannot be complete, while the issue named a winnable end-state invariant."
options:
  - action: {example_action}
    evidence: "Cycles 1-5 each found a new bypass (missing pull, force-push, alias variants)."
    forge_operation: "re-run with constraint (re-dev under a corrected framing)"
    risk: "The corrected framing may still be under-specified."
    consequence: "Dev re-runs against the end-state invariant instead of the blocklist."
  - action: elevate
    evidence: "The blocklist-vs-invariant choice is an unmade architecture decision."
    forge_operation: "bump (route to a human design/architecture decision)"
    risk: "Adds a human round-trip before any code changes."
    consequence: "A human picks the enforcement architecture; the story re-enters with it fixed."
</advisory_report>
"""
