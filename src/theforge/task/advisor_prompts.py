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

from collections.abc import Sequence

from theforge.escalation_advisor import (
    ACTION_FORGE_OPERATIONS,
    ACTION_LABELS,
    ACTION_TAXONOMY,
    EvidencePacket,
    action_disposition,
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


def _render_taxonomy(actions: Sequence[str]) -> str:
    lines: list[str] = []
    for action in actions:
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
    if packet.topology_signal:
        lines.append("### Detected pattern: TOPOLOGY WALK (deterministic detector)")
        sig = packet.topology_signal
        cycles = ", ".join(str(c) for c in (sig.get("cycles") or []))
        detector_finding = (
            "A deterministic detector found that successive review cycles each "
            "resolved their predecessor's findings and then raised a NEW instance of "
            "the same underlying concern at a location nobody had enumerated. The "
            "loop was not converging — it was inventorying a surface one development "
            "pass at a time."
        )
        # Only say the ceiling was not reached when the detector is what stopped
        # the story. The same signal is recorded on runs that escalated for
        # another reason, and there it is evidence about the churn, not an
        # account of why this escalation happened.
        if packet.topology_triggered:
            lines.append(f"This escalation fired BEFORE the cycle ceiling. {detector_finding}")
        else:
            lines.append(
                f"This escalation was NOT triggered by the detector — see the "
                f"escalation reason below for what stopped the story. The pattern is "
                f"reported here as supporting evidence about the churn. "
                f"{detector_finding}"
            )
        lines.append(f"shared concern (anchor): {sig.get('seed_anchor')}")
        lines.append(f"cycles in the pattern: {cycles}")
        for item in sig.get("sequence") or []:
            loc = item.get("file") or "(unknown file)"
            if item.get("line"):
                loc = f"{loc}:{item['line']}"
            lines.append(f"  - cycle {item.get('cycle')} @ {loc} — {item.get('description')}")
        if sig.get("rationale"):
            lines.append(f"detector rationale: {sig['rationale']}")
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


def build_advisor_prompt(
    packet: EvidencePacket, available_actions: Sequence[str] | None = None
) -> str:
    """Build the fresh-context escalation-advisor prompt from an evidence packet."""
    if available_actions is None:
        available_actions = [
            action for action in ACTION_TAXONOMY if action_disposition(action) != "named"
        ]
    available_actions = [action for action in available_actions if action in ACTION_TAXONOMY]
    taxonomy = _render_taxonomy(available_actions)
    packet_text = _render_packet(packet)
    example_action = available_actions[0] if available_actions else ACTION_TAXONOMY[0]
    # An escalation no longer implies the cycle budget ran out: a detected
    # topology walk routes here with cycles still available, precisely so the
    # decision is made while it is still worth something. Saying "exhausted"
    # there would hand the advisor a false premise about what the run spent.
    #
    # Keyed on topology_TRIGGERED, not on the signal's presence. A run that hit
    # the ceiling can still carry a recorded signal, and telling the advisor
    # cycles remain when they do not is the same false premise in reverse.
    if packet.topology_triggered and packet.topology_signal:
        situation = (
            "A story has escalated BEFORE its review cycles were exhausted: a "
            "deterministic detector found the loop walking a topology rather than "
            "converging (see the detected-pattern section of the packet). Review "
            "cycles remain available — the loop stopped early on purpose, so the "
            "decision could be about the framing instead of about the latest finding."
        )
    else:
        situation = (
            "A story has escalated: the dev agent could not converge and the review "
            "cycles were exhausted."
        )

    return f"""You are the ESCALATION ADVISOR for an autonomous software-development \
orchestrator.

{situation} You did NOT run those cycles. Your job is to read the prepared \
evidence packet BELOW with fresh eyes and route this escalation into a small, \
constrained menu of evidence-backed action choices for a human operator.

An escalation is not just a failed implementation attempt — it is evidence that \
the current task framing may be invalid. The churn pattern (what reviewers kept \
flagging, what the dev kept re-breaking) is the most valuable signal. Look for \
the case where the dev kept attacking an unbounded space (whack-a-mole) instead \
of converging on a concrete, reviewable outcome. Recommend only from the \
currently executable actions listed below.

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
- `recommendation`: one of {list(available_actions)} — your single best action.
- `rationale`: one or two sentences citing the packet for why.
- `options`: a non-empty list. Include the recommended action AND any other \
credible actions. Each option is a mapping with:
    - `action`: one of {list(available_actions)}
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
