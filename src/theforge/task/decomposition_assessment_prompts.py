"""Prompt construction for the preflight decomposition assessment (#2686).

The assessment reads the evidence preflight already assembled — the story body
and its acceptance criteria, the complexity score and its sub-scores, the
criteria preflight actually checked, the files it expects to be touched — and
emits ONE artifact: a candidate split, or a statement that the story is atomic.

Two things this prompt deliberately does not do:

* It does not ask for a recommendation. The operator still answers the same
  approve/decompose pause; the artifact is something they read, not a vote.
* It does not use the escalation advisor's action taxonomy. That vocabulary is
  about routing a *failed* run; this is a description of a story's shape before
  anything has been spent on it.

The output schema and its validation live in
``theforge.decomposition_assessment``; the coordinator control flow that invokes
the agent lives in ``theforge.coordinator.preflight_decomposition_flow``.
"""

from __future__ import annotations

from theforge.decomposition_assessment import MIN_SLICES, AssessmentPacket

# Bound the story body carried into the prompt: an assessment reads the story to
# find boundaries, and a runaway issue body should not become the whole budget.
_BODY_MAX_CHARS = 12_000


def _render_criteria(packet: AssessmentPacket) -> str:
    if not packet.acceptance_criteria:
        return "(none extracted — cover the story body's requirements instead)"
    return "\n".join(f"{i}. {ac}" for i, ac in enumerate(packet.acceptance_criteria, start=1))


def _render_preflight_evidence(packet: AssessmentPacket) -> str:
    lines: list[str] = []
    axes = []
    if packet.implementation_complexity_score is not None:
        axes.append(f"implementation {packet.implementation_complexity_score}")
    if packet.validation_complexity_score is not None:
        axes.append(f"validation {packet.validation_complexity_score}")
    axis_text = f" ({', '.join(axes)})" if axes else ""
    lines.append(f"projected complexity score: {packet.complexity_score}{axis_text}")
    if packet.scope_exceeded:
        lines.append(
            "scope_exceeded: preflight put the implementation axis at its ceiling — it "
            "judged this over what one story should attempt."
        )
    if packet.score_provenance_note:
        lines.append(
            f"score provenance: {packet.score_provenance_note} — the score is conservative "
            "rather than derived, so weigh the story text more heavily than the number."
        )
    if packet.likely_files:
        lines.append("files preflight expects to be touched:")
        lines.extend(f"  - {path}" for path in packet.likely_files)
    if packet.warnings:
        lines.append("preflight warnings:")
        lines.extend(f"  - {warning}" for warning in packet.warnings)
    if packet.criteria_checked:
        lines.append("what preflight actually examined per criterion:")
        for entry in packet.criteria_checked:
            criterion = str(entry.get("criterion", "")).strip()
            evidence = str(entry.get("evidence", "")).strip()
            files = entry.get("files_checked") or []
            lines.append(f"  - {criterion}")
            if files:
                lines.append(f"      files: {', '.join(str(f) for f in files)}")
            if evidence:
                lines.append(f"      evidence: {evidence}")
    return "\n".join(lines)


def build_decomposition_assessment_prompt(packet: AssessmentPacket) -> str:
    """Build the artifact-only prompt for one decomposition assessment."""
    body = packet.story_body or "(no story body)"
    if len(body) > _BODY_MAX_CHARS:
        body = body[:_BODY_MAX_CHARS] + "\n… (story body truncated)"
    criteria = _render_criteria(packet)
    criteria_count = len(packet.acceptance_criteria)
    coverage_rule = (
        f"Every one of the {criteria_count} acceptance criteria above must appear in "
        "at least one slice's `covers_criteria`. A split that drops a criterion is "
        "rejected outright."
        if criteria_count
        else "No acceptance criteria were extracted, so `covers_criteria` may be empty."
    )

    return f"""You are producing a DECOMPOSITION ASSESSMENT for an autonomous \
software-development orchestrator.

A story stopped at the end of PREFLIGHT because its complexity score reached the \
gate threshold. Nothing past preflight has been spent on it. A human operator is \
about to decide whether to implement it as scoped or return it to be split, and \
your artifact is what they read while deciding.

You are NOT deciding. Do not recommend approving or decomposing, do not rank the \
options, and do not address the operator. Produce the artifact and stop.

Your job is to answer one question from the evidence below: **if this story were \
split, what would the pieces be?** A piece is a story someone could run on its \
own: it has a boundary you can state, and the work inside it does not require \
finishing a different piece first except where you say it does.

If the story genuinely has no such boundary — it is one indivisible change that \
is simply large — say so with `atomic: true` and a one-sentence reason. That is a \
real result, not a failure, and it is far better than inventing a split along a \
seam that is not there.

Read the repository if it helps you place a boundary. You have read-only access \
and must not modify anything: no files, no issues, no commands with side effects.

## Story: {packet.story_name} ({packet.issue_ref})

{body}

## Acceptance criteria (indices are what `covers_criteria` refers to)

{criteria}

## Preflight evidence

{_render_preflight_evidence(packet)}

## Required output

Emit EXACTLY ONE `<decomposition_assessment>` block containing a YAML mapping. Do \
not put the block inside a code fence. Emit nothing else — no preamble, no \
summary after it.

For a story you can split:
- `atomic`: false
- `slices`: a list of at least {MIN_SLICES} mappings, each with:
    - `id`: a small positive integer, unique within this assessment
    - `title`: what the slice delivers, in one line
    - `scope`: the BOUNDARY — what is inside this slice and, where it matters, \
what is deliberately left to another slice. Not a restatement of the title.
    - `depends_on`: a list of slice ids that must land first (empty when none). \
Every id must be one you declared here, and never the slice's own id.
    - `covers_criteria`: the acceptance-criterion indices this slice satisfies.
- `unsettled`: a list of the decisions you could NOT settle from the evidence — \
a boundary you could argue either way, a criterion that could belong to two \
slices, a coupling you could not resolve. Say which and why. An empty list is a \
claim that every boundary above is derived; do not make that claim lightly.

{coverage_rule}

For a story that cannot be split:
- `atomic`: true
- `atomic_reason`: one sentence on what makes it indivisible.

Example shape (illustrative only — assess the real story):

<decomposition_assessment>
atomic: false
slices:
  - id: 1
    title: "Add the parser and its data contract"
    scope: "The pure-data types and the strict parser. Excludes every call site."
    depends_on: []
    covers_criteria: [1, 2]
  - id: 2
    title: "Invoke the parser at the gate boundary"
    scope: "Only the call site and its failure handling; the contract is fixed by slice 1."
    depends_on: [1]
    covers_criteria: [3]
unsettled:
  - "Whether criterion 3 belongs with slice 2 or its own slice — the coupling \
through the audit record was not resolved."
</decomposition_assessment>
"""
