"""Render the typed issue specification as operator-facing reference docs.

Documentation of the contract is a *rendering* of the contract, not a prose
restatement of it (ADR-0009). Every previous restatement drifted: the authoring
guide named six of thirty enforced verbs, ``bug-shape.md`` covered the Diagnosis
bullets and nothing else, and one rule existed only in an ADR and the changelog.

The generated document is checked against this renderer in CI, so a rule cannot
be enforced without being documented, and cannot be documented differently from
how it is enforced.

Stdlib plus the pure-data specification.
"""

from __future__ import annotations

from theforge.shape_check.issue_spec import (
    ISSUE_SHAPE_REFERENCE_PATH,
    ISSUE_TYPES,
    SECTIONS,
    ContradictionTrigger,
    IssueTypeSpec,
    Presence,
    SectionSpec,
)

__all__ = ["ISSUE_SHAPE_REFERENCE_PATH", "render_issue_shape_reference"]

_PRESENCE_NOTE: dict[Presence, str] = {
    Presence.REQUIRED: "required — the gate refuses a body without it",
    Presence.ADVISORY: "advisory — reported when absent, but it decides nothing",
    Presence.OPTIONAL: "optional — modeled so it renders canonically",
    Presence.FORBIDDEN: "forbidden — its presence contradicts this type",
}


def _heading_spellings(section: SectionSpec) -> str:
    canonical = f"`{section.canonical_heading_line}`"
    others = [f"`{alias}`" for alias in section.aliases if alias != section.canonical_heading]
    if not others:
        return canonical
    return f"{canonical} (also recognized: {', '.join(others)})"


def _section_list(section_keys: tuple[str, ...]) -> str:
    return ", ".join(f"`{SECTIONS[key].canonical_heading}`" for key in section_keys)


def _render_type(spec: IssueTypeSpec) -> list[str]:
    lines = [
        f"## `{spec.label}`",
        "",
        spec.summary[:1].upper() + spec.summary[1:] + ".",
        "",
    ]

    lines.append("### Sections, in canonical order")
    lines.append("")
    lines.append("| Section | Heading | Rule |")
    lines.append("| --- | --- | --- |")
    for rule in spec.section_rules:
        section = SECTIONS[rule.section_key]
        note = _PRESENCE_NOTE[rule.presence]
        if (
            rule.presence is Presence.FORBIDDEN
            and rule.trigger is ContradictionTrigger.BUG_BODY_SHAPE
        ):
            note = "forbidden — but only as part of the bug-report shape (see below)"
        lines.append(f"| {section.canonical_heading} | {_heading_spellings(section)} | {note} |")
    lines.append("")

    fielded = [
        SECTIONS[rule.section_key]
        for rule in spec.section_rules
        if SECTIONS[rule.section_key].fields and rule.presence is not Presence.FORBIDDEN
    ]
    for section in fielded:
        lines.append(f"### Fields of `{section.canonical_heading_line}`")
        lines.append("")
        lines.append("Each field is written as a bolded bullet lead-in. The gate matches the")
        lines.append("**label** literally (case-insensitive).")
        lines.append("")
        for field in section.fields:
            lines.append(f"- **{field.label}** — {field.satisfies}")
            lines.append(f"  Example: {field.bullet()}")
        lines.append("")

    lines.append("### Lifecycle states")
    lines.append("")
    lines.append("| State | Admits implementation | Meaning |")
    lines.append("| --- | --- | --- |")
    for state in spec.lifecycle_states:
        admits = "yes" if state.admits_implementation else f"no (`{state.refusal_code}`)"
        lines.append(f"| `{state.key}` | {admits} | {state.summary} |")
    lines.append("")

    if spec.section_keys_with(Presence.FORBIDDEN):
        lines.append("### Type/shape contradiction")
        lines.append("")
        if spec.contradiction is not None:
            lines.append(f"{spec.contradiction.rule_text.capitalize()}.")
            lines.append("")
        on_sight = _section_list(
            spec.forbidden_keys_with_trigger(ContradictionTrigger.ANY_SECTION)
        )
        if on_sight:
            lines.append(f"- Refused on sight: {on_sight}")
        in_shape = _section_list(
            spec.forbidden_keys_with_trigger(ContradictionTrigger.BUG_BODY_SHAPE)
        )
        if in_shape:
            lines.append(
                f"- Refused only as part of the bug-report shape: {in_shape} — a reproduction"
                " heading, or a symptom heading paired with an expectation heading, must be"
                " present before these count. One of them alone is ordinary prose."
            )
        if spec.contradiction is not None:
            lines.append(f"- Remediation: {spec.contradiction.remediation_hint}")
        lines.append("")

    return lines


def render_issue_shape_reference() -> str:
    """Render the full per-type reference, published as ``issue-shape.md``."""
    lines = [
        "# Issue shape reference",
        "",
        "> Generated from `theforge.shape_check.issue_spec`. Do not edit by hand —",
        "> change the specification and regenerate (the drift test in",
        "> `tests/test_issue_spec.py` fails until the two agree).",
        "",
        "This is the whole structural contract: what a well-formed issue of each type",
        "is, which sections it must carry, which it may not, and the states it can",
        "occupy. The checker validates against this same data, so a rule stated here",
        "is the rule the gate enforces.",
        "",
        "Two widths of heading recognition are deliberate. A section is *recognized*",
        "generously — a body written `## Observed behavior` is a bug body — but only",
        "the exact spellings listed below are *canonicalized* on output. Recognizing a",
        "spelling is never a licence to rewrite a heading whose extra words carry the",
        "author's meaning.",
        "",
        "Content the specification does not model — background, notes, worked examples,",
        "your own headings — is preserved as written. It is never dropped, re-levelled,",
        "or demoted into quoted prose.",
        "",
        "## Types",
        "",
    ]
    for spec in ISSUE_TYPES:
        lines.append(f"- [`{spec.label}`](#{spec.label}) — {spec.summary}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for spec in ISSUE_TYPES:
        lines.extend(_render_type(spec))
        lines.append("---")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"
