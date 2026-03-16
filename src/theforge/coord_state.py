"""Coordinator state: enums, dataclasses, logging helpers, and shell utility.

These are extracted from coordinator.py to avoid circular imports — every
other coord_* module depends on items defined here.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from .review import ReviewResult
from .runner import AgentResult, LogLevel

# ── Phase enum ────────────────────────────────────────────────────────


class Phase(Enum):
    """Coordinator state machine phases."""

    INIT = auto()
    WORKSPACE = auto()
    PREFLIGHT = auto()
    PLAN = auto()
    DEV = auto()
    VALIDATE = auto()
    REVIEW = auto()
    HUMAN_REVIEW = auto()
    DONE = auto()
    ESCALATE = auto()


# ── Dataclasses ──────────────────────────────────────────────────────


@dataclass
class ReviewCycleMetadata:
    """Per-cycle metadata for audit logging."""

    pool_models: list[str]  # profile names of all pool agents
    successful: list[str]  # profile names that succeeded
    failed: list[str]  # profile names that failed
    synthesized: bool  # whether synthesis ran
    parse_retries: int = 0  # parse/schema retry count for this cycle
    failed_detail: dict[str, str] = field(default_factory=dict)  # profile → "exit=N"


@dataclass
class CoordinatorState:
    """Mutable state tracking for a single task execution."""

    phase: Phase = Phase.INIT
    started_at: str | None = None  # ISO timestamp set at INIT
    workspace_path: Path | None = None
    branch_name: str | None = None
    dev_session_id: str | None = None
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
    plan_result: AgentResult | None = None
    plan_output: str | None = None  # contents of forge_plan.md, passed to dev
    error: str | None = None
    dev_escalated: bool = False  # True once model escalation has occurred this run
    retry_reason: str | None = (
        None  # "review_changes" | "gate_fail" | "dirty_worktree" | "extend" | "reject" | None
    )

    @property
    def total_dev_cost(self) -> float:
        return sum(r.cost_usd for r in self.dev_results)

    @property
    def total_review_cost(self) -> float:
        return sum(r.cost_usd for r in self.review_agent_results)

    @property
    def total_preflight_cost(self) -> float:
        return self.preflight_result.cost_usd if self.preflight_result else 0.0

    @property
    def total_plan_cost(self) -> float:
        return self.plan_result.cost_usd if self.plan_result else 0.0

    @property
    def total_cost(self) -> float:
        return (
            self.total_dev_cost
            + self.total_review_cost
            + self.total_preflight_cost
            + self.total_plan_cost
        )


@dataclass
class CoordinatorResult:
    """Final result from a coordinator run."""

    success: bool
    phase: Phase
    state: CoordinatorState
    message: str
    merge: dict | None = None


# ── Logging ──────────────────────────────────────────────────────────

_LOG_LEVEL: LogLevel = LogLevel.PROGRESS


def set_log_level(level: LogLevel) -> None:
    global _LOG_LEVEL
    _LOG_LEVEL = level


def _fmt_duration(seconds: float) -> str:
    """Format duration as '2h 14m 3s', '14m 3s', or '47s'."""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _log(msg: str) -> None:
    """Print coordinator status to stderr (always shown)."""
    print(f"[forge] {msg}", file=sys.stderr, flush=True)


def _log_verbose(msg: str) -> None:
    """Print coordinator detail to stderr (verbose mode only)."""
    if _LOG_LEVEL >= LogLevel.VERBOSE:
        print(f"[forge] {msg}", file=sys.stderr, flush=True)


def _log_phase(phase: Phase, detail: str = "") -> None:
    suffix = f"   {detail}" if detail else ""
    _log(f"▸ {phase.name}{suffix}")


# ── Run ID ───────────────────────────────────────────────────────────


def _generate_run_id() -> str:
    """Return a short random hex run ID (12 chars)."""
    return secrets.token_hex(6)


# ── Shell helper ─────────────────────────────────────────────────────


def _run_shell(cmd: str, cwd: Path, timeout: int = 120) -> tuple[bool, str]:
    """Run a shell command. Returns (success, combined output)."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=timeout,
        )
        output = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s: {cmd}"
    except Exception as e:
        return False, f"ERROR: {e}"
