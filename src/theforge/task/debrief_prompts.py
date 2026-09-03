"""Ask a phase for a receipt on the prior-run claims it was given (#2866).

Prompt construction only: this module renders the debrief instruction and knows
nothing about how the answer is captured, verified, or counted. The closed
disposition set it names is imported from :mod:`theforge.knowledge_receipts`
rather than restated, so the prompt cannot drift from the verifier that reads
the answer.

The instruction is emitted **only** when the assembled context actually carries
injected claims. A phase asked to debrief claims it never received could only
produce citations that match nothing, which the verifier would then have to
record as unmatched — noise manufactured by the prompt.
"""

from __future__ import annotations

from typing import Any

from theforge.knowledge_receipts import (
    DISPOSITION_ALREADY_KNOWN,
    DISPOSITION_CHANGED_DECISION,
    DISPOSITION_CONFIRMED_APPROACH,
    DISPOSITION_IRRELEVANT,
    DISPOSITION_PROMPTED_VERIFICATION,
    DISPOSITION_STALE_OR_WRONG,
)

#: Where the debrief goes, per phase output contract.
STYLE_YAML_BLOCK = "yaml_block"  # plan / review: a top-level key in the YAML output
STYLE_HANDOFF = "handoff"  # dev: a key in the <forge_handoff> block
STYLE_TOOL_CALL = "tool_call"  # api-mode review: a field in the submit_review call

_DISPOSITION_LINES = (
    f"- `{DISPOSITION_CHANGED_DECISION}` — the claim changed a decision you made.",
    f"- `{DISPOSITION_PROMPTED_VERIFICATION}` — the claim made you verify something "
    "you would not otherwise have checked.",
    f"- `{DISPOSITION_CONFIRMED_APPROACH}` — the claim confirmed an approach you "
    "had already taken.",
    f"- `{DISPOSITION_ALREADY_KNOWN}` — you already knew what it says.",
    f"- `{DISPOSITION_IRRELEVANT}` — it did not bear on this task.",
    f"- `{DISPOSITION_STALE_OR_WRONG}` — it no longer describes the code, or it is wrong.",
)

_USE_DISPOSITIONS = f"`{DISPOSITION_CHANGED_DECISION}` and `{DISPOSITION_PROMPTED_VERIFICATION}`"


def exposed_claim_refs(assembled_context: Any) -> tuple[str, ...]:
    """The claim references this context pack rendered, in render order.

    Read off the pack's own exposure manifest — the same structure the audit
    record stores — so the list an agent is asked about is exactly the list its
    receipt will be matched against.
    """
    prior = getattr(assembled_context, "prior_run_context", None)
    if not isinstance(prior, dict):
        return ()
    refs: list[str] = []
    for included in prior.get("included") or []:
        if not isinstance(included, dict):
            continue
        for claim in included.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            ref = str(claim.get("claim_ref") or "").strip()
            if ref and ref not in refs:
                refs.append(ref)
    return tuple(refs)


def render_debrief_section(assembled_context: Any, *, style: str = STYLE_YAML_BLOCK) -> str:
    """Render the debrief instruction, or an empty string when nothing was injected."""
    refs = exposed_claim_refs(assembled_context)
    if not refs:
        return ""

    parts = [
        "",
        "## Prior-Run Knowledge Debrief (required)",
        "",
        f"The Repository Context Pack above injected {len(refs)} prior-run claim(s).",
        "Each is shown with a stable reference in square brackets.",
        f"{_placement(style)}, holding exactly one entry per",
        "reference below — no more, no fewer:",
        "",
        *(f"  - {ref}" for ref in refs),
        "",
        *_example(style, refs[0]),
        "",
        "Dispositions (choose exactly one per claim; these are the only accepted",
        "values — do not invent a new one, and do not substitute a near-synonym):",
        "",
        *_DISPOSITION_LINES,
        "",
        f"`evidence` is REQUIRED for {_USE_DISPOSITIONS}, and must point at something",
        "observable in THIS run's artifacts: a repo-relative path you changed,",
        "`plan §N` or `plan step N`, a commit, or a test. Use `evidence: []` for the",
        "other four dispositions. `did` is one short sentence describing what you did",
        "with the claim.",
        "",
        "This is an audit record and nothing else. It does not affect how your work is",
        "judged, there is no field for how useful the context was, and no disposition",
        "is a better answer than another — `irrelevant` and `stale_or_wrong` are as",
        f"useful to record as `{DISPOSITION_CHANGED_DECISION}`.",
        "",
    ]
    return "\n".join(parts)


def _placement(style: str) -> str:
    if style == STYLE_HANDOFF:
        return "Add a `knowledge_debrief` key to your `<forge_handoff>` block"
    if style == STYLE_TOOL_CALL:
        return "Pass a `knowledge_debrief` array in your `submit_review` call"
    return "Add a top-level `knowledge_debrief` key to your YAML output"


def _example(style: str, sample_ref: str) -> list[str]:
    if style == STYLE_TOOL_CALL:
        return ["Each entry is an object with `claim_ref`, `disposition`, `did`, and `evidence`."]
    return [
        "Shape:",
        "",
        "```yaml",
        "knowledge_debrief:",
        f'  - claim_ref: "{sample_ref}"',
        f"    disposition: {DISPOSITION_CHANGED_DECISION}",
        '    did: "one sentence: what you did with this claim"',
        # Placeholder rather than a concrete path: this prompt ships to every
        # project forge runs, and a worked example naming one repository's layout
        # reads as an instruction to produce that layout.
        '    evidence: ["<path/to/file_you_changed>", "plan §3"]',
        "```",
    ]
