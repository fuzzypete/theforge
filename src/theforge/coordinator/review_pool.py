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
from theforge.sessions import save_sessions
from theforge.task import ContextAssembler, TaskStory, build_review_prompt
from theforge.traces import write_trace

from . import util as _cu
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


def _record_reviewer_attempts(
    state: CoordinatorState,
    config: ForgeConfig,
    pool: list,
    results: list,
    *,
    cycle_num: int,
    parsed_by_name: dict[str, bool] | None = None,
    budget_excluded: set[str] | None = None,
) -> None:
    """Append one authoritative attempt record per invoked reviewer (#1388).

    Writes into ``state.reviewer_attempts`` — the native per-run capture that is
    both persisted into the audit record and folded into the derived
    completion-rate profile. Every reviewer that produced a result this cycle gets
    a record regardless of outcome, closing the survivorship-bias gap where a
    reviewer that timed out / crashed / returned unparseable output evaporated
    from the profile. ``parsed_by_name`` maps reviewer name → whether its final
    (post parse-retry) output was a parseable verdict; when absent (an early
    escalation return before parsing), transport success is used as the proxy.
    """
    from theforge.model_profiles import canonical_id_from_identity  # noqa: PLC0415

    budget_excluded = budget_excluded or set()
    by_name: dict[str, Any] = {}
    for r in results:
        by_name.setdefault(r.profile_name, r)
    for profile in pool:
        result = by_name.get(profile.name)
        if result is None:
            continue  # not invoked this cycle (e.g. demoted before running)
        if parsed_by_name is None:
            parseable = bool(result.success)
        else:
            parseable = parsed_by_name.get(profile.name, bool(result.success))
        completed, outcome, reason = _classify_reviewer_attempt(result, parseable, config)
        if profile.name in budget_excluded:
            outcome = "budget_excluded"
            reason = "excluded from cycle: over per-reviewer budget"
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
                "completed_parseable_verdict": bool(completed),
                "outcome": outcome,
                "failure_reason": reason,
                "cycle": int(cycle_num),
            }
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
            if retried.session_id:
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

    pool_size = len(pool)

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
                    fix_claim_flags=fix_claim_flags,
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
                fix_claim_flags=fix_claim_flags,
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
        cost_usd=state.total_cost,
        complexity=state.preflight_complexity,
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
        if result.session_id:
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

    # Per-profile budget enforcement BEFORE synthesis — exclude over-budget
    # reviewers from this cycle's results rather than killing the whole run.
    _budget_excluded: set[str] = set()
    if enforce_budgets:
        for profile in pool:
            profile_cost = sum(
                r.cost_usd if r.cost_usd is not None else 0.0
                for r in state.review_agent_results
                if r.profile_name == profile.name
            )
            if profile_cost > profile.budget_usd:
                _log(
                    f"  ⚠ {profile.name} over budget: "
                    f"${profile_cost:.4f} > ${profile.budget_usd:.4f} — "
                    f"excluding from this cycle"
                )
                _budget_excluded.add(profile.name)

    if _budget_excluded:
        pool_results = [r for r in pool_results if r.profile_name not in _budget_excluded]
        if not pool_results:
            # All reviewers excluded — escalate. Still record the attempts: every
            # reviewer was invoked (#1388), the budget cap is a spend decision.
            _record_reviewer_attempts(
                state,
                config,
                pool,
                _all_pool_results,
                cycle_num=_cycle_num,
                budget_excluded=_budget_excluded,
            )
            state.phase = Phase.ESCALATE
            _excluded = ", ".join(sorted(_budget_excluded))
            state.error = f"All reviewers over budget ({_excluded}) — no reviews to synthesize"
            return [], [], None, [], []

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

    quorum_threshold = min(config.retry.review_quorum_threshold, pool_size)
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
                f"Degraded quorum: {len(successful)}/{pool_size} reviewer(s) "
                f"delivered a verdict (threshold {quorum_threshold}); proceeding "
                f"on surviving verdict(s) after non-verdict reviewer failure(s): "
                f"{failed_desc}"
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
                budget_excluded=_budget_excluded,
            )
            state.phase = Phase.ESCALATE
            state.error = (
                f"Quorum unmet: {len(successful)}/{pool_size} succeeded "
                f"< threshold {quorum_threshold}; failed: {failed_desc}"
            )
            return successful, failed_results, None, [], []

    if failed_results and meta.quorum_met:
        _log(
            f"Panel quorum met ({len(successful)}/{pool_size} reviewers succeeded "
            f"≥ quorum threshold {quorum_threshold}) — proceeding to synthesis"
        )

    _synthesis_path = (
        workspace_path / ".forge/traces" / f"{_cycle_num}-{pool_attempt}-synthesis.txt"
    )

    # ── Parse initial outputs ─────────────────────────────────────────
    parsed_results: list[ReviewResult] = []
    for r in successful:
        if r.structured_data:
            parsed_results.append(parse_review_json(r.structured_data, r.profile_name))
        else:
            parsed_results.append(parse_review_output(r.output, r.profile_name))
    names = [r.profile_name for r in successful]

    # ── Per-reviewer parse retry (all modes) ─────────────────────────
    # For each reviewer whose initial output has parse errors, send a corrective
    # prompt via run_agent up to max_review_parse_retries times.
    # meta.parse_retries accumulates the sum of per-reviewer retries attempted.
    _profile_by_name = {p.name: p for p in pool}
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
                parsed_results[i] = _retried
                state.review_agent_results.append(_retry_result)
                break
            else:
                _retry_errors = parse_review_output(_retry_result.output).parse_errors
                _log_verbose(
                    f"  {name} retry {_retry_num} still rejected: "
                    f"{[str(e) for e in _retry_errors]}"
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

    # Attempt-completion telemetry (#1388): a reviewer "completed" iff its final
    # (post parse-retry) output is a parseable verdict — transport success alone
    # is not enough. Records every reviewer invoked this cycle, including failures.
    _parsed_by_name = {name: (not parsed.parse_errors) for name, parsed in named_parsed}
    _record_reviewer_attempts(
        state,
        config,
        pool,
        _all_pool_results,
        cycle_num=_cycle_num,
        parsed_by_name=_parsed_by_name,
        budget_excluded=_budget_excluded,
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
    )
    return successful, failed_results, merged, individual_parsed, named_parsed
