"""PLAN and PLAN_REVIEW phase execution.

Owns everything between PREFLIGHT PROCEED and the DEV loop:
  - Optional story spec validation (advisory pre-PLAN check)
  - Plan agent invocation
  - Plan agent review pool (plan_agent_review config)
  - Human plan review (pending-file, remote, interactive)
  - Plan regeneration with model escalation
  - Advisory plan validation against story criteria

Called from run_task(); returns None to continue to DEV or a
CoordinatorResult to stop early (abandon, escalate, or stop_phase).
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

from theforge import knowledge_receipts
from theforge.artifacts import PLAN_PATH, ensure_parent_dir, plan_paths, resolve_plan_path
from theforge.assignment import (
    MECHANISM_PLAN_MODEL_ESCALATION,
    MECHANISM_RUN_SCOPED_RESET,
)
from theforge.config import (
    DEFAULT_INVESTIGATION_TOOLS,
    MODEL_REGISTRY,
    ForgeConfig,
    ModelProfile,
    apply_model_info,
)
from theforge.config.bridge import model_ref_to_profile
from theforge.config.model_identity import PHASE_PLAN
from theforge.config.profiles import _apply_transport_fallback
from theforge.knowledge_receipts import extract_knowledge_debrief
from theforge.log_level import _LOG_LEVEL, LogLevel
from theforge.plan_finding_classifier import (
    format_provenance,
    match_plan_findings,
    strip_reviewer_prefix,
)
from theforge.review import (
    PlanReviewFinding,
    PlanReviewResult,
    merge_plan_review_results,
    parse_plan_review_output,
    plan_review_findings_to_text,
)
from theforge.reviewer_value import compute_plan_reviewer_uniqueness
from theforge.sessions import save_sessions
from theforge.task import (
    ContextAssembler,
    TaskStory,
    build_plan_prompt,
    build_plan_review_prompt,
    parse_plan_output,
)
from theforge.traces import write_trace

from . import util as _cu
from .agent_failure import (
    NO_JUDGMENT,
    AgentInvocationFailure,
    classify_agent_failure,
    mark_infrastructure_abort,
    produced_model_output,
    record_degraded_pool,
    record_invocation_failure,
)
from .agent_identity import resolved_identity_for_result
from .log_tee import _write_log_artifact
from .notify import (
    _escalate_notify,
    _is_pending_file_mode,
    _is_remote_mode,
    _plan_review_interactive,
)
from .pending_hitl import _pending_plan_review
from .plan_trajectory import (
    build_disposition_context,
    build_filtered_regen_findings,
    record_plan_attempt,
)
from .preflight import _escalate_dev_model, _find_registry_key_for_profile
from .preflight_evidence import render_partial_evidence
from .remote_gates import _plan_review_remote
from .resume_persistence import save_resume_record
from .reviewer_progress import ReviewerProgressChannel
from .state import CoordinatorResult, CoordinatorState, Phase, PlanFindingRecord
from .util import (
    _fmt_cost,
    _fmt_duration,
    _log_phase,
    _round_cost,
    resolve_timeout_with_active,
    sum_costs,
)

if TYPE_CHECKING:
    from theforge.agent_types import AgentResult
    from theforge.coordinator.logging import StructuredLogger

_log = _cu._log
_log_verbose = _cu._log_verbose

_TRANSIENT_PLAN_REVIEW_ERROR_PATTERNS = (
    "429",
    "rate limit",
    "rate-limited",
    "resource_exhausted",
    "resource exhausted",
    "quota exceeded",
    "quota_exceeded",
    "500",
    "502",
    "503",
    "504",
    "internal error",
    "server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "connection reset",
    "connection-reset",
    "econnreset",
    "connection aborted",
    "connection refused",
    "peer closed connection",
    "temporarily unavailable",
    "try again later",
    "timeout awaiting headers",
)

# Transient signatures for the PLAN draft/regen agent call. Extends the
# plan-review set with connection-closed/mid-stream-disconnect wordings — the
# exact failure that killed sprint 065a91f3caf1 was an empty plan output whose
# only line was "API Error: Connection closed mid-response. The response above
# may be incomplete." (#1672), which none of the review patterns above match.
_TRANSIENT_PLAN_ERROR_PATTERNS = (
    *_TRANSIENT_PLAN_REVIEW_ERROR_PATTERNS,
    "connection closed",
    "connection-closed",
    "closed mid-response",
    "response above may be incomplete",
    "response may be incomplete",
    "mid-stream disconnect",
    "stream disconnected",
    "stream idle timeout",
    "partial response received",
)

# Initial backoff (seconds) before a transient plan retry; doubled per retry.
_PLAN_TRANSPORT_RETRY_BACKOFF_BASE_SECONDS = 2


# ── Lazy runner slots ─────────────────────────────────────────────────
# None until first call; tests may replace before calling run_task.
# Patch targets:
#   theforge.coordinator.plan_flow.run_agent      — plan agent + regen
#   theforge.coordinator.plan_flow.run_agent_pool — plan review pool
run_agent = None
run_agent_pool = None


def _ensure_runners() -> None:
    global run_agent, run_agent_pool
    if run_agent is not None and run_agent_pool is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if run_agent_pool is None:
        run_agent_pool = _r.run_agent_pool


def _is_transient_plan_review_failure(result: "AgentResult") -> bool:
    """Return True when a failed plan-review invocation looks transient/retryable."""
    if result.success:
        return False
    failure_code = (result.failure_code or "").lower()
    if failure_code in {"rate_limit", "provider_internal_error", "connection_reset"}:
        return True
    output = (result.output or "").lower()
    return any(pattern in output for pattern in _TRANSIENT_PLAN_REVIEW_ERROR_PATTERNS)


def _summarize_plan_review_failure(result: "AgentResult") -> str:
    """Produce a compact, audit-friendly failure summary."""
    parts = [f"exit={result.exit_code}"]
    if result.failure_code:
        parts.append(f"failure_code={result.failure_code}")
    output = " ".join((result.output or "").split())
    if output:
        parts.append(output[:200])
    return ": ".join((parts[0], " | ".join(parts[1:]))) if len(parts) > 1 else parts[0]


def _is_transient_plan_failure(result: "AgentResult") -> bool:
    """Return True when a failed PLAN draft/regen invocation looks transient/retryable.

    A connection-closed / empty-output transport failure (exit!=0 with the error
    banner as the only content) is retryable; a genuine planning failure or a
    startup failure (CLI missing) is not.
    """
    if result.success:
        return False
    if getattr(result, "startup_failure", False):
        return False
    failure_code = (result.failure_code or "").lower()
    if failure_code in {"rate_limit", "provider_internal_error", "connection_reset"}:
        return True
    output = (result.output or "").lower()
    return any(pattern in output for pattern in _TRANSIENT_PLAN_ERROR_PATTERNS)


def _plan_transport_retry_backoff_seconds(retry_count: int) -> int:
    """Return the backoff delay before the next transient plan retry."""
    return _PLAN_TRANSPORT_RETRY_BACKOFF_BASE_SECONDS * (2 ** max(retry_count - 1, 0))


def _run_plan_agent_with_retry(
    *,
    prompt: str,
    profile: ModelProfile,
    workspace_path: Path,
    config: ForgeConfig,
    state: CoordinatorState,
    phase_label: str,
    attempt: int,
    session_id: str | None = None,
) -> "AgentResult":
    """Invoke the plan agent, retrying bounded times on transient transport failures.

    Mirrors the DEV (_is_transient_dev_failure) and PLAN_REVIEW
    (_retry_transient_plan_review_failures) resilience: a single connection-closed
    / empty-output transport error must not escalate the story. Retries use a
    fresh session — a resumed session anchors on its own truncated/empty output —
    with exponential backoff, and each retry is recorded in
    state.plan_transport_retries for the audit trail.

    phase_label is "PLAN" (initial draft) or "PLAN_REGEN" (post-review regen);
    it is used only for logging and audit provenance.
    """
    max_retries = max(0, config.retry.max_plan_transport_retries)
    result = run_agent(
        prompt=prompt,
        profile=profile,
        working_dir=workspace_path,
        session_id=session_id,
        secrets=config.secrets,
    )
    retry_count = 0
    while retry_count < max_retries and _is_transient_plan_failure(result):
        retry_count += 1
        _summary = _summarize_plan_review_failure(result)
        state.plan_transport_retries.append(
            {
                "phase": phase_label,
                "attempt": attempt,
                "retry": retry_count,
                "error": _summary,
            }
        )
        _log(
            f"  ↻ {phase_label}   transient transport failure "
            f"(retry {retry_count}/{max_retries}): {_summary}"
        )
        _backoff_s = _plan_transport_retry_backoff_seconds(retry_count)
        _log_verbose(f"  {phase_label} retry backoff: {_backoff_s}s")
        time.sleep(_backoff_s)
        result = run_agent(
            prompt=prompt,
            profile=profile,
            working_dir=workspace_path,
            session_id=None,  # fresh session — prior anchors on its own bad/empty output
            secrets=config.secrets,
        )
    return result


def _retry_transient_plan_review_failures(
    *,
    prompt: str,
    profiles: list[ModelProfile],
    results: list["AgentResult"],
    state: CoordinatorState,
    config: ForgeConfig,
    workspace_path: Path,
    attempt: int,
    progress_channel: "ReviewerProgressChannel | None" = None,
) -> tuple[list["AgentResult"], list[dict]]:
    """Retry transient plan-review transport failures per reviewer."""
    max_retries = config.retry.max_plan_review_transport_retries
    if max_retries <= 0:
        return results, []

    retried_results = list(results)
    retry_events: list[dict] = []

    for index, (profile, result) in enumerate(zip(profiles, retried_results)):
        if not _is_transient_plan_review_failure(result):
            continue

        retry_count = 0
        current = result
        while retry_count < max_retries and _is_transient_plan_review_failure(current):
            state.plan_review_results.append(current)
            retry_count += 1
            _log(
                f"  ↻ PLAN_REVIEW   {profile.name} transient transport failure "
                f"(retry {retry_count}/{max_retries})"
            )
            if progress_channel is not None:
                progress_channel.set_retry(profile.name, retry_count, max_retries)
            retried = run_agent(
                prompt=prompt,
                profile=profile,
                working_dir=workspace_path,
                quiet=True,
                secrets=config.secrets,
                session_id=current.session_id if profile.mode == "cli" else None,
                progress_cb=progress_channel.cb if progress_channel is not None else None,
            )
            if retried.session_id and produced_model_output(retried):
                state.plan_review_session_ids[profile.name] = retried.session_id
            _write_log_artifact(
                state.log_dir,
                f"plan-review/attempt-{attempt}/{profile.name}-retry{retry_count}.yaml",
                retried.output or "",
                owner_run_id=state.run_id,
            )
            retry_events.append(
                {
                    "attempt": attempt,
                    "reviewer": profile.name,
                    "retry": retry_count,
                    "error": _summarize_plan_review_failure(current),
                }
            )
            current = retried

        # A resolved retry marks the reviewer done (also clears the ↻rN/M glyph)
        # so the pool-done count stays accurate.
        if retry_count > 0 and current.success and progress_channel is not None:
            progress_channel.cb({"label": profile.name, "done": True})
        retried_results[index] = current

    return retried_results, retry_events


def _plan_review_parse_errors(result: "AgentResult") -> list[str]:
    """Return parse-error strings for a reviewer completion, or [] if it parses.

    Only successful-at-transport completions are eligible: transport failures are
    owned by _retry_transient_plan_review_failures and must not be double-counted
    here as parse failures.
    """
    if not result.success:
        return []
    parsed = parse_plan_review_output(result.output or "")
    return [str(e) for e in parsed.parse_errors]


# Corrective YAML structure appended to every plan-review parse-retry prompt.
# Mirrors review_pool._CORRECTIVE_YAML_STRUCTURE for the plan-review schema
# (verdict/summary/criteria_coverage/findings) so the reviewer sees the exact
# shape parse_plan_review_output requires.
_PLAN_REVIEW_CORRECTIVE_YAML_STRUCTURE = (
    "verdict: APPROVE | REJECT\n"
    'summary: "<one-line summary of your review>"\n'
    "criteria_coverage:\n"
    '  - criterion: "<acceptance criterion text from the spec>"\n'
    "    covered: true | false\n"
    "    plan_section: \"<which part of the plan addresses this, or 'missing'>\"\n"
    "findings:\n"
    "  - severity: P0 | P1 | P1-impl | P2\n"
    '    description: "<what is wrong with the plan>"\n'
    '    suggestion: "<how to fix it>"\n'
)


def _build_plan_review_retry_prompt(error_desc: str, original_output: str) -> str:
    """Build a corrective retry prompt that anchors the reviewer's prior content.

    Mirrors review_pool._build_review_retry_prompt: the prior output's CONTENT
    (verdict + findings) is correct, only its YAML ENCODING failed parsing.
    Handing the reviewer its own rejected output plus the specific parse
    objections lets it repair the emission instead of re-deriving the review
    from scratch (#2065).
    """
    return (
        "Your previous plan review output failed schema/parse validation:\n" + error_desc + "\n\n"
        "The CONTENT of your previous review is correct — your verdict and your "
        "findings are what you meant to say. ONLY the ENCODING (YAML formatting) "
        "is broken. Re-emit the SAME review with the formatting error fixed.\n\n"
        "Preserve your previous output verbatim:\n"
        "  - Keep the SAME verdict. Do NOT switch APPROVE <-> REJECT.\n"
        "  - Keep EVERY finding. Do NOT drop, merge, or downgrade a finding.\n\n"
        "Dropping or altering a field to make a validation error go away is a "
        "WRONG response. If a value failed to parse, fix the QUOTING/ESCAPING of "
        "that value — do NOT delete the value, and do NOT change the verdict to "
        "sidestep a cross-validation rule.\n\n"
        "Do NOT re-review the plan.\n\n"
        "Required YAML structure:\n"
        + _PLAN_REVIEW_CORRECTIVE_YAML_STRUCTURE
        + "\n\nYour previous output (fix its formatting, keep its content):\n"
        + original_output
    )


# Parse-error prefixes that mean the prior output never carried structured
# plan-review content to anchor a corrective prompt on — the YAML extraction
# itself failed, or the root wasn't even a mapping. Distinct from errors that
# fire once a mapping WAS parsed (bad verdict value, malformed finding, etc.),
# which are repairable.
_NON_REPAIRABLE_PARSE_ERROR_PREFIXES = (
    "YAML parse error",
    "Plan review output root is not a YAML mapping",
)


def _plan_review_output_is_repairable(output: str | None, errors: list[str]) -> bool:
    """Return True when a parse-failed plan review has content worth repairing.

    A blank/whitespace-only response, or one whose YAML never parsed to a
    mapping in the first place, has nothing for a corrective prompt to anchor
    on — recovery for those must fall back to a full cold re-review of the
    original task rather than repairing unusable text (#2065).
    """
    if not output or not output.strip():
        return False
    return not any(
        error.startswith(prefix)
        for error in errors
        for prefix in _NON_REPAIRABLE_PARSE_ERROR_PREFIXES
    )


def _retry_parse_failed_plan_reviews(
    *,
    prompt: str,
    profiles: list[ModelProfile],
    results: list["AgentResult"],
    state: CoordinatorState,
    config: ForgeConfig,
    workspace_path: Path,
    attempt: int,
) -> tuple[list["AgentResult"], list[dict]]:
    """Retry plan reviewers whose completion succeeded but produced unparseable output.

    A reviewer that returns success at the transport level but emits prose or a
    non-mapping YAML root is a transient *agent* failure, not a story failure. The
    transport-retry path (_retry_transient_plan_review_failures) never reaches these
    because it gates on result.success == False. Mirror it for the success-but-
    unparseable case: re-invoke that specific reviewer up to
    max_plan_review_parse_retries times before the minimum-reviewers gate is
    evaluated.

    When the prior output has usable content, the retry prompt hands the reviewer
    its own rejected output together with the specific parse objections and asks
    for a corrected emission (#2065) — the reviewer has already done the expensive
    work, so recovery repairs the encoding rather than re-deriving the review
    from scratch. Session continuity is kept for CLI-mode reviewers (the explicit
    anchor in the prompt makes relying on session memory alone unnecessary, but
    reusing the session when available is strictly more informative).

    When the prior output is blank or never parsed to a YAML mapping at all
    (`_plan_review_output_is_repairable` returns False), there is nothing to
    repair — the retry falls back to the original task prompt on a fresh
    session, the same cold re-review the transport-retry path performs.

    Each pre-retry result is appended to state.plan_review_results so cost and the
    per-reviewer audit reconstruction stay consistent with the transport path.
    """
    max_retries = config.retry.max_plan_review_parse_retries
    if max_retries <= 0:
        return results, []

    retried_results = list(results)
    retry_events: list[dict] = []

    for index, (profile, result) in enumerate(zip(profiles, retried_results)):
        errors = _plan_review_parse_errors(result)
        if not errors:
            continue

        retry_count = 0
        current = result
        while retry_count < max_retries and errors:
            state.plan_review_results.append(current)
            retry_count += 1
            error_desc = "; ".join(errors)
            repairable = _plan_review_output_is_repairable(current.output, errors)
            if repairable:
                _log(
                    f"  ↻ PLAN_REVIEW   {profile.name} unparseable output "
                    f"(parse retry {retry_count}/{max_retries}): {error_desc[:120]}"
                )
                retry_prompt = _build_plan_review_retry_prompt(error_desc, current.output or "")
                retry_session_id = current.session_id if profile.mode == "cli" else None
            else:
                _log(
                    f"  ↻ PLAN_REVIEW   {profile.name} non-repairable output — cold retry "
                    f"(parse retry {retry_count}/{max_retries}): {error_desc[:120]}"
                )
                retry_prompt = prompt
                retry_session_id = None  # fresh session — nothing usable to anchor on
            retried = run_agent(
                prompt=retry_prompt,
                profile=profile,
                working_dir=workspace_path,
                quiet=True,
                secrets=config.secrets,
                session_id=retry_session_id,
            )
            if retried.session_id and produced_model_output(retried):
                state.plan_review_session_ids[profile.name] = retried.session_id
            _write_log_artifact(
                state.log_dir,
                f"plan-review/attempt-{attempt}/{profile.name}-parse-retry{retry_count}.yaml",
                retried.output or "",
                owner_run_id=state.run_id,
            )
            retry_events.append(
                {
                    "attempt": attempt,
                    "reviewer": profile.name,
                    "retry": retry_count,
                    "errors": list(errors),
                    "repair": repairable,
                }
            )
            current = retried
            errors = _plan_review_parse_errors(current)

        if retry_count:
            if errors:
                _log(f"  ✗ PLAN_REVIEW   {profile.name} parse retries exhausted")
            else:
                _log(f"  ✓ PLAN_REVIEW   {profile.name} parse retry {retry_count} succeeded")
        retried_results[index] = current

    return retried_results, retry_events


def _clean_stale_plan_files(workspace_path: Path) -> None:
    """Remove stale plan artifacts before starting a fresh PLAN phase."""
    for plan_path in plan_paths(workspace_path):
        if plan_path.exists():
            plan_path.unlink()

    traces_dir = workspace_path / ".forge" / "traces"
    if traces_dir.exists():
        for stale_trace in traces_dir.glob("plan-attempt-*.txt"):
            stale_trace.unlink()


def _maybe_escalate_decompose(
    state: CoordinatorState,
    task: TaskStory,
    plan_result: "AgentResult",
    plan_elapsed: float,
    notify: bool,
    config: ForgeConfig,
    logger: "StructuredLogger | None",
) -> CoordinatorResult | None:
    """Escalate when the planner signalled the story is too large to plan.

    The planner emits ``decompose: true`` (with an optional ``decompose_reason``)
    when the story bundles independent deliverables that cannot be implemented as
    a single unit. This escalates the story for a human to split manually — the
    coordinator does NOT auto-split (auto-decomposition is the separate #28 effort).

    Returns a terminal CoordinatorResult when the signal is present, or None to
    continue the normal PLAN → PLAN_REVIEW → DEV flow.
    """
    if not (state.plan_structured and state.plan_structured.get("decompose") is True):
        return None

    _reason = str(state.plan_structured.get("decompose_reason") or "").strip()
    state.phase = Phase.ESCALATE
    state.escalate_kind = "decompose"
    state.escalate_reason = (
        _reason or "Planner signalled the story is too large to plan as one unit."
    )
    state.error = (
        "PLAN signalled DECOMPOSE_NEEDED — the story is too large to plan and "
        "implement as a single unit. Split it into smaller runnable stories; the "
        "coordinator does not auto-split."
    )
    if _reason:
        state.error += f" Planner reason: {_reason}"
    _log(f"  ⚑ PLAN   decompose signalled after {_fmt_duration(plan_elapsed)}")
    _log(f"✗ ESCALATE   {state.error}")
    if logger:
        logger._safe_emit("phase_end", phase="PLAN", outcome="escalate")
        logger._safe_emit("escalate", reason=state.error, phase="PLAN", escalate_kind="decompose")
    _escalate_notify(task, state, notify, config)
    return CoordinatorResult(
        success=False,
        phase=state.phase,
        state=state,
        message=state.error,
    )


def _apply_post_plan_dev_checkpoint(
    state: CoordinatorState,
    config: "ForgeConfig",
    plan_review_decision: str = "APPROVE",
) -> None:
    """Re-evaluate ONLY the dev tier after a plan-review that proceeds to DEV (#1387).

    Shared by every plan-review path that continues to DEV — the agent
    plan-review APPROVE branch, the human/pending-file/remote APPROVE branch, and
    the refactor advisory branch (which proceeds regardless of verdict) — so the
    post-plan checkpoint is always recorded. ``plan_review_decision`` carries the
    real merged verdict so a non-APPROVE advisory continuation records
    ``preserve``/``plan_review_not_approve`` rather than leaving the pending
    sentinel. All gate conditions (including verdict == APPROVE) are enforced
    inside :func:`apply_post_plan_checkpoint`; even a non-firing decision records
    the audit block. Must be called AFTER ``record_plan_attempt()`` so the latest
    per-attempt p1/p2 counts are available.
    """
    _adaptive = getattr(state, "_adaptive_decision", None)
    if _adaptive is None:
        return
    from theforge.assignment import apply_post_plan_checkpoint  # noqa: PLC0415

    # Rerank the demoted-tier pick on the same recency-weighted success-rate and
    # domain signals the preflight dev assignment used — only under adaptive
    # routing, mirroring assign_models (static mode ignores profile learning).
    _adaptive_enabled = config.assignment.adaptive_enabled
    # Demonstrated-capability record (#2466): loaded OUTSIDE the adaptive guard.
    # It is not a learning signal that static routing may ignore — it is a hard
    # eligibility fact about the identity, so the post-plan reroute honors it in
    # both modes.
    from theforge.model_capabilities import capabilities_path, load_capabilities  # noqa: PLC0415

    _capability_records = load_capabilities(capabilities_path(config.project_root))
    _model_profiles = None
    _observed_costs = None
    _recency = None
    _domains = None
    _reasoning_effort = None
    if _adaptive_enabled:
        from theforge.assignment import _requested_dev_reasoning_effort  # noqa: PLC0415
        from theforge.coordinator.audit_substrate import (  # noqa: PLC0415
            load_observed_cost_cohorts,
        )
        from theforge.model_profiles_storage import load_profiles  # noqa: PLC0415

        _model_profiles = load_profiles(config.project_root / ".forge" / "model_profiles.yaml")
        _observed_costs, _ = load_observed_cost_cohorts(config.project_root)
        _recency = config.assignment.recency
        _domains = list(state.preflight_domains or [])
        _reasoning_effort = _requested_dev_reasoning_effort(
            state.preflight_complexity_score,
            config.assignment.reasoning_effort,
        )

    _latest_meta = state.plan_attempt_metadata[-1] if state.plan_attempt_metadata else {}
    _updated = apply_post_plan_checkpoint(
        _adaptive,
        config.agents,
        config.assignment,
        state.preflight_complexity or "medium",
        plan_review_decision=plan_review_decision,
        plan_review_cycles=state.plan_regen_count + 1,
        p1_count=int(_latest_meta.get("p1_count", 0)),
        p2_count=int(_latest_meta.get("p2_count", 0)),
        explicit_roles=getattr(state, "_explicit_roles", set()),
        secrets=config.secrets,
        model_profiles=_model_profiles,
        observed_costs=_observed_costs,
        reasoning_effort=_reasoning_effort,
        domains=_domains,
        recency=_recency,
        transport_fallbacks=config.transport_fallbacks,
        capability_records=_capability_records,
    )
    state._adaptive_decision = _updated
    if _updated.routing_decision:
        state.routing_decision = _updated.routing_decision
    _cp = (_updated.routing_decision.get("dev") or {}).get("post_plan_checkpoint") or {}
    if _cp.get("fired"):
        _log(
            f"  ↳ post-plan checkpoint: dev {_cp.get('baseline_tier')} → "
            f"{_cp.get('final_tier')} ({_updated.dev.model}) — {_cp.get('rationale')}"
        )


def _record_plan_model_escalation(
    state: CoordinatorState,
    *,
    previous_model: str,
    escalated_model: str,
    rejections: int,
    findings: str,
) -> None:
    """Attach the in-run plan-model escalation to the canonical routing_decision.

    Mirrors ``review_phase._record_persistent_p1_dev_escalation`` for the planner
    role (ADR-0006 clause 7 / #1391): a telemetry log line is not enough — the
    mechanism that fired, the consecutive-rejection signal, and the planner model
    swap must all be reconstructable from the audit's ``routing_decision`` block.
    ``scope``/``return_path`` record clause-5 symmetry: this escalation is
    story-scoped and reset by the next story's fresh ``CoordinatorState``.
    """
    if not isinstance(state.routing_decision, dict):
        state.routing_decision = {"origin": "preflight"}

    planner_block = state.routing_decision.get("planner")
    if not isinstance(planner_block, dict):
        planner_block = {}
        state.routing_decision["planner"] = planner_block

    planner_block["plan_model_escalation"] = {
        "mechanism": MECHANISM_PLAN_MODEL_ESCALATION,
        "fired": True,
        "scope": "run",
        "return_path": MECHANISM_RUN_SCOPED_RESET,
        "signal": {
            "kind": "consecutive_plan_rejections",
            "rejections": rejections,
            "findings": findings,
        },
        "model_swap": {
            "from_model": previous_model,
            "to_model": escalated_model,
        },
    }


def _run_plan_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    story_content: str,
    workspace_path: Path,
    plan_path: Path | None,
    preflight_result: "AgentResult | None",
    *,
    notify: bool,
    logger: "StructuredLogger | None",
    run_id: str | None,
    state_update_fn: "Callable[[dict], None] | None",
) -> CoordinatorResult | None:
    """Run SPEC VALIDATION → PLAN → PLAN_REVIEW for run_task.

    Args:
        plan_path: If not None, plan was injected via --plan; skip spec
            validation and the plan agent invocation.
        preflight_result: AgentResult from the preflight agent, or None if
            preflight was skipped. Used to build the plan prompt.

    Returns:
        None to continue to the DEV loop, or a CoordinatorResult to stop
        early (plan failure, review abandon, escalation).
    """
    _ensure_runners()

    def _persist_plan_review_record() -> None:
        """Write the plan-review outcome to the story's durable phase record.

        Best-effort and control-flow-neutral by contract (#2155): the helper the
        coordinator calls here must never turn a recovery-aid failure into a
        phase failure.
        """
        save_resume_record(
            config.project_root,
            state,
            slug=task.slug,
            story_content=story_content,
            run_id=run_id,
        )

    if plan_path is None:
        _clean_stale_plan_files(workspace_path)

    # ── SPEC VALIDATION ───────────────────────────────────────────────
    _should_validate_spec = (
        plan_path is None
        and config.plan.enabled
        and config.plan.validate_spec
        and state.preflight_complexity in ("medium", "large")
        and state.preflight_sufficiency != "implementation_ready"
    )

    if _should_validate_spec:
        from theforge.story_validator import validate_story  # noqa: PLC0415

        _fast_profile = config.dev_profile
        if "opus" in config.dev_profile.model.lower():
            _sonnet_info = MODEL_REGISTRY.get("anthropic/sonnet/cli")
            if _sonnet_info is not None:
                _fast_profile = apply_model_info(config.dev_profile, _sonnet_info)
        _sv_result = validate_story(
            story_content=story_content,
            profile=_fast_profile,
            working_dir=workspace_path,
            secrets=config.secrets,
            runner=run_agent,
        )

        state.story_validation_result = _sv_result
        if _sv_result.verdict == "WARN":
            _log("⚠  Story validation: WARN — review findings before planning")
            for _finding in _sv_result.findings:
                _log(f"   [{_finding.category}] {_finding.description}")
                if _finding.split_suggestion:
                    _stories = _finding.split_suggestion.get("stories") or []
                    for _story in _stories:
                        _log(f"     → {_story.get('name', '?')}")

    # ── PLAN ──────────────────────────────────────────────────────────
    should_plan = (
        plan_path is None
        and config.plan.enabled
        and state.preflight_complexity in ("medium", "large")
        and state.preflight_sufficiency != "implementation_ready"
    )
    if not should_plan:
        if state.preflight_sufficiency == "implementation_ready":
            _log("  ↩ PLAN   skipped — spec classified as implementation-ready")
        return None

    # Remove stale plan files from prior runs before the plan agent writes a new one
    _cleaned = 0
    for _stale in plan_paths(workspace_path):
        if _stale.exists():
            _stale.unlink()
            _cleaned += 1
    if _cleaned:
        _log(f"  ↺ PLAN   removed {_cleaned} stale plan file(s)")

    state.phase = Phase.PLAN
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "PLAN",
                "iteration": 0,
                "cost_usd": state.total_cost_measured,
                "coordinator_state": state,
                **_cu.live_complexity_fields(
                    state.preflight_complexity, state.preflight_complexity_score
                ),
                "detail": {
                    "plan_attempt": state.plan_regen_count + 1,
                    "plan_max_attempts": config.retry.max_plan_regen_attempts,
                },
            }
        )
    _plan_timeout, _plan_override_active = resolve_timeout_with_active(
        config.plan.timeout,
        config.plan.timeout_medium,
        config.plan.timeout_large,
        state.preflight_complexity,
        state.preflight_complexity_score,
    )
    if _plan_override_active:
        _derived_timeout = False
        if state.preflight_complexity_score is not None:
            if state.preflight_complexity_score >= 8:
                _derived_timeout = config.plan.timeout_large is None
            elif state.preflight_complexity_score >= 6:
                _derived_timeout = config.plan.timeout_medium is None
        elif state.preflight_complexity == "large":
            _derived_timeout = config.plan.timeout_large is None
        elif state.preflight_complexity == "medium":
            _derived_timeout = config.plan.timeout_medium is None
        _source = "derived" if _derived_timeout else "explicit override"
        _detail = f"{state.preflight_complexity} complexity, {_source}"
        _log(f"  Plan timeout: {_plan_timeout}s ({_detail})")
    else:
        _log(f"  Plan timeout: {_plan_timeout}s")

    # Use adaptive planner if available and not explicitly configured
    _adaptive = getattr(state, "_adaptive_decision", None)
    _explicit_roles_now = getattr(state, "_explicit_roles", set())
    if (
        _adaptive is not None
        and _adaptive.planner is not None
        and "planner" not in _explicit_roles_now
    ):
        plan_profile = _adaptive.planner
        plan_profile = dataclasses.replace(plan_profile, timeout_seconds=_plan_timeout)
        _log(f"  [adaptive] planner: {plan_profile.model}")
    else:
        plan_profile = model_ref_to_profile(
            "plan",
            config.plan.ref,
            # Named, not borrowed from preflight: preflight's surface is
            # deliberately narrower (no Bash, #2346) and the plan agent has no
            # reason to inherit that narrowing.
            allowed_tools=DEFAULT_INVESTIGATION_TOOLS,
            phase=PHASE_PLAN,
            timeout_seconds=_plan_timeout,
        )
        plan_profile = _apply_transport_fallback(plan_profile, config.transport_fallbacks)
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "PLAN",
                "iteration": 0,
                "cost_usd": state.total_cost_measured,
                "coordinator_state": state,
                **_cu.live_complexity_fields(
                    state.preflight_complexity, state.preflight_complexity_score
                ),
                "current_model": plan_profile.model,
                "detail": {
                    "plan_attempt": state.plan_regen_count + 1,
                    "plan_max_attempts": config.retry.max_plan_regen_attempts,
                },
            }
        )
    _log_phase(state.phase, plan_profile.model)
    if logger:
        logger._safe_emit("phase_start", phase="PLAN", iteration=0)

    plan_context = ContextAssembler.from_config(config).assemble(
        phase="plan",
        story_text=story_content,
        file_list=state.preflight_likely_files,
        agent_role="plan",
        phase_iteration=state.plan_regen_count + 1,
    )

    state.context_manifests.append({"phase": "plan", "manifest": plan_context})
    plan_prompt = build_plan_prompt(
        task,
        story_content=story_content,
        preflight_output=(
            preflight_result.output if preflight_result and preflight_result.success else None
        ),
        partial_preflight_evidence=render_partial_evidence(state.preflight_partial_evidence),
        assembled_context=plan_context,
        conventions=config.conventions_soft,
        work_type=state.preflight_work_type,
    )

    from .workspace_hygiene import (  # noqa: PLC0415
        check_phase_no_mutation,
        snapshot_porcelain,
    )

    _plan_hygiene_before = snapshot_porcelain(workspace_path)
    _plan_start = time.monotonic()
    plan_result = _run_plan_agent_with_retry(
        prompt=plan_prompt,
        profile=plan_profile,
        workspace_path=workspace_path,
        config=config,
        state=state,
        phase_label="PLAN",
        attempt=0,
    )
    _plan_elapsed = time.monotonic() - _plan_start
    _plan_ok, _plan_diag, _plan_offending = check_phase_no_mutation(
        workspace_path, _plan_hygiene_before, "PLAN"
    )
    state.workspace_hygiene_audit.append(
        {"phase": "PLAN", "ok": _plan_ok, "offending_paths": _plan_offending}
    )
    state.plan_durations.append(_plan_elapsed)
    state.plan_results.append(plan_result)
    state.plan_session_id = plan_result.session_id or state.plan_session_id
    write_trace(workspace_path / ".forge/traces" / "plan-attempt-0.txt", plan_result.output)
    _write_log_artifact(
        state.log_dir,
        "plan-attempt-0.txt",
        plan_result.output or "",
        owner_run_id=state.run_id,
    )
    if not _plan_ok:
        state.phase = Phase.ESCALATE
        state.error = _plan_diag or "PLAN phase mutated the worktree"
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("phase_end", phase="PLAN", outcome="escalate")
            logger._safe_emit("escalate", reason=state.error, phase="PLAN")
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False, phase=state.phase, state=state, message=state.error
        )

    if plan_result.success:
        plan_text = plan_result.output
        worktree_plan_path = workspace_path / PLAN_PATH
        ensure_parent_dir(worktree_plan_path)
        worktree_plan_path.write_text(plan_text, encoding="utf-8")
        state.plan_output = plan_text
        state.plan_structured = parse_plan_output(plan_text)
        # Prior-run knowledge receipt (#2866). Read out of the plan agent's own
        # YAML after the plan is parsed, recorded for audit, and consulted by no
        # branch below: a plan is accepted or regenerated on exactly the grounds
        # it always was.
        state.knowledge_debriefs.append(
            knowledge_receipts.debrief_submission(
                phase="plan",
                agent_role="plan",
                phase_iteration=state.plan_regen_count + 1,
                source="plan_yaml",
                payload=extract_knowledge_debrief(plan_text),
            )
        )

        # Decompose signal: the planner judged the story too large to implement
        # as one unit. Escalate for manual splitting — the coordinator does NOT
        # auto-split (auto-decomposition is the separate #28 effort). This runs
        # before PLAN_REVIEW/DEV so no dev budget is spent on an oversized story.
        _decompose_result = _maybe_escalate_decompose(
            state, task, plan_result, _plan_elapsed, notify, config, logger
        )
        if _decompose_result is not None:
            return _decompose_result

        _log(f"  ✓ PLAN   {_fmt_cost(plan_result.cost_usd)}  {_fmt_duration(_plan_elapsed)}")
        if logger:
            logger._safe_emit(
                "phase_end",
                phase="PLAN",
                outcome="success",
                cost_usd=_round_cost(plan_result.cost_usd),
                duration_s=round(_plan_elapsed, 2),
            )

        _work_type = state.preflight_work_type or "feature"

        if _work_type == "mechanical":
            # Skip plan review entirely for mechanical tasks
            _log("  ↩ PLAN_REVIEW   skipped (mechanical work type)")
        elif config.plan_agent_review.enabled:
            result = _run_plan_agent_review(
                state=state,
                config=config,
                task=task,
                story_content=story_content,
                workspace_path=workspace_path,
                plan_profile=plan_profile,
                plan_text=plan_text,
                preflight_result=preflight_result,
                notify=notify,
                logger=logger,
                advisory=(_work_type == "refactor"),
                state_update_fn=state_update_fn,
            )
            # Durable copy of the plan-review outcome. A resumed attempt starts
            # at DEV/REVIEW and never re-runs plan review, so without this the
            # audit for a story whose plan review demonstrably ran reports it as
            # absent (#2155).
            _persist_plan_review_record()
            if result is not None:
                return result

        elif config.plan_review.enabled:
            result = _run_human_plan_review(
                state=state,
                config=config,
                task=task,
                workspace_path=workspace_path,
                plan_profile=plan_profile,
                plan_text=plan_text,
                plan_prompt=plan_prompt,
                notify=notify,
                logger=logger,
                run_id=run_id,
            )
            _persist_plan_review_record()
            if result is not None:
                if _work_type == "refactor" and not _is_terminal_plan_result(result):
                    _log(
                        "  ⚠ PLAN_REVIEW   advisory (refactor — "
                        "regen exhausted but not blocking, continuing to DEV)"
                    )
                else:
                    return result

    else:
        state.phase = Phase.ESCALATE
        state.error = (
            "PLAN phase failed — task requires a plan but the planning agent "
            f"did not produce one (exit={plan_result.exit_code}). "
            "Consider increasing plan timeout or simplifying the spec."
        )
        _log("  ✗ PLAN failed — escalating (not proceeding blind)")
        if logger:
            logger._safe_emit("phase_end", phase="PLAN", outcome="escalate")
        _log(f"✗ ESCALATE   {state.error}")
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    # ── Plan validation (advisory, before DEV) ────────────────────────
    if state.plan_structured is not None and state.workspace_path is not None:
        from theforge.plan_validator import extract_story_criteria  # noqa: PLC0415
        from theforge.plan_validator import validate_plan as _validate_plan  # noqa: PLC0415

        _story_criteria = extract_story_criteria(story_content)
        _pv_findings = _validate_plan(state.plan_structured, _story_criteria, state.workspace_path)
        state.plan_validation_findings = _pv_findings
        for _f in _pv_findings:
            import logging as _logging  # noqa: PLC0415

            _logging.getLogger(__name__).warning(
                "Plan validation [%s]: %s", _f["check"], _f["message"]
            )
        if _pv_findings:
            _log(
                f"  ⚠ PLAN_VALIDATION  {len(_pv_findings)} finding(s) — "
                f"advisory only, proceeding to DEV"
            )
        else:
            _log("  ✓ PLAN_VALIDATION  all checks passed")

    return None


def _is_terminal_plan_result(result: "CoordinatorResult") -> bool:
    """Return True if a plan-review result represents a terminal stop (not advisory).

    Used to distinguish refactor-advisory non-blocking outcomes from explicit
    operator cancellations and hard errors:

    - ESCALATE phase → always terminal (plan regen failure, unreadable plan file)
    - "abandoned by human" message → explicit operator cancel → terminal
    - "rejected N time(s)" message → exhausted regen attempts → NOT terminal
      (for refactor advisory, proceed to DEV)
    """
    if result.phase == Phase.ESCALATE:
        return True
    return "abandoned by human" in (result.message or "")


def _run_plan_agent_review(
    *,
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    story_content: str,
    workspace_path: Path,
    plan_profile: ModelProfile,
    plan_text: str,
    preflight_result: "AgentResult | None",
    notify: bool,
    logger: "StructuredLogger | None",
    advisory: bool = False,
    state_update_fn: "Callable[[dict], None] | None" = None,
) -> CoordinatorResult | None:
    """Run agent-based plan review pool with regen loop.

    Returns None to continue to DEV, or CoordinatorResult to stop early.

    When advisory=True (refactor work type), findings are logged but the review
    never rejects the plan — always continues to DEV regardless of verdict.
    """
    state.phase = Phase.PLAN_REVIEW
    state.plan_review_mode = "agent"
    _adaptive = getattr(state, "_adaptive_decision", None)
    if (
        _adaptive is not None
        and _adaptive.plan_reviewers
        and "plan_agent_review" not in state._explicit_roles
    ):
        par_profiles = _adaptive.plan_reviewers
        _log(f"  [adaptive] plan_reviewers: {', '.join(p.model for p in par_profiles)}")
    else:
        par_profiles = [
            _apply_transport_fallback(profile, config.transport_fallbacks)
            for profile in config.plan_agent_review.profiles
        ]
    _pool_names = [p.name for p in par_profiles]
    _pool_label = "+".join(p.model for p in par_profiles)
    if config.plan_review.enabled:
        _log("  ⚠ Both plan_agent_review and plan_review enabled — agent review takes precedence")

    _max = config.retry.max_plan_regen_attempts
    for _attempt in range(_max + 2):
        _log_phase(
            state.phase,
            f"agent review ({_pool_label}, {len(par_profiles)} reviewer(s))",
        )
        if logger:
            logger._safe_emit("phase_start", phase="PLAN_REVIEW", iteration=_attempt)

        pr_prompt = build_plan_review_prompt(
            task,
            story_content=story_content,
            plan_content=state.plan_structured or plan_text,
            mode=par_profiles[0].mode,
            preflight_output=(
                preflight_result.output if preflight_result and preflight_result.success else None
            ),
            rejection_findings=state.plan_agent_review_findings,
        )

        _pr_start = time.monotonic()
        _pool_session_ids = [state.plan_review_session_ids.get(p.name) for p in par_profiles]
        # Passive per-reviewer progress channel — identical contract to REVIEW.
        _pr_channel = ReviewerProgressChannel(
            reviewer_names=_pool_names,
            phase="PLAN_REVIEW",
            iteration=_attempt,
            cost_usd=state.total_cost_measured,
            complexity=state.preflight_complexity,
            complexity_score=state.preflight_complexity_score,
            state_update_fn=state_update_fn,
        )
        # Capture per-reviewer wall-clock from the initial pool fan-out so the
        # per-plan-reviewer latency signal (#1443) attributes time to the reviewer
        # that spent it, not the pool aggregate. Index-aligned to par_profiles.
        _pr_reviewer_durations: list[float] = []
        pr_results = run_agent_pool(
            prompt=pr_prompt,
            profiles=par_profiles,
            working_dir=workspace_path,
            session_ids=_pool_session_ids,
            secrets=config.secrets,
            progress_cb=_pr_channel.cb,
            durations_out=_pr_reviewer_durations,
        )
        # Persist each reviewer's FIRST-attempt raw output before any retry loop
        # can replace it (#2066). Retries write their own -retryN /
        # -parse-retryN artifact, so writing this slot from the post-retry
        # result made the canonical file a byte-duplicate of the last retry and
        # left the output that actually failed validation written nowhere — the
        # single most diagnostic output the run produced. Written here rather
        # than at the end of the phase, mirroring review_pool, which persists
        # the base per-reviewer artifact straight off the initial pool result.
        # Persist raw output regardless of approve/reject outcome.
        for _prof, _res in zip(par_profiles, pr_results):
            _write_log_artifact(
                state.log_dir,
                f"plan-review/attempt-{_attempt}/{_prof.name}.yaml",
                _res.output or "",
                owner_run_id=state.run_id,
            )
        pr_results, _transport_retry_events = _retry_transient_plan_review_failures(
            prompt=pr_prompt,
            profiles=par_profiles,
            results=pr_results,
            state=state,
            config=config,
            workspace_path=workspace_path,
            attempt=_attempt,
            progress_channel=_pr_channel,
        )
        state.plan_review_transport_retries.extend(_transport_retry_events)
        pr_results, _parse_retry_events = _retry_parse_failed_plan_reviews(
            prompt=pr_prompt,
            profiles=par_profiles,
            results=pr_results,
            state=state,
            config=config,
            workspace_path=workspace_path,
            attempt=_attempt,
        )
        state.plan_review_parse_retries.extend(_parse_retry_events)
        _pr_elapsed = time.monotonic() - _pr_start
        state.plan_review_durations.append(_pr_elapsed)

        for _prof, _res in zip(par_profiles, pr_results):
            if _res.session_id and produced_model_output(_res):
                state.plan_review_session_ids[_prof.name] = _res.session_id
        state.plan_review_results.extend(pr_results)
        save_sessions(
            workspace_path,
            state.dev_session_id,
            state.reviewer_session_ids,
            state.plan_review_session_ids,
        )

        _parsed_prs: list[PlanReviewResult] = []
        # Reviewers lost to the substrate this attempt: they produced no model
        # output, so they cast no vote of any kind (#1951).
        _no_judgment_reviewers: list[str] = []
        _no_judgment_failures: list[AgentInvocationFailure] = []
        for _prof, _res in zip(par_profiles, pr_results):
            _transport_failure = _is_transient_plan_review_failure(_res)
            if not _res.success:
                _failure_summary = _summarize_plan_review_failure(_res)
                # A reviewer that never answered has not rejected the plan. It
                # is excluded from the merge either way (parse_errors do that),
                # but recording "REJECT" here put a manufactured reviewer verdict
                # into the audit trail and shrank the pool silently. Mark it as
                # NO_JUDGMENT and keep the pool loss as first-class metadata.
                _no_judgment = classify_agent_failure(
                    _res, phase="PLAN_REVIEW", profile_name=_prof.name
                )
                if _no_judgment is not None:
                    record_invocation_failure(state, _no_judgment)
                    _no_judgment_reviewers.append(_prof.name)
                    _no_judgment_failures.append(_no_judgment)
                    _log(
                        f"  ✗ PLAN_REVIEW   {_prof.name} produced no model output "
                        f"({_no_judgment.summary()}) — no judgment (dropped from pool)"
                    )
                else:
                    _log(
                        f"  ✗ PLAN_REVIEW   {_prof.name} failed "
                        f"({_failure_summary}) — unparseable, excluded from merge"
                    )
                _parsed = PlanReviewResult(
                    verdict=NO_JUDGMENT,
                    findings=[],
                    parse_errors=[f"Transport failure: {_failure_summary}"],
                )
            else:
                if not (_res.output or "").strip():
                    _log(
                        f"  ⚠ PLAN_REVIEW   {_prof.name} returned empty output "
                        "— treating as REJECT"
                    )
                    _parsed = parse_plan_review_output("")
                else:
                    _parsed = parse_plan_review_output(_res.output)

            if _parsed.parse_errors:
                if _res.success:
                    _failure_kind = "parse"
                elif _transport_failure:
                    _failure_kind = "transport"
                else:
                    _failure_kind = _res.failure_code or "generic"
                _retry_count = sum(
                    1
                    for _event in (*_transport_retry_events, *_parse_retry_events)
                    if _event["attempt"] == _attempt and _event["reviewer"] == _prof.name
                )
                _log(
                    f"  ⚠ PLAN_REVIEW   {_prof.name} parse issues: "
                    f"{'; '.join(_parsed.parse_errors)}"
                )
                state.plan_review_failures.append(
                    {
                        "attempt": _attempt,
                        "reviewer": _prof.name,
                        "failure_kind": _failure_kind,
                        "retry_count": _retry_count,
                        "retryable": _transport_failure,
                        # Whether a model spoke at all (#1951). A reviewer whose
                        # output was unparseable still judged the plan; one that
                        # never answered did not, and reviewer-reliability
                        # consumers must be able to tell those apart.
                        "produced_model_output": _prof.name not in _no_judgment_reviewers,
                        "errors": list(_parsed.parse_errors),
                    }
                )

            _p1_count = sum(1 for f in _parsed.findings if f.severity in ("P0", "P1"))
            _p2_count = sum(1 for f in _parsed.findings if f.severity == "P2")
            # A reviewer that never answered has zero findings — rendering that
            # as "no blockers" would read as a clean pass it never gave (#1951).
            if _prof.name not in _no_judgment_reviewers:
                _log(
                    f"  {'✓' if _p1_count == 0 else '✗'} PLAN_REVIEW   {_prof.name} "
                    f"({'no blockers' if _p1_count == 0 else f'{_p1_count} P1'}"
                    f"{f', {_p2_count} P2' if _p2_count else ''})"
                )
            # Raw output is already persisted: the first attempt at the pool
            # fan-out above, every retry by its own retry loop (#2066).
            _parsed_prs.append(_parsed)

        # ── Per-plan-reviewer mechanical value telemetry (#1443) ──────────────
        # Deterministic, coordinator-computed, over the structured findings from
        # THIS completed pool (no LLM judgment). Uniqueness compares each
        # reviewer's blocking (P1) findings against every peer's via anchor
        # overlap; parse-error count is derived from the same parse step that
        # feeds plan_review_failures (no parallel parse-failure writer, per the
        # ADR-0006 boundary). Latency is the per-reviewer pool wall-clock captured
        # above. Recorded for every reviewer regardless of outcome so the audit
        # can answer "is this reviewer earning its wall-clock cost?".
        _uniq_inputs = [
            (_prof.name, _parsed.findings)
            for _prof, _parsed in zip(par_profiles, _parsed_prs)
            if not _parsed.parse_errors
        ]
        _uniqueness = compute_plan_reviewer_uniqueness(_uniq_inputs)
        for _i, (_prof, _parsed) in enumerate(zip(par_profiles, _parsed_prs)):
            _unique_p1, _total_p1 = _uniqueness.get(_prof.name, (0, 0))
            _latency_s = (
                round(_pr_reviewer_durations[_i], 2) if _i < len(_pr_reviewer_durations) else None
            )
            state.plan_reviewer_value.append(
                {
                    "attempt": _attempt,
                    "reviewer": _prof.name,
                    "complexity": state.preflight_complexity or "medium",
                    "unique_p1_count": _unique_p1,
                    "total_p1_count": _total_p1,
                    "latency_s": _latency_s,
                    "parse_error_count": len(_parsed.parse_errors),
                    "actual_model": getattr(_prof, "model", None),
                    "provider": getattr(_prof, "provider", None),
                    "cli": getattr(_prof, "cli", None),
                    # The concrete version that served THIS attempt (#2226).
                    # Captured here rather than joined by reviewer name later:
                    # one reviewer can be served by different versions across
                    # attempts, and a name-only join would stamp every sample
                    # with whichever version happened to be newest.
                    "resolved_model": (
                        resolved_identity_for_result(pr_results[_i])
                        if _i < len(pr_results)
                        else None
                    ),
                }
            )

        # Minimum-success gate: require the configured number of parseable reviewers
        _failed_this_attempt = sum(1 for _p in _parsed_prs if _p.parse_errors)
        _successful_count = len(par_profiles) - _failed_this_attempt
        _min_reviewers = config.plan_agent_review.min_reviewers
        # ── Degraded pool (#1951) ─────────────────────────────────────────
        # The pool completed, but with fewer members than it was configured
        # with because the substrate ate some of them. A degraded-pool result
        # is not the same kind of result as a full-pool one; preserve the loss
        # so downstream consumers can tell them apart instead of seeing a
        # smaller pool that looks intact.
        if _no_judgment_reviewers and _successful_count >= _min_reviewers:
            record_degraded_pool(
                state,
                phase="PLAN_REVIEW",
                pool_size=len(par_profiles),
                lost=_no_judgment_reviewers,
                failures=_no_judgment_failures,
            )
            _log(
                f"  ⚠ PLAN_REVIEW   degraded pool: "
                f"{len(_no_judgment_reviewers)}/{len(par_profiles)} reviewer(s) produced "
                f"no model output ({', '.join(_no_judgment_reviewers)}); continuing with "
                f"{_successful_count}"
            )
        if _successful_count < _min_reviewers:
            # Infrastructure abort vs. genuine review failure: when every
            # reviewer that fell short did so without producing any model
            # output, no reviewer judged this plan and the shortfall is a
            # statement about the substrate. Escalating it as a plan-review
            # rejection would record a story-level verdict no model formed.
            if _no_judgment_reviewers and len(_no_judgment_reviewers) >= _failed_this_attempt:
                _failure = _no_judgment_failures[0]
                state.phase = Phase.ESCALATE
                state.error = (
                    f"{len(_no_judgment_reviewers)}/{len(par_profiles)} plan reviewer(s) "
                    f"produced no model output ({', '.join(_no_judgment_reviewers)}); "
                    f"fewer than the required {_min_reviewers} reviewer(s) judged the plan "
                    "— aborting as an infrastructure failure, not a plan rejection."
                )
                record_degraded_pool(
                    state,
                    phase="PLAN_REVIEW",
                    pool_size=len(par_profiles),
                    lost=_no_judgment_reviewers,
                    failures=_no_judgment_failures,
                )
                mark_infrastructure_abort(state, _failure, message=state.error)
                _log(f"  ✗ ABORT   infrastructure failure: {state.error}")
                if logger:
                    logger._safe_emit(
                        "infrastructure_abort",
                        phase="PLAN_REVIEW",
                        reason=state.error,
                        category=_failure.category,
                    )
                return CoordinatorResult(
                    success=False,
                    phase=Phase.ESCALATE,
                    state=state,
                    message=state.error,
                    infrastructure_failure=True,
                )
            state.plan_review_decision = "reject"
            state.phase = Phase.ESCALATE
            state.error = (
                f"Only {_successful_count}/{len(par_profiles)} plan reviewer(s) produced "
                f"parseable output; minimum required is {_min_reviewers}."
            )
            _log(f"  ✗ PLAN_REVIEW   {state.error}")
            _escalate_notify(task, state, notify, config)
            return CoordinatorResult(
                success=False,
                phase=Phase.ESCALATE,
                state=state,
                message=state.error,
            )

        _total_pr_cost = sum_costs(r.cost_usd for r in pr_results)

        # ── Plan finding identity tracking ────────────────────────────────
        # Snapshot registry before this cycle so new inserts don't interfere.
        _prior_registry_snapshot = list(state.plan_finding_registry)

        # Merge with corroboration: pass registry so single-reviewer
        # first-occurrence P1s can be downgraded to P1-impl.
        merged_pr, _corroboration_downgrades = merge_plan_review_results(
            _parsed_prs,
            _pool_names,
            prior_registry=_prior_registry_snapshot,
            current_attempt=_attempt,
        )

        _prior_as_findings = [
            PlanReviewFinding(severity=r.severity, description=r.description, suggestion=None)
            for r in _prior_registry_snapshot
        ]
        _match_results = match_plan_findings(list(merged_pr.findings), _prior_as_findings)
        state.plan_match_provenance.append(format_provenance(_match_results))

        # Capture pre-update dispositions NOW, before the loop mutates the records.
        # _prior_registry_snapshot is a shallow copy — modifying .disposition modifies
        # the same objects.  We need the original "fixed"/"new"/"unresolved" values to
        # distinguish genuinely recurring findings from revived-from-fixed ones.
        _prior_dispositions: dict[int, str] = {
            i: rec.disposition for i, rec in enumerate(_prior_registry_snapshot)
        }
        # Build downgrade lookup for original_severity audit trail.
        _downgrade_descs: dict[str, str] = {
            d.description: d.original_severity for d in _corroboration_downgrades
        }

        _matched_prior_indices: set[int] = set()
        for _mr in _match_results:
            if _mr.prior_index is not None:
                _prior_registry_snapshot[_mr.prior_index].cycle_last_seen = _attempt
                _prior_registry_snapshot[_mr.prior_index].disposition = "unresolved"
                # Update effective severity from current corroborated finding.
                _cf = merged_pr.findings[_mr.current_index]
                _prior_registry_snapshot[_mr.prior_index].severity = _cf.severity
                _matched_prior_indices.add(_mr.prior_index)
            else:
                _cf = merged_pr.findings[_mr.current_index]
                _orig_sev = _downgrade_descs.get(_cf.description)
                state.plan_finding_registry.append(
                    PlanFindingRecord(
                        description=strip_reviewer_prefix(_cf.description),
                        severity=_cf.severity,
                        cycle_first_seen=_attempt,
                        cycle_last_seen=_attempt,
                        disposition="new",
                        original_severity=_orig_sev,
                    )
                )

        # Mark prior findings not seen this cycle as "fixed".
        for _i, _rec in enumerate(_prior_registry_snapshot):
            if _i not in _matched_prior_indices and _rec.cycle_last_seen < _attempt:
                _rec.disposition = "fixed"
        # ─────────────────────────────────────────────────────────────────

        # Build filtered findings for the regen prompt (if rejection follows).
        # Registry is now fully updated for this attempt; pass _prior_dispositions so
        # the filter can distinguish genuinely recurring findings from revived-from-fixed ones.
        _filtered_regen_text, _filter_audit = build_filtered_regen_findings(
            list(merged_pr.findings),
            _match_results,
            _attempt,
            state.plan_finding_registry,
            _prior_dispositions,
        )

        record_plan_attempt(
            state,
            merged_pr.findings,
            max_plan_regen_attempts=config.retry.max_plan_regen_attempts,
        )

        if merged_pr.verdict == "APPROVE":
            state.plan_review_decision = "approve"
            # ── Post-plan dev-tier checkpoint (#1387) ──────────────────
            # A clean plan-review resolves the uncertainty that may have promoted
            # the dev tier at preflight. Re-evaluate ONLY the dev tier (max one
            # step down); engine.py swaps config.dev_profile before DEV.
            _apply_post_plan_dev_checkpoint(state, config)
            if merged_pr.findings:
                findings_text = plan_review_findings_to_text(merged_pr)
                state.plan_agent_review_findings = findings_text
                _log(
                    f"  ✓ PLAN_REVIEW   approve (merged, "
                    f"{len(merged_pr.findings)} advisory)  "
                    f"{_fmt_cost(_total_pr_cost)}  {_fmt_duration(_pr_elapsed)}"
                )
                _log(f"  Advisory findings (passed to dev):\n{findings_text}")
            else:
                _log(
                    f"  ✓ PLAN_REVIEW   approve (merged)  "
                    f"{_fmt_cost(_total_pr_cost)}  {_fmt_duration(_pr_elapsed)}"
                )
            if logger:
                logger._safe_emit(
                    "phase_end",
                    phase="PLAN_REVIEW",
                    outcome="approve",
                    cost_usd=_round_cost(_total_pr_cost),
                    duration_s=round(_pr_elapsed, 2),
                    plan_regen_disposition=state.plan_regen_disposition,
                )
            try:
                _cu._run_shell(["git", "add", str(PLAN_PATH)], cwd=workspace_path)
                _cu._run_shell(
                    [
                        "git",
                        "commit",
                        "-m",
                        f"docs(plan): approved implementation plan for {task.slug}",
                    ],
                    cwd=workspace_path,
                )
                _log(f"  ✓ PLAN   committed {PLAN_PATH}")
            except Exception as _commit_err:
                _log(f"  ⚠ PLAN   could not commit {PLAN_PATH}: {_commit_err}")
            _write_log_artifact(
                state.log_dir,
                "plan.md",
                plan_text,
                owner_run_id=state.run_id,
            )
            return None  # continue to DEV

        # Advisory mode (refactor work type): log findings but never reject
        if advisory:
            # This branch proceeds to DEV regardless of verdict, so record the
            # post-plan checkpoint with the ACTUAL merged verdict (#1387). A
            # non-APPROVE advisory continuation lands preserve/plan_review_not_
            # approve rather than leaving the pending sentinel; an APPROVE advisory
            # is gated identically to the regular APPROVE path.
            _apply_post_plan_dev_checkpoint(state, config, merged_pr.verdict)
            findings_text = plan_review_findings_to_text(merged_pr)
            state.plan_agent_review_findings = findings_text
            _log(
                f"  ⚠ PLAN_REVIEW   advisory findings (refactor — not blocking)  "
                f"{_fmt_cost(_total_pr_cost)}  {_fmt_duration(_pr_elapsed)}"
            )
            _log(f"  Advisory findings (logged, not blocking):\n{findings_text}")
            if logger:
                logger._safe_emit(
                    "phase_end",
                    phase="PLAN_REVIEW",
                    outcome="advisory",
                    cost_usd=_round_cost(_total_pr_cost),
                    duration_s=round(_pr_elapsed, 2),
                )
            _write_log_artifact(
                state.log_dir,
                "plan.md",
                plan_text,
                owner_run_id=state.run_id,
            )
            return None  # continue to DEV regardless of verdict

        # REJECT path
        findings_text = plan_review_findings_to_text(merged_pr)
        state.plan_agent_review_findings = findings_text
        # Store per-attempt filter audit (even when filtering_applied=False).
        state.plan_regen_filter_audit.append(_filter_audit)
        _log(
            f"  ✗ PLAN_REVIEW   reject (merged)  "
            f"{_fmt_cost(_total_pr_cost)}  {_fmt_duration(_pr_elapsed)}"
        )
        if _filter_audit.get("filtering_applied"):
            _p2_omit = _filter_audit.get("p2_omitted_count", 0)
            _rec_p1 = _filter_audit.get("recurring_p1_count", 0)
            _log(
                f"  ↳ regen filter: {_rec_p1} recurring P1(s) highlighted"
                + (f", {_p2_omit} P2(s) omitted" if _p2_omit else "")
            )
        if logger:
            logger._safe_emit(
                "phase_end",
                phase="PLAN_REVIEW",
                outcome="reject",
                cost_usd=_round_cost(_total_pr_cost),
                duration_s=round(_pr_elapsed, 2),
                plan_regen_disposition=state.plan_regen_disposition,
            )

        state.plan_regen_count += 1

        # Escalate planner model after N consecutive rejections
        if (
            state.plan_regen_count >= config.retry.plan_escalation_threshold
            and not state.plan_escalated
        ):
            # Registry passed as-is: an explicitly empty {} stays empty instead
            # of collapsing to the built-in default (`... or None`).
            _registry = config.model_registry
            _curr_key = _find_registry_key_for_profile(plan_profile, registry=_registry)
            if _curr_key is not None and config.models is not None:
                _next_key = _escalate_dev_model(_curr_key, config.models, registry=_registry)
                if _next_key is not None:
                    from theforge.config.models import (  # noqa: PLC0415
                        _resolve_model_info,
                    )

                    _next_info = _resolve_model_info(_next_key, registry=_registry)
                    _old_model = plan_profile.model
                    _new_model = _next_info.model
                    plan_profile = apply_model_info(plan_profile, _next_info)
                    state.plan_escalated = True
                    state.plan_escalation_note = (
                        f"MODEL ESCALATION: The plan was rejected"
                        f" {state.plan_regen_count} time(s). The previous"
                        f" model ({_old_model}) was unable to produce an"
                        f" acceptable plan. You are now running on an upgraded"
                        f" model ({_new_model}). Key findings from prior"
                        f" rejections: {findings_text}"
                    )
                    _record_plan_model_escalation(
                        state,
                        previous_model=_old_model,
                        escalated_model=_new_model,
                        rejections=state.plan_regen_count,
                        findings=findings_text,
                    )
                    _log(
                        f"  Plan escalation:"
                        f" {_old_model} → {_new_model}"
                        f" (rejected {state.plan_regen_count}x)"
                    )
                    if logger:
                        logger._safe_emit(
                            "plan_model_escalate",
                            from_model=_old_model,
                            to_model=_new_model,
                            rejections=state.plan_regen_count,
                        )
                else:
                    _log(
                        "  Plan escalation: no higher tier available"
                        " — continuing with current model"
                    )

        # Backtrack regen is separate from the patch-loop budget: exclude it from
        # the ceiling count so an early backtrack does not consume a patch slot.
        _patch_regen_count = state.plan_regen_count - (1 if state.plan_backtrack_used else 0)
        if _patch_regen_count > config.retry.max_plan_regen_attempts:
            _disposition_now = state.plan_regen_disposition or "patch"
            if _disposition_now != "escalate" and (
                _disposition_now != "backtrack" or state.plan_backtrack_used
            ):
                state.plan_review_decision = "reject"
                state.phase = Phase.ESCALATE
                _max_regen = config.retry.max_plan_regen_attempts
                state.error = (
                    f"Plan rejected {state.plan_regen_count} time(s)"
                    f" by agent reviewer"
                    f" (max_plan_regen_attempts={_max_regen})."
                    f" Findings:\n{findings_text}"
                )
                _log(f"  ✗ PLAN_REVIEW   rejected {state.plan_regen_count}x — escalating")
                _escalate_notify(task, state, notify, config)
                return CoordinatorResult(
                    success=False,
                    phase=Phase.ESCALATE,
                    state=state,
                    message=state.error,
                )

        # Early escalation: disposition tracker determined the backtrack attempt
        # itself diverged — no further regen will help; escalate immediately.
        if state.plan_regen_disposition == "escalate":
            _assessment = state.plan_regen_assessment or {}
            _reason_code = _assessment.get("reason_code")
            _reason_suffix = ""
            if _reason_code in {"unassessable_no_comparable_structure", "too_few_attempts"}:
                _reason_suffix = f" Trajectory assessment: {_reason_code}."
            state.plan_review_decision = "reject"
            state.phase = Phase.ESCALATE
            state.error = (
                f"Plan rejected {state.plan_regen_count} time(s)"
                f" by agent reviewer — backtrack attempt failed to converge."
                f"{_reason_suffix}"
                f" Findings:\n{findings_text}"
            )
            _log(
                f"  ✗ PLAN_REVIEW   escalate disposition"
                f" after {state.plan_regen_count} attempt(s) — escalating immediately"
            )
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

        _disposition = state.plan_regen_disposition or "patch"

        if _disposition == "backtrack":
            # build_disposition_context is the single owner of all backtrack-specific
            # text (opening, learned constraints, rejected strategy, instructions).
            # The patch ## Instructions block is intentionally omitted here — patch
            # guidance ("Do not discard working parts") contradicts backtrack intent.
            state.plan_backtrack_used = True
            disposition_ctx = build_disposition_context(state)
            regen_prompt = dedent(f"""\
                You are a planning agent for **{task.name}**.

                ## Your Role

                See trajectory analysis below.

                ## Spec

                {story_content}

                ## Your Plan (current version)

                {state.plan_output}

                ## Reviewer Findings

                {_filtered_regen_text}
            """)
            regen_prompt += f"\n\n{disposition_ctx}\n"
        else:
            # patch (default): targeted edits, preserve working parts
            _patch_instructions = (
                "1. Read each finding against your plan above.\n"
                "2. Fix every P1 (must fix) and P2 (improvement).\n"
                "3. Output the complete updated plan in the same YAML schema.\n"
                "4. Do NOT discard working parts of your plan. Only change what\n"
                "   the findings call out."
            )
            regen_prompt = dedent(f"""\
                You are a planning agent for **{task.name}**.

                ## Your Role

                You wrote the plan below. Reviewers found issues. Fix your plan
                to address every P1 and P2 finding. Do not rewrite from scratch —
                make targeted edits to the plan you already wrote.

                ## Spec

                {story_content}

                ## Your Plan (current version)

                {state.plan_output}

                ## Reviewer Findings

                {_filtered_regen_text}

                ## Instructions

                {_patch_instructions}
            """)

            disposition_ctx = build_disposition_context(state)
            if disposition_ctx:
                regen_prompt += f"\n\n{disposition_ctx}\n"

        if state.plan_escalation_note:
            regen_prompt += f"\n\n## Model Escalation\n\n{state.plan_escalation_note}\n"

        if _LOG_LEVEL >= LogLevel.VERBOSE:
            regen_prompt += (
                "\n\n## Session Continuity Check\n\n"
                "Begin your response with exactly one line in this format:\n"
                "PRIOR CONTEXT: [one sentence describing the key naming/filing "
                "approach from your previous plan attempt]\n\n"
                "This confirms you have access to your prior session context."
            )

        _plan_start = time.monotonic()
        _log(f"  Starting plan regen (model={plan_profile.model}, new session)...")
        plan_result = _run_plan_agent_with_retry(
            prompt=regen_prompt,
            profile=plan_profile,
            workspace_path=workspace_path,
            config=config,
            state=state,
            phase_label="PLAN_REGEN",
            attempt=state.plan_regen_count,
            session_id=None,  # fresh session — prior session anchors on its own wrong output
        )
        _plan_elapsed = time.monotonic() - _plan_start
        state.plan_durations.append(_plan_elapsed)
        state.plan_results.append(plan_result)
        state.plan_session_id = plan_result.session_id or state.plan_session_id
        write_trace(
            workspace_path / ".forge/traces" / f"plan-attempt-{state.plan_regen_count}.txt",
            plan_result.output,
        )
        _write_log_artifact(
            state.log_dir,
            f"plan-attempt-{state.plan_regen_count}.txt",
            plan_result.output or "",
            owner_run_id=state.run_id,
        )

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
        worktree_plan_path = workspace_path / PLAN_PATH
        ensure_parent_dir(worktree_plan_path)
        worktree_plan_path.write_text(plan_text, encoding="utf-8")
        state.plan_output = plan_text
        state.plan_attempt_plans.append(state.plan_structured)
        state.plan_structured = parse_plan_output(plan_text)
        _log(
            "  ✓ PLAN (regenerated)  "
            f"{_fmt_cost(plan_result.cost_usd)}  {_fmt_duration(_plan_elapsed)}"
        )

    # Loop exhausted without APPROVE — should not happen (guarded by regen_count check above)
    return None


def _run_human_plan_review(
    *,
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    workspace_path: Path,
    plan_profile: ModelProfile,
    plan_text: str,
    plan_prompt: str,
    notify: bool,
    logger: "StructuredLogger | None",
    run_id: str | None,
) -> CoordinatorResult | None:
    """Run human-in-the-loop plan review (pending-file, remote, or interactive).

    Returns None to continue to DEV, or CoordinatorResult to stop early.
    """
    for _ in range(config.retry.max_plan_regen_attempts + 1):
        state.phase = Phase.PLAN_REVIEW
        _log_phase(state.phase, "waiting for human decision...")
        _log(f"  Plan written to: {workspace_path / PLAN_PATH}")

        _pr_start = time.monotonic()
        if _is_pending_file_mode(notify, config):
            plan_review_decision = _pending_plan_review(
                state, plan_text, workspace_path, task, config, run_id=run_id
            )
        elif _is_remote_mode(notify, config):
            plan_review_decision = _plan_review_remote(
                state, plan_text, workspace_path, task, config
            )
        else:
            plan_review_decision = _plan_review_interactive(state, plan_text, workspace_path, task)
            state.plan_review_mode = "interactive"
        state.plan_review_waited_seconds = time.monotonic() - _pr_start
        state.plan_review_decision = plan_review_decision

        if plan_review_decision == "approve":
            try:
                updated = resolve_plan_path(workspace_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                state.phase = Phase.ESCALATE
                _unreadable_path = resolve_plan_path(workspace_path)
                state.error = f"{_unreadable_path} unreadable after edit: {exc}"
                _log(f"  ✗ PLAN_REVIEW   {state.error}")
                _escalate_notify(task, state, notify, config)
                return CoordinatorResult(
                    success=False,
                    phase=Phase.ESCALATE,
                    state=state,
                    message=state.error,
                )
            state.plan_output = updated
            state.plan_structured = parse_plan_output(updated)
            plan_text = updated
            record_plan_attempt(
                state,
                [],
                max_plan_regen_attempts=config.retry.max_plan_regen_attempts,
                trajectory_assessable=False,
            )
            # ── Post-plan dev-tier checkpoint (#1387) ──────────────────
            # Same demotion signal as the agent-review APPROVE path: a clean
            # human/pending-file/remote approval on a medium story de-risks the
            # dev tier too. Human approvals record zero findings for the
            # checkpoint, but the attempt is marked non-assessable so the
            # structured trajectory classifier ignores it.
            _apply_post_plan_dev_checkpoint(state, config)
            write_trace(
                workspace_path
                / ".forge/traces"
                / f"plan-attempt-{state.plan_regen_count}-approved.txt",
                updated,
            )
            _write_log_artifact(
                state.log_dir,
                f"plan-attempt-{state.plan_regen_count}-approved.txt",
                updated,
                owner_run_id=state.run_id,
            )
            _log(
                "  ✓ PLAN_REVIEW   approve  "
                f"({_fmt_duration(state.plan_review_waited_seconds or 0)})"
            )
            if logger:
                logger._safe_emit(
                    "phase_end",
                    phase="PLAN_REVIEW",
                    outcome="approve",
                    plan_regen_disposition=state.plan_regen_disposition,
                )
            try:
                _cu._run_shell(["git", "add", str(PLAN_PATH)], cwd=workspace_path)
                _cu._run_shell(
                    [
                        "git",
                        "commit",
                        "-m",
                        f"docs(plan): approved implementation plan for {task.slug}",
                    ],
                    cwd=workspace_path,
                )
                _log(f"  ✓ PLAN   committed {PLAN_PATH}")
            except Exception as _commit_err:
                _log(f"  ⚠ PLAN   could not commit {PLAN_PATH}: {_commit_err}")
            _write_log_artifact(
                state.log_dir,
                "plan.md",
                plan_text,
                owner_run_id=state.run_id,
            )
            return None  # continue to DEV

        if plan_review_decision == "regenerate":
            state.plan_regen_count += 1
            record_plan_attempt(
                state,
                [],
                max_plan_regen_attempts=config.retry.max_plan_regen_attempts,
                trajectory_assessable=False,
            )
            if logger:
                logger._safe_emit(
                    "phase_end",
                    phase="PLAN_REVIEW",
                    outcome="regenerate",
                    plan_regen_disposition=state.plan_regen_disposition,
                )
            if state.plan_regen_count > config.retry.max_plan_regen_attempts:
                state.plan_review_decision = "abandon"
                _log(f"  ✗ PLAN_REVIEW   rejected {state.plan_regen_count}x — abandoning")
                return CoordinatorResult(
                    success=False,
                    phase=Phase.PLAN_REVIEW,
                    state=state,
                    message=(f"Plan rejected {state.plan_regen_count} time(s) — abandoning."),
                )

            _max2 = config.retry.max_plan_regen_attempts
            _log(
                f"  ↺ PLAN_REVIEW   regenerate — re-running PLAN agent "
                f"(attempt {state.plan_regen_count}/{_max2})"
            )

            # Human-review regen does NOT use disposition-aware prompts. These
            # attempts stay in the trajectory table, but they are marked
            # non-assessable so the classifier does not fabricate accumulated or
            # unassessable stops from missing structured findings.
            _disposition_ctx = build_disposition_context(state)
            if _disposition_ctx:
                regen_plan_prompt = plan_prompt + f"\n\n{_disposition_ctx}\n"
            else:
                regen_plan_prompt = plan_prompt

            _plan_start = time.monotonic()
            plan_result = _run_plan_agent_with_retry(
                prompt=regen_plan_prompt,
                profile=plan_profile,
                workspace_path=workspace_path,
                config=config,
                state=state,
                phase_label="PLAN_REGEN",
                attempt=state.plan_regen_count,
                session_id=state.plan_session_id,
            )
            _plan_elapsed = time.monotonic() - _plan_start
            state.plan_durations.append(_plan_elapsed)
            state.plan_results.append(plan_result)
            state.plan_session_id = plan_result.session_id or state.plan_session_id
            write_trace(
                workspace_path / ".forge/traces" / f"plan-attempt-{state.plan_regen_count}.txt",
                plan_result.output,
            )
            _write_log_artifact(
                state.log_dir,
                f"plan-attempt-{state.plan_regen_count}.txt",
                plan_result.output or "",
                owner_run_id=state.run_id,
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
            worktree_plan_path = workspace_path / PLAN_PATH
            ensure_parent_dir(worktree_plan_path)
            worktree_plan_path.write_text(plan_text, encoding="utf-8")
            state.plan_output = plan_text
            state.plan_attempt_plans.append(state.plan_structured)
            state.plan_structured = parse_plan_output(plan_text)
            _log(
                "  ✓ PLAN (regenerated)  "
                f"{_fmt_cost(plan_result.cost_usd)}"
                f"  {_fmt_duration(_plan_elapsed)}"
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

    return None  # loop exhausted (shouldn't reach here normally)
