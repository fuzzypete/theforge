"""Coordinator state: enums and dataclasses only (stdlib-only imports).

Contains Phase, ReviewCycleMetadata, CoordinatorState, and CoordinatorResult.
All helper functions (logging, shell, run-id) live in coord_util.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .review import ReviewResult
    from .runner import AgentResult
    from .task import PlanData

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
    ESCALATE = auto()


# ── Disposition enum ──────────────────────────────────────────────────


Disposition = Literal[
    "unresolved", "fixed", "regression", "net_new", "corroborated_new", "downgraded"
]


# ── Dataclasses ──────────────────────────────────────────────────────


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
class ReviewCycleMetadata:
    """Per-cycle metadata for audit logging."""

    pool_models: list[str]  # profile names of all pool agents
    successful: list[str]  # profile names that succeeded
    failed: list[str]  # profile names that failed
    synthesized: bool  # whether synthesis ran
    parse_retries: int = 0  # parse/schema retry count for this cycle
    failed_detail: dict[str, str] = field(default_factory=dict)  # profile → "exit=N"


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
    workspace_path: Path | None = None
    branch_name: str | None = None
    dev_session_id: str | None = None
    plan_session_id: str | None = None
    plan_review_session_ids: dict[str, str] = field(default_factory=dict)  # keyed by profile.name
    reviewer_session_ids: dict[str, str] = field(default_factory=dict)  # keyed by profile.name
    review_cycle: int = 0  # which dev→review loop we're on
    dev_iteration: int = 0  # retries within the current review cycle
    dev_results: list[AgentResult] = field(default_factory=list)
    dev_durations: list[float] = field(default_factory=list)  # wall-clock seconds per dev call
    review_agent_results: list[AgentResult] = field(default_factory=list)
    review_durations: list[float] = field(
        default_factory=list
    )  # wall-clock seconds per review call
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
    preflight_result: AgentResult | None = None
    plan_results: list[AgentResult] = field(default_factory=list)
    plan_output: str | None = (
        None  # contents of forge_plan.md, passed to dev (raw string for audit)
    )
    plan_structured: PlanData | None = None  # parsed structured plan; None if fallback to markdown
    plan_review_decision: str | None = None  # "approve" | "regenerate" | "abandon"
    plan_regen_count: int = 0  # number of plan regen attempts so far
    plan_review_waited_seconds: float | None = None
    plan_review_mode: str | None = None  # "interactive" | "remote" | "advisory-timeout"
    plan_agent_review_findings: str | None = None  # rejection findings for regen feedback
    plan_review_results: list[AgentResult] = field(
        default_factory=list
    )  # separate from plan_results to avoid corrupting plan generation cost tracking
    log_dir: Path | None = None  # per-story log directory under <project_root>/.forge/logs/
    error: str | None = None
    dev_escalated: bool = False  # True once model escalation has occurred this run
    retry_reason: str | None = (
        None
        # "review_changes" | "gate_fail" | "dirty_worktree" | "extend"
        # | "reject" | "timeout_resume" | None
    )
    last_cycle_reviewer_results: list[tuple[str, ReviewResult]] = field(
        default_factory=list
    )  # (profile_name, ReviewResult) pairs from the most recent pool run
    finding_registry: list[FindingRecord] = field(default_factory=list)
    # Stable record of all findings across cycles, classified by finding_classifier
    last_dev_start_commit: str | None = None
    # HEAD commit hash captured before each dev iteration; used by finding_classifier
    # to compute git diff --name-only for changed-file correlation.
    escalate_decision: str | None = None  # "approve" | "reject" | "continue"
    escalate_reason: str | None = None  # human-readable escalation reason

    @property
    def total_dev_cost(self) -> float:
        return sum(r.cost_usd or 0.0 for r in self.dev_results)

    @property
    def total_review_cost(self) -> float:
        return sum(r.cost_usd or 0.0 for r in self.review_agent_results)

    @property
    def total_preflight_cost(self) -> float:
        return self.preflight_result.cost_usd or 0.0 if self.preflight_result else 0.0

    @property
    def total_plan_cost(self) -> float:
        return sum(r.cost_usd or 0.0 for r in self.plan_results)

    @property
    def total_plan_review_cost(self) -> float:
        return sum(r.cost_usd or 0.0 for r in self.plan_review_results)

    @property
    def total_cost(self) -> float:
        return (
            self.total_dev_cost
            + self.total_review_cost
            + self.total_preflight_cost
            + self.total_plan_cost
            + self.total_plan_review_cost
        )


@dataclass
class CoordinatorResult:
    """Final result from a coordinator run."""

    success: bool
    phase: Phase
    state: CoordinatorState
    message: str
    merge: dict | None = None
