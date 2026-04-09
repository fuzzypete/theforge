"""PREFLIGHT phase execution.

Owns everything between WORKSPACE completion and the PLAN/DEV loop:
  - Preflight agent invocation
  - Verdict parsing (PROCEED / ALREADY_DONE / BLOCKED)
  - Warning and complexity parsing
  - Adaptive model assignment (smart-config + assignment.enabled paths)
  - ALREADY_DONE routing (normal short-circuit or override-to-REVIEW)
  - BLOCKED escalation
  - stop_phase gate at PREFLIGHT

Called from run_task(); returns (updated_config, result, already_done_loop):
  - result is not None   → caller returns result immediately
  - result is None, already_done_loop is True  → ALREADY_DONE override;
    caller enters coordinator loop with skip_dev_first_iter=True
  - result is None, already_done_loop is False → PROCEED to PLAN/DEV
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from theforge.config import ForgeConfig
from theforge.sprint.dag import _is_branch_merged
from theforge.task import ContextAssembler, TaskStory, build_preflight_prompt

from . import util as _cu
from .audit import has_review_approve
from .log_tee import _write_log_artifact
from .notify import _escalate_notify, _ntfy_done_notify
from .preflight import (
    _apply_preflight_config,
    _parse_preflight_bundle_candidate,
    _parse_preflight_complexity,
    _parse_preflight_likely_files,
    _parse_preflight_sufficiency,
    _parse_preflight_verdict,
    _parse_preflight_warnings,
    _parse_preflight_work_type,
)
from .state import CoordinatorResult, CoordinatorState, Phase
from .util import _fmt_duration, _log_phase


def _prepare_preflight_working_dir(
    project_root: Path, base_branch: str
) -> tuple[Path, Callable[[], None]]:
    """Materialize a clean baseline checkout for deterministic preflight evaluation."""
    temp_dir = Path(tempfile.mkdtemp(prefix="forge-preflight-"))
    git_dir = project_root / ".git"
    if not git_dir.exists():
        return temp_dir, lambda: shutil.rmtree(temp_dir, ignore_errors=True)

    try:
        archive = subprocess.run(
            ["git", "archive", base_branch],
            cwd=project_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["tar", "-xmf", "-"],
            cwd=temp_dir,
            input=archive.stdout,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to archive baseline branch {base_branch!r}") from exc

    def cleanup() -> None:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return temp_dir, cleanup


if TYPE_CHECKING:
    from theforge.coordinator.logging import StructuredLogger

_log = _cu._log
_log_verbose = _cu._log_verbose

# ── Lazy runner slots ─────────────────────────────────────────────────
# None until first call; tests may replace before calling run_task.
# Patch targets:
#   theforge.coordinator.preflight_flow.run_agent        — preflight agent call
#   theforge.coordinator.preflight_flow.log_agent_result — preflight result logging
run_agent = None
log_agent_result = None


def _ensure_runners() -> None:
    global run_agent, log_agent_result
    if run_agent is not None and log_agent_result is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if log_agent_result is None:
        log_agent_result = _r.log_agent_result


def _run_preflight_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    story_content: str,
    workspace_path: Path,
    branch_name: str,
    *,
    notify: bool,
    logger: "StructuredLogger | None",
    task_start: float,
    state_update_fn: "Callable[[dict], None] | None",
    stop_phase: Phase | None,
) -> tuple[ForgeConfig, CoordinatorResult | None, bool]:
    """Run the PREFLIGHT phase.

    Returns ``(updated_config, result, already_done_loop)``:

    - ``result is not None`` — stop; caller returns ``result`` immediately.
    - ``result is None, already_done_loop is True`` — ALREADY_DONE override;
      caller enters ``_coordinator_loop`` with ``skip_dev_first_iter=True``.
    - ``result is None, already_done_loop is False`` — PROCEED; caller
      continues to ``_run_plan_phase``.
    """
    _ensure_runners()

    state.phase = Phase.PREFLIGHT
    if state_update_fn is not None:
        state_update_fn({"phase": "PREFLIGHT", "iteration": 0, "cost_usd": state.total_cost})
    preflight_profile = config.preflight_profile
    _log_phase(state.phase, preflight_profile.model)
    if logger:
        logger._safe_emit("phase_start", phase="PREFLIGHT", iteration=0)

    preflight_context = ContextAssembler.from_config(config).assemble(
        phase="preflight",
        story_text=story_content,
    )
    state.context_manifests.append({"phase": "preflight", "manifest": preflight_context})
    preflight_prompt = build_preflight_prompt(
        task,
        story_content=story_content,
        assembled_context=preflight_context,
    )

    baseline_working_dir, cleanup_preflight_dir = _prepare_preflight_working_dir(
        config.project_root, config.workspace.base_branch
    )
    try:
        _preflight_start = time.monotonic()
        preflight_result = run_agent(
            prompt=preflight_prompt,
            profile=preflight_profile,
            working_dir=baseline_working_dir,
            secrets=config.secrets,
        )
        _preflight_elapsed = time.monotonic() - _preflight_start
    finally:
        cleanup_preflight_dir()
    state.preflight_duration_s = _preflight_elapsed
    state.preflight_result = preflight_result
    log_agent_result(preflight_result, "PREFLIGHT")

    if preflight_result.success:
        verdict, reason = _parse_preflight_verdict(preflight_result.output)
    else:
        verdict, reason = (
            "BLOCKED",
            f"Preflight agent failed (exit={preflight_result.exit_code}); blocking execution.",
        )
        _log("  ⚠ PREFLIGHT failed — blocking execution")

    state.preflight_verdict = verdict
    state.preflight_reason = reason

    # ── Parsed preflight signals ────────────────────────────────────────
    if preflight_result.success:
        _warnings = _parse_preflight_warnings(preflight_result.output)
        state.preflight_warnings = _warnings
        _likely_files = _parse_preflight_likely_files(preflight_result.output)
        if _likely_files is not None:
            state.preflight_likely_files = _likely_files
        complexity = _parse_preflight_complexity(preflight_result.output)
        state.preflight_complexity = complexity
        sufficiency = _parse_preflight_sufficiency(preflight_result.output)
        state.preflight_sufficiency = sufficiency
        work_type = _parse_preflight_work_type(preflight_result.output)
        state.preflight_work_type = work_type
        bundle_candidate = _parse_preflight_bundle_candidate(preflight_result.output)
        state.preflight_bundle_candidate = bundle_candidate
        _log(f"  Complexity: {complexity} (from preflight)")
        _log(f"  Sufficiency: {sufficiency}")
        _log(f"  Work type: {work_type}")
        _log(f"  Bundle candidate: {bundle_candidate}")
        if _warnings:
            _log(f"  ⚠ PREFLIGHT warnings: {'; '.join(_warnings)}")
        if _likely_files is not None:
            _log(f"  Likely files: {', '.join(_likely_files)}")
    else:
        complexity = state.preflight_complexity or "medium"
        state.preflight_complexity = complexity
        state.preflight_sufficiency = state.preflight_sufficiency or "needs_planning"
        state.preflight_work_type = state.preflight_work_type or "feature"
        state.preflight_bundle_candidate = False

    config = _apply_preflight_config(config, state, log=_log, log_verbose=_log_verbose)

    _log(f"  ✓ PREFLIGHT   {verdict}")
    _log_verbose(f"  Reason: {reason}")
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="PREFLIGHT",
            outcome=verdict.lower(),
            cost_usd=preflight_result.cost_usd,
            duration_s=round(_preflight_elapsed, 2),
        )
    branch_merged = None
    if verdict == "ALREADY_DONE":
        branch_merged = _is_branch_merged(
            branch_name,
            config.workspace.base_branch,
            config.project_root,
            slug=task.slug,
        )
    _preflight_artifact = {
        "verdict": verdict,
        "reason": reason,
        "complexity": state.preflight_complexity,
        "sufficiency": state.preflight_sufficiency,
        "cost_usd": preflight_result.cost_usd,
        "duration_s": round(_preflight_elapsed, 2),
        "likely_files": state.preflight_likely_files,
        "bundle_candidate": state.preflight_bundle_candidate,
        "branch_merged": branch_merged,
        "evaluation_base_branch": config.workspace.base_branch,
    }
    _write_log_artifact(state.log_dir, "preflight-raw.log", preflight_result.output or "")
    _write_log_artifact(
        state.log_dir,
        "preflight.yaml",
        yaml.dump(_preflight_artifact, default_flow_style=False, allow_unicode=True),
    )

    # ── stop_phase gate ────────────────────────────────────────────────
    if stop_phase is not None and stop_phase == Phase.PREFLIGHT:
        return (
            config,
            CoordinatorResult(
                success=True,
                phase=Phase.PREFLIGHT,
                state=state,
                message=f"Stopped at --until {stop_phase.name.lower()}",
            ),
            False,
        )

    # ── ALREADY_DONE ──────────────────────────────────────────────────
    if verdict == "ALREADY_DONE":
        branch_merged = (
            branch_merged
            if branch_merged is not None
            else _is_branch_merged(
                branch_name,
                config.workspace.base_branch,
                config.project_root,
                slug=task.slug,
            )
        )
        ok_log, log_out = _cu._run_shell(
            f"git log {config.workspace.base_branch}..{branch_name} --oneline",
            config.project_root,
        )
        commits_ahead = [ln for ln in log_out.strip().splitlines() if ln.strip()] if ok_log else []
        if (
            not branch_merged
            and commits_ahead
            and not has_review_approve(
                config.project_root, task.slug, config.workspace.base_branch, branch_name
            )
        ):
            n = len(commits_ahead)
            _log(
                f"  ↻ ALREADY_DONE overridden — {n} commit{'s' if n != 1 else ''} on "
                f"{branch_name} without prior APPROVE; resuming from REVIEW"
            )
            if logger:
                logger._safe_emit(
                    "phase_end",
                    phase="PREFLIGHT",
                    outcome="already_done_override",
                    reason="commits_ahead_no_approve",
                )
            # Signal caller to enter coordinator loop with skip_dev_first_iter=True
            return config, None, True

        state.phase = Phase.DONE
        elapsed = time.monotonic() - task_start
        _log(f"✓ DONE   total=${state.total_cost:.2f}  {_fmt_duration(elapsed)}")
        if logger:
            logger._safe_emit(
                "run_end",
                outcome="already_done",
                total_cost_usd=round(state.total_cost, 6),
                total_duration_s=round(elapsed, 2),
            )
        _ntfy_done_notify(
            task,
            state,
            config,
            notify,
            reason or "Spec already satisfied.",
            elapsed,
            branch_name,
        )
        return (
            config,
            CoordinatorResult(
                success=True,
                phase=state.phase,
                state=state,
                message=f"Preflight: spec already implemented. {reason}",
            ),
            False,
        )

    # ── BLOCKED ───────────────────────────────────────────────────────
    if verdict == "BLOCKED":
        state.phase = Phase.ESCALATE
        state.error = f"Preflight: spec is blocked. {reason}"
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("escalate", reason=state.error, phase="PREFLIGHT")
            logger._safe_emit(
                "run_end",
                outcome="escalate",
                total_cost_usd=round(state.total_cost, 6),
                total_duration_s=round(time.monotonic() - task_start, 2),
            )
        _escalate_notify(task, state, notify, config)
        return (
            config,
            CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
            ),
            False,
        )

    # verdict == "PROCEED"
    return config, None, False
