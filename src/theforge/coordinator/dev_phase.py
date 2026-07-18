"""DEV phase handler: prompt routing, agent invocation, budget enforcement, zero-change guard."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Iterable
from dataclasses import replace as _dc_replace
from pathlib import Path

import yaml

from theforge.config import ForgeConfig, apply_model_info
from theforge.config.auth import sandbox_available_for_profile
from theforge.config.types import StuckDetectionConfig
from theforge.coordinator.context_scope import plan_file_list
from theforge.review import append_convention_retry_findings
from theforge.schemas import dev_handoff_claims_unproven_completion
from theforge.sessions import save_sessions
from theforge.task import ContextAssembler, TaskStory, build_dev_prompt, build_fix_prompt
from theforge.traces import write_trace

from .commit_guard import (
    _checkpoint_commit,
    _commits_exist_strict,
    _has_commits_ahead_of_base,
    _worktree_has_changes,
)
from .gate import _is_gate_skip
from .logging import StructuredLogger
from .notify import _escalate_notify
from .preflight import _escalate_dev_model, _find_registry_key_for_profile
from .state import CoordinatorResult, CoordinatorState, DevIterationTelemetry, Phase, RetryReason
from .util import (
    _fmt_duration,
    _log,
    _log_phase,
    _log_verbose,
    resolve_timeout_with_active,
)

# ── Lazy runner slot ──────────────────────────────────────────────────
# None until first call; tests may replace before calling run_task.
# Patch targets:
#   theforge.coordinator.dev_phase.run_agent        — dev agent call
#   theforge.coordinator.dev_phase.log_agent_result — dev result logging
run_agent = None
log_agent_result = None

_RUNNER_FAILURE_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "runner_argument_error",
        (
            "error: unexpected argument",
            "unexpected argument",
            "unrecognized option",
            "unknown option",
            "invalid option",
        ),
    ),
    (
        "runner_command_not_found",
        (
            "command not found",
            "no such file or directory",
        ),
    ),
    (
        "runner_permission_denied",
        ("permission denied",),
    ),
)
_SHELL_ERROR_PREFIXES = ("bash:", "sh:", "zsh:")
_DEV_TRANSPORT_RETRY_BACKOFF_BASE_SECONDS = 2
_TRANSIENT_DEV_ERROR_PATTERNS = (
    "rate limit",
    "rate-limited",
    "resource_exhausted",
    "resource exhausted",
    "quota exceeded",
    "quota_exceeded",
    "internal error",
    "server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "stream idle timeout",
    "partial response received",
    "mid-stream disconnect",
    "stream disconnected",
    "connection reset",
    "connection-reset",
    "econnreset",
    "connection aborted",
    "peer closed connection",
    "temporarily unavailable",
    "try again later",
    "timeout awaiting headers",
)
# HTTP status codes that indicate a transient provider failure. Matched with
# digit-boundary anchoring so they don't fire on substrings of unrelated
# numbers (port numbers, token counts, etc.) that happen to contain these
# digits, e.g. "5003" or "1500". "connection refused" is deliberately not
# treated as transient: it signals a misconfigured or unreachable endpoint,
# not transient load, so retrying is unlikely to succeed.
_TRANSIENT_DEV_ERROR_STATUS_CODES = ("429", "500", "502", "503", "504")
_TRANSIENT_DEV_ERROR_STATUS_CODE_RE = re.compile(
    r"(?<!\d)(?:" + "|".join(_TRANSIENT_DEV_ERROR_STATUS_CODES) + r")(?!\d)"
)


def _scale_stuck_for_complexity(
    cfg: StuckDetectionConfig,
    complexity: str | None,
    plan_file_count: int,
) -> StuckDetectionConfig:
    """Return a StuckDetectionConfig with thresholds scaled by complexity and plan size.

    LARGE/medium stories legitimately need more pre-modification exploration; flat
    thresholds false-terminate competent dev agents. Scaling raises (never lowers)
    no_progress_iterations and post_nudge_iterations:
      - no_progress_iterations: base × multiplier(complexity), plus plan_file_count
        so a plan touching many files gives the agent room to read each one.
      - post_nudge_iterations: base × multiplier(complexity), giving complex stories
        a meaningful grace window after the nudge.
    """
    np_mult = cfg.no_progress_multipliers.get(complexity or "", 1.0) if complexity else 1.0
    pn_mult = cfg.post_nudge_multipliers.get(complexity or "", 1.0) if complexity else 1.0
    scaled_no_progress = max(
        cfg.no_progress_iterations,
        round(cfg.no_progress_iterations * np_mult) + max(plan_file_count, 0),
    )
    scaled_post_nudge = max(
        cfg.post_nudge_iterations,
        round(cfg.post_nudge_iterations * pn_mult),
    )
    return _dc_replace(
        cfg,
        no_progress_iterations=scaled_no_progress,
        post_nudge_iterations=scaled_post_nudge,
    )


def _plan_files_for_stuck_scaling(
    state: CoordinatorState,
    logger: StructuredLogger | None,
) -> list[str]:
    """Return the plan's target files for stuck-detection scaling.

    A plan must populate ``state.plan_structured`` before DEV entry (non-resume:
    plan_flow after the PLAN agent; resume: run_setup.load_plan_state from the
    worktree's .forge/plan.md). When it is ``None`` here, the plan structure
    never reached the dev phase, so the +N plan-scope exploration bonus silently
    collapses to zero (issue #1135). Surface that as a structured warning naming
    the missing field rather than letting a bare 0 flow into policy — a genuinely
    file-less plan yields a non-None structure with an empty file list, which is
    distinct from this degraded case and does not warn.
    """
    if state.plan_structured is None:
        _log_verbose(
            "  ⚠ DEV   plan_structured missing from state — stuck-detection scaling "
            "proceeds with 0 plan files (no plan-scope exploration bonus)"
        )
        if logger:
            logger._safe_emit(
                "plan_structured_missing",
                phase="DEV",
                iteration=state.dev_iteration,
                field="plan_structured",
                consumer="stuck_detection_scaling",
            )
        return []
    return plan_file_list(state.plan_structured)


def _summarize_runner_failure(output: str, indicators: tuple[str, ...]) -> str:
    """Return a short, operator-friendly summary line for a runner crash."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    lowered = tuple(line.lower() for line in lines)
    for indicator in indicators:
        for idx, line in enumerate(lowered):
            if indicator in line:
                return lines[idx][:200]
    for idx, line in enumerate(lowered):
        if not line.startswith("usage:"):
            return lines[idx][:200]
    return lines[0][:200] if lines else "(no output)"


def _runner_failure_evidence(output: str, exit_code: int) -> list[str]:
    """Return candidate shell-level crash lines from runner output."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return []
    candidates = lines[:3]
    if len(lines) > 3:
        candidates.extend(lines[-2:])
    lowered = [line.lower() for line in candidates]
    if exit_code == 127:
        return [
            line
            for line, lowered_line in zip(candidates, lowered, strict=False)
            if lowered_line.startswith(_SHELL_ERROR_PREFIXES)
            and "command not found" in lowered_line
        ]
    if exit_code == 126:
        return [
            line
            for line, lowered_line in zip(candidates, lowered, strict=False)
            if lowered_line.startswith(_SHELL_ERROR_PREFIXES)
            and "permission denied" in lowered_line
        ]
    return candidates


def classify_runner_subprocess_failure(output: str, exit_code: int) -> tuple[str, str] | None:
    """Classify a runner subprocess crash that occurred before agent execution."""
    evidence_lines = _runner_failure_evidence(output, exit_code)
    evidence_text = "\n".join(evidence_lines)
    lowered = evidence_text.lower()
    for failure_code, indicators in _RUNNER_FAILURE_SIGNATURES:
        if failure_code == "runner_command_not_found" and exit_code != 127:
            continue
        if failure_code == "runner_permission_denied" and exit_code != 126:
            continue
        if failure_code in {"runner_command_not_found", "runner_permission_denied"}:
            if not evidence_lines:
                continue
        if any(indicator in lowered for indicator in indicators):
            return failure_code, _summarize_runner_failure(evidence_text, indicators)
    return None


def _runner_display_name(config: ForgeConfig) -> str:
    """Return a stable operator-facing runner label for escalation messages."""
    return config.dev_profile.cli or config.dev_profile.provider or config.dev_profile.name


def _is_transient_dev_failure(
    result: object, runner_failure: tuple[str, str] | None = None
) -> bool:
    """Return True when a failed dev invocation looks transient and retryable."""
    from theforge.agent_types import AgentResult as _AgentResult  # noqa: PLC0415

    if not isinstance(result, _AgentResult):
        raise TypeError(f"Expected AgentResult, got {type(result)}")
    if result.success or result.startup_failure or runner_failure is not None:
        return False
    failure_code = (result.failure_code or "").lower()
    if failure_code in {"rate_limit", "provider_internal_error", "connection_reset"}:
        return True
    output = (result.output or "").lower()
    if _TRANSIENT_DEV_ERROR_STATUS_CODE_RE.search(output):
        return True
    return any(pattern in output for pattern in _TRANSIENT_DEV_ERROR_PATTERNS)


def _summarize_dev_transport_failure(result: object) -> str:
    """Produce a compact summary for audit/logging when dev transport fails."""
    from theforge.agent_types import AgentResult as _AgentResult  # noqa: PLC0415

    if not isinstance(result, _AgentResult):
        raise TypeError(f"Expected AgentResult, got {type(result)}")
    parts = [f"exit={result.exit_code}"]
    if result.failure_code:
        parts.append(f"failure_code={result.failure_code}")
    output = " ".join((result.output or "").split())
    if output:
        parts.append(output[:200])
    return ": ".join((parts[0], " | ".join(parts[1:]))) if len(parts) > 1 else parts[0]


def _dev_transport_retry_backoff_seconds(retry_count: int) -> int:
    """Return the backoff delay before the next transient dev retry."""
    return _DEV_TRANSPORT_RETRY_BACKOFF_BASE_SECONDS * (2 ** max(retry_count - 1, 0))


def _extract_failed_tests(gate_output_tail: str) -> list[str]:
    """Best-effort extraction of failing test identifiers from gate output."""
    import re

    _xdist_prefix = re.compile(r"^\[gw\d+\]\s+")

    failed: list[str] = []
    for raw_line in gate_output_tail.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip pytest-xdist worker prefix, e.g. "[gw7] FAILED tests/..."
        line = _xdist_prefix.sub("", line)
        if line.startswith(("FAILED ", "ERROR ")):
            candidate = line.split()[1].rstrip(":")
            if candidate not in failed:
                failed.append(candidate)
        elif "::" in line and any(token in line.lower() for token in ("failed", "error")):
            candidate = line.split()[0].rstrip(":")
            if candidate not in failed:
                failed.append(candidate)
    return failed


def _git_lines(workspace_path: Path, args: Iterable[str]) -> list[str]:
    from . import util as _cu

    cmd = "git " + " ".join(str(arg) for arg in args)
    ok, output = _cu._run_shell(cmd, workspace_path)
    if not ok and not output:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _retry_review_findings_for_dev_prompt(state: CoordinatorState) -> str | None:
    """Return current actionable findings for a validate-driven retry prompt."""
    return append_convention_retry_findings(
        state.last_review_findings,
        state.convention_violations,
    )


def record_dev_iteration_telemetry(
    state: CoordinatorState,
    workspace_path: Path,
    *,
    max_iterations: int,
    gate_result: str | None,
    gate_output_tail: str = "",
    is_timeout: bool = False,
    runner_failure_summary: str | None = None,
) -> None:
    """Capture per-iteration dev telemetry after validation completes."""
    if not state.dev_results or not state.dev_durations:
        return
    iteration = state.dev_iteration
    attempt_count = state.pending_dev_transport_retry_count + 1
    dev_attempts = state.dev_results[-attempt_count:]
    duration_attempts = state.dev_durations[-attempt_count:]
    dev_result = dev_attempts[-1]
    duration_s = sum(duration_attempts)
    cost_usd = (
        sum(result.cost_usd or 0.0 for result in dev_attempts)
        if any(result.cost_usd is not None for result in dev_attempts)
        else None
    )
    baseline = state.last_dev_start_commit or "HEAD"
    files_changed = _git_lines(workspace_path, ["diff", "--name-only", baseline, "HEAD"])
    dirty_files = [
        line.split(maxsplit=1)[-1]
        for line in _git_lines(workspace_path, ["status", "--porcelain"])
    ]
    for dirty in dirty_files:
        if dirty not in files_changed:
            files_changed.append(dirty)

    failed_tests = _extract_failed_tests(gate_output_tail)
    prev_failed = (
        state.dev_iteration_telemetry[-1].failed_tests if state.dev_iteration_telemetry else []
    )
    tests_fixed_count = len(set(prev_failed) - set(failed_tests)) if prev_failed else 0
    meaningful_progress = bool(files_changed or tests_fixed_count > 0)
    state.dev_iteration_telemetry.append(
        DevIterationTelemetry(
            iteration=iteration,
            max_iterations=max_iterations,
            cost_usd=cost_usd,
            duration_s=duration_s,
            cycle=state.review_cycle,
            gate_result=gate_result,
            failed_tests=failed_tests,
            existing_test_failures=False,
            is_timeout=is_timeout,
            files_changed=files_changed,
            files_changed_count=len(files_changed),
            tests_fixed_count=tests_fixed_count,
            meaningful_progress=meaningful_progress,
            sandboxed=state.sandboxed,
            agent_exit_code=dev_result.exit_code,
            runner_failure_code=dev_result.failure_code,
            runner_failure_summary=runner_failure_summary,
            cli_quota_error_observed=dev_result.cli_quota_error_observed,
            transport_fallback_fired=dev_result.transport_fallback_fired,
            transport_fallback_reason=dev_result.transport_fallback_reason,
            transport_used=dev_result.transport_used,
            model_used=dev_result.model_used,
            transport_retry_count=state.pending_dev_transport_retry_count,
            transport_retry_events=list(state.pending_dev_transport_retry_events),
        )
    )
    state.pending_dev_transport_retry_count = 0
    state.pending_dev_transport_retry_events = []


def _still_open_p1s_for_dev_prompt(state: CoordinatorState) -> list:
    """Return still-open P1 findings for carry-forward prompt context."""
    open_dispositions = {
        "unresolved",
        "net_new",
        "corroborated_new",
        "regression",
        "ac_blocking",
    }
    return [
        record
        for record in state.finding_registry
        if record.severity == "P1" and record.disposition in open_dispositions
    ]


def _prior_open_p1s_for_dev_prompt(state: CoordinatorState) -> list:
    """Return still-open P1s that predate the most recent review cycle."""
    if state.review_cycle <= 1:
        return []
    return [
        record
        for record in _still_open_p1s_for_dev_prompt(state)
        if record.cycle_first_seen < state.review_cycle
    ]


def _current_cycle_p1s_for_dev_prompt(state: CoordinatorState) -> list:
    """Return classified P1s from the most recent review cycle."""
    if state.review_cycle <= 0:
        return []
    return [
        record
        for record in state.finding_registry
        if record.severity == "P1" and record.cycle_last_seen == state.review_cycle
    ]


def _ensure_runners() -> None:
    global run_agent, log_agent_result
    if run_agent is not None and log_agent_result is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if log_agent_result is None:
        log_agent_result = _r.log_agent_result


def _capture_dev_handoff(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    dev_result: object,
) -> Path | None:
    """Capture the handoff snapshot from a completed dev agent result.

    Writes the forge artifact when structured output is present; falls back to
    reading the workspace handoff file. Appends to state.dev_handoff_snapshots.
    Returns the forge artifact path when written, else None.
    """
    from theforge.agent_types import AgentResult as _AgentResult  # noqa: PLC0415

    if not isinstance(dev_result, _AgentResult):
        raise TypeError(f"Expected AgentResult, got {type(dev_result)}")
    if dev_result.dev_handoff is not None:
        _forge_handoff_dir = config.project_root / ".forge" / "handoffs" / task.slug
        _forge_handoff_dir.mkdir(parents=True, exist_ok=True)
        _forge_artifact_path = _forge_handoff_dir / f"iter_{state.dev_iteration}.yaml"
        try:
            _forge_artifact_path.write_text(
                yaml.dump(dev_result.dev_handoff, allow_unicode=True, default_flow_style=False),
                encoding="utf-8",
            )
            _log(f"  Handoff artifact: {_forge_artifact_path}")
        except Exception as _write_exc:  # noqa: BLE001
            _log(f"  ⚠ Failed to write handoff artifact: {_write_exc}")
        state.dev_handoff_snapshots.append(
            {
                "source": "structured_output",
                "path": str(_forge_artifact_path),
                "handoff": dev_result.dev_handoff,
            }
        )
        return _forge_artifact_path
    else:
        state.dev_handoff_snapshots.append({"source": "missing", "path": None, "handoff": None})
        return None


def _run_dev_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    story_content: str,
    workspace_path: Path,
    branch_name: str,
    *,
    notify: bool,
    logger: StructuredLogger | None,
    stop_event: "threading.Event | None" = None,
) -> CoordinatorResult | None:
    """Run one DEV iteration. Returns CoordinatorResult on budget escalation, else None.

    Caller must increment state.dev_iteration and _dev_calls_this_cycle before calling.
    Mutates state in-place (appends dev_results, updates dev_session_id, etc.).
    """
    _ensure_runners()
    state.pending_dev_transport_retry_count = 0
    state.pending_dev_transport_retry_events = []
    # Probe sandbox availability once per run (lru_cache-backed — cheap on repeat calls).
    state.sandboxed = sandbox_available_for_profile(config.dev_profile)
    if config.dev_profile.mode == "cli" and config.dev_profile.sandbox_mode == "none":
        _log(
            "  WARNING: sandbox_mode: none — dev agent runs without write containment. "
            "Use for debugging only."
        )
    _preserve_error_type = state.error_type == "max_iterations_no_submit"
    if not _preserve_error_type:
        state.error_type = None
    _log_phase(
        state.phase,
        f"{config.dev_profile.model}  iter={state.dev_iteration}",
    )
    if logger:
        logger._safe_emit("phase_start", phase="DEV", iteration=state.dev_iteration)

    # ── Workspace hygiene gate (first DEV entry only) ─────────────────
    # Reject or sanitise the worktree before the dev agent sees it for the
    # first time. Stray untracked files at repo root (left behind by tracked-
    # leftover files in main, or by a non-DEV phase that wrote where it
    # shouldn't) silently sabotage dev runs — see issue #1179. Quarantine
    # non-destructively into .forge/quarantine/<run-id>/iter-<n>/ so
    # operators can recover originals.
    #
    # Iterations 2+ are not re-gated: validate-phase's auto-commit owns
    # cleanup after the first iteration, and intermediate dirty state on a
    # retry is a legitimate handoff between iterations rather than a stray
    # phase mutation.
    from .workspace_hygiene import enforce_pre_dev_hygiene, ensure_scratch_dir  # noqa: PLC0415

    _hygiene_run_id = state.run_id or "unknown"
    ensure_scratch_dir(workspace_path, _hygiene_run_id)
    if state.dev_iteration <= 1:
        _hygiene_ok, _hygiene_diag, _hygiene_audit = enforce_pre_dev_hygiene(
            workspace_path,
            _hygiene_run_id,
            iteration=state.dev_iteration,
        )
        state.workspace_hygiene_audit.append({"phase": "PRE_DEV", **_hygiene_audit})
        if _hygiene_audit.get("quarantined"):
            _q_paths = ", ".join(_hygiene_audit["quarantined"])
            _q_dir = _hygiene_audit.get("quarantine_dir")
            _log(f"  ⚠ DEV   quarantined stray paths to {_q_dir}: {_q_paths}")
        if not _hygiene_ok:
            state.phase = Phase.ESCALATE
            state.error = _hygiene_diag or "Workspace hygiene gate refused DEV entry"
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("phase_end", phase="DEV", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="DEV")
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            )

    # Capture HEAD before the dev agent runs — used by finding_classifier for git diff.
    # Best-effort: any failure is silently ignored (non-critical for correctness).
    try:
        _head_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace_path),
            capture_output=True,
            timeout=10,
        )
        if _head_proc.returncode == 0:
            state.last_dev_start_commit = _head_proc.stdout.decode().strip()
    except Exception:  # noqa: BLE001  # best-effort, any error is harmless
        pass

    _gate_cmd = (
        task.gate_override
        if task.gate_override is not None and not _is_gate_skip(task.gate_override)
        else config.validation.gate_command
    )
    _test_cmd = config.validation.test_command or _gate_cmd
    _dev_entry_reason = state.retry_reason  # snapshot before consumed by prompt routing
    match state.retry_reason:
        case RetryReason.TIMEOUT_RESUME:
            prompt = (
                state.human_feedback
                or "You were cut off by a timeout. Continue from where you left off."
            )
            state.dev_prompt_injected_finding_ids.append([])
            state.retry_reason = None
            state.human_feedback = None
        case RetryReason.P2_CLEANUP:
            prompt = build_fix_prompt(
                task,
                workspace_path=workspace_path,
                branch_name=branch_name,
                allowed_tools=config.dev_profile.allowed_tools,
                review_findings=state.last_review_findings or "No specific findings provided.",
                gate_command=_gate_cmd,
                test_command=_test_cmd,
                gate_skipped=_is_gate_skip(task.gate_override),
                iteration=state.dev_iteration,
                cycle_history=state.cycle_history or None,
                escalation_note=state.escalation_note,
                plan_output=state.plan_structured
                if state.plan_structured is not None
                else state.plan_output,
                prior_open_p1s=None,
                classified_p1s=None,
                surviving_families=None,
                conventions=config.conventions_soft,
                advisory_p2_only=True,
            )
            state.dev_prompt_injected_finding_ids.append([])
            state.escalation_note = None
        case RetryReason.REVIEW_CHANGES | RetryReason.EXTEND if state.last_review_findings:
            carry_forward_p1s = _prior_open_p1s_for_dev_prompt(state)
            current_cycle_p1s = _current_cycle_p1s_for_dev_prompt(state)
            prompt = build_fix_prompt(
                task,
                workspace_path=workspace_path,
                branch_name=branch_name,
                allowed_tools=config.dev_profile.allowed_tools,
                review_findings=state.last_review_findings,
                gate_command=_gate_cmd,
                test_command=_test_cmd,
                gate_skipped=_is_gate_skip(task.gate_override),
                iteration=state.dev_iteration,
                cycle_history=state.cycle_history or None,
                escalation_note=state.escalation_note,
                plan_output=state.plan_structured
                if state.plan_structured is not None
                else state.plan_output,
                prior_open_p1s=carry_forward_p1s or None,
                classified_p1s=current_cycle_p1s or None,
                surviving_families=state.surviving_families or None,
                conventions=config.conventions_soft,
            )
            injected_finding_ids = [r.finding_id for r in carry_forward_p1s]
            injected_finding_ids.extend(
                r.finding_id for r in current_cycle_p1s if r.finding_id not in injected_finding_ids
            )
            state.dev_prompt_injected_finding_ids.append(injected_finding_ids)
            state.escalation_note = None  # consumed
        case RetryReason.MAX_ITERATIONS_NO_SUBMIT:
            dev_context = ContextAssembler.from_config(config).assemble(
                phase="dev",
                story_text=story_content,
                file_list=plan_file_list(state.plan_structured) or None,
            )
            state.context_manifests.append({"phase": "dev", "manifest": dev_context})
            prompt = build_dev_prompt(
                task,
                workspace_path=workspace_path,
                branch_name=branch_name,
                allowed_tools=config.dev_profile.allowed_tools,
                story_content=story_content,
                gate_command=_gate_cmd,
                test_command=_test_cmd,
                gate_skipped=_is_gate_skip(task.gate_override),
                review_findings=_retry_review_findings_for_dev_prompt(state),
                human_feedback=state.human_feedback,
                preflight_output=(
                    state.preflight_result.output if state.preflight_result else None
                ),
                plan_output=state.plan_structured
                if state.plan_structured is not None
                else state.plan_output,
                plan_review_advisory=state.plan_agent_review_findings,
                iteration=state.dev_iteration,
                escalation_note=state.escalation_note,
                cycle_history=state.cycle_history or None,
                preflight_sufficiency=state.preflight_sufficiency,
                contract_change=state.preflight_contract_change,
                conventions=config.conventions_soft,
                assembled_context=dev_context,
            )
            state.dev_prompt_injected_finding_ids.append([])
            state.escalation_note = None
        case (
            None
            | RetryReason.GATE_FAIL
            | RetryReason.CONVENTION_VIOLATIONS
            | RetryReason.DIRTY_WORKTREE
            | RetryReason.REJECT
            | RetryReason.EXTEND
            | RetryReason.REVIEW_CHANGES
        ):
            # None → first iteration; gate_fail/convention_violations/dirty_worktree/reject
            # /extend(no findings) → fresh dev prompt
            dev_context = ContextAssembler.from_config(config).assemble(
                phase="dev",
                story_text=story_content,
                file_list=plan_file_list(state.plan_structured) or None,
            )
            state.context_manifests.append({"phase": "dev", "manifest": dev_context})
            prompt = build_dev_prompt(
                task,
                workspace_path=workspace_path,
                branch_name=branch_name,
                allowed_tools=config.dev_profile.allowed_tools,
                story_content=story_content,
                gate_command=_gate_cmd,
                test_command=_test_cmd,
                gate_skipped=_is_gate_skip(task.gate_override),
                review_findings=_retry_review_findings_for_dev_prompt(state),
                human_feedback=state.human_feedback,
                preflight_output=(
                    state.preflight_result.output if state.preflight_result else None
                ),
                plan_output=state.plan_structured
                if state.plan_structured is not None
                else state.plan_output,
                plan_review_advisory=state.plan_agent_review_findings,
                iteration=state.dev_iteration,
                escalation_note=state.escalation_note,
                cycle_history=state.cycle_history or None,
                preflight_sufficiency=state.preflight_sufficiency,
                contract_change=state.preflight_contract_change,
                conventions=config.conventions_soft,
                assembled_context=dev_context,
            )
            state.dev_prompt_injected_finding_ids.append([])
            state.escalation_note = None  # consumed
        case _:
            raise ValueError(f"Unrecognized retry_reason: {state.retry_reason!r}")
    state.retry_reason = None  # consumed

    write_trace(
        workspace_path / ".forge/traces" / f"{state.dev_trace_count}-dev-prompt.txt",
        prompt,
    )

    _resolved_timeout, _dev_override_active = resolve_timeout_with_active(
        config.dev_profile.timeout_seconds,
        config.dev_profile.timeout_medium_seconds,
        config.dev_profile.timeout_large_seconds,
        state.preflight_complexity,
        state.preflight_complexity_score,
    )
    _dev_timeout = state.adaptive_dev_timeout_seconds or _resolved_timeout
    if _dev_override_active:
        _log(f"  Dev timeout: {_dev_timeout}s ({state.preflight_complexity} complexity)")
    else:
        _log(f"  Dev timeout: {_dev_timeout}s")
    _plan_files = _plan_files_for_stuck_scaling(state, logger)
    _scaled_stuck = _scale_stuck_for_complexity(
        config.stuck_detection,
        state.preflight_complexity,
        len(_plan_files),
    )
    if (
        _scaled_stuck.no_progress_iterations != config.stuck_detection.no_progress_iterations
        or _scaled_stuck.post_nudge_iterations != config.stuck_detection.post_nudge_iterations
    ):
        _log_verbose(
            f"  Stuck-detection scaled for {state.preflight_complexity} "
            f"({len(_plan_files)} plan files): "
            f"no_progress={_scaled_stuck.no_progress_iterations} "
            f"(base {config.stuck_detection.no_progress_iterations}), "
            f"post_nudge={_scaled_stuck.post_nudge_iterations} "
            f"(base {config.stuck_detection.post_nudge_iterations})"
        )
    _dev_profile = _dc_replace(
        config.dev_profile,
        timeout_seconds=_dev_timeout,
        max_iterations=state.adaptive_dev_max or config.dev_profile.max_iterations,
        stuck_detection=_scaled_stuck,
    )

    _dev_total_start = time.monotonic()
    _dev_results_this_iteration = []
    _dev_durations_this_iteration = []
    _runner_failure = None
    _current_session_id = state.dev_session_id
    _dev_retry_events: list[dict] = []
    _max_transport_retries = max(0, config.retry.max_dev_transport_retries)

    while True:
        _attempt_start = time.monotonic()
        dev_result = run_agent(
            prompt=prompt,
            profile=_dev_profile,
            working_dir=workspace_path,
            session_id=_current_session_id,
            secrets=config.secrets,
            stop_event=stop_event,
        )
        _attempt_elapsed = time.monotonic() - _attempt_start
        _runner_failure = None
        if not dev_result.success and not dev_result.startup_failure:
            _runner_failure = classify_runner_subprocess_failure(
                dev_result.output, dev_result.exit_code
            )
            if _runner_failure is not None:
                dev_result = _dc_replace(dev_result, failure_code=_runner_failure[0])

        if len(_dev_retry_events) < _max_transport_retries and _is_transient_dev_failure(
            dev_result, _runner_failure
        ):
            retry_count = len(_dev_retry_events) + 1
            _failure_summary = _summarize_dev_transport_failure(dev_result)
            _dev_results_this_iteration.append(dev_result)
            _dev_durations_this_iteration.append(_attempt_elapsed)
            _dev_retry_events.append(
                {
                    "iteration": state.dev_iteration,
                    "retry": retry_count,
                    "error": _failure_summary,
                }
            )
            _log(
                f"  ↻ DEV   transient transport failure "
                f"(retry {retry_count}/{_max_transport_retries})"
            )
            if state.log_dir is not None:
                write_trace(
                    state.log_dir
                    / (
                        f"dev-iter-{state.dev_iteration}-{config.dev_profile.name}"
                        f"-retry{retry_count}.log"
                    ),
                    dev_result.output or "",
                )
            _current_session_id = dev_result.session_id if _dev_profile.mode == "cli" else None
            _backoff_s = _dev_transport_retry_backoff_seconds(retry_count)
            _log_verbose(f"  DEV retry backoff: {_backoff_s}s")
            time.sleep(_backoff_s)
            continue

        _dev_results_this_iteration.append(dev_result)
        _dev_durations_this_iteration.append(_attempt_elapsed)
        _dev_elapsed = time.monotonic() - _dev_total_start
        break

    state.pending_dev_transport_retry_count = len(_dev_retry_events)
    state.pending_dev_transport_retry_events = list(_dev_retry_events)
    write_trace(
        workspace_path / ".forge/traces" / f"{state.dev_trace_count}-dev-output.txt",
        dev_result.output,
    )
    # Write dev iteration log to durable story log dir
    if state.log_dir is not None:
        write_trace(
            state.log_dir / f"dev-iter-{state.dev_iteration}-{config.dev_profile.name}.log",
            dev_result.output or "",
        )
    state.dev_results.extend(_dev_results_this_iteration)
    state.dev_durations.extend(_dev_durations_this_iteration)
    _capture_dev_handoff(state, config, task, workspace_path, dev_result)
    if _dev_profile.mode == "cli" and dev_result.transport_used == "api":
        state.dev_session_id = None
    else:
        state.dev_session_id = dev_result.session_id or state.dev_session_id
    save_sessions(workspace_path, state.dev_session_id, state.reviewer_session_ids)
    log_agent_result(dev_result, "DEV")
    _dev_cost_total = sum(result.cost_usd or 0.0 for result in _dev_results_this_iteration)
    _dev_cost_str = (
        "${:.2f}".format(_dev_cost_total)
        if any(result.cost_usd is not None for result in _dev_results_this_iteration)
        else "unknown"
    )
    # Glyph reflects the actual outcome: a killed or crashed iteration must not
    # be rendered with the success glyph one line above the failure it caused.
    _dev_glyph = "✓" if dev_result.success else "✗"
    _log(f"  {_dev_glyph} DEV   {_dev_cost_str}  {_fmt_duration(_dev_elapsed)}")
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="DEV",
            outcome="success" if dev_result.success else "failure",
            cost_usd=_dev_cost_total if _dev_cost_str != "unknown" else None,
            duration_s=round(_dev_elapsed, 2),
        )

    # ── Unproven-completion guard (successful dev only) ──────────────
    # Fail closed at the dev seam: a dev that exits successfully but hands off a
    # completion claim (an acceptance criterion marked MET) without gate PASS
    # evidence has reported done without proving the gate ran. Escalate rather
    # than accept an unverified completion and waste a downstream gate run
    # rediscovering the failure. This is the coordinator catching what the dev
    # should have declared as a blocking failure (gate_result: BLOCKED).
    if dev_result.success and dev_handoff_claims_unproven_completion(dev_result.dev_handoff or {}):
        state.phase = Phase.ESCALATE
        state.error = (
            "Dev handoff claims completion (acceptance criteria MET) without gate PASS "
            "evidence — the gate was not proven to pass; refusing to accept an "
            "unverified completion"
        )
        record_dev_iteration_telemetry(
            state,
            workspace_path,
            max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
            gate_result="HANDOFF_NO_GATE_EVIDENCE",
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
        if _runner_failure is not None:
            runner_name = _runner_display_name(config)
            state.phase = Phase.ESCALATE
            state.error_type = dev_result.failure_code
            state.error = (
                f"Runner crashed before agent execution: {runner_name}: {_runner_failure[1]}"
            )
            record_dev_iteration_telemetry(
                state,
                workspace_path,
                max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
                gate_result="RUNNER_CRASH",
                runner_failure_summary=_runner_failure[1],
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
        if dev_result.failure_code == "max_iterations_reached" and dev_result.dev_handoff is None:
            state.error_type = "max_iterations_no_submit"
            state.retry_reason = RetryReason.MAX_ITERATIONS_NO_SUBMIT
            if not state.dev_escalated:
                _old_model = config.dev_profile.model
                if config.retry.auto_model_escalation and config.models is not None:
                    # Registry passed as-is: an explicitly empty {} stays empty
                    # instead of collapsing to the built-in default (`... or None`).
                    _registry = config.model_registry
                    _curr_key = _find_registry_key_for_profile(
                        config.dev_profile, registry=_registry
                    )
                    if _curr_key is not None:
                        _next_key = _escalate_dev_model(
                            _curr_key, config.models, registry=_registry
                        )
                        if _next_key is not None:
                            from theforge.config.models import (  # noqa: PLC0415
                                _resolve_model_info,
                            )

                            _next_info = _resolve_model_info(_next_key, registry=_registry)
                            _new_dev = apply_model_info(config.dev_profile, _next_info)
                            config.dev_profile = _new_dev
                            state.dev_escalated = True
                            state.escalation_note = (
                                "MODEL ESCALATION: The previous dev iteration exhausted "
                                "its iteration budget without calling submit. "
                                f"Previous model: {_old_model}. "
                                f"Escalated model: {_next_info.model}."
                            )
                if not state.dev_escalated:
                    state.escalation_note = (
                        "RETRY ADAPTATION: The previous dev iteration exhausted its "
                        "iteration budget without calling submit. "
                        f"Previous model: {_old_model}. The retry uses explicit submit "
                        "pressure and narrower scope instead of repeating unchanged "
                        "conditions."
                    )
            state.human_feedback = (
                "The previous dev iteration exhausted its iteration budget without calling the "
                "submit tool, so there is no structured handoff to continue from. Do not repeat "
                "the same exploratory loop. Narrow scope, stabilize the worktree, and submit a "
                "structured result promptly."
            )
            # Preserve any partial edits before retrying (#1746). Like the
            # timeout resume below, this is another retry-with-possibly-dirty-
            # worktree case: the agent burned its internal iteration budget
            # without committing, so whatever it produced is uncommitted
            # working-tree state the next attempt would otherwise branch from as
            # if empty. Checkpoint it so the retry continues from committed work.
            if _worktree_has_changes(workspace_path):
                _checkpointed = _checkpoint_commit(
                    workspace_path, "max_iterations_reached without submit"
                )
                if _checkpointed:
                    _log(
                        "  ⎇ DEV   checkpoint-committed stranded work before max-iterations retry"
                    )
                    if logger:
                        logger._safe_emit(
                            "dev_checkpoint_commit",
                            phase="DEV",
                            iteration=state.dev_iteration,
                            reason="max_iterations_reached",
                        )
            return None

    if (
        dev_result.success
        and state.error_type != "max_iterations_no_submit"
        and state.total_dev_cost
        > (state.adaptive_dev_cost_estimate_usd or config.dev_profile.budget_usd)
    ):
        # The per-story dollar value is a historical-cost ESTIMATE, not an
        # enforced budget. Post-hoc dollar governance lives at the sprint level
        # (forge.yaml budget_usd); exceeding the per-story estimate is never, by
        # itself, an operator-actionable overrun. So there are only two outcomes:
        #   - committed work → the estimate was simply low; proceed, no action.
        #   - no commits → the attempt produced no usable output; escalate on the
        #     unproductive-attempt semantics (what actually went wrong), NOT a
        #     dollar overrun.
        _cost_estimate = state.adaptive_dev_cost_estimate_usd or config.dev_profile.budget_usd
        if _commits_exist_strict(workspace_path, config.workspace.base_branch):
            _log_verbose(
                f"  DEV cost ${state.total_dev_cost:.4f} exceeded the per-story estimate "
                f"${_cost_estimate:.4f} — committed work found; estimate was low, proceeding "
                "to validate/review (per-story estimates are not enforced budgets)"
            )
        else:
            state.phase = Phase.ESCALATE
            state.error = (
                f"Dev attempt produced no usable output "
                f"(${state.total_dev_cost:.4f} spent, no commits)"
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
        _is_timeout = dev_result.failure_code == "timeout"
        if _is_timeout:
            # Record the wall-clock kill on state immediately, before any
            # retry/escalate/fall-through decision runs. This is the only
            # reliable signal that the dev process was harness-killed at its
            # timeout: the killed iteration's telemetry entry can be overwritten
            # by a later VALIDATE-phase write once checkpoint-committed work lets
            # execution fall through (#1754). model_profiles_bridge reads this to
            # segregate the run as a censored observation for the kill floor.
            state.dev_process_timeout_killed = True
        # The signal number records only what was done to the process, not what
        # went wrong. When the runner already explained the failure in words
        # (e.g. "TIMEOUT: Agent exceeded 900s limit"), surface that instead of
        # the raw exit code so the operator is not left to reconstruct a fact
        # the system already had.
        _failure_detail = (
            dev_result.output.strip()
            if _is_timeout and dev_result.output and dev_result.output.strip()
            else f"exit={dev_result.exit_code}"
        )
        _log_verbose(f"Dev agent failed ({_failure_detail})")
        # ── Checkpoint-commit stranded work (#1746) ──────────────────────
        # A killed/failed dev iteration may leave correct work as uncommitted
        # working-tree state that every commit-reasoning mechanism below is
        # blind to: the timeout-retry (which would restart from an empty
        # base), the zero-commit guard (which would escalate on a diff that is
        # not actually empty), integration, and the audit trail. The agent may
        # already be gone (SIGKILL); the coordinator owns the worktree and is
        # the only party still alive able to commit. Preserve whatever was
        # produced as a checkpoint commit BEFORE any retry/escalate decision
        # runs. Committing only happens when the worktree is genuinely dirty,
        # so a truly empty iteration still escalates as before.
        if _worktree_has_changes(workspace_path):
            _checkpointed = _checkpoint_commit(workspace_path, _failure_detail)
            if _checkpointed:
                _log(
                    f"  ⎇ DEV   checkpoint-committed stranded work before "
                    f"failure handling ({_failure_detail})"
                )
                if logger:
                    logger._safe_emit(
                        "dev_checkpoint_commit",
                        phase="DEV",
                        iteration=state.dev_iteration,
                        reason=_failure_detail,
                    )
        # ── Timeout retry (iterations remaining) ─────────────────────────
        # A per-iteration timeout is a retryable failure, not a terminal
        # escalation. Running out of time is not the same event as crashing or
        # producing wrong work: it is ordinarily retryable and arrives with its
        # own explanation. Where dev iterations remain, spend one and re-enter
        # dev with the timeout and its limit stated in context rather than
        # ending the story with unused budget. The empty-diff guard's job is to
        # keep a zero-commit run from reaching APPROVE — it must not also become
        # the thing that declares a story terminal while a safe outcome (another
        # attempt) remained available. #1216 established this for the gate; it
        # applies equally to the dev phase.
        if _is_timeout and not state.budget.is_exhausted():
            state.retry_reason = RetryReason.TIMEOUT_RESUME
            state.human_feedback = (
                f"Your previous dev iteration was cut off by a timeout: {_failure_detail}. "
                "Any work you had already produced was checkpoint-committed for you, so "
                "the branch already contains it — continue from that committed state rather "
                "than redoing it. Narrow the remaining scope so you finish within the time "
                "limit."
            )
            record_dev_iteration_telemetry(
                state,
                workspace_path,
                max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
                gate_result="DEV_TIMEOUT",
                is_timeout=True,
            )
            _log(
                f"  ✗ DEV   TIMEOUT  (iter={state.dev_iteration} → retrying dev; "
                f"{state.budget.remaining()} iteration(s) remaining)"
            )
            if logger:
                logger._safe_emit(
                    "phase_end",
                    phase="DEV",
                    outcome="timeout_retry",
                    iteration=state.dev_iteration,
                )
            return None
        # ── Zero-commit guard (any failed dev iteration) ─────────────────
        # If the dev agent exited with failure (non-zero or signal-killed) and
        # the worktree has no new commits ahead of base, escalate immediately
        # rather than letting an empty diff flow through to a fake APPROVE.
        if not _has_commits_ahead_of_base(workspace_path, config.workspace.base_branch):
            state.phase = Phase.ESCALATE
            state.error = (
                f"Dev agent failed ({_failure_detail}) and produced no commits "
                "ahead of base — escalating to avoid an empty-diff APPROVE"
            )
            record_dev_iteration_telemetry(
                state,
                workspace_path,
                max_iterations=state.adaptive_dev_max or config.retry.max_dev_iterations,
                gate_result="DEV_FAILURE",
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

    # ── Zero-change guard (review-driven retry only) ─────────────────
    # If the coordinator retried DEV after review REQUEST_CHANGES and the dev
    # agent produced no changes relative to the previous iteration baseline,
    # escalate immediately. This rejects self-reported handoffs when the
    # worktree is unchanged instead of burning another review cycle.
    # Only applies when THIS dev pass was entered for review_changes or extend —
    # gate retries and timeout resumes may legitimately produce no code changes.
    _is_review_driven = _dev_entry_reason in ("review_changes", "extend")
    if _is_review_driven and state.last_dev_start_commit:
        _has_commits = False
        _has_dirty = False
        try:
            _diff_proc = subprocess.run(
                ["git", "diff", "--quiet", state.last_dev_start_commit, "HEAD"],
                cwd=str(workspace_path),
                capture_output=True,
                timeout=10,
            )
            _has_commits = _diff_proc.returncode != 0  # exit 1 = diff exists
        except Exception:  # noqa: BLE001
            pass
        try:
            _status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(workspace_path),
                capture_output=True,
                timeout=10,
            )
            _has_dirty = bool(_status_proc.stdout.strip())
        except Exception:  # noqa: BLE001
            pass
        if not _has_commits and not _has_dirty:
            state.phase = Phase.ESCALATE
            state.error = (
                "Dev retry produced no changes in the worktree relative to the previous "
                "iteration baseline — escalating to avoid re-reviewing identical code"
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

    return None
