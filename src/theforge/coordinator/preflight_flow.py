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

import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from theforge.agent_types import AgentResult
from theforge.config import (
    PREFLIGHT_FORBIDDEN_TOOLS,
    ForgeConfig,
    ModelProfile,
    resolve_preflight_tools,
)
from theforge.policy_provenance import (
    adjudicate_blocked_verdict,
    load_policy_assertions,
    parse_citations,
)
from theforge.sprint.dag import _is_branch_merged
from theforge.task import ContextAssembler, TaskStory, build_preflight_prompt

from . import util as _cu
from .agent_failure import (
    CATEGORY_PROCESS,
    NO_JUDGMENT,
    AgentInvocationFailure,
    classify_agent_failure,
    mark_infrastructure_abort,
    record_invocation_failure,
)
from .audit import has_review_approve
from .audit_render import build_invocation_ledger
from .baseline_checkout import prepare_baseline_checkout
from .log_tee import _write_log_artifact
from .notify import _escalate_notify, _ntfy_done_notify
from .preflight import (
    COMPLEXITY_SCORE_MAX,
    _apply_preflight_config,
    _detect_large_preflight_story_categories,
    _parse_preflight_blocking_basis,
    _parse_preflight_complexity,
    _parse_preflight_complexity_score,
    _parse_preflight_contract_change,
    _parse_preflight_criteria_checked,
    _parse_preflight_domains,
    _parse_preflight_likely_files,
    _parse_preflight_policy_assertions,
    _parse_preflight_scope_exceeded,
    _parse_preflight_sufficiency,
    _parse_preflight_symptom_verification,
    _parse_preflight_verdict,
    _parse_preflight_warnings,
    _parse_preflight_work_type,
    complexity_source,
    degraded_preflight_fields,
    persist_routing_decision,
    score_to_band,
)
from .preflight_cache import capture_preflight_cache_snapshot
from .preflight_complexity_gate import evaluate_preflight_complexity_gate
from .preflight_evidence import build_partial_evidence
from .resume_persistence import save_resume_record
from .state import CoordinatorResult, CoordinatorState, Phase
from .util import _fmt_cost_total, _fmt_duration, _log_phase, _round_cost
from .validation_complexity import (
    DIMENSION_IMPLEMENTATION,
    VALIDATION_BASELINE_SCORE,
    ComplexityEvidence,
    assess_validation_complexity,
    implementation_evidence,
    project_complexity_score,
)

# Tokens that indicate a BLOCKED verdict is based on ambiguity/verifiability
# concerns rather than a concrete hard blocker.  Two conditions must hold
# simultaneously before the override fires: an ambiguity token matches AND
# prior-execution evidence exists on the branch.  The conjunction prevents
# false positives from BLOCKEDs that legitimately mention "verifiable" in a
# non-ambiguity context.
_AMBIGUITY_TOKENS = (
    "ambiguous",
    "verif",
    "not objectively",
    "cannot verify",
    "unclear",
    "measurable",
)

# Number of same-profile re-requests to attempt when preflight exits cleanly
# but emits output the parser cannot read (parse_error). A clean exit with
# unparseable output means preflight did not actually run — the model narrated
# (e.g. a hand-off to a non-existent "investigation agent", issue #1773)
# instead of producing the structured dict. A transient narration is cheap to
# re-request against the same output contract and expensive to proceed past, so
# retry the same profile before consulting any fallback profile or falling
# through to a degraded PROCEED.
_PREFLIGHT_PARSE_RETRY_ATTEMPTS = 1


def _evidence_is_too_thin(evidence: str) -> bool:
    """Return True when evidence is too short to justify ALREADY_DONE."""
    return len(evidence.strip()) < 20


def _detect_preflight_risk_signals(
    story_content: str,
    project_root: Path,
    branch_name: str,
    base_branch: str,
) -> list[str]:
    """Return risk signals indicating preflight verification is load-bearing.

    These are the signals that distinguish a story where preflight is a
    confidence boost (safe to fall through on agent failure) from a story
    where preflight is the only check that would catch contract drift
    (must escalate on agent failure rather than silently PROCEED).

    Signals returned as short identifier strings so they can be logged,
    audited, and matched on without parsing free-form prose.
    """
    signals: list[str] = []
    # Reopened-with-new-context stories carry a "## Reopen Context" block
    # appended by sprint/reopen_context.py. Its presence means an operator
    # left a follow-up comment after the issue was reopened — the body and
    # the operator's intent may have diverged.
    if story_content and "## Reopen Context" in story_content:
        signals.append("reopen_context_in_body")
    # A branch with commits ahead of base means a previous run already
    # produced work for this story — the story is in-progress, not fresh.
    # Re-sprinting without preflight verification risks re-doing or
    # contradicting prior commits.
    if _has_prior_execution_evidence(project_root, branch_name, base_branch):
        signals.append("prior_execution_on_branch")
    return signals


def _has_prior_execution_evidence(
    project_root: "Path", branch_name: str, base_branch: str
) -> bool:
    """Return True if branch_name has commits ahead of base_branch.

    Uses ``git log <base>..<branch> --oneline``; returns False on any error so
    that the caller fails conservatively (keeps BLOCKED) rather than crashing.
    """
    try:
        result = subprocess.run(
            ["git", "log", f"{base_branch}..{branch_name}", "--oneline"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
        return len(lines) > 0
    except Exception:
        return False


#: Preflight's clean-checkout helper now lives in :mod:`baseline_checkout`, which
#: every read-only invocation can reach without importing this phase module. The
#: name is kept as the alias preflight's own call site (and its tests) use.
_prepare_preflight_working_dir = prepare_baseline_checkout


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


def _preflight_phase_end_fields(state: CoordinatorState) -> dict[str, object]:
    """Return PREFLIGHT phase_end fields that must remain visible in forge.log."""
    routing = None
    if isinstance(state.complexity_routing_audit, dict):
        rationale = state.complexity_routing_audit.get("rationale")
        if isinstance(rationale, dict):
            routing = rationale
    return {
        "complexity": state.preflight_complexity,
        "complexity_score": state.preflight_complexity_score,
        "implementation_complexity_score": state.preflight_implementation_complexity_score,
        "validation_complexity_score": state.preflight_validation_complexity_score,
        "complexity_projection": state.preflight_complexity_projection,
        "scope_exceeded": state.preflight_scope_exceeded,
        "complexity_routing": routing,
        "domains": list(state.preflight_domains or []),
        # A phase that produced no evidence must not report its conservative
        # fallback in the same words as an agent-founded classification (#2346).
        **degraded_preflight_fields(state),
    }


def _live_degraded_detail(state: CoordinatorState) -> dict[str, object]:
    """Degraded-preflight keys for the live status ``detail`` block.

    Omitted entirely when preflight is healthy: a live row carrying
    ``preflight_degraded: false`` on every story teaches nothing, while its
    presence is the signal (#2346).
    """
    if not state.preflight_degraded:
        return {}
    fields = degraded_preflight_fields(state)
    return {
        "preflight_degraded": True,
        "preflight_degraded_reason": fields["degraded_reason"],
        "preflight_failure_action": fields["failure_action"],
        "preflight_risk_signals": fields["risk_signals"],
        "complexity_source": fields["complexity_source"],
    }


def _policy_refusal_detail(state: CoordinatorState) -> str:
    """Name the ratified assertion(s) that upheld a BLOCKED verdict, if any.

    Reads the resolved-assertion records on ``state`` rather than re-adjudicating,
    so the cached-preflight and resume dispatch paths — which restore those records
    instead of running the adjudication — produce the same refusal text.
    """
    labels = [
        str(entry.get("label") or entry.get("text") or "")
        for entry in state.preflight_policy_assertions_resolved or []
        if isinstance(entry, dict) and entry.get("carries_blocking_authority")
    ]
    labels = [label for label in labels if label]
    if not labels:
        return ""
    return "Blocked by ratified policy assertion(s): " + "; ".join(labels)


def _adjudicate_policy_provenance(
    state: CoordinatorState,
    config: ForgeConfig,
    verdict: str,
    reason: str,
) -> tuple[str, str]:
    """Apply policy-assertion provenance authority to a BLOCKED verdict (#2137).

    Generated or unmarked policy prose may propose, but it may not stop chartered
    work: only an assertion the operator ratified — an ADR clause or a recorded
    operator decision, named in ``.forge/policy-assertions.yaml`` — carries blocking
    authority. A BLOCKED founded solely on unratified rationale is downgraded to
    PROCEED and the conflict is recorded as a retraction candidate instead.

    Blockers that are not policy claims (missing credentials, a direct
    specification contradiction, an absent external dependency) are untouched:
    ``adjudicate_blocked_verdict`` returns ``engaged=False`` for them.

    Returns the (possibly rewritten) ``(verdict, reason)`` and records the full
    adjudication on ``state`` for the artifact, resume record, and audit log.
    """
    if verdict != "BLOCKED":
        return verdict, reason

    registry = load_policy_assertions(config.project_root)
    citations = parse_citations(state.preflight_policy_assertions_cited)
    adjudication = adjudicate_blocked_verdict(
        reason=reason or "",
        blocking_basis=state.preflight_blocking_basis or "",
        citations=citations,
        registry=registry,
    )
    if not adjudication.engaged:
        return verdict, reason

    state.preflight_policy_adjudication = adjudication.audit_fields()
    state.preflight_policy_assertions_resolved = [r.to_dict() for r in adjudication.resolved]
    state.preflight_policy_retraction_candidates = [
        dict(c) for c in adjudication.retraction_candidates
    ]
    state.preflight_policy_ratification_candidates = [
        dict(c) for c in adjudication.ratification_candidates
    ]
    state.preflight_policy_blocking_authority = adjudication.upheld
    state.preflight_warnings = list(state.preflight_warnings or []) + list(adjudication.warnings)
    for warning in adjudication.warnings:
        _log(f"  ⚠ {warning}")

    if adjudication.upheld:
        _log(f"  ✗ PREFLIGHT BLOCKED upheld — {adjudication.refusal_detail()}")
        return verdict, reason

    downgrade_reason = (
        f"{reason} — downgraded to PROCEED: the cited policy assertion(s) carry no "
        "operator ratification, so this conflict is a retraction candidate rather "
        "than a blocker."
    )
    state.preflight_verdict = "PROCEED"
    state.preflight_reason = downgrade_reason
    for candidate in adjudication.retraction_candidates:
        _log(f"  ⓘ retraction candidate: {candidate['assertion']}")
    for candidate in adjudication.ratification_candidates:
        _log(f"  ⓘ ratification candidate: {candidate['assertion']}")
    return "PROCEED", downgrade_reason


def _sanitize_preflight_profile(
    profile: ModelProfile,
    *,
    log: "Callable[[str], None]",
) -> ModelProfile:
    """Seat the resolved preflight tool surface, whatever config supplied (#2346).

    The load-bearing guarantee against the wait-forever failure is tool surface,
    not instruction: with Bash the classifier can start a detached process, end
    its turn waiting for it, and be killed by the runner's post-stream grace
    period having inspected nothing. The default profile no longer grants it,
    but a forge.yaml override, an API-transport default set, or a fallback
    profile can each supply a different one — so the surface is *resolved* here,
    at the one place every preflight invocation passes through, rather than
    trusted from whatever built the profile.

    ``resolve_preflight_tools`` owns the rule and the reasoning; this function
    is the seam that applies it and reports what changed.
    """
    allowed = tuple(profile.allowed_tools or ())
    resolved = resolve_preflight_tools(allowed)
    if resolved == allowed:
        return profile
    # Name the delegation-capable drop separately from the rest. "dropped Bash"
    # is the story's whole subject and an operator should see it as such, not
    # buried in a list beside a tool that was merely off-phase.
    dropped = [t for t in allowed if t not in resolved]
    forbidden = [t for t in dropped if str(t).lower() in PREFLIGHT_FORBIDDEN_TOOLS]
    others = [t for t in dropped if t not in forbidden]
    parts = []
    if forbidden:
        parts.append(f"dropped {', '.join(forbidden)} (delegation-capable)")
    if others:
        parts.append(f"dropped {', '.join(others)} (not on the preflight allow-list)")
    if not dropped:
        parts.append("config named no allowed tool, and an empty allowlist is unrestricted")
    log(
        f"  ⓘ PREFLIGHT tool surface resolved to {', '.join(resolved)}: {'; '.join(parts)} "
        "(read-only classifier cannot delegate work it cannot be resumed for)"
    )
    return replace(profile, allowed_tools=resolved)


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
    preflight_profile = config.preflight_profile
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "PREFLIGHT",
                "iteration": 0,
                "cost_usd": state.total_cost_measured,
                "coordinator_state": state,
                **_cu.live_complexity_fields(
                    state.preflight_complexity, state.preflight_complexity_score
                ),
                "current_model": preflight_profile.model,
            }
        )
    _log_phase(state.phase, preflight_profile.model)
    if logger:
        logger._safe_emit("phase_start", phase="PREFLIGHT", iteration=0)

    # Preflight rendering is signal-only (ADR-0002 clause 5), so this manifest
    # contributes zero claims by construction — the role is recorded anyway so
    # the entry reads as captured-and-empty rather than uncaptured (#2684).
    preflight_context = ContextAssembler.from_config(config).assemble(
        phase="preflight",
        story_text=story_content,
        agent_role="preflight",
        phase_iteration=1,
    )
    state.context_manifests.append({"phase": "preflight", "manifest": preflight_context})
    preflight_prompt = build_preflight_prompt(
        task,
        story_content=story_content,
        assembled_context=preflight_context,
    )

    def _invoke_preflight(profile: ModelProfile, label: str) -> tuple[object, float]:
        profile = _sanitize_preflight_profile(profile, log=_log)
        baseline_working_dir, cleanup_preflight_dir = _prepare_preflight_working_dir(
            config.project_root, config.workspace.base_branch
        )
        try:
            _preflight_start = time.monotonic()
            result = run_agent(
                prompt=preflight_prompt,
                profile=profile,
                working_dir=baseline_working_dir,
                secrets=config.secrets,
            )
            elapsed = time.monotonic() - _preflight_start
        finally:
            cleanup_preflight_dir()
        log_agent_result(result, f"PREFLIGHT[{label}]")
        return result, elapsed

    def _is_parse_degraded(result: object) -> bool:
        """True when the agent exited cleanly but emitted unparseable output.

        Distinct from a crashed run (``success=False``): this is the
        parse_error case where preflight narrated instead of producing the
        structured dict, and re-requesting the same output contract is worth a
        cheap retry.
        """
        if not result.success:
            return False
        _verdict, _reason, parse_degraded = _parse_preflight_verdict(result.output)
        return parse_degraded

    def _attempt_failed(result: object) -> bool:
        if not result.success:
            return True
        return _is_parse_degraded(result)

    attempts: list[dict[str, object]] = []

    def _record_attempt(profile: ModelProfile, result: object, elapsed: float) -> None:
        attempts.append(
            {
                "profile_name": profile.name,
                "model": profile.model,
                "provider": profile.provider,
                "cli": profile.cli,
                "cost_usd": result.cost_usd,
                "duration_s": round(elapsed, 2),
                "success": result.success,
                "exit_code": result.exit_code,
                # Reliability completion (#1489): did this invocation return a
                # usable, parseable preflight result? A crash or a clean-exit
                # parse-degraded narration both fail; the fold attributes this to
                # the model that actually ran the attempt.
                "completed": not _attempt_failed(result),
                # Full invocation ledger for THIS attempt (#2205). Only the final
                # attempt reaches ``state.preflight_result`` and therefore
                # ``cost.agents``; a parse-retry or a fallback that ran and was
                # superseded exists nowhere else. Without this the earlier
                # attempts would keep the collapsed profile_name/model pair while
                # every other invocation in the run carried configured, resolved,
                # and billed identities separately.
                #
                # Explicit null rather than an omitted key when the attempt's
                # result is not a readable AgentResult. Defensive symmetry with
                # the same guard in ``audit_render.build_agent_entries``, where
                # the equivalent crash cost a run its whole audit record: "the
                # ledger was not recorded" is a fact a consumer can act on,
                # unlike a ledger built out of an unknown object's attributes.
                # Unreachable via the phase's own paths today — anything that
                # is not an AgentResult fails earlier on verdict parsing — so
                # this is deliberately untested rather than covered by a test
                # that would only re-assert the branch.
                "ledger": (
                    build_invocation_ledger(
                        result,
                        "preflight",
                        profile.name or "preflight",
                        # Complexity is what preflight is running to determine,
                        # so there is nothing truthful to stamp here.
                        complexity=None,
                        complexity_score=None,
                    )
                    if isinstance(result, AgentResult)
                    else None
                ),
            }
        )

    preflight_result, _preflight_elapsed = _invoke_preflight(preflight_profile, "primary")
    _record_attempt(preflight_profile, preflight_result, _preflight_elapsed)

    # ── Same-profile parse-error retry ──────────────────────────────────
    # A clean exit with unparseable output has not produced a preflight result;
    # re-issue the identical output contract to the same profile before any
    # fallback so a one-off narration does not silently discard all agent-derived
    # metadata (issue #1773). Only fires on parse_error (success=True but
    # malformed) — a crashed agent is handled by the risk-signal path below.
    parse_retry = 0
    while parse_retry < _PREFLIGHT_PARSE_RETRY_ATTEMPTS and _is_parse_degraded(preflight_result):
        parse_retry += 1
        _log(
            "  ⚠ PREFLIGHT output malformed (parse_error) — retrying same profile "
            f"{preflight_profile.model} (attempt {parse_retry}/{_PREFLIGHT_PARSE_RETRY_ATTEMPTS})"
        )
        retry_result, retry_elapsed = _invoke_preflight(
            preflight_profile, f"parse-retry-{parse_retry}"
        )
        _record_attempt(preflight_profile, retry_result, retry_elapsed)
        preflight_result = retry_result
        _preflight_elapsed += retry_elapsed

    fallback_profile = config.preflight_fallback_profile
    if fallback_profile is not None and _attempt_failed(preflight_result):
        _log(
            f"  ⚠ PREFLIGHT primary failed — retrying with fallback model {fallback_profile.model}"
        )
        fallback_result, fallback_elapsed = _invoke_preflight(fallback_profile, "fallback")
        _record_attempt(fallback_profile, fallback_result, fallback_elapsed)
        preflight_result = fallback_result
        _preflight_elapsed += fallback_elapsed

    preflight_raw = dict(preflight_result.raw)
    preflight_raw["attempts"] = attempts
    state.preflight_duration_s = _preflight_elapsed
    state.preflight_result = preflight_result.__class__(
        **{**preflight_result.__dict__, "raw": preflight_raw}
    )

    def _preserve_partial_evidence() -> None:
        # Preserve whatever exploration the preflight agent managed — files
        # inspected, tool calls, any partial conclusion — as an audit artifact
        # the plan phase can consume instead of re-reading the same files
        # (issue #706). Applies both when the agent dies (timeout/SIGKILL) and
        # when it exits cleanly but emits unparseable output: in both cases the
        # tool trace it left behind is real, paid-for signal.
        _partial_evidence = build_partial_evidence(
            preflight_result, duration_s=round(_preflight_elapsed, 2)
        )
        if not _partial_evidence.is_empty():
            state.preflight_partial_evidence = _partial_evidence.to_dict()
            _log(
                "  ⓘ PREFLIGHT partial evidence preserved: "
                f"{len(_partial_evidence.files_inspected)} file(s) inspected, "
                f"{len(_partial_evidence.tool_calls)} tool call(s)"
            )

    # Set when this preflight produced no model output at all (#1951); drives
    # the infrastructure-abort dispatch instead of a manufactured verdict.
    _invocation_failure = None
    if preflight_result.success:
        verdict, reason, _parse_degraded = _parse_preflight_verdict(preflight_result.output)
        if _parse_degraded:
            state.preflight_degraded = True
            state.preflight_degraded_reason = "parse_error"
            # A clean exit with malformed output is still a failed preflight —
            # salvage the exploration the same way as a crashed run.
            _preserve_partial_evidence()
            _log("  ⚠ PREFLIGHT output malformed — fallback PROCEED (degraded)")
    else:
        # Preflight agent failed (timeout, SIGKILL, non-zero exit).
        _preserve_partial_evidence()
        # ── No model output at all (#1951) ──────────────────────────────
        # Before weighing risk signals, ask whether a model spoke. When the
        # invocation died at the substrate — credential rejected, transport
        # dropped, process never started — there is no analysis to be confident
        # or unconfident about. "No risk signals detected" then means "nothing
        # looked", not "nothing is risky", and turning that into PROCEED
        # manufactures a story verdict no agent ever formed. Abort the run as an
        # infrastructure failure instead; the run makes no statement about the
        # story and teaches nothing.
        _invocation_failure = classify_agent_failure(
            preflight_result,
            phase="PREFLIGHT",
            profile_name=preflight_profile.name,
        )
        if _invocation_failure is not None:
            record_invocation_failure(state, _invocation_failure)
            state.preflight_degraded = True
            state.preflight_degraded_reason = f"no_model_output_{_invocation_failure.category}"
            state.preflight_failure_action = "infrastructure_abort"
            verdict = NO_JUDGMENT
            reason = (
                f"Preflight agent produced no model output "
                f"({_invocation_failure.summary()}) — no judgment was obtained about "
                "this story; aborting as an infrastructure failure rather than "
                "substituting a verdict."
            )
            _log(
                f"  ✗ PREFLIGHT   no model output "
                f"({_invocation_failure.summary()}) — infrastructure abort "
                "(no verdict recorded)"
            )
        else:
            # A model DID speak (partial output / tool trace salvaged from the
            # failed run) — the pre-existing risk-signal policy applies.
            # Whether it is safe to fall through to a conservative PROCEED depends
            # on whether preflight was a confidence boost or the load-bearing check
            # that catches contract drift. Detect risk signals deterministically
            # from local state — no extra agent calls — and escalate when any
            # signal indicates the story may be stale relative to the codebase.
            risk_signals = _detect_preflight_risk_signals(
                story_content,
                config.project_root,
                branch_name,
                config.workspace.base_branch,
            )
            state.preflight_risk_signals = risk_signals
            state.preflight_degraded = True
            if risk_signals:
                verdict = "BLOCKED"
                reason = (
                    f"Preflight agent failed (exit={preflight_result.exit_code}); "
                    f"risk signals present ({', '.join(risk_signals)}) — escalating "
                    "rather than silently sprinting on an unverified contract."
                )
                state.preflight_degraded_reason = "agent_failed_with_risk_signals"
                state.preflight_failure_action = "escalate"
                _log(
                    f"  ✗ PREFLIGHT failed (exit={preflight_result.exit_code}) with "
                    f"risk signals [{', '.join(risk_signals)}] — escalating"
                )
            else:
                verdict, reason = (
                    "PROCEED",
                    f"Preflight agent failed (exit={preflight_result.exit_code}); "
                    "no risk signals detected — falling back to conservative PROCEED.",
                )
                state.preflight_degraded_reason = "timeout_no_verdict"
                state.preflight_failure_action = "proceed"
                _log(
                    f"  ⚠ PREFLIGHT failed (exit={preflight_result.exit_code}) — "
                    "no risk signals; fallback PROCEED (degraded)"
                )

    # NO_JUDGMENT is not a verdict: leave state.preflight_verdict unset so no
    # consumer can read a story-level judgment that no model produced (#1951).
    # The local ``verdict`` still carries the marker so the audit artifact, the
    # live-status detail, and the terminal dispatch below all say explicitly
    # that no judgment was obtained.
    state.preflight_verdict = None if verdict == NO_JUDGMENT else verdict
    state.preflight_reason = reason
    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "PREFLIGHT",
                "iteration": 0,
                "cost_usd": state.total_cost_measured,
                "coordinator_state": state,
                # Fired before the parse step computes preflight_complexity_score,
                # so the score may still be None. live_complexity_fields omits the
                # key in that case, preserving any score an earlier phase set on a
                # mid-sprint re-run instead of clearing it.
                **_cu.live_complexity_fields(
                    state.preflight_complexity, state.preflight_complexity_score
                ),
                "current_model": preflight_profile.model,
                "detail": {
                    "preflight_verdict": verdict,
                    "preflight_sufficiency": state.preflight_sufficiency,
                    **_live_degraded_detail(state),
                },
            }
        )

    # ── Parsed preflight signals ────────────────────────────────────────
    if preflight_result.success:
        _warnings = _parse_preflight_warnings(preflight_result.output)
        state.preflight_warnings = _warnings
        _likely_files = _parse_preflight_likely_files(preflight_result.output)
        state.preflight_likely_files = _likely_files
        complexity_enum_raw = _parse_preflight_complexity(preflight_result.output)
        complexity_score = _parse_preflight_complexity_score(
            preflight_result.output, fallback_band=complexity_enum_raw
        )
        state.preflight_complexity_score = complexity_score
        # Legacy consumers read state.preflight_complexity via the compat shim.
        # When the agent emitted a numeric score, derive the enum from it so the
        # score is the single source of truth — this is the shim required by AC
        # "no consumer is silently broken". Fall back to the agent's raw enum
        # only when no score is available (parser returned None with no band).
        if complexity_score is not None:
            complexity = score_to_band(complexity_score)
        else:
            complexity = complexity_enum_raw
        state.preflight_complexity = complexity
        sufficiency = _parse_preflight_sufficiency(preflight_result.output)
        state.preflight_sufficiency = sufficiency
        work_type = _parse_preflight_work_type(preflight_result.output)
        # Structured story type wins over AI inference. A bug-typed story is
        # always work_type="bug" so AC-synthesis paths are skipped; an
        # enhancement/task/spike is normalized to "feature" so the
        # AC-verification flow runs. Epics are tracking-only and should never reach preflight,
        # but if one does we treat it as feature so the pipeline is at least
        # well-defined. AI-inferred refactor/mechanical signals are preserved
        # only when there is no structured type — those are sub-classifications
        # of feature work that the agent may legitimately refine.
        if task.type == "bug":
            work_type = "bug"
        elif task.type in {"enhancement", "task", "spike", "epic"}:
            if work_type not in {"refactor", "mechanical"}:
                work_type = "feature"
        state.preflight_work_type = work_type
        # Domain tags (issue #155): the horizontal routing axis. Recorded as
        # native structured telemetry so it is routing-safe as a current-run fact
        # (ADR-0006 bucket A), consumed as an admissible preference in assignment.
        state.preflight_domains = _parse_preflight_domains(preflight_result.output)
        contract_change = _parse_preflight_contract_change(preflight_result.output)
        state.preflight_contract_change = contract_change
        # preflight_bundle_candidate is no longer sourced from the preflight LLM —
        # the prompt never asked for it and the decision is relational (cross-story).
        # The sprint scheduler writes this field after compute_bundle_assignments.
        criteria_checked = _parse_preflight_criteria_checked(preflight_result.output)
        state.preflight_criteria_checked = criteria_checked
        symptom_verification = _parse_preflight_symptom_verification(preflight_result.output)
        state.preflight_symptom_verification = symptom_verification
        # Policy-assertion provenance inputs (#2137). Parsed for every verdict so
        # the audit trail records what the classifier cited even when the verdict
        # was not BLOCKED; the adjudication below only fires on BLOCKED.
        state.preflight_blocking_basis = _parse_preflight_blocking_basis(preflight_result.output)
        _policy_citations = _parse_preflight_policy_assertions(preflight_result.output)
        state.preflight_policy_assertions_cited = [c.to_dict() for c in _policy_citations]

        # ── Deterministic contract-change policy ───────────────────────
        # A contract change touches a shared interface field, prompt template
        # string, or schema field. The blast radius (callers, parsers, templates,
        # exact-string test assertions) cannot be safely enumerated by a dev agent
        # within its iteration budget. Force needs_planning so the plan phase
        # produces an exhaustive reference map before dev begins.
        if contract_change and sufficiency == "implementation_ready":
            sufficiency = "needs_planning"
            state.preflight_sufficiency = sufficiency
            _log(
                "  ↑ contract_change=true: overriding sufficiency to needs_planning "
                "(blast radius enumeration required)"
            )
        # Also ensure planning actually runs — plan phase is gated on medium/large.
        # The score is authoritative; the legacy enum is derived from that floor.
        if contract_change:
            implementation_score = state.preflight_complexity_score
            if implementation_score is None or implementation_score < 5:
                state.preflight_complexity_score = 5
                complexity = score_to_band(state.preflight_complexity_score)
                state.preflight_complexity = complexity
                _log(
                    "  ↑ contract_change=true: raising implementation "
                    "complexity_score to 5 "
                    f"(legacy band={complexity}; cross-cutting blast radius)"
                )

        # Bug stories describe symptom context (what went wrong, what was expected),
        # not fix scope. The keyword patterns match domain vocabulary in the bug
        # description, not the shape of the change required. Skip the override for
        # bugs — the preflight LLM reads the codebase and should size bugs correctly.
        # The override is load-bearing only for feature/refactor/mechanical stories
        # where imperative spec language reflects genuine cross-cutting scope.
        large_story_categories: list[str] = []
        if work_type != "bug":
            large_story_categories = _detect_large_preflight_story_categories(story_content)
            if large_story_categories and (
                complexity != "large"
                or state.preflight_complexity_score is None
                or state.preflight_complexity_score < 8
            ):
                complexity = "large"
                state.preflight_complexity = complexity
                score_too_low = (
                    state.preflight_complexity_score is None
                    or state.preflight_complexity_score < 8
                )
                if score_too_low:
                    state.preflight_complexity_score = 8
                override_reason = (
                    "coordinator override: upgraded complexity to large for "
                    + ", ".join(large_story_categories)
                )
                state.preflight_warnings = list(state.preflight_warnings or []) + [override_reason]
                _log(f"  ↑ {override_reason}")

        # ── Dual-axis complexity: implementation vs validation envelope ──
        # The (possibly overridden) model score above is the *implementation*
        # (code-change) envelope. Assess the *validation/execution* envelope
        # separately from body-local structural signals, then project the legacy
        # complexity_score = max(implementation, validation) so existing routing,
        # timeout, and review-budget consumers get the validation lift without
        # reading the new fields. Evidence on both axes makes the size auditable.
        implementation_score = state.preflight_complexity_score
        validation = assess_validation_complexity(story_content)
        projected_score, projection_rule = project_complexity_score(
            implementation_score, validation.score
        )
        combined_evidence = (
            implementation_evidence(
                implementation_score,
                large_categories=large_story_categories,
                contract_change=contract_change,
            )
            + validation.evidence
        )
        state.preflight_implementation_complexity_score = implementation_score
        state.preflight_validation_complexity_score = validation.score
        state.preflight_complexity_projection = projection_rule
        state.preflight_complexity_evidence = [e.as_dict() for e in combined_evidence]
        state.preflight_complexity_score = projected_score
        complexity = score_to_band(projected_score)
        state.preflight_complexity = complexity
        if validation.warnings:
            state.preflight_warnings = list(state.preflight_warnings or []) + validation.warnings
        if projected_score > (implementation_score or 0):
            _log(
                f"  ↑ validation-envelope lift: implementation={implementation_score} "
                f"validation={validation.score} → complexity_score={projected_score} "
                f"({projection_rule})"
            )
            for w in validation.warnings:
                _log(f"  ⚠ {w}")

        # ── Scope-exceeded signal (#2680) ─────────────────────────────
        # Read off the *implementation* axis at its ceiling, never off the
        # projected complexity_score: a validation-heavy story can project to
        # 10 while its code change is one coherent unit, and reporting that as
        # over scope would tell the operator to split work that is not
        # divisible. Routing is untouched — 9 and 10 land in the same buckets
        # on every axis — so this is a readable signal, not a routing input.
        scope_exceeded = implementation_score == COMPLEXITY_SCORE_MAX
        state.preflight_scope_exceeded = scope_exceeded
        claimed_scope_exceeded = _parse_preflight_scope_exceeded(preflight_result.output)
        if claimed_scope_exceeded is not None and claimed_scope_exceeded != scope_exceeded:
            # The classifier contradicted its own score. The score is
            # authoritative — it is what every coordinator override writes and
            # what routing reads — so the claim is recorded, not obeyed.
            disagreement = (
                f"preflight emitted scope_exceeded={claimed_scope_exceeded} with "
                f"implementation complexity_score={implementation_score}; recorded "
                f"scope_exceeded={scope_exceeded} from the implementation axis"
            )
            state.preflight_warnings = list(state.preflight_warnings or []) + [disagreement]
            _log(f"  ⚠ {disagreement}")
        elif scope_exceeded:
            _log(
                "  ⚠ scope_exceeded: implementation complexity is at the ceiling "
                f"({COMPLEXITY_SCORE_MAX}) — this story should be decomposed"
            )

        # ── Bounded-bug planning skip ─────────────────────────────────
        # Bounded, diagnosed bugs whose fix is localized to a single area
        # do not benefit from the plan + plan-review pipeline. The plan
        # agent's multi-step decomposition buys nothing when the dev would
        # have made the same single-area fix without one. Downgrade
        # needs_planning → implementation_ready when ALL hold:
        #   - work_type is "bug"           (symptom + expected, not new feature)
        #   - complexity is "small"        (bounded scope per preflight sizing)
        #   - contract_change is False     (no shared-interface blast radius)
        # The contract-change override above runs first and is load-bearing:
        # contract_change=true forces needs_planning + ≥medium complexity, so
        # this gate cannot accidentally skip planning for cross-cutting work.
        if (
            sufficiency == "needs_planning"
            and work_type == "bug"
            and complexity == "small"
            and not contract_change
        ):
            sufficiency = "implementation_ready"
            state.preflight_sufficiency = sufficiency
            override_reason = (
                "coordinator override: bounded bug (work_type=bug, complexity=small, "
                "contract_change=false) → implementation_ready (skip plan pipeline)"
            )
            state.preflight_warnings = list(state.preflight_warnings or []) + [override_reason]
            _log(f"  ↓ {override_reason}")

        if state.preflight_degraded:
            _log(
                f"  Complexity: {complexity} (conservative degraded fallback — "
                f"no founded classification: "
                f"{state.preflight_degraded_reason or 'preflight_degraded'})"
            )
        else:
            _log(f"  Complexity: {complexity} (from preflight)")
        _log(f"  Sufficiency: {sufficiency}")
        _log(f"  Work type: {work_type}")
        _log(f"  Contract change: {contract_change}")
        if _warnings:
            _log(f"  ⚠ PREFLIGHT warnings: {'; '.join(_warnings)}")
        if _likely_files is not None:
            _log(f"  Likely files: {', '.join(_likely_files)}")
        for entry in criteria_checked:
            criterion = entry.get("criterion", "(unnamed)")
            files_checked = entry.get("files_checked") or []
            runtime_path = entry.get("runtime_path", "")
            evidence = " ".join(str(entry.get("evidence", "")).split())
            if len(evidence) > 200:
                evidence = f"{evidence[:197]}..."
            _log_verbose(
                "  Criteria checked: "
                f"criterion={criterion!r} "
                f"satisfied={entry.get('satisfied', False)} "
                f"files_checked={files_checked!r} "
                f"runtime_path={runtime_path!r} "
                f"evidence={evidence!r}"
            )

        # ── ALREADY_DONE evidence downgrade ───────────────────────────
        # Require that every AC has concrete file evidence (files_checked is
        # non-empty) and is marked satisfied=True.  An empty or absent
        # criteria_checked map is itself treated as missing evidence —
        # fail-safe toward PROCEED rather than silently honoring the verdict.
        if verdict == "ALREADY_DONE":
            downgrade_reasons: list[str] = []
            if not criteria_checked:
                downgrade_reasons.append(
                    "criteria_checked absent or empty; cannot verify ALREADY_DONE"
                )
            else:
                for entry in criteria_checked:
                    criterion = entry.get("criterion", "(unnamed)")
                    runtime_path = str(entry.get("runtime_path", "")).strip()
                    evidence = str(entry.get("evidence", "")).strip()
                    if not entry.get("satisfied"):
                        downgrade_reasons.append(f"AC '{criterion}' marked satisfied=false")
                    elif not entry.get("files_checked"):
                        downgrade_reasons.append(
                            f"AC '{criterion}' lacked concrete evidence (no files_checked)"
                        )
                    elif not runtime_path:
                        downgrade_reasons.append(
                            f"AC '{criterion}' lacked runtime-path evidence (runtime_path missing)"
                        )
                    elif not evidence:
                        downgrade_reasons.append(
                            f"AC '{criterion}' lacked concrete evidence (evidence missing)"
                        )
                    elif _evidence_is_too_thin(evidence):
                        downgrade_reasons.append(
                            f"AC '{criterion}' evidence too thin for ALREADY_DONE "
                            f"(len={len(evidence)})"
                        )
            # ── Symptom-verification gate for bug stories ─────────────────
            # AC-evidence checks above prove that named acceptance criteria
            # are observable in the live code. For bug stories the verdict
            # ALREADY_DONE additionally requires that the originally observed
            # symptom no longer reproduces — refuting a hypothesized cause in
            # the body's Diagnosis section is not equivalent to confirming the
            # defect is fixed. Require an explicit symptom_verification block
            # asserting status=verified_resolved with non-thin evidence;
            # otherwise demote to PROCEED so the dev cycle exercises the
            # symptom against the current baseline.
            if work_type == "bug":
                sv_status = str(symptom_verification.get("status") or "").strip()
                sv_evidence = str(symptom_verification.get("evidence") or "").strip()
                reproduces_now = symptom_verification.get("reproduces_now")
                if not symptom_verification or not sv_status:
                    downgrade_reasons.append(
                        "symptom_verification absent — bug ALREADY_DONE requires "
                        "evidence the originally observed symptom does not "
                        "reproduce against the current baseline; refuting a "
                        "diagnosis hypothesis is not symptom verification"
                    )
                elif sv_status != "verified_resolved":
                    downgrade_reasons.append(
                        f"symptom_verification.status={sv_status!r} — only "
                        "'verified_resolved' justifies bug ALREADY_DONE; "
                        "demoting to PROCEED so the dev cycle reproduces and "
                        "verifies symptom resolution"
                    )
                elif reproduces_now is True:
                    downgrade_reasons.append(
                        "symptom_verification.reproduces_now=true — symptom "
                        "still reproduces against the current baseline; "
                        "ALREADY_DONE is not justified"
                    )
                elif not sv_evidence or _evidence_is_too_thin(sv_evidence):
                    downgrade_reasons.append(
                        "symptom_verification.evidence missing or too thin "
                        f"(len={len(sv_evidence)}) — bug ALREADY_DONE requires "
                        "concrete evidence the symptom was exercised and did "
                        "not reproduce"
                    )

            if downgrade_reasons:
                verdict = "PROCEED"
                state.preflight_verdict = verdict
                for dr in downgrade_reasons:
                    _log(f"  ⚠ ALREADY_DONE downgraded → PROCEED: {dr}")
                state.preflight_warnings = list(state.preflight_warnings or []) + downgrade_reasons

        # ── Ambiguity BLOCKED downgrade ───────────────────────────────
        # Must come *after* all signal parsing so that forced overrides are
        # not later clobbered by the parsers.
        if verdict == "BLOCKED" and any(t in (reason or "").lower() for t in _AMBIGUITY_TOKENS):
            if _has_prior_execution_evidence(
                config.project_root, branch_name, config.workspace.base_branch
            ):
                _log(
                    "  ⚠ PREFLIGHT BLOCKED (ambiguity) overridden — prior execution evidence found"
                )
                verdict = "PROCEED"
                state.preflight_verdict = verdict
                state.preflight_degraded = True
                state.preflight_degraded_reason = "blocked_downgraded_prior_evidence"
                # Compensate for missing classification: force planning and
                # upgrade complexity to at least medium. Route the bump through
                # the dual-axis projection so implementation/validation scores,
                # the projection rule, and cited evidence stay consistent with
                # the final complexity_score rather than going stale (issue #1442).
                if state.preflight_complexity == "small":
                    impl_floor = state.preflight_implementation_complexity_score
                    if impl_floor is None or impl_floor < 5:
                        impl_floor = 5
                    validation_score = (
                        state.preflight_validation_complexity_score
                        if state.preflight_validation_complexity_score is not None
                        else VALIDATION_BASELINE_SCORE
                    )
                    projected_score, projection_rule = project_complexity_score(
                        impl_floor, validation_score
                    )
                    state.preflight_implementation_complexity_score = impl_floor
                    state.preflight_validation_complexity_score = validation_score
                    state.preflight_complexity_score = projected_score
                    state.preflight_complexity = score_to_band(projected_score)
                    state.preflight_complexity_projection = projection_rule
                    override_ev = ComplexityEvidence(
                        "implementation_ambiguity_downgrade_floor",
                        "BLOCKED→PROCEED ambiguity downgrade with prior execution "
                        "evidence: raised implementation floor to 5 to force planning",
                        DIMENSION_IMPLEMENTATION,
                    ).as_dict()
                    state.preflight_complexity_evidence = [
                        *(state.preflight_complexity_evidence or []),
                        override_ev,
                    ]
                state.preflight_sufficiency = "needs_planning"
    else:
        # Agent failed — skip all parsers; hard-set conservative values so
        # downstream phases plan carefully despite missing classification.
        # The validation envelope is still assessable from the story body alone
        # (it needs no codebase read), so both axes stay present per AC.
        _failed_validation = assess_validation_complexity(story_content)
        _failed_projected, _failed_projection = project_complexity_score(
            9, _failed_validation.score
        )
        state.preflight_complexity = score_to_band(_failed_projected)
        state.preflight_complexity_score = _failed_projected
        state.preflight_implementation_complexity_score = 9
        state.preflight_validation_complexity_score = _failed_validation.score
        state.preflight_complexity_projection = _failed_projection
        state.preflight_complexity_evidence = [
            e.as_dict()
            for e in implementation_evidence(9, agent_failed=True) + _failed_validation.evidence
        ]
        state.preflight_sufficiency = "needs_planning"
        if task.type == "bug":
            state.preflight_work_type = "bug"
        else:
            state.preflight_work_type = "feature"
        # Say what this number is. The success branch logs "(from preflight)";
        # printing the same phrasing here is how a run that inspected zero files
        # came to report a founded-looking classification (#2346).
        _log(
            f"  Complexity: {state.preflight_complexity} (conservative degraded "
            f"fallback — no founded classification: "
            f"{state.preflight_degraded_reason or 'preflight_failed'})"
        )

    # ── Policy-assertion provenance adjudication (#2137) ────────────────
    # Runs on both the parsed and the degraded path, and *before* the artifact
    # write, the resume record, and the stop_phase return — so a batch preflight
    # that stops here caches the adjudicated verdict rather than the raw one.
    verdict, reason = _adjudicate_policy_provenance(state, config, verdict, reason)

    if state_update_fn is not None:
        state_update_fn(
            {
                "phase": "PREFLIGHT",
                "iteration": 0,
                "cost_usd": state.total_cost_measured,
                "coordinator_state": state,
                # Fired after the parse step; by now preflight_complexity_score is
                # set whenever the agent emitted a numeric score, so the score
                # reaches live state immediately for stories that never enter DEV.
                **_cu.live_complexity_fields(
                    state.preflight_complexity, state.preflight_complexity_score
                ),
                "detail": {
                    # Surface the explicit no-judgment marker rather than a null
                    # that an operator would read as "not run yet" (#1951).
                    "preflight_verdict": state.preflight_verdict or verdict,
                    "preflight_sufficiency": state.preflight_sufficiency,
                    **_live_degraded_detail(state),
                },
            }
        )

    config = _apply_preflight_config(
        config, state, log=_log, log_verbose=_log_verbose, task_slug=task.slug
    )
    # Durable copy of the decision just installed: the in-memory preflight state
    # this run holds does not survive a mid-sprint process re-exec, and a resume
    # without it would seat the static roster instead of this panel (#2154).
    persist_routing_decision(
        config,
        state,
        task_slug=task.slug,
        story_content=story_content,
        run_id=getattr(logger, "_run_id", None),
        log_verbose=_log_verbose,
    )
    # Durable copy of the preflight judgement itself, alongside the routing
    # decision derived from it. A resumed attempt allocates a fresh
    # CoordinatorState, so without this the audit for a story whose preflight
    # demonstrably ran reports it as never having run (#2155).
    save_resume_record(
        config.project_root,
        state,
        slug=task.slug,
        story_content=story_content,
        run_id=getattr(logger, "_run_id", None),
    )

    _log(f"  {'✗' if verdict == NO_JUDGMENT else '✓'} PREFLIGHT   {verdict}")
    _log_verbose(f"  Reason: {reason}")
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="PREFLIGHT",
            outcome=verdict.lower(),
            cost_usd=preflight_result.cost_usd,
            duration_s=round(_preflight_elapsed, 2),
            **_preflight_phase_end_fields(state),
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
        "complexity_score": state.preflight_complexity_score,
        "implementation_complexity_score": state.preflight_implementation_complexity_score,
        "validation_complexity_score": state.preflight_validation_complexity_score,
        "complexity_projection": state.preflight_complexity_projection,
        "complexity_evidence": list(state.preflight_complexity_evidence or []),
        # Distinct from the numeric score: says the work should be split, so a
        # consumer never has to infer decomposition from a magnitude (#2680).
        "scope_exceeded": state.preflight_scope_exceeded,
        "sufficiency": state.preflight_sufficiency,
        "contract_change": state.preflight_contract_change,
        "domains": list(state.preflight_domains or []),
        "cost_usd": preflight_result.cost_usd,
        "duration_s": round(_preflight_elapsed, 2),
        "likely_files": state.preflight_likely_files,
        "bundle_candidate": state.preflight_bundle_candidate,
        "batch_group": state.preflight_batch_group,
        "branch_merged": branch_merged,
        "evaluation_base_branch": config.workspace.base_branch,
        "cache_snapshot": capture_preflight_cache_snapshot(
            config=config,
            workspace_path=workspace_path,
            story_content=story_content,
        ),
        "criteria_checked": state.preflight_criteria_checked,
        "symptom_verification": dict(state.preflight_symptom_verification or {}),
        # Policy-assertion provenance (#2137): which kind of blocker fired, what
        # standing policy the classifier cited, how each citation resolved, and
        # the retraction/ratification candidates the adjudication produced.
        "blocking_basis": state.preflight_blocking_basis,
        "policy_assertions_cited": list(state.preflight_policy_assertions_cited or []),
        "policy_assertions_resolved": list(state.preflight_policy_assertions_resolved or []),
        "policy_retraction_candidates": list(state.preflight_policy_retraction_candidates or []),
        "policy_ratification_candidates": list(
            state.preflight_policy_ratification_candidates or []
        ),
        "policy_blocking_authority": state.preflight_policy_blocking_authority,
        "policy_adjudication": dict(state.preflight_policy_adjudication or {}),
        "degraded": state.preflight_degraded,
        "degraded_reason": state.preflight_degraded_reason,
        "risk_signals": list(state.preflight_risk_signals),
        "failure_action": state.preflight_failure_action,
        # Provenance of the complexity figure above, in the same block that says
        # the phase was degraded, so a reader of the artifact alone can tell a
        # founded classification from a conservative fallback (#2346).
        "complexity_source": complexity_source(state),
        "partial_evidence": state.preflight_partial_evidence,
        # Invocations that produced no model output at all (#1951). Present in
        # the artifact so "no judgment obtained" is inspectable next to the
        # verdict field rather than inferred from a missing one.
        "agent_invocation_failures": list(state.agent_invocation_failures),
        "attempts": attempts,
    }
    state.preflight_cache_snapshot = dict(_preflight_artifact["cache_snapshot"])
    _write_log_artifact(
        state.log_dir,
        "preflight-raw.log",
        preflight_result.output or "",
        owner_run_id=state.run_id,
    )
    _write_log_artifact(
        state.log_dir,
        "preflight.yaml",
        yaml.dump(_preflight_artifact, default_flow_style=False, allow_unicode=True),
        owner_run_id=state.run_id,
    )
    if state.preflight_partial_evidence is not None:
        _write_log_artifact(
            state.log_dir,
            "preflight-partial-evidence.yaml",
            yaml.dump(
                state.preflight_partial_evidence,
                default_flow_style=False,
                allow_unicode=True,
            ),
            owner_run_id=state.run_id,
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

    return _handle_preflight_verdict(
        verdict=verdict,
        reason=reason,
        state=state,
        config=config,
        task=task,
        branch_name=branch_name,
        notify=notify,
        logger=logger,
        task_start=task_start,
        branch_merged=branch_merged,
        invocation_failure=_invocation_failure,
    )


def _handle_preflight_verdict(
    verdict: str,
    reason: str | None,
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    branch_name: str,
    *,
    notify: bool,
    logger: "StructuredLogger | None",
    task_start: float,
    branch_merged: bool | None = None,
    invocation_failure: "AgentInvocationFailure | None" = None,
) -> tuple[ForgeConfig, CoordinatorResult | None, bool]:
    """Dispatch on a preflight verdict and return the standard 3-tuple.

    Returns ``(updated_config, result, already_done_loop)``:

    - ``result is not None`` — stop; caller returns ``result`` immediately.
    - ``result is None, already_done_loop is True`` — ALREADY_DONE override;
      caller enters ``_coordinator_loop`` with ``skip_dev_first_iter=True``.
    - ``result is None, already_done_loop is False`` — PROCEED; caller
      continues to ``_run_plan_phase``.
    """
    # ── Preflight complexity gate (#2681) ─────────────────────────────
    # Anchored here, at the one handoff every preflight path passes through
    # (fresh, cached, resumed), rather than at the head of PLAN: a story
    # preflight classified implementation_ready skips planning altogether and
    # must still stop before the first cost-bearing phase that follows. Only a
    # PROCEED verdict reaches the gate — ALREADY_DONE, BLOCKED, and NO_JUDGMENT
    # are terminal below and spend nothing further either way.
    _gate_result = evaluate_preflight_complexity_gate(state, config, task, verdict, logger=logger)
    if _gate_result is not None:
        return config, _gate_result, False

    # ── NO_JUDGMENT (infrastructure abort, #1951) ─────────────────────
    # The preflight invocation produced no model output, so there is no verdict
    # to dispatch on. End the run as an infrastructure failure: the terminal
    # phase stays ESCALATE (the state machine has one terminal failure state),
    # but the run is marked so no consumer reads it as a story-level judgment
    # and the taint marker keeps it out of every routing aggregate.
    if verdict == NO_JUDGMENT:
        _failure = invocation_failure or AgentInvocationFailure(
            phase="PREFLIGHT", category=CATEGORY_PROCESS
        )
        state.phase = Phase.ESCALATE
        state.error = reason or "Preflight produced no model output."
        mark_infrastructure_abort(state, _failure, message=state.error)
        _log(f"✗ ABORT   infrastructure failure: {state.error}")
        if logger:
            logger._safe_emit(
                "infrastructure_abort",
                phase="PREFLIGHT",
                reason=state.error,
                category=_failure.category,
            )
            logger._safe_emit(
                "run_end",
                outcome="infrastructure_abort",
                total_cost_usd=_round_cost(state.total_cost_measured),
                total_duration_s=round(time.monotonic() - task_start, 2),
            )
        # No _escalate_notify: an escalation notification asserts that the story
        # needs human judgment about its framing. Nothing was learned about the
        # story here — only that the substrate is down.
        return (
            config,
            CoordinatorResult(
                success=False,
                phase=state.phase,
                state=state,
                message=state.error,
                infrastructure_failure=True,
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
                    **_preflight_phase_end_fields(state),
                )
            # Signal caller to enter coordinator loop with skip_dev_first_iter=True
            return config, None, True

        state.phase = Phase.DONE
        elapsed = time.monotonic() - task_start
        _log(
            f"✓ DONE   total={_fmt_cost_total(state.total_cost_measured, state.total_cost)}"
            f"  {_fmt_duration(elapsed)}"
        )
        if logger:
            logger._safe_emit(
                "run_end",
                outcome="already_done",
                total_cost_usd=_round_cost(state.total_cost_measured),
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
        # A refusal that rests on a policy assertion must name the assertion and
        # the kind of authority behind it, so the operator can see which stopped
        # the work without reading git history (#2137). Rebuilt from state rather
        # than passed in, so a cached or resumed BLOCKED names it too.
        _assertion_detail = _policy_refusal_detail(state)
        if _assertion_detail:
            state.error = f"{state.error} {_assertion_detail}"
        _log(f"✗ ESCALATE   {state.error}")
        if logger:
            logger._safe_emit("escalate", reason=state.error, phase="PREFLIGHT")
            logger._safe_emit(
                "run_end",
                outcome="escalate",
                total_cost_usd=_round_cost(state.total_cost_measured),
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
