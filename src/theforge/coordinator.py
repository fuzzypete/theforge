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
import json
import subprocess
import time
from dataclasses import replace as _dc_replace
from pathlib import Path

from . import coord_util as _cu
from .config import MODEL_REGISTRY, ForgeConfig, ModelProfile
from .coord_gate import (  # noqa: F401
    _auto_commit_side_effects,
    _is_gate_skip,
    _parse_dirty_files,
    _read_gate_decision,
    _run_gate,
    _run_gate_full,
)
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
from .review import ReviewResult, parse_review_output, review_to_dev_handoff
from .runner import log_agent_result, run_agent, run_agent_pool
from .task import (
    TaskSpec,
    build_dev_prompt,
    build_fix_prompt,
    build_plan_prompt,
    build_preflight_prompt,
    build_review_prompt,
    build_synthesis_prompt,
    load_spec,
)

# ── Structured logging ────────────────────────────────────────────────


class StructuredLogger:
    """Append-only JSON Lines logger for persistent run events.

    Writes one JSON object per line to ~/.forge/logs/<project>/forge.log
    (or a configured path). All writes are best-effort — failures are
    silently swallowed and never crash the run.
    """

    def __init__(
        self,
        run_id: str,
        project: str,
        task: str,
        log_file: str,
        enabled: bool,
    ) -> None:
        self._run_id = run_id
        self._project = project
        self._task = task
        self._enabled = enabled
        if enabled:
            resolved = log_file.replace("{project}", project)
            self._log_path = Path(resolved).expanduser()
        else:
            self._log_path = Path("/dev/null")

    def emit(self, event: str, **fields: object) -> None:
        """Append one JSON event line to the log file. Never raises."""
        if not self._enabled:
            return
        try:
            entry = {
                "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "project": self._project,
                "run_id": self._run_id,
                "task": self._task,
                "event": event,
                **fields,
            }
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _safe_emit(self, event: str, **fields: object) -> None:
        """Call emit(), silently swallowing any exception (including mocked errors)."""
        try:
            self.emit(event, **fields)
        except Exception:
            pass


# ── Shell helper ─────────────────────────────────────────────────────


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


def _get_dev_notes(config: ForgeConfig, workspace_path: Path) -> str | None:
    """Extract dev_notes from handoff.yaml, or None if absent/unparseable."""
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


# ── State machine ────────────────────────────────────────────────────


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
            _log_phase(
                state.phase,
                f"{config.dev_profile.model}  iter={state.dev_iteration}",
            )
            if logger:
                logger._safe_emit("phase_start", phase="DEV", iteration=state.dev_iteration)

            _gate_cmd = (
                task.gate_override
                if task.gate_override is not None and not _is_gate_skip(task.gate_override)
                else config.validation.gate_command
            )
            if state.retry_reason in ("review_changes", "extend") and state.last_review_findings:
                prompt = build_fix_prompt(
                    task,
                    workspace_path=workspace_path,
                    branch_name=branch_name,
                    review_findings=state.last_review_findings,
                    gate_command=_gate_cmd,
                    gate_skipped=_is_gate_skip(task.gate_override),
                    iteration=state.dev_iteration,
                )
            else:
                prompt = build_dev_prompt(
                    task,
                    workspace_path=workspace_path,
                    branch_name=branch_name,
                    spec_content=spec_content,
                    gate_command=_gate_cmd,
                    gate_skipped=_is_gate_skip(task.gate_override),
                    review_findings=state.last_review_findings,
                    human_feedback=state.human_feedback,
                    preflight_output=(
                        state.preflight_result.output if state.preflight_result else None
                    ),
                    plan_output=state.plan_output,
                    iteration=state.dev_iteration,
                )
            state.retry_reason = None  # consumed

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
            _log(f"  ✓ DEV   ${dev_result.cost_usd:.2f}  {_fmt_duration(_dev_elapsed)}")
            if logger:
                logger._safe_emit(
                    "phase_end",
                    phase="DEV",
                    outcome="success" if dev_result.success else "failure",
                    cost_usd=dev_result.cost_usd,
                    duration_s=round(_dev_elapsed, 2),
                )

            if state.total_dev_cost > config.dev_profile.budget_usd:
                state.phase = Phase.ESCALATE
                state.error = (
                    f"Dev budget exceeded: spent ${state.total_dev_cost:.4f} "
                    f"(limit ${config.dev_profile.budget_usd:.4f})"
                )
                _log(f"✗ ESCALATE   {state.error}")
                if logger:
                    logger._safe_emit("escalate", reason=state.error, phase="DEV")
                _escalate_notify(task, state, notify, config)
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
            if logger:
                logger._safe_emit("phase_start", phase="VALIDATE", iteration=state.dev_iteration)

            _gate_start = time.monotonic()
            gate_override = task.gate_override
            gate_output_tail: str = ""
            if _is_gate_skip(gate_override):
                _log_phase(state.phase, "skipped (gate: none)")
                _log("  Gate: none (spec override)")
                gate_decision: str | None = "PASS"
                gate_err: str | None = None
            else:
                if gate_override is not None:
                    _log_phase(state.phase, "running gate... (override)")
                    _log(f"  Gate: {gate_override} (spec override)")
                else:
                    _log_phase(state.phase, "running gate...")
                gate_decision, gate_err, gate_output_tail = _run_gate_full(
                    config, workspace_path, task=task
                )
            _gate_elapsed = time.monotonic() - _gate_start
            if logger:
                logger._safe_emit(
                    "gate_result",
                    decision=gate_decision or gate_err,
                    duration_s=round(_gate_elapsed, 2),
                    output_tail=gate_output_tail[-500:] if gate_output_tail else "",
                )

            if gate_err:
                use_exit_code = not config.validation.handoff_file
                if use_exit_code:
                    # Infrastructure errors in exit-code mode escalate immediately —
                    # they are not code-quality failures the dev agent can fix.
                    _log(f"✗ ESCALATE   {gate_err}")
                    state.phase = Phase.ESCALATE
                    state.error = gate_err
                    if logger:
                        logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                        logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
                    _escalate_notify(task, state, notify, config)
                    return CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    )
                # Handoff mode: retry dev with feedback (original behavior)
                _log_verbose(f"Gate error: {gate_err}")
                if _dev_calls_this_cycle >= config.retry.max_dev_iterations:
                    state.phase = Phase.ESCALATE
                    state.error = f"Gate failed after {state.dev_iteration} attempts: {gate_err}"
                    _log(f"✗ ESCALATE   {state.error}")
                    if logger:
                        logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                        logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
                    _escalate_notify(task, state, notify, config)
                    return CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    )
                state.human_feedback = f"Gate validation failed: {gate_err}"
                state.retry_reason = "gate_fail"
                _log(f"  ✗ VALIDATE   FAIL  (iter={state.dev_iteration} → retrying)")
                if logger:
                    logger._safe_emit("phase_end", phase="VALIDATE", outcome="fail")
                continue

            assert gate_decision is not None
            state.gate_decisions.append(gate_decision)
            _log_verbose(f"Gate decision: {gate_decision}")

            if gate_decision == "PASS":
                _log("  ✓ VALIDATE   PASS")
                # Run pre_validate_command (e.g. commit build artifacts) before dirty check.
                # Failure is non-fatal — log a warning and proceed to the dirty check.
                pre_validate_cmd = config.validation.pre_validate_command
                if pre_validate_cmd:
                    _log(f"  Running pre-validate command: {pre_validate_cmd}")
                    pv_ok, pv_out = _cu._run_shell(pre_validate_cmd, workspace_path)
                    if not pv_ok:
                        _log(f"  ⚠ Pre-validate command failed (non-fatal): {pv_out[:200]}")
                    else:
                        _log_verbose(f"Pre-validate output: {pv_out[:200]}")
                # Verify worktree is clean — the dev agent must commit all changes.
                # The gate runs against the working tree, so it can pass even with
                # uncommitted files. This check catches that process violation.
                dirty_ok, dirty_out = _cu._run_shell("git status --porcelain", workspace_path)
                if dirty_ok and dirty_out.strip():
                    # Filter out handoff.yaml and other gate artifacts
                    handoff_file = config.validation.handoff_file
                    if handoff_file:
                        dirty_lines = [
                            line
                            for line in dirty_out.splitlines()
                            if line.strip() and not line.endswith(handoff_file)
                        ]
                    else:
                        dirty_lines = [line for line in dirty_out.splitlines() if line.strip()]
                    if dirty_lines:
                        parsed_files = _parse_dirty_files("\n".join(dirty_lines))

                        # A file is in-scope if its path equals a scope entry or is nested
                        # under a scope directory.  Normalise to a trailing "/" so that
                        # "src/theforge" never falsely matches "src/theforgery/...".
                        def _in_scope(f: str) -> bool:
                            for s in task.file_scope:
                                if f == s:
                                    return True
                                prefix = s if s.endswith("/") else f"{s}/"
                                if f.startswith(prefix):
                                    return True
                            return False

                        # Shared helper: log the violation, set state for DEV retry or
                        # ESCALATE, and return a CoordinatorResult on escalation (else None).
                        def _dirty_dev_retry(dirty_files: str) -> CoordinatorResult | None:
                            _log(f"Dirty worktree detected: {dirty_files}")
                            if _dev_calls_this_cycle >= config.retry.max_dev_iterations:
                                state.phase = Phase.ESCALATE
                                state.error = f"Dev agent left uncommitted changes: {dirty_files}"
                                _log(f"✗ ESCALATE   {state.error}")
                                _escalate_notify(task, state, notify, config)
                                return CoordinatorResult(
                                    success=False,
                                    phase=state.phase,
                                    state=state,
                                    message=state.error,
                                )
                            state.human_feedback = (
                                "PROCESS VIOLATION: You left uncommitted changes in"
                                f" the worktree: {dirty_files}. You MUST commit ALL"
                                " modified files before running the gate. Stage and"
                                " commit them now."
                            )
                            state.retry_reason = "dirty_worktree"
                            return None

                        # Auto-commit eligible only when:
                        #   1. file_scope is set
                        #   2. every dirty line is a tracked file (len match — no ??/!! dropped)
                        #   3. no tracked file is in-scope
                        all_tracked = len(parsed_files) == len(dirty_lines)
                        if task.file_scope and parsed_files and all_tracked:
                            in_scope = [f for f in parsed_files if _in_scope(f)]
                            out_of_scope = [f for f in parsed_files if not _in_scope(f)]
                            if not in_scope:
                                # All dirty entries are tracked out-of-scope → auto-commit
                                if _auto_commit_side_effects(workspace_path, out_of_scope):
                                    # Fall through to REVIEW without a DEV retry
                                    pass
                                else:
                                    # Auto-commit failed — fall back to DEV retry
                                    escalate = _dirty_dev_retry(", ".join(out_of_scope))
                                    if escalate is not None:
                                        return escalate
                                    continue
                            else:
                                # Some tracked files are in-scope — existing DEV retry
                                escalate = _dirty_dev_retry(", ".join(parsed_files))
                                if escalate is not None:
                                    return escalate
                                continue
                        else:
                            # Untracked/ignored entries present, empty file_scope, or no
                            # parsed tracked files — treat all dirty as in-scope (existing)
                            raw_names = ", ".join(
                                line.strip().split(maxsplit=1)[-1] for line in dirty_lines
                            )
                            escalate = _dirty_dev_retry(raw_names)
                            if escalate is not None:
                                return escalate
                            continue
            elif gate_decision in ("FAIL", "BLOCKED"):
                if _dev_calls_this_cycle >= config.retry.max_dev_iterations:
                    state.phase = Phase.ESCALATE
                    state.error = (
                        f"Gate returned {gate_decision} after {state.dev_iteration} attempts"
                    )
                    _log(f"✗ ESCALATE   {state.error}")
                    if logger:
                        logger._safe_emit("phase_end", phase="VALIDATE", outcome="escalate")
                        logger._safe_emit("escalate", reason=state.error, phase="VALIDATE")
                    _escalate_notify(task, state, notify, config)
                    return CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    )
                # Retry dev — the gate failure details are in handoff.yaml
                handoff_text = _get_handoff_content(config, workspace_path)
                state.human_feedback = (
                    f"Gate output (last {config.validation.gate_output_tail_chars} chars):\n"
                    f"{gate_output_tail}\n\n"
                    f"Gate returned {gate_decision}. "
                    f"Fix the issues and re-run the gate.\n\n"
                    f"Current handoff:\n{handoff_text}"
                )
                state.retry_reason = "gate_fail"
                _log(f"  ✗ VALIDATE   {gate_decision}  (iter={state.dev_iteration} → retrying)")
                _log(f"Retrying dev (gate={gate_decision}, iter={state.dev_iteration})")
                if logger:
                    logger._safe_emit("phase_end", phase="VALIDATE", outcome="fail")
                continue
            else:
                _log(f"Unknown gate decision: {gate_decision!r}, treating as FAIL")
                state.phase = Phase.ESCALATE
                state.error = f"Unknown gate decision: {gate_decision!r}"
                _log(f"✗ ESCALATE   {state.error}")
                _escalate_notify(task, state, notify, config)
                return CoordinatorResult(
                    success=False,
                    phase=state.phase,
                    state=state,
                    message=state.error,
                )

        if logger:
            logger._safe_emit("phase_end", phase="VALIDATE", outcome="pass")
        _skip_dev = False  # all subsequent iterations start at DEV

        # ── REVIEW ────────────────────────────────────────────
        state.phase = Phase.REVIEW
        if logger:
            logger._safe_emit("phase_start", phase="REVIEW", iteration=state.review_cycle + 1)
        pool_size = len(config.review_pool)
        max_parse_retries = config.retry.max_review_parse_retries
        _review_pool_start = time.monotonic()
        _pool_model_names = "+".join(p.model for p in config.review_pool)
        _log_phase(state.phase, f"{_pool_model_names}  cycle={state.review_cycle + 1}")

        commit_log = _get_commit_log(workspace_path, config.workspace.base_branch)
        handoff_content = _get_handoff_content(config, workspace_path)
        dev_notes = _get_dev_notes(config, workspace_path)

        review_prompts: str | list[str] = (
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
                prompt=review_prompts,
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
                    _escalate_notify(task, state, notify, config)
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
            meta.failed_detail = {
                r.profile_name: f"exit={r.exit_code}: {r.output[:200].strip()}"
                if r.output
                else f"exit={r.exit_code}"
                for r in failed_results
            }

            if not successful:
                state.phase = Phase.ESCALATE
                failed_desc = ", ".join(
                    f"{r.profile_name} (exit={r.exit_code})" for r in failed_results
                )
                state.error = f"All {len(pool_results)} review agent(s) failed: {failed_desc}"
                _escalate_notify(task, state, notify, config)
                return CoordinatorResult(
                    success=False,
                    phase=state.phase,
                    state=state,
                    message=state.error,
                )

            # Determine the output to parse as the final review verdict.
            # Skip synthesis when: no synthesis profile, OR degraded (some reviewers failed
            # from a pool that had multiple reviewers).
            # This lets synthesis run for a 1-reviewer pool that has synthesis_profile
            # materialized by large-complexity adaptation.
            _is_degraded = len(failed_results) > 0 and pool_size > 1
            if config.synthesis_profile is None or _is_degraded:
                # No synthesis configured, or degraded to single successful reviewer
                if _is_degraded:
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
                synthesis_result = _dc_replace(synthesis_result, profile_name="synthesis")

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
                        _escalate_notify(task, state, notify, config)
                        return CoordinatorResult(
                            success=False,
                            phase=state.phase,
                            state=state,
                            message=state.error,
                        )

                if not synthesis_result.success:
                    state.phase = Phase.ESCALATE
                    state.error = f"Synthesis agent failed (exit={synthesis_result.exit_code})"
                    _escalate_notify(task, state, notify, config)
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
            if logger:
                logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
            _escalate_notify(task, state, notify, config)
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
        if logger:
            logger._safe_emit(
                "review_result",
                verdict=parsed_review.verdict,
                p1_count=_p1_count,
                p2_count=_p2_count,
                cost_usd=round(_review_cost, 6),
            )

        if parsed_review.verdict == "APPROVE":
            _log(
                f"  ✓ REVIEW   APPROVE  {_p1_count} P1  {_p2_count} P2"
                f"  ${_review_cost:.2f}  {_fmt_duration(_review_elapsed)}"
            )
            if interactive:
                state.phase = Phase.HUMAN_REVIEW
                _log_phase(state.phase)
                if _is_remote_mode(notify, config):
                    decision, feedback = _remote_human_review(
                        state, parsed_review, workspace_path, branch_name, task, config, task_start
                    )
                else:
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
                            auto_push=config.workspace.auto_push,
                            config=config,
                            task_name=task.name,
                        )
                        merge_suffix = (
                            " Merged."
                            if merge_info["merged"]
                            else f" Merge failed: {merge_info['error']}"
                        )
                    _task_elapsed = time.monotonic() - task_start
                    _log(f"✓ DONE   total=${state.total_cost:.2f}  {_fmt_duration(_task_elapsed)}")
                    _ntfy_done_notify(
                        task,
                        state,
                        config,
                        notify,
                        parsed_review.summary,
                        _task_elapsed,
                        branch_name,
                    )
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
                if decision in ("escalate", "timeout"):
                    state.phase = Phase.ESCALATE
                    state.error = (
                        "Remote review timed out — auto-escalated."
                        if decision == "timeout"
                        else "Human chose to escalate after APPROVE."
                    )
                    _log(f"✗ ESCALATE   {state.error}")
                    _escalate_notify(task, state, notify, config)
                    return CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    )
                if decision == "extend":
                    # Grant a completely fresh budget of max_review_cycles
                    state.dev_iteration = 0
                    state.review_cycle = 0
                    state.human_review_extra_cycles += 1
                    state.last_review_findings = (
                        review_to_dev_handoff(parsed_review) if parsed_review.findings else None
                    )
                    state.human_feedback = None
                    state.retry_reason = "extend"
                    _dev_calls_this_cycle = 0
                    _log(
                        f"Human extended — granting fresh budget "
                        f"(extra_cycles={state.human_review_extra_cycles})"
                    )
                    continue
                # decision == "reject" — loop back to dev with human feedback
                state.human_feedback = feedback
                state.last_review_findings = None
                state.retry_reason = "reject"
                state.dev_iteration = 0
                _dev_calls_this_cycle = 0
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
                        auto_push=config.workspace.auto_push,
                        config=config,
                        task_name=task.name,
                    )
                    merge_suffix = (
                        " Merged."
                        if merge_info["merged"]
                        else f" Merge failed: {merge_info['error']}"
                    )
                    if logger:
                        logger._safe_emit(
                            "merge_result",
                            success=merge_info["merged"],
                            branch=branch_name,
                            error=merge_info.get("error"),
                        )
                if logger:
                    logger._safe_emit(
                        "phase_end",
                        phase="REVIEW",
                        outcome="approve",
                        cost_usd=round(_review_cost, 6),
                        duration_s=round(_review_elapsed, 2),
                    )
                _task_elapsed = time.monotonic() - task_start
                _log(f"✓ DONE   total=${state.total_cost:.2f}  {_fmt_duration(_task_elapsed)}")
                _ntfy_done_notify(
                    task, state, config, notify, parsed_review.summary, _task_elapsed, branch_name
                )
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
        # Detect persistent P1 (smart config only)
        _is_persistent_p1 = False
        if config.smart_config_models is not None and len(state.review_results) >= 2:
            _prev_result = state.review_results[-2]
            _is_persistent_p1 = _has_persistent_p1(parsed_review.findings, _prev_result.findings)

        _persistent_tag = " (persistent)" if _is_persistent_p1 else ""
        _log(
            f"  ✗ REVIEW   REQUEST_CHANGES  {_p1_count} P1{_persistent_tag}"
            f"  ${_review_cost:.2f}  {_fmt_duration(_review_elapsed)}"
        )

        # Escalate dev model on persistent P1 (max once per run, only if budget remains)
        if (
            _is_persistent_p1
            and not state.dev_escalated
            and (state.total_dev_cost < config.dev_profile.budget_usd)
        ):
            _curr_key = _find_registry_key_for_profile(config.dev_profile)
            if _curr_key is not None:
                _next_key = _escalate_dev_model(_curr_key, config.smart_config_models)
                if _next_key is not None:
                    _next_info = MODEL_REGISTRY[_next_key]
                    _p1_file = next(
                        (f.file for f in parsed_review.findings if f.severity == "P1"),
                        "unknown",
                    )
                    _log(
                        f"  Dev escalation: {config.dev_profile.model} → {_next_info.model}"
                        f" (persistent P1 in {_p1_file})"
                    )
                    _new_dev = _dc_replace(
                        config.dev_profile, cli=_next_info.cli, model=_next_info.model
                    )
                    config = _dc_replace(config, dev_profile=_new_dev)
                    state.dev_escalated = True

        if state.review_cycle >= config.retry.max_review_cycles:
            if interactive:
                state.phase = Phase.HUMAN_REVIEW
                _log_phase(state.phase, "cycles exhausted")
                if _is_remote_mode(notify, config):
                    decision, feedback = _remote_human_review(
                        state, parsed_review, workspace_path, branch_name, task, config, task_start
                    )
                else:
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
                            auto_push=config.workspace.auto_push,
                            config=config,
                            task_name=task.name,
                        )
                        merge_suffix = (
                            " Merged."
                            if merge_info["merged"]
                            else f" Merge failed: {merge_info['error']}"
                        )
                    _task_elapsed = time.monotonic() - task_start
                    _log(f"✓ DONE   total=${state.total_cost:.2f}  {_fmt_duration(_task_elapsed)}")
                    _ntfy_done_notify(
                        task,
                        state,
                        config,
                        notify,
                        parsed_review.summary,
                        _task_elapsed,
                        branch_name,
                    )
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
                if decision in ("escalate", "timeout"):
                    state.phase = Phase.ESCALATE
                    state.error = (
                        "Remote review timed out — auto-escalated."
                        if decision == "timeout"
                        else "Human chose to escalate after exhausted cycles."
                    )
                    _log(f"✗ ESCALATE   {state.error}")
                    _escalate_notify(task, state, notify, config)
                    return CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    )
                if decision == "extend":
                    # Grant a completely fresh budget
                    state.dev_iteration = 0
                    state.review_cycle = 0
                    state.human_review_extra_cycles += 1
                    state.last_review_findings = review_to_dev_handoff(parsed_review)
                    state.human_feedback = None
                    state.retry_reason = "extend"
                    _dev_calls_this_cycle = 0
                    _log(
                        f"Human extended — granting fresh budget "
                        f"(extra_cycles={state.human_review_extra_cycles})"
                    )
                    continue
                # decision == "reject" — cycles exhausted: treat as extend + reject
                # Grants a fresh budget at human's explicit direction
                state.dev_iteration = 0
                state.review_cycle = 0
                state.human_review_extra_cycles += 1
                state.human_feedback = feedback
                state.last_review_findings = None
                state.retry_reason = "reject"
                _dev_calls_this_cycle = 0
                _log(
                    "Human rejected (cycles exhausted) — granting fresh budget "
                    f"(extra_cycles={state.human_review_extra_cycles})"
                )
                continue
            else:
                state.phase = Phase.ESCALATE
                state.error = (
                    f"Review requested changes after {state.review_cycle} cycles. "
                    f"Max cycles ({config.retry.max_review_cycles}) exhausted."
                )
                _log(f"✗ ESCALATE   {state.error}")
                if logger:
                    logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
                    logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
                _escalate_notify(task, state, notify, config)
                return CoordinatorResult(
                    success=False,
                    phase=state.phase,
                    state=state,
                    message=state.error,
                )

        # Feed findings back to dev agent
        if logger:
            logger._safe_emit(
                "phase_end",
                phase="REVIEW",
                outcome="request_changes",
                cost_usd=round(_review_cost, 6),
                duration_s=round(_review_elapsed, 2),
            )
        state.last_review_findings = review_to_dev_handoff(parsed_review)
        state.dev_iteration = 0
        state.human_feedback = None
        state.retry_reason = "review_changes"
        _dev_calls_this_cycle = 0
        _log_verbose(f"Sending {len(parsed_review.findings)} findings back to dev agent")


def run_task(
    config: ForgeConfig,
    task: TaskSpec,
    *,
    interactive: bool = False,
    auto_merge: bool = False,
    notify: bool = False,
    run_id: str | None = None,
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
    should_plan = config.plan.enabled and state.preflight_complexity in ("medium", "large")
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
        state.plan_result = plan_result

        if plan_result.success:
            plan_text = plan_result.output
            (workspace_path / "forge_plan.md").write_text(plan_text, encoding="utf-8")
            state.plan_output = plan_text
            _log(f"  ✓ PLAN   ${plan_result.cost_usd:.2f}  {_fmt_duration(_plan_elapsed)}")
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
    state = CoordinatorState(
        phase=Phase.REVIEW,
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

    # Resolve branch name from actual worktree HEAD (P1 fix: don't compute from pattern)
    _ok_branch, _branch_out = _cu._run_shell("git rev-parse --abbrev-ref HEAD", workspace_path)
    if _ok_branch and _branch_out.strip() and _branch_out.strip() != "HEAD":
        branch_name = _branch_out.strip()
    else:
        branch_name = config.workspace.branch_pattern.format(slug=task.slug)
    state.branch_name = branch_name

    spec_content = load_spec(task.spec_path)

    # ── REVIEW→DEV→VALIDATE→REVIEW loop ─────────────────────────
    # First iteration starts at REVIEW (skip DEV+VALIDATE for existing worktree).
    # Subsequent iterations run the full DEV→VALIDATE→REVIEW sequence.
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
    state = CoordinatorState(
        phase=Phase.DEV,
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

    # Resolve branch name from actual worktree HEAD (same as run_from_review)
    _ok_branch, _branch_out = _cu._run_shell("git rev-parse --abbrev-ref HEAD", workspace_path)
    if _ok_branch and _branch_out.strip() and _branch_out.strip() != "HEAD":
        branch_name = _branch_out.strip()
    else:
        branch_name = config.workspace.branch_pattern.format(slug=task.slug)
    state.branch_name = branch_name

    spec_content = load_spec(task.spec_path)

    # ── DEV→VALIDATE→REVIEW loop ─────────────────────────────────
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
    pool_size = len(config.review_pool)
    _pool_model_names_ro = "+".join(p.model for p in config.review_pool)
    _log_phase(state.phase, f"{_pool_model_names_ro}  cycle=1  (review-only)")

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
        failed_detail={
            r.profile_name: f"exit={r.exit_code}: {r.output[:200].strip()}"
            if r.output
            else f"exit={r.exit_code}"
            for r in failed_results
        },
    )
    state.review_cycle_metadata.append(meta)

    if not successful:
        state.phase = Phase.ESCALATE
        failed_desc = ", ".join(f"{r.profile_name} (exit={r.exit_code})" for r in failed_results)
        state.error = f"All {len(pool_results)} review agent(s) failed: {failed_desc}"
        _log(f"✗ ESCALATE   {state.error}")
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    # Synthesis if configured and not degraded (same logic as _coordinator_loop)
    _ro_is_degraded = len(failed_results) > 0 and pool_size > 1
    if config.synthesis_profile is None or _ro_is_degraded:
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
        synthesis_result = _dc_replace(synthesis_result, profile_name="synthesis")
        state.review_agent_results.append(synthesis_result)
        state.review_durations.append(_synth_elapsed)
        log_agent_result(synthesis_result, "SYNTHESIS")

        if not synthesis_result.success:
            state.phase = Phase.ESCALATE
            state.error = f"Synthesis agent failed (exit={synthesis_result.exit_code})"
            _escalate_notify(task, state, notify, config)
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
                "mode": state.human_review_mode or "interactive",
                "decision": state.human_review_decision,
                "feedback": state.human_review_feedback,
                "waited_seconds": (
                    round(state.human_review_waited_seconds, 1)
                    if state.human_review_waited_seconds is not None
                    else None
                ),
                "extra_cycles_granted": state.human_review_extra_cycles,
            }
            if state.human_review_decision is not None
            else None
        ),
        "merge": result.merge,
        "error": state.error,
    }
