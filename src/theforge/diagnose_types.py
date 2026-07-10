"""Pure-data types for the ``forge diagnose`` flow.

stdlib-only imports; no coordinator or runner dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class DiagnosePhase(Enum):
    """State machine phases for a diagnose run.

    Distinct from coordinator.state.Phase — the diagnose flow does not run
    plan/dev/review. Phases are linear: each step either advances or fails.
    """

    INIT = auto()
    FETCH = auto()  # fetch issue body / inputs
    INVESTIGATE = auto()  # run the investigative agent
    PARSE = auto()  # parse agent output into a structured artifact
    LAND = auto()  # publish artifact to comment / body / file
    DONE = auto()
    FAILED = auto()
    TIMEOUT_PARTIAL = auto()  # ran out of time/budget; partial artifact returned
    BUDGET_EXCEEDED = auto()


# Output destinations for a completed diagnosis artifact.
DIAGNOSE_OUTPUT_DESTINATIONS: frozenset[str] = frozenset({"comment", "body_section", "pr_to_body"})


@dataclass(frozen=True)
class Hypothesis:
    """A hypothesis tested during diagnosis."""

    statement: str
    status: str  # "ruled_out" | "confirmed" | "inconclusive"
    evidence: str = ""


@dataclass(frozen=True)
class DiagnosisArtifact:
    """Structured diagnosis output from an investigative agent.

    All fields required by the diagnose AC: observed symptom, reproduction or
    evidence, hypotheses tested and ruled out, confirmed cause, affected code
    path, and fix-success criterion.
    """

    issue_number: int
    observed_symptom: str
    reproduction_or_evidence: str
    hypotheses: tuple[Hypothesis, ...]
    confirmed_cause: str
    affected_code_path: str
    fix_success_criterion: str
    partial: bool = False  # True when the agent ran out of time/budget
    notes: str = ""

    def is_complete(self) -> bool:
        """Return True only when every required field is non-empty."""
        return bool(
            self.observed_symptom.strip()
            and self.reproduction_or_evidence.strip()
            and self.hypotheses
            and self.confirmed_cause.strip()
            and self.affected_code_path.strip()
            and self.fix_success_criterion.strip()
        )

    def has_substantive_content(self) -> bool:
        """Return True when at least one required diagnosis field carries content.

        Distinct from :meth:`is_complete`, which requires *every* field.  An
        artifact with no substantive content is a failure to diagnose — not a
        partial diagnosis — and must never be landed into operator-visible
        state, because the only thing such a section would carry is its own
        headings (structurally complete but content-empty).  ``notes`` is
        deliberately excluded: an investigation that produced only a caveat and
        no diagnosis content has still diagnosed nothing.
        """
        return bool(
            self.observed_symptom.strip()
            or self.reproduction_or_evidence.strip()
            or self.hypotheses
            or self.confirmed_cause.strip()
            or self.affected_code_path.strip()
            or self.fix_success_criterion.strip()
        )


@dataclass
class DiagnoseState:
    """Mutable state for a single diagnose run.

    Tracks phase transitions, agent invocation results, costs, and any partial
    artifact produced before a budget/timeout exit. Used by the audit writer.
    """

    issue_number: int
    phase: DiagnosePhase = DiagnosePhase.INIT
    run_id: str = ""
    started_at: str | None = None
    issue_title: str = ""
    issue_body: str = ""
    agent_output: str = ""
    agent_cost_usd: float = 0.0
    agent_duration_s: float = 0.0
    artifact: DiagnosisArtifact | None = None
    landing_destination: str | None = None
    landed_location: str | None = None  # URL / path / comment id
    error: str | None = None
    phase_transitions: list[tuple[str, str]] = field(default_factory=list)
    # (phase_name, ISO timestamp) entries appended on every transition.
    sub_investigations: list[dict] = field(default_factory=list)
    # Optional log of focused sub-investigations spawned during diagnosis.

    def transition(self, new_phase: DiagnosePhase, when: str) -> None:
        self.phase = new_phase
        self.phase_transitions.append((new_phase.name, when))


@dataclass
class DiagnoseResult:
    """Final result of a diagnose run."""

    success: bool
    state: DiagnoseState
    message: str


# ── Markdown rendering ────────────────────────────────────────────────


def render_artifact_markdown(artifact: DiagnosisArtifact) -> str:
    """Render a DiagnosisArtifact as a Markdown ``## Diagnosis`` section.

    Output format intentionally matches the headings expected by the shape gate
    (DIAGNOSIS_HEADING_PATTERN / DIAGNOSIS_REQUIRED_COMPONENTS in shape_check)
    so a landed artifact makes the issue fix-ready.

    Raises ``ValueError`` when the artifact carries no substantive content.
    Rendering an all-empty artifact would emit a Diagnosis section whose only
    content is its own headings — structurally complete but content-empty — and
    such scaffolding must never reach operator-visible state where a downstream
    readiness check could be satisfied by the headings alone.
    """
    if not artifact.has_substantive_content():
        raise ValueError(
            "refusing to render a Diagnosis section for an all-empty artifact: "
            "no substantive content to land"
        )
    lines: list[str] = ["## Diagnosis"]
    if artifact.partial:
        lines.append("")
        lines.append(
            "> ⚠ Partial diagnosis — the investigation hit its budget or "
            "timeout before reaching a confirmed cause. Operator review required."
        )
    lines.extend(
        [
            "",
            "### Observed symptom",
            "",
            artifact.observed_symptom.strip() or "_(empty)_",
            "",
            "### Reproduction / evidence",
            "",
            artifact.reproduction_or_evidence.strip() or "_(empty)_",
            "",
            "### Hypotheses tested",
            "",
        ]
    )
    if artifact.hypotheses:
        for h in artifact.hypotheses:
            # Underscore→space so the rendered section contains the literal
            # tokens the sprint shape gate scans for ("ruled out" etc.).
            display_status = h.status.replace("_", " ")
            lines.append(f"- **[{display_status}]** {h.statement.strip()}")
            if h.evidence.strip():
                lines.append(f"  - Evidence: {h.evidence.strip()}")
    else:
        lines.append("_(none recorded)_")
    lines.extend(
        [
            "",
            "### Confirmed cause",
            "",
            artifact.confirmed_cause.strip() or "_(empty)_",
            "",
            "### Affected code path",
            "",
            artifact.affected_code_path.strip() or "_(empty)_",
            "",
            "### Fix-success criterion",
            "",
            artifact.fix_success_criterion.strip() or "_(empty)_",
            "",
        ]
    )
    if artifact.notes.strip():
        lines.extend(["### Notes", "", artifact.notes.strip(), ""])
    return "\n".join(lines)


def upsert_diagnosis_section(body: str, section_markdown: str) -> str:
    """Insert or replace a ``## Diagnosis`` section in an issue body.

    If a ``## Diagnosis`` heading exists, replace from that heading up to (but
    not including) the next ``## `` heading or end-of-body.  If absent, append
    the section to the end of the body, separated by a blank line.
    """
    section = section_markdown.rstrip() + "\n"
    if not body.strip():
        return section
    lines = body.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower() == "## diagnosis":
            start = i
            break
    if start is None:
        sep = "" if body.endswith("\n") else "\n"
        return body + sep + "\n" + section
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    new_lines = lines[:start] + [section.rstrip()] + lines[end:]
    return "\n".join(new_lines).rstrip() + "\n"
