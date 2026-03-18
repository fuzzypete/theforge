"""Coordinator: deterministic state machine for dev→review loops.

The coordinator is the heart of TheForge. It is NOT an LLM — it is a Python
program that mechanically orchestrates agent invocations. Every decision is
deterministic. Every boundary is a validation checkpoint.

State machine:
    INIT → WORKSPACE → PREFLIGHT → PLAN → PLAN_REVIEW → DEV → VALIDATE → REVIEW
        → (loop or DONE/ESCALATE)

Transitions:
    INIT → WORKSPACE:       Always (create workspace)
    WORKSPACE → PREFLIGHT:  Workspace created successfully
    PREFLIGHT → PLAN/DEV:   Verdict is PROCEED (or agent failed — fail-open)
    PREFLIGHT → DONE:       Verdict is ALREADY_DONE (spec satisfied on main)
    PREFLIGHT → ESCALATE:   Verdict is BLOCKED (spec is stale/invalid)
    PLAN → PLAN_REVIEW:     Plan agent succeeded and plan review is enabled
    PLAN → DEV:             Plan succeeded and review is skipped/disabled
    PLAN_REVIEW → PLAN:     Human requests one regeneration
    PLAN_REVIEW → DEV:      Human approves the plan
    PLAN_REVIEW → stop:     Human abandons the run
    DEV → VALIDATE:         Dev agent finished (success or failure)
    VALIDATE → REVIEW:      Gate produced handoff.yaml with PASS
    VALIDATE → DEV:         Gate failed, retries remaining
    VALIDATE → ESCALATE:    Gate failed, no retries left
    REVIEW → DONE:          Review verdict is APPROVE
    REVIEW → DEV:           Review verdict is REQUEST_CHANGES, retries remaining
    REVIEW → ESCALATE:      Review verdict is REQUEST_CHANGES, no retries left
"""

from __future__ import annotations

import dataclasses
import datetime
import subprocess
import sys as _sys
import time
from pathlib import Path

import yaml

from . import coord_util as _cu
from .config import MODEL_REGISTRY, ForgeConfig, ModelProfile  # noqa: F401
from .coord_gate import (  # noqa: F401
    _auto_commit_side_effects,
    _is_gate_skip,
    _parse_dirty_files,
    _read_gate_decision,
    _run_gate,
    _run_gate_full,
)

# ── Structured logging ────────────────────────────────────────────────
from .coord_logging import StructuredLogger  # noqa: F401
from .coord_notify import (  # noqa: F401
    _escalate_notify,
    _human_review,
    _is_remote_mode,
    _notify,
    _ntfy_done_notify,
    _ntfy_poll_reply,
    _ntfy_publish,
    _ntfy_reply_url,
    _osa_quote,
    _plan_review_interactive,
    _plan_review_remote,
    _remote_human_review,
)
from .coord_preflight import (  # noqa: F401
    _apply_complexity_adaptation,
    _escalate_dev_model,
    _find_registry_info_for_profile,
    _find_registry_key_for_profile,
    _has_persistent_p1,
    _load_file_scope_contents,
    _parse_preflight_complexity,
    _parse_preflight_verdict,
    _persistent_p1_descriptions,
)

# ── Re-exports for backward compatibility ────────────────────────────
from .coord_state import (  # noqa: F401
    CoordinatorResult,
    CoordinatorState,
    CycleHistory,
    Phase,
    ReviewCycleMetadata,
)
from .coord_util import (  # noqa: F401
    _LOG_LEVEL,
    _fmt_duration,
    _generate_run_id,
    _log,
    _log_phase,
    _log_verbose,
    resolve_timeout,
    set_log_level,
)
from .coord_workspace import (  # noqa: F401
    _create_workspace,
    _fmt_age,
    _is_stale_worktree,
    _merge_branch,
    _remove_worktree,
    _resolve_merge_conflicts,
)
from .devhandoff import DevHandoff, dev_handoff_to_reviewer_text, parse_dev_handoff
from .review import (  # noqa: F401
    PlanReviewResult,
    ReviewResult,
    merge_review_results,
    parse_plan_review_output,
    parse_review_output,
    plan_review_findings_to_text,
    review_to_dev_handoff,
)
from .runner import LogLevel, log_agent_result, run_agent, run_agent_pool
from .sessions import load_sessions, save_sessions
from .task import (  # noqa: F401
    TaskSpec,
    build_dev_prompt,
    build_fix_prompt,
    build_handoff_fix_prompt,
    build_plan_prompt,
    build_plan_review_prompt,
    build_preflight_prompt,
    build_review_prompt,
    load_spec,
)
from .traces import write_trace

# ── Shell helper ─────────────────────────────────────────────────────


def _set_timeout_resume(state: CoordinatorState, gate_result: str) -> None:
    """Mark state for a timeout-resume retry with a short continuation prompt."""
    state.retry_reason = "timeout_resume"
    state.human_feedback = (
        "You were cut off by a timeout. Continue from where you left off. "
        f"Gate result: {gate_result}"
    )


def _run_shell(cmd: str, cwd: Path, timeout: int = 120) -> tuple[bool, str]:
    """Run a shell command. Returns (success, combined output).

    Defined here (not re-exported from coord_util) so that
    ``patch('theforge.coordinator._run_shell')`` intercepts calls made
    directly within this module.  Sub-modules (coord_workspace, coord_gate)
    call coord_util._run_shell; patch that symbol when testing those paths.
    """
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


# ── Commit log extraction ──────────────────────────────────────────


def _has_uncommitted_changes(workspace_path: Path) -> bool:
    """Check if the worktree has uncommitted changes (staged or unstaged)."""
    ok, status = _cu._run_shell("git status --porcelain", workspace_path)
    return ok and bool(status.strip())


def _get_commit_log(workspace_path: Path, base_branch: str = "main") -> str:
    """Get the commit log vs the base branch (like a PR commit list).

    If the worktree has uncommitted changes, appends a warning so reviewers
    know the commits don't tell the full story.
    """
    dirty = _has_uncommitted_changes(workspace_path)

    ok, log = _cu._run_shell(
        f"git log {base_branch}..HEAD --format='%h %s' --reverse", workspace_path
    )

    parts: list[str] = []
    if ok and log:
        parts.append(log)
    else:
        parts.append("(no commits ahead of base branch)")

    if dirty:
        parts.append(
            "\n⚠ WARNING: Worktree has uncommitted changes not reflected above. "
            "Run `git diff` and `git diff --cached` to see them."
        )

    return "\n".join(parts)


def _get_handoff_content(config: ForgeConfig, workspace_path: Path) -> str:
    """Read the handoff.yaml content as text for the reviewer."""
    if not config.validation.handoff_file:
        return "(exit-code gate mode — no handoff file)"
    handoff_path = workspace_path / config.validation.handoff_file
    if handoff_path.exists():
        return handoff_path.read_text(encoding="utf-8")
    return "(handoff.yaml not found)"


def _get_raw_dev_notes(config: ForgeConfig, workspace_path: Path) -> str | None:
    """Extract raw dev_notes string from handoff.yaml, or None if absent."""
    if not config.validation.handoff_file:
        return None
    handoff_path = workspace_path / config.validation.handoff_file
    if not handoff_path.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(handoff_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    val = data.get("dev_notes")
    if isinstance(val, str) and val.strip():
        return val
    return None


def _parse_dev_handoff(config: ForgeConfig, workspace_path: Path) -> DevHandoff | None:
    """Parse and validate the dev handoff from handoff.yaml.

    Returns None only when there's no handoff file at all (exit-code gate mode).
    Returns DevHandoff with parse_errors when dev_notes is missing/blank or
    fails schema validation — so the retry loop can request a rewrite.
    """
    if not config.validation.handoff_file:
        return None
    handoff_path = workspace_path / config.validation.handoff_file
    if not handoff_path.exists():
        return None
    raw = _get_raw_dev_notes(config, workspace_path)
    if raw is None:
        return DevHandoff(
            summary="",
            commits=[],
            acceptance_criteria=[],
            spec_deviations=[],
            deferred_items=[],
            gate_result="",
            parse_errors=["dev_notes field is missing or blank in handoff.yaml"],
            raw={},
        )
    return parse_dev_handoff(raw)


def _get_dev_notes(config: ForgeConfig, workspace_path: Path) -> str | None:
    """Extract dev_notes from handoff.yaml as structured reviewer text.

    If the dev handoff is valid structured YAML, formats it as structured
    markdown sections. Falls back to raw text if parsing fails.
    """
    raw = _get_raw_dev_notes(config, workspace_path)
    if raw is None:
        return None
    handoff = parse_dev_handoff(raw)
    if handoff.parse_errors:
        # Fall back to raw text when structured parsing fails
        return raw
    formatted = dev_handoff_to_reviewer_text(handoff)
    return formatted if formatted else raw


# ── Phase handlers (extracted to coord_phases.py) ────────────────────

from .coord_phases import (  # noqa: E402, F401
    _finalize_approve,
    _ReviewOutcome,
    _ValidateOutcome,
)
from .coord_phases import _run_dev_phase as _run_dev_phase_impl  # noqa: E402
from .coord_phases import _run_review_phase as _run_review_phase_impl  # noqa: E402
from .coord_phases import _run_validate_phase as _run_validate_phase_impl  # noqa: E402


def _run_dev_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    spec_content: str,
    workspace_path: Path,
    branch_name: str,
    *,
    notify: bool,
    logger: StructuredLogger | None,
) -> CoordinatorResult | None:
    return _run_dev_phase_impl(
        state,
        config,
        task,
        spec_content,
        workspace_path,
        branch_name,
        notify=notify,
        logger=logger,
        mod=_sys.modules[__name__],
    )


def _run_review_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    spec_content: str,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    interactive: bool,
    auto_merge: bool,
    notify: bool,
    logger: StructuredLogger | None,
) -> tuple[_ReviewOutcome, CoordinatorResult | None, ForgeConfig]:
    return _run_review_phase_impl(
        state,
        config,
        task,
        spec_content,
        workspace_path,
        branch_name,
        task_start,
        interactive=interactive,
        auto_merge=auto_merge,
        notify=notify,
        logger=logger,
        mod=_sys.modules[__name__],
    )


def _run_validate_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
    dev_calls_this_cycle: int,
    *,
    notify: bool,
    logger: StructuredLogger | None,
) -> tuple[_ValidateOutcome, CoordinatorResult | None]:
    return _run_validate_phase_impl(
        state,
        config,
        task,
        workspace_path,
        dev_calls_this_cycle,
        notify=notify,
        logger=logger,
        mod=_sys.modules[__name__],
    )


def _run_review_pool(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    spec_content: str,
    workspace_path: Path,
    branch_name: str,
    meta: ReviewCycleMetadata,
    *,
    notify: bool,
    review_prompts: str | list[str] | None = None,
    enforce_budgets: bool = True,
    pool_attempt: int = 0,
) -> tuple[list, list, ReviewResult | None]:
    """Run the review pool and merge results.  Returns (successful, failed, merged_result).

    Updates *meta* in-place (successful, failed, failed_detail).
    merged_result is None when all reviewers failed or budget exceeded;
    in that case state.phase and state.error are already set — caller
    just needs to call _escalate_notify and return a CoordinatorResult.

    When multiple reviewers succeed, results are merged deterministically:
    strictest verdict wins, findings are unioned. No LLM synthesis call.

    Args:
        review_prompts: Pre-built prompts. If None, builds them (with role-aware
            prompts when review_role is configured). Pass explicitly to control
            prompt construction (e.g. run_review_only always uses generic prompts).
        enforce_budgets: When True (default), enforces per-profile budgets.
            When False (run_review_only), skips budget checks.
    """
    pool_size = len(config.review_pool)

    if review_prompts is None:
        commit_log = _get_commit_log(workspace_path, config.workspace.base_branch)
        handoff_content = _get_handoff_content(config, workspace_path)
        dev_notes = _get_dev_notes(config, workspace_path)

        review_prompts = (
            [
                build_review_prompt(
                    task,
                    spec_content=spec_content,
                    commit_log=commit_log,
                    workspace_path=str(workspace_path),
                    branch=branch_name,
                    handoff_content=handoff_content,
                    review_role=p.review_role,
                    dev_notes=dev_notes,
                )
                for p in config.review_pool
            ]
            if any(p.review_role for p in config.review_pool)
            else build_review_prompt(
                task,
                spec_content=spec_content,
                commit_log=commit_log,
                workspace_path=str(workspace_path),
                branch=branch_name,
                handoff_content=handoff_content,
                dev_notes=dev_notes,
            )
        )

    _log_verbose(f"Running {pool_size} reviewer(s): {[p.name for p in config.review_pool]}")
    _pool_start = time.monotonic()
    pool_session_ids = [state.reviewer_session_ids.get(p.name) for p in config.review_pool]
    for _p, _sid in zip(config.review_pool, pool_session_ids):
        _tag = f"resuming {_sid[:8]}" if _sid else "new session"
        _log_verbose(f"  reviewer {_p.name}: {_tag}")
    pool_results = run_agent_pool(
        prompt=review_prompts,
        profiles=config.review_pool,
        working_dir=workspace_path,
        session_ids=pool_session_ids,
    )
    _pool_elapsed = time.monotonic() - _pool_start
    for profile, result in zip(config.review_pool, pool_results):
        if result.session_id:
            state.reviewer_session_ids[profile.name] = result.session_id
    save_sessions(workspace_path, state.dev_session_id, state.reviewer_session_ids)
    _per_agent_dur = _pool_elapsed / max(len(pool_results), 1)
    _cycle_num = state.review_cycle + 1
    for r in pool_results:
        state.review_agent_results.append(r)
        state.review_durations.append(_per_agent_dur)
        log_agent_result(r, f"REVIEW/{r.profile_name}")
        write_trace(
            workspace_path
            / ".forge/traces"
            / f"{_cycle_num}-{pool_attempt}-review-{r.profile_name}.txt",
            r.output,
        )

    # Per-profile budget enforcement BEFORE synthesis (original ordering)
    if enforce_budgets:
        for profile in config.review_pool:
            profile_cost = sum(
                r.cost_usd for r in state.review_agent_results if r.profile_name == profile.name
            )
            if profile_cost > profile.budget_usd:
                state.phase = Phase.ESCALATE
                state.error = (
                    f"Review budget exceeded for {profile.name}: "
                    f"spent ${profile_cost:.4f} (limit ${profile.budget_usd:.4f})"
                )
                return [], [], None

    successful = [r for r in pool_results if r.success]
    failed_results = [r for r in pool_results if not r.success]

    for f in failed_results:
        _log_verbose(f"Pool reviewer failed: {f.profile_name} (exit={f.exit_code})")

    meta.successful = [r.profile_name for r in successful]
    meta.failed = [r.profile_name for r in failed_results]
    meta.failed_detail = {
        r.profile_name: (
            f"exit={r.exit_code}: {r.output[:200].strip()}" if r.output else f"exit={r.exit_code}"
        )
        for r in failed_results
    }

    if not successful:
        state.phase = Phase.ESCALATE
        failed_desc = ", ".join(f"{r.profile_name} (exit={r.exit_code})" for r in failed_results)
        state.error = f"All {len(pool_results)} review agent(s) failed: {failed_desc}"
        return successful, failed_results, None

    # Merge all successful reviewer outputs — no synthesis LLM call.
    # If only one succeeded, parse directly. If multiple, merge (strictest verdict,
    # union of findings) so the dev agent sees every finding from every reviewer.
    _synthesis_path = (
        workspace_path / ".forge/traces" / f"{_cycle_num}-{pool_attempt}-synthesis.txt"
    )
    if len(successful) == 1:
        write_trace(_synthesis_path, successful[0].output)
        return successful, failed_results, parse_review_output(successful[0].output)

    _log_verbose(
        f"Merging {len(successful)} review outputs (+{len(failed_results)} failed excluded)"
    )
    parsed_results = [parse_review_output(r.output) for r in successful]
    names = [r.profile_name for r in successful]
    merged = merge_review_results(parsed_results, names)
    write_trace(
        _synthesis_path,
        yaml.dump(dataclasses.asdict(merged), default_flow_style=False, allow_unicode=True),
    )
    return successful, failed_results, merged


def _setup_resume_entry(
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
    *,
    initial_phase: Phase,
    notify: bool,
    run_id: str | None,
) -> tuple[CoordinatorState, StructuredLogger, str, str, float] | CoordinatorResult:
    """Shared setup for run_from_review / run_from_dev.

    Returns (state, logger, branch_name, spec_content, task_start) on success,
    or a CoordinatorResult on failure (worktree missing).
    """
    state = CoordinatorState(
        phase=initial_phase,
        dev_iteration=0,
        review_cycle=0,
        preflight_verdict="SKIPPED",
    )
    state.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _task_start = time.monotonic()

    _run_id = run_id or _generate_run_id()
    logger = StructuredLogger(
        run_id=_run_id,
        project=config.project,
        task=task.slug,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
    )
    logger._safe_emit(
        "run_start",
        specs=[str(task.spec_path)],
        budget_usd=config.dev_profile.budget_usd,
        resume=True,
    )

    if not workspace_path.exists():
        state.phase = Phase.ESCALATE
        state.error = f"Worktree not found at {workspace_path}. Run `forge run` first."
        logger._safe_emit("escalate", reason=state.error, phase="INIT")
        logger._safe_emit("run_end", outcome="escalate", total_cost_usd=0.0, total_duration_s=0.0)
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    state.workspace_path = workspace_path

    # Restore session IDs from prior run if available
    _sessions = load_sessions(workspace_path)
    if _sessions.get("dev_session_id"):
        state.dev_session_id = _sessions["dev_session_id"]
    if _sessions.get("reviewer_session_ids"):
        state.reviewer_session_ids = _sessions["reviewer_session_ids"]

    # Resolve branch name from actual worktree HEAD
    _ok_branch, _branch_out = _cu._run_shell("git rev-parse --abbrev-ref HEAD", workspace_path)
    if _ok_branch and _branch_out.strip() and _branch_out.strip() != "HEAD":
        branch_name = _branch_out.strip()
    else:
        branch_name = config.workspace.branch_pattern.format(slug=task.slug)
    state.branch_name = branch_name

    spec_content = load_spec(task.spec_path)

    return state, logger, branch_name, spec_content, _task_start


def _coordinator_loop(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    spec_content: str,
    task_start: float,
    *,
    interactive: bool = False,
    auto_merge: bool = False,
    skip_dev_first_iter: bool = False,
    notify: bool = False,
    logger: StructuredLogger | None = None,
) -> CoordinatorResult:
    """Shared DEV→VALIDATE→REVIEW loop used by run_task() and run_from_review().

    Callers must set state.workspace_path and state.branch_name before calling.

    Args:
        skip_dev_first_iter: When True, the first loop iteration starts directly at
            REVIEW, skipping DEV+VALIDATE. All subsequent iterations run the full
            DEV→VALIDATE→REVIEW sequence. Used by run_from_review() to review the
            existing worktree before invoking the dev agent for the first time.
    """
    assert state.workspace_path is not None
    assert state.branch_name is not None
    workspace_path = state.workspace_path
    branch_name = state.branch_name
    _skip_dev = skip_dev_first_iter
    # Per-cycle retry counter for escalation: reset to 0 at the start of each
    # new review cycle (and on human extend/reject).  state.dev_iteration is a
    # CUMULATIVE counter across all review cycles.  Prompt routing uses
    # state.retry_reason, not dev_iteration, to select build_fix_prompt vs
    # build_dev_prompt.
    _dev_calls_this_cycle: int = 0

    while True:
        if not _skip_dev:
            # ── DEV ───────────────────────────────────────────────
            state.phase = Phase.DEV
            state.dev_iteration += 1
            _dev_calls_this_cycle += 1
            escalation = _run_dev_phase(
                state,
                config,
                task,
                spec_content,
                workspace_path,
                branch_name,
                notify=notify,
                logger=logger,
            )
            if escalation is not None:
                return escalation

            # ── VALIDATE ──────────────────────────────────────────
            _val_outcome, _val_result = _run_validate_phase(
                state,
                config,
                task,
                workspace_path,
                _dev_calls_this_cycle,
                notify=notify,
                logger=logger,
            )
            if _val_outcome == _ValidateOutcome.ESCALATE:
                return _val_result  # type: ignore[return-value]
            if _val_outcome == _ValidateOutcome.RETRY_DEV:
                if (
                    state.dev_results
                    and state.dev_results[-1].exit_code == -9
                    and state.dev_session_id
                    and state.retry_reason == "gate_fail"
                ):
                    gate_result = "FAIL"
                    if state.gate_decisions:
                        gate_result = state.gate_decisions[-1]
                    elif state.human_feedback:
                        prefix = "Gate validation failed: "
                        if state.human_feedback.startswith(prefix):
                            gate_result = f"FAIL - {state.human_feedback.removeprefix(prefix)}"
                    _set_timeout_resume(state, gate_result)
                continue

        if logger:
            logger._safe_emit("phase_end", phase="VALIDATE", outcome="pass")
        _skip_dev = False  # all subsequent iterations start at DEV

        # ── DEV HANDOFF VALIDATION ────────────────────────────
        # Validate structured dev handoff after gate passes.
        # Retry up to max_handoff_retries; if still invalid, proceed anyway.
        _handoff = _parse_dev_handoff(config, workspace_path)
        if _handoff is not None and _handoff.parse_errors:
            _max_hf_retries = config.retry.max_handoff_retries
            for _hf_attempt in range(_max_hf_retries):
                _log_verbose(
                    f"Dev handoff validation failed "
                    f"(attempt {_hf_attempt + 1}/{_max_hf_retries}): "
                    f"{_handoff.parse_errors}"
                )
                _log(f"  ⚠ HANDOFF   invalid → retry {_hf_attempt + 1}/{_max_hf_retries}")
                _hf_prompt = build_handoff_fix_prompt(
                    task,
                    workspace_path=workspace_path,
                    branch_name=branch_name,
                    validation_errors=_handoff.parse_errors,
                )
                _hf_result = run_agent(
                    prompt=_hf_prompt,
                    profile=config.dev_profile,
                    working_dir=workspace_path,
                    session_id=state.dev_session_id,
                )
                state.dev_results.append(_hf_result)
                state.dev_session_id = _hf_result.session_id or state.dev_session_id
                save_sessions(workspace_path, state.dev_session_id, state.reviewer_session_ids)
                log_agent_result(_hf_result, "DEV/handoff-fix")
                _handoff = _parse_dev_handoff(config, workspace_path)
                if _handoff is None or not _handoff.parse_errors:
                    _log("  ✓ HANDOFF   valid")
                    break
            else:
                _log("  ⚠ HANDOFF   still invalid after retries — proceeding anyway")

        # ── REVIEW ────────────────────────────────────────────
        _rev_outcome, _rev_result, config = _run_review_phase(
            state,
            config,
            task,
            spec_content,
            workspace_path,
            branch_name,
            task_start,
            interactive=interactive,
            auto_merge=auto_merge,
            notify=notify,
            logger=logger,
        )
        if _rev_outcome in (_ReviewOutcome.DONE, _ReviewOutcome.ESCALATE):
            return _rev_result  # type: ignore[return-value]
        # RETRY_DEV — reset cycle counter and loop back
        _dev_calls_this_cycle = 0


def run_task(
    config: ForgeConfig,
    task: TaskSpec,
    *,
    interactive: bool = False,
    auto_merge: bool = False,
    notify: bool = False,
    run_id: str | None = None,
    plan_path: Path | None = None,
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

    # ── Structured logger ──────────────────────────────────────────
    _run_id = run_id or _generate_run_id()
    logger = StructuredLogger(
        run_id=_run_id,
        project=config.project,
        task=task.slug,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
    )
    logger._safe_emit(
        "run_start",
        specs=[str(task.spec_path)],
        budget_usd=config.dev_profile.budget_usd,
        resume=False,
    )

    # ── Smart config display ───────────────────────────────────────
    if config.smart_config_models is not None:
        models_str = ", ".join(config.smart_config_models)
        dev_model = config.dev_profile.model
        review_models = ", ".join(p.model for p in config.review_pool)
        synth_model = config.synthesis_profile.model if config.synthesis_profile else "none"
        _log(f"  Models: {models_str}")
        _log(f"  Auto-config: dev={dev_model}, review=[{review_models}], synthesis={synth_model}")

    # ── Validate --plan path (before touching anything) ─────────
    if plan_path is not None:
        if not plan_path.is_file():
            msg = f"--plan path does not exist or is not a file: {plan_path}"
            _log(f"✗ {msg}")
            return CoordinatorResult(
                success=False,
                phase=Phase.INIT,
                state=state,
                message=msg,
            )
        try:
            plan_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"--plan path is not readable: {plan_path}: {exc}"
            _log(f"✗ {msg}")
            return CoordinatorResult(
                success=False,
                phase=Phase.INIT,
                state=state,
                message=msg,
            )

    # ── WORKSPACE ─────────────────────────────────────────────────
    state.phase = Phase.WORKSPACE
    _log_phase(state.phase, task.slug)
    logger._safe_emit("phase_start", phase="WORKSPACE", iteration=0)

    workspace_path, branch_name, err = _create_workspace(config, task)
    if err:
        state.phase = Phase.ESCALATE
        state.error = err
        logger._safe_emit("phase_end", phase="WORKSPACE", outcome="escalate")
        logger._safe_emit("escalate", reason=state.error, phase="WORKSPACE")
        logger._safe_emit("run_end", outcome="escalate", total_cost_usd=0.0, total_duration_s=0.0)
        _escalate_notify(task, state, notify, config)
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
    logger._safe_emit("phase_end", phase="WORKSPACE", outcome="success")

    # ── Plan injection (--plan) ─────────────────────────────────
    if plan_path is not None:
        plan_text = plan_path.read_text(encoding="utf-8")
        (workspace_path / "forge_plan.md").write_text(plan_text, encoding="utf-8")
        state.plan_output = plan_text
        _log(f"  ✓ PLAN   (injected from {plan_path.name})")
        if config.plan_review.enabled:
            _log("  ℹ PLAN_REVIEW   skipped (plan injected)")

    # ── PREFLIGHT ──────────────────────────────────────────────────
    state.phase = Phase.PREFLIGHT
    preflight_profile = config.preflight_profile
    _log_phase(state.phase, preflight_profile.model)
    logger._safe_emit("phase_start", phase="PREFLIGHT", iteration=0)

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
    _preflight_elapsed = time.monotonic() - _preflight_start
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

    # ── Complexity parsing + adaptive model swapping ───────────────
    if preflight_result.success:
        complexity = _parse_preflight_complexity(preflight_result.output)
        state.preflight_complexity = complexity
        _log(f"  Complexity: {complexity} (from preflight)")
        if config.smart_config_models is not None:
            config = _apply_complexity_adaptation(config, complexity)

    _log(f"  ✓ PREFLIGHT   {verdict}")
    _log_verbose(f"  Reason: {reason}")
    logger._safe_emit(
        "phase_end",
        phase="PREFLIGHT",
        outcome=verdict.lower(),
        cost_usd=preflight_result.cost_usd,
        duration_s=round(_preflight_elapsed, 2),
    )

    if verdict == "ALREADY_DONE":
        state.phase = Phase.DONE
        elapsed = time.monotonic() - _task_start
        _log(f"✓ DONE   total=${state.total_cost:.2f}  {_fmt_duration(elapsed)}")
        logger._safe_emit(
            "run_end",
            outcome="already_done",
            total_cost_usd=round(state.total_cost, 6),
            total_duration_s=round(elapsed, 2),
        )
        _ntfy_done_notify(
            task, state, config, notify, reason or "Spec already satisfied.", elapsed, branch_name
        )
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
        logger._safe_emit("escalate", reason=state.error, phase="PREFLIGHT")
        logger._safe_emit(
            "run_end",
            outcome="escalate",
            total_cost_usd=round(state.total_cost, 6),
            total_duration_s=round(time.monotonic() - _task_start, 2),
        )
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    # verdict == "PROCEED" — continue to DEV (possibly via PLAN)

    # ── PLAN ──────────────────────────────────────────────────────
    should_plan = (
        plan_path is None
        and config.plan.enabled
        and state.preflight_complexity in ("medium", "large")
    )
    if should_plan:
        state.phase = Phase.PLAN
        _plan_timeout = resolve_timeout(
            config.plan.timeout,
            config.plan.timeout_medium,
            config.plan.timeout_large,
            state.preflight_complexity,
        )
        _plan_override_active = (
            state.preflight_complexity == "large" and config.plan.timeout_large is not None
        ) or (state.preflight_complexity == "medium" and config.plan.timeout_medium is not None)
        if _plan_override_active:
            _log(f"  Plan timeout: {_plan_timeout}s ({state.preflight_complexity} complexity)")
        else:
            _log(f"  Plan timeout: {_plan_timeout}s")
        plan_profile = ModelProfile(
            name="plan",
            cli=config.plan.model,
            model=config.plan.model_name,
            budget_usd=config.plan.budget_usd,
            timeout_seconds=_plan_timeout,
            allowed_tools=config.preflight_profile.allowed_tools,
        )
        _log_phase(state.phase, plan_profile.model)

        plan_prompt = build_plan_prompt(
            task,
            spec_content=spec_content,
            file_contents=file_contents,
            preflight_output=(preflight_result.output if preflight_result.success else None),
        )

        _plan_start = time.monotonic()
        plan_result = run_agent(
            prompt=plan_prompt,
            profile=plan_profile,
            working_dir=workspace_path,
        )
        _plan_elapsed = time.monotonic() - _plan_start
        state.plan_results.append(plan_result)
        state.plan_session_id = plan_result.session_id or state.plan_session_id
        write_trace(workspace_path / ".forge/traces" / "plan.txt", plan_result.output)

        if plan_result.success:
            plan_text = plan_result.output
            (workspace_path / "forge_plan.md").write_text(plan_text, encoding="utf-8")
            state.plan_output = plan_text
            _log(f"  ✓ PLAN   ${plan_result.cost_usd:.2f}  {_fmt_duration(_plan_elapsed)}")

            if config.plan_agent_review.enabled:
                # ── Agent plan review ──────────────────────────────
                state.phase = Phase.PLAN_REVIEW
                state.plan_review_mode = "agent"
                par_cfg = config.plan_agent_review
                par_profile = ModelProfile(
                    name="plan-review",
                    cli=par_cfg.cli,
                    model=par_cfg.model,
                    budget_usd=par_cfg.budget_usd,
                    timeout_seconds=par_cfg.timeout,
                    allowed_tools=config.preflight_profile.allowed_tools,
                )
                if config.plan_review.enabled:
                    _log(
                        "  ⚠ Both plan_agent_review and plan_review enabled — "
                        "agent review takes precedence"
                    )

                _max = config.retry.max_plan_regen_attempts
                for _attempt in range(_max + 1):
                    _log_phase(state.phase, f"agent review (model={par_profile.model})")

                    pr_prompt = build_plan_review_prompt(
                        task,
                        story_content=spec_content,
                        plan_content=plan_text,
                        file_contents=file_contents,
                        preflight_output=(
                            preflight_result.output if preflight_result.success else None
                        ),
                        rejection_findings=state.plan_agent_review_findings,
                    )

                    _pr_start = time.monotonic()
                    pr_result = run_agent(
                        prompt=pr_prompt,
                        profile=par_profile,
                        working_dir=workspace_path,
                    )
                    _pr_elapsed = time.monotonic() - _pr_start
                    state.plan_review_results.append(pr_result)

                    if not pr_result.success:
                        # Agent failure → treat as REJECT
                        _log(
                            f"  ✗ PLAN_REVIEW   agent failed (exit={pr_result.exit_code}) "
                            f"— treating as REJECT"
                        )
                        parsed_pr = parse_plan_review_output("")  # force parse error → REJECT
                    else:
                        parsed_pr = parse_plan_review_output(pr_result.output)

                    if parsed_pr.parse_errors:
                        _log(
                            f"  ⚠ PLAN_REVIEW   parse issues: {'; '.join(parsed_pr.parse_errors)}"
                        )

                    # Check if REJECT has only P1/P2 findings (no P0) —
                    # treat as advisory approve, log findings for dev context.
                    # Guard: never downgrade when parse_errors is non-empty —
                    # a malformed response must not silently pass.
                    _has_p0 = any(f.severity == "P0" for f in parsed_pr.findings)
                    if (
                        parsed_pr.verdict == "REJECT"
                        and not _has_p0
                        and parsed_pr.findings
                        and not parsed_pr.parse_errors
                    ):
                        # Downgrade to APPROVE — P1/P2 are advisory in plan review
                        findings_text = plan_review_findings_to_text(parsed_pr)
                        state.plan_agent_review_findings = findings_text
                        _log(
                            f"  ✓ PLAN_REVIEW   approve (agent, {len(parsed_pr.findings)} "
                            f"advisory)  "
                            f"${pr_result.cost_usd:.2f}  {_fmt_duration(_pr_elapsed)}"
                        )
                        _log(f"  Advisory findings (passed to dev):\n{findings_text}")
                        parsed_pr = PlanReviewResult(
                            verdict="APPROVE",
                            findings=parsed_pr.findings,
                            parse_errors=parsed_pr.parse_errors,
                        )

                    if parsed_pr.verdict == "APPROVE":
                        state.plan_review_decision = "approve"
                        _log(
                            f"  ✓ PLAN_REVIEW   approve (agent)  "
                            f"${pr_result.cost_usd:.2f}  {_fmt_duration(_pr_elapsed)}"
                        )
                        # Commit the approved plan so it's preserved in git history
                        try:
                            _cu._run_shell(
                                ["git", "add", "forge_plan.md"],
                                cwd=workspace_path,
                            )
                            _cu._run_shell(
                                [
                                    "git",
                                    "commit",
                                    "-m",
                                    f"docs(plan): approved implementation plan for {task.slug}",
                                ],
                                cwd=workspace_path,
                            )
                            _log("  ✓ PLAN   committed forge_plan.md")
                        except Exception as _commit_err:
                            _log(f"  ⚠ PLAN   could not commit forge_plan.md: {_commit_err}")
                        break

                    # REJECT path
                    findings_text = plan_review_findings_to_text(parsed_pr)
                    state.plan_agent_review_findings = findings_text
                    _log(
                        f"  ✗ PLAN_REVIEW   reject (agent)  "
                        f"${pr_result.cost_usd:.2f}  {_fmt_duration(_pr_elapsed)}"
                    )

                    state.plan_regen_count += 1
                    if state.plan_regen_count > config.retry.max_plan_regen_attempts:
                        state.plan_review_decision = "reject"
                        state.phase = Phase.ESCALATE
                        state.error = (
                            f"Plan rejected {state.plan_regen_count} time(s) by agent reviewer "
                            f"(max_plan_regen_attempts={config.retry.max_plan_regen_attempts}). "
                            f"Findings:\n{findings_text}"
                        )
                        _log(f"  ✗ PLAN_REVIEW   rejected {state.plan_regen_count}x — escalating")
                        _escalate_notify(task, state, notify, config)
                        return CoordinatorResult(
                            success=False,
                            phase=Phase.ESCALATE,
                            state=state,
                            message=state.error,
                        )

                    # REJECT → regenerate plan with findings
                    state.plan_review_decision = "regenerate"
                    _log(
                        f"  ↺ PLAN_REVIEW   reject → regenerating plan "
                        f"(attempt {state.plan_regen_count}/{_max})"
                    )
                    _log(f"  Findings:\n{findings_text}")

                    # Rebuild plan prompt with rejection findings appended
                    regen_prompt = build_plan_prompt(
                        task,
                        spec_content=spec_content,
                        file_contents=file_contents,
                        preflight_output=(
                            preflight_result.output if preflight_result.success else None
                        ),
                    )
                    regen_prompt += (
                        "\n\n## Previous Plan Review Findings\n\n"
                        "The previous plan was REJECTED. Address these issues:\n\n"
                        f"{findings_text}\n"
                    )

                    if _LOG_LEVEL >= LogLevel.VERBOSE:
                        regen_prompt += (
                            "\n\n## Session Continuity Check\n\n"
                            "Begin your response with exactly one line in this format:\n"
                            "PRIOR CONTEXT: [one sentence describing the key naming/filing "
                            "approach from your previous plan attempt]\n\n"
                            "This confirms you have access to your prior session context."
                        )

                    _plan_start = time.monotonic()
                    _resuming = state.plan_session_id is not None
                    _resume_tag = (
                        f"resuming {state.plan_session_id[:8]}" if _resuming else "new session"
                    )
                    _log(f"  Starting plan regen (model={plan_profile.model}, {_resume_tag})...")
                    plan_result = run_agent(
                        prompt=regen_prompt,
                        profile=plan_profile,
                        working_dir=workspace_path,
                        session_id=state.plan_session_id,
                    )
                    _plan_elapsed = time.monotonic() - _plan_start
                    state.plan_results.append(plan_result)
                    state.plan_session_id = plan_result.session_id or state.plan_session_id
                    write_trace(workspace_path / ".forge/traces" / "plan.txt", plan_result.output)

                    if not plan_result.success:
                        state.phase = Phase.ESCALATE
                        state.error = "PLAN regeneration failed after agent review REJECT"
                        _log("  ✗ PLAN regen failed — escalating")
                        _escalate_notify(task, state, notify, config)
                        return CoordinatorResult(
                            success=False,
                            phase=state.phase,
                            state=state,
                            message=state.error,
                        )

                    plan_text = plan_result.output
                    (workspace_path / "forge_plan.md").write_text(plan_text, encoding="utf-8")
                    state.plan_output = plan_text
                    _log(
                        "  ✓ PLAN (regenerated)  "
                        f"${plan_result.cost_usd:.2f}  {_fmt_duration(_plan_elapsed)}"
                    )

            elif config.plan_review.enabled:
                for _ in range(config.retry.max_plan_regen_attempts + 1):
                    state.phase = Phase.PLAN_REVIEW
                    _log_phase(state.phase, "waiting for human decision...")
                    _log(f"  Plan written to: {workspace_path / 'forge_plan.md'}")

                    _pr_start = time.monotonic()
                    if _is_remote_mode(notify, config):
                        plan_review_decision = _plan_review_remote(
                            state, plan_text, workspace_path, task, config
                        )
                    else:
                        plan_review_decision = _plan_review_interactive(
                            state, plan_text, workspace_path, task
                        )
                        state.plan_review_mode = "interactive"
                    state.plan_review_waited_seconds = time.monotonic() - _pr_start
                    state.plan_review_decision = plan_review_decision

                    if plan_review_decision == "approve":
                        try:
                            updated = (workspace_path / "forge_plan.md").read_text(
                                encoding="utf-8"
                            )
                        except (OSError, UnicodeDecodeError) as exc:
                            state.phase = Phase.ESCALATE
                            state.error = f"forge_plan.md unreadable after edit: {exc}"
                            _log(f"  ✗ PLAN_REVIEW   {state.error}")
                            _escalate_notify(task, state, notify, config)
                            return CoordinatorResult(
                                success=False,
                                phase=Phase.ESCALATE,
                                state=state,
                                message=state.error,
                            )
                        state.plan_output = updated
                        plan_text = updated
                        write_trace(workspace_path / ".forge/traces" / "plan.txt", updated)
                        _log(
                            "  ✓ PLAN_REVIEW   approve  "
                            f"({_fmt_duration(state.plan_review_waited_seconds or 0)})"
                        )
                        # Commit the approved plan so it's preserved in git history
                        try:
                            _cu._run_shell(
                                ["git", "add", "forge_plan.md"],
                                cwd=workspace_path,
                            )
                            _cu._run_shell(
                                [
                                    "git",
                                    "commit",
                                    "-m",
                                    f"docs(plan): approved implementation plan for {task.slug}",
                                ],
                                cwd=workspace_path,
                            )
                            _log("  ✓ PLAN   committed forge_plan.md")
                        except Exception as _commit_err:
                            _log(f"  ⚠ PLAN   could not commit forge_plan.md: {_commit_err}")
                        break

                    if plan_review_decision == "regenerate":
                        state.plan_regen_count += 1
                        if state.plan_regen_count > config.retry.max_plan_regen_attempts:
                            state.plan_review_decision = "abandon"
                            _log(
                                f"  ✗ PLAN_REVIEW   rejected "
                                f"{state.plan_regen_count}x — abandoning"
                            )
                            return CoordinatorResult(
                                success=False,
                                phase=Phase.PLAN_REVIEW,
                                state=state,
                                message=(
                                    f"Plan rejected {state.plan_regen_count} time(s) — abandoning."
                                ),
                            )

                        _max2 = config.retry.max_plan_regen_attempts
                        _log(
                            f"  ↺ PLAN_REVIEW   regenerate — re-running PLAN agent "
                            f"(attempt {state.plan_regen_count}/{_max2})"
                        )

                        _plan_start = time.monotonic()
                        plan_result = run_agent(
                            prompt=plan_prompt,
                            profile=plan_profile,
                            working_dir=workspace_path,
                            session_id=state.plan_session_id,
                        )
                        _plan_elapsed = time.monotonic() - _plan_start
                        state.plan_results.append(plan_result)
                        state.plan_session_id = plan_result.session_id or state.plan_session_id
                        write_trace(
                            workspace_path / ".forge/traces" / "plan.txt", plan_result.output
                        )

                        if not plan_result.success:
                            state.phase = Phase.ESCALATE
                            state.error = "PLAN regeneration failed"
                            _log("  ✗ PLAN regen failed — escalating")
                            _escalate_notify(task, state, notify, config)
                            return CoordinatorResult(
                                success=False,
                                phase=state.phase,
                                state=state,
                                message=state.error,
                            )

                        plan_text = plan_result.output
                        (workspace_path / "forge_plan.md").write_text(plan_text, encoding="utf-8")
                        state.plan_output = plan_text
                        _log(
                            "  ✓ PLAN (regenerated)  "
                            f"${plan_result.cost_usd:.2f}  {_fmt_duration(_plan_elapsed)}"
                        )
                        continue

                    _log(f"  ✗ PLAN_REVIEW   abandoned — worktree preserved at {workspace_path}")
                    state.phase = Phase.PLAN_REVIEW
                    return CoordinatorResult(
                        success=False,
                        phase=Phase.PLAN_REVIEW,
                        state=state,
                        message="Plan review abandoned by human.",
                    )
        else:
            state.phase = Phase.ESCALATE
            state.error = (
                "PLAN phase failed — task requires a plan but the planning agent "
                f"did not produce one (exit={plan_result.exit_code}). "
                "Consider increasing plan timeout or simplifying the spec."
            )
            _log("  ✗ PLAN failed — escalating (not proceeding blind)")
            _log(f"✗ ESCALATE   {state.error}")
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )

    # ── DEV→VALIDATE→REVIEW loop ─────────────────────────────────
    result = _coordinator_loop(
        state,
        config,
        task,
        spec_content,
        _task_start,
        interactive=interactive,
        auto_merge=auto_merge,
        notify=notify,
        logger=logger,
    )
    _total_elapsed = time.monotonic() - _task_start
    logger._safe_emit(
        "run_end",
        outcome="done" if result.success else "escalate",
        total_cost_usd=round(state.total_cost, 6),
        total_duration_s=round(_total_elapsed, 2),
    )
    return result


# ── Review-from-existing-worktree mode (full iteration loop) ─────────


def run_from_review(
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
    *,
    interactive: bool = False,
    auto_merge: bool = False,
    notify: bool = False,
    run_id: str | None = None,
) -> CoordinatorResult:
    """Start at REVIEW on an existing worktree, then iterate DEV→VALIDATE→REVIEW as needed.

    This is a first-class entry point that behaves identically to run_task but:
    - Skips WORKSPACE creation and PREFLIGHT (workspace already exists)
    - Begins with an immediate REVIEW of the current worktree state
    - If APPROVE: done (auto-merge if requested)
    - If REQUEST_CHANGES: iterates through DEV→VALIDATE→REVIEW exactly as run_task does

    Args:
        config: The forge configuration.
        task: The task specification.
        workspace_path: Path to the existing worktree.
        interactive: When True, pause at HUMAN_REVIEW for operator input.
        auto_merge: When True, merge the feature branch after APPROVE.
    """
    setup = _setup_resume_entry(
        config,
        task,
        workspace_path,
        initial_phase=Phase.REVIEW,
        notify=notify,
        run_id=run_id,
    )
    if isinstance(setup, CoordinatorResult):
        return setup
    state, logger, branch_name, spec_content, _task_start = setup

    # First iteration starts at REVIEW (skip DEV+VALIDATE for existing worktree).
    result = _coordinator_loop(
        state,
        config,
        task,
        spec_content,
        _task_start,
        interactive=interactive,
        auto_merge=auto_merge,
        skip_dev_first_iter=True,
        notify=notify,
        logger=logger,
    )
    logger._safe_emit(
        "run_end",
        outcome="done" if result.success else "escalate",
        total_cost_usd=round(state.total_cost, 6),
        total_duration_s=round(time.monotonic() - _task_start, 2),
    )
    return result


# ── Dev-from-existing-worktree mode ─────────────────────────────────


def run_from_dev(
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
    *,
    interactive: bool = False,
    auto_merge: bool = False,
    notify: bool = False,
    run_id: str | None = None,
) -> CoordinatorResult:
    """Start at DEV on an existing worktree, skipping WORKSPACE and PREFLIGHT.

    Used by sprint resume when a worktree has commits ahead of the base branch
    but the gate failed. Reuses the existing workspace without recreating it.

    Args:
        config: The forge configuration.
        task: The task specification.
        workspace_path: Path to the existing worktree.
        interactive: When True, pause at HUMAN_REVIEW for operator input.
        auto_merge: When True, merge the feature branch after APPROVE.
    """
    setup = _setup_resume_entry(
        config,
        task,
        workspace_path,
        initial_phase=Phase.DEV,
        notify=notify,
        run_id=run_id,
    )
    if isinstance(setup, CoordinatorResult):
        return setup
    state, logger, branch_name, spec_content, _task_start = setup

    result = _coordinator_loop(
        state,
        config,
        task,
        spec_content,
        _task_start,
        interactive=interactive,
        auto_merge=auto_merge,
        skip_dev_first_iter=False,
        notify=notify,
        logger=logger,
    )
    logger._safe_emit(
        "run_end",
        outcome="done" if result.success else "escalate",
        total_cost_usd=round(state.total_cost, 6),
        total_duration_s=round(time.monotonic() - _task_start, 2),
    )
    return result


# ── Review-only mode ─────────────────────────────────────────────────


def run_review_only(
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
    *,
    notify: bool = False,
) -> CoordinatorResult:
    """Run only the REVIEW phase on an existing worktree.

    Skips WORKSPACE, PREFLIGHT, DEV, VALIDATE.
    Returns a CoordinatorResult with phase=DONE (APPROVE) or ESCALATE
    (REQUEST_CHANGES — no DEV retry in review-only mode).
    """
    state = CoordinatorState()
    state.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _ro_task_start = time.monotonic()

    _run_id = _generate_run_id()
    logger = StructuredLogger(
        run_id=_run_id,
        project=config.project,
        task=task.slug,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
    )
    logger._safe_emit(
        "run_start",
        specs=[str(task.spec_path)],
        budget_usd=config.dev_profile.budget_usd,
        resume=True,
    )

    # ── Verify workspace exists ───────────────────────────────────────
    if not workspace_path.exists():
        state.phase = Phase.ESCALATE
        state.error = f"Worktree not found at {workspace_path}. Run `forge run` first."
        logger._safe_emit("escalate", reason=state.error, phase="INIT")
        logger._safe_emit("run_end", outcome="escalate", total_cost_usd=0.0, total_duration_s=0.0)
        _escalate_notify(task, state, notify, config)
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
    logger._safe_emit("phase_start", phase="REVIEW", iteration=1)
    state.review_cycle = 1
    state.dev_iteration = 0
    _pool_model_names_ro = "+".join(p.model for p in config.review_pool)
    _log_phase(state.phase, f"{_pool_model_names_ro}  cycle=1  (review-only)")

    # Build generic prompt (no review_role) — review-only always used this
    commit_log = _get_commit_log(workspace_path, config.workspace.base_branch)
    handoff_content = _get_handoff_content(config, workspace_path)
    dev_notes = _get_dev_notes(config, workspace_path)
    review_prompt = build_review_prompt(
        task,
        spec_content=spec_content,
        commit_log=commit_log,
        workspace_path=str(workspace_path),
        branch=branch_name,
        handoff_content=handoff_content,
        dev_notes=dev_notes,
    )

    meta = ReviewCycleMetadata(
        pool_models=[p.name for p in config.review_pool],
        successful=[],
        failed=[],
        synthesized=False,
    )
    state.review_cycle_metadata.append(meta)

    _pool_start = time.monotonic()
    successful, failed_results, parsed_review = _run_review_pool(
        state,
        config,
        task,
        spec_content,
        workspace_path,
        branch_name,
        meta,
        notify=notify,
        review_prompts=review_prompt,
        enforce_budgets=False,
    )
    _pool_elapsed = time.monotonic() - _pool_start

    if parsed_review is None:
        # All reviewers failed — state.error already set
        _log(f"✗ ESCALATE   {state.error}")
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

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

    logger._safe_emit(
        "review_result",
        verdict=parsed_review.verdict,
        p1_count=_ro_p1,
        p2_count=_ro_p2,
        cost_usd=round(_ro_cost, 6),
    )

    if parsed_review.verdict == "APPROVE":
        state.phase = Phase.DONE
        _dur = _fmt_duration(_ro_elapsed)
        _log(f"  ✓ REVIEW   APPROVE  {_ro_p1} P1  {_ro_p2} P2  ${_ro_cost:.2f}  {_dur}")
        _log(f"✓ DONE   total=${state.total_cost:.2f}  {_fmt_duration(_ro_elapsed)}")
        logger._safe_emit(
            "phase_end",
            phase="REVIEW",
            outcome="approve",
            cost_usd=round(_ro_cost, 6),
            duration_s=round(_ro_elapsed, 2),
        )
        logger._safe_emit(
            "run_end",
            outcome="done",
            total_cost_usd=round(state.total_cost, 6),
            total_duration_s=round(time.monotonic() - _ro_task_start, 2),
        )
        _ntfy_done_notify(
            task, state, config, notify, parsed_review.summary, _ro_elapsed, branch_name
        )
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
    _log(
        f"  ✗ REVIEW   REQUEST_CHANGES  {_ro_p1} P1  ${_ro_cost:.2f}  {_fmt_duration(_ro_elapsed)}"
    )
    _log(f"✗ ESCALATE   {state.error}")
    logger._safe_emit(
        "phase_end",
        phase="REVIEW",
        outcome="escalate",
        cost_usd=round(_ro_cost, 6),
        duration_s=round(_ro_elapsed, 2),
    )
    logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
    logger._safe_emit(
        "run_end",
        outcome="escalate",
        total_cost_usd=round(state.total_cost, 6),
        total_duration_s=round(time.monotonic() - _ro_task_start, 2),
    )
    _escalate_notify(task, state, notify, config)
    return CoordinatorResult(
        success=False,
        phase=state.phase,
        state=state,
        message=state.error,
    )


# ── Audit ────────────────────────────────────────────────────────────

from .coord_audit import generate_audit_log  # noqa: E402, F401
