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
class DevIterationTelemetry:
    """Per-dev-iteration telemetry captured for audit output."""

    iteration: int
    max_iterations: int
    cost_usd: float | None
    duration_s: float
    cycle: int = 0  # which review cycle this dev iteration belongs to
    gate_result: str | None = None
    failed_tests: list[str] = field(default_factory=list)
    existing_test_failures: bool = False
    is_timeout: bool = False
    files_changed: list[str] = field(default_factory=list)
    files_changed_count: int = 0
    tests_fixed_count: int = 0
    meaningful_progress: bool | None = None
    sandboxed: bool = True
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


@dataclass(frozen=True)
class GateDebugTelemetry:
    """Diagnostic command telemetry captured when the validation gate times out."""

    iteration: int
    command: str
    ran: bool
    timeout_s: int
    exit_code: int | None
    output_tail: str
    output_truncated: bool


@dataclass(frozen=True)
class ReviewIterationTelemetry:
    """Per-review-cycle telemetry captured for audit output."""

    iteration: int
    max_iterations: int
    cost_usd: float
    duration_s: float
    verdict: str
    findings_by_severity: dict[str, int]
    new_findings_by_severity: dict[str, int]
    repeated_findings_by_severity: dict[str, int]
    novel_findings: int
    restated_findings: int


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
    dev_session_id: str | None = None
    pending_dev_transport_retry_count: int = 0
    pending_dev_transport_retry_events: list[dict[str, Any]] = field(default_factory=list)
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
    preflight_complexity_score: int | None = None  # 1-10 bounded integer score
    preflight_sufficiency: str | None = None  # "implementation_ready" | "needs_planning"
    preflight_work_type: str | None = None  # "feature" | "refactor" | "mechanical" | "bug"
    preflight_contract_change: bool = False  # story intentionally alters a tested contract
    preflight_bundle_candidate: bool = False
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
    plan_review_failures: list[dict] = field(
        default_factory=list
    )  # per-reviewer failures: {"attempt": int, "reviewer": str, "errors": list[str], ...}
    plan_attempt_metadata: list[dict] = field(
        default_factory=list
    )  # per-attempt: {files_touched, p1_count, p2_count, finding_themes}
    plan_regen_disposition: str | None = None  # "patch" | "backtrack" | "escalate"
    plan_backtrack_used: bool = False  # True once the backtrack regen has been dispatched
    log_dir: Path | None = None  # per-story log directory under <project_root>/.forge/logs/
    error: str | None = None
    error_type: str | None = None
    sandboxed: bool = False  # True if sandbox isolation was available at dev-phase entry
    dev_escalated: bool = False  # True once model escalation has occurred this run
    timeout_escalation_used: bool = (
        False  # True once a timeout escalation has fired this sprint; gates re-escalation
    )
    timeout_escalation_audit: dict | None = None  # original/new model+timeout recorded for audit
    plan_escalated: bool = False  # True once plan model escalation has occurred this run
    plan_escalation_note: str | None = None  # escalation context injected into regen prompt
    retry_reason: RetryReason | None = None  # see RetryReason enum for valid values
    last_cycle_reviewer_results: list[tuple[str, ReviewResult]] = field(
        default_factory=list
    )  # (profile_name, ReviewResult) pairs from the most recent pool run
    finding_registry: list[FindingRecord] = field(default_factory=list)
    dev_prompt_injected_finding_ids: list[list[str]] = field(default_factory=list)
    dev_handoff_snapshots: list[dict[str, Any] | None] = field(default_factory=list)
    dev_iteration_telemetry: list[DevIterationTelemetry] = field(default_factory=list)
    gate_debug_telemetry: list[GateDebugTelemetry] = field(default_factory=list)
    review_iteration_telemetry: list[ReviewIterationTelemetry] = field(default_factory=list)
    context_manifests: list[dict] = field(default_factory=list)
    # One entry per dev invocation (same index as dev_results).
    # Each entry is the parsed handoff-file dict, or None if absent/unparseable.
    # Stable record of all findings across cycles, classified by finding_classifier
    last_dev_start_commit: str | None = None
    # HEAD commit hash captured before each dev iteration; used by finding_classifier
    # to compute git diff --name-only for changed-file correlation.
    escalate_decision: str | None = None  # "approve" | "reject" | "continue"
    escalate_reason: str | None = None  # human-readable escalation reason
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
    sprint_promotions: dict[str, str] = field(default_factory=dict)
    # Maps complexity (LOW/MEDIUM/HIGH) → promoted tier string.
    # Sticky within a sprint (single forge process lifetime); resets on process exit.
    start_phase: Phase | None = None  # --from <phase>: skip phases before this
    stop_phase: Phase | None = None  # --until <phase>: stop after this phase
    _adaptive_decision: object | None = None  # AssignmentDecision, set after preflight
    _explicit_roles: set = field(default_factory=set)  # roles with explicit forge.yaml config
    complexity_routing_audit: dict | None = None  # set by _apply_complexity_adaptation
    # Adaptive iteration limits (per-story). Populated by derive_limits() before
    # the dev/review loop starts. 0 means "not computed yet"; engine falls back
    # to config.retry.max_dev_iterations / max_review_cycles in that case.
    adaptive_dev_max: int = 0
    adaptive_review_max: int = 0
    adaptive_dev_timeout_seconds: int = 0
    adaptive_dev_budget_usd: float = 0.0
    adaptive_limits_audit: dict = field(default_factory=dict)
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
    # | "skip_cap"), pre-pass P2 count, post-pass P2 count, budget remaining,
    # review_cycle, and dev_iteration at the decision point.
    p2_cleanup_audit: list[dict] = field(default_factory=list)

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
    def total_review_cost(self) -> float:
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
    def total_plan_review_cost(self) -> float:
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
