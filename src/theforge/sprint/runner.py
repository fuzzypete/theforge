"""Sprint runner: parallel story scheduling and the run_sprint entry point."""

from __future__ import annotations

import datetime
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path

import yaml

from ..config import ForgeConfig
from ..config.auth import check_agent_auth
from ..coordinator import workspace as coordinator_workspace
from ..coordinator.agent_failure import (
    CATEGORY_AUTH,
    ERROR_TYPE_INFRASTRUCTURE_ABORT,
    AgentInvocationFailure,
    is_infrastructure_abort,
    mark_infrastructure_abort,
)
from ..coordinator.engine import run_from_dev, run_from_review, run_task
from ..coordinator.gate import run_gate_full
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
from ..task import TaskStory
from .audit import (
    _get_or_create_sprint_id,
    _write_sprint_audit,
    _write_sprint_summary,
    _write_story_audit,
    persist_accumulated_story_state,
    write_live_story_audit,
)
from .auth_gate import enforce_sprint_auth_readiness
from .budget import evaluate_budget
from .ci_checks import failing_required_pr_checks, poll_required_checks
from .collision import (
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
from .gate_timeout_resolver import resolve_effective_gate_timeout
from .launch_guard import (
    REASON_IN_FLIGHT,
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
from .query import NormalizedDependencyPlan, normalize_dependency_plan
from .sources import StorySource
from .state_writer import (
    SPRINT_PHASE_DONE,
    SPRINT_PHASE_FAILED,
    SPRINT_PHASE_STOPPED,
    SprintStateWriter,
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

# CLI transports whose spend forge cannot measure at all, warned about up front.
# `codex` left this set in #2019: `codex exec --json` reports a real token split
# per turn, so codex runs now carry measured cost like the claude transport.
# Actual per-run measurement failures are still caught downstream by the
# cost-is-None checks that feed `unmeasured_spend` — this set is only the
# pre-sprint warning, evaluated over config profiles before any run exists.
_UNTRACKED_COST_CLIS: frozenset[str] = frozenset({"gemini"})
_STORY_RUN_AUDIT_DIR = ".forge/audits/runs"
_STORY_RUN_AUDIT_COMMIT_CMD = (
    f'git commit -m "chore(audit): record sprint run audits" -- {_STORY_RUN_AUDIT_DIR}'
)

# Where the outcome of the publish step is recorded. Lives under .forge/ (which
# .gitignore denies wholesale), so writing it never dirties the base-branch
# checkout the sprint is publishing from.
_STORY_RUN_AUDIT_PUBLISH_STATE_PATH = ".forge/audit-publish-state.json"

# A push rejected because the base branch moved is reconciled and retried. Three
# attempts covers the sprint's own merges landing while the push is in flight;
# beyond that the remote is being advanced by something other than this run and
# looping longer just delays the operator's answer.
_STORY_RUN_AUDIT_PUSH_ATTEMPTS = 3

# Publish end states, recorded to ``_STORY_RUN_AUDIT_PUBLISH_STATE_PATH`` and
# carried on ``StoryRunAuditPublishError.state``. They exist so that an operator
# looking at local-only audit commits can tell *which* thing happened: the run
# never got here (no/stale record), it got here and the remote refused
# (``push_refused``), or publishing was deliberately skipped (``local_only``).
AUDIT_PUBLISH_CLEAN = "clean"
AUDIT_PUBLISH_COMMITTED = "committed_unpublished"
AUDIT_PUBLISH_PUBLISHED = "published"
AUDIT_PUBLISH_LOCAL_ONLY = "local_only"
AUDIT_PUBLISH_COMMIT_FAILED = "commit_failed"
AUDIT_PUBLISH_PUSH_REFUSED = "push_refused"
AUDIT_PUBLISH_RECONCILE_FAILED = "reconcile_failed"
AUDIT_PUBLISH_VERIFY_FAILED = "verify_failed"

run_agent = None
log_agent_result = None


def _log(msg: str) -> None:
    # Worker-slug prefixing (parallel attribution) is applied centrally by
    # ``_log_line``; do not prepend it here or it would double-tag.
    _log_line("[sprint]", msg)


class StoryRunAuditPublishError(RuntimeError):
    """Canonical story run audits could not be published to the base branch.

    ``state`` is one of the ``AUDIT_PUBLISH_*`` constants and names the end
    state the checkout was left in. It is a ``RuntimeError`` subclass because
    the sprint entry point already treats a publish failure as fatal; the extra
    attribute only makes the *kind* of failure legible to callers and tests.
    """

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state


def _record_audit_publish_state(
    project_root: Path,
    base_branch: str,
    state: str,
    detail: str | None = None,
) -> None:
    """Record the publish end state next to the checkout it describes.

    Best-effort: a sprint must not fail because this marker could not be
    written, and the marker must never mask the error it is describing.
    """
    path = project_root / _STORY_RUN_AUDIT_PUBLISH_STATE_PATH
    payload = {
        "state": state,
        "base_branch": base_branch,
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "detail": detail,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover — filesystem-level failure
        _log(f"Warning: could not record story run audit publish state: {exc}")


def _reconcile_base_with_origin(project_root: Path, base_branch: str) -> None:
    """Fetch origin and rebase the local base branch onto its current head.

    Raises ``StoryRunAuditPublishError`` when the reconcile itself cannot be
    completed, aborting any partial rebase so the checkout is left usable.
    """
    from ..coordinator import util as _cu  # noqa: PLC0415

    quoted_base = shlex.quote(base_branch)
    ok_fetch, fetch_out = _cu._run_shell(f"git fetch origin {quoted_base}", project_root)
    if not ok_fetch:
        raise StoryRunAuditPublishError(
            f"Failed to fetch origin/{base_branch} while publishing story run audits: "
            f"{fetch_out.strip()}",
            state=AUDIT_PUBLISH_RECONCILE_FAILED,
        )

    ok_rebase, rebase_out = _cu._run_shell(
        f"git rebase origin/{quoted_base}",
        project_root,
    )
    if not ok_rebase:
        _cu._run_shell("git rebase --abort", project_root)
        raise StoryRunAuditPublishError(
            f"Failed to rebase '{base_branch}' onto origin/{base_branch} while publishing "
            f"story run audits: {rebase_out.strip()}",
            state=AUDIT_PUBLISH_RECONCILE_FAILED,
        )


def _commit_story_run_audits(project_root: Path, base_branch: str, *, publish: bool) -> None:
    """Commit and publish canonical per-run audit JSON emitted during a sprint.

    The sprint writes these records to the project-root base-branch checkout on
    the operator's behalf. A commit that is never pushed is unowned state: later
    story worktrees are cut from that checkout and GitHub attributes the audit
    JSON to whichever story happens to be running. So the commit is only half
    the operation — this pushes it to origin and verifies the base branch is no
    longer ahead, raising loudly if either step fails.

    ``publish`` comes from ``_base_branch_tracks_origin``: it is false only when
    this run lands stories by merging into the local base checkout *and* has
    opted out of pushing them. Pushing a branch publishes all of its ancestors,
    so a push here would then also publish those local merges. In that one
    configuration the commit stays local and the fact is warned about instead.

    A rejected push is not a failure of this step so much as a statement about
    where the remote is: the sprint's own merges are what usually advance the
    base branch, so a run that landed stories is the *normal* case for the local
    branch being behind. So a rejection is reconciled (fetch + rebase onto the
    current remote head) and retried, and only exhausting that raises. Whichever
    end state the checkout lands in is recorded to
    ``.forge/audit-publish-state.json`` so it survives to where the state is
    observed.
    """
    from ..coordinator import util as _cu  # noqa: PLC0415

    if not (project_root / ".git").exists():
        return

    audit_dir = Path(_STORY_RUN_AUDIT_DIR)
    quoted_audit_dir = shlex.quote(audit_dir.as_posix())
    ok_status, status_out = _cu._run_shell(
        f"git status --porcelain -- {quoted_audit_dir}",
        project_root,
    )
    if not ok_status:
        raise StoryRunAuditPublishError(
            f"Failed to inspect story run audits: {status_out}",
            state=AUDIT_PUBLISH_COMMIT_FAILED,
        )
    if not status_out.strip():
        # Nothing pending. Record it so a marker from an earlier run cannot be
        # mistaken for this one's outcome.
        _record_audit_publish_state(project_root, base_branch, AUDIT_PUBLISH_CLEAN)
        return

    ok_add, add_out = _cu._run_shell(f"git add -- {quoted_audit_dir}", project_root)
    if not ok_add:
        raise StoryRunAuditPublishError(
            f"Failed to stage story run audits: {add_out}",
            state=AUDIT_PUBLISH_COMMIT_FAILED,
        )

    ok_commit, commit_out = _cu._run_shell(_STORY_RUN_AUDIT_COMMIT_CMD, project_root)
    if not ok_commit:
        raise StoryRunAuditPublishError(
            f"Failed to commit story run audits: {commit_out}",
            state=AUDIT_PUBLISH_COMMIT_FAILED,
        )
    _log("Committed canonical story run audit records to the base branch checkout.")
    # Written before the push so that a crash mid-publish is distinguishable
    # from a run that never reached this function at all.
    _record_audit_publish_state(project_root, base_branch, AUDIT_PUBLISH_COMMITTED)

    if not publish:
        _record_audit_publish_state(
            project_root,
            base_branch,
            AUDIT_PUBLISH_LOCAL_ONLY,
            detail="workspace.auto_push is off for a run that lands stories locally",
        )
        _log(
            f"⚠ SPRINT  story run audit records remain local: this run merges stories into "
            f"'{base_branch}' with workspace.auto_push off, so pushing would also publish those "
            f"local merges. Push '{base_branch}' yourself before any workflow that diffs a story "
            f"branch against origin/{base_branch}."
        )
        return

    quoted_base = shlex.quote(base_branch)
    push_out = ""
    for attempt in range(1, _STORY_RUN_AUDIT_PUSH_ATTEMPTS + 1):
        ok_push, push_out = _cu._run_shell(
            f"git push origin {quoted_base}",
            project_root,
        )
        if ok_push:
            break
        if attempt == _STORY_RUN_AUDIT_PUSH_ATTEMPTS:
            _record_audit_publish_state(
                project_root,
                base_branch,
                AUDIT_PUBLISH_PUSH_REFUSED,
                detail=push_out.strip(),
            )
            raise StoryRunAuditPublishError(
                f"Failed to push story run audits to origin/{base_branch} after "
                f"{_STORY_RUN_AUDIT_PUSH_ATTEMPTS} attempts (fetch + rebase between "
                f"attempts): {push_out.strip()}",
                state=AUDIT_PUBLISH_PUSH_REFUSED,
            )
        _log(
            f"Push of story run audits to origin/{base_branch} was refused "
            f"(attempt {attempt}/{_STORY_RUN_AUDIT_PUSH_ATTEMPTS}); reconciling with the "
            f"current remote head and retrying."
        )
        try:
            _reconcile_base_with_origin(project_root, base_branch)
        except StoryRunAuditPublishError as exc:
            _record_audit_publish_state(
                project_root,
                base_branch,
                exc.state,
                detail=str(exc),
            )
            raise

    ok_ahead, ahead_out = _cu._run_shell(
        f"git rev-list --count origin/{quoted_base}..{quoted_base}",
        project_root,
    )
    if not ok_ahead:
        message = (
            f"Failed to verify story run audits reached origin/{base_branch}: {ahead_out.strip()}"
        )
        _record_audit_publish_state(
            project_root, base_branch, AUDIT_PUBLISH_VERIFY_FAILED, detail=message
        )
        raise StoryRunAuditPublishError(message, state=AUDIT_PUBLISH_VERIFY_FAILED)
    try:
        ahead = int(ahead_out.strip())
    except ValueError:
        message = (
            f"Failed to verify story run audits reached origin/{base_branch}: "
            f"unexpected rev-list output {ahead_out.strip()!r}"
        )
        _record_audit_publish_state(
            project_root, base_branch, AUDIT_PUBLISH_VERIFY_FAILED, detail=message
        )
        raise StoryRunAuditPublishError(message, state=AUDIT_PUBLISH_VERIFY_FAILED) from None
    if ahead > 0:
        message = (
            f"Story run audits were committed but '{base_branch}' is still {ahead} commit(s) "
            f"ahead of origin/{base_branch} after push. Publish or reset it before rerunning."
        )
        _record_audit_publish_state(
            project_root, base_branch, AUDIT_PUBLISH_VERIFY_FAILED, detail=message
        )
        raise StoryRunAuditPublishError(message, state=AUDIT_PUBLISH_VERIFY_FAILED)
    _record_audit_publish_state(project_root, base_branch, AUDIT_PUBLISH_PUBLISHED)
    _log(f"Pushed canonical story run audit records to origin/{base_branch}.")


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
            phase="preflight",
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


def _optional_cost(raw: object) -> float | None:
    """Coerce a persisted ``cost_usd`` to float, preserving an unmeasured ``None``.

    ``None`` records "the transport could not measure this spend", which is a
    different fact from ``0.0``. Numeric arithmetic (budget checks, carry-forward
    sums) may treat unknown as a zero-valued lower bound, but anything that
    *reports* a story's cost must keep the distinction (#1992).
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return None


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
    if not sprint_id or not os.environ.get("FORGE_PREV_RUN_ID"):
        return 0.0
    audit_path = project_root / ".forge" / "audits" / "sprint-audit.yaml"
    if not audit_path.exists():
        return 0.0
    try:
        with open(audit_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        sprint_block = data.get("sprint", {})
        if sprint_block.get("sprint_id") != sprint_id:
            return 0.0
        total = sprint_block.get("total_cost_usd")
        if total is None:
            total = sprint_block.get("total_cost_measured_usd", 0.0)
        return float(total or 0.0)
    except (OSError, ValueError, TypeError):
        return 0.0


def _prior_sprint_cost_incomplete(project_root: Path, sprint_id: str | None) -> bool:
    """Return True when the prior generation recorded an incomplete sprint cost.

    A carried total from a generation that could not measure all of its spend is
    itself a lower bound; the budget check must know that before enforcing a cap
    against it (#1992). Absent/unreadable records report False — the pre-#1992
    shape simply carries no completeness claim.
    """
    if not sprint_id:
        return False
    audit_path = project_root / ".forge" / "audits" / "sprint-audit.yaml"
    if not audit_path.exists():
        return False
    try:
        with open(audit_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        sprint_block = data.get("sprint", {})
        if not isinstance(sprint_block, dict):
            return False
        if sprint_block.get("sprint_id") != sprint_id:
            return False
        return sprint_block.get("cost_complete") is False
    except (OSError, yaml.YAMLError, AttributeError):
        return False


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
    if not sprint_id:
        return 0.0, None, {}

    from .audit import _load_accumulated_stories  # noqa: PLC0415

    recovered_entries: dict[str, dict] = {}
    recovered_cost = 0.0
    earliest_started_at: datetime.datetime | None = None
    for raw_entry in _load_accumulated_stories(sprint_id, project_root):
        if not isinstance(raw_entry, dict):
            continue
        canonical_ref = raw_entry.get("canonical_ref")
        if not isinstance(canonical_ref, str) or not canonical_ref:
            continue
        entry = dict(raw_entry)
        recovered_entries[canonical_ref] = entry
        try:
            recovered_cost += float(entry.get("cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
        started_at = _parse_accumulated_story_timestamp(entry.get("started_at"))
        if started_at is not None and (
            earliest_started_at is None or started_at < earliest_started_at
        ):
            earliest_started_at = started_at

    if recovered_entries:
        return round(recovered_cost, 4), earliest_started_at, recovered_entries

    if os.environ.get("FORGE_PREV_RUN_ID"):
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


def _run_baseline_gate(config: ForgeConfig, resolved: ResolvedSprint) -> dict[str, object]:
    """Run the configured gate on the sprint merge base before any agent work starts."""

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
            "command": config.validation.gate_command,
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
            "command": config.validation.gate_command,
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
            "command": config.validation.gate_command,
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
            "command": config.validation.gate_command,
            "message": (
                "Broken baseline: sprint baseline gate requires running from the root checkout; "
                "current workspace is not the project toplevel"
            ),
        }

    forge_temp_root = config.project_root / ".forge"
    forge_temp_root.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="forge-baseline-", dir=forge_temp_root))
    baseline_worktree = temp_root / "worktree"
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
                "command": config.validation.gate_command,
                "message": (
                    "Broken baseline: unable to create temporary worktree for merge base "
                    f"{merge_base_ref}: {stderr or 'git worktree add failed'}"
                ),
            }

        if config.workspace.setup_command:
            _log(f"Running baseline workspace setup: {config.workspace.setup_command}")
            setup_ok, setup_out = coordinator_workspace._run_setup_split(
                config.workspace.setup_command,
                baseline_worktree,
                config.workspace.python_interpreter,
            )
            if not setup_ok:
                duration = time.monotonic() - started_monotonic
                return {
                    "status": "error",
                    "passed": False,
                    "exit_code": 1,
                    "duration_seconds": round(duration, 2),
                    "started_at": baseline_started_at,
                    "finished_at": datetime.datetime.now(datetime.timezone.utc),
                    "merge_base": merge_base_ref,
                    "command": config.validation.gate_command,
                    "message": (
                        "Broken baseline: workspace setup command failed in the temporary "
                        f"baseline worktree for merge base {merge_base_ref}: {setup_out}"
                    ),
                }

        decision, error, output_tail, resolved_gate_cmd, gate_exit_code = run_gate_full(
            config,
            baseline_worktree,
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
                "command": resolved_gate_cmd,
                "decision": decision,
                "output_tail": output_tail,
                "message": (
                    "Baseline gate passed on sprint merge base "
                    f"{merge_base_ref} before dev iterations started"
                ),
            }

        message = (
            "Broken baseline: configured gate failed on sprint merge base "
            f"{merge_base_ref} before any dev work started ({error or 'Gate returned FAIL'})"
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
        return {
            "status": "fail",
            "passed": False,
            "exit_code": exit_code,
            "duration_seconds": round(duration, 2),
            "started_at": baseline_started_at,
            "finished_at": finished_at,
            "merge_base": merge_base_ref,
            "command": resolved_gate_cmd,
            "decision": decision,
            "output_tail": output_tail,
            "message": message,
        }
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(baseline_worktree)],
            cwd=config.project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(temp_root, ignore_errors=True)


def _continuation_evidence(
    *,
    reexec: bool,
    live_story_slugs: set[str],
) -> str | None:
    """Describe why this launch is a continuation of in-flight work, or None.

    A re-exec is not, on its own, evidence that work is in flight: the source
    change can be observed by the sprint's *own* first pull, before any story has
    started, and such a launch is still a genuine start. What distinguishes a
    continuation is observed live work — agent process groups this same pid
    spawned before the re-exec and that are still running. Startup-only checks
    are skipped on exactly that evidence, never on the re-exec flag alone, so a
    launch that has not started anything keeps its full startup sequence.

    Deliberately narrow: prior *recorded* outcomes are not used as evidence,
    because a sprint id is stable across separate invocations of the same sprint
    and would make an unrelated later run skip its baseline gate.
    """
    if not reexec or not live_story_slugs:
        return None
    return (
        "agent process groups still running for "
        f"{', '.join(sorted(live_story_slugs))} after the re-exec"
    )


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
        "command": config.validation.gate_command,
        "skip_reason": "reexec_continuation",
        "skip_evidence": evidence,
        "message": (
            "Baseline gate skipped: this process is continuing an in-flight sprint "
            f"after a mid-run re-exec ({evidence}); the gate's precondition — no dev "
            "work started — no longer holds"
        ),
    }


def _agent_cost_tracking_warnings(config: ForgeConfig) -> list[str]:
    """Return sprint-start warnings for configured CLI agents with unknown cost."""

    agents: list[tuple[str, str | None, str | None, str, object | None]] = [
        (
            config.preflight_profile.name,
            config.preflight_profile.cli,
            config.preflight_profile.provider,
            config.preflight_profile.model,
            config.preflight_profile.api_fallback,
        ),
        (
            config.dev_profile.name,
            config.dev_profile.cli,
            config.dev_profile.provider,
            config.dev_profile.model,
            config.dev_profile.api_fallback,
        ),
    ]

    if config.plan.enabled:
        agents.append(
            (
                "planner",
                config.plan.cli,
                config.plan.provider,
                config.plan.model,
                config.plan.api_fallback,
            )
        )

    if config.plan_agent_review.enabled:
        agents.extend(
            (profile.name, profile.cli, profile.provider, profile.model, profile.api_fallback)
            for profile in config.plan_agent_review.profiles
        )

    agents.extend(
        (profile.name, profile.cli, profile.provider, profile.model, profile.api_fallback)
        for profile in config.review_pool
    )

    if config.synthesis_profile is not None:
        agents.append(
            (
                config.synthesis_profile.name,
                config.synthesis_profile.cli,
                config.synthesis_profile.provider,
                config.synthesis_profile.model,
                config.synthesis_profile.api_fallback,
            )
        )

    warnings: list[str] = []
    seen: set[tuple[str, str, str, str | None, str | None]] = set()
    for name, cli, provider, model, api_fallback in agents:
        if provider is not None or cli not in _UNTRACKED_COST_CLIS:
            continue
        fallback_provider = getattr(api_fallback, "provider", None)
        fallback_model = getattr(api_fallback, "model", None)
        key = (name, cli, model, fallback_provider, fallback_model)
        if key in seen:
            continue
        seen.add(key)
        if api_fallback is not None:
            warnings.append(
                f"⚠ CLI cost not tracked for {name} ({cli} CLI, {model}); API fallback to "
                f"{fallback_provider}/{fallback_model} will be tracked if it triggers."
            )
            continue
        warnings.append(
            f"⚠ Cost not tracked for {name} ({cli} CLI, {model}). "
            "Audit totals will exclude this agent's usage."
        )
    return warnings


def parse_manifest_slugs(config: "ForgeConfig", manifest_path: Path) -> list[str]:
    """Extract story slugs from a sprint manifest without full validation.

    Returns an empty list if the manifest cannot be parsed or has no stories.
    Used for pre-launch conflict detection — does not raise on invalid manifests.
    """
    try:
        with open(manifest_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            return []
        stories = raw.get("stories") or raw.get("specs") or []
        if not isinstance(stories, list):
            return []
        slugs: list[str] = []
        for entry in stories:
            if isinstance(entry, dict) and "issue" in entry:
                slugs.append(entry.get("slug", f"issue-{entry['issue']}"))
            elif isinstance(entry, str):
                story_path = (config.project_root / entry).resolve()
                if story_path.exists():
                    task = _build_task_from_story(story_path)
                    slugs.append(task.slug)
                else:
                    # Fallback: use file stem as slug
                    slugs.append(Path(entry).stem)
        return slugs
    except Exception:
        return []


def _release_plan_gates(
    plan_done: dict[str, str],
    file_footprints: dict[str, set[str]],
    plan_gates: dict[str, threading.Event],
    active: dict[str, object],
    phase_lock: threading.Lock,
) -> None:
    """Check newly-planned stories and release their gates if no file overlap.

    Called from the scheduling loop — both the poll interval and after a future
    completes — to avoid deadlock when gated workers block in _run_fresh.
    """
    with phase_lock:
        pd_snapshot = dict(plan_done)

    for pd_slug in pd_snapshot:
        if pd_slug not in file_footprints:
            ws_path = Path(pd_snapshot[pd_slug])
            footprint = _extract_plan_footprint(ws_path)
            file_footprints[pd_slug] = footprint

            # Check overlap with stories already past their gate (in DEV)
            active_dev_files: set[str] = set()
            for other_slug, other_files in file_footprints.items():
                if other_slug != pd_slug and other_slug in active and other_slug not in plan_gates:
                    active_dev_files |= other_files

            overlap = footprint & active_dev_files
            if overlap:
                _log(
                    f"WARNING: {pd_slug} overlaps with active stories on: "
                    f"{', '.join(sorted(overlap))}"
                )
            else:
                if pd_slug in plan_gates:
                    plan_gates[pd_slug].set()
                    del plan_gates[pd_slug]

    # Re-check deferred gates (conflicting story may have finished)
    for deferred_slug, gate in list(plan_gates.items()):
        if deferred_slug in file_footprints:
            active_dev_files = set()
            for other_slug, other_files in file_footprints.items():
                if (
                    other_slug != deferred_slug
                    and other_slug in active
                    and other_slug not in plan_gates
                ):
                    active_dev_files |= other_files
            overlap = file_footprints[deferred_slug] & active_dev_files
            if not overlap:
                gate.set()
                del plan_gates[deferred_slug]


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


def _register_resumed_story_footprints(
    triages: dict[str, StoryTriage],
    preflight_states: dict[str, CoordinatorState],
) -> dict[str, CoordinatorState]:
    """Ensure resumed dev/review stories contribute likely_files to collision detection."""
    for triage in triages.values():
        if triage.action not in {"review", "dev"} or triage.worktree_path is None:
            continue
        state = preflight_states.get(triage.slug)
        if state is None:
            state = CoordinatorState()
            preflight_states[triage.slug] = state
        _populate_resumed_story_footprint(triage.slug, state, triage.worktree_path)
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
    """

    def _update(updates: dict) -> None:
        phase = updates.get("phase", "")
        with phase_lock:
            phase_changed = bool(phase) and worker_phases.get(slug) != phase
            if phase:
                worker_phases[slug] = phase
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
                    update_kwargs["cost_usd"] = updates["cost_usd"]
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
) -> CoordinatorState:
    """Build the CoordinatorState for a story the scheduler had to terminate itself.

    A worker that times out or raises never hands back its ``CoordinatorResult``,
    so the scheduler has to synthesize one. Synthesizing it from scratch threw
    away everything the engine had already accumulated — the audit for a story
    with three dev iterations and two gate retries was written with
    ``run_id: null``, an empty ``dev_loop`` and an empty ``gate_decisions``
    (#2013). Prefer the engine's own live state for the slug, and fall back to a
    bare state only when the story never reached the engine.

    The returned state is always stamped ESCALATE with the scheduler's error, so
    the telemetry is preserved without the audit misreporting how the story ended.
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
    state.phase = Phase.ESCALATE
    state.error = error
    state.error_type = error_type
    return state


def _failing_required_pr_checks(pr_url: str, project_root: Path, base_branch: str) -> list[str]:
    """Required checks on ``pr_url`` that have reached a terminal failing state.

    Thin seam over :mod:`theforge.sprint.ci_checks` so the queued-PR wait can be
    tested without a live ``gh``. Never raises: an unanswerable probe returns no
    failures, which keeps the caller waiting instead of abandoning a PR whose
    check state we could not read.
    """
    try:
        return failing_required_pr_checks(project_root, pr_url, base_branch)
    except Exception:  # pragma: no cover - ci_checks already fails soft
        return []


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
    """
    deadline = time.monotonic() + timeout_seconds
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
                failing = _failing_required_pr_checks(pr_url, project_root, base_branch)
                if failing:
                    return {
                        "status": "checks_failed",
                        "failing_checks": ", ".join(failing),
                    }

        if time.monotonic() >= deadline:
            return {"status": "timeout"}
        time.sleep(30)


def _queued_pr_failure_message(
    poll_result: dict[str, str], pr_url: str, timeout_seconds: int
) -> str:
    """Render the merge-failure cause for a non-merged queued-PR poll result.

    The cause string is the only evidence downstream RCA has, so each terminal
    status gets its own wording: "timed out" is reserved for an actual deadline
    expiry, and a decided-red PR names the required checks that failed.
    """
    status = poll_result.get("status", "unknown")
    if status == "checks_failed":
        failing = poll_result.get("failing_checks") or "unknown"
        return f"Queued PR required checks failed ({failing}): {pr_url}"
    if status == "timeout":
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


def _classify_and_record(
    task: TaskStory,
    result: CoordinatorResult,
    dag: StoryDAG,
    merged_slugs: set[str],
    story_state: "SprintStoryState | None" = None,
) -> StoryOutcome:
    """Classify result and update DAG state.

    Returns the canonical :class:`StoryOutcome` for the story. When
    ``story_state`` is supplied, the outcome is also recorded there — this is
    the single source of truth that all surfaces project from.
    """
    preflight_verdict = result.state.preflight_verdict
    landing_status = getattr(result, "landing_status", None)
    validate_already_complete = getattr(result.state, "validate_already_complete", False)
    # A confirmed-landed DONE is immutable for the rest of the sprint. Mark it
    # so story_state.transition rejects any later non-DONE terminal overwrite
    # (e.g. a bogus FAILED from a redispatch after a process restart).
    is_landed = landing_status == "landed"

    if preflight_verdict == "ALREADY_DONE" and result.success:
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
        _transition_fields: dict = {"cost_usd": _story_reported_cost(result.state)}
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


def run_sprint(
    config: ForgeConfig,
    sprint: "Path | ResolvedSprint",
    *,
    auto_merge: bool = False,
    interactive: bool = False,
    notify: bool = False,
    resume: bool = False,
    reexec: bool = False,
    state_update_fn: "Callable[[dict], None] | None" = None,
    no_pull: bool = False,
    run_id: str | None = None,
    dropped_slugs: "dict[str, str] | None" = None,
    skipped_issues: "list | None" = None,
    entry_intake_outcomes: "dict[int, IntakeOutcome] | None" = None,
    force: bool = False,
    live_story_slugs: "set[str] | None" = None,
) -> SprintResult:
    """Run all stories in a sprint with optional concurrency.

    Accepts either a ``Path`` to a sprint.yaml manifest (backward-compatible)
    or a pre-built ``ResolvedSprint`` object (produced by query mode or
    ``resolve_from_manifest``).  The function body has no path-shaped
    assumptions — it operates entirely on the resolved object.

    When max_parallel > 1, stories with no unmet dependencies are launched
    concurrently up to max_parallel. Budget is pooled across all workers.
    Merge ordering respects dependency order when auto_merge is True.

    When max_parallel == 1 (default), behavior is identical to the original
    sequential runner.

    Args:
        config: Loaded ForgeConfig for the project.
        sprint: Either a Path to sprint.yaml or a pre-built ResolvedSprint.
        auto_merge: If True, merge each story's branch after APPROVE.
        interactive: If True, pause for human review at each story.
        resume: If True, triage each story to find the optimal re-entry point
            (skip_merged / review / dev / full) and carry forward prior costs.
        reexec: True when this process was re-launched via ``os.execv`` after a
            mid-sprint source change (workspace.pull_base_branch). Such a launch
            keeps the original argv (no ``--resume``) but must be treated as
            resume-equivalent for merged-state reconciliation: every manifest
            story is triaged against merged state before dispatch so a story
            whose PR already landed is never re-entered through WORKSPACE.
        live_story_slugs: Stories of this same sprint generation whose agent
            process groups survived the re-exec (resolved by the CLI via
            ``theforge.sprint.live_stories``). Their worktrees are this run's own
            live work: protected from the orphan sweep, excluded from the passes
            that would collide with a running agent, and dispatched through the
            deferred path that waits for that agent and then resumes the story —
            this process is the only one that can still finish them. Their
            existence is also the evidence that startup-only checks (the baseline
            gate) no longer hold their precondition.

    Returns:
        SprintResult with per-story outcomes and aggregate stats.
    """
    if isinstance(sprint, ResolvedSprint):
        resolved = sprint
    else:
        # Backward-compat: Path was passed — resolve via the shared helper so
        # tests can patch the boundary and query-mode behavior stays aligned.
        resolved = resolve_from_manifest(sprint, config.project_root)

    # A re-exec'd launch (source changed mid-sprint) keeps the original argv and
    # therefore never carries ``--resume``, but it MUST run the same merged-state
    # reconciliation a resume would: triage every manifest story against merged
    # state, exclude already-merged stories from preflight/dispatch, and pre-mark
    # them complete in the DAG. Otherwise a story whose PR already landed in the
    # prior (killed) generation is re-entered through WORKSPACE and its DONE
    # outcome is overwritten with a bogus FAILED. Treat re-exec as
    # resume-equivalent for all reconciliation/skip paths.
    reconcile = resume or reexec

    # Stories still executing from before the re-exec. Everything downstream that
    # would otherwise treat their state as foreign — the orphan worktree sweep,
    # the baseline gate's "nothing has started yet" precondition, the gate-timeout
    # load model — is told about them explicitly.
    _live_story_slugs: set[str] = {s for s in (live_story_slugs or set()) if s}

    # Establish that the agents are reachable BEFORE committing wall clock or
    # budget to them (#1952). This runs ahead of the baseline gate, the base
    # pull, and every worktree touch, so a dead credential costs seconds and
    # leaves no story with a verdict — the run simply never happened.
    enforce_sprint_auth_readiness(config, log=_log)

    # Defensive scrub for the root checkout used by sprint commands.
    _scrub_root_forge_artifacts(config)
    sweep_orphan_worktrees(config.project_root, config, protected_slugs=_live_story_slugs)

    max_parallel = (
        resolved.max_parallel if resolved.max_parallel is not None else config.sprint.max_parallel
    )
    base_worker_timeout_seconds = config.sprint.worker_timeout_seconds

    # Build unified context mapping: (task, source, canonical_ref) per entry
    task_entries = resolved.stories
    slug_to_context: dict[str, tuple[TaskStory, StorySource, str]] = {
        task.slug: (task, source, canonical_ref) for task, source, canonical_ref in task_entries
    }
    dependent_slugs = {dep for task, _src, _ref in task_entries for dep in task.depends_on}

    # Dependency parents this sprint can actually dispatch and integrate itself.
    # ``dependent_slugs`` is the raw edge target set, which also names external
    # references — an issue already merged on main, or one this sprint does not
    # carry. Those parents never run here and never merge into the project-root
    # checkout, so a landing obligation derived from the raw set would refuse a
    # PR-landing sprint whose only edge points outside the manifest (#2048
    # review iteration 2).
    in_manifest_dependency_parents = dependent_slugs & set(slug_to_context)

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
    config_lands_in_project_root = config.workspace.on_approve == "merge" or (
        auto_merge and max_parallel <= 1
    )
    dependency_parents_land_in_project_root = (
        max_parallel <= 1 or config.workspace.on_approve != "merge-pr"
    )

    # Stories of this sprint whose agent survived the re-exec. They stay in the
    # DAG and are dispatched like any other story, but through the deferred path
    # (``_run_inherited_story``): wait for the inherited agent, then resume from
    # whatever it left behind. Excluded only from the work that would collide
    # with a running agent — startup triage, intake, batch preflight — because
    # each of those reads or writes a worktree that is being mutated right now.
    _inflight_slugs: set[str] = {s for s in _live_story_slugs if s in slug_to_context}

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
        config,
        auto_merge=(max_parallel <= 1 and (auto_merge or bool(in_manifest_dependency_parents))),
    )

    total = len(task_entries)
    noun = "stories" if total != 1 else "story"
    # Substrate provenance: name the runtime executing this sprint so the
    # operator can never be confused about which install is in effect. See
    # theforge.cli.substrate for the failure mode this closes.
    try:
        from theforge.cli.substrate import emit_provenance

        emit_provenance(
            cwd=config.project_root,
            bypass_mismatch=bool(force),
        )
    except Exception:
        # Provenance is operator-visible information, not a correctness gate;
        # never let a detection failure block sprint start.
        pass
    print(
        f'[sprint] "{resolved.name}"  {total} {noun}  budget=${resolved.budget_usd:.2f}'
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
    _baseline_gate_timeout = int(config.validation.gate_timeout or 600)
    _host_cores = os.cpu_count() or 1
    _gate_cpu_raw = config.validation.gate_cpu_cores
    _gate_cpu_cores = int(_gate_cpu_raw) if _gate_cpu_raw else None
    _mode_raw = config.validation.gate_timeout_scale
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
    )
    if _gate_timeout_resolution is not None:
        print(
            f"[sprint] gate_timeout: {_gate_timeout_resolution.reason}",
            file=sys.stderr,
            flush=True,
        )
    if _gate_timeout_resolution is not None and _gate_timeout_resolution.overcommit:
        _gpc = _gate_timeout_resolution.gate_cpu_cores
        _mp = _gate_timeout_resolution.actual_parallel or _gate_timeout_resolution.max_parallel
        _hc = _gate_timeout_resolution.host_cores
        print(
            f"[sprint] WARNING: gate CPU demand ({_gpc} cores × parallel {_mp} = "
            f"{_gpc * _mp} cores) exceeds host capacity ({_hc} cores) by >50%; "
            "consider lowering --parallel to avoid contention-driven gate timeouts",
            file=sys.stderr,
            flush=True,
        )
    if (
        _gate_timeout_resolution is not None
        and _gate_timeout_resolution.effective_timeout != _baseline_gate_timeout
    ):
        config = replace(
            config,
            validation=replace(
                config.validation,
                gate_timeout=_gate_timeout_resolution.effective_timeout,
            ),
        )

    for warning in _agent_cost_tracking_warnings(config):
        _log(warning)
    for task, _src, _ref in task_entries:
        for phrase in task.dependency_warnings:
            _log(
                "WARN: dependency-shaped prose ignored for "
                f"{task.slug} ({task.name}): {phrase!r}; "
                "declare dependencies with GitHub blocked-by relationships "
                "or leading issue metadata"
            )

    # Sprint-level structured logger
    _cli_run_id = run_id
    _sprint_run_id = _generate_run_id()
    _sprint_logger = StructuredLogger(
        run_id=_sprint_run_id,
        project=config.project,
        task=resolved.name,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
        project_root=config.project_root,
    )
    _sprint_logger.emit(
        "run_start",
        stories=[ref for _, _, ref in task_entries],
        budget_usd=resolved.budget_usd,
        max_parallel=max_parallel,
        resume=resume,
    )

    # Create sprint-level log directory
    _sprint_log_dir = config.project_root / ".forge" / "logs" / resolved.name
    try:
        _sprint_log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        _sprint_log_dir = None  # type: ignore[assignment]

    # Stable sprint_id — does not change across run_id rollovers or --resume.
    # Used to aggregate story outcomes across all worker-process boundaries.
    _sprint_id: str | None = None
    try:
        _sprint_id = _get_or_create_sprint_id(resolved.name, config.project_root)
    except Exception:
        pass

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
        config,
        lands_in_project_root=config_lands_in_project_root,
        stage="sprint-entry",
    )

    if not no_pull and _project_root_is_git_checkout(config.project_root):
        coordinator_workspace.pull_base_branch(config, lands_locally=_sprint_lands_locally)

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
        if not run_id:
            return
        update_state_phase(
            run_id, config.project_root, phase, detail=detail, started_at=started_at
        )

    baseline_started_at = datetime.datetime.now(datetime.timezone.utc)
    _continuation_reason = _continuation_evidence(
        reexec=reexec,
        live_story_slugs=_live_story_slugs,
    )
    if _continuation_reason is not None:
        baseline_gate = _skipped_baseline_gate(config, _continuation_reason)
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
            detail=f"merge base of {config.workspace.base_branch}",
            started_at=baseline_started_at.isoformat(),
        )
        try:
            baseline_gate = _run_baseline_gate(config, resolved)
        finally:
            _publish_sprint_phase(SPRINT_PHASE_STARTING)
    resolved.baseline_gate = baseline_gate
    _log(str(baseline_gate.get("message", "Baseline gate check completed")))
    if not bool(baseline_gate.get("passed", False)):
        _write_sprint_audit(
            manifest=resolved,
            result=SprintResult(
                name=resolved.name,
                specs_total=total,
                specs_succeeded=0,
                specs_failed=total,
                specs_skipped=0,
                total_cost_usd=0.0,
                budget_usd=resolved.budget_usd,
                results=[],
                stopped_reason="broken_baseline",
            ),
            canonical_refs=[ref for _, _, ref in task_entries],
            started_at=baseline_started_at,
            finished_at=datetime.datetime.now(datetime.timezone.utc),
            duration=float(baseline_gate.get("duration_seconds", 0.0)),
            project_root=config.project_root,
            slug_map={ref: task.slug for task, _src, ref in task_entries},
            tasks_by_slug={task.slug: task for task, _src, _ref in task_entries},
            sprint_id=_sprint_id,
            dropped_slugs=dropped_slugs,
            skipped_issues=skipped_issues,
            run_id=run_id,
        )
        raise RuntimeError(str(baseline_gate.get("message", "Broken baseline")))

    started_at = datetime.datetime.now(datetime.timezone.utc)
    accumulated_cost = 0.0
    # Every source of spend this sprint could not measure, in the order it was
    # discovered ("issue-1945" for a story, "intake:issue-1946" for a
    # remediation pass, "carried:..." for spend inherited on resume). While this is
    # non-empty, ``accumulated_cost`` is a measured LOWER BOUND, not the
    # sprint's spend — the budget check must refuse to certify a cap it cannot
    # evaluate rather than dispatch more work against an understated total
    # (#1992). Guarded by ``cost_lock`` alongside ``accumulated_cost``.
    unmeasured_spend: list[str] = []
    # Entry-level intake remediation runs in the CLI before run_sprint and
    # spends the same sprint-authorized budget; fold its agent cost into
    # the sprint total so operator-visible accounting matches actual spend.
    if entry_intake_outcomes:
        for _issue_num, _entry_outcome in entry_intake_outcomes.items():
            if _intake_outcome_cost_measured(_entry_outcome) is None:
                unmeasured_spend.append(f"entry-intake:issue-{_issue_num}")
        _entry_intake_cost = sum(_intake_outcome_cost(o) for o in entry_intake_outcomes.values())
        if _entry_intake_cost > 0.0:
            accumulated_cost += _entry_intake_cost
            _log(
                f"Entry-intake remediation cost: ${_entry_intake_cost:.4f} "
                "(rolled into sprint total)"
            )
    prior_cost = 0.0
    results: list[tuple[str, CoordinatorResult]] = []
    if notify and config.notifications.backend not in ("ntfy", "none"):
        from ..notify_backends import send_notifications

        send_notifications(
            config,
            f'TheForge: sprint started \u2014 "{resolved.name}"',
            f"{total} stories \u00b7 budget ${resolved.budget_usd:.2f}",
        )
    # Canonical sprint story state — single source of truth for every
    # operator-facing surface (forge status, banner, summary, notifications).
    # No local counters are kept; counts are projected from this structure.
    _story_state = SprintStoryState()
    # Pre-restart spend that the canonical story state will not be holding by
    # the time totals are projected. It is re-attached at wrap-up (see
    # ``_bump_story_cost``); without it the summary total — which sums the
    # canonical state — silently drops spend that SprintResult still counts via
    # ``prior_cost``, so two operator-facing totals disagree about one run.
    #
    # Two disjoint ways the canonical state loses it, both only for stories that
    # re-enter this generation:
    #   unseeded — a non-succeeded (or unmappable) prior outcome is never
    #     seeded, because monotonicity would reject the later transition to
    #     RUNNING. Its cost is therefore always missing.
    #   seeded-then-overwritten — a succeeded prior outcome IS seeded with its
    #     cost, but if the story runs again, transition() replaces cost_usd
    #     with the coordinator's current-generation total. The seed survives
    #     only for a story that does not re-run (e.g. resume_skip_merged), so
    #     this one is re-attached conditionally, keyed on whether the story
    #     actually ran.
    unseeded_prior_story_cost: dict[str, float] = {}
    seeded_prior_story_cost: dict[str, float] = {}
    ran_this_generation: set[str] = set()
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
    if _sprint_id is not None:
        from .audit import _load_accumulated_stories as _preload  # noqa: PLC0415

        _current_run_slugs = set(slug_to_context.keys())
        _succeeded_outcomes = {"DONE", "ALREADY_DONE"}

        def _prior_cost_of(prior: dict) -> float:
            try:
                return float(prior.get("cost_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def _defer_prior_cost(slug: str, prior: dict) -> None:
            _cost = _prior_cost_of(prior)
            if _cost > 0.0:
                unseeded_prior_story_cost[slug] = unseeded_prior_story_cost.get(slug, 0.0) + _cost

        for _prior in _preload(_sprint_id, config.project_root):
            _prior_slug = _prior.get("slug")
            if not _prior_slug:
                continue
            # An earlier generation's unpriced story is still unpriced spend
            # this sprint carries. Without re-flagging it here, a --resume would
            # start with an empty ledger and enforce the cap against a carried
            # lower bound as if it were complete (#1992).
            if "cost_usd" in _prior and _prior.get("cost_usd") is None:
                unmeasured_spend.append(f"carried:{_prior_slug}")
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
                _seeded_cost = _prior_cost_of(_prior)
                if _seeded_cost > 0.0:
                    seeded_prior_story_cost[_prior_slug] = (
                        seeded_prior_story_cost.get(_prior_slug, 0.0) + _seeded_cost
                    )
            # Strip per-run terminal artifacts so an accumulated story cannot
            # carry forward a stale review summary or final_outcome from an
            # earlier generation. The current run must write these fresh.
            _prior_detail_raw = _prior.get("detail")
            _prior_detail = dict(_prior_detail_raw) if isinstance(_prior_detail_raw, dict) else {}
            for _stale in ("final_outcome", "review_verdict", "review_p1", "review_p2"):
                _prior_detail.pop(_stale, None)
            _story_state.register(
                _prior_slug,
                _prior.get("path", _prior_slug),
                outcome=_mapped_outcome,
                cost_usd=_optional_cost(_prior.get("cost_usd")),
                canonical_ref=_prior.get("canonical_ref"),
                detail=_prior_detail,
            )
    _state_writer: SprintStateWriter | None = None
    stopped_reason: str | None = None
    ci_halt_slug: str | None = None
    merged_slugs: set[str] = set()

    def _set_outcome(slug: str, outcome: StoryOutcome | str, **fields: object) -> None:
        """Transition a story's canonical outcome.

        All count-affecting events flow through this helper so the canonical
        structure is the only place the runner records outcomes. The state
        writer (when present) shares the same SprintStoryState instance and
        the on-disk live status file is updated in lockstep.
        """
        if not _story_state.has(slug):
            ctx = slug_to_context.get(slug)
            if ctx is not None:
                _t, _src, _ref = ctx
                _key = f"Issue #{_ref.split(':')[1]}" if _ref.startswith("issue:") else _ref
                _story_state.register(slug, _key, canonical_ref=_ref)
            else:
                _story_state.register(slug, slug)
        canonical_outcome = coerce_outcome(outcome)
        if canonical_outcome.is_terminal and "finished_at" not in fields:
            fields["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if _state_writer is not None:
            # Writer holds the same instance; this both transitions outcome
            # AND atomically rewrites the live .state file.
            _state_writer.update(slug, status=outcome, **fields)
        else:
            _story_state.transition(slug, outcome=outcome, **fields)

    # Derive slug_to_spec from unified context mapping
    slug_to_spec: dict[str, str] = {slug: ctx[2] for slug, ctx in slug_to_context.items()}

    recovered_prior_started_at: datetime.datetime | None = None
    recovered_prior_entries_by_ref: dict[str, dict] = {}
    # Resume mode (and re-exec, treated as resume-equivalent): triage all stories
    # and carry forward prior costs.
    triages: dict[str, StoryTriage] = {}
    if reconcile:
        prior_cost, recovered_prior_started_at, recovered_prior_entries_by_ref = (
            _read_prior_sprint_accounting(config.project_root, _sprint_id)
        )
        if prior_cost > 0.0:
            _log(f"Resuming with prior cost: ${prior_cost:.2f}")
        if _prior_sprint_cost_incomplete(config.project_root, _sprint_id):
            # The carried total came from a generation that recorded incomplete
            # cost, so it is a lower bound too (#1992).
            unmeasured_spend.append("carried:prior-generation")
        if recovered_prior_started_at is not None and recovered_prior_started_at < started_at:
            started_at = recovered_prior_started_at
        _log("Triaging specs...")
        _publish_sprint_phase(SPRINT_PHASE_TRIAGE)
        for slug, (task, _src, canonical_ref) in slug_to_context.items():
            if slug in _inflight_slugs:
                # Triage reads the worktree to decide the re-entry point, and
                # this one is being written to by a running agent right now — any
                # verdict taken here describes a half-finished state. Defer it to
                # dispatch, after the inherited agent has stopped.
                _log(f"  {slug:<20} DEFERRED (agent still running; triage after it finishes)")
                continue
            triage = _triage_spec(
                canonical_ref,
                config,
                config.project_root,
                task=task,
                on_gate_start=lambda label: _publish_reuse_gate_start(
                    run_id, config.project_root, label
                ),
                on_gate_end=lambda label: _publish_reuse_gate_end(
                    run_id, config.project_root, label
                ),
            )
            triages[canonical_ref] = triage
            _log(
                f"  {triage.slug:<20} {triage.action.upper().replace('_', ' ')} ({triage.reason})"
            )

    # Build satisfied set: closed dep slugs detected at manifest build time,
    # resume-mode skip states, plus any cross-sprint depends_on slugs whose
    # branch is already merged to the base branch.
    pre_satisfied: set[str] = set(resolved.closed_dependency_slugs)
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

    # Build DAG
    all_tasks = [ctx[0] for ctx in slug_to_context.values()]
    satisfied_slugs = resolve_satisfied_dependencies(
        all_tasks,
        project_root=config.project_root,
        base_branch=config.workspace.base_branch,
        branch_pattern=config.workspace.branch_pattern,
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
        config,
        lands_in_project_root=(
            bool(_dispatchable_dependency_parents) and dependency_parents_land_in_project_root
        ),
        stage="dependency-resolved",
    )

    # Surface the current sprint phase to forge status --watch so operators
    # see meaningful progress signals during the multi-minute pre-init window.
    if run_id:
        from .state_writer import update_state_phase as _update_state_phase

        _update_state_phase(run_id, config.project_root, "intake-remediation")

    # Per-story projection used by audit and summary writers when a slug never
    # produces a CoordinatorResult (e.g., dropped at the intake gate, blocked
    # by deps, or skipped pre-launch). Defined here — before the intake gate —
    # so intake-dropped stories can populate it with their finding detail; the
    # writers read from this dict to surface error/error_type/intake metadata
    # that would otherwise be null in audit YAML and sprint summary YAML.
    current_story_entries_by_ref: dict[str, dict] = {}

    story_cost_adjustments: dict[str, float] = {}

    def _persist_accumulated_story_entries() -> None:
        if _sprint_id is None:
            return
        accumulated_by_ref = {
            ref: dict(entry) for ref, entry in recovered_prior_entries_by_ref.items()
        }
        for canonical_ref, entry in current_story_entries_by_ref.items():
            accumulated_by_ref[canonical_ref] = {"canonical_ref": canonical_ref, **entry}
        persist_accumulated_story_state(
            _sprint_id,
            resolved.name,
            config.project_root,
            list(accumulated_by_ref.values()),
        )

    def _persist_current_story_result(
        slug: str,
        result: CoordinatorResult,
        *,
        started_at: datetime.datetime,
        finished_at: datetime.datetime,
    ) -> None:
        task_ctx = slug_to_context.get(slug)
        if task_ctx is None:
            return
        # Every terminal path for a dispatched story lands here (completion,
        # timeout, exception), so this is the reliable record of which stories
        # actually consumed a coordinator run this generation — and therefore
        # which ones had a seeded prior cost overwritten by transition().
        ran_this_generation.add(slug)
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
            "preflight_verdict"
            if outcome == "ALREADY_DONE" and preflight == "ALREADY_DONE"
            else None
        )
        current_story_entries_by_ref[canonical_ref] = {
            "path": display_key,
            "slug": slug,
            "outcome": outcome,
            "outcome_source": outcome_source,
            "verdict": None,
            "cost_usd": _story_reported_cost(result.state, story_cost_adjustments.get(slug, 0.0)),
            "story_run_id": run_id,
            "preflight": preflight,
            "preflight_original_verdict": getattr(
                result.state, "preflight_cached_original_verdict", None
            ),
            "preflight_source_run_id": getattr(result.state, "preflight_cached_from_run_id", None),
            "error": result.state.error,
            "error_type": result.state.error_type,
            "outcome_code": result.state.error_type or outcome.lower(),
            "merge": result.merge is not None and result.merge.get("merged", False),
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
        _persist_accumulated_story_entries()

    def _record_current_story_entry(
        slug: str,
        outcome: str,
        *,
        error: str | None = None,
        error_type: str | None = None,
        cost_usd: float = 0.0,
        extras: dict | None = None,
    ) -> None:
        task_ctx = slug_to_context.get(slug)
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
            "cost_usd": cost_usd,
            "story_run_id": run_id,
            "preflight": None,
            "preflight_original_verdict": None,
            "preflight_source_run_id": None,
            "error": error,
            "error_type": error_type,
            "outcome_code": error_type or outcome.lower(),
            "merge": False,
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
        current_story_entries_by_ref[canonical_ref] = entry
        _persist_accumulated_story_entries()

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
    _dropped_exclusion = {s for s in (dropped_slugs or {}) if s in slug_to_context}
    _no_dispatch_slugs = skip_slugs | _dropped_exclusion
    dispatch_tasks = [
        t
        for t in normalized.tasks
        if t.slug not in _no_dispatch_slugs and t.slug not in _inflight_slugs
    ]

    intake_outcomes = _run_intake_remediation_pass(
        config=config,
        tasks=dispatch_tasks,
        log=_log,
        force=force,
        sprint_id=_sprint_id,
    )
    # Intake remediation agent spend (auto_fix LLM rewrites) must roll up
    # into the sprint total. Without this, sprint.total_cost_usd silently
    # excludes every dollar spent on intake auto-fix attempts.
    for _intake_slug, _intake_outcome in intake_outcomes.items():
        if _intake_outcome_cost_measured(_intake_outcome) is None:
            unmeasured_spend.append(f"intake:{_intake_slug}")
    _intake_remediation_cost = sum(_intake_outcome_cost(o) for o in intake_outcomes.values())
    if _intake_remediation_cost > 0.0:
        accumulated_cost += _intake_remediation_cost
        _log(
            f"Intake remediation cost: ${_intake_remediation_cost:.4f} (rolled into sprint total)"
        )
    for _slug, _outcome in intake_outcomes.items():
        story_cost_adjustments[_slug] = story_cost_adjustments.get(
            _slug, 0.0
        ) + _intake_outcome_cost(_outcome)
    for _issue_num, _outcome in (entry_intake_outcomes or {}).items():
        _issue_slug = f"issue-{_issue_num}"
        story_cost_adjustments[_issue_slug] = story_cost_adjustments.get(
            _issue_slug, 0.0
        ) + _intake_outcome_cost(_outcome)
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
            task_ctx = slug_to_context.get(slug)
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

    if run_id:
        from .state_writer import update_state_phase as _update_state_phase

        _update_state_phase(run_id, config.project_root, "preflight")

    # Re-derive the filter here: ``normalized`` may have been re-bound by the
    # intake drop above, and reconcile-skipped merged stories (plus pre-launch
    # dropped stories) must never enter the preflight batch (WORKSPACE re-entry
    # against their stale worktree, or spending budget on an already-dropped
    # story).
    _no_dispatch_slugs = skip_slugs | {s for s in (dropped_slugs or {}) if s in slug_to_context}
    preflight_tasks = [
        t
        for t in normalized.tasks
        if t.slug not in _no_dispatch_slugs and t.slug not in _inflight_slugs
    ]
    preflight_states = run_batch_preflight(
        preflight_tasks,
        config,
        sprint_name=resolved.name,
        no_pull=no_pull,
        max_parallel=max_parallel,
        notify=notify,
    )
    story_worker_timeouts: dict[str, int] = {}
    for task, _src, _canonical_ref in task_entries:
        if resolved.worker_timeout_seconds is not None:
            story_worker_timeouts[task.slug] = resolved.worker_timeout_seconds
            _log(
                f"  Worker timeout {task.slug}: {resolved.worker_timeout_seconds}s "
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
    if resume:
        _register_resumed_story_footprints(triages, preflight_states)
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
    synthetic_edges = compute_synthetic_edges(preflight_states, normalized.tasks)
    if synthetic_edges:
        _log(f"Injected synthetic dependency constraints for {len(synthetic_edges)} stories")
    augmented_tasks = inject_synthetic_deps(normalized.tasks, synthetic_edges)
    blocked_slugs = dict(normalized.blocked)
    if run_id:
        from .state_writer import update_state_phase as _update_state_phase

        _update_state_phase(run_id, config.project_root, "dag-build")
    try:
        dag = build_dag(augmented_tasks, satisfied=satisfied_slugs)
    except ValueError as exc:
        raise ValueError(f"{exc} Synthetic collision edges: {synthetic_edges}") from exc

    # Dependencies already satisfied outside this sprint still count as landed
    # for deferred integration ordering.
    merged_slugs.update(satisfied_slugs)

    # Resume / re-exec: pre-mark skip_merged / skip stories as complete in DAG.
    # skip_merged stories are already merged and should satisfy dependencies
    # immediately, but they still count as skipped in sprint aggregates. This is
    # the block that actually removes a slug from dag.ready()/remaining(): without
    # it a re-exec'd process would re-dispatch an already-merged story even though
    # it was excluded from preflight above.
    if reconcile:
        for slug, (_task, _src, canonical_ref) in slug_to_context.items():
            triage = triages.get(canonical_ref)
            if triage and triage.action in ("skip_merged", "skip"):
                _log(f"SKIP {slug} ({triage.reason})")
                if triage.action == "skip_merged":
                    merged_slugs.add(slug)
                    dag.mark_complete(slug)
                    _prior_entry = recovered_prior_entries_by_ref.get(canonical_ref)
                    if _prior_entry is not None:
                        _already_done_entry = dict(_prior_entry)
                        _already_done_entry["outcome"] = "ALREADY_DONE"
                        _already_done_entry["outcome_source"] = "resume_skip_merged"
                        current_story_entries_by_ref[canonical_ref] = {
                            k: v for k, v in _already_done_entry.items() if k != "canonical_ref"
                        }
                        _persist_accumulated_story_entries()
                    # Preserve preloaded prior-run outcome (e.g., DONE) when
                    # accumulated state already has a stronger terminal —
                    # otherwise mark SKIPPED for the legacy aggregate contract.
                    _existing = _story_state.get(slug)
                    if _existing is None or not _existing.outcome.is_succeeded:
                        _set_outcome(slug, StoryOutcome.SKIPPED, reason=triage.reason)
                else:
                    dag.mark_skipped(slug)
                    _existing = _story_state.get(slug)
                    if _existing is None or not _existing.outcome.is_succeeded:
                        _set_outcome(slug, StoryOutcome.SKIPPED, reason=triage.reason)
                    _record_current_story_entry(slug, "SKIPPED", error=triage.reason)

    auto_enabled_dependency_merges = dependent_slugs - satisfied_slugs - merged_slugs
    if (
        max_parallel > 1
        and not auto_merge
        and config.workspace.on_approve != "merge-pr"
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
        dag.mark_skipped(slug)
        _blocked_reason = f"blocked: {', '.join(blocked_by)}"
        _set_outcome(slug, StoryOutcome.SKIPPED, reason=_blocked_reason)
        _record_current_story_entry(slug, "SKIPPED", error=_blocked_reason)

    # Stories dropped pre-launch (e.g. re-exec collision) never enter the DAG.
    # They surface with a distinct DROPPED/PRESERVED outcome in sprint-audit and
    # the live state file so operators can see exactly which stories did not
    # run and why — a silent WARNING is not enough visibility.
    #
    # ``preserved-escalated`` is a disjoint case: the worktree is intentionally
    # kept for human review, and counts as skipped (not failed) in aggregates.
    _dropped_slugs: dict[str, str] = dict(dropped_slugs or {})
    for slug, reason in _dropped_slugs.items():
        if slug not in slug_to_context:
            continue
        if reason == "preserved-escalated":
            _log(f"PRESERVED {slug} (escalated worktree held for review)")
            dag.mark_skipped(slug)
            _set_outcome(slug, StoryOutcome.PRESERVED, reason=reason)
            _record_current_story_entry(slug, "PRESERVED", error=reason, error_type="dropped")
        elif reason == REASON_RECONCILE_PRIOR_DONE:
            # The prior generation already completed this story; its worktree
            # collision is a reconcilable success, not a fresh drop. Mark it
            # ALREADY_DONE so it counts as succeeded and is preserved durably.
            # Use mark_complete (not mark_skipped) so it satisfies the hard
            # dependencies of any current story that depends_on this slug —
            # a reconciled prior-DONE is a met dependency, exactly like a
            # resume skip_merged, so dependents must not be stranded/skipped.
            _log(f"ALREADY_DONE {slug} (reconciled from prior generation)")
            dag.mark_complete(slug)
            _set_outcome(slug, StoryOutcome.ALREADY_DONE, reason=reason)
            _record_current_story_entry(
                slug,
                "ALREADY_DONE",
                extras={
                    "drop_reason": reason,
                    "outcome_source": "reexec_reconcile",
                },
            )
        elif reason == REASON_STRANDED_WORKTREE:
            # A prior-generation worktree exists but the story did not succeed:
            # recoverable stranded sprint state. Keep it DROPPED but retain the
            # distinct reason so RCA/audit can tell it apart from a fresh
            # collision (do NOT clear the worktree and re-sprint fresh).
            _log(f"DROPPED {slug} (stranded prior-generation sprint state)")
            dag.mark_skipped(slug)
            _set_outcome(slug, StoryOutcome.DROPPED, reason=reason)
            _record_current_story_entry(
                slug,
                "DROPPED",
                error=reason,
                error_type="dropped",
                extras={"drop_reason": reason},
            )
        else:
            _log(f"DROPPED {slug} (reason: {reason})")
            dag.mark_skipped(slug)
            _set_outcome(slug, StoryOutcome.DROPPED, reason=reason)
            _record_current_story_entry(slug, "DROPPED", error=reason, error_type="dropped")

    # Persist resume-time already-completed stories before any possible re-exec
    # handoff so later generations can recover the full logical sprint history.
    if resume:

        def _already_done_story_entry(
            canonical_ref: str,
            slug: str,
            *,
            depends_on: list[str],
        ) -> dict:
            prior_entry = recovered_prior_entries_by_ref.get(canonical_ref, {})
            display_key = (
                f"Issue #{canonical_ref.split(':')[1]}"
                if canonical_ref.startswith("issue:")
                else canonical_ref
            )
            return {
                "canonical_ref": canonical_ref,
                "path": display_key,
                "slug": slug,
                "outcome": "ALREADY_DONE",
                "outcome_source": "resume_skip_merged",
                "verdict": prior_entry.get("verdict"),
                "cost_usd": _optional_cost(prior_entry.get("cost_usd")),
                "story_run_id": prior_entry.get("story_run_id", run_id),
                "preflight": prior_entry.get("preflight"),
                "preflight_original_verdict": prior_entry.get("preflight_original_verdict"),
                "preflight_source_run_id": prior_entry.get("preflight_source_run_id"),
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
            ref: dict(entry) for ref, entry in recovered_prior_entries_by_ref.items()
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
                            slug_to_context.get(_resume_slug, (None, None, None))[0],
                            "depends_on",
                            None,
                        )
                        or []
                    ),
                ),
            )
        for _closed_slug in sorted(resolved.closed_dependency_slugs):
            _canonical_ref = f"issue:{_closed_slug.removeprefix('issue-')}"
            if _canonical_ref in triages:
                continue
            _resume_accumulated_by_ref.setdefault(
                _canonical_ref,
                _already_done_story_entry(_canonical_ref, _closed_slug, depends_on=[]),
            )
        if _resume_accumulated_by_ref:
            persist_accumulated_story_state(
                _sprint_id,
                resolved.name,
                config.project_root,
                list(_resume_accumulated_by_ref.values()),
            )

    # Initialise live state file for forge sprint-status (only when a CLI run_id
    # is present — headless/test invocations without a run_id skip this).
    if run_id:
        _bundle_candidate_slugs: set[str] = {s for bundle in bundle_assignments for s in bundle}
        _initial_stories: list[dict] = []
        _initial_story_slugs: set[str] = set()
        for _slug, (_task, _src, _canonical_ref) in slug_to_context.items():
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
                # done, reconciled, not a fresh drop.
                _status = "done"
                _blocked_by = []
                _detail = {
                    "final_outcome": "ALREADY_DONE",
                    "outcome_source": "reexec_reconcile",
                }
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
            elif _triage and _triage.action == "skip_merged":
                _status = "done"
                _detail = {
                    "final_outcome": "ALREADY_DONE",
                    "outcome_source": "resume_skip_merged",
                }
            elif _triage and _triage.action == "skip":
                _status = "skipped"
                _detail = {"final_outcome": "SKIPPED"}
            elif _slug in _inflight_slugs:
                # Still running from before the re-exec: this run will wait for
                # that agent and then resume the story, so it is waiting work —
                # not a drop, and not something an operator should read as idle.
                _status = "waiting"
                _blocked_by = [f"in flight: {REASON_IN_FLIGHT}"]
                _detail = {"in_flight": True, "in_flight_reason": REASON_IN_FLIGHT}
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
                    "cost_usd": 0.0,
                    "bundle_candidate": _slug in _bundle_candidate_slugs,
                    "blocked_by": _blocked_by,
                    "complexity": None,
                    "detail": _detail,
                }
            )
            _initial_story_slugs.add(_slug)
        for _closed_slug in sorted(resolved.closed_dependency_slugs):
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
        _state_writer = SprintStateWriter(
            run_id,
            config.project_root,
            resolved.name,
            sprint_id=_sprint_id,
            story_state=_story_state,
            budget_usd=resolved.budget_usd,
            max_parallel=max_parallel,
            base_branch=getattr(getattr(config, "workspace", None), "base_branch", None),
        )
        _state_writer.init(_initial_stories)
        _state_writer.set_phase("running")
        # Register shape-gate-skipped issues in the canonical structure so
        # forge status surfaces them with the gate reason. They are visible
        # to every operator surface from this point on.
        for _sk in skipped_issues or []:
            _sk_dict = _sk.as_dict() if hasattr(_sk, "as_dict") else dict(_sk)
            _sk_num = _sk_dict.get("issue_number")
            if _sk_num is None:
                continue
            _sk_slug = f"issue-{_sk_num}"
            if _story_state.has(_sk_slug):
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
            _sk_intake = (entry_intake_outcomes or {}).get(_sk_num)
            if _sk_intake is not None:
                _sk_detail["intake_kind"] = _sk_intake.kind.value
                _sk_detail["intake_detail"] = _sk_intake.detail
                _sk_detail["intake_findings"] = [f.as_dict() for f in _sk_intake.findings]
                _sk_detail["intake_audit"] = dict(_sk_intake.audit)
                _sk_detail["intake_proposed_replacement"] = _sk_intake.proposed_replacement
            _state_writer.register(
                _sk_slug,
                f"Issue #{_sk_num}",
                outcome=_sk_outcome,
                reason=_sk_reason,
                detail=_sk_detail,
            )
    elif skipped_issues or []:
        # Headless invocation (no run_id) — still register skipped issues in
        # the canonical structure so summary projects them.
        for _sk in skipped_issues or []:
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
            _sk_intake = (entry_intake_outcomes or {}).get(_sk_num)
            if _sk_intake is not None:
                _sk_detail["intake_kind"] = _sk_intake.kind.value
                _sk_detail["intake_detail"] = _sk_intake.detail
                _sk_detail["intake_findings"] = [f.as_dict() for f in _sk_intake.findings]
                _sk_detail["intake_audit"] = dict(_sk_intake.audit)
                _sk_detail["intake_proposed_replacement"] = _sk_intake.proposed_replacement
            _story_state.register(
                _sk_slug,
                f"Issue #{_sk_num}",
                outcome=_sk_outcome,
                reason=_sk_reason,
                detail=_sk_detail,
            )

    # Parallel scheduling state
    active: dict[str, Future[object]] = {}
    story_deadlines: dict[str, float] = {}
    story_wait_started: set[str] = set()
    cost_lock = threading.Lock()
    story_times: dict[str, tuple[datetime.datetime, datetime.datetime]] = {}
    live_telemetry_snapshots: dict[str, dict] = {}
    batch_assignments: dict[str, int] = {}
    batch_number = 0
    worker_phases: dict[str, str] = {}
    phase_lock = threading.Lock()
    pending_integration: dict[str, tuple[TaskStory, CoordinatorResult]] = {}
    queued_prs: dict[str, tuple[TaskStory, CoordinatorResult, str]] = {}
    _submission_counter = [0]  # mutable for closure capture; counts submitted stories

    # Per-story cancellation events: set by the timeout handler so worker
    # threads stop running instead of continuing past their deadline.
    stop_events: dict[str, threading.Event] = {}

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

    # Overlap detection state (plan gates)
    file_footprints: dict[str, set[str]] = {}  # slug -> files from plan
    plan_gates: dict[str, threading.Event] = {}  # slug -> gate for PLAN→DEV pause
    plan_done: dict[str, str] = {}  # slug -> workspace_path (set by phase callback)
    use_plan_gates = max_parallel > 1  # only for parallel mode

    def _attempt_integration(
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
        nonlocal stopped_reason, ci_halt_slug

        if not all(dep in merged_slugs for dep in task.depends_on):
            result.landing_status = "pending_integration"
            _write_story_audit(config, task, result, sprint_id=_sprint_id)
            return False

        branch = config.workspace.branch_pattern.format(slug=slug)
        wt = config.project_root / config.workspace.path_pattern.format(slug=slug)

        # Read effective mode from the pending merge action stored by _finalize_approve.
        # Falls back to config.workspace.on_approve for legacy/direct callers.
        effective_on_approve = (result.merge or {}).get("action") or config.workspace.on_approve
        story_run_id = result.state.run_id or _sprint_run_id

        story_logger = StructuredLogger(
            run_id=story_run_id,
            project=config.project,
            task=task.slug,
            log_file=config.log.log_file,
            enabled=config.log.enabled,
            project_root=config.project_root,
        )

        with integration_lock(config.project_root):
            from ..coordinator.completion import land_story  # noqa: PLC0415

            parsed_review = (
                result.state.review_results[-1] if result.state.review_results else None
            )
            merge_info, landing_status = land_story(
                config,
                task,
                branch,
                wt,
                parsed_review,
                result.state,
                effective_on_approve,
                logger=story_logger,
                run_id=story_run_id,
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

        if merge_info.get("merged"):
            merged_slugs.add(slug)
            dag.mark_complete(slug)
            _write_story_audit(config, task, result, sprint_id=_sprint_id)
            if effective_on_approve == "merge-pr" and not merge_info.get(
                "auto_merge_queued", False
            ):
                ci_result = poll_required_checks(
                    config.project_root,
                    config.workspace.base_branch,
                    config.workspace.ci_check_timeout_seconds,
                )
                if ci_result["status"] in {"fail", "timeout"}:
                    failing = ", ".join(ci_result["failing_checks"]) or "pending required checks"
                    stopped_reason = (
                        "Required CI checks "
                        f"{ci_result['status']} after merging {slug} "
                        f"at {ci_result['sha']}: {failing}"
                    )
                    ci_halt_slug = slug
                    _log(
                        f"HALT {slug}: required CI checks {ci_result['status']} "
                        f"for {ci_result['sha']} ({failing})"
                    )
            return True

        if merge_info.get("merge_queued"):
            queued_prs[slug] = (task, result, merge_info["pr_url"])
            _write_story_audit(config, task, result, sprint_id=_sprint_id)
            _log(f"INFO {slug}: PR auto-merge queued; waiting for GitHub to report MERGED")
            return True

        result.state.error = merge_info.get("error") or "integration failed"
        _log(f"WARN {slug}: integration failed: {merge_info.get('error')}")
        _write_story_audit(config, task, result, sprint_id=_sprint_id)
        return True

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        while not dag.is_done():
            _log(f"[debug] loop: active={list(active.keys())} fin={dag._finished}")
            _refresh_external_satisfied(dag, all_tasks, config, merged_slugs)
            ready = [t for t in dag.ready() if t.slug not in active]

            for task in ready:
                # Auth circuit breaker (#1952): a credential the substrate
                # already had refused is not worth re-presenting. Skip rather
                # than fail — nothing about this story was ever judged.
                if auth_circuit is not None:
                    dag.mark_skipped(task.slug)
                    _set_outcome(task.slug, StoryOutcome.SKIPPED, reason=auth_circuit_reason)
                    _log(f"SKIPPED {task.slug} ({auth_circuit_reason})")
                    _record_current_story_entry(task.slug, "SKIPPED", error=auth_circuit_reason)
                    if _state_writer is not None:
                        _state_writer.update(task.slug, status="skipped")
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
                blocked_by_queued = [dep for dep in _gate_deps if dep in queued_prs]
                if blocked_by_queued:
                    dependency_failed = False
                    for dep in blocked_by_queued:
                        dep_task, dep_result, dep_pr_url = queued_prs[dep]
                        poll_result = _poll_queued_pr(
                            dep_pr_url,
                            config.project_root,
                            config.workspace.merge_wait_timeout_seconds,
                            base_branch=config.workspace.base_branch,
                        )
                        if poll_result["status"] == "merged":
                            merged_slugs.add(dep)
                            dag.mark_complete(dep)
                            del queued_prs[dep]
                            _write_story_audit(config, dep_task, dep_result, sprint_id=_sprint_id)
                        else:
                            from ..coordinator.completion import (  # noqa: PLC0415
                                mark_merge_failed as _mark_mf,
                            )

                            _err = _queued_pr_failure_message(
                                poll_result,
                                dep_pr_url,
                                config.workspace.merge_wait_timeout_seconds,
                            )
                            _mark_mf(
                                dep_result.state,
                                dep_result,
                                _err,
                                dep_result.state.branch_name,
                            )
                            _set_outcome(
                                dep, StoryOutcome.MERGE_FAILED, phase=dep_result.phase.name
                            )
                            del queued_prs[dep]
                            # Defensive/idempotent: the parent is normally already
                            # in the DAG's _finished set (added when its PR was
                            # queued, via the pending_integration classify branch),
                            # so its collision (soft) edge is already released. This
                            # call guarantees _finished membership on any path that
                            # queued without that classification. _finished (not
                            # _completed) is what keeps a genuine depends_on (hard)
                            # dependent blocked. The redispatch of the released
                            # dependent onto the current base is driven by the
                            # dag.ready() re-check before the deadlock-cleanup sweep.
                            dag.mark_skipped(dep)
                            _write_story_audit(config, dep_task, dep_result, sprint_id=_sprint_id)
                            _log(
                                f"✗ {dep}: queued PR {poll_result['status']} "
                                "before dependent dispatch"
                            )
                            dependency_failed = True
                    if dependency_failed:
                        continue
                    if any(dep in queued_prs for dep in _gate_deps):
                        continue

                # Cap concurrent submissions at max_parallel
                if len(active) >= max_parallel:
                    break

                with cost_lock:
                    _budget_decision = evaluate_budget(
                        accumulated_cost=accumulated_cost,
                        prior_cost=prior_cost,
                        budget_usd=resolved.budget_usd,
                        unmeasured_spend=list(unmeasured_spend),
                    )
                if _budget_decision is not None:
                    dag.mark_skipped(task.slug)
                    _budget_reason = _budget_decision.story_reason
                    _set_outcome(task.slug, StoryOutcome.SKIPPED, reason=_budget_reason)
                    if stopped_reason is None:
                        stopped_reason = _budget_decision.stopped_reason
                        if notify and config.notifications.backend not in ("ntfy", "none"):
                            from ..notify_backends import send_notifications

                            send_notifications(
                                config,
                                _budget_decision.notification_title(resolved.name),
                                f"{_budget_decision.detail} \u2014 remaining stories skipped",
                            )
                    _log(f"SKIPPED {task.slug} ({_budget_reason})")
                    _record_current_story_entry(task.slug, "SKIPPED", error=_budget_reason)
                    if _state_writer is not None:
                        _state_writer.update(task.slug, status="skipped")
                    continue

                # Eager merge for sequential mode; disabled in parallel mode
                effective_am = (
                    False if max_parallel > 1 else (auto_merge or task.slug in dependent_slugs)
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
                triage = triages.get(spec_str) if resume else None
                # A story whose agent survived the re-exec is dispatched through
                # the deferred path: it waits for that agent, triages what it
                # left, and resumes — so it reaches a real terminal outcome in
                # this run instead of being abandoned to the next invocation's
                # orphan reaper.
                _inherited = task.slug in _inflight_slugs
                batch_assignments[task.slug] = batch_number
                _submission_counter[0] += 1
                print(
                    _story_header(_submission_counter[0], total, task.slug),
                    file=sys.stderr,
                    flush=True,
                )
                if _state_writer is not None:
                    _state_writer.update(
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
                    plan_gates[task.slug] = gate

                worker_config = config

                state_fn = _make_worker_phase_fn(
                    task.slug,
                    worker_phases,
                    phase_lock,
                    state_update_fn,
                    plan_done=plan_done if use_plan_gates else None,
                    state_writer=_state_writer,
                    audit_flush=_make_audit_flush_fn(
                        config, task, resolved.name, sprint_id=_sprint_id
                    ),
                )
                stop_evt = threading.Event()
                stop_events[task.slug] = stop_evt
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
                fut = pool.submit(
                    _dispatch_fn,
                    worker_config,
                    task,
                    triage,
                    _sprint_run_id,
                    resolved.name,
                    interactive,
                    notify,
                    resume,
                    effective_am,
                    state_fn,
                    no_pull,
                    gate,
                    preflight_states,
                    stop_evt,
                    # Keyword, not positional: stop_evt must stay the last
                    # positional argument for callers that index args[-1].
                    **_dispatch_kwargs,
                )
                active[task.slug] = fut
                story_deadlines[task.slug] = time.monotonic() + float(
                    story_worker_timeouts[task.slug]
                )

            _log(
                f"[debug] post-submit: active={list(active.keys())}"
                f" queued_prs={list(queued_prs.keys())}"
            )
            if not active and not queued_prs:
                # A terminal-but-not-merged soft-edge parent may have just
                # released a dependent's collision edge. Before declaring a
                # deadlock and sweeping remaining tasks into SKIP, re-enter
                # dispatch for anything now schedulable so a released, ready
                # story runs on the current base instead of being skipped.
                if any(t.slug not in active for t in dag.ready()):
                    continue
                # Deadlock: remaining tasks have unmet or budget-blocked deps
                # Release any pending plan gates so worker threads can exit
                for g_slug, _gate in plan_gates.items():
                    _log(f"Releasing plan gate for {g_slug} (deadlock cleanup)")
                    _gate.set()
                plan_gates.clear()
                for t in dag.remaining():
                    # A mark_skipped earlier in THIS sweep can release a
                    # sibling's soft edge; if that makes any task schedulable,
                    # stop skipping and re-enter dispatch on the next loop pass
                    # rather than sweeping the just-released sibling into a SKIP.
                    if any(r.slug not in active for r in dag.ready()):
                        break
                    unmet = dag.unmet_deps(t.slug)
                    if unmet:
                        dep_list = ", ".join(unmet)
                        _log(f"SKIPPED {t.slug} (dependency failed: {dep_list})")
                        _record_current_story_entry(
                            t.slug, "SKIPPED", error=f"dependency failed: {dep_list}"
                        )
                        _set_outcome(
                            t.slug,
                            StoryOutcome.SKIPPED,
                            reason=f"dependency failed: {dep_list}",
                        )
                    else:
                        _log(f"SKIPPED {t.slug} (blocked)")
                        _record_current_story_entry(t.slug, "SKIPPED", error="blocked")
                        _set_outcome(t.slug, StoryOutcome.SKIPPED, reason="blocked")
                    dag.mark_skipped(t.slug)
                else:
                    break
                continue

            # No active workers but queued PRs are still in flight.
            # Poll each queued PR directly so dependents can be dispatched
            # once the PR lands — do not declare deadlock while PRs are pending.
            if not active and queued_prs:
                for _qp_slug in list(queued_prs):
                    _qp_task, _qp_result, _qp_pr_url = queued_prs[_qp_slug]
                    _qp_poll = _poll_queued_pr(
                        _qp_pr_url,
                        config.project_root,
                        config.workspace.merge_wait_timeout_seconds,
                        base_branch=config.workspace.base_branch,
                    )
                    if _qp_poll["status"] == "merged":
                        merged_slugs.add(_qp_slug)
                        dag.mark_complete(_qp_slug)
                        _qp_result.landing_status = "landed"
                        # Record the confirmed-landed DONE with the immutability
                        # marker. This is the code path most directly implicated
                        # in the redispatch-after-restart bug: a queued PR merges
                        # while another story occupies the only worker slot. The
                        # marker guarantees this DONE cannot later be clobbered.
                        _set_outcome(_qp_slug, StoryOutcome.DONE, landed=True)
                        del queued_prs[_qp_slug]
                        _write_story_audit(config, _qp_task, _qp_result, sprint_id=_sprint_id)
                        _log(f"INFO {_qp_slug}: queued PR merged; unblocking dependents")
                    else:
                        from ..coordinator.completion import (  # noqa: PLC0415
                            mark_merge_failed as _mark_mf,
                        )

                        _err = _queued_pr_failure_message(
                            _qp_poll, _qp_pr_url, config.workspace.merge_wait_timeout_seconds
                        )
                        _mark_mf(_qp_result.state, _qp_result, _err, _qp_result.state.branch_name)
                        _set_outcome(
                            _qp_slug, StoryOutcome.MERGE_FAILED, phase=_qp_result.phase.name
                        )
                        del queued_prs[_qp_slug]
                        # Defensive/idempotent: the parent is normally already in
                        # _finished (added when its PR was queued), so its collision
                        # (soft) edge is already released; this guarantees it on any
                        # path that skipped that classification. _finished (not
                        # _completed) is what still blocks a hard depends_on
                        # dependent. Actual redispatch of a released dependent onto
                        # the current base comes from the dag.ready() re-check at the
                        # top of the deadlock-cleanup branch, reached after the
                        # `continue` below.
                        dag.mark_skipped(_qp_slug)
                        _write_story_audit(config, _qp_task, _qp_result, sprint_id=_sprint_id)
                        _log(f"✗ {_qp_slug}: queued PR {_qp_poll['status']} (no active workers)")
                continue

            _log(f"[debug] calling wait() with {len(active)} active futures")
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
                            else story_deadlines[slug] - _now
                        )
                        for slug in active
                    ),
                )
                _poll_interval = (
                    min(2.0, _time_to_next_timeout) if plan_gates else _time_to_next_timeout
                )
                done_futs, _ = wait(
                    list(active.values()),
                    return_when=FIRST_COMPLETED,
                    timeout=_poll_interval,
                )
                story_wait_started.update(active)
                if not done_futs and use_plan_gates:
                    # Service plan gates while polling
                    _release_plan_gates(plan_done, file_footprints, plan_gates, active, phase_lock)
                _now = time.monotonic()
                expired_slugs = [
                    slug
                    for slug, fut in active.items()
                    if fut not in done_futs and _now >= story_deadlines[slug]
                ]

            _log(f"[debug] wait() returned: {len(done_futs)} done")
            batch_number += 1

            if expired_slugs:
                for slug in expired_slugs:
                    if slug in plan_gates:
                        _log(f"TIMEOUT releasing plan gate for {slug}")
                        plan_gates[slug].set()
                        del plan_gates[slug]
                    fut = active.pop(slug)
                    story_deadlines.pop(slug, None)
                    story_wait_started.discard(slug)
                    # Set the cancellation event BEFORE cancel() so any in-flight
                    # work stops at the next phase boundary or subprocess read.
                    # Future.cancel() is a no-op for an already-running thread.
                    _stop_evt = stop_events.pop(slug, None)
                    if _stop_evt is not None:
                        _stop_evt.set()
                    fut.cancel()
                    _log(
                        f"TIMEOUT {slug} (worker unresponsive after "
                        f"{story_worker_timeouts[slug]}s — marking as failed)"
                    )
                    spec_str = slug_to_spec[slug]
                    timed_out_at = datetime.datetime.now(datetime.timezone.utc)
                    snapshot = _snapshot_last_known(slug, _state_writer)
                    last_phase = snapshot["last_phase"]
                    if slug in story_times:
                        story_started_at = story_times[slug][0]
                    elif snapshot["last_started_at"] is not None:
                        story_started_at = snapshot["last_started_at"]
                    else:
                        story_started_at = timed_out_at
                    _phase_label = f" during phase {last_phase}" if last_phase else ""
                    _timeout_state = _terminal_state_for(
                        slug,
                        config=config,
                        sprint_name=resolved.name,
                        started_at=story_started_at,
                        error=(f"Worker timeout (>{story_worker_timeouts[slug]}s){_phase_label}"),
                        error_type="TimeoutError",
                    )
                    _timeout_result = CoordinatorResult(
                        success=False,
                        phase=Phase.ESCALATE,
                        state=_timeout_state,
                        message=f"Worker thread timed out after {story_worker_timeouts[slug]}s",
                    )
                    story_times[slug] = (story_started_at, timed_out_at)
                    live_telemetry_snapshots[slug] = snapshot
                    # A worker the auth breaker cancelled can also cross its
                    # deadline before returning. It is still a story the sprint
                    # killed over a dead credential, not one that failed — same
                    # attribution as the ordinary cancellation path below.
                    _timeout_outcome: StoryOutcome = StoryOutcome.FAILED
                    if slug in auth_cancelled_slugs:
                        auth_cancelled_slugs.discard(slug)
                        _cancel_reason = f"cancelled mid-flight: {auth_circuit_reason}"
                        _mark_story_auth_cancelled(
                            _timeout_result, auth_circuit, reason=_cancel_reason
                        )
                        _timeout_outcome = StoryOutcome.SKIPPED
                        _log(f"SKIPPED {slug} ({_cancel_reason})")
                    results.append((spec_str, _timeout_result))
                    _write_story_audit(
                        config,
                        slug_to_context[slug][0],
                        _timeout_result,
                        sprint_id=_sprint_id,
                        telemetry_snapshot=snapshot,
                    )
                    _set_outcome(
                        slug,
                        _timeout_outcome,
                        phase="ESCALATE",
                        last_phase=last_phase,
                        # The gate this story may have been sitting in never
                        # reported a decision and never will; leaving the live
                        # detail at gate_status=running is what made the state
                        # file claim a running gate on a failed story (#2013).
                        detail_updates={"gate_status": GATE_STATUS_TIMEOUT},
                    )
                    _persist_current_story_result(
                        slug,
                        _timeout_result,
                        started_at=story_started_at,
                        finished_at=timed_out_at,
                    )
                    dag.mark_skipped(slug)
                continue

            for slug, fut in list(active.items()):
                if fut not in done_futs:
                    continue
                try:
                    task, result, elapsed, t0, t1 = fut.result()  # type: ignore[misc]
                except Exception as exc:
                    _log(f"ERROR {slug}: worker thread raised {type(exc).__name__}: {exc}")
                    del active[slug]
                    story_deadlines.pop(slug, None)
                    story_wait_started.discard(slug)
                    stop_events.pop(slug, None)
                    spec_str = slug_to_spec[slug]
                    failed_at = datetime.datetime.now(datetime.timezone.utc)
                    snapshot = _snapshot_last_known(slug, _state_writer)
                    last_phase = snapshot["last_phase"]
                    if slug in story_times:
                        story_started_at = story_times[slug][0]
                    elif snapshot["last_started_at"] is not None:
                        story_started_at = snapshot["last_started_at"]
                    else:
                        story_started_at = failed_at
                    _phase_label = f" during phase {last_phase}" if last_phase else ""
                    _exc_state = _terminal_state_for(
                        slug,
                        config=config,
                        sprint_name=resolved.name,
                        started_at=story_started_at,
                        error=f"Worker exception{_phase_label}: {exc}",
                        error_type=type(exc).__name__,
                    )
                    _exc_result = CoordinatorResult(
                        success=False,
                        phase=Phase.ESCALATE,
                        state=_exc_state,
                        message=f"Worker thread raised {type(exc).__name__}: {exc}",
                    )
                    story_times[slug] = (story_started_at, failed_at)
                    live_telemetry_snapshots[slug] = snapshot
                    # Same attribution as the other two cancellation exits: a
                    # worker that raised on its way out of an auth-breaker
                    # cancellation was killed by the sprint, not by the story.
                    _exc_outcome: StoryOutcome = StoryOutcome.FAILED
                    if slug in auth_cancelled_slugs:
                        auth_cancelled_slugs.discard(slug)
                        _cancel_reason = f"cancelled mid-flight: {auth_circuit_reason}"
                        _mark_story_auth_cancelled(
                            _exc_result, auth_circuit, reason=_cancel_reason
                        )
                        _exc_outcome = StoryOutcome.SKIPPED
                        _log(f"SKIPPED {slug} ({_cancel_reason})")
                    results.append((spec_str, _exc_result))
                    _write_story_audit(
                        config,
                        slug_to_context[slug][0],
                        _exc_result,
                        sprint_id=_sprint_id,
                        telemetry_snapshot=snapshot,
                    )
                    _set_outcome(
                        slug,
                        _exc_outcome,
                        phase="ESCALATE",
                        last_phase=last_phase,
                        detail_updates={"gate_status": GATE_STATUS_INCOMPLETE},
                    )
                    _persist_current_story_result(
                        slug,
                        _exc_result,
                        started_at=story_started_at,
                        finished_at=failed_at,
                    )
                    dag.mark_skipped(slug)
                    continue
                del active[slug]
                story_deadlines.pop(slug, None)
                story_wait_started.discard(slug)
                stop_events.pop(slug, None)
                story_times[slug] = (t0, t1)

                with cost_lock:
                    # The budget can only ever be enforced against measured
                    # spend. Record the shortfall alongside it so the dispatch
                    # check knows the running total is a lower bound (#1992).
                    if result.state.total_cost_measured is None:
                        unmeasured_spend.append(slug)
                    accumulated_cost += result.state.total_cost

                spec_str = slug_to_spec[slug]
                results.append((spec_str, result))

                spec_cost = result.state.total_cost_measured
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
                        if stopped_reason is None:
                            stopped_reason = (
                                f"Agent authentication failed ({auth_circuit_reason}); "
                                "remaining stories skipped — every subsequent call would "
                                "present the same rejected credential"
                            )
                        _log(f"HALT sprint: {stopped_reason}")
                        # Stop in-flight workers at their next phase boundary and
                        # release any plan gate they are parked on, so the sprint
                        # ends in seconds rather than at the worker timeout.
                        # Remember which slugs WE cancelled: their results come
                        # back through the timeout-oriented cancellation path,
                        # which would otherwise hand them a story failure verdict
                        # for a substrate outage (#1951).
                        for _pending_slug, _pending_evt in stop_events.items():
                            auth_cancelled_slugs.add(_pending_slug)
                            _pending_evt.set()
                        for _gate_slug, _pending_gate in plan_gates.items():
                            _log(f"Releasing plan gate for {_gate_slug} (auth abort)")
                            _pending_gate.set()
                        plan_gates.clear()

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
                    _log(f"SKIPPED {slug} ({_cancel_reason})")
                    _record_current_story_entry(slug, "SKIPPED", error=_cancel_reason)
                    _set_outcome(slug, StoryOutcome.SKIPPED, reason=_cancel_reason)
                    if _state_writer is not None:
                        _state_writer.update(slug, status="skipped")
                    dag.mark_skipped(slug)
                    _write_story_audit(config, task, result, sprint_id=_sprint_id)
                    _print_worker_status(active, worker_phases, dag, total)
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
                if _state_writer is not None and _done_status == "waiting":
                    _state_writer.update(
                        slug,
                        status=_done_status,
                        phase=result.phase.name,
                        cost_usd=_story_reported_cost(result.state),
                    )

                _classify_outcome = _classify_and_record(
                    task, result, dag, merged_slugs, story_state=_story_state
                )
                _terminal_model = _terminal_story_model(result)
                _outcome_fields: dict[str, object] = {
                    "phase": result.phase.name,
                    "cost_usd": _story_reported_cost(result.state),
                }
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
                    _existing_entry = _story_state.get(task.slug)
                    _existing_detail = (
                        dict(_existing_entry.detail) if _existing_entry is not None else {}
                    )
                    _existing_detail["outcome_source"] = "preflight_verdict"
                    _outcome_fields["detail"] = _existing_detail
                _set_outcome(task.slug, _classify_outcome, **_outcome_fields)
                _persist_current_story_result(
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
                    integrated = _attempt_integration(slug, task, result)
                    if not integrated:
                        pending_integration[slug] = (task, result)
                    elif result.landing_status == "failed":
                        # Optimistic classify recorded this as DONE; landing
                        # failed — correct the canonical outcome (terminal-to-
                        # terminal correction is permitted).
                        _merge_info = result.merge if isinstance(result.merge, dict) else {}
                        _failed_outcome = landing_failure_outcome(_merge_info)
                        _set_outcome(slug, _failed_outcome, phase=result.phase.name)
                    changed = True
                    while changed:
                        changed = False
                        for pending_slug, (pending_task, pending_result) in list(
                            pending_integration.items()
                        ):
                            if _attempt_integration(pending_slug, pending_task, pending_result):
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
                                        pending_slug,
                                        _pending_failed_outcome,
                                        phase=pending_result.phase.name,
                                    )
                                changed = True
                else:
                    _write_story_audit(config, task, result, sprint_id=_sprint_id)

                # Fire StorySource lifecycle callbacks
                ctx = slug_to_context.get(slug)
                if ctx:
                    _ctx_task, source, _ctx_ref = ctx
                    if result.success:
                        try:
                            source.on_complete(task, result, config)
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
                                source.on_escalate(task, result.state, config)
                            except Exception as exc:
                                _log(f"WARN on_escalate callback failed for {slug}: {exc}")

                _print_worker_status(active, worker_phases, dag, total)

            # ── Overlap detection: check plan gates ────────────────────
            if use_plan_gates:
                _release_plan_gates(plan_done, file_footprints, plan_gates, active, phase_lock)

    if queued_prs:
        for slug, (task, result, pr_url) in list(queued_prs.items()):
            poll_result = _poll_queued_pr(
                pr_url,
                config.project_root,
                config.workspace.merge_wait_timeout_seconds,
                base_branch=config.workspace.base_branch,
            )
            if poll_result["status"] == "merged":
                merged_slugs.add(slug)
                dag.mark_complete(slug)
                result.landing_status = "landed"
                _set_outcome(slug, StoryOutcome.DONE, landed=True)
            else:
                from ..coordinator.completion import (  # noqa: PLC0415
                    mark_merge_failed as _mark_mf,
                )

                _err = _queued_pr_failure_message(
                    poll_result, pr_url, config.workspace.merge_wait_timeout_seconds
                )
                _mark_mf(result.state, result, _err, result.state.branch_name)
                _set_outcome(slug, StoryOutcome.MERGE_FAILED, phase=result.phase.name)
                _log(f"✗ {slug}: queued PR {poll_result['status']} during sprint wrap-up")
            _write_story_audit(config, task, result, sprint_id=_sprint_id)
            del queued_prs[slug]

    finished_at = datetime.datetime.now(datetime.timezone.utc)
    set_worker_slug("")
    duration = (finished_at - started_at).total_seconds()

    # ── Terminalize live state ────────────────────────────────────────
    # The work loop is over, so nothing in this sprint can still advance. Write
    # that fact to the live .state file BEFORE summary/audit finalization: those
    # steps can take a while (and can raise), and until the terminal transition
    # lands, `forge status` keeps rendering a running sprint next to stories it
    # already reports as failed (#2013). The file is removed further down on the
    # normal path; this is what a crash or a stop in between now finds.
    if _state_writer is not None:
        _stranded = _state_writer.terminalize_stories(
            outcome=StoryOutcome.FAILED,
            reason=stopped_reason or "sprint ended before this story reached a verdict",
        )
        if _stranded:
            _log(
                "Terminalized "
                f"{len(_stranded)} story/stories still non-terminal at sprint end: "
                + ", ".join(_stranded)
            )
        _terminal_counts = _story_state.counts()
        if stopped_reason is not None:
            _terminal_sprint_phase = SPRINT_PHASE_STOPPED
        elif _terminal_counts["failed"]:
            _terminal_sprint_phase = SPRINT_PHASE_FAILED
        else:
            _terminal_sprint_phase = SPRINT_PHASE_DONE
        _state_writer.set_phase(_terminal_sprint_phase)

    # Attribute intake remediation agent spend back to each story's canonical
    # cost_usd so per-story sums (used by sprint-summary.yaml) match the
    # SprintResult total. The dev/review cycle's transition() overwrites
    # cost_usd with CoordinatorState.total_cost, so this attribution must
    # happen after the work loop completes — never before.
    def _bump_story_cost(slug: str, extra: float) -> None:
        if extra <= 0.0:
            return
        entry = _story_state.get(slug)
        if entry is None:
            return
        if entry.cost_usd is None:
            # Unknown + known is still unknown: adding measured intake spend to a
            # cost-unknown story must not turn it into a confident figure (#1992).
            return
        _story_state.transition(slug, cost_usd=entry.cost_usd + extra)

    for _slug, _outcome in (intake_outcomes or {}).items():
        _bump_story_cost(_slug, _intake_outcome_cost(_outcome))
    for _issue_num, _outcome in (entry_intake_outcomes or {}).items():
        _bump_story_cost(f"issue-{_issue_num}", _intake_outcome_cost(_outcome))

    # Re-attach pre-restart spend the canonical state is no longer holding, so
    # the summary total (which sums that state) matches SprintResult's
    # accumulated + prior_cost. Runs here, after the work loop, for the same
    # reason the intake attribution above does: transition() overwrites
    # cost_usd with the coordinator's current-generation total while a story
    # is running, so any earlier attribution would be discarded.
    _carried_by_slug: dict[str, float] = dict(unseeded_prior_story_cost)
    for _seed_slug, _seed_cost in seeded_prior_story_cost.items():
        # A seeded cost only needs restoring when the story re-ran; otherwise
        # the seed is still intact and adding it would double-count.
        if _seed_slug in ran_this_generation:
            _carried_by_slug[_seed_slug] = _carried_by_slug.get(_seed_slug, 0.0) + _seed_cost
    for _carried_slug, _carried_cost in _carried_by_slug.items():
        _bump_story_cost(_carried_slug, _carried_cost)
    if _carried_by_slug:
        _carried_total = sum(_carried_by_slug.values())
        _carried_detail = ", ".join(f"{s}=${c:.4f}" for s, c in sorted(_carried_by_slug.items()))
        _log(
            f"Re-attached pre-restart spend to {len(_carried_by_slug)} story/stories: "
            f"${_carried_total:.4f} ({_carried_detail})"
        )

    final_cost = accumulated_cost + prior_cost
    # A sprint total is only a total when every story's cost was measured. When
    # any story ran on a transport that reported no cost, ``final_cost`` is a
    # measured lower bound and every surface must say so rather than present it
    # as the sprint's cost (#1992). Intake remediation spends the same budget
    # outside any story's CoordinatorState, so its unmeasured passes count too.
    _cost_complete = not unmeasured_spend and all(
        e.cost_usd is not None for e in _story_state.stories()
    )
    # Banner, summary, notifications, and SprintResult all project from the
    # same canonical structure — by construction they cannot disagree.
    _canonical_counts = _story_state.counts()
    specs_succeeded = _canonical_counts["succeeded"]
    specs_failed = _canonical_counts["failed"]
    specs_skipped = _canonical_counts["skipped"]
    # Canonical total: include canonical-only stories (shape-gate skips,
    # closed-at-fetch ALREADY_DONE, etc.) so SprintResult/banner/summary
    # /notifications all report the same total.
    canonical_total = _canonical_counts["total"] or total
    sprint_result = SprintResult(
        name=resolved.name,
        specs_total=canonical_total,
        specs_succeeded=specs_succeeded,
        specs_failed=specs_failed,
        specs_skipped=specs_skipped,
        total_cost_usd=final_cost,
        budget_usd=resolved.budget_usd,
        cost_complete=_cost_complete,
        unmeasured_spend_sources=tuple(unmeasured_spend),
        results=results,
        stopped_reason=stopped_reason,
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
    _sprint_outcome = "done" if specs_failed == 0 and stopped_reason is None else "partial"
    _sprint_logger.emit(
        "run_end",
        outcome=_sprint_outcome,
        total_cost_usd=round(final_cost, 6) if _cost_complete else None,
        total_cost_measured_usd=round(final_cost, 6),
        total_duration_s=round(_sprint_elapsed, 2),
    )
    if notify:
        # Notifications project from canonical counts/total so every
        # operator surface reports the same numbers by construction.
        if config.notifications.backend != "none":
            _notify(
                f"TheForge: {resolved.name}",
                (
                    f"✓ {specs_succeeded} passed, ✗ {specs_failed} failed, "
                    f"⊘ {specs_skipped} skipped"
                ),
            )
        if config.notifications.ntfy is not None:
            _ntfy_title = f'TheForge: sprint done \u2014 "{resolved.name}"'
            _ntfy_body_lines = [
                (
                    f"{canonical_total} stories: {specs_succeeded} succeeded "
                    f"\u00b7 {specs_failed} failed \u00b7 {specs_skipped} skipped"
                ),
                f"Total cost: {_sprint_cost_str}   Duration: {_sprint_dur}",
            ]
            if stopped_reason:
                _ntfy_body_lines.append(f"Stopped: {stopped_reason}")
            _ntfy_publish(
                config.notifications.ntfy.url,
                _ntfy_title,
                "\n".join(_ntfy_body_lines),
                priority=config.notifications.ntfy.priority,
            )
        if config.notifications.backend not in ("ntfy", "none"):
            from ..notify_backends import send_notifications

            _sc_title = f'TheForge sprint complete \u2014 "{resolved.name}"'
            _sc_body_lines = [
                (
                    f"{canonical_total} stories: {specs_succeeded} succeeded "
                    f"\u00b7 {specs_failed} failed \u00b7 {specs_skipped} skipped"
                ),
                f"Total cost: {_sprint_cost_str}   Duration: {_fmt_duration(_sprint_elapsed)}",
            ]
            if stopped_reason:
                _sc_body_lines.append(f"Stopped: {stopped_reason}")
            send_notifications(config, _sc_title, "\n".join(_sc_body_lines))

    # Build slug map and canonical_refs for audit writers
    slug_map: dict[str, str] = {ctx[2]: slug for slug, ctx in slug_to_context.items()}
    canonical_refs = [ctx[2] for ctx in slug_to_context.values()]

    # Write sprint-audit.yaml (existing format; kept for backward compatibility)
    _write_sprint_audit(
        manifest=resolved,
        result=sprint_result,
        canonical_refs=canonical_refs,
        started_at=started_at,
        finished_at=finished_at,
        duration=duration,
        project_root=config.project_root,
        story_times=story_times,
        batch_assignments=batch_assignments,
        slug_map=slug_map,
        tasks_by_slug={slug: ctx[0] for slug, ctx in slug_to_context.items()},
        ci_break_slug=ci_halt_slug,
        sprint_id=_sprint_id,
        dropped_slugs=_dropped_slugs,
        skipped_issues=skipped_issues,
        current_story_entries_by_ref=current_story_entries_by_ref,
        triage_actions_by_ref={
            canonical_ref: triage.action for canonical_ref, triage in triages.items()
        },
        run_id=run_id,
        live_telemetry_snapshots=live_telemetry_snapshots,
    )

    # Write sprint-summary.yaml to .forge/logs/<sprint-name>/
    if _sprint_log_dir is not None:
        _write_sprint_summary(
            manifest=resolved,
            result=sprint_result,
            canonical_refs=canonical_refs,
            started_at=started_at,
            finished_at=finished_at,
            duration=duration,
            sprint_log_dir=_sprint_log_dir,
            story_times=story_times,
            batch_assignments=batch_assignments,
            slug_map=slug_map,
            run_id=run_id,
            tasks_by_slug={slug: ctx[0] for slug, ctx in slug_to_context.items()},
            ci_break_slug=ci_halt_slug,
            sprint_id=_sprint_id,
            project_root=config.project_root,
            dropped_slugs=_dropped_slugs,
            skipped_issues=skipped_issues,
            triage_actions_by_ref={
                canonical_ref: triage.action for canonical_ref, triage in triages.items()
            },
            current_story_entries_by_ref=current_story_entries_by_ref,
            story_state=_story_state,
            config=config,
            live_telemetry_snapshots=live_telemetry_snapshots,
        )

        # Eagerly generate sprint-rca.yaml when any story finished non-DONE.
        # The RCA engine is a pure function over the artifacts just written
        # (sprint-summary.yaml + per-story audit/logs), so it runs off the
        # runner's hot path and stays regenerable via `forge rca`.
        try:
            from .rca import write_sprint_rca

            _rca_path = write_sprint_rca(_sprint_log_dir)
            if _rca_path is not None:
                _log(f"Sprint RCA written: {_rca_path}")
        except Exception as _rca_exc:  # noqa: BLE001 — RCA is best-effort
            _log(f"Warning: sprint RCA generation failed: {_rca_exc}")

    if _state_writer is not None:
        _state_writer.remove()

    # Runs after _state_writer.remove() so sprint state is cleaned up regardless,
    # but the failure is NOT swallowed: a local-only audit commit contaminates
    # every later story PR cut from this checkout, so the sprint must exit
    # nonzero rather than report success over divergent base-branch state.
    from ..coordinator.workspace import _base_branch_tracks_origin

    try:
        _commit_story_run_audits(
            config.project_root,
            config.workspace.base_branch,
            publish=_base_branch_tracks_origin(config, lands_locally=_sprint_lands_locally),
        )
    except RuntimeError as exc:
        _state = getattr(exc, "state", None)
        _state_suffix = (
            f" [state={_state}; recorded in {_STORY_RUN_AUDIT_PUBLISH_STATE_PATH}]"
            if _state
            else ""
        )
        _log(f"✗ SPRINT  canonical story run audit publish failed: {exc}{_state_suffix}")
        raise

    # ── POST_SPRINT hook ──────────────────────────────────────────────
    if config.hooks and config.hooks.post_sprint:
        from ..coordinator.hooks import build_post_sprint_payload
        from ..coordinator.hooks import run_hook as _run_hook

        _stories = []
        for spec_str, res in results:
            # Derive slug: use workspace_path leaf (set during WORKSPACE phase) or slug_map
            _ws = res.state.workspace_path
            if _ws is not None:
                _slug = _ws.name
            else:
                _slug = slug_map.get(spec_str, Path(spec_str).stem)
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
            sprint_name=resolved.name,
            stories=_stories,
            run_id=_sprint_run_id,
            config=config,
            total_cost_usd=final_cost if _cost_complete else None,
            duration_seconds=_sprint_elapsed,
        )
        _run_hook(
            config.hooks.post_sprint,
            _ps_payload,
            config.hooks.timeout_seconds,
            "post_sprint",
            _sprint_logger,
            secrets=config.secrets,
        )

    return sprint_result
