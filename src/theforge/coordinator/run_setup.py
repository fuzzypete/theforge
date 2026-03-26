"""Shared setup logic for resume entry points (run_from_review, run_from_dev).

Provides _setup_resume_entry which initialises coordinator state, structured
logging, and session restoration for entry points that reuse an existing
worktree instead of creating one from scratch.
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path

from theforge.config import ForgeConfig
from theforge.sessions import load_sessions
from theforge.task import TaskSpec
from theforge.task import load_story as load_spec

from . import util as _cu
from .logging import StructuredLogger
from .notify import _escalate_notify
from .state import CoordinatorResult, CoordinatorState, Phase


def _setup_resume_entry(
    config: ForgeConfig,
    task: TaskSpec,
    workspace_path: Path,
    *,
    initial_phase: Phase,
    notify: bool,
    run_id: str | None,
) -> tuple[CoordinatorState, StructuredLogger, str, str, float] | CoordinatorResult:
    """Shared setup for run_from_review / run_from_dev.

    Returns (state, logger, branch_name, story_content, task_start) on success,
    or a CoordinatorResult on failure (worktree missing).
    """
    state = CoordinatorState(
        phase=initial_phase,
        dev_iteration=0,
        review_cycle=0,
        preflight_verdict="SKIPPED",
    )
    state.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _task_start = time.monotonic()

    _run_id = run_id or _cu._generate_run_id()
    logger = StructuredLogger(
        run_id=_run_id,
        project=config.project,
        task=task.slug,
        log_file=config.log.log_file,
        enabled=config.log.enabled,
        project_root=config.project_root,
    )
    logger._safe_emit(
        "run_start",
        specs=[str(task.story_path)],
        budget_usd=config.dev_profile.budget_usd,
        resume=True,
    )

    if not workspace_path.exists():
        state.phase = Phase.ESCALATE
        state.error = f"Worktree not found at {workspace_path}. Run `forge run` first."
        logger._safe_emit("escalate", reason=state.error, phase="INIT")
        logger._safe_emit("run_end", outcome="escalate", total_cost_usd=0.0, total_duration_s=0.0)
        _escalate_notify(task, state, notify, config)
        return CoordinatorResult(
            success=False,
            phase=state.phase,
            state=state,
            message=state.error,
        )

    state.workspace_path = workspace_path

    # Restore session IDs from prior run if available
    _sessions = load_sessions(workspace_path)
    if _sessions.get("dev_session_id"):
        state.dev_session_id = _sessions["dev_session_id"]
    if _sessions.get("reviewer_session_ids"):
        state.reviewer_session_ids = _sessions["reviewer_session_ids"]
    if _sessions.get("plan_review_session_ids"):
        state.plan_review_session_ids = _sessions["plan_review_session_ids"]

    # Resolve branch name from actual worktree HEAD
    _ok_branch, _branch_out = _cu._run_shell("git rev-parse --abbrev-ref HEAD", workspace_path)
    if _ok_branch and _branch_out.strip() and _branch_out.strip() != "HEAD":
        branch_name = _branch_out.strip()
    else:
        branch_name = config.workspace.branch_pattern.format(slug=task.slug)
    state.branch_name = branch_name

    story_content = load_spec(task.story_path)

    return state, logger, branch_name, story_content, _task_start
