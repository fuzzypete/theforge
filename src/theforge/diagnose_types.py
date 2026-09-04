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
    # Retained for old audit records written before cause/no-cause were split
    # out of it (#2803). No longer assigned by the flow.
    UNCLASSIFIED_PARTIAL = auto()
    CAUSE_FOUND_PARTIAL = auto()  # confirmed_cause populated; other lifecycle fields missing
    NO_CAUSE_FOUND = auto()  # honest no-cause landing; confirmed_cause empty
    ALREADY_RESOLVED = auto()  # premise absent from baseline; no diagnosis written
    DISCARDED = auto()  # operator declined to land; nothing written


class DiagnosePartialReason(Enum):
    """Operator-visible reason a landed partial diagnosis is incomplete."""

    BUDGET_EXCEEDED = "budget_exceeded"
    TIMEOUT = "timeout"
    # Retained so audit records written before cause/no-cause were split out
    # of this value (#2803) still deserialize. No longer assigned by the flow.
    UNCLASSIFIED = "unclassified"
    CAUSE_FOUND_INCOMPLETE = "cause_found_incomplete"
    NO_CAUSE_FOUND = "no_cause_found"
    DISCARDED = "discarded"


# Output destinations for a completed diagnosis artifact.
DIAGNOSE_OUTPUT_DESTINATIONS: frozenset[str] = frozenset({"comment", "body_section", "pr_to_body"})
DIAGNOSE_SUPPORT_SOURCE_TYPES: frozenset[str] = frozenset(
    {"observed", "prior_assertion", "mixed", "unknown"}
)
DIAGNOSE_CLAIM_VERIFICATION_TYPES: frozenset[str] = frozenset(
    {"source", "attached_evidence", "source_and_attached_evidence", "unknown"}
)
DIAGNOSE_HYPOTHESIS_STATUSES: frozenset[str] = frozenset(
    {"ruled_out", "confirmed", "inconclusive", "unverifiable"}
)
_INDEPENDENCE_VOCAB_RE = re.compile(
    r"\b(independent(?:ly)?|corroborat\w*|converg\w*|second source)\b",
    re.IGNORECASE,
)


def _normalize_support_source_type(source_type: str) -> str:
    normalized = source_type.strip().lower()
    if normalized in DIAGNOSE_SUPPORT_SOURCE_TYPES:
        return normalized
    return "unknown"


def _normalize_claim_verification_type(verification_type: str) -> str:
    normalized = verification_type.strip().lower()
    if normalized in DIAGNOSE_CLAIM_VERIFICATION_TYPES:
        return normalized
    return "unknown"


def _normalize_hypothesis_status(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in DIAGNOSE_HYPOTHESIS_STATUSES:
        return normalized
    return "inconclusive"


@dataclass(frozen=True)
class SupportProvenance:
    """Where a piece of diagnosis support came from."""

    source_type: str = "unknown"
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_type", _normalize_support_source_type(self.source_type))
        object.__setattr__(self, "detail", self.detail.strip())

    def is_meaningful(self) -> bool:
        return self.source_type != "unknown" or bool(self.detail)


@dataclass(frozen=True)
class ClaimVerification:
    """Whether a diagnosis claim was checked against source or attached evidence."""

    verification_type: str = "unknown"
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "verification_type",
            _normalize_claim_verification_type(self.verification_type),
        )
        object.__setattr__(self, "detail", self.detail.strip())

    def is_meaningful(self) -> bool:
        return self.verification_type != "unknown" or bool(self.detail)

    def has_recorded_verification_type(self) -> bool:
        return self.verification_type != "unknown"


@dataclass(frozen=True)
class Hypothesis:
    """A hypothesis tested during diagnosis."""

    statement: str
    status: str  # "ruled_out" | "confirmed" | "inconclusive" | "unverifiable"
    evidence: str = ""
    evidence_provenance: SupportProvenance = field(default_factory=SupportProvenance)
    claim_verification: ClaimVerification = field(default_factory=ClaimVerification)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _normalize_hypothesis_status(self.status))


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
class ScopeCoverageLocation:
    """One structurally analogous location examined for categorical scope.

    ``status`` is ``"covered"`` when the location is part of the confirmed
    defect scope and ``"excluded"`` when the agent examined it and found that
    it does not share the same omission / behavior. The rationale is required
    so downstream reviewers can see why the location was included or excluded.
    """

    location: str
    status: str  # "covered" | "excluded"
    rationale: str = ""

    def is_valid(self) -> bool:
        normalized = self.status.strip().lower()
        return bool(
            self.location.strip()
            and self.rationale.strip()
            and normalized in {"covered", "excluded"}
        )


@dataclass(frozen=True)
class SymptomScopeCoverage:
    """Diagnosis-visible coverage record for categorically stated symptoms.

    Non-categorical issues leave this at its default empty record. Categorical
    issues must fill in the stated scope and the examined structurally
    analogous sibling locations so downstream phases can verify that the
    diagnosis did not silently narrow the bug below what the issue claimed.
    """

    symptom_is_categorical: bool = False
    stated_scope: str = ""
    examined_locations: tuple[ScopeCoverageLocation, ...] = ()

    def is_complete(self) -> bool:
        if not self.symptom_is_categorical:
            return True
        return bool(
            self.stated_scope.strip()
            and self.examined_locations
            and all(location.is_valid() for location in self.examined_locations)
        )

    def satisfies_issue_requirement(
        self, *, issue_requires_categorical_scope: bool = False
    ) -> bool:
        """Return whether this record satisfies the issue's stated scope contract."""
        if issue_requires_categorical_scope and not self.symptom_is_categorical:
            return False
        return self.is_complete()


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
    unable_to_check: tuple["UncheckedPremise", ...] = ()


@dataclass(frozen=True)
class UncheckedPremise:
    """A premise the coordinator could not verify against baseline."""

    file: str
    pattern: str
    reason: str


@dataclass(frozen=True)
class DiagnosisArtifact:
    """Structured diagnosis output from an investigative agent.

    All fields required by the diagnose AC: observed symptom, reproduction or
    evidence, hypotheses tested, confirmed cause, affected code path, and
    fix-success criterion. Advisory repair guesses live separately from the
    confirmed-localization fields so speculative implementation advice does not
    read like established diagnosis.
    """

    issue_number: int
    observed_symptom: str
    reproduction_or_evidence: str
    hypotheses: tuple[Hypothesis, ...]
    confirmed_cause: str
    affected_code_path: str
    fix_success_criterion: str
    partial: bool = False  # True when the agent ran out of time/budget
    partial_reason: DiagnosePartialReason = DiagnosePartialReason.UNCLASSIFIED
    # Unverified implementation advice or fix-location guesses. This is
    # intentionally separate from confirmed_cause / affected_code_path /
    # fix_success_criterion so a dev can distinguish verified localization from
    # advisory repair speculation at a glance.
    advisory_repair_proposal: str = ""
    notes: str = ""
    baseline_sha: str = ""
    baseline_captured_at: str = ""
    inspected_files: tuple[InspectedFile, ...] = ()
    premise_anchors: tuple[PremiseAnchor, ...] = ()
    # Adjacent-but-unrelated defects the agent noticed in nearby code. These
    # are surfaced as separate linked findings and MUST NOT be folded into
    # confirmed_cause — they are not the cause of this issue's stated symptom.
    related_findings: tuple[RelatedFinding, ...] = ()
    # Audit-visible coverage record for categorically stated symptoms. This is
    # descriptive only: the coordinator does not route on it, but diagnose
    # completeness can require it when the fetched issue text states
    # categorical scope so a fix-ready diagnosis cannot silently under-scope
    # the bug by omitting or negating the record.
    symptom_scope_coverage: SymptomScopeCoverage = field(default_factory=SymptomScopeCoverage)
    # Optional support text for the confirmed cause, separate from the cause
    # statement itself so corroboration/restatement provenance can be rendered
    # explicitly.
    confirmed_cause_support: str = ""
    confirmed_cause_support_provenance: SupportProvenance = field(
        default_factory=SupportProvenance
    )
    confirmed_cause_verification: ClaimVerification = field(default_factory=ClaimVerification)
    # Coordinator-populated report of premise anchors it could not verify.
    unchecked_premises: tuple[UncheckedPremise, ...] = ()

    def _substantive_hypothesis_claims_missing_verification(self) -> tuple[str, ...]:
        missing: list[str] = []
        for idx, hypothesis in enumerate(self.hypotheses):
            if not (hypothesis.statement.strip() or hypothesis.evidence.strip()):
                continue
            if not hypothesis.claim_verification.has_recorded_verification_type():
                missing.append(f"hypotheses[{idx}].claim_verification")
        return tuple(missing)

    def _confirmed_cause_missing_verification(self) -> tuple[str, ...]:
        if (
            self.confirmed_cause.strip()
            and not self.confirmed_cause_verification.has_recorded_verification_type()
        ):
            return ("confirmed_cause_verification",)
        return ()

    def missing_required_fields(
        self, *, issue_requires_categorical_scope: bool = False
    ) -> tuple[str, ...]:
        """Return the required diagnosis fields still missing from this artifact."""
        missing: list[str] = []
        if not self.observed_symptom.strip():
            missing.append("observed_symptom")
        if not self.reproduction_or_evidence.strip():
            missing.append("reproduction_or_evidence")
        if not self.hypotheses:
            missing.append("hypotheses")
        if not self.confirmed_cause.strip():
            missing.append("confirmed_cause")
        if not self.affected_code_path.strip():
            missing.append("affected_code_path")
        if not self.fix_success_criterion.strip():
            missing.append("fix_success_criterion")
        missing.extend(self._substantive_hypothesis_claims_missing_verification())
        missing.extend(self._confirmed_cause_missing_verification())
        if not self.symptom_scope_coverage.satisfies_issue_requirement(
            issue_requires_categorical_scope=issue_requires_categorical_scope
        ):
            missing.append("symptom_scope_coverage")
        return tuple(missing)

    def is_complete(self, *, issue_requires_categorical_scope: bool = False) -> bool:
        """Return True only when every required field is non-empty."""
        return not self.missing_required_fields(
            issue_requires_categorical_scope=issue_requires_categorical_scope
        )

    @staticmethod
    def _is_nonblocking_missing_field(name: str) -> bool:
        """True when a strict-schema gap should stay audit-visible, not block landing.

        These fields remain part of :meth:`missing_required_fields` so malformed
        or under-documented artifacts are still inspectable. They are excluded
        from lifecycle blocking because the landed Diagnosis section shape gate
        either cannot see them at all (hypotheses, symptom scope coverage,
        verification metadata) or evaluates only the rendered label presence
        rather than the artifact-level schema completeness.
        """
        return name in {
            "hypotheses",
            "symptom_scope_coverage",
            "confirmed_cause_verification",
        } or (name.startswith("hypotheses[") and name.endswith("].claim_verification"))

    def lifecycle_blocking_missing_fields(
        self, *, issue_requires_categorical_scope: bool = False
    ) -> tuple[str, ...]:
        """Missing required fields that make this diagnosis substantively partial.

        A subset of :meth:`missing_required_fields`, which stays the strict
        schema/audit signal.  This one answers the narrower lifecycle question:
        does what is missing stop the rendered diagnosis from being the
        fix-ready artifact it looks like?  Only fields that remain lifecycle-
        blocking in the coordinator flow belong here; strict-schema gaps the
        gate cannot see stay inspectable via :meth:`nonblocking_missing_fields`
        instead of forcing a partial landing.
        """
        return tuple(
            name
            for name in self.missing_required_fields(
                issue_requires_categorical_scope=issue_requires_categorical_scope
            )
            if not self._is_nonblocking_missing_field(name)
        )

    def nonblocking_missing_fields(
        self, *, issue_requires_categorical_scope: bool = False
    ) -> tuple[str, ...]:
        """Strict-schema gaps that remain visible after a successful landing.

        Recorded in the audit so landing a gate-conforming diagnosis as
        runnable stays inspectable after the run succeeds.
        """
        return tuple(
            name
            for name in self.missing_required_fields(
                issue_requires_categorical_scope=issue_requires_categorical_scope
            )
            if self._is_nonblocking_missing_field(name)
        )

    def has_substantive_content(self) -> bool:
        """Return True when at least one required diagnosis field carries content.

        Distinct from :meth:`is_complete`, which requires *every* field.  An
        artifact with no substantive content is a failure to diagnose — not a
        partial diagnosis — and must never be landed into operator-visible
        state, because the only thing such a section would carry is its own
        headings (structurally complete but content-empty).  ``notes`` and the
        advisory repair proposal are deliberately excluded: an investigation
        that produced only caveats or a speculative fix guess and no diagnosis
        content has still diagnosed nothing.

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
    agent_reported_success: bool = False
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
    unchecked_premises: tuple[UncheckedPremise, ...] = ()
    # Set when premise verification could not check cited anchors/patterns.
    missing_metadata_fields: tuple[str, ...] = ()
    # Required-field names the artifact left unrecorded that do NOT force a
    # partial landing. This includes verification metadata plus other strict-
    # schema diagnosis gaps the rendered-body gate cannot see, so a successful
    # landing still leaves the coordinator's stricter artifact audit visible.
    starting_evidence_labels: list[str] = field(default_factory=list)
    # Short labels for each excerpt auto-loaded from issue-body references and
    # injected into the prompt as STARTING EVIDENCE. Empty when the body cited
    # nothing recognizable. Recorded so the audit shows exactly what the
    # orchestrator handed the agent (convention: instrument cross-phase data).
    starting_evidence_chars: int = 0
    starting_evidence_declined: list[str] = field(default_factory=list)
    # Namespace-free "#NNNN" references the body cited that were deliberately
    # not resolved (a bare number names no repository, so resolving it against
    # this checkout would inject same-numbered content from the wrong project).
    # Recorded so the audit distinguishes "declined on purpose" from "found
    # nothing".
    # ── Attached evidence (issue filed by ``forge report`` elsewhere) ──
    # Set when the issue carried an observed run's evidence with it. When
    # ``attached_evidence_source`` is non-empty the packet is the ONLY
    # description of the observed run the agent was given: no local pre-load
    # ran, no baseline SHA was stamped, and no premise was verified against this
    # checkout's git history. Recorded so an operator reading the audit can tell
    # which runtime a diagnosis actually describes, and can spot a diagnosis
    # that cited local state anyway.
    attached_evidence_source: str = ""
    attached_evidence_run_id: str = ""
    attached_evidence_forge_version: str = ""
    attached_evidence_read: list[str] = field(default_factory=list)
    # Every part of the observed record the packet does NOT carry, with the
    # reason (absent from the bundle, never attached, clipped to fit). The
    # diagnosis is expected to report these gaps rather than fill them locally.
    attached_evidence_unreadable: list[str] = field(default_factory=list)
    attached_evidence_chars: int = 0
    issue_scope_is_categorical: bool = False
    issue_scope_text: str = ""
    # Derived from the fetched issue text and recorded in the audit because it
    # influences whether a parsed diagnosis can land as fix-ready.
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


def render_artifact_markdown(
    artifact: DiagnosisArtifact, *, issue_requires_categorical_scope: bool = False
) -> str:
    """Render a DiagnosisArtifact as a Markdown ``## Diagnosis`` section.

    Output format intentionally matches the headings expected by the shape gate
    (DIAGNOSIS_HEADING_PATTERN / required Diagnosis labels in shape_check) so a
    landed artifact is *readable* by the gate. Readable is not the same as
    fix-ready: the confirmed-cause value decides that, and an artifact that
    confirmed no cause lands investigation-ready (#2060).

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
        if artifact.partial_reason is DiagnosePartialReason.BUDGET_EXCEEDED:
            warning = (
                "> ⚠ Partial diagnosis — the investigation exceeded its budget "
                "before reaching a confirmed cause. Operator review required."
            )
        elif artifact.partial_reason is DiagnosePartialReason.TIMEOUT:
            warning = (
                "> ⚠ Partial diagnosis — the investigation timed out before "
                "reaching a confirmed cause. Operator review required."
            )
        elif (
            artifact.partial_reason is DiagnosePartialReason.CAUSE_FOUND_INCOMPLETE
            or artifact.confirmed_cause.strip()
        ):
            warning = (
                "> ⚠ Partial diagnosis — the investigation confirmed a cause, "
                "but the diagnosis is otherwise incomplete. Operator review "
                "required."
            )
        else:
            warning = (
                "> ⚠ Partial diagnosis — the investigation did not reach a "
                "confirmed cause. Operator review required."
            )
        lines.append("")
        lines.append(warning)
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

    def render_provenance_text(provenance: SupportProvenance) -> str:
        if provenance.source_type == "observed":
            text = "observed — directly observed during this investigation."
        elif provenance.source_type == "prior_assertion":
            text = (
                "prior_assertion — cited material already stated this cause; "
                "it is a restatement, not independent corroboration."
            )
        elif provenance.source_type == "mixed":
            text = (
                "mixed — combines direct observations with material that already "
                "stated this cause; the prior-assertion portion is not independent "
                "corroboration."
            )
        else:
            text = (
                "unknown — the diagnosis did not record whether this support was "
                "directly observed or read as a prior assertion."
            )
        if provenance.detail:
            return f"{text} {provenance.detail}"
        return text

    def render_claim_verification_text(verification: ClaimVerification) -> str:
        if verification.verification_type == "source":
            text = "verified against the target repository source."
        elif verification.verification_type == "attached_evidence":
            text = "rests only on attached evidence."
        elif verification.verification_type == "source_and_attached_evidence":
            text = "verified against source and attached evidence."
        else:
            text = "the diagnosis did not record whether this claim was checked against source."
        if verification.detail:
            return f"{text} {verification.detail}"
        return text

    def render_independence_note(
        text: str, provenance: SupportProvenance, *, indent: str = ""
    ) -> list[str]:
        if not _INDEPENDENCE_VOCAB_RE.search(text or ""):
            return []
        if provenance.source_type in {"prior_assertion", "mixed"}:
            note = (
                "This text uses independence/corroboration language, but the cited "
                "material already stated this cause and is not independent corroboration."
            )
        else:
            note = (
                "This text uses independence/corroboration language. Verify the cited "
                "material is a second source rather than a prior assertion."
            )
        return [f"{indent}Independence note: {note}"]

    claim_verifications = [
        h.claim_verification
        for h in artifact.hypotheses
        if h.statement.strip() or h.evidence.strip() or h.claim_verification.is_meaningful()
    ]
    if (
        artifact.confirmed_cause.strip()
        or artifact.confirmed_cause_support.strip()
        or artifact.confirmed_cause_verification.is_meaningful()
    ):
        claim_verifications.append(artifact.confirmed_cause_verification)
    meaningful_claim_types = {
        verification.verification_type
        for verification in claim_verifications
        if verification.verification_type != "unknown"
    }
    show_claim_verification = bool(meaningful_claim_types) and meaningful_claim_types != {"source"}

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
                if show_claim_verification:
                    lines.append(
                        "  - Claim verification: "
                        f"{render_claim_verification_text(h.claim_verification)}"
                    )
                lines.append(
                    f"  - Evidence provenance: {render_provenance_text(h.evidence_provenance)}"
                )
                lines.extend(
                    render_independence_note(h.evidence, h.evidence_provenance, indent="  - ")
                )
            elif show_claim_verification and h.claim_verification.is_meaningful():
                lines.append(
                    "  - Claim verification: "
                    f"{render_claim_verification_text(h.claim_verification)}"
                )
    else:
        lines.append("_(none recorded)_")
    lines.extend(
        [
            "",
            "### Confirmed cause",
            "",
            # An empty cause is the designed honest-refusal outcome (the
            # diagnose prompt asks for `confirmed_cause: ""` when nothing was
            # confirmed), so it is written as an explicit non-assertion rather
            # than a slot placeholder: the readiness derivation reads this
            # value, and a record that no cause was found must never read as a
            # record that one was (#2060).
            artifact.confirmed_cause.strip()
            or "unknown — the investigation did not confirm a cause",
            "",
        ]
    )
    if (
        artifact.confirmed_cause_support.strip()
        or artifact.confirmed_cause_support_provenance.is_meaningful()
        or (show_claim_verification and artifact.confirmed_cause_verification.is_meaningful())
        or _INDEPENDENCE_VOCAB_RE.search(artifact.confirmed_cause or "")
    ):
        if show_claim_verification and artifact.confirmed_cause_verification.is_meaningful():
            lines.append(
                "Claim verification: "
                f"{render_claim_verification_text(artifact.confirmed_cause_verification)}"
            )
        lines.append(f"Support: {artifact.confirmed_cause_support.strip() or '_(none recorded)_'}")
        lines.append(
            "Support provenance: "
            f"{render_provenance_text(artifact.confirmed_cause_support_provenance)}"
        )
        independence_notes = render_independence_note(
            artifact.confirmed_cause,
            artifact.confirmed_cause_support_provenance,
        ) + render_independence_note(
            artifact.confirmed_cause_support,
            artifact.confirmed_cause_support_provenance,
        )
        lines.extend(dict.fromkeys(independence_notes))
        lines.append("")
    if artifact.unchecked_premises:
        lines.extend(
            [
                "### Premise verification",
                "",
                (
                    "> The coordinator could not verify the following cited "
                    "premises against the baseline."
                ),
                "",
            ]
        )
        for premise in artifact.unchecked_premises:
            target = premise.file + (f":{premise.pattern}" if premise.pattern else "")
            lines.append(f"- `{target}` — unable to check: {premise.reason}")
        lines.append("")
    lines.extend(
        [
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
    if artifact.advisory_repair_proposal.strip():
        lines.extend(
            [
                "### Advisory repair proposal",
                "",
                (
                    "> Advisory only: this is an unverified repair idea from the "
                    "investigation, not part of the confirmed diagnosis contract."
                ),
                "",
                artifact.advisory_repair_proposal.strip(),
                "",
            ]
        )
    if artifact.symptom_scope_coverage.symptom_is_categorical:
        lines.extend(
            [
                "### Stated symptom scope coverage",
                "",
                artifact.symptom_scope_coverage.stated_scope.strip() or "_(empty)_",
                "",
            ]
        )
        for location in artifact.symptom_scope_coverage.examined_locations:
            display_status = location.status.strip().lower() or "unknown"
            lines.append(f"- **[{display_status}]** {location.location.strip()}")
            lines.append(f"  - Rationale: {location.rationale.strip() or '_(empty)_'}")
        lines.append("")
    if artifact.notes.strip():
        lines.extend(["### Notes", "", artifact.notes.strip(), ""])
    return "\n".join(lines)


def render_already_resolved_markdown(
    *,
    issue_number: int,
    baseline_sha: str,
    absent: tuple[AbsentPremise, ...],
    unable_to_check: tuple[UncheckedPremise, ...] = (),
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
    if unable_to_check:
        lines.extend(
            [
                "",
                "**Premises the coordinator could not verify:**",
                "",
            ]
        )
        for premise in unable_to_check:
            target = premise.file + (f":{premise.pattern}" if premise.pattern else "")
            lines.append(f"- `{target}` — unable to check: {premise.reason}")
    lines.extend(
        [
            "",
            "If this issue is still valid, restate its premise against the current "
            "baseline before re-running `forge diagnose`.",
        ]
    )
    return "\n".join(lines)


# Local, minimal heading scan for the reconciliation below. This module is
# pure-data / stdlib-only by contract, so it does not import
# shape_check.parsing's heading machinery. The heading text a landing call
# reconciles is not a fixed list to keep in sync — it is read directly off
# section_markdown's own leading heading (see _leading_heading_text), so
# only headings matching what is actually being landed are ever touched.
_HEADING_LINE_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def _normalize_heading_text(text: str) -> str:
    return re.sub(r"[\s:.\-—]+$", "", text.strip()).strip().lower()


def _iter_heading_lines(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return ``(line_index, level, text)`` for headings outside fenced blocks."""
    headings: list[tuple[int, int, str]] = []
    fence_char: str | None = None
    fence_len = 0
    for i, line in enumerate(lines):
        fm = _FENCE_LINE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if fence_char is None:
                fence_char = marker[0]
                fence_len = len(marker)
                continue
            if marker[0] == fence_char and len(marker) >= fence_len and not fm.group(2).strip():
                fence_char = None
                fence_len = 0
                continue
        if fence_char is not None:
            continue
        hm = _HEADING_LINE_RE.match(line)
        if hm:
            headings.append((i, len(hm.group(1)), hm.group(2).strip()))
    return headings


def _leading_heading_text(section_markdown: str) -> str | None:
    """Return the normalized text of ``section_markdown``'s own opening heading."""
    for line in section_markdown.splitlines():
        if not line.strip():
            continue
        hm = _HEADING_LINE_RE.match(line)
        return _normalize_heading_text(hm.group(2)) if hm else None
    return None


def upsert_diagnosis_section(body: str, section_markdown: str) -> str:
    """Insert or reconcile a ``## Diagnosis`` section in an issue body.

    Locates every heading already present whose text exactly matches
    ``section_markdown``'s own heading (e.g. "Diagnosis"), at any level —
    which may include an earlier step's placeholder, a prior diagnose run's
    artifact, or a duplicate left by either — and replaces all of them with
    a single instance of ``section_markdown`` at the position of the first
    one. A body must never end up carrying two sections a reader could
    mistake for "the" diagnosis; landing an artifact is where that gets
    reconciled, not appended beside it (#2263).

    Only headings matching the landed section's own name are touched.
    Ordinary prose headings that merely mention the word, and *other*
    named sections a body may carry — e.g. an operator-authored "Root
    cause" narrative distinct from the section actually being landed — are
    left untouched; this function absorbs duplicates of what it writes, not
    unrelated content that happens to share a heading vocabulary. If no
    matching heading exists, appends the section to the end of the body,
    separated by a blank line.
    """
    section = section_markdown.rstrip() + "\n"
    if not body.strip():
        return section

    target_text = _leading_heading_text(section_markdown)
    lines = body.splitlines()
    headings = _iter_heading_lines(lines)
    canonical = [
        (idx, level)
        for idx, level, text in headings
        if target_text is not None and _normalize_heading_text(text) == target_text
    ]

    if not canonical:
        sep = "" if body.endswith("\n") else "\n"
        return body + sep + "\n" + section

    def _section_end(start_idx: int, level: int) -> int:
        for idx, lvl, _text in headings:
            if idx > start_idx and lvl <= level:
                return idx
        return len(lines)

    spans = [(idx, _section_end(idx, level)) for idx, level in canonical]
    new_lines = lines[: spans[0][0]] + [section.rstrip()]
    prev_end = spans[0][1]
    for start, end in spans[1:]:
        new_lines.extend(lines[prev_end:start])
        prev_end = end
    new_lines.extend(lines[prev_end:])
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
