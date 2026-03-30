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

import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from theforge.config import ForgeConfig
from theforge.task import TaskStory, build_preflight_prompt

from . import util as _cu
from .audit import has_review_approve
from .log_tee import _write_log_artifact
from .notify import _escalate_notify, _ntfy_done_notify
from .preflight import (
    _apply_complexity_adaptation,
    _parse_preflight_complexity,
    _parse_preflight_verdict,
    _parse_preflight_warnings,
)
from .state import CoordinatorResult, CoordinatorState, Phase
from .util import _fmt_duration, _log_phase

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

    preflight_prompt = build_preflight_prompt(task, story_content=story_content)

    _preflight_start = time.monotonic()
    preflight_result = run_agent(
        prompt=preflight_prompt,
        profile=preflight_profile,
        working_dir=workspace_path,
        secrets=config.secrets,
    )
    _preflight_elapsed = time.monotonic() - _preflight_start
    state.preflight_duration_s = _preflight_elapsed
    state.preflight_result = preflight_result
    log_agent_result(preflight_result, "PREFLIGHT")

    if preflight_result.success:
        verdict, reason = _parse_preflight_verdict(preflight_result.output)
    else:
        verdict, reason = (
            "PROCEED",
            f"Preflight agent failed (exit={preflight_result.exit_code}); proceeding anyway.",
        )
        state.preflight_complexity = "large"
        _log("  ⚠ PREFLIGHT failed — defaulting complexity to large")

    state.preflight_verdict = verdict
    state.preflight_reason = reason

    # ── Warnings parsing (non-blocking advisories) ─────────────────────
    if preflight_result.success:
        _warnings = _parse_preflight_warnings(preflight_result.output)
        state.preflight_warnings = _warnings
        if _warnings:
            _log(f"  ⚠ PREFLIGHT warnings: {'; '.join(_warnings)}")

    # ── Complexity parsing + adaptive model swapping ───────────────────
    if preflight_result.success:
        complexity = _parse_preflight_complexity(preflight_result.output)
        state.preflight_complexity = complexity
        _log(f"  Complexity: {complexity} (from preflight)")
    else:
        complexity = state.preflight_complexity
        _log(f"  Complexity: {complexity} (preflight failed — using fallback)")

    if config.smart_config_models is not None:
        config = _apply_complexity_adaptation(config, complexity)

    # ── Adaptive assignment ────────────────────────────────────────────
    if config.assignment.enabled and config.agents:
        from .assignment import (  # noqa: PLC0415
            PHASE_TIER as _PHASE_TIER,
        )
        from .assignment import (  # noqa: PLC0415
            _normalize_complexity as _norm_complexity,
        )
        from .assignment import (  # noqa: PLC0415
            _pick_agent as _pick_agt,
        )
        from .assignment import (  # noqa: PLC0415
            _promote_tier as _prom_tier,
        )
        from .assignment import (  # noqa: PLC0415
            assign_models as _assign_models,
        )
        from .assignment import (  # noqa: PLC0415
            load_escalation_history as _load_esc_history,
        )

        _history_path = config.project_root / ".forge" / "assignment_history.yaml"
        _esc_history = _load_esc_history(_history_path)

        from .config import DEFAULT_DEV_PROFILE as _DEF_DEV  # noqa: PLC0415
        from .config import DEFAULT_PREFLIGHT_PROFILE as _DEF_PRE  # noqa: PLC0415

        _explicit: dict[str, object] = {}
        _explicit_roles: set[str] = set()
        if config.smart_config_models is None:
            if config.dev_profile is not _DEF_DEV:
                _explicit["dev"] = config.dev_profile
                _explicit_roles.add("dev")
            if config.preflight_profile is not _DEF_PRE:
                _explicit["preflight"] = config.preflight_profile
                _explicit_roles.add("preflight")
            if config.review_pool and not config.review_pool_is_default:
                _explicit_roles.add("review_pool")
            if not config.plan_model_is_default:
                _explicit_roles.add("planner")
            if config.plan_agent_review.enabled and config.plan_agent_review.profiles:
                _explicit_roles.add("plan_agent_review")

        _decision = _assign_models(
            config.agents,
            config.assignment,
            complexity,
            _esc_history,
            _explicit if _explicit else None,
            state.sprint_promotions,
            config.secrets,
        )

        import dataclasses as _dc  # noqa: PLC0415

        _replace_kwargs: dict = {
            "dev_profile": _decision.dev,
            "preflight_profile": _decision.preflight,
        }
        if _decision.code_reviewers:
            if "review_pool" not in _explicit_roles:
                _replace_kwargs["review_pool"] = _decision.code_reviewers
            else:
                _log("  [adaptive] review_pool: explicit override preserved")
        config = _dc.replace(config, **_replace_kwargs)

        state._adaptive_decision = _decision
        state._explicit_roles = _explicit_roles

        _dev_base_tier = _PHASE_TIER["dev"][_norm_complexity(complexity)]
        _dev_agent = _pick_agt(config.agents, _dev_base_tier, config.secrets)
        _dev_name = _dev_agent.name if _dev_agent else ""
        if _dev_name and "dev" not in _explicit and complexity not in state.sprint_promotions:
            from .assignment import _check_promotion as _chk_prom  # noqa: PLC0415

            _prom = _chk_prom(
                _norm_complexity(complexity),
                _dev_name,
                _esc_history,
                state.sprint_promotions,
            )
            if _prom is not None:
                _promoted_tier = _prom_tier(_dev_base_tier)
                state.sprint_promotions[_norm_complexity(complexity)] = _promoted_tier
                _log_verbose(
                    f"[adaptive] {_norm_complexity(complexity)} dev promoted "
                    f"{_dev_name} → tier {_promoted_tier} (sticky for sprint)"
                )

        _log_verbose(f"[adaptive] Complexity: {_norm_complexity(complexity)} (from preflight)")
        for _phase, _rsn in _decision.rationale.items():
            _log_verbose(f"[adaptive] {_phase}: {_rsn}")

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
    _preflight_artifact = {
        "verdict": verdict,
        "reason": reason,
        "complexity": state.preflight_complexity,
        "cost_usd": preflight_result.cost_usd,
        "duration_s": round(_preflight_elapsed, 2),
    }
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
        ok_log, log_out = _cu._run_shell(
            f"git log {config.workspace.base_branch}..{branch_name} --oneline",
            config.project_root,
        )
        commits_ahead = [ln for ln in log_out.strip().splitlines() if ln.strip()] if ok_log else []
        if commits_ahead and not has_review_approve(
            config.project_root, task.slug, config.workspace.base_branch, branch_name
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
