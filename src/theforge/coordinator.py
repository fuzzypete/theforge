"""Coordinator: deterministic state machine for dev→review loops.

The coordinator is the heart of TheForge. It is NOT an LLM — it is a Python
program that mechanically orchestrates agent invocations. Every decision is
deterministic. Every boundary is a validation checkpoint.

State machine:
    INIT → WORKSPACE → PREFLIGHT → DEV → VALIDATE → REVIEW → (loop or DONE/ESCALATE)

Transitions:
    INIT → WORKSPACE:       Always (create workspace)
    WORKSPACE → PREFLIGHT:  Workspace created successfully
    PREFLIGHT → DEV:        Verdict is PROCEED (or agent failed — fail-open)
    PREFLIGHT → DONE:       Verdict is ALREADY_DONE (spec satisfied on main)
    PREFLIGHT → ESCALATE:   Verdict is BLOCKED (spec is stale/invalid)
    DEV → VALIDATE:         Dev agent finished (success or failure)
    VALIDATE → REVIEW:      Gate produced handoff.yaml with PASS
    VALIDATE → DEV:         Gate failed, retries remaining
    VALIDATE → ESCALATE:    Gate failed, no retries left
    REVIEW → DONE:          Review verdict is APPROVE
    REVIEW → DEV:           Review verdict is REQUEST_CHANGES, retries remaining
    REVIEW → ESCALATE:      Review verdict is REQUEST_CHANGES, no retries left
"""

from __future__ import annotations

import datetime
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

import yaml

from .config import ForgeConfig
from .review import ReviewResult, findings_to_markdown, parse_review_output
from .runner import AgentResult, LogLevel, log_agent_result, run_agent, run_agent_pool
from .task import (
    TaskSpec,
    build_dev_prompt,
    build_preflight_prompt,
    build_review_prompt,
    build_synthesis_prompt,
    load_spec,
)


class Phase(Enum):
    """Coordinator state machine phases."""

    INIT = auto()
    WORKSPACE = auto()
    PREFLIGHT = auto()
    DEV = auto()
    VALIDATE = auto()
    REVIEW = auto()
    HUMAN_REVIEW = auto()
    DONE = auto()
    ESCALATE = auto()


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
    human_review_decision: str | None = None  # "approve" | "reject" | "escalate"
    human_review_feedback: str | None = None  # rejection text from human
    preflight_verdict: str | None = None  # "PROCEED" | "ALREADY_DONE" | "BLOCKED"
    preflight_reason: str | None = None
    preflight_result: AgentResult | None = None
    error: str | None = None

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
    def total_cost(self) -> float:
        return self.total_dev_cost + self.total_review_cost + self.total_preflight_cost


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


# ── Human review ─────────────────────────────────────────────────────


def _human_review(
    state: CoordinatorState,
    parsed_review: "ReviewResult",  # noqa: F821
    workspace_path: "Path",  # noqa: F821
    branch_name: str,
) -> tuple[str, str | None]:
    """Prompt the human operator for a review decision.

    Returns (decision, feedback) where decision is one of:
      "approve"   → transition to DONE
      "reject"    → transition back to DEV with feedback text
      "escalate"  → transition to ESCALATE
    """

    p1 = sum(1 for f in parsed_review.findings if f.severity == "P1")
    p2 = sum(1 for f in parsed_review.findings if f.severity == "P2")
    finding_summary = f"{p1} P1, {p2} P2" if (p1 or p2) else "no findings"

    _log("─── Human Review ───")
    _log(f"  Verdict:   {parsed_review.verdict} ({finding_summary})")
    _log(f"  Summary:   {parsed_review.summary}")
    _log(f"  Workspace: {workspace_path}")
    _log(f"  Branch:    {branch_name}")
    _log(f"  Cost:      ${state.total_cost:.3f}")
    _log("")
    _log("Options:")
    _log("  [a]pprove  → DONE (ready to merge)")
    _log("  [r]eject   → send findings back to dev")
    _log("  [e]scalate → give up")
    _log("")

    while True:
        print("[forge] Choice [a/r/e]: ", end="", file=sys.stderr, flush=True)
        raw = sys.stdin.readline()
        if not raw:
            # EOF (Ctrl+D or piped input exhausted) → treat as escalate
            _log("EOF on stdin — escalating.")
            return "escalate", None
        choice = raw.strip().lower()
        if choice in ("a", "approve"):
            return "approve", None
        if choice in ("e", "escalate"):
            return "escalate", None
        if choice in ("r", "reject"):
            _log("Enter your findings (empty line to finish):")
            lines: list[str] = []
            while True:
                print("> ", end="", file=sys.stderr, flush=True)
                line = sys.stdin.readline()
                if not line:
                    # EOF during findings input
                    break
                stripped = line.rstrip("\n")
                if stripped == "":
                    break
                lines.append(stripped)
            return "reject", "\n".join(lines)
        _log("Invalid choice. Enter 'a', 'r', or 'e'.")


# ── Shell helpers ────────────────────────────────────────────────────


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


# ── Merge ────────────────────────────────────────────────────────────


def _merge_branch(
    project_root: Path,
    base_branch: str,
    branch_name: str,
    slug: str,
    workspace_path: Path,
) -> dict:
    """Merge branch_name into base_branch in project_root.

    Returns a merge info dict with keys: attempted, merged, base_branch, error.
    """
    info: dict = {
        "attempted": True,
        "merged": False,
        "base_branch": base_branch,
        "error": None,
    }

    # Safety check 1: verify base branch exists
    ok, out = _run_shell(f"git branch --list {base_branch}", project_root)
    if not ok or not out.strip():
        info["error"] = f"Base branch {base_branch!r} not found in project root"
        _log(f"Auto-merge skipped: {info['error']}")
        return info

    # Safety check 2: no uncommitted changes in project root
    ok, dirty = _run_shell("git status --porcelain", project_root)
    if ok and dirty.strip():
        info["error"] = f"Uncommitted changes in project root: {dirty.strip()[:200]}"
        _log(f"Auto-merge skipped: {info['error']}")
        return info

    # Safety check 3: verify branch has commits not on base
    ok, log_out = _run_shell(f"git log {base_branch}..{branch_name} --oneline", project_root)
    if not ok or not log_out.strip():
        info["error"] = f"Branch {branch_name!r} has no commits ahead of {base_branch!r}"
        _log(f"Auto-merge skipped: {info['error']}")
        return info

    # Checkout base branch
    ok, out = _run_shell(f"git checkout {base_branch}", project_root)
    if not ok:
        info["error"] = f"Failed to checkout {base_branch!r}: {out}"
        _log(f"Auto-merge failed: {info['error']}")
        return info

    # Attempt fast-forward merge
    ok, out = _run_shell(f"git merge --ff-only {branch_name}", project_root)
    if not ok:
        _log(f"Fast-forward merge failed, falling back to regular merge: {out}")
        ok, out = _run_shell(f"git merge --no-edit {branch_name}", project_root)

    if not ok:
        info["error"] = f"Merge failed: {out}"
        _log(f"Auto-merge failed: {info['error']}")
        return info

    info["merged"] = True
    _log(f"Auto-merge succeeded: {branch_name} → {base_branch}")

    # Worktree cleanup (best-effort)
    worktree_rel = f".forge/worktrees/{slug}"
    ok_rm, rm_out = _run_shell(f"git worktree remove {worktree_rel}", project_root)
    if not ok_rm:
        _log(f"Warning: worktree cleanup failed (harmless): {rm_out}")
    else:
        _log(f"Worktree removed: {worktree_rel}")

    return info


# ── Preflight ───────────────────────────────────────────────────────


_VALID_PREFLIGHT_VERDICTS = frozenset({"PROCEED", "ALREADY_DONE", "BLOCKED"})


def _load_file_scope_contents(task: TaskSpec, project_root: Path) -> dict[str, str]:
    """Read current contents of files in task.file_scope.

    Returns a dict of {relative_path: content}. Missing files are
    silently skipped (the preflight agent will note their absence).
    """
    contents: dict[str, str] = {}
    for rel_path in task.file_scope:
        full_path = project_root / rel_path
        if full_path.is_file():
            try:
                contents[rel_path] = full_path.read_text(encoding="utf-8")
            except OSError:
                pass
    return contents


def _parse_preflight_verdict(output: str) -> tuple[str, str]:
    """Extract verdict and reason from preflight agent output.

    Returns (verdict, reason). If parsing fails, returns ("PROCEED", reason)
    to avoid blocking on a broken preflight — it's cheaper to try DEV than
    to stall.
    """
    # Extract YAML block from markdown fences
    yaml_text = output
    if "```yaml" in output:
        start = output.index("```yaml") + len("```yaml")
        end = output.index("```", start)
        yaml_text = output[start:end]
    elif "```" in output:
        start = output.index("```") + len("```")
        end = output.index("```", start)
        yaml_text = output[start:end]

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return "PROCEED", f"Failed to parse preflight YAML; proceeding anyway. Raw: {output[:200]}"

    if not isinstance(parsed, dict):
        return "PROCEED", "Preflight output is not a dict; proceeding anyway."

    verdict = str(parsed.get("verdict", "PROCEED")).upper()
    reason = str(parsed.get("reason", "(no reason provided)"))

    if verdict not in _VALID_PREFLIGHT_VERDICTS:
        return "PROCEED", f"Unknown preflight verdict {verdict!r}; proceeding anyway. {reason}"

    return verdict, reason


# ── Workspace ────────────────────────────────────────────────────────


def _create_workspace(
    config: ForgeConfig, task: TaskSpec
) -> tuple[Path | None, str | None, str | None]:
    """Create an isolated workspace. Returns (path, branch, error)."""
    slug = task.slug
    cmd = config.workspace.create_command.format(slug=slug)
    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=slug)
    branch_name = config.workspace.branch_pattern.format(slug=slug)

    if workspace_path.exists():
        _log(f"Workspace already exists: {workspace_path}")
        return workspace_path, branch_name, None

    _log(f"Creating workspace: {cmd}")
    ok, output = _run_shell(cmd, config.project_root)
    if not ok:
        return None, None, f"Failed to create workspace: {output}"

    if not workspace_path.exists():
        return None, None, f"Workspace path does not exist after creation: {workspace_path}"

    return workspace_path, branch_name, None


# ── Validation ───────────────────────────────────────────────────────


def _read_gate_decision(
    config: ForgeConfig, workspace_path: Path
) -> tuple[str | None, str | None]:
    """Read gate decision from handoff.yaml. Returns (decision, error)."""
    handoff_path = workspace_path / config.validation.handoff_file
    if not handoff_path.exists():
        return None, f"handoff file not found: {handoff_path}"

    try:
        with open(handoff_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        return None, f"Failed to parse handoff YAML: {e}"
    except OSError as e:
        return None, f"Failed to read handoff file: {e}"

    decision = data.get(config.validation.gate_decision_key)
    if decision is None:
        return None, (
            f"Key {config.validation.gate_decision_key!r} not found in "
            f"{config.validation.handoff_file}"
        )

    return str(decision).upper(), None


def _run_gate(
    config: ForgeConfig, workspace_path: Path, task: "TaskSpec | None" = None
) -> tuple[str | None, str | None]:
    """Run the gate command and read the decision. Returns (decision, error).

    Supports two modes:
    1. Handoff-based: gate_command writes a handoff file with a gate decision key.
    2. Exit-code-based: if no handoff_file/gate_decision_key is configured,
       gate PASS/FAIL is determined purely by the command's exit code.

    The gate_command supports {pytest_target} substitution from the TaskSpec.
    """
    # Delete stale handoff to prevent a prior PASS from leaking through on gate failure
    # (only relevant in handoff-based mode)
    if config.validation.handoff_file:
        stale_handoff = workspace_path / config.validation.handoff_file
        if stale_handoff.exists():
            try:
                stale_handoff.unlink()
            except OSError as e:
                return None, f"Cannot remove stale handoff file: {e}"

    # Substitute task-specific placeholders in the gate command
    gate_cmd = config.validation.gate_command
    if task is not None:
        pytest_target = task.pytest_target or "tests/"
        gate_cmd = gate_cmd.replace("{pytest_target}", pytest_target)
        gate_cmd = gate_cmd.replace("{slug}", task.slug)

    _log_verbose(f"Running gate: {gate_cmd}")
    gate_timeout = config.validation.gate_timeout or 600
    ok, output = _run_shell(
        gate_cmd,
        workspace_path,
        timeout=gate_timeout,
    )

    # Exit-code mode: if no handoff_file configured, use command exit code directly
    use_exit_code = not config.validation.handoff_file
    if use_exit_code:
        if ok:
            return "PASS", None
        # Distinguish infrastructure failures (timeout, shell error) from code failures.
        # _run_shell prefixes these with "TIMEOUT" or "ERROR" — surface them as errors
        # so the coordinator escalates immediately rather than burning dev retries.
        if output.startswith("TIMEOUT") or output.startswith("ERROR"):
            return None, f"Gate infrastructure failure: {output[:300]}"
        _log_verbose(f"Gate command failed: {output[:200]}")
        return "FAIL", None

    # Handoff-based mode: read decision from handoff file
    if not ok:
        _log_verbose(f"Gate command failed: {output[:200]}")
        # Gate may have still produced a handoff with FAIL/BLOCKED
        decision, err = _read_gate_decision(config, workspace_path)
        if decision:
            return decision, None
        return None, f"Gate command failed and no handoff produced: {output[:500]}"

    return _read_gate_decision(config, workspace_path)


# ── Diff extraction ─────────────────────────────────────────────────


def _get_diff(workspace_path: Path, base_branch: str = "main") -> str:
    """Get the diff of changes on the current branch vs the base branch."""
    ok, diff = _run_shell(f"git diff {base_branch}...HEAD", workspace_path)
    if ok and diff:
        return diff

    # Fallback: diff of staged + unstaged
    ok, diff = _run_shell("git diff HEAD", workspace_path)
    if ok and diff:
        return diff

    return "(no diff available)"


def _get_handoff_content(config: ForgeConfig, workspace_path: Path) -> str:
    """Read the handoff.yaml content as text for the reviewer."""
    if not config.validation.handoff_file:
        return "(exit-code gate mode — no handoff file)"
    handoff_path = workspace_path / config.validation.handoff_file
    if handoff_path.exists():
        return handoff_path.read_text(encoding="utf-8")
    return "(handoff.yaml not found)"


# ── State machine ────────────────────────────────────────────────────


def run_task(
    config: ForgeConfig,
    task: TaskSpec,
    *,
    interactive: bool = False,
    auto_merge: bool = False,
) -> CoordinatorResult:
    """Execute the full coordinator state machine for a single task.

    This is the main entry point. It creates a workspace, runs the dev agent,
    validates output, runs the review pool (+synthesis if >1 reviewer), and
    loops until done or exhausted.

    Every transition is deterministic. No LLM makes process decisions.

    Args:
        config: The forge configuration.
        task: The task specification.
        interactive: When True, pause at HUMAN_REVIEW for operator input before
            finalizing DONE or ESCALATE. When False (default), behave as before.
        auto_merge: When True, merge the feature branch into base_branch after
            a successful APPROVE. Does NOT merge on ESCALATE or ALREADY_DONE.
    """
    state = CoordinatorState()
    state.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _task_start = time.monotonic()
    spec_content = load_spec(task.spec_path)

    # ── WORKSPACE ─────────────────────────────────────────────────
    state.phase = Phase.WORKSPACE
    _log_phase(state.phase, task.slug)

    workspace_path, branch_name, err = _create_workspace(config, task)
    if err:
        state.phase = Phase.ESCALATE
        state.error = err
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=f"Workspace creation failed: {err}",
        )

    assert workspace_path is not None
    assert branch_name is not None
    state.workspace_path = workspace_path
    state.branch_name = branch_name

    # ── PREFLIGHT ──────────────────────────────────────────────────
    state.phase = Phase.PREFLIGHT
    preflight_profile = config.preflight_profile
    _log_phase(state.phase, preflight_profile.model)

    file_contents = _load_file_scope_contents(task, config.project_root)
    preflight_prompt = build_preflight_prompt(
        task, spec_content=spec_content, file_contents=file_contents
    )

    _preflight_start = time.monotonic()
    preflight_result = run_agent(
        prompt=preflight_prompt,
        profile=preflight_profile,
        working_dir=workspace_path,
    )
    state.preflight_result = preflight_result
    log_agent_result(preflight_result, "PREFLIGHT")

    if preflight_result.success:
        verdict, reason = _parse_preflight_verdict(preflight_result.output)
    else:
        # Agent failed — don't block on a broken preflight, proceed
        verdict, reason = (
            "PROCEED",
            f"Preflight agent failed (exit={preflight_result.exit_code}); proceeding anyway.",
        )

    state.preflight_verdict = verdict
    state.preflight_reason = reason
    _log(f"  ✓ PREFLIGHT   {verdict}")
    _log_verbose(f"  Reason: {reason}")

    if verdict == "ALREADY_DONE":
        state.phase = Phase.DONE
        elapsed = time.monotonic() - _task_start
        _log(f"✓ DONE   total=${state.total_cost:.2f}  {elapsed:.0f}s")
        return CoordinatorResult(
            success=True,
            phase=state.phase,
            state=state,
            message=f"Preflight: spec already implemented. {reason}",
        )

    if verdict == "BLOCKED":
        state.phase = Phase.ESCALATE
        state.error = f"Preflight: spec is blocked. {reason}"
        _log(f"✗ ESCALATE   {state.error}")
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    # verdict == "PROCEED" — continue to DEV

    # ── DEV→VALIDATE→REVIEW loop ─────────────────────────────────
    while True:
        # ── DEV ───────────────────────────────────────────────
        state.phase = Phase.DEV
        state.dev_iteration += 1
        _log_phase(
            state.phase,
            f"{config.dev_profile.model}  iter={state.dev_iteration}",
        )

        prompt = build_dev_prompt(
            task,
            workspace_path=workspace_path,
            branch_name=branch_name,
            spec_content=spec_content,
            gate_command=config.validation.gate_command,
            review_findings=state.last_review_findings,
            human_feedback=state.human_feedback,
            iteration=state.dev_iteration,
        )

        _dev_start = time.monotonic()
        dev_result = run_agent(
            prompt=prompt,
            profile=config.dev_profile,
            working_dir=workspace_path,
            session_id=state.dev_session_id,
        )
        _dev_elapsed = time.monotonic() - _dev_start
        state.dev_results.append(dev_result)
        state.dev_durations.append(_dev_elapsed)
        state.dev_session_id = dev_result.session_id
        log_agent_result(dev_result, "DEV")
        _log(f"  ✓ DEV   ${dev_result.cost_usd:.2f}  {_dev_elapsed:.0f}s")

        if state.total_dev_cost > config.dev_profile.budget_usd:
            state.phase = Phase.ESCALATE
            state.error = (
                f"Dev budget exceeded: spent ${state.total_dev_cost:.4f} "
                f"(limit ${config.dev_profile.budget_usd:.4f})"
            )
            _log(f"✗ ESCALATE   {state.error}")
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )

        if not dev_result.success:
            _log_verbose(f"Dev agent failed (exit={dev_result.exit_code})")
            # Don't immediately escalate — try validation anyway,
            # the agent may have committed partial work + run the gate

        # ── VALIDATE ──────────────────────────────────────────
        state.phase = Phase.VALIDATE
        _log_phase(state.phase, "running gate...")

        gate_decision, gate_err = _run_gate(config, workspace_path, task=task)

        if gate_err:
            _log_verbose(f"Gate error: {gate_err}")
            if state.dev_iteration >= config.retry.max_dev_iterations:
                state.phase = Phase.ESCALATE
                state.error = f"Gate failed after {state.dev_iteration} attempts: {gate_err}"
                _log(f"✗ ESCALATE   {state.error}")
                return CoordinatorResult(
                    success=False,
                    phase=state.phase,
                    state=state,
                    message=state.error,
                )
            # Retry dev with feedback about the gate failure
            state.human_feedback = f"Gate validation failed: {gate_err}"
            _log(f"  ✗ VALIDATE   FAIL  (iter={state.dev_iteration} → retrying)")
            continue

        assert gate_decision is not None
        state.gate_decisions.append(gate_decision)
        _log_verbose(f"Gate decision: {gate_decision}")

        if gate_decision == "PASS":
            _log("  ✓ VALIDATE   PASS")
            # Verify worktree is clean — the dev agent must commit all changes.
            # The gate runs against the working tree, so it can pass even with
            # uncommitted files. This check catches that process violation.
            dirty_ok, dirty_out = _run_shell("git status --porcelain", workspace_path)
            if dirty_ok and dirty_out.strip():
                # Filter out handoff.yaml and other gate artifacts
                handoff_file = config.validation.handoff_file
                dirty_lines = [
                    line
                    for line in dirty_out.strip().splitlines()
                    if not (handoff_file and line.strip().endswith(handoff_file))
                ]
                if dirty_lines:
                    dirty_files = ", ".join(
                        line.strip().split(maxsplit=1)[-1] for line in dirty_lines
                    )
                    _log(f"Dirty worktree detected: {dirty_files}")
                    if state.dev_iteration >= config.retry.max_dev_iterations:
                        state.phase = Phase.ESCALATE
                        state.error = f"Dev agent left uncommitted changes: {dirty_files}"
                        _log(f"✗ ESCALATE   {state.error}")
                        return CoordinatorResult(
                            success=False,
                            phase=state.phase,
                            state=state,
                            message=state.error,
                        )
                    state.human_feedback = (
                        "PROCESS VIOLATION: You left uncommitted changes in the "
                        f"worktree: {dirty_files}. You MUST commit ALL modified "
                        "files before running the gate. Stage and commit them now."
                    )
                    continue
        elif gate_decision in ("FAIL", "BLOCKED"):
            if state.dev_iteration >= config.retry.max_dev_iterations:
                state.phase = Phase.ESCALATE
                state.error = f"Gate returned {gate_decision} after {state.dev_iteration} attempts"
                _log(f"✗ ESCALATE   {state.error}")
                return CoordinatorResult(
                    success=False,
                    phase=state.phase,
                    state=state,
                    message=state.error,
                )
            # Retry dev — the gate failure details are in handoff.yaml
            handoff_text = _get_handoff_content(config, workspace_path)
            state.human_feedback = (
                f"Gate returned {gate_decision}. "
                f"Fix the issues and re-run the gate.\n\n"
                f"Current handoff:\n{handoff_text}"
            )
            _log(f"  ✗ VALIDATE   {gate_decision}  (iter={state.dev_iteration} → retrying)")
            _log(f"Retrying dev (gate={gate_decision}, iter={state.dev_iteration})")
            continue
        else:
            _log(f"Unknown gate decision: {gate_decision!r}, treating as FAIL")
            state.phase = Phase.ESCALATE
            state.error = f"Unknown gate decision: {gate_decision!r}"
            _log(f"✗ ESCALATE   {state.error}")
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )

        # ── REVIEW ────────────────────────────────────────────
        state.phase = Phase.REVIEW
        pool_size = len(config.review_pool)
        max_parse_retries = config.retry.max_review_parse_retries
        _review_pool_start = time.monotonic()
        _pool_model_names = "+".join(p.model for p in config.review_pool)
        _log_phase(state.phase, f"{_pool_model_names}  cycle={state.review_cycle + 1}")

        diff_text = _get_diff(workspace_path, config.workspace.base_branch)
        handoff_content = _get_handoff_content(config, workspace_path)

        review_prompt = build_review_prompt(
            task,
            spec_content=spec_content,
            diff_text=diff_text,
            handoff_content=handoff_content,
        )

        # Create metadata now and append immediately — mutations will be visible
        # through the stored reference (present on all early returns including budget exits)
        meta = ReviewCycleMetadata(
            pool_models=[p.name for p in config.review_pool],
            successful=[],
            failed=[],
            synthesized=False,
            parse_retries=0,
        )
        state.review_cycle_metadata.append(meta)
        _review_cost_before_cycle = sum(r.cost_usd for r in state.review_agent_results)

        parsed_review = None
        last_parse_error: str | None = None

        for _parse_attempt in range(max_parse_retries + 1):
            if _parse_attempt > 0:
                _log_verbose(
                    f"Parse retry {_parse_attempt}/{max_parse_retries} "
                    f"for review cycle {state.review_cycle + 1}"
                )

            # Run all pool reviewers (sequentially for MVP)
            _log_verbose(
                f"Running {pool_size} reviewer(s): {[p.name for p in config.review_pool]}"
            )
            _pool_start = time.monotonic()
            pool_results = run_agent_pool(
                prompt=review_prompt,
                profiles=config.review_pool,
                working_dir=workspace_path,
            )
            _pool_elapsed = time.monotonic() - _pool_start
            _per_agent_dur = _pool_elapsed / max(len(pool_results), 1)
            for r in pool_results:
                state.review_agent_results.append(r)
                state.review_durations.append(_per_agent_dur)
                log_agent_result(r, f"REVIEW/{r.profile_name}")

            # Per-profile budget enforcement (cumulative across cycles)
            for profile in config.review_pool:
                profile_cost = sum(
                    r.cost_usd
                    for r in state.review_agent_results
                    if r.profile_name == profile.name
                )
                if profile_cost > profile.budget_usd:
                    state.phase = Phase.ESCALATE
                    state.error = (
                        f"Review budget exceeded for {profile.name}: "
                        f"spent ${profile_cost:.4f} (limit ${profile.budget_usd:.4f})"
                    )
                    return CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    )

            successful = [r for r in pool_results if r.success]
            failed_results = [r for r in pool_results if not r.success]

            for f in failed_results:
                _log_verbose(f"Pool reviewer failed: {f.profile_name} (exit={f.exit_code})")

            meta.successful = [r.profile_name for r in successful]
            meta.failed = [r.profile_name for r in failed_results]
            meta.failed_detail = {r.profile_name: f"exit={r.exit_code}" for r in failed_results}

            if not successful:
                state.phase = Phase.ESCALATE
                failed_desc = ", ".join(
                    f"{r.profile_name} (exit={r.exit_code})" for r in failed_results
                )
                state.error = f"All {len(pool_results)} review agent(s) failed: {failed_desc}"
                return CoordinatorResult(
                    success=False,
                    phase=state.phase,
                    state=state,
                    message=state.error,
                )

            # Determine the output to parse as the final review verdict
            if config.synthesis_profile is None or len(successful) == 1:
                # Pool of 1, or degraded to 1 successful reviewer — no synthesis
                if len(failed_results) > 0:
                    _log_verbose(
                        f"Degraded: {len(successful)} of {pool_size} reviewers succeeded, "
                        "skipping synthesis"
                    )
                synthesis_output = successful[0].output

            else:
                # Multi-model: run synthesis over all successful outputs
                meta.synthesized = True  # mutate in place; already in state.review_cycle_metadata
                _log_verbose(
                    f"Synthesizing {len(successful)} review outputs "
                    f"(+{len(failed_results)} failed excluded)"
                )
                synthesis_prompt = build_synthesis_prompt(
                    task,
                    review_outputs=[r.output for r in successful],
                    review_names=[r.profile_name for r in successful],
                    spec_content=spec_content,
                    failed_count=len(failed_results),
                    total_count=pool_size,
                )
                _synth_start = time.monotonic()
                synthesis_result = run_agent(
                    prompt=synthesis_prompt,
                    profile=config.synthesis_profile,
                    working_dir=workspace_path,
                )
                _synth_elapsed = time.monotonic() - _synth_start
                # Tag with profile name using dataclasses.replace
                from dataclasses import replace as _replace

                synthesis_result = _replace(synthesis_result, profile_name="synthesis")

                state.review_agent_results.append(synthesis_result)
                state.review_durations.append(_synth_elapsed)
                log_agent_result(synthesis_result, "SYNTHESIS")

                # Synthesis budget enforcement
                if config.synthesis_profile is not None:
                    synth_cost = sum(
                        r.cost_usd
                        for r in state.review_agent_results
                        if r.profile_name == "synthesis"
                    )
                    if synth_cost > config.synthesis_profile.budget_usd:
                        state.phase = Phase.ESCALATE
                        state.error = (
                            f"Synthesis budget exceeded: "
                            f"spent ${synth_cost:.4f} "
                            f"(limit ${config.synthesis_profile.budget_usd:.4f})"
                        )
                        return CoordinatorResult(
                            success=False,
                            phase=state.phase,
                            state=state,
                            message=state.error,
                        )

                if not synthesis_result.success:
                    state.phase = Phase.ESCALATE
                    state.error = f"Synthesis agent failed (exit={synthesis_result.exit_code})"
                    return CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    )

                synthesis_output = synthesis_result.output

            _candidate = parse_review_output(synthesis_output)

            if _candidate.parse_errors:
                last_parse_error = str(_candidate.parse_errors)
                _log_verbose(
                    f"Review parse errors (attempt {_parse_attempt + 1}): "
                    f"{_candidate.parse_errors}"
                )
                if _parse_attempt < max_parse_retries:
                    meta.parse_retries += 1
                    _log_verbose(
                        f"Retrying reviewer ({meta.parse_retries}/{max_parse_retries} retries "
                        f"used) — parse error does NOT increment review cycle"
                    )
                    continue
                # All retries exhausted
                break

            # Valid verdict obtained
            parsed_review = _candidate
            break

        if parsed_review is None:
            # All parse retries exhausted with no valid verdict
            state.phase = Phase.ESCALATE
            state.error = (
                f"Review pool unreliable: all reviewers failed to produce valid output "
                f"after {meta.parse_retries} retries. Last error: {last_parse_error}"
            )
            _log(f"✗ ESCALATE   {state.error}")
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )

        # Valid verdict obtained — NOW increment review cycle counter
        state.review_cycle += 1
        state.review_results.append(parsed_review)

        _review_elapsed = time.monotonic() - _review_pool_start
        _p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
        _p2_count = sum(1 for f in parsed_review.findings if f.severity == "P2")
        _review_cost = (
            sum(r.cost_usd for r in state.review_agent_results) - _review_cost_before_cycle
        )

        _log_verbose(f"Review verdict: {parsed_review.verdict}")
        _log_verbose(f"  Summary: {parsed_review.summary}")
        _log_verbose(f"  Findings: {len(parsed_review.findings)} ({_p1_count} P1)")

        if parsed_review.verdict == "APPROVE":
            _log(
                f"  ✓ REVIEW   APPROVE  {_p1_count} P1  {_p2_count} P2"
                f"  ${_review_cost:.2f}  {_review_elapsed:.0f}s"
            )
            if interactive:
                state.phase = Phase.HUMAN_REVIEW
                _log_phase(state.phase)
                decision, feedback = _human_review(
                    state, parsed_review, workspace_path, branch_name
                )
                state.human_review_decision = decision
                state.human_review_feedback = feedback
                if decision == "approve":
                    state.phase = Phase.DONE
                    merge_info: dict | None = None
                    merge_suffix = ""
                    if auto_merge:
                        merge_info = _merge_branch(
                            config.project_root,
                            config.workspace.base_branch,
                            branch_name,
                            task.slug,
                            workspace_path,
                        )
                        merge_suffix = (
                            " Merged."
                            if merge_info["merged"]
                            else f" Merge failed: {merge_info['error']}"
                        )
                    _task_elapsed = time.monotonic() - _task_start
                    _log(f"✓ DONE   total=${state.total_cost:.2f}  {_task_elapsed:.0f}s")
                    return CoordinatorResult(
                        success=True,
                        phase=state.phase,
                        state=state,
                        message=(
                            f"Task '{task.name}' completed. "
                            f"Human approved after {state.review_cycle} cycle(s), "
                            f"{state.dev_iteration} dev iteration(s). "
                            f"Branch: {branch_name}{merge_suffix}"
                        ),
                        merge=merge_info,
                    )
                if decision == "escalate":
                    state.phase = Phase.ESCALATE
                    state.error = "Human chose to escalate after APPROVE."
                    _log(f"✗ ESCALATE   {state.error}")
                    return CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    )
                # decision == "reject" — loop back to dev with human feedback
                state.human_feedback = feedback
                state.last_review_findings = None
                state.dev_iteration = 0
                _log("Human rejected — looping back to dev with feedback")
                continue
            else:
                state.phase = Phase.DONE
                merge_info = None
                merge_suffix = ""
                if auto_merge:
                    merge_info = _merge_branch(
                        config.project_root,
                        config.workspace.base_branch,
                        branch_name,
                        task.slug,
                        workspace_path,
                    )
                    merge_suffix = (
                        " Merged."
                        if merge_info["merged"]
                        else f" Merge failed: {merge_info['error']}"
                    )
                _task_elapsed = time.monotonic() - _task_start
                _log(f"✓ DONE   total=${state.total_cost:.2f}  {_task_elapsed:.0f}s")
                return CoordinatorResult(
                    success=True,
                    phase=state.phase,
                    state=state,
                    message=(
                        f"Task '{task.name}' completed. "
                        f"Review approved after {state.review_cycle} cycle(s), "
                        f"{state.dev_iteration} dev iteration(s). "
                        f"Branch: {branch_name}{merge_suffix}"
                    ),
                    merge=merge_info,
                )

        # REQUEST_CHANGES — loop back to dev
        _log(
            f"  ✗ REVIEW   REQUEST_CHANGES  {_p1_count} P1"
            f"  ${_review_cost:.2f}  {_review_elapsed:.0f}s"
        )
        if state.review_cycle >= config.retry.max_review_cycles:
            if interactive:
                state.phase = Phase.HUMAN_REVIEW
                _log_phase(state.phase, "cycles exhausted")
                decision, feedback = _human_review(
                    state, parsed_review, workspace_path, branch_name
                )
                state.human_review_decision = decision
                state.human_review_feedback = feedback
                if decision == "approve":
                    state.phase = Phase.DONE
                    merge_info = None
                    merge_suffix = ""
                    if auto_merge:
                        merge_info = _merge_branch(
                            config.project_root,
                            config.workspace.base_branch,
                            branch_name,
                            task.slug,
                            workspace_path,
                        )
                        merge_suffix = (
                            " Merged."
                            if merge_info["merged"]
                            else f" Merge failed: {merge_info['error']}"
                        )
                    _task_elapsed = time.monotonic() - _task_start
                    _log(f"✓ DONE   total=${state.total_cost:.2f}  {_task_elapsed:.0f}s")
                    return CoordinatorResult(
                        success=True,
                        phase=state.phase,
                        state=state,
                        message=(
                            f"Task '{task.name}' completed. "
                            f"Human approved after {state.review_cycle} cycle(s). "
                            f"Branch: {branch_name}{merge_suffix}"
                        ),
                        merge=merge_info,
                    )
                if decision == "reject":
                    # Human wants another dev loop; reset iteration counter
                    state.human_feedback = feedback
                    state.last_review_findings = None
                    state.dev_iteration = 0
                    _log("Human rejected — looping back to dev with feedback")
                    continue
                # escalate
                state.phase = Phase.ESCALATE
                state.error = "Human chose to escalate after exhausted cycles."
                _log(f"✗ ESCALATE   {state.error}")
                return CoordinatorResult(
                    success=False,
                    phase=state.phase,
                    state=state,
                    message=state.error,
                )
            else:
                state.phase = Phase.ESCALATE
                state.error = (
                    f"Review requested changes after {state.review_cycle} cycles. "
                    f"Max cycles ({config.retry.max_review_cycles}) exhausted."
                )
                _log(f"✗ ESCALATE   {state.error}")
                return CoordinatorResult(
                    success=False,
                    phase=state.phase,
                    state=state,
                    message=state.error,
                )

        # Feed findings back to dev agent
        state.last_review_findings = findings_to_markdown(parsed_review.findings)
        state.dev_iteration = 0  # reset iteration count for new review cycle
        state.human_feedback = None  # clear any gate feedback
        _log_verbose(f"Sending {len(parsed_review.findings)} findings back to dev agent")


# ── Review-only mode ─────────────────────────────────────────────────


def run_review_only(
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
) -> CoordinatorResult:
    """Run only the REVIEW phase on an existing worktree.

    Skips WORKSPACE, PREFLIGHT, DEV, VALIDATE.
    Returns a CoordinatorResult with phase=DONE (APPROVE) or ESCALATE
    (REQUEST_CHANGES — no DEV retry in review-only mode).
    """
    state = CoordinatorState()
    state.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # ── Verify workspace exists ───────────────────────────────────────
    if not workspace_path.exists():
        state.phase = Phase.ESCALATE
        state.error = f"Worktree not found at {workspace_path}. Run `forge run` first."
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    state.workspace_path = workspace_path
    branch_name = config.workspace.branch_pattern.format(slug=task.slug)
    state.branch_name = branch_name

    spec_content = load_spec(task.spec_path)

    # ── REVIEW ────────────────────────────────────────────────────────
    state.phase = Phase.REVIEW
    state.review_cycle = 1
    state.dev_iteration = 0
    pool_size = len(config.review_pool)
    _pool_model_names_ro = "+".join(p.model for p in config.review_pool)
    _log_phase(state.phase, f"{_pool_model_names_ro}  cycle=1  (review-only)")

    diff_text = _get_diff(workspace_path, config.workspace.base_branch)
    handoff_content = _get_handoff_content(config, workspace_path)

    review_prompt = build_review_prompt(
        task,
        spec_content=spec_content,
        diff_text=diff_text,
        handoff_content=handoff_content,
    )

    _log_verbose(f"Running {pool_size} reviewer(s): {[p.name for p in config.review_pool]}")
    _pool_start = time.monotonic()
    pool_results = run_agent_pool(
        prompt=review_prompt,
        profiles=config.review_pool,
        working_dir=workspace_path,
    )
    _pool_elapsed = time.monotonic() - _pool_start
    _per_agent_dur = _pool_elapsed / max(len(pool_results), 1)
    for r in pool_results:
        state.review_agent_results.append(r)
        state.review_durations.append(_per_agent_dur)
        log_agent_result(r, f"REVIEW/{r.profile_name}")

    successful = [r for r in pool_results if r.success]
    failed_results = [r for r in pool_results if not r.success]

    for f in failed_results:
        _log_verbose(f"Pool reviewer failed: {f.profile_name} (exit={f.exit_code})")

    meta = ReviewCycleMetadata(
        pool_models=[p.name for p in config.review_pool],
        successful=[r.profile_name for r in successful],
        failed=[r.profile_name for r in failed_results],
        synthesized=False,
        failed_detail={r.profile_name: f"exit={r.exit_code}" for r in failed_results},
    )
    state.review_cycle_metadata.append(meta)

    if not successful:
        state.phase = Phase.ESCALATE
        failed_desc = ", ".join(f"{r.profile_name} (exit={r.exit_code})" for r in failed_results)
        state.error = f"All {len(pool_results)} review agent(s) failed: {failed_desc}"
        _log(f"✗ ESCALATE   {state.error}")
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    # Synthesis if multi-model pool
    if config.synthesis_profile is None or len(successful) == 1:
        synthesis_output = successful[0].output
    else:
        meta.synthesized = True
        _log_verbose(f"Synthesizing {len(successful)} review outputs")
        synthesis_prompt = build_synthesis_prompt(
            task,
            review_outputs=[r.output for r in successful],
            review_names=[r.profile_name for r in successful],
            spec_content=spec_content,
            failed_count=len(failed_results),
            total_count=pool_size,
        )
        _synth_start = time.monotonic()
        synthesis_result = run_agent(
            prompt=synthesis_prompt,
            profile=config.synthesis_profile,
            working_dir=workspace_path,
        )
        _synth_elapsed = time.monotonic() - _synth_start
        from dataclasses import replace as _replace

        synthesis_result = _replace(synthesis_result, profile_name="synthesis")
        state.review_agent_results.append(synthesis_result)
        state.review_durations.append(_synth_elapsed)
        log_agent_result(synthesis_result, "SYNTHESIS")

        if not synthesis_result.success:
            state.phase = Phase.ESCALATE
            state.error = f"Synthesis agent failed (exit={synthesis_result.exit_code})"
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )
        synthesis_output = synthesis_result.output

    parsed_review = parse_review_output(synthesis_output)
    state.review_results.append(parsed_review)

    if parsed_review.parse_errors:
        _log_verbose(f"Review parse errors: {parsed_review.parse_errors}")
        canonical_summary = f"PARSE ERROR: {parsed_review.summary}"
        parsed_review = ReviewResult(
            verdict="REQUEST_CHANGES",
            summary=canonical_summary,
            findings=parsed_review.findings,
            spec_matches=parsed_review.spec_matches,
            spec_mismatches=parsed_review.spec_mismatches,
            test_adequate=parsed_review.test_adequate,
            test_gaps=parsed_review.test_gaps,
            parse_errors=parsed_review.parse_errors,
            raw_yaml=parsed_review.raw_yaml,
        )
        state.review_results[-1] = parsed_review

    _log_verbose(f"Review verdict: {parsed_review.verdict}")
    _log_verbose(f"  Summary: {parsed_review.summary}")

    _ro_p1 = sum(1 for f in parsed_review.findings if f.severity == "P1")
    _ro_p2 = sum(1 for f in parsed_review.findings if f.severity == "P2")
    _ro_cost = sum(r.cost_usd for r in state.review_agent_results)
    _ro_elapsed = _pool_elapsed

    if parsed_review.verdict == "APPROVE":
        state.phase = Phase.DONE
        _log(
            f"  ✓ REVIEW   APPROVE  {_ro_p1} P1  {_ro_p2} P2  ${_ro_cost:.2f}  {_ro_elapsed:.0f}s"
        )
        _log(f"✓ DONE   total=${state.total_cost:.2f}  {_ro_elapsed:.0f}s")
        return CoordinatorResult(
            success=True,
            phase=state.phase,
            state=state,
            message=(f"Task '{task.name}' review-only: APPROVE. Branch: {branch_name}"),
        )

    # REQUEST_CHANGES — no DEV retry in review-only mode
    state.phase = Phase.ESCALATE
    p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
    state.error = (
        f"Review requested changes ({p1_count} P1 finding(s)). No retry in review-only mode."
    )
    _log(f"  ✗ REVIEW   REQUEST_CHANGES  {_ro_p1} P1  ${_ro_cost:.2f}  {_ro_elapsed:.0f}s")
    _log(f"✗ ESCALATE   {state.error}")
    return CoordinatorResult(
        success=False,
        phase=state.phase,
        state=state,
        message=state.error,
    )


# ── Audit ────────────────────────────────────────────────────────────


def generate_audit_log(config: ForgeConfig, task: TaskSpec, result: CoordinatorResult) -> dict:
    """Generate a structured audit log for the entire coordination run.

    This is the orchestrator's own handoff — a complete record of what happened.
    """
    state = result.state

    # Compute overall timing
    finished_at = datetime.datetime.now(datetime.timezone.utc)
    finished_at_str = finished_at.isoformat()
    duration_seconds: float | None = None
    if state.started_at:
        try:
            started = datetime.datetime.fromisoformat(state.started_at)
            duration_seconds = (finished_at - started).total_seconds()
        except ValueError:
            pass

    # Build per-agent invocation list for cost breakdown.
    # Durations are measured in the coordinator around each agent call.
    agents: list[dict] = []
    for i, r in enumerate(state.dev_results):
        dur = state.dev_durations[i] if i < len(state.dev_durations) else None
        entry: dict = {
            "role": "dev",
            "profile": r.profile_name or config.dev_profile.name,
            "cost_usd": r.cost_usd,
            "duration_seconds": dur,
        }
        if r.model_usage:
            entry["model_usage"] = [
                {
                    "model": u.model,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_read_tokens": u.cache_read_tokens,
                    "cache_creation_tokens": u.cache_creation_tokens,
                    "cost_usd": u.cost_usd,
                }
                for u in r.model_usage
            ]
        agents.append(entry)
    for i, r in enumerate(state.review_agent_results):
        dur = state.review_durations[i] if i < len(state.review_durations) else None
        role = "synthesis" if r.profile_name == "synthesis" else "review"
        entry = {
            "role": role,
            "profile": r.profile_name,
            "cost_usd": r.cost_usd,
            "duration_seconds": dur,
        }
        if r.model_usage:
            entry["model_usage"] = [
                {
                    "model": u.model,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_read_tokens": u.cache_read_tokens,
                    "cache_creation_tokens": u.cache_creation_tokens,
                    "cost_usd": u.cost_usd,
                }
                for u in r.model_usage
            ]
        agents.append(entry)

    # Build reviews list from cycle metadata (primary) joined with parsed results
    reviews = []
    for i, meta in enumerate(state.review_cycle_metadata):
        entry: dict = {
            "cycle": i + 1,
            "pool_models": meta.pool_models,
            "successful": meta.successful,
            "failed": meta.failed,
            "failed_detail": meta.failed_detail,
            "synthesized": meta.synthesized,
            "parse_retries": meta.parse_retries,
        }
        if i < len(state.review_results):
            r = state.review_results[i]
            findings_list = [
                {
                    "severity": f.severity,
                    "file": f.file,
                    "line": f.line,
                    "description": f.description,
                }
                for f in r.findings
            ]
            entry.update(
                {
                    "verdict": r.verdict,
                    "summary": r.summary,
                    "p1_count": sum(1 for f in r.findings if f.severity == "P1"),
                    "p2_count": sum(1 for f in r.findings if f.severity == "P2"),
                    "findings": findings_list,
                }
            )
        reviews.append(entry)

    return {
        "forge_version": "0.1.0",
        "task": {
            "name": task.name,
            "slug": task.slug,
            "spec_path": str(task.spec_path),
        },
        "outcome": {
            "success": result.success,
            "final_phase": result.phase.name,
            "message": result.message,
        },
        "timing": {
            "started_at": state.started_at,
            "finished_at": finished_at_str,
            "duration_seconds": duration_seconds,
        },
        "workspace": {
            "path": str(state.workspace_path) if state.workspace_path else None,
            "branch": state.branch_name,
        },
        "iterations": {
            "review_cycles": state.review_cycle,
            "dev_iterations": state.dev_iteration,
            "gate_decisions": state.gate_decisions,
        },
        "cost": {
            "total_usd": state.total_cost,
            "dev_usd": state.total_dev_cost,
            "review_usd": state.total_review_cost,
            "dev_invocations": len(state.dev_results),
            "review_invocations": len(state.review_agent_results),
            "agents": agents,
        },
        "preflight": (
            {
                "verdict": state.preflight_verdict,
                "reason": state.preflight_reason,
                "cost_usd": state.preflight_result.cost_usd if state.preflight_result else 0.0,
            }
            if state.preflight_verdict is not None
            else None
        ),
        "reviews": reviews,
        "human_review": (
            {
                "decision": state.human_review_decision,
                "feedback": state.human_review_feedback,
            }
            if state.human_review_decision is not None
            else None
        ),
        "merge": result.merge,
        "error": state.error,
    }
