"""Sprint runner: parallel story scheduling and the run_sprint entry point."""

from __future__ import annotations

import dataclasses
import datetime
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path

import yaml

from .. import worker_budget
from ..advisory_conventions import AdvisoryArtifactError
from ..config import PREFLIGHT_GATE_DECOMPOSE, ForgeConfig, ModelProfile
from ..config.auth import check_agent_auth
from ..config.model_identity import PHASE_PREFLIGHT
from ..coordinator import config_snapshot as config_snapshot_mod
from ..coordinator import workspace as coordinator_workspace
from ..coordinator.agent_failure import (
    CATEGORY_AUTH,
    ERROR_TYPE_INFRASTRUCTURE_ABORT,
    AgentInvocationFailure,
    is_infrastructure_abort,
    mark_infrastructure_abort,
)
from ..coordinator.audit_storage import SubstrateLockTimeoutError
from ..coordinator.audit_storage import substrate_path as audit_substrate_path
from ..coordinator.batch_diff import BatchReviewContext, latest_dev_handoff
from ..coordinator.cancellation import BUDGET_CANCEL_ERROR_TYPE, StopSignal, cancel_cause
from ..coordinator.config_snapshot import SprintConfigSnapshot, capture_or_load
from ..coordinator.dev_phase import extract_failed_tests
from ..coordinator.engine import run_from_dev, run_from_review, run_review_only, run_task
from ..coordinator.gate import run_gate_full
from ..coordinator.landing_record import build_landing_record
from ..coordinator.log_tee import _make_story_log_dir, set_worker_slug
from ..coordinator.logging import StructuredLogger
from ..coordinator.notify import _notify
from ..coordinator.ntfy_client import _ntfy_publish
from ..coordinator.state import CoordinatorResult, CoordinatorState, GateLabel, Phase
from ..coordinator.util import (
    _fmt_cost_total,
    _fmt_duration,
    _generate_run_id,
    resolve_timeout,
)
from ..coordinator.workspace import sweep_orphan_worktrees
from ..intake import (
    AgentRewriteResult,
    IntakeOutcome,
    IntakeOutcomeKind,
    build_agent_rewrite_prompt,
    parse_agent_rewrite_output,
    run_intake_remediation,
)
from ..log_util import _log_line
from ..process_group import ProcessTeardown
from ..runners.rate_registry import AccountingMode, accounting_mode_for
from ..task import BatchMember, TaskStory, load_story
from ..validation_profiles import PHASE_MERGE, select_validation
from . import unmeasured as unmeasured_spend_policy
from .abnormal import (
    ABNORMAL_LAUNCH_GUARD_DROP,
    ABNORMAL_SHARED_INFRASTRUCTURE,
    ABNORMAL_WORKER_EXCEPTION,
    ABNORMAL_WORKER_TIMEOUT,
    accumulate_failure_history,
    build_abnormal_cause,
    carry_failure_cause,
)
from .audit import (
    _get_or_create_sprint_id,
    _write_sprint_audit,
    _write_story_audit,
    load_prior_generation_story_audit,
    persist_accumulated_story_state,
    preflight_degraded_row_fields,
    preflight_likely_files_row_field,
    write_live_story_audit,
)
from .audit_publish import (
    drain_project_memory_before_dispatch,
    project_root_dirt_is_story_run_artifacts_only,
    publish_pending_story_run_audits,
    publish_story_run_artifacts_for_config,
    publish_story_run_audits,
    read_audit_publish_state,
    write_terminal_sprint_audits,
)
from .auth_gate import enforce_sprint_auth_readiness
from .budget_runtime import (
    SprintBudgetRuntime,
    SprintCostLedger,
    SprintCostObservation,
    checkpoint_cost,
    optional_cost,
)
from .carry import (
    previous_run_marker_present as _previous_run_marker_present,
)
from .carry import (
    prior_sprint_cost_incomplete as _query_prior_sprint_cost_incomplete,
)
from .carry import (
    prior_unmeasured_spend_sources as _query_prior_unmeasured_spend_sources,
)
from .carry import (
    read_prior_sprint_accounting as _query_read_prior_sprint_accounting,
)
from .carry import (
    read_prior_sprint_audit_cost as _query_read_prior_sprint_audit_cost,
)
from .ci_checks import PrCheckState, poll_required_checks, required_pr_check_state
from .collision import (
    batch_group_id,
    compute_batch_groups,
    compute_bundle_assignments,
    compute_synthetic_edges,
    inject_synthetic_deps,
    run_batch_preflight,
)
from .dag import (
    REUSE_GATE_PHASE,
    StoryDAG,
    StoryTriage,
    _triage_spec,
    build_dag,
    resolve_satisfied_dependencies,
)
from .display import _print_worker_status, _story_header
from .dropped_work import WorktreeWork, describe_worktree_work, inspect_worktree_work
from .gate_timeout_resolver import resolve_effective_gate_timeout
from .landing_observation import OBSERVER_BATCH_MEMBER as LANDING_OBSERVER_BATCH_MEMBER
from .landing_observation import OBSERVER_INTEGRATION as LANDING_OBSERVER_INTEGRATION
from .landing_observation import OBSERVER_QUEUED_PR as LANDING_OBSERVER_QUEUED_PR
from .landing_observation import reconcile_landing_evidence
from .launch_guard import (
    REASON_IN_FLIGHT,
    REASON_IN_FLIGHT_UNRESOLVED,
    REASON_RECONCILE_PRIOR_DONE,
    REASON_STRANDED_WORKTREE,
)
from .lock import integration_lock
from .manifest import (
    ResolvedSprint,
    SprintResult,
    _build_task_from_story,
    resolve_from_manifest,
)
from .preserved_resume import preserved_escalated_message
from .prior_landing import landing_settled, prior_execution_landed
from .query import (
    NormalizedDependencyPlan,
    normalize_dependency_plan,
)
from .sources import StorySource
from .state_writer import (
    SPRINT_PHASE_DONE,
    SPRINT_PHASE_FAILED,
    SPRINT_PHASE_STOPPED,
    SprintStateWriter,
    read_recorded_spend_usd,
    update_state_phase,
    update_state_story,
)
from .story_state import (
    GATE_STATUS_INCOMPLETE,
    GATE_STATUS_TIMEOUT,
    SprintStoryState,
    StoryOutcome,
    coerce_outcome,
    landing_failure_outcome,
)
from .unmeasured import AcceptedUnmeasuredSpend

# Whether a CLI transport's spend is measurable is now a property of its
# accounting mode (theforge.runners.rate_registry.AccountingMode), not of a
# hardcoded name list: the runner that prices a transport and the warning that
# announces it cannot measure one must read the same classification or they
# drift apart (#2335). `codex` left the untracked set in #2019 because
# `codex exec --json` reports a real token split; `gemini` is still warned about
# because it reports usage only on the invocations that emit a stats block, so
# its cost record is conditional rather than guaranteed.
# Actual per-run measurement failures are still caught downstream by the
# cost-is-None checks that feed `unmeasured_spend` — this is only the pre-sprint
# warning, evaluated over config profiles before any run exists.
_UNTRACKED_ACCOUNTING_MODES = frozenset(
    {
        AccountingMode.UNMEASURABLE,
        AccountingMode.TOKEN_ESTIMATED_IF_REPORTED,
    }
)


def _cli_cost_untracked(runner: str | None) -> bool:
    """Is a CLI transport's spend unmeasurable, or measurable only sometimes?"""
    return accounting_mode_for("cli", runner) in _UNTRACKED_ACCOUNTING_MODES


run_agent = None
log_agent_result = None


def _log(msg: str) -> None:
    # Worker-slug prefixing (parallel attribution) is applied centrally by
    # ``_log_line``; do not prepend it here or it would double-tag.
    _log_line("[sprint]", msg)


def _scrub_root_forge_artifacts(config: ForgeConfig) -> None:
    """Best-effort: remove tracked .forge artifacts from the project-root index."""
    from ..coordinator.workspace import _deindex_forge_artifacts  # noqa: PLC0415

    _deindex_forge_artifacts(config.project_root)


def _ensure_intake_runner() -> None:
    global run_agent, log_agent_result
    if run_agent is not None and log_agent_result is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if log_agent_result is None:
        log_agent_result = _r.log_agent_result


def _build_intake_agent_caller(
    *,
    config: ForgeConfig,
    log: Callable[[str], None],
) -> tuple[Callable[[str, list, list[str]], AgentRewriteResult] | None, str]:
    """Build the configured intake remediation caller or return an explicit reason."""
    _ensure_intake_runner()
    try:
        profile = replace(
            config.dev_profile,
            name="intake-remediation",
            phase=PHASE_PREFLIGHT,
            allowed_tools=(),
        )
    except TypeError:
        detail = "auto-fix enabled but no intake agent caller is available: invalid dev profile"
        log(detail)
        return None, detail
    ready, reason = check_agent_auth(profile, config.secrets)
    if not ready:
        detail = f"auto-fix enabled but no intake agent caller is available: {reason}"
        log(f"Intake remediation agent unavailable: {reason}")
        return None, detail

    def _call(body: str, findings: list, comments: list[str]) -> AgentRewriteResult:
        prompt = build_agent_rewrite_prompt(body, findings, comments)
        result = run_agent(
            prompt=prompt,
            profile=profile,
            working_dir=config.project_root,
            quiet=True,
            secrets=config.secrets,
            plain_text=True,
        )
        log_agent_result(result, "INTAKE_REMEDIATION")
        if not result.success:
            output_snippet = (result.output or "").strip()[:200] or "unknown error"
            detail = f"agent invocation failed: {output_snippet}"
            return AgentRewriteResult(
                replacement=None,
                detail=detail,
                attempted=True,
                profile_name=profile.name,
                model_used=result.model_used or profile.model,
                cost_usd=result.cost_usd,
                transport_used=result.transport_used,
            )
        parsed = parse_agent_rewrite_output(result.output)
        return replace(
            parsed,
            profile_name=profile.name,
            model_used=result.model_used or profile.model,
            cost_usd=result.cost_usd,
            transport_used=result.transport_used,
        )

    return _call, ""


def _run_intake_remediation_pass(
    *,
    config: ForgeConfig,
    tasks: list[TaskStory],
    log: Callable[[str], None],
    force: bool = False,
    sprint_id: str | None = None,
    milestone: str | None = None,
) -> dict[str, IntakeOutcome]:
    """Run the intake remediation pass on the normalized task list.

    Returns an empty dict when intake is fully disabled (grooming + auto_fix
    both False) — the runner skips the dropped/remediated bookkeeping in
    that case, preserving today's behavior exactly.

    When ``intake.grooming`` is enabled and remediation actually fires for an
    issue (a non-PASSED outcome), each firing emits the ADR-0001 training-
    wheels WARNING naming ``forge groom`` and writes a structured record to
    the audit substrate (``inline_remediation_events``). ``sprint_id`` and
    ``milestone`` label those records for per-milestone refusal-economics
    rollups; both default to ``None`` for the entry-shape-gate bridge path,
    which has no sprint id in scope.
    """
    intake_cfg = getattr(config, "intake", None)
    grooming_raw = getattr(intake_cfg, "grooming", False)
    auto_fix_raw = getattr(intake_cfg, "auto_fix", False)
    auto_fix_mode_raw = getattr(intake_cfg, "auto_fix_mode", "comment")
    grooming_enabled = grooming_raw if isinstance(grooming_raw, bool) else False
    auto_fix_enabled = auto_fix_raw if isinstance(auto_fix_raw, bool) else False
    auto_fix_mode = auto_fix_mode_raw if isinstance(auto_fix_mode_raw, str) else "comment"
    auto_fix_mode = auto_fix_mode or "comment"
    if not grooming_enabled and not auto_fix_enabled and not force:
        return {}
    if force:
        log(
            "Intake remediation gate bypassed by --force "
            f"(grooming={grooming_enabled} auto_fix={auto_fix_enabled} mode={auto_fix_mode})"
        )
    else:
        log(
            "Intake remediation gate: grooming="
            f"{grooming_enabled} auto_fix={auto_fix_enabled} mode={auto_fix_mode}"
        )
    agent_caller = None
    missing_agent_detail = "auto-fix enabled but no agent caller wired"
    if auto_fix_enabled and not force:
        agent_caller, missing_agent_detail = _build_intake_agent_caller(
            config=config,
            log=log,
        )
    _remediation_started = time.monotonic()
    outcomes = run_intake_remediation(
        tasks,
        config.project_root,
        grooming_enabled=grooming_enabled,
        auto_fix_enabled=auto_fix_enabled,
        auto_fix_mode=auto_fix_mode,
        agent_caller=agent_caller,
        missing_agent_detail=missing_agent_detail,
        force=force,
    )
    _remediation_duration = time.monotonic() - _remediation_started
    # Training-wheels posture (ADR-0001): when the opt-in grooming fallback
    # fires inline, tell the operator the intended pre-sprint path and record
    # the event for refusal-economics rollups. Gated on grooming_enabled so
    # auto_fix-only runs (a distinct opt-in) don't emit the grooming memo.
    if grooming_enabled:
        _emit_inline_remediation_events(
            config=config,
            tasks=tasks,
            outcomes=outcomes,
            log=log,
            sprint_id=sprint_id,
            milestone=milestone,
            duration_seconds=_remediation_duration,
        )
    return outcomes


def _emit_inline_remediation_events(
    *,
    config: ForgeConfig,
    tasks: list[TaskStory],
    outcomes: dict[str, IntakeOutcome],
    log: Callable[[str], None],
    sprint_id: str | None,
    milestone: str | None,
    duration_seconds: float,
) -> None:
    """Emit the ADR-0001 training-wheels WARNING + audit record per firing.

    "Firing" = a non-PASSED outcome (blocking findings triggered remediation).
    PASSED outcomes did nothing, so they neither warn nor record. Substrate
    write failures are observability-only: they log a WARNING and never abort
    the sprint.
    """
    slug_to_issue = {t.slug: getattr(t, "github_issue", None) for t in tasks}
    for slug, outcome in outcomes.items():
        # Production outcomes are always IntakeOutcome; guard so a caller that
        # passes a stub/placeholder (only tests do) never trips substrate I/O.
        if not isinstance(outcome, IntakeOutcome):
            continue
        if outcome.kind is IntakeOutcomeKind.PASSED:
            continue
        emit_inline_remediation_event(
            config=config,
            issue=slug_to_issue.get(slug),
            slug=slug,
            outcome=outcome,
            log=log,
            sprint_id=sprint_id,
            milestone=milestone,
            duration_seconds=duration_seconds,
        )


def emit_inline_remediation_event(
    *,
    config: ForgeConfig,
    issue: int | None,
    slug: str,
    outcome: IntakeOutcome,
    log: Callable[[str], None],
    sprint_id: str | None,
    milestone: str | None,
    duration_seconds: float,
) -> None:
    """Emit the ADR-0001 training-wheels WARNING + one audit record for a firing.

    A single inline-remediation firing (one issue). Shared by the in-pass loop
    and the entry-shape-gate bridge, which converts a body-PASSED outcome into
    a ``declined`` DROPPED_SHAPE *after* the pass loop has skipped it (that
    conversion path has no other firing hook). The ``remediation_source`` in
    the outcome's audit block drives the recorded ``action``: a ``declined``
    source records ``action="declined"`` and takes the triggering verdict from
    the shape-gate reason codes the bridge stashed in ``audit`` (the body
    checks produced no findings). Substrate write failures are
    observability-only.
    """
    from ..coordinator.audit_substrate import (  # noqa: PLC0415
        SubstrateError,
        record_inline_remediation_event,
    )

    logger = logging.getLogger("theforge.intake")
    issue_ref = f"#{issue}" if issue is not None else slug
    groom_ref = str(issue) if issue is not None else slug
    line1 = f"Inline intake remediation ran at sprint entry for {issue_ref}."
    line2 = f"Intended workflow: run `forge groom {groom_ref}` before sprint selection."
    # Operator-facing sprint log (matches the ADR `[forge]` example) …
    log(line1)
    log(line2)
    # … and a WARNING-level record so the message carries severity.
    logger.warning("%s %s", line1, line2)

    audit = outcome.audit if isinstance(outcome.audit, dict) else {}
    remediation_source = audit.get("remediation_source")
    codes = _intake_finding_codes(outcome)
    if not codes:
        shape_gate_codes = audit.get("shape_gate_codes")
        if isinstance(shape_gate_codes, list) and shape_gate_codes:
            codes = [str(c) for c in shape_gate_codes]
    declined = remediation_source == "declined"
    action = "declined" if declined else outcome.kind.value
    event = {
        "issue_id": str(issue) if issue is not None else slug,
        "sprint_id": sprint_id,
        "milestone": milestone,
        "shape_verdict": codes[0] if codes else outcome.kind.value,
        "shape_verdict_codes": codes,
        "action": action,
        "succeeded": outcome.kind is IntakeOutcomeKind.REMEDIATED,
        "cost_usd": _intake_outcome_cost(outcome),
        "duration_seconds": duration_seconds,
        "remediation_source": remediation_source,
        "detail": outcome.detail,
    }
    try:
        record_inline_remediation_event(config.project_root, event)
    except SubstrateError as exc:
        msg = f"inline-remediation substrate write failed (continuing): {exc}"
        log(f"WARNING: {msg}")
        logger.warning(msg)


def _story_reported_cost(state: object, adjustment: float = 0.0) -> float | None:
    """Per-story reported cost: measured total plus adjustment, or ``None``.

    Uses ``CoordinatorState.total_cost_measured`` so a story with any unmeasured
    phase is reported as cost-unknown instead of as the measured remainder.
    """
    if hasattr(state, "total_cost_measured"):
        measured = state.total_cost_measured
    else:
        measured = getattr(state, "total_cost", None)
    if not isinstance(measured, (int, float)) or isinstance(measured, bool):
        return None
    return round(float(measured) + adjustment, 4)


def _intake_outcome_cost(outcome: IntakeOutcome) -> float:
    """Return the agent cost recorded in an intake outcome's audit block.

    Intake remediation agent calls spend sprint-authorized budget but live
    outside CoordinatorState.total_cost. Sprint cost rollups must consult
    this seam so reported sprint totals reflect actual spend.
    """
    return _intake_outcome_cost_measured(outcome) or 0.0


def _intake_outcome_cost_measured(outcome: IntakeOutcome) -> float | None:
    """Intake agent cost, or ``None`` when an agent ran without reporting cost.

    ``attempted`` is what separates "no agent ran, so genuinely free" from "an
    agent ran on a transport that reported no cost". Only the latter is
    cost-unknown; collapsing it to ``0.0`` would let unpriced intake spend pass
    the sprint budget check as if it were free (#1992).
    """
    agent = outcome.audit.get("agent") if isinstance(outcome.audit, dict) else None
    if not isinstance(agent, dict):
        return 0.0
    raw = agent.get("cost_usd")
    if raw is None:
        return None if agent.get("attempted") else 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _intake_finding_codes(outcome: IntakeOutcome) -> list[str]:
    """Stable-sorted list of unique blocking finding codes for an intake outcome."""
    seen: set[str] = set()
    ordered: list[str] = []
    for finding in outcome.findings:
        code = finding.code
        if code and code not in seen:
            seen.add(code)
            ordered.append(code)
    return ordered


def _intake_outcome_summary(outcome: IntakeOutcome) -> str:
    """One-line operator-readable summary: '<codes> — <detail>'.

    Carries enough information for an operator to know which intake rule fired
    on the dropped story, the agent attempt status, and the rerun-gate result.
    """
    codes = _intake_finding_codes(outcome)
    code_part = "[" + ", ".join(codes) + "]" if codes else ""
    detail = outcome.detail or outcome.kind.value
    if code_part and detail:
        return f"{code_part} {detail}"
    return code_part or detail


def _intake_problem_lines(outcome: IntakeOutcome) -> list[str]:
    """Human-readable per-finding descriptors: 'code (location): problem'."""
    lines: list[str] = []
    for finding in outcome.findings:
        loc = finding.location or ""
        head = f"{finding.code} ({loc})" if loc else finding.code
        problem = (finding.problem or "").strip()
        lines.append(f"{head}: {problem}" if problem else head)
    return lines


def _intake_error_type(outcome: IntakeOutcome) -> str:
    """outcome_code-style classifier: dominant rule code, else the kind value."""
    codes = _intake_finding_codes(outcome)
    if codes:
        return codes[0]
    return outcome.kind.value


def _intake_log_lines(outcome: IntakeOutcome, *, outcome_name: str, display_key: str) -> list[str]:
    """Lines the runner emits to the sprint log for a non-PASSED intake outcome.

    Operators must learn the rule code(s), the per-finding problem strings,
    and the agent attempt details (whether the LLM ran, cost, model,
    transport, whether the issue was edited or commented on) — without
    consulting audit YAML or re-deriving with a Python snippet against
    groom_check.
    """
    summary = _intake_outcome_summary(outcome)
    lines: list[str] = []
    if summary:
        lines.append(f"  {outcome_name} {display_key}: {summary}")
    else:
        lines.append(f"  {outcome_name} {display_key}")
    for problem_line in _intake_problem_lines(outcome):
        lines.append(f"      - {problem_line}")
    agent_summary = _intake_agent_summary(outcome)
    if agent_summary:
        lines.append(f"      auto-fix: {agent_summary}")
    return lines


def _intake_agent_summary(outcome: IntakeOutcome) -> str:
    """One-line agent-attempt summary derived from outcome.audit.

    Surfaces ``remediation_source`` plus the agent block fields (attempted,
    cost_usd, model_used, transport_used) so the run log and forge status
    DETAIL show whether the auto-fix actually tried and what it cost — not
    just whether the rerun gate failed.
    """
    audit = outcome.audit or {}
    parts: list[str] = []
    source = audit.get("remediation_source")
    if isinstance(source, str) and source and source != "none":
        parts.append(f"source={source}")
    agent = audit.get("agent")
    if isinstance(agent, dict):
        attempted = bool(agent.get("attempted"))
        parts.append(f"agent_attempted={'yes' if attempted else 'no'}")
        cost = agent.get("cost_usd")
        if isinstance(cost, (int, float)) and cost > 0:
            parts.append(f"cost=${float(cost):.4f}")
        model = agent.get("model_used")
        if isinstance(model, str) and model:
            parts.append(f"model={model}")
        transport = agent.get("transport_used")
        if isinstance(transport, str) and transport:
            parts.append(f"transport={transport}")
    if audit.get("issue_updated") is True:
        parts.append("issue_updated=true")
    if audit.get("comment_posted") is True:
        parts.append("comment_posted=true")
    return ", ".join(parts)


def _intake_audit_block(outcome: IntakeOutcome) -> dict:
    """Structured intake_findings block surfaced into audit/summary YAML."""
    return {
        "kind": outcome.kind.value,
        "detail": outcome.detail,
        "codes": _intake_finding_codes(outcome),
        "findings": [f.as_dict() for f in outcome.findings],
        "agent_summary": _intake_agent_summary(outcome),
        "audit": dict(outcome.audit),
    }


def _filter_normalized_for_intake(
    plan: NormalizedDependencyPlan,
    dropped: set[str],
) -> NormalizedDependencyPlan:
    """Drop intake-rejected slugs from a normalized plan.

    Tasks whose slug is in ``dropped`` are removed entirely. Their dependents
    surface here as additions to ``plan.blocked`` so downstream stages can
    report consistent blocked-by reasons.
    """
    if not dropped:
        return plan
    surviving = [t for t in plan.tasks if t.slug not in dropped]
    blocked = dict(plan.blocked)
    for task in plan.tasks:
        if task.slug in dropped:
            continue
        upstream_dropped = [d for d in (task.depends_on or ()) if d in dropped]
        if upstream_dropped:
            existing = list(blocked.get(task.slug, []))
            for slug in upstream_dropped:
                if slug not in existing:
                    existing.append(slug)
            blocked[task.slug] = existing
    return NormalizedDependencyPlan(tasks=surviving, blocked=blocked)


def derive_worker_timeout(
    base: int,
    complexity: str | None,
    complexity_score: int | None = None,
) -> int:
    """Derive a per-story worker timeout from sprint defaults and complexity."""
    return resolve_timeout(base, None, None, complexity, complexity_score)


def _read_prior_sprint_cost(project_root: Path, sprint_id: str | None) -> float:
    """Read carry-forward cost for a same-sprint re-exec from sprint-audit.yaml.

    ``total_cost_usd`` is null when the prior generation had a story whose cost
    was unmeasured (#1992). Carry-forward is arithmetic, so it falls back to the
    measured lower bound rather than dropping the prior spend entirely — the
    cost-unknown signal itself is carried by the per-story entries.
    """
    if not sprint_id or not _previous_run_marker_present():
        return 0.0
    return _query_read_prior_sprint_audit_cost(project_root, sprint_id)


def _prior_unmeasured_spend_sources(project_root: Path, sprint_id: str | None) -> list[str]:
    """The sources the prior generation recorded as unmeasured, if any."""
    return _query_prior_unmeasured_spend_sources(project_root, sprint_id)


def _prior_sprint_cost_incomplete(
    project_root: Path,
    sprint_id: str | None,
    accepted: Mapping[str, AcceptedUnmeasuredSpend] | None = None,
) -> bool:
    """Return True when the prior generation recorded an incomplete sprint cost.

    A carried total from a generation that could not measure all of its spend is
    itself a lower bound; the budget check must know that before enforcing a cap
    against it (#1992). Absent/unreadable records report False — the pre-#1992
    shape simply carries no completeness claim.

    ``accepted`` clears the flag only when the prior generation NAMED what it
    could not measure and every one of those sources has been accepted with a
    recorded ceiling (#2310). An incomplete generation that named nothing keeps
    the flag: there is no source there for an operator to have resolved, so the
    whole-generation carry remains the honest statement.
    """
    return _query_prior_sprint_cost_incomplete(project_root, sprint_id, dict(accepted or {}))


def _parse_accumulated_story_timestamp(value: object) -> datetime.datetime | None:
    """Parse timestamps persisted in accumulated sprint story state."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_prior_sprint_accounting(
    project_root: Path,
    sprint_id: str | None,
) -> tuple[float, datetime.datetime | None, dict[str, dict]]:
    """Recover prior same-sprint cost/timing from progressive story state."""
    recovered_cost, earliest_started_at, recovered_entries = _query_read_prior_sprint_accounting(
        project_root, sprint_id
    )
    if recovered_entries:
        return recovered_cost, earliest_started_at, recovered_entries

    if _previous_run_marker_present():
        return _read_prior_sprint_cost(project_root, sprint_id), None, {}

    return 0.0, None, {}


def _project_root_is_git_checkout(project_root: Path) -> bool:
    """Return True when the project root is inside a git checkout."""

    git_dir_check = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return git_dir_check.returncode == 0


def _refuse_dirty_root_before_spend(
    config: ForgeConfig, *, lands_in_project_root: bool, stage: str
) -> None:
    """Abort the sprint when the project root cannot accept the landing it owes.

    Called at two points, each as soon as the obligation it tests becomes
    knowable: ``sprint-entry`` for the configuration-level signals, and
    ``dependency-resolved`` once the satisfied and resume-triage sets say which
    in-manifest dependency parents will actually be dispatched and merged. Both
    run ahead of every agent spend, so the operator learns of a dirty root
    instead of paying dev and review and meeting the refusal at landing (#2048).

    ``stage`` is logged with the refusal so the audit trail records which pass
    fired — the two answer different questions and a misfire is diagnosed by
    knowing which one spoke.
    """
    if not lands_in_project_root:
        return
    if not _project_root_is_git_checkout(config.project_root):
        return
    block = coordinator_workspace.landing_precondition_error(config, lands_in_project_root=True)
    if block is None:
        return
    _log(f"✗ SPRINT ABORT  [{stage}] {block}")
    raise RuntimeError(block)


#: ``GateLabel.purpose`` for the pre-sprint merge-base gate.
BASELINE_GATE_PURPOSE = "baseline gate"

#: ``GateLabel.purpose`` for the immediate re-run that decides whether a failing
#: baseline gate is evidence about the merge base or about one noisy invocation
#: (#2434). A distinct purpose keeps the two runs distinguishable in the log,
#: where the resolved command is identical.
BASELINE_GATE_CONFIRM_PURPOSE = "baseline gate confirmation"

#: Top-level ``sprint_phase`` values for the pre-story window. Both gates below
#: can run for many minutes; without their own phases the whole window reports
#: as ``starting`` with every story ``waiting`` (#2014).
SPRINT_PHASE_STARTING = "starting"
SPRINT_PHASE_BASELINE_GATE = "baseline-gate"
SPRINT_PHASE_TRIAGE = "triage"


def _publish_reuse_gate_start(run_id: str | None, project_root: Path, label: GateLabel) -> None:
    """Show a story as actively gated while resume triage validates its worktree.

    Triage runs before ``SprintStateWriter`` exists, so this writes the bootstrap
    state file directly. The story is not dispatched yet, but it is not waiting
    either: a real gate subprocess is running against its worktree, and that —
    with the branch, commit, and start time — is what the operator needs to see
    instead of ``waiting`` for the gate's whole duration (#2014).

    No-op without a ``run_id`` (headless invocations have no live state file).
    """
    if not run_id or not label.slug:
        return
    update_state_story(
        run_id,
        project_root,
        label.slug,
        status="running",
        phase=REUSE_GATE_PHASE,
        started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        detail_updates={
            "gate_purpose": label.purpose,
            "gate_branch": label.target,
            "gate_commit": label.commit,
            "gate_worktree": label.worktree_path,
        },
    )


def _publish_reuse_gate_end(run_id: str | None, project_root: Path, label: GateLabel) -> None:
    """Return the story to waiting once its reuse gate is done.

    The triage verdict is applied by the normal dispatch path later; leaving the
    story at ``running`` here would keep claiming in-flight work — with a
    growing elapsed time — for the rest of the startup window.
    """
    if not run_id or not label.slug:
        return
    update_state_story(
        run_id,
        project_root,
        label.slug,
        status="waiting",
        phase=None,
        started_at=None,
        detail_updates={
            "gate_purpose": None,
            "gate_branch": None,
            "gate_commit": None,
            "gate_worktree": None,
        },
    )


#: Bound on the gate excerpt carried inside the broken-baseline message. The
#: full tail stays available under the result's ``output_tail`` and in the run
#: log; this is only what has to travel with the verdict itself.
BASELINE_DIAGNOSTIC_MAX_LINES = 12
BASELINE_DIAGNOSTIC_MAX_CHARS = 1200


def _baseline_failure_diagnostic(output_tail: object) -> str:
    """Render the gate's own output as a bounded excerpt for the failure message.

    Returns "" when the gate captured nothing, so the caller's message is
    unchanged rather than gaining an empty section.
    """
    tail = (output_tail or "").strip() if isinstance(output_tail, str) else ""
    if not tail:
        return ""
    lines = tail.splitlines()
    truncated = len(lines) > BASELINE_DIAGNOSTIC_MAX_LINES
    excerpt = "\n".join(lines[-BASELINE_DIAGNOSTIC_MAX_LINES:])
    if len(excerpt) > BASELINE_DIAGNOSTIC_MAX_CHARS:
        excerpt = excerpt[-BASELINE_DIAGNOSTIC_MAX_CHARS:]
        truncated = True
    header = "Gate output (last lines, truncated):" if truncated else "Gate output:"
    return f"{header}\n{excerpt}"


#: How many preserved baseline worktrees may exist at once. A halting baseline
#: gate keeps the worktree that produced it, because that worktree is the only
#: place the failure can be re-run in the environment that produced it. Each one
#: is a full checkout plus whatever the setup command installed into it, so the
#: set is bounded: the newest are kept, older ones are reclaimed on the next
#: baseline run. The durable evidence file outlives all of them.
BASELINE_WORKTREE_KEEP = 2

#: Directory prefix for the baseline gate's temporary worktree parent.
BASELINE_TEMP_PREFIX = "forge-baseline-"


def _remove_baseline_temp_root(project_root: Path, temp_root: Path) -> None:
    """Unregister and delete one ``forge-baseline-*`` temp root, best-effort.

    The prune covers the leftovers of a run that died before its own cleanup:
    ``git worktree remove`` declines a path it no longer knows about, which
    would leave the admin entry behind after the directory goes.
    """
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(temp_root / "worktree")],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    shutil.rmtree(temp_root, ignore_errors=True)
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _prune_preserved_baseline_worktrees(project_root: Path, keep: int) -> None:
    """Reclaim all but the *keep* newest preserved baseline worktrees.

    Called before a new baseline run creates its own temp root, so that a run
    which goes on to preserve leaves at most ``BASELINE_WORKTREE_KEEP`` behind.
    Only ``forge-baseline-*`` directories are touched, and only ones a previous
    run left: a passing run removes its own.

    Best-effort — pruning old evidence must never be the reason a sprint fails
    to start.
    """
    forge_root = project_root / ".forge"
    try:
        candidates = [
            path
            for path in forge_root.iterdir()
            if path.is_dir() and path.name.startswith(BASELINE_TEMP_PREFIX)
        ]
    except OSError:
        return
    try:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in candidates[max(keep, 0) :]:
        try:
            _remove_baseline_temp_root(project_root, stale)
        except Exception as exc:  # noqa: BLE001
            _log(f"Warning: could not reclaim preserved baseline worktree {stale}: {exc}")


def _write_baseline_gate_evidence(
    *,
    log_dir: Path,
    filename: str,
    header_lines: list[str],
    full_output: str | None,
) -> tuple[str | None, str | None]:
    """Persist the halting baseline gate's raw output outside the temp worktree.

    Returns ``(path, unavailable_reason)`` — exactly one is set. An absent
    account is written *as* an absent account: when the gate produced no output
    record the file is still written, saying so, so a reader can tell "nothing
    was captured" from "nothing went wrong". A reason is returned only when the
    file itself could not be written, which is the one case no file can state.
    """
    if full_output:
        body = full_output
    else:
        body = (
            "Gate output was not captured: the gate command produced no output "
            "record for this run.\n"
        )
    content = "\n".join([*header_lines, "", body])
    path = log_dir / filename
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return None, f"could not write {path}: {exc}"
    return str(path), None


def _baseline_evidence_footer(
    *,
    worktree: str | None,
    evidence_path: str | None,
    evidence_unavailable: str | None,
) -> str:
    """Name, on the verdict itself, where the evidence for it now lives.

    The verdict travels further than the run log — it becomes the RuntimeError
    text and the audit record's message — so the pointers travel with it rather
    than sitting in a file the verdict never references. When the full output
    could not be persisted the message says that, so a missing pointer is never
    read as "there was nothing to point at".
    """
    parts: list[str] = []
    if evidence_path:
        parts.append(f"Full gate output: {evidence_path}")
    elif evidence_unavailable:
        parts.append(f"Full gate output unavailable: {evidence_unavailable}")
    if worktree:
        parts.append(f"Baseline worktree preserved for reproduction: {worktree}")
    return "\n" + "\n".join(parts) if parts else ""


def _baseline_validation_fields(
    config: ForgeConfig, *, command: str | None = None
) -> dict[str, object]:
    """Describe the validation the baseline record refers to (#2358).

    The baseline gate is an authoritative run like any other, so its record has
    to name the profile behind it. ``command`` is the command that actually ran
    when one did; on the paths where the gate never executed (not a git
    checkout, worktree setup failed, re-exec continuation) the record still
    names the command it *would* have run, resolved through the same
    merge-authority profile rather than read off the raw config field — those
    two can differ once a project declares profiles, and a record naming a
    command that was never going to run is worse than no record.
    """
    selection = select_validation(config.validation, phase=PHASE_MERGE)
    return {
        "command": command if command is not None else selection.command,
        "validation_profile": selection.profile,
        "validation_authority": selection.authority,
    }


def _run_baseline_gate(
    config: ForgeConfig,
    resolved: ResolvedSprint,
    *,
    run_id: str | None = None,
    on_confirmation_start: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Run the configured gate on the sprint merge base before any agent work starts.

    A halting outcome (the gate failed, or the workspace setup that precedes it
    failed) keeps two things the run would otherwise destroy at the moment it
    produces them: the gate's full raw output, written under
    ``.forge/logs/<sprint>/`` where it outlives the run, and the temporary
    worktree the gate ran in, which is the only environment the failure can be
    reproduced in. Cleanup of the worktree happens after that capture, never
    before, and only on outcomes that did not halt the sprint (#2160).

    A gate that fails is re-run once against the same worktree and commit before
    its FAIL is treated as the baseline's verdict; only a failure that reproduces
    halts the sprint (#2434). Every record produced by an executed gate carries
    ``confirmation_attempted`` and ``failure_reproduced`` so a reader — and the
    audit — can tell a confirmed broken baseline from a failure that vanished on
    re-run. ``on_confirmation_start``, when given, is called just before that
    second run so a caller can say so on its live progress surface: the re-run
    doubles the gate's wall time and would otherwise look like a stuck sprint.
    """

    # Declared before the first return so *every* baseline record carries the
    # same key. The early exits happen before any project command has run, so
    # theirs is empty — which is the truthful answer, and a uniform shape means a
    # reader never has to ask whether this record is one of the kinds that says
    # (#2309).
    gate_teardowns: list[ProcessTeardown] = []
    base_branch = config.workspace.base_branch
    if not _project_root_is_git_checkout(config.project_root):
        now = datetime.datetime.now(datetime.timezone.utc)
        return {
            "status": "skipped",
            "passed": True,
            "exit_code": 0,
            "duration_seconds": 0.0,
            "started_at": now,
            "finished_at": now,
            "merge_base": None,
            **_baseline_validation_fields(config),
            "process_teardowns": [t.to_audit_dict() for t in gate_teardowns],
            "message": "Baseline gate skipped: project root is not a git checkout",
        }

    baseline_started_at = datetime.datetime.now(datetime.timezone.utc)
    started_monotonic = time.monotonic()
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", base_branch],
        cwd=config.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if merge_base.returncode != 0:
        duration = time.monotonic() - started_monotonic
        stderr = (merge_base.stderr or "").strip()
        return {
            "status": "error",
            "passed": False,
            "exit_code": merge_base.returncode,
            "duration_seconds": round(duration, 2),
            "started_at": baseline_started_at,
            "finished_at": datetime.datetime.now(datetime.timezone.utc),
            "merge_base": None,
            **_baseline_validation_fields(config),
            "process_teardowns": [t.to_audit_dict() for t in gate_teardowns],
            "message": (
                "Broken baseline: unable to determine merge base against "
                f"{base_branch}: {stderr or 'git merge-base failed'}"
            ),
        }

    merge_base_ref = (merge_base.stdout or "").strip()
    if not merge_base_ref:
        duration = time.monotonic() - started_monotonic
        return {
            "status": "error",
            "passed": False,
            "exit_code": merge_base.returncode,
            "duration_seconds": round(duration, 2),
            "started_at": baseline_started_at,
            "finished_at": datetime.datetime.now(datetime.timezone.utc),
            "merge_base": None,
            **_baseline_validation_fields(config),
            "process_teardowns": [t.to_audit_dict() for t in gate_teardowns],
            "message": (
                "Broken baseline: unable to determine merge base against "
                f"{base_branch}: empty merge-base result"
            ),
        }

    show_top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    show_top_path = (show_top.stdout or "").strip()
    same_toplevel = False
    if show_top.returncode == 0 and show_top_path:
        try:
            same_toplevel = os.path.samefile(show_top_path, config.project_root)
        except OSError:
            same_toplevel = Path(show_top_path).resolve() == config.project_root.resolve()
    if not same_toplevel:
        duration = time.monotonic() - started_monotonic
        return {
            "status": "error",
            "passed": False,
            "exit_code": show_top.returncode,
            "duration_seconds": round(duration, 2),
            "started_at": baseline_started_at,
            "finished_at": datetime.datetime.now(datetime.timezone.utc),
            "merge_base": merge_base_ref,
            **_baseline_validation_fields(config),
            "process_teardowns": [t.to_audit_dict() for t in gate_teardowns],
            "message": (
                "Broken baseline: sprint baseline gate requires running from the root checkout; "
                "current workspace is not the project toplevel"
            ),
        }

    forge_temp_root = config.project_root / ".forge"
    forge_temp_root.mkdir(parents=True, exist_ok=True)
    # Reclaim older preserved worktrees *before* claiming disk for this run's, so
    # the bound holds across a streak of failing runs.
    _prune_preserved_baseline_worktrees(config.project_root, BASELINE_WORKTREE_KEEP - 1)
    temp_root = Path(tempfile.mkdtemp(prefix=BASELINE_TEMP_PREFIX, dir=forge_temp_root))
    baseline_worktree = temp_root / "worktree"
    sprint_log_dir = config.project_root / ".forge" / "logs" / resolved.name
    # Run-id-keyed where there is one, matching the rest of the sprint log
    # directory. Headless invocations have no run id, so the temp root's own
    # unique suffix names the file instead — a wall-clock stamp would collide
    # between two runs in the same second and silently overwrite the first
    # run's evidence with the second's.
    evidence_filename = (
        f"run-{run_id}-baseline-gate.txt"
        if run_id
        else f"baseline-gate-{temp_root.name.removeprefix(BASELINE_TEMP_PREFIX)}.txt"
    )
    # Set to the worktree path by any outcome that halts the sprint; the finally
    # block reclaims the worktree only while this is None.
    preserved_worktree: str | None = None
    try:
        add_worktree = subprocess.run(
            ["git", "worktree", "add", "--detach", str(baseline_worktree), merge_base_ref],
            cwd=config.project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if add_worktree.returncode != 0:
            duration = time.monotonic() - started_monotonic
            stderr = (add_worktree.stderr or "").strip()
            return {
                "status": "error",
                "passed": False,
                "exit_code": add_worktree.returncode,
                "duration_seconds": round(duration, 2),
                "started_at": baseline_started_at,
                "finished_at": datetime.datetime.now(datetime.timezone.utc),
                "merge_base": merge_base_ref,
                **_baseline_validation_fields(config),
                "process_teardowns": [t.to_audit_dict() for t in gate_teardowns],
                "message": (
                    "Broken baseline: unable to create temporary worktree for merge base "
                    f"{merge_base_ref}: {stderr or 'git worktree add failed'}"
                ),
            }

        # Both the setup command and the gate below are project commands run in
        # this temporary worktree, and a leak from either belongs in the one
        # record the sprint keeps for this baseline (#2309).
        if config.workspace.setup_command:
            _log(
                "Running baseline workspace setup "
                f"(timeout {config.workspace.setup_timeout}s): "
                f"{config.workspace.setup_command}"
            )
            setup_ok, setup_out = coordinator_workspace._run_setup_split(
                config.workspace.setup_command,
                baseline_worktree,
                config.workspace.python_interpreter,
                timeout=config.workspace.setup_timeout,
                teardown_out=gate_teardowns,
            )
            if not setup_ok:
                duration = time.monotonic() - started_monotonic
                preserved_worktree = str(baseline_worktree)
                setup_message = (
                    "Broken baseline: workspace setup command failed in the temporary "
                    f"baseline worktree for merge base {merge_base_ref}: {setup_out}"
                )
                evidence_path, evidence_unavailable = _write_baseline_gate_evidence(
                    log_dir=sprint_log_dir,
                    filename=evidence_filename,
                    header_lines=[
                        f"# baseline workspace setup failed on merge base {merge_base_ref}",
                        f"# setup command: {config.workspace.setup_command}",
                        f"# worktree: {baseline_worktree}",
                    ],
                    full_output=setup_out,
                )
                setup_message += _baseline_evidence_footer(
                    worktree=preserved_worktree,
                    evidence_path=evidence_path,
                    evidence_unavailable=evidence_unavailable,
                )
                return {
                    "status": "error",
                    "passed": False,
                    "exit_code": 1,
                    "duration_seconds": round(duration, 2),
                    "started_at": baseline_started_at,
                    "finished_at": datetime.datetime.now(datetime.timezone.utc),
                    "merge_base": merge_base_ref,
                    **_baseline_validation_fields(config),
                    "process_teardowns": [t.to_audit_dict() for t in gate_teardowns],
                    "worktree": preserved_worktree,
                    "evidence_path": evidence_path,
                    "evidence_unavailable": evidence_unavailable,
                    "message": setup_message,
                }

        # The gate runs in a worktree that does not survive this function, so
        # ``run_gate_full``'s own trace (written into that worktree) cannot be the
        # durable record. The full output is taken out by value instead.
        gate_full_output: list[str] = []
        gate_worktree_state: list[dict[str, object]] = []
        decision, error, output_tail, resolved_gate_cmd, gate_exit_code = run_gate_full(
            config,
            baseline_worktree,
            full_output=gate_full_output,
            process_teardowns=gate_teardowns,
            worktree_state_out=gate_worktree_state,
            label=GateLabel(
                purpose=BASELINE_GATE_PURPOSE,
                target="merge base",
                commit=merge_base_ref,
                worktree_path=str(baseline_worktree),
            ),
        )
        duration = time.monotonic() - started_monotonic
        finished_at = datetime.datetime.now(datetime.timezone.utc)
        exit_code = gate_exit_code if gate_exit_code is not None else 1
        if decision == "PASS" and error is None:
            exit_code = gate_exit_code if gate_exit_code is not None else 0
            return {
                "status": "pass",
                "passed": True,
                "exit_code": exit_code,
                "duration_seconds": round(duration, 2),
                "started_at": baseline_started_at,
                "finished_at": finished_at,
                "merge_base": merge_base_ref,
                **_baseline_validation_fields(config, command=resolved_gate_cmd),
                "process_teardowns": [t.to_audit_dict() for t in gate_teardowns],
                "decision": decision,
                "output_tail": output_tail,
                "worktree_state": gate_worktree_state[0] if gate_worktree_state else None,
                # Nothing failed, so nothing needed confirming; the keys are
                # present on every executed-gate record so a reader never has to
                # ask whether this is one of the kinds that says.
                "confirmation_attempted": False,
                "failure_reproduced": None,
                "message": (
                    "Baseline gate passed on sprint merge base "
                    f"{merge_base_ref} before dev iterations started"
                ),
            }

        # A verdict that decides whether work may begin is evidence about the
        # merge base only if it reproduces. Process-teardown and sandbox flakes
        # fail one invocation and pass the next against the identical tree, and
        # this gate's FAIL ends the sprint before any story starts — so the
        # failure is re-run once, immediately, in the same worktree at the same
        # commit, and only a failure that repeats is treated as the baseline's
        # answer (#2434). The re-run happens *before* the evidence capture and
        # worktree preservation below: a first run that does not reproduce
        # returns through the ordinary cleanup path and leaves nothing behind.
        initial_result = {
            "decision": decision,
            "error": error,
            "exit_code": exit_code,
            "output_tail": output_tail,
            "duration_seconds": round(duration, 2),
            "worktree_state": gate_worktree_state[0] if gate_worktree_state else None,
        }
        if on_confirmation_start is not None:
            on_confirmation_start()
        _log(
            f"Baseline gate failed on merge base {merge_base_ref}; re-running the identical "
            "gate once to confirm the failure reproduces before refusing the sprint"
        )
        confirm_full_output: list[str] = []
        confirm_worktree_state: list[dict[str, object]] = []
        (
            confirm_decision,
            confirm_error,
            confirm_tail,
            confirm_cmd,
            confirm_exit,
        ) = run_gate_full(
            config,
            baseline_worktree,
            full_output=confirm_full_output,
            process_teardowns=gate_teardowns,
            worktree_state_out=confirm_worktree_state,
            label=GateLabel(
                purpose=BASELINE_GATE_CONFIRM_PURPOSE,
                target="merge base",
                commit=merge_base_ref,
                worktree_path=str(baseline_worktree),
            ),
        )
        duration = time.monotonic() - started_monotonic
        finished_at = datetime.datetime.now(datetime.timezone.utc)
        confirmation_passed = confirm_decision == "PASS" and confirm_error is None
        confirm_exit_code = (
            confirm_exit if confirm_exit is not None else (0 if confirmation_passed else 1)
        )
        confirmation_result = {
            "decision": confirm_decision,
            "error": confirm_error,
            "exit_code": confirm_exit_code,
            "output_tail": confirm_tail,
            "worktree_state": confirm_worktree_state[0] if confirm_worktree_state else None,
        }
        if confirmation_passed:
            # The first run's output is still the only record of what failed, and
            # the operator has no other way to see it — the sprint is about to
            # proceed as though the failure never happened. Written under its own
            # filename so it can never be mistaken for a halting verdict's
            # evidence, and the worktree is *not* preserved: nothing halted.
            unreproduced_filename = (
                f"{evidence_filename.removesuffix('.txt')}-unreproduced-failure.txt"
            )
            evidence_path, evidence_unavailable = _write_baseline_gate_evidence(
                log_dir=sprint_log_dir,
                filename=unreproduced_filename,
                header_lines=[
                    f"# baseline gate {decision or 'ERROR'} on merge base {merge_base_ref}",
                    "# NOT REPRODUCED: an immediate re-run of the identical gate passed",
                    f"# gate command: {resolved_gate_cmd}",
                    f"# exit code: {exit_code}",
                    f"# worktree: {baseline_worktree}",
                ],
                full_output=gate_full_output[0] if gate_full_output else None,
            )
            message = (
                "Baseline gate failed and then passed on an immediate re-run of the identical "
                f"gate against merge base {merge_base_ref}; the failure did not reproduce, so "
                "it is not treated as evidence about the merge base and the sprint proceeds "
                f"(first run exit {exit_code}: {error or 'Gate returned FAIL'})"
            )
            message += _baseline_evidence_footer(
                worktree=None,
                evidence_path=evidence_path,
                evidence_unavailable=evidence_unavailable,
            )
            return {
                "status": "pass_unreproduced_failure",
                "passed": True,
                "exit_code": confirm_exit_code,
                "duration_seconds": round(duration, 2),
                "started_at": baseline_started_at,
                "finished_at": finished_at,
                "merge_base": merge_base_ref,
                **_baseline_validation_fields(config, command=confirm_cmd),
                "process_teardowns": [t.to_audit_dict() for t in gate_teardowns],
                "decision": confirm_decision,
                "output_tail": confirm_tail,
                "worktree_state": confirm_worktree_state[0] if confirm_worktree_state else None,
                "confirmation_attempted": True,
                "failure_reproduced": False,
                "initial_result": initial_result,
                "confirmation_result": confirmation_result,
                "evidence_path": evidence_path,
                "evidence_unavailable": evidence_unavailable,
                "message": message,
            }

        # Reproduced: the confirmation run is the verdict, and its output is what
        # the operator investigates.
        decision = confirm_decision
        error = confirm_error
        output_tail = confirm_tail
        resolved_gate_cmd = confirm_cmd
        exit_code = confirm_exit_code
        if confirm_full_output:
            gate_full_output = confirm_full_output
        message = (
            "Broken baseline: configured gate failed on sprint merge base "
            f"{merge_base_ref} before any dev work started ({error or 'Gate returned FAIL'})"
            " (failure reproduced on an immediate re-run of the identical gate)"
        )
        try:
            local_sha = subprocess.check_output(
                ["git", "rev-parse", base_branch],
                cwd=config.project_root,
                text=True,
            ).strip()
            origin_sha = subprocess.check_output(
                ["git", "rev-parse", f"origin/{base_branch}"],
                cwd=config.project_root,
                text=True,
            ).strip()
        except subprocess.CalledProcessError:
            local_sha = origin_sha = None
        if local_sha and origin_sha and local_sha != origin_sha:
            message += (
                f" (local {base_branch} is at {local_sha[:12]}, origin is at {origin_sha[:12]}; "
                f"local branch may be stale; run `git pull` on {base_branch} or omit --no-pull)"
            )
        # Capture before the finally block would have reclaimed the workspace,
        # and preserve the workspace itself: the excerpt below names which check
        # failed, the file and the worktree are what let the operator find out
        # why (#2160).
        preserved_worktree = str(baseline_worktree)
        evidence_path, evidence_unavailable = _write_baseline_gate_evidence(
            log_dir=sprint_log_dir,
            filename=evidence_filename,
            header_lines=[
                f"# baseline gate {decision or 'ERROR'} on merge base {merge_base_ref}",
                f"# gate command: {resolved_gate_cmd}",
                f"# exit code: {exit_code}",
                f"# worktree: {baseline_worktree}",
            ],
            full_output=gate_full_output[0] if gate_full_output else None,
        )
        message += _baseline_evidence_footer(
            worktree=preserved_worktree,
            evidence_path=evidence_path,
            evidence_unavailable=evidence_unavailable,
        )
        failing_target_extraction = extract_failed_tests(
            output_tail or "",
            config.validation.failed_test_pattern,
        )
        # The sha names where the gate ran; only the gate's own output names what
        # must change. ``run_gate_full`` leaves ``error`` None for a plain nonzero
        # exit, so without this the operator is handed a commit to investigate
        # when the remedy is one key in one file (#1980). Appended last, after the
        # sha, the stale-branch hint and the evidence pointers, and bounded so the
        # same string stays usable as a log line and as the raised RuntimeError
        # text — the unbounded record is the evidence file the pointers name.
        _diagnostic = _baseline_failure_diagnostic(output_tail)
        if _diagnostic:
            message += f"\n{_diagnostic}"
        return {
            "status": "fail",
            "passed": False,
            "exit_code": exit_code,
            "duration_seconds": round(duration, 2),
            "started_at": baseline_started_at,
            "finished_at": finished_at,
            "merge_base": merge_base_ref,
            **_baseline_validation_fields(config, command=resolved_gate_cmd),
            "process_teardowns": [t.to_audit_dict() for t in gate_teardowns],
            "decision": decision,
            "output_tail": output_tail,
            "worktree_state": confirm_worktree_state[0] if confirm_worktree_state else None,
            "confirmation_attempted": True,
            "failure_reproduced": True,
            "initial_result": initial_result,
            "confirmation_result": confirmation_result,
            "worktree": preserved_worktree,
            "evidence_path": evidence_path,
            "evidence_unavailable": evidence_unavailable,
            "failing_targets": list(failing_target_extraction.tests),
            "failing_target_extraction": {
                "source": failing_target_extraction.source,
                "format_recognized": failing_target_extraction.format_recognized,
            },
            "message": message,
        }
    finally:
        if preserved_worktree is None:
            _remove_baseline_temp_root(config.project_root, temp_root)


def _continuation_evidence(
    *,
    reexec: bool,
    live_story_slugs: "AbstractSet[str]",
    unresolved_slugs: "AbstractSet[str] | None" = None,
    registered_slugs: "AbstractSet[str] | None" = None,
) -> str | None:
    """Describe why this launch is a continuation of in-flight work, or None.

    A re-exec is not, on its own, evidence that work is in flight: the source
    change can be observed by the sprint's *own* first pull, before any story has
    started, and such a launch is still a genuine start. What distinguishes a
    continuation is observed live work — agent process groups this same pid
    spawned before the re-exec and that are still running, or stories the prior
    image had dispatched and never settled. Startup-only checks are skipped on
    exactly that evidence, never on the re-exec flag alone, so a launch that has
    not started anything keeps its full startup sequence.

    The two kinds of evidence are named apart because they claim different
    things. A process group is a process; an ownership record is a
    responsibility, and a story executing in-process when the exec fired has only
    the second (#2617). Reporting the second as the first would tell an operator
    a process is running when none is.

    Deliberately narrow: prior *recorded* outcomes are not used as evidence,
    because a sprint id is stable across separate invocations of the same sprint
    and would make an unrelated later run skip its baseline gate.

    Liveness that could not be *resolved* counts as evidence too, and is named as
    such: the gate's precondition is "no dev work has started", and an unreadable
    sidecar cannot establish that. Asserting the precondition anyway is the same
    empty-set-means-idle inference that produced #2079.
    """
    if not reexec:
        return None
    registered = frozenset(registered_slugs or ()) & frozenset(live_story_slugs)
    with_agents = frozenset(live_story_slugs) - registered
    parts: list[str] = []
    if with_agents:
        parts.append(
            "agent process groups still running for "
            f"{', '.join(sorted(with_agents))} after the re-exec"
        )
    if registered:
        parts.append(
            "stories this sprint had dispatched and not settled when the re-exec "
            f"fired: {', '.join(sorted(registered))}"
        )
    if unresolved_slugs:
        parts.append(
            "liveness unresolved (worktree present, agent record unreadable) for "
            f"{', '.join(sorted(unresolved_slugs))} after the re-exec"
        )
    if not parts:
        return None
    return "; ".join(parts)


def _skipped_baseline_gate(config: ForgeConfig, evidence: str) -> dict[str, object]:
    """The baseline-gate record for a run that legitimately did not run it.

    The baseline gate answers one question — was the merge base green *before any
    dev work started* — and a continuation cannot ask it: work has started, and
    the host is busy running it, so a failure would report a conclusion about the
    code drawn from a measurement of our own load. Skipping is recorded
    explicitly, with its evidence, rather than silently omitted.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "status": "skipped",
        "passed": True,
        "exit_code": 0,
        "duration_seconds": 0.0,
        "started_at": now,
        "finished_at": now,
        "merge_base": None,
        **_baseline_validation_fields(config),
        "skip_reason": "reexec_continuation",
        "skip_evidence": evidence,
        "message": (
            "Baseline gate skipped: this process is continuing an in-flight sprint "
            f"after a mid-run re-exec ({evidence}); the gate's precondition — no dev "
            "work started — no longer holds"
        ),
    }


def _agent_cost_tracking_warnings(config: ForgeConfig) -> list[str]:
    """Return sprint-start warnings for configured CLI agents with unknown cost.

    Whether an agent's spend is trackable is a property of its *transport*, not
    of a cli/provider field pair: a CLI transport that reports no usage is
    untracked regardless of which provider it belongs to. Identity (provider,
    model) and transport are carried separately here so the warning names both.
    """

    @dataclass(frozen=True)
    class _Agent:
        name: str
        transport_kind: str
        runner: str | None
        provider: str | None
        model: str
        api_fallback: object | None

    def _from_profile(profile: ModelProfile, name: str | None = None) -> _Agent:
        transport = profile.transport
        return _Agent(
            name=name or profile.name,
            transport_kind=profile.mode,
            runner=transport.runner if transport is not None else None,
            provider=profile.provider_family,
            model=profile.model,
            api_fallback=profile.api_fallback,
        )

    agents: list[_Agent] = [
        _from_profile(config.preflight_profile),
        _from_profile(config.dev_profile),
    ]

    if config.plan.enabled:
        agents.append(
            _Agent(
                name="planner",
                transport_kind=config.plan.mode,
                runner=config.plan.transport.runner if config.plan.transport else None,
                provider=config.plan.provider_family,
                model=config.plan.model,
                api_fallback=config.plan.api_fallback,
            )
        )

    if config.plan_agent_review.enabled:
        agents.extend(_from_profile(p) for p in config.plan_agent_review.profiles)

    agents.extend(_from_profile(p) for p in config.review_pool)

    if config.synthesis_profile is not None:
        agents.append(_from_profile(config.synthesis_profile))

    warnings: list[str] = []
    seen: set[tuple[str, str, str, str | None, str | None]] = set()
    for agent in agents:
        if agent.transport_kind != "cli" or not _cli_cost_untracked(agent.runner):
            continue
        fallback_provider = getattr(agent.api_fallback, "provider", None)
        fallback_model = getattr(agent.api_fallback, "model", None)
        key = (agent.name, agent.runner, agent.model, fallback_provider, fallback_model)
        if key in seen:
            continue
        seen.add(key)
        if agent.api_fallback is not None:
            warnings.append(
                f"⚠ CLI cost not tracked for {agent.name} ({agent.runner} CLI, {agent.model}); "
                f"API fallback to {fallback_provider}/{fallback_model} will be tracked "
                "if it triggers."
            )
            continue
        warnings.append(
            f"⚠ Cost not tracked for {agent.name} ({agent.runner} CLI, {agent.model}). "
            "Audit totals will exclude this agent's usage."
        )
    return warnings


def parse_manifest_story_refs(
    config: "ForgeConfig", manifest_path: Path
) -> tuple[list[str], dict[str, str]]:
    """Extract manifest slugs plus best-effort canonical refs.

    Returns ``([], {})`` if the manifest cannot be parsed or has no stories.
    Used for pre-launch conflict detection and operator guidance, so it stays
    best-effort and does not raise on invalid manifests.
    """
    try:
        with open(manifest_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            return [], {}
        stories = raw.get("stories") or raw.get("specs") or []
        if not isinstance(stories, list):
            return [], {}
        slugs: list[str] = []
        canonical_refs_by_slug: dict[str, str] = {}
        for entry in stories:
            if isinstance(entry, dict) and "issue" in entry:
                slug = entry.get("slug", f"issue-{entry['issue']}")
                slugs.append(slug)
                canonical_refs_by_slug[slug] = f"issue:{entry['issue']}"
            elif isinstance(entry, str):
                story_path = (config.project_root / entry).resolve()
                if story_path.exists():
                    task = _build_task_from_story(story_path)
                    slug = task.slug
                else:
                    # Fallback: use file stem as slug
                    slug = Path(entry).stem
                slugs.append(slug)
                canonical_refs_by_slug[slug] = entry
        return slugs, canonical_refs_by_slug
    except Exception:
        return [], {}


def parse_manifest_slugs(config: "ForgeConfig", manifest_path: Path) -> list[str]:
    """Extract story slugs from a sprint manifest without full validation."""
    slugs, _canonical_refs_by_slug = parse_manifest_story_refs(config, manifest_path)
    return slugs


#: A collision claim is held while its story is running past the plan gate.
CLAIM_IN_DEV = "in dev"
#: Held while the story's work is queued/pending a landing decision that this
#: run will still resolve — the gate can legitimately open once it resolves.
CLAIM_PENDING_LANDING = "landing pending"
#: Held because the story stopped without a landing verdict but its work was
#: preserved for an operator decision. Nothing in this run can resolve it, so
#: the files it planned to rewrite stay claimed until the sprint ends (#2234).
CLAIM_PRESERVED = "preserved for operator decision"

#: How often the scheduling loop wakes to service plan gates while stories are
#: in flight. A named constant rather than a literal in the wait() call so it is
#: one knob: the gate-service tests drive several ticks each, and at 2.0s that
#: made them the slowest tests in the suite — one of them 6.3s, past the
#: five-second per-test convention — for a duration none of them assert.
PLAN_GATE_TICK_SECONDS = 2.0

#: How often a queued PR whose claim is holding a sibling's plan gate is probed
#: while the scheduling loop is otherwise busy. Matches _poll_queued_pr's own
#: inter-poll sleep: the gate should open on the merge, not on its own timeout,
#: without turning the gate-service tick into a `gh pr view` per second.
_QUEUED_CLAIM_PROBE_SECONDS = 30.0


#: Live-state detail keys describing why a story's plan gate is being held.
#: Written while the hold is in force and cleared when it ends, so the status
#: view can tell a deliberate wait from a story that stopped moving (#2235).
GATE_HOLD_BLOCKERS_KEY = "collision_gate_blockers"
GATE_HOLD_FILES_KEY = "collision_gate_files"
GATE_HOLD_CLAIMS_KEY = "collision_gate_claims"
GATE_HOLD_DETAIL_KEYS = (
    GATE_HOLD_BLOCKERS_KEY,
    GATE_HOLD_FILES_KEY,
    GATE_HOLD_CLAIMS_KEY,
)


def _make_gate_hold_publisher(
    state_writer: "SprintStateWriter | None",
) -> "Callable[[str, dict | None], None]":
    """Return a ``gate_hold_fn`` that writes gate holds into the live state file.

    Gate servicing runs on a 2s tick; only *changes* are written, so a hold that
    lasts twenty minutes costs one state write, not six hundred. Updates go
    through ``detail_updates``, which merges — writing ``detail`` would replace
    the dict wholesale and erase co-resident keys such as the reviewer event
    timestamp the EVENT AGE column reads (#2235).

    This writes straight to ``SprintStateWriter``, which serializes on its own
    lock. It deliberately does *not* route through ``_make_worker_phase_fn``:
    that path takes the scheduler's ``phase_lock``, and a publisher that did so
    would couple every gate-service tick to the worker-state lock. See
    ``_release_plan_gates`` for the ordering rule that keeps this true.
    """
    published: dict[str, dict] = {}

    def _publish(slug: str, payload: dict | None) -> None:
        if state_writer is None:
            return
        if payload is None:
            # Only clear a hold this publisher actually wrote: every opened gate
            # calls through here, and most were never held.
            if published.pop(slug, None) is None:
                return
            state_writer.update(slug, detail_updates=dict.fromkeys(GATE_HOLD_DETAIL_KEYS, None))
            return
        if published.get(slug) == payload:
            return
        published[slug] = payload
        state_writer.update(slug, phase="PLAN_DONE", detail_updates=dict(payload))

    return _publish


def _release_plan_gates(
    plan_done: dict[str, str],
    file_footprints: dict[str, set[str]],
    plan_gates: dict[str, threading.Event],
    active: dict[str, object],
    phase_lock: threading.Lock,
    collision_claims: "dict[str, str] | None" = None,
    gate_hold_fn: "Callable[[str, dict | None], None] | None" = None,
) -> list[str]:
    """Check newly-planned stories and release their gates if no file overlap.

    Called from the scheduling loop — both the poll interval and after a future
    completes — to avoid deadlock when gated workers block in _run_fresh.

    *gate_hold_fn* publishes the reason a gate is being held into the story's
    live state — ``fn(slug, payload)`` while the hold is in force, ``fn(slug,
    None)`` once it ends. A gated worker emits no events of its own, so without
    this the wait is indistinguishable from a hang in the status view (#2235).

    Ordering rule: *gate_hold_fn* is never invoked while *phase_lock* is held.
    This function takes *phase_lock* only around the ``plan_done`` snapshot; all
    publish calls sit outside it. ``phase_lock`` is a plain non-reentrant
    ``Lock``, and the worker state path (``_make_worker_phase_fn``) takes it to
    write live state — so a publisher wired through that path, or a publish call
    moved under the snapshot's ``with`` block, would deadlock the scheduler.
    ``test_publisher_may_take_phase_lock_without_deadlocking`` enforces this
    mechanically rather than leaving it to this comment.

    *collision_claims* maps slug -> claim reason for stories that are past their
    plan gate. A claim, not worker liveness, is what holds a file: a story that
    escalates with its worktree preserved is no longer ``active`` but its work
    is still expected to land, so releasing the gate on that transition would
    hand the waiting story a base the pending decision is about to rewrite
    (#2234).

    Returns the slugs whose gate can never open in this run — every file they
    are blocked on is claimed by preserved, undecided work — for the caller to
    stand down. Those slugs are left in *plan_gates* for the caller to remove.
    """
    claims = collision_claims if collision_claims is not None else {}

    def _blockers(slug: str, footprint: set[str]) -> dict[str, set[str]]:
        """Map blocker slug -> overlapping files for stories holding *footprint*.

        A story holds its files while it is running past its own gate (``active``)
        *or* while it still holds a collision claim.
        """
        held: dict[str, set[str]] = {}
        for other_slug, other_files in file_footprints.items():
            if other_slug == slug or other_slug in plan_gates:
                continue
            if other_slug not in active and other_slug not in claims:
                continue
            shared = footprint & other_files
            if shared:
                held[other_slug] = shared
        return held

    def _publish_hold(slug: str, blockers: "dict[str, set[str]] | None") -> None:
        """Record (or clear) why *slug*'s gate is held, for the status view."""
        if gate_hold_fn is None:
            return
        if not blockers:
            gate_hold_fn(slug, None)
            return
        gate_hold_fn(
            slug,
            {
                GATE_HOLD_BLOCKERS_KEY: sorted(blockers),
                GATE_HOLD_FILES_KEY: sorted(set().union(*blockers.values())),
                GATE_HOLD_CLAIMS_KEY: {
                    blocker: claims.get(blocker, CLAIM_IN_DEV) for blocker in sorted(blockers)
                },
            },
        )

    with phase_lock:
        pd_snapshot = dict(plan_done)

    for pd_slug in pd_snapshot:
        if pd_slug not in file_footprints:
            ws_path = Path(pd_snapshot[pd_slug])
            footprint = _extract_plan_footprint(ws_path)
            file_footprints[pd_slug] = footprint

            # Check overlap with stories already past their gate (in DEV) or
            # still claiming their files after leaving the active set.
            blockers = _blockers(pd_slug, footprint)
            if blockers:
                overlap = set().union(*blockers.values())
                _log(
                    f"WARNING: {pd_slug} overlaps with active stories on: "
                    f"{', '.join(sorted(overlap))}"
                )
                _publish_hold(pd_slug, blockers)
            else:
                gate = plan_gates.pop(pd_slug, None)
                if gate is not None:
                    gate.set()
                claims[pd_slug] = CLAIM_IN_DEV
                _publish_hold(pd_slug, None)

    # Re-check deferred gates (conflicting story may have finished)
    stood_down: list[str] = []
    for deferred_slug, gate in list(plan_gates.items()):
        if deferred_slug not in file_footprints:
            continue
        blockers = _blockers(deferred_slug, file_footprints[deferred_slug])
        if not blockers:
            gate.set()
            del plan_gates[deferred_slug]
            claims[deferred_slug] = CLAIM_IN_DEV
            _publish_hold(deferred_slug, None)
            continue
        # Every blocker is preserved, undecided work that no longer has a
        # worker or a landing path in this run: the gate cannot open here.
        # Waiting forever and releasing onto a doomed base are both wrong —
        # stand the story down instead so the operator's decision on the
        # preserved work comes first.
        if all(
            blocker not in active and claims.get(blocker) == CLAIM_PRESERVED
            for blocker in blockers
        ):
            overlap = sorted(set().union(*blockers.values()))
            _log(
                f"STAND DOWN {deferred_slug}: blocked only by preserved, undecided "
                f"work from {', '.join(sorted(blockers))} on {', '.join(overlap)} — "
                "the collision gate cannot open in this run"
            )
            _publish_hold(deferred_slug, None)
            stood_down.append(deferred_slug)
            continue
        _publish_hold(deferred_slug, blockers)
    return stood_down


def _validate_story_paths(config: ForgeConfig, manifest_path: Path) -> list[Path]:
    """Backward-compatible shim for tests that patch story path validation.

    The sprint runner now resolves manifests through ``resolve_from_manifest`` and
    no longer needs a separate validation pass here, but daemon tests still patch
    this symbol to isolate ``run_sprint``. Keep a no-op helper so those patches
    remain valid without affecting runtime behavior.
    """
    del config, manifest_path
    return []


def _extract_plan_footprint(workspace_path: Path | None) -> set[str]:
    """Extract the set of files referenced in a plan's steps.

    Reads .forge/plan.md from the workspace, parses YAML plan data, and collects
    file paths from all steps. Returns empty set on any parse failure (best-effort).
    """
    from ..artifacts import PLAN_PATH  # noqa: PLC0415
    from ..task.plan_parser import parse_plan_output  # noqa: PLC0415

    if workspace_path is None:
        return set()
    plan_file = workspace_path / PLAN_PATH
    if not plan_file.exists():
        return set()
    try:
        text = plan_file.read_text(encoding="utf-8")
        plan_data = parse_plan_output(text)
        if plan_data is None:
            return set()
        files: set[str] = set()
        for step in plan_data.get("steps", []):
            files.update(step.get("files", []))
        return files
    except Exception:
        return set()


def _populate_resumed_story_footprint(
    slug: str,
    state: CoordinatorState,
    workspace_path: Path,
) -> CoordinatorState:
    """Populate preflight_likely_files from an existing plan.md for resumed stories."""
    if state.preflight_likely_files:
        return state

    files = sorted(_extract_plan_footprint(workspace_path))
    state.preflight_likely_files = files
    _log(
        f"Resumed story {slug}: registered {len(files)} file(s) from plan.md "
        f"for collision detection: {files}"
    )
    return state


def _recovered_story_footprint(
    slug: str,
    project_root: Path | None,
    task: TaskStory | None,
) -> tuple[list[str] | None, str]:
    """Return ``(likely_files, source_reason)`` from the durable resume record.

    A story already past preflight gets a *fresh* live preflight attempt on
    resume, and for a preserved, diverged worktree that attempt routinely fails
    or drift-classifies, leaving ``preflight_likely_files=None``. The original
    run's real, evidence-backed footprint is still on disk in the coordinator's
    resume record, so it is read back here rather than re-derived from the
    strictly weaker plan.md scrape (#2610). ``likely_files`` is None when no
    usable footprint could be recovered; the reason names why, so an empty
    footprint in a real repro can be attributed to a source instead of guessed.
    """
    if project_root is None:
        return None, "no_project_root"
    from ..coordinator.resume_persistence import (  # noqa: PLC0415
        apply_resume_record_to_state,
        load_resume_record,
        validate_resume_record,
    )

    record = load_resume_record(project_root, slug)
    if record is None:
        return None, "no_record"
    story_content: str | None = None
    if task is not None:
        try:
            if task.story_text is not None:
                story_content = task.story_text
            elif task.story_path is not None:
                story_content = load_story(Path(task.story_path))
        except OSError:
            story_content = None
    # story_content=None is accepted by validate_resume_record as
    # ``unverified_story``: the record still names phases that demonstrably ran,
    # and discarding it would throw away a usable footprint for every
    # issue-backed story that carries no story file.
    usable, reason = validate_resume_record(record, story_content=story_content)
    if not usable:
        return None, reason
    probe = CoordinatorState()
    try:
        apply_resume_record_to_state(probe, record)
    except Exception:  # pragma: no cover - defensive; record is best-effort data
        return None, "record_unusable"
    files = probe.preflight_likely_files
    if not files:
        return None, f"{reason}:no_likely_files"
    return sorted({str(f) for f in files}), reason


def _register_resumed_story_footprints(
    triages: dict[str, StoryTriage],
    preflight_states: dict[str, CoordinatorState],
    *,
    project_root: Path | None = None,
    tasks: list[TaskStory] | None = None,
) -> dict[str, CoordinatorState]:
    """Ensure resumed dev/review stories contribute likely_files to collision detection.

    Footprint sources, in precedence order: this run's own live preflight
    result, the durable coordinator resume record from the run that first
    scheduled the story, then the story's plan.md. Which one populated the
    footprint is logged, because a collision edge that silently went missing is
    the failure this path exists to prevent (#2610).
    """
    tasks_by_slug = {t.slug: t for t in (tasks or [])}
    for triage in triages.values():
        if triage.action not in {"review", "dev"} or triage.worktree_path is None:
            continue
        state = preflight_states.get(triage.slug)
        if state is None:
            state = CoordinatorState()
            preflight_states[triage.slug] = state
        if state.preflight_likely_files:
            # A live preflight that produced a concrete claim this run is
            # authoritative; nothing recorded displaces it.
            _log(
                f"Resumed story {triage.slug}: footprint source=live preflight "
                f"({len(state.preflight_likely_files)} file(s))"
            )
            continue
        recovered, reason = _recovered_story_footprint(
            triage.slug, project_root, tasks_by_slug.get(triage.slug)
        )
        if recovered:
            state.preflight_likely_files = recovered
            _log(
                f"Resumed story {triage.slug}: footprint source=resume record "
                f"({reason}); registered {len(recovered)} file(s) for collision "
                f"detection: {recovered}"
            )
            continue
        _log(
            f"Resumed story {triage.slug}: no durable footprint available "
            f"({reason}); falling back to plan.md"
        )
        _populate_resumed_story_footprint(triage.slug, state, triage.worktree_path)
        if not state.preflight_likely_files:
            _log(
                f"Resumed story {triage.slug}: footprint source=none — no live "
                f"preflight, no resume record ({reason}), no plan.md files; this "
                f"story makes no file claim and will not be serialized against "
                f"another story that makes none either"
            )
    return preflight_states


def _run_fresh(
    config: ForgeConfig,
    task: TaskStory,
    sprint_run_id: str,
    sprint_name: str,
    interactive: bool,
    notify: bool,
    effective_auto_merge: bool,
    state_update_fn: "Callable[[dict], None] | None",
    no_pull: bool,
    plan_gate: "threading.Event | None",
    preflight_states: dict[str, CoordinatorState] | None = None,
    *,
    stop_event: "threading.Event | None" = None,
    base_lands_locally: bool | None = None,
    lands_in_project_root: bool | None = None,
) -> CoordinatorResult:
    """Run a fresh story, optionally splitting at PLAN_REVIEW for overlap gating."""
    if plan_gate is None:
        # Synthetic issue-backed tasks may be created before query materialization.
        # Fail them explicitly at the sprint seam instead of relying on downstream
        # task text reads inside run_task().
        if task.story_path is None and task.github_issue is not None:
            if task.story_text is None:
                return CoordinatorResult(
                    success=False,
                    phase=Phase.PREFLIGHT,
                    state=CoordinatorState(
                        phase=Phase.PREFLIGHT,
                        started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        workspace_path=None,
                        log_dir=None,
                        error="Issue-backed story has no materialized story text",
                        error_type="ValueError",
                    ),
                    message="Issue-backed story has no materialized story text",
                )
            return run_task(
                config,
                task,
                interactive=interactive,
                auto_merge=effective_auto_merge,
                notify=notify,
                sprint_name=sprint_name,
                state_update_fn=state_update_fn,
                no_pull=no_pull,
                cached_preflight_state=(preflight_states or {}).get(task.slug),
                defer_landing=True,
                stop_event=stop_event,
                base_lands_locally=base_lands_locally,
                lands_in_project_root=lands_in_project_root,
            )
        return run_task(
            config,
            task,
            interactive=interactive,
            auto_merge=effective_auto_merge,
            notify=notify,
            sprint_name=sprint_name,
            state_update_fn=state_update_fn,
            no_pull=no_pull,
            cached_preflight_state=(preflight_states or {}).get(task.slug),
            defer_landing=True,
            stop_event=stop_event,
            base_lands_locally=base_lands_locally,
            lands_in_project_root=lands_in_project_root,
        )

    # Phase 1: run through PLAN only
    plan_result = run_task(
        config,
        task,
        interactive=interactive,
        auto_merge=effective_auto_merge,
        notify=notify,
        sprint_name=sprint_name,
        state_update_fn=state_update_fn,
        no_pull=no_pull,
        stop_phase=Phase.PLAN_REVIEW,
        cached_preflight_state=(preflight_states or {}).get(task.slug),
        defer_landing=True,
        stop_event=stop_event,
        base_lands_locally=base_lands_locally,
        lands_in_project_root=lands_in_project_root,
    )

    if not plan_result.success:
        return plan_result

    workspace_path = plan_result.state.workspace_path
    if workspace_path is None:
        return plan_result

    # Signal plan completion so scheduler can check footprints
    if state_update_fn is not None:
        state_update_fn(
            {
                "spec": task.slug,
                "phase": "PLAN_DONE",
                "workspace_path": str(workspace_path),
            }
        )

    # Wait for scheduler to release the gate (with safety timeout)
    plan_gate.wait(timeout=7200)

    # The scheduler also opens the gate to *stop* a worker — on a stand-down
    # (blocked only by preserved work that cannot land in this run), an auth
    # abort, or a deadline. It sets stop_event first, so an open gate plus a set
    # stop_event means "come back out", not "proceed into DEV" (#2234).
    if stop_event is not None and stop_event.is_set():
        _log(f"{task.slug}: plan gate opened for shutdown — not entering DEV")
        return plan_result

    # Phase 2: continue from DEV
    return run_from_dev(
        config,
        task,
        workspace_path,
        interactive=interactive,
        auto_merge=effective_auto_merge,
        notify=notify,
        sprint_name=sprint_name,
        state_update_fn=state_update_fn,
        no_pull=no_pull,
        cached_preflight_state=(preflight_states or {}).get(task.slug),
        defer_landing=True,
        stop_event=stop_event,
        base_lands_locally=base_lands_locally,
        lands_in_project_root=lands_in_project_root,
    )


def _run_single_story(
    config: ForgeConfig,
    task: TaskStory,
    triage: "StoryTriage | None",
    sprint_run_id: str,
    sprint_name: str,
    interactive: bool,
    notify: bool,
    resume: bool,
    effective_auto_merge: bool,
    state_update_fn: "Callable[[dict], None] | None",
    no_pull: bool = False,
    plan_gate: "threading.Event | None" = None,
    preflight_states: dict[str, CoordinatorState] | None = None,
    stop_event: "threading.Event | None" = None,
    base_lands_locally: bool | None = None,
    lands_in_project_root: bool | None = None,
) -> "tuple[TaskStory, CoordinatorResult, float, datetime.datetime, datetime.datetime]":
    """Execute a single story and return (task, result, elapsed, started_at, finished_at).

    Designed to run in a worker thread. Dispatches to run_task / run_from_review /
    run_from_dev based on triage (resume mode) or always run_task (fresh mode).

    When *plan_gate* is provided (parallel overlap detection), fresh runs are split:
    1. run_task with stop_phase=PLAN_REVIEW
    2. Signal PLAN_DONE via state_update_fn
    3. Wait on plan_gate for scheduler release
    4. run_from_dev to continue
    """
    started_at = datetime.datetime.now(datetime.timezone.utc)
    set_worker_slug(task.slug)
    workspace_path = config.project_root / config.workspace.path_pattern.format(slug=task.slug)

    # Whether the project-root forge.yaml still matches what this sprint pinned.
    # The story runs under the pin either way; what changes is that the boundary
    # between stories that ran under different project-root content is recorded
    # instead of being reconstructed later from what the runs happened to log
    # (#1980).
    _drift = config_snapshot_mod.check_drift(
        config_snapshot_mod.active_snapshot(), story=task.slug
    )
    if _drift is not None:
        _log(f"⚠ {config_snapshot_mod.describe_drift(_drift)}")

    if state_update_fn is not None:
        state_update_fn({"spec": task.slug, "phase": "STARTING"})

    try:
        if resume and triage is not None:
            if triage.action == "review" and triage.worktree_path is not None:
                result = run_from_review(
                    config,
                    task,
                    triage.worktree_path,
                    interactive=interactive,
                    auto_merge=effective_auto_merge,
                    notify=notify,
                    sprint_name=sprint_name,
                    state_update_fn=state_update_fn,
                    no_pull=no_pull,
                    cached_preflight_state=(preflight_states or {}).get(task.slug),
                    defer_landing=True,
                    stop_event=stop_event,
                    base_lands_locally=base_lands_locally,
                    lands_in_project_root=lands_in_project_root,
                )
            elif triage.action == "dev" and triage.worktree_path is not None:
                result = run_from_dev(
                    config,
                    task,
                    triage.worktree_path,
                    interactive=interactive,
                    auto_merge=effective_auto_merge,
                    notify=notify,
                    sprint_name=sprint_name,
                    state_update_fn=state_update_fn,
                    no_pull=no_pull,
                    cached_preflight_state=(preflight_states or {}).get(task.slug),
                    defer_landing=True,
                    stop_event=stop_event,
                    base_lands_locally=base_lands_locally,
                    lands_in_project_root=lands_in_project_root,
                    # Why this story is entering at DEV, in the form the phase
                    # can act on. The triage reason string stays the sprint
                    # log's record; the structured outcome is what tells a dev
                    # agent the gate ran out of time rather than failed (#2796).
                    entry_gate_outcome=triage.gate_outcome,
                )
            else:
                result = _run_fresh(
                    config,
                    task,
                    sprint_run_id,
                    sprint_name,
                    interactive,
                    notify,
                    effective_auto_merge,
                    state_update_fn,
                    no_pull,
                    plan_gate,
                    preflight_states,
                    stop_event=stop_event,
                    base_lands_locally=base_lands_locally,
                    lands_in_project_root=lands_in_project_root,
                )
        else:
            result = _run_fresh(
                config,
                task,
                sprint_run_id,
                sprint_name,
                interactive,
                notify,
                effective_auto_merge,
                state_update_fn,
                no_pull,
                plan_gate,
                preflight_states,
                stop_event=stop_event,
                base_lands_locally=base_lands_locally,
                lands_in_project_root=lands_in_project_root,
            )
    except AdvisoryArtifactError as exc:
        # Second of the two layers that keep a shared-resource failure off the
        # story's record (#2107). Both are pinned by tests:
        #
        #   1. Containment at the call site — coordinator.validate_phase absorbs
        #      a failed advisory update, so the story keeps its real phase,
        #      audit, and cost. Pinned by test_coord_convention_parallel.py::
        #      test_advisory_persistence_failure_does_not_cost_the_story_its_result.
        #   2. This worker boundary — whatever the call site did not contain must
        #      still be attributed to the infrastructure. Pinned by
        #      test_sprint_infrastructure_attribution.py.
        #
        # Layer 1 handles today's only raise site, which is why layer 2 does not
        # fire in the current call graph. It is retained deliberately: the
        # blanket handler immediately below converts *any* escaping exception
        # into this story's ESCALATE verdict at cost_usd 0.0 — precisely the
        # misattribution this issue reports — and once the exception has left the
        # coordinator, nothing else can tell the two apart.
        _log(f"ERROR {task.slug}: shared infrastructure failure: {exc}")
        message = f"Shared infrastructure failure (advisory artifact): {exc}"
        failure_record = exc.as_failure_record()
        failure_state = CoordinatorState(
            phase=Phase.ESCALATE,
            started_at=started_at.isoformat(),
            workspace_path=workspace_path,
            log_dir=_make_story_log_dir(config, task.slug, sprint_name),
            error=message,
            error_type=ERROR_TYPE_INFRASTRUCTURE_ABORT,
        )
        failure_state.infrastructure_failure = {"message": message, **failure_record}
        failure_state.shared_infrastructure_failures.append(failure_record)
        failure_state.run_id = failure_state.run_id or _generate_run_id()
        failure_state.abnormal_termination = build_abnormal_cause(
            kind=ABNORMAL_SHARED_INFRASTRUCTURE,
            cause=message,
            error_type=ERROR_TYPE_INFRASTRUCTURE_ABORT,
            run_id=failure_state.run_id,
            source="sprint.runner:worker-shared-infrastructure",
        )
        result = CoordinatorResult(
            success=False,
            phase=Phase.ESCALATE,
            state=failure_state,
            message=message,
            infrastructure_failure=True,
        )
    except SubstrateLockTimeoutError as exc:
        # The sprint decided how many workers run at once, so the contention
        # that decision creates on the shared audit substrate is the sprint's,
        # not this story's (#2906). Without this clause the exception falls
        # into the blanket handler below and reaches the operator as
        # `unknown_needs_rca` — a claim that no cause was determined, about a
        # failure that arrived carrying its own description.
        _log(f"ERROR {task.slug}: shared infrastructure failure: {exc}")
        message = f"Shared infrastructure failure (audit substrate lock contention): {exc}"
        failure_record = {
            "component": "audit_substrate_lock",
            "path": str(audit_substrate_path(config.project_root)),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        failure_state = CoordinatorState(
            phase=Phase.ESCALATE,
            started_at=started_at.isoformat(),
            workspace_path=workspace_path,
            log_dir=_make_story_log_dir(config, task.slug, sprint_name),
            error=message,
            error_type=ERROR_TYPE_INFRASTRUCTURE_ABORT,
        )
        failure_state.infrastructure_failure = {"message": message, **failure_record}
        failure_state.shared_infrastructure_failures.append(failure_record)
        failure_state.run_id = failure_state.run_id or _generate_run_id()
        failure_state.abnormal_termination = build_abnormal_cause(
            kind=ABNORMAL_SHARED_INFRASTRUCTURE,
            cause=message,
            error_type=ERROR_TYPE_INFRASTRUCTURE_ABORT,
            run_id=failure_state.run_id,
            source="sprint.runner:worker-shared-infrastructure",
        )
        result = CoordinatorResult(
            success=False,
            phase=Phase.ESCALATE,
            state=failure_state,
            message=message,
            infrastructure_failure=True,
        )
    except Exception as exc:
        _log(f"ERROR {task.slug}: worker thread raised {type(exc).__name__}: {exc}")
        failure_state = CoordinatorState(
            phase=Phase.ESCALATE,
            started_at=started_at.isoformat(),
            workspace_path=workspace_path,
            log_dir=_make_story_log_dir(config, task.slug, sprint_name),
            error=f"Worker exception: {exc}",
            error_type=type(exc).__name__,
        )
        # A worker that raises here never reached audit finalization, so this is
        # the only place the cause can be recorded as structured telemetry. The
        # run id is synthesized when the exception preceded one, so the record is
        # addressable as a run instead of dissolving into the summary (#2030).
        failure_state.run_id = failure_state.run_id or _generate_run_id()
        failure_state.abnormal_termination = build_abnormal_cause(
            kind=ABNORMAL_WORKER_EXCEPTION,
            cause=f"Worker exception: {exc}",
            error_type=type(exc).__name__,
            run_id=failure_state.run_id,
            source="sprint.runner:worker-body",
        )
        result = CoordinatorResult(
            success=False,
            phase=Phase.ESCALATE,
            state=failure_state,
            message=f"Worker thread raised {type(exc).__name__}: {exc}",
        )

    finished_at = datetime.datetime.now(datetime.timezone.utc)
    set_worker_slug("")
    elapsed = (finished_at - started_at).total_seconds()
    return task, result, elapsed, started_at, finished_at


def _batch_member_failure(
    config: ForgeConfig,
    task: TaskStory,
    sprint_name: str,
    started_at: datetime.datetime,
    message: str,
) -> CoordinatorResult:
    """Terminal result for a member whose group's shared dev pass never delivered.

    The member is not silently dropped and not credited with the leader's
    verdict: it gets its own failed row naming the group, so an operator reads
    "this story was batched and the batch failed" rather than an unexplained
    zero-cost escalation.
    """
    state = CoordinatorState(
        phase=Phase.ESCALATE,
        started_at=started_at.isoformat(),
        workspace_path=config.project_root / config.workspace.path_pattern.format(slug=task.slug),
        log_dir=_make_story_log_dir(config, task.slug, sprint_name),
        error=message,
        error_type="BatchDevFailed",
    )
    state.preflight_batch_group = task.batch_group
    return CoordinatorResult(
        success=False,
        phase=Phase.ESCALATE,
        state=state,
        message=message,
    )


def _run_batch_group(
    config: ForgeConfig,
    leader_task: TaskStory,
    member_tasks: list[TaskStory],
    sprint_run_id: str,
    sprint_name: str,
    interactive: bool,
    notify: bool,
    effective_auto_merge: bool,
    state_update_fns: "dict[str, Callable[[dict], None] | None]",
    no_pull: bool,
    preflight_states: dict[str, CoordinatorState] | None,
    stop_event: "threading.Event | None",
    *,
    base_lands_locally: bool | None = None,
    lands_in_project_root: bool | None = None,
) -> "dict[str, tuple[TaskStory, CoordinatorResult, float, datetime.datetime, datetime.datetime]]":
    """Run one cost-aware batch group: one dev pass, per-story review.

    Returns one result tuple per member slug — the same shape a single-story
    worker returns, one entry per original story. That is the whole point of the
    primitive: the *implementation* is shared to amortise orchestration cost,
    but nothing else is. Each member keeps its own state row, review verdict,
    findings, cost, outcome, and audit record.

    Shape of the run:

    1. The leader story runs a normal coordinator pass (workspace, preflight
       cache, DEV, VALIDATE, REVIEW) on its own worktree and branch, except that
       its DEV prompt carries every member's spec (``batch_members``).
    2. Each non-leader member is then reviewed *on that same worktree*, against
       its own spec, via ``run_review_only``. Its verdict, findings, and cost are
       its own.

    Landing stays the leader's job: one branch carries the group's commits, so
    only the leader returns ``pending_integration``. Members return with no
    landing status, which the scheduler classifies as DONE-without-merge — their
    code lands with the leader's branch.
    """
    started_at = datetime.datetime.now(datetime.timezone.utc)
    group_id = leader_task.batch_group or "batch"
    results: dict[
        str, tuple[TaskStory, CoordinatorResult, float, datetime.datetime, datetime.datetime]
    ] = {}

    _log(
        f"BATCH {group_id}: one dev pass for "
        f"{', '.join(t.slug for t in [leader_task, *member_tasks])}"
    )

    def _broadcast(update: dict) -> None:
        """Mirror shared-dev-pass phase updates onto every member's live row.

        All members really are in DEV at that moment; showing only the leader as
        running would make the others read as idle for the length of the pass.

        The cost in a mirrored update is the *group's* one spend, shown on each
        member's row. Every mirror but the leader's is marked as such, so a
        sprint charging in-flight spend to its cap counts that one pass once
        rather than once per member (#2547).
        """
        for slug, fn in state_update_fns.items():
            if fn is None:
                continue
            mirrored = {**update, "spec": slug}
            if slug != leader_task.slug:
                mirrored["cost_mirrored"] = True
            fn(mirrored)

    leader_task_dispatch, leader_result, leader_elapsed, leader_t0, leader_t1 = _run_single_story(
        config,
        leader_task,
        None,
        sprint_run_id,
        sprint_name,
        interactive,
        notify,
        False,
        effective_auto_merge,
        _broadcast,
        no_pull,
        None,
        preflight_states,
        stop_event,
        base_lands_locally=base_lands_locally,
        lands_in_project_root=lands_in_project_root,
    )
    leader_result.state.preflight_batch_group = group_id
    results[leader_task.slug] = (
        leader_task_dispatch,
        leader_result,
        leader_elapsed,
        leader_t0,
        leader_t1,
    )

    workspace_path = leader_result.state.workspace_path
    if not leader_result.success or workspace_path is None:
        message = (
            f"batch group {group_id}: shared dev pass on {leader_task.slug} did not complete "
            f"({leader_result.message})"
        )
        for member in member_tasks:
            finished = datetime.datetime.now(datetime.timezone.utc)
            results[member.slug] = (
                member,
                _batch_member_failure(config, member, sprint_name, started_at, message),
                (finished - started_at).total_seconds(),
                started_at,
                finished,
            )
        return results

    branch_name = leader_result.state.branch_name or config.workspace.branch_pattern.format(
        slug=leader_task.slug
    )
    # The shared dev pass produced one handoff covering every member, with each
    # commit carrying the slug of the story it implements (the per-story handoff
    # contract in task/dev_prompts.py). Review needs it to judge each member
    # against that member's own commits rather than the branch's combined diff,
    # which is the group's change and not any one story's (#2525).
    # The context is built unconditionally: every member here is on a shared
    # branch whether or not the dev pass left a handoff, and it is membership
    # that decides how the member is grounded. Passing nothing when the handoff
    # is absent would silently return the member to the branch diff.
    batch_context = BatchReviewContext(dev_handoff=latest_dev_handoff(leader_result.state))
    if batch_context.dev_handoff is None:
        _log(
            f"BATCH {group_id}: shared dev pass left no per-story handoff — member "
            f"reviews cannot attribute commits and will treat every finding as "
            f"unverifiable against the member's own change"
        )

    def _cancelled_batch_member_result(
        member: TaskStory, started_at: datetime.datetime
    ) -> CoordinatorResult:
        reason, error_type = cancel_cause(stop_event)
        return _abnormal_story_result(
            member.slug,
            config=config,
            sprint_name=sprint_name,
            started_at=started_at,
            error=reason,
            error_type=error_type,
            message=reason,
        )

    for member in member_tasks:
        member_t0 = datetime.datetime.now(datetime.timezone.utc)
        set_worker_slug(member.slug)
        member_fn = state_update_fns.get(member.slug)
        if member_fn is not None:
            member_fn({"spec": member.slug, "phase": "REVIEW"})
        review_started = False
        try:
            if stop_event is not None and stop_event.is_set():
                member_result = _cancelled_batch_member_result(member, member_t0)
            else:
                review_started = True
                member_result = run_review_only(
                    config,
                    member,
                    workspace_path,
                    notify=notify,
                    sprint_name=sprint_name,
                    branch_name=branch_name,
                    batch_context=batch_context,
                )
        except Exception as exc:
            _log(f"ERROR {member.slug}: batch review raised {type(exc).__name__}: {exc}")
            member_result = _batch_member_failure(
                config,
                member,
                sprint_name,
                member_t0,
                f"batch group {group_id}: per-story review raised {type(exc).__name__}: {exc}",
            )
        finally:
            set_worker_slug("")
        if review_started and member_fn is not None:
            member_update: dict[str, object] = {
                "spec": member.slug,
                "coordinator_state": member_result.state,
            }
            measured_cost = optional_cost(
                getattr(member_result.state, "total_cost_measured", None)
            )
            if measured_cost is not None:
                member_update["cost_usd"] = measured_cost
            member_fn(member_update)
        member_result.state.preflight_batch_group = group_id
        # The gate ran once, on the shared worktree. Carry its result onto every
        # member so a batched story's audit shows the validation it actually
        # passed instead of an empty VALIDATE section (conventions #6).
        member_result.state.gate_decisions = list(leader_result.state.gate_decisions)
        member_result.state.gate_runs = leader_result.state.gate_runs
        member_result.state.last_gate_commit = leader_result.state.last_gate_commit
        member_result.state.last_gate_decision = leader_result.state.last_gate_decision
        # Including which profile produced it and what authority it carried: a
        # member audit that showed the decision without its provenance would be
        # the one place a verdict's standing is unreadable (#2358).
        member_result.state.validation_runs = list(leader_result.state.validation_runs)
        # Members land with the leader's branch; the scheduler must not try to
        # merge a branch that only exists for the leader.
        member_result.landing_status = None
        member_t1 = datetime.datetime.now(datetime.timezone.utc)
        results[member.slug] = (
            member,
            member_result,
            (member_t1 - member_t0).total_seconds(),
            member_t0,
            member_t1,
        )

    return results


def _run_inherited_story(
    config: ForgeConfig,
    task: TaskStory,
    triage: "StoryTriage | None",
    sprint_run_id: str,
    sprint_name: str,
    interactive: bool,
    notify: bool,
    resume: bool,
    effective_auto_merge: bool,
    state_update_fn: "Callable[[dict], None] | None",
    no_pull: bool = False,
    plan_gate: "threading.Event | None" = None,
    preflight_states: dict[str, CoordinatorState] | None = None,
    stop_event: "threading.Event | None" = None,
    base_lands_locally: bool | None = None,
    lands_in_project_root: bool | None = None,
    *,
    canonical_ref: str,
    quiesce_timeout: float,
) -> "tuple[TaskStory, CoordinatorResult, float, datetime.datetime, datetime.datetime]":
    """Resume a story whose agent process group survived a mid-run re-exec.

    Occupies a worker slot exactly like a normal dispatch — which is honest, the
    story *is* consuming one — and does three things before handing off to
    :func:`_run_single_story`:

    1. Waits for the inherited agent group to finish. Two agents must never write
       to one worktree, and nothing else can adopt this group: its sidecar names
       this pid as owner, so the moment this process exits a later invocation's
       orphan reaper will kill it.
    2. Reclaims the group if the wait overruns, so an agent that is wedged cannot
       hold the story hostage past the worker timeout.
    3. Triages the worktree *then*, not at sprint start — the re-entry point
       (review / dev / fresh) is only knowable once the agent has stopped
       writing.

    The worker deadline the scheduler enforces covers this whole call, so
    ``quiesce_timeout`` is deliberately a fraction of it: the story must still
    have time to actually run after the wait.
    """
    from .live_stories import await_inherited_agents, reclaim_inherited_agents  # noqa: PLC0415

    quiesced = await_inherited_agents(
        task.slug,
        project_root=config.project_root,
        path_pattern=config.workspace.path_pattern,
        timeout=quiesce_timeout,
        stop_event=stop_event,
        log=_log,
    )
    if not quiesced:
        killed = reclaim_inherited_agents(
            task.slug,
            project_root=config.project_root,
            path_pattern=config.workspace.path_pattern,
        )
        _log(
            f"IN-FLIGHT {task.slug}: inherited agent still running after "
            f"{int(quiesce_timeout)}s — terminated process group(s) "
            f"{', '.join(str(p) for p in killed) or 'none'} and resuming from whatever "
            "it committed"
        )

    fresh_triage = triage
    if fresh_triage is None:
        try:
            fresh_triage = _triage_spec(canonical_ref, config, config.project_root, task=task)
            _log(
                f"IN-FLIGHT {task.slug}: resuming "
                f"{fresh_triage.action.upper().replace('_', ' ')} ({fresh_triage.reason})"
            )
        except Exception as exc:
            # Triage is an optimisation over "start over"; losing it must not
            # lose the story.
            _log(f"WARN {task.slug}: could not triage inherited worktree ({exc}); running fresh")
            fresh_triage = None

    return _run_single_story(
        config,
        task,
        fresh_triage,
        sprint_run_id,
        sprint_name,
        interactive,
        notify,
        # Resume semantics, regardless of the sprint-level --resume flag: the
        # triage above is the whole point of this path.
        True,
        effective_auto_merge,
        state_update_fn,
        no_pull,
        plan_gate,
        preflight_states,
        stop_event,
        base_lands_locally=base_lands_locally,
        lands_in_project_root=lands_in_project_root,
    )


def _make_worker_phase_fn(
    slug: str,
    worker_phases: dict[str, str],
    phase_lock: threading.Lock,
    outer_fn: "Callable[[dict], None] | None",
    plan_done: "dict[str, str] | None" = None,
    state_writer: "SprintStateWriter | None" = None,
    audit_flush: "Callable[[str], None] | None" = None,
    budget_checkpoint: "Callable[[str, SprintCostObservation | None], None] | None" = None,
    live_cost_updates: "dict[str, SprintCostObservation] | None" = None,
    stop_event: threading.Event | None = None,
    cost_projection: "Callable[[float | None], float | None] | None" = None,
) -> "Callable[[dict], None]":
    """Return a thread-safe state_update_fn wrapper for worker live state.

    Updates worker_phases[slug] from updates["phase"] and (under lock) forwards
    updates to the outer daemon state_update_fn if provided.

    When *plan_done* is provided and a PLAN_DONE phase update arrives, stores
    the workspace_path in plan_done[slug] for the scheduler to read.

    When *state_writer* is provided, live-facing fields are also written to the
    sprint state file so ``forge sprint-status`` reflects both the current phase
    and the latest per-story cost.

    When *audit_flush* is provided it is called once per phase *change* with the
    new phase, so the story's audit.yaml on disk keeps pace with the run. That
    file is the only record an outside process (``forge stop``) can finalize —
    it cannot see this one's memory (#2013). Flushing on phase changes only
    keeps the cost proportional to real progress rather than to update chatter.

    When *budget_checkpoint* is provided it is called at coordinator phase
    boundaries with the best measured lower bound that update carried, or
    ``None`` when the update brought no new lower-bound data. Those boundaries
    are precisely where a sprint cap can be enforced against a story that is
    already running: the sprint learns what the story has spent at the same
    moment the story is between phases and can still be stopped without wasting
    a phase's work (#2547).

    When *cost_projection* is provided every ``cost_usd`` forwarded to the state
    writer passes through it first. The coordinator reports what THIS generation
    measured; the row has to report what the sprint has spent on the story,
    including whatever an earlier generation spent before a re-exec. Projecting
    here rather than at wrap-up is what keeps that money on disk when an operator
    stops the run mid-flight (#2922).

    When *stop_event* is already set the worker has been cancelled by the
    scheduler, so any later phase update is stale: accepting it would recreate
    provisional in-flight spend for a story the scheduler has already retired
    and accounted for.
    """

    def _update(updates: dict) -> None:
        if stop_event is not None and stop_event.is_set():
            return
        phase = updates.get("phase", "")
        _observed_cost: SprintCostObservation | None = None
        # A mirrored cost belongs to another slug's run (a batch group's shared
        # dev pass). It is displayed on this row and charged on that one.
        _cost_mirrored = bool(updates.pop("cost_mirrored", False))
        # Before the lock: the checkpoint may stop the sprint and set every
        # in-flight story's cancellation signal, and no other worker's live
        # update should queue behind that decision.
        if not _cost_mirrored:
            # ``None`` means "no new lower bound arrived", not "stop checking":
            # once a story has gone unmeasured the sprint must keep re-checking
            # the cap against the best lower bound it already holds (#1992, #2547).
            _detail = updates.get("detail")
            _has_lower_bound_detail = isinstance(_detail, Mapping) and (
                "cost_measured_lower_bound_usd" in _detail
            )
            if "cost_usd" in updates or "coordinator_state" in updates or _has_lower_bound_detail:
                _observed_cost = checkpoint_cost(updates)
                if budget_checkpoint is not None:
                    budget_checkpoint(slug, _observed_cost)
        with phase_lock:
            phase_changed = bool(phase) and worker_phases.get(slug) != phase
            if phase:
                worker_phases[slug] = phase
            if live_cost_updates is not None and _observed_cost is not None:
                live_cost_updates[slug] = _observed_cost
            if state_writer is not None:
                incoming_detail = updates.get("detail")
                detail_updates: dict[str, object] = (
                    dict(incoming_detail) if isinstance(incoming_detail, dict) else {}
                )
                if phase == "VALIDATE" and not detail_updates:
                    detail_updates = {"gate_status": "running"}
                update_kwargs: dict[str, object] = {}
                if phase:
                    update_kwargs["phase"] = phase
                if "complexity" in updates:
                    update_kwargs["complexity"] = updates["complexity"]
                if "complexity_score" in updates:
                    update_kwargs["complexity_score"] = updates["complexity_score"]
                if "cost_usd" in updates:
                    _live_cost = updates["cost_usd"]
                    update_kwargs["cost_usd"] = (
                        cost_projection(_live_cost) if cost_projection is not None else _live_cost
                    )
                if "current_model" in updates:
                    update_kwargs["current_model"] = updates["current_model"]
                if detail_updates:
                    update_kwargs["detail"] = detail_updates
                if update_kwargs:
                    state_writer.update(slug, **update_kwargs)
            if phase == "PLAN_DONE" and plan_done is not None:
                ws = updates.get("workspace_path", "")
                if ws:
                    plan_done[slug] = ws
            if outer_fn is not None:
                outer_fn(updates)
        # Outside the lock: the flush serializes the whole coordinator state and
        # writes a file, and no other worker's live-state update should queue
        # behind that.
        if phase_changed and audit_flush is not None:
            audit_flush(phase)

    return _update


def _make_audit_flush_fn(
    config: ForgeConfig,
    task: "TaskStory",
    sprint_name: str,
    *,
    sprint_id: str | None = None,
) -> "Callable[[str], None]":
    """Return a callback that rewrites *task*'s audit.yaml from its live state.

    The story's own worker thread runs this, so it reads the engine's live
    ``CoordinatorState`` straight out of the in-process registry. What it writes
    is the handoff to processes that have no such access: ``forge stop`` runs
    elsewhere, after this process is gone, and can only finalize an audit that is
    already on disk (#2013).
    """

    def _flush(_phase: str) -> None:
        from ..coordinator.live_state import snapshot_live_state  # noqa: PLC0415

        state = snapshot_live_state(task.slug)
        if state is None:
            return
        write_live_story_audit(config, task, state, sprint_id=sprint_id, sprint_name=sprint_name)

    return _flush


def _terminal_story_model(result: CoordinatorResult) -> str | None:
    """Return the dev model to display for a terminal story row.

    Live status uses ``current_model`` while a story is running. REVIEW
    intentionally overwrites that with ``panel(N)`` for operator visibility, so
    terminal transitions must restore the model that actually produced the work.
    Prefer the explicit ``model_used`` recorded by the runner and fall back to
    legacy ``model_usage`` data when needed.
    """

    dev_results = list(getattr(result.state, "dev_results", []) or [])
    for dev_result in reversed(dev_results):
        model_used = getattr(dev_result, "model_used", None)
        if isinstance(model_used, str) and model_used:
            return model_used
        model_usage = tuple(getattr(dev_result, "model_usage", ()) or ())
        if model_usage:
            usage_model = getattr(model_usage[-1], "model", None)
            if isinstance(usage_model, str) and usage_model:
                return usage_model
    return None


def _snapshot_last_known(
    slug: str,
    state_writer: "SprintStateWriter | None",
) -> dict:
    """Snapshot last-known telemetry for a slug from the canonical SprintStoryState.

    The runner builds this snapshot at the moment a worker times out or raises
    so failure rows can render the real phase/model/cost/elapsed instead of the
    hollow defaults of a synthetic timeout CoordinatorState.
    """
    snapshot: dict = {
        "last_phase": None,
        "last_model": None,
        "last_cost": None,
        "last_started_at": None,
    }
    if state_writer is None:
        return snapshot
    entry = state_writer.story_state.get(slug)
    if entry is None:
        return snapshot
    snapshot["last_phase"] = entry.phase
    extras = entry.extras or {}
    model = extras.get("current_model")
    if isinstance(model, str) and model:
        snapshot["last_model"] = model
    if entry.cost_usd:
        snapshot["last_cost"] = float(entry.cost_usd)
    started_raw = extras.get("started_at")
    if isinstance(started_raw, str) and started_raw:
        try:
            snapshot["last_started_at"] = datetime.datetime.fromisoformat(
                started_raw.replace("Z", "+00:00")
            )
        except ValueError:
            snapshot["last_started_at"] = None
    return snapshot


def _terminal_state_for(
    slug: str,
    *,
    config: ForgeConfig,
    sprint_name: str,
    started_at: datetime.datetime,
    error: str,
    error_type: str,
    phase: Phase = Phase.ESCALATE,
) -> CoordinatorState:
    """Build the CoordinatorState for a story the scheduler had to terminate itself.

    A worker that times out or raises never hands back its ``CoordinatorResult``,
    so the scheduler has to synthesize one. Synthesizing it from scratch threw
    away everything the engine had already accumulated — the audit for a story
    with three dev iterations and two gate retries was written with
    ``run_id: null``, an empty ``dev_loop`` and an empty ``gate_decisions``
    (#2013). Prefer the engine's own live state for the slug, and fall back to a
    bare state only when the story never reached the engine.

    The returned state is stamped with the scheduler's error and, by default,
    ESCALATE, so the telemetry is preserved without the audit misreporting how
    the story ended. ``phase`` is overridden for a story the launch guard dropped
    before dispatch: it never reached the state machine, and stamping it ESCALATE
    would claim an escalation that never happened (and mark its worktree as one).
    """
    from ..coordinator.live_state import snapshot_live_state  # noqa: PLC0415

    state = snapshot_live_state(slug)
    if state is None:
        state = CoordinatorState(
            started_at=started_at.isoformat(),
            workspace_path=(config.project_root / config.workspace.path_pattern.format(slug=slug)),
            log_dir=_make_story_log_dir(config, slug, sprint_name),
        )
    else:
        if not state.started_at:
            state.started_at = started_at.isoformat()
        if state.workspace_path is None:
            state.workspace_path = config.project_root / config.workspace.path_pattern.format(
                slug=slug
            )
        if state.log_dir is None:
            state.log_dir = _make_story_log_dir(config, slug, sprint_name)
    state.phase = phase
    state.error = error
    state.error_type = error_type
    return state


def _abnormal_story_result(
    slug: str,
    *,
    config: ForgeConfig,
    sprint_name: str,
    started_at: datetime.datetime,
    error: str,
    error_type: str,
    message: str,
    phase: Phase = Phase.ESCALATE,
) -> CoordinatorResult:
    """Synthesize the CoordinatorResult for a story that never returned one.

    Every abnormal exit — launch-guard drop, worker exception, worker timeout —
    needs the same thing before it can be audited: a result carrying the primary
    cause in ``state.error``. Built here so all three produce records of the same
    shape, and so a story that never reached the engine still gets a ``run_id``
    and is therefore addressable as a run rather than dissolving into the sprint
    summary (#2030).
    """
    state = _terminal_state_for(
        slug,
        config=config,
        sprint_name=sprint_name,
        started_at=started_at,
        error=error,
        error_type=error_type,
        phase=phase,
    )
    if not state.run_id:
        state.run_id = _generate_run_id()
    if not state.sprint_name:
        state.sprint_name = sprint_name
    return CoordinatorResult(success=False, phase=phase, state=state, message=message)


def _required_pr_check_state(pr_url: str, project_root: Path, base_branch: str) -> PrCheckState:
    """Terminal state of ``pr_url``'s required checks, split by verdict.

    Thin seam over :mod:`theforge.sprint.ci_checks` so the queued-PR wait can be
    tested without a live ``gh``. Never raises: an unanswerable probe returns an
    empty state, which keeps the caller waiting instead of abandoning a PR whose
    check state we could not read.
    """
    try:
        return required_pr_check_state(project_root, pr_url, base_branch)
    except Exception:  # pragma: no cover - ci_checks already fails soft
        return PrCheckState([], [])


def _poll_queued_pr(
    pr_url: str,
    project_root: Path,
    timeout_seconds: int,
    base_branch: str | None = None,
) -> dict[str, str]:
    """Poll GitHub until a queued PR is merged, closed, red, or times out.

    When ``base_branch`` is supplied, "merged" is only reported once GitHub
    reports MERGED *and* the PR's merge commit is reachable on
    ``origin/<base_branch>``. GitHub's auto-merge queue flips a PR to MERGED
    several seconds before the merge commit propagates to the base ref;
    releasing a collision-serialized dependent on the GitHub state alone
    re-creates the race the collision DAG existed to prevent (issue #1402).

    A still-open PR is also probed for terminally-failed required checks each
    iteration. A decided-red PR can never merge, so waiting on it only burns the
    merge-wait budget and then misreports the outcome as a queue timeout (issue
    #1946); such a PR returns ``checks_failed`` immediately, carrying the names
    of the failing checks. The wait budget is reserved for *pending* checks.

    Only a check that ran and rejected the change counts as decided-red. A check
    that stopped without a verdict — cancelled by a platform incident, superseded,
    or ended in an outcome we cannot recognise — is an absence of evidence about
    the change, so it keeps the wait running and is named in the timeout result
    instead. Abandoning such a PR as red sends the operator to debug a change that
    was never tested (issue #2270).
    """
    deadline = time.monotonic() + timeout_seconds
    unjudged_seen: dict[str, None] = {}  # ordered set of check names
    while True:
        try:
            proc = subprocess.run(
                ["gh", "pr", "view", pr_url, "--json", "state", "-q", ".state"],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=30,
            )
        except Exception:
            return {"status": "timeout"}

        if proc.returncode == 0:
            state = proc.stdout.strip()
            if state == "MERGED":
                if base_branch is None or _merge_visible_on_base(
                    pr_url, project_root, base_branch
                ):
                    return {"status": "merged"}
            elif state == "CLOSED":
                return {"status": "closed"}
            elif base_branch is not None:
                checks = _required_pr_check_state(pr_url, project_root, base_branch)
                if checks.failing:
                    return {
                        "status": "checks_failed",
                        "failing_checks": ", ".join(checks.failing),
                    }
                unjudged_seen.update(dict.fromkeys(checks.unjudged))

        if time.monotonic() >= deadline:
            if unjudged_seen:
                return {
                    "status": "timeout",
                    "unjudged_checks": ", ".join(unjudged_seen),
                }
            return {"status": "timeout"}
        time.sleep(30)


def _queued_pr_failure_message(
    poll_result: dict[str, str], pr_url: str, timeout_seconds: int
) -> str:
    """Render the merge-failure cause for a non-merged queued-PR poll result.

    The cause string is the only evidence downstream RCA has, so each terminal
    status gets its own wording: "timed out" is reserved for an actual deadline
    expiry, and a decided-red PR names the required checks that failed. A wait
    that expired on checks which stopped without ever judging the change says so
    explicitly, because that is recovered by dispatching the checks again rather
    than by changing the code (issue #2270).
    """
    status = poll_result.get("status", "unknown")
    if status == "checks_failed":
        failing = poll_result.get("failing_checks") or "unknown"
        return f"Queued PR required checks failed ({failing}): {pr_url}"
    if status == "timeout":
        unjudged = poll_result.get("unjudged_checks")
        if unjudged:
            return (
                f"Queued PR required checks never produced a verdict ({unjudged}) "
                f"within {timeout_seconds}s: {pr_url}"
            )
        return f"Queued PR timed out after {timeout_seconds}s: {pr_url}"
    return f"Queued PR {status}: {pr_url}"


def _merge_visible_on_base(pr_url: str, project_root: Path, base_branch: str) -> bool:
    """Return True when the PR's merge commit is reachable on origin/<base_branch>.

    The collision detector serializes overlapping stories with `depends_on`
    edges; the gating predicate that unblocks a dependent must match the
    edge's purpose, which is to make the parent's diff visible to the
    dependent's dev iteration. "PR auto-merge queued" or even GitHub's
    `state == MERGED` happens before the merge commit reaches origin/<base>;
    a dependent worktree created in that window is stale relative to the
    parent's edits and rebases conflict on the shared file. Confirm origin
    actually carries the merge commit before treating the parent as landed.
    """
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                pr_url,
                "--json",
                "mergeCommit",
                "-q",
                '.mergeCommit.oid? // ""',
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    merge_sha = proc.stdout.strip()
    if not merge_sha:
        return False
    try:
        subprocess.run(
            ["git", "fetch", "origin", base_branch],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )
    except Exception:
        return False
    try:
        ancestor = subprocess.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                merge_sha,
                f"origin/{base_branch}",
            ],
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=30,
        )
    except Exception:
        return False
    return ancestor.returncode == 0


def _fatal_auth_cause(result: CoordinatorResult) -> dict | None:
    """Return the structured cause when *result* died on a credential rejection.

    Returns ``None`` for every other outcome, including infrastructure aborts
    of a different category. A transport drop or a provider 5xx can succeed on
    the next attempt, so re-trying the next story is genuine resilience there.
    An auth rejection cannot: the same credential will be presented, and it will
    be refused identically. Re-discovering that per story and per phase is what
    turned #1952's revoked token into six minutes and nine identical errors.
    """
    if not (
        getattr(result, "infrastructure_failure", False) or is_infrastructure_abort(result.state)
    ):
        return None
    cause = getattr(result.state, "infrastructure_failure", None)
    if not isinstance(cause, dict):
        return None
    if cause.get("category") != CATEGORY_AUTH:
        return None
    return cause


def _mark_story_auth_cancelled(
    result: CoordinatorResult,
    cause: dict | None,
    *,
    reason: str,
) -> None:
    """Re-attribute a mid-flight cancellation to the credential that caused it.

    The worker returned through the generic stop_event path, whose result is
    shaped for a worker *timeout*: a plain ``ESCALATE`` with
    ``error_type="StoryCancelled"``. Left alone, that reads downstream as a
    story-level failure. This restamps it with the same infrastructure-abort
    vocabulary the discovering story carries, so #1951's taint machinery
    excludes it from adaptive memory and no consumer can mistake a sprint-issued
    kill for a judgment about the work.

    Best-effort by construction: the story is already terminal, and failing to
    relabel it must not take the sprint's shutdown path down with it.
    """
    try:
        result.infrastructure_failure = True
        state = result.state
        state.error = reason
        state.error_type = ERROR_TYPE_INFRASTRUCTURE_ABORT
        failure = AgentInvocationFailure(
            phase=str(getattr(getattr(result, "phase", None), "name", "") or "UNKNOWN"),
            category=CATEGORY_AUTH,
            detail=reason[:500],
            profile_name=(cause or {}).get("profile_name"),
        )
        mark_infrastructure_abort(state, failure, message=reason)
    except Exception as exc:  # pragma: no cover - defensive
        _log(f"WARN: could not re-attribute auth-cancelled story: {exc}")


def _budget_cancel_reason(state: SprintExecutionState) -> str:
    """The operator-facing reason a story was cancelled for the sprint's cap."""
    recorded = state.stop.reason or "sprint budget exhausted"
    return f"cancelled mid-flight: {recorded}"


def _mark_story_budget_cancelled(result: CoordinatorResult, *, reason: str) -> None:
    """Re-attribute a mid-flight cancellation to the sprint's spending cap.

    The same problem ``_mark_story_auth_cancelled`` solves, for the other reason
    a sprint kills work it started: left alone, the generic cancellation reads
    downstream as a story that failed. It did not fail — it was stopped, by a
    decision about money that says nothing about the work, and the record has to
    say which (#2547).

    Deliberately NOT an infrastructure abort: nothing was broken. The story is
    simply unfinished, and re-running it under a larger cap is the whole remedy.
    """
    try:
        result.state.error = reason
        result.state.error_type = BUDGET_CANCEL_ERROR_TYPE
        result.message = reason
    except Exception as exc:  # pragma: no cover - defensive
        _log(f"WARN: could not re-attribute budget-cancelled story: {exc}")


#: Operator-facing text for a story the preflight complexity gate returned.
DECOMPOSED_STORY_REASON = "returned for decomposition"


def _returned_for_decomposition(result: CoordinatorResult) -> bool:
    """True when the preflight complexity gate returned this story to be split.

    Read off the coordinator's recorded decision rather than off ``success`` or
    the phase: the gate is the only thing that writes it, and every other
    non-success path this runner sees means something went wrong (#2681).
    """
    return (
        getattr(result.state, "preflight_complexity_gate_decision", None)
        == PREFLIGHT_GATE_DECOMPOSE
    )


def _classify_and_record(
    task: TaskStory,
    result: CoordinatorResult,
    dag: StoryDAG,
    merged_slugs: set[str],
    story_state: "SprintStoryState | None" = None,
    cost_projection: "Callable[[float | None], float | None] | None" = None,
) -> StoryOutcome:
    """Classify result and update DAG state.

    Returns the canonical :class:`StoryOutcome` for the story. When
    ``story_state`` is supplied, the outcome is also recorded there — this is
    the single source of truth that all surfaces project from.

    ``cost_projection`` turns the coordinator's current-generation cost into the
    figure the row reports, so this write agrees with every other one about what
    the sprint has spent on the story (#2922).
    """
    preflight_verdict = result.state.preflight_verdict
    landing_status = getattr(result, "landing_status", None)
    validate_already_complete = getattr(result.state, "validate_already_complete", False)
    # A confirmed-landed DONE is immutable for the rest of the sprint. Mark it
    # so story_state.transition rejects any later non-DONE terminal overwrite
    # (e.g. a bogus FAILED from a redispatch after a process restart).
    is_landed = landing_status == "landed"

    if _returned_for_decomposition(result):
        # Asked and answered at the preflight complexity gate (#2681): the
        # story was returned to be split before any cost-bearing phase past
        # preflight. Classified ahead of every result.success branch below
        # because it is neither — nothing failed, and nothing was delivered.
        # mark_skipped, not mark_complete: the work is not on the base branch,
        # so a dependent must not be released as though it were.
        outcome = StoryOutcome.DECOMPOSED
        dag.mark_skipped(task.slug)
    elif preflight_verdict == "ALREADY_DONE" and result.success:
        outcome = StoryOutcome.ALREADY_DONE
        merged_slugs.add(task.slug)
        dag.mark_complete(task.slug)
    elif validate_already_complete and result.success:
        # VALIDATE determined the dev cycle correctly produced no commits
        # because the work was already on the base branch. Treat this as a
        # successful ALREADY_DONE-shaped outcome rather than FAILED.
        outcome = StoryOutcome.ALREADY_DONE
        merged_slugs.add(task.slug)
        dag.mark_complete(task.slug)
    elif landing_status == "landed":
        outcome = StoryOutcome.DONE
        merged_slugs.add(task.slug)
        dag.mark_complete(task.slug)
    elif landing_status == "failed":
        merge_info = getattr(result, "merge", None) or {}
        outcome = landing_failure_outcome(merge_info if isinstance(merge_info, dict) else None)
        dag.mark_skipped(task.slug)
    elif landing_status == "pending_integration":
        # Approved but merge deferred or queued — counts as succeeded, not yet in DAG
        outcome = StoryOutcome.DONE
        dag.mark_skipped(task.slug)
    elif result.success:
        # No merge operation performed (on_approve=none or similar)
        outcome = StoryOutcome.DONE
        dag.mark_skipped(task.slug)
    else:
        outcome = StoryOutcome.FAILED
        dag.mark_skipped(task.slug)

    if story_state is not None:
        _reported_cost = _story_reported_cost(result.state)
        _transition_fields: dict = {
            "cost_usd": (
                cost_projection(_reported_cost) if cost_projection is not None else _reported_cost
            )
        }
        if is_landed:
            _transition_fields["landed"] = True
        story_state.transition(task.slug, outcome=outcome, **_transition_fields)

    return outcome


def _refresh_external_satisfied(
    dag: StoryDAG,
    all_tasks: list[TaskStory],
    config: ForgeConfig,
    merged_slugs: set[str] | None = None,
) -> set[str]:
    """Re-check unmet external dependencies and mark newly satisfied slugs.

    External issue dependencies can close while a sprint is running. Keeping this
    refresh in the scheduler loop lets dependents become ready without requiring
    operators to stop and resume the sprint.
    """
    manifest_slugs = {task.slug for task in all_tasks}
    external_deps = {
        dep
        for task in dag.remaining()
        for dep in dag.unmet_deps(task.slug)
        if dep not in manifest_slugs
    }
    if not external_deps:
        return set()

    dependent_tasks = [
        task for task in all_tasks if any(dep in external_deps for dep in task.depends_on)
    ]
    satisfied = resolve_satisfied_dependencies(
        dependent_tasks,
        project_root=config.project_root,
        base_branch=config.workspace.base_branch,
        branch_pattern=config.workspace.branch_pattern,
    )
    newly_satisfied = {
        slug for slug in satisfied if slug in external_deps and slug not in dag._completed
    }
    for slug in sorted(newly_satisfied):
        dag.mark_complete(slug)
        if merged_slugs is not None:
            merged_slugs.add(slug)
        _log(f"dep satisfied: {slug} (GitHub issue closed)")
    return newly_satisfied


@dataclass(frozen=True)
class SprintStop:
    """Why a sprint stopped, and the story that halted it if there was one."""

    reason: str
    halt_slug: str | None = None


class SprintStopCondition:
    """The single owner of whether a sprint has stopped and why.

    The reason and the halt slug are one fact, not two: a CI halt that recorded
    the reason but lost the slug leaves the summary unable to say what broke.
    Recording them together through one owner is what keeps them from drifting.

    Two write paths exist because the sprint genuinely has two policies.
    ``stop`` is for the caller that has just established the authoritative
    reason (a red required check names its own story); ``stop_if_unset`` is
    first-writer-wins, for the callers that must not overwrite an earlier and
    more specific cause with a downstream consequence of it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop: SprintStop | None = None

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stop is not None

    @property
    def record(self) -> SprintStop | None:
        """The whole stop, reason and halt slug together, or None."""
        with self._lock:
            return self._stop

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._stop.reason if self._stop is not None else None

    @property
    def halt_slug(self) -> str | None:
        with self._lock:
            return self._stop.halt_slug if self._stop is not None else None

    def stop(self, reason: str, *, halt_slug: str | None = None) -> SprintStop:
        """Record the stop, replacing any earlier one."""
        with self._lock:
            self._stop = SprintStop(reason=reason, halt_slug=halt_slug)
            return self._stop

    def stop_if_unset(self, reason: str, *, halt_slug: str | None = None) -> bool:
        """Record the stop only if nothing has stopped the sprint yet.

        Returns True when this call is the one that stopped it, so a caller can
        hang its one-shot side effects (the operator notification) off the same
        decision rather than re-deriving it.
        """
        with self._lock:
            if self._stop is not None:
                return False
            self._stop = SprintStop(reason=reason, halt_slug=halt_slug)
            return True


@dataclass(frozen=True)
class SprintRunContext:
    """What a sprint consults but never changes.

    Everything here is settled before the first story is dispatched: the
    resolved sprint, the identifiers its audit trail is written under, and the
    operator's invocation choices. This is also ``run_sprint``'s whole
    signature — a new sprint-wide input becomes a field here rather than the
    nineteenth parameter. The run and the functions it calls read these and only
    read them, and the frozen dataclass is what says so: the split between what
    a sprint mutates and what it merely consults is a fact about this type
    versus :class:`SprintExecutionState`, not a convention.

    ``config`` and ``resolved`` are objects rather than values; freezing the
    context pins *which* ones the run uses, not their internals.
    """

    config: ForgeConfig
    resolved: ResolvedSprint
    sprint_id: str | None = None
    run_id: str | None = None
    auto_merge: bool = False
    interactive: bool = False
    notify: bool = False
    resume: bool = False
    reexec: bool = False
    no_pull: bool = False
    force: bool = False
    state_update_fn: "Callable[[dict], None] | None" = None
    dropped_slugs: "dict[str, str] | None" = None
    skipped_issues: "list | None" = None
    entry_intake_outcomes: "dict[int, IntakeOutcome] | None" = None
    live_story_slugs: frozenset[str] = frozenset()
    unresolved_live_slugs: frozenset[str] = frozenset()
    # The subset of ``live_story_slugs`` whose only evidence is this run's own
    # ownership record — in-flight work with no surviving agent process group
    # (#2617). Carried so operator-facing text can say which it is: "an agent is
    # still running" and "this sprint dispatched it and has not settled it" call
    # for different reading, and only one of them is true of a story that was
    # executing in-process when the re-exec fired.
    registered_live_slugs: frozenset[str] = frozenset()
    # Operator acceptance of unmeasured spend. Consulted by the budget guard and
    # never written by the run, so it belongs here beside ``force`` rather than
    # on the execution state (#2399).
    accept_unmeasured_spend: "Sequence[str] | None" = None
    accept_unmeasured_reason: str | None = None
    # Derived once from ``resolved`` — the (task, source, canonical_ref) triple
    # every consumer looks up by slug. Derived rather than passed, so no caller
    # can hand the run a mapping that disagrees with the sprint it resolved.
    slug_to_context: "dict[str, tuple[TaskStory, StorySource, str]]" = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        # Normalise the liveness sets here rather than at each construction
        # site: an unresolved slug that is also confirmed live is one story, and
        # every consumer downstream reads ``live_slugs`` for "do not treat this
        # worktree as foreign".
        confirmed = frozenset(s for s in (self.live_story_slugs or ()) if s)
        object.__setattr__(self, "live_story_slugs", confirmed)
        object.__setattr__(
            self,
            "unresolved_live_slugs",
            frozenset(s for s in (self.unresolved_live_slugs or ()) if s and s not in confirmed),
        )
        object.__setattr__(
            self,
            "registered_live_slugs",
            frozenset(s for s in (self.registered_live_slugs or ()) if s in confirmed),
        )
        object.__setattr__(
            self,
            "slug_to_context",
            {
                task.slug: (task, source, canonical_ref)
                for task, source, canonical_ref in self.resolved.stories
            },
        )

    @classmethod
    def for_sprint(
        cls,
        config: ForgeConfig,
        sprint: "Path | ResolvedSprint",
        **options: object,
    ) -> "SprintRunContext":
        """Build the context for an invocation, resolving a manifest path if given.

        ``run_sprint`` takes the context and nothing else, so the manifest →
        :class:`ResolvedSprint` step lives here: one place resolves it, and the
        run body has no path-shaped branch left to keep in step with query mode.
        Tests and callers that patch ``resolve_from_manifest`` still see the same
        boundary. A manifest is identified by being a path — everything else is
        taken as already resolved, so query mode's own resolver output goes
        through untouched.
        """
        if isinstance(sprint, (str, Path)):
            resolved = resolve_from_manifest(Path(sprint), config.project_root)
        else:
            resolved = sprint
        live = options.pop("live_story_slugs", None) or ()
        unresolved = options.pop("unresolved_live_slugs", None) or ()
        registered = options.pop("registered_live_slugs", None) or ()
        return cls(
            config=config,
            resolved=resolved,
            live_story_slugs=frozenset(live),  # type: ignore[arg-type]
            unresolved_live_slugs=frozenset(unresolved),  # type: ignore[arg-type]
            registered_live_slugs=frozenset(registered),  # type: ignore[arg-type]
            **options,  # type: ignore[arg-type]
        )

    @property
    def live_slugs(self) -> frozenset[str]:
        """Every slug this run must not treat as foreign work.

        Unresolved liveness joins confirmed-live deliberately: failing to resolve
        an agent sidecar is not evidence the agent is gone (#2079).
        """
        return self.live_story_slugs | self.unresolved_live_slugs

    @property
    def slug_by_canonical_ref(self) -> dict[str, str]:
        """The reverse of :attr:`slug_to_context`: canonical ref → slug.

        Derived here beside the mapping it inverts because two unrelated
        consumers need it — the terminal audit writers, which key every story
        row by ref, and the post-sprint hook, which falls back to it for a
        story whose workspace never got created. Deriving it twice would be two
        chances to disagree about the sprint that ran.
        """
        return {
            canonical_ref: slug for slug, (_t, _s, canonical_ref) in self.slug_to_context.items()
        }

    @property
    def name(self) -> str:
        return self.resolved.name

    @property
    def budget_usd(self) -> float:
        return self.resolved.budget_usd


@dataclass
class SprintExecutionState:
    """The sprint's execution state, as a thing with a name.

    This is what ``run_sprint`` used to hold in its stack frame: reachable only
    from inside the function, nameable by nothing, assertable by no test that
    was not willing to run a whole sprint. Constructing one needs only a
    :class:`SprintRunContext`, so a caller can build the state a sprint would
    have and assert against it without dispatching a story.

    The two questions the old ``nonlocal`` writes left unanswered each have a
    single owner here: :attr:`cost` is the only thing that advances the sprint
    total, and :attr:`stop` is the only thing that decides the sprint has
    stopped. Neither can be advanced by assigning to a shared variable, because
    neither is one.

    Holding the state is what lets a function leave ``run_sprint``: the
    module-scope helpers below take this object and their own per-call inputs,
    never a list of the values it carries — which is the parameter threading
    that produced an eighteen-parameter entrypoint in the first place (#2399).
    ``tests/test_sprint_runner_structure.py`` holds that shape.
    """

    context: SprintRunContext
    cost: SprintCostLedger = field(default_factory=SprintCostLedger)
    stop: SprintStopCondition = field(default_factory=SprintStopCondition)
    stories: SprintStoryState = field(default_factory=SprintStoryState)
    merged_slugs: set[str] = field(default_factory=set)
    results: list[tuple[str, CoordinatorResult]] = field(default_factory=list)
    story_times: dict[str, tuple[datetime.datetime, datetime.datetime]] = field(
        default_factory=dict
    )
    live_telemetry_snapshots: dict[str, dict] = field(default_factory=dict)
    batch_assignments: dict[str, int] = field(default_factory=dict)
    batch_number: int = 0

    # --- identifiers and writers -------------------------------------------
    # The run id the sprint's own structured log is written under. Distinct from
    # ``context.run_id``, which is the CLI's live-state id and may be absent.
    sprint_run_id: str = ""
    # Present only once a live state file exists (a run_id was supplied).
    state_writer: "SprintStateWriter | None" = None
    publish_gate_hold: "Callable[..., None] | None" = None

    # --- scheduling ---------------------------------------------------------
    # Built once the satisfied/triaged sets are known; None before that.
    dag: "StoryDAG | None" = None
    active: "dict[str, Future[object]]" = field(default_factory=dict)  # slug -> running future
    # Per-story cancellation events: set by the timeout handler so worker
    # threads stop running instead of continuing past their deadline.
    stop_events: dict[str, threading.Event] = field(default_factory=dict)
    queued_prs: "dict[str, tuple[TaskStory, CoordinatorResult, str]]" = field(default_factory=dict)
    queued_probe_at: dict[str, float] = field(default_factory=dict)  # slug -> last PR probe

    # --- plan gates and collision claims ------------------------------------
    # ``phase_lock`` guards ``plan_done``; the plan gate holds a story between
    # PLAN and DEV while a sibling claims files it planned to touch (#2234).
    phase_lock: threading.Lock = field(default_factory=threading.Lock)
    file_footprints: dict[str, set[str]] = field(default_factory=dict)  # slug -> files from plan
    plan_gates: dict[str, threading.Event] = field(default_factory=dict)  # slug -> PLAN→DEV gate
    plan_done: dict[str, str] = field(default_factory=dict)  # slug -> workspace_path
    collision_claims: dict[str, str] = field(default_factory=dict)  # slug -> CLAIM_* reason
    claim_results: "dict[str, CoordinatorResult]" = field(default_factory=dict)  # last known
    gate_stood_down: dict[str, str] = field(default_factory=dict)  # slug -> never entered DEV

    # --- batch-group landing bookkeeping (#727) -----------------------------
    # A group's commits live on ONE branch — the leader's — so a member is
    # exactly as landed as its leader is, and no more. The leader's landing can
    # resolve at four different moments (immediate integration, the
    # pending-integration retry sweep, queued-PR polling, sprint wrap-up), and
    # members may be recorded before *or* after that resolution, so both
    # directions have to be handled.
    batch_group_of_leader: dict[str, str] = field(default_factory=dict)  # leader -> group
    batch_leader_landing: dict[str, str] = field(default_factory=dict)  # group -> landing status
    batch_member_records: "dict[str, tuple[str, TaskStory, CoordinatorResult]]" = field(
        default_factory=dict
    )  # member -> (group, task, result), awaiting the leader's verdict

    # --- durable per-story accounting ---------------------------------------
    # This generation's per-story projection, by canonical ref. Written for
    # every slug, including those that never produce a CoordinatorResult
    # (dropped at the intake gate, blocked by deps, skipped pre-launch), so the
    # audit and summary writers have error/error_type/intake metadata to report.
    current_story_entries_by_ref: dict[str, dict] = field(default_factory=dict)
    recovered_prior_entries_by_ref: dict[str, dict] = field(default_factory=dict)
    # Failure causes already on disk for this sprint, by canonical ref. Read
    # unconditionally — not just when resuming — because every generation
    # rewrites the accumulated state file, so a plain re-run of the same sprint
    # would otherwise erase the previous attempt's recorded cause before
    # anything had a chance to read it (#2030).
    prior_failure_history_by_ref: dict[str, list[dict]] = field(default_factory=dict)
    # slug -> what the generation that ran the story before this one recorded.
    # Populated when its drop record is written, and read back for the story's
    # sprint row so both surfaces report the same accounting (#2214).
    prior_generation_work: dict[str, dict] = field(default_factory=dict)
    story_cost_adjustments: dict[str, float] = field(default_factory=dict)
    # --- pre-restart per-story spend (#2922) ---------------------------------
    # Spend an earlier generation of THIS sprint already attributed to a story
    # that re-enters this one. It lives here, not in ``run_sprint``'s frame,
    # because a signal-driven stop never returns to that frame: money held only
    # in a local variable is money the run loses when an operator stops it, and
    # the run then reports a final total below the carried figure it had already
    # disclosed.
    #
    # ``carried_prior_story_cost`` is active — every persisted projection of the
    # story's cost adds it. ``seeded_prior_story_cost`` is prior cost the
    # canonical row still physically holds (a succeeded prior outcome IS seeded);
    # it is activated exactly once, at the first write that replaces that row's
    # cost with this generation's measured figure, and stays inert for a story
    # that never re-runs.
    carried_prior_story_cost: dict[str, float] = field(default_factory=dict)
    seeded_prior_story_cost: dict[str, float] = field(default_factory=dict)
    # Per slug, the attribution already folded into its canonical row. The
    # wrap-up reconciliation adds only the difference, so money that live and
    # terminal writes already applied is never applied a second time.
    applied_story_attribution: dict[str, float] = field(default_factory=dict)
    # Stories this sprint cancelled mid-flight because its cap was reached.
    # Their results come back through the generic cancellation path, and this is
    # what tells the scheduler the cancellation was a spending decision rather
    # than a judgment about the work (#2547).
    budget_cancelled_slugs: set[str] = field(default_factory=set)
    # The latest measured lower bound each active story reported through live
    # state updates. Used to recover spend when a worker dies before it can
    # return a terminal CoordinatorResult (#2547 follow-up).
    latest_live_costs: dict[str, SprintCostObservation] = field(default_factory=dict)
    # Stories this generation actually put through a coordinator run — and
    # therefore the ones whose seeded prior cost was overwritten.
    ran_this_generation: set[str] = field(default_factory=set)
    # Slugs with a live ownership record on disk (#2617). A story is added the
    # moment this sprint takes responsibility for it — before ``pool.submit``,
    # so no work is ever spent unowned — and removed only once the scheduler has
    # settled its terminal outcome. The set mirrors what is in
    # ``.forge/runs/stories``; the files are the durable half, this is what lets
    # the scheduler find them without re-listing the directory.
    owned_story_executions: set[str] = field(default_factory=set)

    # --- budget runtime (#2621) ---------------------------------------------
    # The runtime half of the sprint's cap: the enforcement moments, the live
    # budget status, the operator acceptances in force, and the cancellation
    # that stops paid-for work when the cap is met. It reads this state and is
    # reached through it, so a budget change is a change to
    # ``sprint/budget_runtime.py`` and not to ``run_sprint``. Built in
    # ``__post_init__`` because it takes the state it belongs to, which no
    # ``default_factory`` can name.
    budget: SprintBudgetRuntime = field(init=False)

    def __post_init__(self) -> None:
        self.budget = SprintBudgetRuntime(self)

    # --- pre-restart per-story spend (#2922) ---------------------------------

    def defer_prior_story_cost(self, slug: str, cost_usd: float, *, seeded: bool) -> None:
        """Record spend an earlier generation attributed to *slug*.

        *seeded* says whether the canonical row was registered holding that cost.
        A seeded amount waits in ``seeded_prior_story_cost`` until the row is
        overwritten; an unseeded one is carried from this moment on, which is
        what makes it survive a stop that never reaches wrap-up.
        """
        if cost_usd <= 0.0:
            return
        target = self.seeded_prior_story_cost if seeded else self.carried_prior_story_cost
        target[slug] = target.get(slug, 0.0) + float(cost_usd)

    def activate_seeded_prior_cost(self, slug: str) -> None:
        """Carry *slug*'s seeded prior cost now that its row no longer holds it.

        Called from every write that replaces the row's cost with this
        generation's measured figure. Idempotent: the seed is consumed once.
        """
        amount = self.seeded_prior_story_cost.pop(slug, None)
        if amount:
            self.carried_prior_story_cost[slug] = (
                self.carried_prior_story_cost.get(slug, 0.0) + amount
            )

    def consume_carried_prior_cost(self, slug: str) -> None:
        """Drop *slug*'s carried prior cost because a row now states it directly.

        The reconciliation paths write a story's prior-generation cost onto its
        row as an absolute figure. That is the same money, so it must stop being
        added on top of it.
        """
        self.carried_prior_story_cost.pop(slug, None)
        self.seeded_prior_story_cost.pop(slug, None)

    @classmethod
    def for_run(cls, context: SprintRunContext) -> "SprintExecutionState":
        """The state a run of *context* starts from.

        Everything here is empty at the start of a run, so this is a
        one-argument constructor by construction — which is the point: a caller
        can build the state a sprint would have and assert against it, and a
        function extracted from ``run_sprint`` can be called with it.
        """
        return cls(context=context)


# --- functions extracted from run_sprint (ADR-0008, #2399) -----------------
#
# Each of these was a closure over run_sprint's frame. They take the sprint's
# execution state and their own per-call inputs, and nothing else: the state
# is the parameter, not a list of the values it holds, which is what makes the
# next extraction a move rather than a re-decision.


def _story_attribution_usd(state: SprintExecutionState, slug: str) -> float:
    """Spend attributed to *slug* that its coordinator state does not report.

    Two sources, both real money already in the sprint's ledger: this
    generation's intake-remediation spend (``story_cost_adjustments``) and
    pre-restart spend an earlier generation attributed to the story
    (``carried_prior_story_cost``).
    """
    intake = state.story_cost_adjustments.get(slug, 0.0)
    carried = state.carried_prior_story_cost.get(slug, 0.0)
    return round(intake + carried, 6)


def _projected_story_cost(
    state: SprintExecutionState,
    slug: str,
    measured: float | None,
    *,
    canonical: bool = False,
    overwrites_seed: bool = False,
    include_seed: bool = False,
) -> float | None:
    """The cost a persisted row reports for *slug*.

    This generation's measured cost plus everything else the sprint has
    attributed to the story. Applied at every live and terminal write rather
    than once at wrap-up, so a stopped process cannot take the attribution with
    it (#2922) — a ``forge stop`` never returns to ``run_sprint``'s wrap-up, and
    what is not on disk by then is lost.

    *measured* of ``None`` is cost-unknown and stays cost-unknown: adding a known
    amount to an unknown one does not produce a confident figure (#1992).

    *canonical* marks a write to the canonical story registry, whose attribution
    the wrap-up reconciliation would otherwise apply a second time.

    *overwrites_seed* marks a write that replaces the row's cost with this
    generation's own measured figure — the moment a seeded prior cost stops
    being on the row and has to be carried instead. A write that merely restates
    a row (the initial live-state projection) must not claim it, or a story
    skipped as already-merged would be charged its prior cost twice.

    *include_seed* is for the accumulated file, whose rows are replaced
    wholesale rather than transitioned. Replacing a seeded prior row overwrites
    that cost in THAT file while the canonical row still holds it, so the value
    written must include the seed — but the seed itself must stay where it is,
    or the wrap-up reconciliation would add it to the canonical row a second
    time. Reading it without consuming it is what keeps both files right.
    """
    if overwrites_seed:
        state.activate_seeded_prior_cost(slug)
    if measured is None:
        return None
    attribution = _story_attribution_usd(state, slug)
    if canonical:
        state.applied_story_attribution[slug] = attribution
    if include_seed:
        attribution += state.seeded_prior_story_cost.get(slug, 0.0)
    return measured + attribution


def _canonical_cost_projector(
    state: SprintExecutionState, slug: str
) -> "Callable[[float | None], float | None]":
    """A one-argument projection of *slug*'s cost onto its canonical row.

    ``_make_worker_phase_fn`` is a module-level function that holds no execution
    state, so the projection is bound here and handed to it rather than the
    state being threaded through the worker plumbing.
    """

    def _project(measured: float | None) -> float | None:
        return _projected_story_cost(state, slug, measured, canonical=True, overwrites_seed=True)

    return _project


def _claim_story_execution(state: SprintExecutionState, slug: str) -> None:
    """Record on disk that this sprint owns *slug*'s execution.

    Called before the story is submitted to the pool, because the record is what
    a re-exec of this same process reads to tell its own unfinished work from a
    competing run's leftovers (#2617). A story executing in pure Python at the
    instant of an ``os.execv`` leaves no process group behind; without this
    record its worktree surfaces on the far side as an ``active-worktree-collision``
    and the story is dropped, however far it had got and however much it cost.

    Raises on failure, and the caller must not dispatch. Launching a story whose
    ownership could not be written is launching spend that the next re-exec is
    entitled to throw away — the asymmetry the story is about.
    """
    from .story_executions import register_story_execution  # noqa: PLC0415

    config = state.context.config
    try:
        worktree = config.project_root / config.workspace.path_pattern.format(slug=slug)
    except (KeyError, IndexError, ValueError):
        worktree = None
    register_story_execution(
        slug,
        project_root=config.project_root,
        worktree=worktree,
        run_id=state.sprint_run_id or state.context.run_id,
    )
    state.owned_story_executions.add(slug)


def _release_story_execution(state: SprintExecutionState, slug: str) -> None:
    """Drop the ownership record for a story the scheduler has settled."""
    from .story_executions import clear_story_execution  # noqa: PLC0415

    state.owned_story_executions.discard(slug)
    clear_story_execution(slug, project_root=state.context.config.project_root)


def _release_settled_story_executions(state: SprintExecutionState) -> None:
    """Clear ownership for every story whose terminal outcome is now recorded.

    Deliberately *not* done in the worker's ``finally``. The worker returning is
    not the end of the story: the scheduler still has to record the outcome,
    attempt integration, and write the live state. A re-exec inside that window
    would find no ownership record and no prior-generation outcome either — the
    precise hole this registry closes — so the record is held until both halves
    are on disk, and a redundant deferral is accepted as the cheap side.

    Once the outcome *is* recorded, the ordinary prior-generation reconciliation
    can speak for the story, and it speaks more precisely than the record does.
    """
    for slug in sorted(state.owned_story_executions):
        if slug in state.active:
            continue
        entry = state.stories.get(slug)
        if entry is None or not entry.outcome.is_terminal:
            continue
        _release_story_execution(state, slug)


def _set_outcome(
    state: SprintExecutionState, slug: str, outcome: StoryOutcome | str, **fields: object
) -> None:
    """Transition a story's canonical outcome.

    All count-affecting events flow through this helper so the canonical
    structure is the only place the runner records outcomes. The state
    writer (when present) shares the same SprintStoryState instance and
    the on-disk live status file is updated in lockstep.
    """
    if not state.stories.has(slug):
        ctx = state.context.slug_to_context.get(slug)
        if ctx is not None:
            _t, _src, _ref = ctx
            _key = f"Issue #{_ref.split(':')[1]}" if _ref.startswith("issue:") else _ref
            state.stories.register(slug, _key, canonical_ref=_ref)
        else:
            state.stories.register(slug, slug)
    if "cost_usd" in fields:
        # Every count-affecting event flows through here, so this is also where
        # a story's persisted cost picks up the spend its coordinator state does
        # not report — at the moment the row is written, not at a wrap-up that a
        # stopped sprint never reaches (#2922).
        _incoming_cost = fields["cost_usd"]
        _measured_cost = (
            float(_incoming_cost)
            if isinstance(_incoming_cost, (int, float)) and not isinstance(_incoming_cost, bool)
            else None
        )
        fields["cost_usd"] = _projected_story_cost(state, slug, _measured_cost, canonical=True)
    canonical_outcome = coerce_outcome(outcome)
    if canonical_outcome.is_terminal and "finished_at" not in fields:
        fields["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if state.state_writer is not None:
        # Writer holds the same instance; this both transitions outcome
        # AND atomically rewrites the live .state file.
        state.state_writer.update(slug, status=outcome, **fields)
    else:
        state.stories.transition(slug, outcome=outcome, **fields)


def _landing_evidence_fields(result: CoordinatorResult) -> dict:
    """The accumulated entry's account of this story's landing.

    ``landing_status`` is the coordinator/scheduler's own three-state answer
    (``None`` = no landing owed, ``pending_integration`` = owed and not yet
    resolved, ``landed`` / ``failed`` = resolved), and ``landing`` carries the
    structured record once a landing attempt actually happened. Together they
    are what lets a later generation tell a completed story from one that
    merely reached Phase.DONE before its landing step ran (#2189).
    """
    _merge = result.merge if isinstance(result.merge, dict) else None
    return {
        "landing_status": getattr(result, "landing_status", None),
        "landing": build_landing_record(_merge),
        "merge": bool(_merge and _merge.get("merged", False)),
    }


def _story_allocation_entry(
    state: SprintExecutionState, story_state: CoordinatorState, story_cost: float | None
) -> dict | None:
    """Join this story's allocation with the sprint ceiling for reporting.

    The coordinator derives and enforces the per-story allocation but has no
    view of the sprint ceiling — budget governance at that level lives here.
    Joining the two on the story row is what makes "the story ran out of its
    own allocation while the sprint still had $74 left" readable as one fact
    rather than two disconnected ones (#2169).
    """
    from theforge.coordinator import story_budget as _story_budget

    block = _story_budget.evaluate_allocation_dict(
        getattr(story_state, "story_allocation", None),
        story_state.total_cost_measured,
    )
    exhausted = getattr(story_state, "allocation_exhausted", None)
    if block is None and not exhausted:
        return None
    block = block or {}
    _cost_snapshot = state.cost.snapshot()
    _spent = _cost_snapshot.spent
    _remaining = round(state.context.resolved.budget_usd - _spent, 4)
    block["reported_cost_usd"] = story_cost
    block["sprint_budget_usd"] = state.context.resolved.budget_usd
    block["sprint_spent_usd"] = round(_spent, 4)
    block["sprint_remaining_usd"] = _remaining
    # A lower-bound sprint total cannot certify headroom; say so rather
    # than asserting a number the sprint does not actually have (#1992).
    block["sprint_cost_measured"] = _cost_snapshot.measured
    block["sprint_headroom_remained"] = None if not _cost_snapshot.measured else _remaining > 0
    if exhausted:
        block["allocation_exhausted"] = exhausted
        block["status"] = "allocation_exhausted"
    return block


def _persist_accumulated_story_entries(
    state: SprintExecutionState,
) -> None:
    if state.context.sprint_id is None:
        return
    accumulated_by_ref = {
        ref: dict(entry) for ref, entry in state.recovered_prior_entries_by_ref.items()
    }
    for canonical_ref, entry in state.current_story_entries_by_ref.items():
        # This generation's entry replaces the prior one — except for the
        # failure history, which accumulates. Wholesale replacement is what
        # let a resume destroy the only recorded cause of the attempt it was
        # resuming from, so the story that failed became undiagnosable the
        # moment someone tried to fix it (#2030).
        prior_entry = accumulated_by_ref.get(canonical_ref) or {
            "failure_history": state.prior_failure_history_by_ref.get(canonical_ref) or []
        }
        merged_entry: dict = {"canonical_ref": canonical_ref, **entry}
        history = accumulate_failure_history(prior_entry, merged_entry)
        if history:
            merged_entry["failure_history"] = history
        accumulated_by_ref[canonical_ref] = merged_entry
    persist_accumulated_story_state(
        state.context.sprint_id,
        state.context.resolved.name,
        state.context.config.project_root,
        list(accumulated_by_ref.values()),
    )


# A queued PR's poll status, as a landing-attempt outcome. The scheduler treats
# every one of these as "not landed" and moves on, which is correct for it; the
# corpus needs the distinction, because only some of them can still resolve into
# a landing later (see ``RECONCILABLE_OUTCOMES``).
_QUEUED_PR_ATTEMPT_OUTCOME = {
    "timeout": "timeout",
    "closed": "closed",
    "checks_failed": "failed",
}


def _record_landing_evidence(
    state: SprintExecutionState,
    slug: str,
    result: CoordinatorResult,
    *,
    landing_mode: str,
    observer: str,
    attempt_outcome: str | None = None,
    carrier: dict | None = None,
) -> None:
    """Publish durable evidence for a landing attempt that just resolved (#2598).

    Distinct from :func:`_persist_story_landing`, which updates the sprint's own
    live scheduling state. This writes the immutable artifacts the *corpus*
    keeps: an attempt for every landing forge tried, and a positive assertion
    only where a successful landing was observed and can be named. The sprint's
    ``landing_status`` remains what the scheduler consults; it is no longer what
    a fresh clone has to believe.

    ``carrier`` overrides the merge info the carrier is resolved from. A batch
    member's changes exist only on its *leader's* branch, so the leader's merge
    info is what names the pull request that carried them; the member's own is
    empty. Its attested commits still come from its own run — the review and
    gate that judged it are its own.
    """
    from .landing_observation import attested_commits, observe_landing  # noqa: PLC0415

    run_id = result.state.run_id or state.sprint_run_id
    if not run_id:
        return
    merge_info = carrier if isinstance(carrier, dict) else result.merge
    reviewed, gated = attested_commits(result.state)
    observe_landing(
        state.context.config,
        run_id=run_id,
        slug=slug,
        landing_mode=landing_mode,
        landing_status=getattr(result, "landing_status", None),
        merge_info=merge_info if isinstance(merge_info, dict) else None,
        reviewed_commit=reviewed,
        gated_commit=gated,
        observer=observer,
        attempt_outcome=attempt_outcome,
    )


def _persist_story_landing(
    state: SprintExecutionState, slug: str, result: CoordinatorResult
) -> None:
    """Re-persist a dispatched story's landing evidence once it resolves.

    ``_persist_current_story_result`` runs before the landing step, so the
    entry it writes records the landing as *owed*. Every site that later
    resolves a landing — immediate integration, the pending-integration
    sweep, queued-PR polling, sprint wrap-up — calls this so the durable
    record matches reality before the next re-exec can read it. Without it a
    story that genuinely landed would stay recorded as landing-owed and be
    stranded on the next generation instead of reconciled.
    """
    task_ctx = state.context.slug_to_context.get(slug)
    if task_ctx is None:
        return
    canonical_ref = task_ctx[2]
    entry = state.current_story_entries_by_ref.get(canonical_ref)
    if entry is None:
        return
    entry.update(_landing_evidence_fields(result))
    _persist_accumulated_story_entries(state)


def _persist_current_story_result(
    state: SprintExecutionState,
    slug: str,
    result: CoordinatorResult,
    *,
    started_at: datetime.datetime,
    finished_at: datetime.datetime,
) -> None:
    task_ctx = state.context.slug_to_context.get(slug)
    if task_ctx is None:
        return
    # Every terminal path for a dispatched story lands here (completion,
    # timeout, exception), so this is the reliable record of which stories
    # actually consumed a coordinator run this generation — and therefore
    # which ones had a seeded prior cost overwritten by transition().
    state.ran_this_generation.add(slug)
    task, _source, canonical_ref = task_ctx
    display_key = (
        f"Issue #{canonical_ref.split(':')[1]}"
        if canonical_ref.startswith("issue:")
        else canonical_ref
    )
    preflight = (
        "cached"
        if getattr(result.state, "preflight_cached", False)
        else (result.state.preflight_verdict or "PROCEED")
    )
    outcome = "ALREADY_DONE" if preflight == "ALREADY_DONE" else result.phase.name
    outcome_source = (
        "preflight_verdict" if outcome == "ALREADY_DONE" and preflight == "ALREADY_DONE" else None
    )
    # The accumulated file is the record the NEXT generation reloads, so it
    # carries the same projection every other surface reports: this generation's
    # measured cost plus the intake and pre-restart spend attributed to the
    # story. Written here, while the story settles, rather than at a wrap-up a
    # stopped process never reaches (#2922).
    _story_cost = _projected_story_cost(
        state, slug, _story_reported_cost(result.state), overwrites_seed=True
    )
    state.current_story_entries_by_ref[canonical_ref] = {
        "path": display_key,
        "slug": slug,
        "outcome": outcome,
        "outcome_source": outcome_source,
        "verdict": None,
        "cost_usd": _story_cost,
        # Spend against BOTH governors (#2169): the story's own band-derived
        # allocation and the sprint ceiling. An allocation shortfall that
        # happened while sprint headroom remained is only visible as such
        # when both numbers sit on the same row.
        "story_allocation": _story_allocation_entry(state, result.state, _story_cost),
        "story_run_id": state.context.run_id,
        "preflight": preflight,
        "preflight_original_verdict": getattr(
            result.state, "preflight_cached_original_verdict", None
        ),
        "preflight_source_run_id": getattr(result.state, "preflight_cached_from_run_id", None),
        # A story dispatched on a preflight that produced no evidence is
        # still a story this run proceeded on unfounded values. Recording it
        # on the run's own row is what makes the condition countable from
        # the sprint record instead of from a log line at the moment of
        # failure — the property that let it repeat unnoticed for months
        # (#2346). Escalated stories carry it too: failure_action='escalate'
        # is the half that stopped, and it belongs on the same row.
        **preflight_degraded_row_fields(result.state),
        # The footprint collision scheduling used for this story (#2610).
        **preflight_likely_files_row_field(result.state),
        "error": result.state.error,
        "error_type": result.state.error_type,
        "outcome_code": result.state.error_type or outcome.lower(),
        # Landing evidence recorded beside the phase, because the phase alone
        # cannot say whether the story landed: this entry is written the
        # moment the coordinator returns, and for an approved story that is
        # BEFORE _attempt_integration runs. A re-exec in that window used to
        # leave a bare ``outcome: DONE`` that the next generation read as
        # proof of completion for a story still unmerged on its branch
        # (#2189). ``landing_status`` names the obligation and its state;
        # _persist_story_landing rewrites these once landing resolves.
        **_landing_evidence_fields(result),
        "batch_group": getattr(result.state, "preflight_batch_group", None),
        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "batch": 0,
        "depends_on": list(getattr(task, "depends_on", None) or []),
        "dependency_warnings": list(getattr(task, "dependency_warnings", None) or []),
        "inferred_dependencies": {
            "manifest": [
                dep
                for dep in (getattr(task, "depends_on", None) or [])
                if dep not in (getattr(task, "inferred_dependencies", None) or [])
            ],
            "github_blockers": list(getattr(task, "inferred_dependencies", None) or []),
        },
    }
    # A worker exception or timeout reaches here with the cause on its state.
    # Carrying it structurally is what keeps it in the accumulated row: the
    # ``error`` prose alone is a string a later generation overwrites (#2030).
    carry_failure_cause(
        state.current_story_entries_by_ref[canonical_ref],
        getattr(result.state, "abnormal_termination", None),
        prior_history=state.prior_failure_history_by_ref.get(canonical_ref),
    )
    _persist_accumulated_story_entries(state)


def _record_dropped_story_audit(state: SprintExecutionState, slug: str, cause_text: str) -> dict:
    """Write the per-run audit record for a story dropped before dispatch.

    A dropped story never reaches the coordinator, so it never reaches audit
    finalization either — which made the runs with the least recoverable
    context the only ones with no record at all (#2030). The record written
    here is deliberately the same shape as the worker-exception and
    worker-timeout ones: same synthetic result, same ``error``, same
    ``abnormal_termination`` block, differing only in ``kind``.

    A drop at a re-exec boundary is not necessarily a story that never ran:
    the generation before the boundary may have run dev, run review and
    committed an implementation. The record it flushed as it went is carried
    into this one, so the drop reports the phase that work reached and the
    budget it consumed instead of the INIT/$0.00 of a story that never
    started (#2214).

    Returns the cause record so the caller can retain it in sprint state as
    well. Best-effort: a story is never left un-dropped because its evidence
    could not be written.
    """
    task_ctx = state.context.slug_to_context.get(slug)
    if task_ctx is None:
        return {}
    dropped_at = datetime.datetime.now(datetime.timezone.utc)
    try:
        drop_result = _abnormal_story_result(
            slug,
            config=state.context.config,
            sprint_name=state.context.resolved.name,
            started_at=dropped_at,
            error=f"Dropped before dispatch: {cause_text}",
            error_type="LaunchGuardDrop",
            message=f"Launch guard dropped {slug} before dispatch: {cause_text}",
            # Never ESCALATE: the story did not escalate, it never ran, and
            # stamping ESCALATE would mark its worktree as escalated too.
            phase=Phase.INIT,
        )
        cause = build_abnormal_cause(
            kind=ABNORMAL_LAUNCH_GUARD_DROP,
            cause=cause_text,
            error_type="LaunchGuardDrop",
            phase="LAUNCH",
            run_id=drop_result.state.run_id,
            source="sprint.runner:launch-guard-drop",
        )
        drop_result.state.abnormal_termination = cause
        prior = load_prior_generation_story_audit(
            state.context.config.project_root,
            state.context.resolved.name,
            slug,
            exclude_run_id=drop_result.state.run_id,
        )
        if prior is not None:
            state.prior_generation_work[slug] = {
                **prior.summary,
                "recoverable_cost_usd": prior.recoverable_cost_usd,
            }
            _log(
                f"CARRIED {slug}: drop record carries the prior generation's work "
                f"(run {prior.summary['run_id']}, phase "
                f"{prior.summary['final_phase']}, cost "
                f"{prior.summary['cost_usd']})"
            )
        _write_story_audit(
            state.context.config,
            task_ctx[0],
            drop_result,
            sprint_id=state.context.sprint_id,
            # A dropped story shares its log directory with the generation
            # that actually ran it. Its audit.yaml is that run's evidence and
            # must survive the drop record, not be replaced by it.
            overwrite_story_audit=False,
            prior_generation=prior,
        )
        return cause
    except Exception as exc:  # noqa: BLE001 - evidence writing must not fail the sprint
        _log(f"WARN {slug}: could not write drop audit record: {exc}")
        return {}


def _end_collision_claim(state: SprintExecutionState, slug: str, why: str) -> None:
    """Drop a story's claim on its planned files, and the state behind it."""
    if state.collision_claims.pop(slug, None) is not None:
        _log(f"Collision claim released for {slug} ({why})")
    state.claim_results.pop(slug, None)
    state.file_footprints.pop(slug, None)
    # Nothing throttles a claim that no longer exists, and a slug that were
    # ever re-queued must be probed immediately rather than inheriting a
    # timestamp from its previous landing attempt.
    state.queued_probe_at.pop(slug, None)
    # plan_done is what re-derives the footprint; leaving it behind would
    # resurrect the claim on the next _release_plan_gates pass.
    with state.phase_lock:
        state.plan_done.pop(slug, None)


def _apply_batch_landing_to_member(
    state: SprintExecutionState,
    member_slug: str,
    task: TaskStory,
    result: CoordinatorResult,
    landing_status: str,
    leader_slug: str,
    group_id: str,
    carrier: dict | None = None,
) -> None:
    """Give one batch member the landing verdict its leader's branch reached.

    A member's changes exist only on the leader's branch, so reporting it
    DONE while that branch failed to land would claim work that is not on
    the base branch — the exact misreport this propagation exists to
    prevent. A member that failed on its own merits keeps its own verdict:
    the leader landing successfully does not retroactively approve it.

    This is a landing observation like any other, so it owes the corpus the
    same evidence (#2598). ``carrier`` is the *leader's* merge info, because
    that is what names the pull request or merge the member's changes actually
    rode in on — the member has none of its own. Its reviewed and gated commits
    are still its own, read off its own run.
    """
    if not result.success:
        return
    if landing_status == "landed":
        if result.landing_status == "landed":
            return
        result.landing_status = "landed"
        state.merged_slugs.add(member_slug)
        state.dag.mark_complete(member_slug)
        _set_outcome(state, member_slug, StoryOutcome.DONE, phase=result.phase.name, landed=True)
        _log(f"✓ {member_slug}: landed with batch leader {leader_slug}")
    else:
        if result.landing_status == "failed":
            return
        reason = (
            f"batch group {group_id}: leader {leader_slug} failed to land; "
            "this story's changes exist only on that branch"
        )
        result.landing_status = "failed"
        result.state.error = result.state.error or reason
        _set_outcome(
            state,
            member_slug,
            StoryOutcome.MERGE_FAILED,
            phase=result.phase.name,
            reason=reason,
        )
        _log(f"✗ {member_slug}: {reason}")
    _leader_mode = (carrier or {}).get("action") or state.context.config.workspace.on_approve
    _record_landing_evidence(
        state,
        member_slug,
        result,
        landing_mode=str(_leader_mode),
        observer=LANDING_OBSERVER_BATCH_MEMBER,
        carrier=carrier,
        attempt_outcome=None if landing_status == "landed" else "failed",
    )
    _write_story_audit(state.context.config, task, result, sprint_id=state.context.sprint_id)
    _times = state.story_times.get(member_slug)
    if _times is not None:
        _persist_current_story_result(
            state, member_slug, result, started_at=_times[0], finished_at=_times[1]
        )


def _resolve_batch_leader_landing(
    state: SprintExecutionState,
    leader_slug: str,
    landing_status: str,
    *,
    carrier: dict | None = None,
) -> None:
    """Record a batch leader's final landing status and push it to its members.

    Called from every site where a leader's landing reaches a terminal
    answer, including the queued-PR paths that resolve long after the
    member rows were written. A no-op for stories that do not lead a batch.

    ``carrier`` is the leader's merge info, carried through so each member's
    landing evidence can name the pull request or merge that actually delivered
    its changes rather than inferring one it does not have.
    """
    group_id = state.batch_group_of_leader.get(leader_slug)
    if group_id is None:
        return
    state.batch_leader_landing[group_id] = landing_status
    for member_slug, (
        member_group,
        member_task,
        member_result,
    ) in list(state.batch_member_records.items()):
        if member_group != group_id:
            continue
        _apply_batch_landing_to_member(
            state,
            member_slug,
            member_task,
            member_result,
            landing_status,
            leader_slug,
            group_id,
            carrier=carrier,
        )


def _resolve_queued_pr(state: SprintExecutionState, slug: str, *, blocking: bool, why: str) -> str:
    """Poll one queued PR and apply its landing verdict everywhere it is owed.

    The single site for queued-PR resolution during the work loop. Every
    caller owes the same bookkeeping — DAG, canonical outcome, durable
    landing evidence, audit, batch-leader propagation and, since #2234, the
    collision claim — and the copies drifted: the dependent-dispatch gate
    recorded neither the landing nor the claim release, so a merged parent
    left a gated sibling parked on a claim nothing would ever clear.

    With *blocking* false the PR is probed exactly once and anything short
    of a confirmed merge returns ``"pending"``, leaving it queued for the
    blocking poll that owns the failure verdict. Returns ``"merged"``,
    ``"failed"`` or ``"pending"``.
    """
    task, result, pr_url = state.queued_prs[slug]
    state.queued_probe_at[slug] = time.monotonic()
    poll_result = _poll_queued_pr(
        pr_url,
        state.context.config.project_root,
        state.context.config.workspace.merge_wait_timeout_seconds if blocking else 0,
        base_branch=state.context.config.workspace.base_branch,
    )
    if not blocking and poll_result["status"] != "merged":
        # A single probe cannot tell "still in the queue" from "decided
        # against us" without re-running the wait budget, and only a merge
        # is safe to conclude from it. Everything else stays queued.
        return "pending"

    # Out of queued_prs before the claim is reconciled: membership there is
    # itself a reason to keep a collision claim alive.
    del state.queued_prs[slug]
    # The throttle only exists to pace probes of a *queued* PR. This one is
    # decided; _end_collision_claim drops the entry on the paths that keep a
    # claim, and this covers a resolution with no claim behind it.
    state.queued_probe_at.pop(slug, None)

    if poll_result["status"] == "merged":
        state.merged_slugs.add(slug)
        state.dag.mark_complete(slug)
        result.landing_status = "landed"
        # The immutability marker: this DONE is confirmed-landed and must
        # not be clobbered by a later re-dispatch or wrap-up pass.
        _set_outcome(state, slug, StoryOutcome.DONE, landed=True)
        _persist_story_landing(state, slug, result)
        # This resolver observes the landing as surely as the wrap-up does, and
        # owes the corpus the same artifact (#2598). Its docstring already
        # listed durable landing evidence among the bookkeeping every caller
        # owes; the evidence was the one item that had not been wired.
        _record_landing_evidence(
            state,
            slug,
            result,
            landing_mode="merge-pr",
            observer=LANDING_OBSERVER_QUEUED_PR,
        )
        _write_story_audit(state.context.config, task, result, sprint_id=state.context.sprint_id)
        _resolve_batch_leader_landing(state, slug, "landed", carrier=result.merge)
        _end_collision_claim(state, slug, "queued PR merged")
        _log(f"INFO {slug}: queued PR merged ({why}); unblocking dependents")
        return "merged"

    from ..coordinator.completion import (  # noqa: PLC0415
        mark_merge_failed as _mark_mf,
    )

    _err = _queued_pr_failure_message(
        poll_result, pr_url, state.context.config.workspace.merge_wait_timeout_seconds
    )
    _mark_mf(result.state, result, _err, result.state.branch_name)
    _set_outcome(state, slug, StoryOutcome.MERGE_FAILED, phase=result.phase.name)
    _persist_story_landing(state, slug, result)
    # The poll status, not the scheduler's collapsed "failed": a queued PR forge
    # stopped waiting for stays reconcilable, and one that was closed does not.
    _record_landing_evidence(
        state,
        slug,
        result,
        landing_mode="merge-pr",
        observer=LANDING_OBSERVER_QUEUED_PR,
        attempt_outcome=_QUEUED_PR_ATTEMPT_OUTCOME.get(poll_result["status"], "failed"),
    )
    # Defensive/idempotent: the parent is normally already in the DAG's
    # _finished set (added when its PR was queued, via the
    # pending_integration classify branch), so its collision (soft) edge is
    # already released; this guarantees it on any path that skipped that
    # classification. _finished (not _completed) is what still blocks a hard
    # depends_on dependent. Actual redispatch of a released dependent onto
    # the current base comes from the dag.ready() re-check at the top of the
    # deadlock-cleanup branch.
    state.dag.mark_skipped(slug)
    _write_story_audit(state.context.config, task, result, sprint_id=state.context.sprint_id)
    _resolve_batch_leader_landing(state, slug, "failed", carrier=result.merge)
    _end_collision_claim(state, slug, f"queued PR {poll_result['status']}")
    _log(f"✗ {slug}: queued PR {poll_result['status']} ({why})")
    return "failed"


def _service_plan_gates(
    state: SprintExecutionState,
) -> None:
    """Release openable plan gates and stand down the unopenable ones."""
    # A CLAIM_PENDING_LANDING claim is resolvable *in this run*, but only
    # the queued-PR paths resolve it, and those run when the loop has no
    # active workers. A worker parked at its plan gate keeps the loop busy
    # forever, so the gate would wait on a PR that may have merged minutes
    # ago and then open on its own 7200s timeout instead (#2234). Probe
    # cheaply — one `gh pr view`, no queue wait — so the gate opens on the
    # merge. The blocking polls keep sole ownership of the failure verdict.
    if state.plan_gates and state.queued_prs:
        _probe_now: float | None = None
        for _q_slug in list(state.queued_prs):
            if state.collision_claims.get(_q_slug) != CLAIM_PENDING_LANDING:
                continue
            # Read the clock only once a probe is actually in question: this
            # runs on the 2s gate-service tick, and the common case has no
            # queued PR holding a claim at all.
            if _probe_now is None:
                _probe_now = time.monotonic()
            _last = state.queued_probe_at.get(_q_slug)
            if _last is not None and _probe_now - _last < _QUEUED_CLAIM_PROBE_SECONDS:
                continue
            _resolve_queued_pr(state, _q_slug, blocking=False, why="holding a collision gate")

    for _sd_slug in _release_plan_gates(
        state.plan_done,
        state.file_footprints,
        state.plan_gates,
        state.active,
        state.phase_lock,
        state.collision_claims,
        gate_hold_fn=state.publish_gate_hold,
    ):
        _sd_gate = state.plan_gates.pop(_sd_slug, None)
        state.gate_stood_down[_sd_slug] = (
            "collision gate stood down: the files it planned to change are held "
            "by preserved work that has not landed"
        )
        # stop_event BEFORE the gate, same ordering as the deadline teardown:
        # the worker must observe the shutdown flag when the gate wakes it.
        _sd_evt = state.stop_events.get(_sd_slug)
        if _sd_evt is not None:
            _sd_evt.set()
        if _sd_gate is not None:
            _sd_gate.set()


# How many times a landing refused on nothing but sibling story-run artifacts
# will republish and try again. Progress — the root's dirt actually changing —
# is what normally ends the loop; this only bounds a sprint whose siblings
# complete faster than the seam can commit them, so an approved story fails
# with an attributable reason instead of holding integration_lock forever.
_SIBLING_ARTIFACT_REPUBLISH_ATTEMPTS = 3


def _publish_sibling_artifacts(state: SprintExecutionState, slug: str) -> bool:
    """Commit pending story-run artifacts before this story merges into the root.

    Each story writes its canonical run record and knowledge summary straight
    into the shared project-root checkout as it completes, outside any lock. The
    quiescent mid-sprint publish (#2595) only fires with nothing in flight, so
    under ``max_parallel > 1`` a sibling's completion artifacts sit there
    untracked while an approved story reaches ``_merge_branch``'s dirty-root
    check — and that story is refused, after dev and review have been paid for,
    for something it did not do (#2602).

    Committing them here, inside ``integration_lock`` and immediately before the
    merge, is what makes the serialized integration path the one place that both
    writes and reads that state. This is a new *call site* for the unchanged
    #2595 publish, not a new publish: ``publish_pending_story_run_audits`` and
    its required ``lands_locally`` keyword predate this seam, and the scheduler
    loop still calls the same function with the sprint's own answer. Here that
    answer is ``True`` by construction — the caller only reaches this when the
    story is about to merge into the project-root base checkout, which is
    exactly what the flag asserts.

    Returns whether the publish completed; a failure is logged by the publish
    helper and reported by the caller rather than raised, because abandoning an
    approved story over a transport problem is the outcome this exists to stop.
    """
    published = publish_pending_story_run_audits(state, lands_locally=True)
    if not published:
        _log(f"WARN {slug}: pre-merge story run artifact publish did not complete")
    return published


def _publish_terminal_story_artifacts(
    state: SprintExecutionState,
    slug: str,
    *,
    lands_locally: bool,
    needs_quiescence: bool,
) -> bool:
    """Publish the run artifacts of a story that terminated without integrating.

    Every terminal outcome writes a canonical run record into the shared
    project-root checkout, but only the ones that reach ``_attempt_integration``
    publish it (``_publish_sibling_artifacts``, #2602). A story refused or
    cancelled *before* approval — a blocked preflight, an auth or budget
    cancellation, a collision stand-down, an abnormal worker exit — never gets
    there, and under ``max_parallel > 1`` the pass-level publish is gated on a
    quiescent pass that a live sibling denies. Its record then stands untracked
    in the project root and refuses every later story at WORKSPACE entry, for
    dirt the sprint itself created and no operator can be asked to reconcile
    (#2755).

    Publishing here closes that window. Where the sprint lands into the
    project-root checkout, the publish is serialized through ``integration_lock``
    — the same lock the merge path takes — so committing the index can never run
    underneath a concurrent merge. That is what makes it safe to publish while
    workers are still in flight, which the quiescence gate exists to prevent.

    **This helper acquires ``integration_lock`` itself and must never be called
    with that lock already held** — it is a bounded non-blocking flock and is not
    reentrant across file descriptors, so a call from inside
    ``_attempt_integration``'s locked block would spin until the lock times out.

    Idempotent by construction: the publish commits whatever is pending and is a
    no-op when nothing is. A failure is logged against the slug and reported,
    never raised — the terminal sweep at sprint exit is the fatal one.
    """
    try:
        if needs_quiescence:
            with integration_lock(state.context.config.project_root):
                published = publish_pending_story_run_audits(state, lands_locally=lands_locally)
        else:
            published = publish_pending_story_run_audits(state, lands_locally=lands_locally)
    except TimeoutError as exc:
        # Another process holds the integration lock. Deferring is correct: the
        # next quiescent pass, or the terminal sweep, publishes the same record.
        _log(f"WARN {slug}: terminal story run artifact publish could not take the lock: {exc}")
        return False
    if not published:
        _log(f"WARN {slug}: terminal story run artifact publish did not complete")
    return published


def _attempt_integration(
    state: SprintExecutionState,
    slug: str,
    task: TaskStory,
    result: CoordinatorResult,
) -> bool:
    """Attempt to land an approved story under integration_lock.

    Returns True when integration was attempted (success, failure, or queued).
    Returns False when dependencies are unmet — caller should retry later.

    This is the sole merge site for sprint execution.  Workers never merge;
    they set landing_status="pending_integration" and return.
    """
    if not all(dep in state.merged_slugs for dep in task.depends_on):
        result.landing_status = "pending_integration"
        _write_story_audit(state.context.config, task, result, sprint_id=state.context.sprint_id)
        return False

    branch = state.context.config.workspace.branch_pattern.format(slug=slug)
    wt = state.context.config.project_root / state.context.config.workspace.path_pattern.format(
        slug=slug
    )

    # Read effective mode from the pending merge action stored by _finalize_approve.
    # Falls back to config.workspace.on_approve for legacy/direct callers.
    effective_on_approve = (result.merge or {}).get(
        "action"
    ) or state.context.config.workspace.on_approve
    story_run_id = result.state.run_id or state.sprint_run_id

    story_logger = StructuredLogger(
        run_id=story_run_id,
        project=state.context.config.project,
        task=task.slug,
        log_file=state.context.config.log.log_file,
        enabled=state.context.config.log.enabled,
        project_root=state.context.config.project_root,
    )

    with integration_lock(state.context.config.project_root):
        from ..coordinator.completion import (  # noqa: PLC0415
            land_story,
            resolve_landing_review,
        )

        parsed_review = resolve_landing_review(result.state)

        def _land() -> tuple[dict, str]:
            return land_story(
                state.context.config,
                task,
                branch,
                wt,
                parsed_review,
                result.state,
                effective_on_approve,
                logger=story_logger,
                run_id=story_run_id,
            )

        project_root = state.context.config.project_root
        lands_in_root = effective_on_approve == "merge"
        publish_ok = _publish_sibling_artifacts(state, slug) if lands_in_root else True
        merge_info, landing_status = _land()

        # A sibling can finish in the few git calls between that publish and
        # _merge_branch's own dirty check, which is the same race one step
        # narrower. So the seam is tolerant as well as early: while the refusal
        # names nothing but story-run artifacts, republish and land again.
        #
        # One retry was not enough — a second sibling finishing in the second
        # window strands the story exactly as the first one used to. What bounds
        # this is progress, not a single shot: each pass must actually change
        # the root's dirt, and a publish that reports success while leaving the
        # same files uncommitted ends it. The attempt cap is a backstop for a
        # sprint whose siblings land faster than this loop can publish them, so
        # an approved story fails loudly rather than spinning under the lock.
        #
        # Operator dirt is untouched by any of this: the loop never runs unless
        # every dirty path is a story-run artifact, so unrelated changes refuse
        # on the first attempt exactly as before.
        republishes = 0
        while (
            lands_in_root
            and landing_status == "failed"
            and republishes < _SIBLING_ARTIFACT_REPUBLISH_ATTEMPTS
            and project_root_dirt_is_story_run_artifacts_only(project_root)
        ):
            dirt_before = coordinator_workspace.project_root_dirty_status(project_root)
            republishes += 1
            _log(
                f"INFO {slug}: landing refused on sibling run artifacts; republishing "
                f"and retrying ({republishes}/{_SIBLING_ARTIFACT_REPUBLISH_ATTEMPTS})"
            )
            publish_ok = _publish_sibling_artifacts(state, slug)
            if not publish_ok:
                break
            if coordinator_workspace.project_root_dirty_status(project_root) == dirt_before:
                # The publish reported success and the same files are still
                # uncommitted, so landing again would refuse for the same
                # reason. Stopping here keeps the failure attributable.
                _log(
                    f"WARN {slug}: republish left the same story run artifacts uncommitted; "
                    f"not retrying the landing again"
                )
                break
            merge_info, landing_status = _land()

        if republishes:
            # The retries are part of what happened to this landing, so the
            # audit record carries them rather than leaving the count to be
            # reconstructed from the log.
            merge_info["sibling_artifact_republishes"] = republishes

        if lands_in_root and landing_status == "failed" and not publish_ok:
            # Attribution the operator cannot otherwise get: a swallowed publish
            # failure and ordinary sibling dirt produce the identical merge
            # error, and only one of them is a checkout-health problem.
            publish_state = read_audit_publish_state(state.context.config.project_root)
            merge_info["story_run_audit_publish_state"] = publish_state
            _log(
                f"WARN {slug}: story run artifact publish did not complete before this "
                f"landing (publish state: {publish_state or 'unrecorded'}); the merge "
                f"refusal below may be that failure rather than ordinary sibling dirt"
            )

    result.merge = merge_info
    result.landing_status = landing_status
    if landing_status == "failed":
        from ..coordinator.completion import mark_merge_failed  # noqa: PLC0415

        mark_merge_failed(
            result.state,
            result,
            merge_info.get("error"),
            branch,
            inherited_dev_residue=bool(merge_info.get("inherited_dev_residue")),
        )

    # The accumulated entry was written before this landing ran and records
    # the landing as owed. Now that it has resolved either way, rewrite it so
    # the durable record a later re-exec reads matches what happened (#2189).
    _persist_story_landing(state, slug, result)
    _record_landing_evidence(
        state,
        slug,
        result,
        landing_mode=effective_on_approve,
        observer=LANDING_OBSERVER_INTEGRATION,
    )

    if merge_info.get("merged"):
        state.merged_slugs.add(slug)
        state.dag.mark_complete(slug)
        _write_story_audit(state.context.config, task, result, sprint_id=state.context.sprint_id)
        _resolve_batch_leader_landing(state, slug, "landed", carrier=merge_info)
        if effective_on_approve == "merge-pr" and not merge_info.get("auto_merge_queued", False):
            ci_result = poll_required_checks(
                state.context.config.project_root,
                state.context.config.workspace.base_branch,
                state.context.config.workspace.ci_check_timeout_seconds,
            )
            if ci_result["status"] in {"fail", "timeout"}:
                failing = ", ".join(ci_result["failing_checks"]) or "pending required checks"
                # A check that stopped without a verdict is not a red result;
                # naming it keeps the halt reason honest about what is known
                # versus merely unknown (#2270).
                unjudged = ", ".join(ci_result.get("unjudged_checks") or [])
                state.stop.stop(
                    "Required CI checks "
                    f"{ci_result['status']} after merging {slug} "
                    f"at {ci_result['sha']}: {failing}"
                    + (f" (no verdict produced by: {unjudged})" if unjudged else ""),
                    halt_slug=slug,
                )
                _log(
                    f"HALT {slug}: required CI checks {ci_result['status']} "
                    f"for {ci_result['sha']} ({failing})"
                )
        return True

    if merge_info.get("merge_queued"):
        state.queued_prs[slug] = (task, result, merge_info["pr_url"])
        _write_story_audit(state.context.config, task, result, sprint_id=state.context.sprint_id)
        _log(f"INFO {slug}: PR auto-merge queued; waiting for GitHub to report MERGED")
        return True

    result.state.error = merge_info.get("error") or "integration failed"
    _log(f"WARN {slug}: integration failed: {merge_info.get('error')}")
    _write_story_audit(state.context.config, task, result, sprint_id=state.context.sprint_id)
    _resolve_batch_leader_landing(state, slug, "failed", carrier=merge_info)
    return True


def _fold_entry_intake_cost(
    context: SprintRunContext,
    state: SprintExecutionState,
    log: Callable[[str], None],
) -> None:
    """Roll pre-``run_sprint`` intake remediation spend into the sprint ledger.

    Entry-level intake remediation runs in the CLI before the sprint starts and
    spends the same sprint-authorized budget, so the sprint total is wrong
    without it. Called once, before the baseline-gate abort branch, because an
    abort before any story starts has still spent this money and an operator
    deciding whether to retry needs the figure (#2434).
    """
    if not context.entry_intake_outcomes:
        return
    for issue_num, outcome in context.entry_intake_outcomes.items():
        if _intake_outcome_cost_measured(outcome) is None:
            state.cost.flag_unmeasured_here(f"entry-intake:issue-{issue_num}")
    entry_intake_cost = sum(
        _intake_outcome_cost(o) for o in context.entry_intake_outcomes.values()
    )
    if entry_intake_cost > 0.0:
        state.cost.add(entry_intake_cost)
        for issue_num, outcome in context.entry_intake_outcomes.items():
            state.cost.note_non_story(f"issue-{issue_num}", _intake_outcome_cost(outcome))
        log(f"Entry-intake remediation cost: ${entry_intake_cost:.4f} (rolled into sprint total)")


def run_sprint(context: SprintRunContext) -> SprintResult:
    """Run all stories in a sprint with optional concurrency.

    Takes the run's context and nothing else. Everything a sprint consults is
    settled before the first story is dispatched, so it arrives as one named,
    frozen object rather than as a parameter list that grows by one every time
    the sprint learns to consult something new. Use
    :meth:`SprintRunContext.for_sprint` to build it from a manifest path or a
    pre-resolved sprint; the run body has no path-shaped assumptions left.

    Everything the run *mutates* lives on the :class:`SprintExecutionState` this
    function builds from the context — never in this frame, so a function that
    needs sprint state can be moved out of here by taking the state (#2399).

    When max_parallel > 1, stories with no unmet dependencies are launched
    concurrently up to max_parallel. Budget is pooled across all workers.
    Merge ordering respects dependency order when auto_merge is True.

    When max_parallel == 1 (default), behavior is identical to the original
    sequential runner.

    Args:
        context: The sprint's run context — see :class:`SprintRunContext` for
            what each field means and :meth:`SprintRunContext.for_sprint` for
            building one.

    Returns:
        SprintResult with per-story outcomes and aggregate stats.
    """
    # The context is rebound (never mutated) exactly twice below: once for the
    # adaptively scaled gate timeout, once for the sprint id this run resolves.
    _ctx = context

    # A re-exec'd launch (source changed mid-sprint) keeps the original argv and
    # therefore never carries ``--resume``, but it MUST run the same merged-state
    # reconciliation a resume would: triage every manifest story against merged
    # state, exclude already-merged stories from preflight/dispatch, and pre-mark
    # them complete in the DAG. Otherwise a story whose PR already landed in the
    # prior (killed) generation is re-entered through WORKSPACE and its DONE
    # outcome is overwritten with a bogus FAILED. Treat re-exec as
    # resume-equivalent for all reconciliation/skip paths.
    reconcile = _ctx.resume or _ctx.reexec

    # Stories still executing from before the re-exec. Everything downstream that
    # would otherwise treat their state as foreign — the orphan worktree sweep,
    # the baseline gate's "nothing has started yet" precondition, the gate-timeout
    # load model — is told about them explicitly. The context normalises the two
    # sets and joins them as ``live_slugs``.
    _live_story_slugs: set[str] = set(_ctx.live_slugs)

    # Establish that the agents are reachable BEFORE committing wall clock or
    # budget to them (#1952). This runs ahead of the baseline gate, the base
    # pull, and every worktree touch, so a dead credential costs seconds and
    # leaves no story with a verdict — the run simply never happened.
    enforce_sprint_auth_readiness(_ctx.config, log=_log)

    # Defensive scrub for the root checkout used by sprint commands.
    _scrub_root_forge_artifacts(_ctx.config)
    sweep_orphan_worktrees(
        _ctx.config.project_root, _ctx.config, protected_slugs=_live_story_slugs
    )

    max_parallel = (
        _ctx.resolved.max_parallel
        if _ctx.resolved.max_parallel is not None
        else _ctx.config.sprint.max_parallel
    )
    base_worker_timeout_seconds = _ctx.config.sprint.worker_timeout_seconds

    task_entries = _ctx.resolved.stories
    dependent_slugs = {dep for task, _src, _ref in task_entries for dep in task.depends_on}

    # Dependency parents this sprint can actually dispatch and integrate itself.
    # ``dependent_slugs`` is the raw edge target set, which also names external
    # references — an issue already merged on main, or one this sprint does not
    # carry. Those parents never run here and never merge into the project-root
    # checkout, so a landing obligation derived from the raw set would refuse a
    # PR-landing sprint whose only edge points outside the manifest (#2048
    # review iteration 2).
    in_manifest_dependency_parents = dependent_slugs & set(_ctx.slug_to_context)

    # Which landing paths in this sprint actually merge into the project-root
    # checkout? Both flags mirror the effective-mode resolution the workers
    # reach (``effective_am`` below → ``completion._finalize_approve``), because
    # a landing precondition asserted for a merge that never happens refuses
    # work forge could have done.
    #
    #   * ``on_approve: merge`` merges locally in either mode.
    #   * ``--auto-merge`` forces ``"merge"`` only where the flag survives to
    #     the worker. ``effective_am`` is hard-False in parallel mode, so the
    #     flag is dropped there and nothing merges locally on its account.
    #   * A dependency parent merges locally in sequential mode — ``effective_am``
    #     forces ``"merge"`` for it whatever ``on_approve`` says — and in
    #     parallel mode through the scheduler's ``pending_integration``
    #     conversion. That conversion fires only for a story that produced no
    #     landing of its own (``landing_status is None``), i.e. ``on_approve``
    #     ``"pr"`` or ``"none"``. Under ``"merge-pr"`` the parent lands through
    #     its own PR and never touches the project-root checkout — the same
    #     carve-out the ``auto_enabled_dependency_merges`` warning makes below.
    config_lands_in_project_root = _ctx.config.workspace.on_approve == "merge" or (
        _ctx.auto_merge and max_parallel <= 1
    )
    dependency_parents_land_in_project_root = (
        max_parallel <= 1 or _ctx.config.workspace.on_approve != "merge-pr"
    )
    # Whether the mid-sprint project-memory publish has to wait for a quiet
    # pass. Only a run that lands in the project root does: the constraint is
    # about a concurrent merge into the checkout the publish commits from, and
    # a run with no local landings has no such merge to protect.
    _publish_needs_quiescence = config_lands_in_project_root

    # Stories of this sprint whose agent survived the re-exec. They stay in the
    # DAG and are dispatched like any other story, but through the deferred path
    # (``_run_inherited_story``): wait for the inherited agent, then resume from
    # whatever it left behind. Excluded only from the work that would collide
    # with a running agent — startup triage, intake, batch preflight — because
    # each of those reads or writes a worktree that is being mutated right now.
    _inflight_slugs: set[str] = {s for s in _live_story_slugs if s in _ctx.slug_to_context}

    # Does ANY story in this sprint merge into the project-root base checkout?
    # This is a sprint-wide question, not a per-story one: story N merging
    # locally leaves the base branch ahead of origin when story N+1's worktree
    # is cut, so every story's workspace guard needs the sprint's answer, not
    # its own effective_auto_merge. Parallel mode never eager-merges (see the
    # effective_am computation below, which forces False when max_parallel > 1);
    # sequential mode merges for --auto-merge and, independently of it, for any
    # story other stories depend on — which is the in-manifest parent set, the
    # same one ``effective_am`` tests per story. The raw edge-target set would
    # also count purely external references, which no story here ever merges.
    _sprint_lands_locally = coordinator_workspace._base_branch_lands_locally(
        _ctx.config,
        auto_merge=(
            max_parallel <= 1 and (_ctx.auto_merge or bool(in_manifest_dependency_parents))
        ),
    )

    total = len(task_entries)
    noun = "stories" if total != 1 else "story"
    # Substrate provenance: name the runtime executing this sprint so the
    # operator can never be confused about which install is in effect. See
    # theforge.cli.substrate for the failure mode this closes.
    try:
        from theforge.cli.substrate import emit_provenance

        emit_provenance(
            cwd=_ctx.config.project_root,
            bypass_mismatch=bool(_ctx.force),
        )
    except Exception:
        # Provenance is operator-visible information, not a correctness gate;
        # never let a detection failure block sprint start.
        pass
    print(
        f'[sprint] "{_ctx.resolved.name}"  {total} {noun}  budget=${_ctx.resolved.budget_usd:.2f}'
        f"  parallel={max_parallel}",
        file=sys.stderr,
        flush=True,
    )

    # Resolve adaptive per-gate timeout once, propagate via dataclasses.replace
    # so each per-story coordinator invocation reads the scaled value through
    # the existing config.validation.gate_timeout path.
    #
    # gate_timeout, gate_cpu_cores, and gate_timeout_scale are validated at
    # config-load time (theforge.config.load), so a malformed value here is
    # not a recoverable "mock or partial config" case — it is a config bug
    # that must fail fast rather than silently disable adaptive scaling.
    _baseline_gate_timeout = int(_ctx.config.validation.gate_timeout or 600)
    _host_cores = os.cpu_count() or 1
    _gate_cpu_raw = _ctx.config.validation.gate_cpu_cores
    _gate_cpu_cores = int(_gate_cpu_raw) if _gate_cpu_raw else None
    try:
        _observed_host_load = float(os.getloadavg()[0])
    except (AttributeError, OSError):
        _observed_host_load = None
    _mode_raw = _ctx.config.validation.gate_timeout_scale
    if _mode_raw is None:
        _mode = "adaptive"
    elif isinstance(_mode_raw, str):
        _mode = _mode_raw
    else:
        # Not a real forge.yaml value (e.g. a test double) — config-load
        # already rejects malformed strings, so this is test scaffolding,
        # not an operator misconfiguration. Fall back to the safe default
        # rather than raising on incidental mock attribute access.
        _mode = "adaptive"
    # A fresh start contends only with its own configured parallelism. A
    # continuation additionally contends with the agents it inherited, so the
    # derived limit must count them — otherwise the gate is measured against a
    # load model that describes a different run than the one executing.
    _gate_timeout_resolution = resolve_effective_gate_timeout(
        baseline=_baseline_gate_timeout,
        max_parallel=max_parallel,
        host_cores=_host_cores,
        gate_cpu_cores=_gate_cpu_cores,
        mode=_mode,
        running_stories=len(_live_story_slugs),
        observed_host_load=_observed_host_load,
    )
    if _gate_timeout_resolution is not None:
        print(
            f"[sprint] gate_timeout: {_gate_timeout_resolution.reason}",
            file=sys.stderr,
            flush=True,
        )
    _setup_timeout_resolution = None
    _setup_timeout_reason: str | None = None
    _workspace_setup_command_raw = getattr(_ctx.config.workspace, "setup_command", None)
    _workspace_setup_command = (
        _workspace_setup_command_raw
        if isinstance(_workspace_setup_command_raw, str) and _workspace_setup_command_raw
        else None
    )
    _workspace_setup_timeout_raw = getattr(_ctx.config.workspace, "setup_timeout", 120)
    try:
        _workspace_setup_timeout = int(_workspace_setup_timeout_raw)
    except (TypeError, ValueError):
        _workspace_setup_timeout = 120
    _can_rebind_workspace = dataclasses.is_dataclass(_ctx.config.workspace)
    if _workspace_setup_command:
        _resolved_setup_timeout = resolve_effective_gate_timeout(
            baseline=_workspace_setup_timeout,
            max_parallel=max_parallel,
            host_cores=_host_cores,
            gate_cpu_cores=_gate_cpu_cores,
            mode=_mode,
            running_stories=len(_live_story_slugs),
            observed_host_load=_observed_host_load,
        )
        if _can_rebind_workspace:
            _setup_timeout_resolution = _resolved_setup_timeout
            _setup_timeout_reason = _resolved_setup_timeout.reason
        else:
            _setup_timeout_reason = (
                "baseline="
                f"{_workspace_setup_timeout}s mode={_resolved_setup_timeout.mode} "
                f"parallel={_resolved_setup_timeout.max_parallel} "
                f"gate_cpu_cores={_resolved_setup_timeout.gate_cpu_cores} "
                f"host_cores={_resolved_setup_timeout.host_cores} "
                f"factor={_resolved_setup_timeout.factor:.2f} "
                f"candidate_effective={_resolved_setup_timeout.effective_timeout}s "
                f"effective={_workspace_setup_timeout}s "
                "workspace_rebind=skipped(non-dataclass)"
            )
        print(
            f"[sprint] workspace.setup_timeout: {_setup_timeout_reason}",
            file=sys.stderr,
            flush=True,
        )
    if _gate_timeout_resolution is not None and _gate_timeout_resolution.overcommit:
        _observed = _gate_timeout_resolution.observed_host_load
        _warning_load = _gate_timeout_resolution.warning_host_load
        _hc = _gate_timeout_resolution.host_cores
        if _gate_timeout_resolution.mode == "fixed":
            _warning_suffix = (
                "already indicates contention; fixed gate_timeout leaves the baseline "
                "unchanged, so concurrent work on this host may still stretch gate completion"
            )
        else:
            _warning_suffix = (
                "already indicates contention; the expanded gate_timeout may still be "
                "insufficient while concurrent work on this host persists"
            )
        _warning_load_fragment = ""
        if _warning_load is not None and _warning_load != _observed:
            _story_noun = "story" if _gate_timeout_resolution.running_stories == 1 else "stories"
            _warning_load_fragment = (
                f" after discounting {_gate_timeout_resolution.running_stories} inherited "
                f"{_story_noun} ({_warning_load:.2f} effective)"
            )
        print(
            f"[sprint] WARNING: gate CPU observed host load ({_observed:.2f} 1m / {_hc} cores) "
            f"{_warning_load_fragment} {_warning_suffix}".lstrip(),
            file=sys.stderr,
            flush=True,
        )
    _rebound_config = _ctx.config
    _did_rebind_config = False
    if (
        _gate_timeout_resolution is not None
        and _gate_timeout_resolution.effective_timeout != _baseline_gate_timeout
    ):
        _rebound_config = replace(
            _rebound_config,
            validation=replace(
                _rebound_config.validation,
                gate_timeout=_gate_timeout_resolution.effective_timeout,
            ),
        )
        _did_rebind_config = True
    if (
        _can_rebind_workspace
        and _setup_timeout_resolution is not None
        and _setup_timeout_resolution.effective_timeout != _workspace_setup_timeout
    ):
        _rebound_config = replace(
            _rebound_config,
            workspace=replace(
                _rebound_config.workspace,
                setup_timeout=_setup_timeout_resolution.effective_timeout,
            ),
        )
        _did_rebind_config = True
    if _did_rebind_config:
        # Rebind the context, not a frame local: the scaled timeout has to be the
        # one every consulted-config read below sees, and there is only one place
        # a sprint reads its config from.
        _ctx = replace(
            _ctx,
            config=_rebound_config,
        )

    for warning in _agent_cost_tracking_warnings(_ctx.config):
        _log(warning)
    for task, _src, _ref in task_entries:
        for phrase in task.dependency_warnings:
            _log(
                "WARN: dependency-shaped prose ignored for "
                f"{task.slug} ({task.name}): {phrase!r}; "
                "declare dependencies with GitHub blocked-by relationships "
                "or leading issue metadata"
            )

    _cli_run_id = _ctx.run_id

    # Stable sprint_id — does not change across run_id rollovers or --resume.
    # Used to aggregate story outcomes across all worker-process boundaries.
    _sprint_id: str | None = None
    try:
        _sprint_id = _get_or_create_sprint_id(_ctx.resolved.name, _ctx.config.project_root)
    except Exception:
        pass

    # Everything the sprint consults is settled by this point, so the context is
    # final from here on and the mutable half gets its own named object. Below
    # this line the nested functions read ``_ctx`` and write ``_sprint_state``,
    # and nothing a relocated function needs is held in this frame: the two
    # questions the old ``nonlocal`` writes left open — what accumulates cost,
    # and what decides the sprint has stopped — are answered by
    # ``_sprint_state.cost`` and ``_sprint_state.stop`` and by nothing else.
    _ctx = replace(_ctx, sprint_id=_sprint_id)
    _sprint_state = SprintExecutionState.for_run(_ctx)

    # Ownership records the pre-exec image left behind. Adopted rather than
    # ignored: this is the same process, so they are this run's own claims, and
    # whichever of them the launch guard did not turn into scheduled work (a
    # reconciled prior landing, a story dropped for another reason) must still be
    # released when the run ends rather than outliving it (#2617).
    try:
        from .story_executions import scan_story_executions as _scan_executions  # noqa: PLC0415

        _sprint_state.owned_story_executions.update(
            record.slug for record in _scan_executions(_ctx.config.project_root).owned
        )
    except Exception as exc:  # noqa: BLE001 - adoption is hygiene, never a launch blocker
        _log(f"WARN could not read this sprint's story ownership records: {exc}")

    def _settle_terminal_story_audit(
        _slug: str,
        _task: TaskStory,
        _result: CoordinatorResult,
        *,
        telemetry_snapshot: dict | None = None,
    ) -> None:
        """Write a terminated story's canonical audit *and* publish it, as one step.

        This is the single seam for every scheduler-owned terminal outcome that
        does not attempt integration — a refusal, an escalation, a cancellation,
        an abnormal worker exit. Writing and publishing are one operation here
        rather than two adjacent calls because the gap between them is exactly
        the defect: a record written into the shared project-root checkout and
        left unpublished is dirt that refuses every later story at WORKSPACE
        entry (#2755), and a new terminal branch that called only the writer
        would reintroduce that silently.

        The stories that *do* integrate keep their own seam:
        ``_attempt_integration`` writes the record and publishes inside
        ``integration_lock`` (``_publish_sibling_artifacts``, #2602).

        See ``_publish_terminal_story_artifacts`` for why the publish cannot
        wait for a quiescent pass, and for the rule that it must never run while
        ``integration_lock`` is already held — every call site of this closure is
        in the scheduler thread, outside ``_attempt_integration``.
        """
        _write_story_audit(
            _ctx.config,
            _task,
            _result,
            sprint_id=_ctx.sprint_id,
            telemetry_snapshot=telemetry_snapshot,
        )
        _publish_terminal_story_artifacts(
            _sprint_state,
            _slug,
            lands_locally=_sprint_lands_locally,
            needs_quiescence=_publish_needs_quiescence,
        )

    # Sprint-level structured logger
    _sprint_state.sprint_run_id = _generate_run_id()
    _sprint_logger = StructuredLogger(
        run_id=_sprint_state.sprint_run_id,
        project=_ctx.config.project,
        task=_ctx.resolved.name,
        log_file=_ctx.config.log.log_file,
        enabled=_ctx.config.log.enabled,
        project_root=_ctx.config.project_root,
    )
    _sprint_logger.emit(
        "run_start",
        stories=[ref for _, _, ref in task_entries],
        budget_usd=_ctx.resolved.budget_usd,
        max_parallel=max_parallel,
        resume=_ctx.resume,
    )

    # Create sprint-level log directory
    _sprint_log_dir = _ctx.config.project_root / ".forge" / "logs" / _ctx.resolved.name
    try:
        _sprint_log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        _sprint_log_dir = None  # type: ignore[assignment]

    # Pin the configuration this sprint runs under (#1980). Captured once, on
    # first entry, and reloaded on every re-entry — --resume, and the re-exec
    # that follows a source update, which is the path that first exposed this:
    # a story landing a config-contract change made the sprint's own forge.yaml
    # invalid, and the next re-entry read the changed file. A project root that
    # has since moved off the pin is reported as drift, never silently adopted.
    _config_snapshot: "SprintConfigSnapshot | None" = None
    if _ctx.sprint_id:
        try:
            _config_snapshot = capture_or_load(_ctx.config.project_root, _ctx.sprint_id)
        except Exception:  # pragma: no cover - snapshotting must never abort a sprint
            _config_snapshot = None
    config_snapshot_mod.activate(_config_snapshot)
    if _config_snapshot is not None and _config_snapshot.present:
        _log(
            f"Sprint config pinned: forge.yaml {(_config_snapshot.digest or '')[:12]} "
            f"captured {_config_snapshot.captured_at}"
            + (" (reused from earlier entry)" if _config_snapshot.reused else "")
        )
        _sprint_logger.emit(
            "sprint_config_pinned",
            digest=_config_snapshot.digest,
            captured_at=_config_snapshot.captured_at,
            source=_config_snapshot.source,
            reused=_config_snapshot.reused,
        )
        _entry_drift = config_snapshot_mod.check_drift(_config_snapshot, story=None)
        if _entry_drift is not None:
            _log(f"⚠ {config_snapshot_mod.describe_drift(_entry_drift)}")
            _sprint_logger.emit("sprint_config_drift", **_entry_drift)

    # Failure causes already on disk for this sprint (see
    # SprintExecutionState.prior_failure_history_by_ref).
    if _ctx.sprint_id:
        from .audit import _load_accumulated_stories  # noqa: PLC0415

        for _prior in _load_accumulated_stories(_ctx.sprint_id, _ctx.config.project_root):
            if not isinstance(_prior, dict):
                continue
            _ref = _prior.get("canonical_ref")
            _history = accumulate_failure_history(_prior, None)
            if isinstance(_ref, str) and _ref and _history:
                _sprint_state.prior_failure_history_by_ref[_ref] = _history

    # Landing precondition, first pass: a dirty project root makes every local
    # merge refuse, and discovering that at landing means the story's full
    # dev+review spend is already sunk (#2048). Only the configuration-level
    # answer is used here, because it is the only one knowable this early. It
    # deliberately differs from _sprint_lands_locally, which suppresses itself
    # in parallel mode: parallel stories skip the *eager* merge but still land
    # through the integration step, so on_approve == "merge" merges locally
    # regardless of max_parallel.
    #
    # The dependency-derived obligation is NOT evaluated here. Whether an
    # in-manifest parent actually merges depends on the satisfied and
    # resume-triage sets, which are only resolved further down — asserting it
    # now would refuse sprints whose parent is already merged and will never be
    # dispatched. That term is checked at the "dependency-resolved" pass below,
    # still ahead of every agent spend.
    _refuse_dirty_root_before_spend(
        _ctx.config,
        lands_in_project_root=config_lands_in_project_root,
        stage="sprint-entry",
    )

    if _project_root_is_git_checkout(_ctx.config.project_root):
        coordinator_workspace.assert_base_branch_checked_out(
            _ctx.config, operation="sprint launch"
        )
    if not _ctx.no_pull and _project_root_is_git_checkout(_ctx.config.project_root):
        coordinator_workspace.pull_base_branch(_ctx.config, lands_locally=_sprint_lands_locally)
        # Landings owed by earlier sprints may have resolved while nothing was
        # watching — a queued auto-merge that completed, a PR an operator merged
        # last week. Closing them out here is the post-exit observation seam
        # asynchronous modes need (#2598): it runs before this sprint writes
        # anything of its own, and publishes so the evidence it produces cannot
        # then refuse this sprint's first story.
        try:
            if reconcile_landing_evidence(_ctx.config):
                publish_story_run_artifacts_for_config(
                    _ctx.config, lands_locally=_sprint_lands_locally
                )
        except Exception as _reconcile_exc:  # noqa: BLE001 — never blocks a sprint
            _log(f"Warning: landing-evidence reconciliation did not complete: {_reconcile_exc}")

    def _publish_sprint_phase(
        phase: str,
        *,
        detail: str | None = None,
        started_at: str | None = None,
    ) -> None:
        """Record the sprint-level phase in the live state file, when there is one.

        Headless invocations pass no ``run_id`` and have no state file; those
        callers simply produce no live phase.
        """
        if not _ctx.run_id:
            return
        update_state_phase(
            _ctx.run_id, _ctx.config.project_root, phase, detail=detail, started_at=started_at
        )

    baseline_started_at = datetime.datetime.now(datetime.timezone.utc)
    _continuation_reason = _continuation_evidence(
        reexec=_ctx.reexec,
        live_story_slugs=_ctx.live_story_slugs,
        unresolved_slugs=_ctx.unresolved_live_slugs,
        registered_slugs=_ctx.registered_live_slugs,
    )
    if _continuation_reason is not None:
        baseline_gate = _skipped_baseline_gate(_ctx.config, _continuation_reason)
        _sprint_logger.emit(
            "baseline_gate_skipped",
            reason="reexec_continuation",
            evidence=_continuation_reason,
            live_stories=sorted(_live_story_slugs),
        )
    else:
        # The baseline gate is the sprint's first potentially many-minute step.
        # Publishing it as its own phase (with the moment it began) is what lets
        # `forge status` distinguish a slow gate from a stuck sprint; the phase
        # is cleared the moment the gate returns so the elapsed time on screen
        # is never that of finished work (#2014).
        _publish_sprint_phase(
            SPRINT_PHASE_BASELINE_GATE,
            detail=f"merge base of {_ctx.config.workspace.base_branch}",
            started_at=baseline_started_at.isoformat(),
        )
        try:
            baseline_gate = _run_baseline_gate(
                _ctx.config,
                _ctx.resolved,
                run_id=_ctx.run_id,
                # The confirmation re-run doubles the gate's wall time, and the
                # phase started when the *first* run did — without this the
                # second half of the window reads as a stuck gate (#2434).
                on_confirmation_start=lambda: _publish_sprint_phase(
                    SPRINT_PHASE_BASELINE_GATE,
                    detail=(
                        f"re-running gate to confirm failure on merge base of "
                        f"{_ctx.config.workspace.base_branch}"
                    ),
                    started_at=baseline_started_at.isoformat(),
                ),
            )
        finally:
            _publish_sprint_phase(SPRINT_PHASE_STARTING)
    _ctx.resolved.baseline_gate = baseline_gate
    _log(str(baseline_gate.get("message", "Baseline gate check completed")))
    if baseline_gate.get("failure_reproduced") is False:
        # The sprint is about to proceed past a gate that said FAIL. That is the
        # right call — the failure was not reproducible against the same commit —
        # but it is an ambiguity an operator should be able to see without
        # reading the audit JSON afterwards.
        _initial = baseline_gate.get("initial_result")
        _sprint_logger.emit(
            "baseline_gate_failure_not_reproduced",
            merge_base=baseline_gate.get("merge_base"),
            initial_exit_code=_initial.get("exit_code") if isinstance(_initial, dict) else None,
            evidence_path=baseline_gate.get("evidence_path"),
        )
    # Entry-level intake remediation runs in the CLI before run_sprint and
    # spends the same sprint-authorized budget. The fold happens here, ahead of
    # the baseline-gate abort below, because an abort before any story starts
    # has still spent that money and an operator deciding whether to retry needs
    # the figure (#2434). Folding once, on every path, is what keeps the abort's
    # audit and the completed sprint's total the same number.
    _fold_entry_intake_cost(_ctx, _sprint_state, _log)
    if not bool(baseline_gate.get("passed", False)):
        _write_sprint_audit(
            manifest=_ctx.resolved,
            result=SprintResult(
                name=_ctx.resolved.name,
                specs_total=total,
                specs_succeeded=0,
                specs_failed=total,
                specs_skipped=0,
                # Spend reaching the abort, not a placeholder: the pre-gate
                # entry-intake remediation folded above is real money and the
                # operator sees it here or nowhere (#2434).
                total_cost_usd=_sprint_state.cost.spent,
                budget_usd=_ctx.resolved.budget_usd,
                # No story has run, so none of the folded intake spend has a
                # row to sit on — it is declared at the sprint level (#2847).
                non_story_spend_usd=_sprint_state.cost.non_story_spend(frozenset()),
                results=[],
                stopped_reason="broken_baseline",
            ),
            canonical_refs=[ref for _, _, ref in task_entries],
            started_at=baseline_started_at,
            finished_at=datetime.datetime.now(datetime.timezone.utc),
            duration=float(baseline_gate.get("duration_seconds", 0.0)),
            project_root=_ctx.config.project_root,
            slug_map={ref: task.slug for task, _src, ref in task_entries},
            tasks_by_slug={task.slug: task for task, _src, _ref in task_entries},
            sprint_id=_ctx.sprint_id,
            dropped_slugs=_ctx.dropped_slugs,
            skipped_issues=_ctx.skipped_issues,
            run_id=_ctx.run_id,
        )
        raise RuntimeError(str(baseline_gate.get("message", "Broken baseline")))

    started_at = datetime.datetime.now(datetime.timezone.utc)
    # The sprint's spend, and every source of spend it could not measure, live
    # in the ledger — the single writer, so no closure can advance the total by
    # assigning to a shared variable. While the ledger reports unmeasured
    # sources its total is a measured LOWER BOUND, not the sprint's spend: the
    # budget check must refuse to certify a cap it cannot evaluate rather than
    # dispatch more work against an understated total (#1992). The ledger also
    # tracks which of those sources THIS generation produced, as opposed to
    # inheriting under a ``carried:`` prefix, so an operator acceptance made for
    # an earlier occurrence never absorbs a new unknown nobody has bounded
    # (#2310).
    # Entry-intake remediation spend is folded in above, before the baseline-gate
    # abort, so an abort reports it too (#2434).
    if _ctx.notify and _ctx.config.notifications.backend not in ("ntfy", "none"):
        from ..notify_backends import send_notifications

        send_notifications(
            _ctx.config,
            f'TheForge: sprint started \u2014 "{_ctx.resolved.name}"',
            f"{total} stories \u00b7 budget ${_ctx.resolved.budget_usd:.2f}",
        )
    # Canonical sprint story state — single source of truth for every
    # operator-facing surface (forge status, banner, summary, notifications).
    # No local counters are kept; counts are projected from this structure.
    # Pre-restart spend that the canonical story state will not be holding by
    # the time totals are projected. Without it the summary total — which sums
    # the canonical state — silently drops spend that SprintResult still counts
    # via the ledger's carried prior, so two operator-facing totals disagree
    # about one run.
    #
    # Two disjoint ways the canonical state loses it, both only for stories that
    # re-enter this generation:
    #   unseeded — a non-succeeded (or unmappable) prior outcome is never
    #     seeded, because monotonicity would reject the later transition to
    #     RUNNING. Its cost is therefore always missing, so it becomes carried
    #     attribution immediately.
    #   seeded-then-overwritten — a succeeded prior outcome IS seeded with its
    #     cost, but if the story runs again, transition() replaces cost_usd
    #     with the coordinator's current-generation total. The seed survives
    #     only for a story that does not re-run (e.g. resume_skip_merged), so
    #     it is activated conditionally, at the first write that overwrites it.
    #
    # Both live on the execution state, not in this frame: a signal-driven stop
    # never returns here, and money held only in a local variable is money the
    # run reports as never spent once an operator stops it (#2922).
    # Pre-populate from prior-run accumulated state so cross-process resume
    # invocations see the full logical sprint in counts and projections.
    # Stories present in the current run are seeded only when their prior
    # outcome is succeeded (DONE / ALREADY_DONE); the resume triage handler
    # below preserves those terminal states. A non-succeeded terminal outcome
    # (SKIPPED, DROPPED, FAILED, etc.) from a prior run must NOT be seeded
    # for a story that re-enters the current run, because monotonicity would
    # then reject the transition to RUNNING — producing a live row that shows
    # the story as skipped/failed while phase, model, and cost continue to
    # advance from the active run.
    if _ctx.sprint_id is not None:
        from .audit import _load_accumulated_stories as _preload  # noqa: PLC0415

        _current_run_slugs = set(_ctx.slug_to_context.keys())
        _succeeded_outcomes = {"DONE", "ALREADY_DONE"}

        def _prior_cost_of(prior: dict) -> float:
            try:
                return float(prior.get("cost_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def _defer_prior_cost(slug: str, prior: dict) -> None:
            _sprint_state.defer_prior_story_cost(slug, _prior_cost_of(prior), seeded=False)

        for _prior in _preload(_ctx.sprint_id, _ctx.config.project_root):
            _prior_slug = _prior.get("slug")
            if not _prior_slug:
                continue
            # An earlier generation's unpriced story is still unpriced spend
            # this sprint carries. Without re-flagging it here, a --resume would
            # start with an empty ledger and enforce the cap against a carried
            # lower bound as if it were complete (#1992).
            if "cost_usd" in _prior and _prior.get("cost_usd") is None:
                _sprint_state.cost.note_carried_unmeasured(f"carried:{_prior_slug}")
            _prior_outcome = (_prior.get("outcome") or "").upper()
            if _prior_slug in _current_run_slugs and _prior_outcome not in _succeeded_outcomes:
                _defer_prior_cost(_prior_slug, _prior)
                continue
            _outcome_map = {
                "DONE": StoryOutcome.DONE,
                "ALREADY_DONE": StoryOutcome.ALREADY_DONE,
                "SKIPPED": StoryOutcome.SKIPPED,
                "PRESERVED": StoryOutcome.PRESERVED,
                "DROPPED": StoryOutcome.DROPPED,
                "ESCALATE": StoryOutcome.ESCALATED,
                "FAILED": StoryOutcome.FAILED,
                "MERGE_FAILED": StoryOutcome.MERGE_FAILED,
                "MERGE_ARMING_FAILED": StoryOutcome.MERGE_ARMING_FAILED,
            }
            _mapped_outcome = _outcome_map.get(_prior_outcome)
            if _mapped_outcome is None:
                # Same reasoning as the non-succeeded skip above: an outcome
                # this runner cannot map is still spend that occurred. Only
                # current-run slugs are deferred — a story absent from this
                # run has no canonical row for the spend to land on, so
                # deferring it would drop the value silently instead.
                if _prior_slug in _current_run_slugs:
                    _defer_prior_cost(_prior_slug, _prior)
                continue
            # Seeded cost is only durable while the story stays put; if it
            # re-runs, transition() overwrites it with the current generation's
            # total. Remember it so wrap-up can restore it in that case.
            if _prior_slug in _current_run_slugs:
                _sprint_state.defer_prior_story_cost(
                    _prior_slug, _prior_cost_of(_prior), seeded=True
                )
            # Strip per-run terminal artifacts so an accumulated story cannot
            # carry forward a stale review summary or final_outcome from an
            # earlier generation. The current run must write these fresh.
            _prior_detail_raw = _prior.get("detail")
            _prior_detail = dict(_prior_detail_raw) if isinstance(_prior_detail_raw, dict) else {}
            for _stale in ("final_outcome", "review_verdict", "review_p1", "review_p2"):
                _prior_detail.pop(_stale, None)
            _sprint_state.stories.register(
                _prior_slug,
                _prior.get("path", _prior_slug),
                outcome=_mapped_outcome,
                cost_usd=optional_cost(_prior.get("cost_usd")),
                canonical_ref=_prior.get("canonical_ref"),
                detail=_prior_detail,
            )
    # ── Operator resolutions of unmeasured spend (#2310) ─────────────────────
    # Owned by the budget runtime (#2621): which unmeasured spend an operator
    # has resolved is consulted by every enforcement moment, so it is read and
    # persisted where those moments live rather than here.
    _sprint_state.budget.load_operator_acceptances()

    # Derive slug_to_spec from unified context mapping
    slug_to_spec: dict[str, str] = {slug: ctx[2] for slug, ctx in _ctx.slug_to_context.items()}

    recovered_prior_started_at: datetime.datetime | None = None
    # Resume mode (and re-exec, treated as resume-equivalent): triage all stories
    # and carry forward prior costs.
    triages: dict[str, StoryTriage] = {}
    if reconcile:
        (
            _recovered_prior_cost,
            recovered_prior_started_at,
            _sprint_state.recovered_prior_entries_by_ref,
        ) = _read_prior_sprint_accounting(_ctx.config.project_root, _ctx.sprint_id)
        # Floor the carried figure at the spend this run id has already recorded
        # to its own live state. A re-exec keeps the run id and the .state file,
        # so that high-water is the surviving account of what the previous
        # process image spent — and it outlives the accumulated rows, which are
        # exactly what a lost generation takes with it. Enforcing the cap against
        # the smaller number would admit work the run has no headroom for, which
        # is the half of #2922 that costs money rather than only misreporting it.
        _durable_recorded_spend = (
            read_recorded_spend_usd(_ctx.run_id, _ctx.config.project_root) if _ctx.run_id else None
        )
        if _durable_recorded_spend is not None and _durable_recorded_spend > _recovered_prior_cost:
            _log(
                f"Carried spend raised to this run's recorded high-water: "
                f"${_durable_recorded_spend:.4f} (accumulated rows account for "
                f"${_recovered_prior_cost:.4f})"
            )
            _recovered_prior_cost = _durable_recorded_spend
        _sprint_state.cost.set_prior(_recovered_prior_cost)
        if _recovered_prior_cost > 0.0:
            _log(f"Resuming with prior cost: ${_recovered_prior_cost:.2f}")
        if _prior_sprint_cost_incomplete(_ctx.config.project_root, _ctx.sprint_id):
            # The carried total came from a generation that recorded incomplete
            # cost, so it is a lower bound too (#1992). An accepted ceiling can
            # stand in for it — but only per source, and only if the ledger
            # actually carries that source. Clearing the whole-generation marker
            # without re-surfacing what it stood for would let the acceptance
            # open the guard while its ceiling was charged to nothing, under a
            # cap the ceiling might not even fit (#2310 review).
            _cleared_by_acceptance = not _prior_sprint_cost_incomplete(
                _ctx.config.project_root,
                _ctx.sprint_id,
                _sprint_state.budget.accepted_unmeasured,
            )
            if _cleared_by_acceptance:
                # The marker itself is never re-surfaced. It is a derived
                # statement that SOME source that generation named was
                # unmeasured, so once every one of those sources is accepted it
                # asserts nothing the per-source records do not already assert —
                # and it has no origin, no ceiling and no accept path, so
                # carrying it forward would refuse the run on a condition no
                # operator action can satisfy (#2310 review). Only the named,
                # acceptable sources come across.
                _carried_already = {
                    unmeasured_spend_policy.normalize_source_id(s)
                    for s in _sprint_state.cost.unmeasured_sources
                }
                for _prior_norm in unmeasured_spend_policy.acceptable_prior_sources(
                    _prior_unmeasured_spend_sources(_ctx.config.project_root, _ctx.sprint_id)
                ):
                    if _prior_norm in _carried_already:
                        continue
                    # Accepted, and nothing else in this run's ledger names it —
                    # typically because the accumulated story row was pruned.
                    # It is still spend this sprint carries, so it goes in the
                    # ledger by name: its ceiling gets charged, the audit records
                    # it, and the total stays a lower bound.
                    _sprint_state.cost.note_carried_unmeasured(f"carried:{_prior_norm}")
                    _carried_already.add(_prior_norm)
            else:
                _sprint_state.cost.note_carried_unmeasured("carried:prior-generation")
        if recovered_prior_started_at is not None and recovered_prior_started_at < started_at:
            started_at = recovered_prior_started_at
        _log("Triaging specs...")
        _publish_sprint_phase(SPRINT_PHASE_TRIAGE)
        for slug, (task, _src, canonical_ref) in _ctx.slug_to_context.items():
            if slug in _inflight_slugs:
                # Triage reads the worktree to decide the re-entry point, and
                # this one is being written to by a running agent right now — any
                # verdict taken here describes a half-finished state. Defer it to
                # dispatch, after the inherited agent has stopped.
                _log(f"  {slug:<20} DEFERRED (agent still running; triage after it finishes)")
                continue
            triage = _triage_spec(
                canonical_ref,
                _ctx.config,
                _ctx.config.project_root,
                task=task,
                on_gate_start=lambda label: _publish_reuse_gate_start(
                    _ctx.run_id, _ctx.config.project_root, label
                ),
                on_gate_end=lambda label: _publish_reuse_gate_end(
                    _ctx.run_id, _ctx.config.project_root, label
                ),
            )
            triages[canonical_ref] = triage
            _log(
                f"  {triage.slug:<20} {triage.action.upper().replace('_', ' ')} ({triage.reason})"
            )

    # The startup half of budget enforcement, owned by the budget runtime
    # (#2621): pin each inherited unmeasured source to the run it came from
    # before any story can rewrite its per-story audit, then disclose the
    # headroom this run actually has and refuse it outright if there is none.
    _sprint_state.budget.pin_carried_occurrences()
    _startup_budget_decision = _sprint_state.budget.evaluate_startup_headroom()

    def _recorded_prior_done(slug: str, canonical_ref: str | None = None) -> bool:
        """True when this sprint already recorded the story as run-to-DONE.

        Two records can hold that evidence and either one settles it: the
        canonical story state (seeded above from the accumulated file) and the
        accumulated entry itself. DONE is the only succeeded outcome that means
        "a generation of this sprint ran the story"; ALREADY_DONE means the
        opposite, so it is deliberately not accepted here.

        A recorded DONE is accepted only when the same record shows its landing
        obligation resolved. The coordinator reaches Phase.DONE when review
        approves — before the sprint's landing step runs — so a DONE persisted by
        a generation that then re-exec'd describes a story that was approved and
        never landed. Reading that as a completed story is what reported #1108 as
        landed while its issue stayed open with unmerged commits (#2189). The
        dict record is preferred where one exists, because only it carries the
        landing fields; the canonical entry is consulted for the in-run case,
        where a confirmed landing is marked with ``landed``.
        """
        ref = canonical_ref or _ctx.slug_to_context.get(slug, (None, None, None))[2]
        record = _sprint_state.current_story_entries_by_ref.get(ref) if ref else None
        if record is None:
            _prior = _sprint_state.recovered_prior_entries_by_ref.get(ref) if ref else None
            record = _prior if isinstance(_prior, dict) else None
        if record is not None:
            return str(record.get("outcome") or "").upper() == "DONE" and landing_settled(record)
        entry = _sprint_state.stories.get(slug)
        if entry is None or entry.outcome is not StoryOutcome.DONE:
            return False
        return landing_settled(dict(entry.extras))

    def _skip_merged_outcome(slug: str, canonical_ref: str) -> tuple[StoryOutcome, str | None]:
        """Outcome and ``outcome_source`` to record for a ``skip_merged`` triage.

        ``_triage_spec`` answers a question about git alone — "is this branch
        merged into the base branch right now?" — and has no notion of *which*
        run produced the merge. Reading that answer as "the work pre-dated this
        run" is only sound when nothing in this sprint recorded having done it.
        A mid-sprint re-exec breaks exactly that assumption: the story finished
        DEV+REVIEW, queued its auto-merge, the process re-exec'd, and the new
        generation's triage then observes the merge its own predecessor landed
        moments earlier. Stamping ALREADY_DONE / ``resume_skip_merged`` there
        tells the operator that a story which ran, spent budget and landed a
        commit did nothing — while the same row still reports that spend
        (#2150).

        The recorded execution is the authoritative account of what happened,
        so a prior DONE wins over the triage label and carries no
        ``outcome_source``. ALREADY_DONE / ``resume_skip_merged`` remains for
        the case it actually describes: merged work with no record of this
        sprint having produced it.
        """
        if _recorded_prior_done(slug, canonical_ref):
            return StoryOutcome.DONE, None
        return StoryOutcome.ALREADY_DONE, "resume_skip_merged"

    # Build satisfied set: closed dep slugs detected at manifest build time,
    # resume-mode skip states, plus any cross-sprint depends_on slugs whose
    # branch is already merged to the base branch.
    pre_satisfied: set[str] = set(_ctx.resolved.closed_dependency_slugs)
    # Slugs the reconcile triage classified as already-merged / to-skip. These
    # must be excluded from every dispatch/spend path below (intake remediation,
    # batch preflight) and pre-marked complete in the DAG, so a re-exec'd process
    # never re-enters a merged story through WORKSPACE.
    skip_slugs: set[str] = set()
    if reconcile:
        for triage in triages.values():
            if triage.action in ("skip_merged", "skip"):
                pre_satisfied.add(triage.slug)
                skip_slugs.add(triage.slug)

    # Stories the resolver kept because this sprint's own earlier generation ran
    # them and their issue has since closed (#2847). The restore decision is
    # carried here explicitly rather than re-derived from the worktree or the
    # branch: a story that landed, had its PR auto-merged and its tree cleaned
    # before the re-exec satisfies neither the launch guard's reconcile drop nor
    # the skip_merged triage, so nothing else would neutralise it — and its issue
    # body was never fetched, so dispatching it would re-spend on already-landed
    # work with no story text. They are records, not runnable work.
    restored_prior_slugs: set[str] = {
        slug
        for slug in (getattr(_ctx.resolved, "reconciled_prior_slugs", None) or set())
        if slug in _ctx.slug_to_context
    }
    pre_satisfied |= restored_prior_slugs
    skip_slugs |= restored_prior_slugs

    # Build DAG
    all_tasks = [ctx[0] for ctx in _ctx.slug_to_context.values()]
    satisfied_slugs = resolve_satisfied_dependencies(
        all_tasks,
        project_root=_ctx.config.project_root,
        base_branch=_ctx.config.workspace.base_branch,
        branch_pattern=_ctx.config.workspace.branch_pattern,
        pre_satisfied=pre_satisfied,
    )
    normalized = normalize_dependency_plan(all_tasks, satisfied=satisfied_slugs)

    # Landing precondition, dependency-resolved pass. A dependency parent this
    # sprint carries is merged into the project-root checkout to unblock its
    # child — but only when two things hold, and both are false often enough
    # that asserting either one alone refuses work forge could have done:
    #
    #   1. The parent is actually dispatched. One already satisfied (closed
    #      dependency, branch merged into base) or triaged skip_merged on resume
    #      never runs and never merges (#2048 review iteration 3). Both sets are
    #      known now, and nothing has been spent: intake remediation, batch
    #      preflight, and story dispatch are all still ahead.
    #   2. The sprint's landing path for parents is a local merge at all — see
    #      dependency_parents_land_in_project_root. A parallel merge-pr sprint
    #      lands its parent through that parent's own PR and never touches the
    #      project-root checkout (#2048 review iteration 4).
    _dispatchable_dependency_parents = (
        in_manifest_dependency_parents - satisfied_slugs - skip_slugs
    )
    _refuse_dirty_root_before_spend(
        _ctx.config,
        lands_in_project_root=(
            bool(_dispatchable_dependency_parents) and dependency_parents_land_in_project_root
        ),
        stage="dependency-resolved",
    )

    # Surface the current sprint phase to forge status --watch so operators
    # see meaningful progress signals during the multi-minute pre-init window.
    if _ctx.run_id:
        from .state_writer import update_state_phase as _update_state_phase

        _update_state_phase(_ctx.run_id, _ctx.config.project_root, "intake-remediation")

    # Cost-aware batch-group assignment (#727), filled in after preflight once
    # compute_batch_groups has run. Declared here so the terminal-entry writers
    # below can read it on paths that fire before the scheduler gets that far.
    batch_group_by_slug: dict[str, str] = {}
    batch_members_by_group: dict[str, list[str]] = {}
    # Group -> leader. Its inverse, and the rest of the batch-group landing
    # bookkeeping, lives on the execution state (#727).
    _dispatched_batch_leader: dict[str, str] = {}

    def _break_batch_group(group_id: str, reason: str) -> None:
        """Dissolve a batch group and dispatch its members individually.

        Batching is an optimisation; when its precondition (all members ready as
        a unit, none resuming) fails, the stories still run — they just run on
        their own. The grouping metadata is cleared everywhere an operator can
        see it so ``forge status`` never shows a batch that did not happen.
        """
        members = batch_members_by_group.pop(group_id, [])
        if not members:
            return
        for member in members:
            batch_group_by_slug.pop(member, None)
            _state = preflight_states.get(member)
            if _state is not None:
                _state.preflight_batch_group = None
            if _sprint_state.state_writer is not None:
                _sprint_state.state_writer.update(member, batch_group=None)
        _log(
            f"BATCH {group_id}: dissolved ({reason}); dispatching {', '.join(members)} separately"
        )

    def _make_batch_leader(tasks: list[TaskStory], group_id: str) -> TaskStory:
        """Build the group leader's TaskStory, carrying every member's spec.

        Only the leader is handed to the coordinator, so only the leader's slug
        owns a worktree and a branch. The members ride along as
        ``batch_members`` — enough for the dev prompt to name and specify them,
        and nothing more: their own TaskStory objects remain the authority for
        review, landing, and reporting.
        """
        members = tuple(
            BatchMember(
                name=t.name,
                slug=t.slug,
                story_text=(
                    t.story_text
                    if t.story_text is not None
                    else (t.story_path.read_text(encoding="utf-8") if t.story_path else "")
                ),
                display_ref=(f"Issue #{t.github_issue}" if t.github_issue is not None else t.slug),
            )
            for t in tasks
        )
        return replace(tasks[0], batch_members=members, batch_group=group_id)

    def _record_current_story_entry(
        slug: str,
        outcome: str,
        *,
        error: str | None = None,
        error_type: str | None = None,
        cost_usd: float | None = 0.0,
        extras: dict | None = None,
        failure_cause: dict | None = None,
    ) -> None:
        task_ctx = _ctx.slug_to_context.get(slug)
        if task_ctx is None:
            return
        task, _source, canonical_ref = task_ctx
        display_key = (
            f"Issue #{canonical_ref.split(':')[1]}"
            if canonical_ref.startswith("issue:")
            else canonical_ref
        )
        entry: dict = {
            "path": display_key,
            "slug": slug,
            "outcome": outcome,
            "verdict": None,
            # Same projection every other persisted row gets. This is the
            # accumulated record for a story that leaves the sprint WITHOUT a
            # coordinator result — an intake drop, an auth or budget skip, a gate
            # stand-down — and every one of those defaults the cost to 0.0. For a
            # story re-entering after a re-exec that 0.0 would overwrite its
            # pre-restart spend with nothing, and a stop before wrap-up would keep
            # the zero (#2922). The absolute-prior reconciliation paths consume
            # the carried attribution before handing their figure in, so the money
            # is added here exactly once.
            #
            # ``include_seed`` because this write REPLACES the prior accumulated
            # row: a prior-generation DONE story that re-enters and is refused
            # before dispatch has its cost seeded on the canonical row, not
            # carried, and without the seed the replacement would publish $0.00
            # over its pre-restart spend until wrap-up.
            "cost_usd": _projected_story_cost(_sprint_state, slug, cost_usd, include_seed=True),
            "story_run_id": _ctx.run_id,
            "preflight": None,
            "preflight_original_verdict": None,
            "preflight_source_run_id": None,
            "error": error,
            "error_type": error_type,
            "outcome_code": error_type or outcome.lower(),
            "merge": False,
            "batch_group": batch_group_by_slug.get(slug),
            "batch": 0,
            "depends_on": list(getattr(task, "depends_on", None) or []),
            "dependency_warnings": list(getattr(task, "dependency_warnings", None) or []),
            "inferred_dependencies": {
                "manifest": [
                    dep
                    for dep in (getattr(task, "depends_on", None) or [])
                    if dep not in (getattr(task, "inferred_dependencies", None) or [])
                ],
                "github_blockers": list(getattr(task, "inferred_dependencies", None) or []),
            },
        }
        if extras:
            entry.update(extras)
        # Carried as history from the start so every reader of the entry — not
        # only the accumulated state file — sees the abnormal kind and run id
        # rather than the flattened error prose, and sees this attempt's cause
        # after the ones that preceded it.
        carry_failure_cause(
            entry,
            failure_cause,
            prior_history=_sprint_state.prior_failure_history_by_ref.get(canonical_ref),
        )
        _sprint_state.current_story_entries_by_ref[canonical_ref] = entry
        _persist_accumulated_story_entries(_sprint_state)

    # The two runner-owned side effects a budget refusal has to cause. Recording
    # a story's canonical outcome and writing this generation's accumulated entry
    # are the runner's concerns, not budget's, so the budget runtime is handed
    # them rather than importing them back (#2621).
    _sprint_state.budget.bind_story_hooks(
        set_outcome=partial(_set_outcome, _sprint_state),
        record_story_entry=_record_current_story_entry,
    )

    # Intake remediation gate: between dependency normalization and the
    # batch preflight spend, run the shared shape + grooming check on the
    # full normalized task list. When ``intake.auto_fix`` is enabled, semantic
    # findings get a single agent rewrite pass; mechanical findings are
    # patched without an LLM. Stories that still fail are dropped here and
    # never enter the preflight batch. When grooming and auto_fix are both
    # disabled, this is a near no-op (parity with pre-remediation behavior).
    # Exclude reconcile-skipped (already-merged) stories from every spend/dispatch
    # path. They stay in ``normalized.tasks`` so the DAG can be built and they can
    # be pre-marked complete below, but they must not enter intake remediation or
    # the batch preflight, where a re-exec'd merged story would be re-dispatched
    # into its stale round-1 worktree. When ``reconcile`` is False, skip_slugs is
    # empty and this is a no-op (no behavior change for plain fresh runs).
    #
    # Pre-launch drops (re-exec worktree collisions, reconciled prior-generation
    # completions, stranded prior-generation state, preserved-escalated, lock
    # conflicts) are handled by the dropped-slug loop further below and must
    # never consume preflight or worker budget in this generation. Exclude them
    # from every spend/dispatch path here alongside reconcile-skipped stories.
    #
    # In-flight stories are excluded from these two passes as well, for a
    # different reason: they are still scheduled, but an agent is writing to
    # their worktree right now, so intake and preflight would be reasoning about
    # (and spending on) a story already being worked.
    _dropped_exclusion = {s for s in (_ctx.dropped_slugs or {}) if s in _ctx.slug_to_context}
    _no_dispatch_slugs = skip_slugs | _dropped_exclusion
    dispatch_tasks = [
        t
        for t in normalized.tasks
        if t.slug not in _no_dispatch_slugs and t.slug not in _inflight_slugs
    ]
    if _startup_budget_decision is not None:
        # The startup headroom check already refused this run. Its stories are
        # marked skipped further down, once the DAG exists — but intake
        # remediation spends real money on agent rewrites, so the refusal has to
        # bite here, ahead of it, the same way the landing precondition does.
        # Refusing after spending is not refusing (#2922).
        dispatch_tasks = []

    intake_outcomes = _run_intake_remediation_pass(
        config=_ctx.config,
        tasks=dispatch_tasks,
        log=_log,
        force=_ctx.force,
        sprint_id=_ctx.sprint_id,
    )
    # Intake remediation agent spend (auto_fix LLM rewrites) must roll up
    # into the sprint total. Without this, sprint.total_cost_usd silently
    # excludes every dollar spent on intake auto-fix attempts.
    for _intake_slug, _intake_outcome in intake_outcomes.items():
        if _intake_outcome_cost_measured(_intake_outcome) is None:
            _sprint_state.cost.flag_unmeasured_here(f"intake:{_intake_slug}")
    _intake_remediation_cost = sum(_intake_outcome_cost(o) for o in intake_outcomes.values())
    if _intake_remediation_cost > 0.0:
        _sprint_state.cost.add(_intake_remediation_cost)
        _log(
            f"Intake remediation cost: ${_intake_remediation_cost:.4f} (rolled into sprint total)"
        )
    for _slug, _outcome in intake_outcomes.items():
        _sprint_state.story_cost_adjustments[_slug] = _sprint_state.story_cost_adjustments.get(
            _slug, 0.0
        ) + _intake_outcome_cost(_outcome)
        _sprint_state.cost.note_non_story(_slug, _intake_outcome_cost(_outcome))
    for _issue_num, _outcome in (_ctx.entry_intake_outcomes or {}).items():
        _issue_slug = f"issue-{_issue_num}"
        _sprint_state.story_cost_adjustments[_issue_slug] = (
            _sprint_state.story_cost_adjustments.get(_issue_slug, 0.0)
            + _intake_outcome_cost(_outcome)
        )
    if intake_outcomes:
        terminal_kinds = {IntakeOutcomeKind.DROPPED_SHAPE, IntakeOutcomeKind.DROPPED_AFTER_FIX}
        dropped_slugs_intake = {
            slug for slug, outcome in intake_outcomes.items() if outcome.kind in terminal_kinds
        }
        # Convention 6: instrument the gate so PASSED stories also leave a
        # trace in the sprint log, not only drops/remediations. A passed-only
        # gate would otherwise produce no audit signal at all.
        _kind_counts: dict[str, int] = {}
        for outcome in intake_outcomes.values():
            _kind_counts[outcome.kind.value] = _kind_counts.get(outcome.kind.value, 0) + 1
        _log(
            "Intake remediation gate outcomes: "
            + ", ".join(f"{k}={v}" for k, v in sorted(_kind_counts.items()))
        )
        for slug, outcome in intake_outcomes.items():
            if outcome.kind is IntakeOutcomeKind.PASSED:
                continue
            if (
                outcome.kind is IntakeOutcomeKind.DROPPED_AFTER_FIX
                and outcome.proposed_replacement
            ):
                if outcome.audit.get("comment_posted"):
                    _log(
                        f"  Intake candidate for {slug} posted as issue comment "
                        "(rerun gate still failing — operator review required)"
                    )
                elif outcome.audit.get("candidate_artifact_path"):
                    _log(
                        f"  Intake candidate for {slug} persisted to "
                        f"{outcome.audit['candidate_artifact_path']} "
                        "(comment post failed — rerun gate still failing)"
                    )
            outcome_value = (
                StoryOutcome.REMEDIATED
                if outcome.kind is IntakeOutcomeKind.REMEDIATED
                else StoryOutcome.DROPPED_SHAPE
                if outcome.kind is IntakeOutcomeKind.DROPPED_SHAPE
                else StoryOutcome.DROPPED_AFTER_FIX
            )
            intake_codes = _intake_finding_codes(outcome)
            intake_summary = _intake_outcome_summary(outcome)
            intake_error_type = _intake_error_type(outcome)
            intake_agent_summary = _intake_agent_summary(outcome)
            intake_problem_lines = _intake_problem_lines(outcome)
            intake_block = _intake_audit_block(outcome)
            # Per-issue run-log line so operators reading the live sprint log
            # learn the blocking rule code(s) and detail without having to open
            # audit YAML or re-derive them with a Python snippet against
            # groom_check.
            task_ctx = _ctx.slug_to_context.get(slug)
            if task_ctx is not None:
                _, _, canonical_ref = task_ctx
                display_key = (
                    f"Issue #{canonical_ref.split(':')[1]}"
                    if canonical_ref.startswith("issue:")
                    else canonical_ref
                )
            else:
                display_key = slug
            for log_line in _intake_log_lines(
                outcome, outcome_name=outcome_value.name, display_key=display_key
            ):
                _log(log_line)
            _set_outcome(
                _sprint_state,
                slug,
                outcome_value,
                detail={
                    "intake_kind": outcome.kind.value,
                    "intake_findings": [f.as_dict() for f in outcome.findings],
                    "intake_codes": intake_codes,
                    "intake_summary": intake_summary,
                    "intake_problem_lines": intake_problem_lines,
                    "intake_agent_summary": intake_agent_summary,
                    "intake_detail": outcome.detail,
                    "intake_audit": dict(outcome.audit),
                },
                reason=intake_summary or outcome.detail or outcome.kind.value,
            )
            # Mirror the structured detail into current_story_entries_by_ref so
            # both audit YAML and sprint summary YAML carry the rule code,
            # human-readable problem, and the structured intake_findings block —
            # not just the bare SKIPPED outcome with error: null.
            if outcome.kind in {
                IntakeOutcomeKind.DROPPED_SHAPE,
                IntakeOutcomeKind.DROPPED_AFTER_FIX,
            }:
                _record_current_story_entry(
                    slug,
                    outcome_value.name,
                    error=intake_summary,
                    error_type=intake_error_type,
                    extras={"intake": intake_block},
                )
        if dropped_slugs_intake:
            normalized = _filter_normalized_for_intake(normalized, dropped_slugs_intake)

    if _ctx.run_id:
        from .state_writer import update_state_phase as _update_state_phase

        _update_state_phase(_ctx.run_id, _ctx.config.project_root, "preflight")

    # Re-derive the filter here: ``normalized`` may have been re-bound by the
    # intake drop above, and reconcile-skipped merged stories (plus pre-launch
    # dropped stories) must never enter the preflight batch (WORKSPACE re-entry
    # against their stale worktree, or spending budget on an already-dropped
    # story).
    _no_dispatch_slugs = skip_slugs | {
        s for s in (_ctx.dropped_slugs or {}) if s in _ctx.slug_to_context
    }
    preflight_tasks = [
        t
        for t in normalized.tasks
        if t.slug not in _no_dispatch_slugs and t.slug not in _inflight_slugs
    ]
    if _startup_budget_decision is not None:
        # Same reason as the intake pass above: preflight is a reasoning task and
        # every story in the batch costs money. A run the startup headroom check
        # has already refused must not pay for it (#2922).
        _log("Skipping batch preflight: the selected run cannot dispatch under its ceiling")
        preflight_tasks = []
    preflight_states = run_batch_preflight(
        preflight_tasks,
        _ctx.config,
        sprint_name=_ctx.resolved.name,
        no_pull=_ctx.no_pull,
        max_parallel=max_parallel,
        notify=_ctx.notify,
    )
    story_worker_timeouts: dict[str, int] = {}
    for task, _src, _canonical_ref in task_entries:
        if _ctx.resolved.worker_timeout_seconds is not None:
            story_worker_timeouts[task.slug] = _ctx.resolved.worker_timeout_seconds
            _log(
                f"  Worker timeout {task.slug}: {_ctx.resolved.worker_timeout_seconds}s "
                "(manifest override)"
            )
            continue
        _state = preflight_states.get(task.slug)
        if _state is None:
            story_worker_timeouts[task.slug] = base_worker_timeout_seconds
            _log(
                f"  Worker timeout {task.slug}: {base_worker_timeout_seconds}s "
                "(base default; no preflight state)"
            )
            continue
        _timeout_seconds = derive_worker_timeout(
            base_worker_timeout_seconds,
            _state.preflight_complexity,
            _state.preflight_complexity_score,
        )
        story_worker_timeouts[task.slug] = _timeout_seconds
        _source = "derived" if _timeout_seconds != base_worker_timeout_seconds else "base default"
        _complexity = _state.preflight_complexity or "unknown"
        _log(
            f"  Worker timeout {task.slug}: {_timeout_seconds}s "
            f"({_complexity} complexity, {_source})"
        )
    if _ctx.resume:
        _register_resumed_story_footprints(
            triages,
            preflight_states,
            project_root=_ctx.config.project_root,
            tasks=list(normalized.tasks),
        )
    bundle_assignments = compute_bundle_assignments(preflight_states, normalized.tasks)
    if bundle_assignments:
        _log(f"Computed deterministic bundles: {bundle_assignments}")
    # Audit signal: stamp scheduler decision onto preflight_states so downstream
    # audit serialization (per-story sprint audit, cached_preflight_state carry)
    # reflects the actual bundling decision. The field used to be sourced from
    # preflight LLM output and is now scheduler-written.
    _scheduled_bundled_slugs: set[str] = {s for bundle in bundle_assignments for s in bundle}
    for _slug, _state in preflight_states.items():
        _state.preflight_bundle_candidate = _slug in _scheduled_bundled_slugs

    # Cost-aware batch groups (#727) — the third scheduling primitive, computed
    # *after* conflict bundles and excluding everything they claimed. Bundles
    # exist to avoid merge pain and win whenever both would apply; batch groups
    # exist only to amortise per-story orchestration cost across small,
    # independent, implementation-ready stories.
    batch_groups = compute_batch_groups(
        preflight_states,
        normalized.tasks,
        batch_config=_ctx.config.sprint.batch,
        excluded_slugs=_scheduled_bundled_slugs,
    )
    for _group in batch_groups:
        _gid = batch_group_id(_group)
        batch_members_by_group[_gid] = list(_group)
        for _member_slug in _group:
            batch_group_by_slug[_member_slug] = _gid
    if batch_groups:
        _log(
            "Computed cost-aware batch groups: "
            + "; ".join(f"{gid}=[{', '.join(m)}]" for gid, m in batch_members_by_group.items())
        )
    for _slug, _state in preflight_states.items():
        _state.preflight_batch_group = batch_group_by_slug.get(_slug)
    synthetic_edges = compute_synthetic_edges(preflight_states, normalized.tasks)
    if synthetic_edges:
        _log(f"Injected synthetic dependency constraints for {len(synthetic_edges)} stories")
    augmented_tasks = inject_synthetic_deps(normalized.tasks, synthetic_edges)
    blocked_slugs = dict(normalized.blocked)
    if _ctx.run_id:
        from .state_writer import update_state_phase as _update_state_phase

        _update_state_phase(_ctx.run_id, _ctx.config.project_root, "dag-build")
    try:
        _sprint_state.dag = build_dag(augmented_tasks, satisfied=satisfied_slugs)
    except ValueError as exc:
        raise ValueError(f"{exc} Synthetic collision edges: {synthetic_edges}") from exc

    # Dependencies already satisfied outside this sprint still count as landed
    # for deferred integration ordering.
    _sprint_state.merged_slugs.update(satisfied_slugs)

    # Resume / re-exec: pre-mark skip_merged / skip stories as complete in DAG.
    # skip_merged stories are already merged and should satisfy dependencies
    # immediately, but they still count as skipped in sprint aggregates. This is
    # the block that actually removes a slug from dag.ready()/remaining(): without
    # it a re-exec'd process would re-dispatch an already-merged story even though
    # it was excluded from preflight above.
    if reconcile:
        for slug, (_task, _src, canonical_ref) in _ctx.slug_to_context.items():
            triage = triages.get(canonical_ref)
            if triage and triage.action in ("skip_merged", "skip"):
                _log(f"SKIP {slug} ({triage.reason})")
                if triage.action == "skip_merged":
                    _sprint_state.merged_slugs.add(slug)
                    _sprint_state.dag.mark_complete(slug)
                    _prior_entry = _sprint_state.recovered_prior_entries_by_ref.get(canonical_ref)
                    if _prior_entry is not None:
                        _skip_outcome, _skip_source = _skip_merged_outcome(slug, canonical_ref)
                        _already_done_entry = dict(_prior_entry)
                        _already_done_entry["outcome"] = _skip_outcome.name
                        _already_done_entry["outcome_source"] = _skip_source
                        _sprint_state.current_story_entries_by_ref[canonical_ref] = {
                            k: v for k, v in _already_done_entry.items() if k != "canonical_ref"
                        }
                        _persist_accumulated_story_entries(_sprint_state)
                    # Preserve preloaded prior-run outcome (e.g., DONE) when
                    # accumulated state already has a stronger terminal —
                    # otherwise mark SKIPPED for the legacy aggregate contract.
                    _existing = _sprint_state.stories.get(slug)
                    if _existing is None or not _existing.outcome.is_succeeded:
                        _set_outcome(
                            _sprint_state, slug, StoryOutcome.SKIPPED, reason=triage.reason
                        )
                else:
                    _sprint_state.dag.mark_skipped(slug)
                    _existing = _sprint_state.stories.get(slug)
                    if _existing is None or not _existing.outcome.is_succeeded:
                        _set_outcome(
                            _sprint_state, slug, StoryOutcome.SKIPPED, reason=triage.reason
                        )
                    _record_current_story_entry(slug, "SKIPPED", error=triage.reason)

    # Stories restored at resolution time (#2847): pre-mark them in the DAG and
    # publish the record this sprint already wrote for them. The prior entry is
    # carried verbatim — the outcome this sprint recorded is the authoritative
    # account of what happened, so a story that ran to DONE keeps DONE and one
    # that failed keeps its failure; neither is relabelled by the fact that the
    # issue is now closed. A landed success satisfies its dependents exactly as a
    # skip_merged does; anything else only stops blocking them.
    for _restored_slug in sorted(restored_prior_slugs):
        _restored_ctx = _ctx.slug_to_context.get(_restored_slug)
        if _restored_ctx is None:
            continue
        _restored_ref = _restored_ctx[2]
        _restored_prior = _sprint_state.recovered_prior_entries_by_ref.get(_restored_ref)
        _restored_outcome_name = (
            str((_restored_prior or {}).get("outcome") or "").upper() or "unknown"
        )
        _log(
            f"RESTORED {_restored_slug} (issue closed; recorded execution "
            f"{_restored_outcome_name} preserved as a story of this sprint)"
        )
        if isinstance(_restored_prior, dict):
            _sprint_state.current_story_entries_by_ref[_restored_ref] = {
                k: v for k, v in _restored_prior.items() if k != "canonical_ref"
            }
            _persist_accumulated_story_entries(_sprint_state)
        _restored_existing = _sprint_state.stories.get(_restored_slug)
        if _restored_existing is None:
            # Nothing seeded a canonical row for it (an outcome this runner
            # cannot map, or a pruned entry). Record the story rather than let
            # the DAG treat it as runnable work whose body we do not have.
            _set_outcome(
                _sprint_state,
                _restored_slug,
                StoryOutcome.SKIPPED,
                reason="issue closed; prior execution record preserved",
            )
            _sprint_state.dag.mark_skipped(_restored_slug)
        elif prior_execution_landed(_restored_prior):
            _sprint_state.merged_slugs.add(_restored_slug)
            _sprint_state.dag.mark_complete(_restored_slug)
        else:
            _sprint_state.dag.mark_skipped(_restored_slug)

    auto_enabled_dependency_merges = dependent_slugs - satisfied_slugs - _sprint_state.merged_slugs
    if (
        max_parallel > 1
        and not _ctx.auto_merge
        and _ctx.config.workspace.on_approve != "merge-pr"
        and auto_enabled_dependency_merges
    ):
        listed = ", ".join(sorted(auto_enabled_dependency_merges))
        _log(
            "WARN: parallel dependency merging auto-enabled for "
            f"{listed} so dependent stories are not silently skipped"
        )

    # Stories blocked by unresolved external dependencies never enter the DAG.
    for slug, blocked_by in blocked_slugs.items():
        _log(f"SKIPPED {slug} (blocked: {', '.join(blocked_by)})")
        _sprint_state.dag.mark_skipped(slug)
        _blocked_reason = f"blocked: {', '.join(blocked_by)}"
        _set_outcome(_sprint_state, slug, StoryOutcome.SKIPPED, reason=_blocked_reason)
        _record_current_story_entry(slug, "SKIPPED", error=_blocked_reason)

    # Stories dropped pre-launch (e.g. re-exec collision) never enter the DAG.
    # They surface with a distinct DROPPED/PRESERVED outcome in sprint-audit and
    # the live state file so operators can see exactly which stories did not
    # run and why — a silent WARNING is not enough visibility.
    #
    # ``preserved-escalated`` is a disjoint case: the worktree is intentionally
    # kept for human review, and counts as skipped (not failed) in aggregates.
    _dropped_slugs: dict[str, str] = dict(_ctx.dropped_slugs or {})
    _dropped_work: dict[str, WorktreeWork] = {}
    # slug -> description of the work the drop abandoned. Membership is the
    # single test for "this drop was not free and not evidence-free".
    _dropped_with_work: dict[str, str] = {}

    def _inspect_dropped_work(slug: str) -> WorktreeWork:
        work = inspect_worktree_work(
            slug,
            project_root=_ctx.config.project_root,
            path_pattern=_ctx.config.workspace.path_pattern,
            base_branch=_ctx.config.workspace.base_branch,
            branch_pattern=getattr(_ctx.config.workspace, "branch_pattern", None),
        )
        _dropped_work[slug] = work
        return work

    def _reclaim_dropped_agents(slug: str) -> None:
        """Settle any inherited agent group belonging to a story we will not run.

        This sprint owns the group — the sidecar names this pid — so nothing else
        can adopt it. Leaving it running hands a live process to the next
        invocation's orphan reaper, hours later and with no context (#2079).
        """
        from .live_stories import reclaim_inherited_agents  # noqa: PLC0415

        try:
            killed = reclaim_inherited_agents(
                slug,
                project_root=_ctx.config.project_root,
                path_pattern=_ctx.config.workspace.path_pattern,
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must not fail the sprint
            _log(f"WARN {slug}: could not reclaim inherited agent group(s): {exc}")
            return
        if killed:
            _log(
                f"RECLAIMED {slug}: terminated inherited agent process group(s) "
                f"{', '.join(str(p) for p in killed)} belonging to a story this "
                "sprint did not schedule"
            )

    def _reconciled_prior_outcome(slug: str) -> StoryOutcome:
        """Terminal outcome to preserve for a prior generation's finished story.

        The launch guard collapses every succeeded prior outcome into the single
        drop reason ``REASON_RECONCILE_PRIOR_DONE`` (see
        ``launch_guard._PRIOR_SUCCEEDED_OUTCOMES``), so the reason alone cannot
        say whether the prior generation ran the story to DONE or short-circuited
        it as a no-op ALREADY_DONE. Projecting ALREADY_DONE for both tells the
        operator that a story which ran, spent budget and landed a commit did
        nothing — the exact inversion reported in #2042, where a mid-sprint
        re-exec ("source updated after pull") relabelled every already-completed
        story minutes after it finished.

        The prior generation's own recorded terminal is the evidence that
        settles it, so prefer it. ALREADY_DONE remains the fallback for the case
        it legitimately describes: a reconciled story with no surviving record of
        having run.
        """
        if _recorded_prior_done(slug):
            return StoryOutcome.DONE
        return StoryOutcome.ALREADY_DONE

    def _prior_generation_record(slug: str) -> dict | None:
        """The prior generation's accumulated entry for ``slug``, if recovered."""
        ref = _ctx.slug_to_context.get(slug, (None, None, None))[2]
        prior = _sprint_state.recovered_prior_entries_by_ref.get(ref) if ref else None
        return prior if isinstance(prior, dict) else None

    def _reconcilable_prior_record(slug: str) -> bool:
        """True when the prior generation's record is a *settled* success.

        The launch guard already applies this test, but the runner must not take
        the drop reason on faith: the guard classifies from whatever prior-outcome
        map the CLI managed to resolve, and a record that claims DONE while its
        landing was owed and unresolved has no completed outcome to preserve. A
        story reconciled on that basis is reported as landed, its DAG node marked
        complete, and it is dropped from dispatch on every subsequent re-exec —
        with approved work still sitting unmerged on its branch (#2189). Absence
        of any record is not treated as a demotion: an ALREADY_DONE reconciled
        with no surviving record stays reconcilable, as before.
        """
        record = _prior_generation_record(slug)
        if record is None:
            return True
        outcome = str(record.get("outcome") or "").upper()
        if outcome not in {"DONE", "ALREADY_DONE"}:
            return True
        return landing_settled(record)

    def _measured_recorded_cost(slug: str) -> float | None:
        """Cost already measured for a story whose row this generation rewrites.

        A reconciled or stranded story is recorded without a coordinator result,
        and defaulting its row to ``0.0`` overwrites the spend the generation that
        actually ran it recorded — dropping it from the run total and rendering a
        known amount as absent (#2189). The prior accumulated entry is preferred;
        its ``None`` (cost-unknown) is a real value and is carried through as-is.
        Returns ``0.0`` only when no record of spend exists at all.
        """
        record = _prior_generation_record(slug)
        if record is not None and "cost_usd" in record:
            # The row is about to state the prior accumulated cost outright, so
            # the same money must stop being carried as attribution on top of it
            # (#2922). Both readings come from this one record.
            _sprint_state.consume_carried_prior_cost(slug)
            raw = record.get("cost_usd")
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
        entry = _sprint_state.stories.get(slug)
        if entry is not None:
            return entry.cost_usd
        return 0.0

    def _attribute_prior_generation_cost(slug: str) -> float | None:
        """Spend recovered from the generation that ran ``slug`` before this one.

        The prior generation's audit is the only surviving account of a story
        that was still in flight when the boundary was crossed: it never wrote an
        accumulated row, so its spend is in neither the ledger's carried prior nor
        its accumulated total, and the story's row would report 0.0 for work that
        cost real money (#2214). Rolling it into the ledger as the row
        is written keeps the sprint total and the sum of the rows equal, and the
        bump happens once per story however many times this is asked.

        That "it never wrote an accumulated row" is the ordinary case, not a
        guarantee: a generation can flush both, and then the accumulated row and
        the audit are two accounts of ONE occurrence of the money. Where both
        hold it, the accumulated half is already in the ledger's carried prior
        and in this story's carried attribution, so only the excess is new — and
        the carried attribution is consumed here, because the row is about to
        state the whole figure outright (#2922 review).

        Returns ``None`` when nothing was recovered — never 0.0, which would
        assert that the prior generation spent nothing.
        """
        carried = _sprint_state.prior_generation_work.get(slug)
        if not carried:
            return None
        cost = carried.get("recoverable_cost_usd")
        if cost is None:
            return None
        if carried.get("cost_attributed"):
            # Asked again for the same story: report the figure already settled,
            # not a freshly derived one — the accumulated half has been consumed
            # by now and would no longer be visible to recompute it.
            return float(carried.get("attributed_row_cost_usd", cost))
        _already_carried = _sprint_state.carried_prior_story_cost.get(slug, 0.0)
        _row_cost = max(float(cost), _already_carried)
        _new_money = round(_row_cost - _already_carried, 6)
        carried["cost_attributed"] = True
        carried["attributed_row_cost_usd"] = _row_cost
        _sprint_state.consume_carried_prior_cost(slug)
        if _new_money > 0.0:
            _sprint_state.cost.add(_new_money)
        _log(
            f"RECOVERED {slug}: ${_row_cost:.4f} of prior-generation spend on the row "
            f"(${_new_money:.4f} new to this generation's ledger, "
            f"${_already_carried:.4f} already carried)"
        )
        return _row_cost

    def _dropped_row_cost(slug: str) -> float | None:
        """The cost every surface reports for a dropped story's row.

        Ordered by what is actually known: spend recovered from the generation
        that ran the story, then unmeasured for a drop that abandoned work whose
        cost nothing recorded, then 0.0 only for a story that really did nothing.

        Only *attributed* spend is reported. A recovered amount that the drop
        branch decided not to use (because the sprint already held a measured
        cost for the story) is not in this generation's ledger, and putting it on
        the row would make the rows sum to more than the sprint total.
        """
        carried = _sprint_state.prior_generation_work.get(slug)
        if carried and carried.get("cost_attributed"):
            # The resolved figure, not the raw recovered one: where the same
            # money was also in the accumulated row, attribution settled on one
            # occurrence of it and this row must report that same number.
            return carried.get("attributed_row_cost_usd", carried.get("recoverable_cost_usd"))
        return None if slug in _dropped_with_work else 0.0

    for slug, reason in list(_dropped_slugs.items()):
        if slug not in _ctx.slug_to_context:
            continue
        _reclaim_dropped_agents(slug)
        if reason == REASON_RECONCILE_PRIOR_DONE and not _reconcilable_prior_record(slug):
            # A prior-generation success whose landing never completed. Rewrite
            # the reason in place so every downstream reader — the initial live
            # status rows below, sprint audit, RCA — reports the same recoverable
            # stranded state, instead of one surface preserving a success the
            # story never reached (#2189).
            _recorded_outcome = str(
                (_prior_generation_record(slug) or {}).get("outcome") or "success"
            ).upper()
            _log(
                f"UNLANDED {slug}: prior generation recorded {_recorded_outcome} before its "
                "landing completed; treating as stranded prior-generation state, "
                "not a preserved success"
            )
            reason = REASON_STRANDED_WORKTREE
            _dropped_slugs[slug] = reason
        if reason == "preserved-escalated":
            _, _, _canonical_ref = _ctx.slug_to_context[slug]
            _log(preserved_escalated_message(slug, canonical_ref=_canonical_ref))
            _sprint_state.dag.mark_skipped(slug)
            _set_outcome(_sprint_state, slug, StoryOutcome.PRESERVED, reason=reason)
            _record_current_story_entry(
                slug,
                "PRESERVED",
                error=reason,
                error_type="dropped",
                extras={"drop_reason": reason},
            )
        elif reason == REASON_RECONCILE_PRIOR_DONE:
            # The prior generation already completed this story; its worktree
            # collision is a reconcilable success, not a fresh drop. Mark it
            # ALREADY_DONE so it counts as succeeded and is preserved durably.
            # Use mark_complete (not mark_skipped) so it satisfies the hard
            # dependencies of any current story that depends_on this slug —
            # a reconciled prior-DONE is a met dependency, exactly like a
            # resume skip_merged, so dependents must not be stranded/skipped.
            _reconciled = _reconciled_prior_outcome(slug)
            _log(f"{_reconciled.name} {slug} (reconciled from prior generation)")
            _sprint_state.dag.mark_complete(slug)
            _set_outcome(_sprint_state, slug, _reconciled, reason=reason)
            _reconcile_extras: dict = {"drop_reason": reason}
            if _reconciled is StoryOutcome.ALREADY_DONE:
                # Only a genuine no-op carries the ALREADY_DONE source tag; a
                # reconciled DONE must not be tagged as one.
                _reconcile_extras["outcome_source"] = "reexec_reconcile"
            # Carry the spend the generation that ran this story measured. The
            # 0.0 default would overwrite it, dropping real spend from the run
            # total and rendering a known amount as absent (#2189).
            _record_current_story_entry(
                slug,
                _reconciled.name,
                cost_usd=_measured_recorded_cost(slug),
                extras=_reconcile_extras,
            )
        elif reason == REASON_STRANDED_WORKTREE:
            # A prior-generation worktree exists but the story did not succeed:
            # recoverable stranded sprint state. Keep it DROPPED but retain the
            # distinct reason so RCA/audit can tell it apart from a fresh
            # collision (do NOT clear the worktree and re-sprint fresh).
            _log(f"DROPPED {slug} (stranded prior-generation sprint state)")
            _sprint_state.dag.mark_skipped(slug)
            _stranded_cause = _record_dropped_story_audit(_sprint_state, slug, reason)
            _stranded_cost = _measured_recorded_cost(slug)
            if _stranded_cost == 0.0:
                # No record of spend anywhere the sprint keeps one — but the
                # generation that stranded this worktree may have flushed its own
                # audit before it was interrupted (#2214).
                _recovered_stranded = _attribute_prior_generation_cost(slug)
                if _recovered_stranded is not None:
                    _stranded_cost = _recovered_stranded
            if _stranded_cost is None:
                _sprint_state.cost.flag_unmeasured_here(f"stranded-unmeasured:{slug}")
            _set_outcome(
                _sprint_state,
                slug,
                StoryOutcome.DROPPED,
                reason=reason,
                cost_usd=_stranded_cost,
                failure_cause=_stranded_cause or None,
            )
            # Same reasoning as the reconciled branch: the prior generation's
            # measured spend is this sprint's spend, whatever outcome the story
            # ended at. It must not be replaced by the 0.0 default (#2189).
            _stranded_extras: dict[str, object] = {"drop_reason": reason}
            _stranded_prior = _sprint_state.prior_generation_work.get(slug)
            if _stranded_prior:
                _stranded_extras["prior_generation_run_id"] = _stranded_prior.get("run_id")
                _stranded_extras["prior_generation_final_phase"] = _stranded_prior.get(
                    "final_phase"
                )
            _record_current_story_entry(
                slug,
                "DROPPED",
                error=reason,
                error_type="dropped",
                cost_usd=_stranded_cost,
                extras=_stranded_extras,
                failure_cause=_stranded_cause or None,
            )
        else:
            # A dropped story is normally a story that never ran — but if its
            # worktree holds commits, it did run, and recording that as a $0.00
            # no-evidence drop erases exactly the run an operator needs evidence
            # for. The spend was the previous process image's, so it is recovered
            # from the audit that generation flushed when there is one (#2214),
            # and recorded as unmeasured — never as zero — when there is not.
            work = _inspect_dropped_work(slug)
            work_detail = describe_worktree_work(work)
            if work_detail:
                _dropped_with_work[slug] = work_detail
            _detail_msg = f"{reason}: {work_detail}" if work_detail else reason
            _log(f"DROPPED {slug} (reason: {_detail_msg})")
            _sprint_state.dag.mark_skipped(slug)
            _extras: dict[str, object] = {"drop_reason": reason, **work.as_state_fields()}
            # The drop audit carries the fullest account available — the guard's
            # reason plus whatever work the drop abandoned — because that record,
            # not the sprint summary line, is what outlives the next resume.
            _drop_cause = _record_dropped_story_audit(_sprint_state, slug, _detail_msg)
            # The generation that ran this story recorded what it reached and
            # what it spent; the sprint row reports the same, so the summary
            # cannot claim a story with committed work cost nothing and reached
            # nothing (#2214).
            _carried = _sprint_state.prior_generation_work.get(slug)
            if _carried:
                _extras["prior_generation_run_id"] = _carried.get("run_id")
                _extras["prior_generation_final_phase"] = _carried.get("final_phase")
            _carried_cost = _attribute_prior_generation_cost(slug)
            if work_detail:
                if _carried_cost is None:
                    _sprint_state.cost.flag_unmeasured_here(f"dropped-with-work:{slug}")
                _set_outcome(
                    _sprint_state,
                    slug,
                    StoryOutcome.DROPPED,
                    reason=_detail_msg,
                    cost_usd=_carried_cost,
                    detail={"final_outcome": "DROPPED", **_extras},
                    failure_cause=_drop_cause or None,
                )
                _record_current_story_entry(
                    slug,
                    "DROPPED",
                    error=_detail_msg,
                    error_type="dropped",
                    cost_usd=_carried_cost,
                    extras=_extras,
                    failure_cause=_drop_cause or None,
                )
            else:
                # No worktree work to describe, but a prior generation may still
                # have spent budget on this story; 0.0 only when nothing did.
                _row_cost = _carried_cost if _carried_cost is not None else 0.0
                _set_outcome(
                    _sprint_state,
                    slug,
                    StoryOutcome.DROPPED,
                    reason=reason,
                    cost_usd=_row_cost,
                    failure_cause=_drop_cause or None,
                )
                _record_current_story_entry(
                    slug,
                    "DROPPED",
                    error=reason,
                    error_type="dropped",
                    cost_usd=_row_cost,
                    extras=_extras,
                    failure_cause=_drop_cause or None,
                )

    # Persist resume-time already-completed stories before any possible re-exec
    # handoff so later generations can recover the full logical sprint history.
    if _ctx.resume:

        def _already_done_story_entry(
            canonical_ref: str,
            slug: str,
            *,
            depends_on: list[str],
        ) -> dict:
            prior_entry = _sprint_state.recovered_prior_entries_by_ref.get(canonical_ref, {})
            # A story this sprint already ran to DONE keeps that outcome even
            # though its branch now reads as merged (#2150) — see
            # ``_skip_merged_outcome``.
            entry_outcome, entry_outcome_source = _skip_merged_outcome(slug, canonical_ref)
            display_key = (
                f"Issue #{canonical_ref.split(':')[1]}"
                if canonical_ref.startswith("issue:")
                else canonical_ref
            )
            return {
                "canonical_ref": canonical_ref,
                "path": display_key,
                "slug": slug,
                "outcome": entry_outcome.name,
                "outcome_source": entry_outcome_source,
                "verdict": prior_entry.get("verdict"),
                "cost_usd": optional_cost(prior_entry.get("cost_usd")),
                "story_run_id": prior_entry.get("story_run_id", _ctx.run_id),
                "preflight": prior_entry.get("preflight"),
                "preflight_original_verdict": prior_entry.get("preflight_original_verdict"),
                "preflight_source_run_id": prior_entry.get("preflight_source_run_id"),
                # A degradation the prior generation recorded is a fact about
                # the run, not about the generation that observed it — carry it
                # forward or a re-exec erases the only durable record (#2346).
                "preflight_degraded": bool(prior_entry.get("preflight_degraded", False)),
                "preflight_degraded_reason": prior_entry.get("preflight_degraded_reason"),
                "preflight_failure_action": prior_entry.get("preflight_failure_action"),
                "preflight_risk_signals": list(prior_entry.get("preflight_risk_signals") or []),
                # Same carry-forward reason as the degraded fields above: the
                # footprint this story was scheduled on is a fact about the run
                # (#2610), and a re-exec that dropped it would erase the only
                # record of what the collision edges were derived from.
                "preflight_likely_files": prior_entry.get("preflight_likely_files"),
                "error": prior_entry.get("error"),
                "error_type": prior_entry.get("error_type"),
                "merge": bool(prior_entry.get("merge", False)),
                "started_at": prior_entry.get("started_at"),
                "finished_at": prior_entry.get("finished_at"),
                "batch": int(prior_entry.get("batch", 0) or 0),
                "depends_on": depends_on,
                "dependency_warnings": list(prior_entry.get("dependency_warnings", [])),
                "inferred_dependencies": dict(prior_entry.get("inferred_dependencies", {})),
            }

        _resume_accumulated_by_ref: dict[str, dict] = {
            ref: dict(entry) for ref, entry in _sprint_state.recovered_prior_entries_by_ref.items()
        }
        for _canonical_ref, _triage in triages.items():
            if _triage.action != "skip_merged":
                continue
            _resume_slug = _triage.slug
            _resume_accumulated_by_ref.setdefault(
                _canonical_ref,
                _already_done_story_entry(
                    _canonical_ref,
                    _resume_slug,
                    depends_on=list(
                        getattr(
                            _ctx.slug_to_context.get(_resume_slug, (None, None, None))[0],
                            "depends_on",
                            None,
                        )
                        or []
                    ),
                ),
            )
        for _closed_slug in sorted(_ctx.resolved.closed_dependency_slugs):
            _canonical_ref = f"issue:{_closed_slug.removeprefix('issue-')}"
            if _canonical_ref in triages:
                continue
            _resume_accumulated_by_ref.setdefault(
                _canonical_ref,
                _already_done_story_entry(_canonical_ref, _closed_slug, depends_on=[]),
            )
        if _resume_accumulated_by_ref:
            persist_accumulated_story_state(
                _ctx.sprint_id,
                _ctx.resolved.name,
                _ctx.config.project_root,
                list(_resume_accumulated_by_ref.values()),
            )

    # Initialise live state file for forge sprint-status (only when a CLI run_id
    # is present — headless/test invocations without a run_id skip this).
    if _ctx.run_id:
        _bundle_candidate_slugs: set[str] = {s for bundle in bundle_assignments for s in bundle}
        _initial_stories: list[dict] = []
        _initial_story_slugs: set[str] = set()
        for _slug, (_task, _src, _canonical_ref) in _ctx.slug_to_context.items():
            _display_key = (
                f"Issue #{_canonical_ref.split(':')[1]}"
                if _canonical_ref.startswith("issue:")
                else _canonical_ref
            )
            _blocked_by = list(blocked_slugs.get(_slug, []))
            _drop_reason = _dropped_slugs.get(_slug)
            # reconcile (resume or re-exec): surface the merged/skip triage state
            # in the initial live status file so `forge sprint-status` shows a
            # re-exec'd merged story as done/skipped instead of waiting.
            _triage = triages.get(_canonical_ref) if reconcile else None
            if _drop_reason == "preserved-escalated":
                _status = "preserved"
                _blocked_by = [f"preserved: {_drop_reason}"]
                _detail = {"final_outcome": "ESCALATE"}
            elif _drop_reason == REASON_RECONCILE_PRIOR_DONE:
                # Prior generation already completed this story — surface it as
                # done, reconciled, not a fresh drop. Preserve *which* terminal
                # it reached: this row is what `forge status` renders in the
                # DETAIL column, so hard-coding ALREADY_DONE here is what made a
                # landed story read as a no-op after a re-exec (#2042).
                _status = "done"
                _blocked_by = []
                _reconciled_live = _reconciled_prior_outcome(_slug)
                _detail = {"final_outcome": _reconciled_live.name}
                if _reconciled_live is StoryOutcome.ALREADY_DONE:
                    _detail["outcome_source"] = "reexec_reconcile"
            elif _drop_reason == REASON_STRANDED_WORKTREE:
                # Recoverable stranded prior-generation sprint state — name it
                # distinctly rather than as a generic drop.
                _status = "failed"
                _blocked_by = [f"stranded prior-generation sprint state: {_drop_reason}"]
                _detail = {"final_outcome": "DROPPED", "drop_reason": _drop_reason}
            elif _drop_reason:
                _status = "failed"
                _blocked_by = [f"dropped: {_drop_reason}"]
                _detail = {"final_outcome": "ESCALATE"}
                # A drop that abandoned real work says so here, with the work
                # named — an operator reading this row must not have to infer
                # from a zero cost that nothing was produced.
                _work = _dropped_work.get(_slug)
                _work_detail = _dropped_with_work.get(_slug)
                if _work is not None and _work_detail:
                    _blocked_by = [f"dropped: {_drop_reason}: {_work_detail}"]
                    _detail = {
                        "final_outcome": "DROPPED",
                        "drop_reason": _drop_reason,
                        "drop_detail": _work_detail,
                        **_work.as_state_fields(),
                    }
            elif _triage and _triage.action == "skip_merged":
                # Preserve *which* terminal this sprint reached: a story this
                # run already carried to DONE must not be relabelled as
                # pre-existing work just because its own landing completed
                # around the re-exec boundary (#2150).
                _status = "done"
                _skip_outcome, _skip_source = _skip_merged_outcome(_slug, _canonical_ref)
                _detail = {"final_outcome": _skip_outcome.name}
                if _skip_source:
                    _detail["outcome_source"] = _skip_source
            elif _triage and _triage.action == "skip":
                _status = "skipped"
                _detail = {"final_outcome": "SKIPPED"}
            elif _slug in _inflight_slugs:
                # Still this run's own unfinished work from before the re-exec —
                # an agent group that survived it, a story the prior image had
                # dispatched and not settled, or liveness that could not be
                # resolved, which this run treats the same way. In every case it
                # waits for any inherited agent and then resumes the story: not a
                # drop, and not something an operator should read as idle.
                _in_flight_reason = (
                    REASON_IN_FLIGHT_UNRESOLVED
                    if _slug in _ctx.unresolved_live_slugs
                    else REASON_IN_FLIGHT
                )
                _status = "waiting"
                _blocked_by = [f"in flight: {_in_flight_reason}"]
                _detail = {
                    "in_flight": True,
                    "in_flight_reason": _in_flight_reason,
                    # Names what actually vouches for the story, so a reader is
                    # never told a process group is running when none is (#2617).
                    "in_flight_evidence": (
                        "sprint-owned-execution"
                        if _slug in _ctx.registered_live_slugs
                        else "agent-process-group"
                    ),
                }
            elif _blocked_by:
                _status = "blocked"
                _detail = {}
            else:
                _status = "waiting"
                _detail = {}
            _initial_stories.append(
                {
                    "slug": _slug,
                    "path": _display_key,
                    "status": _status,
                    "phase": "PREFLIGHT" if _status == "waiting" else None,
                    # A dropped story that abandoned work has a real cost: the
                    # prior generation's when its audit survived (#2214),
                    # unmeasured when it did not — never a fabricated $0.00.
                    # Projected so a story re-entering this generation starts its
                    # row holding the pre-restart spend, before any work — and so
                    # before anything can stop the process — rather than waiting
                    # on a wrap-up reattachment (#2922).
                    "cost_usd": _projected_story_cost(
                        _sprint_state, _slug, _dropped_row_cost(_slug), canonical=True
                    ),
                    "bundle_candidate": _slug in _bundle_candidate_slugs,
                    "batch_group": batch_group_by_slug.get(_slug),
                    "blocked_by": _blocked_by,
                    "complexity": None,
                    "detail": _detail,
                }
            )
            _initial_story_slugs.add(_slug)
        for _closed_slug in sorted(_ctx.resolved.closed_dependency_slugs):
            if _closed_slug in _initial_story_slugs:
                continue
            _issue_number = _closed_slug.removeprefix("issue-")
            _initial_stories.append(
                {
                    "slug": _closed_slug,
                    "path": f"Issue #{_issue_number}" if _issue_number.isdigit() else _closed_slug,
                    "status": "done",
                    "phase": None,
                    "cost_usd": 0.0,
                    "bundle_candidate": False,
                    "blocked_by": [],
                    "complexity": None,
                    "detail": {
                        "final_outcome": "ALREADY_DONE",
                        "outcome_source": "resume_skip_merged",
                    },
                }
            )
            _initial_story_slugs.add(_closed_slug)
        _sprint_state.state_writer = SprintStateWriter(
            _ctx.run_id,
            _ctx.config.project_root,
            _ctx.resolved.name,
            sprint_id=_ctx.sprint_id,
            story_state=_sprint_state.stories,
            budget_usd=_ctx.resolved.budget_usd,
            max_parallel=max_parallel,
            base_branch=getattr(getattr(_ctx.config, "workspace", None), "base_branch", None),
        )
        _sprint_state.state_writer.init(_initial_stories)
        _sprint_state.state_writer.set_phase("running")
        # Register shape-gate-skipped issues in the canonical structure so
        # forge status surfaces them with the gate reason. They are visible
        # to every operator surface from this point on.
        for _sk in _ctx.skipped_issues or []:
            _sk_dict = _sk.as_dict() if hasattr(_sk, "as_dict") else dict(_sk)
            _sk_num = _sk_dict.get("issue_number")
            if _sk_num is None:
                continue
            _sk_slug = f"issue-{_sk_num}"
            if _sprint_state.stories.has(_sk_slug):
                continue
            from .shape_gate import skipped_issue_state_fields  # noqa: PLC0415

            # Typed-verdict reason/detail resolution is centralized in
            # skipped_issue_state_fields; operator-action classification and
            # intake enrichment layer on top of it here.
            _sk_reason, _sk_detail = skipped_issue_state_fields(_sk)
            _sk_codes = _sk_dict.get("reason_codes") or []
            _is_operator_action = "operator_action" in _sk_codes
            _sk_outcome = (
                StoryOutcome.OPERATOR_ACTION if _is_operator_action else StoryOutcome.SKIPPED
            )
            if _is_operator_action:
                _sk_reason = "operator-action — operator deliverable"
                _sk_detail["operator_action"] = True
            _sk_detail["final_outcome"] = _sk_outcome.name
            _sk_intake = (_ctx.entry_intake_outcomes or {}).get(_sk_num)
            if _sk_intake is not None:
                _sk_detail["intake_kind"] = _sk_intake.kind.value
                _sk_detail["intake_detail"] = _sk_intake.detail
                _sk_detail["intake_findings"] = [f.as_dict() for f in _sk_intake.findings]
                _sk_detail["intake_audit"] = dict(_sk_intake.audit)
                _sk_detail["intake_proposed_replacement"] = _sk_intake.proposed_replacement
            _sprint_state.state_writer.register(
                _sk_slug,
                f"Issue #{_sk_num}",
                outcome=_sk_outcome,
                reason=_sk_reason,
                detail=_sk_detail,
            )
    elif _ctx.skipped_issues or []:
        # Headless invocation (no run_id) — still register skipped issues in
        # the canonical structure so summary projects them.
        for _sk in _ctx.skipped_issues or []:
            _sk_dict = _sk.as_dict() if hasattr(_sk, "as_dict") else dict(_sk)
            _sk_num = _sk_dict.get("issue_number")
            if _sk_num is None:
                continue
            _sk_slug = f"issue-{_sk_num}"
            from .shape_gate import skipped_issue_state_fields  # noqa: PLC0415

            _sk_reason, _sk_detail = skipped_issue_state_fields(_sk)
            _sk_codes = _sk_dict.get("reason_codes") or []
            _is_operator_action = "operator_action" in _sk_codes
            _sk_outcome = (
                StoryOutcome.OPERATOR_ACTION if _is_operator_action else StoryOutcome.SKIPPED
            )
            if _is_operator_action:
                _sk_reason = "operator-action — operator deliverable"
                _sk_detail["operator_action"] = True
            _sk_detail["final_outcome"] = _sk_outcome.name
            _sk_intake = (_ctx.entry_intake_outcomes or {}).get(_sk_num)
            if _sk_intake is not None:
                _sk_detail["intake_kind"] = _sk_intake.kind.value
                _sk_detail["intake_detail"] = _sk_intake.detail
                _sk_detail["intake_findings"] = [f.as_dict() for f in _sk_intake.findings]
                _sk_detail["intake_audit"] = dict(_sk_intake.audit)
                _sk_detail["intake_proposed_replacement"] = _sk_intake.proposed_replacement
            _sprint_state.stories.register(
                _sk_slug,
                f"Issue #{_sk_num}",
                outcome=_sk_outcome,
                reason=_sk_reason,
                detail=_sk_detail,
            )

    if _startup_budget_decision is not None:
        # An inherited agent group is paid work still running inside a worktree
        # this sprint owns. Recording its story as skipped and exiting leaves it
        # spending — against a ceiling the run has just declared exhausted — and
        # writing to that worktree with no owner left to settle it, until the
        # next invocation's orphan reaper finds it hours later with no context
        # (#2079). The refusal settles the process before it records the outcome,
        # for the same reason it fires ahead of intake and preflight: a refusal
        # that lets spending continue is not a refusal (#2922).
        for _refused_task in list(_sprint_state.dag.remaining()):
            _reclaim_dropped_agents(_refused_task.slug)
        _sprint_state.budget.skip_remaining_stories(_startup_budget_decision)

    # Parallel scheduling state
    story_deadlines: dict[str, float] = {}
    story_wait_started: set[str] = set()

    def _effective_deadline(slug: str) -> float:
        """The story's deadline with operator-wait time credited back (#2333).

        Time the system spends blocked at a gate it opened is not time the worker
        is unresponsive. Crediting it here — rather than at dispatch, where the
        length of the wait is not yet known — means a deadline that would elapse
        *during* a wait is extended by exactly the wait, and a story sitting at a
        gate with 41 minutes still on its clock is no longer killed eighteen
        seconds later for being "unresponsive".
        """
        return story_deadlines[slug] + worker_budget.operator_wait_credit(slug)

    worker_phases: dict[str, str] = {}
    pending_integration: dict[str, tuple[TaskStory, CoordinatorResult]] = {}
    _submission_counter = [0]  # mutable for closure capture; counts submitted stories

    # Auth circuit breaker (#1952): the structured cause of the first fatal
    # credential rejection observed after launch, or None while the substrate
    # is still believed reachable. Once set, no further story is dispatched —
    # the same credential would be presented and refused identically.
    auth_circuit: dict | None = None
    auth_circuit_reason = ""
    # Slugs the breaker cancelled mid-flight. Their futures return through the
    # generic stop_event cancellation path, which is timeout-shaped and would
    # classify them FAILED; this set is how the scheduler tells "we killed it
    # because the credential was dead" apart from "the story failed".
    auth_cancelled_slugs: set[str] = set()

    use_plan_gates = max_parallel > 1  # only for parallel mode

    def _reconcile_collision_claim(slug: str, result: CoordinatorResult) -> None:
        """Re-evaluate whether *slug* can still land the files it claimed."""
        if slug not in _sprint_state.collision_claims:
            return
        _sprint_state.claim_results[slug] = result
        if (
            slug in _sprint_state.queued_prs
            or slug in pending_integration
            or result.landing_status == "pending_integration"
        ):
            reason = CLAIM_PENDING_LANDING
        elif result.phase == Phase.ESCALATE and not getattr(
            result, "infrastructure_failure", False
        ):
            # Escalation preserves the worktree and its commits for an operator
            # decision. The work has not been abandoned — holding is the cheap
            # side of the asymmetry (waiting vs. a conflict found after spend).
            reason = CLAIM_PRESERVED
        else:
            _end_collision_claim(
                _sprint_state, slug, f"landing_status={result.landing_status or 'none'}"
            )
            return
        if _sprint_state.collision_claims.get(slug) != reason:
            _sprint_state.collision_claims[slug] = reason
            _files = (
                ", ".join(sorted(_sprint_state.file_footprints.get(slug, set())))
                or "its planned files"
            )
            _log(f"Holding collision claim for {slug} on {_files}: {reason}")

    def _refresh_collision_claims() -> None:
        """Re-evaluate every live claim against the latest known result."""
        for _c_slug in list(_sprint_state.collision_claims):
            _c_result = _sprint_state.claim_results.get(_c_slug)
            if _c_result is not None:
                _reconcile_collision_claim(_c_slug, _c_result)

    _sprint_state.publish_gate_hold = _make_gate_hold_publisher(_sprint_state.state_writer)

    def _claim_or_fail(slugs_to_claim: list[str]) -> bool:
        """Take durable ownership of every slug, or fail them all and dispatch none.

        Fail-closed on purpose (#2617). Ownership is what makes this sprint's own
        re-exec recognise the worktree as its own unfinished work; a story
        dispatched without it is spend the next re-exec may legitimately discard.
        An unwritable registry is an infrastructure fault of this run, and it is
        recorded as one — not as a judgment on the story, which nothing has
        judged.
        """
        claimed: list[str] = []
        try:
            for _slug in slugs_to_claim:
                _claim_story_execution(_sprint_state, _slug)
                claimed.append(_slug)
        except Exception as exc:
            for _slug in claimed:
                _release_story_execution(_sprint_state, _slug)
            _failed_at = datetime.datetime.now(datetime.timezone.utc)
            _claim_error = (
                f"Could not record this sprint's ownership of the story before "
                f"dispatching it: {type(exc).__name__}: {exc}"
            )
            for _slug in slugs_to_claim:
                _log(f"ERROR {_slug}: {_claim_error} — not dispatching")
                _claim_result = _abnormal_story_result(
                    _slug,
                    config=_ctx.config,
                    sprint_name=_ctx.resolved.name,
                    started_at=_failed_at,
                    error=_claim_error,
                    error_type=type(exc).__name__,
                    message=(
                        "Story was not dispatched: its execution-ownership record "
                        "could not be written, and running it unowned risks a "
                        "mid-run re-exec discarding the work as a foreign collision"
                    ),
                )
                _claim_cause = build_abnormal_cause(
                    kind=ABNORMAL_SHARED_INFRASTRUCTURE,
                    cause=_claim_error,
                    error_type=type(exc).__name__,
                    run_id=_claim_result.state.run_id,
                    source="sprint.runner:story-execution-registry",
                )
                _claim_result.state.abnormal_termination = _claim_cause
                _claim_result.infrastructure_failure = True
                _sprint_state.story_times[_slug] = (_failed_at, _failed_at)
                _sprint_state.results.append((slug_to_spec[_slug], _claim_result))
                _settle_terminal_story_audit(
                    _slug,
                    _ctx.slug_to_context[_slug][0],
                    _claim_result,
                )
                _set_outcome(
                    _sprint_state,
                    _slug,
                    StoryOutcome.FAILED,
                    phase="ESCALATE",
                    failure_cause=_claim_cause,
                )
                _persist_current_story_result(
                    _sprint_state,
                    _slug,
                    _claim_result,
                    started_at=_failed_at,
                    finished_at=_failed_at,
                )
                _sprint_state.dag.mark_skipped(_slug)
            return False
        return True

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        while not _sprint_state.dag.is_done():
            # Ownership records for stories this scheduler has finished settling.
            # Swept here, at the top of a pass, so every exit from the result loop
            # below — including the ones that ``continue`` the outer loop — passes
            # through it, and so a record is never dropped while its story is
            # still being reconciled (#2617).
            _release_settled_story_executions(_sprint_state)
            _log(
                f"[debug] loop: active={list(_sprint_state.active.keys())} "
                f"fin={_sprint_state.dag._finished}"
            )
            _refresh_external_satisfied(
                _sprint_state.dag, all_tasks, _ctx.config, _sprint_state.merged_slugs
            )
            ready = [t for t in _sprint_state.dag.ready() if t.slug not in _sprint_state.active]

            # Publication keeps pace with enforcement (#2595). Every story this
            # sprint finishes writes its canonical run record and knowledge
            # summary into the project-root checkout, and the landing
            # precondition — evaluated at each story's WORKSPACE entry — refuses
            # on any project-root dirt, untracked files included. Publishing
            # only at sprint exit meant story 1's own artifacts refused story 2.
            #
            # This is the one point every dispatch passes through: the loop body
            # below is the sole place a slug is registered active, and the
            # previous pass has already settled each finished story's terminal
            # audit writes (including the landing-status rewrite that follows
            # integration). So in sequential mode — where a pass with work to
            # dispatch is always a pass with nothing in flight — no story can
            # enter WORKSPACE between a completed story's artifact write and
            # this publish.
            #
            # It is skipped while workers are in flight. Under a project-root
            # landing workflow those workers merge into the same checkout this
            # would be committing from, and taking the index out from under a
            # merge would turn a bookkeeping step into a story failure. Nor
            # would publishing there be sufficient: a sibling finishing a second
            # after the publish dirties the root again before the next entry, so
            # parallel dispatch cannot be made race-free from here at all. Those
            # passes fall through to the next quiescent one, or to the terminal
            # sweep — exactly the behaviour they have today.
            #
            # This call therefore protects sequential *entry* only. Protection
            # at merge time under concurrency lives where it can be race-free:
            # ``_publish_sibling_artifacts``, called from _attempt_integration
            # inside integration_lock immediately before the merge (#2602).
            #
            # A failure is deferred rather than raised: the terminal publish
            # below is the final, fatal sweep.
            #
            # The quiescence requirement is a property of the *direct*
            # transport, not of publishing (#2598). The memory-branch transport
            # commits from an isolated worktree on its own branch and only
            # drains the memory trees out of the project root, so there is no
            # index for a concurrent merge to lose — and under the modes that
            # use it (``merge-pr`` / ``pr`` / ``none``) no story merges into the
            # project-root checkout at all. Those runs publish every pass, which
            # is what keeps a finished story's artifacts from standing across
            # its sibling's entry under parallelism.
            if ready and (not _sprint_state.active or not _publish_needs_quiescence):
                publish_pending_story_run_audits(
                    _sprint_state, lands_locally=_sprint_lands_locally
                )

            for task in ready:
                # ``ready`` is a snapshot taken before this pass. A batch-group
                # dispatch earlier in the same pass registers every member as
                # active at once, so a member still sitting in the snapshot must
                # not be dispatched a second time.
                if task.slug in _sprint_state.active:
                    continue

                # Auth circuit breaker (#1952): a credential the substrate
                # already had refused is not worth re-presenting. Skip rather
                # than fail — nothing about this story was ever judged.
                if auth_circuit is not None:
                    _sprint_state.dag.mark_skipped(task.slug)
                    _set_outcome(
                        _sprint_state, task.slug, StoryOutcome.SKIPPED, reason=auth_circuit_reason
                    )
                    _log(f"SKIPPED {task.slug} ({auth_circuit_reason})")
                    _record_current_story_entry(task.slug, "SKIPPED", error=auth_circuit_reason)
                    if _sprint_state.state_writer is not None:
                        _sprint_state.state_writer.update(task.slug, status="skipped")
                    continue

                # Both hard (depends_on) and soft (collision_deps) parents must
                # honor the queued-PR reachability gate. dag.ready() releases a
                # collision edge the instant its parent reaches a terminal state,
                # and a merge-queued parent is marked terminal (pending_integration
                # -> mark_skipped) the moment its PR is queued — before the merge
                # commit is reachable on origin base. Without gating collision_deps
                # here, a dependent (fresh OR resume-at-review) dispatches onto a
                # base that predates the parent's landed fix, then conflicts at
                # merge time. Gating on queued_prs membership preserves the
                # abandon-and-proceed semantics for genuinely failed collision
                # parents: those are never in queued_prs, so they skip this gate.
                _gate_deps = list(dict.fromkeys((*task.depends_on, *task.collision_deps)))
                blocked_by_queued = [dep for dep in _gate_deps if dep in _sprint_state.queued_prs]
                if blocked_by_queued:
                    dependency_failed = False
                    for dep in blocked_by_queued:
                        if (
                            _resolve_queued_pr(
                                _sprint_state, dep, blocking=True, why="before dependent dispatch"
                            )
                            == "failed"
                        ):
                            dependency_failed = True
                    if dependency_failed:
                        continue
                    if any(dep in _sprint_state.queued_prs for dep in _gate_deps):
                        continue

                # Cap concurrent submissions at max_parallel
                if len(_sprint_state.active) >= max_parallel:
                    break

                _budget_decision = _sprint_state.budget.decision_before_dispatch()
                if _budget_decision is not None:
                    _sprint_state.budget.skip_story(task.slug, _budget_decision)
                    continue

                # A sibling can finish *during* this pass, after the publish
                # above and before this story is admitted, leaving its record in
                # the shared checkout for the newcomer to be refused by (#2598).
                # The pass-level publish cannot close that window; a drain
                # immediately before admission can, and costs a status probe.
                if not _publish_needs_quiescence:
                    drain_project_memory_before_dispatch(_sprint_state)

                # Eager merge for sequential mode; disabled in parallel mode
                effective_am = (
                    False
                    if max_parallel > 1
                    else (_ctx.auto_merge or task.slug in dependent_slugs)
                )

                # Will *this* story's approval merge into the project-root
                # checkout? effective_am alone understates it: a parallel
                # dependency parent is forced into a local merge by the
                # scheduler after it returns (see the pending_integration
                # conversion below), so with on_approve "none" or "pr" the
                # worker would otherwise see no landing obligation, run dev and
                # review, and only then meet the dirty-root refusal (#2048).
                # It also overstates it, hence the dependency_parents_ guard: a
                # parallel merge-pr parent lands through its PR, not here.
                _story_lands_in_root = (
                    effective_am
                    or config_lands_in_project_root
                    or (
                        task.slug in in_manifest_dependency_parents
                        and dependency_parents_land_in_project_root
                    )
                )

                spec_str = slug_to_spec[task.slug]
                triage = triages.get(spec_str) if _ctx.resume else None
                # A story whose agent survived the re-exec is dispatched through
                # the deferred path: it waits for that agent, triages what it
                # left, and resumes — so it reaches a real terminal outcome in
                # this run instead of being abandoned to the next invocation's
                # orphan reaper.
                _inherited = task.slug in _inflight_slugs

                # ── Cost-aware batch group dispatch (#727) ──────────────
                # Reached only after every ordinary readiness decision above:
                # hard depends_on, collision_deps, the queued-PR reachability
                # gate, the parallelism cap, and the budget check. Batching is a
                # cost optimisation layered on top of scheduling, never a way
                # around it.
                _batch_gid = batch_group_by_slug.get(task.slug)
                if _batch_gid is not None and (_inherited or triage is not None):
                    # An inherited/resumed story re-enters through its own
                    # worktree triage; it cannot join a shared dev pass.
                    _break_batch_group(_batch_gid, f"{task.slug} resumed from existing worktree")
                    _batch_gid = None
                if _batch_gid is not None:
                    _members = batch_members_by_group.get(_batch_gid, [])
                    _ready_by_slug = {t.slug: t for t in ready}
                    _dispatchable = [
                        m
                        for m in _members
                        if m in _ready_by_slug
                        and m not in _sprint_state.active
                        and m not in _inflight_slugs
                        and m in _ctx.slug_to_context
                        and (triages.get(slug_to_spec[m]) if _ctx.resume else None) is None
                    ]
                    if len(_dispatchable) < 2:
                        # The group did not become ready as a unit (a member was
                        # skipped, dropped, or is resuming). Dispatch every
                        # member on its own rather than delaying or dropping
                        # work for the sake of a cost optimisation.
                        _break_batch_group(
                            _batch_gid, "members did not become ready simultaneously"
                        )
                        _batch_gid = None
                    elif task.slug != _dispatchable[0]:
                        # Wait for the leader; it dispatches the whole group.
                        continue
                    elif not _claim_or_fail(list(_dispatchable)):
                        # Every member is now recorded as an infrastructure
                        # failure and marked skipped; nothing was submitted.
                        continue
                    else:
                        _batch_tasks = [_ready_by_slug[m] for m in _dispatchable]
                        _leader_task = _make_batch_leader(_batch_tasks, _batch_gid)
                        _batch_stop_evt = StopSignal()
                        _batch_state_fns: dict[str, Callable[[dict], None] | None] = {}
                        for _member_slug, _member_task in zip(
                            _dispatchable, _batch_tasks, strict=True
                        ):
                            _sprint_state.batch_assignments[_member_slug] = (
                                _sprint_state.batch_number
                            )
                            _submission_counter[0] += 1
                            print(
                                _story_header(_submission_counter[0], total, _member_slug),
                                file=sys.stderr,
                                flush=True,
                            )
                            if _sprint_state.state_writer is not None:
                                _sprint_state.state_writer.update(
                                    _member_slug,
                                    status="running",
                                    batch_group=_batch_gid,
                                    started_at=datetime.datetime.now(
                                        datetime.timezone.utc
                                    ).isoformat(),
                                )
                            # No plan gate for a batch group: the members were
                            # selected precisely because they are pairwise
                            # non-overlapping, and they share one worker, so
                            # there is no sibling footprint for a PLAN_DONE gate
                            # to arbitrate. Registering one would park the
                            # scheduler on a signal this path never emits.
                            _batch_state_fns[_member_slug] = _make_worker_phase_fn(
                                _member_slug,
                                worker_phases,
                                _sprint_state.phase_lock,
                                _ctx.state_update_fn,
                                plan_done=None,
                                state_writer=_sprint_state.state_writer,
                                audit_flush=_make_audit_flush_fn(
                                    _ctx.config,
                                    _member_task,
                                    _ctx.resolved.name,
                                    sprint_id=_ctx.sprint_id,
                                ),
                                budget_checkpoint=_sprint_state.budget.checkpoint,
                                live_cost_updates=_sprint_state.latest_live_costs,
                                cost_projection=_canonical_cost_projector(
                                    _sprint_state, _member_slug
                                ),
                                stop_event=_batch_stop_evt,
                            )
                            _sprint_state.stop_events[_member_slug] = _batch_stop_evt
                        # One worker runs the group, so the whole group shares one
                        # deadline: the sum of what its members would each have
                        # been allowed on their own. Registered BEFORE submit —
                        # the worker can reach for its enclosing ceiling on its
                        # first instruction, and a budget published afterwards is
                        # a race the story loses silently (#2333).
                        _batch_started = time.monotonic()
                        _batch_window = float(sum(story_worker_timeouts[m] for m in _dispatchable))
                        _batch_deadline = _batch_started + _batch_window
                        for _member_slug in _dispatchable:
                            # A gate entered under any member's slug must credit
                            # the deadline every member is measured against, so
                            # the whole group shares one budget group.
                            worker_budget.register_worker_budget(
                                _member_slug,
                                _batch_window,
                                group=f"batch:{_batch_gid}",
                                started_at=_batch_started,
                            )
                        _batch_fut = pool.submit(
                            _run_batch_group,
                            _ctx.config,
                            _leader_task,
                            _batch_tasks[1:],
                            _sprint_state.sprint_run_id,
                            _ctx.resolved.name,
                            _ctx.interactive,
                            _ctx.notify,
                            effective_am,
                            _batch_state_fns,
                            _ctx.no_pull,
                            preflight_states,
                            _batch_stop_evt,
                            base_lands_locally=_sprint_lands_locally,
                            lands_in_project_root=_story_lands_in_root,
                        )
                        _dispatched_batch_leader[_batch_gid] = _dispatchable[0]
                        _sprint_state.batch_group_of_leader[_dispatchable[0]] = _batch_gid
                        for _member_slug in _dispatchable:
                            _sprint_state.active[_member_slug] = _batch_fut
                            story_deadlines[_member_slug] = _batch_deadline
                        continue

                # Ownership before anything that spends (#2617). An inherited
                # story already has a record from the generation that dispatched
                # it; rewriting it here is harmless and keeps the one rule —
                # nothing reaches the pool unowned — free of exceptions.
                if not _claim_or_fail([task.slug]):
                    continue

                _sprint_state.batch_assignments[task.slug] = _sprint_state.batch_number
                _submission_counter[0] += 1
                print(
                    _story_header(_submission_counter[0], total, task.slug),
                    file=sys.stderr,
                    flush=True,
                )
                if _sprint_state.state_writer is not None:
                    _sprint_state.state_writer.update(
                        task.slug,
                        status="running",
                        started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    )

                # Create plan gate for fresh parallel runs. An inherited story
                # re-enters through triage and never emits PLAN_DONE, so gating
                # it would block the scheduler on a signal that never comes.
                gate: threading.Event | None = None
                if use_plan_gates and triage is None and not _inherited:
                    gate = threading.Event()
                    _sprint_state.plan_gates[task.slug] = gate

                worker_config = _ctx.config

                stop_evt = StopSignal()
                state_fn = _make_worker_phase_fn(
                    task.slug,
                    worker_phases,
                    _sprint_state.phase_lock,
                    _ctx.state_update_fn,
                    plan_done=_sprint_state.plan_done if use_plan_gates else None,
                    state_writer=_sprint_state.state_writer,
                    audit_flush=_make_audit_flush_fn(
                        _ctx.config, task, _ctx.resolved.name, sprint_id=_ctx.sprint_id
                    ),
                    budget_checkpoint=_sprint_state.budget.checkpoint,
                    live_cost_updates=_sprint_state.latest_live_costs,
                    cost_projection=_canonical_cost_projector(_sprint_state, task.slug),
                    stop_event=stop_evt,
                )
                _sprint_state.stop_events[task.slug] = stop_evt
                _dispatch_kwargs: dict = {
                    "base_lands_locally": _sprint_lands_locally,
                    "lands_in_project_root": _story_lands_in_root,
                }
                if _inherited:
                    _dispatch_fn = _run_inherited_story
                    _dispatch_kwargs["canonical_ref"] = spec_str
                    # Half the worker budget to wait for the inherited agent,
                    # leaving the other half to actually resume the story before
                    # the scheduler's deadline expires the worker.
                    _dispatch_kwargs["quiesce_timeout"] = (
                        float(story_worker_timeouts[task.slug]) / 2.0
                    )
                else:
                    _dispatch_fn = _run_single_story
                # Publish the enclosing ceiling the story runs inside BEFORE the
                # worker starts, so phase and gate allowances derived deep in the
                # coordinator are derived against the budget that contains them
                # rather than racing its registration (#2333).
                _story_started = time.monotonic()
                worker_budget.register_worker_budget(
                    task.slug,
                    float(story_worker_timeouts[task.slug]),
                    started_at=_story_started,
                )
                fut = pool.submit(
                    _dispatch_fn,
                    worker_config,
                    task,
                    triage,
                    _sprint_state.sprint_run_id,
                    _ctx.resolved.name,
                    _ctx.interactive,
                    _ctx.notify,
                    _ctx.resume,
                    effective_am,
                    state_fn,
                    _ctx.no_pull,
                    gate,
                    preflight_states,
                    stop_evt,
                    # Keyword, not positional: stop_evt must stay the last
                    # positional argument for callers that index args[-1].
                    **_dispatch_kwargs,
                )
                _sprint_state.active[task.slug] = fut
                story_deadlines[task.slug] = _story_started + float(
                    story_worker_timeouts[task.slug]
                )

            _log(
                f"[debug] post-submit: active={list(_sprint_state.active.keys())}"
                f" queued_prs={list(_sprint_state.queued_prs.keys())}"
            )
            if not _sprint_state.active and not _sprint_state.queued_prs:
                # A terminal-but-not-merged soft-edge parent may have just
                # released a dependent's collision edge. Before declaring a
                # deadlock and sweeping remaining tasks into SKIP, re-enter
                # dispatch for anything now schedulable so a released, ready
                # story runs on the current base instead of being skipped.
                if any(t.slug not in _sprint_state.active for t in _sprint_state.dag.ready()):
                    continue
                # Deadlock: remaining tasks have unmet or budget-blocked deps
                # Release any pending plan gates so worker threads can exit
                for g_slug, _gate in _sprint_state.plan_gates.items():
                    _log(f"Releasing plan gate for {g_slug} (deadlock cleanup)")
                    _gate.set()
                _sprint_state.plan_gates.clear()
                for t in _sprint_state.dag.remaining():
                    # A mark_skipped earlier in THIS sweep can release a
                    # sibling's soft edge; if that makes any task schedulable,
                    # stop skipping and re-enter dispatch on the next loop pass
                    # rather than sweeping the just-released sibling into a SKIP.
                    if any(r.slug not in _sprint_state.active for r in _sprint_state.dag.ready()):
                        break
                    unmet = _sprint_state.dag.unmet_deps(t.slug)
                    if unmet:
                        dep_list = ", ".join(unmet)
                        _log(f"SKIPPED {t.slug} (dependency failed: {dep_list})")
                        _record_current_story_entry(
                            t.slug, "SKIPPED", error=f"dependency failed: {dep_list}"
                        )
                        _set_outcome(
                            _sprint_state,
                            t.slug,
                            StoryOutcome.SKIPPED,
                            reason=f"dependency failed: {dep_list}",
                        )
                    else:
                        _log(f"SKIPPED {t.slug} (blocked)")
                        _record_current_story_entry(t.slug, "SKIPPED", error="blocked")
                        _set_outcome(_sprint_state, t.slug, StoryOutcome.SKIPPED, reason="blocked")
                    _sprint_state.dag.mark_skipped(t.slug)
                else:
                    break
                continue

            # No active workers but queued PRs are still in flight.
            # Poll each queued PR directly so dependents can be dispatched
            # once the PR lands — do not declare deadlock while PRs are pending.
            if not _sprint_state.active and _sprint_state.queued_prs:
                for _qp_slug in list(_sprint_state.queued_prs):
                    # The redispatch-after-restart bug lives on this path: a
                    # queued PR merges while another story occupies the only
                    # worker slot. _resolve_queued_pr writes the confirmed-landed
                    # DONE with its immutability marker so it cannot be clobbered.
                    _resolve_queued_pr(
                        _sprint_state, _qp_slug, blocking=True, why="no active workers"
                    )
                continue

            _log(f"[debug] calling wait() with {len(_sprint_state.active)} active futures")
            # Use a short poll interval when plan gates are pending so the
            # scheduler can release gated workers between polls.  Without
            # this, gated workers block in _run_fresh waiting for their gate
            # while the scheduler blocks here waiting for a future to finish
            # — a deadlock.
            done_futs: set = set()
            expired_slugs: list[str] = []
            while not done_futs and not expired_slugs:
                _now = time.monotonic()
                _time_to_next_timeout = max(
                    0.0,
                    min(
                        (
                            float(story_worker_timeouts[slug])
                            if slug not in story_wait_started
                            else _effective_deadline(slug) - _now
                        )
                        for slug in _sprint_state.active
                    ),
                )
                _poll_interval = (
                    min(PLAN_GATE_TICK_SECONDS, _time_to_next_timeout)
                    if _sprint_state.plan_gates
                    else _time_to_next_timeout
                )
                done_futs, _ = wait(
                    list(_sprint_state.active.values()),
                    return_when=FIRST_COMPLETED,
                    timeout=_poll_interval,
                )
                story_wait_started.update(_sprint_state.active)
                if not done_futs and use_plan_gates:
                    # Service plan gates while polling
                    _service_plan_gates(_sprint_state)
                _now = time.monotonic()
                expired_slugs = [
                    slug
                    for slug, fut in _sprint_state.active.items()
                    if fut not in done_futs and _now >= _effective_deadline(slug)
                ]

            _log(f"[debug] wait() returned: {len(done_futs)} done")
            _sprint_state.batch_number += 1

            if expired_slugs:
                for slug in expired_slugs:
                    if slug not in _sprint_state.active:
                        continue
                    if slug in _sprint_state.plan_gates:
                        _log(f"TIMEOUT releasing plan gate for {slug}")
                        _sprint_state.plan_gates[slug].set()
                        del _sprint_state.plan_gates[slug]
                    fut = _sprint_state.active[slug]
                    affected_slugs = [
                        active_slug
                        for active_slug, active_fut in _sprint_state.active.items()
                        if active_fut is fut
                    ]
                    recovered_snapshot = _sprint_state.cost.snapshot()
                    for affected_slug in affected_slugs:
                        del _sprint_state.active[affected_slug]
                        story_deadlines.pop(affected_slug, None)
                        story_wait_started.discard(affected_slug)
                        # A worker killed at its deadline is not preserved work
                        # awaiting a decision — nothing can land it, so its files
                        # are free.
                        _end_collision_claim(_sprint_state, affected_slug, "worker timed out")
                        # Set the cancellation event BEFORE cancel() so any in-flight
                        # work stops at the next phase boundary or subprocess read.
                        # Future.cancel() is a no-op for an already-running thread.
                        _stop_evt = _sprint_state.stop_events.pop(affected_slug, None)
                        if _stop_evt is not None:
                            _stop_evt.set()
                        fut.cancel()
                    for affected_slug in affected_slugs:
                        # An elapsed deadline is exhausted time, not a verdict on the
                        # work and not evidence the worker stopped responding — a
                        # story killed here may have been converging, mid-edit, or in
                        # a wait this system itself opened. Say which (#2333).
                        _wait_credit = worker_budget.operator_wait_credit(affected_slug)
                        _was_waiting, _wait_phase, _wait_len = worker_budget.waiting_on_operator(
                            affected_slug
                        )
                        _wait_note = ""
                        if _wait_credit > 0:
                            _wait_note = (
                                f"; {_fmt_duration(_wait_credit)} of operator-decision wait"
                                " was excluded from the deadline"
                            )
                        if _was_waiting:
                            _wait_note += (
                                f"; still waiting on an operator decision"
                                f"{f' at {_wait_phase}' if _wait_phase else ''}"
                                f" after {_fmt_duration(_wait_len)}"
                            )
                        _log(
                            f"TIMEOUT {affected_slug} (story deadline exhausted after "
                            f"{story_worker_timeouts[affected_slug]}s of working time{_wait_note}"
                            " — marking as failed on wall clock, not on quality)"
                        )
                        worker_budget.unregister_worker_budget(affected_slug)
                        spec_str = slug_to_spec[affected_slug]
                        timed_out_at = datetime.datetime.now(datetime.timezone.utc)
                        snapshot = _snapshot_last_known(affected_slug, _sprint_state.state_writer)
                        last_live_cost = _sprint_state.latest_live_costs.pop(affected_slug, None)
                        if _sprint_state.cost.has_in_flight_cost(affected_slug) or (
                            len(affected_slugs) == 1
                            and (last_live_cost is not None or snapshot["last_cost"] is not None)
                        ):
                            recovered_snapshot = _sprint_state.cost.recover_in_flight_cost(
                                affected_slug,
                                fallback_cost=(
                                    last_live_cost.amount
                                    if last_live_cost is not None
                                    else snapshot["last_cost"]
                                ),
                                fallback_measured=(
                                    True if last_live_cost is None else last_live_cost.measured
                                ),
                            )
                        last_phase = snapshot["last_phase"]
                        if affected_slug in _sprint_state.story_times:
                            story_started_at = _sprint_state.story_times[affected_slug][0]
                        elif snapshot["last_started_at"] is not None:
                            story_started_at = snapshot["last_started_at"]
                        else:
                            story_started_at = timed_out_at
                        _phase_label = f" during phase {last_phase}" if last_phase else ""
                        # Deadline exhaustion, stated as such. The operator action for
                        # a story that ran out of wall clock is not the action for one
                        # that produced an unacceptable result, and only the second is
                        # evidence about the work (#2333).
                        _timeout_error = (
                            "Story deadline exhausted (>"
                            f"{story_worker_timeouts[affected_slug]}s of "
                            f"working time){_phase_label}{_wait_note}"
                        )
                        _timeout_result = _abnormal_story_result(
                            affected_slug,
                            config=_ctx.config,
                            sprint_name=_ctx.resolved.name,
                            started_at=story_started_at,
                            error=_timeout_error,
                            error_type="TimeoutError",
                            message=(
                                "Story deadline exhausted after "
                                f"{story_worker_timeouts[affected_slug]}s of working time — "
                                "not a review or quality failure"
                            ),
                        )
                        _timeout_cause = build_abnormal_cause(
                            kind=ABNORMAL_WORKER_TIMEOUT,
                            cause=_timeout_error,
                            error_type="TimeoutError",
                            phase=last_phase,
                            run_id=_timeout_result.state.run_id,
                            source="sprint.runner:worker-deadline",
                        )
                        _timeout_result.state.abnormal_termination = _timeout_cause
                        _sprint_state.story_times[affected_slug] = (
                            story_started_at,
                            timed_out_at,
                        )
                        _sprint_state.live_telemetry_snapshots[affected_slug] = snapshot
                        # A worker the auth breaker cancelled can also cross its
                        # deadline before returning. It is still a story the sprint
                        # killed over a dead credential, not one that failed — same
                        # attribution as the ordinary cancellation path below.
                        _timeout_outcome: StoryOutcome = StoryOutcome.FAILED
                        if affected_slug in auth_cancelled_slugs:
                            auth_cancelled_slugs.discard(affected_slug)
                            _cancel_reason = f"cancelled mid-flight: {auth_circuit_reason}"
                            _mark_story_auth_cancelled(
                                _timeout_result, auth_circuit, reason=_cancel_reason
                            )
                            _timeout_outcome = StoryOutcome.SKIPPED
                            _log(f"SKIPPED {affected_slug} ({_cancel_reason})")
                        elif affected_slug in _sprint_state.budget_cancelled_slugs:
                            _sprint_state.budget_cancelled_slugs.discard(affected_slug)
                            _cancel_reason = _budget_cancel_reason(_sprint_state)
                            _mark_story_budget_cancelled(_timeout_result, reason=_cancel_reason)
                            _timeout_outcome = StoryOutcome.SKIPPED
                            _log(f"SKIPPED {affected_slug} ({_cancel_reason})")
                        _sprint_state.results.append((spec_str, _timeout_result))
                        _settle_terminal_story_audit(
                            affected_slug,
                            _ctx.slug_to_context[affected_slug][0],
                            _timeout_result,
                            telemetry_snapshot=snapshot,
                        )
                        _set_outcome(
                            _sprint_state,
                            affected_slug,
                            _timeout_outcome,
                            phase="ESCALATE",
                            last_phase=last_phase,
                            failure_cause=_timeout_cause,
                            # The gate this story may have been sitting in never
                            # reported a decision and never will; leaving the live
                            # detail at gate_status=running is what made the state
                            # file claim a running gate on a failed story (#2013).
                            detail_updates={"gate_status": GATE_STATUS_TIMEOUT},
                        )
                        _persist_current_story_result(
                            _sprint_state,
                            affected_slug,
                            _timeout_result,
                            started_at=story_started_at,
                            finished_at=timed_out_at,
                        )
                        _sprint_state.dag.mark_skipped(affected_slug)
                    _sprint_state.budget.publish_live_status(
                        recovered_snapshot.spent_including_in_flight
                    )
                continue

            for slug, fut in list(_sprint_state.active.items()):
                if slug not in _sprint_state.active:
                    continue
                if fut not in done_futs:
                    continue
                try:
                    _fut_value = fut.result()
                    # A batch-group worker runs several stories on one future and
                    # returns one result tuple per member slug. Everything below
                    # this unpack is per-story and unchanged: the batch is a
                    # dispatch detail, not a reporting unit.
                    if isinstance(_fut_value, dict):
                        task, result, elapsed, t0, t1 = _fut_value[slug]
                    else:
                        task, result, elapsed, t0, t1 = _fut_value  # type: ignore[misc]
                except Exception as exc:
                    _affected_slugs = [
                        active_slug
                        for active_slug, active_fut in _sprint_state.active.items()
                        if active_fut is fut
                    ]
                    _recovery_slug = next(
                        (
                            active_slug
                            for active_slug in _affected_slugs
                            if _sprint_state.cost.has_in_flight_cost(active_slug)
                        ),
                        _affected_slugs[0] if len(_affected_slugs) == 1 else None,
                    )
                    _recovered_cost = _sprint_state.cost.snapshot()
                    for affected_slug in _affected_slugs:
                        _log(
                            f"ERROR {affected_slug}: worker thread raised "
                            f"{type(exc).__name__}: {exc}"
                        )
                        del _sprint_state.active[affected_slug]
                        story_deadlines.pop(affected_slug, None)
                        worker_budget.unregister_worker_budget(affected_slug)
                        story_wait_started.discard(affected_slug)
                        _sprint_state.stop_events.pop(affected_slug, None)
                        snapshot = _snapshot_last_known(affected_slug, _sprint_state.state_writer)
                        _last_live_cost = _sprint_state.latest_live_costs.pop(affected_slug, None)
                        if affected_slug == _recovery_slug:
                            _recovered_cost = _sprint_state.cost.recover_in_flight_cost(
                                affected_slug,
                                fallback_cost=(
                                    _last_live_cost.amount
                                    if _last_live_cost is not None
                                    else snapshot["last_cost"]
                                ),
                                fallback_measured=(
                                    True if _last_live_cost is None else _last_live_cost.measured
                                ),
                            )
                        _end_collision_claim(_sprint_state, affected_slug, "worker raised")
                        spec_str = slug_to_spec[affected_slug]
                        failed_at = datetime.datetime.now(datetime.timezone.utc)
                        last_phase = snapshot["last_phase"]
                        if affected_slug in _sprint_state.story_times:
                            story_started_at = _sprint_state.story_times[affected_slug][0]
                        elif snapshot["last_started_at"] is not None:
                            story_started_at = snapshot["last_started_at"]
                        else:
                            story_started_at = failed_at
                        _phase_label = f" during phase {last_phase}" if last_phase else ""
                        _exc_error = f"Worker exception{_phase_label}: {exc}"
                        _exc_result = _abnormal_story_result(
                            affected_slug,
                            config=_ctx.config,
                            sprint_name=_ctx.resolved.name,
                            started_at=story_started_at,
                            error=_exc_error,
                            error_type=type(exc).__name__,
                            message=f"Worker thread raised {type(exc).__name__}: {exc}",
                        )
                        _exc_cause = build_abnormal_cause(
                            kind=ABNORMAL_WORKER_EXCEPTION,
                            cause=_exc_error,
                            error_type=type(exc).__name__,
                            phase=last_phase,
                            run_id=_exc_result.state.run_id,
                            source="sprint.runner:worker-exception",
                        )
                        _exc_result.state.abnormal_termination = _exc_cause
                        _sprint_state.story_times[affected_slug] = (story_started_at, failed_at)
                        _sprint_state.live_telemetry_snapshots[affected_slug] = snapshot
                        # Same attribution as the other two cancellation exits: a
                        # worker that raised on its way out of an auth-breaker
                        # cancellation was killed by the sprint, not by the story.
                        _exc_outcome: StoryOutcome = StoryOutcome.FAILED
                        if affected_slug in auth_cancelled_slugs:
                            auth_cancelled_slugs.discard(affected_slug)
                            _cancel_reason = f"cancelled mid-flight: {auth_circuit_reason}"
                            _mark_story_auth_cancelled(
                                _exc_result, auth_circuit, reason=_cancel_reason
                            )
                            _exc_outcome = StoryOutcome.SKIPPED
                            _log(f"SKIPPED {affected_slug} ({_cancel_reason})")
                        elif affected_slug in _sprint_state.budget_cancelled_slugs:
                            _sprint_state.budget_cancelled_slugs.discard(affected_slug)
                            _cancel_reason = _budget_cancel_reason(_sprint_state)
                            _mark_story_budget_cancelled(_exc_result, reason=_cancel_reason)
                            _exc_outcome = StoryOutcome.SKIPPED
                            _log(f"SKIPPED {affected_slug} ({_cancel_reason})")
                        _sprint_state.results.append((spec_str, _exc_result))
                        _settle_terminal_story_audit(
                            affected_slug,
                            _ctx.slug_to_context[affected_slug][0],
                            _exc_result,
                            telemetry_snapshot=snapshot,
                        )
                        _set_outcome(
                            _sprint_state,
                            affected_slug,
                            _exc_outcome,
                            phase="ESCALATE",
                            last_phase=last_phase,
                            failure_cause=_exc_cause,
                            detail_updates={"gate_status": GATE_STATUS_INCOMPLETE},
                        )
                        _persist_current_story_result(
                            _sprint_state,
                            affected_slug,
                            _exc_result,
                            started_at=story_started_at,
                            finished_at=failed_at,
                        )
                        _sprint_state.dag.mark_skipped(affected_slug)
                    _sprint_state.budget.publish_live_status(
                        _recovered_cost.spent_including_in_flight
                    )
                    continue
                del _sprint_state.active[slug]
                story_deadlines.pop(slug, None)
                worker_budget.unregister_worker_budget(slug)
                story_wait_started.discard(slug)
                _sprint_state.stop_events.pop(slug, None)
                _sprint_state.story_times[slug] = (t0, t1)
                _sprint_state.latest_live_costs.pop(slug, None)

                _sprint_state.cost.record_story_cost(
                    slug,
                    result.state.total_cost,
                    measured=result.state.total_cost_measured,
                )
                # The landed figure replaces this story's provisional one, so the
                # live standing against the cap is republished from the ledger's
                # new state rather than left at what the story last reported.
                _sprint_state.budget.publish_live_status(
                    _sprint_state.cost.snapshot().spent_including_in_flight
                )

                spec_str = slug_to_spec[slug]
                _sprint_state.results.append((spec_str, result))

                spec_cost = result.state.total_cost_measured
                # A story returned for decomposition is not a failure and gets
                # neither mark: ✗ next to it would report a story that could not
                # be made to work (#2681).
                if _returned_for_decomposition(result):
                    icon = "⤺"
                else:
                    icon = "✓" if result.success else "✗"
                dur = _fmt_duration(elapsed)
                _log(
                    f"{icon} {slug}   {_fmt_cost_total(spec_cost, result.state.total_cost)}  {dur}"
                )

                # Auth circuit breaker (#1952): the launch gate proves the
                # credential was usable at t=0, but an interactive sign-in can
                # revoke it mid-sprint. The first fatal credential rejection is
                # the whole answer for every remaining story and phase — stop
                # here instead of paying to re-learn it per invocation.
                if auth_circuit is None:
                    _auth_cause = _fatal_auth_cause(result)
                    if _auth_cause is not None:
                        auth_circuit = _auth_cause
                        _auth_detail = str(_auth_cause.get("detail") or "").strip()
                        _auth_phase = str(_auth_cause.get("phase") or "unknown phase")
                        auth_circuit_reason = (
                            "agent credential rejected during "
                            f"{_auth_phase} of {slug}"
                            + (f": {_auth_detail[:200]}" if _auth_detail else "")
                        )
                        _sprint_state.stop.stop_if_unset(
                            f"Agent authentication failed ({auth_circuit_reason}); "
                            "remaining stories skipped — every subsequent call would "
                            "present the same rejected credential"
                        )
                        _log(f"HALT sprint: {_sprint_state.stop.reason}")
                        # Stop in-flight workers at their next phase boundary and
                        # release any plan gate they are parked on, so the sprint
                        # ends in seconds rather than at the worker timeout.
                        # Remember which slugs WE cancelled: their results come
                        # back through the timeout-oriented cancellation path,
                        # which would otherwise hand them a story failure verdict
                        # for a substrate outage (#1951).
                        for _pending_slug, _pending_evt in _sprint_state.stop_events.items():
                            auth_cancelled_slugs.add(_pending_slug)
                            _pending_evt.set()
                        for _gate_slug, _pending_gate in _sprint_state.plan_gates.items():
                            _log(f"Releasing plan gate for {_gate_slug} (auth abort)")
                            _pending_gate.set()
                        _sprint_state.plan_gates.clear()

                # A sibling story we cancelled to stop the bleeding never got a
                # model judgment — the sprint killed it mid-flight. Recording the
                # generic cancellation as FAILED would present the substrate
                # outage as a property of that story, which is precisely the
                # conflation #1951 exists to prevent. Attribute it to the
                # credential and record it as skipped, not judged.
                if slug in auth_cancelled_slugs and not result.success:
                    auth_cancelled_slugs.discard(slug)
                    _cancel_reason = f"cancelled mid-flight: {auth_circuit_reason}"
                    _mark_story_auth_cancelled(result, auth_circuit, reason=_cancel_reason)
                    _end_collision_claim(
                        _sprint_state, slug, "cancelled by the auth circuit breaker"
                    )
                    _log(f"SKIPPED {slug} ({_cancel_reason})")
                    _record_current_story_entry(slug, "SKIPPED", error=_cancel_reason)
                    _set_outcome(_sprint_state, slug, StoryOutcome.SKIPPED, reason=_cancel_reason)
                    if _sprint_state.state_writer is not None:
                        _sprint_state.state_writer.update(slug, status="skipped")
                    _sprint_state.dag.mark_skipped(slug)
                    _settle_terminal_story_audit(slug, task, result)
                    _print_worker_status(
                        _sprint_state.active, worker_phases, _sprint_state.dag, total
                    )
                    continue

                # A story the sprint cancelled because its cap was reached
                # (#2547). Same reasoning as the auth cancellation above: the
                # sprint stopped it, nothing judged it, so it is skipped rather
                # than failed — and the reason names the budget so an operator
                # reading the run afterwards sees a spending decision instead of
                # a story that could not be made to work.
                if slug in _sprint_state.budget_cancelled_slugs and not result.success:
                    _sprint_state.budget_cancelled_slugs.discard(slug)
                    _cancel_reason = _budget_cancel_reason(_sprint_state)
                    _mark_story_budget_cancelled(result, reason=_cancel_reason)
                    _end_collision_claim(_sprint_state, slug, "cancelled by the sprint budget")
                    _log(f"SKIPPED {slug} ({_cancel_reason})")
                    _record_current_story_entry(slug, "SKIPPED", error=_cancel_reason)
                    _set_outcome(_sprint_state, slug, StoryOutcome.SKIPPED, reason=_cancel_reason)
                    if _sprint_state.state_writer is not None:
                        _sprint_state.state_writer.update(slug, status="skipped")
                    _sprint_state.dag.mark_skipped(slug)
                    _settle_terminal_story_audit(slug, task, result)
                    _print_worker_status(
                        _sprint_state.active, worker_phases, _sprint_state.dag, total
                    )
                    continue

                # Stood down at its collision gate (#2234): the scheduler opened
                # the gate to release the worker, not to admit it to DEV. The
                # story reached PLAN and stopped, so it is skipped — not failed:
                # nothing judged it, and its planned work is still valid once the
                # preserved work it collided with is decided.
                if slug in _sprint_state.gate_stood_down:
                    _stand_down_reason = _sprint_state.gate_stood_down.pop(slug)
                    _end_collision_claim(_sprint_state, slug, "stood down before entering DEV")
                    _log(f"SKIPPED {slug} ({_stand_down_reason})")
                    _record_current_story_entry(slug, "SKIPPED", error=_stand_down_reason)
                    _set_outcome(
                        _sprint_state,
                        slug,
                        StoryOutcome.SKIPPED,
                        reason=_stand_down_reason,
                        phase=result.phase.name,
                    )
                    if _sprint_state.state_writer is not None:
                        _sprint_state.state_writer.update(slug, status="skipped")
                    _sprint_state.dag.mark_skipped(slug)
                    _settle_terminal_story_audit(slug, task, result)
                    _print_worker_status(
                        _sprint_state.active, worker_phases, _sprint_state.dag, total
                    )
                    continue

                _done_status = (
                    "done"
                    if (result.success or result.state.preflight_verdict == "ALREADY_DONE")
                    else "failed"
                )
                if (
                    task.story_path is None
                    and task.github_issue is not None
                    and result.phase == Phase.PREFLIGHT
                ):
                    _done_status = "waiting"
                # Live status update — does not commit a terminal outcome to
                # the canonical structure when the slug is still preflighting.
                _project_cost = _canonical_cost_projector(_sprint_state, task.slug)
                if _sprint_state.state_writer is not None and _done_status == "waiting":
                    _sprint_state.state_writer.update(
                        slug,
                        status=_done_status,
                        phase=result.phase.name,
                        cost_usd=_project_cost(_story_reported_cost(result.state)),
                    )

                _classify_outcome = _classify_and_record(
                    task,
                    result,
                    _sprint_state.dag,
                    _sprint_state.merged_slugs,
                    story_state=_sprint_state.stories,
                    cost_projection=_project_cost,
                )
                _terminal_model = _terminal_story_model(result)
                _outcome_fields: dict[str, object] = {
                    "phase": result.phase.name,
                    "cost_usd": _story_reported_cost(result.state),
                }
                if _classify_outcome == StoryOutcome.DECOMPOSED:
                    # Say what happened on the row itself. Without a reason the
                    # story reads as an unexplained non-completion, which is the
                    # misreport this outcome exists to prevent (#2681).
                    _outcome_fields["reason"] = DECOMPOSED_STORY_REASON
                    _log(f"⤺ {slug} {DECOMPOSED_STORY_REASON} ({result.message})")
                if _terminal_model is not None:
                    _outcome_fields["current_model"] = _terminal_model
                # Tag preflight-verdict ALREADY_DONE outcomes so renderers can
                # distinguish them from the resume-skip-merged classification —
                # the two paths have different trust properties and operators
                # must not have to cross-reference GitHub state to tell them
                # apart.
                if (
                    _classify_outcome == StoryOutcome.ALREADY_DONE
                    and result.state.preflight_verdict == "ALREADY_DONE"
                ):
                    _existing_entry = _sprint_state.stories.get(task.slug)
                    _existing_detail = (
                        dict(_existing_entry.detail) if _existing_entry is not None else {}
                    )
                    _existing_detail["outcome_source"] = "preflight_verdict"
                    _outcome_fields["detail"] = _existing_detail
                _set_outcome(_sprint_state, task.slug, _classify_outcome, **_outcome_fields)
                _persist_current_story_result(
                    _sprint_state,
                    slug,
                    result,
                    started_at=t0,
                    finished_at=t1,
                )

                # Dependent stories in parallel mode need scheduler-side local merge
                # even when on_approve is "none" and auto_merge is False.
                if (
                    result.success
                    and result.landing_status is None
                    and max_parallel > 1
                    and slug in dependent_slugs
                ):
                    result.landing_status = "pending_integration"
                    result.merge = {**(result.merge or {}), "action": "merge", "pending": True}

                # The scheduler thread is the sole owner of DAG/landing state.
                # Workers set landing_status="pending_integration" and return;
                # _attempt_integration is the sole merge site for all sprint execution.
                if result.success and result.landing_status == "pending_integration":
                    integrated = _attempt_integration(_sprint_state, slug, task, result)
                    if not integrated:
                        pending_integration[slug] = (task, result)
                    elif result.landing_status == "failed":
                        # Optimistic classify recorded this as DONE; landing
                        # failed — correct the canonical outcome (terminal-to-
                        # terminal correction is permitted).
                        _merge_info = result.merge if isinstance(result.merge, dict) else {}
                        _failed_outcome = landing_failure_outcome(_merge_info)
                        _set_outcome(_sprint_state, slug, _failed_outcome, phase=result.phase.name)
                    changed = True
                    while changed:
                        changed = False
                        for pending_slug, (pending_task, pending_result) in list(
                            pending_integration.items()
                        ):
                            if _attempt_integration(
                                _sprint_state, pending_slug, pending_task, pending_result
                            ):
                                del pending_integration[pending_slug]
                                if pending_result.landing_status == "failed":
                                    _pending_merge_info = (
                                        pending_result.merge
                                        if isinstance(pending_result.merge, dict)
                                        else {}
                                    )
                                    _pending_failed_outcome = landing_failure_outcome(
                                        _pending_merge_info
                                    )
                                    _set_outcome(
                                        _sprint_state,
                                        pending_slug,
                                        _pending_failed_outcome,
                                        phase=pending_result.phase.name,
                                    )
                                changed = True
                else:
                    # No integration for this story — a refusal, an escalation,
                    # a skip. Its record is the sprint's own dirt in the project
                    # root until it is published, and nothing else on this path
                    # will publish it while a sibling is in flight (#2755).
                    _settle_terminal_story_audit(slug, task, result)

                # The landing verdict — not the worker exiting — is what ends a
                # collision claim (#2234). Re-check every live claim, not just
                # this story's: the integration drain above can turn a sibling's
                # pending_integration into landed or merge-failed.
                if use_plan_gates:
                    _reconcile_collision_claim(slug, result)
                    _refresh_collision_claims()

                # Batch groups land as one branch (#727) — the leader's. Register
                # each member so a leader landing that resolves later (queued PR,
                # wrap-up) can still correct it, and apply the leader's verdict
                # immediately when it is already known.
                _group_id = getattr(result.state, "preflight_batch_group", None)
                _group_leader = _dispatched_batch_leader.get(_group_id) if _group_id else None
                if _group_id and _group_leader is not None and slug != _group_leader:
                    _sprint_state.batch_member_records[slug] = (_group_id, task, result)
                    _known_landing = _sprint_state.batch_leader_landing.get(_group_id)
                    if _known_landing is not None:
                        _apply_batch_landing_to_member(
                            _sprint_state,
                            slug,
                            task,
                            result,
                            _known_landing,
                            _group_leader,
                            _group_id,
                        )

                # Fire StorySource lifecycle callbacks
                ctx = _ctx.slug_to_context.get(slug)
                if ctx:
                    _ctx_task, source, _ctx_ref = ctx
                    if result.success:
                        try:
                            source.on_complete(task, result, _ctx.config)
                        except Exception as exc:
                            _log(f"WARN on_complete callback failed for {slug}: {exc}")
                    elif result.phase == Phase.ESCALATE:
                        # An infrastructure abort is not an escalation (#1951):
                        # no agent judged the story, so there is nothing to
                        # report back to the story source about it. Firing
                        # on_escalate would post a story-quality verdict — the
                        # durable, externally visible kind — sourced from a dead
                        # credential.
                        if getattr(result, "infrastructure_failure", False):
                            _log(
                                f"INFO {slug}: infrastructure abort (no agent judgment) — "
                                "skipping on_escalate; run contributes no story signal"
                            )
                        else:
                            try:
                                source.on_escalate(task, result.state, _ctx.config)
                            except Exception as exc:
                                _log(f"WARN on_escalate callback failed for {slug}: {exc}")

                _print_worker_status(_sprint_state.active, worker_phases, _sprint_state.dag, total)

            # ── Overlap detection: check plan gates ────────────────────
            if use_plan_gates:
                _service_plan_gates(_sprint_state)

    if _sprint_state.queued_prs:
        for slug, (task, result, pr_url) in list(_sprint_state.queued_prs.items()):
            poll_result = _poll_queued_pr(
                pr_url,
                _ctx.config.project_root,
                _ctx.config.workspace.merge_wait_timeout_seconds,
                base_branch=_ctx.config.workspace.base_branch,
            )
            if poll_result["status"] == "merged":
                _sprint_state.merged_slugs.add(slug)
                _sprint_state.dag.mark_complete(slug)
                result.landing_status = "landed"
                _set_outcome(_sprint_state, slug, StoryOutcome.DONE, landed=True)
                _resolve_batch_leader_landing(_sprint_state, slug, "landed", carrier=result.merge)
            else:
                from ..coordinator.completion import (  # noqa: PLC0415
                    mark_merge_failed as _mark_mf,
                )

                _err = _queued_pr_failure_message(
                    poll_result, pr_url, _ctx.config.workspace.merge_wait_timeout_seconds
                )
                _mark_mf(result.state, result, _err, result.state.branch_name)
                _set_outcome(
                    _sprint_state, slug, StoryOutcome.MERGE_FAILED, phase=result.phase.name
                )
                _resolve_batch_leader_landing(_sprint_state, slug, "failed", carrier=result.merge)
                _log(f"✗ {slug}: queued PR {poll_result['status']} during sprint wrap-up")
            _persist_story_landing(_sprint_state, slug, result)
            # The queued PR resolved while this sprint was still running, so
            # this process is the observer. A PR that resolves *after* exit is
            # closed out by ``reconcile_landing_evidence`` instead.
            _record_landing_evidence(
                _sprint_state,
                slug,
                result,
                landing_mode="merge-pr",
                observer=LANDING_OBSERVER_QUEUED_PR,
                attempt_outcome=_QUEUED_PR_ATTEMPT_OUTCOME.get(poll_result["status"], "failed"),
            )
            _write_story_audit(_ctx.config, task, result, sprint_id=_ctx.sprint_id)
            del _sprint_state.queued_prs[slug]
            # The landing verdict is in: end the claim outright rather than
            # re-deriving it from the result, whose phase mark_merge_failed can
            # legitimately leave as ESCALATE (inherited-dev-residue).
            _end_collision_claim(
                _sprint_state, slug, f"queued PR {poll_result['status']} at wrap-up"
            )

    finished_at = datetime.datetime.now(datetime.timezone.utc)
    set_worker_slug("")
    # No story of this sprint can still be running, so no enclosing budget of it
    # is still live; leaving one registered would let a later sprint's worker
    # inherit a stale ceiling through the shared slug registry.
    worker_budget.clear_worker_budgets()
    # For the same reason, nothing this run declared itself executing is still in
    # flight. The scheduler clears each record as it settles the story; this
    # covers whatever the work loop exited around (a stop, a deadlock sweep, a
    # queued PR resolved at wrap-up) so no ownership claim outlives the run that
    # made it.
    for _owned_slug in sorted(_sprint_state.owned_story_executions):
        _release_story_execution(_sprint_state, _owned_slug)
    duration = (finished_at - started_at).total_seconds()

    # ── Terminalize live state ────────────────────────────────────────
    # The work loop is over, so nothing in this sprint can still advance. Write
    # that fact to the live .state file BEFORE summary/audit finalization: those
    # steps can take a while (and can raise), and until the terminal transition
    # lands, `forge status` keeps rendering a running sprint next to stories it
    # already reports as failed (#2013). The file is removed further down on the
    # normal path; this is what a crash or a stop in between now finds.
    if _sprint_state.state_writer is not None:
        _stranded = _sprint_state.state_writer.terminalize_stories(
            outcome=StoryOutcome.FAILED,
            reason=_sprint_state.stop.reason or "sprint ended before this story reached a verdict",
        )
        if _stranded:
            _log(
                "Terminalized "
                f"{len(_stranded)} story/stories still non-terminal at sprint end: "
                + ", ".join(_stranded)
            )
        _terminal_counts = _sprint_state.stories.counts()
        if _sprint_state.stop.stopped:
            _terminal_sprint_phase = SPRINT_PHASE_STOPPED
        elif _terminal_counts["failed"]:
            _terminal_sprint_phase = SPRINT_PHASE_FAILED
        else:
            _terminal_sprint_phase = SPRINT_PHASE_DONE
        _sprint_state.state_writer.set_phase(_terminal_sprint_phase)

    # Reconcile per-story attribution — intake remediation spend and pre-restart
    # carried spend — against what the canonical rows already hold, so the
    # per-story sums (used by sprint-summary.yaml) match the SprintResult total.
    #
    # Every live and terminal write now projects the same attribution onto the
    # row as it is written (#2922), because a stop never reaches this point and
    # money that only arrives here is money a stopped run loses. What is left for
    # this pass is the rows nothing ever wrote to: a story dropped at the intake
    # gate, or one whose spend the work loop never got to record. It adds only
    # the outstanding part, so an amount a live write already applied is not
    # applied a second time.
    def _bump_story_cost(slug: str, extra: float) -> None:
        if extra <= 0.0:
            return
        entry = _sprint_state.stories.get(slug)
        if entry is None:
            return
        if entry.cost_usd is None:
            # Unknown + known is still unknown: adding measured intake spend to a
            # cost-unknown story must not turn it into a confident figure (#1992).
            return
        _sprint_state.stories.transition(slug, cost_usd=entry.cost_usd + extra)

    _reconciled_by_slug: dict[str, float] = {}
    for _attr_slug in sorted(
        set(_sprint_state.story_cost_adjustments) | set(_sprint_state.carried_prior_story_cost)
    ):
        _outstanding = round(
            _story_attribution_usd(_sprint_state, _attr_slug)
            - _sprint_state.applied_story_attribution.get(_attr_slug, 0.0),
            6,
        )
        if _outstanding <= 0.0:
            continue
        _bump_story_cost(_attr_slug, _outstanding)
        _sprint_state.applied_story_attribution[_attr_slug] = _story_attribution_usd(
            _sprint_state, _attr_slug
        )
        _reconciled_by_slug[_attr_slug] = _outstanding
    if _reconciled_by_slug:
        _reconciled_total = sum(_reconciled_by_slug.values())
        _reconciled_detail = ", ".join(
            f"{s}=${c:.4f}" for s, c in sorted(_reconciled_by_slug.items())
        )
        _log(
            f"Re-attached unprojected spend to {len(_reconciled_by_slug)} story/stories: "
            f"${_reconciled_total:.4f} ({_reconciled_detail})"
        )

    _final_cost = _sprint_state.cost.snapshot()
    final_cost = _final_cost.spent
    # A sprint total is only a total when every story's cost was measured. When
    # any story ran on a transport that reported no cost, ``final_cost`` is a
    # measured lower bound and every surface must say so rather than present it
    # as the sprint's cost (#1992). Intake remediation spends the same budget
    # outside any story's CoordinatorState, so its unmeasured passes count too.
    _cost_complete = _final_cost.measured and all(
        e.cost_usd is not None for e in _sprint_state.stories.stories()
    )
    # An acceptance resolves the BUDGET question, never the measurement one:
    # ``_cost_complete`` above still reads the raw source list, so an accepted
    # source keeps the sprint total reported as a lower bound. What acceptance
    # changes is which figure the cap was verified against (#2310).
    _budget_verification = _sprint_state.budget.verification(_final_cost)
    # Banner, summary, notifications, and SprintResult all project from the
    # same canonical structure — by construction they cannot disagree.
    _canonical_counts = _sprint_state.stories.counts()
    specs_succeeded = _canonical_counts["succeeded"]
    specs_failed = _canonical_counts["failed"]
    specs_skipped = _canonical_counts["skipped"]
    # Canonical total: include canonical-only stories (shape-gate skips,
    # closed-at-fetch ALREADY_DONE, etc.) so SprintResult/banner/summary
    # /notifications all report the same total.
    canonical_total = _canonical_counts["total"] or total
    sprint_result = SprintResult(
        name=_ctx.resolved.name,
        specs_total=canonical_total,
        specs_succeeded=specs_succeeded,
        specs_failed=specs_failed,
        specs_skipped=specs_skipped,
        total_cost_usd=final_cost,
        budget_usd=_ctx.resolved.budget_usd,
        cost_complete=_cost_complete,
        unmeasured_spend_sources=_final_cost.unmeasured,
        unresolved_unmeasured_spend_sources=_budget_verification.unresolved_sources,
        accepted_unmeasured_spend=tuple(r.as_dict() for r in _budget_verification.accepted),
        budget_verification_spend_usd=_budget_verification.verification_spend_usd,
        # Intake spend on issues this sprint never scheduled: real money in the
        # total that no per-story row was ever going to carry. Declared so the
        # audit's cost cross-check does not read it as unaccounted-for (#2847).
        # Spend on issues that DID get a row is excluded — the runner attributes
        # that to the row itself through ``story_cost_adjustments``.
        non_story_spend_usd=_sprint_state.cost.non_story_spend(
            frozenset(e.slug for e in _sprint_state.stories.stories())
        ),
        # What this run told operators it had spent while it was running. The
        # cross-check below compares the rows against it as well as against the
        # ledger, so a generation that lost part of its own accounting reports a
        # discrepancy rather than a settled lower total (#2922).
        recorded_spend_high_water_usd=(
            _sprint_state.state_writer.recorded_spend_usd() or 0.0
            if _sprint_state.state_writer is not None
            else 0.0
        ),
        results=_sprint_state.results,
        stopped_reason=_sprint_state.stop.reason,
    )

    _sprint_elapsed = (datetime.datetime.now(datetime.timezone.utc) - started_at).total_seconds()
    _sprint_dur = _fmt_duration(_sprint_elapsed)
    _sprint_cost_str = _fmt_cost_total(final_cost if _cost_complete else None, final_cost)
    _log(
        f"Sprint complete: {specs_succeeded} succeeded, {specs_failed} failed, "
        f"{specs_skipped} skipped. "
        f"Total: {_sprint_cost_str}"
        f"  {_sprint_dur}"
    )
    _sprint_outcome = "done" if specs_failed == 0 and not _sprint_state.stop.stopped else "partial"
    _sprint_logger.emit(
        "run_end",
        outcome=_sprint_outcome,
        total_cost_usd=round(final_cost, 6) if _cost_complete else None,
        total_cost_measured_usd=round(final_cost, 6),
        total_duration_s=round(_sprint_elapsed, 2),
    )
    if _ctx.notify:
        # Notifications project from canonical counts/total so every
        # operator surface reports the same numbers by construction.
        if _ctx.config.notifications.backend != "none":
            _notify(
                f"TheForge: {_ctx.resolved.name}",
                (
                    f"✓ {specs_succeeded} passed, ✗ {specs_failed} failed, "
                    f"⊘ {specs_skipped} skipped"
                ),
            )
        if _ctx.config.notifications.ntfy is not None:
            _ntfy_title = f'TheForge: sprint done \u2014 "{_ctx.resolved.name}"'
            _ntfy_body_lines = [
                (
                    f"{canonical_total} stories: {specs_succeeded} succeeded "
                    f"\u00b7 {specs_failed} failed \u00b7 {specs_skipped} skipped"
                ),
                f"Total cost: {_sprint_cost_str}   Duration: {_sprint_dur}",
            ]
            if _sprint_state.stop.reason:
                _ntfy_body_lines.append(f"Stopped: {_sprint_state.stop.reason}")
            _ntfy_publish(
                _ctx.config.notifications.ntfy.url,
                _ntfy_title,
                "\n".join(_ntfy_body_lines),
                priority=_ctx.config.notifications.ntfy.priority,
            )
        if _ctx.config.notifications.backend not in ("ntfy", "none"):
            from ..notify_backends import send_notifications

            _sc_title = f'TheForge sprint complete \u2014 "{_ctx.resolved.name}"'
            _sc_body_lines = [
                (
                    f"{canonical_total} stories: {specs_succeeded} succeeded "
                    f"\u00b7 {specs_failed} failed \u00b7 {specs_skipped} skipped"
                ),
                f"Total cost: {_sprint_cost_str}   Duration: {_fmt_duration(_sprint_elapsed)}",
            ]
            if _sprint_state.stop.reason:
                _sc_body_lines.append(f"Stopped: {_sprint_state.stop.reason}")
            send_notifications(_ctx.config, _sc_title, "\n".join(_sc_body_lines))

    # The terminal audit, summary and RCA — inputs and all — belong to
    # sprint.audit_publish (#2402); the runner hands over the state and the
    # facts only this call knows.
    write_terminal_sprint_audits(
        _sprint_state,
        result=sprint_result,
        started_at=started_at,
        finished_at=finished_at,
        duration=duration,
        sprint_log_dir=_sprint_log_dir,
        dropped_slugs=_dropped_slugs,
        triages=triages,
    )

    if _sprint_state.state_writer is not None:
        _sprint_state.state_writer.remove()

    # Runs after _state_writer.remove() so sprint state is cleaned up regardless,
    # but the failure is NOT swallowed: a local-only audit commit contaminates
    # every later story PR cut from this checkout, so the sprint must exit
    # nonzero rather than report success over divergent base-branch state.
    publish_story_run_audits(_sprint_state, lands_locally=_sprint_lands_locally)

    # ── POST_SPRINT hook ──────────────────────────────────────────────
    if _ctx.config.hooks and _ctx.config.hooks.post_sprint:
        from ..coordinator.hooks import build_post_sprint_payload
        from ..coordinator.hooks import run_hook as _run_hook

        _stories = []
        for spec_str, res in _sprint_state.results:
            # Derive slug: use workspace_path leaf (set during WORKSPACE phase) or slug_map
            _ws = res.state.workspace_path
            if _ws is not None:
                _slug = _ws.name
            else:
                _slug = _ctx.slug_by_canonical_ref.get(spec_str, Path(spec_str).stem)
            _verdict = ""
            if res.state.review_results:
                _verdict = res.state.review_results[-1].verdict
            elif res.success:
                _verdict = "APPROVE"
            _stories.append(
                {
                    "slug": _slug,
                    "outcome": "done" if res.success else "escalate",
                    "verdict": _verdict,
                    "merged": res.merge is not None and res.merge.get("merged", False),
                }
            )
        _ps_payload = build_post_sprint_payload(
            sprint_name=_ctx.resolved.name,
            stories=_stories,
            run_id=_sprint_state.sprint_run_id,
            config=_ctx.config,
            total_cost_usd=final_cost if _cost_complete else None,
            duration_seconds=_sprint_elapsed,
        )
        _run_hook(
            _ctx.config.hooks.post_sprint,
            _ps_payload,
            _ctx.config.hooks.timeout_seconds,
            "post_sprint",
            _sprint_logger,
            secrets=_ctx.config.secrets,
        )

    # ── Opt-in post-sprint triage (#2231) ─────────────────────────────
    # Runs after the sprint's result is terminal and cannot change it: the pass
    # only proposes and persists a pending operator decision, and swallows its
    # own failures. The work itself belongs to sprint.post_sprint_triage.
    if _ctx.config.sprint.post_sprint_triage:
        from .post_sprint_triage import run_post_sprint_triage  # noqa: PLC0415

        run_post_sprint_triage(_sprint_state)

    return sprint_result
