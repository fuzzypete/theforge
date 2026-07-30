"""Pure-data types for the ``forge diagnose`` flow.

stdlib-only imports; no coordinator or runner dependencies.
"""

from __future__ import annotations

import re
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
    VERIFY_PREMISE = auto()  # confirm the cited code/symptom still exists in baseline
    LAND = auto()  # publish artifact to comment / body / file
    DONE = auto()
    FAILED = auto()
    TIMEOUT_PARTIAL = auto()  # ran out of time/budget; partial artifact returned
    BUDGET_EXCEEDED = auto()
    ALREADY_RESOLVED = auto()  # premise absent from baseline; no diagnosis written


# Output destinations for a completed diagnosis artifact.
DIAGNOSE_OUTPUT_DESTINATIONS: frozenset[str] = frozenset({"comment", "body_section", "pr_to_body"})


@dataclass(frozen=True)
class Hypothesis:
    """A hypothesis tested during diagnosis."""

    statement: str
    status: str  # "ruled_out" | "confirmed" | "inconclusive"
    evidence: str = ""


@dataclass(frozen=True)
class InspectedFile:
    """A file the diagnosis agent inspected, with its content hash at baseline.

    ``content_sha256`` is the hex digest of the file's bytes at
    ``baseline_sha`` — the staleness check compares this to a fresh hash of
    the same path against the current base to detect material drift.
    Untracked or missing-at-baseline files carry an empty ``content_sha256``;
    they cannot be used for staleness comparison and the groom-side check
    treats them as informational only.
    """

    path: str
    content_sha256: str = ""


@dataclass(frozen=True)
class PremiseAnchor:
    """A falsifiable premise the reported bug depends on.

    ``pattern`` is a literal substring the agent expects to find in ``file``
    at the diagnosis baseline — a function signature, a code line, an error
    string — that constitutes the concrete premise of the bug.  The coordinator
    verifies these mechanically (no LLM judgment): if the file is gone or the
    pattern is absent at the baseline, the described symptom cannot reproduce
    against code that no longer exists, and the run reports "already resolved"
    instead of manufacturing a confirmed cause.  An empty ``pattern`` anchors
    only on the file's continued existence.
    """

    file: str
    pattern: str = ""


@dataclass(frozen=True)
class RelatedFinding:
    """A real defect noticed in adjacent code that is *not* the cause of the
    issue's stated symptom.

    The diagnose flow must scope its ``confirmed_cause`` to the symptom of the
    issue under investigation.  When the agent notices a separate, genuine
    problem in nearby code — one that belongs to a different issue or domain —
    it records it here instead of folding it into ``confirmed_cause``.  This
    keeps the diagnosis boundary aligned with the issue boundary so a
    downstream dev does not implement an adjacent problem as part of this
    issue's fix (the #1672 scope-creep failure mode).

    ``summary`` is a one-line description of the adjacent defect.  ``related``
    is an optional pointer to the owning/related issue (e.g. ``"#1649"``) or
    domain so the finding can be triaged separately rather than built here.
    """

    summary: str
    related: str = ""


@dataclass(frozen=True)
class AbsentPremise:
    """A cited premise reference that no longer exists in the baseline.

    Carries the commit that removed it so the "already resolved" report can
    name it, per the diagnose AC.  ``pattern`` is empty when the whole file
    was removed (as opposed to a specific pattern removed from a file that
    still exists).
    """

    file: str
    pattern: str
    removing_commit: str
    removing_summary: str = ""


@dataclass(frozen=True)
class PremiseVerdict:
    """Result of checking a diagnosis's cited code against the baseline."""

    resolved: bool
    absent: tuple[AbsentPremise, ...] = ()


@dataclass(frozen=True)
class DiagnosisArtifact:
    """Structured diagnosis output from an investigative agent.

    All fields required by the diagnose AC: observed symptom, reproduction or
    evidence, hypotheses tested, confirmed cause, affected code path, and
    fix-success criterion.
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
    baseline_sha: str = ""
    baseline_captured_at: str = ""
    inspected_files: tuple[InspectedFile, ...] = ()
    premise_anchors: tuple[PremiseAnchor, ...] = ()
    # Adjacent-but-unrelated defects the agent noticed in nearby code. These
    # are surfaced as separate linked findings and MUST NOT be folded into
    # confirmed_cause — they are not the cause of this issue's stated symptom.
    related_findings: tuple[RelatedFinding, ...] = ()

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

        A hypotheses tuple counts only when at least one entry carries real
        content (a non-blank statement or evidence).  The parser turns an empty
        YAML entry such as ``hypotheses: [{}]`` into ``Hypothesis(statement='',
        status='inconclusive', evidence='')``; a tuple of such blank bullets is
        scaffolding, not investigative content, and must not clear the floor.
        """
        has_real_hypothesis = any(
            h.statement.strip() or h.evidence.strip() for h in self.hypotheses
        )
        return bool(
            self.observed_symptom.strip()
            or self.reproduction_or_evidence.strip()
            or has_real_hypothesis
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
    # None means the agent's cost was unmeasured (e.g. run killed before the
    # cost-bearing result event and no usage was reconstructable). Distinct from
    # 0.0, which means a genuinely free run. Never coerce one into the other.
    agent_cost_usd: float | None = 0.0
    agent_duration_s: float = 0.0
    artifact: DiagnosisArtifact | None = None
    landing_destination: str | None = None
    landed_location: str | None = None  # URL / path / comment id
    error: str | None = None
    baseline_sha: str = ""
    baseline_captured_at: str = ""
    phase_transitions: list[tuple[str, str]] = field(default_factory=list)
    # (phase_name, ISO timestamp) entries appended on every transition.
    sub_investigations: list[dict] = field(default_factory=list)
    # Optional log of focused sub-investigations spawned during diagnosis.
    already_resolved: bool = False
    absent_premises: tuple[AbsentPremise, ...] = ()
    # Set when the premise check finds the cited code was removed from baseline.
    starting_evidence_labels: list[str] = field(default_factory=list)
    # Short labels for each excerpt auto-loaded from issue-body references and
    # injected into the prompt as STARTING EVIDENCE. Empty when the body cited
    # nothing recognizable. Recorded so the audit shows exactly what the
    # orchestrator handed the agent (convention: instrument cross-phase data).
    starting_evidence_chars: int = 0
    # Machine-readable failure identifier returned by the runner (e.g.
    # "timeout"). None when the run did not fail or the runner reported no
    # code. Recorded so the audit trail distinguishes a timeout from a crash.
    agent_failure_code: str | None = None
    # Followable-log slug for this run (``diagnose-<issue>``). Set at run
    # registration so status/logs can resolve the per-run log file.
    run_slug: str = ""
    # Sidecar holding the agent's COMPLETE output, written before the first parse
    # attempt. The audit's ``raw_output_tail`` is a bounded display convenience;
    # this is the recoverable copy of a paid-for investigation. Empty when there
    # was no output to persist, or when the write failed (see raw_output_error).
    raw_output_path: str = ""
    raw_output_chars: int = 0
    raw_output_sha256: str = ""
    raw_output_error: str = ""
    # Last-resort carrier for the complete output when no file location accepted
    # the write: the audit record embeds it verbatim as ``agent.raw_output`` rather
    # than degrading to the bounded tail. Empty whenever a file holds the content.
    raw_output_inline: str = ""
    # Every sidecar persisted by this run, in write order: the investigation's own
    # output first, then one per reformat parse retry. A retry never overwrites an
    # earlier emission — the original is the evidence separating an agent
    # serialization defect from a parser defect.
    raw_output_paths: list[str] = field(default_factory=list)
    # One entry per reformat-only parse-retry invocation: attempt number, the
    # parse error that triggered it, cost, duration, and whether it produced
    # parseable YAML. Empty when the first parse succeeded.
    parse_retries: list[dict] = field(default_factory=list)

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
    (DIAGNOSIS_HEADING_PATTERN / required Diagnosis labels in shape_check)
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
    if artifact.baseline_sha:
        lines.append("")
        ts = artifact.baseline_captured_at or "unknown"
        lines.append(f"**Baseline:** `{artifact.baseline_sha}` captured at `{ts}`")
    if artifact.inspected_files:
        lines.append("")
        lines.append("**Inspected files:**")
        lines.append("")
        for f in artifact.inspected_files:
            digest = f.content_sha256 or "unknown"
            lines.append(f"- `{f.path}` — `sha256:{digest}`")
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
            # Underscore→space so status values render as readable prose.
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
    if artifact.related_findings:
        lines.extend(
            [
                "### Related findings (out of scope)",
                "",
                (
                    "> These are adjacent problems noticed during investigation. "
                    "They are **not** the cause of this issue's symptom and are "
                    "**out of scope for this fix** — triage them separately under "
                    "the linked issue. Do not implement them as part of this issue."
                ),
                "",
            ]
        )
        for finding in artifact.related_findings:
            summary = finding.summary.strip()
            ref = finding.related.strip()
            if ref:
                lines.append(f"- {summary} (related: {ref})")
            else:
                lines.append(f"- {summary}")
        lines.append("")
    if artifact.notes.strip():
        lines.extend(["### Notes", "", artifact.notes.strip(), ""])
    return "\n".join(lines)


def render_already_resolved_markdown(
    *, issue_number: int, baseline_sha: str, absent: tuple[AbsentPremise, ...]
) -> str:
    """Render an "appears already resolved" report naming the removing commit(s).

    Deliberately NOT a ``## Diagnosis`` section: the heading omits the word
    "diagnosis" so a downstream shape/readiness check does not mistake this
    note for a fix-ready confirmed-cause diagnosis.  The premise is gone, so
    the issue is not fix-ready — it needs restating, not implementing.
    """
    sha_short = baseline_sha[:12] if baseline_sha else "unknown"
    lines: list[str] = [
        "## Premise check — appears already resolved",
        "",
        (
            f"`forge diagnose` checked issue #{issue_number} against baseline "
            f"`{sha_short}` and found that code the bug's premise depends on no "
            "longer exists. No confirmed-cause diagnosis was written: the described "
            "symptom cannot reproduce against code that has been removed."
        ),
        "",
        "**Absent premise references:**",
        "",
    ]
    for a in absent:
        commit = a.removing_commit[:12] if a.removing_commit else "unknown"
        summary = f" ({a.removing_summary})" if a.removing_summary else ""
        if a.pattern:
            lines.append(
                f"- `{a.file}` — pattern `{a.pattern}` removed by commit `{commit}`{summary}"
            )
        else:
            lines.append(f"- `{a.file}` — file removed by commit `{commit}`{summary}")
    lines.extend(
        [
            "",
            "If this issue is still valid, restate its premise against the current "
            "baseline before re-running `forge diagnose`.",
        ]
    )
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


# ── Baseline metadata extraction ──────────────────────────────────────


_BASELINE_LINE_RE = re.compile(
    r"\*\*Baseline:\*\*\s*`([^`]+)`\s*captured at\s*`([^`]+)`",
    re.IGNORECASE,
)
_INSPECTED_HEADER_RE = re.compile(r"\*\*Inspected files:\*\*", re.IGNORECASE)
_INSPECTED_BULLET_RE = re.compile(
    r"^\s*-\s*`([^`]+)`\s*(?:—|--)\s*`sha256:([0-9a-fA-F]*|unknown)`\s*$"
)


@dataclass(frozen=True)
class BaselineMetadata:
    """Baseline metadata parsed from a rendered diagnosis section."""

    baseline_sha: str
    baseline_captured_at: str
    inspected_files: tuple[InspectedFile, ...]


def parse_baseline_metadata(body: str) -> BaselineMetadata | None:
    """Parse baseline + inspected-file metadata out of an issue body.

    Returns ``None`` when no ``**Baseline:**`` line is present anywhere in
    the body (a diagnosis written before this baseline-anchoring feature
    shipped). Returns a :class:`BaselineMetadata` with possibly-empty
    ``inspected_files`` when a baseline line is present but the inspected
    block is absent or unparseable.
    """
    m = _BASELINE_LINE_RE.search(body)
    if not m:
        return None
    sha = m.group(1).strip()
    ts = m.group(2).strip()

    files: list[InspectedFile] = []
    header_match = _INSPECTED_HEADER_RE.search(body)
    if header_match:
        tail = body[header_match.end() :]
        for line in tail.splitlines():
            stripped = line.strip()
            if not stripped:
                if files:
                    # blank line after at least one bullet ends the block
                    break
                continue
            if stripped.startswith("##") or stripped.startswith("###"):
                break
            bm = _INSPECTED_BULLET_RE.match(line)
            if bm:
                digest = bm.group(2).strip()
                if digest.lower() == "unknown":
                    digest = ""
                files.append(InspectedFile(path=bm.group(1).strip(), content_sha256=digest))
            elif files:
                # non-bullet, non-blank after bullets started — block ended
                break
    return BaselineMetadata(
        baseline_sha=sha,
        baseline_captured_at=ts,
        inspected_files=tuple(files),
    )
