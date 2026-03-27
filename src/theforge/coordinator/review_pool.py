"""Review pool runner: fan-out to multiple reviewers and merge results.

Orchestrates the multi-reviewer pool for a single review cycle:
- Dispatches prompts to the review pool via run_agent_pool
- Enforces per-profile budgets
- Retries parse errors per-reviewer
- Merges results deterministically (strictest verdict wins, findings unioned)
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from theforge.config import ForgeConfig
from theforge.review import (
    ReviewResult,
    _try_parse_review,
    merge_review_results,
    parse_review_json,
    parse_review_output,
)
from theforge.sessions import save_sessions
from theforge.task import TaskSpec, build_review_prompt
from theforge.traces import write_trace

from . import util as _cu
from .log_tee import _write_log_artifact
from .review_context import _get_commit_log, _get_dev_notes, _get_handoff_content
from .state import CoordinatorState, Phase, ReviewCycleMetadata

if TYPE_CHECKING:
    pass

_log = _cu._log
_log_verbose = _cu._log_verbose

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


def _run_review_pool(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
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
    pool_size = len(config.review_pool)

    if review_prompts is None:
        commit_log = _get_commit_log(workspace_path, config.workspace.base_branch)
        handoff_content = _get_handoff_content(config, workspace_path)
        dev_notes = _get_dev_notes(config, workspace_path)

        review_prompts = (
            [
                build_review_prompt(
                    task,
                    story_content=story_content,
                    commit_log=commit_log,
                    workspace_path=str(workspace_path),
                    branch=branch_name,
                    handoff_content=handoff_content,
                    mode=p.mode,
                    review_role=p.review_role,
                    dev_notes=dev_notes,
                    cycle_history=state.cycle_history if state.cycle_history else None,
                )
                for p in config.review_pool
            ]
            if any(p.review_role for p in config.review_pool)
            else build_review_prompt(
                task,
                story_content=story_content,
                commit_log=commit_log,
                workspace_path=str(workspace_path),
                branch=branch_name,
                handoff_content=handoff_content,
                mode=config.review_pool[0].mode,
                dev_notes=dev_notes,
                cycle_history=state.cycle_history if state.cycle_history else None,
            )
        )
    _log_verbose(f"Running {pool_size} reviewer(s): {[p.name for p in config.review_pool]}")
    _pool_start = time.monotonic()
    pool_session_ids = [state.reviewer_session_ids.get(p.name) for p in config.review_pool]
    for _p, _sid in zip(config.review_pool, pool_session_ids):
        _tag = f"resuming {_sid[:8]}" if _sid else "new session"
        _log_verbose(f"  reviewer {_p.name}: {_tag}")
    pool_results = run_agent_pool(
        prompt=review_prompts,
        profiles=config.review_pool,
        working_dir=workspace_path,
        session_ids=pool_session_ids,
        secrets=config.secrets,
    )
    _pool_elapsed = time.monotonic() - _pool_start
    for profile, result in zip(config.review_pool, pool_results):
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

    # Per-profile budget enforcement BEFORE synthesis — exclude over-budget
    # reviewers from this cycle's results rather than killing the whole run.
    _budget_excluded: set[str] = set()
    if enforce_budgets:
        for profile in config.review_pool:
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
            # All reviewers excluded — escalate
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

    if not successful:
        state.phase = Phase.ESCALATE
        failed_desc = ", ".join(f"{r.profile_name} (exit={r.exit_code})" for r in failed_results)
        state.error = f"All {len(pool_results)} review agent(s) failed: {failed_desc}"
        return successful, failed_results, None, [], []

    _synthesis_path = (
        workspace_path / ".forge/traces" / f"{_cycle_num}-{pool_attempt}-synthesis.txt"
    )

    # ── Parse initial outputs ─────────────────────────────────────────
    parsed_results: list[ReviewResult] = []
    for r in successful:
        if r.structured_data:
            parsed_results.append(parse_review_json(r.structured_data))
        else:
            parsed_results.append(parse_review_output(r.output))
    names = [r.profile_name for r in successful]

    # ── Per-reviewer parse retry (all modes) ─────────────────────────
    # For each reviewer whose initial output has parse errors, send a corrective
    # prompt via run_agent up to max_review_parse_retries times.
    # meta.parse_retries accumulates the sum of per-reviewer retries attempted.
    _profile_by_name = {p.name: p for p in config.review_pool}
    _corrective_yaml_structure = (
        "verdict: APPROVE | REQUEST_CHANGES\n"
        'summary: "one-line summary"\n'
        "findings:\n"
        "  - severity: P1 | P2\n"
        '    file: "path"\n'
        "    line: <number or null>\n"
        '    description: "what is wrong"\n'
        '    suggestion: "how to fix"\n'
        "story_compliance:\n"
        "  matches_spec: true | false\n"
        "  mismatches: []\n"
        "test_coverage:\n"
        "  adequate: true | false\n"
        "  gaps: []\n"
    )
    for i, (name, parsed) in enumerate(zip(names, parsed_results)):
        if not parsed.parse_errors:
            continue
        _prof = _profile_by_name.get(name)
        if _prof is None:
            continue
        # Capture original AgentResult for this reviewer (session_id + raw output)
        _original_result = successful[i]
        for _retry_num in range(1, max_review_parse_retries + 1):
            _error_desc = "; ".join(parsed.parse_errors)
            _log(
                f"  ↻ {name} parse failed (retry {_retry_num}/{max_review_parse_retries}): "
                f"{_error_desc[:120]}"
            )
            # Build corrective prompt — mode-specific to avoid re-review
            if _prof.mode == "api":
                # Include original output so the agent can reformat without re-reviewing
                _original_output = _original_result.output or ""
                _retry_prompt = (
                    "Your previous output (reproduced below) had schema/parse errors:\n"
                    + _error_desc
                    + "\n\nReformat your output as valid YAML. Do NOT re-review the code.\n\n"
                    "Required YAML structure:\n"
                    + _corrective_yaml_structure
                    + "\n\nYour previous output:\n"
                    + _original_output
                )
            else:
                # CLI: prompt is simpler — session continuity via session_id handles context
                _retry_prompt = (
                    "Your previous review output had schema/parse errors:\n"
                    + _error_desc
                    + "\n\nReformat your output as valid YAML. Do NOT re-review the code.\n\n"
                    "Required YAML structure:\n" + _corrective_yaml_structure
                )
            _retry_result = run_agent(
                prompt=_retry_prompt,
                profile=_prof,
                working_dir=workspace_path,
                quiet=True,
                secrets=config.secrets,
                session_id=_original_result.session_id if _prof.mode == "cli" else None,
            )
            meta.parse_retries += 1
            if not _retry_result.success:
                _log_verbose(
                    f"  {name} retry {_retry_num} agent failed (exit={_retry_result.exit_code})"
                )
                break
            _retried = _try_parse_review(_retry_result.output, _retry_result.structured_data)
            if _retried is not None:
                _log(f"  ✓ {name} retry {_retry_num} succeeded")
                parsed_results[i] = _retried
                state.review_agent_results.append(_retry_result)
                write_trace(
                    workspace_path
                    / ".forge/traces"
                    / f"{_cycle_num}-{pool_attempt}-review-{name}-retry{_retry_num}.txt",
                    _retry_result.output,
                )
                break
            else:
                _log_verbose(
                    f"  {name} retry {_retry_num} still has parse errors: "
                    f"{parse_review_output(_retry_result.output).parse_errors}"
                )
                parsed = ReviewResult(
                    verdict="REQUEST_CHANGES",
                    summary="",
                    findings=[],
                    story_matches=False,
                    story_mismatches=[],
                    test_adequate=False,
                    test_gaps=[],
                    parse_errors=[_error_desc],
                    raw_yaml={},
                )

    # Individual parsed results (no parse errors) — used by caller for fallback
    individual_parsed: list[ReviewResult] = [p for p in parsed_results if not p.parse_errors]

    # Named pairs aligned 1:1 with successful agents (before parse filtering).
    # Reviewers with persistent parse errors are included with whatever partial
    # data was extracted; callers that use this for PR review attribution will
    # post a COMMENT with potentially empty findings/summary for those reviewers.
    named_parsed: list[tuple[str, ReviewResult]] = list(zip(names, parsed_results))

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
