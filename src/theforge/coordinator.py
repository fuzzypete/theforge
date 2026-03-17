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

import datetime
import subprocess
import sys as _sys
import time
from dataclasses import replace as _dc_replace
from pathlib import Path

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
)

# ── Re-exports for backward compatibility ────────────────────────────
from .coord_state import (  # noqa: F401
    CoordinatorResult,
    CoordinatorState,
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
    ReviewResult,
    parse_plan_review_output,
    parse_review_output,
    plan_review_findings_to_text,
    review_to_dev_handoff,
)
from .runner import log_agent_result, run_agent, run_agent_pool
from .task import (  # noqa: F401
    TaskSpec,
    build_dev_prompt,
    build_fix_prompt,
    build_handoff_fix_prompt,
    build_plan_prompt,
    build_plan_review_prompt,
    build_preflight_prompt,
    build_review_prompt,
    build_synthesis_prompt,
    load_spec,
)

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

    Returns None if there's no handoff file or dev_notes field.
    Returns DevHandoff with parse_errors if validation fails.
    """
    raw = _get_raw_dev_notes(config, workspace_path)
    if raw is None:
        return None
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
) -> tuple[list, list, str | None]:
    """Run the review pool + synthesis.  Returns (successful, failed, synthesis_output).

    Updates *meta* in-place (successful, failed, failed_detail, synthesized).
    synthesis_output is None when all reviewers failed, budget exceeded, or synthesis
    agent failed; in that case state.phase and state.error are already set — caller
    just needs to call _escalate_notify and return a CoordinatorResult.

    Args:
        review_prompts: Pre-built prompts. If None, builds them (with role-aware
            prompts when review_role is configured). Pass explicitly to control
            prompt construction (e.g. run_review_only always uses generic prompts).
        enforce_budgets: When True (default), enforces per-profile and synthesis
            budgets. When False (run_review_only), skips budget checks.
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
    _per_agent_dur = _pool_elapsed / max(len(pool_results), 1)
    for r in pool_results:
        state.review_agent_results.append(r)
        state.review_durations.append(_per_agent_dur)
        log_agent_result(r, f"REVIEW/{r.profile_name}")

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

    # Determine synthesis output
    _is_degraded = len(failed_results) > 0 and pool_size > 1
    if config.synthesis_profile is None or _is_degraded:
        if _is_degraded:
            _log_verbose(
                f"Degraded: {len(successful)} of {pool_size} reviewers succeeded, "
                "skipping synthesis"
            )
        return successful, failed_results, successful[0].output

    # Multi-model: run synthesis over all successful outputs
    meta.synthesized = True
    _log_verbose(
        f"Synthesizing {len(successful)} review outputs (+{len(failed_results)} failed excluded)"
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
    synthesis_result = _dc_replace(synthesis_result, profile_name="synthesis")
    state.review_agent_results.append(synthesis_result)
    state.review_durations.append(_synth_elapsed)
    log_agent_result(synthesis_result, "SYNTHESIS")

    # Synthesis budget enforcement (original ordering: after synthesis, before returning)
    if enforce_budgets and config.synthesis_profile is not None:
        synth_cost = sum(
            r.cost_usd for r in state.review_agent_results if r.profile_name == "synthesis"
        )
        if synth_cost > config.synthesis_profile.budget_usd:
            state.phase = Phase.ESCALATE
            state.error = (
                f"Synthesis budget exceeded: "
                f"spent ${synth_cost:.4f} "
                f"(limit ${config.synthesis_profile.budget_usd:.4f})"
            )
            return successful, failed_results, None

    if not synthesis_result.success:
        state.phase = Phase.ESCALATE
        state.error = f"Synthesis agent failed (exit={synthesis_result.exit_code})"
        return successful, failed_results, None

    return successful, failed_results, synthesis_result.output


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
        plan_profile = ModelProfile(
            name="plan",
            cli=config.plan.model,
            model="opus",
            budget_usd=config.plan.budget_usd,
            timeout_seconds=config.plan.timeout,
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

                for _attempt in range(2):
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

                    if parsed_pr.verdict == "APPROVE":
                        state.plan_review_decision = "approve"
                        _log(
                            f"  ✓ PLAN_REVIEW   approve (agent)  "
                            f"${pr_result.cost_usd:.2f}  {_fmt_duration(_pr_elapsed)}"
                        )
                        break

                    # REJECT path
                    findings_text = plan_review_findings_to_text(parsed_pr)
                    state.plan_agent_review_findings = findings_text
                    _log(
                        f"  ✗ PLAN_REVIEW   reject (agent)  "
                        f"${pr_result.cost_usd:.2f}  {_fmt_duration(_pr_elapsed)}"
                    )

                    if state.plan_regenerated:
                        # Second REJECT → escalate
                        state.plan_review_decision = "reject"
                        state.phase = Phase.ESCALATE
                        state.error = (
                            f"Plan rejected twice by agent reviewer. Findings:\n{findings_text}"
                        )
                        _log("  ✗ PLAN_REVIEW   double reject — escalating")
                        _escalate_notify(task, state, notify, config)
                        return CoordinatorResult(
                            success=False,
                            phase=Phase.ESCALATE,
                            state=state,
                            message=state.error,
                        )

                    # First REJECT → regenerate plan with findings
                    state.plan_regenerated = True
                    state.plan_review_decision = "regenerate"
                    _log("  ↺ PLAN_REVIEW   reject → regenerating plan with findings")

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

                    (workspace_path / "forge_plan.md").unlink(missing_ok=True)
                    _plan_start = time.monotonic()
                    plan_result = run_agent(
                        prompt=regen_prompt,
                        profile=plan_profile,
                        working_dir=workspace_path,
                    )
                    _plan_elapsed = time.monotonic() - _plan_start
                    state.plan_results.append(plan_result)

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
                for _ in range(2):
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
                        _log(
                            "  ✓ PLAN_REVIEW   approve  "
                            f"({_fmt_duration(state.plan_review_waited_seconds or 0)})"
                        )
                        break

                    if plan_review_decision == "regenerate":
                        if state.plan_regenerated:
                            state.plan_review_decision = "abandon"
                            _log("  ✗ PLAN_REVIEW   already regenerated once — abandoning")
                            return CoordinatorResult(
                                success=False,
                                phase=Phase.PLAN_REVIEW,
                                state=state,
                                message="Plan regenerated once already — abandoning.",
                            )

                        state.plan_regenerated = True
                        _log("  ↺ PLAN_REVIEW   regenerate — re-running PLAN agent")

                        (workspace_path / "forge_plan.md").unlink(missing_ok=True)
                        _plan_start = time.monotonic()
                        plan_result = run_agent(
                            prompt=plan_prompt,
                            profile=plan_profile,
                            working_dir=workspace_path,
                        )
                        _plan_elapsed = time.monotonic() - _plan_start
                        state.plan_results.append(plan_result)

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
    successful, failed_results, synthesis_output = _run_review_pool(
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

    if synthesis_output is None:
        # All reviewers failed or synthesis failed — state.error already set
        _log(f"✗ ESCALATE   {state.error}")
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    # Single-shot parse — no retries in review-only mode
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
