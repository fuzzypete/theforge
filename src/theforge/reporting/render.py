"""Render an observed run's evidence into a target-repository bug report.

Two surfaces come out of here: the issue *body* — which carries the operator's
description, a bug-shaped Diagnosis section, and a manifest naming every
artifact captured and every one that could not be — and the evidence *payload*,
a deterministic sequence of comment-sized chunks carrying the artifacts
themselves so nothing has to be copied between checkouts by hand.

The manifest states a publication state on its face. A report is only ever
described as complete once every expected chunk has actually been posted; until
then it says so, and names what is still outstanding.
"""

from __future__ import annotations

from dataclasses import dataclass

from theforge.reporting.evidence import (
    EVIDENCE_KINDS,
    EvidenceArtifact,
    MissingEvidence,
    RunEvidence,
)

# GitHub caps a comment body at 65536 characters. The budget below leaves room
# for the chunk header and code fence on top of the artifact slice.
GITHUB_COMMENT_LIMIT = 65_536
DEFAULT_CHUNK_CHARS = 56_000
DEFAULT_MAX_CHUNKS = 40

_KIND_ORDER = {kind: i for i, (kind, _) in enumerate(EVIDENCE_KINDS)}


@dataclass(frozen=True)
class Diagnosis:
    """Operator-supplied diagnosis content.

    Every field defaults to an explicit non-assertion. A report filed the
    moment a defect is seen genuinely has no confirmed cause, and saying so is
    a state the shape gate recognizes — inventing one is not.
    """

    symptom: str
    cause: str = "unknown — not yet identified; filed from the observing project."
    code_path: str = "unknown — not yet identified; see the attached run record."
    fix_criterion: str = ""


@dataclass(frozen=True)
class EvidenceChunk:
    """One comment-sized slice of one artifact."""

    label: str
    body: str
    artifact_name: str
    kind: str


@dataclass(frozen=True)
class Publication:
    """Publication state of the evidence payload, as stated in the body."""

    expected: tuple[str, ...] = ()
    posted: tuple[str, ...] = ()
    started: bool = False

    @property
    def outstanding(self) -> tuple[str, ...]:
        posted = set(self.posted)
        return tuple(label for label in self.expected if label not in posted)

    @property
    def state(self) -> str:
        if not self.started:
            return "pending"
        if self.outstanding:
            return "incomplete"
        return "complete"


def default_title(evidence: RunEvidence, summary: str) -> str:
    """Title naming what was seen and where, not the run id alone."""
    where = evidence.observed_project or "a consuming project"
    text = summary.strip().splitlines()[0].strip() if summary.strip() else ""
    if not text:
        text = f"forge defect observed in run {evidence.run_id}"
    return f"{text} (observed in {where}, run {evidence.run_id})"


# ── evidence payload ──────────────────────────────────────────────────────────


def build_evidence_chunks(
    evidence: RunEvidence,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> tuple[tuple[EvidenceChunk, ...], tuple[EvidenceArtifact, ...]]:
    """Slice every captured artifact into comment-sized chunks.

    Returns the chunks plus the artifacts that did not fit inside
    ``max_chunks``. Dropped artifacts are returned rather than silently
    skipped so the caller can name them in the manifest — a payload that
    quietly truncates reads as a complete record when it is not.
    """
    ordered = sorted(evidence.artifacts, key=lambda a: (_KIND_ORDER.get(a.kind, 99), a.name))
    chunks: list[EvidenceChunk] = []
    dropped: list[EvidenceArtifact] = []
    for artifact in ordered:
        pieces = _split(artifact.content, chunk_chars)
        if dropped or len(chunks) + len(pieces) > max_chunks:
            dropped.append(artifact)
            continue
        for index, piece in enumerate(pieces, start=1):
            label = _chunk_label(artifact, index, len(pieces))
            chunks.append(
                EvidenceChunk(
                    label=label,
                    body=_chunk_body(artifact, label, piece),
                    artifact_name=artifact.name,
                    kind=artifact.kind,
                )
            )
    return tuple(chunks), tuple(dropped)


def dropped_as_missing(
    dropped: tuple[EvidenceArtifact, ...], max_chunks: int
) -> list[MissingEvidence]:
    """Name payload-budget drops as missing evidence rather than losing them."""
    return [
        MissingEvidence(
            kind=artifact.kind,
            name=artifact.name,
            reason=(
                f"captured but not attached: the evidence payload budget of "
                f"{max_chunks} comments was reached"
            ),
        )
        for artifact in dropped
    ]


def _chunk_label(artifact: EvidenceArtifact, index: int, total: int) -> str:
    base = f"{artifact.label} — {artifact.name}"
    if total > 1:
        return f"{base} (part {index} of {total})"
    return base


def _chunk_body(artifact: EvidenceArtifact, label: str, piece: str) -> str:
    fence = _fence_for(piece)
    header = f"**Evidence — {label}**"
    if artifact.truncated_from is not None:
        header += (
            f"\n\n_Captured truncated: the source artifact was "
            f"{artifact.truncated_from} characters._"
        )
    return f"{header}\n\n{fence}\n{piece}\n{fence}\n"


def _fence_for(text: str) -> str:
    longest = 0
    current = 0
    for char in text:
        if char == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return "`" * max(3, longest + 1)


def _split(text: str, size: int) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + size] for i in range(0, len(text), size)]


# ── issue body ────────────────────────────────────────────────────────────────


def render_issue_body(
    evidence: RunEvidence,
    *,
    description: str,
    diagnosis: Diagnosis,
    publication: Publication,
) -> str:
    lines: list[str] = []
    lines.append("## Observed")
    lines.append("")
    lines.append(description.strip())
    lines.append("")
    lines.append("## Expected")
    lines.append("")
    lines.append(_expected_behavior(description, diagnosis))
    lines.append("")
    lines.append(
        "Filed with `forge report` from the project where the behavior was observed. "
        "The evidence below was captured there and travels with this report; it "
        "describes that run, not the checkout this issue is read on."
    )
    lines.append("")
    lines.append("## Diagnosis")
    lines.append("")
    lines.append(f"- **Observed symptom:** {_one_line(diagnosis.symptom)}")
    lines.append(f"- **Evidence:** {_evidence_bullet(evidence)}")
    lines.append(f"- **Confirmed cause:** {_one_line(diagnosis.cause)}")
    lines.append(f"- **Affected code path:** {_one_line(diagnosis.code_path)}")
    lines.append(f"- **Fix-success criterion:** {_one_line(_fix_criterion(evidence, diagnosis))}")
    lines.append("")
    lines.append("## Evidence (captured from observing project)")
    lines.append("")
    lines.append("```")
    lines.extend(manifest_lines(evidence, publication))
    lines.append("```")
    lines.append("")
    if evidence.missing:
        lines.append("### Missing evidence")
        lines.append("")
        lines.append(
            "This report is incomplete by exactly these parts — each was looked for "
            "in the observing project and not found:"
        )
        lines.append("")
        for entry in evidence.missing:
            lines.append(f"- {entry.display}")
        lines.append("")
    lines.append("### Evidence payload")
    lines.append("")
    if publication.expected:
        lines.append(
            "Each item below is attached to this issue as its own comment, in this order:"
        )
        lines.append("")
        posted = set(publication.posted)
        for label in publication.expected:
            if not publication.started:
                mark = "pending"
            else:
                mark = "attached" if label in posted else "NOT ATTACHED"
            lines.append(f"- `{label}` — {mark}")
    else:
        lines.append("No artifacts were captured, so this report carries no payload.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _expected_behavior(description: str, diagnosis: Diagnosis) -> str:
    if diagnosis.fix_criterion.strip():
        return _one_line(diagnosis.fix_criterion)
    if diagnosis.symptom.strip():
        return "The reported bug should not occur in the target repository."
    return _one_line(description) or "The reported behavior should not occur."


def manifest_lines(evidence: RunEvidence, publication: Publication) -> list[str]:
    available = ", ".join(evidence.available_labels()) or "none"
    missing = ", ".join(_missing_summary(evidence)) or "none"
    run_line = evidence.run_id
    if evidence.sprint_name:
        run_line += f"  (sprint {evidence.sprint_name})"
    if evidence.story_slugs:
        run_line += f"  stories: {', '.join(evidence.story_slugs)}"
    return [
        f"forge version : {evidence.forge_version or 'unrecorded in this run'}",
        f"observed in   : {evidence.observed_project or 'unrecorded (no git origin)'}",
        f"run           : {run_line}",
        f"config        : {evidence.config_summary}",
        f"artifacts     : {available}",
        f"missing       : {missing}",
        f"publication   : {_publication_line(publication)}",
    ]


def _publication_line(publication: Publication) -> str:
    total = len(publication.expected)
    if total == 0:
        return "no evidence payload to attach"
    posted = len(publication.posted)
    if publication.state == "pending":
        return (
            f"INCOMPLETE — 0 of {total} evidence comments attached; "
            "this report is not complete until every item below is attached"
        )
    if publication.state == "complete":
        return f"complete — {posted} of {total} evidence comments attached"
    outstanding = ", ".join(publication.outstanding)
    return (
        f"INCOMPLETE — {posted} of {total} evidence comments attached; "
        f"never attached: {outstanding}"
    )


def _missing_summary(evidence: RunEvidence) -> list[str]:
    seen: dict[str, None] = {}
    for entry in evidence.missing:
        seen.setdefault(entry.label, None)
    return list(seen)


def _evidence_bullet(evidence: RunEvidence) -> str:
    where = evidence.observed_project or "the observing project"
    return (
        f"run `{evidence.run_id}` in {where}, forge "
        f"{evidence.forge_version or 'version unrecorded'} — captured artifacts are "
        "attached to this issue as comments."
    )


def _fix_criterion(evidence: RunEvidence, diagnosis: Diagnosis) -> str:
    if diagnosis.fix_criterion.strip():
        return diagnosis.fix_criterion
    return (
        "the behavior described above no longer occurs for a run equivalent to "
        f"`{evidence.run_id}`."
    )


def _one_line(text: str) -> str:
    return " ".join(text.split()) or "unknown — not stated."


__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_MAX_CHUNKS",
    "GITHUB_COMMENT_LIMIT",
    "Diagnosis",
    "EvidenceChunk",
    "Publication",
    "build_evidence_chunks",
    "default_title",
    "dropped_as_missing",
    "manifest_lines",
    "render_issue_body",
]
