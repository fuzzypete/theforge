"""REVIEW phase handler.

Owns the _run_review_phase function: review pool invocation, verdict handling,
approve finalization, PR creation, story archival, and cycle-history tracking.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import replace as _dc_replace
from enum import Enum, auto
from pathlib import Path
from types import ModuleType

from theforge import finding_classifier as _fc
from theforge.config import MODEL_REGISTRY, ForgeConfig
from theforge.review import (
    ReviewFinding,
    ReviewResult,
    _best_individual_result,
    review_to_dev_handoff,
)
from theforge.task import TaskStory as TaskSpec  # noqa: F401

from .logging import StructuredLogger
from .notify import (
    _escalate_gate_interactive,
    _escalate_gate_remote,
    _escalate_notify,
    _is_pending_file_mode,
    _is_remote_mode,
    _ntfy_done_notify,
    _pending_escalate_gate,
    _pending_human_review,
    _remote_human_review,
)
from .preflight import (
    _escalate_dev_model,
    _find_registry_key_for_profile,
    _has_persistent_p1,
    _persistent_p1_descriptions,
)
from .state import (
    CoordinatorResult,
    CoordinatorState,
    CycleHistory,
    Phase,
    ReviewCycleMetadata,
)
from .util import _fmt_duration, _log, _log_phase, _log_verbose
from .workspace import _merge_branch

_pr_log = logging.getLogger(__name__)


def _archive_story_to_done(
    story_path: str | Path,
    cwd: Path,
    *,
    commit: bool = False,
) -> bool:
    """Move a story file from backlog/ to done/ via git mv.

    Returns True if the move succeeded, False otherwise (best-effort).
    When *commit* is True a small git commit is created for the move.
    """
    src = Path(story_path)
    # Only move files that live under specs/backlog/
    try:
        rel = src.relative_to(cwd)
    except ValueError:
        # Absolute path — try making it relative
        rel = src
    parts = rel.parts
    if "backlog" not in parts:
        return False
    # Build destination: replace 'backlog' with 'done'
    idx = parts.index("backlog")
    dest_parts = parts[:idx] + ("done",) + parts[idx + 1 :]
    dest = Path(*dest_parts)
    dest_abs = cwd / dest
    dest_abs.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["git", "mv", str(rel), str(dest)],
            cwd=str(cwd),
            capture_output=True,
            timeout=15,
        )
        if proc.returncode != 0:
            _log_verbose(f"  story archive git mv failed: {proc.stderr.decode().strip()}")
            return False
        _log(f"  Archived story: {rel} → {dest}")
        if commit:
            subprocess.run(
                ["git", "commit", "-m", f"chore: archive {rel.name} to done/"],
                cwd=str(cwd),
                capture_output=True,
                timeout=15,
            )
        return True
    except Exception as exc:
        _log_verbose(f"  story archive failed: {exc}")
        return False


# ── Helpers ──────────────────────────────────────────────────────────


def _create_pr(
    config: ForgeConfig,
    task: TaskSpec,
    branch_name: str,
    parsed_review: ReviewResult,
    state: CoordinatorState,
) -> dict:
    """Create a GitHub PR via `gh pr create`. Returns a result dict.

    Best-effort: failure returns success=False with error, never raises.
    """
    p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
    p2_count = sum(1 for f in parsed_review.findings if f.severity == "P2")
    reviewer_names = ", ".join(p.name for p in config.review_pool)
    p2_findings = [f for f in parsed_review.findings if f.severity == "P2"]
    findings_md = ""
    if p2_findings:
        lines = []
        for f in p2_findings:
            loc = f" `{f.file}:{f.line}`" if f.file else ""
            lines.append(f"- **[P2]{loc}** {f.description}")
        findings_md = "\n".join(lines)
    else:
        findings_md = "_No findings._"

    pr_body = (
        f"## Summary\n\n"
        f"{parsed_review.summary}\n\n"
        f"## Review\n\n"
        f"- **Verdict:** APPROVE ({p1_count} P1, {p2_count} P2)\n"
        f"- **Reviewers:** {reviewer_names}\n"
        f"- **Cost:** ${state.total_cost:.2f}\n"
        f"- **Dev iterations:** {state.dev_iteration}\n"
        f"- **Tests:** N/A\n\n"
        f"## Findings\n\n"
        f"{findings_md}\n\n"
        f"## Story\n\n"
        f"{task.name} (`{task.story_path}`)\n\n"
        f"---\n"
        f"*Created automatically by [TheForge](https://github.com/fuzzypete/theforge)*"
    )

    pr_title = f"{task.name}"
    cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        pr_title,
        "--body",
        pr_body,
        "--base",
        config.workspace.base_branch,
        "--head",
        branch_name,
    ]
    for label in config.workspace.pr_labels:
        cmd.extend(["--label", label])
    if config.workspace.pr_draft:
        cmd.append("--draft")

    # Archive spec from backlog/ to done/ in the feature branch so the
    # merge carries the move into main.
    worktree_dir = config.workspace.path_pattern.format(slug=task.slug)
    worktree_path = config.project_root / worktree_dir
    push_cwd = worktree_path if worktree_path.is_dir() else config.project_root
    if task.story_path:
        _archive_story_to_done(task.story_path, push_cwd, commit=True)

    # Push the feature branch to origin before creating the PR.
    try:
        push_proc = subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            capture_output=True,
            text=True,
            cwd=push_cwd,
            timeout=60,
        )
        if push_proc.returncode != 0:
            err = push_proc.stderr.strip() or push_proc.stdout.strip()
            _pr_log.warning("git push failed (exit %d): %s", push_proc.returncode, err)
            return {
                "action": "pr",
                "pr_url": None,
                "success": False,
                "error": f"git push failed: {err}",
            }
    except Exception as exc:
        _pr_log.warning("git push failed: %s", exc)
        return {
            "action": "pr",
            "pr_url": None,
            "success": False,
            "error": f"git push failed: {exc}",
        }

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=push_cwd,
            timeout=60,
        )
        if proc.returncode == 0:
            pr_url = proc.stdout.strip()
            _log(f"  ✓ PR created: {pr_url}")
            return {"action": "pr", "pr_url": pr_url, "success": True, "error": None}
        else:
            err = proc.stderr.strip() or proc.stdout.strip()
            _pr_log.warning("PR creation failed (gh exited %d): %s", proc.returncode, err)
            return {"action": "pr", "pr_url": None, "success": False, "error": err}
    except Exception as exc:
        _pr_log.warning("PR creation failed: %s", exc)
        return {"action": "pr", "pr_url": None, "success": False, "error": str(exc)}


def _append_cycle_history(state: CoordinatorState, parsed_review: ReviewResult) -> None:
    """Append a CycleHistory entry for this completed review cycle (capped at 3)."""
    state.cycle_history_total += 1
    entry = CycleHistory(
        cycle=state.cycle_history_total,
        verdict=parsed_review.verdict,
        summary=parsed_review.summary,
        p1_findings=[f.description[:200] for f in parsed_review.findings if f.severity == "P1"],
    )
    state.cycle_history.append(entry)
    if len(state.cycle_history) > 3:
        state.cycle_history = state.cycle_history[-3:]


# ── State machine ────────────────────────────────────────────────────


def _finalize_approve(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    parsed_review: ReviewResult,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    auto_merge: bool,
    notify: bool,
    logger: StructuredLogger | None,
    review_cost: float,
    review_elapsed: float,
    message: str,
    run_id: str = "",
) -> CoordinatorResult:
    """Set DONE, optionally merge, log, notify, return CoordinatorResult.

    Pass logger=None to suppress merge_result/phase_end logger events (interactive paths).
    Pass logger=logger to emit them (non-interactive path).
    """
    state.phase = Phase.DONE
    merge_info: dict | None = None
    merge_suffix = ""

    # Resolve effective on_approve: CLI --auto-merge flag forces "merge"
    effective_on_approve = "merge" if auto_merge else config.workspace.on_approve

    if effective_on_approve == "merge":
        merge_info = _merge_branch(
            config.project_root,
            config.workspace.base_branch,
            branch_name,
            task.slug,
            workspace_path,
            auto_push=config.workspace.auto_push,
            config=config,
            task_name=task.name,
        )
        merge_info = dict(merge_info)
        merge_info["action"] = "merge"
        merge_suffix = (
            " Merged." if merge_info["merged"] else f" Merge failed: {merge_info['error']}"
        )
        if merge_info["merged"] and task.story_path:
            _archive_story_to_done(task.story_path, config.project_root, commit=True)
        if logger:
            logger._safe_emit(
                "merge_result",
                success=merge_info["merged"],
                branch=branch_name,
                error=merge_info.get("error"),
            )
        if merge_info["merged"] and config.hooks and config.hooks.post_merge:
            from .hooks import build_post_merge_payload
            from .hooks import run_hook as _run_hook

            _pm_payload = build_post_merge_payload(task.slug, branch_name, run_id, config)
            _run_hook(
                config.hooks.post_merge,
                _pm_payload,
                config.hooks.timeout_seconds,
                "post_merge",
                logger,
                secrets=config.secrets,
            )
    elif effective_on_approve == "pr":
        merge_info = _create_pr(config, task, branch_name, parsed_review, state)
        if merge_info["success"]:
            merge_suffix = f" PR: {merge_info['pr_url']}"
        else:
            merge_suffix = f" PR creation failed: {merge_info['error']}"
    else:
        # "none" — leave branch, log name
        _log(f"  Branch ready for manual review: {branch_name}")
        merge_info = {"action": "none", "success": True, "error": None}
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="REVIEW",
            outcome="approve",
            cost_usd=round(review_cost, 6),
            duration_s=round(review_elapsed, 2),
        )
    _task_elapsed = time.monotonic() - task_start
    _log(f"✓ DONE   total=${state.total_cost:.2f}  {_fmt_duration(_task_elapsed)}")
    _ntfy_done_notify(
        task, state, config, notify, parsed_review.summary, _task_elapsed, branch_name
    )
    return CoordinatorResult(
        success=True,
        phase=state.phase,
        state=state,
        message=f"{message}Branch: {branch_name}{merge_suffix}",
        merge=merge_info,
    )


def _build_reviewer_verdicts(state: CoordinatorState) -> dict[str, str]:
    """Build a profile_name → verdict dict from the last cycle's reviewer results."""
    verdicts: dict[str, str] = {}
    for name, rr in state.last_cycle_reviewer_results:
        verdicts[name] = rr.verdict
    # Fill in FAIL for reviewers that appear in the last cycle metadata but not in named_parsed
    if state.review_cycle_metadata:
        last_meta = state.review_cycle_metadata[-1]
        for failed_name in last_meta.failed:
            if failed_name not in verdicts:
                verdicts[failed_name] = "FAIL"
    return verdicts


def _run_escalate_gate(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    auto_merge: bool,
    notify: bool,
    logger: "StructuredLogger | None",
    run_id: str = "",
) -> "CoordinatorResult | None":
    """HITL decision gate at review-related ESCALATE exit points.

    Returns:
        CoordinatorResult — approve or reject decision (caller should return it).
        None — continue decision (caller should reset phase to REVIEW, decrement
               review_cycle by 1 if it was already incremented, and loop).
    """
    import sys

    escalate_policy = config.retry.escalate_policy
    reviewer_verdicts = _build_reviewer_verdicts(state)
    gate_result: str | None = None
    if state.gate_decisions:
        gate_result = state.gate_decisions[-1]

    escalate_reason = state.error or "ESCALATE"

    def _make_escalate_result() -> CoordinatorResult:
        state.escalate_decision = "reject"
        state.escalate_reason = escalate_reason
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error or escalate_reason,
        )

    # Policy: reject — preserve current behavior without prompting
    if escalate_policy == "reject":
        state.escalate_decision = "reject"
        state.escalate_reason = escalate_reason
        return _make_escalate_result()

    # Policy: auto_approve — short-circuit when gate passed and majority approved
    if escalate_policy == "auto_approve":
        if state.review_results:
            approve_count = sum(1 for v in reviewer_verdicts.values() if v == "APPROVE")
            total_count = max(len(reviewer_verdicts), 1)
            majority_approved = approve_count > total_count / 2
            gate_passed = gate_result is not None and "PASS" in gate_result.upper()
            if majority_approved and gate_passed:
                _log(
                    f"  auto_approve: {approve_count}/{total_count} reviewers APPROVE"
                    f" + gate PASS — approving"
                )
                state.escalate_decision = "approve"
                state.escalate_reason = escalate_reason
                _append_cycle_history(state, state.review_results[-1])
                return _finalize_approve(
                    state,
                    config,
                    task,
                    state.review_results[-1],
                    workspace_path,
                    branch_name,
                    task_start,
                    auto_merge=auto_merge,
                    notify=notify,
                    logger=logger,
                    review_cost=state.total_review_cost,
                    review_elapsed=0.0,
                    message=(
                        f"Task '{task.name}' completed. "
                        f"Human auto-approved via escalate gate "
                        f"after {state.review_cycle} cycle(s). "
                    ),
                    run_id=run_id,
                )

    # Determine interaction method
    if _is_pending_file_mode(notify, config):
        decision = _pending_escalate_gate(
            state, task, config, escalate_reason, reviewer_verdicts, gate_result, run_id=run_id
        )
    elif _is_remote_mode(notify, config):
        decision = _escalate_gate_remote(
            state, task, config, escalate_reason, reviewer_verdicts, gate_result
        )
    elif sys.stdin.isatty():
        decision = _escalate_gate_interactive(
            state, escalate_reason, reviewer_verdicts, gate_result
        )
    else:
        # No interaction method available — fall through to reject
        _log("  No interaction method available for escalate gate — rejecting (policy=prompt)")
        decision = "reject"

    state.escalate_reason = escalate_reason

    if decision == "approve":
        if not state.review_results:
            _log("  ⚠ Approve requested but no review results available — rejecting instead")
            state.escalate_decision = "reject"
            return _make_escalate_result()
        state.escalate_decision = "approve"
        _append_cycle_history(state, state.review_results[-1])
        return _finalize_approve(
            state,
            config,
            task,
            state.review_results[-1],
            workspace_path,
            branch_name,
            task_start,
            auto_merge=auto_merge,
            notify=notify,
            logger=None,
            review_cost=state.total_review_cost,
            review_elapsed=0.0,
            message=(
                f"Task '{task.name}' completed. "
                f"Human approved via escalate gate after {state.review_cycle} cycle(s). "
            ),
            run_id=run_id,
        )

    if decision == "continue":
        state.escalate_decision = "continue"
        _log("  Escalate gate: continue — granting one more review cycle")
        state.phase = Phase.REVIEW
        return None

    # reject or any unrecognised decision
    state.escalate_decision = "reject"
    return _make_escalate_result()


class _ReviewOutcome(Enum):
    DONE = auto()
    ESCALATE = auto()
    RETRY_DEV = auto()


def _run_review_phase(
    state: CoordinatorState,
    config: ForgeConfig,
    task: TaskSpec,
    story_content: str,
    workspace_path: Path,
    branch_name: str,
    task_start: float,
    *,
    interactive: bool,
    auto_merge: bool,
    notify: bool,
    logger: StructuredLogger | None,
    mod: ModuleType,
    run_id: str = "",
) -> tuple[_ReviewOutcome, CoordinatorResult | None, ForgeConfig]:
    """Run the full REVIEW phase: pool+synthesis, parse retries, verdict handling.

    Returns (outcome, result_or_none, possibly_updated_config).
    DONE/ESCALATE → result is a CoordinatorResult.
    RETRY_DEV → result is None, caller loops back to DEV.
    config is returned because persistent-P1 model escalation may replace it.
    """
    state.phase = Phase.REVIEW
    if logger:
        logger._safe_emit("phase_start", phase="REVIEW", iteration=state.review_cycle + 1)
    max_parse_retries = config.retry.max_review_parse_retries
    _review_pool_start = time.monotonic()
    _pool_model_names = "+".join(p.model for p in config.review_pool)
    _log_phase(state.phase, f"{_pool_model_names}  cycle={state.review_cycle + 1}")

    meta = ReviewCycleMetadata(
        pool_models=[p.name for p in config.review_pool],
        successful=[],
        failed=[],
        synthesized=False,
        parse_retries=0,
    )
    state.review_cycle_metadata.append(meta)
    _review_cost_before_cycle = sum(r.cost_usd or 0.0 for r in state.review_agent_results)

    successful, failed_results, _candidate, _individual_results, _named_parsed = (
        mod._run_review_pool(
            state,
            config,
            task,
            story_content,
            workspace_path,
            branch_name,
            meta,
            notify=notify,
            pool_attempt=0,
            max_review_parse_retries=max_parse_retries,
        )
    )
    state.last_cycle_reviewer_results = _named_parsed

    if _candidate is None:
        # All reviewers failed or budget exceeded —
        # state.error already set by _run_review_pool
        _gate_result = _run_escalate_gate(
            state,
            config,
            task,
            workspace_path,
            branch_name,
            task_start,
            auto_merge=auto_merge,
            notify=notify,
            logger=logger,
            run_id=run_id,
        )
        if _gate_result is not None:
            return _ReviewOutcome.ESCALATE, _gate_result, config
        # Gate said "continue" — re-enter REVIEW (review_cycle not incremented here)
        return _ReviewOutcome.RETRY_DEV, None, config

    parsed_review = _candidate

    # ── Graceful empty-merge fallback ─────────────────────────────────
    if parsed_review.parse_errors:
        _log(
            f"  ⚠ review merge produced parse errors — falling back to best individual result "
            f"({len(_individual_results)} reviewer(s) with valid output)"
        )
        _fallback = _best_individual_result(_individual_results)
        if _fallback is not None:
            _log(f"  ↩ using best individual result: {_fallback.verdict}")
            parsed_review = _fallback
        else:
            _log(
                "  ⚠ all reviewers failed to produce usable output — "
                "injecting synthetic P1, returning REQUEST_CHANGES"
            )
            parsed_review = ReviewResult(
                verdict="REQUEST_CHANGES",
                summary="Review pool failed to produce a usable verdict",
                findings=[
                    ReviewFinding(
                        severity="P1",
                        file="",
                        line=None,
                        description=(
                            "All reviewers failed to produce parseable output. "
                            "Manual review required."
                        ),
                        suggestion="Check reviewer logs for details.",
                    )
                ],
                story_matches=False,
                story_mismatches=[],
                test_adequate=False,
                test_gaps=[],
                parse_errors=[],
                raw_yaml={},
            )

    # Valid verdict — increment review cycle counter
    state.review_cycle += 1
    state.review_results.append(parsed_review)

    _review_elapsed = time.monotonic() - _review_pool_start
    _p1_count = sum(1 for f in parsed_review.findings if f.severity == "P1")
    _p2_count = sum(1 for f in parsed_review.findings if f.severity == "P2")
    _review_cost = (
        sum(r.cost_usd or 0.0 for r in state.review_agent_results) - _review_cost_before_cycle
    )

    # ── Finding classification ─────────────────────────────────────────
    # Call classifier for every cycle (cycle 1 finds are all net_new as baseline).
    # Cycle 1 uses the traditional exit rule (any P1 blocks) because there is no
    # prior registry to determine which findings are latent vs. real regressions.
    # Cycle 2+: disposition-gated logic — only unresolved/regression/corroborated_new block.
    _classified = _fc.update_finding_registry(
        state=state,
        cycle_results=state.last_cycle_reviewer_results,
        workspace_path=workspace_path,
        cycle_num=state.review_cycle,
        prev_commit=state.last_dev_start_commit,
    )
    if state.review_cycle >= 2:
        _blocking_p1 = _fc.has_blocking_p1(_classified)
        _nonblocking_p1s = _fc.net_new_p1s(_classified)
        # Fallback: if the merged review has P1s but none were classified (e.g., synthetic
        # P1 injection when all reviewers failed to produce parseable output), block
        # traditionally to avoid silently passing an unknown failure.
        if not _blocking_p1 and not _nonblocking_p1s and _p1_count > 0:
            _blocking_p1 = True
    else:
        # Cycle 1: any P1 is blocking (no prior baseline to classify against)
        _blocking_p1 = _p1_count > 0
        _nonblocking_p1s = []

    _log(f"  Summary: {parsed_review.summary}")
    # Log findings grouped by severity
    _findings_by_sev: dict[str, list] = {}
    for _f in parsed_review.findings:
        _findings_by_sev.setdefault(_f.severity, []).append(_f)
    for _sev in sorted(_findings_by_sev):
        for _f in _findings_by_sev[_sev]:
            _loc = f" [{_f.file}:{_f.line}]" if _f.file else ""
            _log(f"  [{_sev}]{_loc} {_f.description}")
    if logger:
        logger._safe_emit(
            "review_result",
            verdict=parsed_review.verdict,
            p1_count=_p1_count,
            p2_count=_p2_count,
            cost_usd=round(_review_cost, 6),
        )

    # ── APPROVE (or disposition-gated pass) ─────────────────────────
    # The coordinator makes the blocking decision independently of the synthesized verdict.
    # If the synthesized verdict is REQUEST_CHANGES but all P1s are net_new (single-reviewer,
    # not in changed files, not previously raised), we treat the cycle as passing.
    # Net-new P1s are recorded in the audit trail but do not block.
    _effective_approve = parsed_review.verdict == "APPROVE" or (
        parsed_review.verdict == "REQUEST_CHANGES" and not _blocking_p1
    )
    if _effective_approve and _nonblocking_p1s:
        _nb_descs = "; ".join(r.description[:80] for r in _nonblocking_p1s)
        _log(f"  ↷ {len(_nonblocking_p1s)} net-new P1(s) recorded but not blocking: {_nb_descs}")

    if _effective_approve:
        _verdict_label = (
            "APPROVE" if parsed_review.verdict == "APPROVE" else "REQUEST_CHANGES→net_new_pass"
        )
        _log(
            f"  ✓ REVIEW   {_verdict_label}  {_p1_count} P1  {_p2_count} P2"
            f"  ${_review_cost:.2f}  {_fmt_duration(_review_elapsed)}"
        )
        if interactive:
            state.phase = Phase.HUMAN_REVIEW
            _log_phase(state.phase)
            if _is_pending_file_mode(notify, config):
                decision, feedback = _pending_human_review(
                    state,
                    parsed_review,
                    workspace_path,
                    branch_name,
                    task,
                    config,
                    task_start,
                    run_id=run_id,
                )
            elif _is_remote_mode(notify, config):
                decision, feedback = _remote_human_review(
                    state, parsed_review, workspace_path, branch_name, task, config, task_start
                )
            else:
                decision, feedback = mod._human_review(
                    state, parsed_review, workspace_path, branch_name
                )
            state.human_review_decision = decision
            state.human_review_feedback = feedback
            if decision == "approve":
                _append_cycle_history(state, parsed_review)
                return (
                    _ReviewOutcome.DONE,
                    _finalize_approve(
                        state,
                        config,
                        task,
                        parsed_review,
                        workspace_path,
                        branch_name,
                        task_start,
                        auto_merge=auto_merge,
                        notify=notify,
                        logger=None,
                        review_cost=_review_cost,
                        review_elapsed=_review_elapsed,
                        message=(
                            f"Task '{task.name}' completed. "
                            f"Human approved after {state.review_cycle} cycle(s), "
                            f"{state.dev_iteration} dev iteration(s). "
                        ),
                        run_id=run_id,
                    ),
                    config,
                )
            if decision in ("escalate", "timeout"):
                state.phase = Phase.ESCALATE
                state.error = (
                    "Remote review timed out — auto-escalated."
                    if decision == "timeout"
                    else "Human chose to escalate after APPROVE."
                )
                _log(f"✗ ESCALATE   {state.error}")
                _escalate_notify(task, state, notify, config)
                return (
                    _ReviewOutcome.ESCALATE,
                    CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    ),
                    config,
                )
            if decision == "extend":
                _append_cycle_history(state, parsed_review)
                state.dev_iteration = 0
                state.review_cycle = 0
                state.human_review_extra_cycles += 1
                state.last_review_findings = (
                    review_to_dev_handoff(parsed_review) if parsed_review.findings else None
                )
                state.human_feedback = None
                state.retry_reason = "extend"
                _log(
                    f"Human extended — granting fresh budget "
                    f"(extra_cycles={state.human_review_extra_cycles})"
                )
                return _ReviewOutcome.RETRY_DEV, None, config
            # decision == "reject"
            _append_cycle_history(state, parsed_review)
            state.human_feedback = feedback
            state.last_review_findings = None
            state.retry_reason = "reject"
            state.dev_iteration = 0
            _log("Human rejected — looping back to dev with feedback")
            return _ReviewOutcome.RETRY_DEV, None, config
        else:
            _append_cycle_history(state, parsed_review)
            return (
                _ReviewOutcome.DONE,
                _finalize_approve(
                    state,
                    config,
                    task,
                    parsed_review,
                    workspace_path,
                    branch_name,
                    task_start,
                    auto_merge=auto_merge,
                    notify=notify,
                    logger=logger,
                    review_cost=_review_cost,
                    review_elapsed=_review_elapsed,
                    message=(
                        f"Task '{task.name}' completed. "
                        f"Review approved after {state.review_cycle} cycle(s), "
                        f"{state.dev_iteration} dev iteration(s). "
                    ),
                    run_id=run_id,
                ),
                config,
            )

    # ── REQUEST_CHANGES (blocking P1s present) ───────────────────
    _is_persistent_p1 = False
    if config.smart_config_models is not None and len(state.review_results) >= 2:
        _prev_result = state.review_results[-2]
        _is_persistent_p1 = _has_persistent_p1(parsed_review.findings, _prev_result.findings)

    _persistent_tag = " (persistent)" if _is_persistent_p1 else ""
    _log(
        f"  ✗ REVIEW   REQUEST_CHANGES  {_p1_count} P1  {_p2_count} P2{_persistent_tag}"
        f"  ${_review_cost:.2f}  {_fmt_duration(_review_elapsed)}"
    )

    # Escalate dev model on persistent P1
    if (
        _is_persistent_p1
        and not state.dev_escalated
        and (state.total_dev_cost < config.dev_profile.budget_usd)
    ):
        _curr_key = _find_registry_key_for_profile(config.dev_profile)
        if _curr_key is not None:
            _next_key = _escalate_dev_model(_curr_key, config.smart_config_models)
            if _next_key is not None:
                _next_info = MODEL_REGISTRY[_next_key]
                _p1_file = next(
                    (f.file for f in parsed_review.findings if f.severity == "P1"),
                    "unknown",
                )
                _log(
                    f"  Dev escalation: {config.dev_profile.model} → {_next_info.model}"
                    f" (persistent P1 in {_p1_file})"
                )
                _old_model = config.dev_profile.model
                _new_dev = _dc_replace(
                    config.dev_profile, cli=_next_info.cli, model=_next_info.model
                )
                config = _dc_replace(config, dev_profile=_new_dev)
                state.dev_escalated = True
                _prev_result = state.review_results[-2]
                _persistent_descs = _persistent_p1_descriptions(
                    parsed_review.findings, _prev_result.findings
                )
                state.escalation_note = (
                    f"MODEL ESCALATION: A P1 finding persisted across review cycles. "
                    f"The previous model ({_old_model}) was unable to resolve it. "
                    f"You are now running on an upgraded model ({_next_info.model}). "
                    f"Persistent finding(s): {'; '.join(_persistent_descs)}"
                )

    if state.review_cycle >= config.retry.max_review_cycles:
        if interactive:
            state.phase = Phase.HUMAN_REVIEW
            _log_phase(state.phase, "cycles exhausted")
            if _is_pending_file_mode(notify, config):
                decision, feedback = _pending_human_review(
                    state,
                    parsed_review,
                    workspace_path,
                    branch_name,
                    task,
                    config,
                    task_start,
                    run_id=run_id,
                )
            elif _is_remote_mode(notify, config):
                decision, feedback = _remote_human_review(
                    state, parsed_review, workspace_path, branch_name, task, config, task_start
                )
            else:
                decision, feedback = mod._human_review(
                    state, parsed_review, workspace_path, branch_name
                )
            state.human_review_decision = decision
            state.human_review_feedback = feedback
            if decision == "approve":
                _append_cycle_history(state, parsed_review)
                return (
                    _ReviewOutcome.DONE,
                    _finalize_approve(
                        state,
                        config,
                        task,
                        parsed_review,
                        workspace_path,
                        branch_name,
                        task_start,
                        auto_merge=auto_merge,
                        notify=notify,
                        logger=None,
                        review_cost=_review_cost,
                        review_elapsed=_review_elapsed,
                        message=(
                            f"Task '{task.name}' completed. "
                            f"Human approved after {state.review_cycle} cycle(s). "
                        ),
                        run_id=run_id,
                    ),
                    config,
                )
            if decision in ("escalate", "timeout"):
                state.phase = Phase.ESCALATE
                state.error = (
                    "Remote review timed out — auto-escalated."
                    if decision == "timeout"
                    else "Human chose to escalate after exhausted cycles."
                )
                _log(f"✗ ESCALATE   {state.error}")
                _escalate_notify(task, state, notify, config)
                return (
                    _ReviewOutcome.ESCALATE,
                    CoordinatorResult(
                        success=False,
                        phase=state.phase,
                        state=state,
                        message=state.error,
                    ),
                    config,
                )
            if decision == "extend":
                _append_cycle_history(state, parsed_review)
                state.dev_iteration = 0
                state.review_cycle = 0
                state.human_review_extra_cycles += 1
                state.last_review_findings = review_to_dev_handoff(parsed_review)
                state.human_feedback = None
                state.retry_reason = "extend"
                _log(
                    f"Human extended — granting fresh budget "
                    f"(extra_cycles={state.human_review_extra_cycles})"
                )
                return _ReviewOutcome.RETRY_DEV, None, config
            # decision == "reject" — cycles exhausted: treat as extend + reject
            _append_cycle_history(state, parsed_review)
            state.dev_iteration = 0
            state.review_cycle = 0
            state.human_review_extra_cycles += 1
            state.human_feedback = feedback
            state.last_review_findings = None
            state.retry_reason = "reject"
            _log(
                "Human rejected (cycles exhausted) — granting fresh budget "
                f"(extra_cycles={state.human_review_extra_cycles})"
            )
            return _ReviewOutcome.RETRY_DEV, None, config
        else:
            state.phase = Phase.ESCALATE
            state.error = (
                f"Review requested changes after {state.review_cycle} cycles. "
                f"Max cycles ({config.retry.max_review_cycles}) exhausted."
            )
            _log(f"✗ ESCALATE   {state.error}")
            if logger:
                logger._safe_emit("phase_end", phase="REVIEW", outcome="escalate")
                logger._safe_emit("escalate", reason=state.error, phase="REVIEW")
            _gate_result = _run_escalate_gate(
                state,
                config,
                task,
                workspace_path,
                branch_name,
                task_start,
                auto_merge=auto_merge,
                notify=notify,
                logger=logger,
                run_id=run_id,
            )
            if _gate_result is not None:
                return _ReviewOutcome.ESCALATE, _gate_result, config
            # Gate said "continue" — undo review_cycle increment so next cycle is valid
            state.review_cycle -= 1
            return _ReviewOutcome.RETRY_DEV, None, config

    # Within budget — feed findings back to dev
    if logger:
        logger._safe_emit(
            "phase_end",
            phase="REVIEW",
            outcome="request_changes",
            cost_usd=round(_review_cost, 6),
            duration_s=round(_review_elapsed, 2),
        )
    _append_cycle_history(state, parsed_review)
    state.last_review_findings = review_to_dev_handoff(parsed_review)
    state.dev_iteration = 0
    state.human_feedback = None
    state.retry_reason = "review_changes"
    _log_verbose(f"Sending {len(parsed_review.findings)} findings back to dev agent")
    return _ReviewOutcome.RETRY_DEV, None, config
