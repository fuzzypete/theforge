"""Bridge between CoordinatorState and the model_profiles aggregator.

The coordinator records every run outcome in ``model_profiles.yaml`` so the
assignment system can inform future decisions. This module extracts the
per-role telemetry from ``CoordinatorState`` and hands a ``RunOutcome`` to
:mod:`theforge.model_profiles`. Kept as a thin adapter to keep ``engine.py``
free of aggregation logic.
"""

from __future__ import annotations

from pathlib import Path

from theforge.config import ForgeConfig
from theforge.model_profiles import RunOutcome, update_from_run

from .state import CoordinatorState


def _extract_reviewers(
    state: CoordinatorState,
) -> dict[str, tuple[int, int, float]]:
    """Per-reviewer ``(cycles, findings, cost)`` from cycle metadata + telemetry.

    Findings are the per-cycle aggregate (shared view across reviewers); cost
    is split evenly across successful reviewers in each cycle. Attribution is
    approximate but reasonable without per-reviewer telemetry.
    """
    out: dict[str, tuple[int, int, float]] = {}
    meta_list = state.review_cycle_metadata or []
    tele_list = state.review_iteration_telemetry or []
    for idx, meta in enumerate(meta_list):
        tele = tele_list[idx] if idx < len(tele_list) else None
        findings = sum(int(v) for v in tele.findings_by_severity.values()) if tele else 0
        cost = float(tele.cost_usd) if tele else 0.0
        participants = list(meta.successful or [])
        if not participants:
            continue
        per_head_cost = cost / len(participants) if participants else 0.0
        for name in participants:
            prev = out.get(name, (0, 0, 0.0))
            out[name] = (prev[0] + 1, prev[1] + findings, prev[2] + per_head_cost)
    return out


def build_run_outcome(config: ForgeConfig, state: CoordinatorState, success: bool) -> RunOutcome:
    """Pure: assemble a :class:`RunOutcome` from coordinator state."""
    complexity = state.preflight_complexity or "medium"
    # ``dev_trace_count`` is the only monotonic dev-iteration counter (never reset
    # on cycle boundaries) so it captures total dev attempts across the run.
    dev_iterations = max(int(state.dev_trace_count or 0), 1)
    return RunOutcome(
        complexity=complexity,
        dev_model=config.dev_profile.name,
        dev_actual_model=getattr(config.dev_profile, "model", None),
        dev_provider=getattr(config.dev_profile, "provider", None),
        dev_cli=getattr(config.dev_profile, "cli", None),
        dev_success=bool(success),
        dev_iterations=dev_iterations,
        dev_cost_usd=float(state.total_dev_cost or 0.0),
        preflight_model=config.preflight_profile.name
        if getattr(config, "preflight_profile", None)
        else None,
        preflight_actual_model=getattr(config.preflight_profile, "model", None)
        if getattr(config, "preflight_profile", None)
        else None,
        preflight_provider=getattr(config.preflight_profile, "provider", None)
        if getattr(config, "preflight_profile", None)
        else None,
        preflight_cli=getattr(config.preflight_profile, "cli", None)
        if getattr(config, "preflight_profile", None)
        else None,
        preflight_cost_usd=float(state.total_preflight_cost or 0.0),
        reviewers=_extract_reviewers(state),
    )


def update_profiles_from_run(
    *,
    profiles_path: Path,
    history_path: Path | None,
    config: ForgeConfig,
    state: CoordinatorState,
    success: bool,
) -> dict:
    """Extract telemetry and persist an updated ``model_profiles.yaml``."""
    outcome = build_run_outcome(config, state, success)
    return update_from_run(profiles_path, history_path, outcome)
