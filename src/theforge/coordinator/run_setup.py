"""Shared setup logic for resume entry points (run_from_review, run_from_dev).

Provides _setup_resume_entry which initialises coordinator state, structured
logging, and session restoration for entry points that reuse an existing
worktree instead of creating one from scratch.
"""

from __future__ import annotations

import datetime
import logging
import shutil
import time
from pathlib import Path

import yaml

from theforge.config import ForgeConfig
from theforge.sessions import load_sessions
from theforge.task import TaskStory, load_story

from . import util as _cu
from .logging import StructuredLogger
from .notify import _escalate_notify
from .state import CoordinatorResult, CoordinatorState, Phase

_logger = logging.getLogger(__name__)


def save_trajectory_state(workspace_path: Path, state: CoordinatorState) -> None:
    """Persist trajectory fields to <workspace_path>/.forge/trajectory.yaml.

    Called after each review cycle classification so the trajectory survives
    a ``forge run --resume``.
    """
    sidecar = workspace_path / ".forge" / "trajectory.yaml"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "trajectory_cycle": state.trajectory_cycle,
        "finding_trajectory": state.finding_trajectory,
        "review_cycle_findings": [
            [cycle_num, findings] for cycle_num, findings in state.review_cycle_findings
        ],
        "surviving_families": state.surviving_families,
    }
    sidecar.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")


def load_trajectory_state(workspace_path: Path, state: CoordinatorState) -> None:
    """Restore trajectory fields from <workspace_path>/.forge/trajectory.yaml.

    Called in ``_setup_resume_entry`` so post-resume trajectory numbering
    continues from the correct baseline.  Missing or corrupt sidecar files are
    handled gracefully — state fields remain at their defaults.
    """
    sidecar = workspace_path / ".forge" / "trajectory.yaml"
    if not sidecar.exists():
        return

    try:
        raw = sidecar.read_text(encoding="utf-8")
        if not raw.strip():
            return
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            _logger.warning("trajectory.yaml: expected a dict, got %s — ignoring", type(data))
            return
    except Exception as exc:  # noqa: BLE001
        _logger.warning("trajectory.yaml: failed to parse (%s) — ignoring", exc)
        return

    if "trajectory_cycle" in data:
        state.trajectory_cycle = int(data["trajectory_cycle"])
    if "finding_trajectory" in data and isinstance(data["finding_trajectory"], list):
        state.finding_trajectory = data["finding_trajectory"]
    if "review_cycle_findings" in data and isinstance(data["review_cycle_findings"], list):
        state.review_cycle_findings = [
            (int(entry[0]), list(entry[1]))
            for entry in data["review_cycle_findings"]
            if isinstance(entry, (list, tuple)) and len(entry) == 2
        ]
    if "surviving_families" in data and isinstance(data["surviving_families"], list):
        state.surviving_families = data["surviving_families"]


def _setup_resume_entry(
    config: ForgeConfig,
    task: TaskStory,
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

    # Restore trajectory data from sidecar (survives --resume)
    load_trajectory_state(workspace_path, state)

    # Sync forge.yaml from project root into the worktree
    _forge_yaml_src = config.project_root / "forge.yaml"
    _forge_yaml_dst = workspace_path / "forge.yaml"
    if _forge_yaml_src.exists():
        shutil.copy2(_forge_yaml_src, _forge_yaml_dst)
        _cu._log(f"  ↺ RESUME   synced forge.yaml from {_forge_yaml_src}")

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

    story_content = load_story(task.story_path)

    return state, logger, branch_name, story_content, _task_start
