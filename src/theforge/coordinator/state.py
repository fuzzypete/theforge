"""Coordinator state: enums and dataclasses only (stdlib-only imports).

Contains Phase, ReviewCycleMetadata, CoordinatorState, and CoordinatorResult.
All helper functions (logging, shell, run-id) live in coord_util.py.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from theforge.agent_types import AgentResult
    from theforge.config import ForgeConfig
    from theforge.review import ReviewResult
    from theforge.story_validator import StoryValidationResult
    from theforge.task import PlanData

from .retry_budget import RetryBudget  # noqa: E402

# ── Phase enum ────────────────────────────────────────────────────────


class Phase(Enum):
    """Coordinator state machine phases."""

    INIT = auto()
    WORKSPACE = auto()
    PREFLIGHT = auto()
    PLAN = auto()
    PLAN_REVIEW = auto()
    DEV = auto()
    VALIDATE = auto()
    REVIEW = auto()
    HUMAN_REVIEW = auto()
    DONE = auto()
    MERGE_FAILED = auto()
    ESCALATE = auto()


# ── Escalate-gate decision provenance (#2279) ─────────────────────────
#
# Every escalate-gate outcome names who or what produced it. The value is
# recorded on CoordinatorState.escalate_decision_source, carried into the audit
# record and the resume record, and is the field an operator reads to tell a
# decision they made from one made for them.
ESCALATE_SOURCE_OPERATOR = "operator"  # an explicit selection at a gate surface
ESCALATE_SOURCE_OPERATOR_DECLINED = "operator_declined"  # selection the gate refused
ESCALATE_SOURCE_POLICY_REJECT = "policy_reject"  # retry.escalate_policy=reject
ESCALATE_SOURCE_POLICY_AUTO_APPROVE = "policy_auto_approve"  # escalate_policy=auto_approve
ESCALATE_SOURCE_NO_INTERACTION = "coordinator_no_interaction"  # no gate surface available
ESCALATE_SOURCE_TIMEOUT_PENDING = "timeout_pending"  # expired; still awaiting an operator
ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT = "advisor_on_timeout"  # advice applied on expiry
ESCALATE_DECISION_SOURCES: tuple[str, ...] = (
    ESCALATE_SOURCE_OPERATOR,
    ESCALATE_SOURCE_OPERATOR_DECLINED,
    ESCALATE_SOURCE_POLICY_REJECT,
    ESCALATE_SOURCE_POLICY_AUTO_APPROVE,
    ESCALATE_SOURCE_NO_INTERACTION,
    ESCALATE_SOURCE_TIMEOUT_PENDING,
    ESCALATE_SOURCE_ADVISOR_ON_TIMEOUT,
)

# Why the advisory recommendation was (or was not) applied at an expired gate.
# Only ``applied`` changes the outcome; every other value is a preserve, and
# they are kept distinct because "the advisor never launched", "it produced
# nothing parseable", "it recommended nothing", "it recommended elevate", and
# "it recommended something this run cannot perform" are five different
# situations with different repairs.
ADVICE_APPLIED = "applied"
ADVICE_ELEVATE = "elevate"  # deliberate no-automated-choice signal
ADVICE_NOT_PERFORMABLE = "not_performable"  # recommendation withheld by this run's state
ADVICE_NO_RECOMMENDATION = "no_recommendation"  # valid report, empty recommendation
ADVICE_UNPARSEABLE = "unparseable_report"  # advisor ran, report failed validation
ADVICE_LAUNCH_FAILURE = "launch_failure"  # advisor never reached the model
ADVICE_UNAVAILABLE = "advisor_unavailable"  # no advisory on this gate surface at all
ADVICE_POLICY_PRESERVE = "policy_preserve"  # opt-in not enabled; expiry preserves
ESCALATE_TIMEOUT_ADVICE_STATUSES: tuple[str, ...] = (
    ADVICE_APPLIED,
    ADVICE_ELEVATE,
    ADVICE_NOT_PERFORMABLE,
    ADVICE_NO_RECOMMENDATION,
    ADVICE_UNPARSEABLE,
    ADVICE_LAUNCH_FAILURE,
    ADVICE_UNAVAILABLE,
    ADVICE_POLICY_PRESERVE,
)


_PHASE_NAME_MAP: dict[str, Phase] = {
    "workspace": Phase.WORKSPACE,
    "preflight": Phase.PREFLIGHT,
    "plan": Phase.PLAN,
    "plan-review": Phase.PLAN_REVIEW,
    "dev": Phase.DEV,
    "validate": Phase.VALIDATE,
    "review": Phase.REVIEW,
    "human-review": Phase.HUMAN_REVIEW,
}


def parse_phase_name(name: str) -> Phase:
    """Parse a lowercase hyphenated phase name into a Phase enum value.

    Accepted names: workspace, preflight, plan, plan-review, dev,
    validate, review, human-review.

    ``init`` is intentionally excluded: INIT completes before any observable
    work begins, so ``--until init`` would be a silent no-op.  Use
    ``--until workspace`` to stop after workspace creation.

    Raises ValueError with valid names on unknown input.
    """
    result = _PHASE_NAME_MAP.get(name.lower())
    if result is None:
        valid = ", ".join(sorted(_PHASE_NAME_MAP))
        raise ValueError(f"Unknown phase name {name!r}. Valid names: {valid}")
    return result


# ── RetryReason enum ─────────────────────────────────────────────────


class RetryReason(str, Enum):
    """Reason the coordinator is retrying the dev phase.

    Using str mixin keeps serialization compatible with audit YAML output.
    """

    REVIEW_CHANGES = "review_changes"
    GATE_FAIL = "gate_fail"
    DIRTY_WORKTREE = "dirty_worktree"  # reserved; no active assignment site
    EXTEND = "extend"
    REJECT = "reject"
    TIMEOUT_RESUME = "timeout_resume"
    CONVENTION_VIOLATIONS = "convention_violations"
    MAX_ITERATIONS_NO_SUBMIT = "max_iterations_no_submit"
    P2_CLEANUP = "p2_cleanup"
    # A dev iteration that raised a specification gap and was resolved (by an
    # operator answer, by an expired pause, or by an exhausted allowance).
    # Re-enters DEV directly: the gap cost no review cycle, so none is spent
    # returning from it (#2122).
    SPEC_GAP_RESUME = "spec_gap_resume"


# ── Disposition enum ──────────────────────────────────────────────────


Disposition = Literal[
    "unresolved",
    "fixed",
    "regression",
    "net_new",
    "corroborated_new",
    "downgraded",
    "ac_blocking",
    "gate_contradicted",
    # A P1 whose cited file is not part of this story's merge-base-to-HEAD diff,
    # or which cites no resolvable file, or which arrived when the diff could not
    # be computed at all. Distinguishable from both a grounded blocking finding
    # and a dismissed one: it never decides the story's outcome, and it is never
    # dropped from the registry or the audit's non_blocking_p1s (#2525).
    "diff_ungrounded",
]


# ── Dataclasses ──────────────────────────────────────────────────────


@dataclass
class MergeStepState:
    """Persistent step-state for a resumable _merge_pr execution.

    Written to <workspace_path>/.forge/merge_state.yaml after each step so a
    crash mid-merge can be resumed from the last committed step.
    """

    completed_steps: list[str] = field(default_factory=list)
    pr_url: str | None = None
    merge_queued: bool = False
    auto_merge_queued: bool = False
    error: str | None = None


@dataclass
class FindingRecord:
    """Persistent record of a finding across review cycles.

    finding_id is a stable fingerprint: hash(severity + file + normalized description tokens).
    disposition is assigned by finding_classifier.update_finding_registry() each cycle.
    """

    finding_id: str  # sha256 hex prefix
    cycle_first_seen: int
    cycle_last_seen: int
    file: str | None
    line: int | None
    severity: str  # "P1" | "P2"
    description: str  # canonicalized description
    reporter: str  # profile name that raised it
    disposition: Disposition


@dataclass
class PlanFindingRecord:
    """Persistent record of a plan review finding across plan regen cycles.

    Populated by plan_finding_classifier.match_plan_findings() and updated in
    plan_flow.py after each plan review iteration.  No ``file`` or ``line``
    fields — PlanReviewFinding has neither; anchors are extracted from
    ``description`` text only.
    """

    description: str  # stripped of reviewer attribution prefix
    severity: str  # "P1" | "P1-impl" | "P2"
    cycle_first_seen: int  # plan regen attempt index when first observed
    cycle_last_seen: int  # plan regen attempt index when last observed
    disposition: Literal["unresolved", "fixed", "new"]
    original_severity: str | None = None  # pre-corroboration severity; None = no downgrade


@dataclass(frozen=True)
class FailedTestExtraction:
    """Result of parsing failing-test identifiers out of gate output.

    ``format_recognized`` is the distinguishing signal the extractor owes its
    caller: an empty ``tests`` list means "no failing tests" only when
    ``format_recognized`` is True. When it is False the extractor could not
    parse the gate's output at all (an unrecognized toolchain), so the empty
    list is a *silent absence*, not a genuine one. ``source`` records which
    reader produced the result: ``"builtin"`` (built-in pytest-style grammar),
    ``"xcodebuild"`` (Xcode's ``Failing tests:`` block),
    ``"custom_pattern"`` (project-configured ``failed_test_pattern``), or
    ``"unrecognized"`` (nothing applied).
    """

    tests: list[str]
    format_recognized: bool
    source: str


@dataclass(frozen=True)
class DevIterationTelemetry:
    """Per-dev-iteration telemetry captured for audit output."""

    iteration: int
    max_iterations: int
    cost_usd: float | None
    duration_s: float
    cycle: int = 0  # which review cycle this dev iteration belongs to
    gate_result: str | None = None
    failed_tests: list[str] = field(default_factory=list)
    # Tri-state signal distinguishing "extraction ran and the gate reported no
    # failing tests" from "the gate output was in a format the extractor does
    # not recognize, so extraction did not apply." None means there was no
    # failing-gate output to parse (e.g. a PASS). True/False let the audit trail
    # tell a genuine empty ``failed_tests`` from a silently-degraded one.
    gate_output_format_recognized: bool | None = None
    # SHA-256 of the failing gate output tail. Two consecutive iterations with an
    # identical fingerprint mean the gate said exactly the same thing twice — the
    # convergence signal for failures that name no tests (lint/format), which
    # ``failed_tests`` cannot detect. None on a PASS or an empty tail (#1981).
    gate_output_fingerprint: str | None = None
    existing_test_failures: bool = False
    is_timeout: bool = False
    files_changed: list[str] = field(default_factory=list)
    files_changed_count: int = 0
    tests_fixed_count: int = 0
    meaningful_progress: bool | None = None
    sandboxed: bool = True
    # How writes were contained this iteration: "mechanical" (host sandbox
    # wrapper), "native" (provider --sandbox flag), "unavailable" (fail-closed),
    # or "none". Distinguishes real containment from prompt-only runs (#1907).
    containment: str = "none"
    # Sandbox capability profile granted for this iteration: {"profile",
    # "write_roots", "mach_services"}. An explicit null profile with empty sets
    # records default containment, as distinct from omitted data (#1947).
    sandbox_capabilities: dict = field(default_factory=dict)
    agent_exit_code: int | None = None
    runner_failure_code: str | None = None
    runner_failure_summary: str | None = None
    cli_quota_error_observed: bool = False
    transport_fallback_fired: bool = False
    transport_fallback_reason: str | None = None
    transport_used: str | None = None
    model_used: str | None = None
    transport_retry_count: int = 0
    transport_retry_events: list[dict[str, Any]] = field(default_factory=list)
    # Project-declared verification commands the coordinator ran outside the dev
    # sandbox at this iteration's request (ADR-0007 / #2050). Empty when the
    # project declares none, or when the agent asked for nothing. Each entry
    # carries the requested name, whether it was accepted, and the outcome — so
    # an iteration that "iterated blind" is distinguishable from one that
    # actually executed the toolchain.
    verification_requests: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class GateLabel:
    """Operator-facing identity of one gate invocation.

    The resolved gate command is identical for the sprint baseline gate, each
    story's reuse gate during resume triage, and each story's validation gate,
    so the command string alone cannot tell an operator which of them a log
    line belongs to (#2014). Callers attach the purpose and target they already
    hold at the call site; nothing here affects gate execution or its result.

    ``target`` is the branch under gate, or a marker naming a non-branch target
    such as ``"merge base"`` for the baseline gate.
    """

    purpose: str
    slug: str | None = None
    target: str | None = None
    commit: str | None = None
    worktree_path: str | None = None

    def describe(self) -> str:
        """Render as a log phrase: ``reuse gate for issue-50 on feat/issue-50 abc1234``."""
        parts = [self.purpose]
        if self.slug:
            parts.append(f"for {self.slug}")
        location = self.target or self.worktree_path
        if location:
            parts.append(f"on {location}")
        if self.commit:
            parts.append(short_sha(self.commit))
        return " ".join(parts)


def short_sha(commit: str, length: int = 8) -> str:
    """Abbreviate a full hex SHA; any other ref (branch name, tag) is returned as-is."""
    text = commit.strip()
    if len(text) > length and all(c in "0123456789abcdefABCDEF" for c in text):
        return text[:length]
    return text


@dataclass(frozen=True)
class GateDebugTelemetry:
    """Diagnostic command telemetry captured when the validation gate times out.

    ``trace_index`` is the monotonic ``state.dev_trace_count`` value that also
    names this entry's trace file — deliberately *not* called ``iteration``,
    because ``DevIterationTelemetry.iteration`` is the per-review-cycle counter
    that resets each cycle. One audit record carried both under the same name,
    so a trace path quoted from an escalation no longer matched the dev entry it
    belonged to once a second review cycle opened (#1986). ``trace_path`` is the
    exact worktree-relative path written for this entry, so the correlation is
    stated rather than reconstructed.
    """

    trace_index: int
    trace_path: str
    command: str
    ran: bool
    timeout_s: int
    exit_code: int | None
    output_tail: str
    output_truncated: bool


@dataclass(frozen=True)
class GateDiagnosticTelemetry:
    """Diagnostic re-run telemetry captured when the validation gate times out (issue #1217).

    Records the serialized pytest pass (``-n 0 --timeout=N --timeout-method=thread``)
    run after a gate timeout to surface the hanging test in isolation. Distinct
    from GateDebugTelemetry, which records the free-form user-configured
    gate_debug_command; this pass is the hardcoded pytest-specific instrumentation.

    ``trace_index``/``trace_path`` carry the same meaning as on
    :class:`GateDebugTelemetry`: the monotonic trace-file counter and the exact
    path written for this entry, named distinctly from the per-cycle
    ``iteration`` (#1986).
    """

    trace_index: int
    trace_path: str
    command: str
    # True/False when workload execution was observed/refuted; None = indeterminate.
    ran: bool | None
    budget_s: int  # hard wall-clock cap for the whole pass
    per_test_timeout_s: int  # --timeout= value applied to each test
    exit_code: int | None
    timed_out: bool  # True if the diagnostic pass itself exhausted its budget
    hanging_test: str | None  # single test node that exceeded the per-test timeout, if identified
    output_tail: str
    output_truncated: bool


@dataclass(frozen=True)
class ReviewIterationTelemetry:
    """Per-review-cycle telemetry captured for audit output.

    ``iteration`` is the sequential number of this *recorded* reviewer cycle, so
    it matches the ``cycle`` the rendered ``reviews[]`` list assigns to the same
    cycle. It is deliberately not ``state.review_cycle``: VALIDATE can open a
    review cycle (``RETRY_DEV_NEW_CYCLE``) which advances that counter without
    appending a telemetry entry, which made two consecutive reviewer cycles
    record as iteration 1 and 3 while rendering as cycle 1 and 2 (#1986).
    """

    iteration: int
    max_iterations: int
    # None = the transport could not measure this cycle's cost. Never coerce it
    # to 0.0 — unmeasured spend is not free spend (#1992).
    cost_usd: float | None
    duration_s: float
    verdict: str
    findings_by_severity: dict[str, int]
    new_findings_by_severity: dict[str, int]
    repeated_findings_by_severity: dict[str, int]
    novel_findings: int
    restated_findings: int


#: Verification states a reviewed commit can be in. Derived mechanically from
#: the recorded gate provenance — never inferred from verdict or summary text.
REVIEWED_COMMIT_VERIFICATION_STATES = (
    "gate_passed",
    "gate_failed",
    "gate_skipped",
    "gate_stale",
    "ungated",
    "unknown",
)


@dataclass(frozen=True)
class ReviewedCommitVerification:
    """Verification state of the commit a single review cycle judged (#2052).

    A review verdict stored without the verification state of the code it
    judged is indistinguishable from a stale verdict that later commits already
    superseded. This records, per cycle, what the gate had actually said about
    the exact commit under review at the moment the cycle opened.

    ``state`` is one of :data:`REVIEWED_COMMIT_VERIFICATION_STATES`:

    - ``gate_passed`` / ``gate_failed`` — the gate ran on *this* commit
    - ``gate_skipped``  — a story ``gate:`` override suppressed the gate
    - ``gate_stale``    — the gate ran, but on a different (earlier) commit
    - ``ungated``       — no gate has run for this story yet
    - ``unknown``       — the reviewed commit could not be resolved
    """

    state: str = "unknown"
    # Decision recorded by the most recent gate execution: PASS / FAIL /
    # ERROR / SKIPPED, or None when no gate has run.
    gate_decision: str | None = None
    # Commit the most recent gate execution ran against, or None.
    gate_commit: str | None = None
    # Total gate executions for this story so far (survives --resume).
    gate_runs: int = 0
    # Count of coordinator-raised blocking findings recorded at cycle open.
    validate_blocks: int = 0
    # Verdict of the story (spec) validator, when one ran.
    story_validation_verdict: str | None = None

    @classmethod
    def derive(
        cls,
        *,
        reviewed_commit: str | None,
        gate_commit: str | None,
        gate_decision: str | None,
        gate_runs: int = 0,
        validate_blocks: int = 0,
        story_validation_verdict: str | None = None,
    ) -> ReviewedCommitVerification:
        """Classify a reviewed commit against the recorded gate provenance."""
        if not reviewed_commit:
            state = "unknown"
        elif not gate_commit:
            state = "ungated"
        elif gate_commit != reviewed_commit:
            state = "gate_stale"
        elif gate_decision == "SKIPPED":
            state = "gate_skipped"
        elif gate_decision == "PASS":
            state = "gate_passed"
        else:
            state = "gate_failed"
        return cls(
            state=state,
            gate_decision=gate_decision,
            gate_commit=gate_commit,
            gate_runs=gate_runs,
            validate_blocks=validate_blocks,
            story_validation_verdict=story_validation_verdict,
        )

    def to_audit_dict(self) -> dict[str, Any]:
        """Render as the ``verification`` block of a rendered review cycle."""
        return {
            "state": self.state,
            "gate_decision": self.gate_decision,
            "gate_commit": self.gate_commit,
            "gate_runs": self.gate_runs,
            "validate_blocks": self.validate_blocks,
            "story_validation_verdict": self.story_validation_verdict,
        }


@dataclass
class GateGreenCheckpoint:
    """A commit the gate passed *and* a review approved, retained as a landing floor (#2028).

    Captured when an approving review cycle hands the story back to DEV for P2
    cleanup. If that cleanup iteration turns the gate red with no dev iterations
    left to recover, this is the commit the story lands instead of failing
    outright — the later, unvalidated work is dropped and its findings return as
    normal review findings on a later story.

    ``commit`` is only ever the SHA the reviewers actually judged *and* the gate
    actually passed: recorded from ``ReviewCycleMetadata.verification`` in the
    ``gate_passed`` state, never inferred from ``last_gate_commit`` alone. The
    gate records HEAD before VALIDATE's post-PASS dirty-worktree auto-commit, so
    "the gate passed at some point" does not establish that the reviewed tree is
    the gated tree — only the derived verification state does.
    """

    commit: str
    review_cycle: int
    dev_iterations_spent: int
    review_verdict: str
    carried_p2_count: int
    branch_name: str | None = None
    # The ReviewResult the approval was made on. Landed through
    # ``state.landing_review_result`` so merge-pr has a review to post.
    review_result: ReviewResult | None = None

    def to_audit_dict(self) -> dict[str, Any]:
        """JSON-safe view for the resume sidecar and the audit trail."""
        return {
            "commit": self.commit,
            "review_cycle": self.review_cycle,
            "dev_iterations_spent": self.dev_iterations_spent,
            "review_verdict": self.review_verdict,
            "carried_p2_count": self.carried_p2_count,
            "branch_name": self.branch_name,
        }


@dataclass
class ReviewCycleMetadata:
    """Per-cycle metadata for audit logging."""

    pool_models: list[str]  # profile names of all pool agents
    successful: list[str]  # profile names that succeeded
    failed: list[str]  # profile names that failed
    synthesized: bool  # whether synthesis ran
    parse_retries: int = 0  # parse/schema retry count for this cycle
    failed_detail: dict[str, str] = field(default_factory=dict)  # profile → "exit=N"
    # Per-profile count of transient transport retries attempted this cycle.
    transient_retries: dict[str, int] = field(default_factory=dict)
    # Per-profile outcome: succeeded, transient_retried_then_succeeded,
    # transient_retried_then_failed, hard_failed.
    transient_outcomes: dict[str, str] = field(default_factory=dict)
    # Effective quorum threshold for this cycle (after collapse to panel size).
    quorum_threshold: int = 0
    # Whether the effective threshold was met (i.e. synthesis proceeded).
    quorum_met: bool = False
    # Whether synthesis proceeded on a *degraded* quorum: the threshold was NOT
    # met, but the shortfall was caused entirely by non-verdict reviewer
    # failures (e.g. a reviewer that finished without calling submit) and at
    # least one trustworthy verdict survived, so the story was not killed.
    degraded_quorum: bool = False
    # Human-readable audit note describing a degraded-quorum decision, or None.
    degraded_quorum_warning: str | None = None
    # Reviewer profile name → the concrete model identity that produced the
    # output this cycle actually used (#2226). A parse retry supersedes the
    # initial invocation's output, and can be served by a different version, so
    # "which model produced this cycle's evidence" is a property of the cycle,
    # not something reconstructable from the per-invocation attempt log. Recorded
    # here so the findings/cost fold (which counts cycles via ``successful``) and
    # its version breakdown read the same source and cannot disagree. Absent for
    # a reviewer whose result reported no served identity.
    resolved_by_reviewer: dict[str, str] = field(default_factory=dict)
    # ── Reviewed-commit provenance (#2052) ────────────────────────────
    # The repository HEAD this cycle's reviewers judged, and the verification
    # state of that exact commit. Both stay None for metadata deserialized from
    # older runs, which render as an explicit "unknown" verification state
    # rather than silently reading as current.
    reviewed_commit: str | None = None
    verification: ReviewedCommitVerification | None = None


@dataclass(frozen=True)
class CycleHistory:
    """Lightweight per-cycle summary for dev agent context (anti-churn)."""

    cycle: int
    verdict: str  # "APPROVE" or "REQUEST_CHANGES"
    summary: str
    p1_findings: list[str]  # description strings of P1-severity findings (truncated to 200 chars)


@dataclass
class CoordinatorState:
    """Mutable state tracking for a single task execution."""

    phase: Phase = Phase.INIT
    started_at: str | None = None  # ISO timestamp set at INIT
    run_id: str | None = None  # stable 12-char hex run identity; set at engine entry
    sprint_name: str | None = None  # set when run is part of a sprint; used for cross-story flags
    story_content: str | None = None  # story text as loaded before any runtime mutation
    workspace_path: Path | None = None
    branch_name: str | None = None
    # Provenance of the workspace's contents (#2288): whether the story text
    # that produced them is the text this run executes. One of the
    # ``worktree_provenance.PROVENANCE_*`` values, or None when workspace setup
    # recorded no judgement.
    workspace_provenance_status: str | None = None
    # Dev-prompt text for an adopted worktree whose producing story text has
    # since changed; None whenever there is nothing superseded to report. This
    # is a ONE-SHOT channel: the dev phase clears it after the first prompt
    # carries it, so nothing may derive a run-level fact from it — see
    # workspace_inherited_work_surfaced_to_dev below and
    # workspace_provenance_status above, both of which are written once and
    # never consumed.
    workspace_inherited_work_note: str | None = None
    # Sticky "the dev agent was told it inherited superseded work". Set when the
    # note is injected into a dev prompt, never cleared.
    workspace_inherited_work_surfaced_to_dev: bool = False
    # Immutable snapshot of the file set this run changed against its base ref
    # (#2347): ``{base_ref, head_ref, files: [{path, insertions, deletions,
    # binary}]}``. Captured by ``changed_files.capture_changed_files`` at the
    # last seam before the worktree and branch are destroyed, because landing
    # removes both and the comparison cannot be recomputed afterwards. None
    # means no comparison has been captured — distinct from a captured
    # comparison whose ``files`` list is empty.
    changed_files: dict[str, Any] | None = None
    dev_session_id: str | None = None
    pending_dev_transport_retry_count: int = 0
    pending_dev_transport_retry_events: list[dict[str, Any]] = field(default_factory=list)
    # Verification requests served during the DEV iteration currently in flight,
    # awaiting the telemetry record that closes it out (ADR-0007 / #2050). Set by
    # the dev phase when the broker stops; consumed and cleared by
    # ``record_dev_iteration_telemetry``.
    pending_dev_verification_requests: list[dict[str, Any]] = field(default_factory=list)
    # Every verification request served this run, in order. Kept alongside the
    # per-iteration telemetry because some dev exit paths (max-iterations retry
    # without a handoff, for one) return before telemetry is recorded, and an
    # unconfined coordinator execution must never be invisible in the audit trail.
    dev_verification_requests: list[dict[str, Any]] = field(default_factory=list)
    # ── Specification-gap backchannel (#2122) ────────────────────────────
    # Every ``<forge_spec_gap>`` a dev agent raised this run, in order: what was
    # asked, whether it got an operator pause, and how long the pause waited.
    # Plain JSON-safe dicts so they survive the audit, the pending file, and the
    # durable resume record unchanged.
    spec_gap_events: list[dict[str, Any]] = field(default_factory=list)
    # How each raised gap ended: an operator answer, an expired pause, or an
    # exhausted allowance. One entry per event — no gap resolves without a
    # record. Restored from the durable record on resume *and* on a fresh run of
    # the same story, so an answer is never re-asked.
    spec_gap_resolutions: list[dict[str, Any]] = field(default_factory=list)
    # Pauses this run actually opened. Bounded by
    # ``retry.max_spec_gap_pauses``; a gap raised past the bound is recorded and
    # resolved under its own assumption rather than pausing again.
    spec_gap_pauses_used: int = 0
    plan_session_id: str | None = None
    plan_review_session_ids: dict[str, str] = field(default_factory=dict)  # keyed by profile.name
    reviewer_session_ids: dict[str, str] = field(default_factory=dict)  # keyed by profile.name
    reviewer_parse_failure_counts: dict[str, int] = field(default_factory=dict)
    reviewer_demoted: set[str] = field(
        default_factory=set
    )  # reviewers permanently demoted this run
    review_cycle: int = 0  # which dev→review loop we're on
    # dev_iteration is an InitVar: accepted as a constructor kwarg for backward
    # compatibility (tests pass dev_iteration=N directly), but not stored as a
    # plain int field.  The actual value lives in budget.cycle_count and is
    # exposed via the dev_iteration property defined below the class.
    dev_iteration: InitVar[int] = 0
    budget: RetryBudget = field(
        default_factory=RetryBudget, init=False
    )  # unified retry budget; owns cycle_count and total_count
    dev_trace_count: int = 0  # monotonically increasing across all cycles; never reset
    dev_results: list[AgentResult] = field(default_factory=list)
    dev_durations: list[float] = field(default_factory=list)  # wall-clock seconds per dev call
    # Sticky "the dev process was killed by its wall-clock timeout at least once"
    # flag. Set once at kill time in dev_phase (never cleared), because the
    # killed iteration's telemetry can be overwritten by a later VALIDATE-phase
    # telemetry write once checkpoint-committed work lets execution fall through
    # (#1754) — so the last telemetry entry is not a reliable kill signal. This
    # is distinct from a VALIDATE gate/test-command timeout (a different failure
    # class carried on DevIterationTelemetry.is_timeout).
    dev_process_timeout_killed: bool = False
    # Sticky "the dev process was terminated by stuck-pattern detection at least
    # once" flag. Set once at kill time in dev_phase (never cleared), mirroring
    # dev_process_timeout_killed: the terminated iteration's telemetry can be
    # overwritten by a later VALIDATE-phase write once checkpoint-committed work
    # (#1754) lets execution fall through, so the last telemetry entry is not a
    # reliable signal. This is the reliable signal that the dev process was ended
    # by stuck-pattern detection (runner failure_code == 'stuck_pattern').
    dev_process_stuck_terminated: bool = False
    review_agent_results: list[AgentResult] = field(default_factory=list)
    review_durations: list[float] = field(
        default_factory=list
    )  # wall-clock seconds per review call
    preflight_duration_s: float | None = None  # wall-clock seconds for preflight
    plan_durations: list[float] = field(default_factory=list)  # per plan-gen invocation
    plan_review_durations: list[float] = field(
        default_factory=list
    )  # per agent plan-review iteration
    validate_durations: list[float] = field(default_factory=list)  # per validate (gate) call
    review_results: list[ReviewResult] = field(default_factory=list)
    review_cycle_metadata: list[ReviewCycleMetadata] = field(default_factory=list)
    gate_decisions: list[str] = field(default_factory=list)
    # Count of gate *executions* for this story: incremented once per gate
    # command invocation, including invocations that ended in a timeout or an
    # error, and never incremented when ``gate_override`` skipped the gate.
    # ``gate_decisions`` cannot serve as that count — it only gains an entry
    # when a decision came back (so timeouts are missing) and it gains a
    # synthetic "PASS" on the skip path (so non-runs are counted) (#1984).
    # Persisted to the resume sidecar so escalation counts survive --resume.
    gate_runs: int = 0
    # Commit the most recent gate ran against, and the decision it returned
    # ("PASS"/"FAIL"/"ERROR", or "SKIPPED" for a story gate override). Recorded
    # as a pair so a review cycle can state whether the code it judged was the
    # code the gate judged (#2052). Persisted to the resume sidecar: without it
    # a resumed run reports every cycle as ungated even though a gate ran.
    last_gate_commit: str | None = None
    last_gate_decision: str | None = None
    # Provenance for every validation run this story performed: which profile
    # ran, what authority that profile carries, the resolved command, the
    # result, the commit it judged, and whether it was skipped (#2358). A gate
    # decision on its own says only that a command exited zero; these records
    # say what the result was worth, so the scope and standing behind a verdict
    # is readable afterwards rather than inferred from a command string.
    # Persisted to the resume sidecar and the audit; records written before this
    # field existed are absent, and absence is read as legacy (complete/merge).
    validation_runs: list[dict] = field(default_factory=list)
    last_review_findings: str | None = None
    cycle_history: list[CycleHistory] = field(default_factory=list)
    cycle_history_total: int = 0  # monotonically increasing count of all appended entries
    escalation_note: str | None = None
    human_feedback: str | None = None
    human_review_decision: str | None = (
        None  # "approve" | "reject" | "escalate" | "extend" | "timeout"
    )
    human_review_feedback: str | None = None  # rejection text from human
    human_review_extra_cycles: int = 0  # cycles granted by "extend" decisions
    human_review_waited_seconds: float | None = None  # seconds waited in remote mode
    human_review_mode: str | None = None  # "remote" | "interactive"
    preflight_verdict: str | None = None  # "PROCEED" | "ALREADY_DONE" | "BLOCKED"
    preflight_reason: str | None = None
    preflight_complexity: str | None = None  # "small" | "medium" | "large"
    preflight_complexity_score: int | None = None  # 1-10 projected legacy score
    # Dual-axis sizing (issue #1442). The legacy complexity_score above is
    # projected from these two via preflight_complexity_projection so existing
    # readers keep working; new consumers may opt into the native axes.
    preflight_implementation_complexity_score: int | None = None  # code-change envelope
    preflight_validation_complexity_score: int | None = None  # validation/execution envelope
    preflight_complexity_projection: str | None = None  # e.g. "max_implementation_validation"
    # True when the story exceeds what one story should attempt and should be
    # decomposed (#2680). Derived from the *implementation* axis reaching its
    # ceiling, never from the projected complexity_score — a validation-heavy
    # but cohesive story is not over scope. Routing is unaffected: 9 and 10
    # resolve to the same tier, reviewer count, and reasoning effort.
    preflight_scope_exceeded: bool = False
    # ── Preflight complexity gate (#2681) ─────────────────────────────────
    # Whether the end-of-preflight scope decision was put to an operator, what
    # was decided, and under what policy. All JSON-safe: they are persisted to
    # the resume record and the run audit so a cached or resumed run can still
    # say the story was approved at this size — or returned to be split.
    preflight_complexity_gate_opened: bool = False
    preflight_complexity_gate_score: int | None = None
    preflight_complexity_gate_implementation_score: int | None = None
    preflight_complexity_gate_validation_score: int | None = None
    preflight_complexity_gate_threshold: int | None = None
    # "approve" | "decompose" — the action actually applied.
    preflight_complexity_gate_decision: str | None = None
    # "operator" | "no_decision" — whether a human answered at all.
    preflight_complexity_gate_decision_source: str | None = None
    # Why the configured no-decision action was not usable, when it was not.
    preflight_complexity_gate_no_decision_fallback: str | None = None
    preflight_complexity_gate_waited_seconds: float | None = None
    preflight_complexity_gate_decided_at: str | None = None
    # Cited evidence: list of {rule_id, signal, dimension} dicts naming the rules
    # that fired on each axis. Empty until preflight sizing runs.
    preflight_complexity_evidence: list[dict] = field(default_factory=list)
    preflight_sufficiency: str | None = None  # "implementation_ready" | "needs_planning"
    preflight_work_type: str | None = None  # "feature" | "refactor" | "mechanical" | "bug"
    # Domain tags from the fixed taxonomy (theforge.domains) describing the KIND
    # of work — the horizontal routing axis (issue #155). Empty list = "no domains"
    # (an explicit current-run fact, routing-safe under ADR-0006 bucket A).
    preflight_domains: list[str] = field(default_factory=list)
    preflight_contract_change: bool = False  # story intentionally alters a tested contract
    preflight_bundle_candidate: bool = False
    # Scheduler-written cost-aware batch-group id (#727). Distinct from
    # preflight_bundle_candidate: that flag means "the scheduler put this story
    # in a conflict bundle"; this names the independent-story batch the
    # scheduler packed it into, or None when it was dispatched on its own.
    preflight_batch_group: str | None = None
    preflight_warnings: list[str] = field(default_factory=list)  # non-blocking advisories
    preflight_likely_files: list[str] | None = None
    preflight_result: AgentResult | None = None
    preflight_cached: bool = False
    preflight_cached_from_run_id: str | None = None
    preflight_cached_original_verdict: str | None = None
    preflight_cache_snapshot: dict[str, str] = field(default_factory=dict)
    preflight_cache_validation: dict[str, Any] = field(default_factory=dict)
    preflight_degraded: bool = False
    preflight_degraded_reason: str | None = None
    preflight_criteria_checked: list[dict] = field(default_factory=list)
    # Structured symptom-verification record for bug ALREADY_DONE verdicts.
    # Keys: status ("verified_resolved" | "not_reproduced" | "not_feasible" |
    # "not_attempted" | "" when absent), evidence (str), reproduces_now (bool|None).
    # An empty dict means the preflight output did not contain the field — for
    # bug-typed stories this is treated as missing symptom verification and
    # downgrades ALREADY_DONE to PROCEED.
    preflight_symptom_verification: dict = field(default_factory=dict)
    # Risk signals consulted when preflight agent failed and the coordinator
    # had to choose between conservative PROCEED and explicit escalation.
    # Empty list means no signals were detected (or preflight succeeded so
    # the question never arose).
    preflight_risk_signals: list[str] = field(default_factory=list)
    # Outcome of the failure-fallback policy: "proceed" (no risk signals →
    # conservative PROCEED) or "escalate" (risk signals present → BLOCKED).
    # None when preflight did not fail.
    preflight_failure_action: str | None = None
    # Partial-evidence artifact salvaged from a failed preflight run (#706):
    # files inspected, tool calls, and any partial conclusion the agent reached
    # before crashing/timing out. The serialized (to_dict) form; None when
    # preflight succeeded or the failed run left nothing observable. Consumed by
    # the PLAN phase to avoid re-reading the same files.
    preflight_partial_evidence: dict | None = None
    # ── Policy-assertion provenance (#2137) ───────────────────────────────
    # Which kind of blocker a BLOCKED verdict declared. Only "policy_assertion"
    # is subject to provenance adjudication; every other basis blocks as before.
    preflight_blocking_basis: str | None = None
    # Assertions the preflight check cited, as the agent gave them (advisory).
    preflight_policy_assertions_cited: list[dict] = field(default_factory=list)
    # The same assertions after registry resolution — each carries the decided
    # provenance class ("ratified" | "generated") and how it was matched. This is
    # the field consumers gate on; the cited form is evidence, not authority.
    preflight_policy_assertions_resolved: list[dict] = field(default_factory=list)
    # Assertions contradicted by chartered work that carry no operator decision.
    preflight_policy_retraction_candidates: list[dict] = field(default_factory=list)
    # Cited assertions the registry never recorded — surfaced so an unmarked but
    # real operator decision can be ratified instead of silently demoted.
    preflight_policy_ratification_candidates: list[dict] = field(default_factory=list)
    # True when a ratified assertion is what upheld a BLOCKED verdict.
    preflight_policy_blocking_authority: bool = False
    # Full adjudication record for the audit trail; empty when no BLOCKED verdict
    # founded on a policy assertion was weighed.
    preflight_policy_adjudication: dict = field(default_factory=dict)
    plan_results: list[AgentResult] = field(default_factory=list)
    plan_output: str | None = (
        None  # contents of the worktree plan file, passed to dev (raw string for audit)
    )
    plan_structured: PlanData | None = None  # parsed structured plan; None if fallback to markdown
    plan_attempt_plans: list[PlanData | None] = field(
        default_factory=list
    )  # per-attempt plan snapshots; appended before each regen overwrite of plan_structured
    plan_review_decision: str | None = None  # "approve" | "regenerate" | "abandon"
    plan_regen_count: int = 0  # number of plan regen attempts so far
    plan_review_waited_seconds: float | None = None
    plan_review_mode: str | None = None  # "interactive" | "remote" | "advisory-timeout"
    plan_agent_review_findings: str | None = None  # rejection findings for regen feedback
    plan_review_results: list[AgentResult] = field(
        default_factory=list
    )  # separate from plan_results to avoid corrupting plan generation cost tracking
    plan_review_transport_retries: list[dict] = field(
        default_factory=list
    )  # per-retry audit trail: {"attempt": int, "reviewer": str, "retry": int, "error": str}
    plan_review_parse_retries: list[dict] = field(
        default_factory=list
    )  # per-retry audit for successful-but-unparseable reviewer re-invocations:
    # {"attempt": int, "reviewer": str, "retry": int, "errors": list[str]}
    plan_transport_retries: list[dict] = field(
        default_factory=list
    )  # per-retry audit for transient plan draft/regen re-invocations:
    # {"phase": "PLAN"|"PLAN_REGEN", "attempt": int, "retry": int, "error": str}
    plan_review_failures: list[dict] = field(
        default_factory=list
    )  # per-reviewer failures: {"attempt": int, "reviewer": str, "errors": list[str], ...}
    plan_attempt_metadata: list[dict] = field(
        default_factory=list
    )  # per-attempt: {files_touched, p1_count, p2_count, finding_themes}
    # Per-plan-reviewer mechanical value telemetry (#1443). One dict per (reviewer,
    # pool attempt): {attempt, reviewer, complexity, unique_p1_count, total_p1_count,
    # latency_s, parse_error_count, actual_model, provider, cli}. Uniqueness is the
    # deterministic anchor-overlap computed at pool completion; parse_error_count is
    # derived from the same parse step that feeds plan_review_failures (no parallel
    # parse-failure writer). Consumed by audit.py and the reviewer_value fold.
    plan_reviewer_value: list[dict] = field(default_factory=list)
    # Per-code-reviewer mechanical value telemetry (#2156). Same shape and same
    # deterministic anchor-overlap computation as ``plan_reviewer_value`` above,
    # captured at code-review pool completion instead: one dict per (reviewer,
    # review cycle) {cycle, reviewer, complexity, unique_p1_count, total_p1_count,
    # latency_s, parse_error_count, actual_model, provider, cli}. Folded into the
    # separate ``code_review_value`` profile section so code-review reviewer value
    # stays independently queryable from plan-review value.
    code_reviewer_value: list[dict] = field(default_factory=list)
    plan_regen_disposition: str | None = None  # "patch" | "backtrack" | "escalate"
    plan_backtrack_used: bool = False  # True once the backtrack regen has been dispatched
    log_dir: Path | None = None  # per-story log directory under <project_root>/.forge/logs/
    error: str | None = None
    error_type: str | None = None
    sandboxed: bool = False  # True if mechanical containment was available at dev-phase entry
    # Containment classification for the dev run: "mechanical" | "native" |
    # "unavailable" | "none". Surfaced in audit/status so a prompt-only run is
    # never reported as mechanically contained (#1907).
    dev_containment: str = "none"
    # Resolved sandbox capability profile for the dev run (#1947):
    # {"profile", "write_roots", "mach_services"}. Set at dev-phase entry from
    # ForgeConfig.sandbox; empty only for runs that never entered DEV.
    dev_sandbox_capabilities: dict = field(default_factory=dict)
    dev_escalated: bool = False  # True once model escalation has occurred this run
    timeout_escalation_used: bool = (
        False  # True once a timeout escalation has fired this sprint; gates re-escalation
    )
    timeout_escalation_audit: dict | None = None  # original/new model+timeout recorded for audit
    plan_escalated: bool = False  # True once plan model escalation has occurred this run
    plan_escalation_note: str | None = None  # escalation context injected into regen prompt
    retry_reason: RetryReason | None = None  # see RetryReason enum for valid values
    # True when the DEV prompt built for the current iteration delegated gate
    # execution to the coordinator (a review-fix / P2-cleanup pass whose prompt
    # tells the agent NOT to re-run the gate — see task.fix_prompts.build_fix_prompt).
    # Authoritatively set by the coordinator at prompt-routing time, so the
    # unproven-completion guard can distinguish a legitimately gate-delegated
    # handoff from an ordinary handoff that merely omits gate evidence. Reset
    # every DEV iteration; not agent-attested, so an ordinary iteration cannot
    # spoof delegation by setting a handoff flag.
    gate_delegated_this_iteration: bool = False
    last_cycle_reviewer_results: list[tuple[str, ReviewResult]] = field(
        default_factory=list
    )  # (profile_name, ReviewResult) pairs from the most recent pool run
    finding_registry: list[FindingRecord] = field(default_factory=list)
    dev_prompt_injected_finding_ids: list[list[str]] = field(default_factory=list)
    dev_handoff_snapshots: list[dict[str, Any] | None] = field(default_factory=list)
    dev_iteration_telemetry: list[DevIterationTelemetry] = field(default_factory=list)
    gate_debug_telemetry: list[GateDebugTelemetry] = field(default_factory=list)
    # One entry per gate command that left processes running and had to have them
    # killed (#2309). Audit-shaped dicts rather than ProcessTeardown objects,
    # because this is the only consumer and the record is what it exists for: a
    # leaked gate worker produces no artifact and no cost, so without this the
    # run cannot say the kill happened.
    gate_process_teardowns: list[dict[str, Any]] = field(default_factory=list)
    gate_diagnostic_telemetry: list[GateDiagnosticTelemetry] = field(default_factory=list)
    review_iteration_telemetry: list[ReviewIterationTelemetry] = field(default_factory=list)
    # Every reviewer invocation this run, including failures (#1388). Each entry is
    # a plain dict (name, provider, model, cli, canonical_id, outcome,
    # completed_parseable_verdict, failure_reason, cycle) recorded at the review
    # invocation boundary. This is the native per-run capture that (a) is written
    # into the authoritative audit record and (b) is folded into the derived
    # reviewer completion-rate profile. Accumulates across every review cycle.
    reviewer_attempts: list[dict[str, Any]] = field(default_factory=list)
    context_manifests: list[dict] = field(default_factory=list)
    # One entry per dev invocation (same index as dev_results).
    # Each entry is the parsed handoff-file dict, or None if absent/unparseable.
    # Stable record of all findings across cycles, classified by finding_classifier
    last_dev_start_commit: str | None = None
    # HEAD commit hash captured before each dev iteration; used by finding_classifier
    # to compute git diff --name-only for changed-file correlation.
    # Legacy values: "approve" | "reject" | "continue". Since #1664 this field may
    # also hold a taxonomy action (see escalation_advisor.ACTION_TAXONOMY: accept,
    # land_core_defer_edges, redirect, decompose, elevate, defer_or_abandon) or
    # "advisory_pending" when an escalation timed out awaiting an operator decision.
    escalate_decision: str | None = None
    escalate_reason: str | None = None  # human-readable escalation reason
    # Fresh-context escalation advisor (issue #1664). The advisor reads a prepared
    # evidence packet and emits a constrained menu of action choices; the operator
    # must select one. These fields keep the packet, report, and selected action
    # visible in the audit trail so an escalation decision can be traced.
    escalate_selected_action: str | None = None  # taxonomy action the operator selected
    # Set when the operator selected an action this run could not carry out and
    # the gate DECLINED it (#2300). escalate_decision deliberately stays None in
    # that case: nothing was decided, so a later operator selection can still be
    # recorded, and no downstream reader sees an outcome nobody chose.
    escalate_declined_action: str | None = None
    escalate_declined_reason: str | None = None
    # WHO or WHAT produced the escalate-gate outcome (#2279). escalate_decision
    # says what happened; this says who is answerable for it, so an operator
    # reading a run afterwards can tell an action they chose from one applied on
    # their behalf from a gate still waiting — without inferring it from
    # timestamps. One of ESCALATE_DECISION_SOURCES; None until a gate runs.
    escalate_decision_source: str | None = None
    # Why the advisory recommendation was or was not applied when a gate expired
    # (#2279). One of ESCALATE_TIMEOUT_ADVICE_STATUSES; None when no gate
    # expired. An absent recommendation is the absence of advice, not consent to
    # any outcome — this field names WHICH absence it was.
    escalate_timeout_advice: str | None = None
    # The ReviewResult an approval was taken ON, stamped by _finalize_approve
    # when landing is deferred. Landing runs in a later call (and, for sprints, a
    # another thread), and merge-pr needs a review to post; re-deriving it from
    # review_results there loses an escalate-gate accept taken on a retained
    # quorum-unmet survivor, which has no merged result (#2300). Read through
    # completion.resolve_landing_review, never directly.
    landing_review_result: ReviewResult | None = None
    # Provenance of the above: "merged_cycle_review", "escalate_gate_selection",
    # or "gate_green_checkpoint" (a salvaged gate-green landing, #2028).
    landing_review_source: str | None = None
    # ── Gate-green salvage (#2028) ────────────────────────────────────────────
    # The latest commit that a gate passed AND a review approved, captured when
    # that approval routed back into DEV for P2 cleanup. Refreshed on each
    # approve-equivalent cleanup cycle so the newest approved gate-green commit
    # wins. None when no such commit exists — the ordinary case, and the one
    # that must keep failing exactly as before.
    gate_green_checkpoint: GateGreenCheckpoint | None = None
    # The decision to land the checkpoint instead of the gate-red HEAD, and the
    # record of what is being dropped. JSON-safe; written by
    # gate_green_salvage.salvage_gate_green_landing and read by land_story.
    gate_green_salvage: dict | None = None
    # Why a salvage was NOT taken on a run that reached a terminal gate failure.
    # A gate-green commit that was never approved, a batch leader, or a dirty
    # worktree are all "nothing forge could safely land" — but they are
    # materially different from "nothing gate-green ever existed", and the
    # operator cannot tell them apart from the failure alone (#2028).
    gate_green_salvage_declined: dict | None = None
    advisory_generated: bool = False  # True when a valid advisory report was produced
    advisory_packet: dict | None = None  # serialized EvidencePacket fed to the advisor
    advisory_report: dict | None = None  # serialized AdvisoryReport the advisor produced
    # Why no usable advisory input exists on this gate despite the role being
    # requested. Distinct from advisory_launch_failure, which means the advisor
    # process never reached the model at all.
    advisory_unavailable_reason: str | None = None
    # Set when the advisor process exited before it ever reached the model (a
    # forge configuration / tool-invocation defect, e.g. a CLI refusing to start
    # in the baseline checkout). Kept distinct from advisory_generated=False so
    # the operator checkpoint can tell "the advisor never ran, and nothing was
    # spent" apart from "the advisor ran and produced nothing usable" (#2164).
    advisory_launch_failure: bool = False
    advisory_launch_reason: str | None = None  # the tool's own explanation, one line
    # Structured escalation kind: "hygiene" (workspace mutation by a non-DEV phase),
    # "content" (review or gate found a real problem), or None when there is no
    # active escalation. Distinct from escalate_reason so resume can tell the two
    # apart without parsing the human-readable string.
    escalate_kind: str | None = None
    # Captured at the moment a REVIEW workspace-hygiene escalation fires when the
    # reviewer pool had already produced an APPROVE-consensus candidate for the
    # current dev commit. Used by `forge sprint --resume` to replay the prior
    # consensus instead of re-running the reviewer pool against an unchanged
    # dev commit. None means either (a) no hygiene escalation occurred, or
    # (b) the pool did not reach APPROVE consensus before the trip.
    hygiene_escalation_dev_commit_sha: str | None = None
    hygiene_escalation_prior_review: ReviewResult | None = None
    hygiene_escalation_prior_approve_count: int | None = None
    hygiene_escalation_total_count: int | None = None
    # Audit record describing how the resume entry handled a prior hygiene
    # escalation: replayed the consensus, ran a fresh review because the dev
    # commit moved, or ran a fresh review because no prior consensus existed.
    hygiene_resume_audit: dict | None = None
    story_validation_result: StoryValidationResult | None = None
    convention_violations: list[dict] = field(default_factory=list)
    # Operator-readable detail of the most recent coordinator-observed blocking
    # finding in VALIDATE (gate failure summary, or the hard-convention violation
    # list). Carried to the engine so the record it writes when the finding opens
    # a new review cycle names the real defect (#1981).
    validate_block_detail: str | None = None
    # Coordinator-raised blocking findings that bought a review cycle. This is
    # VALIDATE's own audit record, deliberately separate from the reviewer record
    # (review_results / review_cycle_metadata / review_iteration_telemetry): an
    # entry in those means a reviewer pool ran, and per-model attribution, the
    # adaptive review-cycle learner, and the persistent-P1 lookback all depend on
    # that. Each entry: kind, review_cycle, dev_iterations_spent, gate_decision,
    # detail, convention_violations (#1981).
    validate_blocks: list[dict] = field(default_factory=list)
    # Count of review cycles opened by VALIDATE rather than by a reviewer verdict.
    # Monotonic — unlike review_cycle, which resets on extend/reject and decrements
    # on the exhausted-gate continue. Lets usage reporting show the review budget
    # this story actually spent while keeping reviewer-cycle counts reviewer-only.
    validate_opened_review_cycles: int = 0
    # True when VALIDATE refused a blocking finding because no further review
    # cycle could be opened. The story stops with a cycle in flight, so counter
    # comparison alone reports it as having finished early with budget to spare;
    # usage reporting reads this flag instead (#1981).
    review_budget_exhausted: bool = False
    plan_validation_findings: list[dict] = field(default_factory=list)
    plan_finding_registry: list[PlanFindingRecord] = field(default_factory=list)
    # Stable identity records for plan review findings across regen cycles,
    # populated by plan_finding_classifier.match_plan_findings() in plan_flow.py.
    plan_match_provenance: list[str] = field(default_factory=list)
    plan_regen_filter_audit: list[dict] = field(default_factory=list)
    # Per-attempt audit of which findings were filtered and which were highlighted
    # by build_filtered_regen_findings() in plan_trajectory.py. One entry per regen
    # attempt (index 0 = first review, before any regen). Not populated for attempt 0
    # since no filtering occurs on the first rejection.
    # Human-readable log of match/abstain decisions, one entry per plan review
    # attempt (index 0 = first attempt).  Accumulated across regen cycles so
    # the full decision history is available for post-hoc audit inspection.
    # ── Trajectory tracking (dev review family classification) ─────────────────
    # Monotonically increasing counter used as the key for trajectory snapshots.
    # This is separate from review_cycle, which resets to 0 on extend/reject and
    # decrements on exhausted-cycle gate continue — mutations that are correct for
    # budget management but would corrupt trajectory history if used as the key.
    trajectory_cycle: int = 0
    # Per-family trajectory store.  Each dict: {seed_anchor, cycles, descriptions}.
    # Plain dicts with basic types (str, int, list) for serialization compatibility.
    finding_trajectory: list[dict] = field(default_factory=list)
    # Snapshot of (trajectory_cycle_number, [finding_dicts]) from each review cycle.
    # Used so family classification can match current findings against all prior cycles.
    # Each finding_dict stores file, line, description, severity.
    review_cycle_findings: list[tuple[int, list[dict]]] = field(default_factory=list)
    # Families from the most recent classification that are present in 2+ cycles.
    # Consumed by build_fix_prompt on the RETRY_DEV path.  Reset each classification.
    surviving_families: list[dict] = field(default_factory=list)
    # Topology-walk evidence from review_topology.detect_topology_walk(), or None
    # when the latest cycle's trajectory does not unambiguously say the loop is
    # inventorying a surface rather than converging (#2372).  A plain dict so it
    # serialises straight into the trajectory sidecar, the audit record, and the
    # escalation advisor's evidence packet.  Recomputed every merged cycle.
    review_topology_signal: dict | None = None
    # What the latest review cycle's P1s were diff-grounded against (#2525): the
    # file set, where it came from (the branch diff, or per-story commit
    # attribution inside a batch group's shared worktree), whether it could be
    # established at all, and which findings failed to ground. A suppression
    # whose basis the record does not name is not reviewable after the fact.
    review_diff_grounding: dict | None = None
    # True once a detected topology walk has been routed to the escalate gate.
    # The gate's "continue" is an operator decision to keep going; re-escalating
    # the same pattern on the very next cycle would spend the decision it just
    # made, so detection routes at most once per run.
    review_topology_escalated: bool = False
    # True while the escalation currently in force is the one the detector
    # routed. Distinct from the latch above, which stays set for the rest of the
    # run: after a gate "continue" this clears, so a later ceiling-triggered
    # escalation is not described to the operator as having fired early. The
    # signal itself is still carried as supporting evidence either way (#2372).
    review_topology_triggered: bool = False
    # Challenger-sampling exploration (#325, ADR-0006 clause 8). When the router
    # ran a challenger instead of the winner for this story's dev slot, this
    # holds the decision (routing_key/challenger/winner) so the coordinator can
    # RECOVER if the challenger attempt fails: the story is retried through the
    # winner (below) and the challenger failure is recorded as an *exploration*
    # failure, not the story's final routing outcome.
    exploration_challenger: dict | None = None
    exploration_winner_dev_profile: object | None = None  # ModelProfile to retry through
    exploration_recovered: bool = False
    start_phase: Phase | None = None  # --from <phase>: skip phases before this
    stop_phase: Phase | None = None  # --until <phase>: stop after this phase
    _adaptive_decision: object | None = None  # AssignmentDecision, set after preflight
    _explicit_roles: set = field(default_factory=set)  # roles with explicit forge.yaml config
    complexity_routing_audit: dict | None = None  # set by _apply_complexity_adaptation
    # Per-story dollar allocation derived from the complexity band's observed
    # cost distribution (#2169). Set by _apply_story_allocation right after
    # preflight, before any runtime phase spends. Carries the basis
    # (substrate_band / configured_fallback), the band's median/p90/max and
    # sample count, and the rescaled per-role shares — so an overrun can be
    # reported against what a story of this kind normally costs rather than
    # against a flat constant. Mirrored into complexity_routing_audit, which is
    # what resume persistence already carries across a re-entry.
    story_allocation: dict | None = None
    # Reviewers whose measured cost exceeded their derived share this run.
    # Recorded as telemetry — never as an exclusion: dropping a planned
    # reviewer after it already spent is the silent work reduction #2169 exists
    # to remove.
    reviewer_budget_overruns: list = field(default_factory=list)
    # Set when a phase could not be funded from the story allocation.
    allocation_exhausted: dict | None = None
    # Per-role routing explainability block (#1391). Set from the assignment
    # decision at the preflight assign_models call site; persisted as a top-level
    # routing_decision key in the native per-run audit record.
    routing_decision: dict | None = None
    # Durable-phase-recovery outcome for a resumed attempt (#2155). Set by
    # _setup_resume_entry from coordinator.resume_persistence and emitted as the
    # audit's top-level phase_recovery key. None on a run that was not resumed —
    # a run that produced its own phase outputs recovered nothing. Non-null says
    # which phases the record restored (or why it could not), so a reader can
    # always tell a phase this attempt executed from one lifted off disk, and an
    # absent preflight block from a deliberately skipped one.
    phase_recovery: dict | None = None
    # Adaptive iteration limits (per-story). Populated by derive_limits() before
    # the dev/review loop starts. 0 means "not computed yet"; engine falls back
    # to config.retry.max_dev_iterations / max_review_cycles in that case.
    adaptive_dev_max: int = 0
    adaptive_review_max: int = 0
    adaptive_dev_timeout_seconds: int = 0
    # Per-story dollar cost ESTIMATE (historical-cost derived), not an enforced
    # budget. Informs routing/timeout scaling/telemetry; post-hoc dollar
    # governance lives at the sprint level (forge.yaml budget_usd), not here.
    adaptive_dev_cost_estimate_usd: float = 0.0
    # Deterministic per-cycle review planning price derived at seating from
    # observed review-cycle spend plus explicit headroom. REVIEW dispatch reads
    # this exact record first so it cannot re-price a cycle differently after
    # seating already granted it.
    adaptive_review_cycle_planning: dict | None = None
    # The portion of the allocation seating committed to verification (#2258).
    # Set from the seating reconciliation; keys mirror its reservation fields
    # (reserved_review_usd, reserved_review_cycles, review_cycle_cost_usd,
    # allocation_usd, action). REVIEW funds from this balance and DEV is refused
    # further attempts once the rest of the allocation is spent, so the seating
    # decision binds when it is exceeded rather than only when it is computed.
    # None/empty on runs where the reconciliation reserved nothing.
    # The record can be terminally RELEASED (#2340): once review reaches an
    # approve-equivalent path the seated cycles can no longer all occur, so the
    # release adds released=True plus retained_review_cycles/retained_review_usd,
    # released_review_usd, review_observed_usd, release_review_cycle and
    # release_reason. From then on only the retained amount is protected — a
    # released reserve stops being withheld from dev.
    review_funding_reservation: dict | None = None
    adaptive_limits_audit: dict = field(default_factory=dict)
    # One entry per development invocation whose timeout was shortened to fit
    # the enclosing story deadline (#2333). Each dict carries the requested
    # timeout, the story's remaining working seconds, the tail reserve, and what
    # was granted — the invocation ends on its own recorded, costed timeout
    # instead of being SIGKILLed by the sprint scheduler with no cost measured.
    dev_timeout_clamps: list[dict] = field(default_factory=list)
    review_early_terminated: bool = False  # True when early-termination triggered
    workspace_hygiene_audit: list[dict] = field(default_factory=list)
    # Per-phase hygiene gate audit entries. Each dict carries a "phase" key
    # ("PRE_DEV" / "PLAN" / "PLAN_REVIEW" / "REVIEW") plus phase-specific fields
    # (snapshot, modified, quarantined, quarantine_dir, offending_paths).
    # Set by VALIDATE when the dev cycle determined no commits were needed and the
    # handoff YAML documents this with all acceptance criteria MET and at least one
    # cited commit present in base-branch history. Distinguishes the deliberate
    # "work already complete" outcome from genuine missing-work failures.
    validate_already_complete: bool = False
    validate_already_complete_commits: list[dict] = field(default_factory=list)
    validate_already_complete_reason: str | None = None
    # ── P2 cleanup (post-APPROVE advisory iterations) ─────────────────────────
    # Set True when the coordinator re-enters DEV after an APPROVE that left
    # open P2 findings; cleared on cleanup-clean APPROVE, REQUEST_CHANGES
    # regression, or budget exhaustion. While True, the engine does NOT reset
    # the per-cycle dev budget on RETRY_DEV (cleanup iterations count against
    # the same pool as the original cycle).
    p2_cleanup_active: bool = False
    # Number of post-APPROVE cleanup dev iterations dispatched this run.
    # Counts against config.retry.p2_cleanup_max_iterations when that cap is
    # > 0; counts against the dev budget either way.
    p2_cleanup_iterations: int = 0
    # P2 findings (as dicts: file/line/description/suggestion) handed to the
    # dev agent on the next cleanup pass. Filtered each cleanup pass to the
    # subset of p2_cleanup_carry_keys still raised by the latest reviewer.
    p2_cleanup_findings: list[dict] = field(default_factory=list)
    # Stable fingerprints (file, line, description) of the original P2 set
    # captured at first cleanup entry. The cleanup loop only considers these
    # carried findings; new P2s raised by the reviewer after the carry is
    # captured do not extend the loop. Cleared when cleanup exits.
    p2_cleanup_carry_keys: list[list] = field(default_factory=list)
    # Audit trail for cleanup decisions: one entry per cleanup transition with
    # action ("enter" | "continue" | "exit_clean" | "exit_budget" | "exit_cap"
    # | "exit_regression" | "skip_disabled" | "skip_no_p2" | "skip_budget"
    # | "skip_budget_reserve" | "skip_cap"), pre-pass P2 count, post-pass P2
    # count, budget remaining, review_cycle, and dev_iteration at the decision
    # point.
    p2_cleanup_audit: list[dict] = field(default_factory=list)
    # ── Symptom-verification test escalations (#1560) ─────────────────────────
    # One entry per P2→P1 escalation applied because a bug-fix PR's reviewer
    # flagged an absent seam-level integration test for the closing bug's symptom
    # path. Each dict carries review_cycle, file, line, reporter, description, and
    # original/effective severity so the rule's hit-rate becomes queryable.
    symptom_test_escalations: list[dict] = field(default_factory=list)
    # ── Trust checks (#1851) ──────────────────────────────────────────────────
    # Structured pass/fail results of coordinator-computed trust checks, keyed by
    # check name (e.g. "reviewer_tree_currency"). Populated mechanically during
    # REVIEW (never from LLM prose). The aggregate trust_status on the native
    # per-run record is derived from these entries by trust_status.derive_trust_status
    # — any failed check taints the run (ADR-0006 clause 4); a run type with no
    # implemented check contributes no entry and stays "unchecked" (admissible).
    trust_checks: dict[str, dict] = field(default_factory=dict)
    # ── No-judgment agent invocation failures (#1951) ──────────────────────────
    # Every agent invocation this run that failed WITHOUT producing any model
    # output (auth rejection, transport drop, startup failure, timeout before any
    # text existed). Serialized AgentInvocationFailure dicts, appended by
    # coordinator.agent_failure.record_invocation_failure. Present regardless of
    # how the phase recovered, so the audit can answer "did a model actually
    # judge this?" without re-deriving it from prose.
    agent_invocation_failures: list[dict] = field(default_factory=list)
    # Set when the RUN's terminal outcome is "no judgment was obtained" rather
    # than any model verdict: the structured cause plus the operator message.
    # None on every run whose outcome is backed by real model output. A run with
    # this set carries a failed ``agent_judgment_obtained`` trust check, so it is
    # tainted and teaches nothing (ADR-0006 clause 4).
    infrastructure_failure: dict | None = None
    # ── Shared run-infrastructure failures (#2107) ────────────────────────────
    # Failures of a resource every story in a sprint shares — a path outside the
    # workspace that all workers write, such as the rolling advisory-conventions
    # artifact. Each entry: component, path, error, error_type. Recorded here,
    # and rendered into the audit, so the failure is attributable to the
    # infrastructure rather than to whichever story was executing when it
    # surfaced. Non-fatal by construction: the story's own outcome, audit, and
    # cost accounting are unaffected.
    shared_infrastructure_failures: list[dict] = field(default_factory=list)
    # ── Abnormal termination (#2030) ──────────────────────────────────────────
    # Set when the run did not end by its own state machine: the worker raised,
    # the worker's deadline expired, or the launch guard dropped the story before
    # it was ever dispatched. Carries the kind, the primary cause, and where that
    # cause was observed (see theforge.sprint.abnormal). None for every run that
    # ended normally. Recorded here so the account of the failure is the run's
    # own structured telemetry rather than an agent's prose about itself.
    abnormal_termination: dict | None = None
    # Phases that completed with a reviewer/agent pool shrunk by substrate
    # failures. Each entry: phase, pool_size, lost (names), remaining, failures.
    # A degraded-pool completion is not the same kind of result as a full-pool
    # one and must stay distinguishable downstream.
    degraded_pools: list[dict] = field(default_factory=list)

    def __post_init__(self, dev_iteration: int) -> None:
        # Sync the budget's per-cycle counter with the constructor kwarg.
        # This preserves backward compatibility: CoordinatorState(dev_iteration=N)
        # sets budget.cycle_count = N, and the dev_iteration property reads it back.
        self.budget.cycle_count = dev_iteration

    @property
    def total_dev_cost(self) -> float:
        return sum(r.cost_usd or 0.0 for r in self.dev_results)

    @property
    def total_dev_cost_measured(self) -> float | None:
        """Dev cost, or ``None`` when any dev attempt's cost was unmeasured.

        Unlike :attr:`total_dev_cost` (which coerces an unmeasured ``None`` to
        ``$0.00``), this preserves the cost-unknown signal so the ledger never
        silently records unmeasured spend as free. If *any* contributing dev
        result reports ``cost_usd is None``, the whole aggregate is unknown —
        collapsing a mix to a measured subtotal would hide the unmeasured
        attempt. Empty ``dev_results`` means no spend, so ``0.0``.
        """
        if any(r.cost_usd is None for r in self.dev_results):
            return None
        return sum(r.cost_usd or 0.0 for r in self.dev_results)

    @property
    def reviewer_cycles_run(self) -> int:
        """Review cycles attributable to a reviewer verdict.

        This is the reviewer-facing count: it feeds ``review_cycles_total`` in the
        audit, which the adaptive iteration learner percentiles to set future
        ``max_review_cycles``. A cycle VALIDATE bought for its own gate or
        convention finding is subtracted out — counting it would teach the router
        that gate failures mean reviewers need more cycles (#1981).

        Takes whichever evidence is larger: recorded reviewer telemetry, or the
        counter minus VALIDATE's share. Reviewer telemetry alone misses a cycle
        that escalated before it was recorded; the counter alone can be reset to
        0 on extend/reject while the VALIDATE counter stays monotonic. Using the
        larger of the two never reports fewer reviewer cycles than actually ran,
        and never goes negative.
        """
        return max(
            len(self.review_iteration_telemetry),
            self.review_cycle - self.validate_opened_review_cycles,
        )

    @property
    def review_cycles_spent(self) -> int:
        """Review-cycle budget consumed, whoever spent it.

        Reviewer cycles plus cycles VALIDATE opened for a coordinator-raised
        blocking finding. This is the budget-facing count, used by usage
        reporting so a story that spent review cycles on gate findings is not
        reported as having spent none.
        """
        return self.reviewer_cycles_run + self.validate_opened_review_cycles

    @property
    def total_review_cost(self) -> float:
        return sum(r.cost_usd or 0.0 for r in self.review_agent_results)

    @property
    def total_review_cost_measured(self) -> float | None:
        """Review cost, or ``None`` when any review attempt's cost was unmeasured.

        Mirrors :attr:`total_dev_cost_measured`: a run killed before its
        cost-bearing result event reports ``cost_usd is None``, and coercing that
        to ``$0.00`` would record unmeasured spend as free in the audit. Empty
        ``review_agent_results`` means no spend, so ``0.0``.
        """
        if any(r.cost_usd is None for r in self.review_agent_results):
            return None
        return sum(r.cost_usd or 0.0 for r in self.review_agent_results)

    @property
    def total_preflight_cost(self) -> float:
        if self.preflight_result is None:
            return 0.0
        attempts = self.preflight_result.raw.get("attempts")
        if isinstance(attempts, list):
            return sum(float(a.get("cost_usd") or 0.0) for a in attempts if isinstance(a, dict))
        return self.preflight_result.cost_usd or 0.0

    @property
    def total_preflight_cost_measured(self) -> float | None:
        """Preflight cost, or ``None`` when a preflight attempt's cost was unmeasured.

        Mirrors :attr:`total_dev_cost_measured`: preflight can run over a CLI
        transport (e.g. codex) that reports no cost, and coercing that to
        ``$0.00`` would hide the spend. No preflight result means preflight did
        not run, so ``0.0`` (genuinely no spend). A result whose cost is ``None``
        — or whose attempts include an unmeasured one — is cost-unknown.
        """
        if self.preflight_result is None:
            return 0.0
        attempts = self.preflight_result.raw.get("attempts")
        if isinstance(attempts, list):
            costs = [a.get("cost_usd") for a in attempts if isinstance(a, dict)]
            if any(c is None for c in costs):
                return None
            return sum(float(c or 0.0) for c in costs)
        return self.preflight_result.cost_usd

    @property
    def total_plan_cost(self) -> float:
        return sum(r.cost_usd or 0.0 for r in self.plan_results)

    @property
    def total_plan_cost_measured(self) -> float | None:
        """Plan cost, or ``None`` when any plan attempt's cost was unmeasured.

        Mirrors :attr:`total_dev_cost_measured` so a plan run killed before its
        cost-bearing result event is recorded as cost-unknown rather than free.
        Empty ``plan_results`` means no spend, so ``0.0``.
        """
        if any(r.cost_usd is None for r in self.plan_results):
            return None
        return sum(r.cost_usd or 0.0 for r in self.plan_results)

    @property
    def total_plan_review_cost(self) -> float:
        return sum(r.cost_usd or 0.0 for r in self.plan_review_results)

    @property
    def total_plan_review_cost_measured(self) -> float | None:
        """Plan-review cost, or ``None`` when a reviewer attempt's cost was unmeasured.

        Mirrors :attr:`total_dev_cost_measured` so a plan-review run killed before
        its cost-bearing result event is recorded as cost-unknown rather than
        free. Empty ``plan_review_results`` means no spend, so ``0.0``.
        """
        if any(r.cost_usd is None for r in self.plan_review_results):
            return None
        return sum(r.cost_usd or 0.0 for r in self.plan_review_results)

    @property
    def total_story_validation_cost(self) -> float:
        if self.story_validation_result is None:
            return 0.0
        return (
            self.story_validation_result.cost_usd
            if self.story_validation_result.cost_usd is not None
            else 0.0
        )

    @property
    def total_cost(self) -> float:
        return (
            self.total_dev_cost
            + self.total_review_cost
            + self.total_preflight_cost
            + self.total_plan_cost
            + self.total_plan_review_cost
            + self.total_story_validation_cost
        )

    @property
    def total_cost_measured(self) -> float | None:
        """Grand-total cost, or ``None`` when any contributing phase was unmeasured.

        Sums the per-phase ``*_measured`` aggregates so a single kill-path run
        with cost-unknown poisons the whole total to ``None`` rather than letting
        a coerced ``$0.00`` understate real spend. When every phase is measured,
        equals :attr:`total_cost`.
        """
        parts = [
            self.total_dev_cost_measured,
            self.total_review_cost_measured,
            self.total_preflight_cost_measured,
            self.total_plan_cost_measured,
            self.total_plan_review_cost_measured,
            self.total_story_validation_cost,
        ]
        if any(p is None for p in parts):
            return None
        return sum(p or 0.0 for p in parts)


# dev_iteration property — added after @dataclass so the decorator sees the
# InitVar annotation cleanly, then we replace the class-level attribute with a
# property that reads/writes budget.cycle_count.
CoordinatorState.dev_iteration = property(  # type: ignore[attr-defined,assignment]
    lambda self: self.budget.cycle_count,
    lambda self, val: setattr(self.budget, "cycle_count", val),
)


@dataclass
class CoordinatorResult:
    """Final result from a coordinator run."""

    success: bool
    phase: Phase
    state: CoordinatorState
    message: str
    merge: dict | None = None
    landing_status: str | None = None
    # True when the run ended because no agent judgment could be obtained
    # (#1951). Distinguishes an infrastructure abort from a story-level
    # ESCALATE for every caller that only sees the result: the terminal phase is
    # still ESCALATE, but this run made no statement about the story and must
    # not be reported, notified, or persisted as if it had.
    infrastructure_failure: bool = False
    # True when the reserved DEV slot returned a no-charge, no-artifact failure
    # before any actual development attempt began, so engine.py should release
    # the just-consumed RetryBudget slot.
    unused_dev_iteration: bool = False
    # The final runtime config the coordinator actually executed under. This is
    # authoritative for audit emission when preflight or resume recovery rewrites
    # the in-memory config after the CLI loaded forge.yaml.
    runtime_config: ForgeConfig | None = None
