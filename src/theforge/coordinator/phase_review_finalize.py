"""REVIEW phase — approve finalization, story archival, and cycle-history tracking."""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.review import ReviewResult
from theforge.task import TaskStory as TaskSpec  # noqa: F401

from .logging import StructuredLogger
from .notify import _ntfy_done_notify
from .state import CoordinatorResult, CoordinatorState, CycleHistory, Phase
from .util import _fmt_duration, _log, _log_verbose
from .workspace import _merge_branch

_log_finalize = logging.getLogger(__name__)


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
    from .phase_review_pr import _create_pr

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
