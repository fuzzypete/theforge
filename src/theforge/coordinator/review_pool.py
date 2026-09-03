"""Review pool runner: fan-out to multiple reviewers and merge results.

Orchestrates the multi-reviewer pool for a single review cycle:
- Dispatches prompts to the review pool via run_agent_pool
- Enforces per-profile budgets
- Retries parse errors per-reviewer
- Merges results deterministically (strictest verdict wins, findings unioned)
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from theforge import knowledge_receipts
from theforge.config import ForgeConfig
from theforge.coordinator.context_scope import plan_file_list
from theforge.handoff_claim_checker import check_handoff_fix_claims
from theforge.review import (
    ReviewResult,
    _try_parse_review,
    merge_review_results,
    parse_review_json,
    parse_review_output,
)
from theforge.reviewer_value import code_review_anchor_text, compute_reviewer_uniqueness
from theforge.sessions import save_sessions
from theforge.task import ContextAssembler, TaskStory, build_review_prompt
from theforge.traces import write_trace

from . import story_budget as _story_budget
from . import util as _cu
from .agent_failure import produced_model_output
from .agent_identity import resolved_identity_for_result
from .log_tee import _write_log_artifact
from .review_context import (
    _get_commit_diffs,
    _get_commit_log,
    _get_dev_notes,
    _get_diff_content,
    _get_diff_stat,
    _get_handoff_commit_warning,
    _get_handoff_content,
    _latest_forge_handoff_path,
    _parse_dev_handoff,
    evaluate_reviewer_tree_currency,
    hard_convention_review_kwargs,
)
from .review_context import (
    gate_profile_prompt_kwargs as _gate_profile_prompt_kwargs,
)
from .reviewer_progress import ReviewerProgressChannel
from .state import CoordinatorState, Phase, ReviewCycleMetadata

if TYPE_CHECKING:
    pass

_log = _cu._log
_log_verbose = _cu._log_verbose


def _is_transient_review_failure(result: Any, config: ForgeConfig) -> bool:
    """Return True when a failed review-pool invocation looks transient/retryable.

    Treats three signatures as transient:
    - failure_code matches a configured transient code
    - output text matches a configured transient signature substring
    - the spec's exit=1-with-no-model-output case (success=False and empty output)
    """
    if result.success:
        return False
    failure_code = (getattr(result, "failure_code", None) or "").lower()
    transient_codes = {c.lower() for c in config.retry.review_transient_failure_codes}
    if failure_code in transient_codes:
        return True
    output = (result.output or "").strip()
    if not output:
        return True
    lower = output.lower()
    return any(pattern in lower for pattern in config.retry.review_transient_output_patterns)


# Corrective YAML structure appended to every review-retry prompt. Every field
# the reviewer emitted originally appears here so "reformat" never reads as
# "drop the sections that are hard to encode".
_CORRECTIVE_YAML_STRUCTURE = (
    "verdict: APPROVE | REQUEST_CHANGES\n"
    'summary: "one-line summary"\n'
    "findings:\n"
    "  - severity: P1 | P2\n"
    '    file: "path"   # null for architectural findings\n'
    "    line: <number or null>   # null for file-scope findings "
    "(file existence, whole-file state, structural hygiene)\n"
    '    observed: "what is wrong"\n'
    '    expected: "category-level rule that generalises"\n'
    '    evidence: "file path, line, or anchor"\n'
    '    suggestion: "how to fix (optional)"\n'
    "story_compliance:\n"
    "  matches_spec: true | false\n"
    "  mismatches: []\n"
    "test_coverage:\n"
    "  adequate: true | false\n"
    "  gaps: []\n"
    "ac_verification:\n"
    "  - criterion: \"verbatim AC text, or 'Symptom resolution: ...' for bugs\"\n"
    "    status: VERIFIED | PARTIAL | NOT_VERIFIED\n"
    '    evidence: "file:line diff hunks (VERIFIED) or reason (otherwise)"\n'
    "criteria_enumerable: true   # false ONLY if the issue has no enumerable AC\n"
    'criteria_enumerable_rationale: ""   # required (non-empty) when '
    "criteria_enumerable is false\n"
    "Do not drop a P1 finding to satisfy a line-number requirement: "
    "line: null is valid when the defect is the existence, absence, mode, "
    "or whole-file state of the path.\n"
)


def _build_review_retry_prompt(error_desc: str, original_output: str) -> str:
    """Build a corrective retry prompt that anchors the reviewer's prior content.

    A retry prompt that asks an agent to fix formatting must communicate that the
    *content* of the previous output is correct and only the *encoding* needs
    repair. Reviewers are unreliable at distinguishing "fix the quoting of this
    value" from "emit a different value that won't have the same problem" — the
    latter (dropping ac_verification to dodge the APPROVE-needs-verified-AC rule,
    or flipping the verdict to dodge a cross-validation) produces an oscillation
    trap. This prompt pins the prior output as the anchor and names dropping a
    field as an explicitly wrong response, removing that move from the reachable
    space. ``original_output`` is the reviewer's verbatim prior emission.
    """
    return (
        "Your previous review output failed schema/parse validation:\n" + error_desc + "\n\n"
        "The CONTENT of your previous review is correct — your verdict, your "
        "findings, and your ac_verification entries are what you meant to say. "
        "ONLY the ENCODING (YAML formatting) is broken. Re-emit the SAME review "
        "with the formatting error fixed.\n\n"
        "Preserve your previous output verbatim:\n"
        "  - Keep the SAME verdict. Do NOT switch APPROVE <-> REQUEST_CHANGES.\n"
        "  - Keep EVERY finding. Do NOT drop, merge, or downgrade a finding.\n"
        "  - Keep EVERY ac_verification entry, with the same criterion, status, "
        "and evidence.\n\n"
        "Dropping or altering a field to make a validation error go away is a "
        "WRONG response. If a value failed to parse, fix the QUOTING/ESCAPING of "
        "that value — do NOT delete the value, and do NOT change the verdict to "
        "sidestep a cross-validation rule. If your verdict is APPROVE and your "
        "ac_verification keeps failing to encode, that is a formatting problem to "
        "solve; it is NOT a reason to empty the table or switch to "
        "REQUEST_CHANGES. (If the issue genuinely has no enumerable acceptance "
        "criteria, set criteria_enumerable: false with a rationale rather than "
        "dropping the table.)\n\n"
        "Do NOT re-review the code.\n\n"
        "Required YAML structure:\n"
        + _CORRECTIVE_YAML_STRUCTURE
        + "\n\nYour previous output (fix its formatting, keep its content):\n"
        + original_output
    )


# Failure codes marking a reviewer that completed its turn without delivering a
# verdict (no submit call). These are infrastructure/behavioral failures, not
# review outcomes, and must not be able to kill a story on their own.
_NON_VERDICT_FAILURE_CODES = frozenset({"no_submit_completion"})


def _is_non_verdict_completion(result: Any) -> bool:
    """Return True when a failed reviewer completed cleanly but emitted no verdict.

    A reviewer that finishes its turn without calling submit delivered nothing to
    synthesize. It is an infrastructure failure (the runner surfaces it via the
    ``no_submit_completion`` failure code), not a REQUEST_CHANGES outcome, so the
    pool can recover from it (degrade to surviving verdicts) rather than fail.
    """
    if result.success:
        return False
    code = (getattr(result, "failure_code", None) or "").lower()
    return code in _NON_VERDICT_FAILURE_CODES


def _result_is_parseable(result: Any) -> bool:
    """Return True iff a successful result's output parses to a valid verdict (#1388).

    Parsing review output is a pure function (no LLM, no I/O), so parseability can
    always be *established* directly rather than *proxied* from transport success.
    This is the single source of truth for ``completed_parseable_verdict`` wherever
    the normal parse step has not already determined it — budget-excluded reviewers
    and early escalation returns never reach that step, and transport success alone
    must NEVER be counted as a parseable verdict (the recurring corruption this
    guards against). A failed-transport result is never parseable.
    """
    if not result.success:
        return False
    if getattr(result, "structured_data", None):
        parsed = parse_review_json(result.structured_data, result.profile_name)
    else:
        parsed = parse_review_output(result.output or "", result.profile_name)
    return not parsed.parse_errors


def _classify_reviewer_attempt(
    result: Any, parseable: bool, config: ForgeConfig
) -> tuple[bool, str, str | None]:
    """Classify one reviewer invocation into (completed, outcome, failure_reason).

    ``completed`` is the single load-bearing signal (#1388): did the reviewer
    return a schema-valid verdict the coordinator can act on? Every non-success
    transport/timeout/crash and every transport-success-but-unparseable result
    records ``completed=False``. ``outcome`` is a coarse audit category; only
    ``completed`` carries routing weight. Runner exceptions are already normalized
    into failed ``AgentResult``s upstream (``runners/cli.py``), so every attempt is
    observable here as a result object, never a raised exception.
    """
    if result.success and parseable:
        return True, "completed", None
    if result.success and not parseable:
        return False, "parse_failure", "returned output but no parseable verdict"
    code = (getattr(result, "failure_code", None) or "").lower()
    if code == "timeout":
        return False, "timeout", "runner timeout"
    if code in _NON_VERDICT_FAILURE_CODES:
        return False, "non_verdict", "completed turn without submitting a verdict"
    if getattr(result, "startup_failure", False):
        return False, "transport_failure", "runner startup failure"
    if _is_transient_review_failure(result, config):
        return False, "transport_failure", code or "transient transport failure"
    return False, "crash", code or f"exit={getattr(result, 'exit_code', None)}"


def _append_reviewer_attempt(
    state: CoordinatorState,
    config: ForgeConfig,
    profile: Any,
    result: Any,
    *,
    parseable: bool,
    cycle_num: int,
    outcome_override: str | None = None,
    reason_override: str | None = None,
) -> None:
    """Append a single reviewer-attempt record for one invocation (#1388).

    One invocation → one record. Used both for the final per-reviewer result and
    for superseded transient-failure invocations that a later retry replaced — a
    failed invocation is still an invocation and must not evaporate from the audit
    / completion-rate evidence.
    """
    from theforge.model_profiles_identity import canonical_id_from_identity  # noqa: PLC0415

    completed, outcome, reason = _classify_reviewer_attempt(result, parseable, config)
    if outcome_override is not None:
        outcome = outcome_override
    if reason_override is not None:
        reason = reason_override
    state.reviewer_attempts.append(
        {
            "name": profile.name,
            "provider": getattr(profile, "provider", None),
            "model": getattr(profile, "model", None),
            "cli": getattr(profile, "cli", None),
            "canonical_id": canonical_id_from_identity(
                actual_model=getattr(profile, "model", None),
                provider=getattr(profile, "provider", None),
                cli=getattr(profile, "cli", None),
            ),
            # The concrete model that served THIS invocation (#2226). The
            # identity fields above come off the profile — what was selected —
            # which for a family alias does not say what ran.
            "resolved_model": resolved_identity_for_result(result),
            "completed_parseable_verdict": bool(completed),
            "outcome": outcome,
            "failure_reason": reason,
            "cycle": int(cycle_num),
        }
    )


def _record_reviewer_attempts(
    state: CoordinatorState,
    config: ForgeConfig,
    pool: list,
    results: list,
    *,
    cycle_num: int,
    parsed_by_name: dict[str, bool] | None = None,
    budget_overruns: set[str] | None = None,
) -> None:
    """Append the final attempt record per invoked reviewer (#1388).

    Writes into ``state.reviewer_attempts`` — the native per-run capture that is
    both persisted into the audit record and folded into the derived
    completion-rate profile. Every reviewer that produced a result this cycle gets
    a record regardless of outcome, closing the survivorship-bias gap where a
    reviewer that timed out / crashed / returned unparseable output evaporated
    from the profile. ``parsed_by_name`` maps reviewer name → whether its initial
    output was a parseable verdict (from the parse step). When a reviewer is absent
    from that map — an early escalation return that never reached the parse step —
    parseability is established by parsing the output directly
    (:func:`_result_is_parseable`), NEVER proxied from transport success. Counting
    transport success as a parseable verdict is the recurring corruption this
    guards against.

    ``budget_overruns`` names reviewers whose measured cost exceeded their derived
    share. Since #2169 that is telemetry, not an exclusion: the attempt records a
    ``budget_overrun`` outcome and the reviewer's verdict still counts.

    This records the FINAL result per reviewer. Superseded transient-failure
    invocations (a fail that a later retry replaced) are recorded separately, as
    they happen, inside :func:`_retry_transient_review_failures` — so a
    fail→retry→success reviewer contributes both a failed and a completed attempt.
    """
    budget_overruns = budget_overruns or set()
    by_name: dict[str, Any] = {}
    for r in results:
        by_name.setdefault(r.profile_name, r)
    for profile in pool:
        result = by_name.get(profile.name)
        if result is None:
            continue  # not invoked this cycle (e.g. demoted before running)
        # Parseability is established, never proxied from transport success. Use
        # the explicit parse result from the parse step when we have it; otherwise
        # parse the output here (a pure function) — an escalation-path reviewer
        # that returned unparseable output must record completed=False even though
        # its transport succeeded (#1388).
        if parsed_by_name is not None and profile.name in parsed_by_name:
            parseable = parsed_by_name[profile.name]
        else:
            parseable = _result_is_parseable(result)
        if profile.name in budget_overruns:
            _append_reviewer_attempt(
                state,
                config,
                profile,
                result,
                parseable=parseable,
                cycle_num=cycle_num,
                outcome_override="budget_overrun",
                reason_override="ran over its derived share; verdict retained",
            )
        else:
            _append_reviewer_attempt(
                state, config, profile, result, parseable=parseable, cycle_num=cycle_num
            )


def _resolve_prompt_for_reviewer(review_prompts: str | list[str], index: int) -> str:
    """Return the per-reviewer prompt. Shared string fans out to every reviewer."""
    if isinstance(review_prompts, list):
        return review_prompts[index]
    return review_prompts


def _retry_transient_review_failures(
    state: CoordinatorState,
    config: ForgeConfig,
    pool: list,
    pool_results: list,
    review_prompts: str | list[str],
    workspace_path: Path,
    *,
    cycle_num: int,
    pool_attempt: int,
    meta: ReviewCycleMetadata,
    stop_event: "threading.Event | None" = None,
    progress_channel: "ReviewerProgressChannel | None" = None,
) -> list:
    """Retry per-reviewer transient transport failures with exponential backoff.

    Returns updated pool_results (same order as ``pool``). Each retry's
    AgentResult is appended to state.review_agent_results and a trace + log
    artifact is written. meta.transient_retries and meta.transient_outcomes
    are populated for every reviewer (including those that did not need a
    retry — they record the ``succeeded``/``hard_failed`` outcome).
    """
    max_retries = config.retry.max_review_transport_retries
    backoff_base = config.retry.review_transport_retry_backoff_seconds
    updated = list(pool_results)

    for index, (profile, result) in enumerate(zip(pool, updated)):
        if result.success:
            meta.transient_outcomes[profile.name] = "succeeded"
            continue
        if not _is_transient_review_failure(result, config):
            meta.transient_outcomes[profile.name] = "hard_failed"
            continue
        if max_retries <= 0:
            meta.transient_outcomes[profile.name] = "hard_failed"
            continue

        retry_count = 0
        current = result
        per_reviewer_prompt = _resolve_prompt_for_reviewer(review_prompts, index)
        while retry_count < max_retries and _is_transient_review_failure(current, config):
            retry_count += 1
            backoff = backoff_base * (2 ** (retry_count - 1))
            _log(
                f"  ↻ {profile.name} transient transport failure "
                f"(retry {retry_count}/{max_retries}) in {backoff:.0f}s"
            )
            # Surface the retry live so the watch view shows ↻ rN/M immediately.
            if progress_channel is not None:
                progress_channel.set_retry(profile.name, retry_count, max_retries)
            if backoff > 0:
                time.sleep(backoff)
            retried = run_agent(
                prompt=per_reviewer_prompt,
                profile=profile,
                working_dir=workspace_path,
                quiet=True,
                secrets=config.secrets,
                session_id=current.session_id if profile.mode == "cli" else None,
                stop_event=stop_event,
                progress_cb=progress_channel.cb if progress_channel is not None else None,
            )
            if retried.session_id and produced_model_output(retried):
                state.reviewer_session_ids[profile.name] = retried.session_id
            state.review_agent_results.append(retried)
            log_agent_result(retried, f"REVIEW/{retried.profile_name}")
            _trace_name = (
                f"{cycle_num}-{pool_attempt}-review-{profile.name}"
                f"-transport-retry{retry_count}.txt"
            )
            write_trace(
                workspace_path / ".forge/traces" / _trace_name,
                retried.output,
            )
            _write_log_artifact(
                state.log_dir,
                f"review-cycle-{cycle_num}/{profile.name}-transport-retry{retry_count}.yaml",
                retried.output or "",
                owner_run_id=state.run_id,
            )
            # ``current`` is being superseded by ``retried`` — it was a distinct
            # (failed) reviewer invocation and must be recorded before it is
            # discarded (#1388). The FINAL surviving result is recorded by
            # _record_reviewer_attempts; here we capture only the ones a retry
            # replaced (the original transient failure and any intermediate
            # retries), all of which are failures.
            _append_reviewer_attempt(
                state, config, profile, current, parseable=False, cycle_num=cycle_num
            )
            current = retried

        meta.transient_retries[profile.name] = retry_count
        if current.success:
            meta.transient_outcomes[profile.name] = "transient_retried_then_succeeded"
            _log(f"  ✓ {profile.name} transient retry {retry_count} succeeded")
            # The retry resolved the transient failure — mark the reviewer done
            # (also clears the ↻rN/M glyph) so the pool-done count is accurate.
            if progress_channel is not None:
                progress_channel.cb({"label": profile.name, "done": True})
        else:
            meta.transient_outcomes[profile.name] = "transient_retried_then_failed"
            _log(f"  ✗ {profile.name} transient retries exhausted")
        updated[index] = current

    return updated


# ── Lazy runner slots ─────────────────────────────────────────────────
# None until first call; tests may replace with mocks before calling
# run_task. _ensure_runners() skips any slot that is already non-None.
run_agent = None
run_agent_pool = None
log_agent_result = None


def _ensure_runners() -> None:
    global run_agent, run_agent_pool, log_agent_result
    if run_agent is not None and run_agent_pool is not None and log_agent_result is not None:
        return
    import theforge.runners as _r  # noqa: PLC0415

    if run_agent is None:
        run_agent = _r.run_agent
    if run_agent_pool is None:
        run_agent_pool = _r.run_agent_pool
    if log_agent_result is None:
        log_agent_result = _r.log_agent_result


def _record_provider_health_failures(config: ForgeConfig, results: list[Any]) -> None:
    """Persist a provider-health record for each pool result that failed with
    a provider-shape signature (capacity / rate-limit / 5xx / quota).

    Runtime failures (parse errors, schema rejections, code crashes) are not
    recorded — only signals that mean "the provider itself is currently
    refusing this model" so the adaptive router can deprioritize the model
    for the duration of the health window.
    """
    from theforge.provider_health import (  # noqa: PLC0415
        ProviderHealthRecord,
        append_provider_health_record,
        classify_provider_shape_failure,
        utcnow_iso,
    )

    path = config.project_root / ".forge" / "provider_health.yaml"
    for r in results:
        signature = classify_provider_shape_failure(r)
        if signature is None:
            continue
        append_provider_health_record(
            path,
            ProviderHealthRecord(
                model=r.profile_name,
                signature=signature,
                timestamp=utcnow_iso(),
            ),
        )
        _log_verbose(
            f"  provider-health: recorded {signature} failure for {r.profile_name} "
            f"(exit={r.exit_code})"
        )


def _emit_review_git_context(
    logger: Any | None,
    *,
    base_branch: str,
    commit_log: str,
    diff_stat: str,
    handoff_commit_warning: str | None,
) -> None:
    """Emit verified git metadata and handoff mismatch warnings to the audit trail."""
    if logger is None:
        return
    logger._safe_emit(
        "review_git_context",
        base_branch=base_branch,
        commit_log=commit_log,
        diff_stat=diff_stat,
        handoff_commit_warning=handoff_commit_warning,
    )


def _emit_fix_claim_check(
    logger: Any | None,
    *,
    flagged: list[dict],
    accepted: list[dict],
) -> None:
    """Emit handoff fix-claim check results to the audit trail."""
    if logger is None:
        return
    logger._safe_emit(
        "handoff_fix_claim_check",
        flagged=flagged,
        accepted=accepted,
    )


def _record_review_debriefs(
    state: CoordinatorState,
    named: list[tuple[str, ReviewResult]],
    *,
    cycle_num: int,
) -> None:
    """Record each reviewer's prior-run knowledge receipt for audit (#2866).

    Written after the pool's verdicts are parsed and read by nothing downstream:
    a reviewer's debrief cannot influence its own verdict, the synthesis, or any
    coordinator decision. Reviewers whose output failed to parse still get a
    submission, so "the reviewer was asked and answered nothing" stays
    distinguishable from "the reviewer was never asked".
    """
    for name, parsed in named:
        raw = parsed.raw_yaml if isinstance(parsed.raw_yaml, dict) else {}
        state.knowledge_debriefs.append(
            knowledge_receipts.debrief_submission(
                phase="review",
                agent_role=name,
                phase_iteration=cycle_num,
                source="review_yaml",
                payload=raw.get("knowledge_debrief"),
            )
        )


def _parse_pool_outputs(successful: list) -> list[ReviewResult]:
    """Parse each surviving reviewer's output into a ReviewResult, in pool order.

    Shared by the normal synthesis path and the quorum-collapse path so a
    survivor's verdict is read the same way whether or not quorum was met.
    """
    parsed: list[ReviewResult] = []
    for r in successful:
        if r.structured_data:
            parsed.append(parse_review_json(r.structured_data, r.profile_name))
        else:
            parsed.append(parse_review_output(r.output, r.profile_name))
    return parsed


def _run_review_pool(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskStory,
    story_content: str,
    workspace_path: Path,
    branch_name: str,
    meta: ReviewCycleMetadata,
    *,
    notify: bool,
    review_prompts: str | list[str] | None = None,
    enforce_budgets: bool = True,
    pool_attempt: int = 0,
    max_review_parse_retries: int = 0,
    logger: Any | None = None,
    stop_event: "threading.Event | None" = None,
    state_update_fn: "Callable[[dict], None] | None" = None,
) -> tuple[list, list, ReviewResult | None, list[ReviewResult], list[tuple[str, ReviewResult]]]:
    """Run the review pool and merge results.

    Returns (successful, failed, merged_result, individual_parsed, named_parsed).

    Updates *meta* in-place (successful, failed, failed_detail, parse_retries).
    merged_result is None when all reviewers failed or budget exceeded;
    in that case state.phase and state.error are already set — caller
    just needs to call _escalate_notify and return a CoordinatorResult.
    On a quorum collapse merged_result is None but individual_parsed/named_parsed
    still carry any surviving reviewer's verdict (#2300): quorum decides whether
    the run may proceed automatically, not whether the verdict exists.

    individual_parsed contains per-reviewer ReviewResult objects that passed
    schema validation (after per-reviewer retries).  Callers use this for
    best-individual fallback when the merged result has parse errors.

    named_parsed contains (profile_name, ReviewResult) pairs aligned 1:1 with
    successful agents (before parse-error filtering).  Used for PR review
    attribution in build_post_run_payload().

    When multiple reviewers succeed, results are merged deterministically:
    strictest verdict wins, findings are unioned. No LLM synthesis call.

    Per-reviewer parse retries: for each reviewer whose initial output has parse
    errors, up to max_review_parse_retries corrective prompts are sent via
    run_agent (all modes — API and CLI).  meta.parse_retries accumulates the
    sum of all per-reviewer retries attempted.

    Args:
        review_prompts: Pre-built prompts. If None, builds them (with role-aware
            prompts when review_role is configured). Pass explicitly to control
            prompt construction (e.g. run_review_only always uses generic prompts).
        enforce_budgets: When True (default), enforces per-profile budgets.
            When False (run_review_only), skips budget checks.
        max_review_parse_retries: Per-reviewer parse retry budget.
    """
    _ensure_runners()
    pool = list(config.review_pool)
    demotion_threshold = config.retry.demotion_threshold
    if demotion_threshold > 0:
        active_pool: list = []
        for profile in pool:
            if profile.name in state.reviewer_demoted:
                _log(f"⚠ {profile.name} demoted due to parse failures in a previous cycle")
                continue
            active_pool.append(profile)
        pool = active_pool
        if not pool:
            state.phase = Phase.ESCALATE
            state.error = "All reviewers demoted due to parse failures."
            return [], [], None, [], []

    # The panel recorded against this cycle is the one that will actually be
    # attempted, not the one that was configured. A demoted reviewer never
    # runs and never bills, so a cycle costed at two reviewers must not be
    # recorded as three — downstream pricing joins observed cost to this list
    # and would otherwise attribute a smaller panel's spend to a larger one.
    meta.pool_models = [profile.name for profile in pool]

    pool_size = len(pool)

    # ── Story-allocation funding check (#2169) ────────────────────────────────
    # Decided BEFORE the pool runs, against the panel the run planned to seat.
    # If the allocation cannot fund every planned reviewer, the run says so —
    # it does not quietly seat a smaller panel and then discover the quorum is
    # out of reach. The check is skipped when spend is unmeasured (a lower
    # bound is not a number to refuse work on).
    if enforce_budgets and state.story_allocation:
        configured_ceiling_usd = sum(float(p.budget_usd) for p in pool)
        review_cycle_planning = state.adaptive_review_cycle_planning
        if not isinstance(review_cycle_planning, dict) or not review_cycle_planning:
            review_cycle_planning = _story_budget.derive_review_cycle_planning_price(
                config.project_root,
                configured_ceiling_usd=configured_ceiling_usd,
                composition=[p.name for p in pool],
                complexity_score=state.preflight_complexity_score,
            ).as_dict()
            state.adaptive_review_cycle_planning = review_cycle_planning
        # A cycle the seating reconciliation reserved is funded from that
        # reservation, not from whatever an overrunning dev phase happened to
        # leave behind (#2258). Cycles beyond the reserved ones still draw the
        # general pool, so a story whose dev phase stayed within its estimate
        # keeps every cycle it has always had. Runs with no reservation —
        # configured-fallback allocations, no band history — fall through to the
        # pre-existing whole-allocation check unchanged.
        _shortfall = _story_budget.reserved_review_shortfall(
            state.review_funding_reservation,
            state.story_allocation,
            observed_usd=state.total_cost_measured,
            review_observed_usd=state.total_review_cost_measured,
            participants=[p.name for p in pool],
            planned_usd=float(review_cycle_planning["planned_cost_usd"]),
        )
        if _shortfall is not None:
            _shortfall["review_cycle_planning"] = dict(review_cycle_planning)
            state.allocation_exhausted = _shortfall
            state.phase = Phase.ESCALATE
            state.error = _story_budget.format_shortfall(_shortfall, story=task.slug)
            state.error_type = "allocation_exhausted"
            _log(f"  ⚠ {state.error}")
            return [], [], None, [], []

    # ── Reviewer tree-currency trust check (#1851) ────────────────────────────
    # Promote the coordinator-computed handoff-vs-git reconciliation (#1826) into
    # a structured pass/fail trust-check entry BEFORE the reviewers run, so the
    # native per-run record carries a machine-readable trust_status regardless of
    # what the reviewers assert in prose (ADR-0006 clause 4). Runs from every
    # review entry point (normal pool + review-only) flow through here. A run
    # with no parseable handoff yields no entry — the run stays "unchecked".
    _tc = evaluate_reviewer_tree_currency(
        workspace_path,
        config.workspace.base_branch,
        forge_handoff_path=_latest_forge_handoff_path(state),
    )
    if _tc is not None:
        state.trust_checks[_tc["check"]] = _tc
        if logger is not None:
            logger._safe_emit("trust_check", **_tc)

    if review_prompts is None:
        commit_log = _get_commit_log(workspace_path, config.workspace.base_branch)
        diff_stat = _get_diff_stat(workspace_path, config.workspace.base_branch)
        diff_content = _get_diff_content(workspace_path, config.workspace.base_branch)
        commit_diffs = _get_commit_diffs(workspace_path, config.workspace.base_branch)
        _forge_path = _latest_forge_handoff_path(state)
        handoff_content = _get_handoff_content(forge_handoff_path=_forge_path)
        dev_notes = _get_dev_notes(workspace_path, forge_handoff_path=_forge_path)
        handoff_commit_warning = _get_handoff_commit_warning(
            workspace_path, config.workspace.base_branch, forge_handoff_path=_forge_path
        )

        _emit_review_git_context(
            logger,
            base_branch=config.workspace.base_branch,
            commit_log=commit_log,
            diff_stat=diff_stat,
            handoff_commit_warning=handoff_commit_warning,
        )

        # Heuristic fix-claim check: flag confident fix assertions in the
        # handoff summary that reference a prior P1 but cite no test or invariant.
        # Use only the most recent REQUEST_CHANGES cycle — its p1_findings are the
        # currently unresolved findings. Earlier cycles may contain findings that
        # were already fixed, and including them would cause false-positive flags.
        _last_rc = next(
            (ch for ch in reversed(state.cycle_history or []) if ch.verdict == "REQUEST_CHANGES"),
            None,
        )
        _prior_p1_descs: list[str] = list(_last_rc.p1_findings) if _last_rc else []
        _parsed_handoff = _parse_dev_handoff(forge_handoff_path=_forge_path)
        _claim_summary = (
            _parsed_handoff.summary
            if _parsed_handoff is not None and not _parsed_handoff.parse_errors
            else ""
        )
        _claim_check = check_handoff_fix_claims(_claim_summary, _prior_p1_descs)
        _emit_fix_claim_check(
            logger,
            flagged=[
                {"claim": r.claim_text, "finding": r.related_finding} for r in _claim_check.flagged
            ],
            accepted=[
                {"claim": r.claim_text, "finding": r.related_finding}
                for r in _claim_check.accepted
            ],
        )
        fix_claim_flags: list[str] | None = [
            r.flag_message for r in _claim_check.flagged if r.flag_message
        ] or None

        review_context = ContextAssembler.from_config(config).assemble(
            phase="review",
            story_text=story_content,
            file_list=plan_file_list(state.plan_structured) or None,
            agent_role="review",
            phase_iteration=state.review_cycle,
        )
        state.context_manifests.append({"phase": "review", "manifest": review_context})

        review_prompts = (
            [
                build_review_prompt(
                    task,
                    story_content=story_content,
                    commit_log=commit_log,
                    diff_stat=diff_stat,
                    diff_content=diff_content,
                    commit_diffs=commit_diffs,
                    workspace_path=str(workspace_path),
                    branch=branch_name,
                    handoff_content=handoff_content,
                    handoff_commit_warning=handoff_commit_warning,
                    mode=p.mode,
                    review_role=p.review_role,
                    dev_notes=dev_notes,
                    cycle_history=state.cycle_history if state.cycle_history else None,
                    conventions=config.conventions_soft,
                    **hard_convention_review_kwargs(config),
                    assembled_context=review_context,
                    sandboxed=state.sandboxed,
                    containment=state.dev_containment,
                    authoritative_gate_decision=state.last_gate_decision,
                    authoritative_gate_commit=state.last_gate_commit,
                    **_gate_profile_prompt_kwargs(state),
                    fix_claim_flags=fix_claim_flags,
                    p2_policy=config.dev.p2_policy,
                )
                for p in pool
            ]
            if any(p.review_role for p in pool)
            else build_review_prompt(
                task,
                story_content=story_content,
                commit_log=commit_log,
                diff_stat=diff_stat,
                diff_content=diff_content,
                commit_diffs=commit_diffs,
                workspace_path=str(workspace_path),
                branch=branch_name,
                handoff_content=handoff_content,
                handoff_commit_warning=handoff_commit_warning,
                mode=pool[0].mode,
                dev_notes=dev_notes,
                cycle_history=state.cycle_history if state.cycle_history else None,
                conventions=config.conventions_soft,
                **hard_convention_review_kwargs(config),
                assembled_context=review_context,
                sandboxed=state.sandboxed,
                containment=state.dev_containment,
                authoritative_gate_decision=state.last_gate_decision,
                authoritative_gate_commit=state.last_gate_commit,
                **_gate_profile_prompt_kwargs(state),
                fix_claim_flags=fix_claim_flags,
                p2_policy=config.dev.p2_policy,
            )
        )
    _log_verbose(f"Running {pool_size} reviewer(s): {[p.name for p in pool]}")
    _pool_start = time.monotonic()
    pool_session_ids = [state.reviewer_session_ids.get(p.name) for p in pool]
    for _p, _sid in zip(pool, pool_session_ids):
        _tag = f"resuming {_sid[:8]}" if _sid else "new session"
        _log_verbose(f"  reviewer {_p.name}: {_tag}")
    # Passive per-reviewer progress channel: aggregates iter/done/retry events
    # into live .state so the watch view can render per-reviewer progress.
    progress_channel = ReviewerProgressChannel(
        reviewer_names=[p.name for p in pool],
        phase="REVIEW",
        iteration=state.review_cycle + 1,
        cost_usd=state.total_cost_measured,
        complexity=state.preflight_complexity,
        complexity_score=state.preflight_complexity_score,
        state_update_fn=state_update_fn,
    )
    pool_results = run_agent_pool(
        prompt=review_prompts,
        profiles=pool,
        working_dir=workspace_path,
        session_ids=pool_session_ids,
        secrets=config.secrets,
        stop_event=stop_event,
        progress_cb=progress_channel.cb,
    )
    _pool_elapsed = time.monotonic() - _pool_start
    for profile, result in zip(pool, pool_results):
        if result.session_id and produced_model_output(result):
            state.reviewer_session_ids[profile.name] = result.session_id
    save_sessions(
        workspace_path,
        state.dev_session_id,
        state.reviewer_session_ids,
        state.plan_review_session_ids,
    )
    _per_agent_dur = _pool_elapsed / max(len(pool_results), 1)
    _cycle_num = state.review_cycle + 1
    _record_provider_health_failures(config, pool_results)
    for r in pool_results:
        state.review_agent_results.append(r)
        state.review_durations.append(_per_agent_dur)
        log_agent_result(r, f"REVIEW/{r.profile_name}")
        write_trace(
            workspace_path
            / ".forge/traces"
            / f"{_cycle_num}-{pool_attempt}-review-{r.profile_name}.txt",
            r.output,
        )
        # Write raw reviewer output to durable story log dir
        _write_log_artifact(
            state.log_dir,
            f"review-cycle-{_cycle_num}/{r.profile_name}.yaml",
            r.output or "",
            owner_run_id=state.run_id,
        )

    # ── Transient transport retry ─────────────────────────────────────
    # Re-invoke each reviewer whose initial result has a transient transport
    # signature (rate limit / 5xx / connection reset / empty-output crash) up
    # to the configured budget with exponential backoff before counting the
    # reviewer as failed.
    pool_results = _retry_transient_review_failures(
        state,
        config,
        pool,
        pool_results,
        review_prompts,
        workspace_path,
        cycle_num=_cycle_num,
        pool_attempt=pool_attempt,
        meta=meta,
        stop_event=stop_event,
        progress_channel=progress_channel,
    )
    # Snapshot every reviewer's transport-level result BEFORE budget filtering so
    # attempt telemetry (#1388) covers every invocation this cycle, including
    # reviewers later dropped for going over budget.
    _all_pool_results = list(pool_results)

    # Per-profile budget accounting BEFORE synthesis. A reviewer that ran over
    # its derived share is RECORDED, not removed: it already spent the money and
    # already produced a verdict, so dropping it discards paid-for review signal
    # and can push the quorum out of reach while sprint headroom remains
    # (#2169). Under-funding is reported as a condition against the story
    # allocation; it never silently shrinks the panel the run planned.
    _budget_overruns: set[str] = set()
    if enforce_budgets:
        for profile in pool:
            profile_cost = sum(
                r.cost_usd if r.cost_usd is not None else 0.0
                for r in state.review_agent_results
                if r.profile_name == profile.name
            )
            if profile_cost > profile.budget_usd:
                _log(
                    f"  ⚠ {profile.name} over its derived share: "
                    f"${profile_cost:.4f} > ${profile.budget_usd:.4f} — "
                    f"verdict retained, overrun recorded"
                )
                _budget_overruns.add(profile.name)
                state.reviewer_budget_overruns.append(
                    {
                        "reviewer": profile.name,
                        "cycle": _cycle_num,
                        "measured_usd": round(profile_cost, 6),
                        "share_usd": round(float(profile.budget_usd), 6),
                        "overrun_usd": round(profile_cost - float(profile.budget_usd), 6),
                    }
                )

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

    # Quorum is measured against the reviewers that could actually answer this
    # cycle. Since #2169 a spend overrun no longer withdraws a reviewer — every
    # seat that was planned still counts and still votes — so the eligible pool
    # is the nominal pool. The collapse rule remains: a threshold larger than
    # the pool is a demand no run could satisfy, not a stricter standard.
    eligible_pool_size = pool_size
    quorum_threshold = min(config.retry.review_quorum_threshold, eligible_pool_size)
    if quorum_threshold < 1:
        quorum_threshold = 1
    meta.quorum_threshold = quorum_threshold
    meta.quorum_met = len(successful) >= quorum_threshold

    failed_desc = ", ".join(f"{r.profile_name} (exit={r.exit_code})" for r in failed_results)
    if not meta.quorum_met:
        # ── Degraded-quorum recovery ──────────────────────────────────
        # A reviewer that finishes without a submit call delivered no verdict
        # — an infrastructure failure, not a review outcome. When the quorum
        # shortfall is caused *entirely* by such non-verdict completions and
        # at least one trustworthy verdict survives, degrade to the survivors
        # with an explicit audit warning rather than killing the story.
        # Escalation is reserved for a total quorum collapse (zero survivors)
        # or a genuine hard failure (crash / exhausted transient retry) among
        # the failures.
        can_degrade = (
            config.retry.review_degrade_on_infra_failure
            and bool(successful)
            and bool(failed_results)
            and all(_is_non_verdict_completion(r) for r in failed_results)
        )
        if can_degrade:
            warning = (
                f"Degraded quorum: {len(successful)}/{eligible_pool_size} reviewer(s) "
                f"delivered a verdict (threshold {quorum_threshold}); proceeding "
                f"on surviving verdict(s) after non-verdict reviewer failure(s): "
                f"{failed_desc}"
            )
            if _budget_overruns:
                # Reported, not subtracted: these reviewers ran over their
                # derived share and their verdicts still counted (#2169).
                warning += (
                    "; over derived share (verdict counted): "
                    f"{', '.join(sorted(_budget_overruns))}"
                )
            meta.degraded_quorum = True
            meta.degraded_quorum_warning = warning
            _log(f"  ⚠ {warning}")
            if logger is not None:
                logger._safe_emit(
                    "review_degraded_quorum",
                    cycle=_cycle_num,
                    successful=meta.successful,
                    failed=meta.failed,
                    failed_detail=meta.failed_detail,
                    quorum_threshold=quorum_threshold,
                    pool_size=pool_size,
                    eligible_pool_size=eligible_pool_size,
                    budget_overruns=sorted(_budget_overruns),
                    warning=warning,
                )
        else:
            # Quorum collapse — escalate. Record every reviewer attempt (#1388)
            # before returning: the failures are exactly the survivorship-biased
            # signal this story is closing over.
            _record_reviewer_attempts(
                state,
                config,
                pool,
                _all_pool_results,
                cycle_num=_cycle_num,
                budget_overruns=_budget_overruns,
            )
            state.phase = Phase.ESCALATE
            # Since #2169 a spend overrun cannot cause a shortfall — the seat
            # still votes — so a quorum collapse here is a failure story only.
            # The overruns are still named, because a collapse alongside a
            # blown share is a different diagnosis from a clean one.
            _shortfall = f"failed: {failed_desc}"
            if _budget_overruns:
                _shortfall += (
                    "; over derived share (verdict counted): "
                    f"{', '.join(sorted(_budget_overruns))}"
                )
            state.error = (
                f"Quorum unmet: {len(successful)}/{eligible_pool_size} succeeded "
                f"< threshold {quorum_threshold}; {_shortfall}"
            )
            # The collapse still escalates and still merges nothing — quorum
            # governs automatic progression. But a verdict a surviving reviewer
            # actually produced must not cease to exist for every later decision
            # (#2300): return it in the individual/named slots so the escalate
            # gate can show it, the audit can record it, and an operator who
            # knows quorum was unmet can still act on it explicitly.
            _survivor_parsed = _parse_pool_outputs(successful)
            _survivor_named = [
                (r.profile_name, parsed) for r, parsed in zip(successful, _survivor_parsed)
            ]
            _survivor_individual = [p for p in _survivor_parsed if not p.parse_errors]
            _record_review_debriefs(state, _survivor_named, cycle_num=_cycle_num)
            return successful, failed_results, None, _survivor_individual, _survivor_named

    if failed_results and meta.quorum_met:
        _log(
            f"Panel quorum met ({len(successful)}/{eligible_pool_size} reviewers succeeded "
            f"≥ quorum threshold {quorum_threshold}) — proceeding to synthesis"
        )

    _synthesis_path = (
        workspace_path / ".forge/traces" / f"{_cycle_num}-{pool_attempt}-synthesis.txt"
    )

    # ── Parse initial outputs ─────────────────────────────────────────
    parsed_results: list[ReviewResult] = _parse_pool_outputs(successful)
    names = [r.profile_name for r in successful]

    # Attempt-completion invariant (#1388): every reviewer invocation is recorded
    # exactly once, classified on ITS OWN outcome — never on a later retry's. The
    # initial (pool / transient-final) invocation is recorded by the main
    # _record_reviewer_attempts call below using this snapshot of its FIRST parse
    # result; each parse-retry invocation is a distinct invocation recorded as it
    # happens inside the loop. Snapshot here, before the loop mutates
    # ``parsed_results``, so a retry that later parses cannot rewrite the initial
    # invocation's failed outcome into a spurious completion.
    _initial_parseable = {
        name: (not parsed.parse_errors) for name, parsed in zip(names, parsed_results)
    }

    # ── Per-reviewer parse retry (all modes) ─────────────────────────
    # For each reviewer whose initial output has parse errors, send a corrective
    # prompt via run_agent up to max_review_parse_retries times.
    # meta.parse_retries accumulates the sum of per-reviewer retries attempted.
    _profile_by_name = {p.name: p for p in pool}
    # The AgentResult backing each reviewer's FINAL parsed output (#2226). A
    # successful parse retry replaces ``parsed_results[i]``, and that retry is a
    # fresh invocation that can be served by a different concrete version than
    # the initial one it supersedes. Anything attributing this cycle's evidence
    # to a model has to follow the output that survived, so this list is updated
    # in lockstep with ``parsed_results`` below.
    _results_for_parsed = list(successful)
    for i, (name, parsed) in enumerate(zip(names, parsed_results)):
        if not parsed.parse_errors:
            continue
        _prof = _profile_by_name.get(name)
        if _prof is None:
            continue
        # Capture original AgentResult for this reviewer (session_id + raw output)
        _original_result = successful[i]
        for _retry_num in range(1, max_review_parse_retries + 1):
            _error_desc = "; ".join(str(e) for e in parsed.parse_errors)
            # Stage breakdown lets operators distinguish parser-layer failures
            # from schema/contract-layer failures without parsing the message.
            _stages = sorted({e.stage for e in parsed.parse_errors})
            _stage_tag = ",".join(_stages) if _stages else "UNKNOWN"
            _log(
                f"  ↻ {name} rejected at {_stage_tag} "
                f"(retry {_retry_num}/{max_review_parse_retries}): "
                f"{_error_desc[:120]}"
            )
            # Build corrective prompt anchored on the reviewer's prior output.
            # Both modes include the verbatim prior emission so "reformat" cannot
            # be read as "drop the fields that are hard to encode". CLI mode also
            # carries session continuity via session_id; the explicit anchor is
            # more robust than relying on session memory alone.
            _original_output = _original_result.output or ""
            _retry_prompt = _build_review_retry_prompt(_error_desc, _original_output)
            _retry_result = run_agent(
                prompt=_retry_prompt,
                profile=_prof,
                working_dir=workspace_path,
                quiet=True,
                secrets=config.secrets,
                session_id=_original_result.session_id if _prof.mode == "cli" else None,
                stop_event=stop_event,
            )
            meta.parse_retries += 1
            if not _retry_result.success:
                _log_verbose(
                    f"  {name} retry {_retry_num} agent failed (exit={_retry_result.exit_code})"
                )
                # A parse-retry that fails transport is still a distinct reviewer
                # invocation (#1388) — record it (classified transport/crash) so it
                # does not vanish from the completion evidence.
                _append_reviewer_attempt(
                    state, config, _prof, _retry_result, parseable=False, cycle_num=_cycle_num
                )
                break
            _retried = _try_parse_review(_retry_result.output, _retry_result.structured_data, name)
            write_trace(
                workspace_path
                / ".forge/traces"
                / f"{_cycle_num}-{pool_attempt}-review-{name}-retry{_retry_num}.txt",
                _retry_result.output,
            )
            if _retried is not None:
                _log(f"  ✓ {name} retry {_retry_num} succeeded")
                # The retry produced a parseable verdict — a completed invocation.
                _append_reviewer_attempt(
                    state, config, _prof, _retry_result, parseable=True, cycle_num=_cycle_num
                )
                parsed_results[i] = _retried
                _results_for_parsed[i] = _retry_result
                state.review_agent_results.append(_retry_result)
                break
            else:
                _retry_errors = parse_review_output(_retry_result.output).parse_errors
                _log_verbose(
                    f"  {name} retry {_retry_num} still rejected: "
                    f"{[str(e) for e in _retry_errors]}"
                )
                # Transport succeeded but the output is still unparseable — a failed
                # invocation. Record it before the next retry supersedes it.
                _append_reviewer_attempt(
                    state, config, _prof, _retry_result, parseable=False, cycle_num=_cycle_num
                )
                parsed = ReviewResult(
                    verdict="REQUEST_CHANGES",
                    summary="",
                    findings=[],
                    story_matches=False,
                    story_mismatches=[],
                    test_adequate=False,
                    test_gaps=[],
                    parse_errors=_retry_errors or list(parsed.parse_errors),
                    raw_yaml={},
                )

    if demotion_threshold > 0:
        for name, parsed in zip(names, parsed_results):
            if not parsed.parse_errors:
                continue
            failure_count = state.reviewer_parse_failure_counts.get(name, 0) + 1
            state.reviewer_parse_failure_counts[name] = failure_count
            if failure_count >= demotion_threshold:
                state.reviewer_demoted.add(name)
                _log(f"⚠ {name} demoted after {failure_count} parse failures this run")

    # Individual parsed results (no parse errors) — used by caller for fallback
    individual_parsed: list[ReviewResult] = [p for p in parsed_results if not p.parse_errors]

    # Named pairs aligned 1:1 with successful agents (before parse filtering).
    # Reviewers with persistent parse errors are included with whatever partial
    # data was extracted; callers that use this for PR review attribution will
    # post a COMMENT with potentially empty findings/summary for those reviewers.
    named_parsed: list[tuple[str, ReviewResult]] = list(zip(names, parsed_results))
    _record_review_debriefs(state, named_parsed, cycle_num=_cycle_num)

    # Per-cycle served-version attribution (#2226). Recorded on the cycle rather
    # than reconstructed from the attempt log: the attempt log holds every
    # invocation, including the ones a retry superseded, and nothing in it says
    # which invocation's output the cycle ended up using.
    for _name, _result in zip(names, _results_for_parsed):
        _served = resolved_identity_for_result(_result)
        if _served:
            meta.resolved_by_reviewer[_name] = _served

    # ── Per-code-reviewer mechanical value telemetry (#2156) ──────────────
    # The code-review counterpart of the plan-review capture in plan_flow.py:
    # deterministic, coordinator-computed, over the structured findings from THIS
    # completed pool (no LLM judgment). Uniqueness compares each reviewer's
    # blocking findings against every peer's via anchor overlap over the
    # observed+evidence text; latency is the per-reviewer pool wall-clock measured
    # above. Recorded for every reviewer regardless of outcome so the audit can
    # answer "is this reviewer earning its wall-clock cost?" — reviewers whose
    # output failed to parse are excluded from the uniqueness *comparison* (their
    # findings list is not what they judged), matching plan_flow.
    _profiles_by_name = {p.name: p for p in pool}
    _uniq_inputs = [(n, p.findings) for n, p in named_parsed if not p.parse_errors]
    _uniqueness = compute_reviewer_uniqueness(_uniq_inputs, anchor_text=code_review_anchor_text)
    for _i, (_name, _parsed_r) in enumerate(named_parsed):
        _unique_p1, _total_p1 = _uniqueness.get(_name, (0, 0))
        _prof = _profiles_by_name.get(_name)
        state.code_reviewer_value.append(
            {
                "cycle": _cycle_num,
                "reviewer": _name,
                "complexity": state.preflight_complexity or "medium",
                "unique_p1_count": _unique_p1,
                "total_p1_count": _total_p1,
                "latency_s": round(_per_agent_dur, 2),
                "parse_error_count": len(_parsed_r.parse_errors),
                "actual_model": getattr(_prof, "model", None),
                "provider": getattr(_prof, "provider", None),
                "cli": getattr(_prof, "cli", None),
                # The concrete version that served the invocation whose output
                # this sample measures (#2226). Read from ``_results_for_parsed``,
                # not ``successful``: when a parse retry supersedes the initial
                # output, the sample describes the RETRY's findings, so stamping
                # it with the superseded invocation's version would attribute the
                # measurement to a model that did not produce it.
                "resolved_model": (
                    resolved_identity_for_result(_results_for_parsed[_i])
                    if _i < len(_results_for_parsed)
                    else None
                ),
            }
        )

    # Attempt-completion telemetry (#1388): record the INITIAL (pool /
    # transient-final) invocation for every reviewer, classified on its own first
    # parse result (``_initial_parseable``) — NOT the post-retry state, which would
    # collapse a failed initial parse and its successful retry into a single
    # completion. Parse-retry invocations were already recorded, per invocation,
    # inside the loop above. Together this gives one record per invocation.
    _record_reviewer_attempts(
        state,
        config,
        pool,
        _all_pool_results,
        cycle_num=_cycle_num,
        parsed_by_name=_initial_parseable,
        budget_overruns=_budget_overruns,
    )

    # ── Merge ─────────────────────────────────────────────────────────
    if len(successful) == 1:
        merged = parsed_results[0]
    else:
        _log_verbose(
            f"Merging {len(successful)} review outputs (+{len(failed_results)} failed excluded)"
        )
        merged = merge_review_results(parsed_results, names)

    _synthesis_content = yaml.dump(
        dataclasses.asdict(merged), default_flow_style=False, allow_unicode=True
    )
    write_trace(_synthesis_path, _synthesis_content)
    # Write synthesized result to durable story log dir
    _write_log_artifact(
        state.log_dir,
        f"review-cycle-{_cycle_num}/synthesized.yaml",
        _synthesis_content,
        owner_run_id=state.run_id,
    )
    return successful, failed_results, merged, individual_parsed, named_parsed
